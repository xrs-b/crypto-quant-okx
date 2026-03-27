import copy
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from analytics.backtest import build_governance_workflow_ready_payload, build_regime_policy_calibration_report
from analytics.helper import attach_auto_approval_policy, build_workflow_approval_records, execute_rollout_executor, merge_persisted_approval_state, build_workflow_consumer_view, build_workbench_governance_detail_view
from core.config import Config, DEFAULT_ADAPTIVE_REGIME_CONFIG
from core.database import Database
from core.regime import build_regime_snapshot, normalize_regime_snapshot
from core.regime_policy import resolve_regime_policy, build_risk_effective_snapshot, build_execution_effective_snapshot
from signals import Signal, EntryDecider, SignalValidator

SUPPORTED_CASE_TYPES = {"shadow_signal", "shadow_execution", "shadow_workflow"}
SUPPORTED_MODES = {"baseline", "adaptive", "guarded_execute", "decision_only", "observe_only", "full", "workflow", "workflow_dry_run", "validation_replay"}

@dataclass
class ValidationCase:
    raw: Dict[str, Any]

    @property
    def case_id(self) -> str:
        return str(self.raw.get("case_id") or "unknown-case")

    @property
    def case_type(self) -> str:
        return str(self.raw.get("case_type") or "shadow_execution")

class ValidationCaseError(ValueError):
    pass

class ShadowConfig:
    def __init__(self, base: Config = None, overrides: Dict[str, Any] = None):
        self._base = base or Config()
        self._config = copy.deepcopy(getattr(self._base, "all", {}) or {})
        if overrides:
            self._deep_merge_inplace(self._config, overrides)
        self.all = self._config
        self.db_path = getattr(self._base, "db_path", "data/trading.db")
        self.exchange_mode = str(self.get('exchange.mode', 'testnet') or 'testnet')
        self.position_mode = str(self.get('trading.position_mode', 'hedge') or 'hedge')
        self.symbols = list(self.get('trading.watch_list', []) or [])

    def _deep_merge_inplace(self, base: Dict[str, Any], override: Dict[str, Any]):
        for key, value in (override or {}).items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                self._deep_merge_inplace(base[key], value)
            else:
                base[key] = copy.deepcopy(value)

    def get(self, key: str, default=None):
        value = self._config
        for part in (key or '').split('.'):
            if not part:
                continue
            if not isinstance(value, dict):
                return default
            value = value.get(part)
            if value is None:
                return default
        return value

    def get_symbol_value(self, symbol: str, key: str, default=None):
        overrides = ((self._config.get('symbol_overrides') or {}).get(symbol) or {}) if isinstance(self._config, dict) else {}
        value = overrides
        for part in (key or '').split('.'):
            if not part:
                continue
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
            if value is None:
                break
        if value is not None:
            return value
        return self.get(key, default)

    def get_symbol_overrides(self, symbol: str) -> Dict[str, Any]:
        overrides = self._config.get('symbol_overrides') or {}
        if not isinstance(overrides, dict):
            return {}
        return copy.deepcopy(overrides.get(symbol) or {})

    def get_symbol_section(self, symbol: str, key: str) -> Dict[str, Any]:
        base_section = copy.deepcopy(self.get(key, {}) or {})
        overrides = self.get_symbol_overrides(symbol)
        override_section = overrides
        for part in (key or '').split('.'):
            if not part:
                continue
            if not isinstance(override_section, dict):
                override_section = {}
                break
            override_section = override_section.get(part)
            if override_section is None:
                override_section = {}
                break
        if isinstance(override_section, dict):
            self._deep_merge_inplace(base_section, override_section)
        return base_section

    def get_adaptive_regime_config(self, symbol: str = None) -> Dict[str, Any]:
        merged = copy.deepcopy(DEFAULT_ADAPTIVE_REGIME_CONFIG)
        self._deep_merge_inplace(merged, self._config.get('adaptive_regime') or {})
        if symbol:
            symbol_adaptive = ((self._config.get('symbol_overrides') or {}).get(symbol) or {}).get('adaptive_regime') or {}
            if isinstance(symbol_adaptive, dict):
                self._deep_merge_inplace(merged, symbol_adaptive)
        return merged

def _load_case_raw(path: str) -> Dict[str, Any]:
    content = Path(path).read_text(encoding="utf-8")
    suffix = Path(path).suffix.lower()
    if suffix in {".yaml", ".yml"}:
        data = yaml.safe_load(content) or {}
    else:
        data = json.loads(content)
    if not isinstance(data, dict):
        raise ValidationCaseError("validation case must be an object")
    return data

def load_validation_case(path: str) -> ValidationCase:
    data = _load_case_raw(path)
    case = ValidationCase(raw=data)
    _validate_case(case.raw)
    return case

def collect_validation_case_paths(paths: Iterable[str]) -> List[str]:
    collected: List[str] = []
    for raw_path in paths or []:
        path = Path(raw_path)
        if path.is_dir():
            for child in sorted(path.rglob('*')):
                if child.is_file() and child.suffix.lower() in {'.yaml', '.yml', '.json'}:
                    collected.append(str(child))
        elif path.is_file():
            collected.append(str(path))
        else:
            raise ValidationCaseError(f"validation path not found: {raw_path}")
    deduped = []
    seen = set()
    for item in collected:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    if not deduped:
        raise ValidationCaseError("no validation cases found")
    return deduped

