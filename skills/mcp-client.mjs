#!/usr/bin/env node
/**
 * 云端协同 MCP 客户端
 * 绕过 mcporter 的 SSE 兼容性问题，直接用 MCP SDK 连接
 * 
 * 用法:
 *   node mcp-client.mjs list                        # 列出工具
 *   node mcp-client.mjs call <tool> <json-args>     # 调用工具
 * 
 * 示例:
 *   node mcp-client.mjs list
 *   node mcp-client.mjs call list_devices '{}'
 *   node mcp-client.mjs call get_device_info '{"deviceId":"xxx"}'
 */

// 可移植加载 MCP SDK：优先标准 npm 包，回退到 mcporter 全局安装路径
let Client, SSEClientTransport;
async function loadSdk() {
  try {
    ({ Client } = await import('@modelcontextprotocol/sdk/client/index.js'));
    ({ SSEClientTransport } = await import('@modelcontextprotocol/sdk/client/sse.js'));
  } catch {
    try {
      const base = '/opt/node-v24.11.1-linux-x64/lib/node_modules/mcporter/node_modules/@modelcontextprotocol/sdk/dist/esm';
      ({ Client } = await import(base + '/client/index.js'));
      ({ SSEClientTransport } = await import(base + '/client/sse.js'));
    } catch (e) {
      console.error('无法加载 @modelcontextprotocol/sdk，请安装: npm install @modelcontextprotocol/sdk');
      process.exit(1);
    }
  }
}

// 从全局环境变量读取验证码
const AUTH_CODE = process.env.MCP_AUTH_CODE || '';
const SERVER_URL = process.env.MCP_SERVER_URL || 'https://yunqiao.very.im/mcp';

async function main() {
  await loadSdk();
  const args = process.argv.slice(2);
  // 支持两种格式:
  //   node mcp-client.mjs <配对码> list
  //   node mcp-client.mjs list
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
  node mcp-client.mjs <配对码> list                   # 列出工具
  node mcp-client.mjs <配对码> call <工具名> <JSON>    # 调用工具
  node mcp-client.mjs list                            # 列出工具（用环境变量 MCP_AUTH_CODE）

示例:
  node mcp-client.mjs 880083 list
  node mcp-client.mjs 880083 call execute_command '{"deviceId":"xxx","command":"dir"}'
`);
    process.exit(0);
  }

  // 用 CLI 传入的 authCode 覆盖环境变量
  const code = authCode;

  const transport = new SSEClientTransport(new URL(SERVER_URL));
  const client = new Client({ name: 'cloud-mcp-client', version: '1.0.0' });

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
    } else {
      console.error(`未知操作: ${action}（可用: list, call）`);
      process.exit(1);
    }

    await client.close();
  } catch (err) {
    console.error('MCP 错误:', err.message);
    process.exit(1);
  }
}

main();