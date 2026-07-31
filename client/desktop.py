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
            cfg_url = cfg.get("relayUrl", "")
            if cfg_url and not cfg_url.startswith("ws"):
                RELAY_URL = "wss://" + cfg_url + "/device"
            elif cfg_url:
                RELAY_URL = cfg_url
            else:
                RELAY_URL = "wss://yunqiao.very.im/device"
    except:
        pass
if not RELAY_PSK:
    print("⚠️ PSK 未配置，请在设置中配置")
    print(f"   配置文件: {CONFIG_FILE}")

# ─── 全局状态 ────────────────────────────────────
UI = None  # pywebview 窗口引用
WS = None
SHOULD_RECONNECT = True
CONNECT_THREAD = None
CONNECT_LOCK = threading.Lock()
pair_code = str(100000 + int(time.time() * 1000) % 900000)
device_id = ""

# ─── WebSocket 连接 ──────────────────────────────
async def _ws_connect_headers():
    """兼容不同 websockets 版本的请求头参数名"""
    try:
        import websockets
        import inspect
        sig = inspect.signature(websockets.connect)
        if 'additional_headers' in sig.parameters:
            return {'additional_headers': {"X-PSK": RELAY_PSK}}
    except:
        pass
    return {'extra_headers': {"X-PSK": RELAY_PSK}}

async def ws_connect():
    global WS, device_id, SHOULD_RECONNECT
    import websockets
    url = RELAY_URL
    ws_kwargs = await _ws_connect_headers()
    ws_kwargs['ping_interval'] = 30
    while SHOULD_RECONNECT:
        try:
            async with websockets.connect(url, **ws_kwargs) as ws:
                with CONNECT_LOCK:
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
            notify_ui("agent_status", {"status": "disconnected"})
            notify_ui("log", {"text": f"连接断开: {e}"})
            notify_ui("relay_status", {"status": "disconnected"})
        finally:
            with CONNECT_LOCK:
                WS = None
                CONNECT_THREAD = None
            if SHOULD_RECONNECT:
                await asyncio.sleep(5)

def handle_message(data):
    t = data.get("type")
    p = data.get("payload", {})
    rid = data.get("requestId", "")
    if t == "register_result" and data.get("success"):
        global device_id, WS
        device_id = data.get("deviceId", "")
        notify_ui("log", {"text": "注册成功"})
        # 请求 Agent 状态快照
        if WS:
            import asyncio
            asyncio.run_coroutine_threadsafe(
                WS.send(json.dumps({"type": "get_agent_status", "requestId": "init_status"})),
                loop
            )
    elif t == "agent_status":
        is_online = data.get("status") == "online"
        notify_ui("agent_status", {"status": "connected" if is_online else "disconnected"})
    elif t == "command_result" or t == "session_op_result":
        notify_ui("command_result", {"payload": p})
    elif t == "agent_message":
        notify_ui("log", {"text": f"智能体消息: {p.get('text', '')}"})
    # 直接执行命令（不再需要 agent.py）
    elif t == "execute_command":
        cmd = p.get("command", "")
        timeout = p.get("timeout", 30000)
        notify_ui("log", {"text": f"执行: {cmd[:50]}"})
        threading.Thread(target=_run_cmd, args=(rid, cmd, timeout), daemon=True).start()
    elif t == "read_file":
        path = p.get("path", "")
        threading.Thread(target=_read_file, args=(rid, path), daemon=True).start()
    elif t == "write_file":
        path = p.get("path", "")
        content = p.get("content", "")
        threading.Thread(target=_write_file, args=(rid, path, content), daemon=True).start()
    elif t == "get_device_info":
        threading.Thread(target=_get_info, args=(rid,), daemon=True).start()
    elif t == "session_op":
        op = p.get("op", "")
        notify_ui("log", {"text": f"会话操作: {op}"})
        threading.Thread(target=_handle_session_op, args=(rid, p), daemon=True).start()


import subprocess

def _run_cmd(rid, command, timeout=30000):
    """执行命令并发送结果"""
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            cwd=os.getcwd(), timeout=timeout/1000
        )
        result = {
            "exitCode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "killed": False,
        }
    except subprocess.TimeoutExpired:
        result = {"exitCode": 1, "stdout": "", "stderr": "Command timed out", "killed": True}
    except Exception as e:
        result = {"exitCode": 1, "stdout": "", "stderr": str(e), "killed": False}
    _send_response("command_result", rid, result)

def _read_file(rid, path):
    """读取文件"""
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        _send_response("file_result", rid, {"success": True, "content": content, "path": path})
    except Exception as e:
        _send_response("file_result", rid, {"success": False, "error": str(e), "path": path})

def _write_file(rid, path, content):
    """写入文件"""
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        _send_response("file_result", rid, {"success": True, "path": path})
    except Exception as e:
        _send_response("file_result", rid, {"success": False, "error": str(e), "path": path})

