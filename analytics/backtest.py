"""回测与信号质量分析模块"""
from __future__ import annotations

import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from core.config import Config
from core.database import Database
from signals.detector import SignalDetector
from signals.validator import SignalValidator
from core.regime_policy import normalize_observe_only_view, summarize_observe_only_collection


def _normalize_bucket_tag(value: Optional[str], fallback: str = 'unknown') -> str:
    value = str(value or '').strip()
    return value or fallback


def summarize_trade_buckets(
    trades: List[Dict],
    primary_key: str,
    secondary_key: Optional[str] = None,
) -> List[Dict]:
    buckets: Dict[tuple, List[Dict]] = defaultdict(list)
    for trade in trades:
        primary = _normalize_bucket_tag(trade.get(primary_key))
        secondary = _normalize_bucket_tag(trade.get(secondary_key)) if secondary_key else None
        buckets[(primary, secondary)].append(trade)

    rows = []
    for (primary, secondary), bucket_trades in sorted(buckets.items(), key=lambda item: (item[0][0], item[0][1] or '')):
        returns = [float(t.get('return_pct', 0) or 0) for t in bucket_trades]
        wins = sum(1 for value in returns if value > 0)
        losses = sum(1 for value in returns if value < 0)
        avg_return = sum(returns) / len(returns) if returns else 0.0
        rows.append({
            'bucket': primary,
            'secondary_bucket': secondary,
            'trade_count': len(bucket_trades),
            'wins': wins,
            'losses': losses,
            'win_rate': round((wins / len(bucket_trades) * 100), 2) if bucket_trades else 0.0,
            'total_return_pct': round(sum(returns), 4),
            'avg_return_pct': round(avg_return, 4),
            'avg_abs_return_pct': round(sum(abs(v) for v in returns) / len(returns), 4) if returns else 0.0,
        })
    rows.sort(key=lambda row: (-row['trade_count'], row['bucket'], row.get('secondary_bucket') or ''))
    return rows


