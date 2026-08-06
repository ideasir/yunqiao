# 自定义命令脚本目录

放这里的脚本可通过 `run_custom <命令名>` 从沙箱远程执行。

## 命名规则
- 文件名 = 命令名（如 `deploy.py` → 命令 `deploy`）
- 支持扩展名：`.py` / `.ps1` / `.sh` / `.bat` / `.js` / `.cmd`

## 注册命令
在中转服务器用 admin 密钥执行：
```
add_command { "name": "deploy", "desc": "说明", "allowedUsers": ["*"] }
```
- `allowedUsers` 不传 = 仅 admin 可用
- 传 `["*"]` = 所有用户可用

## 安全
- 只能执行本目录的脚本（白名单目录）
- 命令表在中转服务器，脚本在客户端本机
