"""
OKX量化交易系统 - 仪表盘 (多页面版本)
"""
from flask import Flask, render_template, jsonify, request, send_from_directory
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config, db

app = Flask(__name__)
app.config['SECRET_KEY'] = 'okx-trading-dashboard-secret-key'


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


@app.route('/signals')
def signals():
    """信号记录页面"""
    return render_template('signals.html', active_page='signals')


@app.route('/positions')
def positions():
    """持仓页面"""
    return render_template('overview.html', active_page='positions')


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