def _normalize_strategy_tags(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    tags = []
    seen = set()
    for item in value or []:
        tag = _normalize_bucket_tag(item, '')
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags



def summarize_trade_list_buckets(
    trades: List[Dict],
    list_key: str,
    secondary_key: Optional[str] = None,
) -> List[Dict]:
    expanded = []
    for trade in trades:
        tags = _normalize_strategy_tags(trade.get(list_key))
        if not tags:
            tags = ['unknown']
        for tag in tags:
            expanded.append({
                **trade,
                list_key: tag,
            })
    return summarize_trade_buckets(expanded, list_key, secondary_key)



def build_strategy_fit_summary(trades: List[Dict], *, min_sample_size: int) -> Dict:
    by_strategy = summarize_trade_list_buckets(trades, 'strategy_tags')
    by_regime_strategy = summarize_trade_list_buckets(trades, 'strategy_tags', 'regime_tag')
    by_policy_strategy = summarize_trade_list_buckets(trades, 'strategy_tags', 'policy_tag')

    regime_strategy_map: Dict[str, List[Dict]] = defaultdict(list)
    strategy_policy_map: Dict[str, List[Dict]] = defaultdict(list)
    for row in by_regime_strategy:
        regime_strategy_map[_normalize_bucket_tag(row.get('secondary_bucket'))].append(row)
    for row in by_policy_strategy:
        strategy_policy_map[_normalize_bucket_tag(row.get('bucket'))].append(row)

    regime_strategy_fit = []
    for regime, rows in sorted(regime_strategy_map.items()):
        ranked = sorted(rows, key=lambda item: (-item['avg_return_pct'], -item['win_rate'], -item['trade_count'], item['bucket']))
        qualified = [row for row in ranked if row['trade_count'] >= min_sample_size]
        if not ranked:
            continue
        best = qualified[0] if qualified else ranked[0]
        worst = min(ranked, key=lambda item: (item['avg_return_pct'], item['win_rate'], -item['trade_count'], item['bucket']))
        regime_strategy_fit.append({
            'regime': regime,
            'best_strategy': best['bucket'],
            'best_trade_count': best['trade_count'],
            'best_avg_return_pct': best['avg_return_pct'],
            'best_win_rate': best['win_rate'],
            'worst_strategy': worst['bucket'],
            'worst_trade_count': worst['trade_count'],
            'worst_avg_return_pct': worst['avg_return_pct'],
            'worst_win_rate': worst['win_rate'],
            'sample_ready': best['trade_count'] >= min_sample_size,
            'qualified_strategies': len(qualified),
            'strategies_seen': len(rows),
        })
    regime_strategy_fit.sort(key=lambda item: (not item['sample_ready'], item['regime']))

    strategy_policy_fit = []
    for strategy, rows in sorted(strategy_policy_map.items()):
        ranked = sorted(rows, key=lambda item: (-item['avg_return_pct'], -item['win_rate'], -item['trade_count'], item.get('secondary_bucket') or ''))
        if not ranked:
            continue
        best = ranked[0]
        strategy_policy_fit.append({
            'strategy': strategy,
            'best_policy_version': best.get('secondary_bucket') or 'unknown',
            'trade_count': best['trade_count'],
            'avg_return_pct': best['avg_return_pct'],
            'win_rate': best['win_rate'],
            'sample_ready': best['trade_count'] >= min_sample_size,
        })
    strategy_policy_fit.sort(key=lambda item: (not item['sample_ready'], -item['avg_return_pct'], -item['trade_count'], item['strategy']))

    return {
        'by_strategy': by_strategy,
        'by_regime_strategy': by_regime_strategy,
        'by_policy_strategy': by_policy_strategy,
        'regime_strategy_fit': regime_strategy_fit,
        'strategy_policy_fit': strategy_policy_fit,
    }


def _build_policy_regime_lookup(rows: List[Dict]) -> Dict[Tuple[str, str], Dict]:
    return {
        (_normalize_bucket_tag(row.get('bucket')), _normalize_bucket_tag(row.get('secondary_bucket'))): row
        for row in (rows or [])
    }


def _evaluate_rollout_gate(row: Dict, *, min_sample_size: int) -> Dict:
    trade_count = int(row.get('trade_count') or 0)
    avg_return_pct = float(row.get('avg_return_pct') or 0.0)
    win_rate = float(row.get('win_rate') or 0.0)
    bucket = _normalize_bucket_tag(row.get('bucket'))
    policy = _normalize_bucket_tag(row.get('secondary_bucket'))
    if trade_count < min_sample_size:
        return {
            'decision': 'hold',
            'reason': 'sample_gap',
            'message': f'{bucket} × {policy} 样本不足，继续观察，不建议放大 rollout',
            'trade_count': trade_count,
            'min_sample_size': min_sample_size,
        }
    if avg_return_pct < 0 and win_rate < 45:
        return {
            'decision': 'rollback',
            'reason': 'negative_return_and_low_win_rate',
            'message': f'{bucket} × {policy} 回报转负且胜率偏低，优先考虑回滚或冻结该 rollout',
            'trade_count': trade_count,
            'avg_return_pct': avg_return_pct,
            'win_rate': win_rate,
        }
    if avg_return_pct < 0:
        return {
            'decision': 'tighten',
            'reason': 'negative_return',
            'message': f'{bucket} × {policy} 平均回报为负，优先收紧 decision/risk/execution 侧参数',
            'trade_count': trade_count,
            'avg_return_pct': avg_return_pct,
            'win_rate': win_rate,
        }
    if win_rate >= 60 and avg_return_pct > 0:
        return {
            'decision': 'expand',
            'reason': 'stable_positive_edge',
            'message': f'{bucket} × {policy} 样本达标且边际稳定，可作为扩大 rollout 候选',
            'trade_count': trade_count,
            'avg_return_pct': avg_return_pct,
            'win_rate': win_rate,
        }
    return {
        'decision': 'hold',
        'reason': 'mixed_edge',
        'message': f'{bucket} × {policy} 表现中性，先维持当前 rollout 并继续观察',
        'trade_count': trade_count,
        'avg_return_pct': avg_return_pct,
        'win_rate': win_rate,
    }


def _build_policy_ab_diffs(by_policy: List[Dict], by_regime_policy: List[Dict], *, min_sample_size: int) -> List[Dict]:
    if len(by_policy) < 2:
        return []
    lookup = _build_policy_regime_lookup(by_regime_policy)
    baseline_row = max(by_policy, key=lambda row: (row.get('trade_count', 0), row.get('bucket', '')))
    baseline_policy = _normalize_bucket_tag(baseline_row.get('bucket'))
    diffs = []
    for candidate in by_policy:
        candidate_policy = _normalize_bucket_tag(candidate.get('bucket'))
        if candidate_policy == baseline_policy:
            continue
        regime_deltas = []
        candidate_regimes = [
            row for row in by_regime_policy
            if _normalize_bucket_tag(row.get('secondary_bucket')) == candidate_policy
        ]
        for row in candidate_regimes:
            regime = _normalize_bucket_tag(row.get('bucket'))
            baseline_pair = lookup.get((regime, baseline_policy))
            if not baseline_pair:
                continue
            regime_deltas.append({
                'regime': regime,
                'candidate_policy_version': candidate_policy,
                'baseline_policy_version': baseline_policy,
                'candidate_trade_count': row['trade_count'],
                'baseline_trade_count': baseline_pair['trade_count'],
                'delta_trade_count': row['trade_count'] - baseline_pair['trade_count'],
                'delta_win_rate': round(float(row['win_rate']) - float(baseline_pair['win_rate']), 4),
                'delta_avg_return_pct': round(float(row['avg_return_pct']) - float(baseline_pair['avg_return_pct']), 4),
                'sample_ready': row['trade_count'] >= min_sample_size and baseline_pair['trade_count'] >= min_sample_size,
                'candidate_beats_baseline': float(row['avg_return_pct']) > float(baseline_pair['avg_return_pct']),
            })
        regime_deltas.sort(key=lambda item: (not item['sample_ready'], -item['delta_avg_return_pct'], item['regime']))
        diffs.append({
            'baseline_policy_version': baseline_policy,
            'candidate_policy_version': candidate_policy,
            'baseline_trade_count': baseline_row['trade_count'],
            'candidate_trade_count': candidate['trade_count'],
            'delta_trade_count': candidate['trade_count'] - baseline_row['trade_count'],
            'delta_win_rate': round(float(candidate['win_rate']) - float(baseline_row['win_rate']), 4),
            'delta_avg_return_pct': round(float(candidate['avg_return_pct']) - float(baseline_row['avg_return_pct']), 4),
            'candidate_beats_baseline': float(candidate['avg_return_pct']) > float(baseline_row['avg_return_pct']),
            'sample_ready': candidate['trade_count'] >= min_sample_size and baseline_row['trade_count'] >= min_sample_size,
            'regime_deltas': regime_deltas,
        })
    diffs.sort(key=lambda item: (not item['sample_ready'], -item['delta_avg_return_pct'], item['candidate_policy_version']))
    return diffs


def _build_policy_ab_regime_lookup(policy_ab_diffs: List[Dict]) -> Dict[Tuple[str, str], Dict]:
    lookup: Dict[Tuple[str, str], Dict] = {}
    for diff in policy_ab_diffs or []:
        candidate_policy = _normalize_bucket_tag(diff.get('candidate_policy_version'))
        for row in diff.get('regime_deltas') or []:
            lookup[(_normalize_bucket_tag(row.get('regime')), candidate_policy)] = {
                **row,
                'baseline_policy_version': diff.get('baseline_policy_version'),
                'overall_sample_ready': diff.get('sample_ready', False),
                'overall_candidate_beats_baseline': diff.get('candidate_beats_baseline', False),
                'overall_delta_avg_return_pct': diff.get('delta_avg_return_pct', 0.0),
                'overall_delta_win_rate': diff.get('delta_win_rate', 0.0),
            }
    return lookup


def _build_strategy_regime_lookup(rows: List[Dict]) -> Dict[Tuple[str, str], Dict]:
    return {
        (_normalize_bucket_tag(row.get('secondary_bucket')), _normalize_bucket_tag(row.get('bucket'))): row
        for row in (rows or [])
    }


def _build_regime_strategy_fit_lookup(rows: List[Dict]) -> Dict[str, Dict]:
    return {
        _normalize_bucket_tag(row.get('regime')): row
        for row in (rows or [])
    }


def _delivery_bucket_id(regime: str, policy_version: str) -> str:
    return f'{_normalize_bucket_tag(regime)}::{_normalize_bucket_tag(policy_version)}'


def _delivery_strategy_bucket_id(regime: str, strategy: str) -> str:
    return f'strategy::{_normalize_bucket_tag(regime)}::{_normalize_bucket_tag(strategy)}'

def _delivery_joint_bucket_id(regime: str, policy_version: str, strategy: str) -> str:
    return f'joint::{_normalize_bucket_tag(regime)}::{_normalize_bucket_tag(policy_version)}::{_normalize_bucket_tag(strategy)}'


def _recommendation_decision_rank(value: Optional[str]) -> int:
    return {'rollback': 0, 'tighten': 1, 'hold': 2, 'expand': 3}.get(_normalize_bucket_tag(value), 9)


def _recommendation_mode_rank(value: Optional[str]) -> int:
    return {'rollback': 0, 'tighten': 1, 'review': 2, 'observe': 3, 'rollout': 4}.get(_normalize_bucket_tag(value), 9)


def _strategy_policy_preference_lookup(rows: List[Dict]) -> Dict[str, Dict]:
    return {
        _normalize_bucket_tag(row.get('strategy')): row
        for row in (rows or [])
    }


def _build_regime_policy_strategy_rows(trades: List[Dict]) -> List[Dict]:
    buckets: Dict[Tuple[str, str, str], List[Dict]] = defaultdict(list)
    for trade in trades or []:
        regime = _normalize_bucket_tag(trade.get('regime_tag'))
        policy = _normalize_bucket_tag(trade.get('policy_tag'))
        strategies = _normalize_strategy_tags(trade.get('strategy_tags')) or ['unknown']
        for strategy in strategies:
            buckets[(regime, policy, strategy)].append(trade)

    rows = []
    for (regime, policy, strategy), bucket_trades in sorted(buckets.items()):
        returns = [float(t.get('return_pct', 0) or 0) for t in bucket_trades]
        wins = sum(1 for value in returns if value > 0)
        losses = sum(1 for value in returns if value < 0)
        avg_return = sum(returns) / len(returns) if returns else 0.0
        rows.append({
            'regime': regime,
            'policy_version': policy,
            'strategy': strategy,
            'trade_count': len(bucket_trades),
            'wins': wins,
            'losses': losses,
            'win_rate': round((wins / len(bucket_trades) * 100), 2) if bucket_trades else 0.0,
            'total_return_pct': round(sum(returns), 4),
            'avg_return_pct': round(avg_return, 4),
            'avg_abs_return_pct': round(sum(abs(v) for v in returns) / len(returns), 4) if returns else 0.0,
        })
    rows.sort(key=lambda row: (-row['trade_count'], row['regime'], row['policy_version'], row['strategy']))
    return rows


def _dedupe_actions(actions: List[Dict]) -> List[Dict]:
    deduped = []
    seen = set()
    for action in actions or []:
        key = (action.get('type'), action.get('title'))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    return deduped


def _build_joint_governance_item(
    row: Dict,
    policy_recommendation: Dict,
    strategy_recommendation: Dict,
    strategy_policy_fit: Optional[Dict],
    *,
    min_sample_size: int,
) -> Dict:
    regime = _normalize_bucket_tag(row.get('regime'))
    policy = _normalize_bucket_tag(row.get('policy_version'))
    strategy = _normalize_bucket_tag(row.get('strategy'))
    trade_count = int(row.get('trade_count') or 0)
    policy_type = _normalize_bucket_tag(policy_recommendation.get('type'))
    strategy_type = _normalize_bucket_tag(strategy_recommendation.get('type'))
    policy_mode = _normalize_bucket_tag(policy_recommendation.get('governance_mode'))
    strategy_mode = _normalize_bucket_tag(strategy_recommendation.get('governance_mode'))
    preferred_policy = _normalize_bucket_tag((strategy_policy_fit or {}).get('best_policy_version'))
    strategy_prefers_other_policy = bool(preferred_policy and preferred_policy != 'unknown' and preferred_policy != policy and (strategy_policy_fit or {}).get('sample_ready'))

    conflict_category = 'aligned'
    blocking_precedence = 'none'
    priority_resolution = 'aligned_expand' if policy_mode == 'rollout' and strategy_mode == 'rollout' else 'aligned_hold'
    resolution_reason = 'policy 与 strategy 建议方向一致，可按既有治理节奏执行。'
    final_decision = 'observe'
    final_mode = 'observe'
    fallback_decision = policy_mode or 'observe'

    policy_mode_rank = _recommendation_mode_rank(policy_mode)
    strategy_mode_rank = _recommendation_mode_rank(strategy_mode)

    if policy_mode_rank < strategy_mode_rank and policy_mode in {'rollback', 'tighten', 'review', 'observe'}:
        conflict_category = 'policy_blocking_precedence'
        blocking_precedence = 'policy'
        priority_resolution = 'policy_blocking_precedence'
        resolution_reason = 'policy bucket 的治理结论比 strategy 更保守，联合治理先服从 policy blocker。'
        if policy_mode == 'rollback':
            final_decision = 'freeze'
        elif policy_mode == 'tighten':
            final_decision = 'deweight'
        else:
            final_decision = 'observe'
        final_mode = policy_mode
    elif strategy_mode_rank < policy_mode_rank and strategy_mode in {'rollback', 'tighten', 'review'}:
        conflict_category = 'strategy_blocking_precedence'
        blocking_precedence = 'strategy'
        priority_resolution = 'strategy_blocking_precedence'
        resolution_reason = 'strategy fit 的治理结论比 policy 更保守，联合治理先服从 strategy blocker。'
        final_decision = 'freeze' if strategy_mode == 'rollback' else 'deweight' if strategy_mode == 'tighten' else 'observe'
        final_mode = strategy_mode
    elif policy_mode == 'rollout' and strategy_mode in {'rollback', 'tighten', 'review'}:
        conflict_category = 'strategy_blocks_policy_expand'
        blocking_precedence = 'strategy'
        priority_resolution = 'strategy_blocking_precedence'
        resolution_reason = '虽然 policy bucket 可扩，但 strategy fit 未确认稳定，先限制到 strategy 级冻结/降权/复核。'
        final_decision = 'freeze' if strategy_mode == 'rollback' else 'deweight' if strategy_mode == 'tighten' else 'observe'
        final_mode = strategy_mode
    elif strategy_prefers_other_policy and policy_mode == 'rollout':
        conflict_category = 'policy_strategy_preference_mismatch'
        blocking_precedence = 'strategy_policy_fit'
        priority_resolution = 'policy_preference_guardrail'
        resolution_reason = f'{strategy} 的最佳 policy 倾向 {preferred_policy}，当前 policy 仅适合 guarded/limited rollout。'
        final_decision = 'deweight'
        final_mode = 'tighten'
    elif policy_mode == 'observe' and strategy_mode == 'rollout':
        conflict_category = 'policy_hold_strategy_expand'
        blocking_precedence = 'policy'
        priority_resolution = 'policy_hold_caps_strategy_expand'
        resolution_reason = 'strategy fit 虽正向，但 policy 层未放行；只可维持观察或小范围白名单验证。'
        final_decision = 'observe'
        final_mode = 'observe'
    elif policy_mode == 'rollout' and strategy_mode == 'rollout':
        final_decision = 'expand'
        final_mode = 'rollout'
    elif policy_mode == 'rollback' or strategy_mode == 'rollback':
        final_decision = 'freeze'
        final_mode = 'rollback'
    elif policy_mode == 'tighten' or strategy_mode == 'tighten':
        final_decision = 'deweight'
        final_mode = 'tighten'

    combined_actions = []
    if final_decision == 'freeze':
        combined_actions.append(_governance_action('joint_freeze', '联合冻结', '联合治理判定需要先冻结该 policy × strategy 组合的新增 rollout/权重。', owner='ops', urgency='immediate'))
    elif final_decision == 'deweight':
        combined_actions.append(_governance_action('joint_deweight', '联合降权', '联合治理判定先收缩该组合的 rollout 或 strategy 权重，再继续复核。', owner='ops', urgency='high'))
    elif final_decision == 'expand':
        combined_actions.append(_governance_action('joint_expand_guarded', '联合小步扩张', 'policy 与 strategy 同时支持扩量，可在 guardrail 下对白名单组合做小步扩张。', owner='ops', urgency='normal'))
    else:
        combined_actions.append(_governance_action('joint_observe', '联合观察', '联合治理判定先保持观察，不做自动扩量。', owner='research', urgency='normal'))

    if strategy_prefers_other_policy:
        combined_actions.append(_governance_action('prefer_strategy_best_policy', '优先对齐 strategy 最优 policy', f'该 strategy 当前更匹配 {preferred_policy}，扩量前应优先对齐最优 policy 组合。', owner='research', urgency='high', preferred_policy_version=preferred_policy))
    combined_actions.extend(policy_recommendation.get('actions') or [])
    combined_actions.extend(strategy_recommendation.get('actions') or [])
    combined_actions = _dedupe_actions(combined_actions)[:8]

    blocking_reasons = []
    for issue in [policy_recommendation.get('blocking_issue'), strategy_recommendation.get('blocking_issue')]:
        if issue and issue not in blocking_reasons:
            blocking_reasons.append(issue)
    if strategy_prefers_other_policy and 'strategy_prefers_other_policy' not in blocking_reasons:
        blocking_reasons.append('strategy_prefers_other_policy')

    priority = min(policy_recommendation.get('priority'), strategy_recommendation.get('priority'), key=lambda v: _priority_rank(v))
    confidence = min(policy_recommendation.get('confidence'), strategy_recommendation.get('confidence'), key=lambda v: _confidence_rank(v))

    return {
        'bucket_id': _delivery_joint_bucket_id(regime, policy, strategy),
        'scope': 'joint',
        'regime': regime,
        'policy_version': policy,
        'strategy': strategy,
        'type': 'joint_governance',
        'priority': priority,
        'confidence': confidence,
        'conflict_resolution': {
            'category': conflict_category,
            'priority_resolution': priority_resolution,
            'blocking_precedence': blocking_precedence,
            'fallback_decision': fallback_decision,
            'resolution_reason': resolution_reason,
            'strategy_preferred_policy_version': preferred_policy or None,
            'strategy_prefers_other_policy': strategy_prefers_other_policy,
            'blocking_issues': blocking_reasons,
        },
        'combined_actions': combined_actions,
        'final_governance_decision': {
            'decision': final_decision,
            'governance_mode': final_mode,
            'summary_line': f'{regime} × {policy} × {strategy}: {final_decision} via {priority_resolution}',
            'blocking': bool(blocking_reasons or conflict_category != 'aligned'),
        },
        'policy_recommendation': policy_recommendation,
        'strategy_recommendation': strategy_recommendation,
        'evidence': {
            'trade_count': trade_count,
            'wins': int(row.get('wins') or 0),
            'losses': int(row.get('losses') or 0),
            'win_rate': float(row.get('win_rate') or 0.0),
            'avg_return_pct': float(row.get('avg_return_pct') or 0.0),
            'total_return_pct': float(row.get('total_return_pct') or 0.0),
            'policy_trade_count': (policy_recommendation.get('evidence') or {}).get('trade_count'),
            'strategy_trade_count': (strategy_recommendation.get('evidence') or {}).get('trade_count'),
            'strategy_policy_fit': strategy_policy_fit or {},
            'min_sample_size': min_sample_size,
        },
    }


def _build_joint_governance_summary(items: List[Dict]) -> Dict:
    categories = sorted({(item.get('conflict_resolution') or {}).get('category') for item in items})
    decisions = sorted({(item.get('final_governance_decision') or {}).get('decision') for item in items})
    return {
        'item_count': len(items),
        'blocking': sum(1 for item in items if (item.get('final_governance_decision') or {}).get('blocking')),
        'by_conflict_category': {category: sum(1 for item in items if (item.get('conflict_resolution') or {}).get('category') == category) for category in categories if category},
        'by_final_decision': {decision: sum(1 for item in items if (item.get('final_governance_decision') or {}).get('decision') == decision) for decision in decisions if decision},
        'top_priority_items': [
            {
                'regime': item.get('regime'),
                'policy_version': item.get('policy_version'),
                'strategy': item.get('strategy'),
                'decision': (item.get('final_governance_decision') or {}).get('decision'),
                'summary_line': (item.get('final_governance_decision') or {}).get('summary_line'),
            }
            for item in sorted(items, key=lambda item: (_priority_rank(item.get('priority')), _confidence_rank(item.get('confidence')), -(item.get('evidence') or {}).get('trade_count', 0)))[:5]
        ],
    }


def _build_joint_governance_delivery(items: List[Dict]) -> Dict:
    ordered = sorted(items, key=lambda item: (_priority_rank(item.get('priority')), _confidence_rank(item.get('confidence')), -((item.get('evidence') or {}).get('trade_count') or 0), item.get('regime') or '', item.get('policy_version') or '', item.get('strategy') or ''))
    priority_queue = [
        {
            'bucket_id': item['bucket_id'],
            'scope': 'joint',
            'regime': item['regime'],
            'policy_version': item['policy_version'],
            'strategy': item['strategy'],
            'priority': item['priority'],
            'confidence': item['confidence'],
            'conflict_category': item['conflict_resolution']['category'],
            'blocking_precedence': item['conflict_resolution']['blocking_precedence'],
            'final_decision': item['final_governance_decision']['decision'],
            'governance_mode': item['final_governance_decision']['governance_mode'],
            'summary_line': item['final_governance_decision']['summary_line'],
            'combined_actions': item['combined_actions'][:3],
        }
        for item in ordered
    ]
    next_actions = [
        {
            'bucket_id': item['bucket_id'],
            'regime': item['regime'],
            'policy_version': item['policy_version'],
            'strategy': item['strategy'],
            'final_decision': item['final_governance_decision']['decision'],
            'combined_actions': item['combined_actions'],
            'conflict_resolution': item['conflict_resolution'],
        }
        for item in ordered if item.get('combined_actions')
    ]
    return {
        'items': ordered,
        'priority_queue': priority_queue,
        'blocking': [row for row in priority_queue if row.get('blocking_precedence') != 'none' or row.get('final_decision') in {'freeze', 'deweight'}],
        'next_actions': next_actions,
        'summary': _build_joint_governance_summary(ordered),
    }



def _priority_rank(value: Optional[str]) -> int:
    return {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}.get(_normalize_bucket_tag(value), 9)


def _confidence_rank(value: Optional[str]) -> int:
    return {'high': 0, 'medium': 1, 'low': 2}.get(_normalize_bucket_tag(value), 9)


def _build_orchestration_plan(item: Dict) -> Dict:
    recommendation = item.get('recommendation') or {}
    metrics = item.get('metrics') or {}
    baseline = item.get('baseline_comparison') or {}
    actions = recommendation.get('actions') or []
    bucket_id = item.get('bucket_id')
    trade_count = int(metrics.get('trade_count') or 0)
    next_review_after = recommendation.get('next_review_after_trade_count')

    blockers = []
    blocking_issue = recommendation.get('blocking_issue')
    if blocking_issue:
        blockers.append({
            'type': 'blocking_issue',
            'value': blocking_issue,
            'source': 'recommendation',
            'resolved': False,
        })
    for guardrail in recommendation.get('guardrails') or []:
        blockers.append({
            'type': 'guardrail',
            'value': guardrail,
            'source': 'guardrails',
            'resolved': False,
        })
    if recommendation.get('type') == 'collect_more_samples':
        target = int((recommendation.get('thresholds') or {}).get('target_min_trade_count') or 0)
        blockers.append({
            'type': 'sample_gap',
            'value': f'missing_{max(target - trade_count, 0)}_samples',
            'source': 'thresholds',
            'resolved': trade_count >= target if target else False,
        })
    if baseline.get('sample_ready') and not baseline.get('candidate_beats_baseline'):
        blockers.append({
            'type': 'ab_conflict',
            'value': 'candidate_not_beating_baseline',
            'source': 'policy_ab_diffs',
            'resolved': False,
        })

    action_queue = []
    previous_action_id = None
    for index, action in enumerate(actions, start=1):
        action_type = action.get('type')
        action_id = f'{bucket_id}::{action_type}::{index}'
        if previous_action_id:
            depends_on = [previous_action_id]
        elif action_type in {'expand_guarded', 'rollback_to_baseline'} and blockers:
            depends_on = []
        else:
            depends_on = []
        action_queue.append({
            'id': action_id,
            'order': index,
            'type': action_type,
            'title': action.get('title'),
            'owner': action.get('owner'),
            'urgency': action.get('urgency'),
            'description': action.get('description'),
            'depends_on': depends_on,
            'blocking_prerequisites': [blocker['value'] for blocker in blockers] if index == 1 and blockers else [],
            'evidence': {
                'trade_count': trade_count,
                'avg_return_pct': metrics.get('avg_return_pct'),
                'win_rate': metrics.get('win_rate'),
                'baseline_policy_version': baseline.get('baseline_policy_version'),
                'candidate_beats_baseline': baseline.get('candidate_beats_baseline'),
            },
        })
        previous_action_id = action_id

    next_actions = action_queue[:2]
    review_checkpoints = []
    if next_review_after is not None:
        review_checkpoints.append({
            'type': 'trade_count',
            'target_trade_count': next_review_after,
            'current_trade_count': trade_count,
            'remaining_samples': max(int(next_review_after) - trade_count, 0),
            'reason': recommendation.get('reason') or recommendation.get('suggested_action'),
        })
    thresholds = recommendation.get('thresholds') or {}
    if thresholds:
        review_checkpoints.append({
            'type': 'thresholds',
            'values': thresholds,
            'guardrails': recommendation.get('guardrails') or [],
        })
    if baseline.get('baseline_policy_version'):
        review_checkpoints.append({
            'type': 'baseline_comparison',
            'baseline_policy_version': baseline.get('baseline_policy_version'),
            'sample_ready': baseline.get('sample_ready'),
            'candidate_beats_baseline': baseline.get('candidate_beats_baseline'),
            'delta_avg_return_pct': baseline.get('delta_avg_return_pct'),
            'delta_win_rate': baseline.get('delta_win_rate'),
        })

    rollback_candidate = {
        'eligible': bool(recommendation.get('rollout_plan', {}).get('requires_fast_rollback')),
        'reason': recommendation.get('blocking_issue') or item.get('gate', {}).get('reason'),
        'recommended_target': baseline.get('baseline_policy_version') or 'baseline_or_previous_safe_config',
        'mode': recommendation.get('rollout_plan', {}).get('mode'),
    }

    return {
        'priority_rank': _priority_rank(recommendation.get('priority')),
        'confidence_rank': _confidence_rank(recommendation.get('confidence')),
        'blockers': blockers,
        'blocking_chain': blockers,
        'action_queue': action_queue,
        'next_actions': next_actions,
        'review_checkpoints': review_checkpoints,
        'rollback_candidate': rollback_candidate,
    }

def _build_calibration_delivery_payload(
    *,
    summary: Dict,
    by_regime: List[Dict],
    by_policy: List[Dict],
    by_regime_policy: List[Dict],
    strategy_fit: Dict,
    policy_ab_diffs: List[Dict],
    rollout_gates: List[Dict],
    recommendations: List[Dict],
    strategy_governance: Optional[Dict] = None,
    joint_governance: Optional[Dict] = None,
) -> Dict:
    gate_lookup = {
        (_normalize_bucket_tag(row.get('regime')), _normalize_bucket_tag(row.get('policy_version'))): row
        for row in (rollout_gates or [])
    }
    recommendation_lookup = {
        (_normalize_bucket_tag(row.get('regime')), _normalize_bucket_tag(row.get('policy_version'))): row
        for row in (recommendations or [])
    }
    ab_lookup = _build_policy_ab_regime_lookup(policy_ab_diffs)

    items = []
    for row in by_regime_policy or []:
        regime = _normalize_bucket_tag(row.get('bucket'))
        policy = _normalize_bucket_tag(row.get('secondary_bucket'))
        gate = gate_lookup.get((regime, policy), {})
        recommendation = recommendation_lookup.get((regime, policy), {})
        ab_row = ab_lookup.get((regime, policy))
        priority = recommendation.get('priority', 'low')
        confidence = recommendation.get('confidence', 'low')
        item = {
            'bucket_id': _delivery_bucket_id(regime, policy),
            'regime': regime,
            'policy_version': policy,
            'metrics': {
                'trade_count': int(row.get('trade_count') or 0),
                'wins': int(row.get('wins') or 0),
                'losses': int(row.get('losses') or 0),
                'win_rate': float(row.get('win_rate') or 0.0),
                'avg_return_pct': float(row.get('avg_return_pct') or 0.0),
                'total_return_pct': float(row.get('total_return_pct') or 0.0),
                'avg_abs_return_pct': float(row.get('avg_abs_return_pct') or 0.0),
            },
            'gate': {
                'decision': gate.get('decision'),
                'reason': gate.get('reason'),
                'message': gate.get('message'),
            },
            'recommendation': {
                'type': recommendation.get('type'),
                'category': recommendation.get('category'),
                'priority': priority,
                'confidence': confidence,
                'governance_mode': recommendation.get('governance_mode'),
                'blocking_issue': recommendation.get('blocking_issue'),
                'suggested_action': recommendation.get('suggested_action'),
                'summary_line': recommendation.get('summary_line'),
                'reason': recommendation.get('reason'),
                'actions': recommendation.get('actions') or [],
                'rollout_plan': recommendation.get('rollout_plan') or {},
                'guardrails': recommendation.get('guardrails') or [],
                'thresholds': recommendation.get('thresholds') or {},
                'next_review_after_trade_count': recommendation.get('next_review_after_trade_count'),
            },
            'baseline_comparison': {
                'baseline_policy_version': (ab_row or {}).get('baseline_policy_version'),
                'sample_ready': bool((ab_row or {}).get('sample_ready')),
                'candidate_beats_baseline': bool((ab_row or {}).get('candidate_beats_baseline')),
                'delta_trade_count': (ab_row or {}).get('delta_trade_count'),
                'delta_win_rate': (ab_row or {}).get('delta_win_rate'),
                'delta_avg_return_pct': (ab_row or {}).get('delta_avg_return_pct'),
                'overall_sample_ready': bool((ab_row or {}).get('overall_sample_ready')),
                'overall_candidate_beats_baseline': bool((ab_row or {}).get('overall_candidate_beats_baseline')),
            },
            'status': {
                'blocking': bool(recommendation.get('blocking_issue')),
                'ready_for_dashboard': True,
                'ready_for_rollout_orchestration': bool(gate.get('decision') and recommendation.get('type')),
                'priority_rank': _priority_rank(priority),
                'confidence_rank': _confidence_rank(confidence),
            },
        }
        item['orchestration'] = _build_orchestration_plan(item)
        item['status']['blocking'] = bool(item['orchestration']['blockers'])
        items.append(item)

    items.sort(key=lambda item: (
        item['status']['priority_rank'],
        item['status']['confidence_rank'],
        -item['metrics']['trade_count'],
        item['regime'],
        item['policy_version'],
    ))

    queue = [
        {
            'bucket_id': item['bucket_id'],
            'regime': item['regime'],
            'policy_version': item['policy_version'],
            'decision': item['gate']['decision'],
            'recommendation_type': item['recommendation']['type'],
            'governance_mode': item['recommendation']['governance_mode'],
            'priority': item['recommendation']['priority'],
            'confidence': item['recommendation']['confidence'],
            'blocking': item['status']['blocking'],
            'primary_action': (item['recommendation']['actions'] or [{}])[0].get('type'),
            'summary_line': item['recommendation']['summary_line'],
            'next_actions': item['orchestration']['next_actions'],
            'blocking_chain': item['orchestration']['blocking_chain'],
            'review_checkpoints': item['orchestration']['review_checkpoints'],
            'rollback_candidate': item['orchestration']['rollback_candidate'],
        }
        for item in items
    ]

    def _queue_filter(*, decision: Optional[str] = None, recommendation_type: Optional[str] = None, blocking: Optional[bool] = None) -> List[Dict]:
        rows = queue
        if decision is not None:
            rows = [row for row in rows if row.get('decision') == decision]
        if recommendation_type is not None:
            rows = [row for row in rows if row.get('recommendation_type') == recommendation_type]
        if blocking is not None:
            rows = [row for row in rows if row.get('blocking') is blocking]
        return rows

    prioritized_queue = queue[:]
    next_actions = [
        {
            'bucket_id': item['bucket_id'],
            'regime': item['regime'],
            'policy_version': item['policy_version'],
            'decision': item['gate']['decision'],
            'recommendation_type': item['recommendation']['type'],
            'action_queue': item['orchestration']['action_queue'],
            'next_actions': item['orchestration']['next_actions'],
            'blocking_chain': item['orchestration']['blocking_chain'],
        }
        for item in items
        if item['orchestration']['next_actions']
    ]
    blocking_chain = [
        {
            'bucket_id': item['bucket_id'],
            'regime': item['regime'],
            'policy_version': item['policy_version'],
            'decision': item['gate']['decision'],
            'blocking_chain': item['orchestration']['blocking_chain'],
        }
        for item in items
        if item['orchestration']['blocking_chain']
    ]
    review_checkpoints = [
        {
            'bucket_id': item['bucket_id'],
            'regime': item['regime'],
            'policy_version': item['policy_version'],
            'decision': item['gate']['decision'],
            'checkpoints': item['orchestration']['review_checkpoints'],
        }
        for item in items
        if item['orchestration']['review_checkpoints']
    ]
    rollback_candidates = [
        {
            'bucket_id': item['bucket_id'],
            'regime': item['regime'],
            'policy_version': item['policy_version'],
            **item['orchestration']['rollback_candidate'],
        }
        for item in items
        if item['orchestration']['rollback_candidate'].get('eligible')
    ]
    delivery = {
        'schema_version': 'm5_delivery_v1',
        'summary': {
            'trade_count': summary.get('trade_count', 0),
            'min_sample_size': summary.get('min_sample_size', 0),
            'calibration_ready': bool(summary.get('calibration_ready')),
            'policy_ab_ready': bool(summary.get('policy_ab_ready')),
            'strategy_fit_ready': bool(summary.get('strategy_fit_ready')),
            'bucket_count': len(items),
            'regime_count': len(by_regime or []),
            'policy_count': len(by_policy or []),
            'strategy_count': len((strategy_fit or {}).get('by_strategy') or []),
            'blocking_chain_count': len(blocking_chain),
            'next_action_bucket_count': len(next_actions),
            'rollback_candidate_count': len(rollback_candidates),
        },
        'views': {
            'items': items,
            'tables': {
                'by_regime': by_regime,
                'by_policy_version': by_policy,
                'by_regime_policy': by_regime_policy,
                'by_strategy': (strategy_fit or {}).get('by_strategy') or [],
                'by_regime_strategy': (strategy_fit or {}).get('by_regime_strategy') or [],
                'by_policy_strategy': (strategy_fit or {}).get('by_policy_strategy') or [],
                'regime_strategy_fit': (strategy_fit or {}).get('regime_strategy_fit') or [],
                'strategy_policy_fit': (strategy_fit or {}).get('strategy_policy_fit') or [],
                'policy_ab_diffs': policy_ab_diffs,
                'rollout_gates': rollout_gates,
                'recommendations': recommendations[:50],
                'strategy_recommendations': (strategy_governance or {}).get('items') or [],
                'joint_governance': (joint_governance or {}).get('items') or [],
            },
        },
        'render_ready': {
            'headline': {
                'top_regime': summary.get('top_regime'),
                'top_policy_version': summary.get('top_policy_version'),
                'top_strategy': summary.get('top_strategy'),
                'trade_count': summary.get('trade_count', 0),
                'bucket_count': len(items),
            },
            'sections': {
                'priority_queue': prioritized_queue[:10],
                'blocking_items': _queue_filter(blocking=True),
                'rollout_candidates': _queue_filter(decision='expand'),
                'tighten_watchlist': _queue_filter(decision='tighten'),
                'rollback_queue': _queue_filter(decision='rollback'),
                'sample_gap_queue': _queue_filter(recommendation_type='collect_more_samples'),
                'next_actions': next_actions[:10],
                'review_checkpoints': review_checkpoints[:10],
                'strategy_priority_queue': ((strategy_governance or {}).get('priority_queue') or [])[:10],
                'strategy_blocking_items': ((strategy_governance or {}).get('blocking') or [])[:10],
                'joint_priority_queue': ((joint_governance or {}).get('priority_queue') or [])[:10],
                'joint_blocking_items': ((joint_governance or {}).get('blocking') or [])[:10],
            },
        },
        'orchestration_ready': {
            'queue': prioritized_queue,
            'prioritized_queue': prioritized_queue,
            'next_actions': next_actions,
            'blocking_chain': blocking_chain,
            'review_checkpoints': review_checkpoints,
            'rollback_candidates': rollback_candidates,
            'queues': {
                'blocking': _queue_filter(blocking=True),
                'expand': _queue_filter(decision='expand'),
                'tighten': _queue_filter(decision='tighten'),
                'rollback': _queue_filter(decision='rollback'),
                'observe': [row for row in prioritized_queue if row.get('governance_mode') == 'observe'],
                'review': [row for row in prioritized_queue if row.get('governance_mode') == 'review'],
            },
            'action_catalog': dict(sorted({
                action_type: sum(1 for rec in recommendations for action in (rec.get('actions') or []) if action['type'] == action_type)
                for action_type in {
                    action['type']
                    for rec in recommendations
                    for action in (rec.get('actions') or [])
                }
            }.items())),
            'strategy_priority_queue': (strategy_governance or {}).get('priority_queue') or [],
            'strategy_next_actions': (strategy_governance or {}).get('next_actions') or [],
            'joint_priority_queue': (joint_governance or {}).get('priority_queue') or [],
            'joint_next_actions': (joint_governance or {}).get('next_actions') or [],
        },
    }
    delivery['governance_ready'] = build_joint_governance_ready_payload({
        'summary': summary,
        'delivery': delivery,
        'joint_governance': joint_governance or {},
    })
    return delivery


def _recommendation_priority(decision: str, reason: str, ab_row: Optional[Dict]) -> str:
    if decision == 'rollback':
        return 'critical'
    if decision == 'tighten':
        return 'high'
    if reason == 'sample_gap':
        return 'medium'
    if ab_row and ab_row.get('sample_ready') and not ab_row.get('candidate_beats_baseline'):
        return 'high'
    if decision == 'expand':
        return 'medium'
    return 'low'


def _recommendation_confidence(gate: Dict, ab_row: Optional[Dict], *, min_sample_size: int) -> str:
    trade_count = int(gate.get('trade_count') or 0)
    decision = gate.get('decision')
    if decision == 'rollback' and trade_count >= min_sample_size + 2:
        return 'high'
    if decision in {'tighten', 'expand'} and trade_count >= min_sample_size + 1:
        return 'high'
    if ab_row and ab_row.get('sample_ready'):
        return 'medium'
    if trade_count >= min_sample_size:
        return 'medium'
    return 'low'


def _governance_action(action_type: str, title: str, description: str, **extra) -> Dict:
    action = {
        'type': action_type,
        'title': title,
        'description': description,
    }
    action.update(extra)
    return action


def _build_calibration_recommendation(gate: Dict, bucket_row: Dict, ab_row: Optional[Dict], *, min_sample_size: int) -> Dict:
    regime = gate['regime']
    policy = gate['policy_version']
    decision = gate['decision']
    reason = gate['reason']
    trade_count = int(bucket_row.get('trade_count') or 0)
    avg_return_pct = float(bucket_row.get('avg_return_pct') or 0.0)
    win_rate = float(bucket_row.get('win_rate') or 0.0)
    priority = _recommendation_priority(decision, reason, ab_row)
    confidence = _recommendation_confidence(gate, ab_row, min_sample_size=min_sample_size)
    baseline_ready = bool(ab_row and ab_row.get('sample_ready'))
    beats_baseline = bool(ab_row and ab_row.get('candidate_beats_baseline'))
    baseline_delta = float((ab_row or {}).get('delta_avg_return_pct') or 0.0)

    recommendation_type = 'maintain_hold'
    category = 'mixed_signal'
    suggested_action = '维持当前 rollout，不急于放大，继续观察更多样本。'
    blocking_issue = None
    reason_text = gate['message']
    governance_mode = 'observe'
    actions = [
        _governance_action(
            'maintain_guardrails',
            '维持现有 guardrails',
            '保持当前 rollout gate 与风险阈值不变，持续跟踪后续 bucket 漂移。',
            owner='research',
            urgency='normal',
        )
    ]
    next_review_after = max(min_sample_size, trade_count + 2)
    rollout_plan = {'mode': 'hold', 'max_rollout_pct': 0, 'requires_fast_rollback': False}
    thresholds = {'target_min_trade_count': min_sample_size}
    guardrails = []

    if reason == 'sample_gap':
        recommendation_type = 'collect_more_samples'
        category = 'sample_sufficiency'
        blocking_issue = 'insufficient_sample'
        governance_mode = 'observe'
        next_review_after = min_sample_size
        suggested_action = (
            f'优先补齐 {regime} × {policy} 的样本；在达到至少 {min_sample_size} 笔可比交易前，避免 rollout / repricing。'
        )
        reason_text = f'{reason_text} 当前仅 {trade_count} 笔，未达到最小样本门槛。'
        actions = [
            _governance_action(
                'collect_more_samples',
                '补齐 bucket 样本',
                f'继续收集 {regime} × {policy} 交易样本，至少补到 {min_sample_size} 笔再重新评估。',
                owner='research',
                target_sample_size=min_sample_size,
                missing_samples=max(min_sample_size - trade_count, 0),
                urgency='normal',
            ),
            _governance_action(
                'rollout_freeze',
                '冻结新增 rollout',
                '样本不足期间不扩大该 bucket 的 rollout，也不做 repricing。',
                owner='ops',
                urgency='normal',
            ),
        ]
        rollout_plan = {'mode': 'freeze', 'max_rollout_pct': 0, 'requires_fast_rollback': False}
        thresholds = {'target_min_trade_count': min_sample_size, 'current_trade_count': trade_count}
        guardrails = ['until_sample_ready']
    elif decision == 'rollback':
        recommendation_type = 'rollout_freeze'
        category = 'underperform'
        blocking_issue = 'negative_return_and_low_win_rate'
        governance_mode = 'rollback'
        next_review_after = trade_count + 2
        suggested_action = '冻结或回滚该 policy 在对应 regime 的 rollout，恢复 baseline，并复核 entry/filter/risk 定价。'
        actions = [
            _governance_action(
                'rollout_freeze',
                '立即冻结 rollout',
                '暂停该 regime × policy 的新增放量，防止继续放大负边际。',
                owner='ops',
                urgency='immediate',
            ),
            _governance_action(
                'rollback_to_baseline',
                '回退到 baseline policy',
                '把该 bucket 恢复到基线 policy 或默认风控参数，再观察恢复情况。',
                owner='ops',
                baseline_policy_version=(ab_row or {}).get('baseline_policy_version'),
                urgency='immediate',
            ),
            _governance_action(
                'repricing_review',
                '复核 entry/filter/risk 定价',
                '检查 entry 门槛、过滤器、仓位/止损/执行参数是否过宽或定价错误。',
                owner='research',
                urgency='high',
            ),
        ]
        rollout_plan = {'mode': 'rollback', 'max_rollout_pct': 0, 'requires_fast_rollback': True}
        thresholds = {'max_negative_avg_return_pct': 0.0, 'min_win_rate_pct': 45.0}
        guardrails = ['rollback_enabled', 'baseline_only_until_review']
    elif decision == 'tighten':
        recommendation_type = 'tighten_thresholds'
        category = 'underperform'
        blocking_issue = 'negative_return'
        governance_mode = 'tighten'
        next_review_after = trade_count + 2
        suggested_action = '先收紧放行阈值与风险/执行参数；若后续补样后仍为负，再进入 rollback 或 repricing。'
        actions = [
            _governance_action(
                'tighten_thresholds',
                '收紧 decision / risk 阈值',
                '优先收紧 entry、validator、risk/execution 阈值，降低继续放大亏损的概率。',
                owner='research',
                urgency='high',
            ),
            _governance_action(
                'repricing_review',
                '检查定价是否偏宽',
                '若 tightening 后仍持续为负，进入 repricing review，重新校准止损、仓位与执行容忍度。',
                owner='research',
                urgency='normal',
            ),
        ]
        rollout_plan = {'mode': 'tighten', 'max_rollout_pct': 25, 'requires_fast_rollback': True}
        thresholds = {'max_negative_avg_return_pct': 0.0}
        guardrails = ['reduced_rollout_cap']
    elif decision == 'expand':
        recommendation_type = 'expand_guarded'
        category = 'validated_edge'
        governance_mode = 'rollout'
        next_review_after = trade_count + 2
        suggested_action = '可小步扩大该 regime 下 rollout，继续监控胜率、均值回报与回撤漂移。'
        actions = [
            _governance_action(
                'expand_guarded',
                '小步灰度扩量',
                '在保留快速回退开关的前提下，小步提升该 bucket 的 rollout 占比。',
                owner='ops',
                urgency='normal',
            ),
            _governance_action(
                'monitor_drift',
                '持续观察 edge 漂移',
                '扩量后继续监控 win_rate、avg_return_pct 与 regime 漂移，防止 edge 失真。',
                owner='ops',
                urgency='normal',
            ),
        ]
        rollout_plan = {'mode': 'guarded_expand', 'max_rollout_pct': 25, 'requires_fast_rollback': True}
        thresholds = {'min_win_rate_pct': 60.0, 'min_avg_return_pct': 0.0}
        guardrails = ['fast_rollback_ready', 'post_expand_monitoring']
    elif decision == 'hold':
        recommendation_type = 'mixed_signal_review'
        category = 'mixed_signal'
        blocking_issue = 'mixed_signal'
        governance_mode = 'observe'
        next_review_after = trade_count + 2
        suggested_action = '先保持当前配置，继续收集样本，并重点检查是否存在 regime 内部不稳定或符号分化。'
        actions = [
            _governance_action(
                'mixed_signal_review',
                '复核混合信号来源',
                '重点检查 regime 内部不稳定、symbol 分化或交易分布偏移。',
                owner='research',
                urgency='normal',
            )
        ]
        rollout_plan = {'mode': 'hold', 'max_rollout_pct': 0, 'requires_fast_rollback': False}
        guardrails = ['no_auto_expand']

    if ab_row:
        if baseline_ready and not beats_baseline:
            if decision == 'expand':
                recommendation_type = 'expand_guarded'
                category = 'mixed_signal'
                blocking_issue = 'ab_conflict_with_baseline'
                governance_mode = 'rollout'
                suggested_action = '虽然单桶 gate 偏正向，但相对 baseline 未形成优势；先小流量灰度，不建议全量 expand。'
                actions.insert(0, _governance_action(
                    'repricing_review',
                    '先确认 A/B 优势是否真实',
                    'expand 前先复核该 bucket 相对 baseline 的优势是否稳定，避免误把噪音当 edge。',
                    owner='research',
                    urgency='high',
                ))
                rollout_plan = {'mode': 'guarded_expand', 'max_rollout_pct': 10, 'requires_fast_rollback': True}
                guardrails.append('ab_advantage_required_for_full_expand')
            elif decision in {'hold', 'tighten'}:
                blocking_issue = blocking_issue or 'underperform_vs_baseline'
                suggested_action = '相对 baseline 未见优势，建议继续收紧/维持 hold，并把 repricing 或 rollback 纳入候选。'
                actions.insert(0, _governance_action(
                    'repricing_review',
                    '复核相对 baseline 的退化来源',
                    '重点分析为什么该 policy 在当前 regime 下跑输 baseline，再决定继续 tighten 还是 rollback。',
                    owner='research',
                    urgency='high',
                ))
                guardrails.append('baseline_advantage_missing')
        elif baseline_ready and beats_baseline and decision == 'expand':
            suggested_action = '该 regime 下已优于 baseline，可按小步灰度扩大 rollout，并保留快速回退开关。'
            rollout_plan = {'mode': 'guarded_expand', 'max_rollout_pct': 35, 'requires_fast_rollback': True}
            guardrails.append('baseline_advantage_confirmed')
        elif not baseline_ready and decision == 'expand':
            recommendation_type = 'expand_guarded'
            suggested_action = '单桶表现偏正向，但 baseline 对照样本未充分，建议仅做有限 rollout，继续补充 A/B 样本。'
            actions.append(_governance_action(
                'collect_more_samples',
                '补齐 A/B 对照样本',
                '由于 baseline 对照未达样本门槛，扩量期间必须继续补样验证。',
                owner='research',
                target_sample_size=min_sample_size,
                urgency='normal',
            ))
            rollout_plan = {'mode': 'guarded_expand', 'max_rollout_pct': 10, 'requires_fast_rollback': True}
            guardrails.append('ab_sample_gap')

    instability_flag = win_rate < 55 and avg_return_pct > 0 and decision == 'hold'
    if instability_flag:
        category = 'instability'
        recommendation_type = 'repricing_review'
        blocking_issue = blocking_issue or 'unstable_edge'
        governance_mode = 'review'
        suggested_action = '收益为正但稳定性不足，先不要扩量；继续观察并检查是否需要 tightening/repricing 降低波动。'
        actions = [
            _governance_action(
                'repricing_review',
                '复核收益来源与波动',
                '虽然平均回报为正，但胜率偏低，需检查是否靠少量极端收益支撑。',
                owner='research',
                urgency='high',
            ),
            _governance_action(
                'tighten_thresholds',
                '必要时收紧阈值',
                '若复核后确认 edge 不稳定，应先收紧阈值，避免误扩量。',
                owner='research',
                urgency='normal',
            ),
        ]
        rollout_plan = {'mode': 'hold', 'max_rollout_pct': 0, 'requires_fast_rollback': True}
        guardrails = ['no_expand_until_stable']

    return {
        'regime': regime,
        'policy_version': policy,
        'type': recommendation_type,
        'category': category,
        'priority': priority,
        'confidence': confidence,
        'reason': reason_text,
        'suggested_action': suggested_action,
        'blocking_issue': blocking_issue,
        'gate_decision': decision,
        'gate_reason': reason,
        'aligned_with_rollout_gate': True,
        'message': gate['message'],
        'governance_mode': governance_mode,
        'next_review_after_trade_count': next_review_after,
        'actions': actions,
        'rollout_plan': rollout_plan,
        'thresholds': thresholds,
        'guardrails': guardrails,
        'summary_line': f'{regime} × {policy}: {recommendation_type} ({decision}/{priority}/{confidence})',
        'evidence': {
            'trade_count': trade_count,
            'min_sample_size': min_sample_size,
            'avg_return_pct': avg_return_pct,
            'win_rate': win_rate,
            'baseline_comparison': ab_row,
            'baseline_ready': baseline_ready,
            'candidate_beats_baseline': beats_baseline,
            'delta_vs_baseline_avg_return_pct': round(baseline_delta, 4) if ab_row else None,
        },
    }



def _build_strategy_governance_recommendation(row: Dict, regime_fit: Optional[Dict], *, min_sample_size: int) -> Dict:
    strategy = _normalize_bucket_tag(row.get('bucket'))
    regime = _normalize_bucket_tag(row.get('secondary_bucket'))
    trade_count = int(row.get('trade_count') or 0)
    avg_return_pct = float(row.get('avg_return_pct') or 0.0)
    win_rate = float(row.get('win_rate') or 0.0)
    total_return_pct = float(row.get('total_return_pct') or 0.0)
    sample_ready = trade_count >= min_sample_size
    best_strategy = _normalize_bucket_tag((regime_fit or {}).get('best_strategy'))
    worst_strategy = _normalize_bucket_tag((regime_fit or {}).get('worst_strategy'))
    best_avg_return_pct = float((regime_fit or {}).get('best_avg_return_pct') or 0.0)
    worst_avg_return_pct = float((regime_fit or {}).get('worst_avg_return_pct') or 0.0)
    delta_vs_best = round(avg_return_pct - best_avg_return_pct, 4) if regime_fit else None
    delta_vs_worst = round(avg_return_pct - worst_avg_return_pct, 4) if regime_fit else None
    is_best = bool(regime_fit and strategy == best_strategy)
    is_worst = bool(regime_fit and strategy == worst_strategy)

    priority = 'low'
    confidence = 'low' if not sample_ready else 'medium'
    recommendation_type = 'strategy_observe'
    category = 'mixed_signal'
    governance_mode = 'observe'
    blocking_issue = None
    suggested_action = '维持当前 strategy 权重，继续观察更多 regime 样本。'
    actions = [
        _governance_action(
            'maintain_guardrails',
            '维持 strategy guardrails',
            '保持当前 strategy 权重与 guardrails，不做自动放量。',
            owner='research',
            urgency='normal',
        )
    ]
    rollout_plan = {'mode': 'hold', 'target_weight_pct': None, 'requires_fast_rollback': False}
    thresholds = {'target_min_trade_count': min_sample_size}
    guardrails = []
    reason = f'{strategy} 在 {regime} 下暂无明确优势，先保持观察。'
    summary_line = f'{regime} × {strategy}: strategy_observe (hold/{priority}/{confidence})'
    next_review_after = max(min_sample_size, trade_count + 2)

    if not sample_ready:
        priority = 'medium'
        recommendation_type = 'collect_more_samples'
        category = 'sample_sufficiency'
        governance_mode = 'observe'
        blocking_issue = 'insufficient_strategy_sample'
        suggested_action = f'该 strategy 在 {regime} 下样本未达标，先补齐至至少 {min_sample_size} 笔，再决定是否调权或冻结。'
        actions = [
            _governance_action(
                'collect_more_samples',
                '补齐 strategy 样本',
                f'继续收集 {strategy} 在 {regime} 下的样本，至少补到 {min_sample_size} 笔。',
                owner='research',
                target_sample_size=min_sample_size,
                missing_samples=max(min_sample_size - trade_count, 0),
                urgency='normal',
            ),
            _governance_action(
                'rollout_freeze',
                '冻结 strategy 扩量',
                '样本不足期间不扩大该 strategy 在当前 regime 下的权重。',
                owner='ops',
                urgency='normal',
            ),
        ]
        rollout_plan = {'mode': 'freeze', 'target_weight_pct': 0, 'requires_fast_rollback': False}
        thresholds = {'target_min_trade_count': min_sample_size, 'current_trade_count': trade_count}
        guardrails = ['until_strategy_sample_ready']
        reason = f'{strategy} 在 {regime} 下仅有 {trade_count} 笔样本，未达到最小治理门槛。'
        summary_line = f'{regime} × {strategy}: collect_more_samples (observe/{priority}/{confidence})'
        next_review_after = min_sample_size
    elif avg_return_pct < 0 and win_rate < 45:
        priority = 'critical'
        confidence = 'high' if trade_count >= min_sample_size + 2 else 'medium'
        recommendation_type = 'rollout_freeze'
        category = 'underperform'
        governance_mode = 'rollback'
        blocking_issue = 'strategy_negative_return_and_low_win_rate'
        suggested_action = '该 strategy 在当前 regime 下应先冻结/下线，避免继续放大负边际。'
        actions = [
            _governance_action('rollout_freeze', '冻结 strategy', '立即冻结该 strategy 在当前 regime 的新增权重。', owner='ops', urgency='immediate'),
            _governance_action('deweight_strategy', '下调 strategy 权重', '把该 strategy 权重降到最低或 0，待复核通过再恢复。', owner='ops', urgency='immediate'),
            _governance_action('repricing_review', '复核 strategy 定价/触发逻辑', '检查触发条件、仓位、止损与执行容忍度是否失配。', owner='research', urgency='high'),
        ]
        rollout_plan = {'mode': 'freeze', 'target_weight_pct': 0, 'requires_fast_rollback': True}
        thresholds = {'max_negative_avg_return_pct': 0.0, 'min_win_rate_pct': 45.0}
        guardrails = ['strategy_disabled_until_review']
        reason = f'{strategy} 在 {regime} 下平均回报为负且胜率偏低，应优先冻结。'
        summary_line = f'{regime} × {strategy}: rollout_freeze (rollback/{priority}/{confidence})'
    elif avg_return_pct < 0 or (is_worst and delta_vs_best is not None and delta_vs_best <= -0.5):
        priority = 'high'
        confidence = 'high' if trade_count >= min_sample_size + 1 else 'medium'
        recommendation_type = 'deweight_strategy'
        category = 'underperform'
        governance_mode = 'tighten'
        blocking_issue = 'strategy_underperforming_regime_fit'
        suggested_action = '该 strategy 在当前 regime 下应先降权并进入 review，避免继续主导该 bucket。'
        actions = [
            _governance_action('deweight_strategy', '下调 strategy 权重', '降低该 strategy 在当前 regime 下的权重占比。', owner='ops', urgency='high'),
            _governance_action('repricing_review', '复核 strategy 参数', '确认是否需要收紧触发阈值或重定价。', owner='research', urgency='high'),
        ]
        rollout_plan = {'mode': 'deweight', 'target_weight_pct': 10, 'requires_fast_rollback': True}
        thresholds = {'max_negative_avg_return_pct': 0.0, 'max_gap_vs_best_avg_return_pct': -0.5}
        guardrails = ['reduced_strategy_weight_cap']
        reason = f'{strategy} 在 {regime} 下落后于最佳 strategy，建议降权并复核。'
        summary_line = f'{regime} × {strategy}: deweight_strategy (tighten/{priority}/{confidence})'
    elif is_best and win_rate >= 60 and avg_return_pct > 0:
        priority = 'medium'
        confidence = 'high' if trade_count >= min_sample_size + 1 else 'medium'
        recommendation_type = 'expand_guarded'
        category = 'validated_edge'
        governance_mode = 'rollout'
        suggested_action = '该 strategy 在当前 regime 下 fit 明显，可小步扩张，但保留快速回退。'
        actions = [
            _governance_action('expand_guarded', '小步扩张 strategy 权重', '逐步提高该 strategy 在当前 regime 的权重。', owner='ops', urgency='normal'),
            _governance_action('monitor_drift', '监控 strategy 漂移', '扩张后持续观察收益、胜率与 regime 漂移。', owner='ops', urgency='normal'),
        ]
        rollout_plan = {'mode': 'guarded_expand', 'target_weight_pct': 35, 'requires_fast_rollback': True}
        thresholds = {'min_win_rate_pct': 60.0, 'min_avg_return_pct': 0.0}
        guardrails = ['fast_rollback_ready', 'post_expand_monitoring']
        reason = f'{strategy} 当前是 {regime} 下的最佳 fit，可按 guardrail 逐步扩张。'
        summary_line = f'{regime} × {strategy}: expand_guarded (rollout/{priority}/{confidence})'
    elif avg_return_pct > 0 and win_rate < 55:
        priority = 'medium'
        recommendation_type = 'repricing_review'
        category = 'instability'
        governance_mode = 'review'
        blocking_issue = 'unstable_strategy_edge'
        suggested_action = '收益为正但稳定性不足，先观察并复核触发/定价逻辑。'
        actions = [
            _governance_action('repricing_review', '复核 strategy edge 稳定性', '检查是否依赖少量极端收益支撑。', owner='research', urgency='high'),
            _governance_action('maintain_guardrails', '保持保守 guardrails', '在稳定前不自动扩量。', owner='ops', urgency='normal'),
        ]
        rollout_plan = {'mode': 'hold', 'target_weight_pct': None, 'requires_fast_rollback': True}
        thresholds = {'min_win_rate_pct': 55.0}
        guardrails = ['no_expand_until_stable']
        reason = f'{strategy} 在 {regime} 下收益为正，但稳定性不足，需先 review。'
        summary_line = f'{regime} × {strategy}: repricing_review (review/{priority}/{confidence})'

    return {
        'scope': 'strategy',
        'regime': regime,
        'strategy': strategy,
        'type': recommendation_type,
        'category': category,
        'priority': priority,
        'confidence': confidence,
        'reason': reason,
        'suggested_action': suggested_action,
        'blocking_issue': blocking_issue,
        'governance_mode': governance_mode,
        'next_review_after_trade_count': next_review_after,
        'actions': actions,
        'rollout_plan': rollout_plan,
        'thresholds': thresholds,
        'guardrails': guardrails,
        'summary_line': summary_line,
        'blocking': bool(blocking_issue or guardrails),
        'priority_rank': _priority_rank(priority),
        'confidence_rank': _confidence_rank(confidence),
        'fit_status': 'best' if is_best else 'worst' if is_worst else 'mixed',
        'evidence': {
            'trade_count': trade_count,
            'min_sample_size': min_sample_size,
            'win_rate': win_rate,
            'avg_return_pct': avg_return_pct,
            'total_return_pct': total_return_pct,
            'sample_ready': sample_ready,
            'best_strategy': best_strategy if regime_fit else None,
            'worst_strategy': worst_strategy if regime_fit else None,
            'delta_vs_best_avg_return_pct': delta_vs_best,
            'delta_vs_worst_avg_return_pct': delta_vs_worst,
            'qualified_strategies': (regime_fit or {}).get('qualified_strategies'),
            'strategies_seen': (regime_fit or {}).get('strategies_seen'),
        },
    }


def _build_strategy_governance_summary(recommendations: List[Dict]) -> Dict:
    action_types = sorted({action['type'] for rec in recommendations for action in (rec.get('actions') or [])})
    return {
        'critical': sum(1 for item in recommendations if item.get('priority') == 'critical'),
        'high': sum(1 for item in recommendations if item.get('priority') == 'high'),
        'medium': sum(1 for item in recommendations if item.get('priority') == 'medium'),
        'low': sum(1 for item in recommendations if item.get('priority') == 'low'),
        'by_type': dict(sorted({
            rec['type']: sum(1 for row in recommendations if row.get('type') == rec['type'])
            for rec in recommendations
        }.items())),
        'by_governance_mode': dict(sorted({
            rec['governance_mode']: sum(1 for row in recommendations if row.get('governance_mode') == rec['governance_mode'])
            for rec in recommendations
        }.items())),
        'blocking': sum(1 for item in recommendations if item.get('blocking')),
        'top_actions': {
            action_type: sum(1 for rec in recommendations for action in (rec.get('actions') or []) if action['type'] == action_type)
            for action_type in action_types
        },
        'top_priority_items': [
            {
                'regime': item.get('regime'),
                'strategy': item.get('strategy'),
                'type': item.get('type'),
                'priority': item.get('priority'),
                'summary_line': item.get('summary_line'),
            }
            for item in recommendations[:5]
        ],
    }


def _build_strategy_governance_delivery(strategy_recommendations: List[Dict]) -> Dict:
    items = []
    for rec in strategy_recommendations:
        item = {
            'bucket_id': _delivery_strategy_bucket_id(rec.get('regime'), rec.get('strategy')),
            'scope': 'strategy',
            'regime': rec.get('regime'),
            'strategy': rec.get('strategy'),
            'recommendation': {
                'type': rec.get('type'),
                'category': rec.get('category'),
                'priority': rec.get('priority'),
                'confidence': rec.get('confidence'),
                'governance_mode': rec.get('governance_mode'),
                'blocking_issue': rec.get('blocking_issue'),
                'suggested_action': rec.get('suggested_action'),
                'summary_line': rec.get('summary_line'),
                'reason': rec.get('reason'),
                'actions': rec.get('actions') or [],
                'rollout_plan': rec.get('rollout_plan') or {},
                'guardrails': rec.get('guardrails') or [],
                'thresholds': rec.get('thresholds') or {},
                'next_review_after_trade_count': rec.get('next_review_after_trade_count'),
            },
            'fit': {
                'status': rec.get('fit_status'),
                **(rec.get('evidence') or {}),
            },
            'status': {
                'blocking': bool(rec.get('blocking')),
                'priority_rank': rec.get('priority_rank', _priority_rank(rec.get('priority'))),
                'confidence_rank': rec.get('confidence_rank', _confidence_rank(rec.get('confidence'))),
            },
        }
        item['orchestration'] = _build_orchestration_plan(item)
        item['status']['blocking'] = bool(item['orchestration']['blockers'])
        items.append(item)

    items.sort(key=lambda item: (
        item['status']['priority_rank'],
        item['status']['confidence_rank'],
        -(item['fit'].get('trade_count') or 0),
        item.get('regime') or '',
        item.get('strategy') or '',
    ))

    priority_queue = [
        {
            'bucket_id': item['bucket_id'],
            'scope': 'strategy',
            'regime': item['regime'],
            'strategy': item['strategy'],
            'recommendation_type': item['recommendation']['type'],
            'governance_mode': item['recommendation']['governance_mode'],
            'priority': item['recommendation']['priority'],
            'confidence': item['recommendation']['confidence'],
            'blocking': item['status']['blocking'],
            'primary_action': (item['recommendation']['actions'] or [{}])[0].get('type'),
            'summary_line': item['recommendation']['summary_line'],
            'next_actions': item['orchestration']['next_actions'],
            'blocking_chain': item['orchestration']['blocking_chain'],
            'review_checkpoints': item['orchestration']['review_checkpoints'],
        }
        for item in items
    ]

    return {
        'items': items,
        'priority_queue': priority_queue,
        'summary': _build_strategy_governance_summary(strategy_recommendations),
        'blocking': [row for row in priority_queue if row.get('blocking')],
        'next_actions': [
            {
                'bucket_id': item['bucket_id'],
                'regime': item['regime'],
                'strategy': item['strategy'],
                'next_actions': item['orchestration']['next_actions'],
                'action_queue': item['orchestration']['action_queue'],
                'blocking_chain': item['orchestration']['blocking_chain'],
            }
            for item in items
            if item['orchestration']['next_actions']
        ],
    }


def _coerce_calibration_report_source(source: Dict) -> Dict:
    if not isinstance(source, dict):
        return {}
    if isinstance(source.get('calibration_report'), dict):
        return source['calibration_report']
    return source


def build_joint_governance_ready_payload(source: Dict) -> Dict:
    report = _coerce_calibration_report_source(source)
    delivery = report.get('delivery') or {}
    summary = report.get('summary') or {}
    orchestration_ready = delivery.get('orchestration_ready') or {}
    render_ready = delivery.get('render_ready') or {}
    tables = (delivery.get('views') or {}).get('tables') or {}
    joint_governance = report.get('joint_governance') or {}

    items = joint_governance.get('items') or tables.get('joint_governance') or []
    priority_queue = orchestration_ready.get('joint_priority_queue') or joint_governance.get('priority_queue') or []
    next_actions = orchestration_ready.get('joint_next_actions') or joint_governance.get('next_actions') or []
    blocking_items = (render_ready.get('sections') or {}).get('joint_blocking_items') or joint_governance.get('blocking') or []
    bucket_index = {
        item.get('bucket_id'): item
        for item in items
        if item.get('bucket_id')
    }

    return {
        'schema_version': 'm5_joint_governance_ready_v1',
        'delivery_schema_version': delivery.get('schema_version'),
        'summary': {
            'trade_count': int(summary.get('trade_count') or 0),
            'joint_governance_summary': summary.get('joint_governance_summary') or {},
            'delivery_ready': summary.get('delivery_ready') or {},
            'item_count': len(items),
            'priority_queue_size': len(priority_queue),
            'next_action_bucket_count': len(next_actions),
            'blocking_item_count': len(blocking_items),
        },
        'items': items,
        'priority_queue': priority_queue,
        'next_actions': next_actions,
        'blocking_items': blocking_items,
        'bucket_index': bucket_index,
        'tables': {
            'joint_governance': items,
            'joint_priority_queue': priority_queue,
            'joint_next_actions': next_actions,
            'joint_blocking_items': blocking_items,
        },
    }


def build_calibration_report_ready_payload(source: Dict) -> Dict:
    report = _coerce_calibration_report_source(source)
    delivery = report.get('delivery') or {}
    summary = report.get('summary') or {}
    views = delivery.get('views') or {}
    governance_ready = build_joint_governance_ready_payload(report)
    return {
        'schema_version': 'm5_report_ready_v1',
        'delivery_schema_version': delivery.get('schema_version'),
        'summary': summary,
        'delivery_ready': summary.get('delivery_ready') or {},
        'views': {
            'items': views.get('items') or [],
        },
        'render_ready': delivery.get('render_ready') or {},
        'orchestration_ready': delivery.get('orchestration_ready') or {},
        'governance_ready': governance_ready,
        'joint_governance': governance_ready.get('items') or [],
        'priority_queue': governance_ready.get('priority_queue') or [],
        'next_actions': governance_ready.get('next_actions') or [],
        'blocking_items': governance_ready.get('blocking_items') or [],
        'bucket_index': governance_ready.get('bucket_index') or {},
        'tables': {
            **(views.get('tables') or {}),
            'governance_ready': governance_ready,
        },
    }


def export_calibration_payload(source: Dict, *, view: str = 'report_ready') -> Dict:
    report = _coerce_calibration_report_source(source)
    if view == 'delivery':
        return report.get('delivery') or {}
    if view == 'governance_ready':
        return build_joint_governance_ready_payload(report)
    if view == 'report_ready':
        return build_calibration_report_ready_payload(report)
    return report


def build_regime_policy_calibration_report(symbol_results: List[Dict]) -> Dict:
    all_trades = [
        trade
        for row in symbol_results
        for trade in (row.get('all_trades') or row.get('recent_trades') or [])
    ]
    by_regime = summarize_trade_buckets(all_trades, 'regime_tag')
    by_policy = summarize_trade_buckets(all_trades, 'policy_tag')
    by_regime_policy = summarize_trade_buckets(all_trades, 'regime_tag', 'policy_tag')

    min_sample_size = 3
    strategy_fit = build_strategy_fit_summary(all_trades, min_sample_size=min_sample_size)
    strategy_regime_lookup = _build_strategy_regime_lookup(strategy_fit['by_regime_strategy'])
    regime_fit_lookup = _build_regime_strategy_fit_lookup(strategy_fit['regime_strategy_fit'])
    strategy_recommendations = [
        _build_strategy_governance_recommendation(
            row,
            regime_fit_lookup.get(_normalize_bucket_tag(row.get('secondary_bucket'))),
            min_sample_size=min_sample_size,
        )
        for row in strategy_fit['by_regime_strategy']
    ]
    strategy_recommendations.sort(
        key=lambda item: (
            item.get('priority_rank', _priority_rank(item.get('priority'))),
            item.get('confidence_rank', _confidence_rank(item.get('confidence'))),
            -((item.get('evidence') or {}).get('trade_count') or 0),
            item.get('regime') or '',
            item.get('strategy') or '',
        )
    )
    strategy_governance = _build_strategy_governance_delivery(strategy_recommendations)
    strategy_policy_lookup = _strategy_policy_preference_lookup(strategy_fit['strategy_policy_fit'])
    rollout_gates = []
    bucket_lookup = {}
    for row in by_regime_policy:
        bucket = row['bucket']
        policy = row.get('secondary_bucket') or 'unknown'
        gate = {
            'regime': bucket,
            'policy_version': policy,
            **_evaluate_rollout_gate(row, min_sample_size=min_sample_size),
        }
        rollout_gates.append(gate)
        bucket_lookup[(bucket, policy)] = row

    policy_ab_diffs = _build_policy_ab_diffs(by_policy, by_regime_policy, min_sample_size=min_sample_size)
    ab_lookup = _build_policy_ab_regime_lookup(policy_ab_diffs)
    recommendations = []
    for gate in rollout_gates:
        bucket_row = bucket_lookup[(gate['regime'], gate['policy_version'])]
        ab_row = ab_lookup.get((gate['regime'], gate['policy_version']))
        recommendations.append(
            _build_calibration_recommendation(gate, bucket_row, ab_row, min_sample_size=min_sample_size)
        )

    regime_policy_strategy_rows = _build_regime_policy_strategy_rows(all_trades)
    strategy_rec_lookup = {
        (_normalize_bucket_tag(item.get('regime')), _normalize_bucket_tag(item.get('strategy'))): item
        for item in strategy_recommendations
    }
    policy_rec_lookup = {
        (_normalize_bucket_tag(item.get('regime')), _normalize_bucket_tag(item.get('policy_version'))): item
        for item in recommendations
    }
    joint_governance_items = []
    for row in regime_policy_strategy_rows:
        policy_rec = policy_rec_lookup.get((_normalize_bucket_tag(row.get('regime')), _normalize_bucket_tag(row.get('policy_version'))))
        strategy_rec = strategy_rec_lookup.get((_normalize_bucket_tag(row.get('regime')), _normalize_bucket_tag(row.get('strategy'))))
        if not policy_rec or not strategy_rec:
            continue
        joint_governance_items.append(
            _build_joint_governance_item(
                row,
                policy_rec,
                strategy_rec,
                strategy_policy_lookup.get(_normalize_bucket_tag(row.get('strategy'))),
                min_sample_size=min_sample_size,
            )
        )
    joint_governance = _build_joint_governance_delivery(joint_governance_items)

    recommendations.sort(
        key=lambda item: (
            {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}.get(item['priority'], 9),
            {'high': 0, 'medium': 1, 'low': 2}.get(item['confidence'], 9),
            -(item.get('evidence') or {}).get('trade_count', 0),
            item['regime'],
            item['policy_version'],
        )
    )
    top_regime = by_regime[0]['bucket'] if by_regime else 'unknown'
    top_policy = by_policy[0]['bucket'] if by_policy else 'unknown'
    rollout_gate_summary = {
        'expand': sum(1 for item in rollout_gates if item['decision'] == 'expand'),
        'hold': sum(1 for item in rollout_gates if item['decision'] == 'hold'),
        'tighten': sum(1 for item in rollout_gates if item['decision'] == 'tighten'),
        'rollback': sum(1 for item in rollout_gates if item['decision'] == 'rollback'),
    }
    recommendation_summary = {
        'critical': sum(1 for item in recommendations if item['priority'] == 'critical'),
        'high': sum(1 for item in recommendations if item['priority'] == 'high'),
        'medium': sum(1 for item in recommendations if item['priority'] == 'medium'),
        'low': sum(1 for item in recommendations if item['priority'] == 'low'),
        'by_type': dict(sorted({
            item['type']: sum(1 for row in recommendations if row['type'] == item['type'])
            for item in recommendations
        }.items())),
        'by_governance_mode': dict(sorted({
            item['governance_mode']: sum(1 for row in recommendations if row['governance_mode'] == item['governance_mode'])
            for item in recommendations
        }.items())),
        'blocked': sum(1 for item in recommendations if item.get('blocking_issue')),
        'aligned_with_rollout_gate': sum(1 for item in recommendations if item.get('aligned_with_rollout_gate')),
        'top_actions': dict(sorted({
            action_type: sum(1 for row in recommendations for action in (row.get('actions') or []) if action['type'] == action_type)
            for action_type in {
                action['type']
                for recommendation in recommendations
                for action in (recommendation.get('actions') or [])
            }
        }.items())), 
        'top_priority_items': [
            {
                'regime': item['regime'],
                'policy_version': item['policy_version'],
                'type': item['type'],
                'priority': item['priority'],
                'summary_line': item['summary_line'],
            }
            for item in recommendations[:5]
        ],
    }
    summary = {
        'trade_count': len(all_trades),
        'regimes': len(by_regime),
        'policy_versions': len(by_policy),
        'strategies': len(strategy_fit['by_strategy']),
        'top_regime': top_regime,
        'top_policy_version': top_policy,
        'top_strategy': strategy_fit['by_strategy'][0]['bucket'] if strategy_fit['by_strategy'] else 'unknown',
        'calibration_ready': bool(all_trades),
        'min_sample_size': min_sample_size,
        'policy_ab_ready': len(by_policy) >= 2,
        'strategy_fit_ready': bool(strategy_fit['by_strategy']),
        'rollout_gate_summary': rollout_gate_summary,
        'recommendation_summary': recommendation_summary,
        'strategy_governance_summary': strategy_governance['summary'],
        'joint_governance_summary': joint_governance['summary'],
    }
    delivery = _build_calibration_delivery_payload(
        summary=summary,
        by_regime=by_regime,
        by_policy=by_policy,
        by_regime_policy=by_regime_policy,
        strategy_fit=strategy_fit,
        policy_ab_diffs=policy_ab_diffs,
        rollout_gates=rollout_gates,
        recommendations=recommendations[:50],
        strategy_governance=strategy_governance,
        joint_governance=joint_governance,
    )
    summary['delivery_ready'] = {
        'schema_version': delivery['schema_version'],
        'bucket_count': delivery['summary']['bucket_count'],
        'blocking_items': len(delivery['orchestration_ready']['queues']['blocking']),
        'priority_queue_size': len(delivery['orchestration_ready']['queue']),
        'next_action_bucket_count': len(delivery['orchestration_ready']['next_actions']),
        'blocking_chain_count': len(delivery['orchestration_ready']['blocking_chain']),
        'rollback_candidate_count': len(delivery['orchestration_ready']['rollback_candidates']),
        'strategy_priority_queue_size': len(delivery['orchestration_ready']['strategy_priority_queue']),
        'strategy_next_action_bucket_count': len(delivery['orchestration_ready']['strategy_next_actions']),
        'joint_priority_queue_size': len(delivery['orchestration_ready']['joint_priority_queue']),
        'joint_next_action_bucket_count': len(delivery['orchestration_ready']['joint_next_actions']),
    }
    delivery['governance_ready'] = build_joint_governance_ready_payload({
        'summary': summary,
        'delivery': delivery,
        'joint_governance': joint_governance,
    })
    summary['governance_ready'] = {
        'schema_version': delivery['governance_ready']['schema_version'],
        'delivery_schema_version': delivery['governance_ready']['delivery_schema_version'],
        **(delivery['governance_ready'].get('summary') or {}),
    }
    return {
        'summary': summary,
        'by_regime': by_regime,
        'by_policy_version': by_policy,
        'by_regime_policy': by_regime_policy,
        'by_strategy': strategy_fit['by_strategy'],
        'by_regime_strategy': strategy_fit['by_regime_strategy'],
        'by_policy_strategy': strategy_fit['by_policy_strategy'],
        'strategy_fit': {
            'regime_strategy_fit': strategy_fit['regime_strategy_fit'],
            'strategy_policy_fit': strategy_fit['strategy_policy_fit'],
            'strategy_recommendations': strategy_recommendations[:50],
            'strategy_governance': strategy_governance,
        },
        'joint_governance': joint_governance,
        'policy_ab_diffs': policy_ab_diffs,
        'rollout_gates': rollout_gates,
        'recommendations': recommendations[:50],
        'delivery': delivery,
    }


@dataclass
class BacktestPosition:
    side: str
    entry_price: float
    entry_time: str
    highest_price: float
    lowest_price: float
    signal_strength: int
    regime_snapshot: Optional[Dict] = None
    adaptive_policy_snapshot: Optional[Dict] = None
    reasons: Optional[List[Dict]] = None
    strategies_triggered: Optional[List[str]] = None


class MarketDataLoader:
    def __init__(self, data_dir: str = 'ml/data'):
        self.data_dir = Path(data_dir)

    def symbol_to_filename(self, symbol: str) -> str:
        mapping = {
            'BTC/USDT': 'BTC_USDT',
            'ETH/USDT': 'ETH_USDT',
            'SOL/USDT': 'SOL_USDT',
            'XRP/USDT': 'XRP_USDT',
            'HYPE/USDT': 'HYPE_USDT',
        }
        return mapping.get(symbol, symbol.replace('/', '_').replace(':', '_'))

    def load_symbol(self, symbol: str, timeframe: str = '1h') -> Optional[pd.DataFrame]:
        path = self.data_dir / f'{self.symbol_to_filename(symbol)}_{timeframe}.csv'
        if not path.exists():
            return None
        df = pd.read_csv(path)
        if 'datetime' in df.columns:
            df['datetime'] = pd.to_datetime(df['datetime'])
        elif 'timestamp' in df.columns:
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df


class StrategyBacktester:
    def __init__(self, config: Config):
        self.config = config
        self.detector = SignalDetector(config.all)
        self.validator = SignalValidator(config, None)
        self.loader = MarketDataLoader()
        self._cache = None
        self._cache_at = None

    def run_all(self, symbols: Optional[List[str]] = None, timeframe: str = '1h', use_cache: bool = True) -> Dict:
        now = datetime.now()
        if use_cache and self._cache is not None and self._cache_at and (now - self._cache_at).total_seconds() < 300:
            return self._cache

        symbols = symbols or self.config.symbols
        results = []
        for symbol in symbols:
            df = self.loader.load_symbol(symbol, timeframe)
            if df is None or len(df) < 150:
                continue
            results.append(self._run_symbol(symbol, df))

        summary = self._aggregate_results(results)
        self._cache = summary
        self._cache_at = now
        return summary

    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        close = out['close']
        delta = close.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(14).mean()
        avg_loss = loss.rolling(14).mean()
        rs = avg_gain / (avg_loss + 1e-10)
        out['RSI'] = 100 - (100 / (1 + rs))

        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        out['MACD'] = ema12 - ema26
        out['MACD_signal'] = out['MACD'].ewm(span=9).mean()

        out['BB_mid'] = close.rolling(20).mean()
        std = close.rolling(20).std()
        out['BB_upper'] = out['BB_mid'] + 2 * std
        out['BB_lower'] = out['BB_mid'] - 2 * std
        return out

    def _to_detector_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        frame = pd.DataFrame({
            0: df['timestamp'].values if 'timestamp' in df.columns else range(len(df)),
            1: df['open'].values,
            2: df['high'].values,
            3: df['low'].values,
            4: df['close'].values,
            5: df['volume'].values,
            'RSI': df['RSI'].values,
            'MACD': df['MACD'].values,
            'MACD_signal': df['MACD_signal'].values,
            'BB_mid': df['BB_mid'].values,
            'BB_upper': df['BB_upper'].values,
            'BB_lower': df['BB_lower'].values,
        })
        return frame

    def _run_symbol(self, symbol: str, raw_df: pd.DataFrame) -> Dict:
        df = self._add_indicators(raw_df)
        trades = []
        position: Optional[BacktestPosition] = None
        warmup = max(80, 5 + 20 + 60)
        stop_loss = float(self.config.get('trading', {}).get('stop_loss', 0.02))
        take_profit = float(self.config.get('trading', {}).get('take_profit', 0.04))
        trailing_stop = float(self.config.get('trading', {}).get('trailing_stop', 0.015))

        for i in range(warmup, len(df) - 1):
            window = df.iloc[: i + 1].copy()
            detector_df = self._to_detector_frame(window)
            current_row = window.iloc[-1]
            current_price = float(current_row['close'])
            timestamp = str(current_row['datetime'])
            signal = self.detector.analyze(symbol, detector_df, current_price, None)

            current_positions = {}
            if position:
                current_positions[symbol] = {
                    'symbol': symbol,
                    'side': position.side,
                    'entry_price': position.entry_price,
                    'current_price': current_price,
                    'quantity': 1.0,
                    'leverage': self.config.get('trading', {}).get('leverage', 3),
                }

            passed, _, _ = self.validator.validate(signal, current_positions=current_positions, tracking_data={})

            if position:
                if position.side == 'long':
                    position.highest_price = max(position.highest_price, current_price)
                    pnl = (current_price - position.entry_price) / position.entry_price
                    trailing_hit = current_price <= position.highest_price * (1 - trailing_stop)
                    opposite_hit = signal.signal_type == 'sell' and signal.strength >= max(25, position.signal_strength * 0.8)
                else:
                    position.lowest_price = min(position.lowest_price, current_price)
                    pnl = (position.entry_price - current_price) / position.entry_price
                    trailing_hit = current_price >= position.lowest_price * (1 + trailing_stop)
                    opposite_hit = signal.signal_type == 'buy' and signal.strength >= max(25, position.signal_strength * 0.8)

                exit_reason = None
                if pnl <= -stop_loss:
                    exit_reason = 'stop_loss'
                elif pnl >= take_profit:
                    exit_reason = 'take_profit'
                elif trailing_hit and pnl > 0:
                    exit_reason = 'trailing_stop'
                elif opposite_hit:
                    exit_reason = 'opposite_signal'

                if exit_reason:
                    trades.append({
                        'symbol': symbol,
                        'side': position.side,
                        'entry_time': position.entry_time,
                        'exit_time': timestamp,
                        'entry_price': position.entry_price,
                        'exit_price': current_price,
                        'return_pct': round(pnl * 100, 4),
                        'reason': exit_reason,
                        'regime_tag': ((position.regime_snapshot or {}).get('name') if position.regime_snapshot else None),
                        'policy_tag': ((position.adaptive_policy_snapshot or {}).get('policy_version') if position.adaptive_policy_snapshot else None),
                        'strategy_tags': _normalize_strategy_tags(position.strategies_triggered),
                        'dominant_strategy': (_normalize_strategy_tags(position.strategies_triggered) or ['unknown'])[0],
                        'strategy_count': len(_normalize_strategy_tags(position.strategies_triggered)),
                        'strategy_reasons': list(position.reasons or []),
                        'observe_only': normalize_observe_only_view(
                            regime_snapshot=position.regime_snapshot or {},
                            policy_snapshot=position.adaptive_policy_snapshot or {},
                            fallback_summary=(position.adaptive_policy_snapshot or {}).get('summary') or (position.regime_snapshot or {}).get('details'),
                        ),
                    })
                    position = None
                    continue

            if not position and passed and signal.signal_type in ['buy', 'sell']:
                side = 'long' if signal.signal_type == 'buy' else 'short'
                position = BacktestPosition(
                    side=side,
                    entry_price=current_price,
                    entry_time=timestamp,
                    highest_price=current_price,
                    lowest_price=current_price,
                    signal_strength=signal.strength,
                    regime_snapshot=dict(getattr(signal, 'regime_snapshot', {}) or getattr(signal, 'regime_info', {}) or {}),
                    adaptive_policy_snapshot=dict(getattr(signal, 'adaptive_policy_snapshot', {}) or {}),
                    reasons=list(getattr(signal, 'reasons', []) or []),
                    strategies_triggered=list(getattr(signal, 'strategies_triggered', []) or []),
                )

        if position:
            last_row = df.iloc[-1]
            last_price = float(last_row['close'])
            pnl = ((last_price - position.entry_price) / position.entry_price) if position.side == 'long' else ((position.entry_price - last_price) / position.entry_price)
            trades.append({
                'symbol': symbol,
                'side': position.side,
                'entry_time': position.entry_time,
                'exit_time': str(last_row['datetime']),
                'entry_price': position.entry_price,
                'exit_price': last_price,
                'return_pct': round(pnl * 100, 4),
                'reason': 'end_of_backtest',
                'regime_tag': ((position.regime_snapshot or {}).get('name') if position.regime_snapshot else None),
                'policy_tag': ((position.adaptive_policy_snapshot or {}).get('policy_version') if position.adaptive_policy_snapshot else None),
                'strategy_tags': _normalize_strategy_tags(position.strategies_triggered),
                'dominant_strategy': (_normalize_strategy_tags(position.strategies_triggered) or ['unknown'])[0],
                'strategy_count': len(_normalize_strategy_tags(position.strategies_triggered)),
                'strategy_reasons': list(position.reasons or []),
                'observe_only': normalize_observe_only_view(
                    regime_snapshot=position.regime_snapshot or {},
                    policy_snapshot=position.adaptive_policy_snapshot or {},
                    fallback_summary=(position.adaptive_policy_snapshot or {}).get('summary') or (position.regime_snapshot or {}).get('details'),
                ),
            })

        total_return = sum(t['return_pct'] for t in trades)
        wins = sum(1 for t in trades if t['return_pct'] > 0)
        losses = sum(1 for t in trades if t['return_pct'] < 0)
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for t in trades:
            equity += t['return_pct']
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity - peak)

        regime_tags = sorted({t.get('regime_tag') for t in trades if t.get('regime_tag')})
        policy_tags = sorted({t.get('policy_tag') for t in trades if t.get('policy_tag')})
        strategy_tags = sorted({tag for t in trades for tag in (t.get('strategy_tags') or []) if tag})
        observe_only_tags = sorted({tag for t in trades for tag in ((t.get('observe_only') or {}).get('tags') or []) if tag})
        strategy_fit = build_strategy_fit_summary(trades, min_sample_size=3)
        return {
            'symbol': symbol,
            'trades': len(trades),
            'wins': wins,
            'losses': losses,
            'win_rate': round((wins / len(trades) * 100), 2) if trades else 0.0,
            'total_return_pct': round(total_return, 4),
            'avg_return_pct': round((total_return / len(trades)), 4) if trades else 0.0,
            'max_drawdown_pct': round(max_drawdown, 4),
            'all_trades': trades,
            'recent_trades': trades[-10:],
            'observe_only_summary_view': summarize_observe_only_collection(trades[-10:]),
            'regime_tags': regime_tags,
            'policy_tags': policy_tags,
            'strategy_tags': strategy_tags,
            'observe_only_tags': observe_only_tags,
            'regime_policy_calibration': {
                'by_regime': summarize_trade_buckets(trades, 'regime_tag'),
                'by_policy_version': summarize_trade_buckets(trades, 'policy_tag'),
                'by_regime_policy': summarize_trade_buckets(trades, 'regime_tag', 'policy_tag'),
                'by_strategy': strategy_fit['by_strategy'],
                'by_regime_strategy': strategy_fit['by_regime_strategy'],
                'by_policy_strategy': strategy_fit['by_policy_strategy'],
                'regime_strategy_fit': strategy_fit['regime_strategy_fit'],
                'strategy_policy_fit': strategy_fit['strategy_policy_fit'],
            },
        }

    def _aggregate_results(self, symbol_results: List[Dict]) -> Dict:
        total_trades = sum(x['trades'] for x in symbol_results)
        total_wins = sum(x['wins'] for x in symbol_results)
        total_return = sum(x['total_return_pct'] for x in symbol_results)
        max_drawdown = min([x['max_drawdown_pct'] for x in symbol_results], default=0.0)
        observe_only_tags = sorted({tag for row in symbol_results for tag in (row.get('observe_only_tags') or []) if tag})
        regime_tags = sorted({tag for row in symbol_results for tag in (row.get('regime_tags') or []) if tag})
        policy_tags = sorted({tag for row in symbol_results for tag in (row.get('policy_tags') or []) if tag})
        strategy_tags = sorted({tag for row in symbol_results for tag in (row.get('strategy_tags') or []) if tag})
        observe_only_summary_view = summarize_observe_only_collection([
            trade
            for row in symbol_results
            for trade in (row.get('recent_trades') or [])
            if trade.get('observe_only')
        ])
        calibration_report = build_regime_policy_calibration_report(symbol_results)
        return {
            'summary': {
                'symbols': len(symbol_results),
                'total_trades': total_trades,
                'win_rate': round((total_wins / total_trades * 100), 2) if total_trades else 0.0,
                'total_return_pct': round(total_return, 4),
                'max_drawdown_pct': round(max_drawdown, 4),
                'observe_only': True,
                'observe_only_tags': observe_only_tags,
                'observe_only_banner': observe_only_summary_view.get('banner'),
                'observe_only_summary_view': observe_only_summary_view,
                'regime_tags': regime_tags,
                'policy_tags': policy_tags,
                'strategy_tags': strategy_tags,
                'calibration_ready': calibration_report['summary']['calibration_ready'],
            },
            'symbols': symbol_results,
            'calibration_report': calibration_report,
        }


