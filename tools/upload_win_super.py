#!/usr/bin/env python3
"""同步 ui.html + desktop.py 到 Windows（超级模式，直接写绝对路径）"""
import subprocess, os, json

YQ = "/home/admin/.openclaw/workspace/skills/yunqiao/scripts/yq"
CODE = "845235"
os.environ["YUNQIAO_URL"] = "https://yunqiao.very.im/mcp/23f1de5368d22b7597d563f761f1dcab"
REPO = "/home/admin/.openclaw/workspace/yunqiao/client"

def crlf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")

files = ["ui.html", "desktop.py"]
for name in files:
    with open(os.path.join(REPO, name), "rb") as f:
        content = crlf(f.read()).decode("utf-8")
    args = json.dumps({"path": rf"D:\AICODE\yunqiao\client\{name}", "content": content})
    r = subprocess.run([YQ, CODE, "call", "write_file", args], capture_output=True, text=True, timeout=120)
    print(f"[{name}]", r.stdout.strip()[:100], r.stderr.strip()[:100])

print("DONE")