"""
云桥 - 桌面客户端（pywebview 版）
=============================
职责：UI 显示（设置、连接状态、日志、配对码）
不负责：WebSocket 连接、命令执行（那是 agent.py 的事）

用法:
  pip install pywebview websockets
  python desktop.py
"""

import json
import os
import platform
import sys
import threading
import time
import random
from pathlib import Path

# ─── 配置 ────────────────────────────────────────
CONFIG_DIR = Path(os.environ.get("YUNQIAO_CONFIG", str(Path.home() / ".yunqiao")))
CONFIG_FILE = CONFIG_DIR / "config.json"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

DEVICE_NAME = os.environ.get("DEVICE_NAME", platform.node())

def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text("utf-8"))
        except:
            pass
    return {}

def save_config(relay_url, key, name, auto_connect=False):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({
        "relayUrl": relay_url, "key": key, "deviceName": name,
        "autoConnect": auto_connect
    }, indent=2), "utf-8")

cfg = load_config()
RELAY_URL = cfg.get("relayUrl", "")
RELAY_KEY = cfg.get("key", "")

# ─── 全局状态 ────────────────────────────────────
UI = None
pair_code = str(random.randint(100000, 999999))
agent = None  # Agent 实例，首次连接时创建

# ─── Agent 集成 ──────────────────────────────────
def get_agent():
    global agent
    if agent is None:
        from agent import Agent
        agent = Agent(RELAY_URL, RELAY_KEY, DEVICE_NAME, pair_code)
        agent.on_log = lambda msg: notify_ui("log", {"text": msg})
        def on_status(s):
            if "agent" in s:
                notify_ui("agent_status", {
                    "status": s["agent"],
                    "latency": s.get("latency", 0),
                    "agentId": s.get("agentId", ""),
                    "agentPlatform": s.get("agentPlatform", ""),
                    "agentHostname": s.get("agentHostname", ""),
                    "relayPlatform": s.get("relayPlatform", ""),
                })
            else:
                notify_ui("relay_status", {
                    "status": "connected" if s.get("connected") else "disconnected",
                    "latency": s.get("latency", 0)
                })
        agent.on_status = on_status
        agent.on_result = lambda r: notify_ui("command_result", {"payload": r})
        agent.on_command = lambda c: notify_ui("command_start", {"cmd": c})
        agent.on_progress = lambda p: notify_ui("task_progress", {"progress": p})
        agent.on_messages_read = lambda ids: notify_ui("messages_read", {"ids": ids})
        agent.on_activity = lambda a: notify_ui("agent_activity", a)
    return agent

def start_agent():
    a = get_agent()
    if a._running:
        # 已在运行中，仅同步状态
        threading.Timer(0.5, lambda: sync_ui_state(a)).start()
        return
    a.start()
    # 同步会话和状态到 UI
    threading.Timer(1.0, lambda: sync_ui_state(a)).start()

def sync_ui_state(a):
    sl = a.sessions.list_all()
    cur = a.sessions.get_current()
    notify_ui("sync_status", {
        "pairCode": a.auth_code,
        "workDir": cur.workDir if cur else a.default_work_dir,
        "sessions": sl.get("sessions", []),
        "currentSessionId": sl.get("defaultId"),
    })

def stop_agent():
    global agent
    if agent:
        agent.stop()
        agent = None

def notify_ui(action, data):
    if UI:
        try:
            js = f"if(window.handleBridge)window.handleBridge('{action}',{json.dumps(data)})"
            UI.evaluate_js(js)
        except:
            pass

