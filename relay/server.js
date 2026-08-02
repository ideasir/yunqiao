import { createServer } from 'node:http';
import { WebSocketServer, WebSocket } from 'ws';
import { randomUUID, randomBytes } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync, mkdirSync, appendFileSync, statSync, renameSync } from 'node:fs';
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
    // 首次创建 admin 时必须由管理员显式提供 RELAY_KEY，禁止使用任何默认/硬编码密钥，
    // 否则未配置的服务器会用全网已知的默认密码暴露在公网（原硬编码 'yunqiao-mcp-key-2026' 已移除）。
    // 注意：仅"首次创建"才校验；若 .users.json 已存在且含 admin 密钥（历史合法持久化），不阻止启动。
    if (!process.env.RELAY_KEY) {
      console.error('[fatal] 首次启动必须设置 RELAY_KEY 环境变量作为管理员密钥，拒绝使用默认密钥启动。');
      console.error('        请执行: export RELAY_KEY="<你自己的强密钥>"  然后重新启动。');
      process.exit(1);
    }
    users['admin'] = { key: process.env.RELAY_KEY, name: '管理员', role: 'admin', createdAt: new Date().toISOString() };
    saveUsers();
  }
  // 为没有 mcpTicket 的用户补齐（新机制：每用户一个当前有效的动态 MCP 地址）
  let changed = false;
  for (const u of Object.values(users)) {
    if (!u.mcpTicket) { u.mcpTicket = randomBytes(16).toString('hex'); changed = true; }
  }
  if (changed) saveUsers();
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
function findByTicket(ticket) {
  for (const [uid, u] of Object.entries(users)) {
    if (u.mcpTicket === ticket) return { userId: uid, ...u };
  }
  return null;
}
// 生成新的动态 MCP 地址 ticket（作废旧 ticket，保证只有最新一个有效）
function newMcpTicket(userId) {
  const ticket = randomBytes(16).toString('hex');
  if (users[userId]) users[userId].mcpTicket = ticket;
  saveUsers();
  return ticket;
}
loadUsers();

// ─── Agent 活跃度（实时推送，事件驱动） ─────────
// 统计某用户当前：MCP 连接数 / 运行中任务 / 挂起的工具调用（含配额供灯排显示）
function getActivity(userId) {
  const connections = Array.from(transports.values()).filter(t => t.userId === userId).length;
  const runningTasks = Array.from(tasks.values()).filter(t => t.userId === userId && t.status === 'running').length;
  let pendingCalls = 0;
  for (const [, entry] of pendingRequests) {
    const d = devices.get(entry.deviceId);
    if (d && d.userId === userId) pendingCalls++;
  }
  return { connections, runningTasks, pendingCalls, maxConnections: getLimits(userId).maxConnections };
}

// 事件驱动推送（长连接是实时的，变化发生时立即推；200ms 节流合并高频变化）
const activityTimers = new Map();
function scheduleActivityPush(userId) {
  if (!userId || activityTimers.has(userId)) return;
  activityTimers.set(userId, setTimeout(() => {
    activityTimers.delete(userId);
    broadcastToDevices({ type: 'agent_activity', payload: getActivity(userId) }, userId);
  }, 200));
}

// 定期推送活跃状态，保持呼吸灯同步（即使之前的推送丢失）
setInterval(() => {
  for (const userId of Object.keys(users)) {
    scheduleActivityPush(userId);
  }
}, 30000); // 每 30 秒

// ─── 异步任务 ───────────────────────────────────
const TASK_TIMEOUT = parseInt(process.env.TASK_TIMEOUT || '1800000', 10);       // 运行超时，默认 30 分钟
const TASK_RESULT_TTL = parseInt(process.env.TASK_RESULT_TTL || '900000', 10);  // 结果保留，默认 15 分钟
const TASK_NOTIFY_TTL = parseInt(process.env.TASK_NOTIFY_TTL || '300000', 10);  // 任务完成通知有效期（默认 5 分钟），过期不再推送旧通知
const TASK_MAX_CONCURRENT = parseInt(process.env.TASK_MAX_CONCURRENT || '50', 10); // 每设备并发上限（默认放宽到 50）
const SSE_IDLE_TIMEOUT = parseInt(process.env.SSE_IDLE_TIMEOUT || '600000', 10);  // SSE 空闲超时（默认 10 分钟），防僵尸连接占满连接数
const tasks = new Map();  // taskId -> { userId, deviceId, command, status, ... }

// 配对码验证缓存：首次验证通过后缓存，SSE 重连免验
// key: userId, value: { ticket, deviceId, since }
const authCache = new Map();
const AUTH_CACHE_TTL = parseInt(process.env.AUTH_CACHE_TTL || '3600000', 10); // 1 小时

// 定期清理：完成的超期任务（防止任务表无限膨胀，也处理"Agent 忘了取"）
const taskCleanup = setInterval(() => {
  const now = Date.now();
  for (const [id, t] of tasks) {
    if (t.status !== 'running' && t.finishedAt && now - t.finishedAt > TASK_RESULT_TTL) {
      tasks.delete(id);
    }
  }
}, 60000);

// 定期清理僵尸 SSE 连接（客户端异常断开时 res close 可能不触发，
// 残留会占满该用户的连接数导致永久 429——超时未活动即主动清理）
const sseCleanup = setInterval(() => {
  const now = Date.now();
  for (const [sid, entry] of transports) {
    if (now - (entry.lastActive || now) > SSE_IDLE_TIMEOUT) {
      transports.delete(sid);
      try { entry.transport.close(); } catch {}
      broadcastToDevices({ type: 'agent_disconnected' }, entry.userId);
      scheduleActivityPush(entry.userId);  // 僵尸清理后更新活跃度（否则灯残留一直闪）
      console.error(`[sse] 空闲连接已清理: ${sid}`);
    }
  }
}, parseInt(process.env.SSE_CLEANUP_INTERVAL || '60000', 10));

