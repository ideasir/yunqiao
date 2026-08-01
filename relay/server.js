import { createServer } from 'node:http';
import { WebSocketServer, WebSocket } from 'ws';
import { randomUUID, randomBytes } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { SSEServerTransport } from '@modelcontextprotocol/sdk/server/sse.js';
import { z } from 'zod/v4';

const PORT = parseInt(process.env.PORT || '9876');
const USERS_FILE = process.env.USERS_FILE || '/opt/cloud-mcp/.users.json';
const COMMAND_TIMEOUT = parseInt(process.env.COMMAND_TIMEOUT || '60000');
const MCP_PATH = '/mcp';
const MCP_MESSAGE_PATH = '/mcp/message';

// ═══════════════════════════════════════════════
// 多用户管理
// ═══════════════════════════════════════════════

let users = {};
function loadUsers() {
  if (existsSync(USERS_FILE)) {
    try { users = JSON.parse(readFileSync(USERS_FILE, 'utf-8')); } catch {}
  }
  // 默认管理员用户
  if (!users['admin']) {
    users['admin'] = { key: process.env.RELAY_KEY || 'yunqiao-mcp-key-2026', name: '管理员', role: 'admin', createdAt: new Date().toISOString() };
    saveUsers();
  }
}
function saveUsers() {
  mkdirSync(dirname(USERS_FILE), { recursive: true });
  writeFileSync(USERS_FILE, JSON.stringify(users, null, 2), 'utf-8');
}
function findByKey(key) {
  for (const [uid, u] of Object.entries(users)) {
    if (u.key === key) return { userId: uid, ...u };
  }
  return null;
}
loadUsers();

const ALLOWED_COMMANDS = (process.env.ALLOWED_COMMANDS || '').split(',').filter(Boolean);
const ALLOWED_FILE_PREFIX = process.env.ALLOWED_FILE_PREFIX || '';

const devices = new Map();
const pendingRequests = new Map();
const transports = new Map();
const agentMessages = [];

// ═══════════════════════════════════════════════
// 工具函数
// ═══════════════════════════════════════════════

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

// 客户端消息 → 在线 Agent 推送（SSE 短连接存活期间可即时收到通知，兜底靠 get_client_messages 轮询）
function notifyAgentsNewMessage(userId) {
  const unread = agentMessages.filter(m => m.userId === userId && !m.read).length;
  for (const { transport, userId: sessionUserId } of transports.values()) {
    if (sessionUserId !== userId) continue;
    transport.send({
      jsonrpc: '2.0',
      method: 'notifications/message',
      params: { unread },
    }).catch(() => {});
  }
}

// 按客户端任务队列拖拽后的顺序重排未读消息（只影响本用户自己的消息）
function reorderMessages(orderedIds, userId) {
  const idSet = new Set(orderedIds);
  if (idSet.size === 0) return;
  const byId = new Map();
  const rest = [];
  for (const m of agentMessages) {
    if (idSet.has(m.id) && m.userId === userId) byId.set(m.id, m);
    else rest.push(m);
  }
  const ordered = orderedIds.map(id => byId.get(id)).filter(Boolean);
  if (ordered.length === 0) return;
  // 找到第一个被排序消息原本的位置，把排序后的消息插回该处，保持其余消息相对顺序
  let insertAt = 0;
  for (let i = 0; i < agentMessages.length; i++) {
    if (idSet.has(agentMessages[i].id) && agentMessages[i].userId === userId) { insertAt = i; break; }
  }
  rest.splice(insertAt, 0, ...ordered);
  agentMessages.length = 0;
  agentMessages.push(...rest);
  console.error(`[message] reordered ${ordered.length} messages`);
}

function getDefaultDevice(userId) {
  if (devices.size === 0) return null;
  for (const device of devices.values()) {
    if (device.authCode && device.os !== 'web' && (!userId || device.userId === userId)) return device;
  }
  for (const device of devices.values()) {
    if (device.authCode && (!userId || device.userId === userId)) return device;
  }
  // 兜底也只返回本用户的设备，避免无认证身份越权访问其他用户的设备
  for (const device of devices.values()) {
    if (!userId || device.userId === userId) return device;
  }
  return null;
}

