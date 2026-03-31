#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PID_FILE="${PID_FILE:-$PROJECT_DIR/relay.pid}"
LOG_DIR="${LOG_DIR:-$PROJECT_DIR/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/relay-keepalive.log}"
START_SCRIPT="$PROJECT_DIR/scripts/start.sh"
RELAY_PATTERN="$PROJECT_DIR/bot/run.py --relay-outbox"

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
  pgrep -f "$RELAY_PATTERN" >/dev/null 2>&1
}

relay_summary() {
  PROJECT_DIR="$PROJECT_DIR" python3 - <<'PY'
import json, os
from pathlib import Path
project_dir = Path(os.environ['PROJECT_DIR']).expanduser().resolve()
p = project_dir / 'data' / 'runtime_state.json'
if not p.exists():
    print('runtime_state=missing')
else:
    try:
        data = json.loads(p.read_text())
    except Exception:
        print('runtime_state=invalid')
    else:
        relay = data.get('relay') if isinstance(data.get('relay'), dict) else {}
        last = relay.get('last_result') if isinstance(relay.get('last_result'), dict) else {}
        print(
            'relay_running={running} checked_at={checked} scanned={scanned} delivered={delivered} failed={failed}'.format(
                running=relay.get('running', False),
                checked=relay.get('last_checked_at', '--'),
                scanned=last.get('scanned', 0),
                delivered=last.get('delivered', 0),
                failed=last.get('failed', 0),
            )
        )
PY
}

if is_running; then
  log "relay healthy; $(relay_summary)"
  exit 0
fi

log "relay missing; starting via scripts/start.sh"
cd "$PROJECT_DIR"
PROJECT_DIR="$PROJECT_DIR" /bin/bash "$START_SCRIPT" relay >> "$LOG_FILE" 2>&1
sleep 3

if is_running; then
  log "relay recovered successfully; $(relay_summary)"
  exit 0
fi

log "relay recovery failed"
exit 1
