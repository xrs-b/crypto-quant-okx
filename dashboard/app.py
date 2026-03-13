"""
OKX量化交易系统 - 仪表盘
"""
from flask import Flask, render_template, jsonify, request
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config, db

app = Flask(__name__)
app.config['SECRET_KEY'] = 'okx-trading-dashboard-secret-key'


@app.route('/')
def index():
    """首页"""
    stats = db.get_dashboard_stats()
    positions = db.get_positions()
    
    return render_template('dashboard/index.html',
                         stats=stats,
                         positions=positions)


@app.route('/trades')
def trades():
    """交易记录"""
    page = request.args.get('page', 1, type=int)
    limit = 20
    offset = (page - 1) * limit
    
    trades = db.get_trades(limit=limit, offset=offset)
    total = len(trades)
    
    return render_template('dashboard/trades.html',
                         trades=trades,
                         page=page,
                         total=total)


@app.route('/signals')
def signals():
    """信号记录"""
    page = request.args.get('page', 1, type=int)
    limit = 20
    offset = (page - 1) * limit
    
    signals = db.get_signals(limit=limit, offset=offset)
    stats = db.get_signal_stats()
    
    return render_template('dashboard/signals.html',
                         signals=signals,
                         stats=stats,
                         page=page)


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
