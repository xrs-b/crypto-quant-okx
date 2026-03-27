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


def _delivery_bucket_id(regime: str, policy_version: str) -> str:
    return f'{_normalize_bucket_tag(regime)}::{_normalize_bucket_tag(policy_version)}'




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
    policy_ab_diffs: List[Dict],
    rollout_gates: List[Dict],
    recommendations: List[Dict],
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
    return {
        'schema_version': 'm5_delivery_v1',
        'summary': {
            'trade_count': summary.get('trade_count', 0),
            'min_sample_size': summary.get('min_sample_size', 0),
            'calibration_ready': bool(summary.get('calibration_ready')),
            'policy_ab_ready': bool(summary.get('policy_ab_ready')),
            'bucket_count': len(items),
            'regime_count': len(by_regime or []),
            'policy_count': len(by_policy or []),
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
                'policy_ab_diffs': policy_ab_diffs,
                'rollout_gates': rollout_gates,
                'recommendations': recommendations[:50],
            },
        },
        'render_ready': {
            'headline': {
                'top_regime': summary.get('top_regime'),
                'top_policy_version': summary.get('top_policy_version'),
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
        },
    }


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


def _coerce_calibration_report_source(source: Dict) -> Dict:
    if not isinstance(source, dict):
        return {}
    if isinstance(source.get('calibration_report'), dict):
        return source['calibration_report']
    return source


def build_calibration_report_ready_payload(source: Dict) -> Dict:
    report = _coerce_calibration_report_source(source)
    delivery = report.get('delivery') or {}
    summary = report.get('summary') or {}
    views = delivery.get('views') or {}
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
        'tables': views.get('tables') or {},
    }


def export_calibration_payload(source: Dict, *, view: str = 'report_ready') -> Dict:
    report = _coerce_calibration_report_source(source)
    if view == 'delivery':
        return report.get('delivery') or {}
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
        'top_regime': top_regime,
        'top_policy_version': top_policy,
        'calibration_ready': bool(all_trades),
        'min_sample_size': min_sample_size,
        'policy_ab_ready': len(by_policy) >= 2,
        'rollout_gate_summary': rollout_gate_summary,
        'recommendation_summary': recommendation_summary,
    }
    delivery = _build_calibration_delivery_payload(
        summary=summary,
        by_regime=by_regime,
        by_policy=by_policy,
        by_regime_policy=by_regime_policy,
        policy_ab_diffs=policy_ab_diffs,
        rollout_gates=rollout_gates,
        recommendations=recommendations[:50],
    )
    summary['delivery_ready'] = {
        'schema_version': delivery['schema_version'],
        'bucket_count': delivery['summary']['bucket_count'],
        'blocking_items': len(delivery['orchestration_ready']['queues']['blocking']),
        'priority_queue_size': len(delivery['orchestration_ready']['queue']),
        'next_action_bucket_count': len(delivery['orchestration_ready']['next_actions']),
        'blocking_chain_count': len(delivery['orchestration_ready']['blocking_chain']),
        'rollback_candidate_count': len(delivery['orchestration_ready']['rollback_candidates']),
    }
    return {
        'summary': summary,
        'by_regime': by_regime,
        'by_policy_version': by_policy,
        'by_regime_policy': by_regime_policy,
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
        observe_only_tags = sorted({tag for t in trades for tag in ((t.get('observe_only') or {}).get('tags') or []) if tag})
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
            'observe_only_tags': observe_only_tags,
            'regime_policy_calibration': {
                'by_regime': summarize_trade_buckets(trades, 'regime_tag'),
                'by_policy_version': summarize_trade_buckets(trades, 'policy_tag'),
                'by_regime_policy': summarize_trade_buckets(trades, 'regime_tag', 'policy_tag'),
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
