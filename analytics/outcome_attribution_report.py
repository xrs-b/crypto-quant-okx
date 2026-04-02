from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from analytics.helper import build_close_outcome_digest
from core.database import Database


DEFAULT_FETCH_LIMIT_FOR_HOURS = 5000


def _parse_time(value: Any) -> Optional[datetime]:
    if value in (None, ''):
        return None
    text = str(value).strip()
    if not text:
        return None
    for candidate in (text, text.replace('Z', '+00:00')):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ''):
            return None
        result = float(value)
        if math.isnan(result):
            return None
        return result
    except Exception:
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ''):
            return default
        return int(value)
    except Exception:
        return default


def _share(part: int, total: int) -> float:
    return round((part / total) * 100, 2) if total > 0 else 0.0


def _percentile(sorted_values: Sequence[float], percentile: float) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return round(sorted_values[0], 6)
    position = (len(sorted_values) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(sorted_values[int(position)], 6)
    lower_value = sorted_values[lower]
    upper_value = sorted_values[upper]
    interpolated = lower_value + (upper_value - lower_value) * (position - lower)
    return round(interpolated, 6)


def _describe_numeric(values: Iterable[Any]) -> Dict[str, Any]:
    normalized = sorted(v for v in (_safe_float(item) for item in values) if v is not None)
    if not normalized:
        return {
            'count': 0,
            'min': None,
            'max': None,
            'mean': None,
            'median': None,
            'p90': None,
        }
    count = len(normalized)
    return {
        'count': count,
        'min': round(normalized[0], 6),
        'max': round(normalized[-1], 6),
        'mean': round(sum(normalized) / count, 6),
        'median': _percentile(normalized, 0.5),
        'p90': _percentile(normalized, 0.9),
    }


def _format_distribution(label: str, stats: Dict[str, Any], unit: str = '') -> str:
    if not stats.get('count'):
        return f"{label}: n=0"
    suffix = unit or ''
    return (
        f"{label}: n={stats['count']} mean={stats['mean']}{suffix} "
        f"median={stats['median']}{suffix} p90={stats['p90']}{suffix} "
        f"min={stats['min']}{suffix} max={stats['max']}{suffix}"
    )


def _normalize_trade(row: Dict[str, Any]) -> Dict[str, Any]:
    trade = dict(row or {})
    outcome = trade.get('outcome_attribution') or {}
    if not isinstance(outcome, dict):
        outcome = {}
    strategy_tags = trade.get('strategy_tags') or outcome.get('strategy_tags') or []
    if isinstance(strategy_tags, str):
        strategy_tags = [strategy_tags]
    strategy_tags = [str(tag).strip() for tag in strategy_tags if str(tag).strip()]
    close_reason_code = str(trade.get('close_reason_code') or outcome.get('close_reason_code') or 'unknown').strip() or 'unknown'
    close_decision = str(trade.get('close_decision') or outcome.get('close_decision') or 'unknown').strip() or 'unknown'
    close_reason_category = str(trade.get('close_reason_category') or outcome.get('close_reason_category') or 'unknown').strip() or 'unknown'
    signal_age = _safe_float(trade.get('signal_age_seconds_at_entry'))
    if signal_age is None:
        signal_age = _safe_float(outcome.get('signal_age_seconds_at_entry'))
    entry_drift = _safe_float(trade.get('entry_drift_pct_from_signal'))
    if entry_drift is None:
        entry_drift = _safe_float(outcome.get('entry_drift_pct_from_signal'))
    stale_ttl = _safe_float(outcome.get('stale_signal_ttl_seconds'))
    tolerance_bps = _safe_float(outcome.get('entry_drift_tolerance_bps'))
    drift_tolerance_pct = round(tolerance_bps / 100.0, 6) if tolerance_bps is not None else None
    close_time = trade.get('close_time') or outcome.get('close_time')
    return {
        'trade_id': trade.get('id') or trade.get('trade_id'),
        'symbol': str(trade.get('symbol') or '--').strip() or '--',
        'status': str(trade.get('status') or 'closed').strip().lower() or 'closed',
        'regime_tag': str(trade.get('regime_tag') or outcome.get('regime_tag') or 'unknown').strip() or 'unknown',
        'policy_tag': str(trade.get('policy_tag') or outcome.get('policy_tag') or 'unknown').strip() or 'unknown',
        'dominant_strategy': str(trade.get('dominant_strategy') or outcome.get('dominant_strategy') or (strategy_tags[0] if strategy_tags else 'unknown')).strip() or 'unknown',
        'strategy_tags': strategy_tags,
        'close_reason_code': close_reason_code,
        'close_reason_category': close_reason_category,
        'close_decision': close_decision,
        'pnl': _safe_float(trade.get('pnl')),
        'return_pct': _safe_float(trade.get('return_pct') if trade.get('return_pct') is not None else trade.get('pnl_percent')),
        'instant_stopout': bool(trade.get('instant_stopout')) if trade.get('instant_stopout') is not None else bool(outcome.get('instant_stopout')),
        'pre_arm_exit': bool(trade.get('pre_arm_exit')) if trade.get('pre_arm_exit') is not None else bool(outcome.get('pre_arm_exit')),
        'instant_exit': bool(trade.get('instant_exit')) if trade.get('instant_exit') is not None else bool(outcome.get('instant_exit')),
        'signal_age_seconds_at_entry': signal_age,
        'entry_drift_pct_from_signal': entry_drift,
        'stale_signal_ttl_seconds': stale_ttl,
        'entry_drift_tolerance_pct': drift_tolerance_pct,
        'drift_tolerance_bps': tolerance_bps,
        'stale_signal_breach': bool(signal_age is not None and stale_ttl is not None and signal_age > stale_ttl),
        'drift_breach': bool(entry_drift is not None and drift_tolerance_pct is not None and abs(entry_drift) > drift_tolerance_pct),
        'outcome_schema_version': outcome.get('schema_version'),
        'close_time': close_time,
        'close_time_dt': _parse_time(close_time),
    }


def load_outcome_attribution_rows(
    db_path: str,
    *,
    limit: int = 200,
    hours: Optional[float] = None,
    symbols: Optional[Sequence[str]] = None,
    fetch_limit: Optional[int] = None,
) -> Dict[str, Any]:
    db = Database(db_path)
    normalized_symbols = {str(item).strip() for item in (symbols or []) if str(item).strip()}
    effective_fetch_limit = max(int(fetch_limit or 0), int(limit or 0), 1)
    if hours is not None:
        effective_fetch_limit = max(effective_fetch_limit, DEFAULT_FETCH_LIMIT_FOR_HOURS)
    raw_rows = db.get_recent_close_outcome_trades(limit=effective_fetch_limit)
    cutoff = None
    if hours is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=float(hours))
    filtered: List[Dict[str, Any]] = []
    for row in raw_rows:
        if normalized_symbols and str(row.get('symbol') or '').strip() not in normalized_symbols:
            continue
        normalized = _normalize_trade(row)
        close_dt = normalized.get('close_time_dt')
        if cutoff is not None and (close_dt is None or close_dt < cutoff):
            continue
        filtered.append(normalized)
        if hours is None and limit and len(filtered) >= int(limit):
            break
    structured = [row for row in filtered if row.get('outcome_schema_version')]
    return {
        'rows': filtered,
        'structured_rows': structured,
        'cutoff': cutoff.isoformat() if cutoff else None,
        'requested_limit': int(limit or 0),
        'fetch_limit': effective_fetch_limit,
        'requested_hours': float(hours) if hours is not None else None,
        'symbols': sorted(normalized_symbols),
    }


