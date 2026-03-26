"""Adaptive regime policy resolver (M0/M1 observe-only scaffold)."""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.regime import normalize_regime_snapshot


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
