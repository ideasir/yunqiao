# 云桥发布计划（ROADMAP）

> 当前状态：内测中（仅作者自用）。此文档记录正式发布前的全部待办。

## 🔴 待办（正式发布前）

### 首次使用引导（欢迎页）
- [ ] 首次启动显示欢迎卡片，引导三步：
  1. 点"配置"填服务器地址 + Key
  2. 点"复制配对码"发给 AI Agent
  3. 等待 AI 连接
- [ ] 配对码按钮 tooltip 明确"复制后发给智能体"（已有 tooltip，欢迎页统一说明）
- [ ] 连接地址/Key 输入框加默认示例与说明

### 用户自助能力（内测通过后开放）
- [ ] 用户自助注册/邀请（当前：管理员手动 create_user）
- [ ] 用户自助改密钥（当前：泄露找管理员 reset_user_key 换新）
- [ ] 配额用量看板

### 容灾（已定稿：手动双域名）
- [ ] 备用机（韩国 146.56.129.15）配独立域名 + HTTPS 证书
- [ ] 主→备 数据定时加密同步（rsync over SSH）
- [ ] 备用机 nginx + 防火墙加固
- [ ] （不做）自动切换 / DNS API / Cloudflare Token —— 避免攻击面

### 工程化
- [ ] 版本号 + CHANGELOG（当前 v2.0）
- [ ] 客户端自动更新（当前靠 git pull）
- [ ] 多服务器/域名容灾演练

## 🟢 已完成
- [x] 客户端日志 IDE 化（结构化卡片）
- [x] 多用户权限分层（普通用户=纯透传，管理员=完整）
- [x] relay_exec 中转服务器运维（status/view_log/update_relay）
- [x] systemd 看门狗（yunqiao-relay.service，崩溃自愈+开机自启）
- [x] 工具架（stats/tree/grep_code/project_map）
- [x] 自定义命令（run_custom，脚本在客户端 custom-commands/）
- [x] reset_user_key 密钥作废工具
- [x] README 修正（main.py→desktop.py）+ 新功能文档
- [x] 云桥.spec 打包配置 + CI 修复

## 🟡 体验优化（进行中）
- [x] 配置弹窗：地址/Key 默认示例与引导
- [ ] 日志区重做：显示 Agent 的每个命令/回复/文件操作过程（执行中→完成）
- [ ] 术语统一：工作区 vs 会话
- [ ] 连接错误提示详细化（区分 DNS/超时/密钥无效）
- [ ] 日志条数上限（防无限增长卡顿）
