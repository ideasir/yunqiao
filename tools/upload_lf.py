#!/usr/bin/env python3
"""用 LF 行尾重新上传三个文件（修复 \r\r\n 问题）"""
import subprocess, os, json, base64

YQ = "/home/admin/.openclaw/workspace/skills/yunqiao/scripts/yq"
CODE = "598581"
os.environ["YUNQIAO_URL"] = "https://yunqiao.very.im/mcp/23f1de5368d22b7597d563f761f1dcab"
WORKDIR = r"D:\AICODE\veryAgent"

def run_yq(args_list, timeout=120):
    return subprocess.run([YQ, CODE] + args_list, capture_output=True, text=True, timeout=timeout)

def to_lf(data: bytes) -> bytes:
    # 先把所有 CRLF/CR 归一成 LF，再确保是纯 LF
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

# 三个文件：本地路径 -> (临时块前缀, 目标相对路径)
files = [
    ("src-tauri/src/acp/provider_proxy.rs", "pp", "src-tauri/src/acp/provider_proxy.rs"),
    ("src-tauri/src/commands/acp/mod.rs", "m", "src-tauri/src/commands/acp/mod.rs"),
    ("src-tauri/Cargo.toml", "ct", "src-tauri/Cargo.toml"),
]

SRC = "/tmp/veryagent-src"

for src_rel, prefix, dest_rel in files:
    with open(os.path.join(SRC, src_rel), "rb") as f:
        content = to_lf(f.read())
    b64 = base64.b64encode(content).decode("ascii")
    CHUNK = 28000
    chunks = [b64[i:i+CHUNK] for i in range(0, len(b64), CHUNK)]
    print(f"[{dest_rel}] {len(content)} bytes, {len(chunks)} chunks")

    # join 脚本（用二进制写，行尾保持 LF）
    join_script = f'''import base64, os
cwd = os.getcwd()
parts = []
i = 0
while True:
    fn = os.path.join(cwd, f"_c_%%PREFIX%%_{i}.txt")
    if not os.path.exists(fn):
        break
    with open(fn, encoding="ascii") as f:
        parts.append(f.read().strip())
    i += 1
data = base64.b64decode("".join(parts))
with open(os.path.join(cwd, r"{dest_rel}".replace("/", os.sep)), "wb") as f:
    f.write(data)
print("OK", len(data))
'''
    r = run_yq(["call", "write_file", json.dumps({"path": WORKDIR + f"\\_j_{prefix}.py", "content": join_script})], 60)
    assert r.returncode == 0, r.stderr
    
    for i, chunk in enumerate(chunks):
        r = run_yq(["call", "write_file", json.dumps({"path": WORKDIR + f"\\_c_{prefix}_{i}.txt", "content": chunk})], 120)
        assert r.returncode == 0 and "写入" in r.stdout, r.stdout
    
    r = run_yq(["exec", f"python _j_{prefix}.py"], 60)
    print(f"[{dest_rel}]", r.stdout.strip()[:60], r.stderr.strip()[:80])

print("DONE")