#!/usr/bin/env node
/**
 * 云桥持久 MCP 客户端 — 保持 SSE 长连接，接收客户端消息并即时响应
 * 
 * 用法:
 *   node yunqiao-daemon.mjs <pairing-code>
 * 
 * 环境变量:
 *   YUNQIAO_URL  - MCP 地址（含 ticket）
 *   YUNQIAO_KEY  - 用户密钥（可选，管理员操作需要）
 */

import { Client } from '/opt/node-v24.11.1-linux-x64/lib/node_modules/mcporter/node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js';
import { SSEClientTransport } from '/opt/node-v24.11.1-linux-x64/lib/node_modules/mcporter/node_modules/@modelcontextprotocol/sdk/dist/esm/client/sse.js';

const SERVER_URL = process.env.YUNQIAO_URL;
const AUTH_CODE = process.argv[2];
const YUNQIAO_KEY = process.env.YUNQIAO_KEY || '';

if (!SERVER_URL) {
  console.error('请设置 YUNQIAO_URL 环境变量');
  process.exit(1);
}
if (!AUTH_CODE) {
  console.error('用法: node yunqiao-daemon.mjs <配对码>');
  process.exit(1);
}

const headers = {};
if (YUNQIAO_KEY) headers['X-Key'] = YUNQIAO_KEY;
if (AUTH_CODE) headers['X-Code'] = AUTH_CODE;

async function connect() {
  const transport = new SSEClientTransport(new URL(SERVER_URL), {
    requestInit: Object.keys(headers).length ? { headers } : undefined,
  });
  const client = new Client({ name: 'yunqiao-daemon', version: '1.0.0' });
  
  try {
    await client.connect(transport);
    console.log('[daemon] 已连接', SERVER_URL);
    
    // 定期检查客户端消息
    setInterval(async () => {
      try {
        const result = await client.callTool({ name: 'get_client_messages', arguments: {} });
        const text = result.content?.[0]?.text || '';
        if (text && !text.includes('暂无未读消息')) {
          console.log('[消息]', text);
          process.stdout.write('\n📬 ' + text + '\n> ');
        }
      } catch (e) {
        // 忽略轮询错误
      }
    }, 5000);
    
    // 保持连接
    process.stdin.resume();
    
  } catch (e) {
    console.error('[daemon] 连接失败:', e.message);
    setTimeout(connect, 5000);
  }
}

connect();