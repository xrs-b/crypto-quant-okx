"""
交易执行模块 - 增强版
"""
import json
import time
from math import isclose
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from core.config import Config, DEFAULT_LAYERING_CONFIG
from core.exchange import Exchange
from core.database import Database
from core.logger import trade_logger
from analytics.recommendation import get_recommendation_provider
from analytics.helper import build_close_outcome_feedback_loop, build_close_outcome_scope_windows
from core.risk_budget import get_risk_budget_config, summarize_margin_usage, compute_entry_plan, summarize_risk_hint_changes
from core.regime_policy import build_observe_only_payload, build_risk_effective_snapshot, build_execution_effective_snapshot


def build_observability_context(*, symbol: str = None, side: str = None, signal_id: int = None, root_signal_id: int = None, layer_no: int = None, deny_reason: str = None, current_symbol_exposure: float = None, projected_symbol_exposure: float = None, current_total_exposure: float = None, projected_total_exposure: float = None, extra: Dict[str, Any] = None) -> Dict[str, Any]:
    context = {
        'symbol': symbol,
        'side': side,
        'signal_id': signal_id,
        'root_signal_id': root_signal_id,
        'layer_no': layer_no,
        'deny_reason': deny_reason,
        'current_symbol_exposure': round(float(current_symbol_exposure or 0.0), 6),
        'projected_symbol_exposure': round(float(projected_symbol_exposure if projected_symbol_exposure is not None else current_symbol_exposure or 0.0), 6),
        'current_total_exposure': round(float(current_total_exposure or 0.0), 6),
        'projected_total_exposure': round(float(projected_total_exposure if projected_total_exposure is not None else current_total_exposure or 0.0), 6),
    }
    if extra:
        context.update(extra)
    return context


def merge_observability_details(details: Dict[str, Any] = None, context: Dict[str, Any] = None) -> Dict[str, Any]:
    merged = dict(details or {})
    merged['observability'] = dict(context or {})
    return merged


def observability_log_text(context: Dict[str, Any]) -> str:
    if not context:
        return '{}'
    return json.dumps(context, ensure_ascii=False, sort_keys=True)


def validate_live_execution_guard_contract(plan_context: Dict[str, Any] = None, *, exchange_mode: str = None) -> Dict[str, Any]:
    payload = dict(plan_context or {})
    permit = dict(payload.get('final_execution_permit') or {})
    contract = dict(payload.get('live_execution_guard') or {})
    normalized_mode = str(exchange_mode or permit.get('exchange_mode') or payload.get('exchange_mode') or 'unknown').strip().lower()
    enforcement_required = bool(permit) and normalized_mode in {'testnet', 'live', 'real'}
    result = {
        'required': enforcement_required,
        'passed': True,
        'fail_closed': True,
        'exchange_mode': normalized_mode,
        'reason': None,
        'reason_code': None,
        'contract': contract,
    }
    if not enforcement_required:
        result['fail_closed'] = False
        result['reason'] = 'not_required'
        return result
    if not contract:
        result.update({'passed': False, 'reason': 'missing_live_execution_guard_contract', 'reason_code': 'DENY_LIVE_GUARD_CONTRACT_MISSING'})
        return result
    required_fields = ['schema_version', 'symbol', 'side', 'signal_id', 'exchange_mode', 'final_execution_permit_reason_code', 'final_execution_allowed', 'guard_passed']
    missing = [key for key in required_fields if contract.get(key) in (None, '')]
    if missing:
        result.update({'passed': False, 'reason': f"missing_required_fields:{','.join(missing)}", 'reason_code': 'DENY_LIVE_GUARD_CONTRACT_INVALID'})
        return result
    if str(contract.get('exchange_mode') or '').strip().lower() != normalized_mode:
        result.update({'passed': False, 'reason': 'exchange_mode_mismatch', 'reason_code': 'DENY_LIVE_GUARD_CONTRACT_INVALID'})
        return result
    if str(contract.get('schema_version') or '').strip() != 'live_execution_guard_v1':
        result.update({'passed': False, 'reason': 'schema_version_mismatch', 'reason_code': 'DENY_LIVE_GUARD_CONTRACT_INVALID'})
        return result
    if permit:
        if str(contract.get('final_execution_permit_reason_code') or '') != str(permit.get('reason_code') or ''):
            result.update({'passed': False, 'reason': 'permit_reason_code_mismatch', 'reason_code': 'DENY_LIVE_GUARD_CONTRACT_INVALID'})
            return result
        if bool(contract.get('final_execution_allowed')) != bool(permit.get('allowed', False)):
            result.update({'passed': False, 'reason': 'permit_allowed_mismatch', 'reason_code': 'DENY_LIVE_GUARD_CONTRACT_INVALID'})
            return result
        if not bool(contract.get('guard_passed')) or not bool(permit.get('allowed', False)):
            result.update({'passed': False, 'reason': contract.get('guard_reason') or 'final_execution_permit_denied', 'reason_code': str(contract.get('guard_reason_code') or permit.get('reason_code') or 'DENY_LIVE_GUARD_CONTRACT_INVALID')})
            return result
    return result


