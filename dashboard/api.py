"""
仪表盘后端API - Flask实现
"""
from flask import Flask, jsonify, request, send_from_directory, render_template
from flask_cors import CORS
from datetime import datetime, timedelta
import pandas as pd
from typing import Dict, List, Any
import os
import threading
import shutil
import json
from pathlib import Path
import yaml
import difflib

# 初始化Flask
app = Flask(__name__, static_folder='templates', static_url_path='')
CORS(app)

def _ensure_workflow_ready_payload(calibration_report, payload):
    payload = payload or {}
    if isinstance(payload, dict) and payload.get('schema_version') and payload.get('workflow_state') and payload.get('approval_state'):
        return payload
    fallback = (calibration_report or {}).get('workflow_ready') or {}
    if fallback.get('schema_version') and fallback.get('workflow_state') and fallback.get('approval_state'):
        return fallback
    return build_governance_workflow_ready_payload(calibration_report or {})


# Custom JSON encoder to handle NaN/Inf values
import math

class SafeJSONProvider(app.json.__class__):
    """Custom JSON provider that converts NaN/Inf to null"""
    
    def dumps(self, obj, **kwargs):
        import json as _json
        
        def fix_nan(o):
            if isinstance(o, dict):
                return {k: fix_nan(v) for k, v in o.items()}
            elif isinstance(o, list):
                return [fix_nan(v) for v in o]
            elif isinstance(o, float):
                if math.isnan(o) or math.isinf(o):
                    return None
            return o
        
        return _json.dumps(fix_nan(obj), **kwargs)

# Replace the app's JSON provider
app.json = SafeJSONProvider(app)

from core.config import Config
from core.database import Database
from core.exchange import Exchange
from core.presets import PresetManager
from trading.executor import RiskManager
from core.risk_budget import get_risk_budget_config, summarize_margin_usage, compute_entry_plan
from signals.validator import SignalValidator
from bot.run import execute_exchange_smoke, reconcile_exchange_positions, load_runtime_state
from ml.engine import MLEngine
from core.regime import RegimeDetector, detect_regime, Regime
from analytics import StrategyBacktester, SignalQualityAnalyzer, ParameterOptimizer, GovernanceEngine, build_workflow_approval_records, merge_persisted_approval_state, build_approval_audit_overview, build_transition_journal_overview, attach_auto_approval_policy, execute_controlled_rollout_layer, execute_controlled_auto_approval_layer, execute_auto_promotion_review_queue_layer, execute_recovery_queue_layer, execute_adaptive_rollout_orchestration, execute_rollout_executor, build_rollout_control_plane_manifest, build_control_plane_readiness_summary, build_workflow_consumer_view, build_workflow_recovery_view, build_workflow_attention_view, build_workflow_operator_digest, build_workflow_alert_digest, build_dashboard_summary_cards, build_runtime_orchestration_summary, build_production_rollout_readiness, build_workbench_governance_view, build_workbench_governance_filter_view, build_workbench_governance_detail_view, build_workbench_merged_timeline, build_workbench_timeline_summary_aggregation, build_unified_workbench_overview, build_auto_promotion_candidate_view, build_auto_promotion_review_queue_filter_view, build_auto_promotion_review_queue_detail_view
from analytics.backtest import export_calibration_payload, build_governance_workflow_ready_payload
from analytics.mfe_mae import MFEAnalyzer, get_mfe_mae_analysis
from core.regime_policy import summarize_observe_only_collection

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


def _persist_workflow_approval_payload(payload: Dict[str, Any], replay_source: str = 'workflow_ready') -> Dict[str, Any]:
    payload = attach_auto_approval_policy(payload)
    approval_records = build_workflow_approval_records(payload)
    if not approval_records:
        payload['auto_approval_execution'] = {
            'enabled': bool(config.get('governance.auto_approval_execution.enabled', False)),
            'mode': str(config.get('governance.auto_approval_execution.mode', 'disabled') or 'disabled'),
            'actor': str(config.get('governance.auto_approval_execution.actor', 'system:auto-approval') or 'system:auto-approval'),
            'source': str(config.get('governance.auto_approval_execution.source', 'auto_approval_execution') or 'auto_approval_execution'),
            'replay_source': replay_source,
            'executed_count': 0,
            'skipped_count': 0,
            'items': [],
        }
        return payload
    db.sync_approval_items(approval_records, replay_source=replay_source, preserve_terminal=True)
    persisted_rows = [db.get_approval_state(row.get('item_id')) for row in approval_records if row.get('item_id')]
    persisted_rows = [row for row in persisted_rows if row]
    payload = attach_auto_approval_policy(merge_persisted_approval_state(payload, persisted_rows))
    payload = execute_adaptive_rollout_orchestration(payload, db, config=config, replay_source=replay_source)
    payload = attach_auto_approval_policy(payload)
    build_workflow_consumer_view(payload)
    return payload


def _get_exchange_client():
    global _exchange_client
    if _exchange_client is None:
        _exchange_client = Exchange(config.all)
    return _exchange_client


def _refresh_runtime_components():
    global risk_manager, ml_engine, backtester, signal_quality_analyzer, optimizer, governance, preset_manager, _exchange_client
    config.reload()
    risk_manager = RiskManager(config, db)
    ml_engine = MLEngine(config.all)
    backtester = StrategyBacktester(config)
    signal_quality_analyzer = SignalQualityAnalyzer(config, db)
    optimizer = ParameterOptimizer(config, db)
    governance = GovernanceEngine(config, db)
    preset_manager = PresetManager(config)
    _exchange_client = None


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


def _get_local_config_path() -> Path:
    return Path(config.config_path).with_name('config.local.yaml')


# ========== 配置审计日志 ==========
CONFIG_AUDIT_PATH = Path(__file__).parent.parent / 'data' / 'config_audit.json'

def _load_config_audit(limit: int = 50) -> List[Dict]:
    """加载配置审计记录"""
    try:
        if CONFIG_AUDIT_PATH.exists():
            data = json.loads(CONFIG_AUDIT_PATH.read_text())
            return data.get('records', [])[:limit]
    except Exception:
        pass
    return []


