"""
云桥 - Agent 核心引擎
====================
负责：连接中继、注册设备、接收命令、执行命令、会话管理
不负责：UI 显示（那是 desktop.py 的事）

用法：
  # 命令行
  python agent.py

  # 被 desktop.py 嵌入
  from agent import Agent
  agent = Agent(relay_url, relay_key, device_name)
  agent.on_log = lambda msg: print(msg)
  agent.on_status = lambda s: print(s)
  agent.start()
"""

import asyncio
import json
import os
import platform
import sys
import time
import threading
from pathlib import Path


# ═══════════════════════════════════════════════════════════
# 会话管理
# ═══════════════════════════════════════════════════════════

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
        return {"success": False, "error": f"会话不存在: {sid}"}

    def list_all(self):
        return {"sessions": [s.to_dict() for s in self.sessions], "defaultId": self.default_id}

    def switch(self, session_id):
        for s in self.sessions:
            if s.id == session_id:
                self.default_id = session_id
                return {"success": True, "sessionId": s.id, "name": s.name, "workDir": s.workDir}
        return {"success": False, "error": f"会话不存在: {session_id}"}


# ═══════════════════════════════════════════════════════════
# Agent 核心
# ═══════════════════════════════════════════════════════════

class Agent:
    """云桥 Agent 核心引擎
    
    职责：
    - 连接中继服务器（WebSocket）
    - 注册设备，上报配对码
    - 接收并执行上游命令（execute_command / read_file / write_file / session_op）
    - 通过回调函数通知 desktop.py 状态变化
    
    不负责：
    - UI 渲染
    - 设置管理（由 desktop.py 的 config.json 负责）
    """
    
    def __init__(self, relay_url, relay_key, device_name=None, auth_code=None):
        self.relay_url = relay_url
        self.relay_key = relay_key
        self.device_name = device_name or platform.node()
        self.auth_code = auth_code  # 配对码（desktop 用，CLI 不用）
        self.device_id = None
        self.connected = False
        
        # 权限模式: workspace（仅工作区）/ super（全盘）
        self.permission = "workspace"
        
        # 会话管理
        self.sessions = SessionManager()
        
        # 默认工作区：项目根目录下的 worker 目录
        script_dir = Path(__file__).parent.parent  # client/ 的上一级 = 项目根
        self.default_work_dir = str(script_dir / 'worker')
        os.makedirs(self.default_work_dir, exist_ok=True)
        self.sessions.create(self.default_work_dir, '默认工作区')
        
        # 回调函数（由 desktop.py 设置）
        self.on_log = lambda msg: None       # 日志消息
        self.on_status = lambda status: None  # 连接状态变化
        self.on_command = lambda cmd: None    # 收到命令时
        self.on_result = lambda result: None  # 命令执行结果
        
        # 内部状态
        self._ws = None
        self._loop = None
        self._thread = None
        self._running = False
    
    def _emit(self, callback, *args):
        """线程安全地调用回调"""
        try:
            callback(*args)
        except Exception:
            pass
    
    def start(self):
        """启动 Agent（后台线程）"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        """停止 Agent"""
        self._running = False
        if self._loop and self._ws:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
    
    def update_code(self, code):
        """更新配对码并同步到中继"""
        self.auth_code = code
        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._ws.send(json.dumps({
                    "type": "update_code",
                    "authCode": code,
                })),
                self._loop
            )
    
    def send_message(self, text):
        """发送消息给上游 Agent"""
        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._ws.send(json.dumps({
                    "type": "agent_message",
                    "text": text,
                })),
                self._loop
            )
    
    def set_permission(self, mode):
        """设置权限模式: workspace 或 super"""
        self.permission = mode
    
    # ── 内部实现 ──────────────────────────────
    
    def _run_loop(self):
        """后台线程：运行 asyncio 事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect())
    
    async def _connect(self):
        """WebSocket 连接循环"""
        import websockets
        self._emit(self.on_log, f"正在连接 {self.relay_url}")
        
        while self._running:
            try:
                try:
                    ws = await websockets.connect(
                        self.relay_url,
                        extra_headers={"X-Key": self.relay_key, "X-PSK": self.relay_key},
                        ping_interval=15
                    )
                except TypeError:
                    ws = await websockets.connect(
                        self.relay_url,
                        additional_headers={"X-Key": self.relay_key, "X-PSK": self.relay_key},
                        ping_interval=15
                    )
                async with ws:
                    self._ws = ws
                    self.connected = True
                    self._emit(self.on_log, "已连接到中继服务器")
                    self._emit(self.on_status, {"connected": True})
                    
                    # 注册设备
                    await ws.send(json.dumps({
                        "type": "register",
                        "deviceName": self.device_name,
                        "os": sys.platform,
                        "arch": platform.machine(),
                        "hostname": platform.node(),
                        "authCode": self.auth_code,
                    }))
                    
                    # 处理消息
                    async for message in ws:
                        try:
                            msg = json.loads(message)
                        except json.JSONDecodeError:
                            continue
                        
                        await self._handle_message(msg)
                    
            except Exception as e:
                self.connected = False
                self._ws = None
                self._emit(self.on_log, f"连接断开: {e}")
                self._emit(self.on_status, {"connected": False})
            
            if self._running:
                await asyncio.sleep(5)
    
    async def _handle_message(self, msg):
        """处理服务器下发的消息"""
        msg_type = msg.get("type")
        rid = msg.get("requestId")
        payload = msg.get("payload", {})
        
        if msg_type == "register_result" and msg.get("success"):
            self.device_id = msg.get("deviceId", "")
            self._emit(self.on_log, f"注册成功: {self.device_id[:8]}...")
            return
        
        if msg_type == "notify":
            self._emit(self.on_log, f"[通知] {msg.get('text', '')}")
            return
        
        if msg_type == "agent_connected":
            self._emit(self.on_status, {"agent": "connected", "connected": True})
            return
        
        if msg_type == "agent_disconnected":
            self._emit(self.on_status, {"agent": "disconnected", "connected": True})
            return
        
        if msg_type == "execute_command":
            cmd = payload.get("command", "")
            # 工作区模式检查
            ok, err = self._check_command(cmd)
            if not ok:
                await self._send("command_result", rid, {"exitCode": 1, "stdout": "", "stderr": err, "killed": False})
                return
            self._emit(self.on_command, {"type": "execute", "command": cmd})
            result = await self._exec_cmd(cmd, payload.get("timeout", 30000))
            await self._send("command_result", rid, result)
            self._emit(self.on_result, result)
            cwd = self.sessions.get_current()
            cwd_str = cwd.cwd if cwd else os.getcwd()
            self._emit(self.on_log, f"[执行] {cmd[:80]}")
            self._emit(self.on_log, f"[目录] {cwd_str}")
            return
        
        if msg_type == "read_file":
            path = payload.get("path", "")
            self._emit(self.on_command, {"type": "read", "path": path})
            result = self._read_file(path)
            await self._send("file_result", rid, result)
            if result.get("success"):
                size = len(result.get("content", ""))
                self._emit(self.on_log, f"[读取] {path} ({size} 字节)")
            else:
                self._emit(self.on_log, f"[读取] {path} 失败: {result.get('error','')}")
            return
        
        if msg_type == "write_file":
            path = payload.get("path", "")
            self._emit(self.on_command, {"type": "write", "path": path})
            # 判断文件是否存在（新建 vs 修改）
            existed = os.path.exists(path)
            result = self._write_file(path, payload.get("content", ""))
            await self._send("file_result", rid, result)
            if result.get("success"):
                size = len(payload.get("content", ""))
                action = "修改" if existed else "新建"
                self._emit(self.on_log, f"[{action}] {path} ({size} 字节)")
            else:
                self._emit(self.on_log, f"[写入] {path} 失败: {result.get('error','')}")
            return
        
        if msg_type == "get_device_info":
            result = self._get_info()
            await self._send("device_info", rid, result)
            return
        
        if msg_type == "download":
            path = payload.get("path", "")
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            try:
                import base64
                with open(path, "rb") as f:
                    data = base64.b64encode(f.read()).decode()
                size = os.path.getsize(path)
                await self._send("download_result", rid, {"success": True, "data": data, "size": size, "path": path})
            except Exception as e:
                await self._send("download_result", rid, {"success": False, "error": str(e)})
            return
        
        if msg_type == "session_op":
            op = payload.get("op", "")
            self._emit(self.on_command, {"type": "session", "op": op})
            result = await self._handle_session_op(op, payload)
            await self._send("session_op_result", rid, result)
            if op == "exec" and "exitCode" in result:
                cwd = self.sessions.get_current()
                cwd_str = cwd.cwd if cwd else os.getcwd()
                self._emit(self.on_log, f"[执行] {payload.get('command','')[:80]}")
                self._emit(self.on_log, f"[目录] {cwd_str}")
            elif op == "read_file":
                self._emit(self.on_log, f"[读取] {payload.get('path','')}")
            elif op == "write_file":
                self._emit(self.on_log, f"[写入] {payload.get('path','')}")
            return
    
    async def _send(self, msg_type, rid, payload):
        if self._ws:
            await self._ws.send(json.dumps({
                "type": msg_type, "requestId": rid, "payload": payload
            }))
    
    # ── 命令执行 ──────────────────────────────
    
    async def _exec_cmd(self, command, timeout, cwd=None):
        # 工作区模式检查命令
        ok, err = self._check_command(command)
        if not ok:
            return {"exitCode": 1, "stdout": "", "stderr": err, "killed": False}
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout / 1000
                )
                return {"exitCode": proc.returncode or 0,
                        "stdout": stdout.decode("utf-8", errors="replace") if stdout else "",
                        "stderr": stderr.decode("utf-8", errors="replace") if stderr else "",
                        "killed": False}
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()
                return {"exitCode": 1,
                        "stdout": stdout.decode("utf-8", errors="replace") if stdout else "",
                        "stderr": stderr.decode("utf-8", errors="replace") if stderr else "",
                        "killed": True}
        except Exception as e:
            return {"exitCode": 1, "stdout": "", "stderr": str(e), "killed": False}
    
    def _check_path(self, path):
        """检查路径是否在工作区范围内"""
        if self.permission != "workspace":
            return True
        session = self.sessions.get_current()
        if not session:
            return False
        workspace = os.path.normpath(session.workDir)
        resolved = os.path.normpath(path)
        return resolved == workspace or resolved.startswith(workspace + os.sep)
    
    def _check_command(self, command):
        """检查命令是否可能逃逸工作区"""
        if self.permission != "workspace":
            return True, ""
        import re
        # 检测绝对路径 C:\ D:\ / 等
        if re.search(r'\b[A-Za-z]:\\', command):
            return False, "工作区模式禁止使用绝对路径"
        # 检测 .. 逃逸
        if re.search(r'(?:^|\s|[&|;])\.\.(?:\\|/|\s|$)', command):
            return False, "工作区模式禁止使用 .. 逃逸"
        # 检测 cd 命令
        if re.search(r'(?:^|\s|[&|;])cd\s', command, re.IGNORECASE):
            return False, "工作区模式禁止 cd 命令"
        # 检测 pushd/popd
        if re.search(r'(?:^|\s|[&|;])(?:pushd|popd)\s', command, re.IGNORECASE):
            return False, "工作区模式禁止 pushd/popd"
        return True, ""
    
    def _read_file(self, path):
        try:
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            path = os.path.normpath(path)
            if not self._check_path(path):
                return {"success": False, "error": "超出工作区范围", "path": path}
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            return {"success": True, "content": content, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e), "path": path}
    
    def _write_file(self, path, content):
        try:
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            path = os.path.normpath(path)
            if not self._check_path(path):
                return {"success": False, "error": "超出工作区范围", "path": path}
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e), "path": path}
    
    def _get_info(self):
        try:
            import psutil
            total = psutil.virtual_memory().total
            free = psutil.virtual_memory().available
        except:
            total = 0; free = 0
        return {
            "hostname": platform.node(), "platform": sys.platform,
            "arch": platform.machine(), "cpus": os.cpu_count() or 0,
            "totalMem": total, "freeMem": free,
            "uptime": time.time(), "homedir": str(Path.home()),
            "userInfo": {"username": os.getlogin()},
        }
    
    async def _handle_session_op(self, op, payload):
        try:
            if op == "create":
                return self.sessions.create(payload.get("workDir", ""), payload.get("name"))
            elif op == "exec":
                session = self.sessions.get_current()
                if not session:
                    return {"exitCode": 1, "stdout": "", "stderr": "没有当前会话", "killed": False}
                return await self._exec_cmd(payload.get("command", ""), payload.get("timeout", 30000), session.cwd)
            elif op == "read_file":
                session = self.sessions.get_current()
                if not session:
                    return {"success": False, "error": "没有当前会话"}
                return self._read_file(payload.get("path", ""))
            elif op == "write_file":
                session = self.sessions.get_current()
                if not session:
                    return {"success": False, "error": "没有当前会话"}
                return self._write_file(payload.get("path", ""), payload.get("content", ""))
            elif op == "close":
                return self.sessions.close(payload.get("sessionId"))
            elif op == "list":
                return self.sessions.list_all()
            elif op == "switch":
                return self.sessions.switch(payload.get("sessionId", ""))
            else:
                return {"success": False, "error": f"未知操作: {op}"}
        except Exception as e:
            return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    url = os.environ.get("RELAY_URL", "")
    key = os.environ.get("RELAY_KEY", "")
    name = os.environ.get("DEVICE_NAME", platform.node())
    
    if not url or not key:
        print("用法:")
        print("  set RELAY_URL=wss://your-server/device")
        print("  set RELAY_KEY=your-key")
        print("  python agent.py")
        sys.exit(1)
    
    agent = Agent(url, key, name)
    agent.on_log = lambda msg: print(f"[agent] {msg}")
    agent.on_status = lambda s: print(f"[agent] {'已连接' if s.get('connected') else '已断开'}")
    agent.on_command = lambda c: print(f"[agent] 收到命令: {c}")
    agent.on_result = lambda r: print(f"[agent] 结果: exitCode={r.get('exitCode', '?')}")
    agent.start()
    
    print(f"[agent] 已启动, 设备: {name}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        agent.stop()
        print("[agent] 已停止")