// ─── 配对码校验（防暴力） ──────────────────────
const AUTH_MAX_FAILS = parseInt(process.env.AUTH_MAX_FAILS || '5');
const AUTH_LOCK_MS = parseInt(process.env.AUTH_LOCK_MS || '300000');

function checkAuthCode(device, code) {
  if (!device) return false;
  const now = Date.now();
  if (device.authLockUntil && now < device.authLockUntil) return false;
  if (device.authCode !== code) {
    device.authFails = (device.authFails || 0) + 1;
    if (device.authFails >= AUTH_MAX_FAILS) {
      device.authLockUntil = now + AUTH_LOCK_MS;
      console.error(`[auth] 设备 ${device.id} 配对码失败 ${device.authFails} 次，锁定 ${Math.round(AUTH_LOCK_MS / 60000)} 分钟`);
    }
    return false;
  }
  device.authFails = 0;
  return true;
}

function broadcastToDevices(msg, userId) {
  for (const device of devices.values()) {
    if (!userId || device.userId === userId) {
      sendJSON(device.ws, msg);
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

async function handleSessionOp(op, payload, deviceId) {
  return await sendAndWait('session_op', { op, ...payload }, deviceId);
}

// ═══════════════════════════════════════════════
// MCP 服务
// ═══════════════════════════════════════════════

// 管理工具（用户管理）要求管理员密钥认证，未认证一律拒绝
function requireAdmin(authInfo) {
  if (!authInfo || !authInfo.isAdminAuth) {
    return { content: [{ type: 'text', text: 'Error: 需要管理员密钥认证（请求头 X-Key 或 Authorization: Bearer 传管理员密钥）' }], isError: true };
  }
  return null;
}

function createMcpServer(userId, authInfo = {}) {
  const server = new McpServer({
    name: 'yunqiao',
    version: '2.0.0',
  });

  server.registerTool('list_devices', {
    description: '列出所有已连接到中转的私人电脑设备',
    inputSchema: z.object({}),
  }, async () => {
    const list = Array.from(devices.values())
      .filter(d => !userId || d.userId === userId)
      .map(d => ({
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

  // 用户管理（admin 权限）
  server.registerTool('create_user', {
    description: '创建新用户（需要管理员密钥认证）',
    inputSchema: z.object({
      userId: z.string().describe('用户 ID'),
      name: z.string().optional().describe('用户名称'),
      key: z.string().optional().describe('密钥（不传则自动生成）'),
    }),
  }, async ({ userId, name, key }) => {
    const denied = requireAdmin(authInfo);
    if (denied) return denied;
    const user = users[userId];
    if (user && user.role !== 'admin') return { content: [{ type: 'text', text: 'Error: 需要 admin 权限' }], isError: true };
    if (users[userId]) return { content: [{ type: 'text', text: 'Error: 用户已存在' }], isError: true };
    if (!key) key = randomBytes(16).toString('hex');
    users[userId] = { key, name: name || userId, role: 'user', createdAt: new Date().toISOString() };
    saveUsers();
    return { content: [{ type: 'text', text: `用户已创建: ${userId}\n密钥: ${key}` }] };
  });

  server.registerTool('list_users', {
    description: '列出所有用户（需要管理员密钥认证）',
    inputSchema: z.object({}),
  }, async () => {
    const denied = requireAdmin(authInfo);
    if (denied) return denied;
    const list = Object.entries(users).map(([id, u]) =>
      `- ${id} (${u.name})${u.role === 'admin' ? ' 👑' : ''}`
    ).join('\n');
    return { content: [{ type: 'text', text: list || '暂无用户' }] };
  });

  server.registerTool('delete_user', {
    description: '删除用户（需要管理员密钥认证）',
    inputSchema: z.object({
      userId: z.string().describe('用户 ID'),
    }),
  }, async ({ userId }) => {
    const denied = requireAdmin(authInfo);
    if (denied) return denied;
    if (userId === 'admin') return { content: [{ type: 'text', text: 'Error: 不能删除 admin' }], isError: true };
    if (!users[userId]) return { content: [{ type: 'text', text: 'Error: 用户不存在' }], isError: true };
    delete users[userId];
    saveUsers();
    return { content: [{ type: 'text', text: `用户已删除: ${userId}` }] };
  });

  // 会话管理
  const sessionTools = [
    ['create_session', '在远程电脑上创建一个新的工作会话', { workDir: z.string(), name: z.string().optional(), code: z.string() }],
    ['exec', '在当前默认会话中执行 shell 命令', { command: z.string(), timeout: z.number().optional(), code: z.string() }],
    ['read_file', '在当前默认会话中读取文件', { path: z.string(), code: z.string() }],
    ['write_file', '在当前默认会话中写入文件', { path: z.string(), content: z.string(), code: z.string() }],
    ['close_session', '关闭当前默认会话', { code: z.string() }],
    ['list_sessions', '列出远程电脑上所有已创建的工作会话', { code: z.string() }],
    ['switch_session', '切换到指定的会话', { sessionId: z.string(), code: z.string() }],
  ];

  for (const [name, desc, schema] of sessionTools) {
    server.registerTool(name, { description: desc, inputSchema: z.object(schema) }, async (params) => {
      const device = getDefaultDevice(userId);
      if (!device) return { content: [{ type: 'text', text: 'Error: 没有已连接的设备' }], isError: true };
      if (!checkAuthCode(device, params.code)) {
        return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
      }
      const op = name.replace('_session', '');
      const result = await handleSessionOp(op === 'exec' ? 'exec' : op, params, device.id);
      return formatResult(name, result, params);
    });
  }

  // 旧工具
  server.registerTool('execute_command', {
    description: '[旧版] 在指定的私人电脑上执行 shell 命令',
    inputSchema: z.object({
      deviceId: z.string().optional(), command: z.string(), timeout: z.number().optional(), code: z.string(),
    }),
  }, async ({ deviceId, code, command, timeout }) => {
    const device = deviceId ? devices.get(deviceId) : getDefaultDevice(userId);
    if (!device) return { content: [{ type: 'text', text: 'Error: device not found' }], isError: true };
    if (!checkAuthCode(device, code)) return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    if (!checkCommandAllowed(command)) return { content: [{ type: 'text', text: 'Error: command not allowed' }], isError: true };
    const output = await sendAndWait('execute_command', { command, timeout }, device.id);
    const o = output.payload;
    return { content: [{ type: 'text', text: `Exit Code: ${o.exitCode}${o.stdout ? '\nSTDOUT:' + o.stdout : ''}${o.stderr ? '\nSTDERR:' + o.stderr : ''}${o.killed ? '\n[超时]' : ''}` }] };
  });

  server.registerTool('read_file_old', {
    description: '[旧版] 读取私人电脑上的文件',
    inputSchema: z.object({ deviceId: z.string().optional(), code: z.string(), path: z.string() }),
  }, async ({ deviceId, code, path }) => {
    const device = deviceId ? devices.get(deviceId) : getDefaultDevice(userId);
    if (!device) return { content: [{ type: 'text', text: 'Error: device not found' }], isError: true };
    if (!checkAuthCode(device, code)) return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    if (!checkPathAllowed(path)) return { content: [{ type: 'text', text: 'Error: path outside allowed prefix' }], isError: true };
    const result = await sendAndWait('read_file', { path }, device.id);
    const o = result.payload;
    return { content: [{ type: 'text', text: o.success ? o.content : `Error: ${o.error}` }], isError: !o.success };
  });

  server.registerTool('write_file_old', {
    description: '[旧版] 写入私人电脑上的文件',
    inputSchema: z.object({ deviceId: z.string().optional(), code: z.string(), path: z.string(), content: z.string() }),
  }, async ({ deviceId, code, path, content }) => {
    const device = deviceId ? devices.get(deviceId) : getDefaultDevice(userId);
    if (!device) return { content: [{ type: 'text', text: 'Error: device not found' }], isError: true };
    if (!checkAuthCode(device, code)) return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    if (!checkPathAllowed(path)) return { content: [{ type: 'text', text: 'Error: path outside allowed prefix' }], isError: true };
    const result = await sendAndWait('write_file', { path, content }, device.id);
    const o = result.payload;
    return { content: [{ type: 'text', text: o.success ? `文件已写入: ${o.path}` : `Error: ${o.error}` }], isError: !o.success };
  });

  server.registerTool('get_client_messages', {
    description: '获取客户端发来的新消息（客户端 UI 发来的消息，读取后自动标记已读并回执给客户端）。建议每次会话开始时调用一次',
    inputSchema: z.object({}),
  }, async () => {
    // 只取走本用户未读消息（顺带清理），并广播回执
    const unread = agentMessages.filter(m => m.userId === userId && !m.read);
    const ids = unread.map(m => m.id);
    if (unread.length > 0) {
      const readIds = new Set(ids);
      for (let i = agentMessages.length - 1; i >= 0; i--) {
        if (readIds.has(agentMessages[i].id)) agentMessages.splice(i, 1);
      }
      broadcastToDevices({ type: 'messages_read', ids }, userId);
    }
    if (unread.length === 0) {
      return { content: [{ type: 'text', text: '没有新消息' }] };
    }
    const text = unread.map(m =>
      `${m.urgent ? '⚠️ [紧急] ' : ''}[${new Date(m.time).toLocaleTimeString()}] ${m.deviceName}: ${m.text}`
    ).join('\n');
    return { content: [{ type: 'text', text }] };
  });

  server.registerTool('get_device_info', {
    description: '获取远程电脑的系统信息',
    inputSchema: z.object({ code: z.string() }),
  }, async ({ code }) => {
    const device = getDefaultDevice(userId);
    if (!device) return { content: [{ type: 'text', text: 'Error: 没有已连接的设备' }], isError: true };
    if (!checkAuthCode(device, code)) return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    const result = await sendAndWait('get_device_info', {}, device.id);
    const info = result.payload;
    const gb = (b) => (b / 1024 / 1024 / 1024).toFixed(1) + ' GB';
    const text = [
      `Hostname: ${info.hostname}`, `Platform: ${info.platform}`, `Architecture: ${info.arch}`,
      `CPU Cores: ${info.cpus}`, `Uptime: ${(info.uptime / 3600).toFixed(1)} hours`,
      `Home Directory: ${info.homedir}`, `User: ${info.userInfo?.username || 'unknown'}`,
    ].join('\n');
    return { content: [{ type: 'text', text }] };
  });

  server.registerTool('notify', {
    description: '发送通知消息到客户端日志',
    inputSchema: z.object({ text: z.string(), code: z.string() }),
  }, async ({ text, code }) => {
    const device = getDefaultDevice(userId);
    if (!device) return { content: [{ type: 'text', text: 'Error: 没有已连接的设备' }], isError: true };
    if (!checkAuthCode(device, code)) return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    sendJSON(device.ws, { type: 'notify', text });
    return { content: [{ type: 'text', text: '已发送' }] };
  });

  server.registerTool('download', {
    description: '下载远程电脑上的文件（返回 base64 编码）',
    inputSchema: z.object({ path: z.string(), code: z.string() }),
  }, async ({ path, code }) => {
    const device = getDefaultDevice(userId);
    if (!device) return { content: [{ type: 'text', text: 'Error: 没有已连接的设备' }], isError: true };
    if (!checkAuthCode(device, code)) return { content: [{ type: 'text', text: 'Error: 验证码错误' }], isError: true };
    if (!checkPathAllowed(path)) return { content: [{ type: 'text', text: 'Error: path outside allowed prefix' }], isError: true };
    const result = await sendAndWait('download', { path }, device.id);
    const o = result.payload;
    if (o.success) return { content: [{ type: 'text', text: `FILE:${path}|${o.size}|${o.data}` }] };
    return { content: [{ type: 'text', text: `Error: ${o.error}` }], isError: true };
  });

  return server;
}

function formatResult(name, result, params) {
  const o = result.payload;
  if (name === 'create_session') {
    const session = o;
    return { content: [{ type: 'text', text: `会话已创建: ${session?.name || '?'} (${session?.id || '?'})\n工作目录: ${session?.workDir || '?'}` }] };
  }
  if (name === 'exec') {
    const text = [`Exit Code: ${o.exitCode}`, o.stdout ? `\nSTDOUT:\n${o.stdout}` : '', o.stderr ? `\nSTDERR:\n${o.stderr}` : '', o.killed ? '\n[超时]' : ''].join('');
    return { content: [{ type: 'text', text }] };
  }
  if (name === 'read_file') {
    return { content: [{ type: 'text', text: o.success ? o.content : `Error: ${o.error}` }], isError: !o.success };
  }
  if (name === 'write_file') {
    return { content: [{ type: 'text', text: o.success ? `文件已写入: ${o.path}` : `Error: ${o.error}` }], isError: !o.success };
  }
  if (name === 'close_session') {
    return { content: [{ type: 'text', text: o.success ? '会话已关闭' : `Error: ${o.error}` }], isError: !o.success };
  }
  if (name === 'list_sessions') {
    const sessions = o.sessions || [];
    if (sessions.length === 0) return { content: [{ type: 'text', text: '暂无会话' }] };
    const text = sessions.map(s => `${s.isDefault ? '👉' : '  '} ${s.name} (${s.id})\n    工作目录: ${s.workDir}\n    当前路径: ${s.cwd}`).join('\n\n');
    return { content: [{ type: 'text', text }] };
  }
  if (name === 'switch_session') {
    return { content: [{ type: 'text', text: o.success ? `已切换到: ${o.name} (${o.sessionId})` : `Error: ${o.error}` }], isError: !o.success };
  }
  return { content: [{ type: 'text', text: JSON.stringify(o) }] };
}

// ═══════════════════════════════════════════════
// HTTP + WebSocket
// ═══════════════════════════════════════════════

const httpServer = createServer(async (req, res) => {
  const url = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);

  if (url.pathname === '/api/users') {
    res.writeHead(403, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: 'API disabled' }));
    return;
  }

  if (url.pathname === MCP_PATH) {
    try {
      // 认证：优先 X-Key / Authorization: Bearer（用户密钥），否则回退 ?user=（向后兼容，无管理员权限）
      const authKey = req.headers['x-key'] || (req.headers.authorization || '').replace(/^Bearer\s+/i, '') || '';
      const authUser = authKey ? findByKey(authKey) : null;
      const isAdminAuth = !!(authUser && authUser.role === 'admin');
      const userId = (authUser && authUser.userId) || url.searchParams.get('user') || 'admin';
      const mcpServer = createMcpServer(userId, { isAdminAuth, authUser });
      const transport = new SSEServerTransport(MCP_MESSAGE_PATH, res);
      transports.set(transport.sessionId, { server: mcpServer, transport, userId });
      res.on('close', () => {
        transports.delete(transport.sessionId);
        broadcastToDevices({ type: 'agent_disconnected' }, userId);
      });
      await mcpServer.connect(transport);
      let latency = 0;
      for (const device of devices.values()) {
        if (device.latency && device.userId === userId) { latency = device.latency; break; }
      }
      broadcastToDevices({ type: 'agent_connected', latency, platform: 'sandbox', hostname: 'OpenClaw Agent', relayPlatform: 'Ubuntu Linux' }, userId);
    } catch (err) {
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
    try { await transports.get(sessionId).transport.handlePostMessage(req, res); } catch {}
    return;
  }

  if (url.pathname === '/health') {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok', devices: devices.size, users: Object.keys(users).length }));
    return;
  }

  res.writeHead(404).end('Not Found');
});

const wss = new WebSocketServer({ noServer: true });

const heartbeat = setInterval(() => {
  for (const [id, device] of devices) {
    if (device.ws.readyState === WebSocket.OPEN) {
      device._lastPingAt = Date.now();
      device.ws.ping();
    }
  }
}, 15000);

httpServer.on('upgrade', (req, socket, head) => {
  const url = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);
  if (url.pathname === '/device') {
    const authKey = req.headers['x-key'] || url.searchParams.get('key');
    const user = findByKey(authKey);
    if (!user) {
      socket.write('HTTP/1.1 403 Forbidden\r\n\r\n');
      socket.destroy();
      return;
    }
    wss.handleUpgrade(req, socket, head, (ws) => {
      wss.emit('connection', ws, req, user);
    });
    return;
  }
  socket.write('HTTP/1.1 404 Not Found\r\n\r\n');
  socket.destroy();
});

wss.on('connection', (ws, req, user) => {
  const deviceId = randomUUID();
  // 持久 pong 监听（避免每次 ping 注册 once 监听器导致泄漏），延迟负值归零
  ws.on('pong', () => {
    const d = devices.get(deviceId);
    if (d && d._lastPingAt) {
      d.latency = Math.max(0, Date.now() - d._lastPingAt);
      d._lastPingAt = null;
    }
  });
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
      devices.set(deviceId, {
        id: deviceId, name, os: os || 'unknown', arch: arch || 'unknown',
        hostname: hostname || 'unknown', ws,
        authCode: authCode || null, userId: user.userId,
        connectedAt: new Date().toISOString(), latency: 0,
        authFails: 0, authLockUntil: null,
      });
      console.error(`[device] registered: ${name} (${deviceId}) user:${user.userId} code:${authCode || 'none'}`);
      sendJSON(ws, { type: 'register_result', requestId, success: true, deviceId });
      return;
    }
    if (type === 'agent_message') {
      const device = devices.get(deviceId);
      if (device) {
        agentMessages.push({
          id: msg.msgId || randomUUID().slice(0, 8),
          text: msg.text || '',
          deviceName: device.name,
          time: new Date().toISOString(),
          urgent: !!msg.urgent,
          read: false,
          userId: device.userId,
        });
        if (agentMessages.length > 200) agentMessages.shift();
        console.error(`[message] from ${device.name}: ${(msg.text || '').slice(0, 50)}`);
        sendJSON(ws, { type: 'agent_message_result', requestId, success: true });
        // 尽量即时通知在线的 Agent 会话有新消息
        notifyAgentsNewMessage(device.userId);
      }
      return;
    }
    if (type === 'update_code') {
      const device = devices.get(deviceId);
      if (device) {
        device.authCode = msg.authCode;
        device.authFails = 0;
        device.authLockUntil = null;
        for (const [otherId, other] of devices) {
          if (otherId !== deviceId && other.hostname === device.hostname && other.userId === device.userId) {
            other.authCode = msg.authCode;
            other.authFails = 0;
            other.authLockUntil = null;
          }
        }
        sendJSON(ws, { type: 'update_code_result', requestId, success: true });
      }
      return;
    }
    if (type === 'reorder_messages') {
      const device = devices.get(deviceId);
      if (device && Array.isArray(msg.orderedIds)) {
        reorderMessages(msg.orderedIds, device.userId);
      }
      sendJSON(ws, { type: 'reorder_result', requestId, success: true });
      return;
    }

    if (requestId && pendingRequests.has(requestId)) {
      const { resolve, reject, timer } = pendingRequests.get(requestId);
      clearTimeout(timer);
      pendingRequests.delete(requestId);
      if (type === 'error') reject(new Error(msg.error));
      else resolve(msg);
    }
  });
  ws.on('close', () => {
    const device = devices.get(deviceId);
    if (device) console.error(`[device] disconnected: ${device.name} (${deviceId})`);
    devices.delete(deviceId);
    rejectDeviceRequests(deviceId, `device '${deviceId}' disconnected`);
  });
  ws.on('error', (err) => { console.error(`[device] ws error: ${deviceId}`, err.message); });
});

httpServer.listen(PORT, () => {
  console.error(`[server] listening on http://0.0.0.0:${PORT}`);
  console.error(`[server] Users: ${Object.keys(users).length} (admin: ${users['admin']?.key?.slice(0,8)}...)`);
});

process.on('SIGINT', () => {
  wss.close();
  httpServer.close();
  process.exit(0);
});