def _validate_case(data: Dict[str, Any]):
    if not data.get("case_id"):
        raise ValidationCaseError("case_id is required")
    case_type = str(data.get("case_type") or "")
    if case_type not in SUPPORTED_CASE_TYPES:
        raise ValidationCaseError(f"unsupported case_type: {case_type}")
    mode = str(data.get("mode") or "guarded_execute")
    if mode not in SUPPORTED_MODES:
        raise ValidationCaseError(f"unsupported mode: {mode}")
    if case_type in {"shadow_signal", "shadow_execution"}:
        signal = data.get("input", {}).get("signal") or {}
        if not signal.get("symbol"):
            raise ValidationCaseError("input.signal.symbol is required")
        if signal.get("signal_type") not in {"buy", "sell", "hold"}:
            raise ValidationCaseError("input.signal.signal_type must be buy/sell/hold")
    elif case_type == 'shadow_workflow':
        workflow_input = data.get('input') or {}
        if not (workflow_input.get('symbol_results') or workflow_input.get('report') or workflow_input.get('workflow_ready')):
            raise ValidationCaseError("shadow_workflow requires input.symbol_results, input.report, or input.workflow_ready")

def _build_signal(case_data: Dict[str, Any]) -> Signal:
    signal_input = copy.deepcopy(case_data.get("input", {}).get("signal") or {})
    market_context = signal_input.get("market_context") or {}
    regime_snapshot = signal_input.get("regime_snapshot")
    if regime_snapshot:
        normalized = normalize_regime_snapshot(regime_snapshot)
    else:
        regime_name = market_context.get("regime") or "unknown"
        normalized = build_regime_snapshot(
            regime=regime_name,
            confidence=float(market_context.get("regime_confidence", 0.0) or 0.0),
            indicators={
                "volatility": market_context.get("volatility"),
                "ema_direction": 1 if market_context.get("trend") == "bullish" else (-1 if market_context.get("trend") == "bearish" else 0),
            },
            details=market_context.get("regime_details") or "shadow validation case",
        )
    signal_input.setdefault("price", signal_input.get("current_price") or 0)
    signal_input.setdefault("strength", 0)
    signal_input.setdefault("strategies_triggered", [])
    signal_input.setdefault("reasons", [])
    signal_input.setdefault("market_context", market_context)
    signal_input.setdefault("regime_info", {"regime": normalized.get("regime", "unknown"), "confidence": normalized.get("confidence", 0.0)})
    signal_input["regime_snapshot"] = normalized
    signal_input.setdefault("adaptive_policy_snapshot", {})
    return Signal(**signal_input)

def _evaluate_with_config(cfg: ShadowConfig, signal: Signal) -> Dict[str, Any]:
    signal = copy.deepcopy(signal)
    regime_snapshot = getattr(signal, "regime_snapshot", None) or build_regime_snapshot("unknown", 0.0, {}, "shadow")
    policy_snapshot = resolve_regime_policy(cfg, signal.symbol, regime_snapshot)
    signal.regime_snapshot = regime_snapshot
    signal.adaptive_policy_snapshot = policy_snapshot
    signal.regime_info = signal.regime_info or {"regime": regime_snapshot.get("regime", "unknown"), "confidence": regime_snapshot.get("confidence", 0.0)}
    decision = EntryDecider(cfg.all).decide(signal)
    validator_passed, validator_reason, validator_details = SignalValidator(cfg, None).validate(signal)
    risk_snapshot = build_risk_effective_snapshot(cfg, signal.symbol, signal=signal, regime_snapshot=regime_snapshot, policy_snapshot=policy_snapshot)
    execution_snapshot = build_execution_effective_snapshot(cfg, signal.symbol, signal=signal, regime_snapshot=regime_snapshot, policy_snapshot=policy_snapshot)
    return {
        "signal": {"symbol": signal.symbol, "signal_type": signal.signal_type, "strength": signal.strength, "strategies_triggered": list(signal.strategies_triggered or [])},
        "regime_snapshot": regime_snapshot,
        "adaptive_policy_snapshot": policy_snapshot,
        "decision": decision.to_dict(),
        "validator": {"passed": validator_passed, "reason": validator_reason, "details": validator_details},
        "risk": {
            "effective_state": risk_snapshot.get("effective_state"), "observe_only": risk_snapshot.get("observe_only"),
            "baseline": risk_snapshot.get("baseline"), "effective": risk_snapshot.get("effective"),
            "enforced_budget": risk_snapshot.get("enforced_budget"), "would_tighten": risk_snapshot.get("would_tighten"),
            "would_tighten_fields": risk_snapshot.get("would_tighten_fields"), "enforced_fields": risk_snapshot.get("enforced_fields"),
            "hint_codes": risk_snapshot.get("hint_codes"),
        },
        "execution": {
            "effective_state": execution_snapshot.get("effective_state"), "observe_only": execution_snapshot.get("observe_only"),
            "baseline": execution_snapshot.get("baseline"), "effective": execution_snapshot.get("effective"), "live": execution_snapshot.get("live"),
            "enforced_profile": execution_snapshot.get("enforced_profile"), "would_tighten": execution_snapshot.get("would_tighten"),
            "would_tighten_fields": execution_snapshot.get("would_tighten_fields"), "enforced_fields": execution_snapshot.get("enforced_fields"),
            "execution_profile_really_enforced": execution_snapshot.get("execution_profile_really_enforced"),
            "layering_profile_really_enforced": execution_snapshot.get("layering_profile_really_enforced"),
            "plan_shape_really_enforced": execution_snapshot.get("plan_shape_really_enforced"), "hint_codes": execution_snapshot.get("hint_codes"),
        },
    }

