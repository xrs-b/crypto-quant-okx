from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

from analytics.parameter_tuning_advice import build_parameter_tuning_advice_payload
from core.config import Config

PARAMETER_SPECS: Dict[str, Dict[str, Any]] = {
    'stale_signal_ttl_seconds': {
        'config_key': 'trading.layering.stale_signal_ttl_seconds',
        'patch_parts': ['trading', 'layering', 'stale_signal_ttl_seconds'],
        'kind': 'seconds',
    },
    'entry_drift_tolerance_bps': {
        'config_key': 'trading.layering.entry_drift_tolerance_bps',
        'patch_parts': ['trading', 'layering', 'entry_drift_tolerance_bps'],
        'kind': 'bps',
    },
    'exit_min_hold_seconds': {
        'config_key': 'trading.exit_min_hold_seconds',
        'patch_parts': ['trading', 'exit_min_hold_seconds'],
        'kind': 'seconds',
    },
    'exit_arm_profit_threshold': {
        'config_key': 'trading.exit_arm_profit_threshold',
        'patch_parts': ['trading', 'exit_arm_profit_threshold'],
        'kind': 'ratio',
    },
}


class _PatchConfigView(Config):
    def has_symbol_override(self, symbol: str, key: str) -> bool:
        return self._get_nested_value(self.get_symbol_overrides(symbol), key, _MISSING) is not _MISSING


_MISSING = object()


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ''):
            return None
        return float(value)
    except Exception:
        return None


def _round_seconds(value: float, *, direction: str) -> int:
    step = 30
    if direction == 'down':
        return max(step, int(math.floor(value / step) * step))
    if direction == 'up':
        return max(step, int(math.ceil(value / step) * step))
    return max(step, int(round(value / step) * step))


def _round_bps(value: float, *, direction: str) -> float:
    step = 5.0
    if direction == 'down':
        rounded = math.floor(value / step) * step
    elif direction == 'up':
        rounded = math.ceil(value / step) * step
    else:
        rounded = round(value / step) * step
    if abs(rounded - round(rounded)) < 1e-9:
        return int(round(rounded))
    return round(rounded, 2)


def _round_ratio(value: float, *, direction: str) -> float:
    step = 0.0025
    if direction == 'down':
        rounded = math.floor(value / step) * step
    elif direction == 'up':
        rounded = math.ceil(value / step) * step
    else:
        rounded = round(value / step) * step
    return round(max(0.0, rounded), 4)


def _format_value(value: Any) -> str:
    if value is None:
        return 'null'
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f'{value:.4f}'.rstrip('0').rstrip('.')
    return str(value)


def _nested_patch(parts: Sequence[str], value: Any) -> Dict[str, Any]:
    patch: Dict[str, Any] = value
    for part in reversed(parts):
        patch = {part: patch}
    return patch


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _infer_stale_signal_ttl(recommendation: Dict[str, Any], evidence: Dict[str, Any], current: Any) -> tuple[Any, str]:
    current_value = _safe_float(current)
    if current_value is None:
        return current, 'current value unavailable; keep manual review'
    signal_age = evidence.get('signal_age_seconds_at_entry') or {}
    age_median = _safe_float(signal_age.get('median'))
    age_p90 = _safe_float(signal_age.get('p90'))
    if recommendation.get('action') == 'tighten':
        candidates = [current_value * 0.8]
        if age_median is not None:
            candidates.append(age_median)
        elif age_p90 is not None:
            candidates.append(age_p90 * 0.85)
        target = max(30.0, min(candidates))
        suggested = min(int(current_value), _round_seconds(target, direction='down'))
        if suggested >= int(current_value):
            suggested = max(30, int(current_value) - 30)
        return suggested, 'tighten toward observed signal-age median / conservative 20% cut'
    return int(current_value), 'action is hold-like; no numeric patch suggested'


def _infer_entry_drift_tolerance(recommendation: Dict[str, Any], evidence: Dict[str, Any], current: Any) -> tuple[Any, str]:
    current_value = _safe_float(current)
    if current_value is None:
        return current, 'current value unavailable; keep manual review'
    drift_stats = evidence.get('entry_drift_pct_from_signal') or {}
    drift_p90_pct = _safe_float(drift_stats.get('p90'))
    candidates = [current_value * 0.8]
    if drift_p90_pct is not None:
        candidates.append(abs(drift_p90_pct) * 100 * 0.9)
    if recommendation.get('action') == 'tighten':
        target = max(5.0, min(candidates))
        suggested = min(current_value, _round_bps(target, direction='down'))
        if suggested >= current_value:
            suggested = max(5, _round_bps(current_value - 5, direction='down'))
        return suggested, 'tighten via conservative 20% cut, bounded by observed drift p90'
    return _round_bps(current_value, direction='nearest'), 'action is hold-like; no numeric patch suggested'


