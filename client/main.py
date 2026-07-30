"""
云桥 MCP — 桌面客户端（tkinter 版）
"""
import asyncio
import json
import os
import platform
import random
import sys
import threading
import time
from pathlib import Path

try:
    import websockets
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets

import tkinter as tk
from tkinter import ttk, messagebox

# ─── 配置 ────────────────────────────────────
CONFIG_DIR = Path(os.environ.get("YUNQIAO_CONFIG", str(Path.home() / ".yunqiao")))
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_RELAY = "wss://yunqiao.very.im/device"

# ─── 配色 ────────────────────────────────────
C = {
    "bg": "#F4F4F6", "panel": "#FEFDFC", "card": "#FFFFFF",
    "primary": "#F3A04C", "accent": "#E58522",
    "success": "#40B43E", "warning": "#EBA400", "danger": "#E6444E",
    "text": "#333333", "text2": "#888888", "text3": "#AAAAAA",
    "border": "#E0E0E0", "int4": "#F5F5F5", "int8": "#EBEBEB",
    "log_bg": "#1f2329", "log_fg": "#d6d9df",
}

# ─── 状态 ────────────────────────────────────
state = {
    "connected": False, "latency": 0, "deviceId": "", "deviceName": platform.node(),
    "pairCode": "", "psk": "", "relayUrl": DEFAULT_RELAY,
    "logs": [], "activities": [], "ws_client": None, "workDir": "", "directMode": True,
}


def load_config():
    if CONFIG_FILE.exists():
        try: return json.loads(CONFIG_FILE.read_text())
        except: pass
    return {}

def save_config():
    CONFIG_FILE.write_text(json.dumps({
        "psk": state["psk"], "relayUrl": state["relayUrl"],
        "deviceName": state["deviceName"], "workDir": state["workDir"],
    }, indent=2))

def gen_code():
    return str(random.randint(100000, 999999))