def _build_group_summary(rows: Sequence[Dict[str, Any]], group_key: str) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        key = str(row.get(group_key) or 'unknown').strip() or 'unknown'
        buckets.setdefault(key, []).append(row)
    items: List[Dict[str, Any]] = []
    for key, bucket in buckets.items():
        total = len(bucket)
        instant_stopout_count = sum(1 for row in bucket if row.get('instant_stopout'))
        pre_arm_exit_count = sum(1 for row in bucket if row.get('pre_arm_exit'))
        stale_signal_breach_count = sum(1 for row in bucket if row.get('stale_signal_breach'))
        drift_breach_count = sum(1 for row in bucket if row.get('drift_breach'))
        reason_counts: Dict[str, int] = {}
        for row in bucket:
            reason = str(row.get('close_reason_code') or 'unknown').strip() or 'unknown'
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        items.append({
            'group': key,
            'trade_count': total,
            'instant_stopout_count': instant_stopout_count,
            'instant_stopout_share': _share(instant_stopout_count, total),
            'pre_arm_exit_count': pre_arm_exit_count,
            'pre_arm_exit_share': _share(pre_arm_exit_count, total),
            'stale_signal_breach_count': stale_signal_breach_count,
            'stale_signal_breach_share': _share(stale_signal_breach_count, total),
            'drift_breach_count': drift_breach_count,
            'drift_breach_share': _share(drift_breach_count, total),
            'signal_age_seconds_at_entry': _describe_numeric(row.get('signal_age_seconds_at_entry') for row in bucket),
            'entry_drift_pct_from_signal': _describe_numeric(row.get('entry_drift_pct_from_signal') for row in bucket),
            'close_reason_code_counts': dict(sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))),
        })
    items.sort(key=lambda item: (-int(item['trade_count']), str(item['group'])))
    return items