def _infer_exit_min_hold(recommendation: Dict[str, Any], evidence: Dict[str, Any], current: Any) -> tuple[Any, str]:
    current_value = _safe_float(current)
    if current_value is None:
        return current, 'current value unavailable; keep manual review'
    guard = evidence.get('exit_guard') or {}
    hold_stats = guard.get('hold_seconds') or {}
    hold_median = _safe_float(hold_stats.get('median'))
    if recommendation.get('action') == 'loosen' and hold_median is not None:
        target = max(30.0, hold_median * 1.2)
        suggested = min(int(current_value), _round_seconds(target, direction='up'))
        if suggested >= int(current_value):
            suggested = max(30, int(current_value) - 30)
        return suggested, 'shorten min-hold to roughly median hold + 20% buffer'
    return int(current_value), 'action is hold-like; no numeric patch suggested'


def _infer_exit_arm_profit_threshold(recommendation: Dict[str, Any], evidence: Dict[str, Any], current: Any) -> tuple[Any, str]:
    current_value = _safe_float(current)
    if current_value is None:
        return current, 'current value unavailable or already disabled; keep manual review'
    guard = evidence.get('exit_guard') or {}
    armed_share = _safe_float(guard.get('exit_armed_share')) or 0.0
    if recommendation.get('action') == 'loosen':
        factor = 0.75 if armed_share <= 50 else 0.85
        target = max(0.0, current_value * factor)
        suggested = min(current_value, _round_ratio(target, direction='down'))
        if suggested >= current_value and current_value > 0:
            suggested = max(0.0, _round_ratio(current_value - 0.0025, direction='down'))
        return suggested, 'lower arm-profit threshold by 15%~25% depending on current arming share'
    return _round_ratio(current_value, direction='nearest'), 'action is hold-like; no numeric patch suggested'


def _infer_suggested_value(parameter: str, recommendation: Dict[str, Any], evidence: Dict[str, Any], current: Any) -> tuple[Any, str]:
    if parameter == 'stale_signal_ttl_seconds':
        return _infer_stale_signal_ttl(recommendation, evidence, current)
    if parameter == 'entry_drift_tolerance_bps':
        return _infer_entry_drift_tolerance(recommendation, evidence, current)
    if parameter == 'exit_min_hold_seconds':
        return _infer_exit_min_hold(recommendation, evidence, current)
    if parameter == 'exit_arm_profit_threshold':
        return _infer_exit_arm_profit_threshold(recommendation, evidence, current)
    return current, 'no drafting rule configured'


def _build_change_review(cfg: _PatchConfigView, symbol: str, evidence: Dict[str, Any], recommendation: Dict[str, Any]) -> Dict[str, Any]:
    parameter = str(recommendation.get('parameter'))
    spec = PARAMETER_SPECS[parameter]
    config_key = spec['config_key']
    current = cfg.get_symbol_value(symbol, config_key)
    has_override = cfg.has_symbol_override(symbol, config_key)
    suggested, heuristic = _infer_suggested_value(parameter, recommendation, evidence, current)
    changed = suggested != current and recommendation.get('action') not in {'hold', 'slightly_loosen_or_hold'}
    patch_path = '.'.join(['symbol_overrides', symbol, *spec['patch_parts']])
    return {
        'parameter': parameter,
        'config_key': config_key,
        'patch_path': patch_path,
        'current': current,
        'suggested': suggested,
        'action': recommendation.get('action'),
        'changed': changed,
        'current_source': 'symbol_override' if has_override else 'effective_global_or_default',
        'reason': recommendation.get('reason'),
        'evidence': recommendation.get('evidence_line'),
        'advice': recommendation.get('suggestion'),
        'heuristic': heuristic,
    }


