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

# 管理员级路径白名单（与 relay 的 ALLOWED_FILE_PREFIX 一致，可选；统一转正斜杠）
ALLOWED_FILE_PREFIX = os.environ.get('ALLOWED_FILE_PREFIX', '').strip().replace('\\', '/').rstrip('/')


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

    def _save(self):
        try:
            p = self._save_path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w", encoding="utf-8").write(json.dumps({
                "sessions": [s.to_dict() for s in self.sessions],
                "defaultId": self.default_id,
            }, ensure_ascii=False, indent=2))
        except:
            pass

    def create(self, work_dir, name=None):
        import uuid
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
        self.sessions.load()
        
        # 默认工作区：打包成 exe 时在 exe 旁边（绿色版便携，文件持久）；
        # 源码运行时在项目根。不能用 __file__（PyInstaller 里指向临时解压目录，文件会"消失"）
        if getattr(sys, 'frozen', False):
            base_dir = Path(sys.executable).parent  # exe 所在目录
        else:
            base_dir = Path(__file__).parent.parent  # 项目根
        self.default_work_dir = str(base_dir / 'worker')
        os.makedirs(self.default_work_dir, exist_ok=True)
        # 只有首次启动（无任何会话）才自动创建默认工作区
        if not self.sessions.sessions:
            self.sessions.create(self.default_work_dir, '默认工作区')
        
        # 回调函数（由 desktop.py 设置）
        self.on_log = lambda msg: None       # 日志消息
        self.on_status = lambda status: None  # 连接状态变化
        self.on_command = lambda cmd: None    # 收到命令时
        self.on_result = lambda result: None  # 命令执行结果
        self.on_messages_read = lambda ids: None  # 上游 Agent 已读消息回执
        self.on_activity = lambda a: None  # 上游 Agent 活跃度（连接数/任务数/调用数）
        
        # 内部状态
        self._ws = None
        self._loop = None
        self._thread = None
        self._running = False
        self.pending_messages = {}  # msgId -> {text, urgent, time}，等待上游 Agent 已读回执
        self._ticket_waiter = None  # 动态 MCP 地址 ticket 请求的等待器
    
    def _emit(self, callback, *args):
        """线程安全地调用回调"""
        try:
            callback(*args)
        except Exception:
            pass

    async def _send_ws(self, obj):
        """向中继发送 JSON（ws 已关闭时静默失败，不抛异常——否则任务完成回传会未捕获崩溃）"""
        ws = self._ws
        if ws:
            try:
                await ws.send(json.dumps(obj))
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
                self._send_ws({"type": "update_code", "authCode": code}),
                self._loop
            )
    
    def send_message(self, text, urgent=False):
        """发送消息给上游 Agent，返回消息 ID（用于已读回执）

        消息先进入中继队列，上游 Agent 调用 get_client_messages 读取；
        读取后中继会广播 messages_read，触发 on_messages_read 回调。
        """
        import uuid
        msg_id = uuid.uuid4().hex[:12]
        self.pending_messages[msg_id] = {
            "text": text, "urgent": urgent, "time": time.time(),
        }
        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send_ws({
                    "type": "agent_message",
                    "text": text,
                    "urgent": urgent,
                    "msgId": msg_id,
                }),
                self._loop
            )
            self._emit(self.on_log, f"消息已发送，等待 Agent 读取: {text[:60]}")
        else:
            self._emit(self.on_log, "消息未发送（尚未连接中继服务器）")
        return msg_id
    
    def reorder_messages(self, ordered_ids):
        """任务队列拖拽排序后，向中继同步消息的新顺序（Agent 将按此顺序读取）"""
        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send_ws({"type": "reorder_messages", "orderedIds": list(ordered_ids)}),
                self._loop
            )

    def delete_messages(self, ids):
        """从任务队列删除消息（Agent 尚未读取时有效），返回实际删除数"""
        ids = [i for i in ids if i in self.pending_messages]
        for i in ids:
            self.pending_messages.pop(i, None)
        if ids and self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send_ws({"type": "delete_messages", "ids": ids}),
                self._loop
            )
        return len(ids)

    def edit_message(self, msg_id, text):
        """编辑任务队列中某条消息的内容"""
        if msg_id in self.pending_messages:
            self.pending_messages[msg_id]["text"] = text
        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send_ws({"type": "edit_message", "id": msg_id, "text": text}),
                self._loop
            )

    def get_mcp_ticket(self):
        """向中继请求新的动态 MCP 地址 ticket（旧 ticket 作废），返回 ticket 或 None"""
        if not (self._ws and self._loop):
            return None
        try:
            fut = asyncio.run_coroutine_threadsafe(self._request_ticket(), self._loop)
            return fut.result(timeout=4)
        except Exception:
            return None

    async def _request_ticket(self):
        waiter = self._loop.create_future()
        self._ticket_waiter = waiter
        await self._send_ws({"type": "get_mcp_ticket", "requestId": "ticket"})
        try:
            return await asyncio.wait_for(waiter, timeout=3)
        except asyncio.TimeoutError:
            return None

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
                t0 = time.time()
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
                    latency = int((time.time() - t0) * 1000)
                    self._emit(self.on_log, "已连接到中继服务器")
                    self._emit(self.on_status, {"connected": True, "latency": latency})
                    
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
        
        if msg_type == "messages_read":
            ids = msg.get("ids", [])
            read = [i for i in ids if i in self.pending_messages]
            for i in read:
                self.pending_messages.pop(i, None)
            if read:
                self._emit(self.on_log, f"✅ Agent 已读取 {len(read)} 条消息")
                self._emit(self.on_messages_read, read)
            return
        
        if msg_type == "mcp_ticket":
            if self._ticket_waiter and not self._ticket_waiter.done():
                if msg.get("success"):
                    self._ticket_waiter.set_result(msg.get("ticket", ""))
                else:
                    self._ticket_waiter.set_result(None)
            return
        
        if msg_type == "agent_activity":
            self._emit(self.on_activity, msg.get("payload", {}))
            return
        
        if msg_type == "agent_action":
            # Agent 工具调用流（显示在客户端日志，绿色 AGT 徽章）
            self._emit(self.on_log, f"[Agent] {msg.get('text', '')}")
            return
        
        if msg_type == "agent_connected":
            sid = msg.get("sessionId", "") or ""
            self._emit(self.on_status, {
                "agent": "connected", "connected": True,
                "latency": msg.get("latency", 0),
                "agentId": sid[:8] if sid else "agent",
                "agentPlatform": msg.get("platform", "sandbox"),
                "agentHostname": msg.get("hostname", "OpenClaw"),
                "relayPlatform": msg.get("relayPlatform", ""),
            })
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
            cwd = self.sessions.get_current()
            cwd_str = cwd.cwd if cwd else os.getcwd()
            result = await self._exec_cmd(cmd, payload.get("timeout", 30000), cwd_str)
            await self._send("command_result", rid, result)
            self._emit(self.on_result, {**result, "command": cmd, "cwd": cwd_str})
            self._emit(self.on_log, f"[执行] {cmd[:80]}")
            self._emit(self.on_log, f"[目录] {cwd_str}")
            out = result.get("stdout", "")
            err = result.get("stderr", "")
            if out:
                for line in out.strip().split('\n')[:10]:
                    self._emit(self.on_log, f"  {line[:120]}")
            if err:
                self._emit(self.on_log, f"  ❌ {err[:120]}")
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
            # 判断文件是否存在（新建 vs 修改），相对路径按会话目录解析
            existed_path = path
            if not os.path.isabs(existed_path):
                session = self.sessions.get_current()
                if session:
                    existed_path = os.path.join(session.cwd, existed_path)
            existed = os.path.exists(os.path.normpath(existed_path))
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
            path = os.path.normpath(path)
            if not self._check_path(path):
                await self._send("download_result", rid, {"success": False, "error": "超出工作区范围"})
                return
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
                # 让 UI 渲染命令块（含耗时、目录、命令名）
                self._emit(self.on_result, {**result, "command": payload.get("command", ""), "cwd": cwd_str})
                self._emit(self.on_log, f"[执行] {payload.get('command','')[:80]}")
                self._emit(self.on_log, f"[目录] {cwd_str}")
                # 显示结果摘要
                out = result.get("stdout", "")
                err = result.get("stderr", "")
                if out:
                    for line in out.strip().split('\n')[:10]:
                        self._emit(self.on_log, f"  {line[:120]}")
                if err:
                    self._emit(self.on_log, f"  ❌ {err[:120]}")
                if result.get("exitCode") != 0:
                    self._emit(self.on_log, f"  (退出码: {result.get('exitCode')})")
            elif op == "read_file":
                self._emit(self.on_log, f"[读取] {payload.get('path','')}")
            elif op == "write_file":
                self._emit(self.on_log, f"[写入] {payload.get('path','')}")
            return
        
        if msg_type == "task_start":
            task_id = msg.get("taskId", "")
            task_payload = msg.get("payload", {})
            command = task_payload.get("command", "")
            timeout = task_payload.get("timeout", 1800000)
            self._emit(self.on_command, {"type": "task", "taskId": task_id, "command": command})
            self._emit(self.on_log, f"[任务] 提交后台执行: {command[:60]}")
            # 后台执行，不阻塞主消息循环（可同时跑多个任务）
            asyncio.create_task(self._run_task(task_id, command, timeout))
            return
    
    async def _send(self, msg_type, rid, payload):
        await self._send_ws({"type": msg_type, "requestId": rid, "payload": payload})
    
    # ── 命令执行 ──────────────────────────────
    
    async def _exec_cmd(self, command, timeout, cwd=None):
        # 工作区模式检查命令
        ok, err = self._check_command(command)
        if not ok:
            return {"exitCode": 1, "stdout": "", "stderr": err, "killed": False, "duration": 0}
        # Windows 编码: 先试 gbk，再 utf-8
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
    
    async def _run_task(self, task_id, command, timeout):
        """后台执行异步任务（不阻塞主循环），完成后回传结果给中继"""
        try:
            session = self.sessions.get_current()
            cwd = session.cwd if session else os.getcwd()
            result = await self._exec_cmd(command, timeout, cwd)
            await self._send_task_result(task_id, result)
            self._emit(self.on_log, f"[任务] {task_id[:8]} 完成: exitCode={result.get('exitCode')} ({result.get('duration')}ms)")
        except Exception as e:
            await self._send_task_result(task_id, {"exitCode": 1, "stdout": "", "stderr": str(e), "killed": False})

    async def _send_task_result(self, task_id, result):
        """taskId 放顶层（relay 按 msg.taskId 匹配），payload 是结果"""
        await self._send_ws({"type": "task_result", "taskId": task_id, "payload": result})
    
    def _check_path(self, path):
        """检查路径是否在允许范围内（管理员白名单 + 工作区）"""
        # 管理员级路径白名单（与 relay 的 ALLOWED_FILE_PREFIX 一致，可选）
        if ALLOWED_FILE_PREFIX:
            p = os.path.normpath(path).replace('\\', '/')
            prefix = ALLOWED_FILE_PREFIX.rstrip('/')
            if p != prefix and not p.startswith(prefix + '/'):
                return False
        # 工作区模式
        if self.permission != "workspace":
            return True
        session = self.sessions.get_current()
        if not session:
            return False
        workspace = os.path.normpath(session.workDir)
        resolved = os.path.normpath(path)
        return resolved == workspace or resolved.startswith(workspace + os.sep)
    
    def _check_command(self, command):
        """检查命令是否可能逃逸工作区（工作区模式下的软限制，防误操作越界；非安全核心防线）"""
        if self.permission != "workspace":
            return True, ""
        import re
        # 检测绝对路径：Windows 盘符 C:\ 或 C:/（用负向后顾排除 URL 如 https:// 中的 s:/）
        if re.search(r'(?<![A-Za-z])[A-Za-z]:[\\/]', command):
            return False, "工作区模式禁止使用绝对路径"
        # 检测 Linux/macOS 绝对路径 /path（要求 / 前是行首/空格/命令连接符，排除 URL 和路径分隔 a/b）
        if re.search(r'(?:^|\s|[&|;(])/(?:[A-Za-z0-9_\-]|$)', command):
            return False, "工作区模式禁止使用绝对路径"
        # 检测 .. 逃逸（要求 .. 前是行首/空格/命令连接符，排除 a...b 等文件名误报）
        if re.search(r'(?:^|\s|[&|;(])\.\.(?:[\\/]|[^\w]|$)', command):
            return False, "工作区模式禁止 .. 逃逸"
        # 检测切换目录命令：cd/chdir/Set-Location/pushd/popd（含 cd.. cd\ 等无空格形式；不匹配行尾避免 echo cd 误报）
        if re.search(r'(?:^|\s|[&|;(])(?:cd|chdir|set-location|pushd|popd)(?:\s|\.+|[\\/])', command, re.IGNORECASE):
            return False, "工作区模式禁止 cd/chdir/Set-Location/pushd/popd 切换目录"
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
            uptime = time.time() - psutil.boot_time()
        except:
            total = 0; free = 0; uptime = 0
        # os.getlogin() 在无 TTY 环境（系统服务/某些容器）会抛 OSError，
        # 原代码它在 return 字典里、不在上方 try 块内，会直接崩溃并断开连接
        try:
            username = os.getlogin()
        except OSError:
            import getpass
            try:
                username = getpass.getuser()
            except Exception:
                username = os.environ.get('USERNAME') or os.environ.get('USER') or 'unknown'
        return {
            "hostname": platform.node(), "platform": sys.platform,
            "arch": platform.machine(), "cpus": os.cpu_count() or 0,
            "totalMem": total, "freeMem": free,
            "uptime": uptime, "homedir": str(Path.home()),
            "userInfo": {"username": username},
        }
    
    async def _handle_session_op(self, op, payload):
        try:
            if op == "create":
                work_dir = payload.get("workDir", "")
                # 工作区模式下，Agent 不能自己划定工作区，只能用用户设定的默认工作区
                if self.permission == "workspace" and work_dir:
                    if os.path.normpath(work_dir) != os.path.normpath(self.default_work_dir):
                        return {"success": False, "error": "工作区模式下不能自定义工作目录，只能使用默认工作区"}
                    work_dir = self.default_work_dir
                return self.sessions.create(work_dir, payload.get("name"))
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