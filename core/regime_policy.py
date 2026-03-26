"""Adaptive regime policy resolver (M0/M1 observe-only scaffold)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.regime import build_regime_snapshot, normalize_regime_snapshot


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
        'decision_overrides': {},
        'validation_overrides': {},
        'risk_overrides': {},
        'execution_overrides': {},
        'effective_overrides': {},
        'is_effective': False,
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
    return {
        'regime_snapshot': normalized_regime,
        'adaptive_policy_snapshot': normalized_policy,
        'regime_observe_only': regime_view,
        'adaptive_policy_observe_only': policy_view,
        'observe_only_summary': bundle['summary'],
        'observe_only_tags': bundle['tags'],
        'observe_only_notes': bundle['notes'],
        'observe_only_phase': bundle['phase'],
        'observe_only_state': bundle['state'],
        'observe_only': True,
    }


class RegimePolicyResolver:
    def __init__(self, config_helper: Any):
        self.config = config_helper

    def resolve(self, symbol: Optional[str], regime_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        adaptive_cfg = self.config.get_adaptive_regime_config(symbol)
        mode = adaptive_cfg.get('mode', 'observe_only')
        enabled = bool(adaptive_cfg.get('enabled', False))
        defaults = adaptive_cfg.get('defaults', {}) or {}
        matched_override = bool(symbol and (self.config.get_symbol_overrides(symbol) or {}).get('adaptive_regime'))
        policy_source = 'symbol_override' if matched_override else 'adaptive_regime.defaults'
        return build_neutral_policy_snapshot(
            regime_snapshot,
            mode=mode,
            enabled=enabled,
            policy_version=defaults.get('policy_version', ADAPTIVE_POLICY_VERSION),
            policy_source=policy_source,
            matched_symbol=symbol,
            matched_symbol_override=matched_override,
            notes=['m1-observe-only', 'neutral-policy'],
        )


def resolve_regime_policy(config_helper: Any, symbol: Optional[str], regime_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return RegimePolicyResolver(config_helper).resolve(symbol, regime_snapshot)
