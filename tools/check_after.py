#!/usr/bin/env python3
"""检查僵尸连接是否已清理 + 当前活动状态"""
import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("45.152.65.49", port=58962, username="root", password="2);[GN?oO8i,", timeout=25)

def run(cmd):
    _, stdout, _ = client.exec_command(cmd, timeout=30)
    return stdout.read().decode("utf-8", "replace").strip()

print("=== 当前 ESTABLISHED 连接数（重启前是 52） ===")
print(run("ss -tnp | grep -c ESTAB"))
print("\n=== 远端 IP 分布 ===")
print(run("ss -tn | grep ESTAB | awk '{print $5}' | cut -d: -f1 | sort | uniq -c | sort -rn | head -12"))
print("\n=== 最近日志 ===")
print(run("tail -12 /opt/cloud-mcp/relay.log"))
client.close()