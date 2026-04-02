from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence

from analytics.outcome_attribution_report import _describe_numeric, _format_distribution, _share
from scripts.outcome_issue_summary import (
    DEFAULT_HOURS,
    DEFAULT_LIMIT,
    DEFAULT_SYMBOLS,
    build_outcome_issue_summary_payload,
)

MIN_SAMPLE_SIZE = 3
HIGH_SHARE_THRESHOLD = 35.0
MEDIUM_SHARE_THRESHOLD = 20.0
LOW_SHARE_THRESHOLD = 5.0


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ''):
            return None
        return float(value)
    except Exception:
        return None


def _describe(values: Iterable[Any]) -> Dict[str, Any]:
    return _describe_numeric(values)


def _top_reason(counts: Dict[str, int]) -> Optional[str]:
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _format_optional_number(value: Any, digits: int = 2) -> str:
    numeric = _safe_float(value)
    if numeric is None:
        return 'n/a'
    if float(numeric).is_integer():
        return str(int(numeric))
    return f'{numeric:.{digits}f}'


def _pick_strength(share: float) -> str:
    if share >= HIGH_SHARE_THRESHOLD:
        return 'high'
    if share >= MEDIUM_SHARE_THRESHOLD:
        return 'medium'
    return 'low'


def _pick_priority(share: float, sample_count: int) -> str:
    if sample_count < MIN_SAMPLE_SIZE:
        return 'observe'
    if share >= HIGH_SHARE_THRESHOLD:
        return 'high'
    if share >= MEDIUM_SHARE_THRESHOLD:
        return 'medium'
    return 'low'


def _rule_context(evidence: Dict[str, Any], row_count: int) -> Dict[str, Any]:
    return {
        'min_sample_size': MIN_SAMPLE_SIZE,
        'high_share_threshold_pct': HIGH_SHARE_THRESHOLD,
        'medium_share_threshold_pct': MEDIUM_SHARE_THRESHOLD,
        'low_share_threshold_pct': LOW_SHARE_THRESHOLD,
        'row_count': row_count,
        'evidence': evidence,
    }


def _extract_symbol_rows(report: Dict[str, Any], symbol: str) -> List[Dict[str, Any]]:
    return [row for row in (report.get('structured_rows') or []) if row.get('symbol') == symbol]


