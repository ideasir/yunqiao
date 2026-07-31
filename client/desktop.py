"""
云桥 MCP — 桌面客户端（pywebview 正式版）
用法: pip install pywebview websockets && python desktop.py
PSK 从 ~/.yunqiao/config.json 读取
"""

import asyncio
import json
import os
import platform
import sys
import time
import threading
import uuid
from pathlib import Path

# ─── 配置 ────────────────────────────────────────
RELAY_URL = os.environ.get("RELAY_URL", "wss://yunqiao.very.im/device")
DEVICE_NAME = os.environ.get("DEVICE_NAME", platform.node())
CONFIG_DIR = Path(os.environ.get("YUNQIAO_CONFIG", str(Path.home() / ".yunqiao")))
CONFIG_FILE = CONFIG_DIR / "config.json"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

RELAY_PSK = ""
if CONFIG_FILE.exists():
    try:
        cfg = json.loads(CONFIG_FILE.read_text("utf-8"))
        RELAY_PSK = cfg.get("psk", "")
        if not RELAY_URL or RELAY_URL == "wss://yunqiao.very.im/device":
            RELAY_URL = cfg.get("relayUrl", RELAY_URL)
    except:
        pass
if not RELAY_PSK:
    print("⚠️ PSK 未配置，请在设置中配置")
    print(f"   配置文件: {CONFIG_FILE}")

# ─── 全局状态 ────────────────────────────────────
UI = None  # pywebview 窗口引用
WS = None
pair_code = str(100000 + int(time.time() * 1000) % 900000)
device_id = ""

# ─── WebSocket 连接 ──────────────────────────────
async def ws_connect():
    global WS, device_id
    import websockets
    url = f"{RELAY_URL}?psk={RELAY_PSK}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=30) as ws:
                WS = ws
                notify_ui("log", {"text": "已连接到中转服务器"})
                notify_ui("relay_status", {"status": "connected"})
                await ws.send(json.dumps({
                    "type": "register", "deviceName": DEVICE_NAME,
                    "os": sys.platform, "arch": platform.machine(),
                    "hostname": platform.node(), "authCode": pair_code,
                }))
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        handle_message(data)
                    except:
                        pass
        except Exception as e:
            notify_ui("log", {"text": f"连接断开: {e}"})
            notify_ui("relay_status", {"status": "disconnected"})
            await asyncio.sleep(5)

def handle_message(data):
    t = data.get("type")
    p = data.get("payload", {})
    if t == "register_result" and data.get("success"):
        global device_id
        device_id = data.get("deviceId", "")
        notify_ui("log", {"text": "注册成功"})
    elif t == "agent_connected":
        notify_ui("agent_status", {"status": "connected"})
    elif t == "agent_disconnected":
        notify_ui("agent_status", {"status": "disconnected"})
    elif t == "command_result" or t == "session_op_result":
        notify_ui("command_result", {"payload": p})
    elif t == "agent_message":
        notify_ui("log", {"text": f"智能体消息: {p.get('text', '')}"})

def notify_ui(action, data):
    """通知 HTML UI"""
    global UI
    if UI:
        try:
            js = f"if(window.handleBridge)window.handleBridge('{action}',{json.dumps(data)})"
            UI.evaluate_js(js)
        except:
            pass

# ─── JS Bridge API ───────────────────────────────
class Api:
    def send_command(self, command):
        global WS
        if WS:
            rid = "cmd_" + uuid.uuid4().hex[:8]
            asyncio.run_coroutine_threadsafe(
                WS.send(json.dumps({
                    "type": "execute_command", "requestId": rid,
                    "payload": {"command": command, "timeout": 30000},
                })),
                loop,
            )
            return {"requestId": rid}
        return {"error": "未连接"}

    def send_message(self, text):
        global WS
        if WS:
            rid = "msg_" + uuid.uuid4().hex[:8]
            asyncio.run_coroutine_threadsafe(
                WS.send(json.dumps({
                    "type": "agent_message", "requestId": rid, "text": text,
                })),
                loop,
            )
            return {"success": True}
        return {"error": "未连接"}

    def get_status(self):
        return {
            "pairCode": pair_code, "deviceName": DEVICE_NAME,
            "hostname": platform.node(), "platform": sys.platform,
            "relayStatus": "已连接" if WS else "未连接",
            "connected": WS is not None,
        }

    def save_settings(self, psk, relay_url):
        global RELAY_PSK, RELAY_URL
        cfg = {"psk": psk, "relayUrl": relay_url, "deviceName": DEVICE_NAME}
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), "utf-8")
        RELAY_PSK = psk
        RELAY_URL = relay_url
        return {"success": True}

    def get_settings(self):
        return {"psk": RELAY_PSK, "relayUrl": RELAY_URL, "deviceName": DEVICE_NAME}

    def browse_folder(self):
        """原生文件夹选择器"""
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.askdirectory(title="选择工作目录")
        root.destroy()
        return path or ""

    def get_sessions(self):
        sessions_dir = Path.home() / ".yunqiao" / "sessions"
        index_file = Path.home() / ".yunqiao" / "sessions.json"
        sessions = []
        default_id = None
        if index_file.exists():
            try:
                data = json.loads(index_file.read_text("utf-8"))
                default_id = data.get("defaultSessionId")
                for sid in data.get("sessions", []):
                    sf = sessions_dir / f"{sid}.json"
                    if sf.exists():
                        sd = json.loads(sf.read_text("utf-8"))
                        sd["isDefault"] = sd["id"] == default_id
                        sessions.append(sd)
            except:
                pass
        return {"sessions": sessions, "currentId": default_id}

loop = None

def start_ws():
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ws_connect())

def main():
    import webview

    t = threading.Thread(target=start_ws, daemon=True)
    t.start()

    ui_path = os.path.join(os.path.dirname(__file__), "ui.html")
    window = webview.create_window(
        "云桥 MCP v2.0", ui_path,
        width=1100, height=720, min_size=(900, 600),
        resizable=True, js_api=Api(),
    )
    global UI
    UI = window
    webview.start(debug=False)

if __name__ == "__main__":
    main()