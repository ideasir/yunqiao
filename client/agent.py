"""
云桥 - Windows 本地代理（CLI 版）
用法: python agent.py
"""

import asyncio
import json
import os
import platform
import subprocess
import sys
import time
import shutil
from pathlib import Path


# ─── 会话管理（供 desktop.py 导入） ───
class Session:
    def __init__(self, sid, name, work_dir):
        self.id = sid
        self.name = name
        self.workDir = work_dir
        self.cwd = work_dir
        self.alive = True
        self.lastActive = time.time()

    def to_dict(self):
        return {"id": self.id, "name": self.name, "workDir": self.workDir,
                "cwd": self.cwd, "alive": self.alive, "lastActive": self.lastActive}


class SessionManager:
    def __init__(self):
        self.sessions = []
        self.default_id = None

    def create(self, work_dir, name=None):
        import uuid
        sid = uuid.uuid4().hex[:8]
        name = name or f"session-{sid}"
        s = Session(sid, name, work_dir)
        self.sessions.append(s)
        self.default_id = sid
        return {"success": True, "id": sid, "name": name, "workDir": work_dir, "cwd": work_dir}

    def get_current(self):
        for s in self.sessions:
            if s.id == self.default_id:
                s.lastActive = time.time()
                return s
        return None

    def close(self, session_id=None):
        sid = session_id or self.default_id
        for s in self.sessions:
            if s.id == sid:
                self.sessions.remove(s)
                if self.default_id == sid:
                    self.default_id = self.sessions[0].id if self.sessions else None
                return {"success": True}
        return {"success": False, "error": f"会话 {sid} 不存在"}

    def list_all(self):
        return {"sessions": [s.to_dict() for s in self.sessions], "defaultId": self.default_id}

    def switch(self, session_id):
        for s in self.sessions:
            if s.id == session_id:
                self.default_id = session_id
                return {"success": True, "sessionId": s.id, "name": s.name, "workDir": s.workDir}
        return {"success": False, "error": f"会话 {session_id} 不存在"}


# ─── 配置 ───────────────────────────────────────
import sys
import time

RELAY_URL = os.environ.get("RELAY_URL", "")
RELAY_KEY = os.environ.get("RELAY_KEY", "")
DEVICE_NAME = os.environ.get("DEVICE_NAME", platform.node())
RECONNECT_DELAY = int(os.environ.get("RECONNECT_DELAY", "5000"))

try:
    import websockets
except ImportError:
    print("正在安装 websockets 库...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets
    print("安装完成!")


async def handle_command(ws, msg_type, request_id, payload):
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

            await ws.send(json.dumps({
                "type": "command_result", "requestId": request_id,
                "payload": {
                    "exitCode": exit_code,
                    "stdout": stdout.decode("utf-8", errors="replace") if stdout else "",
                    "stderr": stderr.decode("utf-8", errors="replace") if stderr else "",
                    "killed": killed,
                },
            }))
        except Exception as e:
            await ws.send(json.dumps({
                "type": "command_result", "requestId": request_id,
                "payload": {"exitCode": 1, "stdout": "", "stderr": str(e), "killed": False},
            }))

    elif msg_type == "read_file":
        path = payload.get("path", "")
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            size = os.path.getsize(path)
            await ws.send(json.dumps({
                "type": "file_result", "requestId": request_id,
                "payload": {"success": True, "content": content, "size": size, "path": path},
            }))
        except Exception as e:
            await ws.send(json.dumps({
                "type": "file_result", "requestId": request_id,
                "payload": {"success": False, "error": str(e), "path": path},
            }))

    elif msg_type == "write_file":
        path = payload.get("path", "")
        content = payload.get("content", "")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            await ws.send(json.dumps({
                "type": "file_result", "requestId": request_id,
                "payload": {"success": True, "path": path},
            }))
        except Exception as e:
            await ws.send(json.dumps({
                "type": "file_result", "requestId": request_id,
                "payload": {"success": False, "error": str(e), "path": path},
            }))

    elif msg_type == "get_device_info":
        import shutil
        await ws.send(json.dumps({
            "type": "device_info", "requestId": request_id,
            "payload": {
                "hostname": platform.node(),
                "platform": sys.platform,
                "arch": platform.machine(),
                "cpus": os.cpu_count() or 0,
                "totalMem": 0,
                "freeMem": 0,
                "uptime": time.time(),
                "homedir": os.path.expanduser("~"),
                "userInfo": {"username": os.getlogin()},
            },
        }))


async def connect():
    print(f"[agent] connecting to {RELAY_URL}...")

    async def run():
        while True:
            try:
                async with websockets.connect(RELAY_URL, extra_headers={"X-Key": RELAY_KEY}, ping_interval=30) as ws:
                    print(f"[agent] connected!")
                    await ws.send(json.dumps({
                        "type": "register",
                        "deviceName": DEVICE_NAME,
                        "os": sys.platform,
                        "arch": platform.machine(),
                        "hostname": platform.node(),
                    }))

                    async for message in ws:
                        try:
                            msg = json.loads(message)
                        except json.JSONDecodeError:
                            continue

                        msg_type = msg.get("type")
                        request_id = msg.get("requestId")

                        if msg_type == "register_result" and msg.get("success"):
                            print(f"[agent] registered as device: {msg.get('deviceId')}")
                            continue

                        await handle_command(ws, msg_type, request_id, msg.get("payload", {}))

            except websockets.exceptions.ConnectionClosed:
                print(f"[agent] disconnected, reconnecting in {RECONNECT_DELAY}ms...")
            except Exception as e:
                print(f"[agent] error: {e}, reconnecting in {RECONNECT_DELAY}ms...")

            await asyncio.sleep(RECONNECT_DELAY / 1000)

    await run()


if __name__ == "__main__":
    if not RELAY_URL:
        print("❌ 必须设置 RELAY_URL 环境变量（云桥 WebSocket 地址）")
        sys.exit(1)
    if not RELAY_KEY:
        print("❌ 必须设置 RELAY_KEY 环境变量")
        sys.exit(1)
    print(f"[agent] starting, device name: {DEVICE_NAME}")
    asyncio.run(connect())
