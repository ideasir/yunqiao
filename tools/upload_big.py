#!/usr/bin/env python3
"""分块上传大文件到 Windows（避免命令行长度限制）"""
import subprocess, os, json, base64, sys

YQ = "/home/admin/.openclaw/workspace/skills/yunqiao/scripts/yq"
CODE = "598581"
os.environ["YUNQIAO_URL"] = "https://yunqiao.very.im/mcp/23f1de5368d22b7597d563f761f1dcab"
WORKDIR = r"D:\AICODE\veryAgent"

def run_yq(args_list, timeout=120):
    return subprocess.run([YQ, CODE] + args_list, capture_output=True, text=True, timeout=timeout)

def crlf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")

src = "/tmp/veryagent-src/src-tauri/src/commands/acp/mod.rs"
with open(src, "rb") as f:
    content = crlf(f.read())
print("total bytes:", len(content))

# base64 编码，分块（每块 30KB base64 文本）
b64 = base64.b64encode(content).decode("ascii")
CHUNK = 30000
chunks = [b64[i:i+CHUNK] for i in range(0, len(b64), CHUNK)]
print("chunks:", len(chunks))

# 1. 写一个 python 拼接脚本到 Windows，把各块拼起来
join_script = r'''
import base64, os
cwd = os.getcwd()
parts = []
i = 0
while True:
    fn = os.path.join(cwd, f"_chunk_{i}.txt")
    if not os.path.exists(fn):
        break
    with open(fn, encoding="ascii") as f:
        parts.append(f.read().strip())
    i += 1
data = base64.b64decode("".join(parts))
with open(os.path.join(cwd, "src-tauri", "src", "commands", "acp", "mod.rs"), "wb") as f:
    f.write(data)
print("JOINED_OK", len(data))
'''
r = run_yq(["call", "write_file", json.dumps({"path": WORKDIR + r"\zz_join.py", "content": join_script})], 60)
print("[join script]", r.stdout.strip()[:80], r.stderr.strip()[:80])

# 2. 逐块写入
for i, chunk in enumerate(chunks):
    r = run_yq(["call", "write_file", json.dumps({"path": WORKDIR + f"\\_chunk_{i}.txt", "content": chunk})], 120)
    if r.returncode != 0 or "写入" not in r.stdout:
        print(f"[chunk {i} FAIL]", r.stdout.strip()[:80], r.stderr.strip()[:80])
        sys.exit(1)
    if i % 2 == 0:
        print(f"[chunk {i}/{len(chunks)}] ok")

# 3. 执行拼接
r = run_yq(["exec", "python zz_join.py"], 60)
print("[join]", r.stdout.strip()[:80], r.stderr.strip()[:80])
print("DONE")