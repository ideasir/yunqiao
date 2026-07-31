import { createServer } from 'node:http';
import { WebSocketServer, WebSocket } from 'ws';
import { randomUUID, randomBytes } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { SSEServerTransport } from '@modelcontextprotocol/sdk/server/sse.js';
import { z } from 'zod/v4';

const PORT = parseInt(process.env.PORT || '9876');
const PSK_FILE = process.env.PSK_FILE || '/opt/cloud-mcp/.psk';
const ALLOWED_DEVICES = (process.env.ALLOWED_DEVICES || '').split(',').filter(Boolean);
const COMMAND_TIMEOUT = parseInt(process.env.COMMAND_TIMEOUT || '60000');
const MCP_PATH = '/mcp';
const MCP_MESSAGE_PATH = '/mcp/message';

// 自动管理 PSK
let PSK = '';
if (existsSync(PSK_FILE)) {
  PSK = readFileSync(PSK_FILE, 'utf-8').trim();
  console.error(`[server] 🔑 PSK 已从 ${PSK_FILE} 读取`);
} else {
  PSK = randomBytes(32).toString('hex');
  mkdirSync(dirname(PSK_FILE), { recursive: true });
  writeFileSync(PSK_FILE, PSK, 'utf-8');
  console.error(`[server] 🔑 新 PSK 已生成并保存到 ${PSK_FILE}`);
  console.error(`[server] 📋 PSK: ${PSK}`);
}
if (process.env.RELAY_PSK) {
  PSK = process.env.RELAY_PSK;
  console.error('[server] 🔑 使用环境变量 RELAY_PSK 覆盖');
}

const ALLOWED_COMMANDS = (process.env.ALLOWED_COMMANDS || '').split(',').filter(Boolean);
const ALLOWED_FILE_PREFIX = process.env.ALLOWED_FILE_PREFIX || '';

const devices = new Map();
const pendingRequests = new Map();
const transports = new Map();
const agentMessages = [];  // 客户端发往智能体的消息队列

function sendJSON(ws, data) {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(data));
  }
}

function sendAndWait(type, payload, deviceId) {
  return new Promise((resolve, reject) => {
    const device = devices.get(deviceId);
    if (!device) {
      reject(new Error(`device '${deviceId}' not found`));
      return;
    }
    const requestId = randomUUID();
    const timer = setTimeout(() => {
      pendingRequests.delete(requestId);
      reject(new Error(`request timed out after ${COMMAND_TIMEOUT}ms`));
    }, COMMAND_TIMEOUT);
    pendingRequests.set(requestId, { deviceId, resolve, reject, timer });
    sendJSON(device.ws, { type, requestId, payload });
  });
}

function rejectDeviceRequests(deviceId, reason) {
  for (const [reqId, entry] of pendingRequests) {
    if (entry.deviceId === deviceId) {
      clearTimeout(entry.timer);
      pendingRequests.delete(reqId);
      entry.reject(new Error(reason));
    }
  }
}

// 获取默认设备（优先选有配对码的，否则选第一个）
function getDefaultDevice() {
  if (devices.size === 0) return null;
  // 优先选有 authCode 的设备（agent.py 可能没有，main.py 有）
  for (const device of devices.values()) {
    if (device.authCode) return device;
  }
  return devices.values().next().value;
}

function checkCommandAllowed(command) {
  if (ALLOWED_COMMANDS.length === 0) return true;
  const cmdName = command.trim().split(/\s+/)[0];
  return ALLOWED_COMMANDS.some(allowed =>
    cmdName === allowed || cmdName.startsWith(allowed + '.') || cmdName.startsWith(allowed + '\\')
  );
}

function checkPathAllowed(filePath) {
  if (!ALLOWED_FILE_PREFIX) return true;
  return filePath.startsWith(ALLOWED_FILE_PREFIX.replace(/\\/g, '/').replace(/\/$/, '') + '/')
    || filePath.startsWith(ALLOWED_FILE_PREFIX.replace(/\\/g, '\\').replace(/\\$/, '') + '\\');
}

// ─── 会话管理工具 ─────────────────────────────

async function handleSessionOp(op, payload, deviceId) {
  return await sendAndWait('session_op', { op, ...payload }, deviceId);
}

// ─── MCP 服务 ────────────────────────────────

