#!/usr/bin/env python3
"""检查中继服务器当前设备连接状态和配对码"""
import paramiko

HOST = "45.152.65.49"; PORT = 58962; USER = "root"; PASS = "2);[GN?oO8i,"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, port=PORT, username=USER, password=PASS, timeout=25)

def run(cmd):
    _, stdout, stderr = client.exec_command(cmd, timeout=30)
    return stdout.read().decode("utf-8", "replace").strip()

print("=== 最新设备日志 ===")
print(run("tail -15 /opt/cloud-mcp/relay.log"))
print("\n=== users.json ===")
print(run("cat /opt/cloud-mcp/.users.json"))
client.close()