def _save_config_audit_record(record: Dict):
    """保存配置审计记录"""
    try:
        CONFIG_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        
        existing = []
        if CONFIG_AUDIT_PATH.exists():
            try:
                existing = json.loads(CONFIG_AUDIT_PATH.read_text()).get('records', [])
            except Exception:
                existing = []
        
        existing.insert(0, record)
        # 保留最近100条记录
        existing = existing[:100]
        
        CONFIG_AUDIT_PATH.write_text(json.dumps({'records': existing}, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[config_audit] 保存审计记录失败: {e}")


def _get_local_backups_dir() -> Path:
    local_path = _get_local_config_path()
    backups_dir = local_path.parent / 'backups'
    backups_dir.mkdir(parents=True, exist_ok=True)
    return backups_dir


def _cleanup_old_backups(backup_dir: Path, keep: int = 10):
    """清理旧备份，保留最近 keep 份"""
    try:
        backups = sorted(backup_dir.glob('config.local-*.yaml'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old_backup in backups[keep:]:
            old_backup.unlink()
    except Exception as e:
        print(f"[backup] 清理旧备份失败: {e}")


def _list_config_backups() -> List[Dict[str, Any]]:
    """列出 config.local.yaml 的所有备份"""
    backups = []
    backup_dir = _get_local_backups_dir()
    for path in sorted(backup_dir.glob('config.local-*.yaml'), key=lambda p: p.stat().st_mtime, reverse=True):
        backups.append({
            'name': path.name,
            'path': str(path),
            'size': path.stat().st_size,
            'modified_at': datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
        })
    return backups


def _build_effective_symbol_override_view(cfg: Config) -> List[Dict[str, Any]]:
    rows = []
    overrides = cfg.get('symbol_overrides', {}) or {}
    for symbol in sorted(overrides.keys()):
        symbol_override = overrides.get(symbol) or {}
        composite = cfg.get_symbol_section(symbol, 'strategies.composite') if hasattr(cfg, 'get_symbol_section') else cfg.get('strategies.composite', {}) or {}
        market_filters = cfg.get_symbol_section(symbol, 'market_filters') if hasattr(cfg, 'get_symbol_section') else cfg.get('market_filters', {}) or {}
        trading = cfg.get_symbol_section(symbol, 'trading') if hasattr(cfg, 'get_symbol_section') else cfg.get('trading', {}) or {}
        rows.append({
            'symbol': symbol,
            'override': symbol_override,
            'effective': {
                'strategies': {
                    'composite': {
                        'min_strength': composite.get('min_strength'),
                        'min_strategy_count': composite.get('min_strategy_count'),
                    }
                },
                'market_filters': {
                    'min_volatility': market_filters.get('min_volatility'),
                    'max_volatility': market_filters.get('max_volatility'),
                    'block_counter_trend': market_filters.get('block_counter_trend'),
                },
                'trading': {
                    'cooldown_minutes': trading.get('cooldown_minutes'),
                    'position_size': trading.get('position_size'),
                    'max_exposure': trading.get('max_exposure'),
                }
            }
        })
    return rows


def _list_local_override_backups() -> List[Dict[str, Any]]:
    backups = []
    for path in sorted(_get_local_backups_dir().glob('config.local-*.yaml'), reverse=True):
        backups.append({
            'name': path.name,
            'path': str(path),
            'size': path.stat().st_size,
            'modified_at': datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
        })
    return backups


def _apply_symbol_override_draft(draft: Dict[str, Any], note: str = None) -> Dict[str, Any]:
    local_path = _get_local_config_path()
    local_path.parent.mkdir(parents=True, exist_ok=True)
    backups_dir = _get_local_backups_dir()

    before = {}
    if local_path.exists():
        with open(local_path, 'r', encoding='utf-8') as f:
            before = yaml.safe_load(f) or {}

    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = backups_dir / f'config.local-{timestamp}.yaml'
    if local_path.exists():
        shutil.copy2(local_path, backup_path)

    symbol_overrides = (draft.get('symbol_overrides') or {}) if isinstance(draft, dict) else {}
    after = dict(before)
    after['symbol_overrides'] = config._deep_merge(before.get('symbol_overrides', {}) or {}, symbol_overrides)

    with open(local_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(after, f, allow_unicode=True, sort_keys=False)

    db.record_override_audit(
        action='apply',
        target_file=str(local_path),
        backup_path=str(backup_path) if backup_path.exists() else None,
        symbols=sorted(symbol_overrides.keys()),
        parameter_count=_count_nested_leaf_values(symbol_overrides) if symbol_overrides else 0,
        note=note,
        details={
            'draft': draft,
            'before_symbol_overrides': before.get('symbol_overrides', {}) or {},
            'after_symbol_overrides': after.get('symbol_overrides', {}) or {},
        }
    )

    _refresh_runtime_components()

    return {
        'config_path': str(local_path),
        'backup_path': str(backup_path) if backup_path.exists() else None,
        'applied_symbols': sorted(symbol_overrides.keys()),
        'applied_symbol_count': len(symbol_overrides),
        'applied_parameter_count': _count_nested_leaf_values(symbol_overrides) if symbol_overrides else 0,
        'skipped': draft.get('skipped', []) if isinstance(draft, dict) else [],
        'note': note,
        'message': 'override 草案已写入 config.local.yaml，并已重新加载配置。',
    }


# ============================================================================
# 根路由
# ============================================================================

@app.route('/')
def index():
    """仪表盘首页 - 重定向到 overview"""
    return render_template('overview.html', active_page='overview')


# ============================================================================
# 新版多页面路由
# ============================================================================

@app.route('/overview')
def overview_page():
    """总览页面"""
    return render_template('overview.html', active_page='overview')


@app.route('/trades')
def trades_page():
    """交易记录页面"""
    return render_template('trades.html', active_page='trades')


@app.route('/partial-tp')
def partial_tp_page():
    """Partial TP 触发历史页面"""
    return render_template('partial_tp.html', active_page='partial_tp')


@app.route('/signals')
def signals_page():
    """信号记录页面"""
    return render_template('signals.html', active_page='signals')


@app.route('/positions')
def positions_page():
    """持仓页面"""
    return render_template('positions.html', active_page='positions')


@app.route('/strategy')
def strategy_page():
    """策略分析页面"""
    return render_template('strategy.html', active_page='strategy')


@app.route('/risk')
def risk_page():
    """风控状态页面"""
    return render_template('risk.html', active_page='risk')


@app.route('/governance')
def governance_page():
    """治理审批页面"""
    return render_template('governance.html', active_page='governance')


@app.route('/optimizer')
def optimizer_page():
    """参数优化页面"""
    return render_template('optimizer.html', active_page='optimizer')


@app.route('/backtest')
def backtest_page():
    """回测分析页面"""
    return render_template('backtest.html', active_page='backtest')


@app.route('/quality')
def quality_page():
    """信号质量页面"""
    return render_template('quality.html', active_page='quality')


@app.route('/config')
def config_page_view():
    """系统配置页面"""
    return render_template('config.html', active_page='config')



def _get_live_position_snapshot_map():
    """尽量使用交易所实时持仓覆盖展示字段；失败时回退数据库。"""
    try:
        exchange = _get_exchange_client()
        rows = exchange.fetch_positions() or []
    except Exception:
        return {}
    result = {}
    for row in rows:
        normalized = row if row.get('contract_size') is not None else exchange.normalize_position(row)
        if not normalized:
            continue
        key = (normalized.get('symbol'), normalized.get('side'))
        result[key] = normalized
    return result


def _merge_position_with_exchange_snapshot(position: Dict[str, Any], snapshot: Dict[str, Any] = None) -> Dict[str, Any]:
    data = dict(position or {})
    if snapshot:
        for field in ('entry_price', 'current_price', 'leverage', 'quantity', 'contracts', 'contract_size', 'coin_quantity', 'realized_pnl'):
            value = snapshot.get(field)
            if value not in (None, ''):
                data[field] = value
        data['sync_source'] = 'exchange'
    else:
        data['sync_source'] = data.get('sync_source') or 'database'

    quantity = float(data.get('quantity') or data.get('contracts') or 0)
    contract_size = float(data.get('contract_size') or 1)
    stored_coin_qty = data.get('coin_quantity')
    coin_qty = float(stored_coin_qty) if stored_coin_qty not in (None, '') and not (isinstance(stored_coin_qty, float) and math.isnan(stored_coin_qty)) else quantity * contract_size
    data['quantity'] = quantity
    data['contracts'] = quantity
    data['contract_size'] = contract_size
    data['coin_quantity'] = coin_qty

    current_price = data.get('current_price')
    entry_price = data.get('entry_price')
    if current_price is None or (isinstance(current_price, float) and math.isnan(current_price)):
        price = entry_price if entry_price else 0
    else:
        price = current_price
    price = float(price) if price else 0
    leverage = float(data.get('leverage') or 1)
    data['notional_value'] = round(coin_qty * price, 2)
    data['margin'] = round(data['notional_value'] / leverage, 2) if leverage > 0 else data['notional_value']
    return data


# ============================================================================
# 交易数据API
# ============================================================================

@app.route('/api/positions')
def get_positions():
    """获取当前持仓（展示优先使用交易所实时同步字段）"""
    positions = db.get_positions()
    live_map = _get_live_position_snapshot_map()
    merged = []
    for p in positions:
        snapshot = live_map.get((p.get('symbol'), p.get('side')))
        merged.append(_merge_position_with_exchange_snapshot(p, snapshot))
    return jsonify({
        'success': True,
        'data': merged,
        'count': len(merged)
    })


@app.route('/api/trades')
def get_trades():
    """获取交易记录"""
    symbol = request.args.get('symbol')
    status = request.args.get('status')
    limit = int(request.args.get('limit', 100))
    
    trades = db.get_trades(symbol=symbol, status=status, limit=limit)
    live_map = _get_live_position_snapshot_map()
    # 补充显示用字段：名义价值、估算保证金；open trade 若仍有交易所持仓则优先展示交易所真实杠杆/数量
    for t in trades:
        snapshot = None
        if str(t.get('status') or '').lower() == 'open':
            snapshot = live_map.get((t.get('symbol'), t.get('side')))
            if snapshot:
                for field in ('leverage', 'quantity', 'contracts', 'contract_size', 'coin_quantity', 'entry_price', 'current_price'):
                    value = snapshot.get(field)
                    if value not in (None, ''):
                        t[field] = value
                t['sync_source'] = 'exchange'
        if not t.get('sync_source'):
            if t.get('close_source') and str(t.get('close_source')).startswith('exchange'):
                t['sync_source'] = 'exchange_history'
            elif t.get('close_source') == 'ticker_fallback':
                t['sync_source'] = 'ticker_fallback'
            else:
                t['sync_source'] = 'database'
        quantity = float(t.get('quantity') or t.get('contracts') or 0)
        contract_size = float(t.get('contract_size') or 1)
        stored_coin_qty = t.get('coin_quantity')
        coin_qty = float(stored_coin_qty) if stored_coin_qty not in (None, '') and not (isinstance(stored_coin_qty, float) and math.isnan(stored_coin_qty)) else quantity * contract_size
        t['quantity'] = quantity
        t['contracts'] = quantity
        t['coin_quantity'] = coin_qty
        exit_price = t.get('exit_price')
        entry_price = t.get('entry_price')
        if exit_price is None or (isinstance(exit_price, float) and math.isnan(exit_price)):
            price = entry_price if entry_price else 0
        else:
            price = exit_price
        price = float(price) if price else 0
        leverage = float(t.get('leverage') or 1)
        t['notional_value'] = round(coin_qty * price, 2)
        t['margin'] = round(t['notional_value'] / leverage, 2) if leverage > 0 else t['notional_value']
        t['close_source'] = t.get('close_source') or 'legacy'
    
    return jsonify({
        'success': True,
        'data': trades,
        'count': len(trades)
    })


@app.route('/api/partial-tp-history')
def get_partial_tp_history():
    """获取 partial TP 触发历史"""
    symbol = request.args.get('symbol')
    limit = int(request.args.get('limit', 100))
    
    history = db.get_partial_tp_history(symbol=symbol, limit=limit)
    
    return jsonify({
        'success': True,
        'data': history,
        'count': len(history)
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


@app.route('/api/signals/parameter-advice/apply-draft', methods=['POST'])
def apply_signal_parameter_advice_draft():
    """将 override 草案安全写入 config.local.yaml"""
    payload = request.get_json(silent=True) or {}
    note = (payload.get('note') or '').strip() or None
    draft = payload.get('draft')

    if not draft:
        days = int(payload.get('days', 14))
        limit = int(payload.get('limit', 200))
        window_hours = int(payload.get('window_hours', 24))
        min_move_pct = float(payload.get('min_move_pct', 1.5))
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

    symbol_overrides = (draft.get('symbol_overrides') or {}) if isinstance(draft, dict) else {}
    if not symbol_overrides:
        return jsonify({'success': False, 'error': 'draft has no applicable symbol_overrides'}), 400

    result = _apply_symbol_override_draft(draft, note=note)
    return jsonify({'success': True, 'data': result})


@app.route('/api/signals/overrides/status')
def get_signal_override_status():
    """查看当前 local override 生效状态"""
    local_path = _get_local_config_path()
    local_cfg = {}
    if local_path.exists():
        with open(local_path, 'r', encoding='utf-8') as f:
            local_cfg = yaml.safe_load(f) or {}
    rows = _build_effective_symbol_override_view(config)
    return jsonify({
        'success': True,
        'data': {
            'config_path': str(local_path),
            'exists': local_path.exists(),
            'symbol_override_count': len(local_cfg.get('symbol_overrides', {}) or {}),
            'rows': rows,
        }
    })


@app.route('/api/signals/overrides/backups')
def get_signal_override_backups():
    """列出 override local config backups"""
    return jsonify({'success': True, 'data': _list_local_override_backups()})


@app.route('/api/signals/overrides/rollback', methods=['POST'])
def rollback_signal_override_backup():
    """回滚 config.local.yaml 到指定 backup"""
    payload = request.get_json(silent=True) or {}
    backup_name = payload.get('backup_name') or payload.get('name')
    if not backup_name:
        return jsonify({'success': False, 'error': 'missing backup_name'}), 400

    backup_path = _get_local_backups_dir() / backup_name
    if not backup_path.exists():
        return jsonify({'success': False, 'error': 'backup not found'}), 404

    local_path = _get_local_config_path()
    restore_backup_path = None
    if local_path.exists():
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        restore_backup_path = _get_local_backups_dir() / f'config.local-rollback-before-{timestamp}.yaml'
        shutil.copy2(local_path, restore_backup_path)

    shutil.copy2(backup_path, local_path)
    restored_symbols = {}
    with open(local_path, 'r', encoding='utf-8') as f:
        restored_cfg = yaml.safe_load(f) or {}
        restored_symbols = restored_cfg.get('symbol_overrides', {}) or {}

    db.record_override_audit(
        action='rollback',
        target_file=str(local_path),
        backup_path=str(backup_path),
        symbols=sorted(restored_symbols.keys()),
        parameter_count=_count_nested_leaf_values(restored_symbols) if restored_symbols else 0,
        note=None,
        details={
            'rolled_back_to': str(backup_path),
            'pre_restore_backup': str(restore_backup_path) if restore_backup_path else None,
            'restored_symbol_overrides': restored_symbols,
        }
    )

    _refresh_runtime_components()

    return jsonify({
        'success': True,
        'data': {
            'config_path': str(local_path),
            'rolled_back_to': str(backup_path),
            'pre_restore_backup': str(restore_backup_path) if restore_backup_path else None,
            'message': 'override local config 已回滚并重新加载。'
        }
    })


@app.route('/api/signals/overrides/history')
def get_signal_override_history():
    """获取 override 审计历史"""
    limit = int(request.args.get('limit', 30))
    return jsonify({'success': True, 'data': db.get_override_audit_history(limit=limit)})


def _classify_blocker_dimension(code, group, reason):
    """将过滤码分类到主阻塞维度"""
    code_upper = (code or '').upper()
    reason_lower = (reason or '').lower()
    
    # direction - 方向相关
    if 'DIRECTION' in code_upper or 'NO_DIRECTION' in code_upper:
        return 'direction'
    # trend - 趋势相关
    if 'TREND' in code_upper or 'COUNTER_TREND' in code_upper:
        return 'trend'
    # volatility - 波动率相关
    if 'VOLATILITY' in code_upper or 'VOLUME' in code_upper:
        return 'volatility'
    # cooldown - 冷却期相关
    if 'COOLDOWN' in code_upper or 'COOLING' in code_upper or 'RECENT' in code_upper:
        return 'cooldown'
    # position - 持仓相关
    if 'POSITION' in code_upper or 'EXISTS' in code_upper or 'HOLDING' in code_upper:
        return 'position'
    # risk - 风险相关
    if 'RISK' in code_upper or 'DRAWDOWN' in code_upper or 'LOSS' in code_upper or group == 'risk':
        return 'risk'
    # other - 其他
    return 'other'


@app.route('/api/signals/coin-breakdown')
def get_signal_coin_breakdown():
    """按币种拆解观望 / 过滤 / 执行情况，包含诊断层字段"""
    limit = int(request.args.get('limit', 1000))
    days = int(request.args.get('days', 1))
    include_all = str(request.args.get('all', '')).lower() in ('1', 'true', 'yes')
    
    # 支持 24h vs 48h 对比
    compare_24h = request.args.get('compare_24h', 'true').lower() in ('1', 'true', 'yes')
    
    signals = db.get_signals(limit=limit)
    now = datetime.now()
    
    # 主周期过滤
    cutoff = now - timedelta(days=days)
    filtered_signals = []
    for row in signals:
        created_at = row.get('created_at')
        try:
            created_dt = datetime.fromisoformat(created_at)
        except Exception:
            created_dt = None
        if include_all or not created_dt or created_dt >= cutoff:
            filtered_signals.append(row)

    # 24h vs 48h 对比数据
    signals_24h = [s for s in filtered_signals if datetime.fromisoformat(s['created_at']) >= (now - timedelta(days=1))]
    signals_48h = [s for s in filtered_signals if datetime.fromisoformat(s['created_at']) >= (now - timedelta(days=2))]
    
    # 主周期内每个币的统计
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
            '_dimension_counts': {},
            '_24h_signals': 0,
            '_48h_signals': 0,
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
            dimension = _classify_blocker_dimension(code, group, reason)
            bucket['_reason_counts'][reason] = bucket['_reason_counts'].get(reason, 0) + 1
            bucket['_code_counts'][code] = bucket['_code_counts'].get(code, 0) + 1
            bucket['_group_counts'][group] = bucket['_group_counts'].get(group, 0) + 1
            bucket['_dimension_counts'][dimension] = bucket['_dimension_counts'].get(dimension, 0) + 1
        if row.get('executed'):
            bucket['executed_signals'] += 1

        # 24h vs 48h 统计
        try:
            created_dt = datetime.fromisoformat(row.get('created_at'))
            if created_dt >= (now - timedelta(days=1)):
                bucket['_24h_signals'] += 1
            if created_dt >= (now - timedelta(days=2)):
                bucket['_48h_signals'] += 1
        except Exception:
            pass

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
        dimension_counts = bucket.pop('_dimension_counts', {})
        
        bucket['top_filter_reasons'] = reason_rows[:3]
        bucket['top_filter_codes'] = code_rows[:3]
        bucket['filter_groups'] = group_counts
        bucket['blocker_dimension'] = dimension_counts
        
        # 主阻塞维度
        if dimension_counts:
            dominant_dimension = sorted(dimension_counts.items(), key=lambda x: (-x[1], x[0]))[0][0]
            bucket['dominant_blocker'] = dominant_dimension
        else:
            bucket['dominant_blocker'] = None
        
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
        
        # 24h vs 48h 对比
        bucket['samples_24h'] = bucket.pop('_24h_signals', 0)
        bucket['samples_48h'] = bucket.pop('_48h_signals', 0)
        bucket['samples_24h_vs_48h'] = bucket['samples_48h'] - bucket['samples_24h'] if compare_24h else None
        bucket['sample_status'] = 'sufficient' if bucket['samples_48h'] >= 3 else 'insufficient'
        
        # 诊断建议
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
        
        # 综合 recommendation
        recommendation_parts = []
        if bucket.get('dominant_blocker'):
            dim = bucket['dominant_blocker']
            if dim == 'direction':
                recommendation_parts.append('等方向明确')
            elif dim == 'trend':
                recommendation_parts.append('关注趋势一致性')
            elif dim == 'volatility':
                recommendation_parts.append('等待波动率恢复')
            elif dim == 'cooldown':
                recommendation_parts.append('等待冷却期结束')
            elif dim == 'position':
                recommendation_parts.append('处理现有持仓')
            elif dim == 'risk':
                recommendation_parts.append('关注风险指标')
            else:
                recommendation_parts.append('检查过滤配置')
        
        if bucket.get('sample_status') == 'insufficient':
            recommendation_parts.append('积累更多样本')
        
        bucket['recommendation'] = ' → '.join(recommendation_parts) if recommendation_parts else '继续观察'
        
        rows.append(bucket)

    status_rank = {
        'filtered': 0,
        'watch': 1,
        'direction_ready': 2,
        'executed': 3,
        'sample_low': 4,
    }
    rows.sort(key=lambda x: (status_rank.get(x.get('latest_status'), 99), -x.get('total_signals', 0), x.get('symbol', '')))

    # 全局统计
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
        # 新增：全局阻塞维度统计
        'dimension_breakdown': {
            'direction': sum((x.get('blocker_dimension') or {}).get('direction', 0) for x in rows),
            'trend': sum((x.get('blocker_dimension') or {}).get('trend', 0) for x in rows),
            'volatility': sum((x.get('blocker_dimension') or {}).get('volatility', 0) for x in rows),
            'cooldown': sum((x.get('blocker_dimension') or {}).get('cooldown', 0) for x in rows),
            'position': sum((x.get('blocker_dimension') or {}).get('position', 0) for x in rows),
            'risk': sum((x.get('blocker_dimension') or {}).get('risk', 0) for x in rows),
            'other': sum((x.get('blocker_dimension') or {}).get('other', 0) for x in rows),
        },
        # 新增：样本状态统计
        'sample_status': {
            'sufficient': sum(1 for x in rows if x.get('sample_status') == 'sufficient'),
            'insufficient': sum(1 for x in rows if x.get('sample_status') == 'insufficient'),
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
# Forward Readiness API
# ============================================================================

from signals.readiness import ForwardReadinessChecker, check_forward_readiness

@app.route('/api/forward/readiness')
def get_forward_readiness():
    """
    前向数据就绪检查器
    
    自动判断 forward data 是否达到可开始校准 Entry Decision 阈值/权重的门槛。
    
    返回状态:
    - OBSERVE: 样本未够，继续观察
    - WEAK_READY: 勉强可分析，但分布不足/偏态  
    - READY: 可开始校准
    """
    limit = int(request.args.get('limit', 5000))
    
    # 获取信号数据
    signals = db.get_signals(limit=limit)
    
    # 执行就绪检查
    result = check_forward_readiness(db=db, signals=signals)
    control_plane_manifest = build_rollout_control_plane_manifest()
    control_plane_readiness = build_control_plane_readiness_summary(
        control_plane_manifest=control_plane_manifest,
        readiness=result,
    )
    result['control_plane_manifest'] = control_plane_manifest
    result['control_plane_readiness'] = control_plane_readiness
    result['related_summary'] = {
        'control_plane_readiness': control_plane_readiness,
        'control_plane_manifest': {
            'schema_version': control_plane_manifest.get('schema_version'),
            'compatibility': control_plane_manifest.get('compatibility') or {},
            'contracts': control_plane_manifest.get('contracts') or {},
        },
    }
    
    return jsonify({
        'success': True,
        'data': result,
        'summary': {
            'status': result.get('status'),
            'readiness_pct': result.get('readiness_pct'),
            'control_plane_readiness': control_plane_readiness,
        },
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


def _build_runtime_checklist() -> Dict[str, Any]:
    runtime = load_runtime_state() or {}
    watch_list = config.symbols
    exchange_mode = config.get('exchange.mode', 'testnet')
    model_rows = []
    for symbol in watch_list:
        model_name = ml_engine._symbol_to_name(symbol) if hasattr(ml_engine, '_symbol_to_name') else symbol.replace('/', '_')
        model_file = Path(ml_engine.model_path) / f'{model_name}_model.pkl'
        metrics_file = Path(ml_engine.model_path) / f'{model_name}_metrics.json'
        metrics = None
        if metrics_file.exists():
            try:
                metrics = json.loads(metrics_file.read_text())
            except Exception:
                metrics = None
        model_rows.append({
            'symbol': symbol,
            'model_file': str(model_file),
            'model_exists': model_file.exists(),
            'metrics_exists': metrics_file.exists(),
            'test_accuracy': metrics.get('test_accuracy') if metrics else None,
            'f1': metrics.get('f1') if metrics else None,
        })

    last_summary = runtime.get('last_summary') or {}
    last_error = runtime.get('last_error')
    checks = [
        {
            'key': 'dashboard',
            'label': 'Dashboard 在线',
            'status': 'ok',
            'detail': '当前接口可响应，说明 dashboard 进程在线。',
        },
        {
            'key': 'daemon',
            'label': '守护机器人在线',
            'status': 'ok' if runtime.get('next_run_at') else 'warn',
            'detail': f"next_run_at={runtime.get('next_run_at') or '--'} ｜ last_finished_at={runtime.get('last_finished_at') or '--'}",
        },
        {
            'key': 'exchange_mode',
            'label': '交易环境',
            'status': 'ok' if exchange_mode == 'testnet' else 'warn',
            'detail': f'当前模式：{exchange_mode}',
        },
        {
            'key': 'watch_list',
            'label': '监听币种',
            'status': 'ok' if watch_list else 'bad',
            'detail': ', '.join(watch_list) if watch_list else '未配置 watch_list',
        },
        {
            'key': 'latest_cycle',
            'label': '最近一轮执行',
            'status': 'bad' if last_error else 'ok',
            'detail': f"signals={last_summary.get('signals', 0)} ｜ passed={last_summary.get('passed', 0)} ｜ opened={last_summary.get('opened', 0)} ｜ errors={last_summary.get('errors', 0)}",
        },
        {
            'key': 'models',
            'label': 'ML 模型文件',
            'status': 'ok' if all(row['model_exists'] for row in model_rows) else 'warn',
            'detail': ' ｜ '.join([f"{row['symbol']}: model={'Y' if row['model_exists'] else 'N'}, acc={row['test_accuracy']}" for row in model_rows]) or '--',
        },
    ]
    return {
        'checks': checks,
        'runtime': runtime,
        'models': model_rows,
        'exchange_mode': exchange_mode,
        'watch_list': watch_list,
    }


@app.route('/api/system/status')
def get_system_status():
    """获取系统状态"""
    positions = db.get_positions()
    trade_stats = db.get_trade_stats(days=30)
    signals = db.get_signals(limit=200)
    today_signals = sum(1 for s in signals if s.get('created_at', '').startswith(datetime.now().strftime('%Y-%m-%d')))
    executed_today = sum(1 for s in signals if s.get('created_at', '').startswith(datetime.now().strftime('%Y-%m-%d')) and s.get('executed'))
    total_value = sum(p.get('coin_quantity', p.get('quantity', 0)) * p.get('current_price', 0) for p in positions)
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
            'risk': risk,
            'execution': db.get_execution_state_snapshot()
        }
    })


@app.route('/api/system/execution-state')
def get_execution_state():
    """最小执行态观察接口：layer plan / active intents / direction locks"""
    data = db.get_execution_state_snapshot()
    data['summary']['observe_only_summary'] = data.get('observe_only_summary', {})
    return jsonify({'success': True, 'data': data, 'summary': data.get('summary', {})})


@app.route('/api/system/checklist')
def get_system_checklist():
    """获取运行状态自检清单"""
    return jsonify({'success': True, 'data': _build_runtime_checklist()})


# ============================================================================
# 配置API
# ============================================================================

@app.route('/api/risk/status')
def get_risk_status():
    """获取风险状态"""
    risk_data = risk_manager.get_risk_status()
    
    # 扩展返回：sizing 配置（便于前端展示 10%/30% 规则）
    trading_cfg = config.get('trading', {}) or {}
    risk_data['sizing_config'] = {
        '基础单笔目标': float(get_risk_budget_config(config).get('base_entry_margin_ratio', 0.08)),
        '单笔最小保证金比例': float(get_risk_budget_config(config).get('min_entry_margin_ratio', 0.04)),
        '单笔最大保证金比例': float(get_risk_budget_config(config).get('max_entry_margin_ratio', 0.10)),
        '总保证金软上限': float(get_risk_budget_config(config).get('total_margin_soft_cap_ratio', 0.25)),
        '总保证金硬上限': float(get_risk_budget_config(config).get('total_margin_cap_ratio', 0.30)),
        '单币种上限': float(get_risk_budget_config(config).get('symbol_margin_cap_ratio', 0.12)),
        '允许同向加仓': bool(get_risk_budget_config(config).get('add_position_enabled', False)),
        '配置杠杆': int(trading_cfg.get('leverage', 3)),
    }
    
    return jsonify({
        'success': True,
        'data': risk_data
    })


@app.route('/api/risk/sizing-preview')
def get_risk_sizing_preview():
    """
    开仓 sizing 预览 - 展示如果现在开仓，风险占用会是多少
    
    参数:
    - symbol: 币种 (如 BTC/USDT:USDT)
    - side: 方向 (long/short)
    
    返回:
    - 本次计划保证金
    - 配置杠杆 / 实际杠杆
    - 当前总风险占用
    - 本次开仓后总风险占用
    - 当前单币种占用
    - 本次开仓后单币种占用
    - sizing 规则校验结果（仅检查资金/仓位，不检查策略）
    """
    symbol = request.args.get('symbol', 'BTC/USDT:USDT')
    side = request.args.get('side', 'long')
    
    try:
        # 获取当前持仓
        positions = db.get_positions()
        current_positions = {}
        for p in positions:
            current_positions[p['symbol']] = p
        
        # 获取配置
        trading_cfg = config.get('trading', {}) or {}
        configured_leverage = int(trading_cfg.get('leverage', 3))
        risk_budget = get_risk_budget_config(config, symbol)

        balance = risk_manager._get_balance_summary()
        total_balance = balance.get('total', 0) or 0
        free_balance = balance.get('free', 0)
        usage = summarize_margin_usage(positions, symbol)
        entry_plan = compute_entry_plan(
            total_balance=total_balance,
            free_balance=free_balance,
            current_total_margin=usage['current_total_margin'],
            current_symbol_margin=usage['current_symbol_margin'],
            risk_budget=risk_budget,
        )
        position_ratio = float(entry_plan.get('effective_entry_margin_ratio') or 0.0)
        max_exposure = float(risk_budget.get('total_margin_cap_ratio', 0.3))
        soft_exposure = float(risk_budget.get('total_margin_soft_cap_ratio', 0.25))
        max_per_symbol = float(risk_budget.get('symbol_margin_cap_ratio', 0.12))
        planned_margin = float(entry_plan.get('allowed_margin') or 0.0)
        current_total_exposure = float(entry_plan.get('current_total_exposure_ratio') or 0.0)
        current_symbol_exposure = float(entry_plan.get('current_symbol_exposure_ratio') or 0.0)
        after_open_total = float(entry_plan.get('projected_total_exposure_ratio') or current_total_exposure)
        after_open_symbol = float(entry_plan.get('projected_symbol_exposure_ratio') or current_symbol_exposure)

        total_exposure_ok = (not entry_plan.get('blocked')) and after_open_total <= max_exposure
        symbol_exposure_ok = (not entry_plan.get('blocked')) and after_open_symbol <= max_per_symbol
        balance_ok = free_balance >= planned_margin and planned_margin > 0

        will_pass = total_exposure_ok and symbol_exposure_ok and balance_ok and not entry_plan.get('blocked')
        block_reason = None
        if entry_plan.get('blocked'):
            block_reason = entry_plan.get('block_reason')
        elif not total_exposure_ok:
            block_reason = f'总风险占用超限 ({after_open_total*100:.1f}% > {max_exposure*100:.0f}%)'
        elif not symbol_exposure_ok:
            block_reason = f'单币种占用超限 ({after_open_symbol*100:.1f}% > {max_per_symbol*100:.0f}%)'
        elif not balance_ok:
            block_reason = f'余额不足 ({free_balance:.2f} < {planned_margin:.2f})'
        
        result = {
            'symbol': symbol,
            'side': side,
            'will_pass': will_pass,
            'block_reason': block_reason,
            # 配置信息
            'config': {
                'position_ratio': position_ratio,
                'base_entry_margin_ratio': float(risk_budget.get('base_entry_margin_ratio', 0.08)),
                'min_entry_margin_ratio': float(risk_budget.get('min_entry_margin_ratio', 0.04)),
                'max_entry_margin_ratio': float(risk_budget.get('max_entry_margin_ratio', 0.10)),
                'max_exposure': max_exposure,
                'soft_exposure': soft_exposure,
                'max_per_symbol': max_per_symbol,
                'configured_leverage': configured_leverage,
                'add_position_enabled': bool(risk_budget.get('add_position_enabled', False)),
            },
            # 余额与计划保证金
            'balance': {
                'total': balance.get('total', 0),
                'free': balance.get('free', 0),
                'planned_margin': round(planned_margin, 2),
                'target_margin': round(float(entry_plan.get('target_margin') or 0), 2),
            },
            # 杠杆
            'leverage': {
                'configured': configured_leverage,
                'effective': configured_leverage,  # 简化显示，实际以交易所为准
            },
            # 风险占用
            'exposure': {
                'current_total': round(current_total_exposure, 4),
                'after_open_total': round(after_open_total, 4),
                'current_symbol': round(current_symbol_exposure, 4),
                'after_open_symbol': round(after_open_symbol, 4),
                'soft_cap': round(soft_exposure, 4),
            },
            # 规则校验
            'rules_check': {
                'total_exposure_ok': total_exposure_ok,
                'symbol_exposure_ok': symbol_exposure_ok,
                'balance_ok': balance_ok,
                'entry_plan_ok': not entry_plan.get('blocked'),
            },
            'entry_plan': entry_plan,
            # 规则解释（中文化）
            'rules_summary': _build_sizing_summary(
                will_pass, block_reason, 
                current_total_exposure, 
                after_open_total,
                max_exposure,
                after_open_symbol,
                max_per_symbol,
                position_ratio,
                configured_leverage
            )
        }
        
        return jsonify({
            'success': True,
            'data': result
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'error_type': type(e).__name__
        }), 500


def _build_sizing_summary(will_pass, reason, current_total, after_open, max_exp, after_symbol, max_symbol, pos_ratio, lev):
    """构建 sizing 规则执行摘要（中文化）"""
    summary = []
    
    # 10% 规则
    summary.append({
        'rule': '每单保证金',
        'target': f'{pos_ratio*100:.0f}%',
        'current': '0%',
        'after_open': f'{pos_ratio*100:.0f}%',
        'status': 'ok',
        'desc': f'每次开仓占用 {pos_ratio*100:.0f}% 保证金'
    })
    
    # 30% 总仓规则
    total_status = 'ok' if after_open <= max_exp else 'over'
    summary.append({
        'rule': '总风险占用',
        'target': f'{max_exp*100:.0f}%',
        'current': f'{current_total*100:.1f}%',
        'after_open': f'{after_open*100:.1f}%',
        'status': total_status,
        'desc': f'开仓后总占用 {after_open*100:.1f}% (上限 {max_exp*100:.0f}%)'
    })
    
    # 单币种规则
    sym_status = 'ok' if after_symbol <= max_symbol else 'over'
    summary.append({
        'rule': '单币种占用',
        'target': f'{max_symbol*100:.0f}%',
        'current': f'{max(0, after_symbol-pos_ratio)*100:.1f}%',
        'after_open': f'{after_symbol*100:.1f}%',
        'status': sym_status,
        'desc': f'该币种开仓后占用 {after_symbol*100:.1f}% (上限 {max_symbol*100:.0f}%)'
    })
    
    # 杠杆
    summary.append({
        'rule': '杠杆倍数',
        'target': f'{lev}x',
        'current': '-',
        'after_open': f'{lev}x',
        'status': 'ok',
        'desc': f'配置杠杆 {lev}x，实际以交易所为准'
    })
    
    # 总体结论
    if will_pass:
        summary.append({
            'rule': '✅ 开仓可行性',
            'target': '通过',
            'current': '-',
            'after_open': '通过',
            'status': 'ok',
            'desc': '系统将按 10% 单仓 / 30% 总仓 规则执行'
        })
    else:
        summary.append({
            'rule': '❌ 开仓可行性',
            'target': '拦截',
            'current': '-',
            'after_open': '拦截',
            'status': 'blocked',
            'desc': f'原因: {reason}'
        })
    
    return summary


# ============================================================================
# Regime 市场状态API
# ============================================================================

@app.route('/api/regime')
def get_regime():
    """
    获取当前市场状态 (Regime Detection)
    
    返回:
    - regime: 当前市场状态 (trend/range/high_vol/low_vol/risk_anomaly/unknown)
    - confidence: 置信度 0-1
    - indicators: 原始指标值
    - details: 简要说明
    """
    symbol = request.args.get('symbol', 'BTC/USDT:USDT')
    timeframe = request.args.get('timeframe', '1h')
    limit = int(request.args.get('limit', 100))
    
    try:
        # 获取K线数据
        exchange = _get_exchange_client()
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv or len(ohlcv) < 30:
            return jsonify({
                'success': False,
                'error': '数据不足，需要至少30根K线'
            }), 400
        
        # 转换为DataFrame
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        # 检测regime
        result = detect_regime(df)
        
        return jsonify({
            'success': True,
            'data': {
                'symbol': symbol,
                'timeframe': timeframe,
                'regime': result.regime.value,
                'confidence': round(result.confidence, 3),
                'indicators': result.indicators,
                'details': result.details,
                'timestamp': datetime.now().isoformat()
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'error_type': type(e).__name__
        }), 500


@app.route('/api/regime/distribution')
def get_regime_distribution():
    """
    获取 Regime 分布统计 - 按币种
    
    返回:
    - 按币种当前 regime 状态
    - 最近一段时间的 regime 分布统计
    """
    timeframe = request.args.get('timeframe', '1h')
    limit = int(request.args.get('limit', 100))  # K线根数
    hours = int(request.args.get('hours', 48))   # 统计时间窗口
    
    symbols = config.symbols
    if not symbols:
        return jsonify({'success': False, 'error': '未配置监听币种'}), 400
    
    results = []
    regime_counts = {
        'trend': 0,
        'range': 0,
        'high_vol': 0,
        'low_vol': 0,
        'risk_anomaly': 0,
        'unknown': 0
    }
    success_count = 0
    error_count = 0
    
    exchange = _get_exchange_client()
    
    for symbol in symbols:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            
            if not ohlcv or len(ohlcv) < 30:
                results.append({
                    'symbol': symbol,
                    'regime': 'unknown',
                    'confidence': 0,
                    'details': '数据不足',
                    'error': '数据不足'
                })
                error_count += 1
                continue
            
            # 转换为DataFrame
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            # 检测regime
            result = detect_regime(df)
            regime_value = result.regime.value
            
            results.append({
                'symbol': symbol,
                'regime': regime_value,
                'confidence': round(result.confidence, 3),
                'details': result.details,
                'indicators': {k: round(v, 5) if isinstance(v, float) else v 
                              for k, v in result.indicators.items()} if result.indicators else {}
            })
            regime_counts[regime_value] = regime_counts.get(regime_value, 0) + 1
            success_count += 1
            
        except Exception as e:
            results.append({
                'symbol': symbol,
                'regime': 'unknown',
                'confidence': 0,
                'details': '获取失败',
                'error': str(e)
            })
            error_count += 1
    
    # 计算分布百分比
    total = success_count + error_count
    distribution = {}
    for regime, count in regime_counts.items():
        distribution[regime] = {
            'count': count,
            'percentage': round(count / max(total, 1) * 100, 1) if total > 0 else 0
        }
    
    # 找出主导 regime
    dominant_regime = max(regime_counts.items(), key=lambda x: x[1])[0] if regime_counts else 'unknown'
    
    return jsonify({
        'success': True,
        'data': {
            'symbols': results,
            'summary': {
                'total_symbols': len(symbols),
                'success_count': success_count,
                'error_count': error_count,
                'dominant_regime': dominant_regime,
                'distribution': distribution,
                'hours': hours,
                'timeframe': timeframe
            },
            'timestamp': datetime.now().isoformat()
        }
    })


@app.route('/api/risk/loss-streak/reset', methods=['POST'])
def reset_loss_streak_guard():
    """手动清零连亏熔断状态（幂等 API）"""
    payload = request.get_json(silent=True) or {}
    note = (payload.get('note') or '').strip() or 'manual-reset'
    state = risk_manager.manual_reset_loss_streak(note=note)
    
    # Record approval history only when actually reset (not idempotent)
    is_idempotent = state.get('idempotent', False)
    if not is_idempotent:
        db.record_approval('loss_streak_reset', 'loss_streak', 'approved', {'note': note, 'state': state})
        _refresh_runtime_components()
    
    # Build response message based on idempotency
    if is_idempotent:
        message = '连亏熔断当前未锁定，无需重复清零。'
    else:
        message = '连亏熔断已手动清零，系统恢复开仓资格。'
    
    return jsonify({
        'success': True, 
        'data': {
            'state': state, 
            'message': message,
            'idempotent': is_idempotent,
            'action': state.get('action', 'unknown')
        }
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


def _load_recent_transition_journal_overview(*, limit: int = 5, approval_type: str = None,
                                            item_id: str = None, target: str = None,
                                            changed_only: bool = True) -> Dict[str, Any]:
    rows = db.get_recent_transition_journal(
        limit=limit,
        approval_type=approval_type,
        item_id=item_id,
        target=target,
        changed_only=changed_only,
    )
    summary = db.get_transition_journal_summary(
        limit=limit,
        approval_type=approval_type,
        item_id=item_id,
        target=target,
        changed_only=changed_only,
    )
    return build_transition_journal_overview(transition_rows=rows, summary=summary)


@app.route('/api/backtest/calibration-report')
def get_backtest_calibration_report():
    """获取 M5 calibration 聚合输出，默认返回 report-ready payload。"""
    view = (request.args.get('view') or 'report_ready').strip().lower()
    if view not in {'report_ready', 'delivery', 'governance_ready', 'workflow_ready', 'operator_digest', 'workflow_alert_digest', 'dashboard_summary_cards', 'runtime_orchestration_summary', 'production_rollout_readiness', 'workbench_governance_view', 'auto_promotion_candidate_view', 'unified_workbench_overview', 'full'}:
        return jsonify({'success': False, 'error': 'view must be one of report_ready|delivery|governance_ready|workflow_ready|operator_digest|workflow_alert_digest|dashboard_summary_cards|runtime_orchestration_summary|production_rollout_readiness|workbench_governance_view|auto_promotion_candidate_view|unified_workbench_overview|full'}), 400

    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(
        calibration_report,
        view='full' if view == 'full' else ('workflow_ready' if view in {'operator_digest', 'workflow_alert_digest', 'dashboard_summary_cards', 'runtime_orchestration_summary', 'production_rollout_readiness', 'workbench_governance_view', 'unified_workbench_overview'} else view),
    )
    if view in {'workflow_ready', 'operator_digest', 'workflow_alert_digest', 'dashboard_summary_cards', 'runtime_orchestration_summary', 'production_rollout_readiness', 'workbench_governance_view', 'auto_promotion_candidate_view', 'unified_workbench_overview', 'full'}:
        workflow_payload = payload if view in {'workflow_ready', 'operator_digest', 'workflow_alert_digest', 'dashboard_summary_cards', 'runtime_orchestration_summary', 'production_rollout_readiness', 'workbench_governance_view', 'auto_promotion_candidate_view', 'unified_workbench_overview'} else (payload.get('workflow_ready') or {})
        payload_to_persist = dict(workflow_payload)
        if payload_to_persist:
            persisted_workflow = _persist_workflow_approval_payload(payload_to_persist, replay_source=f'calibration_report:{view}')
            transition_journal_overview = _load_recent_transition_journal_overview(limit=5)
            if view == 'workflow_ready':
                payload = persisted_workflow
            elif view == 'operator_digest':
                payload = build_workflow_operator_digest(persisted_workflow, transition_journal_overview=transition_journal_overview)
            elif view == 'workflow_alert_digest':
                build_workflow_operator_digest(persisted_workflow, transition_journal_overview=transition_journal_overview)
                payload = build_workflow_alert_digest(persisted_workflow)
            elif view == 'dashboard_summary_cards':
                payload = build_dashboard_summary_cards(persisted_workflow)
            elif view == 'runtime_orchestration_summary':
                payload = build_runtime_orchestration_summary(persisted_workflow, transition_journal_overview=transition_journal_overview)
            elif view == 'production_rollout_readiness':
                payload = build_production_rollout_readiness(persisted_workflow, transition_journal_overview=transition_journal_overview)
            elif view == 'workbench_governance_view':
                payload = build_workbench_governance_view(persisted_workflow, transition_journal_overview=transition_journal_overview)
            elif view == 'auto_promotion_candidate_view':
                payload = build_auto_promotion_candidate_view(
                    persisted_workflow,
                    lane_ids=request.args.get('lane') or request.args.get('lane_ids'),
                    action_types=request.args.get('action') or request.args.get('action_types'),
                    risk_levels=request.args.get('risk') or request.args.get('risk_levels'),
                    workflow_states=request.args.get('workflow_state') or request.args.get('workflow_states'),
                    approval_states=request.args.get('approval_state') or request.args.get('approval_states'),
                    current_rollout_stages=request.args.get('stage') or request.args.get('current_rollout_stages'),
                    target_rollout_stages=request.args.get('target_stage') or request.args.get('target_rollout_stages'),
                    candidate_status=request.args.get('candidate_status'),
                    manual_fallback_required=(request.args.get('manual_fallback_required').lower() in ('1', 'true', 'yes') if request.args.get('manual_fallback_required') is not None else None),
                    q=request.args.get('q'),
                    limit=max(1, min(int(request.args.get('limit', 50)), 200)),
                )
            elif view == 'unified_workbench_overview':
                payload = build_unified_workbench_overview(persisted_workflow, transition_journal_overview=transition_journal_overview)
            else:
                payload['workflow_ready'] = persisted_workflow
                payload['operator_digest'] = build_workflow_operator_digest(persisted_workflow, transition_journal_overview=transition_journal_overview)
                payload['dashboard_summary_cards'] = build_dashboard_summary_cards(persisted_workflow)
                payload['runtime_orchestration_summary'] = build_runtime_orchestration_summary(persisted_workflow, transition_journal_overview=transition_journal_overview)
                payload['production_rollout_readiness'] = build_production_rollout_readiness(persisted_workflow, transition_journal_overview=transition_journal_overview)
                payload['workbench_governance_view'] = build_workbench_governance_view(persisted_workflow, transition_journal_overview=transition_journal_overview)
                payload['auto_promotion_candidate_view'] = build_auto_promotion_candidate_view(persisted_workflow)
                payload['unified_workbench_overview'] = build_unified_workbench_overview(persisted_workflow, transition_journal_overview=transition_journal_overview)
    summary = calibration_report.get('summary') or {}
    governance_ready = payload if view == 'governance_ready' else (payload.get('governance_ready') or {})
    workflow_ready = {} if view in {'operator_digest', 'workflow_alert_digest', 'dashboard_summary_cards', 'runtime_orchestration_summary', 'production_rollout_readiness', 'workbench_governance_view', 'auto_promotion_candidate_view', 'unified_workbench_overview'} else (payload if view == 'workflow_ready' else (payload.get('workflow_ready') or {}))
    return jsonify({
        'success': True,
        'view': view,
        'data': payload,
        'summary': {
            'symbols': len(backtest_result.get('symbols') or []),
            'trade_count': summary.get('trade_count', 0),
            'calibration_ready': bool(summary.get('calibration_ready')),
            'delivery_ready': summary.get('delivery_ready') or {},
            'governance_ready': summary.get('governance_ready') or (governance_ready.get('summary') or {}),
            'workflow_ready': workflow_ready.get('summary') or {},
            'operator_digest': payload.get('summary') or {} if view == 'operator_digest' else (payload.get('workflow_operator_digest') or payload.get('operator_digest') or {}).get('summary') or {},
            'workflow_alert_digest': payload.get('summary') or {} if view == 'workflow_alert_digest' else (payload.get('workflow_alert_digest') or {}).get('summary') or {},
            'dashboard_summary_cards': payload.get('summary') or {} if view == 'dashboard_summary_cards' else (payload.get('dashboard_summary_cards') or {}).get('summary') or {},
            'runtime_orchestration_summary': payload.get('summary') or {} if view == 'runtime_orchestration_summary' else (payload.get('runtime_orchestration_summary') or {}).get('summary') or {},
            'production_rollout_readiness': payload.get('summary') or {} if view == 'production_rollout_readiness' else (payload.get('production_rollout_readiness') or {}).get('summary') or {},
            'workbench_governance_view': payload.get('summary') or {} if view == 'workbench_governance_view' else (payload.get('workbench_governance_view') or {}).get('summary') or {},
            'auto_promotion_candidate_view': payload.get('summary') or {} if view == 'auto_promotion_candidate_view' else (payload.get('auto_promotion_candidate_view') or {}).get('summary') or {},
            'unified_workbench_overview': payload.get('summary') or {} if view == 'unified_workbench_overview' else (payload.get('unified_workbench_overview') or {}).get('summary') or {},
            'joint_governance_summary': summary.get('joint_governance_summary') or {},
        }
    })


@app.route('/api/backtest/workflow-state')
def get_backtest_workflow_state():
    """获取治理 workflow/approval state 层，供 dashboard/agent/低干预审批流直接消费。"""
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _ensure_workflow_ready_payload(calibration_report, payload)
    payload = _persist_workflow_approval_payload(payload, replay_source='workflow_state_api')
    return jsonify({
        'success': True,
        'view': 'workflow_state',
        'data': payload,
        'summary': payload.get('summary') or {},
    })


@app.route('/api/backtest/workflow-consumer-view')
def get_backtest_workflow_consumer_view():
    """返回适合 dashboard/API 消费的统一 workflow consumer view。"""
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _ensure_workflow_ready_payload(calibration_report, payload)
    payload = _persist_workflow_approval_payload(payload, replay_source='workflow_consumer_view_api')
    consumer_view = payload.get('consumer_view') or build_workflow_consumer_view(payload)
    return jsonify({
        'success': True,
        'view': 'workflow_consumer_view',
        'data': consumer_view,
        'summary': consumer_view.get('summary') or {},
    })


@app.route('/api/backtest/workflow-recovery-view')
def get_backtest_workflow_recovery_view():
    """返回 recovery orchestration / retry queue / rollback candidate / manual recovery 视图。"""
    max_items = max(1, min(int(request.args.get('max_items', 50)), 200))
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='workflow_recovery_view_api')
    recovery = build_workflow_recovery_view(payload, max_items=max_items)
    return jsonify({
        'success': True,
        'view': 'workflow_recovery_view',
        'data': recovery,
        'summary': recovery.get('summary') or {},
    })


@app.route('/api/backtest/recovery-execution')
def get_backtest_recovery_execution():
    """返回 recovery queue 消费执行层，说明 retry / rollback / manual recovery 已点样被安全入队。"""
    max_items = max(1, min(int(request.args.get('max_items', 50)), 200))
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='recovery_execution_api')
    payload = execute_recovery_queue_layer(payload, db, config=config, replay_source='recovery_execution_api')
    execution = payload.get('recovery_execution') or {}
    execution['summary'] = {**(execution.get('summary') or {}), 'max_items': max_items}
    return jsonify({
        'success': True,
        'view': 'recovery_execution',
        'data': execution,
        'summary': execution.get('summary') or {},
    })


@app.route('/api/backtest/workflow-attention-view')
def get_backtest_workflow_attention_view():
    """返回聚焦 manual approval / blocked follow-up 的低干预消费视图。"""
    max_items = max(1, min(int(request.args.get('max_items', 50)), 200))
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='workflow_attention_view_api')
    attention = build_workflow_attention_view(payload, max_items=max_items)
    return jsonify({
        'success': True,
        'view': 'workflow_attention_view',
        'data': attention,
        'summary': attention.get('summary') or {},
    })


@app.route('/api/backtest/workflow-operator-digest')
def get_backtest_workflow_operator_digest():
    """返回低干预治理/巡检摘要，优先给 dashboard、agent、人工巡检入口直接消费。"""
    max_items = max(1, min(int(request.args.get('max_items', 5)), 20))
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='workflow_operator_digest_api')
    transition_journal_overview = _load_recent_transition_journal_overview(limit=max_items)
    digest = build_workflow_operator_digest(payload, max_items=max_items, transition_journal_overview=transition_journal_overview)
    return jsonify({
        'success': True,
        'view': 'workflow_operator_digest',
        'data': digest,
        'summary': digest.get('summary') or {},
        'related_summary': digest.get('related_summary') or {},
    })


@app.route('/api/backtest/workflow-alert-digest')
def get_backtest_workflow_alert_digest():
    """返回生产级低干预告警分层摘要，明确咩需要即刻介入、咩只需观察。"""
    max_items = max(1, min(int(request.args.get('max_items', 10)), 50))
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='workflow_alert_digest_api')
    transition_journal_overview = _load_recent_transition_journal_overview(limit=max_items)
    digest = build_workflow_operator_digest(payload, max_items=max_items, transition_journal_overview=transition_journal_overview)
    alert_digest = build_workflow_alert_digest(payload, max_items=max_items)
    return jsonify({
        'success': True,
        'view': 'workflow_alert_digest',
        'data': alert_digest,
        'summary': alert_digest.get('summary') or {},
        'related_summary': alert_digest.get('related_summary') or digest.get('related_summary') or {},
    })


@app.route('/api/backtest/dashboard-summary-cards')
def get_backtest_dashboard_summary_cards():
    """返回适合 dashboard summary cards 的后端聚合摘要。"""
    max_items = max(1, min(int(request.args.get('max_items', 3)), 20))
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='dashboard_summary_cards_api')
    cards = build_dashboard_summary_cards(payload, max_items=max_items)
    return jsonify({
        'success': True,
        'view': 'dashboard_summary_cards',
        'data': cards,
        'summary': cards.get('summary') or {},
    })


@app.route('/api/backtest/runtime-orchestration-summary')
def get_backtest_runtime_orchestration_summary():
    """返回更聚焦的运行期 orchestration 摘要/entrypoint，直接说明最近推进、当前卡点、下一步与 follow-up。"""
    max_items = max(1, min(int(request.args.get('max_items', 5)), 20))
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='runtime_orchestration_summary_api')
    transition_journal_overview = _load_recent_transition_journal_overview(limit=max_items)
    summary = build_runtime_orchestration_summary(payload, max_items=max_items, transition_journal_overview=transition_journal_overview)
    return jsonify({
        'success': True,
        'view': 'runtime_orchestration_summary',
        'data': summary,
        'summary': summary.get('summary') or {},
        'related_summary': summary.get('related_summary') or {},
    })


@app.route('/api/backtest/production-rollout-readiness')
def get_backtest_production_rollout_readiness():
    """返回面向低干预生产推进的统一 readiness gate，固定化上线前/自动推进前巡检口径。"""
    max_items = max(1, min(int(request.args.get('max_items', 5)), 20))
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='production_rollout_readiness_api')
    transition_journal_overview = _load_recent_transition_journal_overview(limit=max_items)
    readiness = build_production_rollout_readiness(payload, max_items=max_items, transition_journal_overview=transition_journal_overview)
    return jsonify({
        'success': True,
        'view': 'production_rollout_readiness',
        'data': readiness,
        'summary': readiness.get('summary') or {},
        'related_summary': readiness.get('related_summary') or {},
    })


@app.route('/api/backtest/workbench-governance-view')
def get_backtest_workbench_governance_view():
    """返回 approval / rollout 工作台聚合视图，适合 dashboard/agent/人工巡检低干预消费。"""
    max_items = max(1, min(int(request.args.get('max_items', 5)), 50))
    max_adjustments = max(1, min(int(request.args.get('max_adjustments', 10)), 50))
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='workbench_governance_view_api')
    transition_journal_overview = _load_recent_transition_journal_overview(limit=max_items)
    workbench_view = build_workbench_governance_view(
        payload,
        max_items=max_items,
        max_adjustments=max_adjustments,
        transition_journal_overview=transition_journal_overview,
        filters={
            'lane_ids': request.args.get('lane') or request.args.get('lane_ids'),
            'action_types': request.args.get('action') or request.args.get('action_types'),
            'risk_levels': request.args.get('risk') or request.args.get('risk_levels'),
            'workflow_states': request.args.get('workflow_state') or request.args.get('workflow_states'),
            'approval_states': request.args.get('approval_state') or request.args.get('approval_states'),
            'current_rollout_stages': request.args.get('stage') or request.args.get('current_rollout_stages'),
            'target_rollout_stages': request.args.get('target_stage') or request.args.get('target_rollout_stages'),
            'bucket_tags': request.args.get('bucket') or request.args.get('bucket_tags'),
            'auto_approval_decisions': request.args.get('auto_decision') or request.args.get('auto_approval_decisions'),
            'operator_actions': request.args.get('operator_action') or request.args.get('operator_actions'),
            'operator_routes': request.args.get('operator_route') or request.args.get('operator_routes'),
            'operator_follow_ups': request.args.get('follow_up') or request.args.get('operator_follow_up') or request.args.get('operator_follow_ups'),
            'owner_hints': request.args.get('owner') or request.args.get('owner_hints'),
            'q': request.args.get('q'),
        },
    )
    return jsonify({
        'success': True,
        'view': 'workbench_governance_view',
        'data': workbench_view,
        'summary': workbench_view.get('summary') or {},
    })



@app.route('/api/backtest/unified-workbench-overview')
def get_backtest_unified_workbench_overview():
    """返回 approval / rollout / recovery 三线统一工作台总览，适合 dashboard/agent/人工低干预巡检。"""
    max_items = max(1, min(int(request.args.get('max_items', 5)), 50))
    max_adjustments = max(1, min(int(request.args.get('max_adjustments', 10)), 50))
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='unified_workbench_overview_api')
    transition_journal_overview = _load_recent_transition_journal_overview(limit=max_items)
    overview = build_unified_workbench_overview(
        payload,
        max_items=max_items,
        max_adjustments=max_adjustments,
        transition_journal_overview=transition_journal_overview,
        filters={
            'lane_ids': request.args.get('lane') or request.args.get('lane_ids'),
            'action_types': request.args.get('action') or request.args.get('action_types'),
            'risk_levels': request.args.get('risk') or request.args.get('risk_levels'),
            'workflow_states': request.args.get('workflow_state') or request.args.get('workflow_states'),
            'approval_states': request.args.get('approval_state') or request.args.get('approval_states'),
            'current_rollout_stages': request.args.get('stage') or request.args.get('current_rollout_stages'),
            'target_rollout_stages': request.args.get('target_stage') or request.args.get('target_rollout_stages'),
            'bucket_tags': request.args.get('bucket') or request.args.get('bucket_tags'),
            'auto_approval_decisions': request.args.get('auto_decision') or request.args.get('auto_approval_decisions'),
            'operator_actions': request.args.get('operator_action') or request.args.get('operator_actions'),
            'operator_routes': request.args.get('operator_route') or request.args.get('operator_routes'),
            'operator_follow_ups': request.args.get('follow_up') or request.args.get('operator_follow_up') or request.args.get('operator_follow_ups'),
            'owner_hints': request.args.get('owner') or request.args.get('owner_hints'),
            'q': request.args.get('q'),
        },
        approval_timeline_fetcher=lambda approval_id, limit: db.get_approval_timeline(item_id=approval_id, limit=limit, ascending=True) if approval_id else [],
    )
    return jsonify({'success': True, 'view': 'unified_workbench_overview', 'data': overview, 'summary': overview.get('summary') or {}, 'related_summary': overview.get('related_summary') or {}})



