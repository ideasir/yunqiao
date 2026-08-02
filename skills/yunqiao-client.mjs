#!/usr/bin/env node
/**
 * 云端协同 MCP 客户端
 * 绕过 mcporter 的 SSE 兼容性问题，直接用 MCP SDK 连接
 * 
 * 用法:
 *   node yunqiao-client.mjs list                        # 列出工具
 *   node yunqiao-client.mjs call <tool> <json-args>     # 调用工具
 * 
 * 示例:
 *   node yunqiao-client.mjs list
 *   node yunqiao-client.mjs call list_devices '{}'
 *   node yunqiao-client.mjs call get_device_info '{"deviceId":"xxx"}'
 */

import { Client } from '/opt/node-v24.11.1-linux-x64/lib/node_modules/mcporter/node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js';
import { SSEClientTransport } from '/opt/node-v24.11.1-linux-x64/lib/node_modules/mcporter/node_modules/@modelcontextprotocol/sdk/dist/esm/client/sse.js';

// 从全局环境变量读取验证码
const AUTH_CODE = process.env.YUNQIAO_CODE || '';
const SERVER_URL = process.env.YUNQIAO_URL;
if (!SERVER_URL) {
  console.error('请设置 YUNQIAO_URL 环境变量（云桥 MCP 端点地址）');
  process.exit(1);
}
// 可选：用户密钥（有权限时传给 MCP 端点，获得用户身份；管理员密钥可获得管理权限）
const YUNQIAO_KEY = process.env.YUNQIAO_KEY || '';

async function main() {
  const args = process.argv.slice(2);
  // 支持两种格式:
  //   node yunqiao-client.mjs <配对码> list
  //   node yunqiao-client.mjs list
  let authCode = AUTH_CODE;
  let action, toolName, argsStr;
  if (args.length >= 2 && /^\d{6}$/.test(args[0])) {
    // 第一个参数是配对码
    authCode = args[0];
    [action, toolName, ...argsStr] = args.slice(1);
    argsStr = argsStr ? argsStr.join(' ') : '';
  } else {
    [action, toolName, argsStr] = args;
  }

  if (!action) {
    console.log(`用法:
  node yunqiao-client.mjs <配对码> list                   # 列出工具
  node yunqiao-client.mjs <配对码> messages              # 读取客户端发来的消息（读后标记已读）
  node yunqiao-client.mjs <配对码> call <工具名> <JSON>    # 调用工具
  node yunqiao-client.mjs list                            # 列出工具（用环境变量 YUNQIAO_CODE）

示例:
  node yunqiao-client.mjs 880083 list
  node yunqiao-client.mjs 880083 messages
  node yunqiao-client.mjs 880083 call execute_command '{"deviceId":"xxx","command":"dir"}'
`);
    process.exit(0);
  }

  // 用 CLI 传入的 authCode 覆盖环境变量
  const code = authCode;

  // 连接时带认证：用户密钥（X-Key）+ 配对码（X-Code），由服务器验证
  const headers = {};
  if (YUNQIAO_KEY) headers['X-Key'] = YUNQIAO_KEY;
  if (code) headers['X-Code'] = code;
  const transport = new SSEClientTransport(new URL(SERVER_URL), {
    requestInit: Object.keys(headers).length ? { headers } : undefined,
  });
  const client = new Client({ name: 'cloud-yunqiao-client', version: '1.0.0' });

  try {
    await client.connect(transport);

    if (action === 'list') {
      const result = await client.listTools();
      console.log('可用工具:');
      for (const tool of result.tools) {
        console.log(`  ${tool.name}`);
        if (tool.description) console.log(`    描述: ${tool.description}`);
        if (tool.inputSchema?.properties) {
          const props = Object.entries(tool.inputSchema.properties)
            .map(([k, v]) => `    ${k}: ${v.description || v.type || 'any'}`)
            .join('\n');
          if (props) console.log(`  参数:\n${props}`);
        }
      }
    } else if (action === 'call') {
      if (!toolName) {
        console.error('请指定工具名');
        process.exit(1);
      }
      let args = {};
      if (argsStr) {
        try { args = JSON.parse(argsStr); } catch {
          console.error('参数必须是有效的 JSON');
          process.exit(1);
        }
      }
      // 自动注入验证码（优先用 CLI 传入的，再用环境变量）
      if (code && !args.code) {
        args.code = code;
      }
      const result = await client.callTool({ name: toolName, arguments: args });
      for (const content of result.content) {
        if (content.type === 'text') {
          if (result.isError) {
            console.error('错误:', content.text);
          } else {
            console.log(content.text);
          }
        }
      }
    } else if (action === 'messages') {
      // 读取客户端发来的消息（读取后自动标记已读并回执给客户端）
      const result = await client.callTool({ name: 'get_client_messages', arguments: {} });
      for (const content of result.content) {
        if (content.type === 'text') {
          if (result.isError) {
            console.error('错误:', content.text);
          } else {
            console.log(content.text);
          }
        }
      }
    } else if (action === 'listen') {
      // 常驻模式：保持 SSE 连接，接收客户端实时消息
      console.log('[listen] 已连接，等待客户端消息...');
      process.stdin.resume();
      // 直接监听底层 EventSource（MCP SDK 可能不转发 onmessage）
      if (transport._eventSource) {
        transport._eventSource.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data);
            if (msg.method === 'notifications/message') {
              const { text, urgent, deviceName } = msg.params || {};
              console.log('\n' + (urgent ? '⚠️ [紧急] ' : '📩 ') + (deviceName || '') + ': ' + text);
            }
          } catch {}
        };
      }
      // 30 秒保活
      setInterval(async () => { try { await client.ping(); } catch {} }, 30000);
      return;
    } else {
      console.error('未知操作: ' + action + '（可用: list, call, messages, listen）');
      process.exit(1);
    }

    await client.close();
  } catch (err) {
    console.error('MCP 错误:', err.message);
    process.exit(1);
  }
}

main();