def _collect_exit_guard_stats(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    guard_rows = [row.get('exit_guard_state') for row in rows if isinstance(row.get('exit_guard_state'), dict)]
    min_hold_values = [guard.get('min_hold_seconds') for guard in guard_rows]
    profit_threshold_values = [guard.get('profit_threshold') for guard in guard_rows]
    hold_seconds_values = [guard.get('hold_seconds') for guard in guard_rows]
    armed_by_counts = Counter(str(guard.get('armed_by') or 'none') for guard in guard_rows)
    exit_armed_count = sum(1 for guard in guard_rows if guard.get('exit_armed'))
    pre_arm_guard_count = sum(1 for guard in guard_rows if guard.get('pre_arm_exit'))
    return {
        'guard_row_count': len(guard_rows),
        'exit_armed_count': exit_armed_count,
        'exit_armed_share': _share(exit_armed_count, len(guard_rows)),
        'pre_arm_guard_count': pre_arm_guard_count,
        'pre_arm_guard_share': _share(pre_arm_guard_count, len(guard_rows)),
        'min_hold_seconds': _describe(min_hold_values),
        'profit_threshold': _describe(profit_threshold_values),
        'hold_seconds': _describe(hold_seconds_values),
        'armed_by_counts': dict(sorted(armed_by_counts.items(), key=lambda item: (-item[1], item[0]))),
    }


def _build_symbol_evidence(report: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    rows = _extract_symbol_rows(report, symbol)
    row_count = len(rows)
    reason_counts = Counter(str(row.get('close_reason_code') or 'unknown') for row in rows)
    instant_stopout_count = sum(1 for row in rows if row.get('instant_stopout'))
    pre_arm_exit_count = sum(1 for row in rows if row.get('pre_arm_exit'))
    stale_signal_breach_count = sum(1 for row in rows if row.get('stale_signal_breach'))
    drift_breach_count = sum(1 for row in rows if row.get('drift_breach'))
    stale_ttl_stats = _describe(row.get('stale_signal_ttl_seconds') for row in rows)
    drift_tolerance_bps_stats = _describe(row.get('drift_tolerance_bps') for row in rows)
    signal_age_stats = _describe(row.get('signal_age_seconds_at_entry') for row in rows)
    entry_drift_pct_stats = _describe(row.get('entry_drift_pct_from_signal') for row in rows)
    exit_guard_stats = _collect_exit_guard_stats(rows)
    return {
        'symbol': symbol,
        'sample_count': row_count,
        'instant_stopout_count': instant_stopout_count,
        'instant_stopout_share': _share(instant_stopout_count, row_count),
        'pre_arm_exit_count': pre_arm_exit_count,
        'pre_arm_exit_share': _share(pre_arm_exit_count, row_count),
        'stale_signal_breach_count': stale_signal_breach_count,
        'stale_signal_breach_share': _share(stale_signal_breach_count, row_count),
        'drift_breach_count': drift_breach_count,
        'drift_breach_share': _share(drift_breach_count, row_count),
        'close_reason_code_counts': dict(sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))),
        'dominant_close_reason_code': _top_reason(reason_counts),
        'stale_signal_ttl_seconds': stale_ttl_stats,
        'entry_drift_tolerance_bps': drift_tolerance_bps_stats,
        'signal_age_seconds_at_entry': signal_age_stats,
        'entry_drift_pct_from_signal': entry_drift_pct_stats,
        'exit_guard': exit_guard_stats,
    }


def _recommend_stale_signal_ttl(evidence: Dict[str, Any]) -> Dict[str, Any]:
    share = float(evidence.get('stale_signal_breach_share') or 0.0)
    ttl_stats = evidence.get('stale_signal_ttl_seconds') or {}
    signal_age_stats = evidence.get('signal_age_seconds_at_entry') or {}
    sample_count = int(evidence.get('sample_count') or 0)
    priority = _pick_priority(share, sample_count)
    current_anchor = ttl_stats.get('median') or ttl_stats.get('mean')
    age_p90 = signal_age_stats.get('p90')
    age_median = signal_age_stats.get('median')
    if sample_count < MIN_SAMPLE_SIZE:
        return {
            'parameter': 'stale_signal_ttl_seconds',
            'action': 'hold',
            'priority': 'observe',
            'confidence': 'low',
            'reason': f'sample<{MIN_SAMPLE_SIZE}, only {sample_count} structured trade(s)',
            'evidence_line': f"stale_signal_breach={evidence.get('stale_signal_breach_count', 0)}/{sample_count} ({share}%)",
            'suggestion': 'Keep current TTL for now; collect more structured samples before moving this knob.',
            'rule': _rule_context(evidence, sample_count),
        }
    if share >= MEDIUM_SHARE_THRESHOLD:
        target_hint = age_median if age_median is not None else age_p90
        return {
            'parameter': 'stale_signal_ttl_seconds',
            'action': 'tighten',
            'priority': priority,
            'confidence': _pick_strength(share),
            'reason': f'stale_signal_breach share {share}% >= {MEDIUM_SHARE_THRESHOLD}%',
            'evidence_line': (
                f"stale_signal_breach={evidence.get('stale_signal_breach_count', 0)}/{sample_count} ({share}%), "
                f"signal_age p90={_format_optional_number(age_p90)}s vs ttl median={_format_optional_number(current_anchor)}s"
            ),
            'suggestion': (
                'Tighten stale_signal_ttl_seconds. Recent losers include too many late entries; '
                f'current TTL anchor≈{_format_optional_number(current_anchor)}s, while signal age median/p90 is '
                f"{_format_optional_number(age_median)}s/{_format_optional_number(age_p90)}s. "
                f'Prefer moving TTL closer to { _format_optional_number(target_hint) }s than keeping a looser stale window.'
            ),
            'rule': _rule_context(evidence, sample_count),
        }
    if share <= LOW_SHARE_THRESHOLD and current_anchor is not None and age_p90 is not None and current_anchor > age_p90 * 1.8:
        return {
            'parameter': 'stale_signal_ttl_seconds',
            'action': 'slightly_loosen_or_hold',
            'priority': 'low',
            'confidence': 'low',
            'reason': f'stale_signal_breach share {share}% <= {LOW_SHARE_THRESHOLD}% and TTL is much wider than observed age p90',
            'evidence_line': (
                f"stale_signal_breach={evidence.get('stale_signal_breach_count', 0)}/{sample_count} ({share}%), "
                f"signal_age p90={_format_optional_number(age_p90)}s, ttl median={_format_optional_number(current_anchor)}s"
            ),
            'suggestion': 'TTL is not the current bottleneck. Hold it, or only very slightly loosen if you want more fill rate.',
            'rule': _rule_context(evidence, sample_count),
        }
    return {
        'parameter': 'stale_signal_ttl_seconds',
        'action': 'hold',
        'priority': priority,
        'confidence': 'low',
        'reason': 'stale-signal evidence is not concentrated enough to justify moving TTL',
        'evidence_line': f"stale_signal_breach={evidence.get('stale_signal_breach_count', 0)}/{sample_count} ({share}%)",
        'suggestion': 'Hold stale_signal_ttl_seconds for now.',
        'rule': _rule_context(evidence, sample_count),
    }


