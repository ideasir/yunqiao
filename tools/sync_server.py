#!/usr/bin/env python3
"""同步修复后的 server.js 到中继服务器，用 systemd 重启"""
import paramiko

HOST = "45.152.65.49"; PORT = 58962; USER = "root"; PASS = "2);[GN?oO8i,"
LOCAL = "/home/admin/.openclaw/workspace/yunqiao/relay/server.js"
REMOTE = "/opt/cloud-mcp/relay/server.js"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, port=PORT, username=USER, password=PASS, timeout=25)

def run(cmd, timeout=60):
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    return stdout.read().decode("utf-8", "replace").strip(), stderr.read().decode("utf-8", "replace").strip()

# 1. 备份
out, err = run(f"cp {REMOTE} {REMOTE}.bak-leakfix-$(date +%s) && echo BACKUP_OK")
print("备份:", out or err)

# 2. 上传
sftp = client.open_sftp()
sftp.put(LOCAL, REMOTE)
sftp.close()
print("上传完成")

# 3. md5 校验
out, _ = run(f"md5sum {REMOTE}")
print("线上 md5:", out)

# 4. 语法检查
out, err = run(f"node --check {REMOTE} && echo SYNTAX_OK", timeout=30)
print("语法:", out or err)

# 5. systemd 重启
out, err = run("systemctl restart yunqiao-relay && sleep 2 && systemctl is-active yunqiao-relay", timeout=60)
print("重启状态:", out or err)

# 6. 确认新进程
out, _ = run("ps aux | grep 'node server.js' | grep -v grep")
print("新进程:", out)

client.close()
print("DONE")