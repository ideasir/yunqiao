#!/usr/bin/env python3
"""从中继服务器读取管理员密钥用于管理工具"""
import paramiko

HOST = "45.152.65.49"; PORT = 58962; USER = "root"; PASS = "2);[GN?oO8i,"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, port=PORT, username=USER, password=PASS, timeout=25)

def run(cmd):
    _, stdout, stderr = client.exec_command(cmd, timeout=30)
    return stdout.read().decode("utf-8", "replace").strip(), stderr.read().decode("utf-8", "replace").strip()

out, err = run("cat /opt/cloud-mcp/.key 2>/dev/null; echo '---'; cat /opt/cloud-mcp/.psk 2>/dev/null; echo '---ENV---'; grep -i 'ADMIN\\|KEY' /opt/cloud-mcp/.env 2>/dev/null; echo '---ps1---'; ps aux | grep server.js | grep -v grep")
print("OUT:", out)
print("ERR:", err)
client.close()