class SignalQualityAnalyzer:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.loader = MarketDataLoader()
        self.detector = SignalDetector(config.all)
        self._cache = None
        self._cache_at = None

    def analyze(self, limit: int = 200, use_cache: bool = True, symbols: Optional[List[str]] = None) -> Dict:
        now = datetime.now()
        cache_allowed = use_cache and symbols is None
        if cache_allowed and self._cache is not None and self._cache_at and (now - self._cache_at).total_seconds() < 300:
            return self._cache

        target_symbols = list(dict.fromkeys(symbols or []))
        signals = self.db.get_signals(limit=limit)
        by_symbol = {}
        rows = []
        for signal in signals:
            symbol = signal.get('symbol')
            if target_symbols and symbol not in target_symbols:
                continue
            if symbol not in by_symbol:
                df = self.loader.load_symbol(symbol)
                by_symbol[symbol] = df
            df = by_symbol[symbol]
            if df is None or df.empty:
                continue
            row = self._score_signal(signal, df)
            if row:
                rows.append(row)

        valid_symbols = {r.get('symbol') for r in rows if r.get('avg_quality_pct') is not None}
        missing_symbols = [s for s in target_symbols if s not in valid_symbols]
        if (not rows or not any(r.get('avg_quality_pct') is not None for r in rows)):
            rows = self._analyze_historical_generated_signals(symbols=target_symbols or None)
        elif missing_symbols:
            rows.extend(self._analyze_historical_generated_signals(symbols=missing_symbols))

        summary = self._summarize(rows)
        if cache_allowed:
            self._cache = summary
            self._cache_at = now
        return summary

    def _score_signal(self, signal: Dict, df: pd.DataFrame) -> Optional[Dict]:
        created_at = signal.get('created_at')
        if not created_at:
            return None
        try:
            created_ts = pd.to_datetime(created_at)
        except Exception:
            return None

        timeline = df[['datetime', 'close']].copy()
        timeline['delta'] = (timeline['datetime'] - created_ts).abs()
        nearest = timeline.sort_values('delta').iloc[0]
        if pd.isna(nearest['delta']) or nearest['delta'] > pd.Timedelta(hours=2):
            return None
        idx = timeline.sort_values('delta').index[0]
        base_price = float(nearest['close'])
        direction = signal.get('signal_type')
        if direction not in ['buy', 'sell']:
            return None

        def calc_horizon_ret(steps: int) -> Optional[float]:
            target_idx = idx + steps
            if target_idx >= len(df):
                return None
            future_price = float(df.iloc[target_idx]['close'])
            ret = (future_price - base_price) / base_price
            if direction == 'sell':
                ret = -ret
            return round(ret * 100, 4)

        r1 = calc_horizon_ret(1)
        r3 = calc_horizon_ret(3)
        r6 = calc_horizon_ret(6)
        quality_score = [x for x in [r1, r3, r6] if x is not None]
        avg = round(sum(quality_score) / len(quality_score), 4) if quality_score else None
        return {
            'symbol': signal.get('symbol'),
            'created_at': created_at,
            'signal_type': direction,
            'strength': signal.get('strength', 0),
            'filtered': signal.get('filtered', False),
            'filter_reason': signal.get('filter_reason'),
            'return_1h_pct': r1,
            'return_3h_pct': r3,
            'return_6h_pct': r6,
            'avg_quality_pct': avg,
        }

    def _analyze_historical_generated_signals(self, symbols: Optional[List[str]] = None) -> List[Dict]:
        rows = []
        symbols = symbols or self.config.symbols
        for symbol in symbols:
            df = self.loader.load_symbol(symbol)
            if df is None or len(df) < 150:
                continue
            df = df.copy()
            close = df['close']
            delta = close.diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rs = avg_gain / (avg_loss + 1e-10)
            df['RSI'] = 100 - (100 / (1 + rs))
            ema12 = close.ewm(span=12).mean()
            ema26 = close.ewm(span=26).mean()
            df['MACD'] = ema12 - ema26
            df['MACD_signal'] = df['MACD'].ewm(span=9).mean()
            df['BB_mid'] = close.rolling(20).mean()
            std = close.rolling(20).std()
            df['BB_upper'] = df['BB_mid'] + 2 * std
            df['BB_lower'] = df['BB_mid'] - 2 * std

            for i in range(80, len(df) - 6):
                window = df.iloc[: i + 1]
                detector_df = pd.DataFrame({
                    0: window['timestamp'].values if 'timestamp' in window.columns else range(len(window)),
                    1: window['open'].values,
                    2: window['high'].values,
                    3: window['low'].values,
                    4: window['close'].values,
                    5: window['volume'].values,
                    'RSI': window['RSI'].values,
                    'MACD': window['MACD'].values,
                    'MACD_signal': window['MACD_signal'].values,
                    'BB_mid': window['BB_mid'].values,
                    'BB_upper': window['BB_upper'].values,
                    'BB_lower': window['BB_lower'].values,
                })
                current_price = float(window.iloc[-1]['close'])
                signal = self.detector.analyze(symbol, detector_df, current_price, None)
                if signal.signal_type not in ['buy', 'sell']:
                    continue
                mock_signal = {
                    'symbol': symbol,
                    'created_at': str(window.iloc[-1]['datetime']),
                    'signal_type': signal.signal_type,
                    'strength': signal.strength,
                    'filtered': False,
                    'filter_reason': None,
                }
                scored = self._score_signal(mock_signal, df)
                if scored:
                    rows.append(scored)
        return rows

    def _summarize(self, rows: List[Dict]) -> Dict:
        valid = [r for r in rows if r.get('avg_quality_pct') is not None]
        positive = sum(1 for r in valid if r['avg_quality_pct'] > 0)
        by_symbol = {}
        for r in valid:
            sym = r['symbol']
            by_symbol.setdefault(sym, []).append(r['avg_quality_pct'])
        symbol_stats = []
        for sym, vals in by_symbol.items():
            symbol_stats.append({
                'symbol': sym,
                'signals': len(vals),
                'avg_quality_pct': round(sum(vals) / len(vals), 4),
                'positive_rate': round(sum(1 for v in vals if v > 0) / len(vals) * 100, 2),
            })
        symbol_stats.sort(key=lambda x: x['avg_quality_pct'], reverse=True)
        valid.sort(key=lambda x: x['created_at'], reverse=True)
        observe_only_summary_view = summarize_observe_only_collection([
            trade
            for row in symbol_results
            for trade in (row.get('recent_trades') or [])
            if trade.get('observe_only')
        ])
        return {
            'summary': {
                'signals_scored': len(valid),
                'positive_rate': round((positive / len(valid) * 100), 2) if valid else 0.0,
                'avg_quality_pct': round(sum(r['avg_quality_pct'] for r in valid) / len(valid), 4) if valid else 0.0,
            },
            'by_symbol': symbol_stats,
            'recent': valid[:50],
        }
