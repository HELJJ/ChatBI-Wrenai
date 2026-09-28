# wren-chat-api 启动脚本（nohup 后台常驻；生产更稳的 systemd 方式见 wren-chat-api.service）
#
# 启动顺序要求：PostgreSQL → docling 解析服务(5001) → 本服务。
# 两个 docling 开关默认开启（这是生产形态）；如需临时回退旧行为：
#   WREN_CHAT_PENTEST_DOCLING_ENABLED=false WREN_CHAT_RISK_DENGBAO_ENABLED=false ./start.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${WREN_CHAT_PYTHON:-/root/micromamba/envs/wrenai/bin/python}"
HOST="${WREN_CHAT_HOST:-127.0.0.1}"
PORT="${WREN_CHAT_PORT:-8300}"

export WREN_CHAT_PENTEST_DOCLING_ENABLED="${WREN_CHAT_PENTEST_DOCLING_ENABLED:-true}"
export WREN_CHAT_RISK_DENGBAO_ENABLED="${WREN_CHAT_RISK_DENGBAO_ENABLED:-true}"

mkdir -p "$DIR/logs"

if curl -sf "http://$HOST:$PORT/health/live" >/dev/null 2>&1; then
    echo "服务已在运行: http://$HOST:$PORT （重启请先 ./stop.sh）"
    exit 0
fi
if ! curl -sf http://127.0.0.1:5001/health >/dev/null 2>&1; then
    echo "⚠ docling 解析服务(5001)未运行——渗透 detail 与等保任务会持续降级/失败"
    echo "  请先启动: /mnt/sdb/workspace/docling_service/start.sh"
fi

cd "$DIR"
nohup "$PY" -m uvicorn wren_chat_api.main:app \
    --host "$HOST" --port "$PORT" >> logs/wren-chat-api.log 2>&1 &
echo $! > deploy/service.pid
echo "已启动 PID=$! → http://$HOST:$PORT （日志: logs/wren-chat-api.log）"
echo "开关状态: pentest_detail=$WREN_CHAT_PENTEST_DOCLING_ENABLED dengbao=$WREN_CHAT_RISK_DENGBAO_ENABLED"