def _get_info(rid):
    """获取系统信息"""
    try:
        import psutil
        total = psutil.virtual_memory().total
        free = psutil.virtual_memory().available
    except:
        total = 0
        free = 0
    _send_response("device_info", rid, {
        "hostname": platform.node(),
        "platform": sys.platform,
        "arch": platform.machine(),
        "cpus": os.cpu_count() or 0,
        "totalMem": total,
        "freeMem": free,
        "uptime": time.time(),
        "homedir": str(Path.home()),
        "userInfo": {"username": os.getlogin()},
    })

def _handle_session_op(rid, payload):
    """处理会话操作"""
    op = payload.get("op", "")
    global session_mgr
    try:
        if op == "create":
            result = session_mgr.create(payload.get("workDir", ""), payload.get("name"))
        elif op == "exec":
            session = session_mgr.get_current()
            if not session:
                result = {"exitCode": 1, "stdout": "", "stderr": "没有当前会话，请先 create_session", "killed": False}
            else:
                result = _session_exec(session, payload.get("command", ""), payload.get("timeout", 30000))
        elif op == "read_file":
            session = session_mgr.get_current()
            if not session:
                result = {"success": False, "error": "没有当前会话"}
            else:
                result = _session_read_file(session, payload.get("path", ""))
        elif op == "write_file":
            session = session_mgr.get_current()
            if not session:
                result = {"success": False, "error": "没有当前会话"}
            else:
                result = _session_write_file(session, payload.get("path", ""), payload.get("content", ""))
        elif op == "close":
            result = session_mgr.close(payload.get("sessionId"))
        elif op == "list":
            result = session_mgr.list_all()
        elif op == "switch":
            result = session_mgr.switch(payload.get("sessionId", ""))
        else:
            result = {"success": False, "error": f"未知操作: {op}"}
    except Exception as e:
        result = {"success": False, "error": str(e)}
    _send_response("session_op_result", rid, result)

def _send_response(msg_type, rid, payload):
    """线程安全地发送响应到 WebSocket"""
    global WS, loop
    if WS and loop:
        import asyncio
        asyncio.run_coroutine_threadsafe(
            WS.send(json.dumps({"type": msg_type, "requestId": rid, "payload": payload})),
            loop
        )

def _session_exec(session, command, timeout):
    """在会话中执行命令"""
    import asyncio
    async def run():
        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=session.cwd
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout/1000)
            return {"exitCode": proc.returncode or 0, "stdout": stdout.decode("utf-8", errors="replace") if stdout else "", "stderr": stderr.decode("utf-8", errors="replace") if stderr else "", "killed": False}
        except asyncio.TimeoutError:
            proc.kill()
            stdout, stderr = await proc.communicate()
            return {"exitCode": 1, "stdout": stdout.decode("utf-8", errors="replace") if stdout else "", "stderr": stderr.decode("utf-8", errors="replace") if stderr else "", "killed": True}
    return asyncio.run(run())

def _session_read_file(session, path):
    if not os.path.isabs(path):
        path = os.path.join(session.cwd, path)
    path = os.path.normpath(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"success": True, "content": content, "path": path}
    except Exception as e:
        return {"success": False, "error": str(e), "path": path}

def _session_write_file(session, path, content):
    if not os.path.isabs(path):
        path = os.path.join(session.cwd, path)
    path = os.path.normpath(path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"success": True, "path": path}
    except Exception as e:
        return {"success": False, "error": str(e), "path": path}

# 初始化会话管理器
from agent import SessionManager, Session
session_mgr = SessionManager()

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

    def toggle_connect(self):
        """连接/断开切换（线程安全）"""
        global WS, SHOULD_RECONNECT, CONNECT_THREAD, CONNECT_LOCK
        with CONNECT_LOCK:
            if WS:
                # 断开
                SHOULD_RECONNECT = False
                import asyncio
                try:
                    asyncio.run_coroutine_threadsafe(WS.close(), loop)
                except:
                    pass
                WS = None
                CONNECT_THREAD = None
                notify_ui("relay_status", {"status": "disconnected"})
                notify_ui("log", {"text": "已断开连接"})
                return {"connected": False}
            elif CONNECT_THREAD and CONNECT_THREAD.is_alive():
                # 正在连接中，忽略重复点击
                notify_ui("log", {"text": "正在连接中..."})
                return {"connected": False}
            else:
                # 连接
                SHOULD_RECONNECT = True
                CONNECT_THREAD = threading.Thread(target=start_ws, daemon=True)
                CONNECT_THREAD.start()
                notify_ui("log", {"text": "正在连接..."})
                return {"connected": True}

    def refresh_pair_code(self):
        """前端刷新配对码时调用，同步到后端和中继"""
        global pair_code, WS, loop
        import random
        pair_code = str(random.randint(100000, 999999))
        if WS and loop:
            import asyncio
            asyncio.run_coroutine_threadsafe(
                WS.send(json.dumps({
                    "type": "update_code", "requestId": "refresh_" + str(int(time.time() * 1000)),
                    "authCode": pair_code,
                })),
                loop
            )
        return {"pairCode": pair_code}

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