#!/usr/bin/env python3
"""将修复后的文件转 CRLF 并通过云桥 write_file 工具写回 Windows"""
import subprocess, os, sys, json, base64

YQ = "/home/admin/.openclaw/workspace/skills/yunqiao/scripts/yq"
CODE = "907401"
os.environ["YUNQIAO_URL"] = "https://yunqiao.very.im/mcp/23f1de5368d22b7597d563f761f1dcab"
os.environ["YUNQIAO_KEY"] = "yunqiao-mcp-key-2026"
REPO = "/home/admin/.openclaw/workspace/yunqiao/client"

# 修复后的文件：(本地路径, Windows 目标路径, 是否二进制)
files = [
    ("ui.html", r"D:\AICODE\yunqiao\client\ui.html"),
    ("agent.py", r"D:\AICODE\yunqiao\client\agent.py"),
    ("desktop.py", r"D:\AICODE\yunqiao\client\desktop.py"),
]

def crlf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")

for name, winpath in files:
    src = os.path.join(REPO, name)
    with open(src, "rb") as f:
        content_b = crlf(f.read())
    content = content_b.decode("utf-8")
    # 用 write_file 工具直接写（json 参数）
    args = json.dumps({"path": winpath, "content": content})
    r = subprocess.run([YQ, CODE, "call", "write_file", args],
                       capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip()
    print(f"[{name}] -> {winpath} ({len(content_b)}B)")
    print("   ", out[:300])
    print()

print("DONE")