@app.route('/api/backtest/auto-promotion-summary')
def get_backtest_auto_promotion_summary():
    """返回 controlled auto-promotion 执行摘要，方便 dashboard/workbench 低干预巡检。"""
    limit = max(1, min(int(request.args.get('limit', 20)), 200))
    summary = db.get_auto_promotion_activity_summary(limit=limit, item_id=request.args.get('item_id'))
    return jsonify({'success': True, 'view': 'auto_promotion_execution_summary', 'data': summary, 'summary': {
        'event_count': summary.get('event_count', 0),
        'latest_created_at': summary.get('latest_created_at'),
        'rollback_review_candidate_count': summary.get('rollback_review_candidate_count', 0),
        'stage_transition_counts': summary.get('stage_transition_counts') or {},
    }})


@app.route('/api/backtest/auto-promotion-review-queues')
def get_backtest_auto_promotion_review_queues():
    """返回自动推进后的 post-promotion review / rollback review queue，说明观察点、复核时间与升级路径。"""
    limit = max(1, min(int(request.args.get('limit', 20)), 200))
    summary = db.get_auto_promotion_activity_summary(limit=limit, item_id=request.args.get('item_id'))
    return jsonify({'success': True, 'view': 'auto_promotion_review_queues', 'data': {
        'schema_version': 'm5_auto_promotion_review_queues_v1',
        'summary': {
            'event_count': summary.get('event_count', 0),
            'post_promotion_review_queue_count': summary.get('post_promotion_review_queue_count', 0),
            'rollback_review_queue_count': summary.get('rollback_review_queue_count', 0),
            'rollback_review_candidate_count': summary.get('rollback_review_candidate_count', 0),
            'latest_created_at': summary.get('latest_created_at'),
        },
        'review_queues': summary.get('review_queues') or {'post_promotion_review_queue': [], 'rollback_review_queue': []},
        'recent_items': summary.get('recent_items') or [],
        'rollback_review_candidates': summary.get('rollback_review_candidates') or [],
    }})


