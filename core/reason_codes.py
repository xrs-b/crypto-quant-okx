from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


_REASON_CODE_SPECS: Dict[str, Dict[str, str]] = {
    'PERMIT_FINAL_EXECUTION_GRANTED': {
        'disposition': 'permit',
        'stage': 'final_execution_permit',
        'family': 'permit',
        'legacy_code': 'FINAL_EXECUTION_PERMIT_GRANTED',
    },
    'DENY_ENV_TESTNET_ONLY': {
        'disposition': 'deny',
        'stage': 'final_execution_permit',
        'family': 'environment',
        'legacy_code': 'TESTNET_ONLY_EXECUTION_PERMIT',
    },
    'SKIP_SIGNAL_FILTERED': {
        'disposition': 'skip',
        'stage': 'candidate_skip',
        'family': 'signal_filter',
        'legacy_code': 'SIGNAL_FILTERED',
    },
    'DENY_RISK_GATE_BLOCKED': {
        'disposition': 'deny',
        'stage': 'risk_gate',
        'family': 'risk_gate',
        'legacy_code': 'RISK_GATE_BLOCKED',
    },
    'DENY_GUARD_SCOPED_FREEZE': {
        'disposition': 'deny',
        'stage': 'risk_gate',
        'family': 'close_outcome_guard',
        'legacy_code': 'SCOPED_WINDOW_FREEZE',
    },
    'SKIP_GUARD_SCOPED_TIGHTEN': {
        'disposition': 'skip',
        'stage': 'candidate_skip',
        'family': 'close_outcome_guard',
        'legacy_code': 'SCOPED_WINDOW_TIGHTEN',
    },
    'SKIP_GUARD_SCOPED_REVIEW': {
        'disposition': 'skip',
        'stage': 'candidate_skip',
        'family': 'close_outcome_guard',
        'legacy_code': 'SCOPED_WINDOW_REVIEW',
    },
    'SKIP_PRE_EXECUTION_INELIGIBLE': {
        'disposition': 'skip',
        'stage': 'execution_quota',
        'family': 'pre_execution',
        'legacy_code': 'INELIGIBLE_BEFORE_EXECUTION_QUOTA',
    },
    'DEFER_EXECUTION_CYCLE_QUOTA_EXHAUSTED': {
        'disposition': 'defer',
        'stage': 'execution_quota',
        'family': 'execution_quota',
        'legacy_code': 'EXECUTION_QUOTA_EXHAUSTED',
    },
    'DEFER_EXECUTION_CLUSTER_CAP_REACHED': {
        'disposition': 'defer',
        'stage': 'execution_quota',
        'family': 'execution_quota',
        'legacy_code': 'SYMBOL_CLUSTER_CAP_REACHED',
    },
    'DEFER_EXECUTION_SIDE_CAP_REACHED': {
        'disposition': 'defer',
        'stage': 'execution_quota',
        'family': 'execution_quota',
        'legacy_code': 'SIDE_EXECUTION_CAP_REACHED',
    },
    'DEFER_EXECUTION_REGIME_CAP_REACHED': {
        'disposition': 'defer',
        'stage': 'execution_quota',
        'family': 'execution_quota',
        'legacy_code': 'REGIME_EXECUTION_CAP_REACHED',
    },
    'PERMIT_EXECUTION_QUOTA_PASSED': {
        'disposition': 'permit',
        'stage': 'execution_quota',
        'family': 'execution_quota',
        'legacy_code': 'EXECUTION_QUOTA_PASSED',
    },
}

_REASON_CODE_ALIASES: Dict[str, str] = {}
for canonical, spec in _REASON_CODE_SPECS.items():
    _REASON_CODE_ALIASES[canonical] = canonical
    legacy = str(spec.get('legacy_code') or '').strip()
    if legacy:
        _REASON_CODE_ALIASES[legacy] = canonical


def normalize_reason_code(code: Any, *, fallback: Optional[str] = None) -> str:
    raw = str(code or '').strip()
    if raw:
        return _REASON_CODE_ALIASES.get(raw, raw)
    return _REASON_CODE_ALIASES.get(str(fallback or '').strip(), str(fallback or '').strip())


def reason_code_spec(code: Any, *, fallback: Optional[str] = None) -> Dict[str, str]:
    canonical = normalize_reason_code(code, fallback=fallback)
    spec = dict(_REASON_CODE_SPECS.get(canonical) or {})
    spec['code'] = canonical
    spec['legacy_code'] = spec.get('legacy_code') or canonical
    spec['disposition'] = spec.get('disposition') or 'unknown'
    spec['stage'] = spec.get('stage') or 'unknown'
    spec['family'] = spec.get('family') or 'unknown'
    return spec


def build_reason_code_details(code: Any, *, fallback: Optional[str] = None, include_legacy: bool = True) -> Dict[str, str]:
    spec = reason_code_spec(code, fallback=fallback)
    payload = {
        'reason_code': spec['code'],
        'reason_code_family': spec['family'],
        'reason_code_stage': spec['stage'],
        'reason_code_disposition': spec['disposition'],
    }
    if include_legacy:
        payload['legacy_reason_code'] = spec['legacy_code']
    return payload


def merge_reason_codes(*codes: Iterable[Any], primary: Any = None, fallback: Optional[str] = None, include_legacy: bool = True) -> List[str]:
    merged: List[str] = []

    def _add(value: Any):
        normalized = normalize_reason_code(value)
        if normalized and normalized not in merged:
            merged.append(normalized)
        if include_legacy:
            legacy = str(value or '').strip()
            if legacy and legacy not in merged:
                merged.append(legacy)

    primary_code = normalize_reason_code(primary, fallback=fallback)
    if primary_code:
        _add(primary)
        if primary_code != str(primary or '').strip():
            _add(primary_code)
    for group in codes:
        for code in group or []:
            _add(code)
    return merged