def _build_focus_insights(rows: Sequence[Dict[str, Any]], focus_symbols: Sequence[str]) -> List[Dict[str, Any]]:
    insights: List[Dict[str, Any]] = []
    for symbol in focus_symbols:
        bucket = [row for row in rows if row.get('symbol') == symbol]
        if not bucket:
            insights.append({
                'symbol': symbol,
                'trade_count': 0,
                'headline': 'no structured outcome sample in current scope',
                'recent_flagged_trades': [],
            })
            continue
        instant_stopout_count = sum(1 for row in bucket if row.get('instant_stopout'))
        pre_arm_exit_count = sum(1 for row in bucket if row.get('pre_arm_exit'))
        stale_signal_breach_count = sum(1 for row in bucket if row.get('stale_signal_breach'))
        drift_breach_count = sum(1 for row in bucket if row.get('drift_breach'))
        reason_counts: Dict[str, int] = {}
        for row in bucket:
            reason = str(row.get('close_reason_code') or 'unknown').strip() or 'unknown'
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        dominant_reason = sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[0][0] if reason_counts else 'unknown'
        headline_bits = [
            f"instant_stopout={instant_stopout_count}/{len(bucket)} ({_share(instant_stopout_count, len(bucket))}%)",
            f"pre_arm_exit={pre_arm_exit_count}/{len(bucket)} ({_share(pre_arm_exit_count, len(bucket))}%)",
            f"stale_signal_breach={stale_signal_breach_count}/{len(bucket)} ({_share(stale_signal_breach_count, len(bucket))}%)",
            f"drift_breach={drift_breach_count}/{len(bucket)} ({_share(drift_breach_count, len(bucket))}%)",
            f"top_close_reason={dominant_reason}",
        ]
        flagged = [
            row for row in sorted(bucket, key=lambda item: (item.get('close_time') or '', str(item.get('trade_id') or '')), reverse=True)
            if row.get('instant_stopout') or row.get('pre_arm_exit') or row.get('stale_signal_breach') or row.get('drift_breach')
        ][:5]
        insights.append({
            'symbol': symbol,
            'trade_count': len(bucket),
            'headline': ' / '.join(headline_bits),
            'signal_age_seconds_at_entry': _describe_numeric(row.get('signal_age_seconds_at_entry') for row in bucket),
            'entry_drift_pct_from_signal': _describe_numeric(row.get('entry_drift_pct_from_signal') for row in bucket),
            'close_reason_code_counts': dict(sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))),
            'recent_flagged_trades': flagged,
        })
    return insights