def _recommend_entry_drift_tolerance(evidence: Dict[str, Any]) -> Dict[str, Any]:
    share = float(evidence.get('drift_breach_share') or 0.0)
    sample_count = int(evidence.get('sample_count') or 0)
    priority = _pick_priority(share, sample_count)
    tolerance_stats = evidence.get('entry_drift_tolerance_bps') or {}
    drift_stats = evidence.get('entry_drift_pct_from_signal') or {}
    tolerance_anchor = tolerance_stats.get('median') or tolerance_stats.get('mean')
    drift_p90_pct = drift_stats.get('p90')
    drift_p90_bps = abs(float(drift_p90_pct)) * 100 if drift_p90_pct is not None else None
    if sample_count < MIN_SAMPLE_SIZE:
        return {
            'parameter': 'entry_drift_tolerance_bps',
            'action': 'hold',
            'priority': 'observe',
            'confidence': 'low',
            'reason': f'sample<{MIN_SAMPLE_SIZE}, only {sample_count} structured trade(s)',
            'evidence_line': f"drift_breach={evidence.get('drift_breach_count', 0)}/{sample_count} ({share}%)",
            'suggestion': 'Keep current drift tolerance until there are enough trades.',
            'rule': _rule_context(evidence, sample_count),
        }
    if share >= MEDIUM_SHARE_THRESHOLD:
        return {
            'parameter': 'entry_drift_tolerance_bps',
            'action': 'tighten',
            'priority': priority,
            'confidence': _pick_strength(share),
            'reason': f'drift_breach share {share}% >= {MEDIUM_SHARE_THRESHOLD}%',
            'evidence_line': (
                f"drift_breach={evidence.get('drift_breach_count', 0)}/{sample_count} ({share}%), "
                f"observed |drift| p90≈{_format_optional_number(drift_p90_bps)}bps vs tolerance median={_format_optional_number(tolerance_anchor)}bps"
            ),
            'suggestion': (
                'Tighten entry_drift_tolerance_bps. Too many entries are drifting past the intended reference price; '
                f'observed |drift| p90 is about {_format_optional_number(drift_p90_bps)}bps, '
                f'while current tolerance anchor is {_format_optional_number(tolerance_anchor)}bps.'
            ),
            'rule': _rule_context(evidence, sample_count),
        }
    if share <= LOW_SHARE_THRESHOLD and tolerance_anchor is not None and drift_p90_bps is not None and tolerance_anchor < drift_p90_bps * 0.8:
        return {
            'parameter': 'entry_drift_tolerance_bps',
            'action': 'slightly_loosen_or_hold',
            'priority': 'low',
            'confidence': 'low',
            'reason': f'drift_breach share {share}% <= {LOW_SHARE_THRESHOLD}% and configured tolerance is tighter than observed p90 drift',
            'evidence_line': (
                f"drift_breach={evidence.get('drift_breach_count', 0)}/{sample_count} ({share}%), "
                f"|drift| p90≈{_format_optional_number(drift_p90_bps)}bps, tolerance median={_format_optional_number(tolerance_anchor)}bps"
            ),
            'suggestion': 'Drift guard is not the obvious failure source. Hold it, or only slightly loosen if fill rate matters more than precision.',
            'rule': _rule_context(evidence, sample_count),
        }
    return {
        'parameter': 'entry_drift_tolerance_bps',
        'action': 'hold',
        'priority': priority,
        'confidence': 'low',
        'reason': 'drift-breach evidence is not strong enough to move tolerance',
        'evidence_line': f"drift_breach={evidence.get('drift_breach_count', 0)}/{sample_count} ({share}%)",
        'suggestion': 'Hold entry_drift_tolerance_bps for now.',
        'rule': _rule_context(evidence, sample_count),
    }


