"""
云端协同 MCP - Windows 桌面监控面板
用法: python desktop_monitor.py

功能:
  - 实时显示服务器连接状态 + 延迟
  - 显示智能体活动（谁连进来了、执行什么命令、在哪个目录）
  - 智能体与本机延迟
  - 最小化到右下角托盘
"""

import asyncio
import json
import os
import platform
import sys
import time
import threading
import subprocess
import queue
import random

# ─── 配置 ───────────────────────────────────────
RELAY_URL = os.environ.get("RELAY_URL", "wss://yunqiao.very.im/device")
RELAY_PSK = os.environ.get("RELAY_PSK")
if not RELAY_PSK:
    print("❌ 必须设置 RELAY_PSK 环境变量")
    sys.exit(1)
DEVICE_NAME = os.environ.get("DEVICE_NAME", platform.node())
RECONNECT_DELAY = int(os.environ.get("RECONNECT_DELAY", "5000"))

# 验证码
def generate_code():
    return str(random.randint(100000, 999999))

AUTH_CODE = generate_code()

# 尝试导入 websockets
try:
    import websockets
except ImportError:
    print("正在安装 websockets 库...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets
    print("安装完成!")

# 尝试导入系统托盘库
try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False
    print("提示: 安装 pystray 和 pillow 可启用系统托盘: pip install pystray pillow")

# ─── 全局状态 ──────────────────────────────────
server_status = {"connected": False, "latency": 0, "device_id": None}
activities = []  # 活动日志
MAX_ACTIVITIES = 100
msg_queue = queue.Queue()

# ─── 活动日志 ──────────────────────────────────
def add_activity(msg_type, detail=""):
    t = time.strftime("%H:%M:%S")
    activities.append({"time": t, "type": msg_type, "detail": detail})
    if len(activities) > MAX_ACTIVITIES:
        activities.pop(0)
    msg_queue.put("refresh")


# ─── WebSocket 客户端 ──────────────────────────
def refresh_code():
    global AUTH_CODE
    AUTH_CODE = generate_code()
    add_activity("system", f"验证码已刷新: {AUTH_CODE}")
    return AUTH_CODE


async def ws_client():
    url = f"{RELAY_URL}?psk={RELAY_PSK}"
    add_activity("system", f"正在连接服务器 {RELAY_URL}...")

    while True:
        try:
            t_start = time.time()
            async with websockets.connect(url, ping_interval=10) as ws:
                latency = (time.time() - t_start) * 1000
                server_status["connected"] = True
                server_status["latency"] = round(latency, 1)
                add_activity("system", f"服务器已连接，延迟 {latency:.0f}ms")

                # 注册设备（带上验证码）
                await ws.send(json.dumps({
                    "type": "register",
                    "deviceName": DEVICE_NAME,
                    "os": sys.platform,
                    "arch": platform.machine(),
                    "hostname": platform.node(),
                    "authCode": AUTH_CODE,
                }))

                # 延迟心跳
                last_ping = time.time()

                async for message in ws:
                    try:
                        msg = json.loads(message)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type")
                    request_id = msg.get("requestId")

                    if msg_type == "register_result" and msg.get("success"):
                        server_status["device_id"] = msg.get("deviceId")
                        add_activity("system", f"注册成功，设备ID: {msg.get('deviceId')}")
                        continue

                    # 处理命令
                    payload = msg.get("payload", {})
                    await handle_command(ws, msg_type, request_id, payload)

                    # 更新最后活动时间
                    last_ping = time.time()

        except websockets.exceptions.ConnectionClosed:
            server_status["connected"] = False
            add_activity("system", "连接断开，正在重连...")
        except Exception as e:
            server_status["connected"] = False
            add_activity("error", f"连接错误: {e}")
        finally:
            server_status["connected"] = False

        await asyncio.sleep(RECONNECT_DELAY / 1000)


async def handle_command(ws, msg_type, request_id, payload):
    add_activity("command", f"[收到] {msg_type}")

    if msg_type == "execute_command":
        command = payload.get("command", "")
        timeout = payload.get("timeout", 30000)

        # 记录命令和目录
        working_dir = os.getcwd()
        add_activity("execute", f"📂 {working_dir}")
        add_activity("execute", f"💻 {command}")

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
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

            stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
            stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""

            # 截断过长输出
            log_out = stdout_str[:500] + ("..." if len(stdout_str) > 500 else "")
            if log_out:
                add_activity("result", log_out)

            await ws.send(json.dumps({
                "type": "command_result", "requestId": request_id,
                "payload": {
                    "exitCode": exit_code,
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                    "killed": killed,
                },
            }))
            add_activity("result", f"✅ 退出码: {exit_code}" + (" (超时被终止)" if killed else ""))
        except Exception as e:
            add_activity("error", f"命令执行失败: {e}")
            await ws.send(json.dumps({
                "type": "command_result", "requestId": request_id,
                "payload": {"exitCode": 1, "stdout": "", "stderr": str(e), "killed": False},
            }))

    elif msg_type == "read_file":
        path = payload.get("path", "")
        add_activity("file", f"📖 读取文件: {path}")
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            size = os.path.getsize(path)
            await ws.send(json.dumps({
                "type": "file_result", "requestId": request_id,
                "payload": {"success": True, "content": content, "size": size, "path": path},
            }))
            add_activity("file", f"✅ 读取成功 ({size} bytes)")
        except Exception as e:
            await ws.send(json.dumps({
                "type": "file_result", "requestId": request_id,
                "payload": {"success": False, "error": str(e), "path": path},
            }))
            add_activity("error", f"读取失败: {e}")

    elif msg_type == "write_file":
        path = payload.get("path", "")
        content = payload.get("content", "")
        add_activity("file", f"✏️ 写入文件: {path}")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            await ws.send(json.dumps({
                "type": "file_result", "requestId": request_id,
                "payload": {"success": True, "path": path},
            }))
            add_activity("file", f"✅ 写入成功 ({len(content)} bytes)")
        except Exception as e:
            await ws.send(json.dumps({
                "type": "file_result", "requestId": request_id,
                "payload": {"success": False, "error": str(e), "path": path},
            }))
            add_activity("error", f"写入失败: {e}")

    elif msg_type == "get_device_info":
        import shutil
        await ws.send(json.dumps({
            "type": "device_info", "requestId": request_id,
            "payload": {
                "hostname": platform.node(),
                "platform": sys.platform,
                "arch": platform.machine(),
                "cpus": os.cpu_count() or 0,
                "totalMem": shutil.disk_usage("/").total if hasattr(shutil, "disk_usage") else 0,
                "freeMem": shutil.disk_usage("/").free if hasattr(shutil, "disk_usage") else 0,
                "uptime": time.time(),
                "homedir": os.path.expanduser("~"),
                "userInfo": {"username": os.getlogin()},
            },
        }))


# ─── 后台线程 ──────────────────────────────────
def run_ws_loop():
    asyncio.run(ws_client())


# ─── Tkinter UI ────────────────────────────────
def build_ui():
    import tkinter as tk
    from tkinter import scrolledtext, ttk

    root = tk.Tk()
    root.title("云端协同 MCP - 本地代理")
    root.geometry("700x550")
    root.minsize(600, 400)

    # 颜色主题
    BG_DARK = "#1e1e2e"
    BG_CARD = "#2d2d44"
    FG = "#cdd6f4"
    ACCENT = "#89b4fa"
    GREEN = "#a6e3a1"
    RED = "#f38ba8"
    YELLOW = "#f9e2af"

    root.configure(bg=BG_DARK)

    # ─── 顶部状态栏 ───
    status_frame = tk.Frame(root, bg=BG_CARD, height=60)
    status_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

    status_label = tk.Label(status_frame, text="● 连接中...", fg=YELLOW, bg=BG_CARD,
                            font=("Segoe UI", 11, "bold"))
    status_label.pack(side=tk.LEFT, padx=15, pady=10)

    latency_label = tk.Label(status_frame, text="延迟: -- ms", fg=FG, bg=BG_CARD,
                             font=("Segoe UI", 10))
    latency_label.pack(side=tk.LEFT, padx=5, pady=10)

    device_label = tk.Label(status_frame, text="设备: --", fg=FG, bg=BG_CARD,
                            font=("Segoe UI", 10))
    device_label.pack(side=tk.RIGHT, padx=15, pady=10)

    # ─── 中间区域：活动面板 ───
    main_frame = tk.Frame(root, bg=BG_DARK)
    main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))

    # 活动日志
    log_frame = tk.Frame(main_frame, bg=BG_DARK)
    log_frame.pack(fill=tk.BOTH, expand=True)

    log_header = tk.Label(log_frame, text="📋 智能体活动", fg=FG, bg=BG_DARK,
                          font=("Segoe UI", 10, "bold"), anchor="w")
    log_header.pack(fill=tk.X, pady=(0, 3))

    log_text = scrolledtext.ScrolledText(
        log_frame, bg=BG_CARD, fg=FG, insertbackground=FG,
        font=("Consolas", 10), relief=tk.FLAT, borderwidth=0,
        state=tk.DISABLED, wrap=tk.WORD, height=15,
    )
    log_text.pack(fill=tk.BOTH, expand=True)

    # ─── 底部统计栏 ───
    stats_frame = tk.Frame(root, bg=BG_CARD, height=30)
    stats_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

    cmd_count_label = tk.Label(stats_frame, text="命令: 0", fg=FG, bg=BG_CARD,
                               font=("Segoe UI", 9))
    cmd_count_label.pack(side=tk.LEFT, padx=10, pady=5)

    uptime_label = tk.Label(stats_frame, text="运行时间: 0s", fg=FG, bg=BG_CARD,
                            font=("Segoe UI", 9))
    uptime_label.pack(side=tk.RIGHT, padx=10, pady=5)

    # ─── 验证码区域 ───
    code_frame = tk.Frame(root, bg=BG_CARD, height=50)
    code_frame.pack(fill=tk.X, padx=10, pady=(0, 5))

    code_label = tk.Label(code_frame, text=f"🔐 验证码: {AUTH_CODE}", fg=ACCENT, bg=BG_CARD,
                          font=("Segoe UI", 14, "bold"))
    code_label.pack(side=tk.LEFT, padx=15, pady=8)

    def refresh_click():
        new_code = refresh_code()
        code_label.config(text=f"🔐 验证码: {new_code}")

    refresh_btn = tk.Button(code_frame, text="🔄 重新生成", command=refresh_click,
                            bg=BG_DARK, fg=FG, font=("Segoe UI", 9),
                            relief=tk.FLAT, padx=10, cursor="hand2")
    refresh_btn.pack(side=tk.RIGHT, padx=15, pady=8)

    # ─── 系统托盘 ───
    def on_minimize():
        if HAS_TRAY:
            root.withdraw()
            add_activity("system", "已最小化到系统托盘")

    def on_show():
        root.deiconify()
        root.lift()

    if HAS_TRAY:
        # 创建托盘图标
        icon_size = 64
        icon_img = Image.new("RGBA", (icon_size, icon_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(icon_img)
        draw.ellipse([8, 8, 56, 56], fill="#89b4fa")
        draw.text((20, 18), "MCP", fill="#1e1e2e")

        tray_menu = pystray.Menu(
            pystray.MenuItem("显示窗口", on_show),
            pystray.MenuItem("退出", root.quit),
        )
        tray_icon = pystray.Icon("cloud_mcp", icon_img, "云端协同 MCP", tray_menu)

        # 托盘图标在后台线程运行
        def run_tray():
            tray_icon.run()
        threading.Thread(target=run_tray, daemon=True).start()

        root.protocol("WM_DOWNLOAD", on_minimize)

    # ─── UI 更新循环 ───
    start_time = time.time()
    cmd_count = 0

    def update_ui():
        nonlocal cmd_count
        now = time.time()

        # 更新状态
        if server_status["connected"]:
            status_label.config(text="● 已连接", fg=GREEN)
            latency_label.config(text=f"延迟: {server_status['latency']}ms")
        else:
            status_label.config(text="● 未连接", fg=RED)
            latency_label.config(text="延迟: -- ms")

        if server_status["device_id"]:
            device_label.config(text=f"设备: {server_status['device_id'][:8]}...")

        uptime = now - start_time
        if uptime < 60:
            uptime_label.config(text=f"运行时间: {int(uptime)}s")
        elif uptime < 3600:
            uptime_label.config(text=f"运行时间: {int(uptime//60)}m {int(uptime%60)}s")
        else:
            uptime_label.config(text=f"运行时间: {int(uptime//3600)}h {int(uptime%60//60)}m")

        # 处理队列中的刷新
        try:
            while True:
                msg_queue.get_nowait()
        except queue.Empty:
            pass

        # 更新日志
        log_text.config(state=tk.NORMAL)
        log_text.delete(1.0, tk.END)

        # 显示最近的活动
        for a in activities[-30:]:
            tag = a["type"]
            line = f"[{a['time']}] "
            if tag == "system":
                line += f"🔧 {a['detail']}"
            elif tag == "command":
                line += f"📨 {a['detail']}"
            elif tag == "execute":
                line += f"  {a['detail']}"
                cmd_count += 1
            elif tag == "result":
                line += f"  {a['detail']}"
            elif tag == "file":
                line += f"📁 {a['detail']}"
            elif tag == "error":
                line += f"❌ {a['detail']}"
            else:
                line += a["detail"]
            log_text.insert(tk.END, line + "\n")

        log_text.config(state=tk.DISABLED)
        log_text.see(tk.END)

        cmd_count_label.config(text=f"命令: {cmd_count}")

        root.after(1000, update_ui)

    update_ui()
    return root


# ─── 启动 ──────────────────────────────────────
def main():
    import tkinter as tk

    # 启动 WebSocket 后台线程
    ws_thread = threading.Thread(target=run_ws_loop, daemon=True)
    ws_thread.start()

    # 启动 UI
    root = build_ui()
    root.mainloop()


if __name__ == "__main__":
    main()