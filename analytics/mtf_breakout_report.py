from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from analytics.outcome_attribution_report import _describe_numeric, _format_distribution, _parse_time, _safe_float, _share


DEFAULT_FETCH_LIMIT_FOR_HOURS = 5000
DEFAULT_OUTCOME_SAMPLE_MIN = 3


def _safe_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ''):
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _normalize_decision(row: Dict[str, Any], entry_decision: Dict[str, Any]) -> str:
    decision = str(entry_decision.get('decision') or '').strip().lower()
    if decision in {'allow', 'watch', 'block'}:
        return decision
    if row.get('executed'):
        return 'allow'
    if row.get('filtered'):
        return 'block'
    return 'watch'


def _score_bucket(score: int) -> str:
    if score >= 80:
        return '80-100'
    if score >= 60:
        return '60-79'
    if score >= 40:
        return '40-59'
    if score > 0:
        return '1-39'
    return '0'


def _normalize_mtf_breakout_snapshot(signal_row: Dict[str, Any]) -> Dict[str, Any]:
    filter_details = _safe_json(signal_row.get('filter_details'))
    observability = _safe_json(filter_details.get('observability'))
    entry_decision = _safe_json(filter_details.get('entry_decision'))
    breakdown = _safe_json(entry_decision.get('breakdown'))
    payload = _safe_json(observability.get('mtf_breakout'))
    market_context_payload = _safe_json(signal_row.get('market_context_mtf_breakout'))
    if not payload:
        payload = market_context_payload

    raw_present = bool(payload or market_context_payload or breakdown.get('mtf_breakout_score') or breakdown.get('mtf_breakout_reason'))
    direction = str(payload.get('direction') or 'hold').strip() or 'hold'
    score = int(payload.get('score', breakdown.get('mtf_breakout_score', 0)) or 0)
    anchor = _safe_json(payload.get('anchor'))
    trigger = _safe_json(payload.get('trigger'))
    confirm = _safe_json(payload.get('confirm'))
    anchor_trend = str(payload.get('anchor_trend') or anchor.get('trend') or 'unknown').strip() or 'unknown'
    anchor_available = bool(payload.get('anchor_available', anchor.get('available')))
    has_breakout = bool(payload.get('has_breakout', direction in {'buy', 'sell'}))
    anchor_aligned = bool(
        payload.get('anchor_aligned', False)
        or (has_breakout and anchor_available and ((direction == 'buy' and anchor_trend == 'bullish') or (direction == 'sell' and anchor_trend == 'bearish')))
    )
    enabled_value = payload.get('enabled')
    enabled = bool(enabled_value) if enabled_value is not None else raw_present
    eligible = bool(payload.get('eligible', False))
    if not raw_present:
        state = 'not_recorded'
    elif 'state' in payload:
        state = str(payload.get('state') or 'no_breakout').strip() or 'no_breakout'
    elif not enabled:
        state = 'disabled'
    elif not trigger.get('available', bool(payload) or bool(market_context_payload)):
        state = 'data_insufficient'
    elif not has_breakout:
        state = 'no_breakout'
    elif eligible:
        state = 'eligible_breakout'
    elif anchor_available and not anchor_aligned:
        state = 'breakout_counter_anchor'
    else:
        state = 'breakout_watch'
    has_evidence = bool(payload.get('has_evidence', score > 0 or has_breakout or anchor_available or market_context_payload or payload))
    return {
        'schema_version': payload.get('schema_version') or 'mtf_breakout_observability_v1_derived',
        'enabled': enabled,
        'observe_only': bool(payload.get('observe_only', breakdown.get('mtf_breakout_observe_only', True))),
        'decision': _normalize_decision(signal_row, entry_decision),
        'score': score,
        'score_bucket': str(payload.get('score_bucket') or _score_bucket(score)),
        'direction': direction,
        'state': state,
        'has_evidence': has_evidence,
        'has_breakout': has_breakout,
        'eligible': eligible,
        'reason': str(payload.get('reason') or breakdown.get('mtf_breakout_reason') or '--'),
        'anchor_timeframe': str(payload.get('anchor_timeframe') or anchor.get('timeframe') or '4h'),
        'anchor_available': anchor_available,
        'anchor_trend': anchor_trend,
        'anchor_aligned': anchor_aligned,
        'confirm_momentum': str(payload.get('confirm_momentum') or confirm.get('momentum') or 'neutral'),
        'trigger': trigger,
        'anchor': anchor,
        'confirm': confirm,
    }