def _recommend_exit_min_hold(evidence: Dict[str, Any]) -> Dict[str, Any]:
    sample_count = int(evidence.get('sample_count') or 0)
    pre_arm_share = float(evidence.get('pre_arm_exit_share') or 0.0)
    instant_share = float(evidence.get('instant_stopout_share') or 0.0)
    guard = evidence.get('exit_guard') or {}
    hold_stats = guard.get('hold_seconds') or {}
    min_hold_stats = guard.get('min_hold_seconds') or {}
    priority = _pick_priority(max(pre_arm_share, instant_share), sample_count)
    hold_median = hold_stats.get('median')
    min_hold_median = min_hold_stats.get('median')
    if sample_count < MIN_SAMPLE_SIZE or not guard.get('guard_row_count'):
        return {
            'parameter': 'exit_min_hold_seconds',
            'action': 'hold',
            'priority': 'observe',
            'confidence': 'low',
            'reason': 'not enough exit_guard_state coverage to judge min-hold behavior',
            'evidence_line': f"pre_arm_exit={evidence.get('pre_arm_exit_count', 0)}/{sample_count} ({pre_arm_share}%), exit_guard_coverage={guard.get('guard_row_count', 0)}/{sample_count}",
            'suggestion': 'Hold exit_min_hold_seconds until exit_guard_state coverage improves.',
            'rule': _rule_context(evidence, sample_count),
        }
    if pre_arm_share >= MEDIUM_SHARE_THRESHOLD and hold_median is not None and min_hold_median is not None and hold_median < min_hold_median:
        return {
            'parameter': 'exit_min_hold_seconds',
            'action': 'loosen',
            'priority': priority,
            'confidence': _pick_strength(pre_arm_share),
            'reason': f'pre_arm_exit share {pre_arm_share}% >= {MEDIUM_SHARE_THRESHOLD}% and median hold is below configured min hold',
            'evidence_line': (
                f"pre_arm_exit={evidence.get('pre_arm_exit_count', 0)}/{sample_count} ({pre_arm_share}%), "
                f"hold median={_format_optional_number(hold_median)}s vs min_hold median={_format_optional_number(min_hold_median)}s"
            ),
            'suggestion': (
                'Loosen exit_min_hold_seconds (shorten it) so exits can arm earlier. '
                f'Recent pre-arm exits are clustering before the current min-hold window finishes: '
                f'hold median {_format_optional_number(hold_median)}s < min_hold median {_format_optional_number(min_hold_median)}s.'
            ),
            'rule': _rule_context(evidence, sample_count),
        }
    if pre_arm_share <= LOW_SHARE_THRESHOLD and instant_share <= LOW_SHARE_THRESHOLD:
        return {
            'parameter': 'exit_min_hold_seconds',
            'action': 'hold',
            'priority': 'low',
            'confidence': 'low',
            'reason': 'pre-arm / instant-stopout pressure is low',
            'evidence_line': f"pre_arm_exit={evidence.get('pre_arm_exit_count', 0)}/{sample_count} ({pre_arm_share}%), instant_stopout={evidence.get('instant_stopout_count', 0)}/{sample_count} ({instant_share}%)",
            'suggestion': 'Hold exit_min_hold_seconds; it is not the main pain point in this window.',
            'rule': _rule_context(evidence, sample_count),
        }
    return {
        'parameter': 'exit_min_hold_seconds',
        'action': 'hold',
        'priority': priority,
        'confidence': 'low',
        'reason': 'pre-arm evidence exists but min-hold mismatch is not strong enough yet',
        'evidence_line': f"pre_arm_exit={evidence.get('pre_arm_exit_count', 0)}/{sample_count} ({pre_arm_share}%), hold median={_format_optional_number(hold_median)}s, min_hold median={_format_optional_number(min_hold_median)}s",
        'suggestion': 'Keep exit_min_hold_seconds unchanged for now and continue observing.',
        'rule': _rule_context(evidence, sample_count),
    }


