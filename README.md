# 云端协同 MCP 配置指南

## 架构

```
OpenClaw(沙箱) --MCP HTTP--> 公网服务器(MCP+Relay) <--WebSocket--> Windows(本地代理)
```

- **沙箱↔服务器**: 短链接，MCP Streamable HTTP 协议，每调用一次一个请求
- **服务器↔Windows**: 长链接，WebSocket 持久双向通道

## 部署

### 1. 公网服务器（香港 VPS）

```bash
cd cloud-collaborative-mcp
npm install
export RELAY_PSK="your-secure-psk"
export PORT=9876
export ALLOWED_COMMANDS=node,python,git,powershell,cmd
export ALLOWED_FILE_PREFIX=D:\aicodework\github
npm run server
```

### 2. Windows 本地代理

```bash
cd cloud-collaborative-mcp
npm install
set RELAY_PSK=your-secure-psk
set RELAY_URL=ws://45.152.65.49:9876/device
set DEVICE_NAME=my-computer
npm run agent
```

### 3. OpenClaw MCP 配置

在 `openclaw.json` 中添加：

```json
{
  "mcpServers": {
    "cloud-collaborative": {
      "url": "http://45.152.65.49:9876/mcp",
      "transport": "streamable-http"
    }
  }
}
```

## 可用工具

| 工具 | 说明 |
|------|------|
| `list_devices` | 列出已连接的设备 |
| `execute_command` | 远程执行命令 |
| `read_file` | 读取文件 |
| `write_file` | 写入文件 |
| `get_device_info` | 获取系统信息 |

## 安全

- 通过 `ALLOWED_COMMANDS` 限制可执行的命令
- 通过 `ALLOWED_FILE_PREFIX` 限制文件读写路径
- 通过 `RELAY_PSK` 验证设备连接
- 建议生产环境使用 WSS + 443 端口 + TLS 证书