"""Adaptive regime policy resolver (M0/M1 observe-only scaffold)."""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from core.regime import build_regime_snapshot, normalize_regime_snapshot
from core.risk_budget import DEFAULT_RISK_BUDGET, get_risk_budget_config, derive_quality_bucket


ADAPTIVE_POLICY_VERSION = "adaptive_policy_v1_m1"


EXECUTION_PROFILE_FIELDS = [
    "layer_ratios",
    "layer_max_total_ratio",
    "max_layers_per_signal",
    "min_add_interval_seconds",
    "profit_only_add",
    "allow_same_bar_multiple_adds",
    "leverage_cap",
    "stop_loss",
    "take_profit",
    "trailing_stop",
    "trailing_activation",
    "exit_min_hold_seconds",
    "exit_arm_profit_threshold",
]

LAYERING_GUARDRAIL_FIELDS = [
    "layer_max_total_ratio",
    "max_layers_per_signal",
    "min_add_interval_seconds",
    "profit_only_add",
    "allow_same_bar_multiple_adds",
]

EXIT_PROFILE_FIELDS = [
    "stop_loss",
    "take_profit",
    "trailing_stop",
    "trailing_activation",
    "exit_min_hold_seconds",
    "exit_arm_profit_threshold",
]


def _slug(value: Any) -> str:
    text = str(value or "unknown").strip().lower().replace(' ', '_')
    return text or 'unknown'


def _pct(value: Any) -> str:
    try:
        return f"{round(float(value or 0.0) * 100):02.0f}"
    except Exception:
        return "00"


