import { createServer } from 'node:http';
import { WebSocketServer, WebSocket } from 'ws';
import { randomUUID, randomInt, randomBytes } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { SSEServerTransport } from '@modelcontextprotocol/sdk/server/sse.js';
import { z } from 'zod/v4';

const PORT = parseInt(process.env.PORT || '9876');
const PSK_FILE = process.env.PSK_FILE || '/opt/cloud-mcp/.psk';
const ALLOWED_DEVICES = (process.env.ALLOWED_DEVICES || '').split(',').filter(Boolean);
const COMMAND_TIMEOUT = parseInt(process.env.COMMAND_TIMEOUT || '60000');
const MCP_PATH = '/mcp';
const MCP_MESSAGE_PATH = '/mcp/message';

// 自动管理 PSK：第一次启动生成，之后从文件读取
let PSK = '';
if (existsSync(PSK_FILE)) {
  PSK = readFileSync(PSK_FILE, 'utf-8').trim();
  console.error(`[server] 🔑 PSK 已从 ${PSK_FILE} 读取`);
} else {
  PSK = randomBytes(32).toString('hex');
  writeFileSync(PSK_FILE, PSK, 'utf-8');
  console.error(`[server] 🔑 新 PSK 已生成并保存到 ${PSK_FILE}`);
  console.error(`[server] 📋 PSK: ${PSK}`);
}

// 也可通过环境变量覆盖
if (process.env.RELAY_PSK) {
  PSK = process.env.RELAY_PSK;
  console.error('[server] 🔑 使用环境变量 RELAY_PSK 覆盖');
}

const ALLOWED_COMMANDS = (process.env.ALLOWED_COMMANDS || '').split(',').filter(Boolean);
const ALLOWED_FILE_PREFIX = process.env.ALLOWED_FILE_PREFIX || '';

const devices = new Map();
const pendingRequests = new Map();
const transports = new Map();

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

function resetActivityTimer(device) {
  // Mark agent as paired and reset the 3s/30s activity timer
  if (!device._agentPaired) {
    device._agentPaired = true;
    try { sendJSON(device.ws, { type: 'agent_connected', requestId: '0', payload: {} }); } catch(e) {}
  }
  if (device._agentTimer) clearTimeout(device._agentTimer);
  if (device._grayTimer) clearTimeout(device._grayTimer);
  device._agentTimer = setTimeout(() => {
    try {
      sendJSON(device.ws, { type: 'agent_disconnected', requestId: '0', payload: {} });
      device._grayTimer = setTimeout(() => {
        try { sendJSON(device.ws, { type: 'agent_gray', requestId: '0', payload: {} }); } catch(e) {}
      }, 10000);
    } catch(e) {}
  }, 3000);
}

