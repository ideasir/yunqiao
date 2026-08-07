#!/usr/bin/env python3
"""确认中继进程管理方式"""
import paramiko

HOST = "45.152.65.49"; PORT = 58962; USER = "root"; PASS = "2);[GN?oO8i,"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, port=PORT, username=USER, password=PASS, timeout=25)

def run(cmd):
    _, stdout, stderr = client.exec_command(cmd, timeout=30)
    return stdout.read().decode("utf-8", "replace").strip()

print("=== systemd 服务 ===")
print(run("systemctl list-units --type=service | grep -iE 'cloud|yunqiao|relay|mcp' || echo '无匹配 systemd 服务'"))
print("\n=== 进程树（父进程） ===")
print(run("ps -ef | grep -E 'node server.js|PM2|supervisor|forever' | grep -v grep"))
print("\n=== 进程 ppid ===")
print(run("pid=$(pgrep -f 'node server.js' | head -1); echo pid=$pid; cat /proc/$pid/status 2>/dev/null | grep -E 'PPid|Name'; ppid=$(awk '/PPid/{print $2}' /proc/$pid/status 2>/dev/null); ps -p $ppid -o pid,cmd 2>/dev/null"))
print("\n=== 是否有 pm2 ===")
print(run("which pm2 && pm2 list 2>/dev/null | head -15 || echo '无 pm2'"))
client.close()