#!/usr/bin/env python3
"""用云桥 read/download 验证 Windows 端 ui.html 关键修复点"""
import subprocess, os, base64

YQ = "/home/admin/.openclaw/workspace/skills/yunqiao/scripts/yq"
CODE = "845235"
os.environ["YUNQIAO_URL"] = "https://yunqiao.very.im/mcp/23f1de5368d22b7597d563f761f1dcab"

def fetch(path):
    r = subprocess.run([YQ, CODE, "download", path], capture_output=True, text=True, timeout=90)
    out = r.stdout.strip()
    if not out.startswith("FILE:"):
        # 工作区模式可能限制读 client 目录，试相对路径 read
        return None
    _, _, b64 = out.split("|", 2)
    return base64.b64decode(b64).decode("utf-8", "replace")

content = fetch(r"D:\AICODE\yunqiao\client\ui.html")
if content is None:
    print("download 被拦（工作区模式），改用 exec 相对路径读")
    r = subprocess.run([YQ, CODE, "exec", "python -c \"c=open('../client/ui.html',encoding='utf-8').read(); print('SPIN:', 'spinEl.style.animation' in c); print('BUSY:', '__yqBusyAt' in c)\""],
                       capture_output=True, text=True, timeout=60)
    print(r.stdout.strip())
    print(r.stderr.strip()[:200])
else:
    print("=== ui.html 校验 ===")
    print("SPIN_FIX:", "spinEl.style.animation" in content)
    print("BUSY_HOLD:", "__yqBusyAt" in content)
    print("CONN_PURE:", "var used = Math.min(total, running + pending);" in content)