def _normalize_trade_row(row: Dict[str, Any]) -> Dict[str, Any]:
    plan_context = _safe_json(row.get('plan_context'))
    outcome = _safe_json(row.get('outcome_attribution'))
    observability = _safe_json(plan_context.get('observability'))
    mtf_breakout = _safe_json(outcome.get('mtf_breakout') or observability.get('mtf_breakout'))
    pnl = _safe_float(row.get('pnl'))
    return_pct = _safe_float(row.get('pnl_percent'))
    if return_pct is None:
        return_pct = _safe_float(outcome.get('return_pct'))
    close_decision = str(row.get('close_decision') or outcome.get('close_decision') or ('win' if (pnl or 0) > 0 else 'loss' if (pnl or 0) < 0 else 'flat')).strip() or 'unknown'
    return {
        'id': row.get('id'),
        'signal_id': row.get('signal_id'),
        'symbol': row.get('symbol'),
        'status': row.get('status'),
        'close_time': row.get('close_time'),
        'close_time_dt': _parse_time(row.get('close_time')),
        'pnl': pnl,
        'return_pct': return_pct,
        'close_decision': close_decision,
        'close_reason_code': str(row.get('close_reason_code') or outcome.get('close_reason_code') or 'unknown').strip() or 'unknown',
        'mtf_breakout': mtf_breakout,
    }


def load_mtf_breakout_signal_rows(
    db_path: str,
    *,
    limit: int = 500,
    hours: Optional[float] = None,
    symbols: Optional[Sequence[str]] = None,
    fetch_limit: Optional[int] = None,
) -> Dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    effective_fetch_limit = max(int(fetch_limit or 0), int(limit or 0), 1)
    if hours is not None:
        effective_fetch_limit = max(effective_fetch_limit, DEFAULT_FETCH_LIMIT_FOR_HOURS)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, symbol, signal_type, price, strength, filtered, filter_reason, filter_details, executed, trade_id, created_at
        FROM signals
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (effective_fetch_limit,),
    )
    raw_rows = [dict(row) for row in cursor.fetchall()]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(hours)) if hours is not None else None
    normalized_symbols = {str(item).strip() for item in (symbols or []) if str(item).strip()}

    signal_rows: List[Dict[str, Any]] = []
    signal_ids: List[int] = []
    for row in raw_rows:
        if normalized_symbols and str(row.get('symbol') or '').strip() not in normalized_symbols:
            continue
        created_at_dt = _parse_time(row.get('created_at'))
        if cutoff is not None and (created_at_dt is None or created_at_dt < cutoff):
            continue
        row['created_at_dt'] = created_at_dt
        row['filter_details'] = _safe_json(row.get('filter_details'))
        row['mtf_breakout'] = _normalize_mtf_breakout_snapshot(row)
        row['decision'] = row['mtf_breakout']['decision']
        row['closed_trades'] = []
        signal_rows.append(row)
        signal_ids.append(int(row['id']))
        if limit and len(signal_rows) >= int(limit):
            break

    trade_rows_by_signal: Dict[int, List[Dict[str, Any]]] = {}
    if signal_ids:
        placeholders = ','.join('?' for _ in signal_ids)
        cursor.execute(
            f"SELECT * FROM trades WHERE status = 'closed' AND signal_id IN ({placeholders}) ORDER BY close_time DESC, id DESC",
            tuple(signal_ids),
        )
        for trade in cursor.fetchall():
            normalized = _normalize_trade_row(dict(trade))
            trade_rows_by_signal.setdefault(int(normalized['signal_id']), []).append(normalized)
    conn.close()

    for row in signal_rows:
        row['closed_trades'] = trade_rows_by_signal.get(int(row['id']), [])

    return {
        'rows': signal_rows,
        'cutoff': cutoff.isoformat() if cutoff else None,
        'requested_limit': int(limit or 0),
        'fetch_limit': effective_fetch_limit,
        'requested_hours': float(hours) if hours is not None else None,
        'symbols': sorted(normalized_symbols),
    }


