#!/usr/bin/env python3
"""统计中继服务器当前 admin 用户的 SSE/MCP 连接数 + 设备连接"""
import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("45.152.65.49", port=58962, username="root", password="2);[GN?oO8i,", timeout=25)

def run(cmd):
    _, stdout, _ = client.exec_command(cmd, timeout=30)
    return stdout.read().decode("utf-8", "replace").strip()

print("=== 进程启动时间（确认是否重启过） ===")
print(run("ps -o lstart= -p $(pgrep -f 'node server.js' | head -1)"))

print("\n=== 中继进程打开的 TCP 连接数 ===")
print(run("ls /proc/$(pgrep -f 'node server.js' | head -1)/fd 2>/dev/null | wc -l; echo '--- ESTABLISHED ---'; ss -tnp 2>/dev/null | grep node | wc -l"))

print("\n=== node 进程的 ESTABLISHED 连接（看几条是 MCP/SSE） ===")
print(run("ss -tnp 2>/dev/null | grep node | head -20"))

print("\n=== 最近连接/清理日志 ===")
print(run("tail -20 /opt/cloud-mcp/relay.log | grep -E 'sse|connect|register|disconnect'"))
client.close()