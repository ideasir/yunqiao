# 云桥 MCP · Yunqiao MCP

> 一座桥，把你的电脑连到 AI 手里。  
> 通过公网中转，像调用 API 一样远程控制你的电脑。

<p align="center">
  <code>AI Agent ⇄ MCP/SSE ⇄ 中转服务器 ⇄ WSS ⇄ 你的电脑</code>
</p>

---

## 特性

- **会话管理 (v2.0)** — 持久化工作区，隔离不同项目，cwd 保持
- **安全可靠** — PSK + 配对码双验证，命令/文件白名单
- **轻量路由** — 中转服务器无状态，挂了换个地址继续用
- **兼容旧版** — 旧工具保留，新工具更简洁

---

## 目录

- [快速开始](#快速开始)
- [架构](#架构)
- [部署](#部署)
  - [1. 中转服务器](#1-中转服务器)
  - [2. 客户机代理](#2-客户机代理)
  - [3. 智能体 Skill](#3-智能体-skill)
- [工具参考](#工具参考)
- [会话管理](#会话管理)
- [安全](#安全)
- [开发](#开发)

---

## 快速开始

### 30 秒跑通

**1. 在中转服务器上**

```bash
# 已部署到 yunqiao.very.im，直接用
```

**2. 在你电脑上**

```bash
cd client
pip install -r requirements.txt
python main.py
```

打开设置（⚙），填入中继地址和 PSK，点击「保存并连接」。

**3. 在 AI Agent 端**

```bash
node skills/mcp-client.mjs <配对码> list
```

看到设备列表，搞定。

---

## 架构

```
┌──────────────────────────────────────────────────────────────┐
│                    AI Agent (沙箱)                            │
│  工具: create_session, exec, read_file, write_file, ...      │
└──────────────────────┬───────────────────────────────────────┘
                       │ MCP/SSE (短连接)
                       ▼
┌──────────────────────────────────────────────────────────────┐
│              中转服务器 (yunqiao.very.im)                     │
│  - MCP 端点: /mcp (SSE)                                      │
│  - 客户端: /device (WSS)                                     │
│  - 无状态，只做路由转发                                       │
└──────────────────────┬───────────────────────────────────────┘
                       │ WSS (长连接)
                       ▼
┌──────────────────────────────────────────────────────────────┐
│              客户机 (你的 Windows/Linux/Mac)                   │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ 会话管理器 (Session Manager)                          │    │
│  │  ├─ 工作区 A: C:\project_a                           │    │
│  │  ├─ 工作区 B: D:\project_b                           │    │
│  │  └─ (当前默认，智能体操作自动指向此会话)               │    │
│  │                                                      │    │
│  │ 持久化: ~/.yunqiao/sessions/001.json                 │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

---

## 部署

### 1. 中转服务器

部署到一台有公网 IP 的 VPS 上。

```bash
cd relay
npm install

# 生成 PSK（首次启动自动生成）
export PORT=9876
node server.js
```

配合 Nginx + Let's Encrypt 配置 HTTPS：

```nginx
# /etc/nginx/sites-available/yunqiao
server {
    listen 443 ssl;
    server_name yunqiao.very.im;

    ssl_certificate /etc/letsencrypt/live/yunqiao.very.im/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yunqiao.very.im/privkey.pem;

    # MCP 端点（SSE）
    location /mcp {
        proxy_pass http://127.0.0.1:9876;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_buffering off;
    }

    # WebSocket 设备通道
    location /device {
        proxy_pass http://127.0.0.1:9876;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400s;
    }
}
```

> **中转地址:** `wss://yunqiao.very.im/device`

### 2. 客户机代理

在你的电脑上运行：

```bash
cd client
pip install -r requirements.txt
python main.py
```

#### 无头模式（无 GUI）

```bash
set RELAY_PSK=your-psk
set RELAY_URL=wss://yunqiao.very.im/device
python agent.py
```

#### 开机自启（Windows）

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "云桥MCP" main.py
```

### 3. 智能体 Skill

AI Agent 使用 MCP 客户端连接：

```bash
# 列出工具
node skills/mcp-client.mjs <配对码> list

# 列出设备
node skills/mcp-client.mjs <配对码> call list_devices '{}'

# 创建会话（推荐使用新工具）
node skills/mcp-client.mjs <配对码> call create_session '{"workDir":"D:\\project"}'

# 在会话中执行命令
node skills/mcp-client.mjs <配对码> call exec '{"command":"git status"}'

# 读取文件（相对路径基于会话 cwd）
node skills/mcp-client.mjs <配对码> call read_file '{"path":"src/main.rs"}'

# 写入文件
node skills/mcp-client.mjs <配对码> call write_file '{"path":"src/main.rs","content":"..."}'
```

设置环境变量自动带配对码：

```bash
export MCP_AUTH_CODE=984979
node skills/mcp-client.mjs list
```

---

## 工具参考

### 新版工具（推荐，v2.0）

| 工具 | 说明 | 参数 |
|------|------|------|
| `create_session` | 创建新会话并设为当前默认 | workDir, name?, code |
| `exec` | 在当前会话执行命令（保持 cwd） | command, timeout?, code |
| `read_file` | 读文件（相对路径基于会话 cwd） | path, code |
| `write_file` | 写文件（相对路径基于会话 cwd） | path, content, code |
| `close_session` | 关闭当前会话 | code |
| `list_sessions` | 列出所有会话 | code |
| `switch_session` | 切换到指定会话 | sessionId, code |
| `get_device_info` | 获取系统信息 | code |

### 旧版工具（兼容）

| 工具 | 说明 |
|------|------|
| `list_devices` | 列出设备 |
| `execute_command` | 执行命令（需传 deviceId + 绝对路径） |
| `read_file_old` | 读文件（需传绝对路径） |
| `write_file_old` | 写文件（需传绝对路径） |

---

## 会话管理（v2.0 新特性）

### 什么是会话

会话（Session）是一个**持久化的工作区**，智能体在同一会话中执行的命令**共享工作目录和环境变量**。

### 会话文件

保存在 `~/.yunqiao/sessions/` 目录下：

```json
// ~/.yunqiao/sessions/001.json
{
  "id": "001",
  "name": "openclaw 项目",
  "workDir": "D:\\aicodework\\github\\openclaw",
  "cwd": "D:\\aicodework\\github\\openclaw\\src",
  "createdAt": "2026-07-31T10:00:00",
  "lastActive": "2026-07-31T10:18:00"
}
```

### 会话管理原则

- **一次创建，持续使用** — 会话创建后一直存在，直到主动关闭
- **智能体零感知** — 智能体只操作当前默认会话，不需要传会话名
- **用户通过 UI 切换** — 在客户端界面上选择哪个会话是当前默认
- **中转服务器挂了不影响** — 会话状态在客户端，换个中转地址继续用
- **客户端重启可恢复** — 会话文件持久化到磁盘，重启后自动加载

### 典型工作流

```
# 创建会话（项目 A）
智能体: create_session("D:\project_a")
客户端: 创建会话，设为默认

# 操作项目 A
智能体: exec("dir /b")          → 在 D:\project_a 下执行
智能体: read_file("src/main.rs") → 相对路径自动解析

# 用户切换会话（UI 上操作）
用户: 点"新建会话"，选 D:\project_b
客户端: 项目 B 变成当前默认

# 智能体自动在新的默认会话操作
智能体: exec("npm test")        → 在 D:\project_b 下执行
智能体: read_file("package.json") → 相对路径指向 D:\project_b
```

---

## 安全

| 安全措施 | 说明 |
|---------|------|
| **PSK 密钥** | 中转服务器与客户端之间预共享密钥，通过 Header 传递 |
| **配对码** | 6 位数字验证码，客户端 UI 显示，智能体调用时需传入 |
| **命令白名单** | 可限制允许执行的命令（如 `ALLOWED_COMMANDS=git,node,npm`） |
| **文件路径白名单** | 可限制文件读写范围（如 `ALLOWED_FILE_PREFIX=D:\project`） |
| **设备白名单** | 可限制允许连接的设备名称 |
| **HTTPS/WSS** | 所有通信均加密传输 |

---

## 开发

### 项目结构

```
yunqiao-mcp/
├── relay/
│   ├── server.js        # 中转服务器 (Node.js, HTTP + WSS + MCP/SSE)
│   └── package.json
├── client/
│   ├── main.py          # 桌面客户端（tkinter 版，带 UI）
│   ├── agent.py         # 无头客户端代理（带会话管理）
│   ├── requirements.txt
│   ├── ui.html          # HTML 前端 (pywebview)
│   └── desktop_monitor.py
├── skills/
│   ├── mcp-client.mjs   # MCP 客户端（智能体端）
│   └── references/
└── README.md
```

### 快速启动

```bash
# 中转服务器
cd relay && npm install && node server.js

# 客户端（无头版）
cd client && pip install -r requirements.txt && python agent.py

# 验证连接
node skills/mcp-client.mjs list
```

---

<p align="center">
  <sub>MIT License · 开源 · 仅供学习使用</sub>
</p>