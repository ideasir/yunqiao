#!/usr/bin/env python3
"""SSH 到云桥中继服务器检查 server.js 版本与运行状态"""
import paramiko
import sys

HOST = "45.152.65.49"
PORT = 58962
USER = "root"
PASS = "2);[GN?oO8i,"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, port=PORT, username=USER, password=PASS, timeout=25)

def run(cmd, label=None):
    print(f"\n===== {label or cmd} =====")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    if out.strip():
        print(out[:4000])
    if err.strip():
        print("STDERR:", err[:2000])
    return out

run("hostname && uptime && pwd", "基础信息")
run("ps aux | grep -E 'server\\.js|relay' | grep -v grep", "运行中的中继进程")
run("ls -la /opt/cloud-mcp/relay/ 2>/dev/null; ls -la /opt/cloud-mcp/ 2>/dev/null | head", "部署目录")
run("find / -name 'server.js' -path '*relay*' 2>/dev/null | head", "定位 server.js")
run("node --version", "node 版本")

client.close()
print("\nDONE")
