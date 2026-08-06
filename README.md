# 云桥 MCP · Yunqiao MCP

> 一座桥，把你的电脑连到 AI 手里。  
> 通过公网中转，像调用 API 一样远程控制你的电脑。

<p align="center">
  <code>AI Agent ⇄ MCP/SSE ⇄ 中转服务器 ⇄ WSS ⇄ 你的电脑</code>
</p>

---

## 目录

- [快速开始](#快速开始)
- [架构](#架构)
- [部署](#部署)
  - [1. 中转服务器](#1-中转服务器)
  - [2. 客户机代理](#2-客户机代理)
  - [3. 智能体 Skill](#3-智能体-skill)
- [工具参考](#工具参考)
- [安全](#安全)
- [开发](#开发)

---

## 快速开始

### 30 秒跑通

**1. 在中转服务器上**

```bash
# 已部署到 your-server.com，直接用
```

**2. 在你电脑上**

```bash
cd client
pip install -r requirements.txt
python desktop.py
```

> 也可以在项目根目录直接 `python desktop.py`（根目录的入口会自动转发到 client/desktop.py，避免找不到文件）

打开设置（⚙），填入中继地址和 密钥，点击「保存并连接」。

**3. 在 AI Agent 端**

```bash
node skills/yunqiao-client.mjs <配对码> list
```

看到设备列表，搞定。

---

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                     AI Agent (沙箱)                       │
│  node skills/yunqiao-client.mjs <配对码> call <工具> <参数>  │
└──────────────┬──────────────────────────────────────────┘
               │ MCP/SSE (HTTPS)
               ▼
┌─────────────────────────────────────────────────────────┐
│                  中转服务器 (VPS)                         │
│  your-server.com:443                                     │
│  ├─ /mcp    → MCP Server (SSE)                           │
│  └─ /device → WebSocket Relay (WSS)                      │
└──────────────┬──────────────────────────────────────────┘
               │ WebSocket (WSS)
               ▼
┌─────────────────────────────────────────────────────────┐
│                  你的电脑 (Windows/Linux/macOS)           │
│  python desktop.py                                          │
│  ┌─────────────────────────────────────────────────┐     │
│  │ 配对码: 984979                                   │     │
│  │ [📋 复制并发送给 Agent]                          │     │
│  └─────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────┘
```

### 工作流程

1. **客户机**启动 → 连接中继服务器 → 生成 **6 位配对码**
2. **把配对码发给 AI Agent**（复制按钮自动复制 `云桥 配对码 984979`）
3. **AI Agent** 用配对码通过 MCP 协议连接中继 → 找到你的设备
4. **配对完成** → Agent 可以远程执行命令、读写文件、查系统信息

---

## 部署

### 1. 中转服务器

部署到一台有公网 IP 的 VPS 上。

> ⚠️ **首次启动必须设置 `RELAY_KEY` 环境变量**作为管理员密钥，否则服务器拒绝启动（已移除默认密钥，防止未配置的服务器用全网已知密码裸跑）。后续重启无需再设（密钥已持久化到 `.users.json`）。

```bash
cd relay
npm install

# 自用模式（最简单）
export PORT=9876
export RELAY_KEY=请改成你自己的强密钥    # admin 管理员密钥（首次启动必须设置，无默认值）
node server.js
```

#### 多用户公测推荐配置

适用：1 核 / 1GB 服务器，约 100 用户小范围公测。这套值"用得宽绰、不卡正常使用，也不让服务器紧张"：

```bash
export PORT=9876
export USERS_FILE=/opt/cloud-mcp/.users.json   # 用户数据文件
export RELAY_KEY=请改成你自己的管理员密钥        # admin 用户的密钥

# 认证：公测必开，强制所有 Agent 带用户密钥连接，?user= 冒充失效
export AUTH_REQUIRED=1

# 每用户配额（宽绰但可控）
export MAX_CONNECTIONS=10     # 每用户最大并发 MCP 连接（Agent 多会话/多连接时更宽松）
export DEFAULT_QPS=5          # 每用户每秒工具调用上限（正常 Agent 不到 1 QPS）
export MAX_OUTPUT_MB=5        # 单次工具返回上限（命令输出一般 <1MB）
export MAX_DOWNLOAD_MB=5      # 单次下载文件上限（MB）

# 配对码防暴力
export AUTH_MAX_FAILS=5       # 连续失败 5 次
export AUTH_LOCK_MS=300000    # 锁定 5 分钟

# 异步任务（长命令，不怕 Agent 掉线）
export TASK_TIMEOUT=1800000       # 单个任务运行超时（默认 30 分钟）
export TASK_RESULT_TTL=900000     # 结果保留时长（默认 15 分钟，超时自动清理）
export TASK_MAX_CONCURRENT=3      # 每设备同时运行的任务数上限

# 审计日志（追责用，记录谁/何时/调用了什么/结果）
export AUDIT_LOG=/opt/cloud-mcp/audit.log
export AUDIT_MAX_BYTES=20971520   # 超过 20MB 自动轮转为 .old

# 可选：命令/路径白名单
# export ALLOWED_COMMANDS=npm,python,git,dir
# export ALLOWED_FILE_PREFIX=/srv/workspace

node server.js
```

**注意**：开启 `AUTH_REQUIRED=1` 后，Agent 端（`yunqiao-client.mjs`）必须配置用户密钥：

```bash
export YUNQIAO_URL=https://your-server.com/mcp
export YUNQIAO_KEY=某个用户的密钥
```

#### 每用户配额管理（放开 / 收紧）

管理员可单独调整任意用户的配额，无需重启：

```bash
# 收紧（发现某个用户在刷）
yq 123456 call set_user_limit '{"userId":"user-a","qps":2,"maxConnections":1}'

# 放开
yq 123456 call set_user_limit '{"userId":"user-a","qps":20,"maxConnections":5}'

# 恢复默认（不带限制参数）
yq 123456 call set_user_limit '{"userId":"user-a"}'

# 查看所有用户配额
yq 123456 call get_user_limits '{}'

# 审计日志（追责：谁在何时调用了什么）
yq 123456 call get_audit_log '{}'                              # 最近 50 条
yq 123456 call get_audit_log '{"userId":"user-a","limit":20}'   # 按用户过滤
```

> 审计日志落盘在 `AUDIT_LOG` 指定文件（JSON Lines 格式，每行一条），只记录工具名、关键参数（命令全文/文件路径/大小）、结果与耗时，**不记录配对码和文件内容**。

> 配额解读：`maxConnections=10` = 该用户最多同时保持 10 个 MCP 连接（宽松，覆盖多会话/多 Agent）；`qps=5` = 每秒最多 5 次工具调用；`maxOutputMB=5` = 单次返回（命令输出）超过 5MB 被拒绝；`maxDownloadMB=5` = 单次下载文件超过 5MB 被拒绝。默认值已兼顾"不限制正常使用"与"不压垮 1G 服务器"。

建议用 Nginx 反代 + Let's Encrypt 配置 HTTPS：

```nginx
# /etc/nginx/sites-available/yunqiao
server {
    listen 443 ssl;
    server_name your-server.com;

    ssl_certificate /etc/letsencrypt/live/your-server.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-server.com/privkey.pem;

    # MCP 端点（SSE）
    location /mcp {
        proxy_pass http://127.0.0.1:9876;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_buffering off;
    }

    # WebSocket 中继
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

> **生产环境已部署**：`wss://your-server.com/device`

---

### 2. 客户机代理

在你需要远程控制的电脑上运行。

#### 桌面客户端（推荐）

```bash
cd client
pip install -r requirements.txt
python desktop.py
```

> 也可以在项目根目录直接 `python desktop.py`（根目录的入口会自动转发到 client/desktop.py，避免找不到文件）

打开后会看到一个窗口：

| 区域 | 说明 |
|------|------|
| **连接状态** | 顶部状态灯 + 延迟显示 |
| **当前连接** | 本地客户端 → 中转服务器 → 上游 Agent 拓扑 |
| **Agent 活动** | 工具网格（执行中的工具绿色闪烁，完成的变灰） |
| **配对码** | 6 位数字，右键复制 |
| **日志** | 实时显示连接和操作日志 |

**首次使用**：点击 ⚙ 设置，填入中继地址和 密钥。

#### 轻量后台版（无界面）

```bash
set RELAY_密钥=your-密钥
set RELAY_URL=wss://your-server.com/device
python agent.py
```

#### 打包 exe

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "云桥MCP" desktop.py
```

---

### 3. 智能体 Skill

给 AI Agent 使用的 MCP 客户端。

```bash
# 列出工具
node skills/yunqiao-client.mjs <配对码> list

# 列出设备
node skills/yunqiao-client.mjs <配对码> call list_devices '{}'

# 远程执行命令
node skills/yunqiao-client.mjs <配对码> call execute_command '{"deviceId":"xxx","command":"dir"}'

# 读取文件
node skills/yunqiao-client.mjs <配对码> call read_file '{"deviceId":"xxx","path":"C:\\path\\to\\file.txt"}'

# 写入文件
node skills/yunqiao-client.mjs <配对码> call write_file '{"deviceId":"xxx","path":"C:\\path\\to\\file.txt","content":"hello"}'

# 获取系统信息
node skills/yunqiao-client.mjs <配对码> call get_device_info '{"deviceId":"xxx"}'
```

也支持通过环境变量设置配对码：

```bash
export MCP_AUTH_CODE=984979
node skills/yunqiao-client.mjs list
```

---

## 工具参考

### MCP 工具

| 工具 | 说明 | 参数 |
|------|------|------|
| `list_devices` | 列出所有已连接设备（含配对状态） | 无 |
| `execute_command` | 远程执行 shell 命令 | deviceId, command, timeout? |
| `exec_script` | **亲和通道**：执行多行脚本（写成临时文件再执行，彻底避开转义）。语言 python/powershell/node/bash/cmd/auto | code/script, language?, cwd?, timeout? |
| `get_environment` | **亲和通道**：获取环境档案（可用解释器、工具、工作区） | code? |
| `read_file` | 读取远程文件 | deviceId, path |
| `write_file` | 写入远程文件 | deviceId, path, content |
| `get_device_info` | 获取系统信息（OS、CPU、内存等） | deviceId |
| `get_client_messages` | 读取客户端发来的消息（读取后自动标记已读并回执给客户端） | 无 |
| `exec_task` | 提交异步任务（长命令后台执行，立即返回 taskId，适合 >60 秒的长任务） | command, timeout? |
| `get_task_result` | 查询异步任务结果（按 taskId，结果保留约 15 分钟） | taskId |
| `list_tasks` | 列出我的所有异步任务及状态（防止遗忘任务） | 无 |

### 配对码

- 6 位随机数字
- 客户机断开后自动失效
- 支持重新生成
- **会话级授权**：Agent 首次带配对码调用工具后，该 SSE 会话内后续调用可省略配对码（换连接需重新授权）
- 复制按钮自动生成 `云桥 配对码 xxxxxx` 格式，方便直接发给 AI Agent

### 动态 MCP 地址（安全接入，无后门）

- 每个用户的 MCP 端点使用**动态地址**：`https://server/mcp/<ticket>`
- 点击"复制配对码"会**同时向服务器申请新的动态地址**（旧地址立即作废，只有最新有效）
- Agent 连接必须：`/mcp/<ticket>` + 请求头 `X-Code: <配对码>`，两者都通过服务器验证才建立会话
- 旧路径 `/mcp` 已关闭（不留后门）
- `yunqiao-client.mjs` 已支持：`YUNQIAO_URL=https://server/mcp/<ticket>` + `YUNQIAO_CODE=<配对码>`

---

### 亲和通道（AI 友好）

云桥提供面向 AI Agent 的**亲和通道**，让 Agent 在远程电脑上像在家一样顺手。核心原则：**算力全在沙箱侧，客户端只做原语**。

**1. 脚本执行（`exec_script`）**——把代码写成临时脚本文件执行，**彻底避开字符串转义**。适合多行逻辑、管道、引号嵌套：

```bash
# 方式一：CLI 传参（单行代码）
node skills/yunqiao-client.mjs <配对码> script '{"language":"python","script":"print(1+1)"}'

# 方式二：stdin 传多行代码（推荐，无需转义）
cat <<'EOF' | node skills/yunqiao-client.mjs <配对码> script '{"language":"bash"}'
for f in src/*.rs; do echo "$f: $(wc -l < "$f")"; done
EOF
```

支持语言：`python`/`powershell`/`node`/`bash`/`cmd`/`auto`（自动检测）。工作区模式下自动使用当前会话目录，并应用工作区安全限制（禁绝对路径、禁 `..` 逃逸、禁切目录）。

**2. 环境自述（`get_environment`）**——会话开始时获取一份环境档案，避免反复探测：

```bash
node skills/yunqiao-client.mjs <配对码> env
```

返回：可用解释器（python/node/bash）、常用工具（git/jq/curl/grep 等）、工作区信息、系统信息。

**3. 结构化输出**——所有工具返回结构化 JSON（exitCode/stdout/stderr/duration），无需解析裸文本。

**设计原则**：Agent 端不做任何推理（不配模型），全部智能在沙箱侧；客户端是纯工具 + 通道。

### 桌面 → Agent 消息

桌面客户端底部的输入框用于**给上游 Agent 发消息**（`!` 前缀表示紧急消息）。由于 Agent 端是 SSE 短连接（拉取模式），消息会先存入中继队列，Agent 下次调用工具时读取：

```bash
# Agent 侧读取客户端发来的消息（读取后客户端 UI 会显示"已送达"）
node skills/yunqiao-client.mjs <配对码> messages
# 等价于
node skills/yunqiao-client.mjs <配对码> call get_client_messages '{}'
```

建议 Agent 在每次会话开始时调用一次 `get_client_messages`，检查用户是否有新的指令或提示。也建议加载 `skills/yunqiao-usage/SKILL.md` 作为使用规范（含消息优先级、确认回执等约定）。

---

### 工具架（预置只读工具）

云桥内置一组预置只读工具，帮你快速了解远程电脑上的项目，无需反复拼命令。工具脚本存中转服务器，调用时下发到远程电脑执行，**沙箱零安装、客户端零更新、加工具只动服务器**。

| 工具 | 说明 | 示例 |
|------|------|------|
| `stats` | 目录统计：文件数/大小/扩展名分布/最大文件/最近修改 | `stats {"path":"E:\\project"}` |
| `tree` | 目录树（限深度，避免刷屏） | `tree {"path":"E:\\project","maxDepth":2}` |
| `grep_code` | 代码搜索（正则，返回文件/行号/内容） | `grep_code {"pattern":"fn main","path":"E:\\project","glob":"*.rs"}` |
| `project_map` | 项目地图：巨型文件/语言分布/测试目录/依赖/Git 状态 | `project_map {"path":"E:\\project"}` |

```bash
node skills/yunqiao-client.mjs <配对码> call stats '{"path":"E:\\AIcode\\github\\VeryAgent"}'
```

### 自定义命令（run_custom）

把常用脚本固化成一条命令，以后直接调用，不用每次重复拼脚本。

**架构**：命令表（名字→权限）存中转服务器，脚本本体存远程电脑 `client/custom-commands/` 目录，调用时下发执行。

**1. 注册命令（仅管理员）**：
```bash
node skills/yunqiao-client.mjs <配对码> call add_command '{"name":"va-status","desc":"VeryAgent 项目状态"}'
# allowedUsers 不传=仅 admin；传 ["*"]=所有用户；传 ["user-a"]=指定用户
```

**2. 放脚本**（远程电脑 `custom-commands/` 目录，文件名=命令名）：
```
va-status.py / va-status.ps1 / va-status.sh / va-status.bat / va-status.js
```

**3. 执行命令**：
```bash
node skills/yunqiao-client.mjs <配对码> call run_custom '{"name":"va-status","args":["main"]}'
node skills/yunqiao-client.mjs <配对码> call list_commands '{}'   # 查看可用命令
```

**安全**：只能执行 `custom-commands/` 目录内的白名单脚本；非法命令名（含路径/../）被拦截；命令表权限由管理员控制。

### 中转服务器运维（relay_exec，仅管理员）

通过 MCP 直接运维中转服务器，替代 SSH：

```bash
node skills/yunqiao-client.mjs <配对码> call relay_exec '{"op":"status"}'          # 服务器状态
node skills/yunqiao-client.mjs <配对码> call relay_exec '{"op":"view_log","n":50}'  # 看日志
node skills/yunqiao-client.mjs <配对码> call relay_exec '{"op":"update_relay"}'      # 从 GitHub 自更新并重启
```

**安全**：仅管理员可用；只允许预定义的 3 个运维操作，不可执行任意命令。生产环境配合 systemd（`yunqiao-relay.service`）托管：崩溃自动重启、开机自启。

### 多用户权限分层

按用户角色（role）区分可见能力：

| 能力 | 普通用户 | 管理员 |
|------|---------|--------|
| 控制自己设备（exec/script/文件/工具架/自定义命令）| ✅ | ✅ |
| 用户管理 / 配额 / 审计 | ❌（工具列表里根本没有）| ✅ |
| 中转服务器运维 relay_exec | ❌ | ✅ |

**实现**：工具按角色**动态注册**——普通用户的 MCP 工具列表里不包含管理工具（非仅拒绝，而是压根不暴露），安全性最好。管理员密钥对应 `role: admin`，普通用户密钥对应 `role: user`。

---

## 安全

| 机制 | 说明 |
|------|------|
| **密钥 预共享密钥** | 中继服务器和客户机双向认证 |
| **设备配对码** | 临时 6 位数字，一次连接有效 |
| **命令白名单** | `ALLOWED_COMMANDS` 环境变量配置 |
| **文件路径白名单** | `ALLOWED_FILE_PREFIX` 限制读写范围 |
| **HTTPS/WSS** | 全链路加密通信 |

---

## 开发

### 项目结构

```
yunqiao-mcp/
├── relay/              ← 中转服务器（Node.js）
│   ├── server.js       ← 主服务（HTTP + WebSocket + MCP）
│   └── package.json
├── client/             ← 客户机代理（Python）
│   ├── desktop.py     ← 桌面客户端（pywebview + HTML 前端）
│   ├── ui.html         ← HTML 前端（pywebview）
│   ├── desktop_monitor.py  ← 旧版桌面面板
│   └── agent.py        ← 轻量后台版
├── skills/             ← 智能体 Skill
│   ├── yunqiao-client.mjs  ← MCP 客户端（Node.js）
│   └── references/     ← 参考源码
└── README.md
```

### 本地开发

```bash
# 中继服务器
cd relay && npm install && node server.js

# 客户机（另一个终端）
cd client && pip install -r requirements.txt && python desktop.py
# 或在项目根目录直接: python desktop.py（自动转发到 client/desktop.py）

# 测试连接（第三个终端）
node skills/yunqiao-client.mjs list
```

---

<p align="center">
  <sub>MIT License · 开源 · 自由使用</sub>
</p>