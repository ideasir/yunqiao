"""
云桥 - 桌面客户端（pywebview 版）
=============================
职责：UI 显示（设置、连接状态、日志、配对码）
不负责：WebSocket 连接、命令执行（那是 agent.py 的事）

用法:
  pip install pywebview websockets
  python desktop.py
"""

import asyncio
import json
import os
import platform
import sys
import time
import threading
import uuid
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

def save_config(relay_url, key, name):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({
        "relayUrl": relay_url, "key": key, "deviceName": name
    }, indent=2), "utf-8")

cfg = load_config()
RELAY_URL = cfg.get("relayUrl", "")
RELAY_KEY = cfg.get("key", "") or cfg.get("psk", "")

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
        agent.on_status = lambda s: notify_ui("relay_status", {
            "status": "connected" if s.get("connected") else "disconnected"
        })
        agent.on_command = lambda c: notify_ui("log", {"text": f"收到命令: {c.get('type','?')} {c.get('command','')[:50]}"})
        agent.on_result = lambda r: notify_ui("command_result", {"payload": r})
    return agent

def start_agent():
    global agent
    if agent:
        agent.stop()
    agent = None
    a = get_agent()
    a.start()

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
            "platform": sys.platform,
            "relayStatus": "已连接" if (a and a.connected) else "未连接",
            "connected": a is not None and a.connected,
        }

    def save_settings(self, key, relay_url):
        global RELAY_URL, RELAY_KEY
        RELAY_URL = relay_url
        RELAY_KEY = key
        save_config(relay_url, key, DEVICE_NAME)
        if agent:
            stop_agent()
        return {"success": True}

    def get_settings(self):
        return {"key": RELAY_KEY, "relayUrl": RELAY_URL, "deviceName": DEVICE_NAME}

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

    def send_message(self, text):
        if agent:
            agent.send_message(text)
            return {"success": True}
        return {"error": "未连接"}

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
            return agent.sessions.list_all()
        return {"sessions": [], "defaultId": None}


# ─── 启动 ────────────────────────────────────────
def main():
    global UI
    import webview

    ui_path = os.path.join(os.path.dirname(__file__), "ui.html")
    if not os.path.exists(ui_path):
        print(f"❌ 找不到 ui.html: {ui_path}")
        sys.exit(1)

    api = Api()

    UI = webview.create_window(
        title="云桥",
        url=ui_path,
        js_api=api,
        width=1080,
        height=650,
        min_size=(800, 500),
        resizable=True,
    )

    # 如果已配置，自动连接
    if RELAY_URL and RELAY_KEY:
        threading.Timer(2.0, start_agent).start()

    webview.start()


if __name__ == "__main__":
    main()