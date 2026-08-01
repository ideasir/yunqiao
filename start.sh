#!/bin/bash
# 云端协同 MCP 系统启动脚本
#
# 架构:
#   智能体(POD沙盒) --MCP HTTP--> 公网服务器(MCP+Relay) <--WebSocket--> 家里电脑
#
# 部署步骤:
#   1. 在公网服务器上启动 server
#   2. 在家里电脑上启动 local-agent
#   3. 在智能体的 MCP 配置中添加 server 的 HTTP endpoint
#
# 安全边界（可选，通过环境变量）:
#   ALLOWED_COMMANDS=node,python,git,powershell    # 允许的命令前缀
#   ALLOWED_FILE_PREFIX=D:\aicodework\github        # 允许的文件路径前缀

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

usage() {
  echo "Usage: $0 [server|agent]"
  echo ""
  echo "  server - 启动公网服务器 (MCP HTTP + 中转 WebSocket)"
  echo "  agent  - 启动私人电脑代理"
  echo ""
  echo "环境变量:"
  echo "  RELAY_KEY           预共享密钥（必填，需与 agent 一致）"
  echo "  PORT                监听端口（默认 9876）"
  echo "  ALLOWED_COMMANDS    允许的命令前缀（逗号分隔，默认不限）"
  echo "  ALLOWED_FILE_PREFIX 允许的文件路径前缀（默认不限）"
  echo "  RELAY_URL           服务器 WebSocket 地址（agent 必填）"
  echo "  DEVICE_NAME         设备名称（agent，默认 hostname）"
  exit 1
}

check_env() {
  if [ -z "$RELAY_KEY" ]; then
    export RELAY_KEY="change-me-to-a-secure-random-string"
    echo "[warn] RELAY_KEY not set, using default (insecure!)" >&2
  fi
}

start_server() {
  check_env
  if [ -n "$ALLOWED_COMMANDS" ]; then
    echo "[server] allowed commands: $ALLOWED_COMMANDS" >&2
  fi
  if [ -n "$ALLOWED_FILE_PREFIX" ]; then
    echo "[server] allowed file prefix: $ALLOWED_FILE_PREFIX" >&2
  fi
  echo "[server] starting on port ${PORT:-9876}..." >&2
  exec node "$SCRIPT_DIR/server/src/index.js"
}

start_agent() {
  if [ -z "$RELAY_URL" ]; then
    echo "[error] RELAY_URL must be set to the server address, e.g. ws://your-server:9876/device" >&2
    exit 1
  fi
  check_env
  echo "[agent] connecting to $RELAY_URL..." >&2
  exec node "$SCRIPT_DIR/local-agent/src/index.js"
}

case "${1:-}" in
  server) start_server ;;
  agent)  start_agent  ;;
  *)      usage        ;;
esac