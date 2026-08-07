#!/usr/bin/env python3
"""下载 Windows 上的三个文件，解码保存到本地，供 diff 对比"""
import subprocess, base64, os, sys

YQ = "/home/admin/.openclaw/workspace/skills/yunqiao/scripts/yq"
CODE = "419364"
os.environ["YUNQIAO_URL"] = "https://yunqiao.very.im/mcp/23f1de5368d22b7597d563f761f1dcab"

OUT = "/tmp/yunqiao-sync"
os.makedirs(OUT, exist_ok=True)

files = {
    "ui.html": r"D:\AICODE\yunqiao\client\ui.html",
    "agent.py": r"D:\AICODE\yunqiao\client\agent.py",
    "desktop.py": r"D:\AICODE\yunqiao\client\desktop.py",
}

for name, path in files.items():
    r = subprocess.run([YQ, CODE, "download", path], capture_output=True, text=True, timeout=90)
    out = r.stdout.strip()
    if not out.startswith("FILE:"):
        print(f"[{name}] FAIL: {out[:200]}")
        continue
    # FILE:path|size|base64
    _, _, b64 = out.partition("|")
    _, size_str, b64 = out.split("|", 2)
    try:
        data = base64.b64decode(b64)
    except Exception as e:
        print(f"[{name}] base64 decode fail: {e}")
        continue
    dest = os.path.join(OUT, name)
    with open(dest, "wb") as f:
        f.write(data)
    print(f"[{name}] saved {len(data)} bytes -> {dest}")

print("DONE")