def _count_by(values: Iterable[str], allowed: Optional[Sequence[str]] = None) -> Dict[str, int]:
    counts: Dict[str, int] = {key: 0 for key in (allowed or [])}
    for value in values:
        key = str(value or 'unknown').strip() or 'unknown'
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (item[0] not in {'allow', 'watch', 'block', 'win', 'loss', 'flat'}, item[0])))


def _bucket_summary(label: str, rows: Sequence[Dict[str, Any]], *, min_outcome_samples: int = DEFAULT_OUTCOME_SAMPLE_MIN) -> Dict[str, Any]:
    decision_counts = _count_by((row.get('decision') for row in rows), allowed=['allow', 'watch', 'block'])
    trades = [trade for row in rows for trade in (row.get('closed_trades') or [])]
    close_decision_counts = _count_by((trade.get('close_decision') for trade in trades), allowed=['win', 'loss', 'flat'])
    return_stats = _describe_numeric(trade.get('return_pct') for trade in trades)
    pnl_stats = _describe_numeric(trade.get('pnl') for trade in trades)
    reason_counts = _count_by((trade.get('close_reason_code') for trade in trades))
    signal_count = len(rows)
    closed_trade_count = len(trades)
    return {
        'bucket': label,
        'signal_count': signal_count,
        'decision_counts': decision_counts,
        'decision_shares': {key: _share(value, signal_count) for key, value in decision_counts.items()},
        'closed_trade_count': closed_trade_count,
        'close_decision_counts': close_decision_counts,
        'close_decision_shares': {key: _share(value, closed_trade_count) for key, value in close_decision_counts.items()},
        'return_pct': return_stats,
        'pnl': pnl_stats,
        'close_reason_code_counts': dict(sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))),
        'has_outcome_distribution': closed_trade_count >= min_outcome_samples,
    }


def _build_grouped_summary(rows: Sequence[Dict[str, Any]], key: str, *, min_outcome_samples: int = DEFAULT_OUTCOME_SAMPLE_MIN) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        value = row.get('mtf_breakout', {}).get(key)
        if isinstance(value, bool):
            bucket_key = 'yes' if value else 'no'
        else:
            bucket_key = str(value if value not in (None, '') else 'unknown').strip() or 'unknown'
        buckets.setdefault(bucket_key, []).append(row)
    items = [_bucket_summary(label, bucket_rows, min_outcome_samples=min_outcome_samples) for label, bucket_rows in buckets.items()]
    items.sort(key=lambda item: (-int(item['signal_count']), str(item['bucket'])))
    return items


def analyze_mtf_breakout_report(
    db_path: str,
    *,
    limit: int = 500,
    hours: Optional[float] = None,
    symbols: Optional[Sequence[str]] = None,
    fetch_limit: Optional[int] = None,
    min_outcome_samples: int = DEFAULT_OUTCOME_SAMPLE_MIN,
) -> Dict[str, Any]:
    loaded = load_mtf_breakout_signal_rows(
        db_path,
        limit=limit,
        hours=hours,
        symbols=symbols,
        fetch_limit=fetch_limit,
    )
    rows = loaded['rows']
    evidence_rows = [row for row in rows if row.get('mtf_breakout', {}).get('has_evidence')]
    no_evidence_rows = [row for row in rows if not row.get('mtf_breakout', {}).get('has_evidence')]
    all_trades = [trade for row in rows for trade in (row.get('closed_trades') or [])]
    summary = {
        'signal_rows_in_scope': len(rows),
        'signals_with_mtf_evidence': len(evidence_rows),
        'signals_without_mtf_evidence': len(no_evidence_rows),
        'signals_with_mtf_evidence_share': _share(len(evidence_rows), len(rows)),
        'closed_trade_rows_in_scope': len(all_trades),
        'allow_count': sum(1 for row in rows if row.get('decision') == 'allow'),
        'watch_count': sum(1 for row in rows if row.get('decision') == 'watch'),
        'block_count': sum(1 for row in rows if row.get('decision') == 'block'),
    }
    return {
        'summary': summary,
        'scope': {
            'cutoff': loaded.get('cutoff'),
            'requested_limit': loaded.get('requested_limit'),
            'fetch_limit': loaded.get('fetch_limit'),
            'requested_hours': loaded.get('requested_hours'),
            'symbols': loaded.get('symbols') or [],
            'min_outcome_samples': int(min_outcome_samples or DEFAULT_OUTCOME_SAMPLE_MIN),
        },
        'comparison': {
            'with_mtf_evidence': _bucket_summary('with_mtf_evidence', evidence_rows, min_outcome_samples=min_outcome_samples),
            'without_mtf_evidence': _bucket_summary('without_mtf_evidence', no_evidence_rows, min_outcome_samples=min_outcome_samples),
        },
        'by_score_bucket': _build_grouped_summary(rows, 'score_bucket', min_outcome_samples=min_outcome_samples),
        'by_direction': _build_grouped_summary(rows, 'direction', min_outcome_samples=min_outcome_samples),
        'by_state': _build_grouped_summary(rows, 'state', min_outcome_samples=min_outcome_samples),
        'by_anchor_aligned': _build_grouped_summary(rows, 'anchor_aligned', min_outcome_samples=min_outcome_samples),
        'rows': rows,
    }


