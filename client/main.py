"""
云桥 MCP — 桌面客户端
基于 pywebview 包装，HTML 前端 + Python 后端
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

# 依赖检查
try:
    import websockets
except ImportError:
    print("正在安装 websockets...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets

try:
    import webview
except ImportError:
    print("正在安装 pywebview...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pywebview", "-q"])
    import webview

# ─── 配置 ────────────────────────────────────
CONFIG_DIR = Path(os.environ.get("YUNQIAO_CONFIG", str(Path.home() / ".yunqiao")))
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_RELAY = "wss://yunqiao.very.im/device"

# ─── 状态 ────────────────────────────────────
state = {
    "connected": False,
    "latency": 0,
    "deviceId": "",
    "deviceName": platform.node(),
    "pairCode": "",
    "psk": "",
    "relayUrl": DEFAULT_RELAY,
    "logs": [],
    "activities": [],
}

MAX_LOG = 100


def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except:
            pass
    return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def generate_code():
    return str(random.randint(100000, 999999))


# ─── WebSocket 客户端 ─────────────────────────
class RelayClient:
    def __init__(self, api):
        self.api = api
        self.ws = None
        self._stop = False

    def push(self, cmd, data=None):
        """推送数据到前端"""
        try:
            js = f"window.__push({json.dumps(cmd)},{json.dumps(data or {})})"
            webview.windows[0].evaluate_js(js)
        except:
            pass

    def add_log(self, level, msg):
        state["logs"].append({"t": time.strftime("%H:%M:%S"), "l": level, "m": msg})
        if len(state["logs"]) > MAX_LOG:
            state["logs"].pop(0)
        self.push("log", {"logs": state["logs"][-10:]})

    def add_activity(self, kind, detail):
        state["activities"].append({"t": time.strftime("%H:%M:%S"), "k": kind, "d": detail})
        if len(state["activities"]) > 20:
            state["activities"].pop(0)
        self.push("activity", {"activities": state["activities"]})

    async def run(self):
        psk = state["psk"]
        url = state["relayUrl"]
        if not psk or not url:
            self.push("status", {"connected": False, "error": "请先配置 PSK 和中继地址"})
            return

        while not self._stop:
            try:
                self.add_log("INFO", f"正在连接 {url}...")
                self.push("status", {"connected": False, "status": "connecting"})

                t0 = time.time()
                async with websockets.connect(
                    url,
                    extra_headers={"X-PSK": psk},
                    ping_interval=10,
                ) as ws:
                    self.ws = ws
                    latency = round((time.time() - t0) * 1000, 1)
                    state["connected"] = True
                    state["latency"] = latency
                    self.add_log("INFO", f"已连接，延迟 {latency}ms")
                    self.push("status", {
                        "connected": True,
                        "latency": latency,
                        "status": "connected",
                    })

                    # 注册设备（带上配对码）
                    code = state["pairCode"]
                    await ws.send(json.dumps({
                        "type": "register",
                        "deviceName": state["deviceName"],
                        "os": sys.platform,
                        "arch": platform.machine(),
                        "hostname": platform.node(),
                        "authCode": code,
                    }))

                    # 接收消息
                    async for message in ws:
                        try:
                            msg = json.loads(message)
                        except json.JSONDecodeError:
                            continue

                        t = msg.get("type")
                        rid = msg.get("requestId")

                        if t == "register_result" and msg.get("success"):
                            state["deviceId"] = msg.get("deviceId", "")
                            self.add_log("INFO", f"注册成功，设备ID: {state['deviceId'][:8]}...")
                            self.push("paired", {"deviceId": state["deviceId"]})
                            continue

                        if t == "register_result" and not msg.get("success"):
                            self.add_log("ERROR", f"注册失败: {msg.get('error', 'unknown')}")
                            continue

                        # 处理命令执行
                        payload = msg.get("payload", {})
                        await self.handle_command(t, rid, payload)

            except websockets.exceptions.ConnectionClosed:
                state["connected"] = False
                self.add_log("WARN", "连接断开")
                self.push("status", {"connected": False, "status": "reconnecting"})
            except Exception as e:
                state["connected"] = False
                self.add_log("ERROR", f"连接错误: {e}")
                self.push("status", {"connected": False, "status": "error", "error": str(e)})
            finally:
                state["connected"] = False
                self.ws = None

            if not self._stop:
                await asyncio.sleep(5)

    async def handle_command(self, msg_type, request_id, payload):
        self.add_activity("cmd", f"{msg_type}: {str(payload)[:60]}")
        if msg_type == "execute_command":
            command = payload.get("command", "")
            timeout = payload.get("timeout", 30000)
            try:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout / 1000
                    )
                    exit_code = proc.returncode or 0
                    killed = False
                except asyncio.TimeoutError:
                    proc.kill()
                    stdout, stderr = await proc.communicate()
                    exit_code = 1
                    killed = True

                if self.ws:
                    await self.ws.send(json.dumps({
                        "type": "command_result", "requestId": request_id,
                        "payload": {
                            "exitCode": exit_code,
                            "stdout": (stdout or b"").decode("utf-8", errors="replace"),
                            "stderr": (stderr or b"").decode("utf-8", errors="replace"),
                            "killed": killed,
                        },
                    }))
                self.add_log("INFO", f"命令完成，退出码: {exit_code}")
            except Exception as e:
                self.add_log("ERROR", f"命令执行失败: {e}")

        elif msg_type == "read_file":
            path = payload.get("path", "")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                if self.ws:
                    await self.ws.send(json.dumps({
                        "type": "file_result", "requestId": request_id,
                        "payload": {"success": True, "content": content, "path": path},
                    }))
                self.add_log("INFO", f"读取文件: {path}")
            except Exception as e:
                if self.ws:
                    await self.ws.send(json.dumps({
                        "type": "file_result", "requestId": request_id,
                        "payload": {"success": False, "error": str(e), "path": path},
                    }))
                self.add_log("ERROR", f"读取失败: {e}")

        elif msg_type == "write_file":
            path = payload.get("path", "")
            content = payload.get("content", "")
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                if self.ws:
                    await self.ws.send(json.dumps({
                        "type": "file_result", "requestId": request_id,
                        "payload": {"success": True, "path": path},
                    }))
                self.add_log("INFO", f"写入文件: {path}")
            except Exception as e:
                if self.ws:
                    await self.ws.send(json.dumps({
                        "type": "file_result", "requestId": request_id,
                        "payload": {"success": False, "error": str(e), "path": path},
                    }))
                self.add_log("ERROR", f"写入失败: {e}")

        elif msg_type == "get_device_info":
            if self.ws:
                await self.ws.send(json.dumps({
                    "type": "device_info", "requestId": request_id,
                    "payload": {
                        "hostname": platform.node(),
                        "platform": sys.platform,
                        "arch": platform.machine(),
                        "cpus": os.cpu_count() or 0,
                        "uptime": time.time(),
                        "homedir": str(Path.home()),
                        "userInfo": {"username": os.getlogin()},
                    },
                }))


# ─── WebView API ──────────────────────────────
class Api:
    def __init__(self):
        self.client = None
        self.ws_thread = None

    def get_initial(self):
        cfg = load_config()
        state["psk"] = cfg.get("psk", "")
        state["relayUrl"] = cfg.get("relayUrl", DEFAULT_RELAY)
        state["pairCode"] = generate_code()
        state["deviceName"] = cfg.get("deviceName", platform.node())
        return {
            "pairCode": state["pairCode"],
            "psk": state["psk"],
            "relayUrl": state["relayUrl"],
            "deviceName": state["deviceName"],
            "version": "1.0.0",
        }

    def refresh_code(self):
        state["pairCode"] = generate_code()
        return {"pairCode": state["pairCode"]}

    def save_config(self, psk, relay_url, device_name):
        state["psk"] = psk
        state["relayUrl"] = relay_url
        state["deviceName"] = device_name
        save_config({"psk": psk, "relayUrl": relay_url, "deviceName": device_name})
        return {"ok": True}

    def start_connect(self):
        if self.client and not self.client._stop:
            self.client._stop = True
        self.client = RelayClient(self)
        self.client._stop = False

        def run_loop():
            asyncio.run(self.client.run())

        self.ws_thread = threading.Thread(target=run_loop, daemon=True)
        self.ws_thread.start()
        return {"ok": True}

    def stop_connect(self):
        if self.client:
            self.client._stop = True
            if self.client.ws:
                # 不能直接 close asyncio 对象，标记即可
                pass
        return {"ok": True}

    def copy_code(self, code):
        state["pairCode"] = code
        return {"ok": True}


# ─── 启动 ────────────────────────────────────
def main():
    ui_path = Path(__file__).parent / "ui.html"
    if not ui_path.exists():
        # 回退到内嵌 HTML
        print("ui.html 不存在")
        sys.exit(1)

    api = Api()
    window = webview.create_window(
        "云桥 MCP",
        str(ui_path),
        js_api=api,
        width=360,
        height=580,
        resizable=False,
        text_select=True,
    )
    webview.start(
        private_mode=False,
        storage_path=str(CONFIG_DIR),
    )


if __name__ == "__main__":
    main()
