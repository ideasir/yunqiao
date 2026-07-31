"""
云端协同 MCP - Windows 本地代理（Python 版）
支持会话管理：持久工作区、cwd 保持、多会话隔离
"""

import asyncio
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path

RELAY_URL = os.environ.get("RELAY_URL")
if not RELAY_URL:
    try:
        import json
        config_path = Path.home() / ".yunqiao" / "config.json"
        if config_path.exists():
            cfg = json.loads(config_path.read_text("utf-8"))
            RELAY_URL = cfg.get("relayUrl", "")
    except:
        pass
if not RELAY_URL:
    RELAY_URL = "wss://yunqiao.very.im/device"
# 优先从环境变量读取，其次从配置文件读取，最后用默认值
RELAY_PSK = os.environ.get("RELAY_PSK")
if not RELAY_PSK:
    try:
        import json
        config_path = Path.home() / ".yunqiao" / "config.json"
        if config_path.exists():
            cfg = json.loads(config_path.read_text("utf-8"))
            RELAY_PSK = cfg.get("psk", "")
    except:
        pass
if not RELAY_PSK:
    RELAY_PSK = "yunqiao-mcp-key-2026"
DEVICE_NAME = os.environ.get("DEVICE_NAME", platform.node())
RECONNECT_DELAY = int(os.environ.get("RECONNECT_DELAY", "5000"))

try:
    import websockets
