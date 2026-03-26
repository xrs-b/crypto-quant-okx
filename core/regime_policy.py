"""Adaptive regime policy resolver (M0/M1 observe-only scaffold)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.regime import build_regime_snapshot, normalize_regime_snapshot
from core.risk_budget import DEFAULT_RISK_BUDGET, get_risk_budget_config


ADAPTIVE_POLICY_VERSION = "adaptive_policy_v1_m1"


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
        'execution_overrides': {},
        'effective_overrides': dict(effective_overrides or {}),
        'is_effective': bool(is_effective),
        'notes': list(notes or ['m1-observe-only']),
    }
    return enrich_policy_snapshot(base)


def get_signal_regime_snapshot(signal: Any = None, regime_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if regime_snapshot:
        return normalize_regime_snapshot(regime_snapshot)
    if signal is None:
        return build_regime_snapshot('unknown', 0.0, {}, 'missing regime snapshot')
    raw = getattr(signal, 'regime_snapshot', None) or getattr(signal, 'regime_info', None) or getattr(signal, 'market_context', {}).get('regime_snapshot')
    if raw:
        return normalize_regime_snapshot(raw)
    market_context = getattr(signal, 'market_context', {}) or {}
    return build_regime_snapshot(
        market_context.get('regime', 'unknown'),
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
    normalized_regime = get_signal_regime_snapshot(signal, regime_snapshot)
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
        return {
            'regime_name': regime_name,
            'decision_overrides': decision_overrides,
            'validation_overrides': validation_overrides,
            'risk_overrides': risk_overrides,
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
            effective_overrides=(({'decision': decision_policy['decision_overrides']} if decision_policy['decision_overrides'] else {}) | ({'validation': decision_policy['validation_overrides']} if decision_policy['validation_overrides'] else {}) | ({'risk': decision_policy['risk_overrides']} if decision_policy['risk_overrides'] else {})) if is_effective else {},
            is_effective=is_effective,
            risk_overrides=decision_policy['risk_overrides'],
        )


def resolve_regime_policy(config_helper: Any, symbol: Optional[str], regime_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return RegimePolicyResolver(config_helper).resolve(symbol, regime_snapshot)