function createMcpServer() {
  const server = new McpServer({
    name: 'cloud-collaborative-mcp',
    version: '1.0.0',
  });

  server.registerTool('list_devices', {
    description: '列出所有已连接到中转的私人电脑设备（含验证码状态）',
    inputSchema: z.object({}),
  }, async () => {
    // Mark all connected devices as agent-paired
    for (const d of devices.values()) {
      resetActivityTimer(d);
    }
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

  server.registerTool('execute_command', {
    description: '在指定的私人电脑上执行 shell 命令并返回结果',
    inputSchema: z.object({
      deviceId: z.string().describe('目标设备 ID'),
      code: z.string().describe('客户端显示的验证码'),
      command: z.string().describe('要执行的 shell 命令'),
      timeout: z.number().optional().describe('命令超时时间（毫秒），默认 30000'),
    }),
  }, async ({ deviceId, code, command, timeout }) => {
    const device = devices.get(deviceId);
    if (!device) return { content: [{ type: 'text', text: 'Error: device not found' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误，请在客户端查看最新验证码' }], isError: true };
    }
    // 通知设备：Agent已配对成功
    resetActivityTimer(device);
    if (!checkCommandAllowed(command)) {
      return { content: [{ type: 'text', text: `Error: command '${command.split(/\s+/)[0]}' is not in the allowed list` }], isError: true };
    }
    const output = await executeCommand(deviceId, command, timeout);
    const text = [
      `Exit Code: ${output.exitCode}`,
      output.stdout ? `\nSTDOUT:\n${output.stdout}` : '',
      output.stderr ? `\nSTDERR:\n${output.stderr}` : '',
      output.killed ? '\n[Process was killed due to timeout]' : '',
    ].join('');
    return { content: [{ type: 'text', text }] };
  });

  server.registerTool('read_file', {
    description: '读取私人电脑上的文件内容',
    inputSchema: z.object({
      deviceId: z.string().describe('目标设备 ID'),
      code: z.string().describe('客户端显示的验证码'),
      path: z.string().describe('文件绝对路径'),
    }),
  }, async ({ deviceId, code, path }) => {
    const device = devices.get(deviceId);
    if (!device) return { content: [{ type: 'text', text: 'Error: device not found' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误，请在客户端查看最新验证码' }], isError: true };
    }
    resetActivityTimer(device);
    if (!checkPathAllowed(path)) {
      return { content: [{ type: 'text', text: `Error: path '${path}' is outside allowed file prefix` }], isError: true };
    }
    const output = await readFile(deviceId, path);
    if (output.success) {
      return { content: [{ type: 'text', text: output.content }] };
    }
    return { content: [{ type: 'text', text: `Error: ${output.error}` }], isError: true };
  });

  server.registerTool('write_file', {
    description: '将内容写入私人电脑上的文件',
    inputSchema: z.object({
      deviceId: z.string().describe('目标设备 ID'),
      code: z.string().describe('客户端显示的验证码'),
      path: z.string().describe('文件绝对路径'),
      content: z.string().describe('要写入的文件内容'),
    }),
  }, async ({ deviceId, code, path, content }) => {
    const device = devices.get(deviceId);
    if (!device) return { content: [{ type: 'text', text: 'Error: device not found' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误，请在客户端查看最新验证码' }], isError: true };
    }
    resetActivityTimer(device);
    if (!checkPathAllowed(path)) {
      return { content: [{ type: 'text', text: `Error: path '${path}' is outside allowed file prefix` }], isError: true };
    }
    const output = await writeFile(deviceId, path, content);
    if (output.success) {
      return { content: [{ type: 'text', text: `File written successfully: ${path}` }] };
    }
    return { content: [{ type: 'text', text: `Error: ${output.error}` }], isError: true };
  });

  server.registerTool('get_device_info', {
    description: '获取私人电脑的系统信息（OS、CPU、内存等）',
    inputSchema: z.object({
      deviceId: z.string().describe('目标设备 ID'),
      code: z.string().describe('客户端显示的验证码'),
    }),
  }, async ({ deviceId, code }) => {
    const device = devices.get(deviceId);
    if (!device) return { content: [{ type: 'text', text: 'Error: device not found' }], isError: true };
    if (device.authCode !== code) {
      return { content: [{ type: 'text', text: 'Error: 验证码错误，请在客户端查看最新验证码' }], isError: true };
    }
        resetActivityTimer(device);
    const info = await getDeviceInfo(deviceId);
    const gb = (b) => (b / 1024 / 1024 / 1024).toFixed(1) + ' GB';
    const text = [
      `Hostname: ${info.hostname}`,
      `Platform: ${info.platform}`,
      `Architecture: ${info.arch}`,
      `CPU Cores: ${info.cpus}`,
      `Total Memory: ${gb(info.totalMem)}`,
      `Free Memory: ${gb(info.freeMem)}`,
      `Uptime: ${(info.uptime / 3600).toFixed(1)} hours`,
      `Home Directory: ${info.homedir}`,
      `User: ${info.userInfo.username}`,
    ].join('\n');
    return { content: [{ type: 'text', text }] };
  });

  return server;
}

async function executeCommand(deviceId, command, timeout) {
  const result = await sendAndWait('execute_command', { command, timeout }, deviceId);
  return result.payload;
}

async function readFile(deviceId, path) {
  const result = await sendAndWait('read_file', { path }, deviceId);
  return result.payload;
}

async function writeFile(deviceId, path, content) {
  const result = await sendAndWait('write_file', { path, content }, deviceId);
  return result.payload;
}

async function getDeviceInfo(deviceId) {
  const result = await sendAndWait('get_device_info', {}, deviceId);
  return result.payload;
}

// --- 启动 ---

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
    // 仅通过 Header 传 PSK，不用 URL 参数（避免记录到日志）
    const psk = req.headers['x-psk'];
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
      
      // 设备名白名单检查
      if (ALLOWED_DEVICES.length > 0 && !ALLOWED_DEVICES.includes(name)) {
        console.error(`[device] rejected: ${name} (不在白名单中)`);
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

    // 更新验证码
    if (type === 'update_code') {
      const device = devices.get(deviceId);
      if (device) {
        device.authCode = msg.authCode;
        console.error(`[device] code updated: ${deviceId} -> ${msg.authCode}`);
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
  console.error(`[server] 注意: PSK 仅通过 Header 传递，URL 参数已禁用`);
  if (ALLOWED_COMMANDS.length) console.error(`[server] allowed commands: ${ALLOWED_COMMANDS.join(', ')}`);
  if (ALLOWED_FILE_PREFIX) console.error(`[server] allowed file prefix: ${ALLOWED_FILE_PREFIX}`);
});

process.on('SIGINT', () => {
  console.error('[server] shutting down');
  wss.close();
  httpServer.close();
  process.exit(0);
});


