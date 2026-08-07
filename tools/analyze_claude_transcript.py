#!/usr/bin/env python3
"""读取 Windows 上 Claude 会话 transcript 的 user 记录"""
import subprocess, os, json, base64

YQ = "/home/admin/.openclaw/workspace/skills/yunqiao/scripts/yq"
CODE = "598581"
os.environ["YUNQIAO_URL"] = "https://yunqiao.very.im/mcp/23f1de5368d22b7597d563f761f1dcab"

# 写一个临时分析脚本到 Windows 工作区（super 模式可写）
script = r'''
import glob, os, json, sys
base = os.path.expanduser("~/.claude/projects")
# 找最新的 2026-08-06 会话
import time
best = None
for d in glob.glob(base + "/*chat-sessions-2026-08-06*"):
    for f in glob.glob(d + "/*.jsonl"):
        mtime = os.path.getmtime(f)
        if best is None or mtime > best[0]:
            best = (mtime, f)
if best is None:
    print("NO_SESSION"); sys.exit(0)
path = best[1]
print("FILE:", path)
lines = open(path, encoding="utf-8").readlines()
print("LINES:", len(lines))
for l in lines:
    try:
        j = json.loads(l)
    except Exception:
        continue
    t = j.get("type", "?")
    if t == "user":
        content = j.get("message", {}).get("content", "")
        if isinstance(content, list):
            c = json.dumps(content, ensure_ascii=False)[:150]
        else:
            c = str(content)[:150]
        print("USER:", c)
'''

# 先写到 worker（super 模式下应该能直接写绝对路径）
remote_script = r"D:\AICODE\veryAgent\_tmp_analyze.py"
args = json.dumps({"path": remote_script, "content": script})
r = subprocess.run([YQ, CODE, "call", "write_file", args], capture_output=True, text=True, timeout=60)
print("[write]", r.stdout.strip()[:80], r.stderr.strip()[:80])

# 执行（当前 cwd 是 veryAgent）
r = subprocess.run([YQ, CODE, "exec", "python _tmp_analyze.py"], capture_output=True, text=True, timeout=60)
print("[run]", r.stdout.strip()[:2000])
print("[err]", r.stderr.strip()[:300])