function createMcpServer() {
  const server = new McpServer({
    name: 'cloud-collaborative-mcp',
    version: '2.0.0',
  });

  // ═══ 旧工具（保留兼容） ═══════════════════

  server.registerTool('list_devices', {
    description: '列出所有已连接到中转的私人电脑设备',
    inputSchema: z.object({}),
  }, async () => {
    const list = Array.from(devices.values()).map(d => ({
      id: d.id, name: d.name, os: d.os, arch: d.arch,
      hostname: d.hostname, connectedAt: d.connectedAt,
      verified: !!d.authCode,
    }));
    if (list.length === 0) {
      return { content: [{ type: 'text', text: '当前没有已连接的设备' }] };
    }
    const text = list.map(d =>
      `- ${d.name} (${d.id})\n  OS: ${d.os} ${d.arch}\n  Hostname: ${d.hostname}\n  Connected: ${d.connectedAt}\n  Verified: ${d.verified ? '✅' : '❌'}`
    ).join('\n\n');
    return { content: [{ type: 'text', text }] };
  });

  // ═══ 会话管理工具（新） ═══════════════════

  server.registerTool('create_session', {
    description: '在远程电脑上创建一个新的工作会话，并设为当前默认会话',
    inputSchema: z.object({
      workDir: z.string().describe('工作目录（绝对路径）'),
      name: z.string().optional().describe('会话名称，不传则自动生成'),
      code: z.string().describe('客户端显示的验证码'),
    }),
  }, async ({ workDir, name, code }) => {
    const device = getDefaultDevice();
    if (!device) return { content: [{ type: 'text', text: 'Error: 没有已连接的设备' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    }
    const result = await handleSessionOp('create', { workDir, name }, device.id);
    const session = result.payload;
    return { content: [{ type: 'text', text: `会话已创建: ${session.name} (${session.id})\n工作目录: ${session.workDir}\n当前路径: ${session.cwd}` }] };
  });

  server.registerTool('exec', {
    description: '在当前默认会话中执行 shell 命令，保持工作目录和环境变量',
    inputSchema: z.object({
      command: z.string().describe('要执行的命令'),
      timeout: z.number().optional().describe('超时时间（毫秒），默认 30000'),
      code: z.string().describe('客户端显示的验证码'),
    }),
  }, async ({ command, timeout, code }) => {
    const device = getDefaultDevice();
    if (!device) return { content: [{ type: 'text', text: 'Error: 没有已连接的设备' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    }
    if (!checkCommandAllowed(command)) {
      return { content: [{ type: 'text', text: `Error: command '${command.split(/\s+/)[0]}' is not allowed` }], isError: true };
    }
    const result = await handleSessionOp('exec', { command, timeout }, device.id);
    const o = result.payload;
    const text = [
      `Exit Code: ${o.exitCode}`,
      o.stdout ? `\nSTDOUT:\n${o.stdout}` : '',
      o.stderr ? `\nSTDERR:\n${o.stderr}` : '',
      o.killed ? '\n[Process was killed due to timeout]' : '',
    ].join('');
    return { content: [{ type: 'text', text }] };
  });

  server.registerTool('read_file', {
    description: '在当前默认会话中读取文件（支持相对路径，基于会话工作目录）',
    inputSchema: z.object({
      path: z.string().describe('文件路径（相对路径基于会话 cwd）'),
      code: z.string().describe('客户端显示的验证码'),
    }),
  }, async ({ path, code }) => {
    const device = getDefaultDevice();
    if (!device) return { content: [{ type: 'text', text: 'Error: 没有已连接的设备' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    }
    const result = await handleSessionOp('read_file', { path }, device.id);
    const o = result.payload;
    if (o.success) {
      return { content: [{ type: 'text', text: o.content }] };
    }
    return { content: [{ type: 'text', text: `Error: ${o.error}` }], isError: true };
  });

  server.registerTool('write_file', {
    description: '在当前默认会话中写入文件（支持相对路径，基于会话工作目录）',
    inputSchema: z.object({
      path: z.string().describe('文件路径（相对路径基于会话 cwd）'),
      content: z.string().describe('要写入的文件内容'),
      code: z.string().describe('客户端显示的验证码'),
    }),
  }, async ({ path, content, code }) => {
    const device = getDefaultDevice();
    if (!device) return { content: [{ type: 'text', text: 'Error: 没有已连接的设备' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    }
    const result = await handleSessionOp('write_file', { path, content }, device.id);
    const o = result.payload;
    if (o.success) {
      return { content: [{ type: 'text', text: `文件已写入: ${o.path}` }] };
    }
    return { content: [{ type: 'text', text: `Error: ${o.error}` }], isError: true };
  });

  server.registerTool('close_session', {
    description: '关闭当前默认会话，清理持久进程和缓存',
    inputSchema: z.object({
      code: z.string().describe('客户端显示的验证码'),
    }),
  }, async ({ code }) => {
    const device = getDefaultDevice();
    if (!device) return { content: [{ type: 'text', text: 'Error: 没有已连接的设备' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    }
    const result = await handleSessionOp('close', {}, device.id);
    const o = result.payload;
    if (o.success) {
      return { content: [{ type: 'text', text: `会话已关闭` }] };
    }
    return { content: [{ type: 'text', text: `Error: ${o.error}` }], isError: true };
  });

  server.registerTool('list_sessions', {
    description: '列出远程电脑上所有已创建的工作会话',
    inputSchema: z.object({
      code: z.string().describe('客户端显示的验证码'),
    }),
  }, async ({ code }) => {
    const device = getDefaultDevice();
    if (!device) return { content: [{ type: 'text', text: 'Error: 没有已连接的设备' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    }
    const result = await handleSessionOp('list', {}, device.id);
    const sessions = result.payload.sessions || [];
    if (sessions.length === 0) {
      return { content: [{ type: 'text', text: '当前没有会话。使用 create_session 创建新会话。' }] };
    }
    const text = sessions.map(s =>
      `${s.isDefault ? '👉' : '  '} ${s.name} (${s.id})\n` +
      `    工作目录: ${s.workDir}\n` +
      `    当前路径: ${s.cwd}\n` +
      `    活跃: ${s.alive ? '✅' : '❌'}\n` +
      `    最后活动: ${new Date(s.lastActive * 1000).toLocaleString()}`
    ).join('\n\n');
    return { content: [{ type: 'text', text }] };
  });

  server.registerTool('switch_session', {
    description: '切换到指定的会话，后续操作将在该会话中执行',
    inputSchema: z.object({
      sessionId: z.string().describe('目标会话 ID'),
      code: z.string().describe('客户端显示的验证码'),
    }),
  }, async ({ sessionId, code }) => {
    const device = getDefaultDevice();
    if (!device) return { content: [{ type: 'text', text: 'Error: 没有已连接的设备' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    }
    const result = await handleSessionOp('switch', { sessionId }, device.id);
    const o = result.payload;
    if (o.success) {
      return { content: [{ type: 'text', text: `已切换到会话: ${o.name} (${o.sessionId})\n工作目录: ${o.workDir}` }] };
    }
    return { content: [{ type: 'text', text: `Error: ${o.error}` }], isError: true };
  });

  // ═══ 保留旧工具（兼容旧版智能体） ═════════

  server.registerTool('execute_command', {
    description: '[旧版] 在指定的私人电脑上执行 shell 命令（建议使用 exec + 会话管理）',
    inputSchema: z.object({
      deviceId: z.string().optional().describe('目标设备 ID（不传则自动选择）'),
      code: z.string().describe('客户端显示的验证码'),
      command: z.string().describe('要执行的 shell 命令'),
      timeout: z.number().optional().describe('超时时间（毫秒），默认 30000'),
    }),
  }, async ({ deviceId, code, command, timeout }) => {
    const device = deviceId ? devices.get(deviceId) : getDefaultDevice();
    if (!device) return { content: [{ type: 'text', text: 'Error: device not found' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    }
    if (!checkCommandAllowed(command)) {
      return { content: [{ type: 'text', text: `Error: command not allowed` }], isError: true };
    }
    const output = await sendAndWait('execute_command', { command, timeout }, device.id);
    const o = output.payload;
    const text = [
      `Exit Code: ${o.exitCode}`,
      o.stdout ? `\nSTDOUT:\n${o.stdout}` : '',
      o.stderr ? `\nSTDERR:\n${o.stderr}` : '',
      o.killed ? '\n[Process was killed due to timeout]' : '',
    ].join('');
    return { content: [{ type: 'text', text }] };
  });

  server.registerTool('read_file_old', {
    description: '[旧版] 读取私人电脑上的文件（建议使用 read_file + 会话管理）',
    inputSchema: z.object({
      deviceId: z.string().optional().describe('目标设备 ID（不传则自动选择）'),
      code: z.string().describe('客户端显示的验证码'),
      path: z.string().describe('文件绝对路径'),
    }),
  }, async ({ deviceId, code, path }) => {
    const device = deviceId ? devices.get(deviceId) : getDefaultDevice();
    if (!device) return { content: [{ type: 'text', text: 'Error: device not found' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    }
    if (!checkPathAllowed(path)) {
      return { content: [{ type: 'text', text: 'Error: path outside allowed prefix' }], isError: true };
    }
    const output = await sendAndWait('read_file', { path }, device.id);
    const o = output.payload;
    if (o.success) return { content: [{ type: 'text', text: o.content }] };
    return { content: [{ type: 'text', text: `Error: ${o.error}` }], isError: true };
  });

  server.registerTool('write_file_old', {
    description: '[旧版] 写入私人电脑上的文件（建议使用 write_file + 会话管理）',
    inputSchema: z.object({
      deviceId: z.string().optional().describe('目标设备 ID（不传则自动选择）'),
      code: z.string().describe('客户端显示的验证码'),
      path: z.string().describe('文件绝对路径'),
      content: z.string().describe('要写入的文件内容'),
    }),
  }, async ({ deviceId, code, path, content }) => {
    const device = deviceId ? devices.get(deviceId) : getDefaultDevice();
    if (!device) return { content: [{ type: 'text', text: 'Error: device not found' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    }
    if (!checkPathAllowed(path)) {
      return { content: [{ type: 'text', text: 'Error: path outside allowed prefix' }], isError: true };
    }
    const output = await sendAndWait('write_file', { path, content }, device.id);
    const o = output.payload;
    if (o.success) return { content: [{ type: 'text', text: `File written: ${path}` }] };
    return { content: [{ type: 'text', text: `Error: ${o.error}` }], isError: true };
  });

  server.registerTool('get_client_messages', {
    description: '获取客户端发来的消息（你可以在客户端 UI 上给我发消息）',
    inputSchema: z.object({}),
  }, async () => {
    const msgs = agentMessages.splice(0);  // 取完清空
    if (msgs.length === 0) {
      return { content: [{ type: 'text', text: '没有新消息' }] };
    }
    const text = msgs.map(m =>
      `[${new Date(m.time).toLocaleTimeString()}] ${m.deviceName}: ${m.text}`
    ).join('\n');
    return { content: [{ type: 'text', text }] };
  });

  server.registerTool('get_device_info', {
    description: '获取远程电脑的系统信息（OS、CPU、内存等）',
    inputSchema: z.object({
      code: z.string().describe('客户端显示的验证码'),
    }),
  }, async ({ code }) => {
    const device = getDefaultDevice();
    if (!device) return { content: [{ type: 'text', text: 'Error: 没有已连接的设备' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    }
    const result = await sendAndWait('get_device_info', {}, device.id);
    const info = result.payload;
    const text = [
      `Hostname: ${info.hostname}`,
      `Platform: ${info.platform}`,
      `Architecture: ${info.arch}`,
      `CPU Cores: ${info.cpus}`,
      `Uptime: ${(info.uptime / 3600).toFixed(1)} hours`,
      `Home Directory: ${info.homedir}`,
      `User: ${info.userInfo?.username || 'unknown'}`,
    ].join('\n');
    return { content: [{ type: 'text', text }] };
  });

  return server;
}

// ─── HTTP + WebSocket 服务 ──────────────────

const httpServer = createServer(async (req, res) => {
  const url = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);

  if (url.pathname === MCP_PATH) {
    try {
      const mcpServer = createMcpServer();
      const transport = new SSEServerTransport(MCP_MESSAGE_PATH, res);
      transports.set(transport.sessionId, { server: mcpServer, transport });
      res.on('close', () => { transports.delete(transport.sessionId); });
      await mcpServer.connect(transport);
    } catch (err) {
      console.error('[mcp] SSE connect error:', err);
      try { res.writeHead(500).end('Internal Server Error'); } catch {}
    }
    return;
  }

  if (url.pathname === MCP_MESSAGE_PATH) {
    const sessionId = url.searchParams.get('sessionId');
    if (!sessionId || !transports.has(sessionId)) {
      res.writeHead(400).end('Missing or invalid sessionId');
      return;
    }
    try {
      await transports.get(sessionId).transport.handlePostMessage(req, res);
    } catch (err) {
      console.error('[mcp] handlePostMessage error:', err);
      try { res.writeHead(500).end('Internal Server Error'); } catch {}
    }
    return;
  }

  if (url.pathname === '/health') {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok', devices: devices.size }));
    return;
  }

  res.writeHead(404).end('Not Found');
});

httpServer.on('error', (err) => {
  console.error('[server] HTTP error:', err);
  process.exit(1);
});

const wss = new WebSocketServer({ noServer: true });

httpServer.on('upgrade', (req, socket, head) => {
  const url = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);

  if (url.pathname === '/device') {
    const psk = req.headers['x-psk'] || url.searchParams.get('psk');
    if (psk !== PSK) {
      socket.write('HTTP/1.1 403 Forbidden\r\n\r\n');
      socket.destroy();
      return;
    }
    wss.handleUpgrade(req, socket, head, (ws) => {
      wss.emit('connection', ws, req);
    });
    return;
  }

  socket.write('HTTP/1.1 404 Not Found\r\n\r\n');
  socket.destroy();
});

wss.on('connection', (ws, req) => {
  const deviceId = randomUUID();

  ws.on('message', (raw) => {
    let msg;
    try { msg = JSON.parse(raw.toString()); } catch {
      sendJSON(ws, { type: 'error', error: 'invalid json' });
      return;
    }

    const { type, requestId } = msg;

    if (type === 'register') {
      const { deviceName, os, arch, hostname, authCode } = msg;
      const name = deviceName || 'unknown';
      if (ALLOWED_DEVICES.length > 0 && !ALLOWED_DEVICES.includes(name)) {
        console.error(`[device] rejected: ${name}`);
        sendJSON(ws, { type: 'register_result', requestId, success: false, error: 'device not allowed' });
        return;
      }
      devices.set(deviceId, {
        id: deviceId, name, os: os || 'unknown', arch: arch || 'unknown',
        hostname: hostname || 'unknown', ws,
        authCode: authCode || null,
        connectedAt: new Date().toISOString(),
      });
      console.error(`[device] registered: ${name} (${deviceId}) code:${authCode || 'none'}`);
      sendJSON(ws, { type: 'register_result', requestId, success: true, deviceId });
      return;
    }

    if (type === 'agent_message') {
      const device = devices.get(deviceId);
      if (device) {
        agentMessages.push({
          id: randomUUID().slice(0, 8),
          text: msg.text || '',
          deviceName: device.name,
          time: new Date().toISOString(),
        });
        if (agentMessages.length > 200) agentMessages.shift();
        console.error(`[message] from ${device.name}: ${(msg.text || '').slice(0, 50)}`);
        sendJSON(ws, { type: 'agent_message_result', requestId, success: true });
      }
      return;
    }

    if (type === 'update_code') {
      const device = devices.get(deviceId);
      if (device) {
        device.authCode = msg.authCode;
        // 同步到同 hostname 的所有设备（agent.py 和 main.py 共享配对码）
        for (const [otherId, other] of devices) {
          if (otherId !== deviceId && other.hostname === device.hostname) {
            other.authCode = msg.authCode;
            console.error(`[device] code synced: ${other.name} (${otherId}) -> ${msg.authCode}`);
          }
        }
        console.error(`[device] code updated: ${device.name} (${deviceId}) -> ${msg.authCode}`);
        sendJSON(ws, { type: 'update_code_result', requestId, success: true });
      }
      return;
    }

    if (requestId && pendingRequests.has(requestId)) {
      const { resolve, reject, timer } = pendingRequests.get(requestId);
      clearTimeout(timer);
      pendingRequests.delete(requestId);
      if (type === 'error') {
        reject(new Error(msg.error));
      } else {
        resolve(msg);
      }
    }
  });

  ws.on('close', () => {
    const device = devices.get(deviceId);
    if (device) {
      console.error(`[device] disconnected: ${device.name} (${deviceId})`);
    }
    devices.delete(deviceId);
    rejectDeviceRequests(deviceId, `device '${deviceId}' disconnected`);
  });

  ws.on('error', (err) => {
    console.error(`[device] ws error: ${deviceId}`, err.message);
  });
});

httpServer.listen(PORT, () => {
  console.error(`[server] listening on http://0.0.0.0:${PORT}`);
  console.error(`[server] MCP endpoint: http://0.0.0.0:${PORT}${MCP_PATH}`);
  console.error(`[server] Device WS: ws://0.0.0.0:${PORT}/device`);
  console.error(`[server] Session management enabled`);
  if (ALLOWED_COMMANDS.length) console.error(`[server] allowed commands: ${ALLOWED_COMMANDS.join(', ')}`);
});

process.on('SIGINT', () => {
  console.error('[server] shutting down');
  wss.close();
  httpServer.close();
  process.exit(0);
});