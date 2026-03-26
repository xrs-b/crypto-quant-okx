"""Adaptive regime policy resolver (M0/M1 observe-only scaffold)."""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.regime import build_regime_snapshot, normalize_regime_snapshot


ADAPTIVE_POLICY_VERSION = "adaptive_policy_v1_m0"


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
    return {
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
        'notes': list(notes or ['m0-observe-only']),
    }


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
        return dict(policy_snapshot)
    if signal is not None:
        filter_details = getattr(signal, 'filter_details', None) or {}
        existing = getattr(signal, 'adaptive_policy_snapshot', None) or filter_details.get('adaptive_policy_snapshot')
        if existing:
            return dict(existing)
    normalized_regime = get_signal_regime_snapshot(signal, regime_snapshot)
    if hasattr(config_helper, 'get_adaptive_regime_config') and hasattr(config_helper, 'get_symbol_overrides'):
        return resolve_regime_policy(config_helper, symbol, normalized_regime)
    return build_neutral_policy_snapshot(normalized_regime, matched_symbol=symbol, notes=['m0-observe-only', 'neutral-policy', 'stateless-config'])


def build_observe_only_payload(config_helper: Any, symbol: Optional[str], signal: Any = None, regime_snapshot: Optional[Dict[str, Any]] = None, policy_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    normalized_regime = get_signal_regime_snapshot(signal, regime_snapshot)
    normalized_policy = get_signal_policy_snapshot(config_helper, symbol, signal, normalized_regime, policy_snapshot)
    return {
        'regime_snapshot': normalized_regime,
        'adaptive_policy_snapshot': normalized_policy,
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
            notes=['m0-observe-only', 'neutral-policy'],
        )


def resolve_regime_policy(config_helper: Any, symbol: Optional[str], regime_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return RegimePolicyResolver(config_helper).resolve(symbol, regime_snapshot)