except ImportError:
    print("正在安装 websockets 库...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets
    print("安装完成!")

# ─── 会话配置 ────────────────────────────────
YUNQIAO_DIR = Path.home() / ".yunqiao"
SESSIONS_DIR = YUNQIAO_DIR / "sessions"
SESSIONS_INDEX = YUNQIAO_DIR / "sessions.json"
YUNQIAO_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


# ─── 会话管理器 ──────────────────────────────
class SessionManager:
    """管理多个工作会话，持久化到磁盘"""

    def __init__(self):
        self.sessions = {}  # session_id -> Session
        self.current_session_id = None
        self._load()

    def _load(self):
        """从磁盘加载会话"""
        if SESSIONS_INDEX.exists():
            try:
                data = json.loads(SESSIONS_INDEX.read_text("utf-8"))
                self.current_session_id = data.get("defaultSessionId")
                for sid in data.get("sessions", []):
                    sfile = SESSIONS_DIR / f"{sid}.json"
                    if sfile.exists():
                        sd = json.loads(sfile.read_text("utf-8"))
                        self.sessions[sid] = Session(sd)
            except Exception as e:
                print(f"[sessions] 加载失败: {e}")

    def _save_index(self):
        """保存会话索引"""
        data = {
            "defaultSessionId": self.current_session_id,
            "sessions": list(self.sessions.keys()),
        }
        SESSIONS_INDEX.write_text(json.dumps(data, indent=2), "utf-8")

    def create(self, work_dir, name=None):
        """创建新会话并设为当前（同目录复用）"""
        # 检查是否已有同目录的会话
        for s in self.sessions.values():
            if s.workDir == work_dir:
                self.current_session_id = s.id
                self._save_index()
                s.lastActive = time.time()
                s._save()
                return s.to_dict()
        # 新建
        sid = uuid.uuid4().hex[:8]
        if not name:
            name = f"workspace_{sid}"
        session = Session(
            {
                "id": sid,
                "name": name,
                "workDir": work_dir,
                "cwd": work_dir,
                "createdAt": time.time(),
                "lastActive": time.time(),
            }
        )
        self.sessions[sid] = session
        self.current_session_id = sid
        session._save()
        self._save_index()
        return session.to_dict()

    def get_current(self):
        """获取当前会话"""
        if not self.current_session_id:
            return None
        return self.sessions.get(self.current_session_id)

    def switch(self, sid):
        """切换当前会话"""
        if sid in self.sessions:
            self.current_session_id = sid
            self._save_index()
            s = self.sessions[sid]
            return {"success": True, "sessionId": sid, "name": s.name, "workDir": s.workDir}
        return {"success": False, "error": f"会话 {sid} 不存在"}

    def close(self, sid=None):
        """关闭会话"""
        if sid is None:
            sid = self.current_session_id
        if sid in self.sessions:
            self.sessions[sid].close()
            sfile = SESSIONS_DIR / f"{sid}.json"
            if sfile.exists():
                sfile.unlink()
            del self.sessions[sid]
            if self.current_session_id == sid:
                self.current_session_id = next(iter(self.sessions)) if self.sessions else None
            self._save_index()
            return {"success": True}
        return {"success": False, "error": f"会话 {sid} 不存在"}

    def list_all(self):
        """列出所有会话"""
        sessions = [s.to_dict() for s in self.sessions.values()]
        # 标记当前默认会话
        for s in sessions:
            s["isDefault"] = s["id"] == self.current_session_id
        return {"sessions": sessions}


class Session:
    """单个工作会话，保持 cwd"""

    def __init__(self, data):
        self.id = data["id"]
        self.name = data["name"]
        self.workDir = data["workDir"]
        self.cwd = data.get("cwd", data["workDir"])
        self.createdAt = data.get("createdAt", time.time())
        self.lastActive = data.get("lastActive", time.time())
        self.shell = None  # 持久 shell 进程（后续扩展）

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "workDir": self.workDir,
            "cwd": self.cwd,
            "createdAt": self.createdAt,
            "lastActive": self.lastActive,
            "alive": self.shell is not None and self.shell.returncode is None,
        }

    def _save(self):
        sfile = SESSIONS_DIR / f"{self.id}.json"
        sfile.write_text(json.dumps(self.to_dict(), indent=2), "utf-8")

    async def exec(self, command, timeout=30000):
        """在会话中执行命令，保持 cwd"""
        self.lastActive = time.time()

        # 处理 cd 命令：直接更新 cwd，不执行 shell
        cmd_stripped = command.strip()
        if cmd_stripped.startswith("cd "):
            new_dir = cmd_stripped[3:].strip().strip('"')
            if not os.path.isabs(new_dir):
                new_dir = os.path.join(self.cwd, new_dir)
            new_dir = os.path.normpath(new_dir)
            if os.path.isdir(new_dir):
                self.cwd = new_dir
                self._save()
                return {"exitCode": 0, "stdout": f"cwd -> {self.cwd}", "stderr": "", "killed": False}
            else:
                return {"exitCode": 1, "stdout": "", "stderr": f"目录不存在: {new_dir}", "killed": False}

        # 在 cwd 下执行（一键执行）
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
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

            self._save()
            return {
                "exitCode": exit_code,
                "stdout": stdout.decode("utf-8", errors="replace") if stdout else "",
                "stderr": stderr.decode("utf-8", errors="replace") if stderr else "",
                "killed": killed,
            }
        except Exception as e:
            return {"exitCode": 1, "stdout": "", "stderr": str(e), "killed": False}

    async def _ensure_shell(self):
        """确保持久 shell 进程在运行"""
        if self.shell is not None and self.shell.returncode is None:
            return
        self.shell = await asyncio.create_subprocess_exec(
            'powershell', '-NoExit', '-Command', '-',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self.cwd,
        )
        await asyncio.sleep(0.5)

    async def _exec_persistent(self, command, timeout=30000):
        """在持久 shell 中执行命令"""
        await self._ensure_shell()
        marker = f'__CMD_DONE_{int(time.time() * 1000000)}__'
        # 写入命令 + 标记行（用 \n 换行）
        full_cmd = f'{command}\nWrite-Host \"{marker}\"\n'
        self.shell.stdin.write(full_cmd.encode('utf-8'))
        await self.shell.stdin.drain()
        # 读取输出直到标记
        stdout_lines = []
        try:
            while True:
                line = await asyncio.wait_for(
                    self.shell.stdout.readline(),
                    timeout=timeout / 1000
                )
                decoded = line.decode('utf-8', errors='replace').rstrip('\r\n')
                if marker in decoded:
                    break
                stdout_lines.append(decoded)
        except asyncio.TimeoutError:
            return {"exitCode": 1, "stdout": '\n'.join(stdout_lines), "stderr": "", "killed": True}
        stdout = '\n'.join(stdout_lines)
        self._save()
        return {"exitCode": 0, "stdout": stdout, "stderr": "", "killed": False}

    async def read_file(self, path):
        """读取文件，相对路径基于 cwd"""
        self.lastActive = time.time()
        if not os.path.isabs(path):
            path = os.path.join(self.cwd, path)
        path = os.path.normpath(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self._save()
            return {"success": True, "content": content, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e), "path": path}

    async def write_file(self, path, content):
        """写入文件，相对路径基于 cwd"""
        self.lastActive = time.time()
        if not os.path.isabs(path):
            path = os.path.join(self.cwd, path)
        path = os.path.normpath(path)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self._save()
            return {"success": True, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e), "path": path}

    def close(self):
        """关闭会话，清理资源"""
        if self.shell and self.shell.returncode is None:
            try:
                self.shell.kill()
            except:
                pass