// ─── 审计日志 ───────────────────────────────────
const AUDIT_LOG = process.env.AUDIT_LOG || '/opt/cloud-mcp/audit.log';
const AUDIT_MAX_BYTES = parseInt(process.env.AUDIT_MAX_BYTES || '20971520', 10); // 默认 20MB 后轮转

function appendAudit(entry) {
  try {
    mkdirSync(dirname(AUDIT_LOG), { recursive: true });
    if (existsSync(AUDIT_LOG) && statSync(AUDIT_LOG).size > AUDIT_MAX_BYTES) {
      renameSync(AUDIT_LOG, AUDIT_LOG + '.old');  // 简单轮转：保留一份 .old
    }
    appendFileSync(AUDIT_LOG, JSON.stringify(entry) + '\n', 'utf-8');
  } catch {}
}
// 审计参数摘要：只取追责需要的字段，绝不记录配对码/文件内容
function summarizeArgs(toolName, params) {
  const p = params || {};
  switch (toolName) {
    case 'exec': case 'execute_command': return { command: p.command, cwd: p.cwd, timeout: p.timeout };
    case 'read_file': case 'read_file_old': return { path: p.path };
    case 'write_file': case 'write_file_old': return { path: p.path, contentLen: (p.content || '').length };
    case 'download': return { path: p.path };
    case 'create_session': return { workDir: p.workDir, name: p.name };
    case 'switch_session': case 'close_session': return { sessionId: p.sessionId };
    case 'notify': return { text: (p.text || '').slice(0, 100) };
    case 'create_user': return { userId: p.userId, name: p.name };
    case 'delete_user': return { userId: p.userId };
    case 'set_user_limit': return { target: p.userId, maxConnections: p.maxConnections, qps: p.qps, maxOutputMB: p.maxOutputMB, maxDownloadMB: p.maxDownloadMB };
    case 'exec_task': return { command: p.command, timeout: p.timeout };
    case 'get_task_result': return { taskId: p.taskId };
    default: return { };
  }
}
function readAuditTail(n) {
  try {
    if (!existsSync(AUDIT_LOG)) return [];
    const lines = readFileSync(AUDIT_LOG, 'utf-8').split('\n').filter(Boolean);
    return lines.slice(-n).map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
  } catch { return []; }
}

// ─── 认证与配额 ─────────────────────────────────
// AUTH_REQUIRED=1：/mcp 必须带用户密钥/令牌认证，?user= 参数失效
const AUTH_REQUIRED = (process.env.AUTH_REQUIRED || '0') === '1';
const DEFAULT_LIMITS = {
  maxConnections: parseInt(process.env.MAX_CONNECTIONS || '50', 10),
  qps: parseInt(process.env.DEFAULT_QPS || '50', 10),
  maxOutputMB: parseInt(process.env.MAX_OUTPUT_MB || '5', 10),
  maxDownloadMB: parseInt(process.env.MAX_DOWNLOAD_MB || '5', 10),
};
// 每个用户的配额（可在 users.limits 单独覆盖：放开=调大，收紧=调小）
function getLimits(userId) {
  const u = userId ? users[userId] : null;
  return { ...DEFAULT_LIMITS, ...((u && u.limits) || {}) };
}

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
      scheduleActivityPush(device.userId);  // 调用结束
      reject(new Error(`request timed out after ${COMMAND_TIMEOUT}ms`));
    }, COMMAND_TIMEOUT);
    pendingRequests.set(requestId, { deviceId, resolve, reject, timer });
    sendJSON(device.ws, { type, requestId, payload });
    scheduleActivityPush(device.userId);  // 调用开始
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
  const d = devices.get(deviceId);
  if (d) scheduleActivityPush(d.userId);  // 挂起调用被清，活跃度变化
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