def _build_diff(baseline: Dict[str, Any], adaptive: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "decision": {"baseline": baseline["decision"]["decision"], "adaptive": adaptive["decision"]["decision"], "changed": baseline["decision"]["decision"] != adaptive["decision"]["decision"], "score_delta": adaptive["decision"]["score"] - baseline["decision"]["score"]},
        "validator": {"baseline": baseline["validator"]["passed"], "adaptive": adaptive["validator"]["passed"], "changed": baseline["validator"]["passed"] != adaptive["validator"]["passed"], "baseline_reason": baseline["validator"]["reason"], "adaptive_reason": adaptive["validator"]["reason"]},
        "risk": {"would_tighten": adaptive["risk"]["would_tighten"], "tightened_fields": adaptive["risk"]["would_tighten_fields"], "enforced_fields": adaptive["risk"]["enforced_fields"], "baseline_entry_margin_ratio": (baseline["risk"]["baseline"] or {}).get("base_entry_margin_ratio"), "adaptive_entry_margin_ratio": (adaptive["risk"]["effective"] or {}).get("base_entry_margin_ratio")},
        "execution": {"would_tighten": adaptive["execution"]["would_tighten"], "tightened_fields": adaptive["execution"]["would_tighten_fields"], "enforced_fields": adaptive["execution"]["enforced_fields"], "execution_profile_really_enforced": adaptive["execution"]["execution_profile_really_enforced"], "layering_profile_really_enforced": adaptive["execution"]["layering_profile_really_enforced"], "plan_shape_really_enforced": adaptive["execution"]["plan_shape_really_enforced"], "baseline_layer_ratios": (baseline["execution"]["baseline"] or {}).get("layer_ratios"), "adaptive_live_layer_ratios": (adaptive["execution"]["live"] or {}).get("layer_ratios")},
    }

def _build_workflow_ready(case_data: Dict[str, Any]) -> Dict[str, Any]:
    workflow_input = copy.deepcopy(case_data.get('input') or {})
    if workflow_input.get('workflow_ready'):
        return workflow_input['workflow_ready']
    if workflow_input.get('report'):
        return build_governance_workflow_ready_payload(workflow_input['report'])
    return build_governance_workflow_ready_payload(build_regime_policy_calibration_report(workflow_input.get('symbol_results') or []))

def _replay_workflow_approval_state(case_data: Dict[str, Any], workflow_ready: Dict[str, Any]) -> Dict[str, Any]:
    replay_cfg = copy.deepcopy(case_data.get('replay') or {})
    approval_items = copy.deepcopy((workflow_ready.get('approval_state') or {}).get('items') or [])
    if not approval_items:
        return {'enabled': bool(replay_cfg.get('enabled', True)), 'db_path': None, 'synced_count': 0, 'states': [], 'timeline': [], 'summary': {'approval_count': 0, 'pending_count': 0}}
    if replay_cfg.get('db_path'):
        db_path = replay_cfg['db_path']
    else:
        tmp = tempfile.NamedTemporaryFile(prefix='shadow-workflow-', suffix='.db', delete=False)
        db_path = tmp.name
        tmp.close()
    replay_source = replay_cfg.get('source') or 'shadow_workflow_replay'
    db = Database(db_path)
    synced = db.sync_approval_items(approval_items, replay_source=replay_source, preserve_terminal=bool(replay_cfg.get('preserve_terminal', True)))
    item_id = synced[0].get('item_id') if synced else None
    timeline = db.get_approval_timeline(item_id=item_id, limit=50) if item_id else []
    states = db.get_approval_states(limit=max(len(synced), 1))
    return {
        'enabled': bool(replay_cfg.get('enabled', True)), 'db_path': db_path, 'synced_count': len(synced), 'states': states, 'timeline': timeline,
        'summary': {'approval_count': len(states), 'pending_count': sum(1 for row in states if row.get('state') == 'pending'), 'replayed_count': sum(1 for row in states if row.get('replay_source') == replay_source)},
    }

def _run_workflow_executor(case_data: Dict[str, Any], workflow_ready: Dict[str, Any], replay_result: Dict[str, Any]) -> Dict[str, Any]:
    execution_cfg = copy.deepcopy(case_data.get('workflow_execution') or {})
    if not execution_cfg.get('enabled'):
        return {'enabled': False, 'mode': 'disabled', 'executed': None}
    db_path = replay_result.get('db_path')
    if not db_path:
        return {'enabled': True, 'mode': execution_cfg.get('mode') or 'dry_run', 'executed': None, 'error': 'workflow replay db not initialized'}
    db = Database(db_path)
    payload = attach_auto_approval_policy(copy.deepcopy(workflow_ready))
    approval_records = build_workflow_approval_records(payload)
    if approval_records:
        persisted_rows = [db.get_approval_state(row.get('item_id')) for row in approval_records if row.get('item_id')]
        payload = attach_auto_approval_policy(merge_persisted_approval_state(payload, [row for row in persisted_rows if row]))
    executor_payload = execute_rollout_executor(payload, db, settings={
        'enabled': True,
        'mode': execution_cfg.get('mode') or 'dry_run',
        'dry_run': bool(execution_cfg.get('dry_run', (execution_cfg.get('mode') or 'dry_run') == 'dry_run')),
        'actor': execution_cfg.get('actor') or 'shadow:workflow-executor',
        'source': execution_cfg.get('source') or 'shadow_workflow_executor',
        'reason_prefix': execution_cfg.get('reason_prefix') or 'shadow workflow executor',
        'allowed_action_types': execution_cfg.get('allowed_action_types') or [],
    }, replay_source=execution_cfg.get('replay_source') or 'shadow_workflow_executor')
    return {'enabled': True, 'mode': execution_cfg.get('mode') or 'dry_run', 'executed': executor_payload.get('rollout_executor')}


