"""
云桥 × EasyTier 组网集成 — 客户端管理模块
=============================================
职责：
  1. 自动下载并缓存 easytier-core（Windows x86_64，平台匹配）
  2. 用服务器下发的组网配置（网络名/密钥/IP）启动 no-tun 节点
  3. 探测组网通道是否可用（连服务器组网 IP + 云桥端口）
  4. 提供"组网地址"供 agent.py 切换主通道

设计原则：
  - no-tun 模式：不创建虚拟网卡，免管理员权限、免驱动，用户无感
  - 全程后台：用户只看到"连接"开关，组网配置自动生成
  - 主备切换：组网可用走组网，失败回退公网（由 agent.py 决策）
"""

import json
import os
import platform
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

# ─── 常量 ────────────────────────────────────
EASYTIER_VERSION = "v2.6.4"
EASYTIER_BASE = "https://gh-proxy.com/https://github.com/EasyTier/EasyTier/releases/download"
# 平台 → 下载文件名
_PLATFORM_MAP = {
    ("Windows", "AMD64"): "easytier-windows-x86_64-v2.6.4.zip",
    ("Linux", "x86_64"): "easytier-linux-x86_64-v2.6.4.zip",
    ("Darwin", "arm64"): "easytier-macos-aarch64-v2.6.4.zip",
}
_MESH_DIR_NAME = "easytier"
# 服务器组网 IP（hub 固定用 .1，客户端 DHCP 分配）
SERVER_MESH_IP = "10.144.144.1"
# 云桥服务端口（组网通道上也用同一端口）
CLOUD_BRIDGE_PORT = 9876


def _platform_key():
    return (platform.system(), platform.machine())


def _cache_dir():
    """easytier 缓存目录：先找仓库内置(随云桥客户端一起更新)，否则 ~/.yunqiao/easytier/。"""
    # 仓库内置：随云桥客户端一起 pull 更新（无感，无需下载）
    repo_dir = Path(__file__).parent / "easytier"
    if repo_dir.exists():
        return repo_dir
    base = Path(os.environ.get("YUNQIAO_CONFIG", str(Path.home() / ".yunqiao")))
    d = base / _MESH_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _core_path():
    exe = "easytier-core.exe" if platform.system() == "Windows" else "easytier-core"
    return _cache_dir() / exe


def _cli_path():
    exe = "easytier-cli.exe" if platform.system() == "Windows" else "easytier-cli"
    return _cache_dir() / exe


def is_installed():
    return _core_path().exists()


def _download_url():
    key = _platform_key()
    fname = _PLATFORM_MAP.get(key)
    if not fname:
        raise RuntimeError(f"EasyTier 暂不支持平台: {key}")
    return f"{EASYTIER_BASE}/{EASYTIER_VERSION}/{fname}"


def install(progress_cb=None):
    """下载并解压 easytier-core 到缓存目录。返回是否成功。"""
    if is_installed():
        return True
    url = _download_url()
    # 多源尝试：云桥服务器(国内快)优先 → gh-proxy → 官方直连
    base = "https://yunqiao.very.im/easytier/easytier-win.zip"
    candidates = [
        base,  # 云桥服务器(最快)
        url,  # gh-proxy
        url.replace("https://gh-proxy.com/https://github.com", "https://github.com"),  # 官方
    ]
    tmp_zip = _cache_dir() / "easytier.zip"
    last_err = None
    for u in candidates:
        try:
            if progress_cb:
                progress_cb(f"下载 EasyTier {EASYTIER_VERSION}...")
            # 带超时 + 校验非空
            import urllib.request
            req = urllib.request.Request(u, headers={"User-Agent": "yunqiao-client"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            if len(data) < 1000000:
                raise RuntimeError(f"下载不完整({len(data)}B)")
            tmp_zip.write_bytes(data)
            break
        except Exception as e:
            last_err = e
            if progress_cb:
                progress_cb(f"下载源不可用({u[:60]}...): {e}")
            continue
    else:
        if progress_cb:
            progress_cb(f"EasyTier 下载失败: {last_err}")
        return False
    if progress_cb:
        progress_cb("解压中...")
    try:
        import zipfile
        with zipfile.ZipFile(tmp_zip) as z:
            z.extractall(_cache_dir())
        # 找到解压出的二进制并移到缓存根
        for root, _, files in os.walk(_cache_dir()):
            for f in files:
                if f in ("easytier-core.exe", "easytier-core", "easytier-cli.exe", "easytier-cli"):
                    src = os.path.join(root, f)
                    dst = os.path.join(_cache_dir(), f)
                    if src != dst and os.path.exists(src):
                        shutil.move(src, dst)
        tmp_zip.unlink(missing_ok=True)
        if progress_cb:
            progress_cb("EasyTier 就绪")
        return is_installed()
    except Exception as e:
        if progress_cb:
            progress_cb(f"解压失败: {e}")
        return False


def build_node_command(mesh_config):
    """根据组网配置构建客户端节点启动命令（no-tun）。

    mesh_config: {networkName, networkSecret, ipv4, serverIp}
    """
    cmd = [
        str(_core_path()),
        "--network-name", mesh_config["networkName"],
        "--network-secret", mesh_config["networkSecret"],
        "--dhcp", "true" if not mesh_config.get("ipv4") else "false",
    ]
    if mesh_config.get("ipv4"):
        cmd += ["-i", mesh_config["ipv4"]]
    # 连接服务器 hub（TCP 11010）
    server_ip = mesh_config.get("serverIp", SERVER_MESH_IP)
    # 服务器公网 IP 或组网 IP，客户端通过公网 11010 连 hub 入网
    cmd += ["-p", f"tcp://{server_ip}:11010"]
    # no-tun：不建虚拟网卡，只需逻辑组网身份
    cmd += ["--no-tun"]
    return cmd


def start_node(mesh_config, log_path=None, progress_cb=None):
    """启动 easytier 客户端节点（no-tun）。返回 subprocess.Popen 句柄。"""
    cmd = build_node_command(mesh_config)
    if progress_cb:
        progress_cb("启动组网节点...")
    logf = open(log_path or (_cache_dir() / "easytier.log"), "a", encoding="utf-8")
    # Windows 隐藏窗口；POSIX 丢到后台
    creationflags = 0
    if platform.system() == "Windows":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        cmd,
        stdout=logf,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    return proc


def probe_mesh_channel(timeout=3):
    """探测组网通道：尝试连 服务器组网IP:云桥端口。
    返回 True 表示组网通道可用（客户端已入网且能达服务器组网 IP）。"""
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        rc = s.connect_ex((SERVER_MESH_IP, CLOUD_BRIDGE_PORT))
        return rc == 0
    except Exception:
        return False
    finally:
        s.close()


def stop_node(proc):
    """停止 easytier 节点。"""
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# ─── 配置持久化 ──────────────────────────────
def load_mesh_config(config_dir=None):
    """从 config.json 读取组网配置。"""
    base = Path(config_dir or os.environ.get("YUNQIAO_CONFIG", str(Path.home() / ".yunqiao")))
    cfg_file = base / "config.json"
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text("utf-8"))
            return cfg.get("mesh")
        except Exception:
            return None
    return None


def save_mesh_config(mesh, config_dir=None):
    """保存组网配置到 config.json。"""
    base = Path(config_dir or os.environ.get("YUNQIAO_CONFIG", str(Path.home() / ".yunqiao")))
    cfg_file = base / "config.json"
    cfg = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text("utf-8"))
        except Exception:
            cfg = {}
    cfg["mesh"] = mesh
    cfg_file.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")