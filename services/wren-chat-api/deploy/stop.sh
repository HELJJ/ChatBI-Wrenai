#!/usr/bin/env bash
# wren-chat-api 停止脚本
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/service.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "未找到 service.pid，按端口停止..."
    lsof -ti:"${WREN_CHAT_PORT:-8300}" | xargs -r kill && echo "已停止" || echo "服务未在运行"
    exit 0
fi
PID="$(cat "$PID_FILE")"
if kill "$PID" 2>/dev/null; then
    echo "已停止 PID=$PID"
else
    echo "PID=$PID 已不存在"
fi
rm -f "$PID_FILE"