@app.route('/api/backtest/auto-promotion-candidates')
def get_backtest_auto_promotion_candidates():
    """返回可自动推进 candidate 清单，直接说明可推进原因、缺口、风险与人工兜底需求。"""
    limit = max(1, min(int(request.args.get('limit', 50)), 200))
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='auto_promotion_candidate_view_api')
    candidate_view = build_auto_promotion_candidate_view(
        payload,
        lane_ids=request.args.get('lane') or request.args.get('lane_ids'),
        action_types=request.args.get('action') or request.args.get('action_types'),
        risk_levels=request.args.get('risk') or request.args.get('risk_levels'),
        workflow_states=request.args.get('workflow_state') or request.args.get('workflow_states'),
        approval_states=request.args.get('approval_state') or request.args.get('approval_states'),
        current_rollout_stages=request.args.get('stage') or request.args.get('current_rollout_stages'),
        target_rollout_stages=request.args.get('target_stage') or request.args.get('target_rollout_stages'),
        candidate_status=request.args.get('candidate_status'),
        manual_fallback_required=(request.args.get('manual_fallback_required').lower() in ('1', 'true', 'yes') if request.args.get('manual_fallback_required') is not None else None),
        q=request.args.get('q'),
        limit=limit,
    )
    return jsonify({'success': True, 'view': 'auto_promotion_candidate_view', 'data': candidate_view, 'summary': candidate_view.get('summary') or {}})