def _recommend_exit_arm_profit_threshold(evidence: Dict[str, Any]) -> Dict[str, Any]:
    sample_count = int(evidence.get('sample_count') or 0)
    pre_arm_share = float(evidence.get('pre_arm_exit_share') or 0.0)
    instant_share = float(evidence.get('instant_stopout_share') or 0.0)
    guard = evidence.get('exit_guard') or {}
    threshold_stats = guard.get('profit_threshold') or {}
    armed_share = float(guard.get('exit_armed_share') or 0.0)
    threshold_anchor = threshold_stats.get('median') or threshold_stats.get('mean')
    priority = _pick_priority(max(pre_arm_share, instant_share), sample_count)
    if sample_count < MIN_SAMPLE_SIZE or not guard.get('guard_row_count'):
        return {
            'parameter': 'exit_arm_profit_threshold',
            'action': 'hold',
            'priority': 'observe',
            'confidence': 'low',
            'reason': 'not enough exit_guard_state coverage to judge arm-profit behavior',
            'evidence_line': f"pre_arm_exit={evidence.get('pre_arm_exit_count', 0)}/{sample_count} ({pre_arm_share}%), exit_guard_coverage={guard.get('guard_row_count', 0)}/{sample_count}",
            'suggestion': 'Hold exit_arm_profit_threshold until more exit_guard_state is recorded.',
            'rule': _rule_context(evidence, sample_count),
        }
    if pre_arm_share >= MEDIUM_SHARE_THRESHOLD and armed_share <= (100.0 - MEDIUM_SHARE_THRESHOLD):
        return {
            'parameter': 'exit_arm_profit_threshold',
            'action': 'loosen',
            'priority': priority,
            'confidence': _pick_strength(pre_arm_share),
            'reason': f'pre_arm_exit share {pre_arm_share}% >= {MEDIUM_SHARE_THRESHOLD}% and exit-armed share is only {armed_share}%',
            'evidence_line': (
                f"pre_arm_exit={evidence.get('pre_arm_exit_count', 0)}/{sample_count} ({pre_arm_share}%), "
                f"exit_armed_share={armed_share}%, profit_threshold median={_format_optional_number(threshold_anchor, 4)}"
            ),
            'suggestion': (
                'Loosen exit_arm_profit_threshold (lower the required profit to arm). '
                f'Pre-arm exits are common and only {armed_share}% of guarded trades were armed; '
                f'current threshold anchor is {_format_optional_number(threshold_anchor, 4)}.'
            ),
            'rule': _rule_context(evidence, sample_count),
        }
    if pre_arm_share <= LOW_SHARE_THRESHOLD and armed_share >= 80.0:
        return {
            'parameter': 'exit_arm_profit_threshold',
            'action': 'hold',
            'priority': 'low',
            'confidence': 'low',
            'reason': 'arming already happens on most guarded trades',
            'evidence_line': f"pre_arm_exit={evidence.get('pre_arm_exit_count', 0)}/{sample_count} ({pre_arm_share}%), exit_armed_share={armed_share}%",
            'suggestion': 'Hold exit_arm_profit_threshold; arming speed does not look like the bottleneck.',
            'rule': _rule_context(evidence, sample_count),
        }
    return {
        'parameter': 'exit_arm_profit_threshold',
        'action': 'hold',
        'priority': priority,
        'confidence': 'low',
        'reason': 'arm-profit evidence is mixed',
        'evidence_line': f"pre_arm_exit={evidence.get('pre_arm_exit_count', 0)}/{sample_count} ({pre_arm_share}%), exit_armed_share={armed_share}%",
        'suggestion': 'Keep exit_arm_profit_threshold unchanged for now.',
        'rule': _rule_context(evidence, sample_count),
    }


