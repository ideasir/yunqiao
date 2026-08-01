"""
云桥 - 桌面客户端（pywebview 版）
=============================
职责：UI 显示（设置、连接状态、日志、配对码）
不负责：WebSocket 连接、命令执行（那是 agent.py 的事）

用法:
  pip install pywebview websockets
  python desktop.py
"""

import json
import os
import platform
import sys
import threading
import random
from pathlib import Path

# ─── 配置 ────────────────────────────────────────
CONFIG_DIR = Path(os.environ.get("YUNQIAO_CONFIG", str(Path.home() / ".yunqiao")))
CONFIG_FILE = CONFIG_DIR / "config.json"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

DEVICE_NAME = os.environ.get("DEVICE_NAME", platform.node())

def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text("utf-8"))
        except:
            pass
    return {}

def save_config(relay_url, key, name, auto_connect=False):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({
        "relayUrl": relay_url, "key": key, "deviceName": name,
        "autoConnect": auto_connect
    }, indent=2), "utf-8")

cfg = load_config()
RELAY_URL = cfg.get("relayUrl", "")
RELAY_KEY = cfg.get("key", "")

# ─── 全局状态 ────────────────────────────────────
UI = None
pair_code = str(random.randint(100000, 999999))
agent = None  # Agent 实例，首次连接时创建

# ─── Agent 集成 ──────────────────────────────────
def get_agent():
    global agent
    if agent is None:
        from agent import Agent
        agent = Agent(RELAY_URL, RELAY_KEY, DEVICE_NAME, pair_code)
        agent.on_log = lambda msg: notify_ui("log", {"text": msg})
        def on_status(s):
            if "agent" in s:
                notify_ui("agent_status", {
                    "status": s["agent"],
                    "latency": s.get("latency", 0),
                    "agentId": s.get("agentId", ""),
                    "agentPlatform": s.get("agentPlatform", ""),
                    "agentHostname": s.get("agentHostname", ""),
                    "relayPlatform": s.get("relayPlatform", ""),
                })
            else:
                notify_ui("relay_status", {
                    "status": "connected" if s.get("connected") else "disconnected",
                    "latency": s.get("latency", 0)
                })
        agent.on_status = on_status
        agent.on_result = lambda r: notify_ui("command_result", {"payload": r})
        agent.on_messages_read = lambda ids: notify_ui("messages_read", {"ids": ids})
        agent.on_activity = lambda a: notify_ui("agent_activity", a)
    return agent

def start_agent():
    global agent
    if agent:
        agent.stop()
    agent = None
    a = get_agent()
    a.start()
    # 同步会话和状态到 UI
    threading.Timer(1.0, lambda: sync_ui_state(a)).start()

def sync_ui_state(a):
    sl = a.sessions.list_all()
    notify_ui("sync_status", {
        "pairCode": a.auth_code,
        "workDir": a.default_work_dir,
        "sessions": sl.get("sessions", []),
        "currentSessionId": sl.get("defaultId"),
    })

def stop_agent():
    global agent
    if agent:
        agent.stop()
        agent = None

def notify_ui(action, data):
    if UI:
        try:
            js = f"if(window.handleBridge)window.handleBridge('{action}',{json.dumps(data)})"
            UI.evaluate_js(js)
        except:
            pass