@app.route('/api/backtest/auto-promotion-review-execution')
def get_backtest_auto_promotion_review_execution():
    """返回 auto-promotion follow-up review queue 的受控执行摘要，说明边啲 review 已入队同点解。"""
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='auto_promotion_review_execution_api')
    execution = payload.get('auto_promotion_review_execution') or {}
    return jsonify({'success': True, 'view': 'auto_promotion_review_execution', 'data': execution, 'summary': execution.get('summary') or {}})


@app.route('/api/backtest/auto-promotion-review-items')
def get_backtest_auto_promotion_review_items():
    """返回 auto-promotion review queue item 过滤视图，可按 queue/due/observation target/rollback trigger 钻具体 follow-up。"""
    limit = max(1, min(int(request.args.get('limit', 50)), 200))
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='auto_promotion_review_queue_filter_api')
    filtered = build_auto_promotion_review_queue_filter_view(
        payload,
        queue_kinds=request.args.get('queue_kind') or request.args.get('queue_kinds'),
        due_statuses=request.args.get('due_status') or request.args.get('due_statuses'),
        observation_targets=request.args.get('observation_target') or request.args.get('observation_targets'),
        rollback_triggers=request.args.get('rollback_trigger') or request.args.get('rollback_triggers'),
        q=request.args.get('q'),
        limit=limit,
    )
    return jsonify({'success': True, 'view': 'auto_promotion_review_queue_filter_view', 'data': filtered, 'summary': filtered.get('summary') or {}})


@app.route('/api/backtest/rollout-control-plane')
def get_backtest_rollout_control_plane():
    """返回 rollout control plane version/compatibility manifest，方便低干预升级、审计同回滚前检查。"""
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='rollout_control_plane_manifest_api')
    manifest = build_rollout_control_plane_manifest(payload)
    control_plane_readiness = build_control_plane_readiness_summary(control_plane_manifest=manifest)
    return jsonify({'success': True, 'view': 'rollout_control_plane_manifest', 'data': manifest, 'summary': manifest.get('compatibility') or {}, 'related_summary': {'control_plane_readiness': control_plane_readiness}})


@app.route('/api/backtest/auto-promotion-review-detail')
def get_backtest_auto_promotion_review_detail():
    """返回单个 auto-promotion review item detail，直接说明点解入队、下一步、观察目标同到期时间。"""
    item_id = request.args.get('item_id')
    approval_id = request.args.get('approval_id')
    if not item_id and not approval_id:
        return jsonify({'success': False, 'error': 'item_id or approval_id is required'}), 400
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='auto_promotion_review_queue_detail_api')
    detail = build_auto_promotion_review_queue_detail_view(
        payload,
        item_id=item_id,
        approval_id=approval_id,
        queue_kind=request.args.get('queue_kind'),
        now=request.args.get('now'),
    )
    if not detail.get('found'):
        return jsonify({'success': False, 'error': 'auto-promotion review item not found', 'data': detail}), 404
    return jsonify({'success': True, 'view': 'auto_promotion_review_queue_detail_view', 'data': detail, 'summary': detail.get('summary') or {}})


@app.route('/api/backtest/workbench-governance-items')
def get_backtest_workbench_governance_items():
    """返回 approval / rollout 工作台 item 过滤视图，适合按 lane/action/risk/stage/bucket 快速定位具体项。"""
    limit = max(1, min(int(request.args.get('limit', 50)), 200))
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='workbench_governance_filter_api')
    filtered = build_workbench_governance_filter_view(
        payload,
        lane_ids=request.args.get('lane') or request.args.get('lane_ids'),
        action_types=request.args.get('action') or request.args.get('action_types'),
        risk_levels=request.args.get('risk') or request.args.get('risk_levels'),
        workflow_states=request.args.get('workflow_state') or request.args.get('workflow_states'),
        approval_states=request.args.get('approval_state') or request.args.get('approval_states'),
        current_rollout_stages=request.args.get('stage') or request.args.get('current_rollout_stages'),
        target_rollout_stages=request.args.get('target_stage') or request.args.get('target_rollout_stages'),
        bucket_tags=request.args.get('bucket') or request.args.get('bucket_tags'),
        auto_approval_decisions=request.args.get('auto_decision') or request.args.get('auto_approval_decisions'),
        operator_actions=request.args.get('operator_action') or request.args.get('operator_actions'),
        operator_routes=request.args.get('operator_route') or request.args.get('operator_routes'),
        operator_follow_ups=request.args.get('follow_up') or request.args.get('operator_follow_up') or request.args.get('operator_follow_ups'),
        owner_hints=request.args.get('owner') or request.args.get('owner_hints'),
        q=request.args.get('q'),
        limit=limit,
    )
    return jsonify({'success': True, 'view': 'workbench_governance_filter_view', 'data': filtered, 'summary': filtered.get('summary') or {}})


@app.route('/api/backtest/workbench-governance-detail')
def get_backtest_workbench_governance_detail():
    """返回指定 approval / rollout 工作台 item 的 detail 视图，说明它为何在这里、下一步是什么。"""
    item_id = request.args.get('item_id')
    approval_id = request.args.get('approval_id')
    lane_id = request.args.get('lane') or request.args.get('lane_id')
    if not item_id and not approval_id:
        return jsonify({'success': False, 'error': 'item_id or approval_id is required'}), 400
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='workbench_governance_detail_api')
    approval_timeline = db.get_approval_timeline(item_id=approval_id, limit=200, ascending=True) if approval_id else []
    detail = build_workbench_governance_detail_view(
        payload,
        item_id=item_id,
        approval_id=approval_id,
        lane_id=lane_id,
        operator_action=request.args.get('operator_action'),
        operator_route=request.args.get('operator_route'),
        follow_up=request.args.get('follow_up') or request.args.get('operator_follow_up'),
        approval_timeline=approval_timeline,
    )
    if not detail.get('found'):
        return jsonify({'success': False, 'error': 'workbench item not found', 'data': detail}), 404
    return jsonify({'success': True, 'view': 'workbench_governance_detail_view', 'data': detail, 'summary': detail.get('summary') or {}})


@app.route('/api/backtest/workbench-governance-merged-timeline')
def get_backtest_workbench_governance_merged_timeline():
    """返回指定 item 的 approval DB timeline + executor timeline 合并视图。"""
    item_id = request.args.get('item_id')
    approval_id = request.args.get('approval_id')
    lane_id = request.args.get('lane') or request.args.get('lane_id')
    if not item_id and not approval_id:
        return jsonify({'success': False, 'error': 'item_id or approval_id is required'}), 400
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='workbench_governance_merged_timeline_api')
    detail = build_workbench_governance_detail_view(
        payload,
        item_id=item_id,
        approval_id=approval_id,
        lane_id=lane_id,
        approval_timeline=db.get_approval_timeline(item_id=approval_id, limit=200, ascending=True) if approval_id else [],
    )
    if not detail.get('found'):
        return jsonify({'success': False, 'error': 'workbench item not found', 'data': detail}), 404
    merged_timeline = ((detail.get('drilldown') or {}).get('merged_timeline') or {})
    return jsonify({'success': True, 'view': 'workbench_merged_timeline', 'data': merged_timeline, 'summary': merged_timeline.get('summary') or {}})