def _run_testnet_bridge(case_data: Dict[str, Any], workflow_ready: Dict[str, Any], executor_result: Optional[Dict[str, Any]] = None, *, base_config: Optional[Config] = None, bridge_runner=None) -> Dict[str, Any]:
    from bot.run import build_exchange_smoke_plan, execute_exchange_smoke

    bridge_cfg = copy.deepcopy(case_data.get('testnet_bridge') or {})
    enabled = bool(bridge_cfg.get('enabled', False))
    if not enabled:
        return {'enabled': False, 'mode': 'disabled', 'plan_only': True, 'status': 'skipped', 'result': None, 'audit': {'real_trade_execution': False, 'dangerous_live_parameter_change': False}}

    cfg = ShadowConfig(base=base_config or Config(), overrides=bridge_cfg.get('config_overrides') or {})
    symbol = bridge_cfg.get('symbol')
    side = bridge_cfg.get('side') or 'long'
    allow_execute = bool(bridge_cfg.get('allow_execute', False))
    plan_only = not allow_execute
    require_no_pending_approvals = bool(bridge_cfg.get('require_no_pending_approvals', True))
    cleanup_required = bool(bridge_cfg.get('cleanup_required', True))
    requested_execute_profile = bridge_cfg.get('execute_profile') or ('minimal_smoke' if allow_execute else 'preview_only')

    class _BridgeExchangeStub:
        def fetch_balance(self):
            return bridge_cfg.get('balance') or {'free': {'USDT': bridge_cfg.get('available_usdt', 1000)}}
        def is_futures_symbol(self, selected_symbol):
            unsupported = set(bridge_cfg.get('unsupported_symbols') or [])
            return selected_symbol not in unsupported
        def fetch_ticker(self, selected_symbol):
            prices = bridge_cfg.get('prices') or {}
            return {'last': prices.get(selected_symbol, bridge_cfg.get('last_price', 50000))}
        def normalize_contract_amount(self, selected_symbol, smoke_notional, last_price):
            if bridge_cfg.get('sample_amount') is not None:
                return bridge_cfg['sample_amount']
            last_price = float(last_price or 0)
            return round(float(smoke_notional or 0) / last_price, 6) if last_price > 0 else 0.0
        def get_order_symbol(self, selected_symbol):
            return selected_symbol
        def create_order(self, selected_symbol, order_side, amount, posSide=None):
            return {'id': 'bridge-open', 'symbol': selected_symbol, 'side': order_side, 'amount': amount, 'posSide': posSide}
        def close_order(self, selected_symbol, order_side, amount, posSide=None):
            if bridge_cfg.get('close_order_error'):
                raise RuntimeError(bridge_cfg['close_order_error'])
            return {'id': 'bridge-close', 'symbol': selected_symbol, 'side': order_side, 'amount': amount, 'posSide': posSide}
        def confirm_smoke_open(self, selected_symbol, side, amount, open_order=None):
            return copy.deepcopy(bridge_cfg.get('open_confirmation') or {'status': bridge_cfg.get('open_status', 'filled')})
        def confirm_smoke_close(self, selected_symbol, side, amount, close_order=None, open_order=None):
            return copy.deepcopy(bridge_cfg.get('close_confirmation') or {'status': bridge_cfg.get('close_status', 'filled')})
        def detect_smoke_residual_position(self, selected_symbol, side, amount, open_order=None, close_order=None):
            if 'residual_position' in bridge_cfg:
                return copy.deepcopy(bridge_cfg.get('residual_position'))
            return {'detected': bool(bridge_cfg.get('residual_position_detected', False)), 'quantity': float(bridge_cfg.get('residual_quantity', 0.0) or 0.0)}
        def cleanup_smoke_position(self, selected_symbol, side, amount, open_order=None, close_order=None, residual_position=None):
            if 'cleanup_result' in bridge_cfg:
                return copy.deepcopy(bridge_cfg.get('cleanup_result'))
            if bridge_cfg.get('cleanup_succeeded') is True:
                return {'status': 'flattened'}
            if bridge_cfg.get('cleanup_succeeded') is False:
                return {'status': 'manual_required'}
            return {'status': 'not_attempted'}

    exchange = bridge_cfg.get('exchange') or _BridgeExchangeStub()
    plan = build_exchange_smoke_plan(cfg, exchange, symbol=symbol, side=side)
    gating = {
        'workflow_pending_approvals': sum(1 for row in ((workflow_ready.get('approval_state') or {}).get('items') or []) if row.get('approval_state') == 'pending'),
        'executor_applied_count': (((executor_result or {}).get('executed') or {}).get('summary') or {}).get('applied_count', 0),
        'executor_queued_count': (((executor_result or {}).get('executed') or {}).get('summary') or {}).get('queued_count', 0),
        'require_no_pending_approvals': require_no_pending_approvals,
        'cleanup_required': cleanup_required,
    }
    bridge_runner = bridge_runner or execute_exchange_smoke
    status = 'plan_only' if plan_only else 'controlled_execute'
    blocking_reasons = []
    if not plan.get('execute_ready', False):
        blocking_reasons.append('plan_not_execute_ready')
    if require_no_pending_approvals and gating['workflow_pending_approvals'] > 0:
        blocking_reasons.append('workflow_pending_approvals_present')
    exchange_mode = str(cfg.get('exchange.mode', 'testnet') or 'testnet').lower()
    if exchange_mode != 'testnet':
        blocking_reasons.append('exchange_mode_not_testnet')
    if requested_execute_profile != 'minimal_smoke':
        blocking_reasons.append('unsupported_execute_profile')

    result = {
        'enabled': True,
        'mode': 'plan_only' if plan_only else 'controlled_execute',
        'plan_only': plan_only,
        'status': status,
        'plan': plan,
        'gating': gating,
        'result': None,
        'error': None,
        'blocking_reasons': blocking_reasons,
        'audit': {
            'real_trade_execution': False,
            'dangerous_live_parameter_change': False,
            'exchange_mode': cfg.get('exchange.mode', 'testnet'),
            'allow_execute': allow_execute,
            'execute_profile': requested_execute_profile,
            'cleanup_required': cleanup_required,
            'rollback_expected': cleanup_required,
            'blocked': False,
        },
    }
    if plan_only:
        return result
    if blocking_reasons:
        result['status'] = 'blocked'
        result['audit']['blocked'] = True
        return result
    try:
        result['result'] = bridge_runner(cfg, exchange, symbol=symbol, side=side)
        result['audit']['real_trade_execution'] = bool((result['result'] or {}).get('opened') or (result['result'] or {}).get('closed'))
        result['cleanup_needed'] = bool((result['result'] or {}).get('cleanup_needed', False))
        result['residual_position_detected'] = bool((result['result'] or {}).get('residual_position_detected', False))
        result['open_status'] = (result['result'] or {}).get('open_status')
        result['close_status'] = (result['result'] or {}).get('close_status')
        result['reconcile_summary'] = (result['result'] or {}).get('reconcile_summary') or {}
        result['failure_compensation_hint'] = (result['result'] or {}).get('failure_compensation_hint')
        if (result['result'] or {}).get('error'):
            result['status'] = 'error'
            result['error'] = (result['result'] or {}).get('error')
        elif cleanup_required and bool((result['result'] or {}).get('cleanup_needed')):
            result['status'] = 'error'
            result['error'] = 'cleanup_required_but_cleanup_not_confirmed'
        elif cleanup_required and not bool((result['result'] or {}).get('closed')):
            result['status'] = 'error'
            result['error'] = 'cleanup_required_but_close_not_confirmed'
        else:
            result['status'] = 'controlled_execute'
    except Exception as exc:
        result['status'] = 'error'
        result['error'] = str(exc)
    return result




