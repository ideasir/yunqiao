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


def _find_installed_gui():
    """多路径查找官网安装的 easytier-gui.exe（不同机器安装位置不同）。
    返回 Path 或 None。"""
    if platform.system() != "Windows":
        return None
    candidates = []
    # 1. LOCALAPPDATA（默认安装位置）
    la = os.environ.get("LOCALAPPDATA", "")
    if la:
        candidates.append(Path(la) / "easytier-gui" / "easytier-gui.exe")
    # 2. USERPROFILE/.easytier / .easytier-gui
    hp = os.environ.get("USERPROFILE", str(Path.home()))
    for sub in ("easytier-gui", ".easytier-gui"):
        candidates.append(Path(hp) / "AppData" / "Local" / sub / "easytier-gui.exe")
        candidates.append(Path(hp) / sub / "easytier-gui.exe")
    # 3. Program Files / Program Files (x86)
    for pf in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        if pf:
            candidates.append(Path(pf) / "easytier" / "easytier-gui.exe")
            candidates.append(Path(pf) / "easytier-gui" / "easytier-gui.exe")
    for c in candidates:
        try:
            if c.is_file():
                return c
        except Exception:
            continue
    return None


def _core_path():
    """优先借用官网安装的 easytier-gui.exe（自带运行库能跑、还带 UI 显示节点），
    否则用云桥仓库内置的 easytier-core.exe（随客户端更新，自带 DLL）。"""
    gui = _find_installed_gui()
    if gui:
        return gui
    return _cache_dir() / ("easytier-core.exe" if platform.system() == "Windows" else "easytier-core")


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


def cleanup_stale_nodes():
    """清理旧的 easytier-core 进程（防客户端重启后节点越积越多）。
    只杀 easytier-core（云桥管理的节点），不碰 easytier-gui（用户自己的）。"""
    if platform.system() != "Windows":
        return 0
    import subprocess as sp
    try:
        r = sp.run(["tasklist", "/FI", "IMAGENAME eq easytier-core.exe", "/FO", "CSV"],
                   capture_output=True, timeout=15)
        killed = 0
        for line in r.stdout.decode("utf-8", errors="replace").splitlines()[1:]:
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 2 and parts[0].lower() == "easytier-core.exe":
                pid = parts[1]
                try:
                    sp.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
                    killed += 1
                except Exception:
                    pass
        return killed
    except Exception:
        return 0


def build_node_command(mesh_config):
    """根据组网配置构建客户端节点启动命令（no-tun，与用户其他 easytier 组网零冲突）。

    mesh_config: {networkName, networkSecret, ipv4, serverIp}

    冲突规避设计（主任要求）：
      - --config-dir: 用 ~/.yunqiao/easytier/ 独立目录，不碰用户默认 easytier 配置
      - --no-listener: 客户端只连服务器 hub，不监听任何端口（不占 11010/11011，不冲突）
      - 网络名 yunqiao-<user>：不与用户其他网络重名
    """
    cmd = [
        str(_core_path()),
        "--network-name", mesh_config["networkName"],
        "--network-secret", mesh_config["networkSecret"],
        "--dhcp", "true" if not mesh_config.get("ipv4") else "false",
        "--config-dir", str(_cache_dir()),   # 独立配置目录，不污染用户其他 easytier
        "--no-listener",                      # 不监听端口，只连 hub（零端口冲突）
    ]
    if mesh_config.get("ipv4"):
        cmd += ["-i", mesh_config["ipv4"]]
    # 连接服务器 hub（TCP 11010）
    server_ip = mesh_config.get("serverIp", SERVER_MESH_IP)
    cmd += ["-p", f"tcp://{server_ip}:11010"]
    # no-tun：不建虚拟网卡，只需逻辑组网身份
    cmd += ["--no-tun"]
    return cmd


def start_node(mesh_config, log_path=None, progress_cb=None):
    """启动 easytier 客户端节点（no-tun）。返回 subprocess.Popen 句柄；失败返回 None（不崩）。

    兼容性：任何环境启动失败都不抛异常，由调用方静默回退公网。"""
    try:
        cmd = build_node_command(mesh_config)
        if progress_cb:
            progress_cb("启动组网节点...")
        logf = None
        try:
            logf = open(log_path or (_cache_dir() / "easytier.log"), "a", encoding="utf-8")
        except Exception:
            logf = None
        creationflags = 0
        if platform.system() == "Windows":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # GUI 程序(easytier-gui)用 CREATE_NO_WINDOW 可能无效，用 STARTUPINFO 隐藏更稳
        si = None
        if platform.system() == "Windows":
            si = subprocess.STARTUPINFO()
            try:
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0  # SW_HIDE
            except Exception:
                pass
        proc = subprocess.Popen(
            cmd,
            stdout=logf or subprocess.DEVNULL,
            stderr=logf or subprocess.DEVNULL,
            creationflags=creationflags,
            startupinfo=si,
        )
        return proc
    except Exception as e:
        if progress_cb:
            progress_cb(f"启动组网节点失败: {e}")
        return None


def probe_mesh_channel(timeout=3, mesh=None):
    """探测组网通道是否已打通。

    可靠方式：检查 easytier 进程存活 + 日志出现 'dhcp ip changed' / 'new peer added'
    （说明已连上 hub 并拿到组网 IP，真正入网成功）。
    回退：连服务器 hub 的 easytier 端口（11010）。
    """
    # 1. 日志里找入网成功标志（最近 4KB 内的记录）
    log = _cache_dir() / "easytier.log"
    if log.exists():
        try:
            txt = log.read_text(encoding="utf-8", errors="replace")
            tail = txt[-4000:]
            if "dhcp ip changed" in tail or "new peer added" in tail:
                return True
        except Exception:
            pass
    # 2. 回退：尝试连服务器 hub 的 easytier 端口（11010）
    server = (mesh or {}).get("serverIp") or "45.152.65.49"
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        rc = s.connect_ex((server, 11010))
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