def build_symbol_parameter_advice(report: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    evidence = _build_symbol_evidence(report, symbol)
    recommendations = [
        _recommend_stale_signal_ttl(evidence),
        _recommend_entry_drift_tolerance(evidence),
        _recommend_exit_min_hold(evidence),
        _recommend_exit_arm_profit_threshold(evidence),
    ]
    counts = {
        'tighten': sum(1 for item in recommendations if item.get('action') == 'tighten'),
        'loosen': sum(1 for item in recommendations if item.get('action') == 'loosen'),
        'hold': sum(1 for item in recommendations if item.get('action') == 'hold'),
    }
    return {
        'symbol': symbol,
        'evidence': evidence,
        'recommendations': recommendations,
        'summary': {
            'sample_count': evidence.get('sample_count', 0),
            'action_counts': counts,
            'top_close_reason_code': evidence.get('dominant_close_reason_code'),
        },
    }


def build_parameter_tuning_advice_payload(
    db_path: str,
    *,
    view: str = 'both',
    hours: float = DEFAULT_HOURS,
    limit: int = DEFAULT_LIMIT,
    symbols: Optional[Sequence[str]] = None,
    fetch_limit: Optional[int] = None,
) -> Dict[str, Any]:
    resolved_symbols = list(symbols or DEFAULT_SYMBOLS)
    summary_payload = build_outcome_issue_summary_payload(
        db_path,
        view=view,
        hours=hours,
        limit=limit,
        symbols=resolved_symbols,
        fetch_limit=fetch_limit,
    )
    views = {}
    for scope_name, report in (summary_payload.get('reports') or {}).items():
        views[scope_name] = {
            'scope_name': scope_name,
            'scope_text': (
                f'recent {hours:g}h' if scope_name == 'hours' else f'latest {limit} trades'
            ),
            'issue_summary_text': format_issue_summary_block(scope_name, summary_payload),
            'symbols': [build_symbol_parameter_advice(report, symbol) for symbol in resolved_symbols],
        }
    return {
        'schema_version': 'parameter_tuning_advice_v1',
        'mode': 'advice_only',
        'db_path': db_path,
        'view': view,
        'hours': hours,
        'limit': limit,
        'symbols': resolved_symbols,
        'fetch_limit': fetch_limit,
        'rule_thresholds': {
            'min_sample_size': MIN_SAMPLE_SIZE,
            'medium_share_threshold_pct': MEDIUM_SHARE_THRESHOLD,
            'high_share_threshold_pct': HIGH_SHARE_THRESHOLD,
            'low_share_threshold_pct': LOW_SHARE_THRESHOLD,
        },
        'issue_summary': summary_payload,
        'views': views,
    }


def format_issue_summary_block(scope_name: str, payload: Dict[str, Any]) -> str:
    reports = payload.get('reports') or {}
    report = reports.get(scope_name) or {}
    title = f"Outcome issue summary — {'recent ' + str(payload.get('hours')) + 'h' if scope_name == 'hours' else 'latest ' + str(payload.get('limit')) + ' trades'}"
    return report and payload.get('text', '') and next((block for block in str(payload.get('text') or '').split('\n\n') if block.startswith(title)), '') or ''


def format_parameter_tuning_advice(payload: Dict[str, Any]) -> str:
    lines = [
        'Automatic parameter tuning suggestions (advice only)',
        f"Mode: {payload.get('mode')}",
        (
            'Rules: '
            f"min_sample={payload.get('rule_thresholds', {}).get('min_sample_size')} / "
            f"medium_share>={payload.get('rule_thresholds', {}).get('medium_share_threshold_pct')}% / "
            f"high_share>={payload.get('rule_thresholds', {}).get('high_share_threshold_pct')}%"
        ),
        'Note: this command never edits config; it only prints suggestions based on structured outcome stats.',
    ]

    for scope_name, scope_payload in (payload.get('views') or {}).items():
        lines.append('')
        lines.append(f"=== Scope: {scope_payload.get('scope_text')} ===")
        issue_summary_text = scope_payload.get('issue_summary_text')
        if issue_summary_text:
            lines.append(issue_summary_text)
        for symbol_payload in scope_payload.get('symbols') or []:
            evidence = symbol_payload.get('evidence') or {}
            guard = evidence.get('exit_guard') or {}
            lines.append('')
            lines.append(
                f"{symbol_payload.get('symbol')} | samples={evidence.get('sample_count', 0)} | "
                f"instant_stopout={evidence.get('instant_stopout_count', 0)} ({evidence.get('instant_stopout_share', 0.0)}%) | "
                f"pre_arm_exit={evidence.get('pre_arm_exit_count', 0)} ({evidence.get('pre_arm_exit_share', 0.0)}%) | "
                f"stale_breach={evidence.get('stale_signal_breach_count', 0)} ({evidence.get('stale_signal_breach_share', 0.0)}%) | "
                f"drift_breach={evidence.get('drift_breach_count', 0)} ({evidence.get('drift_breach_share', 0.0)}%)"
            )
            lines.append('  ' + _format_distribution('signal_age', evidence.get('signal_age_seconds_at_entry') or {}, 's'))
            lines.append('  ' + _format_distribution('entry_drift', evidence.get('entry_drift_pct_from_signal') or {}, '%'))
            lines.append('  ' + _format_distribution('ttl', evidence.get('stale_signal_ttl_seconds') or {}, 's'))
            lines.append('  ' + _format_distribution('drift_tolerance', evidence.get('entry_drift_tolerance_bps') or {}, 'bps'))
            if guard.get('guard_row_count'):
                lines.append('  ' + _format_distribution('exit_hold', guard.get('hold_seconds') or {}, 's'))
                lines.append('  ' + _format_distribution('exit_min_hold', guard.get('min_hold_seconds') or {}, 's'))
                lines.append('  ' + _format_distribution('exit_profit_threshold', guard.get('profit_threshold') or {}, ''))
                lines.append(
                    f"  exit_guard: coverage={guard.get('guard_row_count', 0)}/{evidence.get('sample_count', 0)} "
                    f"armed={guard.get('exit_armed_count', 0)} ({guard.get('exit_armed_share', 0.0)}%)"
                )
            for item in symbol_payload.get('recommendations') or []:
                lines.append(
                    f"  - [{item.get('priority')}/{item.get('confidence')}] {item.get('parameter')}: {item.get('action')}"
                )
                lines.append(f"    reason: {item.get('reason')}")
                lines.append(f"    evidence: {item.get('evidence_line')}")
                lines.append(f"    advice: {item.get('suggestion')}")
    return '\n'.join(lines)