# ─── JS Bridge API ───────────────────────────────
class Api:
    def get_status(self):
        a = agent
        # 主动拉取活跃度快照（UI 定时轮询完底，不依赖推送）；未连接时强制归零，绝不返回旧缓存
        ZERO = {"connections": 0, "runningTasks": 0, "pendingCalls": 0, "maxConnections": 50}
        activity = {}
        try:
            if a is not None and a.connected:
                activity = a.get_activity()
            else:
                activity = dict(ZERO)
        except Exception:
            activity = dict(ZERO)
        return {
            "pairCode": pair_code,
            "deviceName": DEVICE_NAME,
            "hostname": platform.node(),
            "platform": "Windows",
            "relayStatus": "已连接" if (a and a.connected) else "未连接",
            "connected": a is not None and a.connected,
            "homeDir": str(Path.home()),  # 用户主目录（新建工作区弹窗的默认目录，避免硬编码 C:\Users\Administrator 导致无权限）
            "activity": activity,
        }

    def save_settings(self, key, relay_url, auto_connect=False):
        global RELAY_URL, RELAY_KEY, agent
        RELAY_URL = relay_url
        RELAY_KEY = key
        save_config(relay_url, key, DEVICE_NAME, auto_connect)
        if agent:
            agent.stop()
            # 必须置 None，否则下次 get_agent() 返回旧实例，仍用旧的 relay_url/key 重连
            agent = None
        return {"success": True}

    def get_settings(self):
        return {"key": RELAY_KEY, "relayUrl": RELAY_URL, "deviceName": DEVICE_NAME,
                "autoConnect": cfg.get("autoConnect", False)}

    def toggle_connect(self):
        if agent and agent.connected:
            agent.stop()
            notify_ui("relay_status", {"status": "disconnected"})
            notify_ui("log", {"text": "已断开连接"})
            return {"connected": False}
        elif agent and agent._running:
            notify_ui("log", {"text": "正在连接中..."})
            return {"connected": False}
        else:
            if not RELAY_URL or not RELAY_KEY:
                notify_ui("log", {"text": "请先设置中继地址和密钥"})
                return {"connected": False}
            start_agent()
            notify_ui("log", {"text": "正在连接..."})
            return {"connected": True}

    def refresh_pair_code(self):
        global pair_code
        pair_code = str(random.randint(100000, 999999))
        if agent:
            agent.update_code(pair_code)
        return {"pairCode": pair_code}

    def send_message(self, text, urgent=False):
        """发送消息给上游 Agent（Agent 通过 get_client_messages 读取）"""
        a = agent
        if not a:
            return {"success": False, "error": "未连接"}
        if not a.connected:
            return {"success": False, "error": "未连接中继服务器"}
        msg_id = a.send_message(text, bool(urgent))
        return {"success": True, "msgId": msg_id}

    def get_codegraph_status(self, path=None):
        """查询当前工作区的索引状态，并给出'是否需要建索引'的建议。
        供 UI 在确认框/提示中显示：文件规模、codegraph 是否安装、是否已建索引。"""
        import os, shutil
        try:
            a = get_agent()
        except Exception:
            a = None
        work_dir = path or (a.sessions.get_current().cwd if a and a.sessions.get_current() else None) or cfg.get("workDir", "")
        if not work_dir or not os.path.isdir(work_dir):
            return {"success": False, "error": "未设置工作区", "workDir": work_dir}
        # codegraph 是否安装
        installed = a._find_codegraph() is not None if a else False
        # 文件数（忽略构建/依赖目录）
        ignore = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'target', 'dist', 'build', '.idea', '.vscode', 'obj', 'bin'}
        file_count = 0
        try:
            for dirpath, dirnames, filenames in os.walk(work_dir):
                dirnames[:] = [d for d in dirnames if d not in ignore]
                file_count += len(filenames)
                if file_count > 5000:
                    break
        except Exception:
            pass
        # 是否已建索引
        indexed = a._codegraph_root(work_dir) is not None if a else False
        # 建议：文件较多且未建索引且已装 codegraph
        recommend = installed and not indexed and file_count >= 50
        return {
            "success": True,
            "workDir": work_dir,
            "fileCount": file_count,
            "codegraphInstalled": installed,
            "indexed": indexed,
            "recommend": recommend,
            "advice": (
                "✅ 已建立索引" if indexed
                else ("⛔ 未安装 codegraph（npm install -g @colbymchenry/codegraph）" if not installed
                else (f"💡 建议建立索引（{file_count}+ 文件）" if recommend
                else f"ℹ 文件较少（{file_count}），暂可不建索引"))
            ),
        }

    def build_index(self, path=None):
        """用户主动建立 CodeGraph 索引（后台执行，进度通过 notify_ui('task_progress') 上报）
        无需等待 Agent 连接即可使用（用独立线程 + 事件循环）。"""
        import asyncio, threading
        # 确保 agent 实例存在（即使未连接服务器，也能用其工具方法）
        try:
            a = get_agent()
        except Exception:
            a = None
        work_dir = path or (a.sessions.get_current().cwd if a and a.sessions.get_current() else None) or cfg.get("workDir", "")
        if not work_dir:
            return {"success": False, "error": "未设置工作区"}
        if a is None:
            return {"success": False, "error": "客户端引擎未就绪"}
        # 确保 on_progress 转发到 UI
        a.on_progress = lambda p: notify_ui("task_progress", {"progress": p})
        # 用独立线程跑 asyncio（不阻塞 UI）
        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(a._run_codegraph_index(work_dir))
            except Exception as e:
                result = {"success": False, "error": str(e)}
            finally:
                loop.close()
            notify_ui("log", {"text": f"[索引] 完成: {'成功' if result.get('success') else '失败'}"})
            # 完成后补一份 project_status（更新索引状态显示）
            if result.get("success"):
                try:
                    loop2 = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop2)
                    loop2.run_until_complete(a._check_codegraph(work_dir))
                    loop2.close()
                except Exception:
                    pass
        threading.Thread(target=run, daemon=True).start()
        return {"success": True, "message": "索引任务已启动"}


    def reorder_messages(self, ordered_ids):
        """拖拽排序任务队列后，同步新顺序到中继"""
        a = agent
        if a:
            a.reorder_messages(list(ordered_ids))
            return {"success": True}
        return {"success": False, "error": "未连接"}

    def delete_messages(self, ids):
        """从任务队列删除消息"""
        a = agent
        if a:
            n = a.delete_messages(list(ids))
            return {"success": True, "deleted": n}
        return {"success": False, "error": "未连接"}

    def edit_message(self, msg_id, text):
        """编辑任务队列中某条消息"""
        a = agent
        if a:
            a.edit_message(msg_id, text)
            return {"success": True}
        return {"success": False, "error": "未连接"}

    def get_mcp_ticket(self):
        """向中继请求新的动态 MCP 地址 ticket（旧 ticket 作废）"""
        a = agent
        if a:
            ticket = a.get_mcp_ticket()
            if ticket:
                return {"success": True, "ticket": ticket}
            return {"success": False, "error": "获取失败，请检查中继连接"}
        return {"success": False, "error": "未连接"}

    def browse_folder(self):
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.askdirectory(title="选择工作目录")
        root.destroy()
        return path or ""

    def get_sessions(self):
        if agent:
            sl = agent.sessions.list_all()
            cur = agent.sessions.get_current()
            return {
                "sessions": sl.get("sessions", []),
                "currentId": sl.get("defaultId"),
                "workDir": cur.workDir if cur else agent.default_work_dir
            }
        # Agent 未启动，尝试从磁盘恢复
        sessions_file = CONFIG_DIR / "sessions.json"
        if sessions_file.exists():
            try:
                data = json.loads(sessions_file.read_text("utf-8"))
                sessions = data.get("sessions", [])
                default_id = data.get("defaultId")
                if sessions:
                    cur = next((s for s in sessions if s.get("id") == default_id), sessions[0])
                    return {
                        "sessions": sessions,
                        "currentId": default_id,
                        "workDir": cur.get("workDir") or cur.get("cwd", "")
                    }
            except:
                pass
        # 无缓存：返回默认
        if getattr(sys, 'frozen', False):
            base = Path(sys.executable).parent
        else:
            base = Path(__file__).parent.parent
        dw = str(base / 'worker')
        return {
            "sessions": [{"id": "default", "name": "默认工作区", "workDir": dw, "cwd": dw, "isDefault": True}],
            "currentId": "default",
            "workDir": dw
        }

    def create_session(self, work_dir, name=None):
        """在远端创建新工作区（用户通过 UI 设定，不走 #5 限制）"""
        try:
            a = agent or get_agent()  # 未连接时也创建本地实例（连接后自动同步）
            if not work_dir:
                return {"success": False, "error": "工作目录不能为空"}
            work_dir = os.path.abspath(work_dir)
            # 目录不存在则创建（否则 Agent 之后在该目录执行命令会失败）
            try:
                os.makedirs(work_dir, exist_ok=True)
            except Exception as e:
                return {"success": False, "error": f"目录不可用: {e}"}
            result = a.sessions.create(work_dir, name)
            return {"success": True, "session": result}
        except Exception as e:
            # 兜底：把真实异常返回给 UI（否则前端只看到无提示的失败）
            return {"success": False, "error": f"创建失败: {e}"}

    def close_session(self, session_id):
        """关闭并删除一个工作区（后端会话）"""
        a = agent or get_agent()
        result = a.sessions.close(session_id)
        if result.get("success"):
            return {"success": True}
        # 会话不存在（已关闭或为占位会话）：视为已删除
        if isinstance(result.get("error"), str) and "不存在" in result["error"]:
            return {"success": True}
        return {"success": False, "error": result.get("error", "关闭失败")}

    def switch_session(self, session_id):
        """切换当前工作区（影响 Agent 的默认工作目录）"""
        a = agent or get_agent()
        result = a.sessions.switch(session_id)
        if result.get("success"):
            return {"success": True}
        return {"success": False, "error": result.get("error", "切换失败")}

    def set_permission(self, mode):
        if agent:
            agent.set_permission(mode)
            return {"success": True}
        return {"success": False, "error": "Agent 未启动"}

    def start_drag(self):
        """标题栏拖拽窗口"""
        if UI:
            UI.start_drag()
        return {"success": True}

    def window_minimize(self):
        """无边框窗口的最小化"""
        if UI:
            UI.minimize()
        return {"success": True}

    def window_close(self):
        """无边框窗口的关闭"""
        if UI:
            UI.destroy()
        return {"success": True}

    def disconnect_agent(self):
        """断开当前所有 Agent SSE 连接，释放给其他智能体"""
        a = agent
        if a and a._ws:
            # 通过 WebSocket 发送断开 Agent 的请求
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    a._send_ws({"type": "disconnect_agent", "requestId": "dc"}),
                    a._loop
                )
                fut.result(timeout=3)
            except Exception:
                pass
        return {"success": True}

    def confirm_dialog(self, title, message):
        """原生确认对话框（tkinter，Windows 风格）"""
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        result = messagebox.askyesno(title, message)
        root.destroy()
        return bool(result)