@app.route('/api/backtest/workbench-governance-timeline-summary')
def get_backtest_workbench_governance_timeline_summary():
    """按 bucket / action_type / lane 聚合 workbench timeline 摘要，避免逐个点开 detail。"""
    max_groups = max(1, min(int(request.args.get('max_groups', 50)), 100))
    max_items_per_group = max(1, min(int(request.args.get('max_items_per_group', 20)), 100))
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='workbench_governance_timeline_summary_api')
    aggregation = build_workbench_timeline_summary_aggregation(
        payload,
        lane_ids=request.args.get('lane') or request.args.get('lane_ids'),
        action_types=request.args.get('action') or request.args.get('action_types'),
        risk_levels=request.args.get('risk') or request.args.get('risk_levels'),
        workflow_states=request.args.get('workflow_state') or request.args.get('workflow_states'),
        approval_states=request.args.get('approval_state') or request.args.get('approval_states'),
        current_rollout_stages=request.args.get('stage') or request.args.get('current_rollout_stages'),
        target_rollout_stages=request.args.get('target_stage') or request.args.get('target_rollout_stages'),
        bucket_tags=request.args.get('bucket') or request.args.get('bucket_tags'),
        auto_approval_decisions=request.args.get('auto_decision') or request.args.get('auto_approval_decisions'),
        operator_actions=request.args.get('operator_action') or request.args.get('operator_actions'),
        operator_routes=request.args.get('operator_route') or request.args.get('operator_routes'),
        operator_follow_ups=request.args.get('follow_up') or request.args.get('operator_follow_up') or request.args.get('operator_follow_ups'),
        owner_hints=request.args.get('owner') or request.args.get('owner_hints'),
        q=request.args.get('q'),
        approval_timeline_fetcher=lambda approval_id, limit: db.get_approval_timeline(item_id=approval_id, limit=limit, ascending=True) if approval_id else [],
        max_groups=max_groups,
        max_items_per_group=max_items_per_group,
    )
    return jsonify({'success': True, 'view': 'workbench_timeline_summary_aggregation', 'data': aggregation, 'summary': aggregation.get('summary') or {}})


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
            approval_type = alert.get('type')
            target = alert.get('recommended_preset')
            item_id = db.build_approval_item_id(approval_type, target, {'target': target})
            state_row = db.upsert_approval_state(
                item_id=item_id,
                approval_type=approval_type,
                target=target,
                title=alert.get('message'),
                decision=alert.get('approval_status') or 'pending',
                state=alert.get('approval_status') or 'pending',
                workflow_state='pending',
                replay_source='governance_pending_api',
                details=alert,
                preserve_terminal=True,
            )
            pending.append({
                'item_id': item_id,
                'type': approval_type,
                'target': target,
                'message': alert.get('message'),
                'state': state_row.get('state'),
                'decision': state_row.get('decision'),
                'updated_at': state_row.get('updated_at'),
                'details': {**alert, 'persisted_state': state_row.get('state')},
            })
    return jsonify({'success': True, 'data': pending})


@app.route('/api/approvals/execute', methods=['POST'])
def execute_approval():
    """执行审批操作"""
    global risk_manager, ml_engine, backtester, signal_quality_analyzer, optimizer, governance, preset_manager
    payload = request.get_json(silent=True) or {}
    approval_type = payload.get('type')
    target = payload.get('target')
    decision = str(payload.get('decision', 'approved') or 'approved').lower()
    
    if not approval_type or not target:
        return jsonify({'success': False, 'error': 'missing type or target'}), 400

    item_id = db.build_approval_item_id(approval_type, target, payload)
    payload['item_id'] = item_id
    payload['state'] = payload.get('state') or decision
    payload['replay_source'] = payload.get('replay_source') or 'approval_execute_api'
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


@app.route('/api/approvals/state-machine')
def get_approval_state_machine_list():
    """返回审批/rollout 的统一状态机摘要，方便 dashboard/agent 直接消费。"""
    limit = int(request.args.get('limit', 100))
    state = request.args.get('state')
    approval_type = request.args.get('type')
    rows = db.get_approval_states(state=state, approval_type=approval_type, limit=limit)
    auto_promotion_summary = db.get_auto_promotion_activity_summary(limit=limit)
    items = []
    phase_counts = {}
    workflow_counts = {}
    validation_status_counts = {}
    validation_freeze_reason_counts = {}
    rollback_trigger_counts = {}
    for row in rows:
        semantics = ((row.get('details') or {}).get('state_machine') or {})
        phase = semantics.get('phase') or 'unknown'
        workflow_state = semantics.get('workflow_state') or row.get('workflow_state') or 'pending'
        execution_status = semantics.get('execution_status') or ((row.get('details') or {}).get('execution_status') if isinstance(row.get('details'), dict) else None) or 'unknown'
        validation_gate = (
            ((row.get('details') or {}).get('validation_gate') if isinstance(row.get('details'), dict) else None)
            or (((row.get('details') or {}).get('auto_advance_gate') or {}).get('validation_gate') if isinstance((row.get('details') or {}).get('auto_advance_gate'), dict) else None)
            or (((row.get('details') or {}).get('rollback_gate') or {}).get('validation_gate') if isinstance((row.get('details') or {}).get('rollback_gate'), dict) else None)
            or (semantics.get('validation_gate') if isinstance(semantics, dict) else None)
            or {}
        )
        validation_status = 'disabled'
        if validation_gate:
            validation_status = 'ready' if validation_gate.get('ready') else 'frozen'
        validation_status_counts[validation_status] = validation_status_counts.get(validation_status, 0) + 1
        for reason in (validation_gate.get('reasons') or []):
            validation_freeze_reason_counts[str(reason)] = validation_freeze_reason_counts.get(str(reason), 0) + 1
        rollback_gate = ((row.get('details') or {}).get('rollback_gate') if isinstance(row.get('details'), dict) else None) or (semantics.get('rollback_gate') if isinstance(semantics, dict) else None) or {}
        for trigger in (rollback_gate.get('triggered') or []):
            rollback_trigger_counts[str(trigger)] = rollback_trigger_counts.get(str(trigger), 0) + 1
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
        workflow_counts[workflow_state] = workflow_counts.get(workflow_state, 0) + 1
        items.append({
            'item_id': row.get('item_id'),
            'approval_type': row.get('approval_type'),
            'target': row.get('target'),
            'title': row.get('title'),
            'decision': row.get('decision'),
            'state': row.get('state'),
            'workflow_state': row.get('workflow_state'),
            'updated_at': row.get('updated_at'),
            'reason': row.get('reason'),
            'actor': row.get('actor'),
            'execution_status': execution_status,
            'execution_timeline': (semantics.get('execution_timeline') or {}),
            'recovery_policy': (semantics.get('recovery_policy') or {}),
            'validation_gate': validation_gate,
            'rollback_gate': rollback_gate,
            'state_machine': semantics,
        })
    recovery_policy_counts = {policy: sum(1 for row in items if ((row.get('recovery_policy') or {}).get('policy') or 'observe') == policy) for policy in sorted({((row.get('recovery_policy') or {}).get('policy') or 'observe') for row in items})}
    return jsonify({'success': True, 'data': items, 'summary': {
        'count': len(items),
        'phase_counts': phase_counts,
        'workflow_state_counts': workflow_counts,
        'execution_status_counts': {status: sum(1 for row in items if row.get('execution_status') == status) for status in sorted({row.get('execution_status') for row in items if row.get('execution_status')})},
        'validation_status_counts': validation_status_counts,
        'validation_freeze_reason_counts': validation_freeze_reason_counts,
        'rollback_trigger_counts': rollback_trigger_counts,
        'recovery_policy_counts': recovery_policy_counts,
        'recovered_count': sum(1 for row in items if (row.get('execution_timeline') or {}).get('recovered')),
        'rollback_candidate_count': sum(1 for row in items if (row.get('state_machine') or {}).get('rollback_candidate')),
        'retryable_count': sum(1 for row in items if (row.get('state_machine') or {}).get('retryable')),
        'terminal_count': sum(1 for row in items if (row.get('state_machine') or {}).get('terminal')),
    }})

@app.route('/api/approvals/state')
def get_approval_state_list():
    """获取审批持久化状态台账"""
    limit = int(request.args.get('limit', 100))
    state = request.args.get('state')
    approval_type = request.args.get('type')
    rows = db.get_approval_states(state=state, approval_type=approval_type, limit=limit)
    return jsonify({'success': True, 'data': rows, 'summary': {
        'count': len(rows),
        'pending': sum(1 for row in rows if row.get('state') == 'pending'),
        'approved': sum(1 for row in rows if row.get('state') == 'approved'),
        'rejected': sum(1 for row in rows if row.get('state') == 'rejected'),
        'deferred': sum(1 for row in rows if row.get('state') == 'deferred'),
    }})


@app.route('/api/approvals/timeline')
def get_approval_timeline_api():
    """获取 approval immutable event timeline。"""
    limit = int(request.args.get('limit', 100))
    item_id = request.args.get('item_id')
    approval_type = request.args.get('type')
    target = request.args.get('target')
    rows = db.get_approval_timeline(item_id=item_id, approval_type=approval_type, target=target, limit=limit)
    return jsonify({'success': True, 'data': rows, 'summary': {'count': len(rows), 'item_id': item_id, 'type': approval_type, 'target': target}})


@app.route('/api/approvals/timeline-summary')
def get_approval_timeline_summary_api():
    """返回单个 approval item 的 timeline 概览摘要。"""
    item_id = request.args.get('item_id')
    if not item_id:
        return jsonify({'success': False, 'error': 'missing item_id'}), 400
    summary = db.get_approval_timeline_summary(item_id)
    if not summary:
        return jsonify({'success': False, 'error': 'approval item not found'}), 404
    return jsonify({'success': True, 'data': summary, 'summary': {
        'item_id': item_id,
        'event_count': summary.get('event_count'),
        'current_state': (summary.get('current') or {}).get('state'),
        'stale': summary.get('stale', False),
    }})


@app.route('/api/approvals/decision-diff')
def get_approval_decision_diff_api():
    """返回最近 approval decision/state/workflow 变化。"""
    limit = int(request.args.get('limit', 20))
    approval_type = request.args.get('type')
    rows = db.get_recent_approval_decision_diff(limit=limit, approval_type=approval_type)
    return jsonify({'success': True, 'data': rows, 'summary': {
        'count': len(rows),
        'type': approval_type,
    }})


@app.route('/api/approvals/transition-journal')
def get_approval_transition_journal_api():
    """返回最近 approval / rollout / recovery 状态迁移 journal。"""
    limit = int(request.args.get('limit', 20))
    approval_type = request.args.get('type')
    item_id = request.args.get('item_id')
    target = request.args.get('target')
    changed_only = str(request.args.get('changed_only', 'true')).lower() not in ('0', 'false', 'no')
    rows = db.get_recent_transition_journal(limit=limit, approval_type=approval_type, item_id=item_id, target=target, changed_only=changed_only)
    summary = db.get_transition_journal_summary(limit=limit, approval_type=approval_type, item_id=item_id, target=target, changed_only=changed_only)
    overview = build_transition_journal_overview(transition_rows=rows, summary=summary)
    return jsonify({'success': True, 'data': overview, 'summary': overview.get('summary') or {}})


@app.route('/api/approvals/stale')
def get_stale_approvals_api():
    """列出 stale pending/ready approvals，方便低干预巡检。"""
    stale_after_minutes = int(request.args.get('stale_after_minutes', 60))
    limit = int(request.args.get('limit', 100))
    approval_type = request.args.get('type')
    rows = db.get_stale_approval_states(stale_after_minutes=stale_after_minutes, approval_type=approval_type, limit=limit)
    return jsonify({'success': True, 'data': rows, 'summary': {
        'count': len(rows),
        'stale_after_minutes': stale_after_minutes,
        'type': approval_type,
    }})


@app.route('/api/approvals/cleanup', methods=['GET', 'POST'])
def cleanup_stale_approvals_api():
    """预览/执行 stale approval cleanup；仅过期 stale pending，不触发真实执行。"""
    dry_run = request.method != 'POST'
    stale_after_minutes = int(request.args.get('stale_after_minutes') or (request.get_json(silent=True) or {}).get('stale_after_minutes') or 60)
    limit = int(request.args.get('limit') or (request.get_json(silent=True) or {}).get('limit') or 100)
    approval_type = request.args.get('type') or (request.get_json(silent=True) or {}).get('type')
    result = db.cleanup_stale_approval_states(
        stale_after_minutes=stale_after_minutes,
        approval_type=approval_type,
        limit=limit,
        dry_run=dry_run,
    )
    return jsonify({'success': True, 'data': result, 'summary': {
        'dry_run': dry_run,
        'matched_count': result.get('matched_count', 0),
        'expired_count': result.get('expired_count', 0),
    }})


@app.route('/api/approvals/audit-overview')
def get_approval_audit_overview_api():
    """聚合 stale pending、decision diff、timeline summary。"""
    stale_after_minutes = int(request.args.get('stale_after_minutes', 60))
    limit = int(request.args.get('limit', 20))
    approval_type = request.args.get('type')
    item_id = request.args.get('item_id')
    overview = build_approval_audit_overview(
        stale_rows=db.get_stale_approval_states(stale_after_minutes=stale_after_minutes, approval_type=approval_type, limit=limit),
        decision_diffs=db.get_recent_approval_decision_diff(limit=limit, approval_type=approval_type),
        timeline_summary=db.get_approval_timeline_summary(item_id) if item_id else None,
    )
    overview['transition_journal'] = build_transition_journal_overview(
        transition_rows=db.get_recent_transition_journal(limit=limit, approval_type=approval_type, item_id=item_id),
        summary=db.get_transition_journal_summary(limit=limit, approval_type=approval_type, item_id=item_id),
    )
    return jsonify({'success': True, 'data': overview, 'summary': {
        'stale_count': overview['stale_pending']['count'],
        'decision_diff_count': overview['decision_diff']['count'],
        'transition_count': (overview.get('transition_journal') or {}).get('summary', {}).get('count', 0),
        'has_timeline_summary': bool(overview.get('timeline_summary')),
    }})


@app.route('/api/approvals/recover', methods=['POST'])
def recover_approval_state_api():
    """根据 immutable event timeline 重建 latest snapshot。"""
    payload = request.get_json(silent=True) or {}
    item_id = payload.get('item_id') or request.args.get('item_id')
    if not item_id:
        return jsonify({'success': False, 'error': 'missing item_id'}), 400
    row = db.recover_approval_state(item_id)
    if not row:
        return jsonify({'success': False, 'error': 'approval item not found'}), 404
    return jsonify({'success': True, 'data': row})


@app.route('/api/approvals/replay')
def replay_approval_state():
    """返回 workflow-ready 视图并叠加已持久化审批状态，用于恢复/审计。"""
    backtest_result = backtester.run_all(config.symbols)
    calibration_report = backtest_result.get('calibration_report') or {}
    payload = export_calibration_payload(calibration_report, view='workflow_ready')
    payload = _persist_workflow_approval_payload(payload, replay_source='approval_replay_api')
    return jsonify({'success': True, 'view': 'approval_replay', 'data': payload, 'summary': payload.get('summary') or {}})


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
            'take_profit': trading.get('take_profit', 0.04),
            # Partial TP / 分批止盈配置
            'partial_tp_enabled': trading.get('partial_tp_enabled', False),
            'partial_tp_threshold': trading.get('partial_tp_threshold', 0.015),
            'partial_tp_ratio': trading.get('partial_tp_ratio', 0.5),
            # 第二止盈层 / 多级退出配置
            'partial_tp2_enabled': trading.get('partial_tp2_enabled', False),
            'partial_tp2_threshold': trading.get('partial_tp2_threshold', 0.03),
            'partial_tp2_ratio': trading.get('partial_tp2_ratio', 0.3)
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
# 可视化配置表单 API
# ============================================================================

