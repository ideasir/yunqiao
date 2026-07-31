"""
云桥 MCP - 桌面客户端（pywebview 玻璃拟态版）
用法: pip install pywebview && python desktop.py
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

RELAY_URL = os.environ.get("RELAY_URL", "wss://yunqiao.very.im/device")
DEVICE_NAME = os.environ.get("DEVICE_NAME", platform.node())

# 从配置文件读取 PSK
CONFIG_DIR = Path(os.environ.get("YUNQIAO_CONFIG", str(Path.home() / ".yunqiao")))
CONFIG_FILE = CONFIG_DIR / "config.json"
RELAY_PSK = ""

if CONFIG_FILE.exists():
    try:
        cfg = json.loads(CONFIG_FILE.read_text("utf-8"))
        RELAY_PSK = cfg.get("psk", "")
        if not RELAY_URL:
            RELAY_URL = cfg.get("relayUrl", RELAY_URL)
    except:
        pass

if not RELAY_PSK:
    print("⚠️ 未找到 PSK 配置")
    print("   请在客户端设置中配置 PSK 和中继地址")

# ─── 会话管理 ────────────────────────────────
YUNQIAO_DIR = Path.home() / ".yunqiao"
SESSIONS_DIR = YUNQIAO_DIR / "sessions"
SESSIONS_INDEX = YUNQIAO_DIR / "sessions.json"
YUNQIAO_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# ─── 全局状态 ────────────────────────────────
state = {
    "ws": None,
    "connected": False,
    "deviceId": "",
    "pairCode": str(100000 + int(time.time() * 1000) % 900000),
    "latency": 0,
    "sessions": [],
    "currentSessionId": None,
    "logs": [],
    "deviceName": DEVICE_NAME,
    "hostname": platform.node(),
    "platform": sys.platform,
    "workDir": "",
    "agentStatus": "等待连接",
    "relayStatus": "未连接",
    "callbacks": {},  # requestId -> callback
}


def load_sessions():
    """从磁盘加载会话"""
    sessions = []
    if SESSIONS_INDEX.exists():
        try:
            data = json.loads(SESSIONS_INDEX.read_text("utf-8"))
            default_id = data.get("defaultSessionId")
            for sid in data.get("sessions", []):
                sfile = SESSIONS_DIR / f"{sid}.json"
                if sfile.exists():
                    sd = json.loads(sfile.read_text("utf-8"))
                    sd["isDefault"] = sd["id"] == default_id
                    sessions.append(sd)
                    if sd["isDefault"]:
                        state["currentSessionId"] = sid
                        state["workDir"] = sd.get("workDir", "")
        except:
            pass
    state["sessions"] = sessions
    return sessions


# ─── WebSocket 连接 ──────────────────────────
async def ws_connect():
    """连接中继服务器"""
    import websockets

    url = f"{RELAY_URL}?psk={RELAY_PSK}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=30) as ws:
                state["ws"] = ws
                state["relayStatus"] = "已连接"
                state["connected"] = True
                _notify_ui("relay_status", {"status": "connected"})
                _notify_ui("log", {"text": "WebSocket 已连接"})

                # 注册
                await ws.send(
                    json.dumps(
                        {
                            "type": "register",
                            "deviceName": state["deviceName"],
                            "os": sys.platform,
                            "arch": platform.machine(),
                            "hostname": state["hostname"],
                            "authCode": state["pairCode"],
                        }
                    )
                )

                async for msg in ws:
                    try:
                        data = json.loads(msg)
                    except:
                        continue
                    _handle_message(data)

        except Exception as e:
            state["relayStatus"] = "未连接"
            state["connected"] = False
            _notify_ui("relay_status", {"status": "disconnected"})
            _notify_ui("log", {"text": f"连接断开: {e}"})
            await asyncio.sleep(5)


def _handle_message(data):
    """处理 WebSocket 消息"""
    t = data.get("type")
    rid = data.get("requestId")
    p = data.get("payload", {})

    if t == "register_result" and data.get("success"):
        state["deviceId"] = data.get("deviceId", "")
        _notify_ui("log", {"text": f"注册成功"})

    elif t == "agent_connected":
        state["agentStatus"] = "已连接"
        _notify_ui("agent_status", {"status": "connected"})

    elif t == "agent_disconnected":
        state["agentStatus"] = "已断开"
        _notify_ui("agent_status", {"status": "disconnected"})

    elif t == "command_result" or t == "session_op_result":
        _notify_ui("command_result", {"rid": rid, "payload": p})

    elif t == "agent_message_result":
        _notify_ui("log", {"text": "消息已发送到服务器"})

    # 处理回调
    if rid and rid in state["callbacks"]:
        state["callbacks"][rid](data)
        del state["callbacks"][rid]


def _notify_ui(action, data):
    """通知 UI（通过 pywebview 桥）"""
    try:
        import webview

        if hasattr(webview, "windows") and webview.windows:
            js = f'window.handleBridge && window.handleBridge({json.dumps(action)},{json.dumps(data)})'
            webview.windows[0].evaluate_js(js)
    except:
        pass


# ─── 操作函数（供 JS 桥调用） ─────────────────
def api_send_command(command):
    """发送命令到中继"""
    if not state["ws"]:
        return {"error": "未连接"}
    rid = "cmd_" + uuid.uuid4().hex[:8]
    asyncio.run_coroutine_threadsafe(
        state["ws"].send(
            json.dumps(
                {
                    "type": "execute_command",
                    "requestId": rid,
                    "payload": {"command": command, "timeout": 30000},
                }
            )
        ),
        _get_loop(),
    )
    return {"requestId": rid}


def api_send_message(text):
    """发送消息给智能体"""
    if not state["ws"]:
        return {"error": "未连接"}
    rid = "msg_" + uuid.uuid4().hex[:8]
    asyncio.run_coroutine_threadsafe(
        state["ws"].send(
            json.dumps(
                {
                    "type": "agent_message",
                    "requestId": rid,
                    "text": text,
                }
            )
        ),
        _get_loop(),
    )
    return {"success": True}


def api_get_sessions():
    """获取会话列表"""
    load_sessions()
    return {"sessions": state["sessions"], "currentId": state["currentSessionId"]}


def api_get_status():
    """获取状态"""
    return {
        "connected": state["connected"],
        "pairCode": state["pairCode"],
        "deviceName": state["deviceName"],
        "hostname": state["hostname"],
        "platform": state["platform"],
        "workDir": state["workDir"],
        "relayStatus": state["relayStatus"],
        "agentStatus": state["agentStatus"],
        "latency": state["latency"],
    }


def api_get_logs():
    return {"logs": state["logs"][-200:]}


_loop = None


def _get_loop():
    global _loop
    return _loop


# ─── 启动 ────────────────────────────────────
def start_ws():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_until_complete(ws_connect())


class Api:
    """pywebview JS API 桥接"""
    def send_command(self, command):
        return api_send_command(command)

    def send_message(self, text):
        return api_send_message(text)

    def get_sessions(self):
        return api_get_sessions()

    def get_status(self):
        return api_get_status()

    def get_logs(self):
        return api_get_logs()

    def save_settings(self, psk, relay_url):
        """保存设置"""
        global RELAY_PSK, RELAY_URL
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg = {"psk": psk, "relayUrl": relay_url, "deviceName": DEVICE_NAME}
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), "utf-8")
        RELAY_PSK = psk
        RELAY_URL = relay_url
        return {"success": True}

    def get_settings(self):
        return {"psk": RELAY_PSK, "relayUrl": RELAY_URL, "deviceName": DEVICE_NAME}


def main():
    import webview

    # 启动 WebSocket 线程
    t = threading.Thread(target=start_ws, daemon=True)
    t.start()

    # 加载会话
    load_sessions()

    # 获取 ui.html 路径
    ui_path = os.path.join(os.path.dirname(__file__), "ui.html")

    # 创建窗口（带 JS API）
    webview.create_window(
        "云桥 MCP v2.0",
        ui_path,
        width=1100,
        height=720,
        min_size=(900, 600),
        resizable=True,
        js_api=Api(),
    )
    webview.start(debug=True)


if __name__ == "__main__":
    main()