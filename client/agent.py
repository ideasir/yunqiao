"""
云桥 - Agent 核心引擎
====================
负责:连接中继、注册设备、接收命令、执行命令、会话管理
不负责:UI 显示(那是 desktop.py 的事)

用法:
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

# 管理员级路径白名单(与 relay 的 ALLOWED_FILE_PREFIX 一致,可选;统一转正斜杠)
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

    职责:
    - 连接中继服务器(WebSocket)
    - 注册设备,上报配对码
    - 接收并执行上游命令(execute_command / read_file / write_file / session_op)
    - 通过回调函数通知 desktop.py 状态变化

    不负责:
    - UI 渲染
    - 设置管理(由 desktop.py 的 config.json 负责)
    """

    def __init__(self, relay_url, relay_key, device_name=None, auth_code=None,
                 direct_mode=False, clash_api=None):
        self.relay_url = relay_url
        self.relay_key = relay_key
        self.device_name = device_name or platform.node()
        self.auth_code = auth_code  # 配对码(desktop 用,CLI 不用)
        # Clash 直连:为 True 时连接前给 Clash 加 DOMAIN-SUFFIX,yunqiao.very.im,DIRECT
        # 规则,让云桥 WSS 绕开代理节点(切节点不业务断线)。默认 False 走系统代理。
        self.direct_mode = direct_mode
        self.clash_api = clash_api
        self.device_id = None
        # 持久化设备 ID:存本地文件,重连/重启复用,服务器据此识别"同一台设备"。
        # 服务器端(relay/server.js 3052e42)已支持 persistentId;旧客户端不传导致
        # 每次重连都生成新 deviceId,表现为连接不稳定(注册→断开→新ID→再注册)。
        self.persistent_id = self._load_or_create_persistent_id()
        self.connected = False

        # 权限模式: workspace(仅工作区)/ super(全盘)
        self.permission = "workspace"

        # 会话管理
        self.sessions = SessionManager()
        self.sessions.load()

        # 默认工作区:打包成 exe 时在 exe 旁边(绿色版便携,文件持久);
        # 源码运行时在项目根。不能用 __file__(PyInstaller 里指向临时解压目录,文件会"消失")
        if getattr(sys, 'frozen', False):
            base_dir = Path(sys.executable).parent  # exe 所在目录
        else:
            base_dir = Path(__file__).parent.parent  # 项目根
        self.default_work_dir = str(base_dir / 'worker')
        os.makedirs(self.default_work_dir, exist_ok=True)
        # 只有首次启动(无任何会话)才自动创建默认工作区
        if not self.sessions.sessions:
            self.sessions.create(self.default_work_dir, '默认工作区')

        # 回调函数(由 desktop.py 设置)
        self.on_log = lambda msg: None       # 日志消息
        self.on_status = lambda status: None  # 连接状态变化
        self.on_command = lambda cmd: None    # 收到命令时
        self.on_result = lambda result: None  # 命令执行结果
        self.on_progress = lambda p: None     # 长任务进度(如 CodeGraph 索引)
        self.on_messages_read = lambda ids: None  # 上游 Agent 已读消息回执
        self.on_activity = lambda a: None  # 上游 Agent 活跃度(连接数/任务数/调用数)

        # 内部状态
        self._ws = None
        self._loop = None
        self._thread = None
        self._running = False
        self.pending_messages = {}  # msgId -> {text, urgent, time},等待上游 Agent 已读回执
        self._ticket_waiter = None  # 动态 MCP 地址 ticket 请求的等待器
        self._activity = {}  # 最近一次上游 Agent 活跃度快照(连接数/任务数/调用数),供 UI 主动拉取兜底

        # 索引自动同步(常驻):工作区代码变动时自动 codegraph sync,保证查询的是最新代码
        self._auto_sync_on = True
        self._auto_sync_lock = threading.Lock()
        self._auto_sync_state = {}  # workDir -> 文件快照 {relpath: mtime_ns}
        self._auto_sync_thread = None
        self._auto_sync_interval = int(os.environ.get('CODEGRAPH_SYNC_INTERVAL', '60'))  # 秒
        self._auto_sync_start()

    def _load_or_create_persistent_id(self):
        """从 ~/.yunqiao/device-id 读取或生成持久设备 ID。"""
        import uuid
        cfg_dir = Path(os.environ.get("YUNQIAO_CONFIG", str(Path.home() / ".yunqiao")))
        cfg_dir.mkdir(parents=True, exist_ok=True)
        id_file = cfg_dir / "device-id"
        try:
            if id_file.exists():
                val = id_file.read_text("utf-8").strip()
                if val:
                    return val
        except Exception:
            pass
        val = str(uuid.uuid4())
        try:
            id_file.write_text(val, "utf-8")
        except Exception:
            pass
        return val

    def _emit(self, callback, *args):
        """线程安全地调用回调"""
        try:
            callback(*args)
        except Exception:
            pass

    async def _send_ws(self, obj):
        """向中继发送 JSON(ws 已关闭时静默失败,不抛异常)。返回是否成功发送。"""
        ws = self._ws
        if ws:
            try:
                await ws.send(json.dumps(obj))
                return True
            except Exception:
                pass
        return False

    def _clash_set_direct(self, enable):
        import urllib.request
        import json as _json
        api = self.clash_api or "http://127.0.0.1:9090"
        rule = "DOMAIN-SUFFIX,yunqiao.very.im,DIRECT"
        try:
            req = urllib.request.Request(api + "/version", method="GET")
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            self._emit(self.on_log, "[clash] Clash API unreachable")
            return
        try:
            if enable:
                data = _json.dumps({"payload": "", "rule_type": "DOMAIN-SUFFIX", "proxy": "DIRECT"}).encode()
                req = urllib.request.Request(api + "/rules", data=data, method="PUT", headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=3)
                self._emit(self.on_log, "[clash] DIRECT rule added")
            else:
                try:
                    req = urllib.request.Request(api + "/rules/DOMAIN-SUFFIX,yunqiao.very.im,DIRECT", method="DELETE")
                    urllib.request.urlopen(req, timeout=3)
                except:
                    pass
                self._emit(self.on_log, "[clash] DIRECT rule removed")
        except Exception as e:
            self._emit(self.on_log, "[clash] rule failed: " + str(e))

    def start(self):
        """启动 Agent(后台线程)"""
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

    def get_activity(self):
        """返回最近一次上游 Agent 活跃度快照(供 UI 主动拉取,不依赖推送)"""
        a = dict(self._activity or {})
        # 未收到过任何快照时给个保底结构,避免前端拿到 None/空导致灯全灭
        if "connections" not in a:
            a["connections"] = 0
        if "runningTasks" not in a:
            a["runningTasks"] = 0
        if "pendingCalls" not in a:
            a["pendingCalls"] = 0
        if "maxConnections" not in a:
            a["maxConnections"] = 50
        return a

    def send_message(self, text, urgent=False):
        """发送消息给上游 Agent,返回消息 ID(用于已读回执)

        消息先进入中继队列,上游 Agent 调用 get_client_messages 读取;
        读取后中继会广播 messages_read,触发 on_messages_read 回调。
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
            self._emit(self.on_log, f"消息已发送,等待 Agent 读取: {text[:60]}")
        else:
            self._emit(self.on_log, "消息未发送(尚未连接中继服务器)")
        return msg_id

    def reorder_messages(self, ordered_ids):
        """任务队列拖拽排序后,向中继同步消息的新顺序(Agent 将按此顺序读取)"""
        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send_ws({"type": "reorder_messages", "orderedIds": list(ordered_ids)}),
                self._loop
            )

    def delete_messages(self, ids):
        """从任务队列删除消息(Agent 尚未读取时有效),返回实际删除数"""
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
        """向中继请求新的动态 MCP 地址 ticket(旧 ticket 作废),返回 ticket 或 None"""
        if not (self._ws and self._loop):
            return None
        try:
            fut = asyncio.run_coroutine_threadsafe(self._request_ticket(), self._loop)
            return fut.result(timeout=5)
        except Exception:
            return None

    async def _request_ticket(self):
        # WSS 不可用时直接失败,不要让调用方干等超时
        if not self._ws:
            return None
        waiter = self._loop.create_future()
        self._ticket_waiter = waiter
        sent = await self._send_ws({"type": "get_mcp_ticket", "requestId": "ticket"})
        if not sent:
            return None
        try:
            return await asyncio.wait_for(waiter, timeout=4)
        except asyncio.TimeoutError:
            return None

    def set_permission(self, mode):
        """设置权限模式: workspace 或 super"""
        self.permission = mode

    # ── 内部实现 ──────────────────────────────

    def _mesh_ready(self):
        """组网通道是否可用(EasyTier 入网后能达服务器组网 IP)。
        延迟导入避免首次启动无 easytier 时拖慢连接。"""
        try:
            from easytier_helper import probe_mesh_channel
            return probe_mesh_channel(timeout=1)
        except Exception:
            return False

    def _ensure_mesh_async(self):
        """后台自动装配组网：等待服务器下发配置(注册后) + 确保 easytier + 启动节点。
        独立线程执行，绝不阻塞 asyncio 事件循环（否则心跳会断）。"""
        if getattr(self, '_mesh_thread', None) and self._mesh_thread.is_alive():
            return
        def _worker():
            try:
                import easytier_helper as eh
                # 1. 等服务器下发 mesh_config（注册后几秒内到达；收到后 _handle_message 会再调本函数）
                mesh = eh.load_mesh_config()
                if not mesh:
                    self._emit(self.on_log, "[组网] 等待组网配置下发...")
                    return  # 等 mesh_config 消息触发下一次 _ensure_mesh_async
                # 2. 确保 easytier-core 可用（仓库内置，随客户端更新）
                if not eh.is_installed():
                    self._emit(self.on_log, "[组网] 未找到 easytier，尝试下载...")
                    if not eh.install(progress_cb=lambda m: self._emit(self.on_log, f"[组网] {m}")):
                        self._emit(self.on_log, "[组网] 下载失败，继续使用公网连接")
                        return
                # 3. 启动 no-tun 节点：先清理旧 easytier-core（避免重启客户端后节点越积越多）
                if getattr(self, '_mesh_proc', None) and self._mesh_proc.poll() is None:
                    return
                self._emit(self.on_log, "[组网] 清理旧节点...")
                eh.cleanup_stale_nodes()
                self._emit(self.on_log, f"[组网] 启动节点 {mesh['networkName']}...")
                self._mesh_proc = eh.start_node(mesh, progress_cb=lambda m: self._emit(self.on_log, f"[组网] {m}"))
                if self._mesh_proc is None:
                    self._emit(self.on_log, "[组网] 节点启动失败，继续使用公网连接")
                    return
                # 4. 等待入网并报告
                import time as _t
                for _ in range(15):
                    _t.sleep(1)
                    if self._mesh_proc.poll() is not None:
                        self._emit(self.on_log, "[组网] 节点进程已退出，继续使用公网连接")
                        return
                    if eh.probe_mesh_channel(timeout=1):
                        self._emit(self.on_log, "[组网] ✅ 组网通道已打通")
                        self._emit(self.on_status, {"connected": True, "proto": "EasyTier"})
                        return
                self._emit(self.on_log, "[组网] 入网等待超时，继续使用公网连接")
            except Exception as e:
                self._emit(self.on_log, f"[组网] 自动装配异常: {e}")
        import threading
        self._mesh_thread = threading.Thread(target=_worker, daemon=True, name="mesh-assemble")
        self._mesh_thread.start()

    def _fetch_mesh_config(self):
        """通过 MCP get_mesh_config 工具获取组网配置。
        在 asyncio 事件循环里调工具会阻塞,这里用同步子进程方式(yq)不可行,
        改为:通过主连接发消息由服务器返回--但简单起见直接返回 None 由外部注入。
        未来:客户端从服务器下发的 mesh 消息里拿配置(见 server get_mesh_config)。"""
        return None

    def _run_loop(self):
        """后台线程:运行 asyncio 事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect())

    async def _connect(self):
        """WebSocket 连接循环(指数退避重连)"""
        import websockets
        self._emit(self.on_log, f"正在连接 {self.relay_url}")
        retry = 0

        while self._running:
            try:
                t0 = time.time()
                # Clash 直连:确保 yunqiao.very.im 走 DIRECT,绕开代理节点切换。
                # 每次重连前都调(幂等);Clash 切节点/重启后规则可能被清。
                if getattr(self, "direct_mode", False):
                    self._clash_set_direct(True)
                try:
                    ws = await websockets.connect(
                        self.relay_url,
                        extra_headers={"X-Key": self.relay_key, "X-PSK": self.relay_key},
                        ping_interval=20,
                        ping_timeout=90,
                        close_timeout=5
                    )
                except TypeError:
                    ws = await websockets.connect(
                        self.relay_url,
                        additional_headers={"X-Key": self.relay_key, "X-PSK": self.relay_key},
                        ping_interval=20,
                        ping_timeout=90,
                        close_timeout=5
                    )
                async with ws:
                    self._ws = ws
                    self.connected = True
                    retry = 0  # 重置重试计数
                    latency = int((time.time() - t0) * 1000)
                    self._emit(self.on_log, "已连接到中继服务器")
                    # proto: 实际传输通道(EasyTier 组网优先,否则 WSS 公网)
                    proto = "EasyTier" if self._mesh_ready() else "WSS"
                    self._emit(self.on_status, {"connected": True, "latency": latency, "proto": proto})

                    # 注册设备
                    await ws.send(json.dumps({
                        "type": "register",
                        "deviceName": self.device_name,
                        "os": sys.platform,
                        "arch": platform.machine(),
                        "hostname": platform.node(),
                        "authCode": self.auth_code,
                        "persistentId": self.persistent_id,
                    }))

                    # 自动装配组网(EasyTier):后台拉取组网配置 + 下载/启动节点,不阻塞主连接
                    self._ensure_mesh_async()

                    # 处理消息
                    async for message in ws:
                        try:
                            msg = json.loads(message)
                        except json.JSONDecodeError:
                            continue

                        await self._handle_message(msg)

                    # 走到这里说明 WebSocket 已正常关闭(收到 close frame),
                    # 或连接被对端关闭--`async for` 正常退出不抛异常,不会进 except。
                    # 不处理的话 UI 会永远显示"已连接"(假连接)。
                    self._mark_disconnected("连接已关闭(服务器端)")

            except Exception as e:
                self._mark_disconnected(f"第{retry + 1}次断开: {e}")
                retry += 1

            if self._running:
                # 指数退避: 2,4,8,16,30,30,... 秒
                backoff = min(2 ** retry, 30)
                await asyncio.sleep(backoff)
            elif getattr(self, "direct_mode", False):
                # 彻底停止才移除直连规则;普通断线 keep 规则,减少抖动。
                self._clash_set_direct(False)

    def _mark_disconnected(self, reason: str):
        """统一处理断开:清状态 + 通知 UI + 清空活动快照。

        覆盖两种断开路径:
        1. 异常断开(网络中断/超时)→ 走 except 调这里
        2. 正常关闭(close frame,如服务器重启)→ async for 正常退出后调这里
        之前只在 except 里处理,第二种情况会让 UI 永远显示"已连接"(假连接)。
        """
        self.connected = False
        self._ws = None
        # 连接断开 → 并发活动必然归零,清空缓存快照,避免 UI 轮询拉到旧值导致灯不灭
        self._activity = {}
        self._emit(self.on_log, f"⚠️ [重连] {reason}")
        self._emit(self.on_status, {"connected": False})

    async def _handle_message(self, msg):
        """处理服务器下发的消息"""
        msg_type = msg.get("type")
        rid = msg.get("requestId")
        payload = msg.get("payload", {})

        if msg_type == "register_result":
            if msg.get("success"):
                self.device_id = msg.get("deviceId", "")
                self._emit(self.on_log, f"注册成功: {self.device_id[:8]}...")
            else:
                # 注册失败(如配对码不符 / 服务器重启后设备表清空)→ 必须显式断开并提示,
                # 否则 UI 仍显示"已连接"绿灯,实际是未完成设备身份注册的半连接。
                reason = msg.get("error") or msg.get("message") or "未知原因"
                self.connected = False
                self._emit(self.on_log, f"⚠️ 设备注册失败: {reason}")
                self._emit(self.on_status, {"connected": False})
                # 关闭当前连接,让外层循环走重连(配对码可能已轮换)
                try:
                    ws = getattr(self, "_ws", None)
                    if ws is not None:
                        await ws.close()
                except Exception:
                    pass
            return

        if msg_type == "mesh_config":
            # 服务器下发组网配置(EasyTier)→ 保存 + 自动装配(独立线程,不阻塞事件循环)
            try:
                import easytier_helper as eh
                mesh = payload if isinstance(payload, dict) else {}
                if mesh.get("networkName"):
                    eh.save_mesh_config(mesh)
                    self._emit(self.on_log, "[组网] 收到组网配置,自动装配...")
                    self._ensure_mesh_async()
                else:
                    self._emit(self.on_log, "[组网] 组网配置不完整,跳过")
            except Exception as e:
                self._emit(self.on_log, f"[组网] 配置处理异常: {e}")
            return

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
            payload = msg.get("payload", {}) or {}
            # 缓存最新活跃度快照(UI 主动拉取兜底,避免依赖推送导致灯不更新)
            if isinstance(payload, dict):
                self._activity = payload
            self._emit(self.on_activity, payload)
            return

        if msg_type == "agent_action":
            # Agent 工具调用流(显示在客户端日志,绿色 AGT 徽章)
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
            # 上游 Agent 断开 → 并发活动归零,清空缓存快照(否则 UI 轮询会拉到旧值让灯一直亮)
            self._activity = {}
            self._emit(self.on_status, {"agent": "disconnected", "connected": True})
            return

        if msg_type == "device_locked":
            until = payload.get("until", 0)
            mins = max(1, round((until - time.time() * 1000) / 60000)) if until > time.time() * 1000 else 0
            self._emit(self.on_log, f"[安全] 设备被锁定,{mins} 分钟后自动解锁")
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
            # 结构化结果:完整命令 + 完整输出(不散行重复)
            self._emit(self.on_result, {**result, "kind": "execute_command", "command": cmd, "cwd": cwd_str})
            self._emit(self.on_log, f"[执行] {cmd[:80]}")
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
            # 判断文件是否存在(新建 vs 修改),相对路径按会话目录解析
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

        if msg_type == "exec_script":
            cmd = {
                "language": payload.get("language", "auto"),
                "code": payload.get("code", ""),
                "cwd": payload.get("cwd"),
                "timeout": payload.get("timeout", 120000),
            }
            self._emit(self.on_command, {"type": "exec_script", "language": cmd["language"], "code": cmd["code"][:80]})
            result = await self._exec_script(**cmd)
            await self._send("script_result", rid, result)
            # 结构化结果:完整脚本 + 完整输出(不再只发截断摘要)
            full_result = {
                **result,
                "kind": "exec_script",
                "command": cmd["code"],
                "language": cmd["language"],
                "cwd": (self.sessions.get_current().cwd if self.sessions.get_current() else os.getcwd()),
            }
            self._emit(self.on_result, full_result)
            # 保留简短的 Agent 动作日志(不重复输出正文,正文已进卡片)
            self._emit(self.on_log, f"[脚本:{cmd['language']}] {cmd['code'][:60]}")
            return

        if msg_type == "get_environment":
            result = self._get_environment()
            await self._send("environment_info", rid, result)
            # 结构化结果:完整环境档案 JSON 发给 UI
            self._emit(self.on_result, {
                "kind": "get_environment",
                "command": "get_environment",
                "exitCode": 0,
                "stdout": json.dumps(result, ensure_ascii=False, indent=2),
                "stderr": "",
                "duration": 0,
                "data": result,
                "cwd": (self.sessions.get_current().cwd if self.sessions.get_current() else os.getcwd()),
            })
            self._emit(self.on_log, "[环境] 已生成环境档案")
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

        if msg_type == "run_custom":
            # 自定义命令:执行客户端本地 custom-commands/ 目录的脚本
            name = payload.get("name", "")
            args_list = payload.get("args", [])
            timeout = payload.get("timeout", 120000)
            self._emit(self.on_command, {"type": "custom", "name": name})
            result = await self._run_custom(name, args_list, timeout)
            await self._send("custom_result", rid, result)
            self._emit(self.on_result, {
                "kind": "custom",
                "command": f"[custom:{name}] {' '.join(map(str, args_list))}",
                "exitCode": result.get("exitCode", 1),
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "duration": result.get("duration", 0),
                "cwd": (self.sessions.get_current().cwd if self.sessions.get_current() else os.getcwd()),
            })
            self._emit(self.on_log, f"[自定义] {name} {" ".join(map(str, args_list))}")
            return

        if msg_type == "codegraph_index":
            # 建立 CodeGraph 语义索引(大项目)
            path = payload.get("path", "")
            cwd = self.sessions.get_current().cwd if self.sessions.get_current() else os.getcwd()
            path = path or cwd
            import shutil
            if self._find_codegraph() is None:
                await self._send("codegraph_index_result", rid, {"success": False, "error": "未安装 CodeGraph(npm install -g @colbymchenry/codegraph)"})
                return
            if not os.path.isdir(path):
                await self._send("codegraph_index_result", rid, {"success": False, "error": f"目录不存在: {path}"})
                return
            self._emit(self.on_log, f"[索引] 正在为 {path} 建立 CodeGraph 索引...")
            self._emit(self.on_command, {"type": "codegraph_index", "path": path})
            result = await self._run_codegraph_index(path)
            await self._send("codegraph_index_result", rid, result)
            if result.get("success"):
                self._emit(self.on_result, {
                    "kind": "codegraph_index", "command": f"codegraph_index {path}",
                    "exitCode": 0, "stdout": result.get("message", "索引完成"),
                    "stderr": "", "cwd": cwd,
                })
            else:
                self._emit(self.on_result, {
                    "kind": "codegraph_index", "command": f"codegraph_index {path}",
                    "exitCode": 1, "stdout": "", "stderr": result.get("error", "索引失败"), "cwd": cwd,
                })
            return

        if msg_type == "session_op":
            op = payload.get("op", "")
            self._emit(self.on_command, {"type": "session", "op": op})
            result = await self._handle_session_op(op, payload)
            await self._send("session_op_result", rid, result)
            if op == "exec" and "exitCode" in result:
                cwd = self.sessions.get_current()
                cwd_str = cwd.cwd if cwd else os.getcwd()
                # 结构化结果:完整命令 + 完整输出(不散行重复)
                self._emit(self.on_result, {**result, "kind": "exec", "command": payload.get("command", ""), "cwd": cwd_str})
                self._emit(self.on_log, f"[执行] {payload.get('command','')[:80]}")
            elif op == "read_file":
                self._emit(self.on_result, {"kind": "read_file", "command": "read_file", "path": payload.get("path", ""), "exitCode": 0 if result.get("success") else 1, "stdout": result.get("content", "") if result.get("success") else "", "stderr": result.get("error", "") if not result.get("success") else "", "cwd": (self.sessions.get_current().cwd if self.sessions.get_current() else os.getcwd())})
                self._emit(self.on_log, f"[读取] {payload.get('path','')}")
            elif op == "write_file":
                self._emit(self.on_result, {"kind": "write_file", "command": "write_file", "path": payload.get("path", ""), "exitCode": 0 if result.get("success") else 1, "stdout": ("已写入: " + str(result.get("path", ""))) if result.get("success") else "", "stderr": result.get("error", "") if not result.get("success") else "", "cwd": (self.sessions.get_current().cwd if self.sessions.get_current() else os.getcwd())})
                self._emit(self.on_log, f"[写入] {payload.get('path','')}")
            else:
                # 其他会话操作(create/close/switch/list):也给出完成反馈,避免卡片卡在"运行中"
                ok = result.get("success", result.get("exitCode", 1) == 0)
                desc = result.get("message", result.get("error", "")) or f"session {op}"
                self._emit(self.on_result, {
                    "kind": "session_" + op, "command": op + "_session",
                    "exitCode": 0 if ok else 1,
                    "stdout": desc if ok else "",
                    "stderr": "" if ok else desc,
                    "cwd": (self.sessions.get_current().cwd if self.sessions.get_current() else os.getcwd()),
                })
                self._emit(self.on_log, f"[会话] {op}: {desc}")
            return

        if msg_type == "task_start":
            task_id = msg.get("taskId", "")
            task_payload = msg.get("payload", {})
            command = task_payload.get("command", "")
            timeout = task_payload.get("timeout", 1800000)
            self._emit(self.on_command, {"type": "task", "taskId": task_id, "command": command})
            self._emit(self.on_log, f"[任务] 提交后台执行: {command[:60]}")
            # 后台执行,不阻塞主消息循环(可同时跑多个任务)
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
        # Windows 编码: 先试 gbk,再 utf-8
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
        """后台执行异步任务(不阻塞主循环),完成后回传结果给中继"""
        try:
            session = self.sessions.get_current()
            cwd = session.cwd if session else os.getcwd()
            result = await self._exec_cmd(command, timeout, cwd)
            await self._send_task_result(task_id, result)
            self._emit(self.on_log, f"[任务] {task_id[:8]} 完成: exitCode={result.get('exitCode')} ({result.get('duration')}ms)")
        except Exception as e:
            await self._send_task_result(task_id, {"exitCode": 1, "stdout": "", "stderr": str(e), "killed": False})

    async def _send_task_result(self, task_id, result):
        """taskId 放顶层(relay 按 msg.taskId 匹配),payload 是结果"""
        await self._send_ws({"type": "task_result", "taskId": task_id, "payload": result})

    def _check_path(self, path):
        """检查路径是否在允许范围内(管理员白名单 + 工作区)"""
        # 管理员级路径白名单(与 relay 的 ALLOWED_FILE_PREFIX 一致,可选)
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
        """检查命令是否可能逃逸工作区(工作区模式下的软限制,防误操作越界;非安全核心防线)"""
        if self.permission != "workspace":
            return True, ""
        import re
        # 检测绝对路径:Windows 盘符 C:\ 或 C:/(用负向后顾排除 URL 如 https:// 中的 s:/)
        if re.search(r'(?<![A-Za-z])[A-Za-z]:[\\/]', command):
            return False, "工作区模式禁止使用绝对路径"
        # 检测 Linux/macOS 绝对路径 /path(排除 Windows 开关 /X 和 URL;要求路径至少两个字符)
        if re.search(r'(?:^|\s|[&|;(])/(?:[A-Za-z0-9_\-]{2,}|[A-Za-z0-9_\-]+/)', command):
            return False, "工作区模式禁止使用绝对路径"
        # 检测 .. 逃逸(要求 .. 前是行首/空格/命令连接符,排除 a...b 等文件名误报)
        if re.search(r'(?:^|\s|[&|;(])\.\.(?:[\\/]|[^\w]|$)', command):
            return False, "工作区模式禁止 .. 逃逸"
        # 检测切换目录命令:cd/chdir/Set-Location/pushd/popd(含 cd.. cd\ 等无空格形式;不匹配行尾避免 echo cd 误报)
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
        # os.getlogin() 在无 TTY 环境(系统服务/某些容器)会抛 OSError,
        # 原代码它在 return 字典里、不在上方 try 块内,会直接崩溃并断开连接
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

    # ── 亲和通道:脚本执行(exec_script)──────────────────
    # 把代码以文件形式传过去执行,避免整条命令字符串的转义地狱。
    # 支持多语言,返回结构化结果。算力在沙箱,这里只是执行原语。
    _SCRIPT_EXT = {
        'python': '.py', 'py': '.py',
        'powershell': '.ps1', 'ps1': '.ps1', 'pwsh': '.ps1',
        'node': '.js', 'js': '.js',
        'bash': '.sh', 'sh': '.sh',
        'cmd': '.bat', 'bat': '.bat', 'batch': '.bat',
    }
    _SCRIPT_RUNNER = {
        'python': ['python'], 'py': ['python'],
        'node': ['node'], 'js': ['node'],
        'cmd': ['cmd', '/c'], 'bat': ['cmd', '/c'], 'batch': ['cmd', '/c'],
    }

    def _resolve_interpreter(self, language):
        """返回 (解释器命令列表, 是否走 shell)。找不到解释器抛 ValueError。"""
        lang = (language or 'auto').lower()
        if lang in ('auto',):
            # 自动探测:优先 bash(Git for Windows 自带),其次 PowerShell,最后 python
            for cand in self._detect_available_shells():
                return cand
            raise ValueError('未找到可用的脚本解释器(bash/pwsh/python 均不可用)')
        if lang in ('bash', 'sh'):
            bash = self._find_bash()
            if bash:
                return ([bash], False)
            raise ValueError('未找到 bash(可安装 Git for Windows 或 WSL)')
        if lang in ('powershell', 'ps1', 'pwsh'):
            return ('powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File')
        runner = self._SCRIPT_RUNNER.get(lang)
        if not runner:
            raise ValueError(f'不支持的脚本语言: {language}')
        return (runner, False)

    def _find_bash(self):
        """查找 bash:优先 Git for Windows,其次系统 PATH。"""
        candidates = []
        for p in (os.environ.get('ProgramFiles', ''), os.environ.get('ProgramFiles(x86)', '')):
            if p:
                candidates.append(os.path.join(p, 'Git', 'bin', 'bash.exe'))
        candidates.append('bash')
        for c in candidates:
            if c == 'bash':
                # 试试 PATH 里有没有
                import shutil
                if shutil.which('bash'):
                    return 'bash'
                continue
            if os.path.exists(c):
                return c
        return None

    def _detect_available_shells(self):
        """返回可用的脚本运行器列表(按优先级)。"""
        runners = []
        bash = self._find_bash()
        if bash:
            runners.append(([bash], False))
        # PowerShell 总是可用
        runners.append(('powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File'))
        return runners

    async def _exec_script(self, language, code, cwd=None, timeout=120000):
        """把代码写成临时脚本文件执行,返回结构化结果。彻底避开字符串转义。"""
        import tempfile
        if not code or not code.strip():
            return {"exitCode": 1, "stdout": "", "stderr": "空脚本", "duration": 0, "killed": False, "language": language}
        # 工作区模式:cwd 强制为当前会话目录(脚本本身信任执行--脚本已是"可执行代码",
        # 不能用面向用户命令行的 _check_command 正则去卡,否则合法路径如 ls /var/log 会被误判。
        # 安全模型 = 锁定 cwd + 审计日志;临时脚本文件在系统 temp,不留工作区内。
        if self.permission == "workspace":
            session = self.sessions.get_current()
            cwd = (session.cwd if session else None) or self.default_work_dir or os.getcwd()
        else:
            cwd = cwd or os.getcwd()

        lang = (language or 'auto').lower()
        # 边界防护:timeout 非法/过小/过大时回退默认
        if not timeout or timeout <= 0:
            timeout = 120000
        timeout = min(timeout, 1800000)
        ext = self._SCRIPT_EXT.get(lang, '.txt')
        try:
            runner, is_shell = self._resolve_interpreter(lang)
        except ValueError as e:
            return {"exitCode": 1, "stdout": "", "stderr": str(e), "duration": 0, "killed": False, "language": language}

        # 写临时脚本文件
        fd, script_path = tempfile.mkstemp(suffix=ext, prefix='yunqiao_')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(code)
            # 命令行= runner + [script_path]
            cmd = list(runner) + [script_path]
            t0 = time.time()
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout / 1000)
                ret = {"exitCode": proc.returncode or 0,
                       "stdout": self._decode_out(stdout),
                       "stderr": self._decode_out(stderr),
                       "killed": False,
                       "duration": int((time.time() - t0) * 1000),
                       "language": lang}
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()
                ret = {"exitCode": 1,
                       "stdout": self._decode_out(stdout) if stdout else "",
                       "stderr": (self._decode_out(stderr) if stderr else "") + "\n[超时]",
                       "killed": True,
                       "duration": int((time.time() - t0) * 1000),
                       "language": lang}
            return ret
        except Exception as e:
            return {"exitCode": 1, "stdout": "", "stderr": str(e), "duration": 0, "killed": False, "language": language}
        finally:
            try:
                os.remove(script_path)
            except OSError:
                pass

    def _decode_out(self, b):
        if not b:
            return ""
        for enc in ['utf-8', 'gbk']:
            try:
                return b.decode(enc)
            except Exception:
                continue
        return b.decode('utf-8', errors='replace')

    # ── 自定义命令(run_custom)────────────────────────
    def _custom_commands_dir(self):
        """自定义命令脚本目录(客户端本地,随 agent.py 一起分发)"""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'custom-commands')

    async def _run_custom(self, name, args_list=None, timeout=120000):
        """执行客户端本地 custom-commands/ 目录里的预定义脚本。
        安全模型:脚本白名单(目录固定、文件名由中转服务器命令表校验),
        不能执行任意脚本,只能执行预置的那几个。"""
        import subprocess, time
        args_list = args_list or []
        name = (name or '').strip()
        if not name or '/' in name or '\\' in name or '..' in name:
            return {"exitCode": 1, "stdout": "", "stderr": "非法命令名", "duration": 0, "killed": False}
        cmds_dir = self._custom_commands_dir()
        # 查找脚本文件(支持 .py/.ps1/.sh/.bat/.js/.cmd)
        script_path = None
        for ext in ['.py', '.ps1', '.sh', '.bat', '.js', '.cmd', '.txt']:
            cand = os.path.join(cmds_dir, name + ext)
            if os.path.isfile(cand):
                script_path = cand
                break
        if not script_path:
            return {"exitCode": 1, "stdout": "", "stderr": f"自定义命令不存在: {name}(已检查 {cmds_dir})", "duration": 0, "killed": False}
        ext = os.path.splitext(script_path)[1].lower()
        lang_map = {'.py': 'python', '.ps1': 'powershell', '.sh': 'bash', '.bat': 'cmd', '.js': 'node', '.cmd': 'cmd'}
        try:
            runner, _ = self._resolve_interpreter(lang_map.get(ext, 'auto'))
        except ValueError as e:
            return {"exitCode": 1, "stdout": "", "stderr": str(e), "duration": 0, "killed": False}
        # 参数转字符串并传给脚本
        str_args = [str(a) for a in args_list]
        if not timeout or timeout <= 0:
            timeout = 120000
        timeout = min(timeout, 1800000)
        cmd = list(runner) + [script_path] + str_args
        cwd = self.sessions.get_current().cwd if self.sessions.get_current() else os.getcwd()
        t0 = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout / 1000)
                killed = False
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()
                killed = True
            return {
                "exitCode": proc.returncode or 0,
                "stdout": self._decode_out(stdout),
                "stderr": self._decode_out(stderr),
                "killed": killed,
                "duration": int((time.time() - t0) * 1000),
            }
        except Exception as e:
            return {"exitCode": 1, "stdout": "", "stderr": str(e), "duration": int((time.time() - t0) * 1000), "killed": False}

    async def _run_codegraph_index(self, path, timeout=600000, sync_only=False):
        """执行 codegraph 建索引。sync_only=True 时用 codegraph sync(增量),否则 init --force(全量)。
        返回结果(含索引统计)。"""
        import subprocess, time
        t0 = time.time()
        try:
            cg = self._find_codegraph()
            if not cg:
                return {"success": False, "error": "未找到 codegraph 命令(请先 npm install -g @colbymchenry/codegraph)", "duration": 0}
            # sync_only:增量同步;否则 init --force 全量重建
            cmd = cg + (['sync', path] if sync_only else ['init', path, '--force'])
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # 逐行读取 stdout,实时上报进度(阶段/文件数/统计/当前文件明细)
            out_lines = []
            err_lines = []
            self._emit(self.on_progress, {"phase": "start", "text": "开始建立代码索引...", "percent": 5})
            async def read_stream(stream, target, is_err=False):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    s = self._decode_out(line)
                    target.append(s)
                    ls = s.strip()
                    lsl = ls.lower()
                    # 文件级明细:能识别出"正在处理的具体文件"就上报,让进度条显示细节
                    if 'scanning' in lsl:
                        self._emit(self.on_progress, {"phase": "scan", "text": "正在扫描项目文件...", "detail": ls.strip(), "percent": 15})
                    elif 'parsing' in lsl:
                        self._emit(self.on_progress, {"phase": "parse", "text": "正在解析代码...", "detail": ls.strip(), "percent": 40})
                    elif 'resolving' in lsl:
                        self._emit(self.on_progress, {"phase": "resolve", "text": "正在解析符号引用...", "detail": ls.strip(), "percent": 65})
                    elif 'linking' in lsl:
                        self._emit(self.on_progress, {"phase": "link", "text": "正在关联动态调用...", "detail": ls.strip(), "percent": 85})
                    elif 'indexed' in lsl:
                        self._emit(self.on_progress, {"phase": "done", "text": ls, "percent": 95})
                    elif is_err and ls:
                        # 错误输出也透传,便于定位问题
                        self._emit(self.on_progress, {"phase": "running", "text": "...", "detail": ls, "percent": None})
            reader = asyncio.create_task(read_stream(proc.stdout, out_lines))
            err_reader = asyncio.create_task(read_stream(proc.stderr, err_lines, True))
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout / 1000)
                killed = False
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                killed = True
            await asyncio.gather(reader, err_reader, return_exceptions=True)
            out = ''.join(out_lines)
            err = ''.join(err_lines)
            dur = int((time.time() - t0) * 1000)
            if killed:
                return {"success": False, "error": f"索引超时(>{timeout // 1000}s)", "duration": dur}
            ok = proc.returncode == 0
            # 提取统计(Indexed N files / N nodes, M edges)
            msg = out
            self._emit(self.on_progress, {"phase": "done", "text": "索引完成", "percent": 100})
            return {"success": ok, "message": msg, "error": err if not ok else "", "duration": dur}
        except FileNotFoundError:
            return {"success": False, "error": "未找到 codegraph 命令(请先 npm install -g @colbymchenry/codegraph)", "duration": int((time.time() - t0) * 1000)}
        except Exception as e:
            return {"success": False, "error": str(e), "duration": int((time.time() - t0) * 1000)}

    # ── CodeGraph 语义索引(大型项目)─────────────────
    def _find_codegraph(self):
        """定位 codegraph 命令。兼容 Windows(.cmd 批处理)和 Linux。
        Windows 上 npm 全局装的是 codegraph.cmd,create_subprocess_exec 不认 .cmd,
        需用完整路径。返回可执行的命令列表([exe] 或 [exe, ...]),找不到返回 None。"""
        import shutil
        # 1) 常规 which
        p = shutil.which('codegraph')
        if p:
            if sys.platform == 'win32' and p.lower().endswith('.cmd'):
                # .cmd 需要经 shell 或 cmd /c 执行;这里转成 ['cmd', '/c', path] 形式
                return ['cmd', '/c', p]
            return [p]
        # 2) Windows: 常见 npm 全局目录
        if sys.platform == 'win32':
            for cand in [
                os.path.expandvars(r'%APPDATA%\npm\codegraph.cmd'),
                os.path.expandvars(r'%ProgramFiles%\nodejs\codegraph.cmd'),
            ]:
                if os.path.isfile(cand):
                    return ['cmd', '/c', cand]
        return None

    def _codegraph_root(self, path):
        """向上找最近的 .codegraph 目录(判断是否已建索引)"""
        p = os.path.abspath(path)
        while True:
            if os.path.isdir(os.path.join(p, '.codegraph')):
                return p
            parent = os.path.dirname(p)
            if parent == p:
                return None
            p = parent

    # ── 索引自动同步(常驻):代码变动时自动 codegraph sync ──
    _CG_IGNORE = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'target', 'dist', 'build',
                  '.idea', '.vscode', 'obj', 'bin', '.codegraph', '.cargo', 'worker'}
    _CG_EXTS = {'.rs', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.py', '.go', '.java', '.kt', '.kts',
                '.cs', '.php', '.rb', '.c', '.h', '.cpp', '.hpp', '.cc', '.swift', '.scala', '.dart',
                '.vue', '.svelte', '.astro', '.lua', '.r', '.ex', '.exs', '.sol', '.tf', '.nix', '.sh', '.sql'}

    def _cg_snapshot(self, root):
        """采集工作区代码文件快照(相对路径 -> mtime_ns),供变更检测。超大目录限深限量。"""
        snap = {}
        try:
            count = 0
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in self._CG_IGNORE]
                for f in filenames:
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in self._CG_EXTS:
                        continue
                    full = os.path.join(dirpath, f)
                    try:
                        snap[os.path.relpath(full, root)] = os.stat(full).st_mtime_ns
                    except Exception:
                        pass
                    count += 1
                    if count > 50000:
                        break
                if count > 50000:
                    break
        except Exception:
            pass
        return snap

    def _cg_changed(self, root, snap):
        """对比快照,返回是否发生代码变更(新增/修改/删除)。"""
        try:
            current = self._cg_snapshot(root)
        except Exception:
            return False
        if len(current) != len(snap):
            return True
        for k, v in current.items():
            if snap.get(k) != v:
                return True
        return False

    def _auto_sync_start(self):
        """启动常驻自动同步守护线程(daemon,随进程退出)"""
        if self._auto_sync_thread and self._auto_sync_thread.is_alive():
            return
        self._auto_sync_thread = threading.Thread(target=self._auto_sync_loop, daemon=True, name="codegraph-autosync")
        self._auto_sync_thread.start()

    def _auto_sync_loop(self):
        """守护循环:定期检查当前工作区代码变更,发现后自动 codegraph sync(增量)"""
        while True:
            try:
                if self._auto_sync_on:
                    self._auto_sync_once()
            except Exception:
                pass
            time.sleep(self._auto_sync_interval)

    def _auto_sync_once(self):
        """单次自动同步检查:当前工作区(已建索引且非索引中)有变更则跑 codegraph sync。"""
        try:
            session = self.sessions.get_current()
            work = session.cwd if session else None
            if not work:
                return
            root = self._codegraph_root(work)
            if not root:
                return  # 还没建过索引,不自动建(建索引要用户明确发起)
            if self._find_codegraph() is None:
                return
            with self._auto_sync_lock:
                prev = self._auto_sync_state.get(root)
                if prev is None:
                    self._auto_sync_state[root] = self._cg_snapshot(root)
                    return
                if not self._cg_changed(root, prev):
                    return
            # 有变更 → 增量同步(快),用同步 subprocess 直接跑(不依赖事件循环,更健壮)
            self._emit(self.on_log, f"[索引] 检测到代码变更,自动同步...")
            try:
                import subprocess
                cg = self._find_codegraph()
                subprocess.run(cg + ['sync', root], capture_output=True, timeout=600)
            finally:
                with self._auto_sync_lock:
                    self._auto_sync_state[root] = self._cg_snapshot(root)
        except Exception:
            pass

    async def _check_codegraph(self, path):
        """检查目录:如果是代码项目,上报项目概况(文件数/是否已索引)到 UI"""
        import shutil
        if not path:
            return
        is_codegraph = self._find_codegraph() is not None
        # 统计文件数(忽略常见构建/依赖目录)
        ignore = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'target', 'dist', 'build', '.idea', '.vscode', 'obj', 'bin'}
        file_count = 0
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d not in ignore]
            file_count += len(filenames)
            if file_count > 5000:
                break
        indexed = self._codegraph_root(path) is not None
        # 上报项目概况(含索引状态)--UI 显示成信息卡片
        self._emit(self.on_result, {
            "kind": "project_status",
            "command": "项目概况",
            "exitCode": 0,
            "stdout": (
                f"📂 工作区: {path}\n"
                f"📄 文件数: {file_count}+\n"
                f"🧠 CodeGraph 索引: {'✅ 已建立' if indexed else '❌ 未建立'}\n"
                + ("💡 可执行 codegraph_index 建立语义索引(大项目推荐)" if (not indexed and is_codegraph and file_count > 300) else "")
            ),
            "stderr": "",
            "cwd": path,
        })
        self._emit(self.on_log, f"[项目] {path}({file_count}+ 文件,索引{'已建' if indexed else '未建'})")

    # ── 亲和通道:环境自述(get_environment)───────────────
    def _get_environment(self):
        """返回一份环境档案:可用解释器、常用工具、工作区、系统信息。算力在沙箱,这里只做探测。"""
        import shutil
        info = self._get_info()
        # 探测常用工具(GNU 工具是否可用)
        tools = {}
        for t in ['bash', 'python', 'node', 'npm', 'git', 'jq', 'curl', 'wget', 'tar', 'unzip', 'grep', 'sed', 'awk', 'wc', 'find', 'rg', 'fd']:
            tools[t] = bool(shutil.which(t))
        # Git for Windows 的 bash 不在 PATH,单独探测
        bash = self._find_bash()
        if not tools.get('bash') and bash:
            tools['bash'] = True

        # 当前会话
        session = self.sessions.get_current()
        sessions = []
        try:
            sessions = self.sessions.list_all() or []
        except Exception:
            sessions = []

        return {
            "system": {
                "hostname": info.get("hostname"), "platform": info.get("platform"),
                "arch": info.get("arch"), "cpus": info.get("cpus"),
                "homedir": info.get("homedir"), "user": info.get("userInfo", {}).get("username"),
            },
            "shells": {"bash": tools.get('bash'), "powershell": sys.platform.startswith('win'), "cmd": sys.platform.startswith('win')},
            "interpreters": {"python": tools.get('python'), "node": tools.get('node'), "npm": tools.get('npm')},
            "tools": tools,
            "git": tools.get('git'),
            "workspace": {
                "permission": self.permission,
                "defaultWorkDir": self.default_work_dir,
                "currentCwd": session.cwd if session else os.getcwd(),
                "sessions": sessions,
                "hint": self._workspace_hint(),  # 工作区模式详细限制(超级模式为 None)
            },
        }

    def _workspace_hint(self):
        """工作区模式限制自述:Agent 一进来就该知道边界和禁用命令,避免用错命令碰壁。"""
        if self.permission != "workspace":
            return None
        session = self.sessions.get_current()
        workspace = os.path.normpath(session.workDir) if session else os.path.normpath(self.default_work_dir)
        return {
            "mode": "workspace",
            "currentCwd": session.cwd if session else os.getcwd(),
            "workspaceRoot": workspace,
            "allowedPathPrefix": workspace + os.sep,  # 读写文件只能在这个目录内
            "pathRules": [
                "读写文件用相对路径(相对当前工作目录),不要带盘符绝对路径",
                f"允许范围: {workspace} 及其子目录",
                "如需访问工作区外文件,请联系用户切换到超级模式",
            ],
            "commandRules": [
                "禁止使用绝对路径(如 C:\\... 或 /home/...)",
                "禁止使用 .. 逃逸出工作区",
                "禁止 cd/chdir/Set-Location/pushd/popd 切换目录",
                "建议用相对路径操作本目录内文件,如 python -c / powershell 处理相对路径",
            ],
            "tip": "所有命令和文件操作都基于当前工作目录,用相对路径最安全",
        }

    async def _handle_session_op(self, op, payload):
        try:
            if op == "create":
                work_dir = payload.get("workDir", "")
                # 工作区模式下,Agent 不能自己划定工作区,只能用用户设定的默认工作区
                if self.permission == "workspace" and work_dir:
                    if os.path.normpath(work_dir) != os.path.normpath(self.default_work_dir):
                        return {"success": False, "error": "工作区模式下不能自定义工作目录,只能使用默认工作区"}
                    work_dir = self.default_work_dir
                result = self.sessions.create(work_dir, payload.get("name"))
                # 创建会话后检查该目录是否需要 CodeGraph 索引(大项目自动提示)
                if result.get("success"):
                    await self._check_codegraph(work_dir or self.sessions.get_current().cwd)
                return result
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
                result = self.sessions.switch(payload.get("sessionId", ""))
                # 切换工作区后上报项目概况
                if result.get("success"):
                    cur = self.sessions.get_current()
                    if cur:
                        await self._check_codegraph(cur.cwd)
                return result
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