# 字段元数据定义 - 可扩展
FIELD_DEFINITIONS = {
    'trading': {
        'label': '交易参数',
        'fields': {
            'trading.leverage': {
                'label': '杠杆倍数',
                'type': 'int',
                'min': 1,
                'max': 125,
                'default': 10,
                'description': '开仓时使用的杠杆倍数 (1-125)',
                'recommended': [5, 10, 20, 50]
            },
            'trading.position_size': {
                'label': '仓位比例',
                'type': 'float',
                'min': 0.01,
                'max': 1.0,
                'default': 0.1,
                'description': '每次开仓使用的资金比例 (0.01-1.0)',
                'recommended': [0.05, 0.1, 0.15, 0.2]
            },
            'trading.total_margin_cap_ratio': {
                'label': '总保证金硬上限',
                'type': 'float',
                'min': 0.05,
                'max': 0.8,
                'default': 0.30,
                'description': '所有持仓合计保证金占总资产的硬上限',
                'recommended': [0.2, 0.25, 0.3, 0.35]
            },
            'trading.total_margin_soft_cap_ratio': {
                'label': '总保证金软警戒',
                'type': 'float',
                'min': 0.05,
                'max': 0.8,
                'default': 0.25,
                'description': '接近该比例时系统会自动收缩单笔开仓',
                'recommended': [0.2, 0.24, 0.25, 0.27]
            },
            'trading.symbol_margin_cap_ratio': {
                'label': '单币种保证金上限',
                'type': 'float',
                'min': 0.02,
                'max': 0.5,
                'default': 0.12,
                'description': '单一币种累计保证金占总资产上限',
                'recommended': [0.08, 0.1, 0.12, 0.15]
            },
            'trading.base_entry_margin_ratio': {
                'label': '基础单笔目标',
                'type': 'float',
                'min': 0.01,
                'max': 0.3,
                'default': 0.08,
                'description': '默认目标开仓保证金比例（风险预算逻辑会在范围内动态收缩）',
                'recommended': [0.05, 0.08, 0.1]
            },
            'trading.min_entry_margin_ratio': {
                'label': '最小单笔保证金比例',
                'type': 'float',
                'min': 0.01,
                'max': 0.2,
                'default': 0.04,
                'description': '剩余预算不足该比例时直接拒绝新开仓',
                'recommended': [0.03, 0.04, 0.05]
            },
            'trading.max_entry_margin_ratio': {
                'label': '最大单笔保证金比例',
                'type': 'float',
                'min': 0.02,
                'max': 0.3,
                'default': 0.10,
                'description': '单笔开仓的动态上限，避免过大仓位',
                'recommended': [0.08, 0.1, 0.12]
            },
            'trading.add_position_enabled': {
                'label': '允许同向加仓',
                'type': 'bool',
                'default': False,
                'description': '默认关闭；开启后才允许同币种同方向继续加仓'
            },
            'trading.quality_scaling_enabled': {
                'label': '启用质量缩放',
                'type': 'bool',
                'default': False,
                'description': '按信号质量在 min/max 单笔范围内做温和放大或缩小'
            },
            'trading.high_quality_multiplier': {
                'label': '高质量倍率',
                'type': 'float',
                'min': 1.0,
                'max': 2.0,
                'default': 1.15,
                'description': '高质量信号时乘上的温和放大倍率',
                'recommended': [1.05, 1.15, 1.25]
            },
            'trading.low_quality_multiplier': {
                'label': '低质量倍率',
                'type': 'float',
                'min': 0.3,
                'max': 1.0,
                'default': 0.75,
                'description': '低质量信号时乘上的保守缩小倍率',
                'recommended': [0.6, 0.75, 0.85]
            },
            'trading.stop_loss': {
                'label': '止损比例',
                'type': 'float',
                'min': 0.001,
                'max': 0.5,
                'default': 0.018,
                'description': '亏损达到此比例时止损',
                'recommended': [0.01, 0.015, 0.02, 0.03]
            },
            'trading.take_profit': {
                'label': '止盈比例',
                'type': 'float',
                'min': 0.001,
                'max': 0.5,
                'default': 0.028,
                'description': '盈利达到此比例时止盈',
                'recommended': [0.02, 0.028, 0.04, 0.05]
            },
            'trading.trailing_stop': {
                'label': '追踪止损',
                'type': 'float',
                'min': 0,
                'max': 0.2,
                'default': 0.01,
                'description': '追踪止损幅度',
                'recommended': [0.005, 0.01, 0.015, 0.02]
            },
            'trading.trailing_activation': {
                'label': '追踪激活阈值',
                'type': 'float',
                'min': 0,
                'max': 0.2,
                'default': 0.01,
                'description': '盈利达到此比例后激活追踪止损',
                'recommended': [0.005, 0.01, 0.015, 0.02]
            },
            'trading.partial_tp_enabled': {
                'label': '分批止盈启用',
                'type': 'bool',
                'default': False,
                'description': '是否启用分批止盈功能'
            },
            'trading.partial_tp_threshold': {
                'label': '第一止盈触发线',
                'type': 'float',
                'min': 0.001,
                'max': 0.2,
                'default': 0.015,
                'description': '盈利达到此比例时触发第一层止盈',
                'recommended': [0.01, 0.015, 0.02, 0.03]
            },
            'trading.partial_tp_ratio': {
                'label': '第一止盈平仓比例',
                'type': 'float',
                'min': 0.1,
                'max': 0.9,
                'default': 0.5,
                'description': '第一层止盈平仓的仓位占比',
                'recommended': [0.3, 0.5, 0.7]
            },
            'trading.partial_tp2_enabled': {
                'label': '第二止盈启用',
                'type': 'bool',
                'default': False,
                'description': '是否启用第二层分批止盈'
            },
            'trading.partial_tp2_threshold': {
                'label': '第二止盈触发线',
                'type': 'float',
                'min': 0.001,
                'max': 0.3,
                'default': 0.03,
                'description': '盈利达到此比例时触发第二层止盈',
                'recommended': [0.02, 0.03, 0.04, 0.05]
            },
            'trading.partial_tp2_ratio': {
                'label': '第二止盈平仓比例',
                'type': 'float',
                'min': 0.1,
                'max': 1.0,
                'default': 0.3,
                'description': '第二层止盈平仓的仓位占比',
                'recommended': [0.3, 0.5, 0.7]
            }
            ,
            'trading.layering.layer_count': {'label': '分仓层数', 'type': 'int', 'min': 1, 'max': 10, 'default': 3, 'description': '分仓计划总层数'},
            'trading.layering.layer_ratios': {'label': '各层保证金比例', 'type': 'list', 'default': [0.06, 0.06, 0.04], 'description': '例如 [0.06,0.06,0.04]'},
            'trading.layering.layer_max_total_ratio': {'label': '分仓累计上限', 'type': 'float', 'min': 0.01, 'max': 1.0, 'default': 0.16, 'description': '分仓累计保证金比例上限'},
            'trading.layering.min_add_interval_seconds': {'label': '最小加仓间隔(秒)', 'type': 'int', 'min': 0, 'max': 86400, 'default': 0, 'description': '两次加仓的最小时间间隔'},
            'trading.layering.profit_only_add': {'label': '仅浮盈时加仓', 'type': 'bool', 'default': False, 'description': '开启后仅在浮盈状态允许继续加仓'},
            'trading.layering.disallow_skip_layers': {'label': '禁止跳层', 'type': 'bool', 'default': True, 'description': '开启后必须按层顺序加仓'},
            'trading.layering.direction_lock_enabled': {'label': '方向锁启用', 'type': 'bool', 'default': True, 'description': '防止并发重复开仓/加仓'},
            'trading.layering.direction_lock_scope': {'label': '方向锁范围', 'type': 'select', 'options': ['symbol_side', 'symbol'], 'default': 'symbol_side', 'description': 'symbol_side=币种+方向，symbol=币种级别'},
            'trading.layering.direction_lock_release_on_flat': {'label': '平仓归零时释放方向锁', 'type': 'bool', 'default': True, 'description': '当前实现默认随 intent/仓位收口释放'},
            'trading.layering.signal_idempotency_enabled': {'label': 'signal 幂等保护', 'type': 'bool', 'default': True, 'description': '同一 signal_id 不重复执行'},
            'trading.layering.signal_idempotency_ttl_seconds': {'label': 'signal 幂等TTL(秒)', 'type': 'int', 'min': 0, 'max': 604800, 'default': 3600, 'description': '当前为最小可用保留字段'},
            'trading.layering.max_layers_per_signal': {'label': '单个Signal最大层数', 'type': 'int', 'min': 1, 'max': 10, 'default': 3, 'description': '限制单次信号可触发的分仓层数'},
            'trading.layering.allow_same_bar_multiple_adds': {'label': '允许同一Bar多次加仓', 'type': 'bool', 'default': False, 'description': '关闭时会阻止同一bar重复加仓'}
        }
    },
    'risk': {
        'label': '风控参数',
        'fields': {
            'trading.max_exposure': {
                'label': '最大风险敞口',
                'type': 'float',
                'min': 0.01,
                'max': 1.0,
                'default': 0.3,
                'description': '兼容旧字段：总保证金硬上限（建议优先看风险预算字段）',
                'recommended': [0.2, 0.3, 0.5, 0.7]
            },
            'trading.max_daily_drawdown': {
                'label': '日内最大回撤',
                'type': 'float',
                'min': 0.01,
                'max': 0.5,
                'default': 0.03,
                'description': '单日亏损比例的容忍上限',
                'recommended': [0.02, 0.03, 0.05, 0.1]
            },
            'trading.max_consecutive_losses': {
                'label': '最大连亏次数',
                'type': 'int',
                'min': 1,
                'max': 20,
                'default': 3,
                'description': '连续亏损次数达到此值后触发熔断',
                'recommended': [2, 3, 5, 7]
            },
            'trading.max_position_per_symbol': {
                'label': '单币种最大持仓',
                'type': 'float',
                'min': 0.01,
                'max': 1.0,
                'default': 0.12,
                'description': '兼容旧字段：单币种保证金上限（建议优先看风险预算字段）',
                'recommended': [0.1, 0.15, 0.2, 0.3]
            },
            'trading.cooldown_minutes': {
                'label': '冷却时间(分钟)',
                'type': 'int',
                'min': 0,
                'max': 120,
                'default': 15,
                'description': '开仓后等待下次交易的最短时间',
                'recommended': [5, 10, 15, 30]
            },
            'trading.max_trades_per_day': {
                'label': '每日最大交易次数',
                'type': 'int',
                'min': 1,
                'max': 50,
                'default': 10,
                'description': '单日最多开仓次数',
                'recommended': [5, 10, 15, 20]
            },
            'trading.min_trade_interval': {
                'label': '最小交易间隔(秒)',
                'type': 'int',
                'min': 60,
                'max': 3600,
                'default': 300,
                'description': '同一币种最小交易间隔秒数',
                'recommended': [180, 300, 600, 900]
            }
        }
    },
    'runtime': {
        'label': '运行参数',
        'fields': {
            'runtime.interval_seconds': {
                'label': '轮询间隔(秒)',
                'type': 'int',
                'min': 60,
                'max': 3600,
                'default': 300,
                'description': '交易循环的间隔秒数',
                'recommended': [60, 120, 300, 600]
            },
            'runtime.mode': {
                'label': '运行模式',
                'type': 'select',
                'options': ['interval', 'schedule', 'realtime'],
                'default': 'interval',
                'description': '运行模式: interval=定时, schedule=计划任务, realtime=实时'
            }
        }
    },
    'symbols': {
        'label': '监听币种',
        'fields': {
            'symbols.watch_list': {
                'label': '主监控列表',
                'type': 'list',
                'default': ['BTC/USDT'],
                'description': '主要监控的交易对列表'
            },
            'symbols.candidate_watch_list': {
                'label': '候选监控列表',
                'type': 'list',
                'default': [],
                'description': '候选交易对列表'
            },
            'symbols.paused_watch_list': {
                'label': '暂停监控列表',
                'type': 'list',
                'default': [],
                'description': '暂停监控的交易对列表'
            },
            'symbols.selection_mode': {
                'label': '选择模式',
                'type': 'select',
                'options': ['focused', 'balanced', 'exploratory'],
                'default': 'focused',
                'description': '币种选择模式'
            }
        }
    },
    'notification': {
        'label': '通知设置',
        'fields': {
            'notification.discord.enabled': {
                'label': 'Discord启用',
                'type': 'bool',
                'default': True,
                'description': '是否启用Discord通知'
            },
            'notification.discord.notify_trades': {
                'label': '通知交易',
                'type': 'bool',
                'default': True,
                'description': '发送交易通知'
            },
            'notification.discord.notify_signals': {
                'label': '通知信号',
                'type': 'bool',
                'default': True,
                'description': '发送信号通知'
            },
            'notification.discord.notify_errors': {
                'label': '通知错误',
                'type': 'bool',
                'default': True,
                'description': '发送错误通知'
            }
        }
    },
    'strategies': {
        'label': '策略开关',
        'fields': {
            'strategies.rsi.enabled': {
                'label': 'RSI策略启用',
                'type': 'bool',
                'default': True,
                'description': '启用RSI超买超卖策略'
            },
            'strategies.rsi.period': {
                'label': 'RSI周期',
                'type': 'int',
                'min': 5,
                'max': 30,
                'default': 14,
                'description': 'RSI计算周期'
            },
            'strategies.rsi.oversold': {
                'label': 'RSI超卖阈值',
                'type': 'int',
                'min': 10,
                'max': 50,
                'default': 35,
                'description': 'RSI超卖阈值'
            },
            'strategies.rsi.overbought': {
                'label': 'RSI超买阈值',
                'type': 'int',
                'min': 50,
                'max': 90,
                'default': 65,
                'description': 'RSI超买阈值'
            },
            'strategies.macd.enabled': {
                'label': 'MACD策略启用',
                'type': 'bool',
                'default': True,
                'description': '启用MACD策略'
            },
            'strategies.macd.fast_period': {
                'label': 'MACD快线周期',
                'type': 'int',
                'min': 5,
                'max': 30,
                'default': 12,
                'description': 'MACD快线周期'
            },
            'strategies.macd.slow_period': {
                'label': 'MACD慢线周期',
                'type': 'int',
                'min': 15,
                'max': 50,
                'default': 26,
                'description': 'MACD慢线周期'
            },
            'strategies.bollinger.enabled': {
                'label': '布林带策略启用',
                'type': 'bool',
                'default': True,
                'description': '启用布林带策略'
            },
            'strategies.bollinger.period': {
                'label': '布林带周期',
                'type': 'int',
                'min': 10,
                'max': 50,
                'default': 20,
                'description': '布林带计算周期'
            },
            'strategies.bollinger.std_multiplier': {
                'label': '布林带标准差倍数',
                'type': 'float',
                'min': 1.0,
                'max': 4.0,
                'default': 2.0,
                'description': '布林带标准差倍数'
            },
            'strategies.composite.enabled': {
                'label': '综合策略启用',
                'type': 'bool',
                'default': True,
                'description': '启用多策略综合评分'
            },
            'strategies.composite.min_strength': {
                'label': '综合最小信号强度',
                'type': 'int',
                'min': 10,
                'max': 100,
                'default': 28,
                'description': '触发交易所需的最小综合信号强度'
            }
        }
    }
}


def _get_merged_config():
    """获取合并后的配置(config.yaml + config.local.yaml)"""
    # 使用 config.all 获取合并后的配置
    return config.all


def _get_field_value(field_key: str, merged_config: dict):
    """从嵌套配置中获取字段值"""
    keys = field_key.split('.')
    value = merged_config
    for k in keys:
        if isinstance(value, dict):
            value = value.get(k)
        else:
            return None
    return value