def enrich_observability_with_snapshots(config_helper: Any, symbol: Optional[str], context: Dict[str, Any] = None, *, signal: Any = None, plan_context: Dict[str, Any] = None, regime_snapshot: Optional[Dict[str, Any]] = None, adaptive_policy_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    enriched = dict(context or {})
    source_plan = dict(plan_context or {})
    payload = build_observe_only_payload(
        config_helper,
        symbol,
        signal=signal,
        regime_snapshot=regime_snapshot or source_plan.get('regime_snapshot'),
        policy_snapshot=adaptive_policy_snapshot or source_plan.get('adaptive_policy_snapshot'),
    )
    enriched.update(payload)
    risk_snapshot = build_risk_effective_snapshot(
        config_helper,
        symbol,
        signal=signal,
        regime_snapshot=payload.get('regime_snapshot'),
        policy_snapshot=payload.get('adaptive_policy_snapshot'),
    )
    risk_hint_summary = summarize_risk_hint_changes(risk_snapshot.get('baseline'), risk_snapshot.get('effective'))
    execution_snapshot = build_execution_effective_snapshot(
        config_helper,
        symbol,
        signal=signal,
        regime_snapshot=payload.get('regime_snapshot'),
        policy_snapshot=payload.get('adaptive_policy_snapshot'),
    )
    enriched['adaptive_risk_snapshot'] = risk_snapshot
    enriched['adaptive_risk_hints'] = {
        'enabled': bool(risk_snapshot.get('enabled')),
        'effective_state': risk_snapshot.get('effective_state', 'disabled'),
        'observe_only': bool(risk_snapshot.get('observe_only', True)),
        'baseline': dict(risk_snapshot.get('baseline') or {}),
        'effective': dict(risk_snapshot.get('effective') or {}),
        'effective_candidate': dict(risk_snapshot.get('effective_candidate') or risk_snapshot.get('effective') or {}),
        'applied': list((risk_snapshot.get('applied_overrides') or {}).keys()),
        'ignored': list(risk_snapshot.get('ignored_overrides') or []),
        'would_tighten': bool(risk_snapshot.get('would_tighten', risk_hint_summary.get('would_tighten'))),
        'would_tighten_fields': list(risk_snapshot.get('would_tighten_fields') or risk_hint_summary.get('would_tighten_fields') or []),
        'enforced_fields': list(risk_snapshot.get('enforced_fields') or []),
        'rollout_match': bool(risk_snapshot.get('rollout_match', True)),
        'hint_codes': list(risk_snapshot.get('hint_codes') or risk_hint_summary.get('hint_codes') or []),
        'field_decisions': list(risk_snapshot.get('field_decisions') or []),
    }
    enriched['adaptive_execution_snapshot'] = execution_snapshot
    effective_profile = dict(execution_snapshot.get('effective') or {})
    live_profile = dict(execution_snapshot.get('live') or execution_snapshot.get('enforced_profile') or execution_snapshot.get('baseline') or {})
    enforced_profile = dict(execution_snapshot.get('enforced_profile') or execution_snapshot.get('baseline') or {})
    ignored_rows = list(execution_snapshot.get('ignored_overrides') or [])
    enriched['adaptive_execution_hints'] = {
        'enabled': bool(execution_snapshot.get('enabled')),
        'effective_state': execution_snapshot.get('effective_state', 'disabled'),
        'baseline': dict(execution_snapshot.get('baseline') or {}),
        'effective': effective_profile,
        'effective_hint': effective_profile,
        'live': live_profile,
        'enforced': enforced_profile,
        'enforced_profile': enforced_profile,
        'applied': list((execution_snapshot.get('applied_overrides') or {}).keys()),
        'ignored': ignored_rows,
        'ignored_fields': [str(item.get('key')) for item in ignored_rows if item.get('key')],
        'enforced_fields': list(execution_snapshot.get('enforced_fields') or []),
        'hinted_only_fields': list(execution_snapshot.get('hinted_only_fields') or []),
        'layering_enforced_fields': list(execution_snapshot.get('layering_enforced_fields') or []),
        'execution_profile_really_enforced': bool(execution_snapshot.get('execution_profile_really_enforced', False)),
        'layering_profile_really_enforced': bool(execution_snapshot.get('layering_profile_really_enforced', False)),
        'plan_shape_really_enforced': bool(execution_snapshot.get('plan_shape_really_enforced', False)),
        'plan_shape_enforced_fields': list(execution_snapshot.get('plan_shape_enforced_fields') or []),
        'plan_shape_ignored_fields': list(execution_snapshot.get('plan_shape_ignored_fields') or []),
        'live_layer_shape_source': execution_snapshot.get('live_layer_shape_source') or 'baseline',
        'shape_guardrail_decisions': list(execution_snapshot.get('shape_guardrail_decisions') or []),
        'plan_shape_validation': dict(execution_snapshot.get('plan_shape_validation') or {}),
        'shape_live_rollout_match': bool(execution_snapshot.get('shape_live_rollout_match', False)),
        'shape_rollout_symbol_match': bool(execution_snapshot.get('shape_rollout_symbol_match', False)),
        'shape_rollout_fraction_match': bool(execution_snapshot.get('shape_rollout_fraction_match', False)),
        'rollout_match': bool(execution_snapshot.get('rollout_match', True)),
        'would_change_execution_profile': bool(execution_snapshot.get('would_tighten')),
        'would_tighten_fields': list(execution_snapshot.get('would_tighten_fields') or []),
        'hint_codes': list(execution_snapshot.get('hint_codes') or []),
        'ignored_reasons': list({str(item.get('reason')) for item in ignored_rows if item.get('reason')}),
        'notes': ['step3 live scope only enforces conservative layering guardrails when rollout/enforcement is enabled', 'layer_ratios remains hints-only unless layering_plan_shape_enforcement_enabled=true'],
        'field_decisions': list(execution_snapshot.get('field_decisions') or []),
    }
    return enriched


class TradingExecutor:
    """交易执行器 - 增强版"""
    
    def __init__(self, config: Config, exchange: Exchange, db: Database):
        self.config = config
        self.exchange = exchange
        self.db = db
        self.trading_config = config.get('trading', {})
        self.layering_config = config.get_layering_config() if hasattr(config, 'get_layering_config') else dict(DEFAULT_LAYERING_CONFIG)
        self._trade_cache = {}  # 交易缓存
        self._last_close_result = {}
        # MFE/MAE 建议提供者
        self._recommendation_provider = get_recommendation_provider(db, config)

    def _exchange_has_position(self, symbol: str, side: str) -> bool:
        try:
            positions = self.exchange.fetch_positions()
        except Exception:
            return True
        for pos in positions or []:
            pos_symbol = pos.get('symbol') or pos.get('info', {}).get('instId') or ''
            if pos_symbol and ':' in pos_symbol:
                pos_symbol = pos_symbol.split(':')[0]
            pos_side = str(pos.get('side') or pos.get('info', {}).get('posSide') or '').lower()
            if pos_side in {'buy', 'long'}:
                pos_side = 'long'
            elif pos_side in {'sell', 'short'}:
                pos_side = 'short'
            contracts = float(pos.get('contracts', 0) or 0)
            if pos_symbol == symbol and pos_side == side and contracts > 0:
                return True
        return False

    def _store_close_result(self, symbol: str, side: str, result: Dict[str, Any]) -> None:
        key = (symbol, side)
        payload = dict(result or {})
        payload.setdefault('symbol', symbol)
        payload.setdefault('side', side)
        self._last_close_result[key] = payload

    def get_last_close_result(self, symbol: str, side: str) -> Optional[Dict[str, Any]]:
        key = (symbol, side)
        result = self._last_close_result.get(key)
        return dict(result) if isinstance(result, dict) else None

    def _close_local_position_as_stale(self, symbol: str, side: str, close_price: float, reason: str) -> bool:
        trade = self.db.get_latest_open_trade(symbol, side)
        trade_id = trade.get('id') if trade else None
        if trade_id:
            self.db.mark_trade_stale_closed(trade_id, reason, close_price=close_price)
        self.db.close_position(symbol)
        self.db.sync_layer_plan_state(symbol, side, reset_if_flat=True)
        self.db.cleanup_orphan_execution_state(stale_after_minutes=1)
        self._store_close_result(symbol, side, {
            'exit_price': close_price,
            'pnl': None,
            'reason': reason,
            'trade_id': trade_id,
            'close_source': 'stale_local_close',
        })
        trade_logger.warning(f"{symbol}: 检测到交易所已无对应仓位，自动收口本地持仓/交易")
        return True
    
    def _recover_open_trade_from_exchange(self, symbol: str, side: str, signal_id: int = None, note: str = None) -> Optional[int]:
        try:
            positions = self.exchange.fetch_positions() or []
        except Exception:
            return None
        for pos in positions:
            normalized = pos if pos.get('contract_size') is not None else (self.exchange.normalize_position(pos) if hasattr(self.exchange, 'normalize_position') else pos)
            if not normalized:
                continue
            pos_symbol = normalized.get('symbol')
            pos_side = str(normalized.get('side') or '').lower()
            if pos_symbol != symbol or pos_side != side:
                continue
            quantity = float(normalized.get('quantity') or normalized.get('contracts') or 0)
            if quantity <= 0:
                continue
            contract_size = float(normalized.get('contract_size') or 1)
            coin_quantity = float(normalized.get('coin_quantity') or quantity * contract_size)
            leverage = int(float(normalized.get('leverage') or self.trading_config.get('leverage', 10) or 10))
            entry_price = float(normalized.get('entry_price') or normalized.get('current_price') or 0)
            trade_id = self.db.record_trade(
                symbol=symbol, side=side, entry_price=entry_price, quantity=quantity, leverage=leverage,
                signal_id=signal_id, notes=note or '交易所持仓恢复 open trade', contract_size=contract_size, coin_quantity=coin_quantity
            )
            self.db.update_position(symbol=symbol, side=side, entry_price=entry_price, quantity=quantity, leverage=leverage, current_price=float(normalized.get('current_price') or entry_price), contract_size=contract_size, coin_quantity=coin_quantity)
            self.db.sync_layer_plan_state(symbol, side, root_signal_id=signal_id, reset_if_flat=False)
            return trade_id
        return None

    def _build_latest_risk_snapshot(self, symbol: str, side: str, current_price: float = None, available_hint: float = None) -> Dict[str, Any]:
        positions = []
        source = 'database'
        try:
            exchange_positions = self.exchange.fetch_positions()
            if exchange_positions:
                positions = exchange_positions
                source = 'exchange'
        except Exception as e:
            trade_logger.warning(f"{symbol}: 拉取交易所持仓失败，回退本地 DB 快照 - {e}")
        if not positions:
            positions = self.db.get_positions()

        try:
            balance = self.exchange.fetch_balance()
            total_balance = float((balance.get('total') or {}).get('USDT', 0) or 0)
            free_balance = float((balance.get('free') or {}).get('USDT', 0) or 0)
        except Exception as e:
            trade_logger.warning(f"{symbol}: 拉取最新余额失败，回退本地估算 - {e}")
            total_balance = 0.0
            free_balance = float(available_hint or 0)

        if total_balance <= 0 and free_balance > 0:
            total_balance = free_balance

        intents = self.db.get_active_open_intents() if self.db else []
        usage = summarize_margin_usage(positions or [], symbol, mark_price=current_price, pending_intents=intents)
        normalized_positions = usage['positions']
        same_side_positions = [
            row for row in normalized_positions
            if row.get('symbol') == symbol and row.get('side') == side and row.get('margin_used', 0) > 0
        ]
        current_total_margin = float(usage['current_total_margin'])
        current_symbol_margin = float(usage['current_symbol_margin'])
        total_exposure_ratio = current_total_margin / total_balance if total_balance > 0 else 0.0
        symbol_exposure_ratio = current_symbol_margin / total_balance if total_balance > 0 else 0.0

        return {
            'source': source,
            'positions': normalized_positions,
            'balance_total': total_balance,
            'balance_free': free_balance,
            'current_total_margin': current_total_margin,
            'current_symbol_margin': current_symbol_margin,
            'current_total_exposure_ratio': total_exposure_ratio,
            'current_symbol_exposure_ratio': symbol_exposure_ratio,
            'same_side_positions': same_side_positions,
        }

    def _execution_time_risk_guard(self, symbol: str, side: str, current_price: float, available_hint: float = None) -> tuple:
        snapshot = self._build_latest_risk_snapshot(symbol, side, current_price=current_price, available_hint=available_hint)
        risk_budget = get_risk_budget_config(self.config, symbol)
        add_position_enabled = bool(risk_budget.get('add_position_enabled', False))
        if not add_position_enabled and (self.db.get_latest_open_trade(symbol, normalized_side) or any(p.get('symbol') == symbol and str(p.get('side')).lower() == normalized_side for p in self.db.get_positions())):
            details['hard_intercept'] = {'passed': False, 'reason': '已有同币种同方向仓位，默认禁止重复开仓', 'add_position_enabled': add_position_enabled}
            return False, '已有同币种同方向仓位，默认禁止重复开仓', details
        total_balance = float(snapshot.get('balance_total') or 0)
        free_balance = float(snapshot.get('balance_free') or 0)
        entry_plan = compute_entry_plan(
            total_balance=total_balance,
            free_balance=free_balance,
            current_total_margin=float(snapshot.get('current_total_margin') or 0.0),
            current_symbol_margin=float(snapshot.get('current_symbol_margin') or 0.0),
            risk_budget=risk_budget,
        )
        planned_margin = float(entry_plan.get('allowed_margin') or 0.0)
        current_total_ratio = float(entry_plan.get('current_total_exposure_ratio') or 0)
        current_symbol_ratio = float(entry_plan.get('current_symbol_exposure_ratio') or 0)
        projected_total_ratio = float(entry_plan.get('projected_total_exposure_ratio') or current_total_ratio)
        projected_symbol_ratio = float(entry_plan.get('projected_symbol_exposure_ratio') or current_symbol_ratio)
        max_exposure = float(risk_budget.get('total_margin_cap_ratio', 0.3) or 0.3)
        max_symbol_exposure = float(risk_budget.get('symbol_margin_cap_ratio', 0.15) or 0.15)
        add_position_enabled = bool(risk_budget.get('add_position_enabled', False))
        duplicate_same_side = bool(snapshot.get('same_side_positions'))

        details = {
            'source': snapshot.get('source'),
            'balance_total': round(total_balance, 4),
            'balance_free': round(free_balance, 4),
            'planned_margin': round(planned_margin, 4),
            'current_total_exposure': round(current_total_ratio, 4),
            'projected_total_exposure': round(projected_total_ratio, 4),
            'max_total_exposure': max_exposure,
            'current_symbol_exposure': round(current_symbol_ratio, 4),
            'projected_symbol_exposure': round(projected_symbol_ratio, 4),
            'max_symbol_exposure': max_symbol_exposure,
            'duplicate_same_side': duplicate_same_side,
            'same_side_count': len(snapshot.get('same_side_positions') or []),
            'add_position_enabled': add_position_enabled,
            'entry_plan': entry_plan,
            'pending_intents': pending_intents,
            'layer_plan': layer_plan,
            'guard_stage': 'execution_time',
        }

        if duplicate_same_side and not add_position_enabled:
            reason = 'execution-time guard: 最新持仓已存在同币种同方向仓位，阻止重复放大'
            details['reason'] = reason
            return False, reason, details

        if total_balance <= 0:
            reason = 'execution-time guard: 最新总余额不可用，拒绝盲目开仓'
            details['reason'] = reason
            return False, reason, details

        if entry_plan.get('blocked') or planned_margin <= 0:
            reason = f"execution-time guard: {entry_plan.get('block_reason') or '最新可用余额不足，无法生成有效保证金'}"
            details['reason'] = reason
            return False, reason, details

        if projected_total_ratio > max_exposure and not isclose(projected_total_ratio, max_exposure):
            reason = 'execution-time guard: 基于最新占用刷新后，总仓上限将被突破'
            details['reason'] = reason
            return False, reason, details

        if projected_symbol_ratio > max_symbol_exposure and not isclose(projected_symbol_ratio, max_symbol_exposure):
            reason = 'execution-time guard: 基于最新占用刷新后，单币种上限将被突破'
            details['reason'] = reason
            return False, reason, details

        return True, None, details

    def _build_lock_owner(self, symbol: str, side: str, signal_id: int = None) -> str:
        suffix = signal_id if signal_id is not None else int(time.time() * 1000)
        return f"executor:{symbol}:{side}:{suffix}"


    def _get_layering_config(self, symbol: str = None) -> Dict[str, Any]:
        if hasattr(self.config, 'get_layering_config'):
            return self.config.get_layering_config(symbol)
        return dict(DEFAULT_LAYERING_CONFIG)

    def _get_live_execution_profile(self, symbol: str, plan_context: Dict[str, Any] = None) -> Dict[str, Any]:
        baseline = self._get_layering_config(symbol)
        execution_snapshot = build_execution_effective_snapshot(
            self.config,
            symbol,
            regime_snapshot=(plan_context or {}).get('regime_snapshot'),
            policy_snapshot=(plan_context or {}).get('adaptive_policy_snapshot'),
        )
        enforced_profile = dict(execution_snapshot.get('enforced_profile') or baseline)
        return {
            'baseline': baseline,
            'effective': dict(execution_snapshot.get('effective') or baseline),
            'live': dict(execution_snapshot.get('live') or enforced_profile),
            'snapshot': execution_snapshot,
        }

    def _get_direction_lock_scope_key(self, symbol: str, side: str) -> tuple:
        layering = self._get_layering_config(symbol)
        if layering.get('direction_lock_enabled', True) and layering.get('direction_lock_scope') == 'symbol':
            return symbol, '*'
        return symbol, side

    def _get_signal_bar_marker(self, signal_id: int = None, plan_context: Dict[str, Any] = None) -> Optional[str]:
        if isinstance(plan_context, dict):
            for key in ('signal_bar_marker', 'bar_marker', 'candle_key', 'bar_time', 'signal_time'):
                value = plan_context.get(key)
                if value not in (None, ''):
                    return str(value)
        return str(signal_id) if signal_id is not None else None

    def _check_layering_runtime_guards(self, symbol: str, side: str, signal_id: int = None, plan_context: Dict[str, Any] = None) -> tuple:
        execution_profile = self._get_live_execution_profile(symbol, plan_context)
        layering = execution_profile['live']
        state = self.db.get_layer_plan_state(symbol, side)
        plan_data = dict(state.get('plan_data') or {})
        filled_layers = sorted(int(x) for x in (plan_data.get('filled_layers') or []))
        last_filled_at = plan_data.get('last_filled_at')

        def _guard_details(stage: str, context: Dict[str, Any], extra: Dict[str, Any] = None) -> Dict[str, Any]:
            payload = {
                'stage': stage,
                'layering': layering,
                'baseline_layering': execution_profile['baseline'],
                'effective_layering': execution_profile['effective'],
                'live_layering': execution_profile['live'],
                'execution_profile_really_enforced': bool((execution_profile.get('snapshot') or {}).get('execution_profile_really_enforced')),
                'layering_profile_really_enforced': bool((execution_profile.get('snapshot') or {}).get('layering_profile_really_enforced')),
                'plan_shape_really_enforced': bool((execution_profile.get('snapshot') or {}).get('plan_shape_really_enforced')),
                'applied': list((((execution_profile.get('snapshot') or {}).get('applied_overrides') or {}).keys())),
                'ignored': list(((execution_profile.get('snapshot') or {}).get('ignored_overrides') or [])),
                'enforced_fields': list(((execution_profile.get('snapshot') or {}).get('enforced_fields') or [])),
                'field_decisions': list(((execution_profile.get('snapshot') or {}).get('field_decisions') or [])),
            }
            if extra:
                payload.update(extra)
            return merge_observability_details(payload, context)

        if layering.get('signal_idempotency_enabled', True) and signal_id is not None:
            existing_trade = self.db.get_trade_by_signal_id(signal_id)
            if existing_trade:
                return False, 'signal_id 已存在成交记录，跳过重复开仓', _guard_details('signal_idempotency', build_observability_context(symbol=symbol, side=side, signal_id=signal_id, root_signal_id=state.get('root_signal_id') or signal_id, layer_no=(max(filled_layers) + 1) if filled_layers else 1, deny_reason='signal_idempotency'), {'trade_id': existing_trade.get('id')})
            existing_intent = self.db.get_open_intent_by_signal_id(signal_id)
            if existing_intent:
                return False, 'signal_id 已存在进行中的开仓 intent', _guard_details('signal_idempotency', build_observability_context(symbol=symbol, side=side, signal_id=signal_id, root_signal_id=existing_intent.get('root_signal_id') or state.get('root_signal_id') or signal_id, layer_no=existing_intent.get('layer_no') or ((max(filled_layers) + 1) if filled_layers else 1), deny_reason='signal_idempotency'), {'intent_id': existing_intent.get('id')})

        if layering.get('profit_only_add') and filled_layers:
            latest_trade = self.db.get_latest_open_trade(symbol, side)
            current_price = float((plan_context or {}).get('current_price') or 0)
            entry_price = float((latest_trade or {}).get('entry_price') or 0)
            if entry_price > 0 and current_price > 0:
                profitable = current_price >= entry_price if side == 'long' else current_price <= entry_price
                if not profitable:
                    return False, 'profit_only_add 已开启，当前未处于浮盈，禁止加仓', _guard_details('profit_only_add', build_observability_context(symbol=symbol, side=side, signal_id=signal_id, root_signal_id=state.get('root_signal_id') or signal_id, layer_no=(max(filled_layers) + 1) if filled_layers else 1, deny_reason='profit_only_add'))

        if filled_layers and layering.get('min_add_interval_seconds', 0) > 0 and last_filled_at:
            try:
                last_dt = datetime.fromisoformat(str(last_filled_at))
            except Exception:
                last_dt = None
            if last_dt is not None:
                elapsed = (datetime.utcnow() - last_dt).total_seconds()
                if elapsed < layering['min_add_interval_seconds']:
                    return False, '未达到最小加仓时间间隔', _guard_details('min_add_interval_seconds', build_observability_context(symbol=symbol, side=side, signal_id=signal_id, root_signal_id=state.get('root_signal_id') or signal_id, layer_no=(max(filled_layers) + 1) if filled_layers else 1, deny_reason='min_add_interval_seconds'), {'remaining_seconds': int(layering['min_add_interval_seconds'] - elapsed)})

        if signal_id is not None:
            signal_counts = dict(plan_data.get('signal_layer_counts') or {})
            count_for_signal = int(signal_counts.get(str(signal_id), 0) or 0)
            if count_for_signal >= int(layering.get('max_layers_per_signal') or layering.get('layer_count') or 1):
                return False, '单个 signal 已达到最大允许分仓层数', _guard_details('max_layers_per_signal', build_observability_context(symbol=symbol, side=side, signal_id=signal_id, root_signal_id=state.get('root_signal_id') or signal_id, layer_no=(max(filled_layers) + 1) if filled_layers else 1, deny_reason='max_layers_per_signal'), {'count_for_signal': count_for_signal})
            if not layering.get('allow_same_bar_multiple_adds', False):
                marker = self._get_signal_bar_marker(signal_id=signal_id, plan_context=plan_context)
                markers = dict(plan_data.get('signal_bar_markers') or {})
                if marker and markers.get(marker):
                    return False, '同一 bar 已执行过加仓，禁止重复加仓', _guard_details('allow_same_bar_multiple_adds', build_observability_context(symbol=symbol, side=side, signal_id=signal_id, root_signal_id=state.get('root_signal_id') or signal_id, layer_no=(max(filled_layers) + 1) if filled_layers else 1, deny_reason='allow_same_bar_multiple_adds'), {'signal_bar_marker': marker})

        return True, None, _guard_details('layering_guard', build_observability_context(symbol=symbol, side=side, signal_id=signal_id, root_signal_id=state.get('root_signal_id') or signal_id, layer_no=(max(filled_layers) + 1) if filled_layers else 1))


    def _get_layer_plan(self, symbol: str, side: str, signal_id: int = None, root_signal_id: int = None, plan_context: Dict[str, Any] = None) -> Dict[str, Any]:
        execution_profile = self._get_live_execution_profile(symbol, plan_context)
        layering = execution_profile['live']
        state = self.db.get_layer_plan_state(symbol, side)
        plan_data = dict(state.get('plan_data') or {})
        layer_ratios = plan_data.get('layer_ratios') or layering.get('layer_ratios') or [0.06, 0.06, 0.04]
        layer_ratios = [float(x) for x in layer_ratios]
        layer_count = len(layer_ratios)
        filled_layers = sorted(int(x) for x in (plan_data.get('filled_layers') or []))
        pending_layers = sorted(int(x) for x in (plan_data.get('pending_layers') or []))
        consumed = set(filled_layers) | set(pending_layers)
        next_layer = 1
        while next_layer in consumed:
            next_layer += 1
        if next_layer > layer_count:
            return {'eligible': False, 'reason': '分仓计划已达最大层数', 'state': state, 'plan_data': plan_data}
        expected = len(consumed) + 1
        if layering.get('disallow_skip_layers', True) and next_layer != expected:
            return {'eligible': False, 'reason': '检测到分仓层级断档，禁止跳层', 'state': state, 'plan_data': plan_data}
        return {
            'eligible': True,
            'layer_no': next_layer,
            'layer_ratio': float(layer_ratios[next_layer - 1]),
            'root_signal_id': root_signal_id or state.get('root_signal_id') or signal_id,
            'state': state,
            'plan_data': plan_data,
            'layer_count': layer_count,
            'layer_ratios': layer_ratios,
            'max_total_ratio': float(plan_data.get('max_total_ratio') or layering.get('layer_max_total_ratio') or sum(layer_ratios) or 0.16),
            'baseline_layering': execution_profile['baseline'],
            'effective_layering': execution_profile['effective'],
            'live_layering': execution_profile['live'],
            'execution_profile_really_enforced': bool((execution_profile.get('snapshot') or {}).get('execution_profile_really_enforced')),
        }

    def _reserve_layer(self, symbol: str, side: str, layer_plan: Dict[str, Any]):
        state = layer_plan.get('state') or self.db.get_layer_plan_state(symbol, side)
        plan_data = dict(state.get('plan_data') or {})
        pending_layers = sorted(set(int(x) for x in (plan_data.get('pending_layers') or [])))
        layer_no = int(layer_plan.get('layer_no') or 0)
        if layer_no and layer_no not in pending_layers:
            pending_layers.append(layer_no)
        plan_data['pending_layers'] = sorted(pending_layers)
        plan_data.setdefault('filled_layers', plan_data.get('filled_layers') or [])
        plan_data.setdefault('signal_layer_counts', plan_data.get('signal_layer_counts') or {})
        plan_data.setdefault('signal_bar_markers', plan_data.get('signal_bar_markers') or {})
        plan_data['layer_ratios'] = layer_plan.get('layer_ratios') or plan_data.get('layer_ratios') or [0.06, 0.06, 0.04]
        plan_data['max_total_ratio'] = float(layer_plan.get('max_total_ratio') or plan_data.get('max_total_ratio') or 0.16)
        self.db.save_layer_plan_state(symbol, side, status='pending', current_layer=max(plan_data.get('filled_layers') or [0]), root_signal_id=layer_plan.get('root_signal_id'), plan_data=plan_data)

    def _finalize_layer(self, symbol: str, side: str, layer_plan: Dict[str, Any], success: bool):
        state = self.db.get_layer_plan_state(symbol, side)
        plan_data = dict(state.get('plan_data') or {})
        filled_layers = sorted(set(int(x) for x in (plan_data.get('filled_layers') or [])))
        pending_layers = sorted(set(int(x) for x in (plan_data.get('pending_layers') or [])))
        layer_no = int((layer_plan or {}).get('layer_no') or 0)
        if layer_no in pending_layers:
            pending_layers.remove(layer_no)
        if success and layer_no and layer_no not in filled_layers:
            filled_layers.append(layer_no)
            signal_id = (layer_plan or {}).get('signal_id')
            if signal_id is not None:
                signal_counts = dict(plan_data.get('signal_layer_counts') or {})
                signal_counts[str(signal_id)] = int(signal_counts.get(str(signal_id), 0) or 0) + 1
                plan_data['signal_layer_counts'] = signal_counts
            marker = self._get_signal_bar_marker(signal_id=signal_id, plan_context=layer_plan or {})
            if marker:
                markers = dict(plan_data.get('signal_bar_markers') or {})
                markers[marker] = datetime.utcnow().isoformat()
                plan_data['signal_bar_markers'] = markers
            plan_data['last_filled_at'] = datetime.utcnow().isoformat()
            plan_data['last_signal_id'] = signal_id
        plan_data['filled_layers'] = sorted(filled_layers)
        plan_data['pending_layers'] = sorted(pending_layers)
        plan_data['layer_ratios'] = (layer_plan or {}).get('layer_ratios') or plan_data.get('layer_ratios') or [0.06, 0.06, 0.04]
        plan_data['max_total_ratio'] = float((layer_plan or {}).get('max_total_ratio') or plan_data.get('max_total_ratio') or 0.16)
        status = 'active' if filled_layers or pending_layers else 'idle'
        self.db.save_layer_plan_state(symbol, side, status=status, current_layer=max(filled_layers or [0]), root_signal_id=(layer_plan or {}).get('root_signal_id') or state.get('root_signal_id'), plan_data=plan_data)

    def _prepare_open_execution(self, symbol: str, side: str, current_price: float, signal_id: int = None, plan_context: Dict[str, Any] = None, root_signal_id: int = None) -> tuple:
        runtime_ok, runtime_reason, runtime_details = self._check_layering_runtime_guards(symbol, side, signal_id=signal_id, plan_context={'current_price': current_price, **(plan_context or {})})
        if not runtime_ok:
            return False, runtime_reason, runtime_details

        lock_symbol, lock_side = self._get_direction_lock_scope_key(symbol, side)
        lock = self.db.get_direction_lock(lock_symbol, lock_side)
        if lock:
            return False, 'symbol+side 方向锁已被占用', merge_observability_details({'stage': 'hard_intercept', 'lock': lock, 'lock_scope': {'symbol': lock_symbol, 'side': lock_side}}, build_observability_context(symbol=symbol, side=side, signal_id=signal_id, root_signal_id=root_signal_id or signal_id, deny_reason='direction_lock'))

        layer_plan = plan_context or self._get_layer_plan(symbol, side, signal_id=signal_id, root_signal_id=root_signal_id, plan_context=plan_context)
        live_execution_profile = self._get_live_execution_profile(symbol, layer_plan)
        live_layering = dict(live_execution_profile.get('live') or {})
        layer_plan = dict(layer_plan or {})
        layer_plan.setdefault('baseline_layering', live_execution_profile.get('baseline') or {})
        layer_plan.setdefault('effective_layering', live_execution_profile.get('effective') or {})
        layer_plan['live_layering'] = live_layering
        snapshot = dict(live_execution_profile.get('snapshot') or {})
        use_live_shape = bool(snapshot.get('plan_shape_really_enforced'))
        if use_live_shape:
            layer_plan['layer_ratios'] = list(live_layering.get('layer_ratios') or layer_plan.get('layer_ratios') or [])
        elif 'layer_ratios' not in layer_plan:
            layer_plan['layer_ratios'] = list(live_layering.get('layer_ratios') or layer_plan.get('layer_ratios') or [])
        layer_plan['layer_count'] = len(layer_plan.get('layer_ratios') or [])
        if layer_plan.get('eligible', True) and layer_plan.get('layer_no') and layer_plan['layer_count'] >= int(layer_plan.get('layer_no') or 0) > 0:
            layer_plan['layer_ratio'] = float((layer_plan.get('layer_ratios') or [])[int(layer_plan.get('layer_no')) - 1])
        existing_max_total_ratio = layer_plan.get('plan_data', {}).get('max_total_ratio')
        live_max_total_ratio = live_layering.get('layer_max_total_ratio')
        if existing_max_total_ratio is not None and live_max_total_ratio is not None:
            layer_plan['max_total_ratio'] = float(min(float(existing_max_total_ratio), float(live_max_total_ratio)))
        else:
            layer_plan['max_total_ratio'] = float(existing_max_total_ratio or live_max_total_ratio or layer_plan.get('max_total_ratio') or sum(layer_plan.get('layer_ratios') or []) or 0.16)
        layer_plan['execution_profile_really_enforced'] = bool((live_execution_profile.get('snapshot') or {}).get('execution_profile_really_enforced'))
        if not layer_plan.get('eligible', True):
            return False, layer_plan.get('reason') or '分层资格不通过', merge_observability_details({'stage': 'layer_eligibility', 'layer_plan': layer_plan}, build_observability_context(symbol=symbol, side=side, signal_id=signal_id, root_signal_id=layer_plan.get('root_signal_id') or root_signal_id or signal_id, layer_no=layer_plan.get('layer_no'), deny_reason='layer_eligibility'))

        snapshot = self._build_latest_risk_snapshot(symbol, side, current_price=current_price)
        adaptive_risk_snapshot = build_risk_effective_snapshot(
            self.config,
            symbol,
            regime_snapshot=(plan_context or {}).get('regime_snapshot'),
            policy_snapshot=(plan_context or {}).get('adaptive_policy_snapshot'),
        )
        risk_budget = dict(adaptive_risk_snapshot.get('enforced_budget') or get_risk_budget_config(self.config, symbol))
        entry_plan = compute_entry_plan(
            total_balance=float(snapshot.get('balance_total') or 0.0),
            free_balance=float(snapshot.get('balance_free') or 0.0),
            current_total_margin=float(snapshot.get('current_total_margin') or 0.0),
            current_symbol_margin=float(snapshot.get('current_symbol_margin') or 0.0),
            risk_budget=risk_budget,
            requested_entry_ratio=float(layer_plan.get('layer_ratio') or 0.0),
            strict_requested_ratio=True,
        )
        if entry_plan.get('blocked'):
            return False, entry_plan.get('block_reason') or '风险预算不足', merge_observability_details({'stage': 'risk_budget', 'entry_plan': entry_plan, 'layer_plan': layer_plan}, build_observability_context(symbol=symbol, side=side, signal_id=signal_id, root_signal_id=layer_plan.get('root_signal_id') or root_signal_id or signal_id, layer_no=layer_plan.get('layer_no'), deny_reason=entry_plan.get('block_reason') or 'risk_budget', current_symbol_exposure=entry_plan.get('current_symbol_exposure_ratio'), projected_symbol_exposure=entry_plan.get('projected_symbol_exposure_ratio'), current_total_exposure=entry_plan.get('current_total_exposure_ratio'), projected_total_exposure=entry_plan.get('projected_total_exposure_ratio')))

        merged = dict(layer_plan)
        merged['signal_id'] = signal_id
        merged['current_price'] = current_price
        merged['adaptive_risk_snapshot'] = adaptive_risk_snapshot
        merged['entry_plan'] = entry_plan
        merged['planned_margin'] = float(entry_plan.get('allowed_margin') or 0.0)
        approved_context = build_observability_context(symbol=symbol, side=side, signal_id=signal_id, root_signal_id=merged.get('root_signal_id') or root_signal_id or signal_id, layer_no=merged.get('layer_no'), current_symbol_exposure=entry_plan.get('current_symbol_exposure_ratio'), projected_symbol_exposure=entry_plan.get('projected_symbol_exposure_ratio'), current_total_exposure=entry_plan.get('current_total_exposure_ratio'), projected_total_exposure=entry_plan.get('projected_total_exposure_ratio'))
        merged['observability'] = enrich_observability_with_snapshots(self.config, symbol, approved_context, plan_context=merged)
        return True, None, merge_observability_details({'stage': 'approved', 'plan_context': merged}, approved_context)

    def open_position(self, symbol: str, side: str, 
                    current_price: float, signal_id: int = None, plan_context: Dict[str, Any] = None, layer_no: int = None, root_signal_id: int = None) -> Optional[int]:
        """开仓"""
        plan_context = dict(plan_context or {})
        plan_context.setdefault('symbol', symbol)
        plan_context.setdefault('side', side)
        plan_context.setdefault('signal_id', signal_id)
        final_execution_permit = dict(plan_context.get('final_execution_permit') or {})
        guard_validation = validate_live_execution_guard_contract(plan_context, exchange_mode=str(self.config.get('exchange.mode', 'unknown') or 'unknown'))
        if guard_validation.get('required') and not guard_validation.get('passed', False):
            trade_logger.warning(
                f"{symbol}: live execution guard contract denied | reason_code={guard_validation.get('reason_code')} | reason={guard_validation.get('reason')}"
            )
            return None
        if final_execution_permit and not final_execution_permit.get('allowed', False):
            trade_logger.warning(
                f"{symbol}: final execution permit denied | reason_code={final_execution_permit.get('reason_code')} | reason={final_execution_permit.get('reason')}"
            )
            return None

        # 检查交易冷却
        if not self._check_cooldown(symbol):
            trade_logger.warning(f"{symbol}: 交易冷却中")
            return None
        
        # 获取余额
        try:
            balance = self.exchange.fetch_balance()
            available = balance.get('free', {}).get('USDT', 0)
        except Exception as e:
            trade_logger.error(f"获取余额失败: {e}")
            return None

        if available < 100:
            trade_logger.warning(f"余额不足: {available}")
            return None

        approved, approve_reason, approve_details = self._prepare_open_execution(
            symbol, side, current_price, signal_id=signal_id, plan_context=plan_context, root_signal_id=root_signal_id
        )
        if not approved:
            trade_logger.warning(f"{symbol}: {approve_reason} | details={approve_details} | obs={observability_log_text((approve_details or {}).get('observability'))}")
            return None
        plan_context = dict((approve_details or {}).get('plan_context') or {})
        if layer_no is not None:
            plan_context['layer_no'] = layer_no
        lock_symbol, lock_side = self._get_direction_lock_scope_key(symbol, side)
        lock_owner = self._build_lock_owner(symbol, side, signal_id=signal_id)
        if not self.db.acquire_direction_lock(lock_symbol, lock_side, owner=lock_owner):
            trade_logger.warning(f"{symbol}: 获取方向锁失败，疑似并发重复开仓")
            return None
        intent_id = None
        try:
            self._reserve_layer(symbol, side, plan_context)
            intent_id = self.db.create_open_intent(
                symbol=symbol,
                side=side,
                signal_id=signal_id,
                root_signal_id=root_signal_id or plan_context.get('root_signal_id') or signal_id,
                planned_margin=float(plan_context.get('planned_margin') or 0.0),
                leverage=int(self.trading_config.get('leverage', 10) or 10),
                layer_no=plan_context.get('layer_no'),
                plan_context=plan_context,
                notes='pre-submit intent',
                status='pending',
            )
        except Exception:
            self.db.release_direction_lock(lock_symbol, lock_side, owner=lock_owner)
            raise

        trade_logger.info(f"{symbol}: executor 最终放行通过 | plan_context={plan_context} | obs={observability_log_text(plan_context.get('observability'))}")
        
        # 计算开仓数量 - 修复：基于实际杠杆计算，确保保证金占比准确
        # 步骤1: 先设置杠杆到交易所（确保一致）
        configured_leverage = self.trading_config.get('leverage', 10)
        adaptive_risk_snapshot = dict(plan_context.get('adaptive_risk_snapshot') or {})
        leverage_cap = (adaptive_risk_snapshot.get('enforced_budget') or adaptive_risk_snapshot.get('effective') or {}).get('leverage_cap')
        planned_leverage = min(int(configured_leverage), int(leverage_cap)) if leverage_cap else int(configured_leverage)
        try:
            if self.exchange.is_futures_symbol(symbol):
                self.exchange.set_leverage(symbol, planned_leverage)
        except Exception as e:
            trade_logger.warning(f"{symbol}: 设置杠杆失败，将使用配置值 - {e}")
        
        # 步骤2: 获取实际杠杆（交易所可能调整）
        exchange_leverage = self.exchange.get_actual_leverage(symbol) if hasattr(self.exchange, 'get_actual_leverage') else planned_leverage
        effective_leverage = min(planned_leverage, int(exchange_leverage or planned_leverage))
        
        # 步骤3: 按目标保证金计算名义价值
        # 目标: 10% 保证金 = available * position_ratio
        # 名义价值 = 保证金 * 实际杠杆
        entry_plan = dict(plan_context.get('entry_plan') or {})
        position_ratio = float(plan_context.get('layer_ratio') or entry_plan.get('effective_entry_margin_ratio') or 0.0)
        target_margin = float(plan_context.get('planned_margin') or entry_plan.get('allowed_margin') or 0.0)
        desired_notional = target_margin * effective_leverage  # 名义价值 (e.g., 1000 * 10 = 10000 USDT)
        
        # 可观察性日志
        trade_logger.info(
            f"{symbol}: 仓位计算 - 配置杠杆:{configured_leverage}x, 计划杠杆:{planned_leverage}x, 实际杠杆:{effective_leverage}x, "
            f"目标保证金:{target_margin:.2f}USDT({position_ratio*100:.0f}%), 目标名义:{desired_notional:.2f}USDT"
        )
        
        try:
            if not self.exchange.is_futures_symbol(symbol):
                trade_logger.warning(f"{symbol}: 非U本位合约，跳过")
                return None
            amount = self.exchange.normalize_contract_amount(symbol, desired_notional, current_price)
            contract_size = self.exchange.get_contract_size(symbol) if hasattr(self.exchange, 'get_contract_size') else 1.0
            coin_quantity = self.exchange.contracts_to_coin_quantity(symbol, amount) if hasattr(self.exchange, 'contracts_to_coin_quantity') else amount * contract_size
            
            # 验证：计算实际保证金占用
            estimated_margin = (coin_quantity * current_price) / effective_leverage if effective_leverage > 0 else desired_notional
            actual_margin_ratio = estimated_margin / available if available > 0 else 0
            trade_logger.info(
                f"{symbol}: 预估保证金 {estimated_margin:.2f}USDT ({actual_margin_ratio*100:.1f}% of balance), "
                f"合约数:{amount}, 币数量:{coin_quantity:.4f}"
            )
        except Exception as e:
            trade_logger.error(f"计算下单数量失败: {e}")
            if 'intent_id' in locals() and intent_id:
                self.db.update_open_intent(intent_id, status='failed', notes=str(e))
                self.db.delete_open_intent(intent_id)
            if 'plan_context' in locals():
                self._finalize_layer(symbol, side, plan_context, success=False)
            if 'lock_owner' in locals():
                self.db.release_direction_lock(lock_symbol, lock_side, owner=lock_owner)
            return None
        
        # 重试机制
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            order = None
            try:
                # 开仓
                if intent_id:
                    self.db.update_open_intent(intent_id, status='submitted', notes=f'order submit attempt #{attempt + 1}')
                order = self.exchange.create_order(
                    symbol, 
                    'buy' if side == 'long' else 'sell', 
                    amount,
                    posSide=side
                )
                
                # 记录交易
                trade_id = self.db.record_trade(
                    symbol=symbol,
                    side=side,
                    entry_price=current_price,
                    quantity=amount,
                    contract_size=contract_size,
                    coin_quantity=coin_quantity,
                    leverage=effective_leverage,
                    signal_id=signal_id,
                    notes=f"开仓尝试 #{attempt + 1}",
                    layer_no=plan_context.get('layer_no'),
                    root_signal_id=root_signal_id or plan_context.get('root_signal_id') or signal_id,
                    plan_context=plan_context
                )
                
                # 更新持仓
                self.db.update_position(
                    symbol=symbol,
                    side=side,
                    entry_price=current_price,
                    quantity=amount,
                    contract_size=contract_size,
                    coin_quantity=coin_quantity,
                    leverage=effective_leverage,
                    current_price=current_price
                )
                
                # 更新冷却时间
                self._update_cooldown(symbol)
                self._seed_trailing_anchor(symbol, side, current_price)
                
                if intent_id:
                    self.db.update_open_intent(intent_id, status='filled', trade_id=trade_id, notes='filled')
                    self.db.delete_open_intent(intent_id)
                    intent_id = None
                self._finalize_layer(symbol, side, plan_context, success=True)
                self.db.release_direction_lock(lock_symbol, lock_side, owner=lock_owner)
                trade_logger.trade(
                    symbol, side, current_price, amount, trade_id
                )
                trade_logger.info(f"{symbol}: 开仓执行完成 | obs={observability_log_text(plan_context.get('observability'))}")
                
                return trade_id
                
            except Exception as e:
                err_text = str(e)
                if order is not None or self._exchange_has_position(symbol, side):
                    recovered_trade_id = self._recover_open_trade_from_exchange(
                        symbol,
                        side,
                        signal_id=signal_id,
                        note=f"开仓后本地补记恢复（attempt={attempt + 1}, error={err_text}）"
                    )
                    if recovered_trade_id:
                        if intent_id:
                            self.db.update_open_intent(intent_id, status='filled', trade_id=recovered_trade_id, notes='recovered_from_exchange')
                            self.db.delete_open_intent(intent_id)
                            intent_id = None
                        self._finalize_layer(symbol, side, plan_context, success=True)
                        self.db.release_direction_lock(lock_symbol, lock_side, owner=lock_owner)
                        self._update_cooldown(symbol)
                        self._seed_trailing_anchor(symbol, side, current_price)
                        trade_logger.warning(f"{symbol}: 开仓后本地处理异常，但已按交易所持仓恢复成功 - {err_text}")
                        return recovered_trade_id
                trade_logger.error(f"开仓失败 (尝试 {attempt + 1}/{max_retries}): {err_text}")
                if '51202' in err_text:
                    amount = round(amount * 0.5, 8)
                    trade_logger.warning(f"{symbol}: 市价单数量超过上限，自动缩量后重试 -> {amount}")
                    if amount <= 0:
                        return None
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    if intent_id:
                        self.db.update_open_intent(intent_id, status='failed', notes=err_text)
                        self.db.delete_open_intent(intent_id)
                        intent_id = None
                    self._finalize_layer(symbol, side, plan_context, success=False)
                    self.db.release_direction_lock(lock_symbol, lock_side, owner=lock_owner)
                    return None
    
    def close_position(self, symbol: str, reason: str = 'manual',
                     close_price: float = None, close_quantity: float = None) -> bool:
        """平仓 - U本位合约
        
        Args:
            symbol: 交易对
            reason: 平仓原因
            close_price: 平仓价格（默认市价）
            close_quantity: 平仓数量（合约张数），默认全部平仓
        """
        
        # 获取持仓
        positions = self.db.get_positions()
        position = None
        for p in positions:
            if p['symbol'] == symbol:
                position = p
                break
        
        if not position:
            trade_logger.warning(f"无持仓: {symbol}")
            return False
        
        side = position['side']  # 'long' or 'short'
        # 支持部分平仓：使用指定数量或全部
        quantity = close_quantity if close_quantity is not None else position['quantity']
        coin_quantity = float(position.get('coin_quantity', 0) or 0)
        contract_size = float(position.get('contract_size', 1) or 1)
        entry_price = position['entry_price']
        
        # 获取当前价格
        if close_price is None:
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                close_price = ticker['last']
            except Exception as e:
                trade_logger.error(f"获取价格失败: {e}")
                return False
        
        # 重试机制
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # U本位平仓 - 通过创建反向订单平仓
                # 多仓平空，空仓平多
                close_side = 'sell' if side == 'long' else 'buy'
                
                self.exchange.close_order(
                    symbol, 
                    close_side,
                    quantity,
                    posSide=side
                )

                trade = self.db.get_latest_open_trade(symbol, side)
                exchange_close = self.exchange.fetch_closed_trade_summary(trade, fallback_price=close_price) if trade else None
                if exchange_close and exchange_close.get('exit_price'):
                    close_price = exchange_close['exit_price']
                
                # 计算部分平仓的币数量
                is_partial = close_quantity is not None and close_quantity < position['quantity']
                if is_partial:
                    # 部分平仓：按比例计算币数量
                    close_ratio = close_quantity / position['quantity']
                    closed_coin_quantity = coin_quantity * close_ratio
                    remaining_coin_quantity = coin_quantity - closed_coin_quantity
                else:
                    closed_coin_quantity = coin_quantity
                    remaining_coin_quantity = 0
                
                # 计算盈亏（基于实际平仓的币数量）
                if side == 'long':
                    pnl = (close_price - entry_price) * closed_coin_quantity
                    pnl_percent = (close_price - entry_price) / entry_price * 100
                else:
                    pnl = (entry_price - close_price) * closed_coin_quantity
                    pnl_percent = (entry_price - close_price) / entry_price * 100
                
                # 杠杆后盈亏
                leverage = position.get('leverage', 1)
                leveraged_pnl_percent = pnl_percent * leverage
                
                # 更新交易记录（positions.id ≠ trades.id，需回查最新 open trade）
                trade_id = trade.get('id') if trade else None
                if trade_id:
                    close_note = f"平仓原因: {reason}"
                    if is_partial:
                        close_note += f" | 部分平仓({close_ratio*100:.0f}%)"
                    if exchange_close:
                        self.db.reconcile_trade_close(trade_id, exchange_close, reason=close_note)
                    else:
                        self.db.close_trade_with_outcome_enrichment(
                            trade_id=trade_id,
                            exit_price=close_price,
                            pnl=pnl,
                            pnl_percent=leveraged_pnl_percent,
                            notes=close_note,
                            close_source='local_market_close'
                        )
                else:
                    trade_logger.warning(f"{symbol}: 未找到可关闭的 open trade 记录，持仓会先从本地移除")
                
                # 部分平仓：更新持仓；全部平仓：删除持仓
                if is_partial:
                    self.db.update_position(
                        symbol=symbol,
                        side=side,
                        entry_price=entry_price,
                        quantity=position['quantity'] - close_quantity,
                        leverage=leverage,
                        current_price=close_price,
                        peak_price=position.get('peak_price'),
                        trough_price=position.get('trough_price'),
                        contract_size=contract_size,
                        coin_quantity=remaining_coin_quantity
                    )
                    self.db.sync_layer_plan_state(symbol, side, reset_if_flat=False)
                    trade_logger.info(f"{symbol}: 部分平仓完成，剩余{(position['quantity'] - close_quantity):.4f}张")
                else:
                    self.db.close_position(symbol)
                    self.db.sync_layer_plan_state(symbol, side, reset_if_flat=True)
                    self.db.cleanup_orphan_execution_state(stale_after_minutes=1)
                    # 只有全部平仓才清除缓存
                    self._update_cooldown(symbol)
                    self._clear_trade_cache(symbol)
                
                trade_logger.close(symbol, close_price, pnl, reason)
                self._store_close_result(symbol, side, {
                    'exit_price': close_price,
                    'pnl': exchange_close.get('pnl') if exchange_close and exchange_close.get('pnl') is not None else pnl,
                    'pnl_percent': exchange_close.get('pnl_percent') if exchange_close and exchange_close.get('pnl_percent') is not None else leveraged_pnl_percent,
                    'reason': reason,
                    'trade_id': trade_id,
                    'close_source': exchange_close.get('source') if exchange_close else 'local_market_close',
                    'close_summary': dict(exchange_close or {}),
                    'is_partial': is_partial,
                })
                
                return True
                
            except Exception as e:
                message = str(e)
                trade_logger.error(f"平仓失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if '51169' in message and not self._exchange_has_position(symbol, side):
                    self._clear_trade_cache(symbol)
                    return self._close_local_position_as_stale(symbol, side, close_price, f'{reason} | 交易所已无对应仓位')
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    return False
        
        return False
    

    def _get_open_trade_plan_context(self, symbol: str, side: str = None) -> Dict[str, Any]:
        trade = self.db.get_latest_open_trade(symbol, side) if self.db else None
        return dict((trade or {}).get('plan_context') or {})

    def _resolve_live_exit_profile(self, symbol: str, side: str = None) -> Dict[str, Any]:
        plan_context = self._get_open_trade_plan_context(symbol, side)
        observability = dict(plan_context.get('observability') or {})
        snapshot = dict(observability.get('adaptive_execution_snapshot') or {})
        if not snapshot:
            snapshot = build_execution_effective_snapshot(
                self.config,
                symbol,
                regime_snapshot=plan_context.get('regime_snapshot'),
                policy_snapshot=plan_context.get('adaptive_policy_snapshot'),
            )
        baseline = dict(snapshot.get('baseline') or {})
        live = dict(snapshot.get('live') or snapshot.get('enforced_profile') or baseline)
        return {
            'plan_context': plan_context,
            'observability': observability,
            'snapshot': snapshot,
            'baseline': baseline,
            'live': live,
            'enforced': bool(snapshot.get('effective_state') == 'effective' and snapshot.get('exit_enforcement_enabled', False)),
            'hinted': bool(snapshot.get('exit_hints_enabled', False)),
        }

    def check_stop_loss(self, symbol: str, current_price: float) -> bool:
        """检查止损"""
        
        # 参数校验
        if not current_price or current_price <= 0:
            return False
        
        positions = self.db.get_positions()
        position = None
        for p in positions:
            if p.get('symbol') == symbol:
                position = p
                break
        
        if not position:
            return False
        
        # 防御性获取必要字段，避免 KeyError
        side = position.get('side')
        entry_price = position.get('entry_price')
        
        if not side or entry_price is None or entry_price <= 0:
            trade_logger.warning(f"{symbol}: 持仓数据不完整 (side={side}, entry_price={entry_price})")
            return False
        
        leverage = position.get('leverage', 1) or 1
        exit_profile = self._resolve_live_exit_profile(symbol, side)
        stop_loss = exit_profile['live'].get('stop_loss')
        if stop_loss is None:
            stop_loss = self._recommendation_provider.get_stop_loss(symbol)
        
        # 计算盈亏比例
        try:
            if side == 'long':
                pnl_percent = (current_price - entry_price) / entry_price
            else:
                pnl_percent = (entry_price - current_price) / entry_price
        except (TypeError, ZeroDivisionError):
            trade_logger.error(f"{symbol}: 止损计算失败 (entry_price={entry_price})")
            return False
        
        # 杠杆后盈亏
        leveraged_pnl = pnl_percent * leverage
        
        if leveraged_pnl <= -stop_loss:
            trade_logger.info(f"触发止损: {symbol} 亏损{leveraged_pnl*100:.2f}%")
            return True
        
        return False
    
    def check_take_profit(self, symbol: str, current_price: float,
                         highest_price: float = None) -> bool:
        """检查止盈/追踪止损"""
        
        # 参数校验
        if not current_price or current_price <= 0:
            return False
        
        positions = self.db.get_positions()
        position = None
        for p in positions:
            if p.get('symbol') == symbol:
                position = p
                break
        
        if not position:
            self._clear_trade_cache(symbol)
            return False
        
        # 防御性获取必要字段，避免 KeyError
        side = position.get('side')
        entry_price = position.get('entry_price')
        
        if not side or entry_price is None or entry_price <= 0:
            trade_logger.warning(f"{symbol}: 持仓数据不完整 (side={side}, entry_price={entry_price})")
            self._clear_trade_cache(symbol)
            return False
        
        leverage = position.get('leverage', 1) or 1
        exit_profile = self._resolve_live_exit_profile(symbol, side)

        # 追踪止损 - 优先使用 adaptive live profile，其次 recommendation/config 默认值
        ts_params = self._recommendation_provider.get_trailing_stop(symbol)

        config_ts = exit_profile['live'].get('trailing_stop')
        rec_ts = ts_params.get('distance')
        trailing_stop = config_ts if config_ts is not None else rec_ts

        config_ta = exit_profile['live'].get('trailing_activation')
        rec_ta = ts_params.get('activation')

        if config_ta is not None:
            trailing_activation = config_ta
        elif rec_ta is not None:
            trailing_activation = rec_ta
        else:
            trailing_activation = 0.01  # 默认 1% 盈利激活
        
        # 计算当前盈利比例（杠杆前）
        try:
            if side == 'long':
                pnl_percent = (current_price - entry_price) / entry_price
            else:
                pnl_percent = (entry_price - current_price) / entry_price
        except (TypeError, ZeroDivisionError):
            pnl_percent = 0
        
        # 检查是否已达到激活阈值（None 表示始终激活，作为安全回退）
        # 一旦激活（trailing_armed），就保持激活状态
        cache = self._trade_cache.setdefault(symbol, {})
        already_armed = cache.get('trailing_armed', False)
        trailing_activated = already_armed or (trailing_activation is None or pnl_percent >= trailing_activation)
        
        if side == 'long':
            anchor = highest_price if highest_price is not None else cache.get('highest_price', position.get('peak_price') or entry_price)
            anchor = max(float(anchor or entry_price), float(position.get('peak_price') or entry_price), float(current_price or entry_price))
            cache['highest_price'] = anchor
            self.db.update_position(symbol, side, entry_price, position.get('quantity', 0), leverage, current_price, peak_price=anchor, trough_price=position.get('trough_price'), contract_size=position.get('contract_size', 1) or 1, coin_quantity=position.get('coin_quantity'))
            
            # 盈利触发型追踪：只有激活后且价格回落才触发
            if trailing_activated:
                stop_price = anchor * (1 - trailing_stop)
                if current_price <= stop_price and current_price > entry_price:
                    try:
                        leveraged_pnl = pnl_percent * leverage
                    except (TypeError, ZeroDivisionError):
                        leveraged_pnl = 0
                    trade_logger.info(f"触发追踪止损: {symbol} 盈利{leveraged_pnl*100:.2f}%")
                    return True
            elif cache.get('trailing_armed'):
                # 记录首次激活
                trade_logger.info(f"{symbol}: 追踪止损已激活 (盈利{pnl_percent*100:.2f}%, 阈值{trailing_activation*100:.2f}%)")
            cache['trailing_armed'] = trailing_activated
        else:
            anchor = cache.get('lowest_price', position.get('trough_price') or entry_price)
            anchor = min(float(anchor or entry_price), float(position.get('trough_price') or entry_price), float(current_price or entry_price))
            cache['lowest_price'] = anchor
            self.db.update_position(symbol, side, entry_price, position.get('quantity', 0), leverage, current_price, peak_price=position.get('peak_price'), trough_price=anchor, contract_size=position.get('contract_size', 1) or 1, coin_quantity=position.get('coin_quantity'))
            
            # 盈利触发型追踪：只有激活后且价格回升才触发
            if trailing_activated:
                stop_price = anchor * (1 + trailing_stop)
                if current_price >= stop_price and current_price < entry_price:
                    try:
                        leveraged_pnl = pnl_percent * leverage
                    except (TypeError, ZeroDivisionError):
                        leveraged_pnl = 0
                    trade_logger.info(f"触发追踪止损: {symbol} 盈利{leveraged_pnl*100:.2f}%")
                    return True
            elif cache.get('trailing_armed'):
                trade_logger.info(f"{symbol}: 追踪止损已激活 (盈利{pnl_percent*100:.2f}%, 阈值{trailing_activation*100:.2f}%)")
            cache['trailing_armed'] = trailing_activated
        
        # 普通止盈 - 优先使用配置值，其次 MFE/MAE 建议
        config_tp = exit_profile['live'].get('take_profit')
        rec_tp = self._recommendation_provider.get_take_profit(symbol)
        take_profit = rec_tp if config_tp is None else config_tp
        
        try:
            if side == 'long':
                pnl_percent = (current_price - entry_price) / entry_price
            else:
                pnl_percent = (entry_price - current_price) / entry_price
        except (TypeError, ZeroDivisionError):
            trade_logger.error(f"{symbol}: 止盈计算失败 (entry_price={entry_price})")
            return False
        
        leveraged_pnl = pnl_percent * leverage
        
        # 检查部分止盈（第一止盈层）
        partial_tp = self._check_partial_take_profit(symbol, leveraged_pnl, position, current_price)
        if partial_tp:
            # 第一止盈层已触发，检查是否需要检查第二止盈层
            partial_tp2 = self._check_partial_take_profit2(symbol, leveraged_pnl, current_price)
            if partial_tp2:
                # 第二止盈层也触发（在同一周期内不可能，因为会先平第一层）
                return True
            # 第一止盈层已执行，本周期返回 True，避免调用方继续把剩余仓位当作整仓处理
            return True
        
        # 检查部分止盈（第二止盈层）- 只有第一止盈层已执行或未配置时才检查
        partial_tp2 = self._check_partial_take_profit2(symbol, leveraged_pnl, current_price)
        if partial_tp2:
            return True
        
        # 普通止盈 - 优先使用配置值，其次 MFE/MAE 建议
        if leveraged_pnl >= take_profit:
            trade_logger.info(f"触发止盈: {symbol} 盈利{leveraged_pnl*100:.2f}%")
            return True
        
        return False
    
    def _check_partial_take_profit(self, symbol: str, leveraged_pnl: float, 
                                    position: Dict, current_price: float) -> bool:
        """检查部分止盈（第一止盈层）
        
        Returns:
            True if partial TP was executed, False otherwise
        """
        # 读取分批止盈配置（带安全回退）
        partial_tp_enabled = self.trading_config.get('partial_tp_enabled', False)
        if not partial_tp_enabled:
            return False
        
        # 获取阈值和比例（带默认值）
        partial_tp_threshold = self.trading_config.get('partial_tp_threshold', 0.015)  # 默认 1.5%
        partial_tp_ratio = self.trading_config.get('partial_tp_ratio', 0.5)  # 默认平 50%
        
        # 检查是否已达到部分止盈阈值
        if leveraged_pnl >= partial_tp_threshold:
            # 检查是否已经执行过部分止盈（避免重复）
            cache = self._trade_cache.setdefault(symbol, {})
            if cache.get('partial_tp_executed', False):
                return False
            
            # 计算部分平仓数量
            full_quantity = position.get('quantity', 0)
            close_quantity = full_quantity * partial_tp_ratio
            close_quantity = round(close_quantity, 4)
            
            if close_quantity <= 0:
                return False
            
            # 标记为已执行
            cache['partial_tp_executed'] = True
            
            # 执行部分平仓
            trade_logger.info(
                f"{symbol}: 触发部分止盈 (盈利{leveraged_pnl*100:.2f}%, 阈值{partial_tp_threshold*100:.1f}%, "
                f"平{partial_tp_ratio*100:.0f}%仓位={close_quantity}张)"
            )
            
            # 获取 trade_id 用于记录 partial TP 历史
            trade = self.db.get_latest_open_trade(symbol, position.get('side'))
            trade_id = trade.get('id') if trade else None
            
            # 计算部分平仓盈亏
            entry_price = position.get('entry_price', 0)
            side = position.get('side', 'long')
            coin_quantity = position.get('coin_quantity', 0) or 0
            contract_size = position.get('contract_size', 1) or 1
            close_coin_quantity = coin_quantity * partial_tp_ratio
            if side == 'long':
                pnl = (current_price - entry_price) * close_coin_quantity
            else:
                pnl = (entry_price - current_price) * close_coin_quantity
            
            # 执行平仓
            result = self.close_position(
                symbol=symbol,
                reason='partial_tp',
                close_price=current_price,
                close_quantity=close_quantity
            )
            
            # 记录 partial TP 触发历史
            if result:
                self.db.record_partial_tp(
                    trade_id=trade_id,
                    symbol=symbol,
                    side=side,
                    trigger_price=current_price,
                    close_ratio=partial_tp_ratio,
                    close_quantity=close_quantity,
                    pnl=pnl,
                    note=f"触发阈值:{partial_tp_threshold*100:.1f}%"
                )
            
            return result
        
        return False
    
    def _check_partial_take_profit2(self, symbol: str, leveraged_pnl: float,
                                     current_price: float) -> bool:
        """检查部分止盈（第二止盈层 / 多级退出）
        
        在第一止盈层执行后，当盈利继续扩大时触发。
        
        Returns:
            True if partial TP2 was executed, False otherwise
        """
        # 读取第二止盈层配置（带安全回退）
        partial_tp2_enabled = self.trading_config.get('partial_tp2_enabled', False)
        if not partial_tp2_enabled:
            return False
        
        # 获取阈值和比例（带默认值）
        partial_tp2_threshold = self.trading_config.get('partial_tp2_threshold', 0.03)  # 默认 3%
        partial_tp2_ratio = self.trading_config.get('partial_tp2_ratio', 0.3)  # 默认平 30%
        
        # 检查是否已达到第二止盈阈值
        if leveraged_pnl >= partial_tp2_threshold:
            # 检查是否已经执行过第二止盈（避免重复）
            cache = self._trade_cache.setdefault(symbol, {})
            if cache.get('partial_tp2_executed', False):
                return False
            
            # 获取当前持仓（第一止盈层执行后可能有剩余）
            positions = self.db.get_positions()
            position = None
            for p in positions:
                if p.get('symbol') == symbol:
                    position = p
                    break
            
            if not position:
                return False
            
            # 计算部分平仓数量
            full_quantity = position.get('quantity', 0)
            close_quantity = full_quantity * partial_tp2_ratio
            close_quantity = round(close_quantity, 4)
            
            if close_quantity <= 0:
                return False
            
            # 标记为已执行
            cache['partial_tp2_executed'] = True
            
            # 执行部分平仓
            trade_logger.info(
                f"{symbol}: 触发第二止盈层 (盈利{leveraged_pnl*100:.2f}%, 阈值{partial_tp2_threshold*100:.1f}%, "
                f"平{partial_tp2_ratio*100:.0f}%仓位={close_quantity}张)"
            )
            
            # 获取 trade_id 用于记录 partial TP 历史
            trade = self.db.get_latest_open_trade(symbol, position.get('side'))
            trade_id = trade.get('id') if trade else None
            
            # 计算部分平仓盈亏
            entry_price = position.get('entry_price', 0)
            side = position.get('side', 'long')
            coin_quantity = position.get('coin_quantity', 0) or 0
            close_coin_quantity = coin_quantity * partial_tp2_ratio
            if side == 'long':
                pnl = (current_price - entry_price) * close_coin_quantity
            else:
                pnl = (entry_price - current_price) * close_coin_quantity
            
            # 执行平仓
            result = self.close_position(
                symbol=symbol,
                reason='partial_tp2',
                close_price=current_price,
                close_quantity=close_quantity
            )
            
            # 记录 partial TP 触发历史
            if result:
                self.db.record_partial_tp(
                    trade_id=trade_id,
                    symbol=symbol,
                    side=side,
                    trigger_price=current_price,
                    close_ratio=partial_tp2_ratio,
                    close_quantity=close_quantity,
                    pnl=pnl,
                    note=f"第二止盈层 | 阈值:{partial_tp2_threshold*100:.1f}%"
                )
            
            return result
        
        return False
    
    def update_positions(self) -> Dict[str, Any]:
        """更新所有持仓状态（优先使用交易所真实持仓字段）"""
        positions = self.db.get_positions()
        updated = {}

        exchange_positions = {}
        try:
            for pos in self.exchange.fetch_positions() or []:
                normalized = pos if pos.get('contract_size') is not None else self.exchange.normalize_position(pos)
                if not normalized:
                    continue
                key = (normalized.get('symbol'), normalized.get('side'))
                exchange_positions[key] = normalized
        except Exception as e:
            trade_logger.warning(f"拉取交易所持仓失败，回退本地价格刷新: {e}")

        for position in positions:
            symbol = position['symbol']
            side = position['side']
            try:
                normalized = exchange_positions.get((symbol, side))
                if normalized:
                    entry_price = float(normalized.get('entry_price') or position.get('entry_price') or 0)
                    current_price = float(normalized.get('current_price') or entry_price or position.get('current_price') or 0)
                    quantity = float(normalized.get('quantity') or normalized.get('contracts') or position.get('quantity') or 0)
                    contract_size = float(normalized.get('contract_size') or position.get('contract_size') or 1)
                    coin_quantity = float(normalized.get('coin_quantity') or quantity * contract_size)
                    leverage = int(float(normalized.get('leverage') or position.get('leverage') or 1))
                else:
                    ticker = self.exchange.fetch_ticker(symbol)
                    current_price = ticker['last']
                    entry_price = position['entry_price']
                    quantity = position['quantity']
                    contract_size = position.get('contract_size', 1)
                    coin_quantity = position.get('coin_quantity')
                    leverage = position['leverage']

                self.db.update_position(
                    symbol=symbol,
                    side=side,
                    entry_price=entry_price,
                    quantity=quantity,
                    contract_size=contract_size,
                    coin_quantity=coin_quantity,
                    leverage=leverage,
                    current_price=current_price
                )

                trade = self.db.get_latest_open_trade(symbol, side)
                if trade and normalized:
                    self.db.sync_trade_with_exchange_snapshot(
                        trade['id'],
                        quantity=quantity,
                        contract_size=contract_size,
                        coin_quantity=coin_quantity,
                        leverage=leverage,
                        entry_price=entry_price,
                        notes='持仓刷新同步交易所字段',
                    )

                updated[symbol] = {
                    'current_price': current_price,
                    'quantity': quantity,
                    'coin_quantity': coin_quantity,
                    'leverage': leverage,
                    'source': 'exchange' if normalized else 'ticker',
                    'updated': True
                }

            except Exception as e:
                trade_logger.error(f"更新{symbol}持仓失败: {e}")
                updated[symbol] = {'error': str(e)}

        return updated
    
    def get_portfolio_status(self) -> Dict[str, Any]:
        """获取投资组合状态"""
        positions = self.db.get_positions()
        
        total_pnl = 0
        total_value = 0
        
        for p in positions:
            unrealized_pnl = p.get('unrealized_pnl', 0)
            value = p.get('coin_quantity', 0) * p.get('current_price', 0)
            total_pnl += unrealized_pnl
            total_value += value
        
        # 获取交易统计
        trade_stats = self.db.get_trade_stats(days=30)
        
        return {
            'total_positions': len(positions),
            'total_value': total_value,
            'unrealized_pnl': total_pnl,
            'trade_stats': trade_stats,
            'positions': positions
        }
    
    def _check_cooldown(self, symbol: str) -> bool:
        """检查交易冷却（优先数据库，避免守护跨周期失效）"""
        cooldown_minutes = self.trading_config.get('cooldown_minutes', 15)
        last_trade = self.db.get_latest_trade_time(symbol)
        if not last_trade and symbol in self._trade_cache:
            last_trade = self._trade_cache[symbol].get('last_trade')
        if last_trade:
            diff_minutes = (datetime.utcnow() - last_trade).total_seconds() / 60
            if diff_minutes < cooldown_minutes:
                return False
        return True
    
    def _update_cooldown(self, symbol: str):
        """更新冷却时间"""
        if symbol not in self._trade_cache:
            self._trade_cache[symbol] = {}
        self._trade_cache[symbol]['last_trade'] = datetime.now()

    def _seed_trailing_anchor(self, symbol: str, side: str, price: float):
        if symbol not in self._trade_cache:
            self._trade_cache[symbol] = {}
        # 清除部分止盈标记（新仓位重新开始）
        self._trade_cache[symbol].pop('partial_tp_executed', None)
        self._trade_cache[symbol].pop('partial_tp2_executed', None)
        if side == 'long':
            self._trade_cache[symbol]['highest_price'] = price
        else:
            self._trade_cache[symbol]['lowest_price'] = price

    def _clear_trade_cache(self, symbol: str):
        self._trade_cache.pop(symbol, None)


class RiskManager:
    """风险管理器"""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.trading_config = config.get('trading', {})
        self.layering_config = config.get_layering_config() if hasattr(config, 'get_layering_config') else dict(DEFAULT_LAYERING_CONFIG)
        self._exchange = None

    def _loss_guard_enabled(self) -> bool:
        return bool(self.trading_config.get('loss_streak_lock_enabled', True))

    def _loss_guard_hours(self) -> int:
        return int(self.trading_config.get('loss_streak_cooldown_hours', 12) or 12)

    def _build_adaptive_risk_snapshot(self, symbol: str, plan_context: Dict[str, Any] = None, signal: Any = None) -> Optional[Dict[str, Any]]:
        """Build adaptive risk snapshot early for use in guards and observability."""
        try:
            from core.regime_policy import (
                build_observe_only_payload,
                build_risk_effective_snapshot,
            )
            payload = build_observe_only_payload(
                self.config,
                symbol,
                signal=signal,
                regime_snapshot=(plan_context or {}).get('regime_snapshot'),
                policy_snapshot=(plan_context or {}).get('adaptive_policy_snapshot'),
            )
            return build_risk_effective_snapshot(
                self.config,
                symbol,
                signal=signal,
                regime_snapshot=payload.get('regime_snapshot'),
                policy_snapshot=payload.get('adaptive_policy_snapshot'),
            )
        except Exception:
            return None

    def _get_close_outcome_guard_config(self) -> Dict[str, Any]:
        raw = self.trading_config.get('close_outcome_feedback_guard', {}) or {}
        return {
            'enabled': bool(raw.get('enabled', True)),
            'lookback_limit': max(int(raw.get('lookback_limit', 20) or 20), 1),
            'min_sample_size': max(int(raw.get('min_sample_size', 5) or 5), 1),
            'block_modes': [str(x).strip().lower() for x in (raw.get('block_modes') or ['rollback']) if str(x).strip()],
            'tighten_modes': [str(x).strip().lower() for x in (raw.get('tighten_modes') or ['tighten']) if str(x).strip()],
            'require_freeze_for_block': bool(raw.get('require_freeze_for_block', True)),
            'tighten_entry_ratio_multiplier': float(raw.get('tighten_entry_ratio_multiplier', 0.5) or 0.5),
            'tighten_total_margin_cap_multiplier': float(raw.get('tighten_total_margin_cap_multiplier', 0.85) or 0.85),
            'tighten_symbol_margin_cap_multiplier': float(raw.get('tighten_symbol_margin_cap_multiplier', 0.75) or 0.75),
            'tighten_leverage_cap_multiplier': float(raw.get('tighten_leverage_cap_multiplier', 0.7) or 0.7),
            'floor_entry_margin_ratio': float(raw.get('floor_entry_margin_ratio', 0.02) or 0.02),
            'floor_total_margin_cap_ratio': float(raw.get('floor_total_margin_cap_ratio', 0.10) or 0.10),
            'floor_symbol_margin_cap_ratio': float(raw.get('floor_symbol_margin_cap_ratio', 0.05) or 0.05),
            'floor_leverage_cap': max(int(raw.get('floor_leverage_cap', 2) or 2), 1),
            'scoped_window_enabled': bool(raw.get('scoped_window_enabled', True)),
            'scoped_window_hours': float(raw.get('scoped_window_hours', 6) or 6),
            'scoped_window_min_sample_size': max(int(raw.get('scoped_window_min_sample_size', 3) or 3), 1),
            'scoped_window_modes': [str(x).strip().lower() for x in (raw.get('scoped_window_modes') or ['rollback', 'tighten']) if str(x).strip()],
            'scope_priority': [str(x).strip() for x in (raw.get('scope_priority') or ['symbol_regime', 'symbol', 'regime', 'global']) if str(x).strip()],
        }

    def _build_close_outcome_guard(self, symbol: str, plan_context: Dict[str, Any] = None) -> Dict[str, Any]:
        cfg = self._get_close_outcome_guard_config()
        regime_snapshot = (plan_context or {}).get('regime_snapshot') if isinstance(plan_context, dict) else {}
        current_regime = str((regime_snapshot or {}).get('name') or (regime_snapshot or {}).get('regime') or '').strip() or None
        result = {
            'enabled': bool(cfg.get('enabled')),
            'passed': True,
            'mode': 'observe',
            'reason': None,
            'action': 'observe_only_followup',
            'route': 'observe_only_followup',
            'freeze_auto_promotion': False,
            'trade_count': 0,
            'min_sample_size': int(cfg.get('min_sample_size') or 0),
            'risk_budget_overrides': {},
            'feedback_loop': {},
            'digest': {},
            'scope_window': {},
            'scope_windows': {},
            'scope_context': {'symbol': symbol, 'regime_tag': current_regime},
            'config': cfg,
        }
        if not result['enabled']:
            result['reason'] = 'disabled'
            return result
        recent_trades = self.db.get_recent_close_outcome_trades(limit=cfg['lookback_limit']) if self.db else []
        digest = self.db.get_close_outcome_digest(symbol=symbol, limit=cfg['lookback_limit']) if self.db else {}
        feedback = build_close_outcome_feedback_loop(digest, label=f'close_outcome_risk_guard:{symbol}')
        scope_windows = build_close_outcome_scope_windows(recent_trades, config=cfg, label=f'close_outcome_scope_guard:{symbol}') if recent_trades else {'active_windows': [], 'windows': [], 'scope_priority': cfg.get('scope_priority') or []}
        active_windows = list(scope_windows.get('active_windows') or [])
        active_scope = None
        has_symbol_regime_windows_for_symbol = any(window.get('scope') == 'symbol_regime' and window.get('symbol') == symbol for window in active_windows)
        has_regime_windows_for_regime = bool(current_regime) and any(window.get('scope') == 'regime' and window.get('regime_tag') == current_regime for window in active_windows)
        for window in active_windows:
            if window.get('scope') == 'symbol_regime' and window.get('symbol') == symbol and window.get('regime_tag') == current_regime:
                active_scope = window
                break
        if active_scope is None and not has_symbol_regime_windows_for_symbol:
            for window in active_windows:
                if window.get('scope') == 'symbol' and window.get('symbol') == symbol:
                    active_scope = window
                    break
        if active_scope is None:
            for window in active_windows:
                if window.get('scope') == 'regime' and current_regime and window.get('regime_tag') == current_regime:
                    active_scope = window
                    break
        if active_scope is None and not has_symbol_regime_windows_for_symbol and not has_regime_windows_for_regime:
            for window in active_windows:
                if window.get('scope') == 'global':
                    active_scope = window
                    break
        effective_feedback = dict(active_scope.get('feedback_loop') or feedback) if active_scope else feedback
        effective_digest = dict(active_scope.get('digest') or digest) if active_scope else digest
        trade_count = int(effective_feedback.get('trade_count') or effective_digest.get('trade_count') or 0)
        mode = str(effective_feedback.get('governance_mode') or 'observe').strip().lower() or 'observe'
        hint = effective_feedback.get('orchestration_hint') or {}
        next_action = effective_feedback.get('next_action') or {}
        result.update({
            'mode': mode,
            'action': next_action.get('action') or hint.get('action') or 'observe_only_followup',
            'route': next_action.get('route') or hint.get('route') or 'observe_only_followup',
            'freeze_auto_promotion': bool(hint.get('freeze_auto_promotion', False)),
            'trade_count': trade_count,
            'feedback_loop': effective_feedback,
            'digest': effective_digest,
            'scope_window': dict(active_scope or {}),
            'scope_windows': dict(scope_windows or {}),
        })
        effective_min_sample_size = int((active_scope or {}).get('min_sample_size') or cfg['min_sample_size'] or 0)
        result['min_sample_size'] = effective_min_sample_size
        if trade_count < effective_min_sample_size:
            result['reason'] = 'sample_too_small'
            return result
        should_block = mode in set(cfg['block_modes'] or [])
        if should_block and cfg.get('require_freeze_for_block', True):
            should_block = bool(result['freeze_auto_promotion'])
        scope_suffix = ''
        if result.get('scope_window'):
            scope_suffix = f":{result['scope_window'].get('scope')}:{result['scope_window'].get('scope_key')}"
        if should_block:
            result['passed'] = False
            result['reason'] = f'close_outcome_guard_block:{mode}{scope_suffix}'
            return result
        if mode in set(cfg['tighten_modes'] or []):
            result['reason'] = f'close_outcome_guard_tighten:{mode}{scope_suffix}'
            result['risk_budget_overrides'] = {
                'base_entry_margin_ratio': max(cfg['floor_entry_margin_ratio'], float(cfg['tighten_entry_ratio_multiplier']) * 1.0),
                'total_margin_cap_ratio': max(cfg['floor_total_margin_cap_ratio'], float(cfg['tighten_total_margin_cap_multiplier']) * 1.0),
                'symbol_margin_cap_ratio': max(cfg['floor_symbol_margin_cap_ratio'], float(cfg['tighten_symbol_margin_cap_multiplier']) * 1.0),
                'leverage_cap_multiplier': float(cfg['tighten_leverage_cap_multiplier']),
                'floor_leverage_cap': int(cfg['floor_leverage_cap']),
            }
        else:
            result['reason'] = f'close_outcome_guard_observe:{mode}{scope_suffix}'
        return result

    def _apply_close_outcome_budget_overrides(self, risk_budget: Dict[str, Any], guard: Dict[str, Any]) -> Dict[str, Any]:
        adjusted = dict(risk_budget or {})
        overrides = dict((guard or {}).get('risk_budget_overrides') or {})
        if not overrides:
            return adjusted
        if adjusted.get('base_entry_margin_ratio') is not None:
            adjusted['base_entry_margin_ratio'] = min(float(adjusted.get('base_entry_margin_ratio') or 0.0), float(adjusted.get('base_entry_margin_ratio') or 0.0) * float(overrides.get('base_entry_margin_ratio', 1.0)))
        if adjusted.get('total_margin_cap_ratio') is not None:
            adjusted['total_margin_cap_ratio'] = min(float(adjusted.get('total_margin_cap_ratio') or 0.0), float(adjusted.get('total_margin_cap_ratio') or 0.0) * float(overrides.get('total_margin_cap_ratio', 1.0)))
        if adjusted.get('symbol_margin_cap_ratio') is not None:
            adjusted['symbol_margin_cap_ratio'] = min(float(adjusted.get('symbol_margin_cap_ratio') or 0.0), float(adjusted.get('symbol_margin_cap_ratio') or 0.0) * float(overrides.get('symbol_margin_cap_ratio', 1.0)))
        current_leverage_cap = adjusted.get('leverage_cap')
        if current_leverage_cap is not None:
            adjusted['leverage_cap'] = max(int(overrides.get('floor_leverage_cap') or 1), int(float(current_leverage_cap or 0) * float(overrides.get('leverage_cap_multiplier', 1.0))))
        elif self.trading_config.get('leverage') is not None:
            adjusted['leverage_cap'] = max(int(overrides.get('floor_leverage_cap') or 1), int(float(self.trading_config.get('leverage') or 1) * float(overrides.get('leverage_cap_multiplier', 1.0))))
        adjusted['close_outcome_guard_applied'] = True
        adjusted['close_outcome_guard_mode'] = guard.get('mode')
        return adjusted

    def _sync_loss_streak_guard(self) -> Dict[str, Any]:
        state = self.db.get_risk_guard_state('loss_streak')
        now = datetime.now()
        changed = False
        just_triggered = False
        auto_recovered = False

        trades = self.db.get_trades(status='closed', limit=200)
        new_trades = [t for t in reversed(trades) if int(t.get('id', 0) or 0) > int(state.get('last_trade_id', 0) or 0)]
        for trade in new_trades:
            state['last_trade_id'] = int(trade.get('id', 0) or state.get('last_trade_id', 0) or 0)
            pnl = trade.get('pnl')
            if pnl is None:
                changed = True
                continue
            pnl_value = float(pnl or 0)
            if pnl_value < 0:
                state['current_streak'] = int(state.get('current_streak', 0) or 0) + 1
                state.setdefault('details', {})['last_loss_at'] = trade.get('close_time') or trade.get('open_time')
            else:
                state['current_streak'] = 0
                state.setdefault('details', {})['last_win_at'] = trade.get('close_time') or trade.get('open_time')
                if state.get('lock_active'):
                    state['lock_active'] = 0
                    state['lock_until'] = None
                    state['triggered_at'] = None
            changed = True

        lock_until = state.get('lock_until')
        if state.get('lock_active') and lock_until:
            try:
                lock_dt = datetime.fromisoformat(str(lock_until))
            except Exception:
                lock_dt = None
            if lock_dt and now >= lock_dt:
                state['lock_active'] = 0
                state['lock_until'] = None
                state['triggered_at'] = None
                state['current_streak'] = 0
                state['reset_at'] = now.isoformat()
                auto_recovered = True
                changed = True

        max_consecutive_losses = int(self.trading_config.get('max_consecutive_losses', 3))
        if self._loss_guard_enabled() and not state.get('lock_active') and int(state.get('current_streak', 0) or 0) >= max_consecutive_losses:
            state['lock_active'] = 1
            state['triggered_at'] = now.isoformat()
            state['lock_until'] = (now + timedelta(hours=self._loss_guard_hours())).isoformat()
            state.setdefault('details', {})['max_consecutive_losses'] = max_consecutive_losses
            just_triggered = True
            changed = True

        if changed:
            self.db.save_risk_guard_state(state)
        state['just_triggered'] = just_triggered
        state['auto_recovered'] = auto_recovered
        return state

    def manual_reset_loss_streak(self, note: str = None) -> Dict[str, Any]:
        """手动清零连亏熔断状态，支持幂等调用"""
        state = self.db.get_risk_guard_state('loss_streak')
        
        # Idempotency: if already unlocked, return current state without re-recording
        already_unlocked = not bool(state.get('lock_active', 0))
        if already_unlocked:
            return {
                **state,
                'idempotent': True,
                'message': 'already_unlocked',
                'action': 'no_change'
            }
        
        # Perform reset
        state['current_streak'] = 0
        state['lock_active'] = 0
        state['lock_until'] = None
        state['triggered_at'] = None
        state['reset_at'] = datetime.now().isoformat()
        details = state.get('details', {}) or {}
        if note:
            details['manual_reset_note'] = note
        state['details'] = details
        self.db.save_risk_guard_state(state)
        
        return {
            **state,
            'idempotent': False,
            'message': 'reset_completed',
            'action': 'reset'
        }

    def can_open_position(self, symbol: str, side: str = None, signal_id: int = None, plan_context: Dict[str, Any] = None, *, signal: Any = None) -> tuple:
        """检查是否可以开仓"""
        details = {}
        normalized_side = side or 'long'
        adaptive_risk_snapshot: Optional[Dict[str, Any]] = None

        # Build adaptive risk snapshot early so it flows into executor_probe guards
        if hasattr(self, '_build_adaptive_risk_snapshot'):
            adaptive_risk_snapshot = self._build_adaptive_risk_snapshot(symbol, plan_context=plan_context, signal=signal)

        executor_probe = TradingExecutor(self.config, None, self.db)
        runtime_ok, runtime_reason, runtime_details = executor_probe._check_layering_runtime_guards(symbol, normalized_side, signal_id=signal_id, plan_context=plan_context or {})
        if not runtime_ok:
            details['hard_intercept'] = {'passed': False, 'reason': runtime_reason, 'details': runtime_details}
            details['observability'] = dict((runtime_details or {}).get('observability') or {})
            details['adaptive_risk_snapshot'] = dict(adaptive_risk_snapshot or {})
            return False, runtime_reason, details

        lock_symbol, lock_side = executor_probe._get_direction_lock_scope_key(symbol, normalized_side)
        direction_lock = self.db.get_direction_lock(lock_symbol, lock_side)
        if direction_lock:
            details['hard_intercept'] = {'passed': False, 'reason': '方向锁占用中', 'lock': direction_lock}
            details['observability'] = enrich_observability_with_snapshots(self.config, symbol, build_observability_context(symbol=symbol, side=normalized_side, signal_id=signal_id, root_signal_id=signal_id, deny_reason='direction_lock'), plan_context=plan_context)
            details['adaptive_risk_snapshot'] = dict(adaptive_risk_snapshot or {})
            return False, '方向锁占用中', details
        details['hard_intercept'] = {'passed': True, 'lock_scope': {'symbol': lock_symbol, 'side': lock_side}}

        max_trades = int(self.trading_config.get('max_trades_per_day', 10))
        today_trades = self._get_today_trade_count()
        if today_trades >= max_trades:
            details['daily_limit'] = {'passed': False, 'reason': f'已达每日交易上限({today_trades})'}
            return False, f"已达每日交易上限({today_trades}/{max_trades})", details
        details['daily_limit'] = {'passed': True, 'count': today_trades, 'max': max_trades}

        min_interval = int(self.trading_config.get('min_trade_interval', 300))
        last_trade = self._get_last_trade_time()
        if last_trade:
            diff_seconds = (datetime.utcnow() - last_trade).total_seconds()
            if diff_seconds < min_interval:
                details['global_cooldown'] = {'passed': False, 'remaining': int(min_interval - diff_seconds)}
                details['adaptive_risk_snapshot'] = dict(adaptive_risk_snapshot or {})
                return False, f"全局冷却中({int(diff_seconds)}s)", details
        details['global_cooldown'] = {'passed': True}

        max_consecutive_losses = int(self.trading_config.get('max_consecutive_losses', 3))
        loss_guard = self._sync_loss_streak_guard()
        consecutive_losses = int(loss_guard.get('current_streak', 0) or 0)
        if loss_guard.get('lock_active'):
            details['loss_streak_limit'] = {
                'passed': False,
                'current': consecutive_losses,
                'max': max_consecutive_losses,
                'locked': True,
                'recover_at': loss_guard.get('lock_until'),
                'triggered_at': loss_guard.get('triggered_at'),
                'cooldown_hours': self._loss_guard_hours(),
                'just_triggered': bool(loss_guard.get('just_triggered')),
                'auto_recovered': bool(loss_guard.get('auto_recovered')),
            }
            details['adaptive_risk_snapshot'] = dict(adaptive_risk_snapshot or {})
            return False, f"连续亏损熔断冷却中({consecutive_losses}/{max_consecutive_losses})", details
        details['loss_streak_limit'] = {
            'passed': True,
            'current': consecutive_losses,
            'max': max_consecutive_losses,
            'locked': False,
            'recover_at': loss_guard.get('lock_until'),
            'triggered_at': loss_guard.get('triggered_at'),
            'cooldown_hours': self._loss_guard_hours(),
            'just_triggered': bool(loss_guard.get('just_triggered')),
            'auto_recovered': bool(loss_guard.get('auto_recovered')),
        }

        max_daily_drawdown = float(self.trading_config.get('max_daily_drawdown', 0.03))
        daily_drawdown_ratio = self._get_daily_drawdown_ratio()
        if daily_drawdown_ratio >= max_daily_drawdown:
            details['daily_drawdown_limit'] = {
                'passed': False,
                'current': round(daily_drawdown_ratio, 4),
                'max': max_daily_drawdown
            }
            details['adaptive_risk_snapshot'] = dict(adaptive_risk_snapshot or {})
            return False, f"日内回撤熔断({daily_drawdown_ratio*100:.2f}%/{max_daily_drawdown*100:.2f}%)", details
        details['daily_drawdown_limit'] = {
            'passed': True,
            'current': round(daily_drawdown_ratio, 4),
            'max': max_daily_drawdown
        }

        close_outcome_guard = self._build_close_outcome_guard(symbol, plan_context=plan_context)
        details['close_outcome_guard'] = {
            'passed': bool(close_outcome_guard.get('passed', True)),
            'enabled': bool(close_outcome_guard.get('enabled', False)),
            'mode': close_outcome_guard.get('mode'),
            'reason': close_outcome_guard.get('reason'),
            'action': close_outcome_guard.get('action'),
            'route': close_outcome_guard.get('route'),
            'freeze_auto_promotion': bool(close_outcome_guard.get('freeze_auto_promotion', False)),
            'trade_count': int(close_outcome_guard.get('trade_count', 0) or 0),
            'min_sample_size': int(close_outcome_guard.get('min_sample_size', 0) or 0),
            'policy_hints': list(((close_outcome_guard.get('feedback_loop') or {}).get('policy_hints') or [])),
            'reason_codes': list(((close_outcome_guard.get('feedback_loop') or {}).get('reason_codes') or [])),
            'recent_closes': list(((close_outcome_guard.get('digest') or {}).get('recent_closes') or [])),
            'risk_budget_overrides': dict(close_outcome_guard.get('risk_budget_overrides') or {}),
            'scope_context': dict(close_outcome_guard.get('scope_context') or {}),
            'scope_window': dict(close_outcome_guard.get('scope_window') or {}),
            'active_scope_windows': list(((close_outcome_guard.get('scope_windows') or {}).get('active_windows') or [])),
            'feedback_loop': dict(close_outcome_guard.get('feedback_loop') or {}),
            'digest': dict(close_outcome_guard.get('digest') or {}),
        }
        if not close_outcome_guard.get('passed', True):
            details['adaptive_risk_snapshot'] = dict(adaptive_risk_snapshot or {})
            return False, f"平仓反馈风控阻止开仓({close_outcome_guard.get('mode') or 'guarded'})", details

        adaptive_risk_snapshot = build_risk_effective_snapshot(
            self.config,
            symbol,
            regime_snapshot=(plan_context or {}).get('regime_snapshot'),
            policy_snapshot=(plan_context or {}).get('adaptive_policy_snapshot'),
        )
        risk_budget = dict(adaptive_risk_snapshot.get('enforced_budget') or get_risk_budget_config(self.config, symbol))
        baseline_risk_budget = dict(risk_budget)
        risk_budget = self._apply_close_outcome_budget_overrides(risk_budget, close_outcome_guard)
        configured_leverage = int(self.trading_config.get('leverage', 10))

        leverage_cap = risk_budget.get('leverage_cap')
        planned_leverage = min(configured_leverage, int(leverage_cap)) if leverage_cap else configured_leverage
        effective_leverage = planned_leverage
        if self._exchange:
            try:
                exchange_leverage = self._exchange.get_actual_leverage(symbol) if hasattr(self._exchange, 'get_actual_leverage') else planned_leverage
                effective_leverage = min(planned_leverage, int(exchange_leverage or planned_leverage))
            except Exception:
                pass

        balance = self._get_balance_summary()
        total_balance = float(balance.get('total', 0) or 0)
        free_balance = float(balance.get('free', 0) or 0)
        pending_intents = self.db.get_active_open_intents()
        positions_usage = summarize_margin_usage(self.db.get_positions(), symbol, pending_intents=pending_intents)
        layering = self.config.get_layering_config(symbol) if hasattr(self.config, 'get_layering_config') else dict(DEFAULT_LAYERING_CONFIG)
        default_ratios = layering.get('layer_ratios') or [0.06, 0.06, 0.04]
        layer_plan = plan_context or {
            'eligible': True,
            'layer_no': 1,
            'layer_ratio': float(default_ratios[0]),
            'root_signal_id': signal_id,
        }
        if plan_context is None:
            state = self.db.get_layer_plan_state(symbol, normalized_side)
            plan_data = dict(state.get('plan_data') or {})
            layer_ratios = plan_data.get('layer_ratios') or layering.get('layer_ratios') or [0.06, 0.06, 0.04]
            filled = sorted(int(x) for x in (plan_data.get('filled_layers') or []))
            pending = sorted(int(x) for x in (plan_data.get('pending_layers') or []))
            consumed = set(filled) | set(pending)
            next_layer = 1
            while next_layer in consumed:
                next_layer += 1
            if next_layer > len(layer_ratios):
                details['layer_eligibility'] = {'passed': False, 'reason': '分仓计划已达最大层数', 'state': state}
                details['observability'] = enrich_observability_with_snapshots(self.config, symbol, build_observability_context(symbol=symbol, side=normalized_side, signal_id=signal_id, root_signal_id=state.get('root_signal_id') or signal_id, deny_reason='layer_limit'), plan_context=plan_context)
                return False, '分仓计划已达最大层数', details
            expected = len(consumed) + 1
            if next_layer != expected:
                details['layer_eligibility'] = {'passed': False, 'reason': '检测到分仓层级断档，禁止跳层', 'state': state}
                details['observability'] = enrich_observability_with_snapshots(self.config, symbol, build_observability_context(symbol=symbol, side=normalized_side, signal_id=signal_id, root_signal_id=state.get('root_signal_id') or signal_id, deny_reason='layer_gap'), plan_context=plan_context)
                return False, '检测到分仓层级断档，禁止跳层', details
            layer_plan = {
                'eligible': True,
                'layer_no': next_layer,
                'layer_ratio': float(layer_ratios[next_layer - 1]),
                'root_signal_id': state.get('root_signal_id') or signal_id,
                'layer_ratios': layer_ratios,
                'max_total_ratio': float(plan_data.get('max_total_ratio') or sum(layer_ratios) or 0.16),
                'state': state,
            }
        details['layer_eligibility'] = {'passed': True, 'layer_plan': layer_plan}
        entry_plan = compute_entry_plan(
            total_balance=total_balance,
            free_balance=free_balance,
            current_total_margin=float(positions_usage.get('current_total_margin') or 0.0),
            current_symbol_margin=float(positions_usage.get('current_symbol_margin') or 0.0),
            risk_budget=risk_budget,
            requested_entry_ratio=float(layer_plan.get('layer_ratio') or 0.0),
            strict_requested_ratio=True,
        )
        current_exposure = float(entry_plan.get('current_total_exposure_ratio') or 0.0)
        projected_exposure = float(entry_plan.get('projected_total_exposure_ratio') or current_exposure)
        projected_symbol = float(entry_plan.get('projected_symbol_exposure_ratio') or 0.0)
        position_ratio = float(entry_plan.get('effective_entry_margin_ratio') or 0.0)
        max_exposure = float(risk_budget.get('total_margin_cap_ratio', 0.3))
        max_symbol = float(risk_budget.get('symbol_margin_cap_ratio', 0.15))

        observability = build_observability_context(
            symbol=symbol,
            side=normalized_side,
            signal_id=signal_id,
            root_signal_id=layer_plan.get('root_signal_id') or signal_id,
            layer_no=layer_plan.get('layer_no'),
            current_symbol_exposure=entry_plan.get('current_symbol_exposure_ratio'),
            projected_symbol_exposure=entry_plan.get('projected_symbol_exposure_ratio'),
            current_total_exposure=entry_plan.get('current_total_exposure_ratio'),
            projected_total_exposure=entry_plan.get('projected_total_exposure_ratio'),
        )
        details['observability'] = enrich_observability_with_snapshots(self.config, symbol, observability, plan_context=plan_context)
        details['adaptive_risk_snapshot'] = dict(details['observability'].get('adaptive_risk_snapshot') or adaptive_risk_snapshot or {})
        details['adaptive_risk_hints'] = dict(details['observability'].get('adaptive_risk_hints') or {})
        trade_logger.info(
            f"风控检查 {symbol}: 当前暴露:{current_exposure*100:.1f}%, "
            f"计划仓位:{position_ratio*100:.1f}%, 配置杠杆:{configured_leverage}x, 实际杠杆:{effective_leverage}x, "
            f"预计总暴露:{projected_exposure*100:.1f}%, 上限:{max_exposure*100:.0f}% | obs={observability_log_text(observability)}"
        )

        if entry_plan.get('blocked'):
            details['observability'] = enrich_observability_with_snapshots(self.config, symbol, build_observability_context(symbol=symbol, side=normalized_side, signal_id=signal_id, root_signal_id=layer_plan.get('root_signal_id') or signal_id, layer_no=layer_plan.get('layer_no'), deny_reason=entry_plan.get('block_reason') or 'risk_budget', current_symbol_exposure=entry_plan.get('current_symbol_exposure_ratio'), projected_symbol_exposure=entry_plan.get('projected_symbol_exposure_ratio'), current_total_exposure=entry_plan.get('current_total_exposure_ratio'), projected_total_exposure=entry_plan.get('projected_total_exposure_ratio')), plan_context=plan_context)
            details['exposure_limit'] = {
                'passed': False,
                'current': round(current_exposure, 4),
                'projected': round(projected_exposure, 4),
                'projected_symbol': round(projected_symbol, 4),
                'max': max_exposure,
                'max_symbol': max_symbol,
                'planned_leverage': configured_leverage,
                'effective_leverage': effective_leverage,
                'position_ratio': position_ratio,
                'entry_plan': entry_plan,
            }
            return False, entry_plan.get('block_reason') or '风险预算不足', details
        details['exposure_limit'] = {
            'passed': True,
            'current': round(current_exposure, 4),
            'projected': round(projected_exposure, 4),
            'projected_symbol': round(projected_symbol, 4),
            'max': max_exposure,
            'max_symbol': max_symbol,
            'planned_leverage': planned_leverage,
            'configured_leverage': configured_leverage,
            'effective_leverage': effective_leverage,
            'leverage_cap': leverage_cap,
            'position_ratio': position_ratio,
            'entry_plan': entry_plan,
            'baseline_risk_budget': baseline_risk_budget,
            'close_outcome_adjusted_risk_budget': risk_budget,
        }

        return True, None, details

    def get_risk_status(self) -> Dict[str, Any]:
        """供仪表盘显示的风险状态"""
        balance = self._get_balance_summary()
        current_exposure = self._get_current_exposure()
        daily_drawdown = self._get_daily_drawdown_ratio()
        loss_guard = self._sync_loss_streak_guard()
        consecutive_losses = int(loss_guard.get('current_streak', 0) or 0)
        status = 'locked' if loss_guard.get('lock_active') else ('guarded' if (daily_drawdown > 0 or consecutive_losses > 0) else 'normal')
        risk_budget = get_risk_budget_config(self.config)
        positions_usage = summarize_margin_usage(self.db.get_positions(), symbol='__portfolio__')
        portfolio_entry_plan = compute_entry_plan(
            total_balance=float(balance.get('total', 0) or 0),
            free_balance=float(balance.get('free', 0) or 0),
            current_total_margin=float(positions_usage.get('current_total_margin') or 0.0),
            current_symbol_margin=0.0,
            risk_budget=risk_budget,
        )
        return {
            'today_trades': self._get_today_trade_count(),
            'last_trade_time': self._get_last_trade_time().isoformat() if self._get_last_trade_time() else None,
            'current_exposure': round(current_exposure, 4),
            'max_exposure': float(risk_budget.get('total_margin_cap_ratio', 0.3)),
            'soft_exposure': float(risk_budget.get('total_margin_soft_cap_ratio', 0.25)),
            'position_size': float(risk_budget.get('base_entry_margin_ratio', 0.08)),
            'daily_drawdown': round(daily_drawdown, 4),
            'max_daily_drawdown': float(self.trading_config.get('max_daily_drawdown', 0.03)),
            'consecutive_losses': consecutive_losses,
            'max_consecutive_losses': int(self.trading_config.get('max_consecutive_losses', 3)),
            'loss_streak_lock_enabled': self._loss_guard_enabled(),
            'loss_streak_cooldown_hours': self._loss_guard_hours(),
            'loss_streak_locked': bool(loss_guard.get('lock_active')),
            'loss_streak_recover_at': loss_guard.get('lock_until'),
            'loss_streak_triggered_at': loss_guard.get('triggered_at'),
            'balance': balance,
            'status': status,
            'risk_budget': risk_budget,
            'portfolio_entry_plan': portfolio_entry_plan,
        }

    def _get_balance_summary(self) -> Dict[str, float]:
        try:
            from core.exchange import Exchange
            if self._exchange is None:
                self._exchange = Exchange(self.config.all)
            balance = self._exchange.fetch_balance()
            total = float(balance.get('total', {}).get('USDT', 0) or 0)
            free = float(balance.get('free', {}).get('USDT', 0) or 0)
            used = max(0.0, total - free)
            return {'total': round(total, 2), 'free': round(free, 2), 'used': round(used, 2)}
        except Exception:
            return {'total': 0.0, 'free': 0.0, 'used': 0.0}

    def _parse_trade_time(self, value: str) -> Optional[datetime]:
        if not value:
            return None
        return datetime.fromisoformat(value)

    def _get_today_trade_count(self) -> int:
        trades = self.db.get_trades(limit=1000)
        today = datetime.utcnow().date()
        count = 0
        for trade in trades:
            opened_at = self._parse_trade_time(trade.get('open_time', ''))
            if opened_at and opened_at.date() == today:
                count += 1
        return count

    def _get_last_trade_time(self) -> Optional[datetime]:
        return self.db.get_latest_trade_time()

    def _get_consecutive_losses(self) -> int:
        trades = self.db.get_trades(status='closed', limit=20)
        count = 0
        for trade in trades:
            pnl = float(trade.get('pnl', 0) or 0)
            if pnl < 0:
                count += 1
            else:
                break
        return count

    def _get_daily_drawdown_ratio(self) -> float:
        trades = self.db.get_trades(status='closed', limit=1000)
        today = datetime.utcnow().date()
        today_pnl = 0.0
        for trade in trades:
            realized_at = self._parse_trade_time(trade.get('close_time') or trade.get('open_time') or '')
            if realized_at and realized_at.date() == today:
                today_pnl += float(trade.get('pnl', 0) or 0)
        if today_pnl >= 0:
            return 0.0
        balance_total = self._get_balance_summary().get('total', 0.0) or 1.0
        return abs(today_pnl) / balance_total

    def _get_balance_summary(self) -> Dict[str, float]:
        try:
            from core.exchange import Exchange
            if self._exchange is None:
                self._exchange = Exchange(self.config.all)
            balance = self._exchange.fetch_balance()
            total = float(balance.get('total', {}).get('USDT', 0) or 0)
            free = float(balance.get('free', {}).get('USDT', 0) or 0)
            used = max(0.0, total - free)
            return {'total': round(total, 2), 'free': round(free, 2), 'used': round(used, 2)}
        except Exception:
            return {'total': 0.0, 'free': 0.0, 'used': 0.0}

    def _get_current_exposure(self) -> float:
        positions = self.db.get_positions()
        total_balance = self._get_balance_summary().get('total', 0.0) or 1.0
        total_margin_used = 0.0
        for p in positions:
            qty = float(p.get('coin_quantity', 0) or 0)
            px = float(p.get('current_price', 0) or p.get('entry_price', 0) or 0)
            lev = max(1, int(p.get('leverage', 1) or 1))
            total_margin_used += (qty * px) / lev if qty and px else 0.0
        return total_margin_used / total_balance if total_balance > 0 else 0.0

    def _parse_trade_time(self, value: str) -> Optional[datetime]:
        if not value:
            return None
        return datetime.fromisoformat(value)

    def _get_today_trade_count(self) -> int:
        trades = self.db.get_trades(limit=1000)
        today = datetime.utcnow().date()
        count = 0
        for trade in trades:
            opened_at = self._parse_trade_time(trade.get('open_time', ''))
            if opened_at and opened_at.date() == today:
                count += 1
        return count

    def _get_last_trade_time(self) -> Optional[datetime]:
        return self.db.get_latest_trade_time()

    def _get_consecutive_losses(self) -> int:
        trades = self.db.get_trades(status='closed', limit=20)
        count = 0
        for trade in trades:
            pnl = float(trade.get('pnl', 0) or 0)
            if pnl < 0:
                count += 1
            else:
                break
        return count

    def _get_daily_drawdown_ratio(self) -> float:
        trades = self.db.get_trades(status='closed', limit=1000)
        today = datetime.utcnow().date()
        today_pnl = 0.0
        for trade in trades:
            realized_at = self._parse_trade_time(trade.get('close_time') or trade.get('open_time') or '')
            if realized_at and realized_at.date() == today:
                today_pnl += float(trade.get('pnl', 0) or 0)
        if today_pnl >= 0:
            return 0.0
        balance_total = self._get_balance_summary().get('total', 0.0) or 1.0
        return abs(today_pnl) / balance_total

    def _get_current_exposure(self) -> float:
        positions = self.db.get_positions()
        total_balance = self._get_balance_summary().get('total', 0.0) or 1.0
        total_margin_used = 0.0
        for p in positions:
            qty = float(p.get('coin_quantity', 0) or 0)
            px = float(p.get('current_price', 0) or p.get('entry_price', 0) or 0)
            lev = max(1, int(p.get('leverage', 1) or 1))
            total_margin_used += (qty * px) / lev if qty and px else 0.0
        return total_margin_used / total_balance if total_balance > 0 else 0.0
