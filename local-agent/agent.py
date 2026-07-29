"""
云端协同 MCP - Windows 本地代理（Python 版）
用法: python agent.py
"""

import asyncio
import json
import os
import platform
import subprocess
import sys
import time

RELAY_URL = os.environ.get("RELAY_URL", "wss://yunqiao.very.im/device")
RELAY_PSK = os.environ.get("RELAY_PSK", "87ba9765c2aa80687c68fe955ea7829d1717afc77bd2f7eeb1b08e34ca0be01e")
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
    url = f"{RELAY_URL}?psk={RELAY_PSK}"
    print(f"[agent] connecting to {RELAY_URL}...")

    async def run():
        nonlocal url
        while True:
            try:
                async with websockets.connect(url, ping_interval=30) as ws:
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
    print(f"[agent] starting, device name: {DEVICE_NAME}")
    asyncio.run(connect())