@app.route('/api/config/form')
def get_config_form():
    """API: 获取表单配置数据(分组结构 + 当前值 + 元数据)"""
    merged = _get_merged_config()
    
    result = {}
    for group_key, group_def in FIELD_DEFINITIONS.items():
        group_data = {
            'label': group_def['label'],
            'fields': {}
        }
        for field_key, field_def in group_def['fields'].items():
            current_value = _get_field_value(field_key, merged)
            # 如果当前值不存在，使用默认值
            if current_value is None:
                current_value = field_def.get('default')
            
            group_data['fields'][field_key] = {
                'label': field_def['label'],
                'type': field_def['type'],
                'value': current_value,
                'default': field_def.get('default'),
                'description': field_def.get('description', ''),
                'recommended': field_def.get('recommended', []),
                'min': field_def.get('min'),
                'max': field_def.get('max'),
                'options': field_def.get('options', [])
            }
        
        result[group_key] = group_data
    
    return jsonify({
        'success': True,
        'data': result
    })


@app.route('/api/config/save', methods=['POST'])
def save_config_form():
    """API: 保存配置到 config.local.yaml"""
    pass  # config is already reloaded via config.reload() below
    
    payload = request.get_json(silent=True) or {}
    fields = payload.get('fields', {})
    auto_restart = payload.get('auto_restart', False)
    
    if not fields:
        return jsonify({'success': False, 'error': '没有要保存的字段'}), 400
    
    # 获取 config.local.yaml 路径
    project_root = Path(__file__).parent.parent
    local_config_path = project_root / 'config' / 'config.local.yaml'
    
    # 读取现有 local 配置(如果存在)
    local_config = {}
    if local_config_path.exists():
        with open(local_config_path, 'r', encoding='utf-8') as f:
            local_config = yaml.safe_load(f) or {}
    
    # 按层级分组写入
    nested = {}
    saved_fields = []
    
    for field_key, value in fields.items():
        # 类型转换
        field_def = None
        for group in FIELD_DEFINITIONS.values():
            if field_key in group.get('fields', {}):
                field_def = group['fields'][field_key]
                break
        
        if field_def:
            field_type = field_def.get('type', 'string')
            
            # 类型转换
            if field_type == 'int':
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    value = field_def.get('default', 0)
            elif field_type == 'float':
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    value = field_def.get('default', 0.0)
            elif field_type == 'bool':
                value = bool(value) if not isinstance(value, bool) else value
            elif field_type == 'list':
                if isinstance(value, str):
                    value = [s.strip() for s in value.split(',') if s.strip()]
                elif not isinstance(value, list):
                    value = field_def.get('default', [])
        
        # 构建嵌套结构
        keys = field_key.split('.')
        current = nested
        for k in keys[:-1]:
            if k not in current:
                current[k] = {}
            current = current[k]
        current[keys[-1]] = value
        saved_fields.append(field_key)
    
    # 深度合并到 local_config
    def deep_merge(base: dict, override: dict) -> dict:
        result = dict(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = deep_merge(result[key], value)
            else:
                result[key] = value
        return result
    
    local_config = deep_merge(local_config, nested)
    
    # 创建备份（在保存前）
    backup_info = None
    if local_config_path.exists():
        backup_dir = local_config_path.parent / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        backup_filename = f'config.local-{timestamp}.yaml'
        backup_path = backup_dir / backup_filename
        shutil.copy2(local_config_path, backup_path)
        # 清理旧备份，保留最近 10 份
        _cleanup_old_backups(backup_dir, keep=10)
        backup_info = {
            'name': backup_filename,
            'path': str(backup_path),
            'created_at': datetime.now().isoformat()
        }
    
    # 保存到文件
    with open(local_config_path, 'w', encoding='utf-8') as f:
        yaml.dump(local_config, f, allow_unicode=True, default_flow_style=False)
    
    # 重新加载配置
    config.reload()
    
    # 记录审计日志
    audit_record = {
        'timestamp': datetime.now().isoformat(),
        'fields': saved_fields,
        'changes': fields,
        'auto_restart': auto_restart
    }
    _save_config_audit_record(audit_record)
    
    result = {
        'success': True,
        'message': f'已保存 {len(saved_fields)} 个配置项到 config.local.yaml',
        'saved_fields': saved_fields,
        'saved_path': str(local_config_path),
        'audit_id': audit_record['timestamp'],
        'backup': backup_info
    }
    
    # 如果请求自动重启
    if auto_restart:
        restart_result = _restart_daemon_internal()
        result['restarted'] = restart_result
    
    return jsonify(result)


def _restart_daemon_internal():
    """内部重启 daemon 方法"""
    import signal
    import subprocess
    
    project_root = Path(__file__).parent.parent
    pid_file = project_root / 'bot.pid'
    
    try:
        # 读取当前 PID
        if pid_file.exists():
            with open(pid_file, 'r') as f:
                old_pid = int(f.read().strip())
            
            # 尝试发送 SIGTERM
            try:
                os.kill(old_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass  # 进程已不存在
        
        # 启动新进程
        run_script = project_root / 'bot' / 'run.py'
        if run_script.exists():
            # 后台启动
            subprocess.Popen(
                ['python3', str(run_script)],
                cwd=str(project_root),
                stdout=open(project_root / 'logs' / 'trading.log', 'a'),
                stderr=subprocess.STDOUT
            )
            return {'success': True, 'message': 'Daemon 已重启'}
        else:
            return {'success': False, 'error': '找不到 bot/run.py'}
    
    except Exception as e:
        return {'success': False, 'error': str(e)}


@app.route('/api/daemon/restart', methods=['POST'])
def restart_daemon():
    """API: 重启交易机器人"""
    result = _restart_daemon_internal()
    return jsonify(result)


# ========== 配置审计 API ==========
@app.route('/api/config/audit')
def get_config_audit():
    """获取配置变更审计记录"""
    limit = int(request.args.get('limit', 20))
    records = _load_config_audit(limit=limit)
    return jsonify({'success': True, 'data': records, 'count': len(records)})


@app.route('/api/config/status')
def get_config_status():
    """获取配置生效状态（运行参数 vs 当前配置）"""
    runtime = load_runtime_state() or {}
    merged = _get_merged_config()
    
    # 关键运行参数对比
    key_fields = ['runtime.interval_seconds', 'runtime.mode', 'symbols.watch_list']
    comparison = []
    
    for field in key_fields:
        form_value = _get_field_value(field, merged)
        runtime_value = runtime.get(field.replace('.', '_'))
        
        # 对于 watch_list，需要比较
        if field == 'symbols.watch_list':
            runtime_list = runtime.get('watch_list') or []
            form_list = form_value if isinstance(form_value, list) else []
            matched = set(runtime_list) == set(form_list) if runtime_list or form_list else True
            if not matched:
                comparison.append({
                    'field': field,
                    'form_value': form_list,
                    'runtime_value': runtime_list,
                    'matched': False,
                    'note': '重启后生效' if runtime.get('running') else ''
                })
        else:
            matched = str(form_value) == str(runtime_value) if runtime_value else False
            if not matched:
                comparison.append({
                    'field': field,
                    'form_value': form_value,
                    'runtime_value': runtime_value,
                    'matched': matched,
                    'note': '重启后生效' if runtime.get('running') else ''
                })
    
    # daemon 状态
    daemon_status = {
        'running': runtime.get('running', False),
        'last_started_at': runtime.get('last_started_at'),
        'last_error': runtime.get('last_error'),
        'mode': runtime.get('mode'),
        'interval_seconds': runtime.get('interval_seconds')
    }
    
    return jsonify({
        'success': True,
        'data': {
            'daemon': daemon_status,
            'comparison': comparison,
            'pending_restart': len(comparison) > 0 and runtime.get('running', False)
        }
    })


# ========== 配置备份/回滚 API ==========
@app.route('/api/config/backups')
def list_config_backups():
    """列出 config.local.yaml 的所有备份"""
    backups = _list_config_backups()
    return jsonify({'success': True, 'data': backups, 'count': len(backups)})


@app.route('/api/config/rollback', methods=['POST'])
def rollback_config_backup():
    """回滚 config.local.yaml 到指定备份"""
    payload = request.get_json(silent=True) or {}
    backup_name = payload.get('backup_name')
    auto_restart = payload.get('auto_restart', False)
    
    if not backup_name:
        return jsonify({'success': False, 'error': 'missing backup_name'}), 400
    
    backup_dir = _get_local_backups_dir()
    backup_path = backup_dir / backup_name
    
    if not backup_path.exists():
        return jsonify({'success': False, 'error': 'backup not found'}), 404
    
    local_path = _get_local_config_path()
    
    # 创建回滚前的当前配置备份
    pre_rollback_backup = None
    if local_path.exists():
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        pre_rollback_backup = backup_dir / f'config.local-pre-rollback-{timestamp}.yaml'
        shutil.copy2(local_path, pre_rollback_backup)
    
    # 执行回滚
    shutil.copy2(backup_path, local_path)
    
    # 重新加载配置
    config.reload()
    
    # 记录审计
    audit_record = {
        'timestamp': datetime.now().isoformat(),
        'action': 'rollback',
        'rolled_back_to': backup_name,
        'pre_rollback_backup': str(pre_rollback_backup) if pre_rollback_backup else None,
    }
    _save_config_audit_record(audit_record)
    
    result = {
        'success': True,
        'message': f'已回滚到备份: {backup_name}',
        'rolled_back_to': backup_name,
        'pre_rollback_backup': str(pre_rollback_backup) if pre_rollback_backup else None,
    }
    
    if auto_restart:
        restart_result = _restart_daemon_internal()
        result['restarted'] = restart_result
    
    return jsonify(result)


@app.route('/api/config/diff')
def config_diff():
    """对比当前配置与指定备份的差异"""
    backup_name = request.args.get('backup_name')
    
    if not backup_name:
        return jsonify({'success': False, 'error': 'missing backup_name'}), 400
    
    backup_dir = _get_local_backups_dir()
    backup_path = backup_dir / backup_name
    
    if not backup_path.exists():
        return jsonify({'success': False, 'error': 'backup not found'}), 404
    
    local_path = _get_local_config_path()
    
    # 读取当前配置
    current_content = ''
    if local_path.exists():
        with open(local_path, 'r', encoding='utf-8') as f:
            current_content = f.read()
    
    # 读取备份配置
    backup_content = ''
    with open(backup_path, 'r', encoding='utf-8') as f:
        backup_content = f.read()
    
    # 生成 unified diff
    diff_lines = list(difflib.unified_diff(
        backup_content.splitlines(keepends=True),
        current_content.splitlines(keepends=True),
        fromfile=f'backup: {backup_name}',
        tofile='current: config.local.yaml',
        lineterm=''
    ))
    
    return jsonify({
        'success': True,
        'data': {
            'backup_name': backup_name,
            'current_path': str(local_path),
            'backup_path': str(backup_path),
            'diff': ''.join(diff_lines) if diff_lines else '无差异'
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
# MFE / MAE 分析
# ============================================================================

@app.route('/api/trades/mfe-mae')
def get_trade_mfe_mae():
    """
    MFE/MAE 分析与止盈止损优化诊断
    
    返回:
    - 每笔交易/持仓的 MFE (最大有利偏移) / MAE (最大不利偏移)
    - 按币种聚合的统计
    - 止盈/止损/追踪止损建议
    """
    try:
        analyzer = MFEAnalyzer(db)
        report = analyzer.generate_analysis_report()
        return jsonify(report)
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e),
            'error_type': type(e).__name__
        }), 500


@app.route('/api/trades/mfe-mae/by-symbol')
def get_trade_mfe_mae_by_symbol():
    """按币种获取 MFE/MAE 分析"""
    symbol = request.args.get('symbol')
    try:
        analyzer = MFEAnalyzer(db)
        report = analyzer.generate_analysis_report()
        
        if symbol:
            by_symbol = report.get('by_symbol', {})
            return jsonify(by_symbol.get(symbol, {
                'status': 'not_found',
                'message': f'无 {symbol} 的数据'
            }))
        return jsonify(report.get('by_symbol', {}))
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/trades/mfe-mae/recommendations')
def get_trade_mfe_mae_recommendations():
    """获取止盈止损建议"""
    try:
        analyzer = MFEAnalyzer(db)
        report = analyzer.generate_analysis_report()
        
        return jsonify({
            'stop_loss': report.get('stop_loss', {}),
            'take_profit': report.get('take_profit', {}),
            'trailing_stop': report.get('trailing_stop', {}),
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/exit-strategy/status')
def get_exit_strategy_status():
    """
    获取退出策略状态汇总
    
    包含:
    - 当前退出配置 (partial TP / partial TP2 / trailing)
    - MFE/MAE 建议
    - 最近触发历史统计
    """
    try:
        trading = config.get('trading', {}) or {}
        
        # 获取 partial TP 历史统计
        pt_history = db.get_partial_tp_history(limit=1000)
        
        # 统计
        pt_count = len(pt_history)
        pt_symbols = list(set([r.get('symbol') for r in pt_history if r.get('symbol')]))
        last_pt = pt_history[0] if pt_history else None
        
        # 按币种统计
        by_symbol = {}
        for r in pt_history:
            sym = r.get('symbol')
            if sym:
                if sym not in by_symbol:
                    by_symbol[sym] = {'count': 0, 'total_pnl': 0.0}
                by_symbol[sym]['count'] += 1
                by_symbol[sym]['total_pnl'] += float(r.get('pnl') or 0)
        
        # 获取 MFE/MAE 建议
        try:
            analyzer = MFEAnalyzer(db)
            report = analyzer.generate_analysis_report()
            mfe_mae = {
                'stop_loss': report.get('stop_loss', {}),
                'take_profit': report.get('take_profit', {}),
                'trailing_stop': report.get('trailing_stop', {}),
            }
        except Exception:
            mfe_mae = {'stop_loss': {}, 'take_profit': {}, 'trailing_stop': {}}
        
        # 构建响应
        return jsonify({
            'success': True,
            'data': {
                # 退出配置
                'config': {
                    'partial_tp_enabled': trading.get('partial_tp_enabled', False),
                    'partial_tp_threshold': trading.get('partial_tp_threshold', 0.015),
                    'partial_tp_ratio': trading.get('partial_tp_ratio', 0.5),
                    'partial_tp2_enabled': trading.get('partial_tp2_enabled', False),
                    'partial_tp2_threshold': trading.get('partial_tp2_threshold', 0.03),
                    'partial_tp2_ratio': trading.get('partial_tp2_ratio', 0.3),
                    # trailing 激活阈值 (从 MFE/MAE 获取或使用默认值)
                    'trailing_activation': mfe_mae.get('trailing_stop', {}).get('activation') or 0.01,
                },
                # MFE/MAE 建议
                'recommendations': mfe_mae,
                # 统计摘要
                'stats': {
                    'total_triggers': pt_count,
                    'unique_symbols': len(pt_symbols),
                    'symbols': pt_symbols[:10],  # 最多10个
                    'by_symbol': by_symbol,
                },
                # 最近触发
                'last_trigger': {
                    'id': last_pt.get('id') if last_pt else None,
                    'symbol': last_pt.get('symbol') if last_pt else None,
                    'side': last_pt.get('side') if last_pt else None,
                    'trigger_price': last_pt.get('trigger_price') if last_pt else None,
                    'close_ratio': last_pt.get('close_ratio') if last_pt else None,
                    'pnl': last_pt.get('pnl') if last_pt else None,
                    'created_at': last_pt.get('created_at') if last_pt else None,
                } if last_pt else None
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'error_type': type(e).__name__
        }), 500


# ============================================================================
# 启动
# ============================================================================

def run_dashboard(host: str = '0.0.0.0', port: int = 5555, debug: bool = False):
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