# ─── JS Bridge API ───────────────────────────────
class Api:
    def get_status(self):
        a = agent
        return {
            "pairCode": pair_code,
            "deviceName": DEVICE_NAME,
            "hostname": platform.node(),
            "platform": "Windows",
            "relayStatus": "已连接" if (a and a.connected) else "未连接",
            "connected": a is not None and a.connected,
        }

    def save_settings(self, key, relay_url, auto_connect=False):
        global RELAY_URL, RELAY_KEY
        RELAY_URL = relay_url
        RELAY_KEY = key
        save_config(relay_url, key, DEVICE_NAME, auto_connect)
        if agent:
            stop_agent()
        return {"success": True}

    def get_settings(self):
        return {"key": RELAY_KEY, "relayUrl": RELAY_URL, "deviceName": DEVICE_NAME,
                "autoConnect": cfg.get("autoConnect", False)}

    def toggle_connect(self):
        if agent and agent.connected:
            stop_agent()
            notify_ui("relay_status", {"status": "disconnected"})
            notify_ui("log", {"text": "已断开连接"})
            return {"connected": False}
        elif agent and agent._running:
            notify_ui("log", {"text": "正在连接中..."})
            return {"connected": False}
        else:
            if not RELAY_URL or not RELAY_KEY:
                notify_ui("log", {"text": "请先设置中继地址和密钥"})
                return {"connected": False}
            start_agent()
            notify_ui("log", {"text": "正在连接..."})
            return {"connected": True}

    def refresh_pair_code(self):
        global pair_code
        pair_code = str(random.randint(100000, 999999))
        if agent:
            agent.update_code(pair_code)
        return {"pairCode": pair_code}

    def send_message(self, text, urgent=False):
        """发送消息给上游 Agent（Agent 通过 get_client_messages 读取）"""
        a = agent
        if not a:
            return {"success": False, "error": "未连接"}
        if not a.connected:
            return {"success": False, "error": "未连接中继服务器"}
        msg_id = a.send_message(text, bool(urgent))
        return {"success": True, "msgId": msg_id}

    def reorder_messages(self, ordered_ids):
        """拖拽排序任务队列后，同步新顺序到中继"""
        a = agent
        if a:
            a.reorder_messages(list(ordered_ids))
            return {"success": True}
        return {"success": False, "error": "未连接"}

    def delete_messages(self, ids):
        """从任务队列删除消息"""
        a = agent
        if a:
            n = a.delete_messages(list(ids))
            return {"success": True, "deleted": n}
        return {"success": False, "error": "未连接"}

    def edit_message(self, msg_id, text):
        """编辑任务队列中某条消息"""
        a = agent
        if a:
            a.edit_message(msg_id, text)
            return {"success": True}
        return {"success": False, "error": "未连接"}

    def get_mcp_ticket(self):
        """向中继请求新的动态 MCP 地址 ticket（旧 ticket 作废）"""
        a = agent
        if a:
            ticket = a.get_mcp_ticket()
            if ticket:
                return {"success": True, "ticket": ticket}
            return {"success": False, "error": "获取失败，请检查中继连接"}
        return {"success": False, "error": "未连接"}

    def browse_folder(self):
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.askdirectory(title="选择工作目录")
        root.destroy()
        return path or ""

    def get_sessions(self):
        if agent:
            sl = agent.sessions.list_all()
            return {
                "sessions": sl.get("sessions", []),
                "currentId": sl.get("defaultId"),
                "workDir": agent.default_work_dir
            }
        # Agent 未启动时也返回默认工作区（与 agent.py 一致：exe 旁/worker 或项目根/worker）
        if getattr(sys, 'frozen', False):
            base = Path(sys.executable).parent
        else:
            base = Path(__file__).parent.parent
        dw = str(base / 'worker')
        return {
            "sessions": [{"id": "default", "name": "默认工作区", "workDir": dw, "cwd": dw, "isDefault": True}],
            "currentId": "default",
            "workDir": dw
        }

    def create_session(self, work_dir, name=None):
        """在远端创建新工作区（用户通过 UI 设定，不走 #5 限制）"""
        a = agent
        if a:
            result = a.sessions.create(work_dir, name)
            return {"success": True, "session": result}
        return {"success": False, "error": "未连接"}

    def switch_session(self, session_id):
        """切换当前工作区（影响 Agent 的默认工作目录）"""
        a = agent
        if a:
            result = a.sessions.switch(session_id)
            if result.get("success"):
                return {"success": True}
            return {"success": False, "error": result.get("error", "切换失败")}
        return {"success": False, "error": "未连接"}

    def set_permission(self, mode):
        if agent:
            agent.set_permission(mode)
            return {"success": True}
        return {"success": False, "error": "Agent 未启动"}

    def window_minimize(self):
        """无边框窗口的最小化"""
        if UI:
            UI.minimize()
        return {"success": True}

    def window_close(self):
        """无边框窗口的关闭"""
        if UI:
            UI.destroy()
        return {"success": True}

    def confirm_dialog(self, title, message):
        """原生确认对话框（tkinter，Windows 风格）"""
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        result = messagebox.askyesno(title, message)
        root.destroy()
        return bool(result)


# ─── 启动 ────────────────────────────────────────
def main():
    global UI
    import webview

    ui_path = os.path.join(os.path.dirname(__file__), "ui.html")
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后 ui.html 在 _MEIPASS 根目录
        ui_path = os.path.join(sys._MEIPASS, "ui.html")
        if not os.path.exists(ui_path):
            # 降级尝试 exe 所在目录
            ui_path = os.path.join(os.path.dirname(sys.executable), "ui.html")
    if not os.path.exists(ui_path):
        print(f"❌ 找不到 ui.html: {ui_path}")
        sys.exit(1)

    api = Api()

    UI = webview.create_window(
        title="云桥",
        url=ui_path,
        js_api=api,
        width=1096,
        height=699,
        min_size=(800, 500),
        resizable=True,
        frameless=True,   # 无系统标题栏，用自绘标题栏
        easy_drag=True,   # 通过 .pywebview-drag-region 区域拖动窗口
        shadow=True,      # 无边框窗口的阴影边框
    )

    # 读取自动连接配置
    auto_connect = cfg.get("autoConnect", False)
    if auto_connect and RELAY_URL and RELAY_KEY:
        threading.Timer(2.0, start_agent).start()

    webview.start()


if __name__ == "__main__":
    main()