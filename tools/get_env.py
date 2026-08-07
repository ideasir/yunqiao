#!/usr/bin/env python3
"""读取中继进程的真实环境变量（RELAY_KEY）"""
import paramiko

HOST = "45.152.65.49"; PORT = 58962; USER = "root"; PASS = "2);[GN?oO8i,"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, port=PORT, username=USER, password=PASS, timeout=25)

def run(cmd):
    _, stdout, stderr = client.exec_command(cmd, timeout=30)
    return stdout.read().decode("utf-8", "replace").strip(), stderr.read().decode("utf-8", "replace").strip()

# 从 /proc/<pid>/environ 读环境变量
out, err = run("tr '\\0' '\\n' < /proc/293295/environ 2>/dev/null | grep -iE 'RELAY_KEY|USERS_FILE|PORT|AUTH' ; echo '---users.json---'; cat /opt/cloud-mcp/.users.json")
print(out)
print("ERR:", err)
client.close()