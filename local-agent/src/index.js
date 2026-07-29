import WebSocket from 'ws';
import { exec } from 'node:child_process';
import { readFileSync, writeFileSync, statSync } from 'node:fs';
import os from 'node:os';

const RELAY_URL = process.env.RELAY_URL || 'ws://localhost:9876/device';
const PSK = process.env.RELAY_PSK || 'change-me-to-a-secure-random-string';
const DEVICE_NAME = process.env.DEVICE_NAME || os.hostname();
const RECONNECT_DELAY = parseInt(process.env.RECONNECT_DELAY || '5000');

let reconnectTimer = null;

function connect() {
  const url = `${RELAY_URL}?psk=${encodeURIComponent(PSK)}`;
  const ws = new WebSocket(url);

  ws.on('open', () => {
    console.error(`[agent] connected to relay: ${RELAY_URL}`);
    ws.send(JSON.stringify({
      type: 'register',
      deviceName: DEVICE_NAME,
      os: os.platform(),
      arch: os.arch(),
      hostname: os.hostname(),
    }));
  });

  ws.on('message', (raw) => {
    let msg;
    try { msg = JSON.parse(raw.toString()); } catch { return; }

    const { type, requestId } = msg;

    if (type === 'register_result' && msg.success) {
      console.error(`[agent] registered as device: ${msg.deviceId}`);
      return;
    }

    handleCommand(ws, type, requestId, msg.payload || {});
  });

  ws.on('close', () => {
    console.error(`[agent] disconnected, reconnecting in ${RECONNECT_DELAY}ms`);
    scheduleReconnect();
  });

  ws.on('error', (err) => {
    console.error('[agent] ws error:', err.message);
  });
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, RECONNECT_DELAY);
}

function handleCommand(ws, type, requestId, payload) {
  if (type === 'execute_command') {
    const { command, timeout = 30000 } = payload;
    exec(command, { timeout, maxBuffer: 10 * 1024 * 1024 }, (err, stdout, stderr) => {
      ws.send(JSON.stringify({
        type: 'command_result', requestId,
        payload: {
          exitCode: err ? (err.code || 1) : 0,
          stdout: stdout || '',
          stderr: stderr || '',
          killed: err?.killed || false,
        },
      }));
    });
    return;
  }

  if (type === 'read_file') {
    const { path } = payload;
    try {
      const content = readFileSync(path, 'utf-8');
      const stat = statSync(path);
      ws.send(JSON.stringify({
        type: 'file_result', requestId,
        payload: { success: true, content, size: stat.size, path },
      }));
    } catch (err) {
      ws.send(JSON.stringify({
        type: 'file_result', requestId,
        payload: { success: false, error: err.message, path },
      }));
    }
    return;
  }

  if (type === 'write_file') {
    const { path, content } = payload;
    try {
      writeFileSync(path, content, 'utf-8');
      ws.send(JSON.stringify({
        type: 'file_result', requestId,
        payload: { success: true, path },
      }));
    } catch (err) {
      ws.send(JSON.stringify({
        type: 'file_result', requestId,
        payload: { success: false, error: err.message, path },
      }));
    }
    return;
  }

  if (type === 'get_device_info') {
    ws.send(JSON.stringify({
      type: 'device_info', requestId,
      payload: {
        hostname: os.hostname(),
        platform: os.platform(),
        arch: os.arch(),
        cpus: os.cpus().length,
        totalMem: os.totalmem(),
        freeMem: os.freemem(),
        uptime: os.uptime(),
        homedir: os.homedir(),
        userInfo: os.userInfo(),
      },
    }));
    return;
  }

  ws.send(JSON.stringify({
    type: 'error', requestId,
    error: `unknown command type: ${type}`,
  }));
}

console.error(`[agent] starting, device name: ${DEVICE_NAME}`);
connect();