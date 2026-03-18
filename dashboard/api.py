"""
仪表盘后端API - Flask实现
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from datetime import datetime, timedelta
import pandas as pd
from typing import Dict, List, Any
import os
import threading
import yaml

# 初始化Flask
app = Flask(__name__, static_folder='templates', static_url_path='')
CORS(app)

from core.config import Config
from core.database import Database
from core.exchange import Exchange
from core.presets import PresetManager
from trading.executor import RiskManager
from bot.run import execute_exchange_smoke, reconcile_exchange_positions, load_runtime_state
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
_exchange_client = None
smoke_execution_lock = threading.Lock()
smoke_execution_state = {
    'running': False,
    'symbol': None,
    'side': None,
    'started_at': None,
    'last_result': None,
}


# ============================================================================
# 内部辅助函数
# ============================================================================


def _get_exchange_client():
    global _exchange_client
    if _exchange_client is None:
        _exchange_client = Exchange(config.all)
    return _exchange_client


def _parse_created_at(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return None


def _evaluate_filtered_signal_outcome(signal: Dict[str, Any], ohlcv: List[List[Any]], window_hours: int = 24,
                                     min_move_pct: float = 1.5, now: datetime = None) -> Dict[str, Any]:
    now = now or datetime.now()
    created_at = _parse_created_at(signal.get('created_at'))
    signal_type = signal.get('signal_type')
    entry_price = float(signal.get('price') or 0)

    result = {
        'status': 'insufficient_data',
        'favorable_move_pct': 0.0,
        'adverse_move_pct': 0.0,
        'window_hours': window_hours,
        'min_move_pct': min_move_pct,
    }

    if created_at is None or entry_price <= 0:
        result['status'] = 'insufficient_data'
        return result

    if now - created_at < timedelta(hours=window_hours):
        result['status'] = 'pending'
        return result

    if signal_type not in ('buy', 'sell'):
        result['status'] = 'insufficient_data'
        return result

    if not ohlcv:
        result['status'] = 'insufficient_data'
        return result

    highs = [float(row[2]) for row in ohlcv if len(row) > 3 and row[2] is not None]
    lows = [float(row[3]) for row in ohlcv if len(row) > 3 and row[3] is not None]
    if not highs or not lows:
        result['status'] = 'insufficient_data'
        return result

    max_high = max(highs)
    min_low = min(lows)
    if signal_type == 'buy':
        favorable_move_pct = (max_high - entry_price) / entry_price * 100
        adverse_move_pct = (entry_price - min_low) / entry_price * 100
    else:
        favorable_move_pct = (entry_price - min_low) / entry_price * 100
        adverse_move_pct = (max_high - entry_price) / entry_price * 100

    favorable_move_pct = round(max(favorable_move_pct, 0.0), 2)
    adverse_move_pct = round(max(adverse_move_pct, 0.0), 2)
    result['favorable_move_pct'] = favorable_move_pct
    result['adverse_move_pct'] = adverse_move_pct

    if adverse_move_pct >= min_move_pct and adverse_move_pct > favorable_move_pct:
        result['status'] = 'avoided_loss'
    elif favorable_move_pct >= min_move_pct and favorable_move_pct > adverse_move_pct:
        result['status'] = 'missed_profit'
    else:
        result['status'] = 'neutral'
    return result


def _evaluate_signals_with_outcomes(signals: List[Dict[str, Any]], exchange_client, window_hours: int = 24,
                                    min_move_pct: float = 1.5, now: datetime = None) -> List[Dict[str, Any]]:
    now = now or datetime.now()
    candle_limit = max(int(window_hours) + 4, 12)
    evaluated = []

    for row in signals:
        if not row.get('filtered'):
            continue
        created_at = _parse_created_at(row.get('created_at'))
        if created_at is None or row.get('signal_type') not in ('buy', 'sell'):
            outcome = _evaluate_filtered_signal_outcome(row, [], window_hours=window_hours, min_move_pct=min_move_pct, now=now)
        elif now - created_at < timedelta(hours=window_hours):
            outcome = _evaluate_filtered_signal_outcome(row, [], window_hours=window_hours, min_move_pct=min_move_pct, now=now)
        else:
            since_ms = int(created_at.timestamp() * 1000)
            try:
                candles = exchange_client.fetch_ohlcv(row['symbol'], '1h', since=since_ms, limit=candle_limit)
            except Exception:
                candles = []
            outcome = _evaluate_filtered_signal_outcome(row, candles, window_hours=window_hours, min_move_pct=min_move_pct, now=now)
        evaluated.append({**row, 'outcome': outcome})
    return evaluated


def _build_filter_effectiveness(signals: List[Dict[str, Any]], exchange_client, window_hours: int = 24,
                                min_move_pct: float = 1.5, now: datetime = None) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}

    for row in _evaluate_signals_with_outcomes(signals, exchange_client, window_hours=window_hours, min_move_pct=min_move_pct, now=now):
        code = row.get('filter_code') or 'UNCLASSIFIED'
        bucket = buckets.setdefault(code, {
            'code': code,
            'group': row.get('filter_group') or 'other',
            'reason': row.get('filter_reason') or '未注明原因',
            'action_hint': row.get('action_hint') or '继续观察',
            'total': 0,
            'analyzed': 0,
            'avoided_loss': 0,
            'missed_profit': 0,
            'neutral': 0,
            'pending': 0,
            'insufficient_data': 0,
            'symbols': set(),
            'samples': [],
        })
        bucket['total'] += 1
        if row.get('symbol'):
            bucket['symbols'].add(row.get('symbol'))

        outcome = row['outcome']
        status = outcome['status']
        if status in ('avoided_loss', 'missed_profit', 'neutral'):
            bucket['analyzed'] += 1
        bucket[status] = bucket.get(status, 0) + 1
        if len(bucket['samples']) < 3:
            bucket['samples'].append({
                'symbol': row.get('symbol'),
                'signal_type': row.get('signal_type'),
                'created_at': row.get('created_at'),
                'status': status,
                'favorable_move_pct': outcome.get('favorable_move_pct'),
                'adverse_move_pct': outcome.get('adverse_move_pct'),
            })

    data = []
    for bucket in buckets.values():
        bucket['symbols'] = sorted(bucket['symbols'])
        bucket['symbol_count'] = len(bucket['symbols'])
        decisive = bucket['avoided_loss'] + bucket['missed_profit']
        bucket['effectiveness_rate'] = round(bucket['avoided_loss'] / decisive * 100, 2) if decisive > 0 else None
        data.append(bucket)

    data.sort(key=lambda x: (-(x['effectiveness_rate'] if x['effectiveness_rate'] is not None else -1), -x['avoided_loss'], -x['total'], x['code']))
    return data


def _build_symbol_filter_effectiveness(signals: List[Dict[str, Any]], exchange_client, window_hours: int = 24,
                                       min_move_pct: float = 1.5, now: datetime = None) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}

    for row in _evaluate_signals_with_outcomes(signals, exchange_client, window_hours=window_hours, min_move_pct=min_move_pct, now=now):
        symbol = row.get('symbol') or '--'
        code = row.get('filter_code') or 'UNCLASSIFIED'
        key = f'{symbol}::{code}'
        bucket = buckets.setdefault(key, {
            'symbol': symbol,
            'code': code,
            'group': row.get('filter_group') or 'other',
            'reason': row.get('filter_reason') or '未注明原因',
            'action_hint': row.get('action_hint') or '继续观察',
            'total': 0,
            'analyzed': 0,
            'avoided_loss': 0,
            'missed_profit': 0,
            'neutral': 0,
            'pending': 0,
            'insufficient_data': 0,
            'latest_created_at': row.get('created_at'),
            'samples': [],
        })
        bucket['total'] += 1
        if (row.get('created_at') or '') > (bucket.get('latest_created_at') or ''):
            bucket['latest_created_at'] = row.get('created_at')

        outcome = row['outcome']
        status = outcome['status']
        if status in ('avoided_loss', 'missed_profit', 'neutral'):
            bucket['analyzed'] += 1
        bucket[status] = bucket.get(status, 0) + 1
        if len(bucket['samples']) < 2:
            bucket['samples'].append({
                'created_at': row.get('created_at'),
                'status': status,
                'favorable_move_pct': outcome.get('favorable_move_pct'),
                'adverse_move_pct': outcome.get('adverse_move_pct'),
            })

    rows = []
    for bucket in buckets.values():
        decisive = bucket['avoided_loss'] + bucket['missed_profit']
        bucket['effectiveness_rate'] = round(bucket['avoided_loss'] / decisive * 100, 2) if decisive > 0 else None
        if bucket['effectiveness_rate'] is None:
            bucket['tuning_bias'] = 'wait'
            bucket['tuning_hint'] = '有效样本未够，继续积累观察样本'
        elif bucket['effectiveness_rate'] >= 70:
            bucket['tuning_bias'] = 'keep_strict'
            bucket['tuning_hint'] = '该币种上此规则多数在帮你避险，暂时不建议放宽'
        elif bucket['effectiveness_rate'] <= 40:
            bucket['tuning_bias'] = 'consider_relax'
            bucket['tuning_hint'] = '该币种上此规则错失机会偏多，可考虑局部放宽'
        else:
            bucket['tuning_bias'] = 'mixed'
            bucket['tuning_hint'] = '该币种上此规则表现一般，建议继续观察更多样本'
        rows.append(bucket)

    rows.sort(key=lambda x: (
        -(x['effectiveness_rate'] if x['effectiveness_rate'] is not None else -1),
        -x['analyzed'],
        -x['total'],
        x['symbol'],
        x['code']
    ))
    return rows


def _build_symbol_parameter_advice(symbol_rows: List[Dict[str, Any]], cfg: Config) -> List[Dict[str, Any]]:
    composite = cfg.get('strategies.composite', {}) or {}
    market_filters = cfg.get('market_filters', {}) or {}
    trading = cfg.get('trading', {}) or {}
    advice = []

    for row in symbol_rows:
        symbol = row.get('symbol') or '--'
        code = row.get('code') or 'UNCLASSIFIED'
        bias = row.get('tuning_bias')
        analyzed = int(row.get('analyzed') or 0)
        missed = int(row.get('missed_profit') or 0)
        avoided = int(row.get('avoided_loss') or 0)
        rate = row.get('effectiveness_rate')

        if analyzed <= 0:
            continue

        def push(parameter, current_value, suggested_value, action, reason, priority='medium'):
            advice.append({
                'symbol': symbol,
                'filter_code': code,
                'parameter': parameter,
                'current_value': current_value,
                'suggested_value': suggested_value,
                'action': action,
                'priority': priority,
                'effectiveness_rate': rate,
                'analyzed': analyzed,
                'missed_profit': missed,
                'avoided_loss': avoided,
                'reason': reason,
            })

        if code == 'WEAK_SIGNAL_STRENGTH':
            current = int(composite.get('min_strength', 20) or 20)
            if bias == 'consider_relax':
                push('strategies.composite.min_strength', current, max(current - 2, 18), 'relax', '该币种经常因强度不足被挡，但后验更常走出顺向机会', 'high')
            elif bias == 'keep_strict':
                push('strategies.composite.min_strength', current, current, 'keep', '该币种上强度门槛多数在帮你避险，建议先保持当前阈值', 'medium')

        elif code == 'INSUFFICIENT_STRATEGY_COUNT':
            current = int(composite.get('min_strategy_count', 1) or 1)
            if bias == 'consider_relax' and current > 1:
                push('strategies.composite.min_strategy_count', current, max(current - 1, 1), 'relax', '该币种经常只差一层确认就被挡，可考虑降低最少策略触发数', 'high')
            elif bias == 'keep_strict':
                push('strategies.composite.min_strategy_count', current, current, 'keep', '该币种上多策略确认大多有效，建议保持当前要求', 'medium')

        elif code == 'LOW_VOLATILITY':
            current = float(market_filters.get('min_volatility', 0.003) or 0.003)
            if bias == 'consider_relax':
                push('market_filters.min_volatility', current, round(current * 0.85, 5), 'relax', '该币种低波动过滤后仍多次出现顺向机会，可考虑小幅下调最低波动阈值', 'medium')
            elif bias == 'keep_strict':
                push('market_filters.min_volatility', current, current, 'keep', '低波动过滤在该币种上大多帮你避开无效波段', 'low')

        elif code == 'HIGH_VOLATILITY':
            current = float(market_filters.get('max_volatility', 0.05) or 0.05)
            if bias == 'consider_relax':
                push('market_filters.max_volatility', current, round(current * 1.15, 5), 'relax', '该币种高波动过滤后经常错失机会，可考虑小幅放宽最高波动阈值', 'medium')
            elif bias == 'keep_strict':
                push('market_filters.max_volatility', current, current, 'keep', '高波动过滤在该币种上仍有明显避险价值', 'low')

        elif code == 'COUNTER_TREND':
            current = bool(market_filters.get('block_counter_trend', True))
            if bias == 'consider_relax':
                push('market_filters.block_counter_trend', current, '保持全局 true，但对该币做灰度放宽试验', 'review', '同一条逆势过滤在该币种上错失机会偏多，建议先做局部实验而唔系全局放开', 'high')
            elif bias == 'keep_strict':
                push('market_filters.block_counter_trend', current, current, 'keep', '逆势过滤在该币种上明显有效，建议继续严格执行', 'medium')

        elif code == 'COOLDOWN_ACTIVE':
            current = int(trading.get('cooldown_minutes', 15) or 15)
            if bias == 'consider_relax':
                push('trading.cooldown_minutes', current, max(current - 5, 5), 'relax', '冷却期在该币种上可能偏长，导致重复错失顺向机会', 'medium')
            elif bias == 'keep_strict':
                push('trading.cooldown_minutes', current, current, 'keep', '冷却期在该币种上仍有稳定避险作用', 'low')

    advice.sort(key=lambda x: ({'high': 0, 'medium': 1, 'low': 2}.get(x['priority'], 9), x['action'] != 'relax', -(x['missed_profit'] + x['avoided_loss']), x['symbol'], x['parameter']))
    return advice


def _set_nested_value(target: Dict[str, Any], key: str, value: Any):
    cursor = target
    parts = key.split('.')
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def _build_symbol_override_draft(advice_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    symbol_overrides: Dict[str, Dict[str, Any]] = {}
    skipped: List[Dict[str, Any]] = []

    for row in advice_rows:
        action = row.get('action')
        if action == 'keep':
            continue
        symbol = row.get('symbol') or '--'
        parameter = row.get('parameter')
        suggested = row.get('suggested_value')
        if action == 'review' and parameter == 'market_filters.block_counter_trend':
            skipped.append({
                'symbol': symbol,
                'parameter': parameter,
                'action': action,
                'reason': '需要灰度实验，暂不自动写入 override 草案',
            })
            continue
        if not parameter or suggested is None:
            continue
        bucket = symbol_overrides.setdefault(symbol, {})
        _set_nested_value(bucket, parameter, suggested)

    return {
        'symbol_overrides': symbol_overrides,
        'skipped': skipped,
    }


def _count_nested_leaf_values(data: Any) -> int:
    if isinstance(data, dict):
        return sum(_count_nested_leaf_values(v) for v in data.values())
    return 1


def _render_override_yaml(draft: Dict[str, Any]) -> str:
    symbol_overrides = draft.get('symbol_overrides', {}) or {}
    if not symbol_overrides:
        return 'symbol_overrides: {}\n'
    return yaml.safe_dump({'symbol_overrides': symbol_overrides}, allow_unicode=True, sort_keys=False)


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


@app.route('/api/signals/filter-diagnostics')
def get_signal_filter_diagnostics():
    """获取标准化过滤诊断排行"""
    limit = int(request.args.get('limit', 20))
    signals = db.get_signals(limit=1000)
    buckets = {}
    for row in signals:
        if not row.get('filtered'):
            continue
        code = row.get('filter_code') or 'UNCLASSIFIED'
        bucket = buckets.setdefault(code, {
            'code': code,
            'group': row.get('filter_group') or 'other',
            'reason': row.get('filter_reason') or '未注明原因',
            'action_hint': row.get('action_hint') or '继续观察',
            'count': 0,
            'symbols': set(),
        })
        bucket['count'] += 1
        if row.get('symbol'):
            bucket['symbols'].add(row.get('symbol'))
    data = []
    for bucket in buckets.values():
        bucket['symbols'] = sorted(bucket['symbols'])
        bucket['symbol_count'] = len(bucket['symbols'])
        data.append(bucket)
    data.sort(key=lambda x: (-x['count'], x['code']))
    return jsonify({'success': True, 'data': data[:limit], 'count': len(data)})


@app.route('/api/signals/filter-effectiveness')
def get_signal_filter_effectiveness():
    """获取过滤规则后验有效性统计"""
    days = int(request.args.get('days', 7))
    limit = int(request.args.get('limit', 100))
    window_hours = int(request.args.get('window_hours', 24))
    min_move_pct = float(request.args.get('min_move_pct', 1.5))
    cutoff = datetime.now() - timedelta(days=days)
    signals = [
        row for row in db.get_signals(limit=max(limit * 3, 200))
        if row.get('filtered') and (_parse_created_at(row.get('created_at')) or datetime.min) >= cutoff
    ]
    signals = signals[:limit]
    data = _build_filter_effectiveness(
        signals,
        _get_exchange_client(),
        window_hours=window_hours,
        min_move_pct=min_move_pct,
        now=datetime.now(),
    )
    summary = {
        'days': days,
        'window_hours': window_hours,
        'min_move_pct': min_move_pct,
        'signals': len(signals),
        'rules': len(data),
        'analyzed': sum(row.get('analyzed', 0) for row in data),
        'avoided_loss': sum(row.get('avoided_loss', 0) for row in data),
        'missed_profit': sum(row.get('missed_profit', 0) for row in data),
        'neutral': sum(row.get('neutral', 0) for row in data),
        'pending': sum(row.get('pending', 0) for row in data),
        'insufficient_data': sum(row.get('insufficient_data', 0) for row in data),
    }
    return jsonify({'success': True, 'data': data, 'summary': summary, 'count': len(data)})


@app.route('/api/signals/filter-effectiveness/by-symbol')
def get_signal_filter_effectiveness_by_symbol():
    """获取按币种 × 过滤规则的后验有效性统计"""
    days = int(request.args.get('days', 14))
    limit = int(request.args.get('limit', 200))
    window_hours = int(request.args.get('window_hours', 24))
    min_move_pct = float(request.args.get('min_move_pct', 1.5))
    cutoff = datetime.now() - timedelta(days=days)
    signals = [
        row for row in db.get_signals(limit=max(limit * 4, 300))
        if row.get('filtered') and (_parse_created_at(row.get('created_at')) or datetime.min) >= cutoff
    ]
    signals = signals[:limit]
    data = _build_symbol_filter_effectiveness(
        signals,
        _get_exchange_client(),
        window_hours=window_hours,
        min_move_pct=min_move_pct,
        now=datetime.now(),
    )
    summary = {
        'days': days,
        'window_hours': window_hours,
        'min_move_pct': min_move_pct,
        'signals': len(signals),
        'rows': len(data),
        'analyzed': sum(row.get('analyzed', 0) for row in data),
        'consider_relax': sum(1 for row in data if row.get('tuning_bias') == 'consider_relax'),
        'keep_strict': sum(1 for row in data if row.get('tuning_bias') == 'keep_strict'),
        'mixed': sum(1 for row in data if row.get('tuning_bias') == 'mixed'),
        'wait': sum(1 for row in data if row.get('tuning_bias') == 'wait'),
    }
    return jsonify({'success': True, 'data': data, 'summary': summary, 'count': len(data)})


@app.route('/api/signals/parameter-advice')
def get_signal_parameter_advice():
    """获取按币种的参数建议器输出"""
    days = int(request.args.get('days', 14))
    limit = int(request.args.get('limit', 200))
    window_hours = int(request.args.get('window_hours', 24))
    min_move_pct = float(request.args.get('min_move_pct', 1.5))
    cutoff = datetime.now() - timedelta(days=days)
    signals = [
        row for row in db.get_signals(limit=max(limit * 4, 300))
        if row.get('filtered') and (_parse_created_at(row.get('created_at')) or datetime.min) >= cutoff
    ]
    signals = signals[:limit]
    symbol_rows = _build_symbol_filter_effectiveness(
        signals,
        _get_exchange_client(),
        window_hours=window_hours,
        min_move_pct=min_move_pct,
        now=datetime.now(),
    )
    data = _build_symbol_parameter_advice(symbol_rows, config)
    summary = {
        'days': days,
        'window_hours': window_hours,
        'min_move_pct': min_move_pct,
        'signals': len(signals),
        'advice_count': len(data),
        'relax': sum(1 for row in data if row.get('action') == 'relax'),
        'keep': sum(1 for row in data if row.get('action') == 'keep'),
        'review': sum(1 for row in data if row.get('action') == 'review'),
        'high_priority': sum(1 for row in data if row.get('priority') == 'high'),
    }
    return jsonify({'success': True, 'data': data, 'summary': summary, 'count': len(data)})


@app.route('/api/signals/parameter-advice/draft')
def get_signal_parameter_advice_draft():
    """根据参数建议生成 symbol override 草案"""
    days = int(request.args.get('days', 14))
    limit = int(request.args.get('limit', 200))
    window_hours = int(request.args.get('window_hours', 24))
    min_move_pct = float(request.args.get('min_move_pct', 1.5))
    cutoff = datetime.now() - timedelta(days=days)
    signals = [
        row for row in db.get_signals(limit=max(limit * 4, 300))
        if row.get('filtered') and (_parse_created_at(row.get('created_at')) or datetime.min) >= cutoff
    ]
    signals = signals[:limit]
    symbol_rows = _build_symbol_filter_effectiveness(
        signals,
        _get_exchange_client(),
        window_hours=window_hours,
        min_move_pct=min_move_pct,
        now=datetime.now(),
    )
    advice_rows = _build_symbol_parameter_advice(symbol_rows, config)
    draft = _build_symbol_override_draft(advice_rows)
    yaml_text = _render_override_yaml(draft)
    symbol_overrides = draft.get('symbol_overrides', {}) or {}
    summary = {
        'days': days,
        'window_hours': window_hours,
        'min_move_pct': min_move_pct,
        'signals': len(signals),
        'advice_count': len(advice_rows),
        'draft_symbols': len(symbol_overrides),
        'draft_parameters': _count_nested_leaf_values(symbol_overrides) if symbol_overrides else 0,
        'skipped': len(draft.get('skipped', []) or []),
    }
    return jsonify({
        'success': True,
        'data': {
            'draft': draft,
            'yaml': yaml_text,
            'advice': advice_rows,
        },
        'summary': summary,
    })


@app.route('/api/signals/coin-breakdown')
def get_signal_coin_breakdown():
    """按币种拆解观望 / 过滤 / 执行情况"""
    limit = int(request.args.get('limit', 1000))
    days = int(request.args.get('days', 1))
    include_all = str(request.args.get('all', '')).lower() in ('1', 'true', 'yes')
    signals = db.get_signals(limit=limit)
    cutoff = datetime.now() - timedelta(days=days)
    filtered_signals = []
    for row in signals:
        created_at = row.get('created_at')
        try:
            created_dt = datetime.fromisoformat(created_at)
        except Exception:
            created_dt = None
        if include_all or not created_dt or created_dt >= cutoff:
            filtered_signals.append(row)

    grouped = {}
    for row in filtered_signals:
        symbol = row.get('symbol') or '--'
        bucket = grouped.setdefault(symbol, {
            'symbol': symbol,
            'total_signals': 0,
            'hold_signals': 0,
            'filtered_signals': 0,
            'executed_signals': 0,
            'buy_signals': 0,
            'sell_signals': 0,
            'latest_time': None,
            'latest_signal_type': None,
            'latest_filter_reason': None,
            'latest_filter_code': None,
            'latest_filter_group': None,
            'latest_action_hint': None,
            'latest_status': 'sample_low',
            'top_filter_reasons': [],
            'top_filter_codes': [],
            'filter_groups': {},
            '_reason_counts': {},
            '_code_counts': {},
            '_group_counts': {},
            '_latest_sort_key': ('', -1),
        })
        bucket['total_signals'] += 1
        signal_type = row.get('signal_type')
        if signal_type == 'hold':
            bucket['hold_signals'] += 1
        elif signal_type == 'buy':
            bucket['buy_signals'] += 1
        elif signal_type == 'sell':
            bucket['sell_signals'] += 1

        if row.get('filtered'):
            bucket['filtered_signals'] += 1
            reason = row.get('filter_reason') or '未注明原因'
            code = row.get('filter_code') or 'UNCLASSIFIED'
            group = row.get('filter_group') or 'other'
            bucket['_reason_counts'][reason] = bucket['_reason_counts'].get(reason, 0) + 1
            bucket['_code_counts'][code] = bucket['_code_counts'].get(code, 0) + 1
            bucket['_group_counts'][group] = bucket['_group_counts'].get(group, 0) + 1
        if row.get('executed'):
            bucket['executed_signals'] += 1

        created_at = row.get('created_at') or ''
        sort_key = (created_at, int(row.get('id') or 0))
        if sort_key >= bucket['_latest_sort_key']:
            bucket['_latest_sort_key'] = sort_key
            bucket['latest_time'] = created_at or None
            bucket['latest_signal_type'] = signal_type
            bucket['latest_filter_reason'] = row.get('filter_reason')
            bucket['latest_filter_code'] = row.get('filter_code')
            bucket['latest_filter_group'] = row.get('filter_group')
            bucket['latest_action_hint'] = row.get('action_hint')
            if row.get('executed'):
                bucket['latest_status'] = 'executed'
            elif signal_type == 'hold':
                bucket['latest_status'] = 'watch'
            elif row.get('filtered'):
                bucket['latest_status'] = 'filtered'
            elif signal_type in ('buy', 'sell'):
                bucket['latest_status'] = 'direction_ready'
            else:
                bucket['latest_status'] = 'sample_low'

    rows = []
    for bucket in grouped.values():
        bucket.pop('_latest_sort_key', None)
        reason_rows = sorted(
            ({'reason': k, 'count': v} for k, v in bucket.pop('_reason_counts', {}).items()),
            key=lambda x: x['count'], reverse=True
        )
        code_rows = sorted(
            ({'code': k, 'count': v} for k, v in bucket.pop('_code_counts', {}).items()),
            key=lambda x: x['count'], reverse=True
        )
        group_counts = bucket.pop('_group_counts', {})
        bucket['top_filter_reasons'] = reason_rows[:3]
        bucket['top_filter_codes'] = code_rows[:3]
        bucket['filter_groups'] = group_counts
        hold_count = bucket['hold_signals']
        filtered_count = bucket['filtered_signals']
        executed_count = bucket['executed_signals']
        if executed_count > 0:
            bucket['breakdown_conclusion'] = '已有执行样本'
        elif filtered_count > hold_count and filtered_count > 0:
            bucket['breakdown_conclusion'] = '当前主要被过滤'
        elif hold_count > 0:
            bucket['breakdown_conclusion'] = '当前主要观望'
        else:
            bucket['breakdown_conclusion'] = '样本仍少'

        dominant_group = None
        if bucket['filter_groups']:
            dominant_group = sorted(bucket['filter_groups'].items(), key=lambda x: (-x[1], x[0]))[0][0]
        bucket['dominant_filter_group'] = dominant_group
        if bucket.get('latest_status') == 'filtered':
            bucket['diagnostic_action'] = bucket.get('latest_action_hint') or '先处理最新过滤项再观察后续样本'
        elif bucket.get('latest_status') == 'watch':
            bucket['diagnostic_action'] = '当前以观望为主，优先关注方向形成而不是贸然放宽风控'
        elif bucket.get('latest_status') == 'executed':
            bucket['diagnostic_action'] = '已有执行样本，下一步观察平仓质量与收益稳定性'
        elif bucket.get('latest_status') == 'direction_ready':
            bucket['diagnostic_action'] = '方向已形成，继续关注是否会被风控或市场条件拦截'
        else:
            bucket['diagnostic_action'] = '当前样本仍少，继续积累信号再判断'
        rows.append(bucket)

    status_rank = {
        'filtered': 0,
        'watch': 1,
        'direction_ready': 2,
        'executed': 3,
        'sample_low': 4,
    }
    rows.sort(key=lambda x: (status_rank.get(x.get('latest_status'), 99), -x.get('total_signals', 0), x.get('symbol', '')))

    summary = {
        'symbols': len(rows),
        'total_signals': sum(x['total_signals'] for x in rows),
        'hold_signals': sum(x['hold_signals'] for x in rows),
        'filtered_signals': sum(x['filtered_signals'] for x in rows),
        'executed_signals': sum(x['executed_signals'] for x in rows),
        'watch_symbols': sum(1 for x in rows if x.get('latest_status') == 'watch'),
        'filtered_symbols': sum(1 for x in rows if x.get('latest_status') == 'filtered'),
        'executed_symbols': sum(1 for x in rows if x.get('latest_status') == 'executed'),
        'group_breakdown': {
            'signal': sum((x.get('filter_groups') or {}).get('signal', 0) for x in rows),
            'market': sum((x.get('filter_groups') or {}).get('market', 0) for x in rows),
            'risk': sum((x.get('filter_groups') or {}).get('risk', 0) for x in rows),
            'position': sum((x.get('filter_groups') or {}).get('position', 0) for x in rows),
            'other': sum((x.get('filter_groups') or {}).get('other', 0) for x in rows),
        },
        'period_days': days,
    }
    return jsonify({'success': True, 'data': rows, 'summary': summary, 'count': len(rows)})


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
        date = (trade.get('close_time') or trade.get('open_time') or '')[:10]
        if not date:
            continue
        if date not in daily_data:
            daily_data[date] = {
                'date': date,
                'trades': 0,
                'wins': 0,
                'losses': 0,
                'pnl': 0.0
            }
        
        daily_data[date]['trades'] += 1
        pnl = float(trade.get('pnl', 0) or 0)
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

@app.route('/api/system/smoke-runs')
def get_smoke_runs():
    """获取 smoke 验收执行记录"""
    limit = int(request.args.get('limit', 20))
    data = db.get_smoke_runs(limit=limit)
    return jsonify({'success': True, 'data': data, 'count': len(data)})


@app.route('/api/system/reconcile-report')
def get_reconcile_report():
    """获取最新交易所/本地持仓/本地 open trades 对账报告"""
    cfg = Config()
    exchange = Exchange(cfg.all)
    report = reconcile_exchange_positions(exchange, Database(cfg.db_path))
    return jsonify({'success': True, 'data': report, 'summary': report.get('summary', {})})


@app.route('/api/system/runtime-state')
def get_runtime_state():
    """获取守护运行状态"""
    return jsonify({'success': True, 'data': load_runtime_state()})


@app.route('/api/system/notification-outbox')
def get_notification_outbox():
    """获取待桥接通知队列"""
    status = request.args.get('status', 'pending')
    limit = int(request.args.get('limit', 50))
    rows = db.get_notification_outbox(status=status, limit=limit)
    return jsonify({'success': True, 'data': rows, 'count': len(rows)})


@app.route('/api/system/notification-outbox-stats')
def get_notification_outbox_stats():
    """获取通知队列统计，用于堆积监控"""
    return jsonify({'success': True, 'data': db.get_notification_outbox_stats()})


@app.route('/api/system/notification-outbox/<int:notification_id>/deliver', methods=['POST'])
def mark_notification_outbox_delivered(notification_id: int):
    payload = request.get_json(silent=True) or {}
    status = payload.get('status') or 'delivered'
    db.mark_notification_delivered(notification_id, status=status)
    return jsonify({'success': True, 'id': notification_id, 'status': status})


@app.route('/api/system/smoke-state')
def get_smoke_state():
    """获取 smoke 执行状态"""
    return jsonify({'success': True, 'data': smoke_execution_state})


@app.route('/api/system/smoke-impact-latest')
def get_latest_smoke_impact():
    """获取最近一次 smoke 影响摘要"""
    row = db.get_latest_smoke_run()
    impact = (row or {}).get('details', {}).get('impact') if row else None
    return jsonify({'success': True, 'data': impact, 'run': row})


@app.route('/api/system/smoke-execute', methods=['POST'])
def execute_smoke_run():
    """从 dashboard 触发 testnet smoke 验收"""
    payload = request.get_json(silent=True) or {}
    symbol = payload.get('symbol')
    side = str(payload.get('side') or 'long').lower()
    if side not in {'long', 'short'}:
        return jsonify({'success': False, 'error': 'side must be long or short'}), 400
    if not smoke_execution_lock.acquire(blocking=False):
        return jsonify({'success': False, 'error': '已有 smoke 验收执行中，请稍候', 'data': smoke_execution_state}), 409
    smoke_execution_state.update({
        'running': True,
        'symbol': symbol,
        'side': side,
        'started_at': datetime.now().isoformat(),
    })
    try:
        cfg = Config()
        exchange = Exchange(cfg.all)
        before_trades = len(db.get_trades(limit=50))
        before_positions = len(db.get_positions())
        result = execute_exchange_smoke(cfg, exchange, symbol=symbol, side=side, db=Database(cfg.db_path))
        after_trades = len(db.get_trades(limit=50))
        after_positions = len(db.get_positions())
        impact = {
            'smokeRunId': result.get('smoke_run_id'),
            'symbol': symbol,
            'side': side,
            'tradeDelta': after_trades - before_trades,
            'positionDelta': after_positions - before_positions,
            'error': result.get('error'),
        }
        if result.get('smoke_run_id'):
            latest = db.get_latest_smoke_run() or {}
            details = latest.get('details', {}) if latest.get('id') == result.get('smoke_run_id') else {}
            details['impact'] = impact
            db.update_smoke_run_details(result['smoke_run_id'], details)
        result['impact'] = impact
        smoke_execution_state['last_result'] = {
            'success': not bool(result.get('error')),
            'symbol': symbol,
            'side': side,
            'finished_at': datetime.now().isoformat(),
            'error': result.get('error'),
            'smoke_run_id': result.get('smoke_run_id'),
            'impact': impact,
        }
        if result.get('error'):
            return jsonify({'success': False, 'error': result['error'], 'data': result}), 400
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        smoke_execution_state['last_result'] = {
            'success': False,
            'symbol': symbol,
            'side': side,
            'finished_at': datetime.now().isoformat(),
            'error': str(e),
        }
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        smoke_execution_state.update({'running': False, 'symbol': None, 'side': None, 'started_at': None})
        smoke_execution_lock.release()


@app.route('/api/system/account-diagnostic')
def get_account_diagnostic():
    """获取当前交易所账户/持仓绑定诊断"""
    cfg = Config()
    exchange = Exchange(cfg.all)
    result = {
        'exchange_mode': cfg.exchange_mode,
        'position_mode': cfg.position_mode,
        'watch_list': cfg.symbols,
        'balance_free_usdt': None,
        'balance_total_usdt': None,
        'raw_positions_count': 0,
        'filtered_positions_count': 0,
        'raw_positions_preview': [],
        'market_checks': [],
    }
    try:
        balance = exchange.fetch_balance()
        result['balance_free_usdt'] = float((balance.get('free') or {}).get('USDT', 0) or 0)
        result['balance_total_usdt'] = float((balance.get('total') or {}).get('USDT', result['balance_free_usdt']) or result['balance_free_usdt'] or 0)
        result['balance_info'] = balance.get('info', {})
    except Exception as e:
        result['balance_error'] = str(e)
    try:
        raw_positions = exchange.exchange.fetch_positions()
        result['raw_positions_count'] = len(raw_positions or [])
        result['raw_positions_preview'] = raw_positions[:5]
    except Exception as e:
        result['raw_positions_error'] = str(e)
        raw_positions = []
    try:
        filtered_positions = exchange.fetch_positions()
        result['filtered_positions_count'] = len(filtered_positions or [])
        result['filtered_positions_preview'] = filtered_positions[:5]
    except Exception as e:
        result['filtered_positions_error'] = str(e)
    for symbol in cfg.symbols:
        market = exchange.get_market(symbol)
        result['market_checks'].append({
            'symbol': symbol,
            'market_found': bool(market),
            'order_symbol': market.get('symbol') if market else None,
            'swap': bool(market and market.get('swap')),
            'linear': bool(market and market.get('linear')),
        })
    if result['raw_positions_count'] == 0:
        result['conclusion'] = '交易所原始持仓返回为 0，当前更像账户本身无模拟仓，而不是前端没显示。'
    elif result['raw_positions_count'] > 0 and result['filtered_positions_count'] == 0:
        result['conclusion'] = '交易所有原始持仓，但项目过滤后为 0，需重点检查持仓字段映射/过滤条件。'
    else:
        result['conclusion'] = '交易所持仓读取正常，可继续检查 dashboard 渲染或统计逻辑。'
    return jsonify({'success': True, 'data': result})


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
    balance = risk.get('balance', {}) if isinstance(risk, dict) else {}

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
                'unrealized_pnl': round(unrealized_pnl, 2),
                'balance_total': round(float(balance.get('total', 0) or 0), 2),
                'balance_free': round(float(balance.get('free', 0) or 0), 2),
                'balance_used': round(float(balance.get('used', 0) or 0), 2)
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
    return jsonify({'success': True, 'data': governance.evaluate(persist=False)})


@app.route('/api/daily-summary')
def get_daily_summary_report():
    """生成/获取今日摘要"""
    force_refresh = str(request.args.get('refresh', '')).lower() in ('1', 'true', 'yes')
    return jsonify({'success': True, 'data': governance.generate_daily_summary(force_refresh=force_refresh)})


@app.route('/api/approvals/pending')
def get_pending_approvals():
    """获取待审批的建议"""
    gov = governance.evaluate(use_cache=True, persist=False)
    pending = []
    for alert in gov.get('alerts', []):
        if alert.get('approval_required') and alert.get('approval_pending', True):
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
    
    if decision == 'approved' and approval_type in ('main_pool_downgrade', 'pool_switch'):
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


@app.route('/api/runtime/cleanup', methods=['GET', 'POST'])
def runtime_cleanup():
    """预览/执行运行期重复记录清理"""
    dry_run = request.method != 'POST'
    result = db.cleanup_duplicate_runtime_records(dry_run=dry_run)
    return jsonify({'success': True, 'data': result})


@app.route('/api/changes/recent')
def get_recent_changes():
    """获取最近变化汇总"""
    preset_history = db.get_preset_history(limit=5)
    candidate_history = db.get_candidate_reviews(limit=5)
    approval_history = db.get_approval_history(limit=5)
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
    for item in approval_history:
        rows.append({
            'time': item.get('created_at'),
            'type': 'approval',
            'title': item.get('approval_type'),
            'detail': f"{item.get('decision')} | {item.get('target') or '--'}"
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

    gov = governance.evaluate(use_cache=True, persist=False)
    for row in gov.get('alerts', []):
        status = row.get('approval_status')
        suffix = ''
        if row.get('approval_required'):
            if row.get('approval_pending'):
                suffix = '（待审批）'
            elif status == 'approved':
                suffix = '（已批准）'
            elif status == 'rejected':
                suffix = '（已拒绝）'
        alerts.append({
            'level': row.get('level', 'info'),
            'message': f"{row.get('message')}{suffix}",
            'type': row.get('type'),
            'approval_status': status,
            'approval_pending': row.get('approval_pending', False),
        })

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


@app.route('/api/config/governance', methods=['POST'])
def update_governance_config():
    """更新治理参数配置"""
    global risk_manager, ml_engine, backtester, signal_quality_analyzer, optimizer, governance, preset_manager
    payload = request.get_json(silent=True) or {}
    pool_switch = payload.get('pool_switch') or {}
    note = (payload.get('note') or '').strip()

    before = {
        'min_hold_hours': int(config.get('governance.pool_switch.min_hold_hours', 6) or 6),
        'score_margin': float(config.get('governance.pool_switch.score_margin', 1.0) or 1.0),
    }
    min_hold_hours = int(pool_switch.get('min_hold_hours', before['min_hold_hours']))
    score_margin = float(pool_switch.get('score_margin', before['score_margin']))

    if min_hold_hours < 0:
        return jsonify({'success': False, 'error': 'min_hold_hours must be >= 0'}), 400
    if score_margin < 0:
        return jsonify({'success': False, 'error': 'score_margin must be >= 0'}), 400

    after = {
        'min_hold_hours': min_hold_hours,
        'score_margin': score_margin,
    }

    config.set('governance.pool_switch.min_hold_hours', min_hold_hours)
    config.set('governance.pool_switch.score_margin', score_margin)
    config.save()
    config.reload()

    if before != after:
        db.record_governance_config_change(
            config_key='governance.pool_switch',
            before_value=before,
            after_value=after,
            details={'payload': payload, 'note': note or None}
        )

    risk_manager = RiskManager(config, db)
    ml_engine = MLEngine(config.all)
    backtester = StrategyBacktester(config)
    signal_quality_analyzer = SignalQualityAnalyzer(config, db)
    optimizer = ParameterOptimizer(config, db)
    governance = GovernanceEngine(config, db)
    preset_manager = PresetManager(config)

    return jsonify({
        'success': True,
        'data': {
            'governance': config.get('governance', {}),
            'before': before,
            'after': after,
            'message': '治理参数已保存并重新加载。'
        }
    })


@app.route('/api/config/governance/history')
def get_governance_config_history():
    """获取治理参数变更历史"""
    limit = int(request.args.get('limit', 20))
    return jsonify({'success': True, 'data': db.get_governance_config_history(limit=limit)})


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