def _build_testnet_bridge_summary(bridge_result: Optional[Dict[str, Any]], *, case_id: Optional[str] = None, case_path: Optional[str] = None) -> Dict[str, Any]:
    bridge_result = bridge_result or {}
    plan = bridge_result.get('plan') or {}
    gating = bridge_result.get('gating') or {}
    audit = bridge_result.get('audit') or {}
    result = bridge_result.get('result') or {}
    blocking_reasons = list(bridge_result.get('blocking_reasons') or [])
    cleanup_needed = bool(bridge_result.get('cleanup_needed', False))
    residual_position_detected = bool(bridge_result.get('residual_position_detected', False))
    close_confirmed = bool((result.get('reconcile_summary') or {}).get('close_order_confirmed'))
    cleanup_confirmed = (result.get('cleanup_result') or {}).get('status') in {'flattened', 'confirmed_flat'}
    return {
        'case_id': case_id,
        'case_path': case_path,
        'enabled': bool(bridge_result.get('enabled', False)),
        'mode': bridge_result.get('mode') or 'disabled',
        'status': bridge_result.get('status') or ('disabled' if not bridge_result.get('enabled') else 'plan_only'),
        'plan_only': bool(bridge_result.get('plan_only', True)),
        'execute_ready': bool(plan.get('execute_ready', False)),
        'exchange_mode': audit.get('exchange_mode'),
        'symbol': plan.get('symbol'),
        'side': plan.get('side'),
        'execute_profile': audit.get('execute_profile'),
        'allow_execute': bool(audit.get('allow_execute', False)),
        'real_trade_execution': bool(audit.get('real_trade_execution', False)),
        'rollback_expected': bool(audit.get('rollback_expected', False)),
        'cleanup_required': bool(audit.get('cleanup_required', False)),
        'cleanup_needed': cleanup_needed,
        'cleanup_confirmed': cleanup_confirmed,
        'residual_position_detected': residual_position_detected,
        'open_status': bridge_result.get('open_status'),
        'close_status': bridge_result.get('close_status'),
        'close_confirmed': close_confirmed,
        'failure_compensation_hint': bridge_result.get('failure_compensation_hint'),
        'error': bridge_result.get('error'),
        'blocking_reasons': blocking_reasons,
        'blocking_reason_count': len(blocking_reasons),
        'workflow_pending_approvals': int(gating.get('workflow_pending_approvals', 0) or 0),
        'executor_applied_count': int(gating.get('executor_applied_count', 0) or 0),
        'executor_queued_count': int(gating.get('executor_queued_count', 0) or 0),
        'reconcile_summary': copy.deepcopy(bridge_result.get('reconcile_summary') or {}),
        'cleanup_result': copy.deepcopy(result.get('cleanup_result') or {}),
    }


