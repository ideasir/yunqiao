#!/usr/bin/env python3
"""查看 transcript 里 [12] system 记录 + 完整 assistant 块内容"""
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
lines = open(path, encoding="utf-8").readlines()
for i, l in enumerate(lines):
    j = json.loads(l)
    t = j.get("type", "?")
    if t == "system":
        print(f"[{i}] SYSTEM full:", json.dumps(j, ensure_ascii=False)[:1500])
    elif t == "assistant":
        msg = j.get("message", {})
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if b.get("type") in ("tool_use", "tool_result"):
                    print(f"[{i}] {b.get('type')}:", json.dumps(b, ensure_ascii=False)[:800])
                elif b.get("type") == "text":
                    print(f"[{i}] TEXT:", str(b.get("text"))[:300])
                elif b.get("type") == "thinking":
                    print(f"[{i}] thinking(len={len(str(b.get('thinking','')))})")
'''

remote_script = r"D:\AICODE\veryAgent\_tmp_sys.py"
args = json.dumps({"path": remote_script, "content": script})
r = subprocess.run([YQ, CODE, "call", "write_file", args], capture_output=True, text=True, timeout=60)
r = subprocess.run([YQ, CODE, "exec", "python _tmp_sys.py"], capture_output=True, text=True, timeout=60)
print(r.stdout[:3500])
print("[err]", r.stderr.strip()[:300])
