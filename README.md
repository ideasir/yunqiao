# 云桥 MCP · Yunqiao MCP

> 通过公网中转，远程控制你的电脑 — 像调用 API 一样简单。

---

## 目录结构

```
yunqiao-mcp/
├── relay/          ← 中转服务器（部署在 VPS）
├── client/         ← 客户机代理（部署在本地电脑）
├── skills/         ← 智能体 Skill（给 AI Agent 用）
└── README.md
```

---

## 快速部署

### 1️⃣ 中转服务器 → `relay/`

部署到一台有公网 IP 的 VPS 上。

```bash
cd relay
npm install

# 配置密钥
export RELAY_PSK="your-secure-random-key"

# 启动（建议用 Nginx 反代 HTTPS）
node server.js
```

建议配合 Nginx + Let's Encrypt 配置 HTTPS，WebSocket 路径为 `/device`。

### 2️⃣ 客户机代理 → `client/`

在你需要远程控制的电脑上运行。

```bash
cd client
pip install websockets

# 连接中转服务器
set RELAY_PSK=your-secure-random-key
set RELAY_URL=wss://your-domain.com/device
python desktop_monitor.py
```

- `desktop_monitor.py` — 桌面监控面板（推荐）
- `agent.py` — 轻量后台版（无界面）

### 3️⃣ 智能体 Skill → `skills/`

给 AI Agent（OpenClaw / Codex 等）使用的 MCP 客户端。

```bash
node skills/mcp-client.mjs list
node skills/mcp-client.mjs call list_devices '{}'
```

---

## 可用工具

| 工具 | 说明 |
|------|------|
| `list_devices` | 列出已连接的设备 |
| `execute_command` | 远程执行命令 |
| `read_file` | 读取文件 |
| `write_file` | 写入文件 |
| `get_device_info` | 获取系统信息 |

---

## 安全

- PSK 预共享密钥认证
- 支持命令白名单（`ALLOWED_COMMANDS`）
- 支持文件路径白名单（`ALLOWED_FILE_PREFIX`）
- 建议使用 HTTPS/WSS 加密通信

---

MIT License · 开源 · 自由使用
