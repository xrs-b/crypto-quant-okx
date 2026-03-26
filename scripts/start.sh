#!/bin/bash
set -euo pipefail

# OKX量化交易机器人 - 通用启动脚本

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
VENV_PYTHON="${VENV_PYTHON:-$PROJECT_DIR/.venv/bin/python3}"
VENV_FLASK="${VENV_FLASK:-$PROJECT_DIR/.venv/bin/flask}"
BOT_LOG_FILE="${BOT_LOG_FILE:-$PROJECT_DIR/logs/bot.log}"
DASHBOARD_LOG_FILE="${DASHBOARD_LOG_FILE:-$PROJECT_DIR/logs/dashboard.log}"
PID_FILE="${PID_FILE:-$PROJECT_DIR/bot.pid}"
RELAY_PID_FILE="${RELAY_PID_FILE:-$PROJECT_DIR/relay.pid}"
DASHBOARD_PID_FILE="${DASHBOARD_PID_FILE:-$PROJECT_DIR/dashboard.pid}"
DASHBOARD_PORT="${DASHBOARD_PORT:-5555}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

check_python() {
    if ! test -x "$VENV_PYTHON" >/dev/null 2>&1; then
        echo -e "${RED}错误: 找不到项目虚拟环境 Python：$VENV_PYTHON${NC}"
        echo "请先执行: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
        exit 1
    fi

    cd "$PROJECT_DIR"
    if ! "$VENV_PYTHON" -c "import ccxt, pandas, yaml" 2>/dev/null; then
        echo -e "${RED}错误: 项目虚拟环境缺少依赖，请先执行 .venv/bin/pip install -r requirements.txt${NC}"
        exit 1
    fi
}

ensure_logs_dir() {
    mkdir -p "$PROJECT_DIR/logs"
}

read_interval() {
    local interval
    interval=$(python3 - <<'PY' "$PROJECT_DIR/config/config.yaml"
import sys, yaml
from pathlib import Path
path = Path(sys.argv[1])
if not path.exists():
    print(300)
else:
    data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    print((((data.get('runtime') or {}).get('interval_seconds')) or 300))
PY
)
    echo "${interval:-300}"
}

start_daemon() {
    check_python
    ensure_logs_dir
    cd "$PROJECT_DIR"

    local interval
    interval="$(read_interval)"
    echo "🔄 守护进程模式启动，间隔: ${interval}秒"

    PROJECT_DIR="$PROJECT_DIR" nohup "$VENV_PYTHON" "$PROJECT_DIR/bot/run.py" --daemon >> "$BOT_LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo -e "${GREEN}✅ 守护进程已启动 (间隔: ${interval}秒)${NC}"
    echo "日志文件: $BOT_LOG_FILE"
}

stop_pid_file() {
    local file="$1"
    if [ -f "$file" ]; then
        local pid
        pid=$(cat "$file")
        if ps -p "$pid" >/dev/null 2>&1; then
            kill "$pid" || true
        fi
        rm -f "$file"
        return 0
    fi
    return 1
}

stop_bot() {
    if stop_pid_file "$PID_FILE"; then
        echo -e "${GREEN}✅ 交易机器人已停止${NC}"
    else
        echo -e "${YELLOW}未找到运行中的进程${NC}"
    fi
}

restart() {
    stop_bot || true
    sleep 2
    start_daemon
}

status() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if ps -p "$pid" >/dev/null 2>&1; then
            echo -e "${GREEN}🟢 交易机器人运行中 (PID: $pid)${NC}"
        else
            echo -e "${RED}🔴 进程已停止，但PID文件存在${NC}"
        fi
    else
        echo -e "${YELLOW}⚪ 交易机器人未运行${NC}"
    fi
}

logs() {
    if [ -f "$BOT_LOG_FILE" ]; then
        tail -50 "$BOT_LOG_FILE"
    else
        echo "暂无日志"
    fi
}

start_relay() {
    check_python
    ensure_logs_dir
    cd "$PROJECT_DIR"

    PROJECT_DIR="$PROJECT_DIR" nohup "$VENV_PYTHON" "$PROJECT_DIR/bot/run.py" --relay-outbox >> "$BOT_LOG_FILE" 2>&1 &
    echo $! > "$RELAY_PID_FILE"
    echo -e "${GREEN}✅ 通知 relay 已启动 (PID: $(cat "$RELAY_PID_FILE"))${NC}"
}

stop_relay() {
    stop_pid_file "$RELAY_PID_FILE" >/dev/null 2>&1 || true
    echo -e "${GREEN}✅ 通知 relay 已停止${NC}"
}

start_dashboard() {
    check_python
    ensure_logs_dir
    cd "$PROJECT_DIR"

    PROJECT_DIR="$PROJECT_DIR" nohup "$VENV_FLASK" --app dashboard.api:app run --host 0.0.0.0 --port "$DASHBOARD_PORT" >> "$DASHBOARD_LOG_FILE" 2>&1 &
    echo $! > "$DASHBOARD_PID_FILE"

    echo -e "${GREEN}✅ 仪表盘已启动 (PID: $(cat "$DASHBOARD_PID_FILE"))${NC}"
    echo "访问: http://localhost:${DASHBOARD_PORT}"
}

stop_dashboard() {
    stop_pid_file "$DASHBOARD_PID_FILE" >/dev/null 2>&1 || true
    pkill -f "flask --app dashboard.api:app run --host 0.0.0.0 --port ${DASHBOARD_PORT}" 2>/dev/null || true
    echo -e "${GREEN}✅ 仪表盘已停止${NC}"
}

case "${1:-}" in
    start|daemon)
        start_daemon
        ;;
    stop)
        stop_bot
        ;;
    restart)
        restart
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    dashboard)
        start_dashboard
        ;;
    stop-dashboard)
        stop_dashboard
        ;;
    relay)
        start_relay
        ;;
    stop-relay)
        stop_relay
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status|logs|dashboard|stop-dashboard|relay|stop-relay}"
        echo "支持环境变量: PROJECT_DIR, DASHBOARD_PORT, VENV_PYTHON, VENV_FLASK"
        exit 1
        ;;
esac

exit 0