def analyze_outcome_attribution(
    db_path: str,
    *,
    limit: int = 200,
    hours: Optional[float] = None,
    symbols: Optional[Sequence[str]] = None,
    focus_symbols: Optional[Sequence[str]] = None,
    fetch_limit: Optional[int] = None,
) -> Dict[str, Any]:
    loaded = load_outcome_attribution_rows(
        db_path,
        limit=limit,
        hours=hours,
        symbols=symbols,
        fetch_limit=fetch_limit,
    )
    structured_rows = loaded['structured_rows']
    total_structured = len(structured_rows)
    digest = build_close_outcome_digest(structured_rows, label='outcome_attribution_analysis')
    global_reason_counts: Dict[str, int] = {}
    for row in structured_rows:
        reason = str(row.get('close_reason_code') or 'unknown').strip() or 'unknown'
        global_reason_counts[reason] = global_reason_counts.get(reason, 0) + 1
    summary = {
        'requested_limit': loaded['requested_limit'],
        'requested_hours': loaded['requested_hours'],
        'cutoff': loaded['cutoff'],
        'symbol_filter': loaded['symbols'],
        'closed_rows_in_scope': len(loaded['rows']),
        'structured_rows_in_scope': total_structured,
        'missing_structured_rows': max(len(loaded['rows']) - total_structured, 0),
        'instant_stopout_count': sum(1 for row in structured_rows if row.get('instant_stopout')),
        'pre_arm_exit_count': sum(1 for row in structured_rows if row.get('pre_arm_exit')),
        'stale_signal_breach_count': sum(1 for row in structured_rows if row.get('stale_signal_breach')),
        'drift_breach_count': sum(1 for row in structured_rows if row.get('drift_breach')),
        'signal_age_seconds_at_entry': _describe_numeric(row.get('signal_age_seconds_at_entry') for row in structured_rows),
        'entry_drift_pct_from_signal': _describe_numeric(row.get('entry_drift_pct_from_signal') for row in structured_rows),
        'close_reason_code_counts': dict(sorted(global_reason_counts.items(), key=lambda item: (-item[1], item[0]))),
    }
    summary['instant_stopout_share'] = _share(summary['instant_stopout_count'], total_structured)
    summary['pre_arm_exit_share'] = _share(summary['pre_arm_exit_count'], total_structured)
    summary['stale_signal_breach_share'] = _share(summary['stale_signal_breach_count'], total_structured)
    summary['drift_breach_share'] = _share(summary['drift_breach_count'], total_structured)
    focus = list(focus_symbols or [])
    return {
        'schema_version': 'outcome_attribution_analysis_report_v1',
        'summary': summary,
        'digest': digest,
        'by_symbol': _build_group_summary(structured_rows, 'symbol'),
        'by_strategy': _build_group_summary(structured_rows, 'dominant_strategy'),
        'by_regime': _build_group_summary(structured_rows, 'regime_tag'),
        'focus_symbols': _build_focus_insights(structured_rows, focus),
        'structured_rows': structured_rows,
    }


def _format_reason_counts(counts: Dict[str, int], *, limit: int = 4) -> str:
    items = list((counts or {}).items())[:limit]
    if not items:
        return 'none'
    return ', '.join(f'{key}:{value}' for key, value in items)


def _format_group_lines(title: str, groups: Sequence[Dict[str, Any]], *, limit: int = 10) -> List[str]:
    lines = [title]
    if not groups:
        lines.append('  - no data')
        return lines
    for item in groups[:limit]:
        lines.append(
            '  - '
            f"{item['group']}: trades={item['trade_count']} "
            f"instant_stopout={item['instant_stopout_count']} ({item['instant_stopout_share']}%) "
            f"pre_arm_exit={item['pre_arm_exit_count']} ({item['pre_arm_exit_share']}%) "
            f"stale_breach={item['stale_signal_breach_count']} ({item['stale_signal_breach_share']}%) "
            f"drift_breach={item['drift_breach_count']} ({item['drift_breach_share']}%) "
            f"close_reason_code=[{_format_reason_counts(item.get('close_reason_code_counts') or {})}]"
        )
        lines.append(f"      {_format_distribution('signal_age_seconds_at_entry', item['signal_age_seconds_at_entry'], 's')}")
        lines.append(f"      {_format_distribution('entry_drift_pct_from_signal', item['entry_drift_pct_from_signal'], '%')}")
    return lines


