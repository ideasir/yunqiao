#!/usr/bin/env python3
"""确认线上中继 server.js 与仓库当前版本是否一致"""
import paramiko, subprocess

HOST = "45.152.65.49"; PORT = 58962; USER = "root"; PASS = "2);[GN?oO8i,"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, port=PORT, username=USER, password=PASS, timeout=25)

_, stdout, _ = client.exec_command("md5sum /opt/cloud-mcp/relay/server.js", timeout=30)
remote = stdout.read().decode().strip()

local = subprocess.run(["md5sum", "/home/admin/.openclaw/workspace/yunqiao/relay/server.js"],
                       capture_output=True, text=True).stdout.strip()

print("线上:", remote)
print("仓库:", local)
print("一致:", remote.split()[0] == local.split()[0])

# 再确认线上活跃灯逻辑完整
_, stdout, _ = client.exec_command(
    "grep -c 'scheduleActivityPush\\|getActivity\\|agent_activity' /opt/cloud-mcp/relay/server.js", timeout=30)
print("线上活跃灯逻辑匹配数:", stdout.read().decode().strip())

client.close()