# ─── 启动 ────────────────────────────────────────
def main():
    global UI
    import webview

    ui_path = os.path.join(os.path.dirname(__file__), "ui.html")
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后 ui.html 在 _MEIPASS 根目录
        ui_path = os.path.join(sys._MEIPASS, "ui.html")
        if not os.path.exists(ui_path):
            # 降级尝试 exe 所在目录
            ui_path = os.path.join(os.path.dirname(sys.executable), "ui.html")
    if not os.path.exists(ui_path):
        print(f"❌ 找不到 ui.html: {ui_path}")
        sys.exit(1)

    api = Api()

    UI = webview.create_window(
        title="云桥",
        url=ui_path,
        js_api=api,
        width=1096,
        height=699,
        min_size=(800, 500),
        resizable=True,
        frameless=True,   # 无系统标题栏，用自绘标题栏
        easy_drag=False,  # 关闭 pywebview 全局 easy_drag（默认 True 会让整个窗口任意位置可拖，日志区无法选字）；改用 .pywebview-drag-region class 限定仅标题栏可拖
        shadow=True,      # 无边框窗口的阴影边框
    )

    # 读取自动连接配置
    auto_connect = cfg.get("autoConnect", False)
    if auto_connect and RELAY_URL and RELAY_KEY:
        threading.Timer(2.0, start_agent).start()

    webview.start()


if __name__ == "__main__":
    main()