def add_tun_route():
    """添加路由绕过TUN（需管理员权限）"""
    try:
        import subprocess
        if sys.platform != "win32":
            return False
        # 检查是否管理员
        import ctypes
        is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        if not is_admin:
            return False
        # 找默认网关
        r = subprocess.run(["route", "print", "0.0.0.0"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                gw = parts[2]
                # 先删再添加（避免重复）
                subprocess.run(["route", "delete", "45.152.65.49"], capture_output=True, timeout=5)
                subprocess.run(["route", "add", "45.152.65.49", "mask", "255.255.255.255", gw, "metric", "1"],
                              capture_output=True, timeout=5)
                return True
    except:
        pass
    return False

def remove_tun_route():
    """移除直连路由"""
    try:
        import subprocess
        if sys.platform == "win32":
            subprocess.run(["route", "delete", "45.152.65.49"], capture_output=True, timeout=5)
    except:
        pass















# ─── 主窗口 ──────────────────────────────────
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("云桥 MCP")
        self.root.geometry("400x680")
        # 主窗口居中（延迟执行确保渲染完成）
        def center_main():
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = max(0, (sw - 400) // 2)
            y = max(0, (sh - 680) // 2)
            self.root.geometry(f"400x680+{x}+{y}")
        self.root.after(50, center_main)
        self.root.resizable(False, False)
        self.root.configure(bg=C["bg"])

        cfg = load_config()
        state["psk"] = cfg.get("psk", "")
        state["relayUrl"] = cfg.get("relayUrl", DEFAULT_RELAY)
        state["deviceName"] = cfg.get("deviceName", platform.node())
        state["workDir"] = cfg.get("workDir", "")
        state["pairCode"] = gen_code()

        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        if not state["psk"]:
            self.root.after(500, self.open_settings)

    # ─── UI 构建 ────────────────────────────
    def build_ui(self):
        # 标题栏
        title = tk.Frame(self.root, bg=C["panel"], height=32)
        title.pack(fill=tk.X)
        title.pack_propagate(False)
        tk.Label(title, text="Y", bg=C["primary"], fg="white",
                 font=("Segoe UI", 12, "bold"), width=2).pack(side=tk.LEFT, padx=8, pady=4)
        tk.Label(title, text="云桥 MCP", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT, padx=4)
        # 首次使用提示标签
        self.first_use_btn = tk.Label(title, text="首次使用请点击这里", bg=C["panel"], fg=C["accent"],
                                       font=("Segoe UI", 8), cursor="hand2")
        self.first_use_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.first_use_btn.bind("<Button-1>", lambda e: self.copy_skills_cmd())
        # 连接/断开按钮
        self.connect_btn = tk.Button(title, text="连接", bg=C["int8"], fg=C["success"],
                                     font=("Segoe UI", 10), bd=1, cursor="hand2",
                                     relief="solid", padx=6, pady=1,
                                     activebackground=C["int4"],
                                     command=self.toggle_connect)
        self.connect_btn.pack(side=tk.RIGHT, padx=(0, 6))


        # 内容区
        content = tk.Frame(self.root, bg=C["bg"])
        content.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        # 连接状态
        head = tk.Frame(content, bg=C["bg"])
        head.pack(fill=tk.X, pady=(0, 3))
        tk.Label(head, text="连接状态", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)
        self.status_frame = tk.Frame(head, bg=C["bg"])
        self.status_frame.pack(side=tk.RIGHT)
        self.status_dot = tk.Canvas(self.status_frame, width=8, height=8,
                                    bg=C["bg"], highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT, padx=(0, 4))
        self.dot = self.status_dot.create_oval(0, 0, 8, 8, fill=C["text3"], outline="")
        self.status_label = tk.Label(self.status_frame, text="初始化中...",
                                     bg=C["bg"], fg=C["text2"], font=("Segoe UI", 13))
        self.status_label.pack(side=tk.LEFT)

        # 当前连接卡片
        self.conn_card = self.make_card(content, "当前连接")
        route = tk.Frame(self.conn_card, bg=C["card"])
        route.pack(fill=tk.X, pady=2)
        self.node_labels = []
        nodes = [
            ("本地客户端", "等待连接"),
            ("中转服务器", "未连接"),
            ("上游 Agent", "待接入"),
        ]
        for i, (name, desc) in enumerate(nodes):
            if i > 0:
                tk.Label(route, text="›", bg=C["card"], fg=C["accent"],
                         font=("Segoe UI", 12)).pack(side=tk.LEFT, padx=2)
            nf = tk.Frame(route, bg=C["int4"], padx=6, pady=4)
            nf.pack(side=tk.LEFT, fill=tk.X, expand=True)
            if i == 1:
                # 中转服务器可点击打开设置
                tk.Button(nf, text=name, bg=C["int4"], fg=C["text"],
                          font=("Segoe UI", 10, "bold"), bd=0, cursor="hand2",
                          activebackground=C["int4"], command=self.open_settings).pack()
            else:
                tk.Label(nf, text=name, bg=C["int4"], fg=C["text"],
                         font=("Segoe UI", 10, "bold")).pack()
            dl = tk.Label(nf, text=desc, bg=C["int4"], fg=C["text2"],
                       font=("Segoe UI", 9))
            dl.pack()
            self.node_labels.append(dl)

        # 状态行
        stat = tk.Frame(self.conn_card, bg=C["card"])
        stat.pack(fill=tk.X, pady=(4, 0))
        tk.Label(stat, text=state["deviceName"], bg=C["card"], fg=C["text"],
                 font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)
        self.latency_label = tk.Label(stat, text="延迟 -- ms", bg=C["card"], fg=C["text2"],
                                      font=("Segoe UI", 10))
        self.latency_label.pack(side=tk.LEFT, padx=8)
        self.status_badge = tk.Label(stat, text="未连接", bg=C["int8"], fg=C["text2"],
                                     font=("Segoe UI", 9), padx=6, pady=1)
        self.status_badge.pack(side=tk.RIGHT)

        # Agent 活动
        self.act_card = tk.Frame(content, bg=C["card"], padx=6, pady=3)
        self.act_card.pack(fill=tk.X, pady=1)
        act_head = tk.Frame(self.act_card, bg=C["card"])
        act_head.pack(fill=tk.X)
        tk.Label(act_head, text="Agent 活动", bg=C["card"], fg=C["text2"],
                 font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self.status_light = tk.Canvas(act_head, width=10, height=10,
                                      bg=C["card"], highlightthickness=0)
        self.status_light.pack(side=tk.RIGHT, padx=(0, 4))
        self.light = self.status_light.create_oval(1, 1, 9, 9, fill=C["text3"], outline="")
        # 工具网格
        self.tool_frame = tk.Frame(self.act_card, bg=C["card"])
        self.tool_frame.pack(fill=tk.X, pady=(0, 2))
        self.tool_labels = []
        self.tool_history = []  # [{"name": str, "active": bool}]
        # 活动日志
        self.act_frame = tk.Frame(self.act_card, bg=C["card"])
        self.act_frame.pack(fill=tk.X)
        self.act_label = tk.Label(self.act_frame, text="暂无活动", bg=C["card"], fg=C["text3"],
                                  font=("Segoe UI", 10))
        self.act_label.pack(pady=4)

        # 配对码
        pair_card = self.make_card(content, "Agent 配对码")
        pf = tk.Frame(pair_card, bg=C["card"])
        pf.pack(fill=tk.X)
        tk.Label(pf, text="配对码", bg=C["card"], fg=C["text2"],
                 font=("Segoe UI", 10)).pack(side=tk.LEFT)
        self.pair_badge = tk.Label(pf, text="", bg=C["card"], fg=C["text2"],
                                   font=("Segoe UI", 9), padx=6, pady=1)
        self.pair_badge.pack(side=tk.RIGHT)

        pair_body = tk.Frame(pair_card, bg=C["card"])
        pair_body.pack(fill=tk.X, pady=2)
        self.code_label = tk.Label(pair_body, text=state["pairCode"],
                                   bg=C["card"], fg=C["accent"],
                                   font=("Courier New", 20, "bold"))
        self.code_label.pack()
        tk.Label(pair_body, text="将此配对码发给智能体，用于建立连接",
                 bg=C["card"], fg=C["text2"], font=("Segoe UI", 9)).pack()
        # Agent 工作区
        wd_label = tk.Label(pair_body, text="Agent 的工作区", bg=C["card"], fg=C["text2"],
                            font=("Segoe UI", 9)).pack(anchor="w", pady=(6, 2))
        wd_frame = tk.Frame(pair_body, bg=C["int4"], padx=6, pady=4)
        wd_frame.pack(fill=tk.X)
        self.workdir_label = tk.Label(wd_frame, text=state["workDir"] or "未设置工作目录",
                                      bg=C["int4"], fg=C["text"], font=("Segoe UI", 10))
        self.workdir_label.pack(side=tk.LEFT)
        tk.Button(wd_frame, text="📁 浏览", bg=C["int8"], fg=C["text"], bd=0,
                  font=("Segoe UI", 9), padx=8, pady=2,
                  command=self.browse_workdir).pack(side=tk.RIGHT)

        btn_frame = tk.Frame(pair_card, bg=C["card"])
        btn_frame.pack(fill=tk.X, pady=(2, 0))
        self.copy_btn = tk.Button(btn_frame, text="📋 复制并发送给Agent",
                                  bg=C["primary"], fg="white", bd=0,
                                  font=("Segoe UI", 10), padx=10, pady=3,
                                  cursor="hand2", command=self.copy_code)
        self.copy_btn.pack(fill=tk.X, pady=(0, 2))
        tk.Button(btn_frame, text="🔄 重新生成",
                  bg=C["int8"], fg=C["text"], bd=0,
                  font=("Segoe UI", 10), padx=10, pady=3,
                  cursor="hand2", command=self.refresh_code).pack(fill=tk.X)

        # 日志
        log_frame = tk.Frame(content, bg=C["log_bg"], padx=6, pady=4)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
        tk.Label(log_frame, text="relay.log", bg=C["log_bg"], fg="#9da5b0",
                 font=("Segoe UI", 9)).pack(anchor="w")
        self.log_text = tk.Text(log_frame, bg=C["log_bg"], fg=C["log_fg"],
                                font=("Consolas", 9), bd=0,
                                highlightthickness=0)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.insert("1.0", "等待连接...")

        # 版本
        tk.Label(content, text="云桥 MCP v1.0.0", bg=C["bg"], fg=C["text3"],
                 font=("Segoe UI", 9)).pack(pady=(2, 0))

    def make_card(self, parent, title):
        card = tk.Frame(parent, bg=C["card"], padx=6, pady=3)
        card.pack(fill=tk.X, pady=1)
        tk.Label(card, text=title, bg=C["card"], fg=C["text2"],
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        return card

    # ─── 配对码 ────────────────────────────
    def copy_code(self):
        code = state["pairCode"]
        cmd = f"> mcp-client {code}"
        self.root.clipboard_clear()
        self.root.clipboard_append(cmd)
        self.pair_badge.configure(text="已复制", bg=C["success"], fg="white")
        self.copy_btn.configure(text="✅ 已复制，请发给Agent")
        self.root.after(2000, lambda: (
            self.copy_btn.configure(text="📋 复制并发送给Agent")
        ))

    def copy_skills_cmd(self):
        """复制连接地址+skills命令给Agent安装"""
        code = state["pairCode"]
        server_url = state.get("relayUrl", "wss://yunqiao.very.im/device")
        mcp_url = server_url.replace("/device", "/mcp").replace("ws://", "http://").replace("wss://", "https://")
        skills_url = "https://raw.githubusercontent.com/ideasir/yunqiao-mcp/main/skills/mcp-client.mjs"
        text = f"安装 skills: {skills_url}\n连接地址: {mcp_url}\n配对码: {code}"
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.first_use_btn.configure(text="✅ 已复制，请发给Agent", fg=C["success"])
        self.root.after(3000, lambda: self.first_use_btn.configure(
            text="首次使用请点击这里", fg=C["accent"]))

    def refresh_code(self):
        state["pairCode"] = gen_code()
        self.code_label.configure(text=state["pairCode"])

    # ─── 设置弹窗 ──────────────────────────
    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.resizable(False, False)
        win.configure(bg=C["panel"])
        win.transient(self.root)
        win.grab_set()

        # 窗口居中于屏幕
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        win.geometry(f"320x280+{(sw-320)//2}+{(sh-280)//2}")

        has_config = bool(state["psk"] and state["relayUrl"])

        fields = [
            ("中继地址", "relay", state["relayUrl"] if has_config else ""),
            ("PSK 密钥", "psk", state["psk"]),
            ("设备名称", "name", state["deviceName"]),
        ]
        # 连接模式
        mode_frame = tk.Frame(win, bg=C["panel"])
        mode_frame.pack(fill=tk.X, padx=12, pady=(6, 0))
        tk.Label(mode_frame, text="连接模式", bg=C["panel"], fg=C["text2"],
                 font=("Segoe UI", 9)).pack(anchor="w")
        mode_btnf = tk.Frame(mode_frame, bg=C["panel"])
        mode_btnf.pack(fill=tk.X, pady=2)
        self.direct_var = tk.BooleanVar(value=state.get("directMode", True))
        tk.Radiobutton(mode_btnf, text="直连", variable=self.direct_var,
                       value=True, bg=C["panel"], fg=C["text"],
                       font=("Segoe UI", 9), selectcolor=C["panel"]).pack(side=tk.LEFT, padx=(0, 10))
        tk.Radiobutton(mode_btnf, text="系统代理", variable=self.direct_var,
                       value=False, bg=C["panel"], fg=C["text"],
                       font=("Segoe UI", 9), selectcolor=C["panel"]).pack(side=tk.LEFT)
        entries = {}

        def save():
            url = entries["relay"].get().strip()
            psk = entries["psk"].get().strip()
            name = entries["name"].get().strip() or platform.node()
            if not url or not psk:
                messagebox.showwarning("提示", "请填写中继地址和 PSK")
                return
            state["relayUrl"] = url
            state["psk"] = psk
            state["deviceName"] = name
            state["directMode"] = self.direct_var.get()
            save_config()
            win.destroy()
            self.start_connect()

        entries = {}
        entry_widgets = []

        for i, (label, key, val) in enumerate(fields):
            tk.Label(win, text=label, bg=C["panel"], fg=C["text2"],
                     font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(8 if i == 0 else 3, 0))
            ef = tk.Frame(win, bg=C["panel"])
            e = tk.Entry(ef, bg="white", fg=C["text"],
                         font=("Segoe UI", 9), bd=1, relief="solid")
            e.insert(0, val)
            if key == "psk":
                e.configure(show="*")
            e.pack(fill=tk.X, side=tk.LEFT, expand=True)

            ef.pack(fill=tk.X, padx=12, pady=2)
            entries[key] = e
            entry_widgets.append(e)

        btnf = tk.Frame(win, bg=C["panel"])
        btnf.pack(fill=tk.X, pady=10, padx=12, side=tk.BOTTOM)

        if has_config:
            # 已保存配置，先设为只读，加编辑按钮
            for e in entry_widgets[:3]:
                e.config(state="readonly", bg=C["int8"], fg=C["text2"], relief="sunken")
            editing = [False]

            def toggle_edit():
                editing[0] = not editing[0]
                for e in entry_widgets[:3]:
                    e.config(state="normal" if editing[0] else "readonly",
                             bg="white" if editing[0] else C["int8"],
                             fg=C["text"] if editing[0] else C["text2"],
                             relief="solid" if editing[0] else "sunken")
                edit_btn.config(text="取消编辑" if editing[0] else "✏️ 编辑")
                save_btn.pack(side=tk.RIGHT, padx=6) if editing[0] else save_btn.pack_forget()

            edit_btn = tk.Button(btnf, text="✏️ 编辑", bg=C["int8"], fg=C["text"], bd=0,
                                font=("Segoe UI", 9), padx=12, pady=3, command=toggle_edit)
            edit_btn.pack(side=tk.LEFT, padx=(0, 6))
            save_btn = tk.Button(btnf, text="保存并连接", bg=C["primary"], fg="white", bd=0,
                                font=("Segoe UI", 9), padx=12, pady=3, command=save)
            tk.Button(btnf, text="关闭", bg=C["int8"], fg=C["text"], bd=0,
                      font=("Segoe UI", 9), padx=12, pady=3, command=win.destroy).pack(side=tk.RIGHT, padx=6)
        else:
            tk.Button(btnf, text="取消", bg=C["int8"], fg=C["text"], bd=0,
                      font=("Segoe UI", 9), padx=12, pady=3, command=win.destroy).pack(side=tk.RIGHT, padx=6)
            tk.Button(btnf, text="保存并连接", bg=C["primary"], fg="white", bd=0,
                      font=("Segoe UI", 9), padx=12, pady=3, command=save).pack(side=tk.RIGHT, padx=6)


    def start_connect(self):
        self.set_status("connecting", "连接中...")
        self.connect_btn.configure(text="断开", fg=C["danger"], bg=C["int8"])
        # 直连模式：绕过系统代理 + 添加路由绕过TUN
        if state.get("directMode", True):
            os.environ["NO_PROXY"] = "*"
            add_tun_route()
        else:
            os.environ.pop("NO_PROXY", None)
            remove_tun_route()
        t = threading.Thread(target=self._ws_loop, daemon=True)
        t.start()

    def toggle_connect(self):
        """切换连接/断开"""
        if state["ws_client"] and state["connected"]:
            # 断开连接
            self.add_log("INFO", "手动断开连接")
            state["connected"] = False
            ws = state["ws_client"]
            state["ws_client"] = None
            try: ws.close()
            except: pass
            self.set_status("disconnected", "未连接")
            self.connect_btn.configure(text="连接", fg=C["success"], bg=C["int8"])
            if len(self.node_labels) >= 3:
                self.node_labels[0].configure(text="等待连接", fg=C["text2"])
                self.node_labels[1].configure(text="未连接", fg=C["text2"])
                self.node_labels[2].configure(text="待接入", fg=C["text2"])
        else:
            # 连接
            if not state["psk"]:
                self.open_settings()
                return
            self.start_connect()

    def _ws_loop(self):
        asyncio.run(self._ws_client())

    async def _ws_client(self):
        url = state["relayUrl"]
        psk = state["psk"]
        while True:
            try:
                self.add_log("INFO", f"正在连接 {url}...")
                t0 = time.time()
                # 兼容不同websockets版本
                ws_kwargs = {"ping_interval": 10}
                ws_version = getattr(websockets, "__version__", "0")
                try:
                    major = int(ws_version.split(".")[0])
                except:
                    major = 0
                if major >= 13:
                    ws_kwargs["additional_headers"] = {"X-PSK": psk}
                else:
                    ws_kwargs["extra_headers"] = {"X-PSK": psk}
                async with websockets.connect(
                    url, **ws_kwargs,
                ) as ws:
                    state["ws_client"] = ws
                    state["connected"] = True
                    lat = round((time.time() - t0) * 1000, 1)
                    self.root.after(0, lambda: self.set_connected(lat))

                    await ws.send(json.dumps({
                        "type": "register", "deviceName": state["deviceName"],
                        "os": sys.platform, "arch": platform.machine(),
                        "hostname": platform.node(), "authCode": state["pairCode"],
                    }))

                    async for msg in ws:
                        try: data = json.loads(msg)
                        except: continue
                        t = data.get("type")
                        rid = data.get("requestId")
                        payload = data.get("payload", {})

                        if t == "register_result" and data.get("success"):
                            state["deviceId"] = data.get("deviceId", "")
                            self.add_log("INFO", f"注册成功")
                            # 上游 Agent 保持"等待配对"，只有智能体用配对码成功调用后才变绿

                        elif t == "execute_command":
                            cmd = payload.get("command", "")
                            timeout = payload.get("timeout", 30000)
                            self.add_log("INFO", f"执行: {cmd[:50]}")
                            threading.Thread(target=self._run_cmd,
                                args=(ws, rid, cmd, timeout), daemon=True).start()

                        elif t == "read_file":
                            path = payload.get("path", "")
                            threading.Thread(target=self._read_file,
                                args=(ws, rid, path), daemon=True).start()

                        elif t == "write_file":
                            path = payload.get("path", "")
                            content = payload.get("content", "")
                            threading.Thread(target=self._write_file,
                                args=(ws, rid, path, content), daemon=True).start()

                        elif t == "get_device_info":
                            threading.Thread(target=self._get_info,
                                args=(ws, rid), daemon=True).start()

            except websockets.ConnectionClosed:
                self.root.after(0, lambda: self.set_status("reconnecting", "重连中..."))
                self.add_log("WARN", "连接断开，5秒后重连")
            except Exception as e:
                self.root.after(0, lambda: self.set_status("error", f"错误: {str(e)[:30]}"))
                self.add_log("ERROR", str(e)[:60])
            finally:
                state["ws_client"] = None
                state["connected"] = False
                self.root.after(0, lambda: self.connect_btn.configure(text="连接", fg=C["success"], bg=C["int8"]))
            await asyncio.sleep(5)

    # ─── UI 更新 ────────────────────────────
    def set_status(self, status, text):
        colors = {"connected": C["success"], "connecting": C["warning"],
                  "reconnecting": C["warning"], "error": C["danger"],
                  "disconnected": C["text3"]}
        labels = {"connected": "已连接", "connecting": "连接中",
                  "reconnecting": "重连中", "error": "错误",
                  "disconnected": "未连接"}
        color = colors.get(status, C["text3"])
        label = labels.get(status, text)
        self.status_dot.itemconfig(self.dot, fill=color)
        self.status_label.configure(text=label)
        self.status_badge.configure(text=label, bg=color, fg="white")

    def set_connected(self, latency):
        self.set_status("connected", "已连接")
        self.connect_btn.configure(text="断开", fg=C["danger"])
        self.latency_label.configure(text=f"延迟 {latency:.0f} ms")
        # 更新节点状态
        if len(self.node_labels) >= 3:
            self.node_labels[0].configure(text="已连接 ✅", fg=C["success"])
            self.node_labels[1].configure(text="已连接 ✅", fg=C["success"])
            self.node_labels[2].configure(text="等待配对码", fg=C["warning"])
        # 配对码状态
        self.pair_badge.configure(text="", bg=C["card"])

    def add_log(self, level, msg):
        ts = time.strftime("%H:%M:%S")
        colors = {"INFO": C["log_fg"], "WARN": "#f5b36b", "ERROR": "#e77070"}
        tag = f"log_{level}"
        self.log_text.insert("end", f"[{ts}] {level} {msg}\n", tag)
        self.log_text.see("end")
        # 更新 Agent 活动（只显示执行消息，不显示完成/退出码）
        if hasattr(self, 'act_label') and "执行:" in msg:
            self.act_label.configure(text=f"▶ {msg.replace('执行: ', '')}", fg=C["success"])
        # 更新工具网格
        self.update_tool_grid(msg)
        # 检查是否是命令执行（智能体在线）
        if "执行" in msg or "命令" in msg:
            if hasattr(self, 'agent_status'):
                self.agent_status.configure(text="🤖 在线", fg=C["success"])
                self.agent_last = time.time()
            # Agent 配对成功，更新上游节点状态
            if len(self.node_labels) >= 3:
                self.node_labels[2].configure(text="已接入", fg=C["success"])
        elif "完成" in msg or "退出码" in msg:
            if hasattr(self, 'agent_last'):
                self.agent_last = time.time()

    def update_tool_grid(self, msg):
        """解析工具调用并累积更新工具网格"""
        import re
        if not hasattr(self, 'tool_frame') or not hasattr(self, 'tool_history'):
            return

        # 判断是开始执行还是完成
        is_exec = "执行" in msg or "调用" in msg
        is_done = "完成" in msg or "退出码" in msg

        # 提取工具/命令名
        tool_name = ""
        for m in re.findall(r'执行[：:]\s*(\S+)', msg):
            tool_name = m
            break
        if not tool_name:
            for m in re.findall(r'调用[：:]\s*(\S+)', msg):
                tool_name = m
                break
        if not tool_name:
            for kw in ["read_file", "write_file", "run_command", "list_directory",
                       "exec", "git", "pip", "npm", "python", "node", "powershell",
                       "cmd", "dir", "cd", "mkdir", "echo", "type", "where", "del"]:
                if kw in msg.lower():
                    tool_name = kw
                    break

        if is_exec and tool_name:
            # 新工具开始执行 - 添加到历史，标记为活跃
            # 先把之前的都标记为非活跃
            for t in self.tool_history:
                t["active"] = False
            # 如果同名工具已在历史中，更新为活跃；否则追加
            found = False
            for t in self.tool_history:
                if t["name"] == tool_name:
                    t["active"] = True
                    found = True
                    break
            if not found:
                self.tool_history.append({"name": tool_name, "active": True})
        elif is_done and self.tool_history:
            # 完成 - 把当前活跃的标记为非活跃
            for t in self.tool_history:
                t["active"] = False

        # 重绘所有工具芯片
        for w in self.tool_frame.winfo_children():
            w.destroy()
        self.tool_labels = []

        # 显示最近最多 8 个工具
        for t in self.tool_history[-8:]:
            is_active = t["active"]
            if is_active:
                # 活跃：绿色背景 + 闪烁
                bg = "#1a3a1a"
                fg = "#4ade80"
                bd = 1
            else:
                # 历史：灰色芯片
                bg = C["int4"]
                fg = C["text3"]
                bd = 0
            lbl = tk.Label(self.tool_frame, text=t["name"], bg=bg, fg=fg,
                           font=("Segoe UI", 9), padx=8, pady=3,
                           relief="solid", bd=bd)
            lbl.pack(side=tk.LEFT, padx=3)
            self.tool_labels.append(lbl)
            if is_active:
                self.blink_tool(lbl)

    def blink_tool(self, label):
        """工具闪烁效果（绿色闪烁）"""
        def toggle():
            if not label.winfo_exists():
                return
            current = label.cget("fg")
            if current == "#4ade80":
                label.configure(fg="#166534", bg="#0a1a0a")
            else:
                label.configure(fg="#4ade80", bg="#1a3a1a")
            self.root.after(500, toggle)
        toggle()

    # ─── 命令处理 ────────────────────────────
    def _run_cmd(self, ws, rid, command, timeout):
        import asyncio, subprocess
        async def run():
            try:
                cwd = state["workDir"] if state["workDir"] else None
                proc = await asyncio.create_subprocess_shell(
                    command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=cwd)
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout/1000)
                    ec = proc.returncode or 0; killed = False
                except asyncio.TimeoutError:
                    proc.kill(); stdout, stderr = await proc.communicate()
                    ec = 1; killed = True
                await ws.send(json.dumps({
                    "type": "command_result", "requestId": rid,
                    "payload": {
                        "exitCode": ec,
                        "stdout": (stdout or b"").decode("utf-8", errors="replace"),
                        "stderr": (stderr or b"").decode("utf-8", errors="replace"),
                        "killed": killed,
                    },
                }))
                self.add_log("INFO", f"完成, 退出码: {ec}")
            except Exception as e:
                self.add_log("ERROR", f"执行失败: {e}")
        asyncio.run(run())

    def _read_file(self, ws, rid, path):
        import asyncio
        async def run():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                await ws.send(json.dumps({
                    "type": "file_result", "requestId": rid,
                    "payload": {"success": True, "content": content, "path": path},
                }))
                self.add_log("INFO", f"读取文件: {path}")
            except Exception as e:
                await ws.send(json.dumps({
                    "type": "file_result", "requestId": rid,
                    "payload": {"success": False, "error": str(e), "path": path},
                }))
                self.add_log("ERROR", f"读取失败: {e}")
        asyncio.run(run())

    def _write_file(self, ws, rid, path, content):
        import asyncio
        async def run():
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                await ws.send(json.dumps({
                    "type": "file_result", "requestId": rid,
                    "payload": {"success": True, "path": path},
                }))
                self.add_log("INFO", f"写入文件: {path}")
            except Exception as e:
                await ws.send(json.dumps({
                    "type": "file_result", "requestId": rid,
                    "payload": {"success": False, "error": str(e), "path": path},
                }))
                self.add_log("ERROR", f"写入失败: {e}")
        asyncio.run(run())

    def _get_info(self, ws, rid):
        import asyncio
        async def run():
            await ws.send(json.dumps({
                "type": "device_info", "requestId": rid,
                "payload": {
                    "hostname": platform.node(), "platform": sys.platform,
                    "arch": platform.machine(), "cpus": os.cpu_count() or 0,
                    "uptime": time.time(), "homedir": str(Path.home()),
                    "userInfo": {"username": os.getlogin()},
                },
            }))
            self.add_log("INFO", "已返回系统信息")
        asyncio.run(run())

    def browse_workdir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(title="选择工作目录")
        if d:
            state["workDir"] = d
            self.workdir_label.configure(text="📂 " + d)
            save_config()

    def on_close(self):
        if state["ws_client"]:
            try:
                import asyncio
                asyncio.run(state["ws_client"].close())
            except: pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