def _aggregate_testnet_bridge_batch(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    bridge_cases = []
    status_counts: Dict[str, int] = {}
    blocking_reason_counts: Dict[str, int] = {}
    mode_counts: Dict[str, int] = {}
    for row in results or []:
        bridge_summary = copy.deepcopy((((row.get('artifacts') or {}).get('testnet_bridge_summary')) or {}))
        if not bridge_summary:
            bridge_summary = _build_testnet_bridge_summary(
                ((row.get('artifacts') or {}).get('testnet_bridge') or {}),
                case_id=row.get('case_id'),
                case_path=((row.get('audit') or {}).get('case_path')),
            )
        if not bridge_summary.get('enabled'):
            continue
        bridge_cases.append(bridge_summary)
        status = bridge_summary.get('status') or 'unknown'
        mode = bridge_summary.get('mode') or 'unknown'
        status_counts[status] = status_counts.get(status, 0) + 1
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        for reason in bridge_summary.get('blocking_reasons') or []:
            blocking_reason_counts[reason] = blocking_reason_counts.get(reason, 0) + 1
    cleanup_case_ids = [row.get('case_id') for row in bridge_cases if row.get('cleanup_needed')]
    blocked_case_ids = [row.get('case_id') for row in bridge_cases if row.get('status') == 'blocked']
    error_case_ids = [row.get('case_id') for row in bridge_cases if row.get('status') == 'error']
    return {
        'case_count': len(bridge_cases),
        'enabled_case_count': len(bridge_cases),
        'status_counts': status_counts,
        'mode_counts': mode_counts,
        'blocking_reason_counts': blocking_reason_counts,
        'real_trade_execution_count': sum(1 for row in bridge_cases if row.get('real_trade_execution')),
        'controlled_execute_success_count': sum(1 for row in bridge_cases if row.get('status') == 'controlled_execute' and not row.get('cleanup_needed') and not row.get('error')),
        'plan_only_count': sum(1 for row in bridge_cases if row.get('plan_only')),
        'blocked_count': len(blocked_case_ids),
        'error_count': len(error_case_ids),
        'cleanup_needed_count': len(cleanup_case_ids),
        'cleanup_confirmed_count': sum(1 for row in bridge_cases if row.get('cleanup_confirmed')),
        'residual_position_detected_count': sum(1 for row in bridge_cases if row.get('residual_position_detected')),
        'close_confirmed_count': sum(1 for row in bridge_cases if row.get('close_confirmed')),
        'case_ids_requiring_cleanup': cleanup_case_ids,
        'blocked_case_ids': blocked_case_ids,
        'error_case_ids': error_case_ids,
        'cases': bridge_cases,
    }


def format_validation_report_markdown(report: Dict[str, Any]) -> str:
    if report.get('mode') == 'validation_replay':
        summary = report.get('summary') or {}
        bridge = summary.get('testnet_bridge') or {}
        lines = [
            '# Shadow Validation Replay Report',
            '',
            '## Summary',
            f"- cases: {summary.get('case_count', 0)}",
            f"- pass: {summary.get('pass_count', 0)}",
            f"- fail: {summary.get('fail_count', 0)}",
            '',
            '## Testnet Bridge',
            f"- enabled cases: {bridge.get('case_count', 0)}",
            f"- real trade execution count: {bridge.get('real_trade_execution_count', 0)}",
            f"- controlled execute success count: {bridge.get('controlled_execute_success_count', 0)}",
            f"- blocked count: {bridge.get('blocked_count', 0)}",
            f"- error count: {bridge.get('error_count', 0)}",
            f"- cleanup needed count: {bridge.get('cleanup_needed_count', 0)}",
            '',
            '### Status Counts',
        ]
        for key, value in sorted((bridge.get('status_counts') or {}).items()):
            lines.append(f'- {key}: {value}')
        if bridge.get('blocking_reason_counts'):
            lines.extend(['', '### Blocking Reasons'])
            for key, value in sorted((bridge.get('blocking_reason_counts') or {}).items()):
                lines.append(f'- {key}: {value}')
        if bridge.get('case_ids_requiring_cleanup'):
            lines.extend(['', '### Cases Requiring Cleanup'])
            for case_id in bridge.get('case_ids_requiring_cleanup') or []:
                lines.append(f'- {case_id}')
        if summary.get('failed_cases'):
            lines.extend(['', '## Failed Cases'])
            for failed in summary.get('failed_cases') or []:
                lines.append(f"- {failed.get('case_id')} ({failed.get('case_type')})")
        return '\n'.join(lines) + '\n'

    bridge = ((report.get('artifacts') or {}).get('testnet_bridge_summary')) or {}
    lines = [
        '# Shadow Validation Report',
        '',
        '## Case',
        f"- case_id: {report.get('case_id')}",
        f"- case_type: {report.get('case_type')}",
        f"- mode: {report.get('mode')}",
        f"- status: {report.get('status')}",
    ]
    if bridge.get('enabled'):
        lines.extend([
            '',
            '## Testnet Bridge',
            f"- status: {bridge.get('status')}",
            f"- mode: {bridge.get('mode')}",
            f"- execute_ready: {bridge.get('execute_ready')}",
            f"- exchange_mode: {bridge.get('exchange_mode')}",
            f"- symbol: {bridge.get('symbol')}",
            f"- side: {bridge.get('side')}",
            f"- real_trade_execution: {bridge.get('real_trade_execution')}",
            f"- cleanup_needed: {bridge.get('cleanup_needed')}",
            f"- residual_position_detected: {bridge.get('residual_position_detected')}",
            f"- open_status: {bridge.get('open_status')}",
            f"- close_status: {bridge.get('close_status')}",
        ])
        if bridge.get('blocking_reasons'):
            lines.append(f"- blocking_reasons: {', '.join(bridge.get('blocking_reasons') or [])}")
        if bridge.get('error'):
            lines.append(f"- error: {bridge.get('error')}")
    return '\n'.join(lines) + '\n'

def _build_workflow_diff(workflow_ready: Dict[str, Any], replay_result: Dict[str, Any], executor_result: Optional[Dict[str, Any]] = None, bridge_result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    actions = workflow_ready.get('actions') or []
    approvals = (workflow_ready.get('approval_state') or {}).get('items') or []
    replay_states = replay_result.get('states') or []
    executed = (executor_result or {}).get('executed') or {}
    executor_summary = executed.get('summary') or {}
    stage_progression = (executed.get('stage_progression') or {}).get('summary') or {}
    bridge_result = bridge_result or {}
    executor_items = executed.get('items') or []
    first_executor = executor_items[0] if executor_items else {}
    timeline_summary = {
        'item_id': first_executor.get('playbook_id') or first_executor.get('item_id'),
        'approval_id': first_executor.get('item_id'),
        'status': first_executor.get('status'),
        'route': ((first_executor.get('dispatch') or {}).get('dispatch_route')),
        'event_types': [value for value in [((first_executor.get('audit') or {}).get('audit_code')), ((first_executor.get('dispatch') or {}).get('code')), ((first_executor.get('result') or {}).get('code'))] if value],
    }
    return {
        'workflow': {'action_count': len(actions), 'approval_count': len(approvals), 'pending_approval_count': sum(1 for row in approvals if row.get('approval_state') == 'pending'), 'blocked_count': sum(1 for row in (workflow_ready.get('workflow_state') or {}).get('item_states', []) if row.get('workflow_state') == 'blocked'), 'ready_count': sum(1 for row in (workflow_ready.get('workflow_state') or {}).get('item_states', []) if row.get('workflow_state') == 'ready')},
        'replay': {'synced_count': replay_result.get('synced_count', 0), 'timeline_events': len(replay_result.get('timeline') or []), 'replayed_state_count': len(replay_states), 'pending_state_count': sum(1 for row in replay_states if row.get('state') == 'pending')},
        'executor': {'enabled': bool((executor_result or {}).get('enabled')), 'mode': (executor_result or {}).get('mode') or 'disabled', 'planned_count': executor_summary.get('planned_count', 0), 'queued_count': executor_summary.get('queued_count', 0), 'dry_run_count': executor_summary.get('dry_run_count', 0), 'applied_count': executor_summary.get('applied_count', 0), 'error_count': executor_summary.get('error_count', 0), 'stage_ready_count': stage_progression.get('ready_stage_count', 0), 'blocked_count': stage_progression.get('blocked_count', 0), 'timeline_summary': timeline_summary},
        'testnet_bridge': {'enabled': bool(bridge_result.get('enabled')), 'mode': bridge_result.get('mode') or 'disabled', 'status': bridge_result.get('status') or ('disabled' if not bridge_result.get('enabled') else 'plan_only'), 'plan_only': bool(bridge_result.get('plan_only', True)), 'execute_ready': bool((bridge_result.get('plan') or {}).get('execute_ready', False)), 'pending_approval_count': (bridge_result.get('gating') or {}).get('workflow_pending_approvals', 0), 'executor_applied_count': (bridge_result.get('gating') or {}).get('executor_applied_count', 0), 'blocked': bool(bridge_result.get('status') == 'blocked'), 'cleanup_needed': bool(bridge_result.get('cleanup_needed', False)), 'residual_position_detected': bool(bridge_result.get('residual_position_detected', False)), 'open_status': bridge_result.get('open_status'), 'close_status': bridge_result.get('close_status'), 'failure_compensation_hint': bridge_result.get('failure_compensation_hint'), 'error': bridge_result.get('error')},
    }

def _evaluate_assertions(case_data: Dict[str, Any], adaptive: Optional[Dict[str, Any]], diff: Dict[str, Any], *, workflow_ready: Optional[Dict[str, Any]] = None, replay_result: Optional[Dict[str, Any]] = None) -> Tuple[bool, list]:
    expectations = case_data.get("expect") or {}
    results = []
    for key, expected in expectations.items():
        if key == "decision":
            actual = adaptive["decision"]["decision"] if adaptive else None
        elif key == "validator_pass":
            actual = adaptive["validator"]["passed"] if adaptive else None
        elif key == "risk_would_tighten":
            actual = diff.get("risk", {}).get("would_tighten")
        elif key == "execution_profile_really_enforced":
            actual = diff.get("execution", {}).get("execution_profile_really_enforced")
        elif key == "layering_profile_really_enforced":
            actual = diff.get("execution", {}).get("layering_profile_really_enforced")
        elif key == "plan_shape_really_enforced":
            actual = diff.get("execution", {}).get("plan_shape_really_enforced")
        elif key == 'workflow_action_count':
            actual = diff.get('workflow', {}).get('action_count')
        elif key == 'approval_count':
            actual = diff.get('workflow', {}).get('approval_count')
        elif key == 'pending_approval_count':
            actual = diff.get('workflow', {}).get('pending_approval_count')
        elif key == 'workflow_has_pending':
            actual = any(row.get('workflow_state') == 'pending' for row in (workflow_ready or {}).get('workflow_state', {}).get('item_states', []))
        elif key == 'approval_replay_synced':
            actual = (replay_result or {}).get('synced_count', 0) > 0
        elif key == 'approval_replay_source':
            states = (replay_result or {}).get('states') or []
            actual = states[0].get('replay_source') if states else None
        elif key == 'executor_planned_count':
            actual = diff.get('executor', {}).get('planned_count')
        elif key == 'executor_queued_count':
            actual = diff.get('executor', {}).get('queued_count')
        elif key == 'executor_dry_run_count':
            actual = diff.get('executor', {}).get('dry_run_count')
        elif key == 'testnet_bridge_enabled':
            actual = diff.get('testnet_bridge', {}).get('enabled')
        elif key == 'testnet_bridge_execute_ready':
            actual = diff.get('testnet_bridge', {}).get('execute_ready')
        elif key == 'testnet_bridge_mode':
            actual = diff.get('testnet_bridge', {}).get('mode')
        elif key == 'testnet_bridge_status':
            actual = diff.get('testnet_bridge', {}).get('status')
        elif key == 'testnet_bridge_blocked':
            actual = diff.get('testnet_bridge', {}).get('blocked')
        elif key == 'testnet_bridge_cleanup_needed':
            actual = diff.get('testnet_bridge', {}).get('cleanup_needed')
        elif key == 'testnet_bridge_residual_position_detected':
            actual = diff.get('testnet_bridge', {}).get('residual_position_detected')
        elif key == 'testnet_bridge_open_status':
            actual = diff.get('testnet_bridge', {}).get('open_status')
        elif key == 'testnet_bridge_close_status':
            actual = diff.get('testnet_bridge', {}).get('close_status')
        elif key == 'testnet_bridge_failure_compensation_hint':
            actual = diff.get('testnet_bridge', {}).get('failure_compensation_hint')
        else:
            actual = None
        results.append({"field": key, "expected": expected, "actual": actual, "passed": actual == expected})
    return all(item["passed"] for item in results) if results else True, results

def _run_signal_or_execution_case(case: ValidationCase, *, base_config: Optional[Config] = None, case_path: Optional[str] = None) -> Dict[str, Any]:
    base_config = base_config or Config()
    signal = _build_signal(case.raw)
    baseline_cfg = ShadowConfig(base=base_config, overrides=case.raw.get("baseline_config_overrides") or {})
    adaptive_cfg = ShadowConfig(base=base_config, overrides=case.raw.get("config_overrides") or {})
    baseline = _evaluate_with_config(baseline_cfg, signal)
    adaptive = _evaluate_with_config(adaptive_cfg, signal)
    diff = _build_diff(baseline, adaptive)
    passed, assertions = _evaluate_assertions(case.raw, adaptive, diff)
    return {"case_id": case.case_id, "case_type": case.case_type, "mode": case.raw.get("mode") or "guarded_execute", "status": "pass" if passed else "fail", "baseline": baseline, "adaptive": adaptive, "diff": diff, "assertions": assertions, "artifacts": {"baseline_policy_snapshot": baseline.get("adaptive_policy_snapshot"), "adaptive_policy_snapshot": adaptive.get("adaptive_policy_snapshot"), "adaptive_regime_snapshot": adaptive.get("regime_snapshot")}, "audit": {"generated_at": datetime.now().isoformat(), "real_trade_execution": False, "dangerous_live_parameter_change": False, "exchange_mode": "shadow", "case_path": str(case_path) if case_path else None}}

def _run_workflow_case(case: ValidationCase, *, case_path: Optional[str] = None, base_config: Optional[Config] = None) -> Dict[str, Any]:
    workflow_ready = _build_workflow_ready(case.raw)
    replay_result = _replay_workflow_approval_state(case.raw, workflow_ready)
    executor_result = _run_workflow_executor(case.raw, workflow_ready, replay_result)
    bridge_result = _run_testnet_bridge(case.raw, workflow_ready, executor_result, base_config=base_config)
    consumer_view = build_workflow_consumer_view({
        **copy.deepcopy(workflow_ready),
        'rollout_executor': (executor_result or {}).get('executed') or {},
        'controlled_rollout_execution': {},
        'auto_approval_execution': {},
    })
    detail_view = build_workbench_governance_detail_view({
        **copy.deepcopy(workflow_ready),
        'consumer_view': consumer_view,
        'rollout_executor': (executor_result or {}).get('executed') or {},
        'controlled_rollout_execution': {},
        'auto_approval_execution': {},
    }, item_id=((workflow_ready.get('actions') or [{}])[0]).get('item_id')) if (workflow_ready.get('actions') or []) else {'found': False}
    diff = _build_workflow_diff(workflow_ready, replay_result, executor_result, bridge_result)
    passed, assertions = _evaluate_assertions(case.raw, None, diff, workflow_ready=workflow_ready, replay_result=replay_result)
    bridge_summary = _build_testnet_bridge_summary(bridge_result, case_id=case.case_id, case_path=str(case_path) if case_path else None)
    return {'case_id': case.case_id, 'case_type': case.case_type, 'mode': case.raw.get('mode') or 'workflow_dry_run', 'status': 'pass' if passed else 'fail', 'baseline': None, 'adaptive': None, 'diff': diff, 'assertions': assertions, 'artifacts': {'workflow_ready': workflow_ready, 'approval_replay': replay_result, 'rollout_executor': executor_result.get('executed'), 'workflow_consumer_view': consumer_view, 'workbench_governance_detail_view': detail_view, 'testnet_bridge': bridge_result, 'testnet_bridge_summary': bridge_summary}, 'audit': {'generated_at': datetime.now().isoformat(), 'real_trade_execution': bool((bridge_result.get('audit') or {}).get('real_trade_execution', False)), 'dangerous_live_parameter_change': False, 'exchange_mode': (bridge_result.get('audit') or {}).get('exchange_mode', 'shadow'), 'case_path': str(case_path) if case_path else None, 'replay_source': ((replay_result.get('states') or [{}])[0]).get('replay_source') if replay_result.get('states') else None}}

def run_shadow_validation_case(case_path: str, *, base_config: Config = None) -> Dict[str, Any]:
    case = load_validation_case(case_path)
    if case.case_type == 'shadow_workflow':
        return _run_workflow_case(case, case_path=case_path, base_config=base_config)
    return _run_signal_or_execution_case(case, base_config=base_config, case_path=case_path)

def build_validation_summary(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    case_types, statuses = {}, {}
    for row in results:
        case_types[row.get('case_type') or 'unknown'] = case_types.get(row.get('case_type') or 'unknown', 0) + 1
        statuses[row.get('status') or 'unknown'] = statuses.get(row.get('status') or 'unknown', 0) + 1
    return {'case_count': len(results), 'pass_count': statuses.get('pass', 0), 'fail_count': statuses.get('fail', 0), 'case_types': case_types, 'statuses': statuses, 'failed_cases': [{'case_id': row.get('case_id'), 'case_type': row.get('case_type'), 'case_path': (row.get('audit') or {}).get('case_path'), 'failed_assertions': [item for item in (row.get('assertions') or []) if not item.get('passed')]} for row in results if row.get('status') != 'pass'], 'generated_at': datetime.now().isoformat(), 'real_trade_execution': False, 'exchange_mode': 'shadow', 'testnet_bridge': _aggregate_testnet_bridge_batch(results)}

def run_shadow_validation_replay(paths: Iterable[str], *, base_config: Optional[Config] = None) -> Dict[str, Any]:
    case_paths = collect_validation_case_paths(paths)
    results = [run_shadow_validation_case(path, base_config=base_config) for path in case_paths]
    return {'mode': 'validation_replay', 'paths': case_paths, 'results': results, 'summary': build_validation_summary(results), 'audit': {'generated_at': datetime.now().isoformat(), 'real_trade_execution': False, 'dangerous_live_parameter_change': False, 'exchange_mode': 'shadow'}}
