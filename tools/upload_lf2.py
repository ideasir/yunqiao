#!/usr/bin/env python3
"""用 LF 行尾重新上传三个文件（修复 \\r\\r\\n 问题）- 简化版"""
import subprocess, os, json, base64

YQ = "/home/admin/.openclaw/workspace/skills/yunqiao/scripts/yq"
CODE = "598581"
os.environ["YUNQIAO_URL"] = "https://yunqiao.very.im/mcp/23f1de5368d22b7597d563f761f1dcab"
WORKDIR = r"D:\AICODE\veryAgent"
SRC = "/tmp/veryagent-src"

def run_yq(args_list, timeout=120):
    return subprocess.run([YQ, CODE] + args_list, capture_output=True, text=True, timeout=timeout)

def to_lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

files = [
    ("src-tauri/src/acp/provider_proxy.rs", "pp", "src-tauri/src/acp/provider_proxy.rs"),
    ("src-tauri/src/commands/acp/mod.rs", "m", "src-tauri/src/commands/acp/mod.rs"),
    ("src-tauri/Cargo.toml", "ct", "src-tauri/Cargo.toml"),
]

for src_rel, prefix, dest_rel in files:
    with open(os.path.join(SRC, src_rel), "rb") as f:
        content = to_lf(f.read())
    b64 = base64.b64encode(content).decode("ascii")
    CHUNK = 26000
    chunks = [b64[i:i+CHUNK] for i in range(0, len(b64), CHUNK)]
    print(f"[{dest_rel}] {len(content)} bytes, {len(chunks)} chunks")

    # 写每个文件自己的 join 脚本，文件名固定
    tmp_prefix = f"_lf_{prefix}"
    join_script = ("import base64, os\n"
                   "cwd = os.getcwd()\n"
                   "parts = []\n"
                   "i = 0\n"
                   "while True:\n"
                   f"    fn = os.path.join(cwd, '{tmp_prefix}_' + str(i) + '.txt')\n"
                   "    if not os.path.exists(fn):\n"
                   "        break\n"
                   "    with open(fn, encoding='ascii') as f:\n"
                   "        parts.append(f.read().strip())\n"
                   "    i += 1\n"
                   "data = base64.b64decode(''.join(parts))\n"
                   f"with open(os.path.join(cwd, r'{dest_rel}'.replace('/', os.sep)), 'wb') as f:\n"
                   "    f.write(data)\n"
                   "print('OK', len(data))\n")
    r = run_yq(["call", "write_file", json.dumps({"path": WORKDIR + "\\" + tmp_prefix + "_j.py", "content": join_script})], 60)
    assert r.returncode == 0, r.stderr

    for i, chunk in enumerate(chunks):
        fn = f"{tmp_prefix}_{i}.txt"
        r = run_yq(["call", "write_file", json.dumps({"path": WORKDIR + "\\" + fn, "content": chunk})], 120)
        assert r.returncode == 0 and "写入" in r.stdout, r.stdout

    r = run_yq(["exec", f"python {tmp_prefix}_j.py"], 60)
    print(f"[{dest_rel}]", r.stdout.strip()[:60], r.stderr.strip()[:80])

print("DONE")