# ─── 全局会话管理器 ──────────────────────────
session_mgr = SessionManager()


# ─── 消息处理 ────────────────────────────────
async def handle_command(ws, msg_type, request_id, payload):
    # ── 旧版消息（兼容） ──
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

            await ws.send(
                json.dumps(
                    {
                        "type": "command_result",
                        "requestId": request_id,
                        "payload": {
                            "exitCode": exit_code,
                            "stdout": stdout.decode("utf-8", errors="replace") if stdout else "",
                            "stderr": stderr.decode("utf-8", errors="replace") if stderr else "",
                            "killed": killed,
                        },
                    }
                )
            )
        except Exception as e:
            await ws.send(
                json.dumps(
                    {
                        "type": "command_result",
                        "requestId": request_id,
                        "payload": {"exitCode": 1, "stdout": "", "stderr": str(e), "killed": False},
                    }
                )
            )
        return

    if msg_type == "read_file":
        path = payload.get("path", "")
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            await ws.send(
                json.dumps(
                    {
                        "type": "file_result",
                        "requestId": request_id,
                        "payload": {"success": True, "content": content, "path": path},
                    }
                )
            )
        except Exception as e:
            await ws.send(
                json.dumps(
                    {
                        "type": "file_result",
                        "requestId": request_id,
                        "payload": {"success": False, "error": str(e), "path": path},
                    }
                )
            )
        return

    if msg_type == "write_file":
        path = payload.get("path", "")
        content = payload.get("content", "")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            await ws.send(
                json.dumps(
                    {
                        "type": "file_result",
                        "requestId": request_id,
                        "payload": {"success": True, "path": path},
                    }
                )
            )
        except Exception as e:
            await ws.send(
                json.dumps(
                    {
                        "type": "file_result",
                        "requestId": request_id,
                        "payload": {"success": False, "error": str(e), "path": path},
                    }
                )
            )
        return

    if msg_type == "agent_message":
        text = payload.get("text", "")
        print(f"[agent] 收到消息: {text}")
        await ws.send(
            json.dumps({
                "type": "agent_message_result",
                "requestId": request_id,
                "success": True,
            })
        )
        return

    if msg_type == "get_device_info":
        await ws.send(
            json.dumps(
                {
                    "type": "device_info",
                    "requestId": request_id,
                    "payload": {
                        "hostname": platform.node(),
                        "platform": sys.platform,
                        "arch": platform.machine(),
                        "cpus": os.cpu_count() or 0,
                        "totalMem": 0,
                        "freeMem": 0,
                        "uptime": time.time(),
                        "homedir": str(Path.home()),
                        "userInfo": {"username": os.getlogin()},
                    },
                }
            )
        )
        return

    # ── 会话管理消息（新） ──
    if msg_type == "session_op":
        op = payload.get("op", "")
        try:
            if op == "create":
                result = session_mgr.create(payload.get("workDir", ""), payload.get("name"))
                await ws.send(
                    json.dumps(
                        {
                            "type": "session_op_result",
                            "requestId": request_id,
                            "payload": result,
                        }
                    )
                )
            elif op == "exec":
                session = session_mgr.get_current()
                if not session:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "session_op_result",
                                "requestId": request_id,
                                "payload": {"exitCode": 1, "stdout": "", "stderr": "没有当前会话，请先 create_session"},
                                "killed": False,
                            }
                        )
                    )
                    return
                result = await session.exec(payload.get("command", ""), payload.get("timeout", 30000))
                await ws.send(
                    json.dumps(
                        {
                            "type": "session_op_result",
                            "requestId": request_id,
                            "payload": result,
                        }
                    )
                )
            elif op == "read_file":
                session = session_mgr.get_current()
                if not session:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "session_op_result",
                                "requestId": request_id,
                                "payload": {"success": False, "error": "没有当前会话"},
                            }
                        )
                    )
                    return
                result = await session.read_file(payload.get("path", ""))
                await ws.send(
                    json.dumps(
                        {
                            "type": "session_op_result",
                            "requestId": request_id,
                            "payload": result,
                        }
                    )
                )
            elif op == "write_file":
                session = session_mgr.get_current()
                if not session:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "session_op_result",
                                "requestId": request_id,
                                "payload": {"success": False, "error": "没有当前会话"},
                            }
                        )
                    )
                    return
                result = await session.write_file(payload.get("path", ""), payload.get("content", ""))
                await ws.send(
                    json.dumps(
                        {
                            "type": "session_op_result",
                            "requestId": request_id,
                            "payload": result,
                        }
                    )
                )
            elif op == "close":
                result = session_mgr.close(payload.get("sessionId"))
                await ws.send(
                    json.dumps(
                        {
                            "type": "session_op_result",
                            "requestId": request_id,
                            "payload": result,
                        }
                    )
                )
            elif op == "list":
                result = session_mgr.list_all()
                await ws.send(
                    json.dumps(
                        {
                            "type": "session_op_result",
                            "requestId": request_id,
                            "payload": result,
                        }
                    )
                )
            elif op == "switch":
                result = session_mgr.switch(payload.get("sessionId", ""))
                await ws.send(
                    json.dumps(
                        {
                            "type": "session_op_result",
                            "requestId": request_id,
                            "payload": result,
                        }
                    )
                )
            else:
                await ws.send(
                    json.dumps(
                        {
                            "type": "session_op_result",
                            "requestId": request_id,
                            "payload": {"success": False, "error": f"未知操作: {op}"},
                        }
                    )
                )
        except Exception as e:
            await ws.send(
                json.dumps(
                    {
                        "type": "session_op_result",
                        "requestId": request_id,
                        "payload": {"success": False, "error": str(e)},
                    }
                )
            )
        return


# ─── 连接管理 ────────────────────────────────
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

async def connect():
    url = RELAY_URL
    print(f"[agent] connecting to {RELAY_URL}...")
    ws_kwargs = await _ws_connect_headers()
    ws_kwargs['ping_interval'] = 30

    while True:
        try:
            async with websockets.connect(url, **ws_kwargs) as ws:
                print(f"[agent] connected!")
                await ws.send(
                    json.dumps(
                        {
                            "type": "register",
                            "deviceName": DEVICE_NAME,
                            "os": sys.platform,
                            "arch": platform.machine(),
                            "hostname": platform.node(),
                        }
                    )
                )

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


if __name__ == "__main__":
    print(f"[agent] starting, device name: {DEVICE_NAME}")
    print(f"[agent] sessions dir: {SESSIONS_DIR}")
    print(f"[agent] existing sessions: {len(session_mgr.sessions)}")
    asyncio.run(connect())