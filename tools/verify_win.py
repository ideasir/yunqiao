#!/usr/bin/env python3
"""从 Windows 拉取修复后的文件，验证关键修复点"""
import subprocess, os, base64

YQ = "/home/admin/.openclaw/workspace/skills/yunqiao/scripts/yq"
CODE = "248853"
os.environ["YUNQIAO_URL"] = "https://yunqiao.very.im/mcp/23f1de5368d22b7597d563f761f1dcab"

def fetch(path):
    r = subprocess.run([YQ, CODE, "download", path], capture_output=True, text=True, timeout=90)
    out = r.stdout.strip()
    if not out.startswith("FILE:"):
        return None
    _, size_str, b64 = out.split("|", 2)
    return base64.b64decode(b64).decode("utf-8", "replace")

checks = {
    "ui.html": [
        ("conn 计数修复", "var conn = num(a.connections, 0);"),
        ("忙碌区分", "var busy = (running + pending) > 0;"),
        ("8s 轮询兜底", "}, 8000);"),
        ("暗灯调亮", "box-shadow:inset 0 0 0 1px #4a4a4a"),
        ("title 更新", "Agent 并发"),
    ],
    "agent.py": [
        ("activity 缓存字段", "self._activity = {}"),
        ("payload 缓存", "self._activity = payload"),
        ("get_activity 方法", "def get_activity(self):"),
    ],
    "desktop.py": [
        ("get_status 返回 activity", '"activity": activity'),
        ("保底 activity", '"connections": 0, "runningTasks": 0, "pendingCalls": 0, "maxConnections": 50'),
    ],
}

ok = True
for fname, items in checks.items():
    content = fetch(rf"D:\AICODE\yunqiao\client\{fname}")
    if content is None:
        print(f"[{fname}] FETCH FAILED")
        ok = False
        continue
    print(f"===== {fname} ({len(content.encode('utf-8'))}B) =====")
    for label, needle in items:
        found = needle in content
        if not found:
            ok = False
        print(f"  {'✅' if found else '❌'} {label}")

print("\nRESULT:", "ALL PASS" if ok else "SOME FAILED")