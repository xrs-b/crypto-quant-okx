#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PID_FILE="${PID_FILE:-$PROJECT_DIR/bot.pid}"
LOG_DIR="${LOG_DIR:-$PROJECT_DIR/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/bot-keepalive.log}"
START_SCRIPT="$PROJECT_DIR/scripts/start.sh"
BOT_PATTERN="$PROJECT_DIR/bot/run.py --daemon"

mkdir -p "$LOG_DIR"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null || true)
    if [[ -n "${pid:-}" ]] && ps -p "$pid" >/dev/null 2>&1; then
      return 0
    fi
  fi
  pgrep -f "$BOT_PATTERN" >/dev/null 2>&1
}

if is_running; then
  log "bot healthy; no action"
  exit 0
fi

log "bot missing; starting via scripts/start.sh"
cd "$PROJECT_DIR"
PROJECT_DIR="$PROJECT_DIR" /bin/bash "$START_SCRIPT" start >> "$LOG_FILE" 2>&1
sleep 3

if is_running; then
  log "bot recovered successfully"
  exit 0
fi

log "bot recovery failed"
exit 1
