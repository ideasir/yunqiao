"""
云桥 - TCP Agent 核心引擎
========================
替代 WebSocket 版的 agent.py，通过 EasyTier 组网 TCP 直连中继服务器。
监听在 EasyTier 虚拟 IP 上，接收 JSON 命令并执行。

用法：
  python tcp_agent.py                    # 启动 TCP 服务端（监听）
  python tcp_agent.py --client           # 启动 TCP 客户端（连接中继）
"""

import asyncio
import json
import os
import platform
import sys
import time
import uuid
import base64
import subprocess
from pathlib import Path


# ═══════════════════════════════════════════════════════════
# 会话管理（复用 agent.py 逻辑）
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

    def _save_path(self):
        return os.path.join(os.environ.get("YUNQIAO_CONFIG", str(Path.home() / ".yunqiao")), "sessions.json")

    def load(self):
        p = self._save_path()
        if os.path.exists(p):
            try:
                data = json.loads(open(p, "r", encoding="utf-8").read())
                for s_data in data.get("sessions", []):
                    s = Session(s_data["id"], s_data.get("name", ""), s_data.get("workDir", ""))
                    s.cwd = s_data.get("cwd", s.workDir)
                    s.lastActive = s_data.get("lastActive", time.time())
                    s.alive = s_data.get("alive", True)
                    self.sessions.append(s)
                self.default_id = data.get("defaultId")
            except:
                pass

    def create(self, work_dir, name=None):
        sid = uuid.uuid4().hex[:8]
        name = name or f"session-{sid}"
        s = Session(sid, name, work_dir)
        self.sessions.append(s)
        self.default_id = sid
        self._save()
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
                self._save()
                return {"success": True}
        return {"success": False, "error": f"会话不存在: {sid}"}

    def list_all(self):
        return {"sessions": [s.to_dict() for s in self.sessions], "defaultId": self.default_id}

    def switch(self, session_id):
        for s in self.sessions:
            if s.id == session_id:
                self.default_id = session_id
                self._save()
                return {"success": True, "sessionId": s.id, "name": s.name, "workDir": s.workDir}
        return {"success": False, "error": f"会话不存在: {session_id}"}


# ═══════════════════════════════════════════════════════════
# TCP Agent
# ═══════════════════════════════════════════════════════════

