#!/usr/bin/env sh
# 启动跨站照护交接后端 + Web 控制台（仅标准库）。
# 用法：./start.sh [端口]   （默认 8000；可用 HOST/PORT/CARE_STATE 环境变量）
set -eu
cd "$(dirname "$0")"
PORT="${1:-${PORT:-8000}}"
HOST="${HOST:-127.0.0.1}"
if [ -n "${CARE_STATE:-}" ]; then
  exec python3 -m backend.server --host "$HOST" --port "$PORT" --state "$CARE_STATE"
else
  exec python3 -m backend.server --host "$HOST" --port "$PORT"
fi