def _format_bucket_lines(title: str, items: Sequence[Dict[str, Any]]) -> List[str]:
    lines = [title]
    if not items:
        lines.append('  (no samples)')
        return lines
    for item in items:
        decision_counts = item.get('decision_counts') or {}
        close_counts = item.get('close_decision_counts') or {}
        lines.append(
            f"- {item['bucket']}: signals={item['signal_count']} "
            f"allow/watch/block={decision_counts.get('allow', 0)}/{decision_counts.get('watch', 0)}/{decision_counts.get('block', 0)} "
            f"closed={item['closed_trade_count']} win/loss/flat={close_counts.get('win', 0)}/{close_counts.get('loss', 0)}/{close_counts.get('flat', 0)}"
        )
        if item.get('has_outcome_distribution'):
            lines.append(f"    {_format_distribution('return_pct', item.get('return_pct') or {}, unit='%')}")
        elif item.get('closed_trade_count'):
            lines.append(f"    return_pct: n={item['closed_trade_count']} (< min_outcome_samples, skip distribution)")
    return lines


def format_mtf_breakout_report(report: Dict[str, Any]) -> str:
    summary = report.get('summary') or {}
    scope = report.get('scope') or {}
    comparison = report.get('comparison') or {}
    lines = [
        'MTF breakout observe-only report',
        f"Signals in scope: {summary.get('signal_rows_in_scope', 0)} | closed trades linked: {summary.get('closed_trade_rows_in_scope', 0)}",
        f"Decision mix: allow={summary.get('allow_count', 0)} watch={summary.get('watch_count', 0)} block={summary.get('block_count', 0)}",
        f"MTF evidence coverage: {summary.get('signals_with_mtf_evidence', 0)}/{summary.get('signal_rows_in_scope', 0)} ({summary.get('signals_with_mtf_evidence_share', 0)}%)",
        f"Scope: hours={scope.get('requested_hours')} limit={scope.get('requested_limit')} symbols={','.join(scope.get('symbols') or []) or 'ALL'} min_outcome_samples={scope.get('min_outcome_samples')}",
        '',
    ]
    lines.extend(_format_bucket_lines('Evidence vs no evidence', [comparison.get('with_mtf_evidence') or {}, comparison.get('without_mtf_evidence') or {}]))
    lines.append('')
    lines.extend(_format_bucket_lines('By score bucket', report.get('by_score_bucket') or []))
    lines.append('')
    lines.extend(_format_bucket_lines('By direction', report.get('by_direction') or []))
    lines.append('')
    lines.extend(_format_bucket_lines('By state', report.get('by_state') or []))
    lines.append('')
    lines.extend(_format_bucket_lines('By 4h anchor aligned', report.get('by_anchor_aligned') or []))
    return '\n'.join(lines)
