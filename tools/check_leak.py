#!/usr/bin/env python3
"""检查中继服务器 SSE 连接泄漏 + MAX_CONNECTIONS 环境变量"""
import paramiko

HOST = "45.152.65.49"; PORT = 58962; USER = "root"; PASS = "2);[GN?oO8i,"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, port=PORT, username=USER, password=PASS, timeout=25)

def run(cmd):
    _, stdout, stderr = client.exec_command(cmd, timeout=30)
    return stdout.read().decode("utf-8", "replace").strip()

ev = "tr '\\0' '\\n' < /proc/293295/environ | grep -iE 'MAX_CONN|SSE_|TASK_MAX|DEFAULT_QPS'"
print("=== 环境变量 ===")
print(run(ev) or "(未设置，走默认值)")

print("\n=== users.json ===")
print(run("cat /opt/cloud-mcp/.users.json"))

print("\n=== ESTABLISHED 连接数 ===")
print(run("ss -tnp | grep -c ESTAB"))

print("\n=== 远端 IP 分布 ===")
print(run("ss -tn | grep ESTAB | awk '{print $5}' | cut -d: -f1 | sort | uniq -c | sort -rn | head -15"))

print("\n=== SSE/connect 日志 ===")
print(run("grep -E 'sse|SSE|connect|ticket' /opt/cloud-mcp/relay.log | tail -20"))

client.close()