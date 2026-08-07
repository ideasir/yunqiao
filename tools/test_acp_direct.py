#!/usr/bin/env python3
"""直接测试 claude-agent-acp 单独跑（绕过 veryAgent），发一条用户消息看 Claude 是否返回文本"""
import subprocess, os, json, time

YQ = "/home/admin/.openclaw/workspace/skills/yunqiao/scripts/yq"
CODE = "598581"
os.environ["YUNQIAO_URL"] = "https://yunqiao.very.im/mcp/23f1de5368d22b7597d563f761f1dcab"

# 用 node 写一个最小 ACP 客户端测试（走 stdio JSON-RPC）
test_js = r"""
const { spawn } = require('child_process');
const path = require('path');

// 启动 claude-agent-acp
const proc = spawn('cmd', ['/c', 'C:\\Users\\EVAN\\AppData\\Roaming\\npm\\claude-agent-acp.cmd'], {
  stdio: ['pipe', 'pipe', 'pipe'],
  shell: false,
  env: { ...process.env, ANTHROPIC_BASE_URL: 'http://10.10.100.10:18080' }
});

let buf = '';
const results = [];
let initialized = false;
let turnStarted = false;
let timeout = setTimeout(() => { console.log('TIMEOUT'); proc.kill(); process.exit(0); }, 60000);

proc.stderr.on('data', d => { /* ignore */ });

proc.stdout.on('data', d => {
  buf += d.toString();
  let idx;
  while ((idx = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, idx).trim();
    buf = buf.slice(idx + 1);
    if (!line) continue;
    let msg; try { msg = JSON.parse(line); } catch { continue; }
    if (msg.method === 'initialize') {
      // 响应 initialize
      proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: msg.id, result: {
        protocolVersion: '0.1.0',
        capabilities: { session: {}, tools: {} },
        agent: { name: 'test', version: '0.0.1' }
      }}) + '\n');
      initialized = true;
    }
    if (msg.method === 'notifications/initialized') {
      // 发一个 prompt
      const prompt = { jsonrpc: '2.0', id: 100, method: 'session/prompt', params: {
        prompt: { content: [{ type: 'text', text: '你好，请回复：测试成功' }] }
      }};
      proc.stdin.write(JSON.stringify(prompt) + '\n');
      turnStarted = true;
    }
    // 收集响应
    if (msg.id === 100) {
      results.push(msg);
      if (msg.result && msg.result.stop_reason) {
        console.log('=== TURN COMPLETE ===');
        console.log(JSON.stringify(msg.result, null, 2).slice(0, 2000));
        clearTimeout(timeout); proc.kill(); process.exit(0);
      }
    }
    if (msg.method === 'session/update') {
      const u = msg.params && msg.params.update;
      if (u) {
        const t = u.type;
        const content = u.content || u.message?.content;
        console.log('UPDATE:', t, JSON.stringify(content||'').slice(0, 200));
      }
    }
  }
});

// 发送 initialize
proc.stdin.write(JSON.stringify({
  jsonrpc: '2.0', id: 1, method: 'initialize',
  params: { protocolVersion: '0.1.0', clientCapabilities: {}, environment: { cwd: './' } }
}) + '\n');
proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
"""

# 写到 veryAgent 目录
remote = r"D:\AICODE\veryAgent\_tmp_acp_test.js"
args = json.dumps({"path": remote, "content": test_js})
subprocess.run([YQ, CODE, "call", "write_file", args], capture_output=True, text=True, timeout=60)

r = subprocess.run([YQ, CODE, "exec", 'cd /d D:\\AICODE\\veryAgent && node _tmp_acp_test.js'], capture_output=True, text=True, timeout=90)
print(r.stdout[:3000])
print("[err]", r.stderr.strip()[:500])