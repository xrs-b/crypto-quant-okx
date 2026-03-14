"""
仪表盘后端API - Flask实现
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from datetime import datetime, timedelta
import pandas as pd
from typing import Dict, List, Any
import os

# 初始化Flask
app = Flask(__name__, static_folder='templates', static_url_path='')
CORS(app)

from core.config import Config
from core.database import Database
from core.presets import PresetManager
from trading.executor import RiskManager
from ml.engine import MLEngine
from analytics import StrategyBacktester, SignalQualityAnalyzer, ParameterOptimizer, GovernanceEngine

# 初始化
config = Config()
db = Database(config.db_path)
risk_manager = RiskManager(config, db)
ml_engine = MLEngine(config.all)
backtester = StrategyBacktester(config)
signal_quality_analyzer = SignalQualityAnalyzer(config, db)
optimizer = ParameterOptimizer(config, db)
preset_manager = PresetManager(config)
governance = GovernanceEngine(config, db)


# ============================================================================
# 根路由
# ============================================================================

@app.route('/')
def index():
    """仪表盘首页"""
    return send_from_directory('templates', 'index.html')


# ============================================================================
# 交易数据API
# ============================================================================

@app.route('/api/positions')
def get_positions():
    """获取当前持仓"""
    positions = db.get_positions()
    return jsonify({
        'success': True,
        'data': positions,
        'count': len(positions)
    })


@app.route('/api/trades')
def get_trades():
    """获取交易记录"""
    symbol = request.args.get('symbol')
    status = request.args.get('status')
    limit = int(request.args.get('limit', 100))
    
    trades = db.get_trades(symbol=symbol, status=status, limit=limit)
    
    return jsonify({
        'success': True,
        'data': trades,
        'count': len(trades)
    })


@app.route('/api/trades/stats')
def get_trade_stats():
    """获取交易统计"""
    days = int(request.args.get('days', 30))
    stats = db.get_trade_stats(days=days)
    
    return jsonify({
        'success': True,
        'data': stats
    })


@app.route('/api/trades/symbol-performance')
def get_symbol_performance():
    """获取按币种聚合的表现"""
    trades = db.get_trades(limit=1000)
    perf = {}
    for t in trades:
        symbol = t.get('symbol')
        if symbol not in perf:
            perf[symbol] = {'symbol': symbol, 'trades': 0, 'wins': 0, 'total_pnl': 0.0}
        perf[symbol]['trades'] += 1
        pnl = float(t.get('pnl', 0) or 0)
        perf[symbol]['total_pnl'] += pnl
        if pnl > 0:
            perf[symbol]['wins'] += 1
    data = []
    for row in perf.values():
        row['win_rate'] = round(row['wins'] / row['trades'] * 100, 2) if row['trades'] > 0 else 0
        row['total_pnl'] = round(row['total_pnl'], 2)
        data.append(row)
    data.sort(key=lambda x: x['total_pnl'], reverse=True)
    return jsonify({'success': True, 'data': data})


# ============================================================================
# 信号数据API
# ============================================================================

@app.route('/api/signals')
def get_signals():
    """获取信号记录"""
    symbol = request.args.get('symbol')
    limit = int(request.args.get('limit', 100))
    
    signals = db.get_signals(symbol=symbol, limit=limit)
    
    return jsonify({
        'success': True,
        'data': signals,
        'count': len(signals)
    })


@app.route('/api/signals/filter-reasons')
def get_signal_filter_reasons():
    """获取过滤原因排行"""
    limit = int(request.args.get('limit', 10))
    signals = db.get_signals(limit=1000)
    counts = {}
    for s in signals:
        if s.get('filtered') and s.get('filter_reason'):
            reason = s.get('filter_reason')
            counts[reason] = counts.get(reason, 0) + 1
    data = sorted([
        {'reason': k, 'count': v} for k, v in counts.items()
    ], key=lambda x: x['count'], reverse=True)[:limit]
    return jsonify({'success': True, 'data': data})


@app.route('/api/signals/stats')
def get_signal_stats():
    """获取信号统计"""
    days = int(request.args.get('days', 30))
    
    signals = db.get_signals(limit=1000)
    
    # 过滤指定天数
    cutoff = datetime.now() - timedelta(days=days)
    signals = [s for s in signals if datetime.fromisoformat(s['created_at']) > cutoff]
    
    total = len(signals)
    executed = sum(1 for s in signals if s.get('executed'))
    filtered = sum(1 for s in signals if s.get('filtered'))
    
    # 统计策略触发
    strategy_counts = {}
    for s in signals:
        for strat in s.get('strategies_triggered', []):
            strategy_counts[strat] = strategy_counts.get(strat, 0) + 1
    
    return jsonify({
        'success': True,
        'data': {
            'total_signals': total,
            'executed_signals': executed,
            'filtered_signals': filtered,
            'execution_rate': round(executed / total * 100, 2) if total > 0 else 0,
            'strategy_counts': strategy_counts,
            'period_days': days
        }
    })


# ============================================================================
# 策略分析API
# ============================================================================

@app.route('/api/strategy/stats')
def get_strategy_stats():
    """获取策略统计"""
    days = int(request.args.get('days', 30))
    stats = db.get_strategy_stats(days=days)
    
    return jsonify({
        'success': True,
        'data': stats
    })


# ============================================================================
# 每日总结API
# ============================================================================

@app.route('/api/daily/summary')
def get_daily_summary():
    """获取每日总结"""
    days = int(request.args.get('days', 30))
    
    # 获取最近交易
    trades = db.get_trades(status='closed', limit=1000)
    
    # 按日期分组
    daily_data = {}
    for trade in trades:
        date = trade.get('open_time', '')[:10]
        if date not in daily_data:
            daily_data[date] = {
                'date': date,
                'trades': 0,
                'wins': 0,
                'losses': 0,
                'pnl': 0
            }
        
        daily_data[date]['trades'] += 1
        pnl = trade.get('pnl', 0)
        daily_data[date]['pnl'] += pnl
        
        if pnl > 0:
            daily_data[date]['wins'] += 1
        elif pnl < 0:
            daily_data[date]['losses'] += 1
    
    # 排序
    summaries = sorted(daily_data.values(), key=lambda x: x['date'], reverse=True)[:days]
    
    return jsonify({
        'success': True,
        'data': summaries
    })


# ============================================================================
# 系统状态API
# ============================================================================

@app.route('/api/system/status')
def get_system_status():
    """获取系统状态"""
    positions = db.get_positions()
    trade_stats = db.get_trade_stats(days=30)
    signals = db.get_signals(limit=200)
    today_signals = sum(1 for s in signals if s.get('created_at', '').startswith(datetime.now().strftime('%Y-%m-%d')))
    executed_today = sum(1 for s in signals if s.get('created_at', '').startswith(datetime.now().strftime('%Y-%m-%d')) and s.get('executed'))
    total_value = sum(p.get('quantity', 0) * p.get('current_price', 0) for p in positions)
    unrealized_pnl = sum(p.get('unrealized_pnl', 0) for p in positions)
    risk = risk_manager.get_risk_status()

    return jsonify({
        'success': True,
        'data': {
            'system': {
                'status': 'running',
                'uptime': 'N/A',
                'last_update': datetime.now().isoformat()
            },
            'portfolio': {
                'total_positions': len(positions),
                'total_value': round(total_value, 2),
                'unrealized_pnl': round(unrealized_pnl, 2)
            },
            'trading': {
                'total_trades_30d': trade_stats.get('total_trades', 0),
                'win_rate': trade_stats.get('win_rate', 0),
                'total_pnl': trade_stats.get('total_pnl', 0)
            },
            'signals': {
                'today_signals': today_signals,
                'executed_today': executed_today,
                'execution_rate': round(executed_today / max(today_signals, 1) * 100, 2)
            },
            'risk': risk
        }
    })


# ============================================================================
# 配置API
# ============================================================================

@app.route('/api/risk/status')
def get_risk_status():
    """获取风险状态"""
    return jsonify({
        'success': True,
        'data': risk_manager.get_risk_status()
    })


@app.route('/api/risk/events')
def get_risk_events():
    """获取最近风控/过滤事件"""
    signals = db.get_signals(limit=100)
    events = []
    keywords = ['风险', '回撤', '亏损', '冷却', '波动率', '逆大趋势', '持仓', '余额']
    for s in signals:
        reason = s.get('filter_reason') or ''
        if s.get('filtered') and any(k in reason for k in keywords):
            events.append({
                'time': s.get('created_at'),
                'symbol': s.get('symbol'),
                'reason': reason,
                'signal_type': s.get('signal_type'),
                'strength': s.get('strength')
            })
    return jsonify({'success': True, 'data': events[:20]})


@app.route('/api/ml/metrics')
def get_ml_metrics():
    """获取模型评估结果"""
    return jsonify({'success': True, 'data': ml_engine.get_all_model_metrics(config.symbols)})


@app.route('/api/backtest/summary')
def get_backtest_summary():
    """获取回测结果"""
    return jsonify({'success': True, 'data': backtester.run_all(config.symbols)})


@app.route('/api/signal-quality')
def get_signal_quality():
    """获取信号质量分析"""
    return jsonify({'success': True, 'data': signal_quality_analyzer.analyze()})


@app.route('/api/optimizer/results')
def get_optimizer_results():
    """获取参数优化与币种分层结果"""
    return jsonify({'success': True, 'data': optimizer.run()})


@app.route('/api/mode/status')
def get_mode_status():
    """获取当前 preset / 模式状态"""
    return jsonify({'success': True, 'data': preset_manager.status()})


@app.route('/api/candidates/promotion')
def get_candidate_promotion():
    """获取候选晋升建议"""
    return jsonify({'success': True, 'data': optimizer.run().get('candidate_promotions', [])})


@app.route('/api/candidates/history')
def get_candidate_history():
    """获取候选审查历史"""
    symbol = request.args.get('symbol')
    limit = int(request.args.get('limit', 50))
    return jsonify({'success': True, 'data': db.get_candidate_reviews(symbol=symbol, limit=limit)})


@app.route('/api/presets')
def get_presets():
    """列出可用预设"""
    return jsonify({'success': True, 'data': preset_manager.list_presets()})


@app.route('/api/presets/history')
def get_preset_history():
    """获取 preset 应用历史"""
    limit = int(request.args.get('limit', 30))
    return jsonify({'success': True, 'data': db.get_preset_history(limit=limit)})


@app.route('/api/presets/apply', methods=['POST'])
def apply_preset():
    """应用预设"""
    global risk_manager, ml_engine, backtester, signal_quality_analyzer, optimizer, governance, preset_manager
    payload = request.get_json(silent=True) or {}
    name = payload.get('name') or request.args.get('name')
    auto_restart = bool(payload.get('auto_restart', True))
    if not name:
        return jsonify({'success': False, 'error': 'missing preset name'}), 400
    result = preset_manager.apply_preset(name, auto_restart=auto_restart)
    # refresh globals using updated config
    config.reload()
    risk_manager = RiskManager(config, db)
    ml_engine = MLEngine(config.all)
    backtester = StrategyBacktester(config)
    signal_quality_analyzer = SignalQualityAnalyzer(config, db)
    optimizer = ParameterOptimizer(config, db)
    governance = GovernanceEngine(config, db)
    preset_manager = PresetManager(config)
    return jsonify({'success': True, 'data': result})


@app.route('/api/governance/status')
def get_governance_status():
    """获取治理状态"""
    return jsonify({'success': True, 'data': governance.evaluate()})


@app.route('/api/daily-summary')
def get_daily_summary_report():
    """生成/获取今日摘要"""
    return jsonify({'success': True, 'data': governance.generate_daily_summary()})


@app.route('/api/approvals/pending')
def get_pending_approvals():
    """获取待审批的建议"""
    gov = governance.evaluate(use_cache=True)
    pending = []
    for alert in gov.get('alerts', []):
        if alert.get('approval_required'):
            pending.append({
                'type': alert.get('type'),
                'target': alert.get('recommended_preset'),
                'message': alert.get('message'),
                'details': alert,
            })
    return jsonify({'success': True, 'data': pending})


@app.route('/api/approvals/execute', methods=['POST'])
def execute_approval():
    """执行审批操作"""
    global risk_manager, ml_engine, backtester, signal_quality_analyzer, optimizer, governance, preset_manager
    payload = request.get_json(silent=True) or {}
    approval_type = payload.get('type')
    target = payload.get('target')
    decision = payload.get('decision', 'approved')
    
    if not approval_type or not target:
        return jsonify({'success': False, 'error': 'missing type or target'}), 400
    
    db.record_approval(approval_type, target, decision, payload)
    
    if decision == 'approved' and approval_type == 'btc_grid_upgrade':
        result = preset_manager.apply_preset(target, auto_restart=True)
        config.reload()
        risk_manager = RiskManager(config, db)
        ml_engine = MLEngine(config.all)
        backtester = StrategyBacktester(config)
        signal_quality_analyzer = SignalQualityAnalyzer(config, db)
        optimizer = ParameterOptimizer(config, db)
        governance = GovernanceEngine(config, db)
        preset_manager = PresetManager(config)
        return jsonify({'success': True, 'data': {'action': 'preset_applied', 'result': result}})
    
    if decision == 'approved' and approval_type == 'main_pool_downgrade':
        result = preset_manager.apply_preset(target, auto_restart=True)
        config.reload()
        risk_manager = RiskManager(config, db)
        ml_engine = MLEngine(config.all)
        backtester = StrategyBacktester(config)
        signal_quality_analyzer = SignalQualityAnalyzer(config, db)
        optimizer = ParameterOptimizer(config, db)
        governance = GovernanceEngine(config, db)
        preset_manager = PresetManager(config)
        return jsonify({'success': True, 'data': {'action': 'preset_applied', 'result': result}})
    
    return jsonify({'success': True, 'data': {'action': 'recorded', 'decision': decision}})


@app.route('/api/approvals/history')
def get_approval_history():
    """获取审批历史"""
    limit = int(request.args.get('limit', 50))
    return jsonify({'success': True, 'data': db.get_approval_history(limit=limit)})


@app.route('/api/changes/recent')
def get_recent_changes():
    """获取最近变化汇总"""
    preset_history = db.get_preset_history(limit=5)
    candidate_history = db.get_candidate_reviews(limit=5)
    rows = []
    for item in preset_history:
        rows.append({
            'time': item.get('created_at'),
            'type': 'preset_apply',
            'title': item.get('preset_name'),
            'detail': ', '.join(item.get('watch_list', []))
        })
    for item in candidate_history:
        rows.append({
            'time': item.get('created_at'),
            'type': 'candidate_review',
            'title': item.get('symbol'),
            'detail': f"{item.get('decision')} | {item.get('reason')}"
        })
    rows.sort(key=lambda x: str(x.get('time', '')), reverse=True)
    return jsonify({'success': True, 'data': rows[:10]})


@app.route('/api/alerts')
def get_alerts():
    """获取当前需要留意的告警"""
    alerts = []
    risk = risk_manager.get_risk_status()
    if risk.get('daily_drawdown', 0) >= risk.get('max_daily_drawdown', 1):
        alerts.append({'level': 'danger', 'message': '已达到日内回撤熔断阈值'})
    elif risk.get('daily_drawdown', 0) >= risk.get('max_daily_drawdown', 1) * 0.7:
        alerts.append({'level': 'warn', 'message': '日内回撤接近熔断阈值'})
    if risk.get('consecutive_losses', 0) >= risk.get('max_consecutive_losses', 99):
        alerts.append({'level': 'danger', 'message': '连续亏损已触发熔断'})
    promotions = optimizer.run().get('candidate_promotions', [])
    for row in promotions:
        if row.get('decision') == 'promote':
            alerts.append({'level': 'info', 'message': f"候选币 {row.get('symbol')} 可考虑升级主池"})
        elif row.get('decision') == 'keep_candidate':
            alerts.append({'level': 'warn', 'message': f"候选币 {row.get('symbol')} 仍需继续观察"})
    if not alerts:
        alerts.append({'level': 'info', 'message': '当前无重大异常，系统处于观察运行状态'})
    return jsonify({'success': True, 'data': alerts})


@app.route('/api/config')
def get_config():
    """获取配置(脱敏)"""
    cfg = config.all
    
    # 脱敏处理
    if 'api' in cfg:
        cfg['api']['key'] = '***' if cfg['api'].get('key') else ''
        cfg['api']['secret'] = '***' if cfg['api'].get('secret') else ''
        cfg['api']['passphrase'] = '***' if cfg['api'].get('passphrase') else ''
    
    return jsonify({
        'success': True,
        'data': cfg
    })


@app.route('/api/config/symbols')
def get_symbols_config():
    """获取监听的币种配置"""
    symbols = config.symbols
    trading = config.get('trading', {})
    
    return jsonify({
        'success': True,
        'data': {
            'symbols': symbols,
            'leverage': trading.get('leverage', 10),
            'position_size': trading.get('position_size', 0.1),
            'stop_loss': trading.get('stop_loss', 0.02),
            'take_profit': trading.get('take_profit', 0.04)
        }
    })


# ============================================================================
# 健康检查
# ============================================================================

@app.route('/health')
def health():
    """健康检查"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat()
    })


# ============================================================================
# 启动
# ============================================================================

def run_dashboard(host: str = '0.0.0.0', port: int = 8050, debug: bool = False):
    """运行仪表盘"""
    dashboard_cfg = config.dashboard_config
    host = dashboard_cfg.get('host', host)
    port = dashboard_cfg.get('port', port)
    
    print(f"\n🚀 仪表盘启动中...")
    print(f"   地址: http://{host}:{port}")
    print(f"   API:  http://{host}:{port}/api/*")
    print(f"   健康: http://{host}:{port}/health\n")
    
    app.run(host=host, port=port, debug=debug)


if __name__ == '__main__':
    run_dashboard(debug=True)
