#!/bin/bash
set -euo pipefail

PROJECT_DIR="/Volumes/MacHD/Projects/crypto-quant-okx"
VENV_FLASK="$PROJECT_DIR/.venv/bin/flask"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/dashboard-keepalive.log"
APP="dashboard.api:app"
HOST="0.0.0.0"
PORT="5555"
CHECK_URL="http://127.0.0.1:${PORT}/overview"

mkdir -p "$LOG_DIR"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}

is_healthy() {
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$CHECK_URL" || true)
  [[ "$code" == "200" ]]
}

start_dashboard() {
  cd "$PROJECT_DIR"
  nohup "$VENV_FLASK" --app "$APP" run --host "$HOST" --port "$PORT" >> "$LOG_FILE" 2>&1 &
  disown || true
  log "dashboard started on ${HOST}:${PORT}"
}

if is_healthy; then
  log "health check ok"
  exit 0
fi

log "health check failed, attempting restart"
pkill -f "flask --app ${APP} run --host ${HOST} --port ${PORT}" >/dev/null 2>&1 || true
sleep 2
start_dashboard
sleep 4

if is_healthy; then
  log "dashboard recovered successfully"
  exit 0
fi

log "dashboard recovery failed"
exit 1