class TCPAgent:
    """TCP Agent 核心引擎
    
    通过 EasyTier 组网 TCP 直连，接收 JSON 命令并执行。
    完全替代 WebSocket 版的 Agent。
    """
    
    def __init__(self, host="10.10.10.88", port=19999):
        self.host = host
        self.port = port
        self.device_name = platform.node()
        self.auth_code = None
        
        # 权限模式
        self.permission = "workspace"
        
        # 会话管理
        self.sessions = SessionManager()
        self.sessions.load()
        
        # 默认工作区
        if getattr(sys, 'frozen', False):
            base_dir = Path(sys.executable).parent
        else:
            base_dir = Path(__file__).parent.parent
        self.default_work_dir = str(base_dir / 'worker')
        os.makedirs(self.default_work_dir, exist_ok=True)
        if not self.sessions.sessions:
            self.sessions.create(self.default_work_dir, '默认工作区')
        
        # 回调
        self.on_log = lambda msg: print(f"[tcp-agent] {msg}")
        self.on_connected = lambda: None
        
        self._server = None
        self._running = False

    def start(self):
        """启动 TCP 服务端"""
        if self._running:
            return
        self._running = True
        self.on_log(f"监听 {self.host}:{self.port} (EasyTier 组网)")
        asyncio.run(self._serve())
    
    async def _serve(self):
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        self.on_connected()
        self.on_log(f"TCP 服务器已启动: {self.host}:{self.port}")
        async with self._server:
            await self._server.serve_forever()
    
    async def _handle_client(self, reader, writer):
        """处理客户端连接"""
        peer = writer.get_extra_info('peername')
        self.on_log(f"新连接: {peer}")
        try:
            # 接收消息（4 字节长度前缀 + JSON）
            data = await self._read_message(reader)
            if not data:
                return
            msg = json.loads(data)
            self.on_log(f"收到命令: {msg.get('type', '?')}")
            
            # 处理命令
            result = await self._handle_command(msg)
            
            # 返回结果
            await self._send_message(writer, result)
            
        except Exception as e:
            self.on_log(f"处理错误: {e}")
            try:
                await self._send_message(writer, {"error": str(e)})
            except:
                pass
        finally:
            writer.close()
    
    async def _read_message(self, reader):
        """读取消息：4字节长度前缀 + JSON"""
        header = await reader.readexactly(4)
        length = int.from_bytes(header, 'big')
        if length > 10 * 1024 * 1024:  # 10MB 限制
            raise ValueError("消息过长")
        return await reader.readexactly(length)
    
    async def _send_message(self, writer, obj):
        """发送消息：4字节长度前缀 + JSON"""
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        writer.write(len(data).to_bytes(4, 'big'))
        writer.write(data)
        await writer.drain()
    
    async def _handle_command(self, msg):
        """处理命令"""
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})
        
        if msg_type == "register":
            return {"success": True, "type": "register_result", "deviceId": self.device_name}
        
        elif msg_type == "execute_command":
            command = payload.get("command", "")
            timeout = payload.get("timeout", 30000)
            session = self.sessions.get_current()
            cwd = session.cwd if session else os.getcwd()
            result = await self._exec_cmd(command, timeout, cwd)
            return {"type": "command_result", "result": result}
        
        elif msg_type == "read_file":
            path = payload.get("path", "")
            result = self._read_file(path)
            return {"type": "file_result", "result": result}
        
        elif msg_type == "write_file":
            path = payload.get("path", "")
            result = self._write_file(path, payload.get("content", ""))
            return {"type": "file_result", "result": result}
        
        elif msg_type == "get_device_info":
            return {"type": "device_info", "result": self._get_info()}
        
        elif msg_type == "download":
            path = payload.get("path", "")
            result = self._download(path)
            return {"type": "download_result", "result": result}
        
        elif msg_type == "session_op":
            op = payload.get("op", "")
            result = await self._handle_session_op(op, payload)
            return {"type": "session_op_result", "result": result}
        
        elif msg_type == "ping":
            return {"type": "pong", "latency": 0}
        
        else:
            return {"type": "error", "error": f"未知命令: {msg_type}"}
    
    # ── 命令执行 ──
    
    async def _exec_cmd(self, command, timeout, cwd=None):
        def _decode(b):
            if not b: return ""
            for enc in ['gbk', 'utf-8']:
                try: return b.decode(enc)
                except: continue
            return b.decode('utf-8', errors='replace')
        
        t0 = time.time()
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
                        "stdout": _decode(stdout),
                        "stderr": _decode(stderr),
                        "killed": False,
                        "duration": int((time.time() - t0) * 1000)}
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()
                return {"exitCode": 1,
                        "stdout": _decode(stdout) if stdout else "",
                        "stderr": _decode(stderr) if stderr else "",
                        "killed": True,
                        "duration": int((time.time() - t0) * 1000)}
        except Exception as e:
            return {"exitCode": 1, "stdout": "", "stderr": str(e), "killed": False, "duration": 0}
    
    def _read_file(self, path):
        try:
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            path = os.path.normpath(path)
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            return {"success": True, "content": content, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _write_file(self, path, content):
        try:
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            path = os.path.normpath(path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _get_info(self):
        return {
            "hostname": platform.node(),
            "platform": sys.platform,
            "arch": platform.machine(),
            "cpuCores": os.cpu_count() or 0,
            "uptime": int(time.time()),
            "homeDir": str(Path.home()),
            "user": os.environ.get("USERNAME", ""),
        }
    
    def _download(self, path):
        try:
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            path = os.path.normpath(path)
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode()
            size = os.path.getsize(path)
            return {"success": True, "data": data, "size": size, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _handle_session_op(self, op, payload):
        if op == "exec":
            command = payload.get("command", "")
            timeout = payload.get("timeout", 30000)
            session = self.sessions.get_current()
            cwd = session.cwd if session else os.getcwd()
            return await self._exec_cmd(command, timeout, cwd)
        elif op == "read_file":
            return self._read_file(payload.get("path", ""))
        elif op == "write_file":
            return self._write_file(payload.get("path", ""), payload.get("content", ""))
        elif op == "create":
            return self.sessions.create(payload.get("workDir", ""), payload.get("name"))
        elif op == "close":
            return self.sessions.close(payload.get("sessionId"))
        elif op == "list":
            return self.sessions.list_all()
        elif op == "switch":
            return self.sessions.switch(payload.get("sessionId"))
        return {"error": f"未知会话操作: {op}"}


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="云桥 TCP Agent")
    parser.add_argument("--host", default="10.10.10.88", help="监听地址（EasyTier 虚拟 IP）")
    parser.add_argument("--port", type=int, default=19999, help="监听端口")
    parser.add_argument("--reverse", action="store_true", help="反向连接模式：主动连接中继服务器的 TCP 端口")
    parser.add_argument("--relay-ip", default="10.144.144.1", help="中继服务器 EasyTier IP")
    parser.add_argument("--relay-port", type=int, default=19998, help="中继服务器反向 TCP 端口")
    args = parser.parse_args()
    
    if args.reverse:
        # 反向连接模式：Windows 主动连接中继服务器
        import asyncio
        async def connect_reverse():
            reader, writer = await asyncio.open_connection(args.relay_ip, args.relay_port)
            print(f'[tcp-agent] 已连接到中继服务器: {args.relay_ip}:{args.relay_port}')
            # 创建 TCP Agent 实例处理命令
            agent = TCPAgent(host=args.host, port=args.port)
            # 用反向连接作为命令通道
            while True:
                try:
                    data = await reader.read(4096)
                    if not data:
                        break
                    # 处理命令
                    msg = json.loads(data.decode('utf-8'))
                    result = await agent._handle_command(msg)
                    writer.write(json.dumps(result).encode('utf-8'))
                    await writer.drain()
                except Exception as e:
                    print(f'[tcp-agent] 反向连接错误: {e}')
                    break
            writer.close()
        
        asyncio.run(connect_reverse())
    else:
        agent = TCPAgent(host=args.host, port=args.port)
        agent.on_log = lambda msg: print(f"[tcp-agent] {msg}")
        agent.start()