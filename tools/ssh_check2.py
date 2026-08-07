#!/usr/bin/env python3
"""对比线上 server.js 与本地仓库是否一致，并检查活跃灯逻辑"""
import paramiko

HOST = "45.152.65.49"; PORT = 58962; USER = "root"; PASS = "2);[GN?oO8i,"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, port=PORT, username=USER, password=PASS, timeout=25)

def run(cmd):
    _, stdout, _ = client.exec_command(cmd, timeout=30)
    return stdout.read().decode("utf-8", "replace")

# 线上 server.js 的 md5 和含活跃灯的检查
print("=== 线上 server.js md5 ===")
print(run("md5sum /opt/cloud-mcp/relay/server.js"))
print("=== 线上是否含活跃灯逻辑 ===")
print(run("grep -c 'scheduleActivityPush\\|getActivity\\|agent_activity' /opt/cloud-mcp/relay/server.js"))
print("=== 线上 server.js 里 getActivity 定义 ===")
print(run("grep -n 'function getActivity\\|function broadcastToDevices\\|function scheduleActivityPush' /opt/cloud-mcp/relay/server.js"))
print("=== 线上 relay.log 尾部 ===")
print(run("tail -25 /opt/cloud-mcp/relay.log"))
client.close()
print("DONE")