def format_outcome_attribution_report(report: Dict[str, Any]) -> str:
    summary = report.get('summary') or {}
    digest = report.get('digest') or {}
    scope_bits = []
    if summary.get('requested_hours') is not None:
        scope_bits.append(f"recent {summary['requested_hours']}h")
    if summary.get('requested_limit'):
        scope_bits.append(f"limit {summary['requested_limit']} trades")
    if summary.get('symbol_filter'):
        scope_bits.append('symbols=' + ','.join(summary['symbol_filter']))
    scope_text = ' / '.join(scope_bits) if scope_bits else 'latest closed trades'
    lines = [
        'Outcome attribution analysis report',
        f'Scope: {scope_text}',
        (
            f"Closed rows={summary.get('closed_rows_in_scope', 0)} / structured={summary.get('structured_rows_in_scope', 0)} "
            f"/ missing_structured={summary.get('missing_structured_rows', 0)}"
        ),
        (
            f"Win/Loss/Flat={digest.get('win_count', 0)}/{digest.get('loss_count', 0)}/{digest.get('flat_count', 0)} "
            f"win_rate={digest.get('win_rate', 0.0)}% net_pnl={digest.get('net_pnl', 0.0)}"
        ),
        (
            f"instant_stopout={summary.get('instant_stopout_count', 0)} ({summary.get('instant_stopout_share', 0.0)}%) / "
            f"pre_arm_exit={summary.get('pre_arm_exit_count', 0)} ({summary.get('pre_arm_exit_share', 0.0)}%) / "
            f"stale_signal_breach={summary.get('stale_signal_breach_count', 0)} ({summary.get('stale_signal_breach_share', 0.0)}%) / "
            f"drift_breach={summary.get('drift_breach_count', 0)} ({summary.get('drift_breach_share', 0.0)}%)"
        ),
        _format_distribution('signal_age_seconds_at_entry', summary.get('signal_age_seconds_at_entry') or {}, 's'),
        _format_distribution('entry_drift_pct_from_signal', summary.get('entry_drift_pct_from_signal') or {}, '%'),
        f"close_reason_code top={_format_reason_counts(summary.get('close_reason_code_counts') or {}, limit=6)}",
    ]
    focus_rows = report.get('focus_symbols') or []
    if focus_rows:
        lines.append('')
        lines.append('Focus symbols')
        for item in focus_rows:
            lines.append(f"  - {item['symbol']}: trades={item['trade_count']} / {item['headline']}")
            if item.get('trade_count'):
                lines.append(f"      {_format_distribution('signal_age_seconds_at_entry', item.get('signal_age_seconds_at_entry') or {}, 's')}")
                lines.append(f"      {_format_distribution('entry_drift_pct_from_signal', item.get('entry_drift_pct_from_signal') or {}, '%')}")
                if item.get('recent_flagged_trades'):
                    rendered = []
                    for row in item['recent_flagged_trades']:
                        flags = []
                        if row.get('instant_stopout'):
                            flags.append('instant_stopout')
                        if row.get('pre_arm_exit'):
                            flags.append('pre_arm_exit')
                        if row.get('stale_signal_breach'):
                            flags.append('stale_signal_breach')
                        if row.get('drift_breach'):
                            flags.append('drift_breach')
                        rendered.append(
                            f"#{row.get('trade_id')}[{','.join(flags)}] reason={row.get('close_reason_code')} age={row.get('signal_age_seconds_at_entry')} drift={row.get('entry_drift_pct_from_signal')}"
                        )
                    lines.append('      recent_flagged=' + ' ; '.join(rendered))
    lines.append('')
    lines.extend(_format_group_lines('By symbol', report.get('by_symbol') or []))
    lines.append('')
    lines.extend(_format_group_lines('By dominant strategy', report.get('by_strategy') or []))
    lines.append('')
    lines.extend(_format_group_lines('By regime', report.get('by_regime') or []))
    return '\n'.join(lines)