def build_regime_observe_only_view(regime_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    snapshot = normalize_regime_snapshot(regime_snapshot)
    state = 'stable' if snapshot.get('stability_score', 0) >= 0.65 else 'transition_risk'
    phase = 'risk_guarded' if snapshot.get('family') == 'risk' else ('directional' if snapshot.get('family') == 'trend' else 'rotation')
    summary = (
        f"{snapshot['name']}[{snapshot['direction']}] conf={snapshot['confidence']:.2f} "
        f"stable={snapshot['stability_score']:.2f} risk={snapshot['transition_risk']:.2f}"
    )
    tags = [
        'observe_only',
        'adaptive_regime',
        f"regime:{_slug(snapshot['name'])}",
        f"regime_family:{_slug(snapshot['family'])}",
        f"regime_direction:{_slug(snapshot['direction'])}",
        f"regime_phase:{phase}",
        f"regime_state:{state}",
        f"regime_conf_band:{_pct(snapshot.get('confidence'))}",
    ]
    notes = [
        'observe-only snapshot; no trading gates changed',
        f"details: {snapshot.get('details') or 'n/a'}",
    ]
    return {
        'phase': phase,
        'state': state,
        'summary': summary,
        'tags': tags,
        'notes': notes,
    }


def build_policy_observe_only_view(policy_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    snapshot = dict(policy_snapshot or {})
    mode = _slug(snapshot.get('mode') or 'observe_only')
    state = 'neutral' if not snapshot.get('is_effective') else 'effective'
    phase = 'observe_only' if mode == 'observe_only' else mode
    summary = (
        f"policy={snapshot.get('policy_version') or ADAPTIVE_POLICY_VERSION} mode={mode} "
        f"source={snapshot.get('policy_source') or 'adaptive_regime.defaults'} state={state}"
    )
    tags = [
        'observe_only',
        'adaptive_policy',
        f"policy_mode:{mode}",
        f"policy_state:{state}",
        f"policy_source:{_slug(snapshot.get('policy_source') or 'adaptive_regime.defaults')}",
        f"policy_regime:{_slug(snapshot.get('regime_name') or 'unknown')}",
        f"policy_version:{_slug(snapshot.get('policy_version') or ADAPTIVE_POLICY_VERSION)}",
    ]
    notes = ['observe-only policy snapshot; overrides are not applied live']
    for item in snapshot.get('notes') or []:
        if item not in notes:
            notes.append(str(item))
    return {
        'phase': phase,
        'state': state,
        'summary': summary,
        'tags': tags,
        'notes': notes,
    }


def enrich_policy_snapshot(policy_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = dict(policy_snapshot or {})
    observe_only_view = build_policy_observe_only_view(snapshot)
    snapshot['phase'] = observe_only_view['phase']
    snapshot['state'] = observe_only_view['state']
    snapshot['summary'] = observe_only_view['summary']
    snapshot['tags'] = list(observe_only_view['tags'])
    snapshot['notes'] = list(observe_only_view['notes'])
    return snapshot


def build_observe_only_bundle(regime_snapshot: Optional[Dict[str, Any]], policy_snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    regime_view = build_regime_observe_only_view(regime_snapshot)
    policy_view = build_policy_observe_only_view(policy_snapshot)
    summary = f"{regime_view['summary']} | {policy_view['summary']}"
    tags: List[str] = []
    for item in regime_view['tags'] + policy_view['tags']:
        if item not in tags:
            tags.append(item)
    return {
        'phase': policy_view['phase'],
        'state': f"{regime_view['state']}+{policy_view['state']}",
        'summary': summary,
        'tags': tags,
        'notes': regime_view['notes'] + [note for note in policy_view['notes'] if note not in regime_view['notes']],
    }




def normalize_observe_only_view(observe_only: Optional[Dict[str, Any]] = None, *, regime_snapshot: Optional[Dict[str, Any]] = None, policy_snapshot: Optional[Dict[str, Any]] = None, fallback_summary: Optional[str] = None) -> Dict[str, Any]:
    observe = dict(observe_only or {})
    normalized_regime = normalize_regime_snapshot(regime_snapshot)
    normalized_policy = enrich_policy_snapshot(dict(policy_snapshot or {})) if policy_snapshot else None
    bundle = build_observe_only_bundle(normalized_regime, normalized_policy or build_neutral_policy_snapshot(normalized_regime))
    tags: List[str] = []
    for item in observe.get('tags') or bundle['tags']:
        if item and item not in tags:
            tags.append(str(item))
    summary = observe.get('summary') or fallback_summary or bundle['summary']
    phase = observe.get('phase') or bundle['phase']
    state = observe.get('state') or bundle['state']
    banner = observe.get('banner') or 'Adaptive regime / policy currently run in observe-only mode; outputs are display-only and do not alter execution logic.'
    top_tags = tags[:5]
    return {
        'enabled': True,
        'phase': phase,
        'state': state,
        'summary': summary,
        'banner': banner,
        'tags': tags,
        'top_tags': top_tags,
        'tag_count': len(tags),
        'regime': {
            'name': normalized_regime.get('name'),
            'family': normalized_regime.get('family'),
            'direction': normalized_regime.get('direction'),
            'confidence': normalized_regime.get('confidence'),
        },
        'policy': {
            'mode': (normalized_policy or {}).get('mode'),
            'version': (normalized_policy or {}).get('policy_version'),
            'source': (normalized_policy or {}).get('policy_source'),
            'state': (normalized_policy or {}).get('state'),
        },
        'notes': list(observe.get('notes') or bundle.get('notes') or []),
        'snapshots': {
            'regime_snapshot': normalized_regime,
            'adaptive_policy_snapshot': normalized_policy or {},
        },
    }


def summarize_observe_only_collection(items: List[Dict[str, Any]], *, recent_limit: int = 5) -> Dict[str, Any]:
    normalized = [normalize_observe_only_view(item.get('observe_only') or item, regime_snapshot=((item.get('observe_only') or item).get('snapshots') or {}).get('regime_snapshot'), policy_snapshot=((item.get('observe_only') or item).get('snapshots') or {}).get('adaptive_policy_snapshot'), fallback_summary=((item.get('observe_only') or item).get('summary')) ) for item in (items or []) if (item.get('observe_only') or item)]
    tag_counts: Dict[str, int] = {}
    regime_counts: Dict[str, int] = {}
    policy_counts: Dict[str, int] = {}
    phase_counts: Dict[str, int] = {}
    state_counts: Dict[str, int] = {}
    recent = []
    for idx, item in enumerate(normalized):
        for tag in item.get('tags') or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
        regime = ((item.get('regime') or {}).get('name')) or '--'
        regime_counts[regime] = regime_counts.get(regime, 0) + 1
        policy = ((item.get('policy') or {}).get('mode')) or '--'
        policy_counts[policy] = policy_counts.get(policy, 0) + 1
        phase = item.get('phase') or '--'
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
        state = item.get('state') or '--'
        state_counts[state] = state_counts.get(state, 0) + 1
        if idx < recent_limit:
            recent.append({
                'summary': item.get('summary'),
                'phase': item.get('phase'),
                'state': item.get('state'),
                'regime': item.get('regime'),
                'policy': item.get('policy'),
                'top_tags': item.get('top_tags') or [],
            })
    def top(counts: Dict[str, int], limit: int = 5):
        return [{'value': k, 'count': v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit] if k and k != '--']
    top_tags = top(tag_counts)
    top_regimes = top(regime_counts, limit=3)
    top_policies = top(policy_counts, limit=3)
    dominant_phase = top(phase_counts, limit=1)
    dominant_state = top(state_counts, limit=1)
    banner = normalized[0].get('banner') if normalized else 'Adaptive regime / policy currently run in observe-only mode; outputs are display-only and do not alter execution logic.'
    if top_tags:
        banner = f"{banner} Top tags: {', '.join([row['value'] for row in top_tags[:3]])}."
    return {
        'count': len(normalized),
        'banner': banner,
        'top_tags': top_tags,
        'top_regimes': top_regimes,
        'top_policies': top_policies,
        'dominant_phase': dominant_phase[0]['value'] if dominant_phase else None,
        'dominant_state': dominant_state[0]['value'] if dominant_state else None,
        'recent': recent,
    }

def build_neutral_policy_snapshot(
    regime_snapshot: Optional[Dict[str, Any]] = None,
    *,
    mode: str = "observe_only",
    enabled: bool = False,
    policy_version: str = ADAPTIVE_POLICY_VERSION,
    policy_source: str = "adaptive_regime.defaults",
    matched_symbol: Optional[str] = None,
    matched_symbol_override: bool = False,
    notes: Optional[list] = None,
    decision_overrides: Optional[Dict[str, Any]] = None,
    validation_overrides: Optional[Dict[str, Any]] = None,
    risk_overrides: Optional[Dict[str, Any]] = None,
    execution_overrides: Optional[Dict[str, Any]] = None,
    effective_overrides: Optional[Dict[str, Any]] = None,
    is_effective: bool = False,
) -> Dict[str, Any]:
    normalized_regime = normalize_regime_snapshot(regime_snapshot)
    base = {
        'enabled': bool(enabled),
        'mode': mode,
        'policy_version': policy_version or ADAPTIVE_POLICY_VERSION,
        'policy_source': policy_source,
        'regime_name': normalized_regime['name'],
        'regime_family': normalized_regime['family'],
        'regime_direction': normalized_regime['direction'],
        'regime_confidence': normalized_regime['confidence'],
        'detector_version': normalized_regime['detector_version'],
        'matched_symbol': matched_symbol,
        'matched_symbol_override': bool(matched_symbol_override),
        'signal_weight_overrides': {},
        'decision_overrides': dict(decision_overrides or {}),
        'validation_overrides': dict(validation_overrides or {}),
        'risk_overrides': dict(risk_overrides or {}),
        'execution_overrides': dict(execution_overrides or {}),
        'effective_overrides': dict(effective_overrides or {}),
        'is_effective': bool(is_effective),
        'notes': list(notes or ['m1-observe-only']),
    }
    return enrich_policy_snapshot(base)


def get_signal_regime_snapshot(signal: Any = None, regime_snapshot: Optional[Dict[str, Any]] = None, policy_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if regime_snapshot:
        return normalize_regime_snapshot(regime_snapshot)
    if signal is not None:
        raw = getattr(signal, 'regime_snapshot', None) or getattr(signal, 'regime_info', None) or getattr(signal, 'market_context', {}).get('regime_snapshot')
        if raw:
            return normalize_regime_snapshot(raw)
        market_context = getattr(signal, 'market_context', {}) or {}
        market_regime = market_context.get('regime_name') or market_context.get('regime')
        if market_regime:
            return build_regime_snapshot(
                market_context.get('regime', market_regime),
                market_context.get('regime_confidence', 0.0),
                market_context.get('regime_indicators') or market_context.get('regime_features') or {},
                market_context.get('regime_details', ''),
                features=market_context.get('regime_features') or market_context.get('regime_indicators') or {},
                detected_at=market_context.get('regime_detected_at'),
                detector_version=market_context.get('regime_detector_version', None) or None,
                name=market_context.get('regime_name') or market_context.get('regime'),
                family=market_context.get('regime_family'),
                direction=market_context.get('regime_direction'),
                stability_score=market_context.get('regime_stability_score'),
                transition_risk=market_context.get('regime_transition_risk'),
            )
    policy_snapshot = dict(policy_snapshot or {})
    policy_regime = policy_snapshot.get('regime_name') or policy_snapshot.get('regime')
    if policy_regime:
        return build_regime_snapshot(
            policy_snapshot.get('regime') or policy_regime,
            policy_snapshot.get('regime_confidence', 0.0),
            policy_snapshot.get('regime_indicators') or policy_snapshot.get('regime_features') or {},
            policy_snapshot.get('regime_details', 'reconstructed from adaptive policy snapshot'),
            features=policy_snapshot.get('regime_features') or policy_snapshot.get('regime_indicators') or {},
            detected_at=policy_snapshot.get('regime_detected_at'),
            detector_version=policy_snapshot.get('detector_version', None) or None,
            name=policy_snapshot.get('regime_name') or policy_snapshot.get('regime'),
            family=policy_snapshot.get('regime_family'),
            direction=policy_snapshot.get('regime_direction'),
            stability_score=policy_snapshot.get('stability_score'),
            transition_risk=policy_snapshot.get('transition_risk'),
        )
    return build_regime_snapshot('unknown', 0.0, {}, 'missing regime snapshot')


def get_signal_policy_snapshot(config_helper: Any, symbol: Optional[str], signal: Any = None, regime_snapshot: Optional[Dict[str, Any]] = None, policy_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if policy_snapshot:
        return enrich_policy_snapshot(dict(policy_snapshot))
    if signal is not None:
        filter_details = getattr(signal, 'filter_details', None) or {}
        existing = getattr(signal, 'adaptive_policy_snapshot', None) or filter_details.get('adaptive_policy_snapshot')
        if existing:
            return enrich_policy_snapshot(dict(existing))
    normalized_regime = get_signal_regime_snapshot(signal, regime_snapshot)
    if hasattr(config_helper, 'get_adaptive_regime_config') and hasattr(config_helper, 'get_symbol_overrides'):
        return resolve_regime_policy(config_helper, symbol, normalized_regime)
    return build_neutral_policy_snapshot(normalized_regime, matched_symbol=symbol, notes=['m1-observe-only', 'neutral-policy', 'stateless-config'])


def build_observe_only_payload(config_helper: Any, symbol: Optional[str], signal: Any = None, regime_snapshot: Optional[Dict[str, Any]] = None, policy_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    normalized_regime = get_signal_regime_snapshot(signal, regime_snapshot, policy_snapshot)
    normalized_policy = get_signal_policy_snapshot(config_helper, symbol, signal, normalized_regime, policy_snapshot)
    regime_view = build_regime_observe_only_view(normalized_regime)
    policy_view = build_policy_observe_only_view(normalized_policy)
    bundle = build_observe_only_bundle(normalized_regime, normalized_policy)
    observe_only = normalize_observe_only_view(bundle, regime_snapshot=normalized_regime, policy_snapshot=normalized_policy)
    return {
        'regime_snapshot': normalized_regime,
        'adaptive_policy_snapshot': normalized_policy,
        'regime_observe_only': regime_view,
        'adaptive_policy_observe_only': policy_view,
        'observe_only': observe_only,
        'observe_only_summary': observe_only['summary'],
        'observe_only_tags': observe_only['tags'],
        'observe_only_notes': observe_only['notes'],
        'observe_only_phase': observe_only['phase'],
        'observe_only_state': observe_only['state'],
    }


def _append_unique(items: List[Any], value: Any) -> None:
    if value not in items:
        items.append(value)


def build_validation_baseline_snapshot(config_helper: Any, symbol: Optional[str]) -> Dict[str, Any]:
    composite_cfg = config_helper.get_symbol_section(symbol, 'strategies.composite') if hasattr(config_helper, 'get_symbol_section') else {}
    market_filters = config_helper.get_symbol_section(symbol, 'market_filters') if hasattr(config_helper, 'get_symbol_section') else {}
    regime_filters = config_helper.get_symbol_section(symbol, 'regime_filters') if hasattr(config_helper, 'get_symbol_section') else {}
    return {
        'min_strength': int(composite_cfg.get('min_strength', 20) or 20),
        'min_strategy_count': int(composite_cfg.get('min_strategy_count', 1) or 1),
        'block_counter_trend': bool(market_filters.get('block_counter_trend', True)),
        'block_high_volatility': bool(market_filters.get('block_high_volatility', True)),
        'block_low_volatility': bool(market_filters.get('block_low_volatility', True)),
        'regime_filter_enabled': bool(regime_filters.get('enabled', True)),
    }


def merge_validation_overrides_conservatively(baseline_snapshot: Dict[str, Any], validation_overrides: Optional[Dict[str, Any]] = None, *, conservative_only: bool = True) -> Dict[str, Any]:
    baseline_snapshot = dict(baseline_snapshot or {})
    effective = dict(baseline_snapshot)
    applied_overrides: Dict[str, Dict[str, Any]] = {}
    ignored_overrides: List[Dict[str, Any]] = []
    numeric_rules = {'min_strength': 'max', 'min_strategy_count': 'max'}
    boolean_rules = {'block_counter_trend', 'block_high_volatility', 'block_low_volatility', 'regime_filter_enabled'}

    for key, requested in dict(validation_overrides or {}).items():
        if key not in numeric_rules and key not in boolean_rules:
            ignored_overrides.append({'key': key, 'requested': requested, 'reason': 'unsupported_validation_field', 'code': 'IGNORED_UNSUPPORTED_VALIDATION_FIELD'})
            continue
        baseline = baseline_snapshot.get(key)
        if key in numeric_rules:
            if not isinstance(requested, (int, float)):
                ignored_overrides.append({'key': key, 'requested': requested, 'reason': 'override_not_numeric', 'code': 'IGNORED_INVALID_OVERRIDE'})
                continue
            final_value = max(float(baseline), float(requested))
            if final_value > float(baseline):
                effective[key] = int(final_value) if key == 'min_strategy_count' else final_value
                applied_overrides[key] = {'baseline': baseline, 'effective': effective[key], 'requested': requested, 'source': f'validation_overrides.{key}'}
            elif conservative_only and float(requested) < float(baseline):
                ignored_overrides.append({'key': key, 'requested': requested, 'baseline': baseline, 'reason': 'non_conservative_override', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE'})
        elif key in boolean_rules:
            requested_bool = bool(requested)
            baseline_bool = bool(baseline)
            if requested_bool and not baseline_bool:
                effective[key] = True
                applied_overrides[key] = {'baseline': baseline_bool, 'effective': True, 'requested': requested_bool, 'source': f'validation_overrides.{key}'}
            elif conservative_only and (not requested_bool) and baseline_bool:
                ignored_overrides.append({'key': key, 'requested': requested_bool, 'baseline': baseline_bool, 'reason': 'non_conservative_override', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE'})
    return {
        'effective': effective,
        'applied_overrides': applied_overrides,
        'ignored_overrides': ignored_overrides,
    }


def build_risk_baseline_snapshot(config_helper: Any, symbol: Optional[str]) -> Dict[str, Any]:
    baseline = dict(DEFAULT_RISK_BUDGET)
    baseline.update(get_risk_budget_config(config_helper, symbol))
    baseline['leverage_cap'] = None
    return baseline


def derive_layer_count_from_ratios(layer_ratios: Optional[List[Any]]) -> int:
    try:
        return len([float(x) for x in (layer_ratios or [])])
    except Exception:
        return 0


def merge_layer_ratios_conservatively(
    baseline_ratios: Optional[List[Any]],
    requested_ratios: Any,
    *,
    conservative_only: bool = True,
    live_total_ratio_cap: Optional[float] = None,
) -> Dict[str, Any]:
    baseline_values = [float(x) for x in (baseline_ratios or [])]
    if not isinstance(requested_ratios, list) or not requested_ratios:
        return {'accepted': False, 'reason': 'override_not_list', 'code': 'IGNORED_INVALID_OVERRIDE', 'baseline': baseline_values}
    try:
        requested_values = [float(x) for x in requested_ratios]
    except Exception:
        return {'accepted': False, 'reason': 'override_not_numeric', 'code': 'IGNORED_INVALID_OVERRIDE', 'baseline': baseline_values}
    if any(x <= 0 for x in requested_values):
        return {'accepted': False, 'reason': 'layer_ratio_not_positive', 'code': 'IGNORED_INVALID_OVERRIDE', 'baseline': baseline_values, 'requested': requested_values}
    if len(requested_values) > len(baseline_values):
        return {'accepted': False, 'reason': 'layer_ratio_length_expands', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE', 'baseline': baseline_values, 'requested': requested_values}
    for idx, req in enumerate(requested_values):
        base = baseline_values[idx]
        if conservative_only and req > base + 1e-12:
            return {'accepted': False, 'reason': 'non_conservative_override', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE', 'baseline': baseline_values, 'requested': requested_values}
    if conservative_only and sum(requested_values) > sum(baseline_values) + 1e-12:
        return {'accepted': False, 'reason': 'non_conservative_override', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE', 'baseline': baseline_values, 'requested': requested_values}
    if live_total_ratio_cap is not None and sum(requested_values) > float(live_total_ratio_cap) + 1e-12:
        return {'accepted': False, 'reason': 'layer_ratio_exceeds_total_cap', 'code': 'IGNORED_INVALID_OVERRIDE', 'baseline': baseline_values, 'requested': requested_values, 'live_total_ratio_cap': float(live_total_ratio_cap)}
    return {'accepted': True, 'effective': requested_values, 'requested': requested_values, 'baseline': baseline_values}



def build_execution_baseline_snapshot(config_helper: Any, symbol: Optional[str]) -> Dict[str, Any]:
    layering = config_helper.get_layering_config(symbol) if hasattr(config_helper, 'get_layering_config') else {}
    trading_cfg = config_helper.get_symbol_section(symbol, 'trading') if hasattr(config_helper, 'get_symbol_section') else {}
    leverage_cap = int(trading_cfg.get('leverage', 10) or 10)
    ratios = [float(x) for x in (layering.get('layer_ratios') or [0.06, 0.06, 0.04])]
    return {
        'layer_ratios': ratios,
        'layer_count': derive_layer_count_from_ratios(ratios),
        'layer_max_total_ratio': float(layering.get('layer_max_total_ratio') or 0.16),
        'max_layers_per_signal': int(layering.get('max_layers_per_signal') or len(ratios)),
        'min_add_interval_seconds': int(layering.get('min_add_interval_seconds') or 0),
        'profit_only_add': bool(layering.get('profit_only_add', False)),
        'allow_same_bar_multiple_adds': bool(layering.get('allow_same_bar_multiple_adds', False)),
        'leverage_cap': leverage_cap,
        'stop_loss': float(trading_cfg.get('stop_loss') or 0.02),
        'take_profit': float(trading_cfg.get('take_profit') or 0.04),
        'trailing_stop': float(trading_cfg.get('trailing_stop') or 0.01),
        'trailing_activation': trading_cfg.get('trailing_activation') if trading_cfg.get('trailing_activation') is not None else 0.01,
        'exit_min_hold_seconds': int(float(trading_cfg.get('exit_min_hold_seconds') or 0)),
        'exit_arm_profit_threshold': float(trading_cfg.get('exit_arm_profit_threshold')) if trading_cfg.get('exit_arm_profit_threshold') is not None else None,
    }


def merge_execution_overrides_conservatively(baseline_snapshot: Dict[str, Any], execution_overrides: Optional[Dict[str, Any]] = None, *, conservative_only: bool = True) -> Dict[str, Any]:
    baseline_snapshot = dict(baseline_snapshot or {})
    effective = dict(baseline_snapshot)
    applied_overrides: Dict[str, Dict[str, Any]] = {}
    ignored_overrides: List[Dict[str, Any]] = []

    for key, requested in dict(execution_overrides or {}).items():
        baseline = baseline_snapshot.get(key)
        if key == 'layer_count':
            ignored_overrides.append({'key': key, 'requested': requested, 'baseline': baseline, 'reason': 'layer_count_derived_only', 'code': 'IGNORED_DERIVED_ONLY_FIELD'})
            continue
        if key == 'layer_ratios':
            merge_result = merge_layer_ratios_conservatively(
                baseline,
                requested,
                conservative_only=conservative_only,
                live_total_ratio_cap=effective.get('layer_max_total_ratio'),
            )
            if not merge_result.get('accepted'):
                ignored_overrides.append({'key': key, 'requested': merge_result.get('requested', requested), 'baseline': merge_result.get('baseline', baseline), 'reason': merge_result.get('reason'), 'code': merge_result.get('code'), 'live_total_ratio_cap': merge_result.get('live_total_ratio_cap')})
                continue
            requested_values = list(merge_result.get('effective') or [])
            baseline_values = list(merge_result.get('baseline') or [])
            if requested_values != baseline_values:
                effective[key] = requested_values
                applied_overrides[key] = {'baseline': baseline_values, 'effective': requested_values, 'requested': requested_values, 'source': f'execution_overrides.{key}'}
            continue
        if key in {'layer_max_total_ratio', 'leverage_cap'}:
            if not isinstance(requested, (int, float)):
                ignored_overrides.append({'key': key, 'requested': requested, 'reason': 'override_not_numeric', 'code': 'IGNORED_INVALID_OVERRIDE'})
                continue
            requested_float = float(requested)
            baseline_float = float(baseline)
            final_value = min(baseline_float, requested_float)
            if final_value + 1e-12 < baseline_float:
                effective[key] = int(final_value) if key == 'leverage_cap' else final_value
                applied_overrides[key] = {'baseline': baseline, 'effective': effective[key], 'requested': requested, 'source': f'execution_overrides.{key}'}
            elif conservative_only and requested_float > baseline_float + 1e-12:
                ignored_overrides.append({'key': key, 'requested': requested, 'baseline': baseline, 'reason': 'non_conservative_override', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE'})
            continue
        if key in {'stop_loss', 'take_profit', 'trailing_stop', 'trailing_activation'}:
            if not isinstance(requested, (int, float)):
                ignored_overrides.append({'key': key, 'requested': requested, 'reason': 'override_not_numeric', 'code': 'IGNORED_INVALID_OVERRIDE'})
                continue
            requested_float = float(requested)
            baseline_float = float(baseline)
            final_value = min(baseline_float, requested_float)
            if final_value + 1e-12 < baseline_float:
                effective[key] = final_value
                applied_overrides[key] = {'baseline': baseline, 'effective': effective[key], 'requested': requested, 'source': f'execution_overrides.{key}'}
            elif conservative_only and requested_float > baseline_float + 1e-12:
                ignored_overrides.append({'key': key, 'requested': requested, 'baseline': baseline, 'reason': 'non_conservative_override', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE'})
            continue
        if key == 'exit_min_hold_seconds':
            if not isinstance(requested, (int, float)):
                ignored_overrides.append({'key': key, 'requested': requested, 'reason': 'override_not_numeric', 'code': 'IGNORED_INVALID_OVERRIDE'})
                continue
            requested_int = max(0, int(requested))
            baseline_int = max(0, int(baseline or 0))
            final_value = max(baseline_int, requested_int)
            if final_value > baseline_int:
                effective[key] = final_value
                applied_overrides[key] = {'baseline': baseline_int, 'effective': final_value, 'requested': requested_int, 'source': f'execution_overrides.{key}'}
            elif conservative_only and requested_int < baseline_int:
                ignored_overrides.append({'key': key, 'requested': requested_int, 'baseline': baseline_int, 'reason': 'non_conservative_override', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE'})
            continue
        if key == 'exit_arm_profit_threshold':
            if requested is None:
                if conservative_only and baseline is not None:
                    ignored_overrides.append({'key': key, 'requested': requested, 'baseline': baseline, 'reason': 'non_conservative_override', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE'})
                continue
            if not isinstance(requested, (int, float)):
                ignored_overrides.append({'key': key, 'requested': requested, 'reason': 'override_not_numeric', 'code': 'IGNORED_INVALID_OVERRIDE'})
                continue
            requested_float = float(requested)
            baseline_float = float(baseline) if baseline is not None else None
            if baseline_float is None:
                effective[key] = requested_float
                applied_overrides[key] = {'baseline': baseline, 'effective': requested_float, 'requested': requested_float, 'source': f'execution_overrides.{key}'}
            elif requested_float > baseline_float + 1e-12:
                effective[key] = requested_float
                applied_overrides[key] = {'baseline': baseline, 'effective': requested_float, 'requested': requested_float, 'source': f'execution_overrides.{key}'}
            elif conservative_only and requested_float + 1e-12 < baseline_float:
                ignored_overrides.append({'key': key, 'requested': requested_float, 'baseline': baseline, 'reason': 'non_conservative_override', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE'})
            continue
        if key == 'max_layers_per_signal':
            if not isinstance(requested, (int, float)):
                ignored_overrides.append({'key': key, 'requested': requested, 'reason': 'override_not_numeric', 'code': 'IGNORED_INVALID_OVERRIDE'})
                continue
            requested_int = int(requested)
            baseline_int = int(baseline)
            final_value = min(baseline_int, requested_int)
            if final_value < baseline_int:
                effective[key] = final_value
                applied_overrides[key] = {'baseline': baseline_int, 'effective': final_value, 'requested': requested_int, 'source': f'execution_overrides.{key}'}
            elif conservative_only and requested_int > baseline_int:
                ignored_overrides.append({'key': key, 'requested': requested_int, 'baseline': baseline_int, 'reason': 'non_conservative_override', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE'})
            continue
        if key == 'min_add_interval_seconds':
            if not isinstance(requested, (int, float)):
                ignored_overrides.append({'key': key, 'requested': requested, 'reason': 'override_not_numeric', 'code': 'IGNORED_INVALID_OVERRIDE'})
                continue
            requested_int = int(requested)
            baseline_int = int(baseline)
            final_value = max(baseline_int, requested_int)
            if final_value > baseline_int:
                effective[key] = final_value
                applied_overrides[key] = {'baseline': baseline_int, 'effective': final_value, 'requested': requested_int, 'source': f'execution_overrides.{key}'}
            elif conservative_only and requested_int < baseline_int:
                ignored_overrides.append({'key': key, 'requested': requested_int, 'baseline': baseline_int, 'reason': 'non_conservative_override', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE'})
            continue
        if key == 'profit_only_add':
            requested_bool = bool(requested)
            baseline_bool = bool(baseline)
            if requested_bool and not baseline_bool:
                effective[key] = True
                applied_overrides[key] = {'baseline': baseline_bool, 'effective': True, 'requested': requested_bool, 'source': f'execution_overrides.{key}'}
            elif conservative_only and (not requested_bool) and baseline_bool:
                ignored_overrides.append({'key': key, 'requested': requested_bool, 'baseline': baseline_bool, 'reason': 'non_conservative_override', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE'})
            continue
        if key == 'allow_same_bar_multiple_adds':
            requested_bool = bool(requested)
            baseline_bool = bool(baseline)
            if (not requested_bool) and baseline_bool:
                effective[key] = False
                applied_overrides[key] = {'baseline': baseline_bool, 'effective': False, 'requested': requested_bool, 'source': f'execution_overrides.{key}'}
            elif conservative_only and requested_bool and not baseline_bool:
                ignored_overrides.append({'key': key, 'requested': requested_bool, 'baseline': baseline_bool, 'reason': 'non_conservative_override', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE'})
            continue
        ignored_overrides.append({'key': key, 'requested': requested, 'reason': 'unsupported_execution_field', 'code': 'IGNORED_UNSUPPORTED_EXECUTION_FIELD'})

    effective['layer_count'] = derive_layer_count_from_ratios(effective.get('layer_ratios'))
    return {
        'effective': effective,
        'applied_overrides': applied_overrides,
        'ignored_overrides': ignored_overrides,
    }


def merge_risk_overrides_conservatively(baseline_snapshot: Dict[str, Any], risk_overrides: Optional[Dict[str, Any]] = None, *, conservative_only: bool = True) -> Dict[str, Any]:
    baseline_snapshot = dict(baseline_snapshot or {})
    effective = dict(baseline_snapshot)
    applied_overrides: Dict[str, Dict[str, Any]] = {}
    ignored_overrides: List[Dict[str, Any]] = []
    lower_only_fields = {
        'total_margin_cap_ratio',
        'total_margin_soft_cap_ratio',
        'symbol_margin_cap_ratio',
        'base_entry_margin_ratio',
        'max_entry_margin_ratio',
        'leverage_cap',
    }

    for key, requested in dict(risk_overrides or {}).items():
        if key not in lower_only_fields:
            ignored_overrides.append({'key': key, 'requested': requested, 'reason': 'unsupported_risk_field', 'code': 'IGNORED_UNSUPPORTED_RISK_FIELD'})
            continue
        if requested is None:
            ignored_overrides.append({'key': key, 'requested': requested, 'reason': 'empty_override', 'code': 'IGNORED_EMPTY_OVERRIDE'})
            continue
        if not isinstance(requested, (int, float)):
            ignored_overrides.append({'key': key, 'requested': requested, 'reason': 'override_not_numeric', 'code': 'IGNORED_INVALID_OVERRIDE'})
            continue
        baseline = baseline_snapshot.get(key)
        if baseline is None:
            effective[key] = float(requested)
            applied_overrides[key] = {'baseline': baseline, 'effective': effective[key], 'requested': requested, 'source': f'risk_overrides.{key}'}
            continue
        baseline_float = float(baseline)
        requested_float = float(requested)
        final_value = min(baseline_float, requested_float)
        if final_value + 1e-12 < baseline_float:
            effective[key] = int(final_value) if key == 'leverage_cap' else final_value
            applied_overrides[key] = {'baseline': baseline, 'effective': effective[key], 'requested': requested, 'source': f'risk_overrides.{key}'}
        elif conservative_only and requested_float > baseline_float + 1e-12:
            ignored_overrides.append({'key': key, 'requested': requested, 'baseline': baseline, 'reason': 'non_conservative_override', 'code': 'IGNORED_NON_CONSERVATIVE_OVERRIDE'})

    if float(effective.get('total_margin_soft_cap_ratio', 0) or 0) > float(effective.get('total_margin_cap_ratio', 0) or 0):
        effective['total_margin_soft_cap_ratio'] = float(effective.get('total_margin_cap_ratio') or 0)
    if float(effective.get('max_entry_margin_ratio', 0) or 0) < float(effective.get('base_entry_margin_ratio', 0) or 0):
        effective['base_entry_margin_ratio'] = float(effective.get('max_entry_margin_ratio') or 0)
    if float(effective.get('max_entry_margin_ratio', 0) or 0) < float(effective.get('min_entry_margin_ratio', 0) or 0):
        effective['min_entry_margin_ratio'] = float(effective.get('max_entry_margin_ratio') or 0)
    return {
        'effective': effective,
        'applied_overrides': applied_overrides,
        'ignored_overrides': ignored_overrides,
    }


def _matches_symbol_rollout(symbol: Optional[str], explicit_symbols: Optional[List[str]], fraction: float) -> Dict[str, Any]:
    symbol = str(symbol or '')
    explicit = [str(item) for item in (explicit_symbols or []) if str(item).strip()]
    symbol_match = (not explicit) or (symbol in explicit)
    fraction = max(0.0, min(1.0, float(fraction or 0.0)))
    if fraction >= 1.0:
        fraction_match = True
    elif fraction <= 0.0:
        fraction_match = False if explicit else False
    else:
        bucket = (int(hashlib.sha256(symbol.encode('utf-8')).hexdigest()[:8], 16) % 10000) / 10000.0 if symbol else 1.0
        fraction_match = bucket < fraction
    return {
        'symbol_match': symbol_match,
        'fraction_match': fraction_match,
        'rollout_match': symbol_match and (fraction_match if fraction > 0 else bool(explicit and symbol_match)),
        'fraction': fraction,
    }



def build_layering_plan_shape_snapshot(
    baseline: Dict[str, Any],
    effective: Dict[str, Any],
    *,
    symbol: Optional[str],
    mode: str,
    conservative_only: bool,
    layering_enforcement_enabled: bool,
    layering_plan_shape_enforcement_enabled: bool,
    guardrails_live: bool,
    rollout_symbols: Optional[List[str]] = None,
    rollout_fraction: float = 0.0,
    require_guardrails_live: bool = True,
    fail_closed: bool = True,
    force_baseline_on_invalid: bool = True,
) -> Dict[str, Any]:
    baseline_shape = list(baseline.get('layer_ratios') or [])
    effective_shape = list(effective.get('layer_ratios') or baseline_shape)
    rollout = _matches_symbol_rollout(symbol, rollout_symbols, rollout_fraction)
    live_shape = list(baseline_shape)
    enforced_fields: List[str] = []
    ignored_fields: List[str] = []
    decisions: List[Dict[str, Any]] = []
    validation = {'valid': True, 'accepted': False, 'reason': 'shape_not_applied', 'ignored_fields': [], 'enforced_fields': []}
    really_enforced = False
    source = 'baseline'

    shape_applied = effective_shape != baseline_shape
    gating_reason = None
    if mode not in {'guarded_execute', 'full'}:
        gating_reason = 'policy_mode_not_live'
    elif not layering_enforcement_enabled:
        gating_reason = 'layering_profile_enforcement_disabled'
    elif not layering_plan_shape_enforcement_enabled:
        gating_reason = 'layering_plan_shape_enforcement_disabled'
    elif require_guardrails_live and not guardrails_live:
        gating_reason = 'layering_guardrails_not_live'
    elif not rollout.get('rollout_match'):
        gating_reason = 'plan_shape_rollout_miss'

    if shape_applied and gating_reason is None:
        merged_shape = merge_layer_ratios_conservatively(
            baseline_shape,
            effective_shape,
            conservative_only=conservative_only,
            live_total_ratio_cap=effective.get('layer_max_total_ratio'),
        )
        if merged_shape.get('accepted'):
            live_shape = list(merged_shape.get('effective') or baseline_shape)
            source = 'adaptive_live'
            enforced_fields.append('layer_ratios')
            really_enforced = live_shape != baseline_shape
            validation = {
                'valid': True,
                'accepted': really_enforced,
                'reason': 'conservative_tighten_enforced' if really_enforced else 'no_change',
                'baseline_sum': float(sum(baseline_shape)),
                'effective_sum': float(sum(effective_shape)),
                'live_sum': float(sum(live_shape)),
                'layer_max_total_ratio': float(effective.get('layer_max_total_ratio') or 0.0),
                'baseline_layer_count': derive_layer_count_from_ratios(baseline_shape),
                'effective_layer_count': derive_layer_count_from_ratios(effective_shape),
                'live_layer_count': derive_layer_count_from_ratios(live_shape),
                'enforced_fields': ['layer_ratios'] if really_enforced else [],
                'ignored_fields': [],
            }
        else:
            ignored_fields.append('layer_ratios')
            validation = {
                'valid': False,
                'accepted': False,
                'reason': merged_shape.get('reason') or 'invalid_layer_ratios',
                'baseline_sum': float(sum(baseline_shape)),
                'effective_sum': float(sum(effective_shape)),
                'live_sum': float(sum(baseline_shape)),
                'layer_max_total_ratio': float(effective.get('layer_max_total_ratio') or 0.0),
                'baseline_layer_count': derive_layer_count_from_ratios(baseline_shape),
                'effective_layer_count': derive_layer_count_from_ratios(effective_shape),
                'live_layer_count': derive_layer_count_from_ratios(baseline_shape),
                'enforced_fields': [],
                'ignored_fields': ['layer_ratios'],
                'fail_closed': bool(fail_closed),
                'force_baseline_on_invalid': bool(force_baseline_on_invalid),
            }
    elif shape_applied:
        ignored_fields.append('layer_ratios')
        validation = {
            'valid': True,
            'accepted': False,
            'reason': gating_reason,
            'baseline_sum': float(sum(baseline_shape)),
            'effective_sum': float(sum(effective_shape)),
            'live_sum': float(sum(baseline_shape)),
            'layer_max_total_ratio': float(effective.get('layer_max_total_ratio') or 0.0),
            'baseline_layer_count': derive_layer_count_from_ratios(baseline_shape),
            'effective_layer_count': derive_layer_count_from_ratios(effective_shape),
            'live_layer_count': derive_layer_count_from_ratios(baseline_shape),
            'enforced_fields': [],
            'ignored_fields': ['layer_ratios'],
        }

    decisions.append({
        'field': 'layer_ratios',
        'baseline': baseline_shape,
        'effective': effective_shape,
        'live': live_shape,
        'enforced': really_enforced,
        'applied': shape_applied,
        'ignored': 'layer_ratios' in ignored_fields,
        'decision': 'enforced' if really_enforced else ('ignored' if shape_applied else 'unchanged'),
        'reason': validation.get('reason') if shape_applied else 'no_change',
    })
    return {
        'baseline': {'layer_ratios': baseline_shape, 'layer_count': derive_layer_count_from_ratios(baseline_shape)},
        'effective': {'layer_ratios': effective_shape, 'layer_count': derive_layer_count_from_ratios(effective_shape)},
        'live': {'layer_ratios': live_shape, 'layer_count': derive_layer_count_from_ratios(live_shape)},
        'plan_shape_really_enforced': really_enforced,
        'plan_shape_enforced_fields': enforced_fields,
        'plan_shape_ignored_fields': ignored_fields,
        'live_layer_shape_source': source,
        'shape_guardrail_decisions': decisions,
        'shape_rollout_symbol_match': rollout.get('symbol_match'),
        'shape_rollout_fraction_match': rollout.get('fraction_match'),
        'shape_live_rollout_match': rollout.get('rollout_match'),
        'plan_shape_validation': validation,
    }



def build_execution_effective_snapshot(config_helper: Any, symbol: Optional[str], *, signal: Any = None, regime_snapshot: Optional[Dict[str, Any]] = None, policy_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    observe_payload = build_observe_only_payload(config_helper, symbol, signal=signal, regime_snapshot=regime_snapshot, policy_snapshot=policy_snapshot)
    adaptive_cfg = config_helper.get_adaptive_regime_config(symbol) if hasattr(config_helper, 'get_adaptive_regime_config') else {}
    guarded_cfg = dict(adaptive_cfg.get('guarded_execute') or {})
    normalized_policy = observe_payload['adaptive_policy_snapshot']
    normalized_regime = observe_payload['regime_snapshot']
    baseline = build_execution_baseline_snapshot(config_helper, symbol)
    conservative_only = bool(guarded_cfg.get('conservative_only', guarded_cfg.get('enforce_conservative_only', True)))
    hints_enabled = bool(guarded_cfg.get('execution_profile_hints_enabled', False))
    enforcement_enabled = bool(guarded_cfg.get('execution_profile_enforcement_enabled', False))
    layering_hints_enabled = bool(guarded_cfg.get('layering_profile_hints_enabled', hints_enabled))
    layering_enforcement_enabled = bool(guarded_cfg.get('layering_profile_enforcement_enabled', False))
    exit_hints_enabled = bool(guarded_cfg.get('exit_profile_hints_enabled', hints_enabled))
    exit_enforcement_enabled = bool(guarded_cfg.get('exit_profile_enforcement_enabled', False))
    layering_plan_shape_enforcement_enabled = bool(guarded_cfg.get('layering_plan_shape_enforcement_enabled', False))
    rollout_symbols = list(guarded_cfg.get('rollout_symbols') or [])
    rollout_match = (not rollout_symbols) or (symbol in rollout_symbols)
    plan_shape_rollout_symbols = list(guarded_cfg.get('layering_plan_shape_rollout_symbols') or [])
    plan_shape_rollout_fraction = float(guarded_cfg.get('layering_plan_shape_rollout_fraction') or 0.0)
    plan_shape_require_guardrails_live = bool(guarded_cfg.get('layering_plan_shape_require_guardrails_live', True))
    plan_shape_fail_closed = bool(guarded_cfg.get('layering_plan_shape_fail_closed', True))
    plan_shape_force_baseline_on_invalid = bool(guarded_cfg.get('layering_plan_shape_force_baseline_on_invalid', True))
    mode = str(normalized_policy.get('mode') or adaptive_cfg.get('mode') or 'observe_only')
    if not bool(adaptive_cfg.get('enabled', False)) or mode in {'observe_only', 'disabled'}:
        hints_enabled = False
        enforcement_enabled = False
        layering_hints_enabled = False
        layering_enforcement_enabled = False
        exit_hints_enabled = False
        exit_enforcement_enabled = False
        layering_plan_shape_enforcement_enabled = False
    merged = merge_execution_overrides_conservatively(
        baseline,
        dict(normalized_policy.get('execution_overrides') or {}),
        conservative_only=conservative_only,
    )
    if enforcement_enabled and mode in {'guarded_execute', 'full'} and rollout_match:
        effective_state = 'effective'
    elif hints_enabled or merged.get('applied_overrides') or merged.get('ignored_overrides'):
        effective_state = 'hints_only'
    else:
        effective_state = 'disabled'

    ignored_overrides = list(merged.get('ignored_overrides') or [])
    if rollout_symbols and not rollout_match:
        ignored_overrides.append({'key': 'rollout_symbols', 'requested': rollout_symbols, 'reason': 'rollout_symbol_not_matched', 'code': 'ROLLOUT_SYMBOL_NOT_MATCHED'})

    applied_keys = list((merged.get('applied_overrides') or {}).keys())
    enforced_effective = dict(baseline)
    live_effective = dict(baseline)
    enforced_fields: List[str] = []
    layering_enforced_fields: List[str] = []
    hinted_only_fields: List[str] = []
    hint_codes: List[str] = []
    field_decisions: List[Dict[str, Any]] = []
    code_map = {
        'layer_ratios': 'WOULD_REDUCE_LAYER_RATIOS',
        'layer_max_total_ratio': 'WOULD_REDUCE_LAYER_MAX_TOTAL_RATIO',
        'max_layers_per_signal': 'WOULD_REDUCE_MAX_LAYERS_PER_SIGNAL',
        'min_add_interval_seconds': 'WOULD_INCREASE_MIN_ADD_INTERVAL_SECONDS',
        'profit_only_add': 'WOULD_ENABLE_PROFIT_ONLY_ADD',
        'allow_same_bar_multiple_adds': 'WOULD_DISABLE_SAME_BAR_MULTIPLE_ADDS',
        'leverage_cap': 'WOULD_REDUCE_LEVERAGE_CAP',
        'stop_loss': 'WOULD_TIGHTEN_STOP_LOSS',
        'take_profit': 'WOULD_EARLY_TAKE_PROFIT',
        'trailing_stop': 'WOULD_TIGHTEN_TRAILING_STOP',
        'trailing_activation': 'WOULD_EARLY_ACTIVATE_TRAILING',
        'exit_min_hold_seconds': 'WOULD_DELAY_EXIT_ARMING_BY_HOLD',
        'exit_arm_profit_threshold': 'WOULD_DELAY_EXIT_ARMING_BY_PROFIT',
    }
    for field in EXECUTION_PROFILE_FIELDS:
        applied = field in applied_keys
        baseline_value = baseline.get(field)
        effective_value = merged['effective'].get(field)
        is_layer_ratio = field == 'layer_ratios'
        is_layering_guardrail = field in LAYERING_GUARDRAIL_FIELDS
        is_exit_field = field in EXIT_PROFILE_FIELDS
        enforce_via_execution = effective_state == 'effective' and applied and field == 'leverage_cap'
        enforce_via_layering = effective_state == 'effective' and applied and layering_enforcement_enabled and is_layering_guardrail
        enforce_via_exit = effective_state == 'effective' and applied and exit_enforcement_enabled and is_exit_field
        enforce_plan_shape = effective_state == 'effective' and applied and layering_enforcement_enabled and layering_plan_shape_enforcement_enabled and is_layer_ratio
        enforced = bool(enforce_via_execution or enforce_via_layering or enforce_via_exit or enforce_plan_shape)
        live_value = baseline_value
        reason = 'no_change'
        decision = 'unchanged'
        ignored = not applied

        if enforced:
            live_value = effective_value
            live_effective[field] = effective_value
            enforced_effective[field] = effective_value
            enforced_fields.append(field)
            if is_layering_guardrail:
                layering_enforced_fields.append(field)
            reason = 'conservative_tighten_enforced'
            decision = 'enforced'
            ignored = False
        elif applied and effective_state != 'effective':
            hinted_only_fields.append(field)
            reason = 'execution_enforcement_disabled'
            decision = 'applied_candidate'
            ignored = False
        elif applied and is_exit_field and not exit_enforcement_enabled:
            hinted_only_fields.append(field)
            reason = 'exit_profile_enforcement_disabled'
            decision = 'hints_only'
            ignored = False
        elif applied and is_layer_ratio and not layering_enforcement_enabled:
            hinted_only_fields.append(field)
            reason = 'layering_profile_enforcement_disabled'
            decision = 'hints_only'
            ignored = False
        elif applied and is_layer_ratio and not layering_plan_shape_enforcement_enabled:
            hinted_only_fields.append(field)
            reason = 'layering_plan_shape_enforcement_disabled'
            decision = 'hints_only'
            ignored = False
        elif applied and is_layering_guardrail and not layering_enforcement_enabled:
            hinted_only_fields.append(field)
            reason = 'layering_profile_enforcement_disabled'
            decision = 'hints_only'
            ignored = False
        elif applied:
            hinted_only_fields.append(field)
            reason = 'field_not_live'
            decision = 'applied_candidate'
            ignored = False

        field_decisions.append({
            'field': field,
            'baseline': baseline_value,
            'effective': effective_value,
            'enforced_value': effective_value if enforced else baseline_value,
            'live': live_value,
            'applied': applied,
            'ignored': ignored,
            'enforced': enforced,
            'decision': decision,
            'reason': reason,
            'hint_only': (field in hinted_only_fields),
            'plan_shape': is_layer_ratio,
            'guardrail': is_layering_guardrail,
        })
        if applied and code_map.get(field) and code_map[field] not in hint_codes:
            hint_codes.append(code_map[field])
    for ignored in ignored_overrides:
        code = ignored.get('code')
        if code and code not in hint_codes:
            hint_codes.append(code)

    baseline['layer_count'] = derive_layer_count_from_ratios(baseline.get('layer_ratios'))
    merged['effective']['layer_count'] = derive_layer_count_from_ratios(merged['effective'].get('layer_ratios'))
    live_effective['layer_count'] = derive_layer_count_from_ratios(live_effective.get('layer_ratios'))
    enforced_effective['layer_count'] = derive_layer_count_from_ratios(enforced_effective.get('layer_ratios'))
    plan_shape_snapshot = build_layering_plan_shape_snapshot(
        baseline,
        merged['effective'],
        symbol=symbol,
        mode=mode,
        conservative_only=conservative_only,
        layering_enforcement_enabled=layering_enforcement_enabled,
        layering_plan_shape_enforcement_enabled=layering_plan_shape_enforcement_enabled,
        guardrails_live=bool(layering_enforced_fields),
        rollout_symbols=plan_shape_rollout_symbols,
        rollout_fraction=plan_shape_rollout_fraction,
        require_guardrails_live=plan_shape_require_guardrails_live,
        fail_closed=plan_shape_fail_closed,
        force_baseline_on_invalid=plan_shape_force_baseline_on_invalid,
    )
    if any(item.get('key') == 'layer_ratios' for item in ignored_overrides):
        if 'layer_ratios' not in (plan_shape_snapshot.get('plan_shape_ignored_fields') or []):
            plan_shape_snapshot['plan_shape_ignored_fields'] = list(plan_shape_snapshot.get('plan_shape_ignored_fields') or []) + ['layer_ratios']
        if not plan_shape_snapshot.get('plan_shape_validation'):
            plan_shape_snapshot['plan_shape_validation'] = {}
        if not plan_shape_snapshot['plan_shape_validation'].get('accepted'):
            first_ignored = next(item for item in ignored_overrides if item.get('key') == 'layer_ratios')
            plan_shape_snapshot['plan_shape_validation']['reason'] = plan_shape_snapshot['plan_shape_validation'].get('reason') or first_ignored.get('reason')
    live_effective['layer_ratios'] = list((plan_shape_snapshot.get('live') or {}).get('layer_ratios') or live_effective.get('layer_ratios') or [])
    live_effective['layer_count'] = int((plan_shape_snapshot.get('live') or {}).get('layer_count') or derive_layer_count_from_ratios(live_effective.get('layer_ratios')))
    enforced_effective['layer_ratios'] = list(live_effective.get('layer_ratios') or [])
    enforced_effective['layer_count'] = int(live_effective.get('layer_count') or derive_layer_count_from_ratios(live_effective.get('layer_ratios')))
    if plan_shape_snapshot.get('plan_shape_really_enforced') and 'layer_ratios' not in enforced_fields:
        enforced_fields.append('layer_ratios')

    field_decisions = [item for item in field_decisions if item.get('field') != 'layer_ratios'] + list(plan_shape_snapshot.get('shape_guardrail_decisions') or [])

    return {
        'enabled': bool(hints_enabled or enforcement_enabled),
        'baseline': baseline,
        'effective': merged['effective'],
        'effective_candidate': merged['effective'],
        'live': live_effective,
        'enforced_profile': enforced_effective,
        'enforced_fields': enforced_fields,
        'hinted_only_fields': hinted_only_fields,
        'layering_enforced_fields': layering_enforced_fields,
        'execution_profile_really_enforced': bool(enforced_fields),
        'layering_profile_really_enforced': bool(layering_enforced_fields),
        'plan_shape_really_enforced': bool(plan_shape_snapshot.get('plan_shape_really_enforced', False)),
        'plan_shape_enforced_fields': list(plan_shape_snapshot.get('plan_shape_enforced_fields') or []),
        'plan_shape_ignored_fields': list(plan_shape_snapshot.get('plan_shape_ignored_fields') or []),
        'live_layer_shape_source': plan_shape_snapshot.get('live_layer_shape_source') or 'baseline',
        'shape_guardrail_decisions': list(plan_shape_snapshot.get('shape_guardrail_decisions') or []),
        'plan_shape_validation': dict(plan_shape_snapshot.get('plan_shape_validation') or {}),
        'shape_live_rollout_match': bool(plan_shape_snapshot.get('shape_live_rollout_match', False)),
        'shape_rollout_symbol_match': bool(plan_shape_snapshot.get('shape_rollout_symbol_match', False)),
        'shape_rollout_fraction_match': bool(plan_shape_snapshot.get('shape_rollout_fraction_match', False)),
        'effective_state': effective_state,
        'policy_mode': mode,
        'policy_version': normalized_policy.get('policy_version') or ADAPTIVE_POLICY_VERSION,
        'policy_source': normalized_policy.get('policy_source') or 'adaptive_regime.defaults',
        'regime_name': normalized_regime.get('name'),
        'regime_confidence': normalized_regime.get('confidence'),
        'stability_score': normalized_regime.get('stability_score'),
        'transition_risk': normalized_regime.get('transition_risk'),
        'applied_overrides': merged['applied_overrides'],
        'ignored_overrides': ignored_overrides,
        'observe_only': effective_state != 'effective',
        'hints_enabled': hints_enabled,
        'enforcement_enabled': enforcement_enabled,
        'layering_hints_enabled': layering_hints_enabled,
        'layering_enforcement_enabled': layering_enforcement_enabled,
        'exit_hints_enabled': exit_hints_enabled,
        'exit_enforcement_enabled': exit_enforcement_enabled,
        'layering_plan_shape_enforcement_enabled': layering_plan_shape_enforcement_enabled,
        'conservative_only': conservative_only,
        'rollout_symbols': rollout_symbols,
        'rollout_match': rollout_match,
        'would_tighten': bool(applied_keys),
        'would_tighten_fields': applied_keys,
        'hint_codes': hint_codes,
        'field_decisions': field_decisions,
    }


def build_risk_effective_snapshot(config_helper: Any, symbol: Optional[str], *, signal: Any = None, regime_snapshot: Optional[Dict[str, Any]] = None, policy_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    observe_payload = build_observe_only_payload(config_helper, symbol, signal=signal, regime_snapshot=regime_snapshot, policy_snapshot=policy_snapshot)
    adaptive_cfg = config_helper.get_adaptive_regime_config(symbol) if hasattr(config_helper, 'get_adaptive_regime_config') else {}
    guarded_cfg = dict(adaptive_cfg.get('guarded_execute') or {})
    normalized_policy = observe_payload['adaptive_policy_snapshot']
    normalized_regime = observe_payload['regime_snapshot']
    baseline = build_risk_baseline_snapshot(config_helper, symbol)
    conservative_only = bool(guarded_cfg.get('enforce_conservative_only', True))
    hints_enabled = bool(guarded_cfg.get('risk_hints_enabled', False))
    enforcement_enabled = bool(guarded_cfg.get('risk_enforcement_enabled', False))
    rollout_symbols = list(guarded_cfg.get('rollout_symbols') or [])
    rollout_match = (not rollout_symbols) or (symbol in rollout_symbols)
    mode = str(normalized_policy.get('mode') or adaptive_cfg.get('mode') or 'observe_only')
    enforcement_fields = [
        str(item).strip()
        for item in (guarded_cfg.get('risk_enforcement_fields') or [
            'total_margin_cap_ratio',
            'total_margin_soft_cap_ratio',
            'symbol_margin_cap_ratio',
            'base_entry_margin_ratio',
            'max_entry_margin_ratio',
            'leverage_cap',
        ]) if str(item).strip()
    ]
    merged = merge_risk_overrides_conservatively(
        baseline,
        dict(normalized_policy.get('risk_overrides') or {}),
        conservative_only=conservative_only,
    )

    if enforcement_enabled and mode in {'guarded_execute', 'full'} and rollout_match:
        effective_state = 'effective'
    elif hints_enabled or merged.get('applied_overrides') or merged.get('ignored_overrides'):
        effective_state = 'hints_only'
    else:
        effective_state = 'disabled'

    enforced_effective = dict(baseline)
    field_decisions = []
    enforced_fields = []
    would_tighten_fields = list(merged.get('applied_overrides') or {})
    hint_codes: List[str] = []

    for field in enforcement_fields:
        base_value = baseline.get(field)
        effective_value = merged['effective'].get(field)
        applied_row = (merged.get('applied_overrides') or {}).get(field)
        applied = bool(applied_row)
        can_enforce = effective_state == 'effective' and applied
        if can_enforce:
            enforced_effective[field] = effective_value
            enforced_fields.append(field)
        if applied:
            hint_codes.append(f"WOULD_TIGHTEN_{field.upper()}")
        if applied and can_enforce:
            decision = 'applied'
            reason = 'conservative_tighten_enforced'
        elif applied and effective_state != 'effective':
            decision = 'observe_only'
            reason = 'risk_enforcement_not_live'
        elif applied:
            decision = 'ignored'
            reason = 'field_not_in_enforcement_scope'
        else:
            decision = 'unchanged'
            reason = 'no_conservative_tighten'
        field_decisions.append({
            'field': field,
            'baseline': base_value,
            'effective': effective_value,
            'applied': applied,
            'ignored': not applied,
            'enforced': can_enforce,
            'decision': decision,
            'reason': reason,
        })

    for field in would_tighten_fields:
        if field not in enforcement_fields:
            applied_row = (merged.get('applied_overrides') or {}).get(field) or {}
            field_decisions.append({
                'field': field,
                'baseline': baseline.get(field),
                'effective': merged['effective'].get(field),
                'applied': True,
                'ignored': True,
                'enforced': False,
                'decision': 'ignored',
                'reason': 'field_not_in_enforcement_scope',
                'requested': applied_row.get('requested'),
            })
            hint_codes.append(f"WOULD_TIGHTEN_{field.upper()}")

    ignored_overrides = list(merged.get('ignored_overrides') or [])
    if rollout_symbols and not rollout_match:
        ignored_overrides.append({'key': 'rollout_symbols', 'requested': rollout_symbols, 'reason': 'rollout_symbol_not_matched', 'code': 'ROLL_OUT_SYMBOL_NOT_MATCHED'})
    for ignored in ignored_overrides:
        code = ignored.get('code')
        if code and code not in hint_codes:
            hint_codes.append(code)

    return {
        'enabled': bool(hints_enabled or enforcement_enabled),
        'baseline': baseline,
        'effective': merged['effective'],
        'enforced_budget': enforced_effective,
        'effective_candidate': merged['effective'],
        'effective_state': effective_state,
        'policy_mode': mode,
        'policy_version': normalized_policy.get('policy_version') or ADAPTIVE_POLICY_VERSION,
        'policy_source': normalized_policy.get('policy_source') or 'adaptive_regime.defaults',
        'regime_name': normalized_regime.get('name'),
        'regime_confidence': normalized_regime.get('confidence'),
        'stability_score': normalized_regime.get('stability_score'),
        'transition_risk': normalized_regime.get('transition_risk'),
        'applied_overrides': merged['applied_overrides'],
        'ignored_overrides': ignored_overrides,
        'observe_only': effective_state != 'effective',
        'hints_enabled': hints_enabled,
        'enforcement_enabled': enforcement_enabled,
        'conservative_only': conservative_only,
        'rollout_symbols': rollout_symbols,
        'rollout_match': rollout_match,
        'enforcement_fields': enforcement_fields,
        'would_tighten': bool(would_tighten_fields),
        'would_tighten_fields': would_tighten_fields,
        'enforced_fields': enforced_fields,
        'hint_codes': hint_codes,
        'field_decisions': field_decisions,
        'quality_scaling_enabled': baseline.get('quality_scaling_enabled', False),
        'quality_bucket': derive_quality_bucket(signal),
    }


def build_validation_effective_snapshot(config_helper: Any, symbol: Optional[str], *, signal: Any = None, regime_snapshot: Optional[Dict[str, Any]] = None, policy_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    observe_payload = build_observe_only_payload(config_helper, symbol, signal=signal, regime_snapshot=regime_snapshot, policy_snapshot=policy_snapshot)
    adaptive_cfg = config_helper.get_adaptive_regime_config(symbol) if hasattr(config_helper, 'get_adaptive_regime_config') else {}
    guarded_cfg = dict(adaptive_cfg.get('guarded_execute') or {})
    normalized_policy = observe_payload['adaptive_policy_snapshot']
    normalized_regime = observe_payload['regime_snapshot']
    baseline = build_validation_baseline_snapshot(config_helper, symbol)
    conservative_only = bool(guarded_cfg.get('enforce_conservative_only', True))
    merged = merge_validation_overrides_conservatively(
        baseline,
        dict(normalized_policy.get('validation_overrides') or {}),
        conservative_only=conservative_only,
    )
    rollout_symbols = list(guarded_cfg.get('rollout_symbols') or [])
    rollout_match = (not rollout_symbols) or (symbol in rollout_symbols)
    enforcement_categories = [str(item).strip() for item in (guarded_cfg.get('validator_enforcement_categories') or ['thresholds', 'market_guards', 'regime_guards']) if str(item).strip()]
    mode = str(normalized_policy.get('mode') or adaptive_cfg.get('mode') or 'observe_only')
    enforcement_enabled = bool(guarded_cfg.get('validator_enforcement_enabled', False))
    snapshot_enabled = bool(guarded_cfg.get('validator_snapshot_enabled', True))
    hints_enabled = bool(guarded_cfg.get('validator_hints_enabled', True))
    if not snapshot_enabled:
        effective_state = 'disabled'
    elif enforcement_enabled and mode in {'guarded_execute', 'full'} and rollout_match:
        effective_state = 'effective'
    else:
        effective_state = 'hints_only'
    if rollout_symbols and not rollout_match:
        merged['ignored_overrides'].append({'key': 'rollout_symbols', 'requested': rollout_symbols, 'reason': 'rollout_symbol_not_matched', 'code': 'ROLL_OUT_SYMBOL_NOT_MATCHED'})
    return {
        'enabled': snapshot_enabled,
        'baseline': baseline,
        'effective': merged['effective'],
        'effective_state': effective_state,
        'policy_mode': mode,
        'policy_version': normalized_policy.get('policy_version') or ADAPTIVE_POLICY_VERSION,
        'policy_source': normalized_policy.get('policy_source') or 'adaptive_regime.defaults',
        'regime_name': normalized_regime.get('name'),
        'regime_confidence': normalized_regime.get('confidence'),
        'stability_score': normalized_regime.get('stability_score'),
        'transition_risk': normalized_regime.get('transition_risk'),
        'applied_overrides': merged['applied_overrides'],
        'ignored_overrides': merged['ignored_overrides'],
        'observe_only': effective_state != 'effective',
        'hints_enabled': hints_enabled,
        'enforcement_enabled': enforcement_enabled,
        'conservative_only': conservative_only,
        'rollout_symbols': rollout_symbols,
        'rollout_match': rollout_match,
        'enforcement_categories': enforcement_categories,
    }


class RegimePolicyResolver:
    DECISION_EFFECTIVE_MODES = {'decision_only', 'guarded_execute', 'full'}

    def __init__(self, config_helper: Any):
        self.config = config_helper

    def _deep_merge(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(base or {})
        for key, value in (override or {}).items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _build_decision_policy(self, adaptive_cfg: Dict[str, Any], regime_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        defaults = adaptive_cfg.get('defaults', {}) or {}
        regimes = adaptive_cfg.get('regimes', {}) or {}
        regime_name = (regime_snapshot or {}).get('name') or (regime_snapshot or {}).get('regime') or 'unknown'
        regime_cfg = regimes.get(regime_name, {}) or {}
        decision_overrides = self._deep_merge(defaults.get('decision_overrides', {}) or {}, regime_cfg.get('decision_overrides', {}) or {})
        validation_overrides = self._deep_merge(defaults.get('validation_overrides', {}) or {}, regime_cfg.get('validation_overrides', {}) or {})
        risk_overrides = self._deep_merge(defaults.get('risk_overrides', {}) or {}, regime_cfg.get('risk_overrides', {}) or {})
        execution_overrides = self._deep_merge(defaults.get('execution_overrides', {}) or {}, regime_cfg.get('execution_overrides', {}) or {})
        return {
            'regime_name': regime_name,
            'decision_overrides': decision_overrides,
            'validation_overrides': validation_overrides,
            'risk_overrides': risk_overrides,
            'execution_overrides': execution_overrides,
        }

    def resolve(self, symbol: Optional[str], regime_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        adaptive_cfg = self.config.get_adaptive_regime_config(symbol)
        mode = adaptive_cfg.get('mode', 'observe_only')
        enabled = bool(adaptive_cfg.get('enabled', False))
        defaults = adaptive_cfg.get('defaults', {}) or {}
        matched_override = bool(symbol and (self.config.get_symbol_overrides(symbol) or {}).get('adaptive_regime'))
        policy_source = 'symbol_override' if matched_override else 'adaptive_regime.defaults'
        normalized_regime = normalize_regime_snapshot(regime_snapshot)
        decision_policy = self._build_decision_policy(adaptive_cfg, normalized_regime)
        is_effective = bool(enabled and mode in self.DECISION_EFFECTIVE_MODES and (decision_policy['decision_overrides'] or decision_policy['validation_overrides'] or decision_policy['risk_overrides']))
        notes = ['m1-observe-only' if not is_effective else 'm2-decision-aware', 'neutral-policy' if not is_effective else 'decision-policy']
        if decision_policy['decision_overrides']:
            notes.append(f"decision_overrides:{decision_policy['regime_name']}")
        if decision_policy['validation_overrides']:
            notes.append(f"validation_overrides:{decision_policy['regime_name']}")
        if decision_policy['risk_overrides']:
            notes.append(f"risk_overrides:{decision_policy['regime_name']}")
        if decision_policy['execution_overrides']:
            notes.append(f"execution_overrides:{decision_policy['regime_name']}")
        return build_neutral_policy_snapshot(
            normalized_regime,
            mode=mode,
            enabled=enabled,
            policy_version=defaults.get('policy_version', ADAPTIVE_POLICY_VERSION),
            policy_source=policy_source,
            matched_symbol=symbol,
            matched_symbol_override=matched_override,
            notes=notes,
            decision_overrides=decision_policy['decision_overrides'],
            validation_overrides=decision_policy['validation_overrides'],
            effective_overrides=(({'decision': decision_policy['decision_overrides']} if decision_policy['decision_overrides'] else {}) | ({'validation': decision_policy['validation_overrides']} if decision_policy['validation_overrides'] else {}) | ({'risk': decision_policy['risk_overrides']} if decision_policy['risk_overrides'] else {}) | ({'execution': decision_policy['execution_overrides']} if decision_policy['execution_overrides'] else {})) if is_effective else {},
            is_effective=is_effective,
            risk_overrides=decision_policy['risk_overrides'],
            execution_overrides=decision_policy['execution_overrides'],
        )


def resolve_regime_policy(config_helper: Any, symbol: Optional[str], regime_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return RegimePolicyResolver(config_helper).resolve(symbol, regime_snapshot)