def _build_symbol_patch(symbol: str, reviews: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    patch: Dict[str, Any] = {}
    for review in reviews:
        if not review.get('changed'):
            continue
        spec = PARAMETER_SPECS[review['parameter']]
        fragment = {'symbol_overrides': {symbol: _nested_patch(spec['patch_parts'], review['suggested'])}}
        patch = _deep_merge(patch, fragment)
    return patch


def build_parameter_tuning_patch_payload(
    db_path: str,
    *,
    config_path: str | None = None,
    view: str = 'both',
    hours: float = 24.0,
    limit: int = 50,
    symbols: Optional[Sequence[str]] = None,
    fetch_limit: Optional[int] = None,
) -> Dict[str, Any]:
    advice_payload = build_parameter_tuning_advice_payload(
        db_path,
        view=view,
        hours=hours,
        limit=limit,
        symbols=symbols,
        fetch_limit=fetch_limit,
    )
    cfg = _PatchConfigView(config_path)
    views: Dict[str, Any] = {}
    for scope_name, scope_payload in (advice_payload.get('views') or {}).items():
        symbol_entries: List[Dict[str, Any]] = []
        for symbol_payload in scope_payload.get('symbols') or []:
            symbol = symbol_payload.get('symbol')
            evidence = symbol_payload.get('evidence') or {}
            reviews = [
                _build_change_review(cfg, symbol, evidence, recommendation)
                for recommendation in (symbol_payload.get('recommendations') or [])
                if recommendation.get('parameter') in PARAMETER_SPECS
            ]
            symbol_entries.append({
                'symbol': symbol,
                'summary': symbol_payload.get('summary') or {},
                'evidence': evidence,
                'recommendations': symbol_payload.get('recommendations') or [],
                'change_reviews': reviews,
                'yaml_patch': _build_symbol_patch(symbol, reviews),
            })
        views[scope_name] = {
            'scope_name': scope_name,
            'scope_text': scope_payload.get('scope_text'),
            'issue_summary_text': scope_payload.get('issue_summary_text'),
            'symbols': symbol_entries,
        }
    return {
        'schema_version': 'parameter_tuning_patch_v1',
        'mode': 'advice_patch_draft_only',
        'db_path': db_path,
        'config_path': str(Path(cfg.config_path)),
        'view': view,
        'hours': hours,
        'limit': limit,
        'symbols': advice_payload.get('symbols') or list(symbols or []),
        'fetch_limit': fetch_limit,
        'advice_payload': advice_payload,
        'views': views,
    }


def format_parameter_tuning_patch_text(payload: Dict[str, Any]) -> str:
    lines = [
        'Parameter tuning patch draft (advice only, no config writes)',
        f"Mode: {payload.get('mode')}",
        f"Config source: {payload.get('config_path')}",
        'Note: current -> suggested is a draft for manual review; patch YAML is not auto-applied.',
    ]
    for scope_name, scope_payload in (payload.get('views') or {}).items():
        lines.append('')
        lines.append(f"=== Scope: {scope_payload.get('scope_text') or scope_name} ===")
        issue_summary = scope_payload.get('issue_summary_text')
        if issue_summary:
            lines.append(issue_summary)
        for symbol_payload in scope_payload.get('symbols') or []:
            lines.append('')
            lines.append(f"{symbol_payload.get('symbol')} patch draft")
            for review in symbol_payload.get('change_reviews') or []:
                marker = 'PATCH' if review.get('changed') else 'HOLD'
                lines.append(
                    f"  - [{marker}] {review.get('parameter')}: {_format_value(review.get('current'))} -> {_format_value(review.get('suggested'))}"
                )
                lines.append(f"    path: {review.get('patch_path')} ({review.get('current_source')})")
                lines.append(f"    reason: {review.get('reason')}")
                lines.append(f"    evidence: {review.get('evidence')}")
                lines.append(f"    basis: {review.get('heuristic')}")
                lines.append(f"    advice: {review.get('advice')}")
            yaml_patch = symbol_payload.get('yaml_patch') or {}
            if yaml_patch:
                lines.append('    yaml_patch:')
                for patch_line in yaml.safe_dump(yaml_patch, allow_unicode=True, sort_keys=False).rstrip().splitlines():
                    lines.append(f'      {patch_line}')
            else:
                lines.append('    yaml_patch: {}  # no concrete change proposed in this scope')
    return '\n'.join(lines)


def format_parameter_tuning_patch_yaml(payload: Dict[str, Any]) -> str:
    yaml_payload = {
        'schema_version': payload.get('schema_version'),
        'mode': payload.get('mode'),
        'db_path': payload.get('db_path'),
        'config_path': payload.get('config_path'),
        'view': payload.get('view'),
        'hours': payload.get('hours'),
        'limit': payload.get('limit'),
        'symbols': payload.get('symbols'),
        'views': payload.get('views'),
    }
    return yaml.safe_dump(yaml_payload, allow_unicode=True, sort_keys=False)
