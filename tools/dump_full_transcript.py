#!/usr/bin/env python3
"""拉取最新 Claude 会话完整 transcript，分析 assistant 记录"""
import subprocess, os, json, glob

YQ = "/home/admin/.openclaw/workspace/skills/yunqiao/scripts/yq"
CODE = "598581"
os.environ["YUNQIAO_URL"] = "https://yunqiao.very.im/mcp/23f1de5368d22b7597d563f761f1dcab"

script = r'''
import glob, os, json, sys
base = os.path.expanduser("~/.claude/projects")
best = None
for d in glob.glob(base + "/*chat-sessions-2026-08-06*"):
    for f in glob.glob(d + "/*.jsonl"):
        mtime = os.path.getmtime(f)
        if best is None or mtime > best[0]:
            best = (mtime, f)
path = best[1]
print("FILE:", path)
for i, l in enumerate(open(path, encoding="utf-8").readlines()):
    try:
        j = json.loads(l)
    except Exception:
        continue
    t = j.get("type", "?")
    ts = j.get("timestamp", "")
    # 打印关键字段
    print(f"[{i}] type={t} ts={ts}")
    msg = j.get("message", {})
    content = msg.get("content")
    if isinstance(content, list):
        for b in content:
            bt = b.get("type", "?")
            if bt == "text":
                print("      text:", str(b.get("text", ""))[:200])
            elif bt == "tool_use":
                print("      tool_use:", b.get("name"), "input:", json.dumps(b.get("input", {}), ensure_ascii=False)[:120])
            elif bt == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = json.dumps(c, ensure_ascii=False)[:150]
                else:
                    c = str(c)[:150]
                print("      tool_result:", c)
            else:
                print("      block:", bt, str(b)[:100])
    elif isinstance(content, str):
        print("      content:", content[:200])
'''

remote_script = r"D:\AICODE\veryAgent\_tmp_full.py"
args = json.dumps({"path": remote_script, "content": script})
r = subprocess.run([YQ, CODE, "call", "write_file", args], capture_output=True, text=True, timeout=60)
r = subprocess.run([YQ, CODE, "exec", "python _tmp_full.py"], capture_output=True, text=True, timeout=60)
print(r.stdout[:4000])
print("[err]", r.stderr.strip()[:300])
