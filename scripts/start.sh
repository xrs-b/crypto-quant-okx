#!/bin/bash
# OKX量化交易机器人 - 启动脚本

# 配置
PROJECT_DIR="/Volumes/MacHD/Projects/crypto-quant-okx"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python3"
VENV_FLASK="$PROJECT_DIR/.venv/bin/flask"
BOT_LOG_FILE="$PROJECT_DIR/logs/bot.log"
DASHBOARD_LOG_FILE="$PROJECT_DIR/logs/dashboard.log"
PID_FILE="$PROJECT_DIR/bot.pid"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 检查Python环境
check_python() {
    if ! test -x "$VENV_PYTHON" &> /dev/null; then
        echo -e "${RED}错误: Python3 未安装${NC}"
        exit 1
    fi
    
    # 检查依赖
    cd "$PROJECT_DIR"
    if ! "$VENV_PYTHON" -c "import ccxt, pandas, yaml" 2>/dev/null; then
        echo -e "${RED}错误: 项目虚拟环境缺少依赖，请先执行 .venv/bin/pip install -r requirements.txt${NC}"
        exit 1
    fi
}

# 启动交易机器人 (守护进程模式)
start_daemon() {
    check_python
    
    cd "$PROJECT_DIR"
    
    # 创建日志目录
    mkdir -p logs
    
    # 读取配置间隔时间(默认5分钟)
    INTERVAL=$(grep "interval_seconds:" config/config.yaml | awk '{print $2}')
    if [ -z "$INTERVAL" ]; then
        INTERVAL=300  # 默认5分钟
    fi
    
    echo "🔄 守护进程模式启动，间隔: ${INTERVAL}秒"
    
    # 后台启动
    nohup "$VENV_PYTHON" "$PROJECT_DIR/bot/run.py" --daemon >> "$BOT_LOG_FILE" 2>&1 &
    
    echo $! > "$PID_FILE"
    echo -e "${GREEN}✅ 守护进程已启动 (间隔: ${INTERVAL}秒)${NC}"
    echo "日志文件: $BOT_LOG_FILE"
}

# 停止交易机器人
stop_bot() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p $PID > /dev/null 2>&1; then
            kill $PID
            rm -f "$PID_FILE"
            echo -e "${GREEN}✅ 交易机器人已停止${NC}"
        else
            rm -f "$PID_FILE"
            echo -e "${YELLOW}进程不存在，已清理${NC}"
        fi
    else
        echo -e "${YELLOW}未找到运行中的进程${NC}"
    fi
}

# 重启
restart() {
    stop_bot
    sleep 2
    start_daemon
}

# 查看状态
status() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p $PID > /dev/null 2>&1; then
            echo -e "${GREEN}🟢 交易机器人运行中 (PID: $PID)${NC}"
        else
            echo -e "${RED}🔴 进程已停止，但PID文件存在${NC}"
        fi
    else
        echo -e "${YELLOW}⚪ 交易机器人未运行${NC}"
    fi
}

# 查看日志
logs() {
    if [ -f "$BOT_LOG_FILE" ]; then
        tail -50 "$BOT_LOG_FILE"
    else
        echo "暂无日志"
    fi
}

# 启动通知 relay
start_relay() {
    check_python

    cd "$PROJECT_DIR"

    mkdir -p logs

    nohup "$VENV_PYTHON" "$PROJECT_DIR/bot/run.py" --relay-outbox >> "$BOT_LOG_FILE" 2>&1 &
    echo $! > "$PROJECT_DIR/relay.pid"

    echo -e "${GREEN}✅ 通知 relay 已启动 (PID: $(cat "$PROJECT_DIR/relay.pid"))${NC}"
}

# 停止通知 relay
stop_relay() {
    if [ -f "$PROJECT_DIR/relay.pid" ]; then
        kill $(cat "$PROJECT_DIR/relay.pid") 2>/dev/null
        rm -f "$PROJECT_DIR/relay.pid"
        echo -e "${GREEN}✅ 通知 relay 已停止${NC}"
    fi
}

# 启动仪表盘
start_dashboard() {
    check_python
    
    cd "$PROJECT_DIR"
    
    mkdir -p logs
    
    nohup "$VENV_FLASK" --app dashboard.api:app run --host 0.0.0.0 --port 5555 >> "$DASHBOARD_LOG_FILE" 2>&1 &
    echo $! > "$PROJECT_DIR/dashboard.pid"
    
    echo -e "${GREEN}✅ 仪表盘已启动 (PID: $(cat "$PROJECT_DIR/dashboard.pid"))${NC}"
    echo "访问: http://localhost:5555"
}

# 停止仪表盘
stop_dashboard() {
    if [ -f "$PROJECT_DIR/dashboard.pid" ]; then
        kill $(cat "$PROJECT_DIR/dashboard.pid") 2>/dev/null || true
        rm -f "$PROJECT_DIR/dashboard.pid"
    fi
    pkill -f "flask --app dashboard.api:app run --host 0.0.0.0 --port 5555" 2>/dev/null || true
    echo -e "${GREEN}✅ 仪表盘已停止${NC}"
}

# 主菜单
case "$1" in
    start)
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
    daemon)
        start_daemon
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
        exit 1
        ;;
esac

exit 0