// 异步任务完成 → 在线 Agent 推送（尽力而为；兜底靠工具响应附加"任务完成"提示）
function notifyTaskDone(userId, task) {
  for (const { transport, userId: sessionUserId } of transports.values()) {
    if (sessionUserId !== userId) continue;
    transport.send({
      jsonrpc: '2.0',
      method: 'notifications/task',
      params: { taskId: task.taskId, status: task.status },
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
    if (device.authCode && device.os !== 'web' && userId && device.userId === userId) return device;
  }
  for (const device of devices.values()) {
    if (device.authCode && userId && device.userId === userId) return device;
  }
  // 兜底也只返回本用户的设备，匿名/跨用户一律拿不到任何设备
  for (const device of devices.values()) {
    if (userId && device.userId === userId) return device;
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

// 会话级配对码：首次带 code 调用验证成功后，该会话内后续调用免 code
function getAuthedDevice(sessionState, userId, deviceId, code) {
  // 无 code 时优先使用本会话已授权设备
  if (!deviceId && !code && sessionState.authedDeviceId) {
    const d = devices.get(sessionState.authedDeviceId);
    if (d && d.userId === userId) return { device: d };
  }
  const device = deviceId ? devices.get(deviceId) : getDefaultDevice(userId);
  if (!device) return { device: null, error: '没有已连接的设备' };
  // 设备归属校验：显式传 deviceId 时只能操作自己名下的设备（防跨用户执行）
  if (device.userId !== userId) return { device: null, error: '设备不存在' };
  if (code) {
    if (!checkAuthCode(device, code)) return { device: null, error: '验证码错误' };
    sessionState.authedDeviceId = device.id;
    return { device };
  }
  if (sessionState.authedDeviceId === device.id) return { device };
  return { device: null, error: '需要配对码（请先调用一次带配对码的工具完成授权）' };
}

function broadcastToDevices(msg, userId) {
  // 匿名（无 userId）不广播给任何设备
  if (!userId) return;
  for (const device of devices.values()) {
    if (device.userId === userId) {
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
  // 统一转正斜杠比较，避免 Windows 反斜杠与 Linux 正斜杠混用导致白名单失效/被绕过
  const p = String(filePath || '').replace(/\\/g, '/').replace(/\/+$/, '');
  const prefix = ALLOWED_FILE_PREFIX.replace(/\\/g, '/').replace(/\/+$/, '');
  return p === prefix || p.startsWith(prefix + '/');
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

// 工具响应附加未读消息 + 任务完成提示：把内容直接贴进每个工具响应（Agent 调任何工具都能看到，想忽略都难）
function withMsgHint(userId, handler) {
  return async (params) => {
    const result = await handler(params);
    const extras = [];
    // 未读消息：内容已展示给 Agent，即视为已读（移除 + 广播回执，客户端 UI 任务划掉）
    const unread = agentMessages.filter(m => m.userId === userId && !m.read);
    if (unread.length > 0) {
      const hint = unread.map(m =>
        `[${new Date(m.time).toLocaleTimeString()}]${m.urgent ? ' ⚠️紧急' : ''} ${m.deviceName}: ${(m.text || '').slice(0, 300)}`
      ).join('\n');
      extras.push(`\n📬 [来自客户端的未读消息，请优先处理]\n${hint}\n（处理完请调用 get_client_messages 确认已读）`);
      // 消息内容已展示给 Agent，视为已读：移除 + 回执（无需 Agent 主动确认）
      const ids = unread.map(m => m.id);
      const idSet = new Set(ids);
      for (let i = agentMessages.length - 1; i >= 0; i--) {
        if (idSet.has(agentMessages[i].id)) agentMessages.splice(i, 1);
      }
      broadcastToDevices({ type: 'messages_read', ids }, userId);
    }
    // 已完成未查看的任务（展示后标记已查看避免堆积；超过 TASK_NOTIFY_TTL 的旧通知直接过期不推）
    const nowN = Date.now();
    for (const t of tasks.values()) {
      if (t.userId === userId && t.status !== 'running' && !t.viewed && t.finishedAt && (nowN - t.finishedAt) >= TASK_NOTIFY_TTL) {
        t.viewed = true;  // 过期通知：强制标记已查看
      }
    }
    const doneTasks = Array.from(tasks.values()).filter(t => t.userId === userId && t.status !== 'running' && !t.viewed);
    if (doneTasks.length > 0) {
      const taskHint = doneTasks.map(t =>
        `[${t.status}] ${t.taskId.slice(0, 8)} ${(t.command || '').slice(0, 50)} 完成于 ${new Date(t.finishedAt).toLocaleTimeString()}`
      ).join('\n');
      extras.push(`\n📋 [任务完成通知]\n${taskHint}\n（用 get_task_result 查看结果，或 list_tasks 查看全部）`);
      // 通知已展示，标记已查看（结果仍在任务表，可随时查询，但不重复推送）
      doneTasks.forEach(t => { t.viewed = true; });
    }
    if (extras.length > 0 && result && Array.isArray(result.content)) {
      result.content = [...result.content, { type: 'text', text: extras.join('\n') }];
    }
    return result;
  };
}

// 每用户调用限流（1 秒滑动窗口）与输出大小限制
const qpsCounters = new Map();
// 需要广播"操作流"给客户端的工具（让用户看到 Agent 在干什么，跳过纯查询/轮询）
const ACTION_TOOLS = new Set(['exec', 'execute_command', 'read_file', 'write_file', 'read_file_old', 'write_file_old', 'download', 'exec_task', 'notify', 'create_session', 'switch_session', 'close_session']);
function checkQps(userId) {
  const limits = getLimits(userId);
  const now = Date.now();
  // 防恶意刷 ?user= 填爆计数器表
  if (qpsCounters.size > 500) qpsCounters.clear();
  let c = qpsCounters.get(userId);
  if (!c || now - c.start >= 1000) { c = { start: now, count: 0 }; qpsCounters.set(userId, c); }
  if (c.count >= limits.qps) return false;
  c.count++;
  return true;
}
function withLimits(userId, handler, toolName) {
  return async (params) => {
    const t0 = Date.now();
    if (!checkQps(userId)) {
      appendAudit({ ts: new Date().toISOString(), userId, tool: toolName || '?', args: summarizeArgs(toolName, params), ok: false, error: 'qps-limit', durationMs: Date.now() - t0 });
      return { content: [{ type: 'text', text: 'Error: 调用过于频繁（超出该用户 QPS 限制）' }], isError: true };
    }
    const result = await handler(params);
    // 审计：谁、何时、调了什么、传了什么参数、结果如何（追责用）
    appendAudit({
      ts: new Date().toISOString(),
      userId,
      tool: toolName || '?',
      args: summarizeArgs(toolName, params),
      ok: !(result && result.isError),
      durationMs: Date.now() - t0,
    });
    // Agent 操作流广播：让客户端日志显示 Agent 实际在干什么
    if (ACTION_TOOLS.has(toolName)) {
      const brief = summarizeArgs(toolName, params);
      const detail = (brief.command || brief.path || brief.text || '').slice(0, 60);
      broadcastToDevices({ type: 'agent_action', text: `Agent ${toolName}${detail ? ': ' + detail : ''}` }, userId);
    }
    // download 的输出大小由 handler 内按 maxDownloadMB 检查，这里豁免
    if (toolName !== 'download' && result && Array.isArray(result.content)) {
      let total = 0;
      for (const c of result.content) if (c && c.text) total += c.text.length;
      const maxBytes = getLimits(userId).maxOutputMB * 1024 * 1024;
      if (total > maxBytes) {
        return { content: [{ type: 'text', text: `Error: 输出过大（超过 ${getLimits(userId).maxOutputMB}MB 上限）` }], isError: true };
      }
    }
    return result;
  };
}
// 工具统一包装：限流 + 输出限制 + 未读消息提示
function wrapTool(userId, handler, toolName) {
  // 先 withLimits（在原始输出上做大小检查/QPS/审计），再 withMsgHint 追加消息提示；
  // 顺序不能反，否则消息提示会被计入输出大小，导致接近上限的合法结果被误判为"输出过大"而丢弃
  return withMsgHint(userId, withLimits(userId, handler, toolName));
}

function createMcpServer(userId, authInfo = {}) {
  const server = new McpServer({
    name: 'yunqiao',
    version: '2.0.0',
  });
  // 会话级状态：首次配对码验证成功后，该会话内免 code（连接时已验则直接预授权）
  const sessionState = { authedDeviceId: (authInfo && authInfo.authedDeviceId) || null };

  server.registerTool('list_devices', {
    description: '列出所有已连接到中转的私人电脑设备',
    inputSchema: z.object({}),
  }, wrapTool(userId, async () => {
    const list = Array.from(devices.values())
      .filter(d => userId && d.userId === userId)
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
  }, 'list_devices'));

  // 用户管理（admin 权限）
  server.registerTool('create_user', {
    description: '创建新用户（需要管理员密钥认证）',
    inputSchema: z.object({
      userId: z.string().describe('用户 ID'),
      name: z.string().optional().describe('用户名称'),
      key: z.string().optional().describe('密钥（不传则自动生成）'),
    }),
  }, wrapTool(userId, async ({ userId, name, key }) => {
    const denied = requireAdmin(authInfo);
    if (denied) return denied;
    const user = users[userId];
    if (user && user.role !== 'admin') return { content: [{ type: 'text', text: 'Error: 需要 admin 权限' }], isError: true };
    if (users[userId]) return { content: [{ type: 'text', text: 'Error: 用户已存在' }], isError: true };
    if (!key) key = randomBytes(16).toString('hex');
    users[userId] = { key, name: name || userId, role: 'user', createdAt: new Date().toISOString() };
    saveUsers();
    return { content: [{ type: 'text', text: `用户已创建: ${userId}\n密钥: ${key}` }] };
  }, 'create_user'));

  server.registerTool('list_users', {
    description: '列出所有用户（需要管理员密钥认证）',
    inputSchema: z.object({}),
  }, wrapTool(userId, async () => {
    const denied = requireAdmin(authInfo);
    if (denied) return denied;
    const list = Object.entries(users).map(([id, u]) =>
      `- ${id} (${u.name})${u.role === 'admin' ? ' 👑' : ''}`
    ).join('\n');
    return { content: [{ type: 'text', text: list || '暂无用户' }] };
  }, 'list_users'));

  server.registerTool('delete_user', {
    description: '删除用户（需要管理员密钥认证）',
    inputSchema: z.object({
      userId: z.string().describe('用户 ID'),
    }),
  }, wrapTool(userId, async ({ userId }) => {
    const denied = requireAdmin(authInfo);
    if (denied) return denied;
    if (userId === 'admin') return { content: [{ type: 'text', text: 'Error: 不能删除 admin' }], isError: true };
    if (!users[userId]) return { content: [{ type: 'text', text: 'Error: 用户不存在' }], isError: true };
    delete users[userId];
    saveUsers();
    return { content: [{ type: 'text', text: `用户已删除: ${userId}` }] };
  }, 'delete_user'));

  server.registerTool('set_user_limit', {
    description: '设置用户资源配额（放开=调大，收紧=调小；不带任何限制参数则恢复默认）。需要管理员密钥认证',
    inputSchema: z.object({
      userId: z.string().describe('用户 ID'),
      maxConnections: z.number().optional().describe('最大并发连接数'),
      qps: z.number().optional().describe('每秒工具调用上限'),
      maxOutputMB: z.number().optional().describe('单次输出上限（MB）'),
      maxDownloadMB: z.number().optional().describe('单次下载文件上限（MB）'),
    }),
  }, wrapTool(userId, async ({ userId: uid, maxConnections, qps, maxOutputMB, maxDownloadMB }) => {
    const denied = requireAdmin(authInfo);
    if (denied) return denied;
    if (!users[uid]) return { content: [{ type: 'text', text: 'Error: 用户不存在' }], isError: true };
    if (maxConnections === undefined && qps === undefined && maxOutputMB === undefined && maxDownloadMB === undefined) {
      delete users[uid].limits;
    } else {
      users[uid].limits = users[uid].limits || {};
      if (maxConnections !== undefined) users[uid].limits.maxConnections = Math.max(1, maxConnections);
      if (qps !== undefined) users[uid].limits.qps = Math.max(1, qps);
      if (maxOutputMB !== undefined) users[uid].limits.maxOutputMB = Math.max(1, maxOutputMB);
      if (maxDownloadMB !== undefined) users[uid].limits.maxDownloadMB = Math.max(1, maxDownloadMB);
    }
    saveUsers();
    return { content: [{ type: 'text', text: `用户 ${uid} 配额: ${JSON.stringify(getLimits(uid))}` }] };
  }, 'set_user_limit'));

  server.registerTool('get_user_limits', {
    description: '列出所有用户的资源配额。需要管理员密钥认证',
    inputSchema: z.object({}),
  }, wrapTool(userId, async () => {
    const denied = requireAdmin(authInfo);
    if (denied) return denied;
    const list = Object.keys(users).map(uid => `- ${uid}: ${JSON.stringify(getLimits(uid))}`).join('\n');
    return { content: [{ type: 'text', text: list || '暂无用户' }] };
  }, 'get_user_limits'));

  server.registerTool('get_audit_log', {
    description: '查看审计日志（谁、何时、调用了什么工具、参数、结果）。需要管理员密钥认证',
    inputSchema: z.object({
      limit: z.number().optional().describe('条数，默认 50'),
      userId: z.string().optional().describe('按用户过滤'),
    }),
  }, wrapTool(userId, async ({ limit, userId: filterUid }) => {
    const denied = requireAdmin(authInfo);
    if (denied) return denied;
    const rows = readAuditTail(Math.max(1, Math.min(500, limit || 50)))
      .filter(r => !filterUid || r.userId === filterUid)
      .slice(-(limit || 50));
    if (rows.length === 0) return { content: [{ type: 'text', text: '暂无审计记录' }] };
    const text = rows.map(r =>
      `[${(r.ts || '').slice(11, 19)}] ${r.userId} ${r.tool} ${JSON.stringify(r.args || {})} ${r.ok ? '✓' : '✗'} ${r.durationMs || 0}ms`
    ).join('\n');
    return { content: [{ type: 'text', text }] };
  }, 'get_audit_log'));

  // 会话管理
  const sessionTools = [
    ['create_session', '在远程电脑上创建一个新的工作会话', { workDir: z.string(), name: z.string().optional(), code: z.string().optional() }],
    ['exec', '在当前默认会话中执行 shell 命令', { command: z.string(), timeout: z.number().optional(), code: z.string().optional() }],
    ['read_file', '在当前默认会话中读取文件', { path: z.string(), code: z.string().optional() }],
    ['write_file', '在当前默认会话中写入文件', { path: z.string(), content: z.string(), code: z.string().optional() }],
    ['close_session', '关闭当前默认会话', { code: z.string().optional() }],
    ['list_sessions', '列出远程电脑上所有已创建的工作会话', { code: z.string().optional() }],
    ['switch_session', '切换到指定的会话', { sessionId: z.string(), code: z.string().optional() }],
  ];

  for (const [name, desc, schema] of sessionTools) {
    server.registerTool(name, { description: desc, inputSchema: z.object(schema) }, wrapTool(userId, async (params) => {
      const { device, error } = getAuthedDevice(sessionState, userId, undefined, params.code);
      if (!device) return { content: [{ type: 'text', text: `Error: ${error}` }], isError: true };
      // 命令/路径白名单（与旧工具保持一致，防止新工具绕过 ALLOWED_COMMANDS / ALLOWED_FILE_PREFIX）
      if (name === 'exec' && !checkCommandAllowed(params.command || '')) {
        return { content: [{ type: 'text', text: 'Error: command not allowed' }], isError: true };
      }
      if ((name === 'read_file' || name === 'write_file') && !checkPathAllowed(params.path || '')) {
        return { content: [{ type: 'text', text: 'Error: path outside allowed prefix' }], isError: true };
      }
      const op = name.replace('_session', '');
      const result = await handleSessionOp(op === 'exec' ? 'exec' : op, params, device.id);
      return formatResult(name, result, params);
    }, name));
  }

  // 旧工具
  server.registerTool('execute_command', {
    description: '[旧版] 在指定的私人电脑上执行 shell 命令',
    inputSchema: z.object({
      deviceId: z.string().optional(), command: z.string(), timeout: z.number().optional(), code: z.string().optional(),
    }),
  }, wrapTool(userId, async ({ deviceId, code, command, timeout }) => {
    const { device, error } = getAuthedDevice(sessionState, userId, deviceId, code);
    if (!device) return { content: [{ type: 'text', text: `Error: ${error}` }], isError: true };
    if (!checkCommandAllowed(command)) return { content: [{ type: 'text', text: 'Error: command not allowed' }], isError: true };
    const output = await sendAndWait('execute_command', { command, timeout }, device.id);
    const o = output.payload;
    return { content: [{ type: 'text', text: `Exit Code: ${o.exitCode}${o.stdout ? '\nSTDOUT:' + o.stdout : ''}${o.stderr ? '\nSTDERR:' + o.stderr : ''}${o.killed ? '\n[超时]' : ''}` }] };
  }, 'execute_command'));

  server.registerTool('read_file_old', {
    description: '[旧版] 读取私人电脑上的文件',
    inputSchema: z.object({ deviceId: z.string().optional(), code: z.string().optional(), path: z.string() }),
  }, wrapTool(userId, async ({ deviceId, code, path }) => {
    const { device, error } = getAuthedDevice(sessionState, userId, deviceId, code);
    if (!device) return { content: [{ type: 'text', text: `Error: ${error}` }], isError: true };
    if (!checkPathAllowed(path)) return { content: [{ type: 'text', text: 'Error: path outside allowed prefix' }], isError: true };
    const result = await sendAndWait('read_file', { path }, device.id);
    const o = result.payload;
    return { content: [{ type: 'text', text: o.success ? o.content : `Error: ${o.error}` }], isError: !o.success };
  }, 'read_file_old'));

  server.registerTool('write_file_old', {
    description: '[旧版] 写入私人电脑上的文件',
    inputSchema: z.object({ deviceId: z.string().optional(), code: z.string().optional(), path: z.string(), content: z.string() }),
  }, wrapTool(userId, async ({ deviceId, code, path, content }) => {
    const { device, error } = getAuthedDevice(sessionState, userId, deviceId, code);
    if (!device) return { content: [{ type: 'text', text: `Error: ${error}` }], isError: true };
    if (!checkPathAllowed(path)) return { content: [{ type: 'text', text: 'Error: path outside allowed prefix' }], isError: true };
    const result = await sendAndWait('write_file', { path, content }, device.id);
    const o = result.payload;
    return { content: [{ type: 'text', text: o.success ? `文件已写入: ${o.path}` : `Error: ${o.error}` }], isError: !o.success };
  }, 'write_file_old'));

  server.registerTool('get_client_messages', {
    description: '获取客户端发来的新消息（客户端 UI 发来的消息，读取后自动标记已读并回执给客户端）。建议每次会话开始时调用一次',
    inputSchema: z.object({}),
  }, wrapTool(userId, async () => {
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
  }, 'get_client_messages'));

  server.registerTool('get_device_info', {
    description: '获取远程电脑的系统信息',
    inputSchema: z.object({ code: z.string().optional() }),
  }, wrapTool(userId, async ({ code }) => {
    const { device, error } = getAuthedDevice(sessionState, userId, undefined, code);
    if (!device) return { content: [{ type: 'text', text: `Error: ${error}` }], isError: true };
    const result = await sendAndWait('get_device_info', {}, device.id);
    const info = result.payload;
    const gb = (b) => (b / 1024 / 1024 / 1024).toFixed(1) + ' GB';
    const text = [
      `Hostname: ${info.hostname}`, `Platform: ${info.platform}`, `Architecture: ${info.arch}`,
      `CPU Cores: ${info.cpus}`, `Uptime: ${(info.uptime / 3600).toFixed(1)} hours`,
      `Home Directory: ${info.homedir}`, `User: ${info.userInfo?.username || 'unknown'}`,
    ].join('\n');
    return { content: [{ type: 'text', text }] };
  }, 'get_device_info'));

  server.registerTool('notify', {
    description: '发送通知消息到客户端日志',
    inputSchema: z.object({ text: z.string(), code: z.string().optional() }),
  }, wrapTool(userId, async ({ text, code }) => {
    const { device, error } = getAuthedDevice(sessionState, userId, undefined, code);
    if (!device) return { content: [{ type: 'text', text: `Error: ${error}` }], isError: true };
    sendJSON(device.ws, { type: 'notify', text });
    return { content: [{ type: 'text', text: '已发送' }] };
  }, 'notify'));

  server.registerTool('download', {
    description: '下载远程电脑上的文件（返回 base64 编码，受该用户 maxDownloadMB 限制）',
    inputSchema: z.object({ path: z.string(), code: z.string().optional() }),
  }, wrapTool(userId, async ({ path, code }) => {
    const { device, error } = getAuthedDevice(sessionState, userId, undefined, code);
    if (!device) return { content: [{ type: 'text', text: `Error: ${error}` }], isError: true };
    if (!checkPathAllowed(path)) return { content: [{ type: 'text', text: 'Error: path outside allowed prefix' }], isError: true };
    const result = await sendAndWait('download', { path }, device.id);
    const o = result.payload;
    if (o.success) {
      const maxMB = getLimits(userId).maxDownloadMB;
      if (o.size > maxMB * 1024 * 1024) {
        return { content: [{ type: 'text', text: `Error: 文件过大（${(o.size / 1024 / 1024).toFixed(1)}MB > ${maxMB}MB 上限）` }], isError: true };
      }
      return { content: [{ type: 'text', text: `FILE:${path}|${o.size}|${o.data}` }] };
    }
    return { content: [{ type: 'text', text: `Error: ${o.error}` }], isError: true };
  }, 'download'));

  // ═══ 异步任务 ═══════════════════════════════

  server.registerTool('exec_task', {
    description: '提交异步任务（长命令在客户端后台执行，立即返回 taskId，稍后用 get_task_result 查询）。适合长任务',
    inputSchema: z.object({
      command: z.string().describe('要执行的命令'),
      timeout: z.number().optional().describe('超时（毫秒），默认 30 分钟'),
      code: z.string().optional().describe('6 位配对码（首次授权需要，已授权会话可省略）'),
    }),
  }, wrapTool(userId, async ({ command, timeout, code }) => {
    const { device, error } = getAuthedDevice(sessionState, userId, undefined, code);
    if (!device) return { content: [{ type: 'text', text: `Error: ${error}` }], isError: true };
    if (!checkCommandAllowed(command)) return { content: [{ type: 'text', text: 'Error: command not allowed' }], isError: true };
    // 并发上限：每设备同时最多 TASK_MAX_CONCURRENT 个运行中任务
    const running = Array.from(tasks.values()).filter(t => t.deviceId === device.id && t.status === 'running').length;
    if (running >= TASK_MAX_CONCURRENT) {
      return { content: [{ type: 'text', text: `Error: 该设备已有 ${running} 个任务在运行（上限 ${TASK_MAX_CONCURRENT}），请等待或使用 list_tasks 查看` }], isError: true };
    }
    const taskId = randomUUID();
    tasks.set(taskId, {
      taskId, userId, deviceId: device.id, command,
      status: 'running', exitCode: null, stdout: '', stderr: '', killed: false,
      createdAt: Date.now(), finishedAt: null, viewed: true,
    });
    sendJSON(device.ws, { type: 'task_start', requestId: randomUUID(), taskId, payload: { command, timeout: timeout || TASK_TIMEOUT } });
    scheduleActivityPush(userId);  // 任务提交，活跃度变化
    return { content: [{ type: 'text', text: `任务已提交: ${taskId}\n状态: running\n用 get_task_result 查询（taskId=${taskId}）` }] };
  }, 'exec_task'));

  server.registerTool('get_task_result', {
    description: '查询异步任务结果（按 taskId）。任务完成后结果保留约 15 分钟',
    inputSchema: z.object({
      taskId: z.string().describe('任务 ID'),
    }),
  }, wrapTool(userId, async ({ taskId }) => {
    const task = tasks.get(taskId);
    if (!task || task.userId !== userId) return { content: [{ type: 'text', text: `任务不存在或不属于你: ${taskId}` }], isError: true };
    if (task.status === 'running') {
      return { content: [{ type: 'text', text: `任务 ${taskId} 正在运行...` }] };
    }
    task.viewed = true;  // 已查看，清除"任务完成通知"
    const text = [
      `任务 ${taskId}: ${task.status}`,
      `Exit Code: ${task.exitCode}`,
      task.stdout ? `\nSTDOUT:\n${task.stdout}` : '',
      task.stderr ? `\nSTDERR:\n${task.stderr}` : '',
      task.killed ? '\n[超时被终止]' : '',
    ].join('');
    return { content: [{ type: 'text', text }] };
  }, 'get_task_result'));

  server.registerTool('list_tasks', {
    description: '列出我的所有异步任务及当前状态/完成时间（防止遗忘任务）',
    inputSchema: z.object({}),
  }, wrapTool(userId, async () => {
    const mine = Array.from(tasks.values()).filter(t => t.userId === userId);
    if (mine.length === 0) return { content: [{ type: 'text', text: '暂无任务' }] };
    const text = mine.map(t => {
      const age = t.status === 'running' ? '运行中' : (t.duration != null ? t.duration + 'ms' : '');
      const doneAt = t.finishedAt ? new Date(t.finishedAt).toLocaleTimeString() : '';
      return `- ${t.taskId.slice(0, 8)} ${t.status} ${t.command.slice(0, 50)} ${age}${doneAt ? ' 完成于 ' + doneAt : ''}`;
    }).join('\n');
    return { content: [{ type: 'text', text }] };
  }, 'list_tasks'));

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

  // 新 MCP 入口：/mcp/<动态ticket> + X-Code 配对码（不留后门，旧 /mcp 一律 404）
  const ticketMatch = url.pathname.match(/^\/mcp\/([0-9a-f]{32})$/);
  if (ticketMatch) {
    try {
      const ticketUser = findByTicket(ticketMatch[1]);
      if (!ticketUser) {
        res.writeHead(404, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: 'Not Found' }));
        return;
      }
      const userId = ticketUser.userId;
      // 配对码验证（SSE 重连免验：首次通过后缓存，后续同 ticket 免配对码）
      const code = req.headers['x-code'] || url.searchParams.get('code') || '';
      const userDevices = Array.from(devices.values()).filter(d => d.userId === userId);
      let authedDevice = null;

      // 检查缓存：同一 ticket 已通过验证，跳过配对码
      const cached = authCache.get(userId);
      if (cached && cached.ticket === ticketMatch[1] && Date.now() - cached.since < AUTH_CACHE_TTL) {
        authedDevice = userDevices.find(d => d.id === cached.deviceId) || userDevices[0] || null;
      } else if (code) {
        // 正常配对码验证
        authedDevice = userDevices.find(d => d.authCode === code) || null;
        if (authedDevice) {
          authedDevice.authFails = 0;
          authedDevice.authLockUntil = null;
          // 缓存验证成功
          authCache.set(userId, { ticket: ticketMatch[1], deviceId: authedDevice.id, since: Date.now() });
        }
      }

      if (!authedDevice) {
        const now = Date.now();
        const target = userDevices.find(d => !d.authLockUntil || now >= d.authLockUntil);
        if (target && code) {
          // 仅当提供了配对码但验证失败时才计数（SSE 重连无码不算失败）
          target.authFails = (target.authFails || 0) + 1;
          if (target.authFails >= AUTH_MAX_FAILS) {
            target.authLockUntil = now + AUTH_LOCK_MS;
            console.error(`[auth] 设备 ${target.id} 配对码失败 ${target.authFails} 次，锁定 ${Math.round(AUTH_LOCK_MS / 60000)} 分钟`);
            // 通知客户端设备被锁
            broadcastToDevices({ type: 'device_locked', deviceId: target.id, until: target.authLockUntil }, userId);
          }
        }
        res.writeHead(401, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: '无效的配对码' }));
        return;
      }
      // 用户密钥认证：带对应用户的密钥可获得身份（含管理员）；AUTH_REQUIRED=1 时强制
      let isAdminAuth = false;
      const authKey = req.headers['x-key'] || (req.headers.authorization || '').replace(/^Bearer\s+/i, '') || '';
      const authUser = authKey ? findByKey(authKey) : null;
      if (AUTH_REQUIRED) {
        if (!authUser || authUser.userId !== userId) {
          res.writeHead(401, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ error: 'authentication required' }));
          return;
        }
        isAdminAuth = authUser.role === 'admin';
      } else if (authUser && authUser.userId === userId) {
        isAdminAuth = authUser.role === 'admin';
      }
      // 连接数限制：先即时清理该用户的超时僵尸连接（防"偶尔 429"），再统计
      const now = Date.now();
      for (const [sid, entry] of transports) {
        if (entry.userId === userId && now - (entry.lastActive || now) > SSE_IDLE_TIMEOUT) {
          transports.delete(sid);
          try { entry.transport.close(); } catch {}
          broadcastToDevices({ type: 'agent_disconnected' }, userId);
        }
      }
      const conns = Array.from(transports.values()).filter(t => t.userId === userId).length;
      if (conns >= getLimits(userId).maxConnections) {
        res.writeHead(429, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: `too many connections (max ${getLimits(userId).maxConnections})` }));
        return;
      }
      const mcpServer = createMcpServer(userId, { isAdminAuth, authedDeviceId: authedDevice.id });
      const transport = new SSEServerTransport(MCP_MESSAGE_PATH, res);
      const sid = transport.sessionId;
      transports.set(sid, { server: mcpServer, transport, userId, lastActive: Date.now() });
      
      // 双保险：res close 和 req close 都触发清理
      const cleanupTransport = () => {
        if (transports.has(sid)) {
          transports.delete(sid);
          broadcastToDevices({ type: 'agent_disconnected' }, userId);
          scheduleActivityPush(userId);
        }
      };
      res.on('close', cleanupTransport);
      req.on('close', cleanupTransport);
      
      await mcpServer.connect(transport);
      let latency = 0;
      for (const device of devices.values()) {
        if (device.latency && device.userId === userId) { latency = device.latency; break; }
      }
      broadcastToDevices({ type: 'agent_connected', latency, platform: 'sandbox', hostname: 'OpenClaw Agent', relayPlatform: 'Ubuntu Linux' }, userId);
      scheduleActivityPush(userId);
    } catch (err) {
      try { res.writeHead(500).end('Internal Server Error'); } catch {}
    }
    return;
  }

  if (url.pathname === MCP_MESSAGE_PATH) {
    const sessionId = url.searchParams.get('sessionId');
    const entry = transports.get(sessionId);
    if (!sessionId || !entry) {
      res.writeHead(400).end('Missing or invalid sessionId');
      return;
    }
    entry.lastActive = Date.now();  // 刷新连接活动时间
    try { await entry.transport.handlePostMessage(req, res); } catch {}
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

// 心跳：发 ping 计延迟；若 pong 超时（默认 3 个周期 = 45s）判定假死连接，主动断开
// 心跳：仅用于计算延迟，不主动断开连接
// 客户端不主动断开则连接永不断（TCP 层面由 OS keepalive 保证）
const heartbeat = setInterval(() => {
  const now = Date.now();
  for (const [id, device] of devices) {
    if (device.ws.readyState !== WebSocket.OPEN) continue;
    device._lastPingAt = now;
    device.ws.ping();
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
      // 短断重连（<10分钟）：保留旧 ticket，MCP 地址不变
      const u = users[user.userId];
      const shortReconnect = u && u._lastDisconnectAt && (Date.now() - u._lastDisconnectAt < 600000);
      if (shortReconnect) {
        console.error(`[device] short reconnect（${Math.round((Date.now() - u._lastDisconnectAt) / 1000)}s），MCP 地址不变`);
      } else if (u && u._lastDisconnectAt) {
        // 长断：作废旧 ticket
        u.mcpTicket = null;
        saveUsers();
        console.error(`[device] long disconnect（${Math.round((Date.now() - u._lastDisconnectAt) / 1000)}s），旧 ticket 作废`);
      }
      delete u._lastDisconnectAt;
      console.error(`[device] registered: ${name} (${deviceId}) user:${user.userId} code:${authCode || 'none'}`);
      sendJSON(ws, { type: 'register_result', requestId, success: true, deviceId });
      // 重连后立即推送当前活跃状态（呼吸灯同步）
      scheduleActivityPush(user.userId);
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
    if (type === 'get_mcp_ticket') {
      // 返回当前有效的 MCP 地址 ticket（如有则复用，无则生成）
      const device = devices.get(deviceId);
      if (device) {
        const u = users[device.userId];
        let ticket = u?.mcpTicket;
        if (!ticket) {
          ticket = randomBytes(16).toString('hex');
          if (u) { u.mcpTicket = ticket; saveUsers(); }
          console.error(`[ticket] 用户 ${device.userId} 生成新 MCP 地址 ticket`);
        } else {
          console.error(`[ticket] 用户 ${device.userId} 复用已有 ticket`);
        }
        authCache.delete(device.userId);
        sendJSON(ws, { type: 'mcp_ticket', requestId, success: true, ticket });
      } else {
        sendJSON(ws, { type: 'mcp_ticket', requestId, success: false, error: 'device not registered' });
      }
      return;
    }
    if (type === 'delete_messages') {
      const device = devices.get(deviceId);
      if (device && Array.isArray(msg.ids) && msg.ids.length > 0) {
        const del = new Set(msg.ids);
        for (let i = agentMessages.length - 1; i >= 0; i--) {
          if (agentMessages[i].userId === device.userId && del.has(agentMessages[i].id)) {
            agentMessages.splice(i, 1);
          }
        }
        console.error(`[message] deleted ${msg.ids.length} messages (user:${device.userId})`);
      }
      sendJSON(ws, { type: 'delete_result', requestId, success: true });
      return;
    }
    if (type === 'edit_message') {
      const device = devices.get(deviceId);
      if (device && msg.id && typeof msg.text === 'string') {
        for (const m of agentMessages) {
          if (m.id === msg.id && m.userId === device.userId) {
            m.text = msg.text;
            console.error(`[message] edited ${msg.id}: ${msg.text.slice(0, 50)}`);
            break;
          }
        }
      }
      sendJSON(ws, { type: 'edit_result', requestId, success: true });
      return;
    }
    if (type === 'task_result') {
      // 客户端异步任务完成回传（只接受任务所属设备的回传，防篡改）
      const task = tasks.get(msg.taskId);
      if (task && task.deviceId === deviceId) {
        const p = msg.payload || {};
        task.status = p.killed ? 'killed' : (p.exitCode === 0 ? 'done' : 'failed');
        task.exitCode = p.exitCode;
        task.stdout = p.stdout || '';
        task.stderr = p.stderr || '';
        task.killed = !!p.killed;
        task.duration = p.duration || 0;
        task.finishedAt = Date.now();
        task.viewed = false;  // 标记未查看，触发"任务完成通知"
        console.error(`[task] ${task.taskId} ${task.status} (${task.duration}ms)`);
        notifyTaskDone(task.userId, task);
        scheduleActivityPush(task.userId);  // 任务完成，活跃度变化
      }
      return;
    }

    if (requestId && pendingRequests.has(requestId)) {
      const { resolve, reject, timer, deviceId: pendDeviceId } = pendingRequests.get(requestId);
      clearTimeout(timer);
      pendingRequests.delete(requestId);
      const d = devices.get(pendDeviceId);
      if (d) scheduleActivityPush(d.userId);  // 调用结束
      if (type === 'error') reject(new Error(msg.error));
      else resolve(msg);
    }
  });
  ws.on('close', () => {
    const device = devices.get(deviceId);
    if (device) {
      console.error(`[device] disconnected: ${device.name} (${deviceId})`);
      authCache.delete(device.userId);
      // 记录断开时间，用于判断短断/长断
      const u = users[device.userId];
      if (u) u._lastDisconnectAt = Date.now();
    }
    // 该设备正在运行的任务标记失败（防悬空任务永久占并发名额）
    for (const [tid, t] of tasks) {
      if (t.deviceId === deviceId && t.status === 'running') {
        t.status = 'failed';
        t.stderr = '设备已断开连接';
        t.exitCode = 1;
        t.finishedAt = Date.now();
        console.error(`[task] ${tid} failed (device disconnected)`);
      }
    }
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