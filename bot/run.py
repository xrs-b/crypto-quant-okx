"""
OKX量化交易机器人 - 主程序入口
使用方法:
    .venv/bin/python3 bot/run.py                 # 运行交易
    .venv/bin/flask --app dashboard.api:app run --host 0.0.0.0 --port 5555  # 运行仪表盘
    python bot/run.py --train         # 训练模型
    python bot/run.py --collect        # 收集数据
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import json
import time
import pandas as pd
from datetime import datetime
from pathlib import Path

from core.config import Config
from core.database import Database
from core.exchange import Exchange
from core.logger import logger
from core.notifier import NotificationManager
from core.presets import PresetManager
from core.paths import DATA_DIR
from signals import SignalDetector, SignalValidator, SignalRecorder, EntryDecider
from trading import TradingExecutor, RiskManager
from ml.engine import MLEngine, ModelTrainer, DataCollector
from analytics import StrategyBacktester, SignalQualityAnalyzer, ParameterOptimizer, GovernanceEngine, build_approval_audit_overview
from validation import format_validation_report_markdown, run_shadow_validation_case, run_shadow_validation_replay


def run_notification_relay(interval: int = 30, once: bool = False, limit: int = 20):
    cfg = Config()
    db = Database(cfg.db_path)
    notifier = NotificationManager(cfg, db, logger)
    print(f"\n📮 Outbox relay 启动 | interval={interval}s | limit={limit} | once={'yes' if once else 'no'}\n")
    state = load_runtime_state()
    state['relay'] = {
        'running': True,
        'interval_seconds': interval,
        'last_started_at': datetime.now().isoformat(),
        'last_result': state.get('relay', {}).get('last_result'),
    }
    save_runtime_state(state)
    while True:
        now = datetime.now().isoformat()
        result = notifier.relay_pending_outbox(limit=limit)
        relay_state = load_runtime_state()
        relay_state['relay'] = {
            'running': not once,
            'interval_seconds': interval,
            'last_started_at': relay_state.get('relay', {}).get('last_started_at') or now,
            'last_checked_at': now,
            'last_result': {'time': now, **result},
        }
        save_runtime_state(relay_state)
        print(json.dumps({'time': now, **result}, ensure_ascii=False))
        if once:
            break
        time.sleep(interval)

RUNTIME_STATE_PATH = DATA_DIR / 'runtime_state.json'


def load_runtime_state() -> dict:
    try:
        return json.loads(RUNTIME_STATE_PATH.read_text()) if RUNTIME_STATE_PATH.exists() else {}
    except Exception:
        return {}


def save_runtime_state(state: dict):
    RUNTIME_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_STATE_PATH.write_text(json.dumps(state or {}, ensure_ascii=False, indent=2))


def _build_approval_hygiene_operator_action_summary(stale_rows: list, decision_diffs: list, *, auto_cleanup_enabled: bool = False) -> dict:
    policy_counts = {
        'review_schedule': 0,
        'escalate': 0,
        'freeze_followup': 0,
        'observe_only_followup': 0,
    }
    routed_items = []

    for row in stale_rows or []:
        state = str(row.get('state') or row.get('approval_state') or '').strip().lower()
        risk = str(row.get('risk_level') or '').strip().lower()
        blocked = list(row.get('blocked_by') or [])
        if risk in {'critical', 'high'} or blocked:
            action = 'escalate'
            route = 'operator_escalation'
        elif auto_cleanup_enabled and state in {'pending', 'ready', 'replayed'}:
            action = 'observe_only_followup'
            route = 'stale_cleanup_audit'
        else:
            action = 'review_schedule'
            route = 'review_schedule_queue'
        policy_counts[action] = policy_counts.get(action, 0) + 1
        routed_items.append({
            'kind': 'stale',
            'approval_id': row.get('approval_id'),
            'item_id': row.get('playbook_id') or row.get('item_id'),
            'action': action,
            'route': route,
            'state': state or 'pending',
            'blocked_by': blocked,
        })

    for row in decision_diffs or []:
        workflow_state = str(row.get('workflow_state') or row.get('to_workflow_state') or row.get('current_workflow_state') or '').strip().lower()
        state = str(row.get('state') or row.get('to_state') or row.get('current_state') or '').strip().lower()
        if workflow_state in {'execution_failed', 'rollback_pending'}:
            action = 'freeze_followup'
            route = 'freeze_followup_queue'
        elif state in {'rejected', 'expired'}:
            action = 'review_schedule'
            route = 'review_schedule_queue'
        else:
            action = 'observe_only_followup'
            route = 'decision_diff_audit'
        policy_counts[action] = policy_counts.get(action, 0) + 1
        routed_items.append({
            'kind': 'decision_diff',
            'approval_id': row.get('approval_id'),
            'item_id': row.get('playbook_id') or row.get('item_id'),
            'action': action,
            'route': route,
            'state': state or workflow_state or 'changed',
            'blocked_by': list(row.get('blocked_by') or []),
        })

    return {
        'schema_version': 'm5_approval_hygiene_operator_action_summary_v1',
        'policy_counts': {k: v for k, v in policy_counts.items() if v},
        'routes': sorted({row['route'] for row in routed_items if row.get('route')}),
        'items': routed_items[:20],
    }


def append_runtime_history(event: dict, limit: int = 20):
    state = load_runtime_state()
    history = state.get('history', [])
    history.insert(0, event)
    state['history'] = history[:limit]
    save_runtime_state(state)


def build_approval_hygiene_summary(cfg: Config, db: Database, limit: int = 20) -> dict:
    hygiene_cfg = cfg.get('runtime.approval_hygiene', {}) or {}
    enabled = bool(hygiene_cfg.get('enabled', True))
    auto_cleanup_enabled = bool(hygiene_cfg.get('auto_cleanup_enabled', False))
    stale_after_minutes = max(1, int(hygiene_cfg.get('stale_after_minutes', 180) or 180))
    approval_type = hygiene_cfg.get('approval_type') or None
    sample_limit = max(1, int(hygiene_cfg.get('limit', limit) or limit))
    stale_rows = db.get_stale_approval_states(
        stale_after_minutes=stale_after_minutes,
        approval_type=approval_type,
        limit=sample_limit,
    )
    decision_diffs = db.get_recent_approval_decision_diff(
        limit=max(5, min(sample_limit, 50)),
        approval_type=approval_type,
    )
    operator_action_summary = _build_approval_hygiene_operator_action_summary(
        stale_rows,
        decision_diffs,
        auto_cleanup_enabled=auto_cleanup_enabled,
    )
    overview = build_approval_audit_overview(stale_rows=stale_rows, decision_diffs=decision_diffs)
    overview['operator_action_summary'] = operator_action_summary
    return {
        'enabled': enabled,
        'auto_cleanup_enabled': auto_cleanup_enabled,
        'stale_after_minutes': stale_after_minutes,
        'approval_type': approval_type,
        'limit': sample_limit,
        'stale_count': len(stale_rows),
        'decision_diff_count': len(decision_diffs),
        'stale_items': stale_rows,
        'decision_diffs': decision_diffs,
        'audit_overview': overview,
        'operator_action_summary': operator_action_summary,
    }


def maybe_run_approval_hygiene(cfg: Config, db: Database, notifier: NotificationManager = None, force: bool = False) -> dict:
    runtime = load_runtime_state()
    hygiene = build_approval_hygiene_summary(cfg, db)
    hygiene_cfg = cfg.get('runtime.approval_hygiene', {}) or {}
    enabled = bool(hygiene.get('enabled', True))
    auto_cleanup_enabled = bool(hygiene.get('auto_cleanup_enabled', False))
    actor = hygiene_cfg.get('actor') or 'system:approval-hygiene'

    result = {
        'enabled': enabled,
        'auto_cleanup_enabled': auto_cleanup_enabled,
        'force': bool(force),
        'summary': hygiene,
        'cleanup': None,
        'ran_cleanup': False,
    }
    if not enabled and not force:
        result['reason'] = 'disabled'
        return result

    if auto_cleanup_enabled or force:
        cleanup_result = db.cleanup_stale_approval_states(
            stale_after_minutes=hygiene['stale_after_minutes'],
            approval_type=hygiene.get('approval_type'),
            limit=hygiene.get('limit', 20),
            dry_run=not auto_cleanup_enabled and force,
            actor=actor,
        )
        result['cleanup'] = cleanup_result
        result['ran_cleanup'] = True
        if cleanup_result.get('expired_count', 0) > 0 and notifier is not None:
            notifier.notify_runtime(
                'approval_hygiene',
                [
                    f"过期审批自动收口：{cleanup_result.get('expired_count', 0)} 项",
                    f"stale 阈值：{cleanup_result.get('stale_after_minutes', hygiene['stale_after_minutes'])} 分钟",
                ],
                cleanup_result,
            )
        hygiene = build_approval_hygiene_summary(cfg, db)
        result['summary'] = hygiene

    runtime['approval_hygiene'] = {
        'last_run_at': datetime.now().isoformat(),
        'enabled': enabled,
        'auto_cleanup_enabled': auto_cleanup_enabled,
        'stale_after_minutes': hygiene.get('stale_after_minutes'),
        'approval_type': hygiene.get('approval_type'),
        'stale_count': hygiene.get('stale_count', 0),
        'decision_diff_count': hygiene.get('decision_diff_count', 0),
        'expired_count': ((result.get('cleanup') or {}).get('expired_count', 0)),
        'matched_count': ((result.get('cleanup') or {}).get('matched_count', 0)),
        'result': result.get('cleanup') or {},
    }
    save_runtime_state(runtime)
    return result


def build_runtime_health_summary(cfg: Config, db: Database) -> dict:
    runtime = load_runtime_state()
    risk = RiskManager(cfg, db).get_risk_status()
    balance = risk.get('balance', {}) if isinstance(risk, dict) else {}
    mode = PresetManager(cfg).status()
    ml_engine = MLEngine(cfg.all)
    model_rows = []
    for symbol in cfg.symbols:
        metrics = ml_engine.get_model_metrics(symbol) or {}
        model_rows.append({
            'symbol': symbol,
            'test_accuracy': metrics.get('test_accuracy'),
            'f1': metrics.get('f1'),
            'model_file': metrics.get('model_file'),
        })

    last_summary = runtime.get('last_summary') or {}
    adaptive_cfg = cfg.get_adaptive_regime_config() if hasattr(cfg, 'get_adaptive_regime_config') else (cfg.get('adaptive_regime', {}) or {})
    adaptive_defaults = adaptive_cfg.get('defaults', {}) if isinstance(adaptive_cfg, dict) else {}
    adaptive_mode = adaptive_cfg.get('mode', 'observe_only') if isinstance(adaptive_cfg, dict) else 'observe_only'
    adaptive_enabled = bool(adaptive_cfg.get('enabled', False)) if isinstance(adaptive_cfg, dict) else False
    approval_hygiene = build_approval_hygiene_summary(cfg, db)
    approval_runtime = runtime.get('approval_hygiene', {}) if isinstance(runtime.get('approval_hygiene'), dict) else {}
    approval_mode = 'auto-cleanup' if approval_hygiene.get('auto_cleanup_enabled') else 'audit-only'
    lines = [
        f'环境：{cfg.exchange_mode}',
        f'监听币种：{", ".join(cfg.symbols) or "--"}',
        f'当前 preset：{mode.get("current_preset") or "manual"}',
        f'守护间隔：{runtime.get("interval_seconds") or cfg.get("runtime.interval_seconds", 300)} 秒',
        f'下次运行：{runtime.get("next_run_at") or "--"}',
        '---',
        f'最近一轮：signals {last_summary.get("signals", 0)} ｜ passed {last_summary.get("passed", 0)} ｜ opened {last_summary.get("opened", 0)} ｜ errors {last_summary.get("errors", 0)}',
        f'风险状态：{risk.get("status") or "--"} ｜ 暴露 {risk.get("current_exposure", 0)} / {risk.get("max_exposure", 0)}',
        f'余额：total {round(float(balance.get("total", 0) or 0), 2)} ｜ free {round(float(balance.get("free", 0) or 0), 2)}',
        '---',
        f'Adaptive Regime：mode={adaptive_mode} ｜ enabled={"yes" if adaptive_enabled else "no"} ｜ policy={adaptive_defaults.get("policy_version") or "--"}',
        '说明：当前仍为 observe-only 展示层，不改真实交易行为。',
        '---',
        f'Approval Hygiene：mode={approval_mode} ｜ stale={approval_hygiene.get("stale_count", 0)} ｜ decision diff={approval_hygiene.get("decision_diff_count", 0)}',
        f'审批卫生：stale>{approval_hygiene.get("stale_after_minutes", 0)} 分钟 ｜ 上次处理 {approval_runtime.get("last_run_at") or "--"} ｜ 上次过期收口 {approval_runtime.get("expired_count", 0)}',
        f'Operator routing：{approval_hygiene.get("operator_action_summary", {}).get("policy_counts") or {}}',
    ]
    if risk.get('loss_streak_locked'):
        lines.extend(['---', f'连亏熔断：{risk.get("consecutive_losses", 0)}/{risk.get("max_consecutive_losses", 0)} ｜ 自动恢复 {risk.get("loss_streak_recover_at") or "--"}'])
    if model_rows:
        model_text = ' ｜ '.join([
            f"{row['symbol']}: acc={row['test_accuracy'] if row['test_accuracy'] is not None else '--'}, f1={row['f1'] if row['f1'] is not None else '--'}"
            for row in model_rows
        ])
        lines.extend(['---', f'ML 状态：{model_text}'])
    return {
        'title': '🩺 每日健康汇总',
        'lines': lines,
        'details': {
            'runtime': runtime,
            'risk': risk,
            'mode': mode,
            'models': model_rows,
            'approval_hygiene': approval_hygiene,
        }
    }


def maybe_send_daily_health_summary(cfg: Config, db: Database, notifier: NotificationManager, force: bool = False) -> dict:
    runtime = load_runtime_state()
    health_cfg = cfg.get('runtime.health_summary', {}) or {}
    enabled = bool(health_cfg.get('enabled', True))
    hour = int(health_cfg.get('hour', 20) or 20)
    now = datetime.now()
    health_state = runtime.get('health_summary', {}) if isinstance(runtime.get('health_summary'), dict) else {}
    today = now.strftime('%Y-%m-%d')

    if not force:
        if not enabled:
            return {'sent': False, 'reason': 'disabled'}
        if health_state.get('last_sent_date') == today:
            return {'sent': False, 'reason': 'already-sent'}
        if now.hour < hour:
            return {'sent': False, 'reason': 'before-hour', 'hour': hour}

    summary = build_runtime_health_summary(cfg, db)
    result = notifier.send('runtime', summary['title'], summary['lines'], 'info', summary['details'], priority='normal')
    runtime['health_summary'] = {
        'last_sent_at': now.isoformat(),
        'last_sent_date': today,
        'hour': hour,
        'result': result,
    }
    save_runtime_state(runtime)
    return {'sent': True, 'summary': summary, 'result': result}


def build_exchange_diagnostics(cfg: Config, exchange: Exchange) -> dict:
    """构建交易所诊断信息（只读，不下单）"""
    report = {
        'exchange_mode': cfg.exchange_mode,
        'position_mode': cfg.position_mode,
        'symbols': [],
        'balance_error': None,
    }

    available = 0
    try:
        balance = exchange.fetch_balance()
        available = float((balance.get('free') or {}).get('USDT', 0) or 0)
        report['available_usdt'] = round(available, 4)
    except Exception as e:
        report['available_usdt'] = 0
        report['balance_error'] = str(e)

    desired_notional = available * float(cfg.position_size or 0) * float(cfg.leverage or 0)

    for symbol in cfg.symbols:
        row = {'symbol': symbol}
        try:
            row['is_futures_symbol'] = bool(exchange.is_futures_symbol(symbol))
            if row['is_futures_symbol']:
                row['order_symbol'] = exchange.get_order_symbol(symbol)
                ticker = exchange.fetch_ticker(symbol)
                row['last_price'] = ticker.get('last')
                if row['last_price']:
                    row['sample_amount'] = exchange.normalize_contract_amount(symbol, desired_notional, row['last_price'])
                preview = {'tdMode': 'isolated'}
                if str(cfg.position_mode).lower() not in {'oneway', 'one-way', 'net', 'single'}:
                    preview['posSide'] = 'long'
                row['order_params_preview'] = preview
            else:
                row['reason'] = 'not-swap-market'
        except Exception as e:
            row['error'] = str(e)
        report['symbols'].append(row)

    return report


def build_exchange_smoke_plan(cfg: Config, exchange: Exchange, symbol: str = None, side: str = 'long') -> dict:
    """构建最小 testnet 验收计划；默认只预演，不落单"""
    selected_symbol = symbol or (cfg.symbols[0] if cfg.symbols else None)
    plan = {
        'exchange_mode': cfg.exchange_mode,
        'position_mode': cfg.position_mode,
        'symbol': selected_symbol,
        'side': side,
        'execute_ready': False,
        'steps': [
            '读取余额',
            '检查目标是否为 U 本位永续',
            '获取最新价格',
            '换算最小验收仓位数量',
            '预览开仓参数',
            '预览平仓参数',
        ]
    }
    if not selected_symbol:
        plan['error'] = '未配置任何 watch_list 币种'
        return plan
    try:
        balance = exchange.fetch_balance()
        available = float((balance.get('free') or {}).get('USDT', 0) or 0)
        plan['available_usdt'] = round(available, 4)
        plan['is_futures_symbol'] = bool(exchange.is_futures_symbol(selected_symbol))
        if not plan['is_futures_symbol']:
            plan['error'] = '目标币种不是可用合约'
            return plan
        ticker = exchange.fetch_ticker(selected_symbol)
        last_price = float(ticker.get('last') or 0)
        plan['last_price'] = last_price
        smoke_notional = max(5.0, available * 0.01)
        plan['smoke_notional'] = round(smoke_notional, 4)
        plan['sample_amount'] = exchange.normalize_contract_amount(selected_symbol, smoke_notional, last_price)
        preview_open = {'tdMode': 'isolated'}
        preview_close = {'tdMode': 'isolated', 'reduceOnly': True}
        if str(cfg.position_mode).lower() not in {'oneway', 'one-way', 'net', 'single'}:
            preview_open['posSide'] = side
            preview_close['posSide'] = side
        plan['open_preview'] = {
            'symbol': exchange.get_order_symbol(selected_symbol),
            'side': 'buy' if side == 'long' else 'sell',
            'amount': plan['sample_amount'],
            'params': preview_open,
        }
        plan['close_preview'] = {
            'symbol': exchange.get_order_symbol(selected_symbol),
            'side': 'sell' if side == 'long' else 'buy',
            'amount': plan['sample_amount'],
            'params': preview_close,
        }
        plan['execute_ready'] = True
    except Exception as e:
        plan['error'] = str(e)
    return plan




def backfill_closed_trades_from_exchange(exchange: Exchange, db: Database, limit: int = 50) -> dict:
    report = {'checked': 0, 'patched': 0, 'errors': []}
    report['mapping_repair'] = db.repair_trade_quantity_mappings(symbols=['BTC/USDT', 'XRP/USDT'])
    for trade in db.get_trades_missing_close_details(limit=limit):
        report['checked'] += 1
        try:
            fallback_price = trade.get('exit_price') or None
            if not fallback_price:
                try:
                    ticker = exchange.fetch_ticker(trade['symbol'])
                    fallback_price = ticker.get('last')
                except Exception:
                    fallback_price = None
            summary = exchange.fetch_closed_trade_summary(trade, fallback_price=fallback_price)
            if not summary and fallback_price:
                summary = exchange.build_close_summary([], open_trade=trade, fallback_price=fallback_price, source='ticker_fallback')
            if summary and db.reconcile_trade_close(trade['id'], summary, reason='历史已关闭交易补齐'):
                report['patched'] += 1
        except Exception as e:
            report['errors'].append({'trade_id': trade.get('id'), 'symbol': trade.get('symbol'), 'error': str(e)})
    return report

def reconcile_exchange_positions(exchange: Exchange, db: Database) -> dict:
    """交易所持仓与本地 DB / open trades 三方对账（第二轮）"""
    report = {
        'synced': 0,
        'removed': 0,
        'exchange_positions': [],
        'local_before': db.get_positions(),
        'local_open_trades': db.get_trades(status='open', limit=200),
    }
    exchange_positions = exchange.fetch_positions()
    report['history_backfill'] = backfill_closed_trades_from_exchange(exchange, db, limit=50)
    normalized_symbols = []
    normalized_keys = []
    created_open_trades = []
    for pos in exchange_positions:
        if pos.get('contract_size') is not None:
            normalized = pos
        elif hasattr(exchange, 'normalize_position'):
            normalized = exchange.normalize_position(pos)
        else:
            raw_symbol = pos.get('symbol')
            normalized = {
                'symbol': raw_symbol.split(':')[0] if isinstance(raw_symbol, str) and ':' in raw_symbol else raw_symbol,
                'side': pos.get('side'),
                'quantity': pos.get('quantity', pos.get('contracts', 0)),
                'contracts': pos.get('contracts', pos.get('quantity', 0)),
                'entry_price': pos.get('entry_price', pos.get('entryPrice', 0)),
                'current_price': pos.get('current_price', pos.get('markPrice', pos.get('entryPrice', 0))),
                'leverage': pos.get('leverage', 1),
                'contract_size': pos.get('contract_size', 1),
                'coin_quantity': pos.get('coin_quantity', (pos.get('contracts', pos.get('quantity', 0)) or 0) * (pos.get('contract_size', 1) or 1)),
                'realized_pnl': pos.get('realized_pnl'),
            }
        if not normalized:
            continue
        symbol = normalized['symbol']
        side = normalized['side']
        contracts = float(normalized.get('quantity') or normalized.get('contracts') or 0)
        if contracts <= 0:
            continue
        entry_price = float(normalized.get('entry_price') or 0)
        current_price = float(normalized.get('current_price') or entry_price or 0)
        leverage = int(float(normalized.get('leverage') or 1))
        contract_size = float(normalized.get('contract_size') or 1)
        coin_quantity = float(normalized.get('coin_quantity') or contracts * contract_size)
        db.update_position(symbol, side, entry_price, contracts, leverage, current_price, contract_size=contract_size, coin_quantity=coin_quantity)
        existing_open_trade = db.get_latest_open_trade(symbol, side)
        if not existing_open_trade:
            trade_id = db.record_trade(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                quantity=contracts,
                leverage=leverage,
                notes='对账补建 open trade（以交易所当前持仓为准）',
                contract_size=contract_size,
                coin_quantity=coin_quantity,
            )
            created_open_trades.append({'trade_id': trade_id, 'symbol': symbol, 'side': side, 'quantity': contracts, 'coin_quantity': coin_quantity, 'leverage': leverage})
        else:
            db.sync_trade_with_exchange_snapshot(
                existing_open_trade['id'],
                quantity=contracts,
                contract_size=contract_size,
                coin_quantity=coin_quantity,
                leverage=leverage,
                entry_price=entry_price,
                notes='交易所持仓对账同步',
            )
        normalized_symbols.append(symbol)
        normalized_keys.append(f'{symbol}::{side}')
        report['exchange_positions'].append({'symbol': symbol, 'side': side, 'quantity': contracts, 'coin_quantity': coin_quantity, 'entry_price': entry_price, 'current_price': current_price, 'leverage': leverage, 'realized_pnl': normalized.get('realized_pnl')})
        report['synced'] += 1
    report['removed'] = db.remove_positions_not_in(normalized_symbols)
    report['local_after'] = db.get_positions()
    report['local_open_trades'] = db.get_trades(status='open', limit=200)

    local_after = report['local_after']
    local_open_trades = report['local_open_trades']
    local_position_keys = {f"{p.get('symbol')}::{p.get('side')}" for p in local_after}
    open_trade_keys = {f"{t.get('symbol')}::{t.get('side')}" for t in local_open_trades}
    exchange_key_set = set(normalized_keys)

    report['diff'] = {
        'exchange_missing_local_position': [row for row in report['exchange_positions'] if f"{row['symbol']}::{row['side']}" not in local_position_keys],
        'local_position_missing_exchange': [row for row in local_after if f"{row.get('symbol')}::{row.get('side')}" not in exchange_key_set],
        'open_trade_missing_exchange': [row for row in local_open_trades if f"{row.get('symbol')}::{row.get('side')}" not in exchange_key_set],
        'exchange_missing_open_trade': [row for row in report['exchange_positions'] if f"{row['symbol']}::{row['side']}" not in open_trade_keys],
    }
    healed_open_trades = []
    for row in list(report['diff']['exchange_missing_open_trade']):
        symbol = row.get('symbol')
        side = row.get('side')
        if not symbol or not side:
            continue
        if db.get_latest_open_trade(symbol, side):
            continue
        matched_local = next((p for p in local_after if p.get('symbol') == symbol and p.get('side') == side), None)
        base_row = matched_local or row
        trade_id = db.record_trade(
            symbol=symbol,
            side=side,
            entry_price=float(base_row.get('entry_price') or 0),
            quantity=float(base_row.get('quantity') or 0),
            leverage=int(float(base_row.get('leverage') or 1)),
            notes='对账自愈补建 open trade（exchange/local snapshot fallback）',
            contract_size=float(base_row.get('contract_size') or 1),
            coin_quantity=float(base_row.get('coin_quantity') or 0),
        )
        healed_open_trades.append({'trade_id': trade_id, 'symbol': symbol, 'side': side})
    if healed_open_trades:
        report['local_open_trades'] = db.get_trades(status='open', limit=200)
        local_open_trades = report['local_open_trades']
        open_trade_keys = {f"{t.get('symbol')}::{t.get('side')}" for t in local_open_trades}
        report['diff']['exchange_missing_open_trade'] = [row for row in report['exchange_positions'] if f"{row['symbol']}::{row['side']}" not in open_trade_keys]
    stale_closed = []
    for row in report['diff']['open_trade_missing_exchange']:
        trade_id = row.get('id')
        symbol = row.get('symbol')
        side = row.get('side')
        current_price = None
        matched_local = next((p for p in local_after if p.get('symbol') == symbol and p.get('side') == side), None)
        if matched_local:
            current_price = matched_local.get('current_price') or matched_local.get('entry_price')
        if trade_id:
            summary = None
            try:
                summary = exchange.fetch_closed_trade_summary(row, fallback_price=current_price)
            except Exception:
                summary = None
            if not summary and current_price:
                summary = exchange.build_close_summary([], open_trade=row, fallback_price=current_price, source='ticker_fallback')
            changed = db.reconcile_trade_close(trade_id, summary or {'exit_price': current_price, 'source': 'reconcile_fallback', 'fills': []}, reason='自动收口: 交易所无对应持仓，对账自动收口')
            if changed:
                stale_closed.append({'trade_id': trade_id, 'symbol': symbol, 'side': side, 'close_source': (summary or {}).get('source', 'reconcile_fallback')})
    if stale_closed:
        report['local_open_trades'] = db.get_trades(status='open', limit=200)
        local_open_trades = report['local_open_trades']
        open_trade_keys = {f"{t.get('symbol')}::{t.get('side')}" for t in local_open_trades}
        report['diff']['open_trade_missing_exchange'] = [row for row in local_open_trades if f"{row.get('symbol')}::{row.get('side')}" not in exchange_key_set]
        report['diff']['exchange_missing_open_trade'] = [row for row in report['exchange_positions'] if f"{row['symbol']}::{row['side']}" not in open_trade_keys]
    touched_pairs = {(row.get('symbol'), row.get('side')) for row in report['exchange_positions'] if row.get('symbol') and row.get('side')}
    touched_pairs.update((row.get('symbol'), row.get('side')) for row in stale_closed if row.get('symbol') and row.get('side'))
    touched_pairs.update((row.get('symbol'), row.get('side')) for row in created_open_trades if row.get('symbol') and row.get('side'))
    touched_pairs.update((row.get('symbol'), row.get('side')) for row in healed_open_trades if row.get('symbol') and row.get('side'))
    if not touched_pairs:
        touched_pairs.update((row.get('symbol'), row.get('side')) for row in report.get('local_before', []) if row.get('symbol') and row.get('side'))
    report['layer_state_sync'] = [db.sync_layer_plan_state(symbol, side, reset_if_flat=True) for symbol, side in sorted(touched_pairs)]
    report['orphan_cleanup'] = db.cleanup_orphan_execution_state(stale_after_minutes=15)

    report['summary'] = {
        'exchange_positions': len(report['exchange_positions']),
        'local_positions': len(local_after),
        'open_trades': len(local_open_trades),
        'created_open_trades': len(created_open_trades),
        'healed_open_trades': len(healed_open_trades),
        'exchange_missing_local_position': len(report['diff']['exchange_missing_local_position']),
        'local_position_missing_exchange': len(report['diff']['local_position_missing_exchange']),
        'open_trade_missing_exchange': len(report['diff']['open_trade_missing_exchange']),
        'exchange_missing_open_trade': len(report['diff']['exchange_missing_open_trade']),
        'stale_open_trades_closed': len(stale_closed),
        'history_backfilled': int((report.get('history_backfill') or {}).get('patched', 0) or 0),
        'layer_states_synced': len(report['layer_state_sync']),
        'orphan_intents_cleaned': len((report.get('orphan_cleanup') or {}).get('removed_intents', [])),
        'orphan_intents_healed': len((report.get('orphan_cleanup') or {}).get('healed_intents', [])),
        'orphan_locks_cleaned': len((report.get('orphan_cleanup') or {}).get('removed_locks', [])),
        'orphan_locks_healed': len((report.get('orphan_cleanup') or {}).get('healed_locks', [])),
        'layer_plan_resets': len((report.get('orphan_cleanup') or {}).get('plan_resets', [])),
    }
    report['stale_closed'] = stale_closed
    report['created_open_trades'] = created_open_trades
    report['healed_open_trades'] = healed_open_trades
    return report


def execute_exchange_smoke(cfg: Config, exchange: Exchange, symbol: str = None, side: str = 'long', db: Database = None) -> dict:
    """执行最小 testnet 开平仓验收。只允许 testnet。"""
    plan = build_exchange_smoke_plan(cfg, exchange, symbol=symbol, side=side)
    result = {
        'plan': plan,
        'opened': False,
        'closed': False,
        'open_status': 'not_started',
        'close_status': 'not_started',
        'cleanup_needed': False,
        'residual_position_detected': False,
        'reconcile_summary': {
            'open_order_confirmed': False,
            'close_order_confirmed': False,
            'residual_position_detected': False,
            'cleanup_attempted': False,
            'cleanup_succeeded': False,
            'residual_quantity': 0.0,
        },
        'failure_compensation_hint': None,
    }

    def _call_exchange_method(name, *args, **kwargs):
        fn = getattr(exchange, name, None)
        if not callable(fn):
            return None
        return fn(*args, **kwargs)

    def _normalize_status(payload, default_status):
        if isinstance(payload, dict):
            status = str(payload.get('status') or default_status)
            return {**payload, 'status': status}
        if payload is None:
            return {'status': default_status}
        return {'status': str(payload)}

    def _normalize_residual(payload):
        if isinstance(payload, dict):
            quantity = float(payload.get('quantity') or payload.get('contracts') or payload.get('residual_quantity') or 0.0)
            detected = bool(payload.get('detected', quantity > 0))
            return {**payload, 'detected': detected, 'quantity': quantity}
        quantity = float(payload or 0.0)
        return {'detected': quantity > 0, 'quantity': quantity}

    if plan.get('error'):
        result['error'] = plan['error']
        result['open_status'] = 'plan_error'
        result['close_status'] = 'plan_error'
        result['failure_compensation_hint'] = 'fix_smoke_plan_before_execute'
    elif str(cfg.exchange_mode).lower() != 'testnet':
        result['error'] = '只允许在 testnet 模式执行 smoke 验收'
        result['open_status'] = 'blocked'
        result['close_status'] = 'blocked'
        result['failure_compensation_hint'] = 'real_mode_blocked_no_execution'
    else:
        try:
            open_side = 'buy' if side == 'long' else 'sell'
            close_side = 'sell' if side == 'long' else 'buy'
            amount = plan['sample_amount']
            open_order = exchange.create_order(plan['symbol'], open_side, amount, posSide=side)
            result['opened'] = True
            result['open_order'] = open_order
            open_status_payload = _normalize_status(_call_exchange_method('confirm_smoke_open', plan['symbol'], side, amount, open_order=open_order), 'filled')
            result['open_status'] = open_status_payload.get('status', 'filled')
            result['open_confirmation'] = open_status_payload
            result['reconcile_summary']['open_order_confirmed'] = result['open_status'] in {'filled', 'closed', 'confirmed', 'opened'}

            close_order = exchange.close_order(plan['symbol'], close_side, amount, posSide=side)
            result['closed'] = True
            result['close_order'] = close_order
            close_status_payload = _normalize_status(_call_exchange_method('confirm_smoke_close', plan['symbol'], side, amount, close_order=close_order, open_order=open_order), 'filled')
            result['close_status'] = close_status_payload.get('status', 'filled')
            result['close_confirmation'] = close_status_payload
            result['reconcile_summary']['close_order_confirmed'] = result['close_status'] in {'filled', 'closed', 'confirmed'}

            residual_payload = _normalize_residual(_call_exchange_method('detect_smoke_residual_position', plan['symbol'], side, amount, open_order=open_order, close_order=close_order))
            result['residual_position'] = residual_payload
            result['residual_position_detected'] = residual_payload.get('detected', False)
            result['reconcile_summary']['residual_position_detected'] = result['residual_position_detected']
            result['reconcile_summary']['residual_quantity'] = residual_payload.get('quantity', 0.0)

            cleanup_needed = (not result['reconcile_summary']['close_order_confirmed']) or result['residual_position_detected']
            result['cleanup_needed'] = cleanup_needed
            if cleanup_needed:
                cleanup_payload = _normalize_status(_call_exchange_method('cleanup_smoke_position', plan['symbol'], side, amount, open_order=open_order, close_order=close_order, residual_position=residual_payload), 'not_attempted')
                result['cleanup_result'] = cleanup_payload
                result['reconcile_summary']['cleanup_attempted'] = cleanup_payload.get('status') != 'not_attempted'
                result['reconcile_summary']['cleanup_succeeded'] = cleanup_payload.get('status') in {'flattened', 'clean', 'confirmed', 'success'}
                if not result['reconcile_summary']['cleanup_succeeded']:
                    if result['residual_position_detected']:
                        result['failure_compensation_hint'] = 'manual_testnet_cleanup_required'
                    else:
                        result['failure_compensation_hint'] = 'retry_close_confirmation_or_manual_cleanup'
            if not result['cleanup_needed']:
                result['failure_compensation_hint'] = None
        except Exception as e:
            result['error'] = str(e)
            result['cleanup_needed'] = bool(result.get('opened') and not result.get('closed'))
            result['failure_compensation_hint'] = 'inspect_open_order_and_force_flatten_on_testnet' if result['cleanup_needed'] else 'inspect_bridge_execution_error'
            if result['opened'] and result['open_status'] == 'not_started':
                result['open_status'] = 'submitted'
            if result['close_status'] == 'not_started':
                result['close_status'] = 'error'

    if db is not None:
        details = {
            'plan': plan,
            'opened': result.get('opened', False),
            'closed': result.get('closed', False),
            'open_order': result.get('open_order'),
            'close_order': result.get('close_order'),
            'open_status': result.get('open_status'),
            'close_status': result.get('close_status'),
            'cleanup_needed': result.get('cleanup_needed', False),
            'residual_position_detected': result.get('residual_position_detected', False),
            'reconcile_summary': result.get('reconcile_summary') or {},
            'failure_compensation_hint': result.get('failure_compensation_hint'),
        }
        smoke_run_id = db.record_smoke_run(
            exchange_mode=cfg.exchange_mode,
            position_mode=cfg.position_mode,
            symbol=plan.get('symbol') or symbol or '--',
            side=side,
            amount=plan.get('sample_amount'),
            success=bool(result.get('opened') and result.get('closed') and not result.get('error') and not result.get('cleanup_needed')),
            error=result.get('error'),
            details=details,
        )
        result['smoke_run_id'] = smoke_run_id
    return result


class RuntimeGuard:
    def __init__(self, lock_path: str = '/tmp/crypto_quant_okx_bot.lock'):
        self.lock_path = Path(lock_path)
        self.locked = False

    def acquire(self) -> bool:
        if self.lock_path.exists():
            try:
                pid = int(self.lock_path.read_text().strip() or 0)
                if pid > 0:
                    os.kill(pid, 0)
                    return False
            except Exception:
                pass
            try:
                self.lock_path.unlink()
            except Exception:
                return False
        self.lock_path.write_text(str(os.getpid()))
        self.locked = True
        return True

    def release(self):
        if self.locked and self.lock_path.exists():
            try:
                self.lock_path.unlink()
            except Exception:
                pass
        self.locked = False


class TradingBot:
    """交易机器人主类"""
    
    def __init__(self):
        self.config = Config()
        self.db = Database(self.config.db_path)
        self.exchange = Exchange(self.config.all)
        self.detector = SignalDetector(self.config.all)
        self.validator = SignalValidator(self.config, self.exchange)
        self.recorder = SignalRecorder(self.db)
        self.entry_decider = EntryDecider(self.config.all)  # Entry Decision Layer MVP
        self.executor = TradingExecutor(self.config, self.exchange, self.db)
        self.risk_mgr = RiskManager(self.config, self.db)
        self.ml = MLEngine(self.config.all)
        self.notifier = NotificationManager(self.config, self.db, logger)
        
        logger.info("交易机器人初始化完成")
    
    def run(self):
        """运行交易循环"""
        started_at = datetime.now()
        summary = {'started_at': started_at.isoformat(), 'symbols': len(self.config.symbols), 'signals': 0, 'passed': 0, 'opened': 0, 'closed': 0, 'errors': 0}
        print(f"\n{'='*60}")
        print(f"🤖 OKX量化交易系统 v2.0")
        print(f"   时间: {started_at}")
        print(f"   币种: {', '.join(self.config.symbols)}")
        print(f"{'='*60}\n")
        # 开始类推送太噪音，改为只写运行历史，不主动推送
        state = load_runtime_state()
        state.update({'running': True, 'last_started_at': started_at.isoformat(), 'last_error': None})
        save_runtime_state(state)
        append_runtime_history({'type': 'start', 'time': started_at.isoformat(), 'message': '机器人周期开始'})
        
        # 获取余额
        try:
            balance = self.exchange.fetch_balance()
            available = balance.get('free', {}).get('USDT', 0)
            print(f"💰 账户余额: {available:.2f} USDT\n")
        except Exception as e:
            print(f"⚠️ 获取余额失败: {e}")
            available = 0
        
        # 先与交易所持仓对账
        reconcile_report = {'synced': 0, 'removed': 0}
        try:
            reconcile_report = reconcile_exchange_positions(self.exchange, self.db)
            reconcile_summary = reconcile_report.get('summary', {})
            reconcile_diff_count = sum(int(reconcile_summary.get(k, 0) or 0) for k in ['exchange_missing_local_position', 'local_position_missing_exchange', 'open_trade_missing_exchange', 'exchange_missing_open_trade'])
            if reconcile_diff_count > 0:
                self.notifier.notify_reconcile_issue(reconcile_report)
        except Exception as e:
            self.notifier.notify_error('持仓对账失败', str(e), {})
        # 获取当前持仓
        positions = self.db.get_positions()
        print(f"📊 当前持仓: {len(positions)}个")
        for p in positions:
            print(f"   {p['symbol']} | {p['side']} | {p['quantity']} | "
                  f"开仓: {p['entry_price']:.2f} | 当前: {p.get('current_price', 'N/A')}")
        print()
        
        # 遍历所有监控的币种
        for symbol in self.config.symbols:
            print(f"=== 分析 {symbol} ===")
            
            try:
                if not self.exchange.is_futures_symbol(symbol):
                    self.notifier.notify_runtime('skip', [f'币种：{symbol}', '原因：暂无 U 本位永续合约'], {'symbol': symbol, 'reason': 'not-futures'})
                    print(f"   ⏭️ 跳过: {symbol} 暂无U本位永续合约")
                    print()
                    continue
                # 获取K线数据
                ohlcv = self.exchange.fetch_ohlcv(symbol, '1h', limit=100)
                df = pd.DataFrame(ohlcv)
                df = self._add_indicators(df)
                
                # 获取当前价格
                ticker = self.exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                
                # 获取ML预测
                ml_pred = None
                if self.ml.enabled:
                    ml_pred = self.ml.predict(symbol, ohlcv)
                
                # 分析信号
                signal = self.detector.analyze(symbol, df, current_price, ml_pred)
                
                print(f"   价格: {current_price:.4f}")
                print(f"   信号: {signal.signal_type.upper()} | 强度: {signal.strength}%")
                print(f"   触发策略: {', '.join(signal.strategies_triggered) or '无'}")
                
                # 详细指标
                indicators = signal.indicators
                if 'RSI' in indicators:
                    print(f"   RSI: {indicators.get('RSI', 'N/A')}")
                if 'MACD' in indicators:
                    print(f"   MACD: {indicators.get('MACD', 'N/A')}")
                
                # 获取当前持仓（每个 symbol 都重新读取，避免沿用本轮旧快照）
                positions = self.db.get_positions()
                current_positions = {p['symbol']: p for p in positions}
                
                # ===== Entry Decision Layer MVP =====
                # 评估"这个信号值不值得开单"
                tracking_data = {}  # 可以从 db 或 cache 获取
                ml_pred = ml_pred if 'ml_pred' in dir() else None
                entry_decision = self.entry_decider.decide(
                    signal, 
                    current_positions=current_positions,
                    tracking_data=tracking_data,
                    ml_prediction=ml_pred
                )
                # 将决策结果写入 filter_details 供观测
                signal.filter_details = signal.filter_details or {}
                signal.filter_details['entry_decision'] = entry_decision.to_dict()
                # 打印决策结果
                print(f"   🎯 开单决策: {entry_decision.decision.upper()} | 分数: {entry_decision.score}")
                # ================================
                
                # 验证信号
                passed, reason, details = self.validator.validate(signal, current_positions)

                # Entry Decision 真正接入执行门槛：watch/block 不再继续开仓
                if passed and signal.signal_type in ['buy', 'sell'] and entry_decision.decision != 'allow':
                    passed = False
                    reason = f"EntryDecision={entry_decision.decision}: {entry_decision.reason_summary}"
                    details = details or {}
                    details['entry_decision_gate'] = {
                        'passed': False,
                        'reason': reason,
                        'decision': entry_decision.decision,
                        'score': entry_decision.score,
                        'watch_reasons': entry_decision.watch_reasons,
                        'group': 'entry_decision',
                    }
                    details['filter_meta'] = {
                        'code': f"ENTRY_DECISION_{entry_decision.decision.upper()}",
                        'group': 'entry_decision',
                        'action_hint': '仅当 Entry Decision=allow 时才执行开仓，当前建议继续观望',
                    }

                summary['signals'] += 1
                if passed:
                    summary['passed'] += 1
                signal.filtered = not passed
                signal.filter_reason = reason
                
                if not passed:
                    print(f"   ❌ 信号过滤: {reason}")
                self.notifier.notify_signal(signal, passed, reason, details)
                
                # 记录信号
                signal_id = self.recorder.record(signal, (passed, reason, details))
                base_obs = {
                    'signal_id': signal_id,
                    'root_signal_id': signal_id,
                    'layer_no': None,
                    'deny_reason': reason if not passed else None,
                    'current_symbol_exposure': 0.0,
                    'projected_symbol_exposure': 0.0,
                    'current_total_exposure': 0.0,
                    'projected_total_exposure': 0.0,
                }
                merged_filter_details = dict(signal.filter_details or {})
                merged_filter_details['observability'] = {**base_obs, **dict(merged_filter_details.get('observability') or {})}
                self.db.update_signal(signal_id, filter_details=json.dumps(merged_filter_details, ensure_ascii=False))
                
                # 如果信号通过且可以开仓
                if passed and signal.signal_type in ['buy', 'sell']:
                    # 风险检查
                    can_open, risk_reason, risk_details = self.risk_mgr.can_open_position(symbol, side='long' if signal.signal_type == 'buy' else 'short', signal_id=signal_id, plan_context={'regime_snapshot': getattr(signal, 'regime_snapshot', {}) or getattr(signal, 'regime_info', {}) or {}, 'adaptive_policy_snapshot': getattr(signal, 'adaptive_policy_snapshot', {}) or {}})
                    risk_obs = dict((risk_details or {}).get('observability') or {})
                    if risk_obs:
                        merged_filter_details = dict(signal.filter_details or {})
                        merged_filter_details['observability'] = {**dict(merged_filter_details.get('observability') or {}), **risk_obs, 'deny_reason': None if can_open else (risk_reason or risk_obs.get('deny_reason'))}
                        self.db.update_signal(signal_id, filter_details=json.dumps(merged_filter_details, ensure_ascii=False), filter_reason=(None if can_open else risk_reason))
                    self.notifier.notify_decision(signal, can_open, risk_reason, risk_details)
                    lock_info = (risk_details or {}).get('loss_streak_limit', {}) if isinstance(risk_details, dict) else {}
                    if lock_info.get('just_triggered'):
                        self.notifier.notify_loss_streak_lock(
                            current=lock_info.get('current', 0),
                            max_count=lock_info.get('max', 0),
                            recover_at=lock_info.get('recover_at'),
                            details={'symbol': symbol, 'risk_details': risk_details}
                        )
                    
                    if can_open:
                        side = 'long' if signal.signal_type == 'buy' else 'short'
                        
                        # 开仓
                        trade_id = self.executor.open_position(
                            symbol, side, current_price, signal_id,
                            plan_context=(risk_details or {}).get('exposure_limit', {}).get('layer_plan') or (risk_details or {}).get('layer_eligibility', {}).get('layer_plan'),
                            root_signal_id=signal_id
                        )
                        positions = self.db.get_positions()
                        
                        if trade_id:
                            summary['opened'] += 1
                            self.recorder.mark_executed(signal_id, trade_id)
                            latest_trade = self.db.get_latest_open_trade(symbol, side)
                            contracts = latest_trade.get('quantity') if latest_trade else 0
                            quantity_details = {}
                            try:
                                contract_size = self.exchange.get_contract_size(symbol)
                                coin_quantity = self.exchange.contracts_to_coin_quantity(symbol, contracts)
                                quantity_details = {
                                    'contracts': contracts,
                                    'contract_size': contract_size,
                                    'coin_quantity': coin_quantity,
                                    'notional_usdt': self.exchange.estimate_notional_usdt(symbol, contracts, current_price),
                                }
                            except Exception:
                                quantity_details = {}
                            self.notifier.notify_trade_open(symbol, side, current_price, contracts, trade_id, signal, quantity_details=quantity_details)
                            print(f"   ✅ 开{'多' if side == 'long' else '空'}成功! Trade ID: {trade_id}")
                        else:
                            self.notifier.notify_trade_open_failed(symbol, side, current_price, '交易所拒绝或执行器返回空结果', signal, {'signal_id': signal_id})
                            print(f"   ❌ 开仓失败")
                    else:
                        print(f"   ⏸️ 风险检查阻止: {risk_reason}")
                
                print()
                
            except Exception as e:
                summary['errors'] += 1
                self.notifier.notify_error('处理币种出错', f'{symbol}: {e}', {'symbol': symbol})
                logger.error(f"处理{symbol}出错: {e}")
                print(f"   ⚠️ 错误: {e}\n")
        
        # 检查现有持仓的止盈止损
        print("=== 检查持仓 ===")
        positions = self.db.get_positions()
        
        for position in positions:
            symbol = position['symbol']
            
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                
                # 更新持仓价格
                self.db.update_position(
                    symbol, position['side'], position['entry_price'],
                    position['quantity'], position['leverage'], current_price
                )
                
                # 检查止损
                if self.executor.check_stop_loss(symbol, current_price):
                    closed = self.executor.close_position(symbol, '止损')
                    if closed:
                        summary['closed'] += 1
                        self.notifier.notify_trade_close(symbol, position['side'], current_price, '止损')
                    else:
                        self.notifier.notify_trade_close_failed(symbol, position['side'], '止损触发后平仓失败', {'symbol': symbol, 'reason': '止损'})
                    print(f"   🔴 止损: {symbol}")
                
                # 检查止盈
                elif self.executor.check_take_profit(symbol, current_price):
                    closed = self.executor.close_position(symbol, '止盈')
                    if closed:
                        summary['closed'] += 1
                        self.notifier.notify_trade_close(symbol, position['side'], current_price, '止盈')
                    else:
                        self.notifier.notify_trade_close_failed(symbol, position['side'], '止盈触发后平仓失败', {'symbol': symbol, 'reason': '止盈'})
                    print(f"   🟢 止盈: {symbol}")
                
            except Exception as e:
                summary['errors'] += 1
                self.notifier.notify_error('检查持仓失败', f'{symbol}: {e}', {'symbol': symbol})
                logger.error(f"检查持仓{symbol}出错: {e}")
        finished_at = datetime.now()
        summary['finished_at'] = finished_at.isoformat()
        state = load_runtime_state()
        state.update({'running': False, 'last_finished_at': finished_at.isoformat(), 'last_summary': summary})
        save_runtime_state(state)
        append_runtime_history({'type': 'end', 'time': finished_at.isoformat(), 'message': f'周期完成：信号 {summary["signals"]} / 开仓 {summary["opened"]} / 平仓 {summary["closed"]} / 错误 {summary["errors"]}'})
        end_lines = [
            f'开始：{summary["started_at"]}',
            f'结束：{summary["finished_at"]}',
            f'监听币种：{", ".join(self.config.symbols)}',
            f'持仓对账：同步 {reconcile_report.get("synced", 0) if isinstance(reconcile_report, dict) else 0} 条 ｜ 清理 {reconcile_report.get("removed", 0) if isinstance(reconcile_report, dict) else 0} 条',
            f'信号：{summary["signals"]} ｜ 通过：{summary["passed"]} ｜ 开仓：{summary["opened"]} ｜ 平仓：{summary["closed"]} ｜ 错误：{summary["errors"]}'
        ]
        self.notifier.notify_runtime('end', end_lines, summary)
        print(f"\n✅ 交易循环完成! {finished_at}\n")
        return summary
    
    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """添加技术指标"""
        close = df[4]
        
        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(14).mean()
        avg_loss = loss.rolling(14).mean()
        rs = avg_gain / (avg_loss + 1e-10)
        df['RSI'] = 100 - (100 / (1 + rs))
        
        # MACD
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        df['MACD'] = ema12 - ema26
        df['MACD_signal'] = df['MACD'].ewm(span=9).mean()
        
        # 布林带
        df['BB_mid'] = close.rolling(20).mean()
        std = close.rolling(20).std()
        df['BB_upper'] = df['BB_mid'] + 2 * std
        df['BB_lower'] = df['BB_mid'] - 2 * std
        
        return df


def main():
    parser = argparse.ArgumentParser(description='OKX量化交易机器人')
    parser.add_argument('--dashboard', action='store_true', help='启动仪表盘')
    parser.add_argument('--daemon', action='store_true', help='守护模式定时运行交易循环')
    parser.add_argument('--interval-seconds', type=int, help='守护模式执行间隔秒数，默认取 config.runtime.interval_seconds')
    parser.add_argument('--train', action='store_true', help='训练模型')
    parser.add_argument('--collect', action='store_true', help='收集数据')
    parser.add_argument('--backtest', action='store_true', help='运行回测')
    parser.add_argument('--signal-quality', action='store_true', help='分析信号质量')
    parser.add_argument('--optimize', action='store_true', help='运行参数优化与币种分层')
    parser.add_argument('--list-presets', action='store_true', help='列出可用预设')
    parser.add_argument('--apply-preset', type=str, help='应用预设配置')
    parser.add_argument('--mode-status', action='store_true', help='显示当前模式状态')
    parser.add_argument('--daily-summary', action='store_true', help='生成日报摘要')
    parser.add_argument('--health-summary', action='store_true', help='立即生成一份健康汇总并发送通知')
    parser.add_argument('--approval-hygiene', action='store_true', help='执行审批卫生检查；开启 auto_cleanup 时会自动收口 stale 审批')
    parser.add_argument('--cleanup-runtime-records', action='store_true', help='清理重复的治理/日报运行记录')
    parser.add_argument('--exchange-diagnose', action='store_true', help='只读诊断交易所/合约参数，不执行下单')
    parser.add_argument('--notify-test', action='store_true', help='测试 Discord/webhook 通知链路')
    parser.add_argument('--relay-outbox', action='store_true', help='补发 pending notification_outbox 到 Discord')
    parser.add_argument('--once', action='store_true', help='配合 relay-outbox，仅执行一轮后退出')
    parser.add_argument('--relay-limit', type=int, default=20, help='配合 relay-outbox，单轮最多处理几条 pending outbox')
    parser.add_argument('--reconcile-positions', action='store_true', help='只读/同步交易所持仓到本地 DB')
    parser.add_argument('--exchange-smoke', action='store_true', help='生成最小 testnet 验收计划；默认只预演')
    parser.add_argument('--validation-entry', type=str, choices=['run'], help='运行单个 shadow validation case')
    parser.add_argument('--validation-replay', action='store_true', help='运行 validation replay，支持目录/多 case 并输出聚合 summary')
    parser.add_argument('--case', type=str, nargs='+', help='validation case 文件或目录路径（json/yaml，可传多个）')
    parser.add_argument('--validation-output', type=str, help='可选：将 validation report / replay report 写入指定 json/md 文件（.md 导出人类可读摘要）')
    parser.add_argument('--execute', action='store_true', help='配合 smoke 验收命令，显式允许执行 testnet 开平仓')
    parser.add_argument('--symbol', type=str, help='指定 smoke/diagnose 目标币种')
    parser.add_argument('--side', type=str, default='long', choices=['long', 'short'], help='smoke 验收方向')
    parser.add_argument('--dry-run', action='store_true', help='配合清理命令，仅预览不删除')
    parser.add_argument('--port', type=int, default=5555, help='仪表盘端口（默认 5555）')
    
    args = parser.parse_args()
    
    if args.dashboard:
        # 启动仪表盘
        from dashboard.api import run_dashboard
        run_dashboard(port=args.port)

    elif args.daemon:
        cfg = Config()
        interval = args.interval_seconds or int(cfg.get('runtime.interval_seconds', 300))
        guard = RuntimeGuard()
        notifier = NotificationManager(cfg, Database(cfg.db_path), logger)
        save_runtime_state({'mode': 'daemon', 'running': False, 'interval_seconds': interval, 'next_run_at': datetime.now().isoformat()})
        notifier.notify_runtime('daemon', [f'守护间隔：{interval} 秒', f'监控币种：{", ".join(cfg.symbols)}'])
        print(f"\n🔁 守护模式启动，间隔 {interval} 秒\n")
        while True:
            if not guard.acquire():
                now = datetime.now().isoformat()
                state = load_runtime_state()
                state.update({'running': False, 'last_skip_at': now, 'next_run_at': datetime.fromtimestamp(time.time() + interval).isoformat()})
                save_runtime_state(state)
                append_runtime_history({'type': 'skip', 'time': now, 'message': '检测到已有交易周期正在运行，本轮跳过'})
                notifier.notify_runtime('skip', ['检测到已有交易周期正在运行，本轮跳过'])
                time.sleep(interval)
                continue
            try:
                bot = TradingBot()
                bot.run()
            except Exception as e:
                error_time = datetime.now().isoformat()
                state = load_runtime_state()
                db = Database(cfg.db_path)
                notification_stats = db.get_notification_outbox_stats()
                relay_state = state.get('relay', {}) if isinstance(state.get('relay'), dict) else {}
                error_context = {
                    'interval_seconds': interval,
                    'mode': state.get('mode') or 'daemon',
                    'next_run_at': state.get('next_run_at'),
                    'relay_running': relay_state.get('running', False),
                    'relay_last_checked_at': relay_state.get('last_checked_at'),
                    'relay_last_result': relay_state.get('last_result', {}),
                    'notification_stats': notification_stats,
                }
                state.update({'running': False, 'last_error': str(e), 'last_error_at': error_time, 'last_error_context': error_context})
                save_runtime_state(state)
                append_runtime_history({'type': 'error', 'time': error_time, 'message': str(e), 'context': error_context})
                notifier.notify_error('守护周期异常', str(e), {'interval': interval, 'context': error_context})
                logger.error(f'守护周期异常: {e}')
            finally:
                guard.release()
            state = load_runtime_state()
            state.update({'next_run_at': datetime.fromtimestamp(time.time() + interval).isoformat(), 'interval_seconds': interval})
            save_runtime_state(state)
            try:
                maybe_run_approval_hygiene(cfg, Database(cfg.db_path), notifier)
            except Exception as e:
                logger.error(f'执行 approval hygiene 失败: {e}')
            try:
                maybe_send_daily_health_summary(cfg, Database(cfg.db_path), notifier)
            except Exception as e:
                logger.error(f'发送每日健康汇总失败: {e}')
            time.sleep(interval)
    
    elif args.train:
        # 训练模型
        print("\n🎯 开始训练模型...\n")
        cfg = Config()
        exchange = Exchange(cfg.all)
        collector = DataCollector(exchange, cfg.all)
        trainer = ModelTrainer(cfg.all)
        results = {}

        for symbol in cfg.symbols:
            if not exchange.is_futures_symbol(symbol):
                print(f"跳过 {symbol}: 暂无U本位永续合约")
                results[symbol] = False
                continue
            print(f"收集并训练 {symbol}...")
            if collector.collect_data(symbol, '1h', 1000):
                import pandas as pd
                csv_name = symbol.replace('/', '_').replace(':', '_')
                symbol_map = {
                    'BTC/USDT': 'BTC_USDT',
                    'ETH/USDT': 'ETH_USDT',
                    'SOL/USDT': 'SOL_USDT',
                    'XRP/USDT': 'XRP_USDT',
                    'HYPE/USDT': 'HYPE_USDT'
                }
                filename = symbol_map.get(symbol, csv_name)
                df = pd.read_csv(f"ml/data/{filename}_1h.csv")
                results[symbol] = trainer.train(symbol, df)
            else:
                results[symbol] = False

        print("\n训练结果:")
        for symbol, success in results.items():
            print(f"   {symbol}: {'✅ 成功' if success else '❌ 失败'}")

    elif args.collect:
        # 收集数据
        print("\n📊 开始收集数据...\n")
        config = Config()
        exchange = Exchange(config.all)
        collector = DataCollector(exchange, config.all)

        for symbol in config.symbols:
            if not exchange.is_futures_symbol(symbol):
                print(f"跳过 {symbol}: 暂无U本位永续合约")
                continue
            print(f"收集 {symbol}...")
            collector.collect_data(symbol, '1h', 1000)

    elif args.backtest:
        print("\n🧪 开始回测...\n")
        cfg = Config()
        backtester = StrategyBacktester(cfg)
        result = backtester.run_all()
        print("回测总览:")
        print(result['summary'])
        print("\n分币种结果:")
        for row in result['symbols']:
            print(f"  {row['symbol']}: trades={row['trades']} win_rate={row['win_rate']}% return={row['total_return_pct']}% dd={row['max_drawdown_pct']}%")

    elif args.signal_quality:
        print("\n🔎 开始分析信号质量...\n")
        cfg = Config()
        db = Database(cfg.db_path)
        analyzer = SignalQualityAnalyzer(cfg, db)
        result = analyzer.analyze()
        print("信号质量总览:")
        print(result['summary'])
        print("\n分币种质量:")
        for row in result['by_symbol']:
            print(f"  {row['symbol']}: signals={row['signals']} positive_rate={row['positive_rate']}% avg_quality={row['avg_quality_pct']}%")

    elif args.optimize:
        print("\n⚙️ 开始参数优化与币种分层...\n")
        cfg = Config()
        db = Database(cfg.db_path)
        optimizer = ParameterOptimizer(cfg, db)
        result = optimizer.run(use_cache=False)
        print("最佳实验:")
        print(result['best_experiment'])
        print("\n币种分层建议:")
        for row in result['symbol_advice']:
            print(f"  {row['symbol']}: {row['tier']} | backtest={row['backtest_return_pct']}% | quality={row['avg_quality_pct']}% | {row['action']}")
        print("\n单币种专项实验:")
        for symbol, rows in result.get('symbol_specific', {}).items():
            print(f"  [{symbol}]")
            for row in rows:
                print(f"    {row['name']}: score={row['score']} return={row['summary']['total_return_pct']}% win={row['summary']['win_rate']}% dd={row['summary']['max_drawdown_pct']}%")
        print("\n候选晋升判断:")
        for row in result.get('candidate_promotions', []):
            print(f"  {row['symbol']}: {row['decision']} | {row['reason']}")
        print("\n预设配置:")
        for preset in result.get('presets', []):
            print(f"  {preset['name']}: {preset['path']}")

    elif args.list_presets:
        pm = PresetManager(Config())
        print("\n📦 可用预设:\n")
        for row in pm.list_presets():
            print(f"  {row['name']}: watch={row['watch_list']} candidate={row['candidate_watch_list']} paused={row['paused_watch_list']}")

    elif args.apply_preset:
        pm = PresetManager(Config())
        result = pm.apply_preset(args.apply_preset, auto_restart=True)
        print("\n✅ 已应用预设:\n")
        print(result)

    elif args.mode_status:
        pm = PresetManager(Config())
        print("\n🧭 当前模式:\n")
        print(pm.status())

    elif args.daily_summary:
        cfg = Config()
        db = Database(cfg.db_path)
        gov = GovernanceEngine(cfg, db)
        print("\n📰 今日日报:\n")
        print(gov.generate_daily_summary())

    elif args.health_summary:
        cfg = Config()
        db = Database(cfg.db_path)
        notifier = NotificationManager(cfg, db, logger)
        result = maybe_send_daily_health_summary(cfg, db, notifier, force=True)
        print("\n🩺 健康汇总:\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.approval_hygiene:
        cfg = Config()
        db = Database(cfg.db_path)
        notifier = NotificationManager(cfg, db, logger)
        result = maybe_run_approval_hygiene(cfg, db, notifier, force=True)
        print("\n🧼 审批卫生结果:\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.cleanup_runtime_records:
        cfg = Config()
        db = Database(cfg.db_path)
        print("\n🧹 清理运行期重复记录:\n")
        print(db.cleanup_duplicate_runtime_records(dry_run=args.dry_run))

    elif args.notify_test:
        cfg = Config()
        notifier = NotificationManager(cfg, Database(cfg.db_path), logger)
        result = notifier.test_discord()
        print("\n🔔 通知链路测试:\n")
        print(result['message'])
        print(f"delivered: {result['delivered']} | enabled: {result['enabled']}")

    elif args.relay_outbox:
        cfg = Config()
        interval = args.interval_seconds or int(cfg.get('runtime.notification_relay_interval_seconds', 30))
        run_notification_relay(interval=interval, once=args.once, limit=args.relay_limit)

    elif args.reconcile_positions:
        cfg = Config()
        exchange = Exchange(cfg.all)
        db = Database(cfg.db_path)
        report = reconcile_exchange_positions(exchange, db)
        print("\n🧭 持仓对账结果:\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("\n📌 对账摘要:\n")
        print(json.dumps(report.get('summary', {}), ensure_ascii=False, indent=2))

    elif args.exchange_diagnose:
        cfg = Config()
        exchange = Exchange(cfg.all)
        report = build_exchange_diagnostics(cfg, exchange)
        print("\n🩺 交易所只读诊断:\n")
        print(f"模式: {report['exchange_mode']} | 持仓模式: {report['position_mode']} | 可用USDT: {report.get('available_usdt', 0)}")
        if report.get('balance_error'):
            print(f"余额读取异常: {report['balance_error']}")
        for row in report['symbols']:
            if args.symbol and row['symbol'] != args.symbol:
                continue
            print(f"\n[{row['symbol']}]")
            if row.get('error'):
                print(f"  错误: {row['error']}")
                continue
            print(f"  futures: {'yes' if row.get('is_futures_symbol') else 'no'}")
            if row.get('order_symbol'):
                print(f"  order_symbol: {row['order_symbol']}")
            if row.get('last_price') is not None:
                print(f"  last_price: {row['last_price']}")
            if row.get('sample_amount') is not None:
                print(f"  sample_amount: {row['sample_amount']}")
            if row.get('order_params_preview'):
                print(f"  order_params_preview: {row['order_params_preview']}")
            if row.get('reason'):
                print(f"  reason: {row['reason']}")

    elif args.exchange_smoke:
        cfg = Config()
        exchange = Exchange(cfg.all)
        plan = build_exchange_smoke_plan(cfg, exchange, symbol=args.symbol, side=args.side)
        print("\n🧪 Testnet 最小验收计划:\n")
        if plan.get('error'):
            print(f"错误: {plan['error']}")
        else:
            print(f"模式: {plan['exchange_mode']} | 持仓模式: {plan['position_mode']} | 目标: {plan['symbol']} | 方向: {plan['side']}")
            print(f"可用USDT: {plan.get('available_usdt', 0)} | 最新价: {plan.get('last_price')} | 验收名义价值: {plan.get('smoke_notional')}")
            print(f"样例数量: {plan.get('sample_amount')} | 可执行: {'yes' if plan.get('execute_ready') else 'no'}")
            print('步骤:')
            for step in plan.get('steps', []):
                print(f"  - {step}")
            if plan.get('open_preview'):
                print(f"开仓预览: {plan['open_preview']}")
            if plan.get('close_preview'):
                print(f"平仓预览: {plan['close_preview']}")
        if args.execute:
            print("\n🚨 已显式允许执行 testnet 最小开平仓验收\n")
            db = Database(cfg.db_path)
            result = execute_exchange_smoke(cfg, exchange, symbol=args.symbol, side=args.side, db=db)
            if result.get('error'):
                print(f"执行结果: 失败 | {result['error']}")
            else:
                print(f"执行结果: 开仓 {'成功' if result.get('opened') else '失败'} / 平仓 {'成功' if result.get('closed') else '失败'}")
                if result.get('open_order'):
                    print(f"open_order: {result['open_order']}")
                if result.get('close_order'):
                    print(f"close_order: {result['close_order']}")
            if result.get('smoke_run_id'):
                print(f"smoke_run_id: {result['smoke_run_id']}")

    elif args.validation_entry:
        if not args.case or len(args.case) != 1:
            raise SystemExit('--validation-entry 需要配合单个 --case <file>')
        report = run_shadow_validation_case(args.case[0], base_config=Config())
        if args.validation_output:
            output_path = Path(args.validation_output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.suffix.lower() == '.md':
                output_path.write_text(format_validation_report_markdown(report), encoding='utf-8')
            else:
                output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print("\n🕶️ Shadow Validation Report:\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))

    elif args.validation_replay:
        if not args.case:
            raise SystemExit('--validation-replay 需要配合一个或多个 --case <file|dir>')
        report = run_shadow_validation_replay(args.case, base_config=Config())
        if args.validation_output:
            output_path = Path(args.validation_output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.suffix.lower() == '.md':
                output_path.write_text(format_validation_report_markdown(report), encoding='utf-8')
            else:
                output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print("\n🧪 Shadow Validation Replay Summary:\n")
        print(json.dumps(report['summary'], ensure_ascii=False, indent=2))

    else:
        # 运行交易
        bot = TradingBot()
        bot.run()


if __name__ == '__main__':
    main()

