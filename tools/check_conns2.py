#!/usr/bin/env python3
"""模拟 getActivity 统计当前 admin 的活跃连接数（getActivity 逻辑）"""
import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("45.152.65.49", port=58962, username="root", password="2);[GN?oO8i,", timeout=25)

def run(cmd):
    _, stdout, _ = client.exec_command(cmd, timeout=30)
    return stdout.read().decode("utf-8", "replace").strip()

# node 进程当前 ESTABLISHED 连接数（MCP/SSE 连接）
print("=== node ESTABLISHED 连接数 ===")
print(run("ss -tnp | grep 'node' | grep ESTAB | wc -l"))
print("\n=== 各连接最近活动（看哪些是活跃的） ===")
print(run("ss -tnp | grep 'node' | grep ESTAB | wc -l; echo '--- 设备连接 ---'; ss -tnp | grep 'node' | grep ESTAB | awk '{print $5}' | sort | uniq -c | sort -rn | head"))
print("\n=== 最近日志 ===")
print(run("tail -8 /opt/cloud-mcp/relay.log"))
client.close()