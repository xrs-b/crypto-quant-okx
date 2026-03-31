"""
[LEGACY DASHBOARD ENTRYPOINT — DO NOT USE AS CURRENT PRODUCTION ENTRY]

这是历史 Dashboard 入口文件，仅保留作旧页面兼容与排查参考。

当前正式入口请使用：
- dashboard.api:app
- 或 python3 bot/run.py --dashboard

不建议把本文件作为当前正式运行命令或部署入口；README 与部署文档已统一指向上述正式入口。
"""
from flask import Flask, render_template, jsonify, request, send_from_directory
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config, db

app = Flask(__name__)
_default_secret = 'dev-only-change-me'
app.config['SECRET_KEY'] = os.getenv('DASHBOARD_SECRET_KEY', _default_secret)
if app.config['SECRET_KEY'] == _default_secret:
    app.logger.warning(
        'DASHBOARD_SECRET_KEY 未设置，当前使用仅适合本地开发的默认 SECRET_KEY；公开部署前请在环境变量或 .env 中设置强随机值。'
    )


# ============================================================================
# 新版多页面路由
# ============================================================================

@app.route('/')
def index():
    """新版首页 - 重定向到 overview"""
    return render_template('overview.html', active_page='dashboard')


@app.route('/overview')
def overview():
    """总览页面"""
    return render_template('overview.html', active_page='overview')


@app.route('/trades')
def trades():
    """交易记录页面"""
    return render_template('trades.html', active_page='trades')


@app.route('/partial-tp')
def partial_tp():
    """Partial TP 触发历史页面"""
    return render_template('partial_tp.html', active_page='partial_tp')


@app.route('/signals')
def signals():
    """信号记录页面"""
    return render_template('signals.html', active_page='signals')


@app.route('/positions')
def positions():
    """持仓页面"""
    return render_template('overview.html', active_page='positions')


@app.route('/strategy')
def strategy():
    """策略分析页面"""
    return render_template('strategy.html', active_page='strategy')


@app.route('/risk')
def risk():
    """风控状态页面"""
    return render_template('risk.html', active_page='risk')


@app.route('/governance')
def governance():
    """治理审批页面"""
    return render_template('governance.html', active_page='governance')


@app.route('/optimizer')
def optimizer():
    """参数优化页面"""
    return render_template('optimizer.html', active_page='optimizer')


@app.route('/backtest')
def backtest():
    """回测分析页面"""
    return render_template('backtest.html', active_page='backtest')


@app.route('/quality')
def quality():
    """信号质量页面"""
    return render_template('quality.html', active_page='quality')


@app.route('/config')
def config_page():
    """系统配置页面"""
    return render_template('config.html', active_page='config')


# ============================================================================
# 旧版兼容路由 (单页tab模式)
# ============================================================================

@app.route('/dashboard/old')
def dashboard_old():
    """旧版单页仪表盘 (兼容)"""
    stats = db.get_dashboard_stats()
    positions = db.get_positions()
    
    return send_from_directory('templates', 'index.html')


@app.route('/api/stats')
def api_stats():
    """API: 获取统计数据"""
    stats = db.get_dashboard_stats()
    positions = db.get_positions()
    
    return jsonify({
        'stats': stats,
        'positions': positions
    })


@app.route('/api/positions')
def api_positions():
    """API: 获取持仓"""
    positions = db.get_positions()
    return jsonify(positions)


def run(host='0.0.0.0', port=8080):
    """运行仪表盘"""
    app.run(host=host, port=port, debug=False)


if __name__ == '__main__':
    run()
