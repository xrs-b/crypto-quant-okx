"""Approval/workflow persistence helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any


TERMINAL_APPROVAL_STATES = {'approved', 'rejected', 'deferred', 'expired'}
AUTO_APPROVAL_DECISIONS = {'auto_approve', 'manual_review', 'freeze', 'defer'}
CONTROLLED_ROLLOUT_ACTION_SPECS = {
    'joint_observe': {
        'state': 'ready',
        'workflow_state': 'ready',
        'event_type': 'controlled_rollout_state_apply',
        'result_action': 'state_applied',
        'effect': 'safe_state_transition',
    },
    'joint_queue_promote_safe': {
        'state': 'ready',
        'workflow_state': 'ready',
        'event_type': 'controlled_rollout_queue_promote',
        'result_action': 'queue_promoted',
        'effect': 'safe_queue_promotion',
    },
    'joint_stage_prepare': {
        'state': 'ready',
        'workflow_state': 'ready',
        'event_type': 'controlled_rollout_stage_prepare',
        'result_action': 'stage_prepared',
        'effect': 'safe_rollout_stage_transition',
    },
    'joint_review_schedule': {
        'state': 'pending',
        'workflow_state': 'pending',
        'event_type': 'controlled_rollout_review_schedule',
        'result_action': 'review_scheduled',
        'effect': 'safe_review_scheduling',
    },
    'joint_metadata_annotate': {
        'state': 'pending',
        'workflow_state': 'pending',
        'event_type': 'controlled_rollout_metadata_annotate',
        'result_action': 'metadata_annotated',
        'effect': 'safe_metadata_annotation',
    },
}

ROLLOUT_EXECUTOR_ACTION_SPECS = {
    'joint_observe': {
        **CONTROLLED_ROLLOUT_ACTION_SPECS['joint_observe'],
        'dispatch_mode': 'apply',
        'executor_class': 'state_transition',
        'audit_code': 'SAFE_STATE_APPLY',
        'rollback_capable': True,
    },
    'joint_queue_promote_safe': {
        **CONTROLLED_ROLLOUT_ACTION_SPECS['joint_queue_promote_safe'],
        'dispatch_mode': 'apply',
        'executor_class': 'queue_metadata',
        'audit_code': 'SAFE_QUEUE_PROMOTE',
        'rollback_capable': True,
    },
    'joint_stage_prepare': {
        **CONTROLLED_ROLLOUT_ACTION_SPECS['joint_stage_prepare'],
        'dispatch_mode': 'apply',
        'executor_class': 'stage_metadata',
        'audit_code': 'SAFE_STAGE_PREPARE',
        'rollback_capable': True,
    },
    'joint_review_schedule': {
        **CONTROLLED_ROLLOUT_ACTION_SPECS['joint_review_schedule'],
        'dispatch_mode': 'apply',
        'executor_class': 'review_metadata',
        'audit_code': 'SAFE_REVIEW_SCHEDULE',
        'rollback_capable': True,
    },
    'joint_metadata_annotate': {
        **CONTROLLED_ROLLOUT_ACTION_SPECS['joint_metadata_annotate'],
        'dispatch_mode': 'apply',
        'executor_class': 'annotation_metadata',
        'audit_code': 'SAFE_METADATA_ANNOTATE',
        'rollback_capable': True,
    },
    'joint_expand_guarded': {
        'dispatch_mode': 'queue_only',
        'executor_class': 'live_trading_change',
        'audit_code': 'QUEUE_ONLY_GUARDED_EXPAND',
        'blocked_reason': 'live_rollout_parameter_change_not_supported',
        'requires_approval': True,
        'rollback_capable': False,
    },
    'joint_freeze': {
        'dispatch_mode': 'queue_only',
        'executor_class': 'governance_control',
        'audit_code': 'QUEUE_ONLY_FREEZE',
        'blocked_reason': 'freeze_apply_not_automated_in_executor',
        'requires_approval': True,
        'rollback_capable': False,
    },
    'joint_deweight': {
        'dispatch_mode': 'queue_only',
        'executor_class': 'strategy_weight_change',
        'audit_code': 'QUEUE_ONLY_DEWEIGHT',
        'blocked_reason': 'strategy_weight_change_not_automated_in_executor',
        'requires_approval': True,
        'rollback_capable': False,
    },
    'prefer_strategy_best_policy': {
        'dispatch_mode': 'queue_only',
        'executor_class': 'policy_switch',
        'audit_code': 'QUEUE_ONLY_POLICY_SWITCH',
        'blocked_reason': 'policy_switch_not_automated_in_executor',
        'requires_approval': True,
        'rollback_capable': False,
    },
    'rollout_freeze': {
        'dispatch_mode': 'queue_only',
        'executor_class': 'rollout_control',
        'audit_code': 'QUEUE_ONLY_ROLLOUT_FREEZE',
        'blocked_reason': 'rollout_freeze_not_automated_in_executor',
        'requires_approval': True,
        'rollback_capable': False,
    },
}


def _normalize_auto_approval_decision(value: Optional[str]) -> str:
    normalized = str(value or '').strip().lower()
    return normalized if normalized in AUTO_APPROVAL_DECISIONS else 'manual_review'


def _normalize_auto_approval_confidence(value: Optional[str]) -> str:
    normalized = str(value or '').strip().lower()
    return normalized if normalized in {'high', 'medium', 'low'} else 'low'


def _dedupe_strings(values: Optional[List[Any]]) -> List[str]:
    seen = set()
    deduped: List[str] = []
    for value in values or []:
        item = str(value or '').strip()
        if not item or item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _collect_open_preconditions(preconditions: Optional[List[Dict]]) -> List[str]:
    pending_status = {'open', 'pending', 'required'}
    collected = []
    for row in preconditions or []:
        if str(row.get('status') or '').strip().lower() not in pending_status:
            continue
        collected.append(row.get('value') or row.get('type') or 'precondition_open')
    return _dedupe_strings(collected)


def _evaluate_auto_approval_policy(row: Dict, *, workflow_item: Optional[Dict] = None) -> Dict[str, Any]:
    workflow_item = workflow_item or {}
    risk_level = str(row.get('risk_level') or workflow_item.get('risk_level') or 'low').strip().lower()
    action_type = str(row.get('action_type') or workflow_item.get('action_type') or '').strip().lower()
    decision = str(row.get('decision') or workflow_item.get('decision') or '').strip().lower()
    governance_mode = str(row.get('governance_mode') or workflow_item.get('governance_mode') or '').strip().lower()
    confidence = str(row.get('confidence') or workflow_item.get('confidence') or 'low').strip().lower()
    approval_required = bool(row.get('approval_required') if row.get('approval_required') is not None else workflow_item.get('approval_required'))
    blocking_issues = _dedupe_strings(
        list(row.get('blocking_issues') or []) + list(workflow_item.get('blocking_reasons') or [])
    )
    open_preconditions = _collect_open_preconditions(row.get('preconditions') or workflow_item.get('preconditions') or [])
    blocked_by = _dedupe_strings(blocking_issues + open_preconditions)

    rule_hits: List[str] = []
    reason_parts: List[str] = []

    if decision == 'freeze' or governance_mode == 'rollback' or risk_level == 'critical':
        rule_hits.append('freeze_guardrail')
        reason_parts.append('critical risk / rollback style item must stay frozen')
        return {
            'auto_approval_decision': 'freeze',
            'reason': '; '.join(reason_parts),
            'confidence': 'high',
            'requires_manual': True,
            'auto_approval_eligible': False,
            'blocked_by': blocked_by or ['critical_risk'],
            'rule_hits': rule_hits,
        }

    if blocked_by:
        rule_hits.append('blocking_preconditions')
        reason_parts.append('blocking issues or unresolved preconditions remain open')
        return {
            'auto_approval_decision': 'defer',
            'reason': '; '.join(reason_parts),
            'confidence': 'high' if 'blocking_issue' in ' '.join(blocked_by) else 'medium',
            'requires_manual': False,
            'auto_approval_eligible': False,
            'blocked_by': blocked_by,
            'rule_hits': rule_hits,
        }

    if governance_mode in {'observe', 'review'} or decision == 'observe':
        rule_hits.append('observe_or_review_mode')
        reason_parts.append('observe/review items should stay deferred until new evidence arrives')
        return {
            'auto_approval_decision': 'defer',
            'reason': '; '.join(reason_parts),
            'confidence': 'medium' if confidence == 'high' else 'low',
            'requires_manual': False,
            'auto_approval_eligible': False,
            'blocked_by': blocked_by,
            'rule_hits': rule_hits,
        }

    if approval_required or risk_level in {'high', 'medium'} or action_type in {'joint_expand_guarded', 'joint_freeze', 'joint_deweight', 'prefer_strategy_best_policy'}:
        rule_hits.append('manual_change_control')
        reason_parts.append('change-control sensitive item still requires human approval')
        return {
            'auto_approval_decision': 'manual_review',
            'reason': '; '.join(reason_parts),
            'confidence': 'high' if risk_level == 'high' else 'medium',
            'requires_manual': True,
            'auto_approval_eligible': False,
            'blocked_by': blocked_by,
            'rule_hits': rule_hits,
        }

    rule_hits.append('low_risk_no_blockers')
    reason_parts.append('low-risk item has no blockers and can be auto-approved at decision layer')
    return {
        'auto_approval_decision': 'auto_approve',
        'reason': '; '.join(reason_parts),
        'confidence': 'high' if confidence in {'high', 'medium'} else 'medium',
        'requires_manual': False,
        'auto_approval_eligible': True,
        'blocked_by': blocked_by,
        'rule_hits': rule_hits,
    }


def attach_auto_approval_policy(payload: Dict) -> Dict:
    approval_state = (payload or {}).get('approval_state') or {}
    workflow_state = (payload or {}).get('workflow_state') or {}
    approval_items = approval_state.get('items') or []
    workflow_items = workflow_state.get('item_states') or []
    workflow_lookup = {row.get('item_id'): row for row in workflow_items if row.get('item_id')}

    for row in approval_items:
        policy = _evaluate_auto_approval_policy(row, workflow_item=workflow_lookup.get(row.get('playbook_id')) or {})
        row.update(policy)

    approval_lookup = {row.get('playbook_id'): row for row in approval_items if row.get('playbook_id')}
    for row in workflow_items:
        policy = _evaluate_auto_approval_policy(row, workflow_item=row)
        if approval_lookup.get(row.get('item_id')):
            policy = {**policy, **{k: approval_lookup[row.get('item_id')].get(k) for k in ('auto_approval_decision', 'reason', 'confidence', 'requires_manual', 'auto_approval_eligible', 'blocked_by', 'rule_hits')}}
        row.update(policy)

    approval_state['summary'] = approval_state.get('summary') or {}
    approval_state['summary']['auto_approval'] = {
        'auto_approve': sum(1 for row in approval_items if row.get('auto_approval_decision') == 'auto_approve'),
        'manual_review': sum(1 for row in approval_items if row.get('auto_approval_decision') == 'manual_review'),
        'freeze': sum(1 for row in approval_items if row.get('auto_approval_decision') == 'freeze'),
        'defer': sum(1 for row in approval_items if row.get('auto_approval_decision') == 'defer'),
    }
    workflow_state['summary'] = workflow_state.get('summary') or {}
    workflow_state['summary']['auto_approval'] = {
        'auto_approve': sum(1 for row in workflow_items if row.get('auto_approval_decision') == 'auto_approve'),
        'manual_review': sum(1 for row in workflow_items if row.get('auto_approval_decision') == 'manual_review'),
        'freeze': sum(1 for row in workflow_items if row.get('auto_approval_decision') == 'freeze'),
        'defer': sum(1 for row in workflow_items if row.get('auto_approval_decision') == 'defer'),
    }
    payload['auto_approval_policy'] = {
        'schema_version': 'm5_auto_approval_policy_v1',
        'summary': {
            'approval_item_count': len(approval_items),
            'workflow_item_count': len(workflow_items),
            'eligible_count': sum(1 for row in approval_items if row.get('auto_approval_eligible')) + sum(1 for row in workflow_items if row.get('auto_approval_eligible')),
            'manual_required_count': sum(1 for row in approval_items if row.get('requires_manual')) + sum(1 for row in workflow_items if row.get('requires_manual')),
        },
        'approval_items': approval_items,
        'workflow_items': workflow_items,
    }
    return payload


def build_workflow_approval_records(payload: Dict) -> List[Dict]:
    approval_state = (payload or {}).get('approval_state') or {}
    workflow_state = (payload or {}).get('workflow_state') or {}
    item_lookup = {
        row.get('item_id'): row
        for row in (workflow_state.get('item_states') or [])
        if row.get('item_id')
    }
    records = []
    for row in approval_state.get('items') or []:
        playbook_id = row.get('playbook_id')
        workflow_item = item_lookup.get(playbook_id) or {}
        records.append({
            'item_id': row.get('approval_id') or playbook_id,
            'approval_id': row.get('approval_id'),
            'approval_type': row.get('action_type') or workflow_item.get('action_type') or 'workflow_approval',
            'target': playbook_id,
            'title': row.get('title') or workflow_item.get('title'),
            'decision': row.get('approval_state') or 'pending',
            'state': row.get('approval_state') or 'pending',
            'workflow_state': workflow_item.get('workflow_state') or row.get('decision_state'),
            'replay_source': 'workflow_ready',
            'bucket_id': row.get('bucket_id'),
            'playbook_id': playbook_id,
            'risk_level': row.get('risk_level'),
            'owner_hint': row.get('owner_hint'),
            'approval_roles': row.get('approval_roles') or [],
            'execution_window': row.get('execution_window') or {},
            'rollout_stage': row.get('rollout_stage'),
            'target_rollout_stage': row.get('target_rollout_stage'),
            'stage_model': row.get('stage_model') or {},
            'queue_progression': row.get('queue_progression') or {},
            'scheduled_review': row.get('scheduled_review') or {},
            'preconditions': row.get('preconditions') or [],
            'rollback_plan': row.get('rollback_plan') or {},
            'auto_approval_decision': row.get('auto_approval_decision'),
            'auto_approval_reason': row.get('reason'),
            'auto_approval_confidence': row.get('confidence'),
            'requires_manual': row.get('requires_manual'),
            'auto_approval_eligible': row.get('auto_approval_eligible'),
            'blocked_by': row.get('blocked_by') or [],
            'rule_hits': row.get('rule_hits') or [],
        })
    return records


def merge_persisted_approval_state(payload: Dict, persisted_rows: Optional[List[Dict]]) -> Dict:
    persisted_lookup = {
        row.get('item_id'): row
        for row in (persisted_rows or [])
        if row.get('item_id')
    }
    approval_state = (payload or {}).get('approval_state') or {}
    workflow_state = (payload or {}).get('workflow_state') or {}

    for row in approval_state.get('items') or []:
        persisted = persisted_lookup.get(row.get('approval_id')) or persisted_lookup.get(row.get('playbook_id'))
        if not persisted:
            continue
        persisted_details = persisted.get('details') or {}
        row['persisted_state'] = persisted.get('state')
        row['persisted_decision'] = persisted.get('decision')
        row['persisted_workflow_state'] = persisted.get('workflow_state')
        row['persisted_updated_at'] = persisted.get('updated_at')
        row['approval_reason'] = persisted.get('reason') or row.get('approval_reason')
        row['actor'] = persisted.get('actor') or row.get('actor')
        row['replay_source'] = persisted.get('replay_source') or row.get('replay_source')
        row['auto_approval_decision'] = persisted_details.get('auto_approval_decision') or row.get('auto_approval_decision')
        row['reason'] = persisted_details.get('auto_approval_reason') or row.get('reason')
        row['confidence'] = persisted_details.get('auto_approval_confidence') or row.get('confidence')
        if persisted_details.get('requires_manual') is not None:
            row['requires_manual'] = persisted_details.get('requires_manual')
        if persisted_details.get('auto_approval_eligible') is not None:
            row['auto_approval_eligible'] = persisted_details.get('auto_approval_eligible')
        row['blocked_by'] = persisted_details.get('blocked_by') or row.get('blocked_by') or []
        if persisted.get('state') in TERMINAL_APPROVAL_STATES:
            row['approval_state'] = persisted.get('state')
            row['decision_state'] = persisted.get('state')

    approval_by_playbook = {
        row.get('playbook_id'): row
        for row in (approval_state.get('items') or [])
        if row.get('playbook_id')
    }
    for row in workflow_state.get('item_states') or []:
        approval_row = approval_by_playbook.get(row.get('item_id'))
        if not approval_row:
            continue
        row['approval_state'] = approval_row.get('approval_state') or row.get('approval_state')
        row['persisted_approval_state'] = approval_row.get('persisted_state')
        row['persisted_decision'] = approval_row.get('persisted_decision')
        row['persisted_workflow_state'] = approval_row.get('persisted_workflow_state')
        if approval_row.get('persisted_workflow_state'):
            row['workflow_state'] = approval_row.get('persisted_workflow_state')
        elif row.get('approval_required') and approval_row.get('approval_state') == 'approved' and row.get('workflow_state') == 'pending':
            row['workflow_state'] = 'ready'
        elif approval_row.get('approval_state') in {'rejected', 'deferred', 'expired'}:
            row['workflow_state'] = approval_row.get('approval_state')

    summary = approval_state.get('summary') or {}
    items = approval_state.get('items') or []
    summary['pending_count'] = sum(1 for row in items if row.get('approval_state') == 'pending')
    summary['approved_count'] = sum(1 for row in items if row.get('approval_state') == 'approved')
    summary['rejected_count'] = sum(1 for row in items if row.get('approval_state') == 'rejected')
    summary['deferred_count'] = sum(1 for row in items if row.get('approval_state') == 'deferred')
    approval_state['summary'] = summary

    workflow_summary = workflow_state.get('summary') or {}
    workflow_items = workflow_state.get('item_states') or []
    workflow_summary['ready_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'ready')
    workflow_summary['pending_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'pending')
    workflow_summary['blocked_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'blocked')
    workflow_summary['approved_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'approved')
    workflow_summary['rejected_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'rejected')
    workflow_summary['deferred_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'deferred')
    workflow_state['summary'] = workflow_summary
    return payload




def _normalize_auto_approval_execution_mode(value: Optional[str]) -> str:
    normalized = str(value or '').strip().lower()
    return normalized if normalized in {'disabled', 'controlled'} else 'disabled'


def _normalize_controlled_rollout_execution_mode(value: Optional[str]) -> str:
    normalized = str(value or '').strip().lower()
    return normalized if normalized in {'disabled', 'state_apply'} else 'disabled'


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _build_controlled_rollout_action_details(action_type: str, row: Dict, workflow_item: Dict, spec: Dict, settings: Dict) -> Dict[str, Any]:
    now_iso = _utc_now_iso()
    details: Dict[str, Any] = {
        'effect': spec.get('effect') or 'safe_state_transition',
        'action_type': action_type,
        'safe_apply': True,
        'real_trade_execution': False,
        'dangerous_live_parameter_change': False,
    }

    if action_type == 'joint_queue_promote_safe':
        queue_name = row.get('target_queue') or workflow_item.get('queue_name') or row.get('bucket_id') or workflow_item.get('bucket_id') or 'priority_queue'
        details.update({
            'queue_name': queue_name,
            'queue_action': 'promote_safe',
            'queue_priority': row.get('queue_priority') or workflow_item.get('queue_priority') or 'expedite_safe',
        })
    elif action_type == 'joint_stage_prepare':
        target_stage = str(row.get('target_rollout_stage') or workflow_item.get('target_rollout_stage') or 'prepared').strip().lower() or 'prepared'
        previous_stage = str(row.get('current_rollout_stage') or workflow_item.get('current_rollout_stage') or row.get('rollout_stage') or workflow_item.get('rollout_stage') or 'pending').strip().lower() or 'pending'
        details.update({
            'rollout_stage': target_stage,
            'target_rollout_stage': target_stage,
            'stage_transition': {'from': previous_stage, 'to': target_stage},
            'stage_model': workflow_item.get('stage_model') or row.get('stage_model') or {},
            'queue_progression': workflow_item.get('queue_progression') or row.get('queue_progression') or {},
        })
    elif action_type == 'joint_review_schedule':
        review_after_hours = int(row.get('review_after_hours') or workflow_item.get('review_after_hours') or settings.get('default_review_after_hours') or 24)
        review_after_hours = max(1, min(review_after_hours, 24 * 14))
        due_at = row.get('review_due_at') or workflow_item.get('review_due_at')
        if not due_at:
            due_at = (datetime.now(timezone.utc) + timedelta(hours=review_after_hours)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        details.update({
            'review_status': 'scheduled',
            'review_scheduled_at': now_iso,
            'review_due_at': due_at,
            'review_after_hours': review_after_hours,
            'scheduled_review': workflow_item.get('scheduled_review') or row.get('scheduled_review') or {
                'type': 'time_window',
                'target_trade_count': row.get('target_trade_count') or workflow_item.get('target_trade_count'),
            },
        })
    elif action_type == 'joint_metadata_annotate':
        annotations = row.get('annotations') or workflow_item.get('annotations') or row.get('metadata_annotations') or workflow_item.get('metadata_annotations') or {}
        tags = _dedupe_strings(row.get('annotation_tags') or workflow_item.get('annotation_tags') or row.get('tags') or workflow_item.get('tags') or [])
        note = row.get('annotation_note') or workflow_item.get('annotation_note') or row.get('note') or workflow_item.get('note')
        details.update({
            'annotations': annotations if isinstance(annotations, dict) else {'value': annotations},
            'annotation_tags': tags,
            'annotation_note': note,
            'annotated_at': now_iso,
        })

    return details


def _get_auto_approval_execution_settings(config: Any = None, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    overrides = dict(overrides or {})
    getter = getattr(config, 'get', None)
    raw = getter('governance.auto_approval_execution', {}) if callable(getter) else {}
    raw = dict(raw or {}) if isinstance(raw, dict) else {}
    settings = {
        'enabled': bool(raw.get('enabled', False)),
        'mode': _normalize_auto_approval_execution_mode(raw.get('mode')),
        'actor': str(raw.get('actor') or 'system:auto-approval'),
        'source': str(raw.get('source') or 'auto_approval_execution'),
        'reason_prefix': str(raw.get('reason_prefix') or 'controlled auto-approval execution'),
    }
    settings.update({k: v for k, v in overrides.items() if v is not None})
    settings['enabled'] = bool(settings.get('enabled', False))
    settings['mode'] = _normalize_auto_approval_execution_mode(settings.get('mode'))
    return settings


def _get_controlled_rollout_execution_settings(config: Any = None, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    overrides = dict(overrides or {})
    getter = getattr(config, 'get', None)
    raw = getter('governance.controlled_rollout_execution', {}) if callable(getter) else {}
    raw = dict(raw or {}) if isinstance(raw, dict) else {}
    allowed_action_types = _dedupe_strings(raw.get('allowed_action_types') or ['joint_observe'])
    settings = {
        'enabled': bool(raw.get('enabled', False)),
        'mode': _normalize_controlled_rollout_execution_mode(raw.get('mode')),
        'actor': str(raw.get('actor') or 'system:controlled-rollout'),
        'source': str(raw.get('source') or 'controlled_rollout_execution'),
        'reason_prefix': str(raw.get('reason_prefix') or 'controlled rollout state apply'),
        'allowed_action_types': allowed_action_types,
        'target_workflow_state': str(raw.get('target_workflow_state') or 'ready').strip().lower() or 'ready',
        'target_state': str(raw.get('target_state') or 'ready').strip().lower() or 'ready',
        'default_review_after_hours': int(raw.get('default_review_after_hours') or 24),
    }
    settings.update({k: v for k, v in overrides.items() if v is not None})
    settings['enabled'] = bool(settings.get('enabled', False))
    settings['mode'] = _normalize_controlled_rollout_execution_mode(settings.get('mode'))
    settings['allowed_action_types'] = _dedupe_strings(settings.get('allowed_action_types') or ['joint_observe'])
    settings['target_workflow_state'] = str(settings.get('target_workflow_state') or 'ready').strip().lower() or 'ready'
    settings['target_state'] = str(settings.get('target_state') or 'ready').strip().lower() or 'ready'
    settings['default_review_after_hours'] = max(1, min(int(settings.get('default_review_after_hours') or 24), 24 * 14))
    return settings


def execute_controlled_rollout_layer(payload: Dict, db: Any, *, config: Any = None, settings: Optional[Dict[str, Any]] = None, replay_source: str = 'workflow_ready') -> Dict:
    payload = payload or {}
    execution_settings = _get_controlled_rollout_execution_settings(config=config, overrides=settings)
    approval_state = payload.get('approval_state') or {}
    workflow_state = payload.get('workflow_state') or {}
    approval_items = approval_state.get('items') or []
    workflow_lookup = {row.get('item_id'): row for row in (workflow_state.get('item_states') or []) if row.get('item_id')}

    result = {
        'enabled': execution_settings.get('enabled', False),
        'mode': execution_settings.get('mode'),
        'actor': execution_settings.get('actor'),
        'source': execution_settings.get('source'),
        'replay_source': replay_source,
        'allowed_action_types': execution_settings.get('allowed_action_types') or [],
        'executed_count': 0,
        'skipped_count': 0,
        'items': [],
    }
    payload['controlled_rollout_execution'] = result

    if not result['enabled'] or result['mode'] != 'state_apply' or not approval_items:
        result['skipped_count'] = len(approval_items)
        return payload

    allowed_action_types = set(execution_settings.get('allowed_action_types') or [])
    executed_rows = []
    for row in approval_items:
        approval_id = row.get('approval_id') or row.get('item_id') or row.get('playbook_id')
        workflow_item = workflow_lookup.get(row.get('playbook_id')) or {}
        persisted_row = db.get_approval_state(approval_id) if approval_id else None
        persisted_details = (persisted_row or {}).get('details') or {}
        action_type = str(row.get('action_type') or workflow_item.get('action_type') or (persisted_row or {}).get('approval_type') or '').strip().lower()
        action_spec = CONTROLLED_ROLLOUT_ACTION_SPECS.get(action_type)
        current_state = str((persisted_row or {}).get('state') or row.get('approval_state') or row.get('persisted_state') or row.get('state') or 'pending').strip().lower()
        current_workflow_state = str((persisted_row or {}).get('workflow_state') or row.get('persisted_workflow_state') or workflow_item.get('workflow_state') or row.get('decision_state') or 'pending').strip().lower()
        auto_decision = _normalize_auto_approval_decision(row.get('auto_approval_decision'))
        blocked_by = _dedupe_strings(row.get('blocked_by') or workflow_item.get('blocked_by') or workflow_item.get('blocking_reasons') or [])
        risk_level = str(row.get('risk_level') or workflow_item.get('risk_level') or '').strip().lower()
        requires_manual = bool(row.get('requires_manual'))
        approval_required = bool(row.get('approval_required') if row.get('approval_required') is not None else workflow_item.get('approval_required'))
        eligible = bool(row.get('auto_approval_eligible'))

        target_state = str((action_spec or {}).get('state') or execution_settings['target_state']).strip().lower() or execution_settings['target_state']
        target_workflow_state = str((action_spec or {}).get('workflow_state') or execution_settings['target_workflow_state']).strip().lower() or execution_settings['target_workflow_state']
        event_type = str((action_spec or {}).get('event_type') or 'controlled_rollout_state_apply')
        result_action = str((action_spec or {}).get('result_action') or 'state_applied')

        skip_reason = None
        already_effect_applied = (
            current_state == target_state
            and current_workflow_state == target_workflow_state
            and persisted_details.get('effect') == (action_spec or {}).get('effect')
            and persisted_details.get('action_type') == action_type
        )

        if current_state in TERMINAL_APPROVAL_STATES:
            skip_reason = f'terminal_state:{current_state}'
        elif action_type not in allowed_action_types:
            skip_reason = f'action_type_not_allowlisted:{action_type or "unknown"}'
        elif not action_spec:
            skip_reason = f'action_type_not_supported:{action_type or "unknown"}'
        elif already_effect_applied:
            skip_reason = 'already_applied'
        elif approval_required:
            skip_reason = 'approval_required'
        elif requires_manual:
            skip_reason = 'requires_manual'
        elif not eligible:
            skip_reason = 'not_eligible'
        elif auto_decision != 'auto_approve':
            skip_reason = f'judgement:{auto_decision}'
        elif risk_level != 'low':
            skip_reason = f'risk_level:{risk_level or "unknown"}'
        elif blocked_by:
            skip_reason = 'blocked_by:' + ','.join(blocked_by)

        if skip_reason:
            result['items'].append({
                'item_id': approval_id,
                'playbook_id': row.get('playbook_id'),
                'action_type': action_type,
                'action': 'skipped',
                'reason': skip_reason,
                'state': current_state,
                'workflow_state': current_workflow_state,
            })
            result['skipped_count'] += 1
            continue

        reason = f"{execution_settings['reason_prefix']}: {row.get('reason') or 'policy judged item as low-risk controlled-rollout candidate'}"
        details = {
            'item_id': approval_id,
            'approval_id': approval_id,
            'playbook_id': row.get('playbook_id'),
            'title': row.get('title') or workflow_item.get('title'),
            'bucket_id': row.get('bucket_id') or workflow_item.get('bucket_id'),
            'state': target_state,
            'workflow_state': target_workflow_state,
            'reason': reason,
            'actor': execution_settings['actor'],
            'source': execution_settings['source'],
            'replay_source': replay_source,
            'auto_approval_decision': auto_decision,
            'auto_approval_reason': row.get('reason'),
            'auto_approval_confidence': row.get('confidence'),
            'auto_approval_eligible': True,
            'requires_manual': False,
            'blocked_by': blocked_by,
            'rule_hits': row.get('rule_hits') or [],
            'risk_level': row.get('risk_level') or workflow_item.get('risk_level'),
            'action_type': action_type,
            'execution_layer': 'controlled_rollout_state_apply',
            'execution_mode': execution_settings['mode'],
        }
        details.update(_build_controlled_rollout_action_details(action_type, row, workflow_item, action_spec, execution_settings))
        db.upsert_approval_state(
            item_id=approval_id,
            approval_type=row.get('action_type') or workflow_item.get('action_type') or 'workflow_approval',
            target=row.get('playbook_id'),
            title=row.get('title') or workflow_item.get('title'),
            decision=row.get('persisted_decision') or row.get('approval_state') or 'pending',
            state=target_state,
            workflow_state=target_workflow_state,
            reason=reason,
            actor=execution_settings['actor'],
            replay_source=replay_source,
            details=details,
            preserve_terminal=True,
            event_type=event_type,
            append_event=True,
        )
        executed_rows.append(db.get_approval_state(approval_id))
        result['items'].append({
            'item_id': approval_id,
            'playbook_id': row.get('playbook_id'),
            'action_type': action_type,
            'action': result_action,
            'state': target_state,
            'workflow_state': target_workflow_state,
            'reason': reason,
        })
        result['executed_count'] += 1

    if executed_rows:
        merge_persisted_approval_state(payload, executed_rows)
    return payload


def execute_controlled_auto_approval_layer(payload: Dict, db: Any, *, config: Any = None, settings: Optional[Dict[str, Any]] = None, replay_source: str = 'workflow_ready') -> Dict:
    payload = payload or {}
    execution_settings = _get_auto_approval_execution_settings(config=config, overrides=settings)
    approval_state = payload.get('approval_state') or {}
    workflow_state = payload.get('workflow_state') or {}
    approval_items = approval_state.get('items') or []
    workflow_lookup = {row.get('item_id'): row for row in (workflow_state.get('item_states') or []) if row.get('item_id')}

    result = {
        'enabled': execution_settings.get('enabled', False),
        'mode': execution_settings.get('mode'),
        'actor': execution_settings.get('actor'),
        'source': execution_settings.get('source'),
        'replay_source': replay_source,
        'executed_count': 0,
        'skipped_count': 0,
        'items': [],
    }
    payload['auto_approval_execution'] = result

    if not result['enabled'] or result['mode'] != 'controlled' or not approval_items:
        result['skipped_count'] = len(approval_items)
        return payload

    executed_rows = []
    for row in approval_items:
        approval_id = row.get('approval_id') or row.get('item_id') or row.get('playbook_id')
        workflow_item = workflow_lookup.get(row.get('playbook_id')) or {}
        persisted_row = db.get_approval_state(approval_id) if approval_id else None
        current_state = str((persisted_row or {}).get('state') or row.get('approval_state') or row.get('persisted_state') or row.get('state') or 'pending').strip().lower()
        auto_decision = _normalize_auto_approval_decision(row.get('auto_approval_decision'))
        blocked_by = _dedupe_strings(row.get('blocked_by') or workflow_item.get('blocked_by') or [])
        risk_level = str(row.get('risk_level') or workflow_item.get('risk_level') or '').strip().lower()
        requires_manual = bool(row.get('requires_manual'))
        approval_required = bool(row.get('approval_required') if row.get('approval_required') is not None else workflow_item.get('approval_required'))
        eligible = bool(row.get('auto_approval_eligible'))

        skip_reason = None
        if current_state in TERMINAL_APPROVAL_STATES:
            skip_reason = f'terminal_state:{current_state}'
        elif current_state == 'ready':
            skip_reason = 'already_ready'
        elif auto_decision != 'auto_approve':
            skip_reason = f'judgement:{auto_decision}'
        elif not eligible:
            skip_reason = 'not_eligible'
        elif requires_manual:
            skip_reason = 'requires_manual'
        elif approval_required:
            skip_reason = 'approval_required'
        elif risk_level != 'low':
            skip_reason = f'risk_level:{risk_level or "unknown"}'
        elif blocked_by:
            skip_reason = 'blocked_by:' + ','.join(blocked_by)

        if skip_reason:
            result['items'].append({
                'item_id': approval_id,
                'playbook_id': row.get('playbook_id'),
                'action': 'skipped',
                'reason': skip_reason,
                'state': current_state,
            })
            result['skipped_count'] += 1
            continue

        reason = f"{execution_settings['reason_prefix']}: {row.get('reason') or 'policy judged item as low-risk auto-approve'}"
        details = {
            'item_id': approval_id,
            'approval_id': approval_id,
            'playbook_id': row.get('playbook_id'),
            'title': row.get('title') or workflow_item.get('title'),
            'bucket_id': row.get('bucket_id'),
            'state': 'approved',
            'workflow_state': 'ready',
            'reason': reason,
            'actor': execution_settings['actor'],
            'source': execution_settings['source'],
            'replay_source': replay_source,
            'auto_approval_decision': auto_decision,
            'auto_approval_reason': row.get('reason'),
            'auto_approval_confidence': row.get('confidence'),
            'auto_approval_eligible': True,
            'requires_manual': False,
            'blocked_by': blocked_by,
            'rule_hits': row.get('rule_hits') or [],
            'risk_level': row.get('risk_level') or workflow_item.get('risk_level'),
            'execution_layer': 'controlled_auto_approval',
            'execution_mode': execution_settings['mode'],
        }
        db.record_approval(row.get('action_type') or workflow_item.get('action_type') or 'workflow_approval', row.get('playbook_id'), 'approved', details)
        executed_rows.append(db.get_approval_state(approval_id))
        result['items'].append({
            'item_id': approval_id,
            'playbook_id': row.get('playbook_id'),
            'action': 'approved',
            'state': 'approved',
            'workflow_state': 'ready',
            'reason': reason,
        })
        result['executed_count'] += 1

    if executed_rows:
        merge_persisted_approval_state(payload, executed_rows)
    return payload



def _normalize_rollout_executor_mode(value: Optional[str]) -> str:
    normalized = str(value or '').strip().lower()
    return normalized if normalized in {'disabled', 'dry_run', 'controlled'} else 'disabled'


def _get_rollout_executor_settings(config: Any = None, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    overrides = dict(overrides or {})
    getter = getattr(config, 'get', None)
    raw = getter('governance.rollout_executor', {}) if callable(getter) else {}
    raw = dict(raw or {}) if isinstance(raw, dict) else {}
    allowed_action_types = _dedupe_strings(raw.get('allowed_action_types') or ['joint_observe'])
    settings = {
        'enabled': bool(raw.get('enabled', False)),
        'mode': _normalize_rollout_executor_mode(raw.get('mode')),
        'actor': str(raw.get('actor') or 'system:rollout-executor'),
        'source': str(raw.get('source') or 'rollout_executor'),
        'reason_prefix': str(raw.get('reason_prefix') or 'rollout executor skeleton apply'),
        'allowed_action_types': allowed_action_types,
        'default_review_after_hours': int(raw.get('default_review_after_hours') or 24),
        'dry_run': bool(raw.get('dry_run', False)),
    }
    settings.update({k: v for k, v in overrides.items() if v is not None})
    settings['enabled'] = bool(settings.get('enabled', False))
    settings['mode'] = _normalize_rollout_executor_mode(settings.get('mode'))
    settings['allowed_action_types'] = _dedupe_strings(settings.get('allowed_action_types') or ['joint_observe'])
    settings['default_review_after_hours'] = max(1, min(int(settings.get('default_review_after_hours') or 24), 24 * 14))
    settings['dry_run'] = bool(settings.get('dry_run', False) or settings['mode'] == 'dry_run')
    return settings


def _build_rollout_executor_catalog(allowed_action_types: Optional[List[str]] = None) -> Dict[str, Any]:
    allow = set(_dedupe_strings(allowed_action_types or []))
    executable = []
    queue_only = []
    unsupported = []
    handlers = {}
    for action_type, spec in ROLLOUT_EXECUTOR_ACTION_SPECS.items():
        row = {
            'action_type': action_type,
            'dispatch_mode': spec.get('dispatch_mode'),
            'executor_class': spec.get('executor_class'),
            'handler_key': f"{spec.get('dispatch_mode') or 'unsupported'}::{spec.get('executor_class') or 'unknown'}",
            'allowlisted': action_type in allow,
            'audit_code': spec.get('audit_code'),
            'rollback_capable': bool(spec.get('rollback_capable', False)),
        }
        if spec.get('blocked_reason'):
            row['blocked_reason'] = spec.get('blocked_reason')
        handlers[action_type] = dict(row)
        if spec.get('dispatch_mode') == 'apply':
            executable.append(row)
        elif spec.get('dispatch_mode') == 'queue_only':
            queue_only.append(row)
        else:
            unsupported.append(row)
    return {
        'executable': executable,
        'queue_only': queue_only,
        'unsupported': unsupported,
        'handlers': handlers,
    }


def _build_rollout_dispatch_envelope(*, mode: str, executor_class: str, handler_key: str, allowed: bool = False,
                                     status: str = 'pending', reason: Optional[str] = None, code: Optional[str] = None,
                                     queue_name: Optional[str] = None, dispatch_route: Optional[str] = None,
                                     transition_rule: Optional[str] = None, next_transition: Optional[str] = None,
                                     retryable: Optional[bool] = None, rollback_hint: Optional[str] = None) -> Dict[str, Any]:
    envelope = {
        'mode': mode,
        'executor_class': executor_class,
        'handler_key': handler_key,
        'allowed': bool(allowed),
        'status': status,
        'reason': reason,
        'code': code,
        'dispatch_route': dispatch_route,
        'transition_rule': transition_rule,
        'next_transition': next_transition,
        'retryable': retryable,
        'rollback_hint': rollback_hint,
    }
    if queue_name:
        envelope['queue_name'] = queue_name
    return envelope


def _build_rollout_apply_envelope(*, attempted: bool = False, persisted: bool = False, status: str = 'pending',
                                  operation: str = 'noop', idempotency_key: Optional[str] = None,
                                  effect_applied: bool = False) -> Dict[str, Any]:
    return {
        'attempted': bool(attempted),
        'persisted': bool(persisted),
        'status': status,
        'operation': operation,
        'idempotency_key': idempotency_key,
        'effect_applied': bool(effect_applied),
    }


def _build_rollout_result_envelope(*, disposition: str, status: str, reason: Optional[str] = None,
                                   code: Optional[str] = None, state: Optional[str] = None,
                                   workflow_state: Optional[str] = None, transition_rule: Optional[str] = None,
                                   dispatch_route: Optional[str] = None, next_transition: Optional[str] = None,
                                   retryable: Optional[bool] = None, rollback_hint: Optional[str] = None) -> Dict[str, Any]:
    result = {
        'disposition': disposition,
        'status': status,
        'reason': reason,
        'code': code,
        'transition_rule': transition_rule,
        'dispatch_route': dispatch_route,
        'next_transition': next_transition,
        'retryable': retryable,
        'rollback_hint': rollback_hint,
    }
    if state is not None:
        result['state'] = state
    if workflow_state is not None:
        result['workflow_state'] = workflow_state
    return result


def _resolve_rollout_transition_rule(*, action_type: str, row: Dict[str, Any], workflow_item: Dict[str, Any],
                                     spec: Optional[Dict[str, Any]], current_state: str,
                                     current_workflow_state: str, auto_decision: str, eligible: bool,
                                     approval_required: bool, requires_manual: bool,
                                     blocked_by: Optional[List[str]] = None) -> Dict[str, Any]:
    stage_model = workflow_item.get('stage_model') or row.get('stage_model') or {}
    queue_progression = workflow_item.get('queue_progression') or row.get('queue_progression') or {}
    rollout_stage = str(
        row.get('rollout_stage')
        or workflow_item.get('rollout_stage')
        or stage_model.get('current_stage')
        or row.get('current_rollout_stage')
        or workflow_item.get('current_rollout_stage')
        or 'pending'
    ).strip().lower() or 'pending'
    target_rollout_stage = str(
        row.get('target_rollout_stage')
        or workflow_item.get('target_rollout_stage')
        or stage_model.get('target_stage')
        or rollout_stage
    ).strip().lower() or rollout_stage
    readiness = str(stage_model.get('readiness') or queue_progression.get('state') or current_workflow_state or 'pending').strip().lower() or 'pending'
    dispatch_mode = str((spec or {}).get('dispatch_mode') or 'unsupported').strip().lower() or 'unsupported'
    blocked = _dedupe_strings(blocked_by or [])

    if current_state in TERMINAL_APPROVAL_STATES:
        return {
            'transition_rule': 'preserve_terminal_state',
            'dispatch_route': 'terminal_hold',
            'next_transition': 'preserve_terminal_state',
            'retryable': False,
            'rollback_hint': 'terminal_state_preserved_no_further_executor_apply',
            'rollout_stage': rollout_stage,
            'target_rollout_stage': target_rollout_stage,
            'readiness': readiness,
        }
    if blocked:
        return {
            'transition_rule': 'defer_until_blockers_clear',
            'dispatch_route': 'deferred_review_queue',
            'next_transition': 'retry_after_blockers_clear',
            'retryable': True,
            'rollback_hint': 'keep_current_state_and_clear_blockers_before_retry',
            'rollout_stage': rollout_stage,
            'target_rollout_stage': target_rollout_stage,
            'readiness': readiness,
        }
    if auto_decision in {'defer', 'freeze'}:
        return {
            'transition_rule': 'defer_or_freeze_by_policy',
            'dispatch_route': 'deferred_hold_queue',
            'next_transition': 'manual_review_or_policy_refresh',
            'retryable': auto_decision == 'defer',
            'rollback_hint': 'revert_to_observe_or_hold_until_next_review',
            'rollout_stage': rollout_stage,
            'target_rollout_stage': target_rollout_stage,
            'readiness': readiness,
        }
    if approval_required or requires_manual or auto_decision == 'manual_review':
        return {
            'transition_rule': 'manual_gate_before_dispatch',
            'dispatch_route': 'manual_review_queue',
            'next_transition': 'await_manual_approval',
            'retryable': True,
            'rollback_hint': 'revert_to_previous_stage_if_manual_review_rejects',
            'rollout_stage': rollout_stage,
            'target_rollout_stage': target_rollout_stage,
            'readiness': readiness,
        }
    if dispatch_mode == 'queue_only':
        route = 'stage_promotion_queue' if action_type in {'joint_expand_guarded', 'joint_stage_prepare'} else 'operator_followup_queue'
        return {
            'transition_rule': 'queue_only_followup_required',
            'dispatch_route': route,
            'next_transition': 'queue_for_followup_execution',
            'retryable': True,
            'rollback_hint': 'cancel_or_deprioritize_queue_item_to_rollback',
            'rollout_stage': rollout_stage,
            'target_rollout_stage': target_rollout_stage,
            'readiness': readiness,
        }
    if action_type == 'joint_stage_prepare':
        return {
            'transition_rule': 'stage_prepare_ready_for_safe_apply',
            'dispatch_route': 'stage_metadata_apply',
            'next_transition': 'promote_to_target_stage',
            'retryable': True,
            'rollback_hint': 'revert_stage_metadata_to_previous_stage',
            'rollout_stage': rollout_stage,
            'target_rollout_stage': target_rollout_stage,
            'readiness': readiness,
        }
    if action_type == 'joint_queue_promote_safe':
        return {
            'transition_rule': 'queue_promotion_ready_for_safe_apply',
            'dispatch_route': 'queue_metadata_apply',
            'next_transition': 'mark_queue_promoted_and_wait_review',
            'retryable': True,
            'rollback_hint': 'demote_queue_priority_and_restore_previous_progression',
            'rollout_stage': rollout_stage,
            'target_rollout_stage': target_rollout_stage,
            'readiness': readiness,
        }
    if action_type == 'joint_review_schedule':
        return {
            'transition_rule': 'schedule_review_checkpoint',
            'dispatch_route': 'review_metadata_apply',
            'next_transition': 'wait_for_review_checkpoint',
            'retryable': True,
            'rollback_hint': 'clear_scheduled_review_and_return_to_previous_stage',
            'rollout_stage': rollout_stage,
            'target_rollout_stage': target_rollout_stage,
            'readiness': readiness,
        }
    return {
        'transition_rule': 'safe_apply_ready',
        'dispatch_route': 'safe_state_apply',
        'next_transition': 'mark_ready_for_followup',
        'retryable': True,
        'rollback_hint': 'restore_previous_state_from_approval_timeline',
        'rollout_stage': rollout_stage,
        'target_rollout_stage': target_rollout_stage,
        'readiness': readiness,
    }



def _build_rollout_approval_hook(*, row: Dict[str, Any], workflow_item: Dict[str, Any], current_state: str,
                                 current_workflow_state: str, auto_decision: str, eligible: bool,
                                 approval_required: bool, requires_manual: bool,
                                 blocked_by: Optional[List[str]] = None) -> Dict[str, Any]:
    blocked = _dedupe_strings(blocked_by or [])
    persisted_decision = str(row.get('persisted_decision') or row.get('approval_state') or current_state or 'pending').strip().lower() or 'pending'
    existing_progression = dict(workflow_item.get('queue_progression') or row.get('queue_progression') or {})
    existing_status = str(existing_progression.get('status') or '').strip().lower()
    hook_status = 'ready_to_queue'
    gate_reason = None
    next_action = 'queue_for_followup'

    if current_state in TERMINAL_APPROVAL_STATES:
        hook_status = 'terminal'
        gate_reason = f'terminal_state:{current_state}'
        next_action = 'preserve_terminal_state'
    elif existing_status in {'awaiting_approval', 'blocked_by_approval'}:
        hook_status = 'blocked_by_approval'
        gate_reason = existing_progression.get('reason') or existing_progression.get('blocked_reason') or 'approval_progression_pending'
        next_action = 'await_manual_approval'
    elif blocked:
        hook_status = 'deferred'
        gate_reason = 'blocked_by:' + ','.join(blocked)
        next_action = 'wait_for_preconditions'
    elif auto_decision == 'defer' or auto_decision == 'freeze':
        hook_status = 'deferred'
        gate_reason = 'auto_approval_deferred' if auto_decision == 'defer' else 'freeze_guardrail'
        next_action = 'hold_queue_progression'
    elif approval_required or requires_manual or auto_decision == 'manual_review':
        hook_status = 'ready_to_queue'
        gate_reason = 'manual_review_required'
        next_action = 'queue_for_manual_review'

    return {
        'status': hook_status,
        'approval_state': current_state,
        'workflow_state': current_workflow_state,
        'decision': persisted_decision,
        'auto_approval_decision': auto_decision,
        'eligible': bool(eligible),
        'approval_required': bool(approval_required),
        'requires_manual': bool(requires_manual),
        'blocked_by': blocked,
        'gate_reason': gate_reason,
        'next_action': next_action,
    }



def _build_rollout_queue_plan(action_type: str, row: Dict[str, Any], workflow_item: Dict[str, Any], spec: Dict[str, Any],
                              approval_hook: Optional[Dict[str, Any]] = None,
                              transition_rule: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    queue_name = row.get('target_queue') or workflow_item.get('queue_name') or row.get('bucket_id') or workflow_item.get('bucket_id') or 'manual_review_queue'
    hook = dict(approval_hook or {})
    transition = dict(transition_rule or {})
    queue_status = hook.get('status') or 'ready_to_queue'
    queue_priority = row.get('queue_priority') or workflow_item.get('queue_priority') or ('approval_blocked' if queue_status == 'blocked_by_approval' else 'deferred_review' if queue_status == 'deferred' else 'needs_human_review')
    queue_progression = {
        'status': queue_status,
        'approval_state': hook.get('approval_state'),
        'workflow_state': hook.get('workflow_state'),
        'decision': hook.get('decision'),
        'gate_reason': hook.get('gate_reason') or spec.get('blocked_reason'),
        'next_action': hook.get('next_action') or action_type,
        'dispatch_route': transition.get('dispatch_route'),
        'next_transition': transition.get('next_transition'),
        'retryable': bool(transition.get('retryable', True)),
    }
    return {
        'queue_name': queue_name,
        'queue_action': 'manual_followup',
        'queue_priority': queue_priority,
        'next_action': action_type,
        'blocked_reason': spec.get('blocked_reason'),
        'requires_approval': bool(spec.get('requires_approval', False)),
        'approval_hook': hook,
        'transition_rule': transition.get('transition_rule'),
        'dispatch_route': transition.get('dispatch_route'),
        'next_transition': transition.get('next_transition'),
        'retryable': bool(transition.get('retryable', True)),
        'rollback_hint': transition.get('rollback_hint'),
        'queue_transition': {
            'from_state': hook.get('approval_state'),
            'from_workflow_state': hook.get('workflow_state'),
            'to_queue_status': queue_status,
            'transition_reason': queue_progression['gate_reason'],
            'transition_rule': transition.get('transition_rule'),
            'dispatch_route': transition.get('dispatch_route'),
            'next_transition': transition.get('next_transition'),
            'retryable': bool(transition.get('retryable', True)),
            'rollback_hint': transition.get('rollback_hint'),
        },
        'queue_progression': queue_progression,
        'real_trade_execution': False,
        'dangerous_live_parameter_change': False,
    }


def execute_rollout_executor(payload: Dict, db: Any, *, config: Any = None, settings: Optional[Dict[str, Any]] = None,
                             replay_source: str = 'workflow_ready') -> Dict:
    payload = payload or {}
    execution_settings = _get_rollout_executor_settings(config=config, overrides=settings)
    approval_state = payload.get('approval_state') or {}
    workflow_state = payload.get('workflow_state') or {}
    approval_items = approval_state.get('items') or []
    workflow_lookup = {row.get('item_id'): row for row in (workflow_state.get('item_states') or []) if row.get('item_id')}
    allowlisted = set(execution_settings.get('allowed_action_types') or [])
    catalog = _build_rollout_executor_catalog(execution_settings.get('allowed_action_types'))

    result = {
        'schema_version': 'm5_rollout_executor_skeleton_v2',
        'enabled': execution_settings.get('enabled', False),
        'mode': execution_settings.get('mode'),
        'dry_run': execution_settings.get('dry_run', False),
        'actor': execution_settings.get('actor'),
        'source': execution_settings.get('source'),
        'replay_source': replay_source,
        'status': 'disabled',
        'summary': {
            'item_count': len(approval_items),
            'planned_count': 0,
            'applied_count': 0,
            'queued_count': 0,
            'skipped_count': 0,
            'dry_run_count': 0,
            'error_count': 0,
            'by_disposition': {},
            'by_status': {},
        },
        'supported_action_map': catalog,
        'items': [],
    }
    payload['rollout_executor'] = result

    def bump(disposition: str, status: str):
        result['summary']['by_disposition'][disposition] = result['summary']['by_disposition'].get(disposition, 0) + 1
        result['summary']['by_status'][status] = result['summary']['by_status'].get(status, 0) + 1

    if not approval_items:
        result['status'] = 'idle'
        return payload
    if not result['enabled'] or result['mode'] == 'disabled':
        result['summary']['skipped_count'] = len(approval_items)
        for row in approval_items:
            item = {
                'item_id': row.get('approval_id') or row.get('item_id') or row.get('playbook_id'),
                'playbook_id': row.get('playbook_id'),
                'action_type': str(row.get('action_type') or '').strip().lower(),
                'status': 'disabled',
                'dispatch': _build_rollout_dispatch_envelope(mode='disabled', executor_class='disabled', handler_key='disabled::disabled', status='disabled', reason='rollout_executor_disabled', code='EXECUTOR_DISABLED'),
                'apply': _build_rollout_apply_envelope(status='disabled', operation='noop'),
                'result': _build_rollout_result_envelope(disposition='disabled', status='disabled', reason='rollout_executor_disabled', code='EXECUTOR_DISABLED'),
            }
            result['items'].append(item)
            bump('disabled', 'disabled')
        return payload

    result['status'] = 'dry_run' if result['dry_run'] else 'ready'
    executed_rows = []
    for row in approval_items:
        approval_id = row.get('approval_id') or row.get('item_id') or row.get('playbook_id')
        workflow_item = workflow_lookup.get(row.get('playbook_id')) or {}
        persisted_row = db.get_approval_state(approval_id) if approval_id else None
        persisted_details = (persisted_row or {}).get('details') or {}
        action_type = str(row.get('action_type') or workflow_item.get('action_type') or (persisted_row or {}).get('approval_type') or '').strip().lower()
        spec = ROLLOUT_EXECUTOR_ACTION_SPECS.get(action_type)
        current_state = str((persisted_row or {}).get('state') or row.get('approval_state') or row.get('persisted_state') or 'pending').strip().lower()
        current_workflow_state = str((persisted_row or {}).get('workflow_state') or row.get('persisted_workflow_state') or workflow_item.get('workflow_state') or row.get('decision_state') or 'pending').strip().lower()
        auto_decision = _normalize_auto_approval_decision(row.get('auto_approval_decision'))
        blocked_by = _dedupe_strings(row.get('blocked_by') or workflow_item.get('blocked_by') or workflow_item.get('blocking_reasons') or [])
        risk_level = str(row.get('risk_level') or workflow_item.get('risk_level') or '').strip().lower()
        requires_manual = bool(row.get('requires_manual'))
        approval_required = bool(row.get('approval_required') if row.get('approval_required') is not None else workflow_item.get('approval_required'))
        eligible = bool(row.get('auto_approval_eligible'))
        dispatch_mode = (spec or {}).get('dispatch_mode') or 'unsupported'
        executor_class = (spec or {}).get('executor_class') or 'unsupported'
        handler_key = f'{dispatch_mode}::{executor_class}'
        idempotency_key = f'rollout_executor::{approval_id}::{action_type}::{(spec or {}).get("state") or current_state}::{(spec or {}).get("workflow_state") or current_workflow_state}'

        transition_rule = _resolve_rollout_transition_rule(
            action_type=action_type,
            row=row,
            workflow_item=workflow_item,
            spec=spec,
            current_state=current_state,
            current_workflow_state=current_workflow_state,
            auto_decision=auto_decision,
            eligible=eligible,
            approval_required=approval_required,
            requires_manual=requires_manual,
            blocked_by=blocked_by,
        )

        plan = {
            'item_id': approval_id,
            'playbook_id': row.get('playbook_id'),
            'title': row.get('title') or workflow_item.get('title'),
            'action_type': action_type,
            'dispatch_mode': dispatch_mode,
            'current_state': current_state,
            'current_workflow_state': current_workflow_state,
            'target_state': (spec or {}).get('state'),
            'target_workflow_state': (spec or {}).get('workflow_state'),
            'risk_level': risk_level or 'unknown',
            'eligible': eligible,
            'requires_manual': requires_manual,
            'approval_required': approval_required,
            'blocked_by': blocked_by,
            'dry_run': result['dry_run'],
            'allowlisted': action_type in allowlisted,
            'executor_class': executor_class,
            'handler_key': handler_key,
            'rollback_capable': bool((spec or {}).get('rollback_capable', False)),
            'idempotency_key': idempotency_key,
            'transition_rule': transition_rule.get('transition_rule'),
            'dispatch_route': transition_rule.get('dispatch_route'),
            'next_transition': transition_rule.get('next_transition'),
            'retryable': bool(transition_rule.get('retryable', True)),
            'rollback_hint': transition_rule.get('rollback_hint'),
            'rollout_stage': transition_rule.get('rollout_stage'),
            'target_rollout_stage': transition_rule.get('target_rollout_stage'),
            'readiness': transition_rule.get('readiness'),
        }

        audit = {
            'executor': 'rollout_executor_skeleton',
            'schema_version': result['schema_version'],
            'actor': execution_settings['actor'],
            'source': execution_settings['source'],
            'replay_source': replay_source,
            'safe_boundary': {
                'real_trade_execution': False,
                'dangerous_live_parameter_change': False,
                'allowlisted_apply_only': True,
            },
            'audit_code': (spec or {}).get('audit_code') or 'UNSUPPORTED_ACTION',
            'auto_approval_decision': auto_decision,
            'auto_approval_reason': row.get('reason'),
            'auto_approval_confidence': row.get('confidence'),
            'transition_rule': transition_rule.get('transition_rule'),
            'dispatch_route': transition_rule.get('dispatch_route'),
            'next_transition': transition_rule.get('next_transition'),
            'retryable': bool(transition_rule.get('retryable', True)),
            'rollback_hint': transition_rule.get('rollback_hint'),
        }

        result_row = {
            'item_id': approval_id,
            'playbook_id': row.get('playbook_id'),
            'action_type': action_type,
            'status': 'planned',
            'plan': plan,
            'dispatch': _build_rollout_dispatch_envelope(mode=dispatch_mode, executor_class=executor_class, handler_key=handler_key, dispatch_route=transition_rule.get('dispatch_route'), transition_rule=transition_rule.get('transition_rule'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint')),
            'apply': _build_rollout_apply_envelope(idempotency_key=idempotency_key),
            'result': _build_rollout_result_envelope(disposition='skipped', status='planned', transition_rule=transition_rule.get('transition_rule'), dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint')),
            'audit': audit,
        }
        result['summary']['planned_count'] += 1

        skip_reason = None
        skip_code = None
        already_applied = (
            persisted_details.get('execution_layer') == 'rollout_executor_skeleton'
            and persisted_details.get('action_type') == action_type
            and persisted_details.get('effect') == (spec or {}).get('effect')
            and current_state == str((spec or {}).get('state') or current_state)
            and current_workflow_state == str((spec or {}).get('workflow_state') or current_workflow_state)
        )
        if current_state in TERMINAL_APPROVAL_STATES:
            skip_reason, skip_code = f'terminal_state:{current_state}', 'TERMINAL_STATE'
        elif not spec:
            skip_reason, skip_code = f'action_type_not_supported:{action_type or "unknown"}', 'ACTION_NOT_SUPPORTED'
        elif action_type not in allowlisted:
            skip_reason, skip_code = f'action_type_not_allowlisted:{action_type or "unknown"}', 'ACTION_NOT_ALLOWLISTED'
        elif dispatch_mode == 'queue_only':
            approval_hook = _build_rollout_approval_hook(
                row=row,
                workflow_item=workflow_item,
                current_state=current_state,
                current_workflow_state=current_workflow_state,
                auto_decision=auto_decision,
                eligible=eligible,
                approval_required=approval_required,
                requires_manual=requires_manual,
                blocked_by=blocked_by,
            )
            queue_plan = _build_rollout_queue_plan(action_type, row, workflow_item, spec, approval_hook=approval_hook, transition_rule=transition_rule)
            result_row['plan']['queue_plan'] = queue_plan
            queue_status = queue_plan['queue_progression']['status']
            if queue_status == 'ready_to_queue':
                result_row['dispatch'] = _build_rollout_dispatch_envelope(mode=dispatch_mode, executor_class=executor_class, handler_key=handler_key, allowed=True, status='queued', reason='queue_only', code='QUEUE_ONLY', queue_name=queue_plan['queue_name'], dispatch_route=transition_rule.get('dispatch_route'), transition_rule=transition_rule.get('transition_rule'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
                result_row['apply'] = _build_rollout_apply_envelope(status='queued', operation='queue_plan_only', idempotency_key=idempotency_key)
                result_row['result'] = _build_rollout_result_envelope(disposition='queued', status='queued', reason=spec.get('blocked_reason') or 'queue_only_action', code='QUEUE_ONLY', transition_rule=transition_rule.get('transition_rule'), dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
                result_row['status'] = 'queued'
                result['summary']['queued_count'] += 1
                result['items'].append(result_row)
                bump('queued', 'queued')
            elif queue_status == 'blocked_by_approval':
                result_row['dispatch'] = _build_rollout_dispatch_envelope(mode=dispatch_mode, executor_class=executor_class, handler_key=handler_key, allowed=True, status='blocked_by_approval', reason=approval_hook.get('gate_reason') or 'approval_gated_dispatch', code='APPROVAL_GATED_QUEUE', queue_name=queue_plan['queue_name'], dispatch_route=transition_rule.get('dispatch_route'), transition_rule=transition_rule.get('transition_rule'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
                result_row['apply'] = _build_rollout_apply_envelope(status='approval_gated', operation='queue_plan_only', idempotency_key=idempotency_key)
                result_row['result'] = _build_rollout_result_envelope(disposition='blocked_by_approval', status='blocked_by_approval', reason=approval_hook.get('gate_reason') or 'approval_gated_dispatch', code='APPROVAL_GATED_QUEUE', transition_rule=transition_rule.get('transition_rule'), dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
                result_row['status'] = 'blocked_by_approval'
                result['summary']['skipped_count'] += 1
                result['items'].append(result_row)
                bump('blocked_by_approval', 'blocked_by_approval')
            else:
                result_row['dispatch'] = _build_rollout_dispatch_envelope(mode=dispatch_mode, executor_class=executor_class, handler_key=handler_key, allowed=True, status='deferred', reason=approval_hook.get('gate_reason') or 'queue_deferred', code='QUEUE_DEFERRED', queue_name=queue_plan['queue_name'], dispatch_route=transition_rule.get('dispatch_route'), transition_rule=transition_rule.get('transition_rule'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
                result_row['apply'] = _build_rollout_apply_envelope(status='deferred', operation='queue_plan_only', idempotency_key=idempotency_key)
                result_row['result'] = _build_rollout_result_envelope(disposition='deferred', status='deferred', reason=approval_hook.get('gate_reason') or 'queue_deferred', code='QUEUE_DEFERRED', transition_rule=transition_rule.get('transition_rule'), dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
                result_row['status'] = 'deferred'
                result['summary']['skipped_count'] += 1
                result['items'].append(result_row)
                bump('deferred', 'deferred')
            continue
        elif approval_required:
            skip_reason, skip_code = 'approval_required', 'APPROVAL_REQUIRED'
        elif requires_manual:
            skip_reason, skip_code = 'requires_manual', 'REQUIRES_MANUAL'
        elif not eligible:
            skip_reason, skip_code = 'not_eligible', 'NOT_ELIGIBLE'
        elif auto_decision != 'auto_approve':
            skip_reason, skip_code = f'judgement:{auto_decision}', 'AUTO_APPROVAL_REJECTED'
        elif risk_level != 'low':
            skip_reason, skip_code = f'risk_level:{risk_level or "unknown"}', 'RISK_NOT_LOW'
        elif blocked_by:
            skip_reason, skip_code = 'blocked_by:' + ','.join(blocked_by), 'BLOCKED_BY'
        elif already_applied:
            skip_reason, skip_code = 'already_applied', 'IDEMPOTENT_ALREADY_APPLIED'
            result_row['apply'] = _build_rollout_apply_envelope(status='idempotent_skip', operation='noop', idempotency_key=idempotency_key)

        if skip_reason:
            result_row['dispatch']['status'] = 'skipped'
            result_row['dispatch']['reason'] = skip_reason
            result_row['dispatch']['code'] = skip_code
            result_row['result'] = _build_rollout_result_envelope(disposition='skipped', status='skipped', reason=skip_reason, code=skip_code, transition_rule=transition_rule.get('transition_rule'), dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
            result_row['status'] = 'skipped'
            result['summary']['skipped_count'] += 1
            result['items'].append(result_row)
            bump('skipped', 'skipped')
            continue

        result_row['dispatch'] = _build_rollout_dispatch_envelope(mode=dispatch_mode, executor_class=executor_class, handler_key=handler_key, allowed=True, status='dispatch_ready', reason='safe_apply_candidate', code='SAFE_APPLY_CANDIDATE', dispatch_route=transition_rule.get('dispatch_route'), transition_rule=transition_rule.get('transition_rule'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
        reason = f"{execution_settings['reason_prefix']}: {row.get('reason') or 'safe rollout executor action'}"
        details = {
            'item_id': approval_id,
            'approval_id': approval_id,
            'playbook_id': row.get('playbook_id'),
            'title': row.get('title') or workflow_item.get('title'),
            'bucket_id': row.get('bucket_id') or workflow_item.get('bucket_id'),
            'state': spec.get('state'),
            'workflow_state': spec.get('workflow_state'),
            'reason': reason,
            'actor': execution_settings['actor'],
            'source': execution_settings['source'],
            'replay_source': replay_source,
            'auto_approval_decision': auto_decision,
            'auto_approval_reason': row.get('reason'),
            'auto_approval_confidence': row.get('confidence'),
            'auto_approval_eligible': True,
            'requires_manual': False,
            'blocked_by': blocked_by,
            'rule_hits': row.get('rule_hits') or [],
            'risk_level': row.get('risk_level') or workflow_item.get('risk_level'),
            'action_type': action_type,
            'execution_layer': 'rollout_executor_skeleton',
            'execution_mode': execution_settings['mode'],
            'dispatch_mode': dispatch_mode,
            'handler_key': handler_key,
            'rollback_capable': bool(spec.get('rollback_capable', False)),
            'idempotency_key': idempotency_key,
            'transition_rule': transition_rule.get('transition_rule'),
            'dispatch_route': transition_rule.get('dispatch_route'),
            'next_transition': transition_rule.get('next_transition'),
            'retryable': bool(transition_rule.get('retryable', True)),
            'rollback_hint': transition_rule.get('rollback_hint'),
            'rollout_stage': transition_rule.get('rollout_stage'),
            'target_rollout_stage': transition_rule.get('target_rollout_stage'),
            'readiness': transition_rule.get('readiness'),
            'real_trade_execution': False,
            'dangerous_live_parameter_change': False,
        }
        details.update(_build_controlled_rollout_action_details(action_type, row, workflow_item, spec, execution_settings))
        result_row['apply'] = _build_rollout_apply_envelope(attempted=True, persisted=False, status='applying', operation='upsert_approval_state', idempotency_key=idempotency_key)
        if result['dry_run']:
            result_row['apply']['status'] = 'dry_run'
            result_row['result'] = _build_rollout_result_envelope(disposition='dry_run', status='dry_run', reason=reason, code='DRY_RUN_ONLY', state=spec.get('state'), workflow_state=spec.get('workflow_state'), transition_rule=transition_rule.get('transition_rule'), dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
            result_row['status'] = 'dry_run'
            result['summary']['applied_count'] += 1
            result['summary']['dry_run_count'] += 1
            result['items'].append(result_row)
            bump('dry_run', 'dry_run')
            continue

        try:
            db.upsert_approval_state(
                item_id=approval_id,
                approval_type=row.get('action_type') or workflow_item.get('action_type') or 'workflow_approval',
                target=row.get('playbook_id'),
                title=row.get('title') or workflow_item.get('title'),
                decision=row.get('persisted_decision') or row.get('approval_state') or 'pending',
                state=spec.get('state'),
                workflow_state=spec.get('workflow_state'),
                reason=reason,
                actor=execution_settings['actor'],
                replay_source=replay_source,
                details=details,
                preserve_terminal=True,
                event_type=str(spec.get('event_type') or 'rollout_executor_apply'),
                append_event=True,
            )
            executed_rows.append(db.get_approval_state(approval_id))
            result_row['apply'] = _build_rollout_apply_envelope(attempted=True, persisted=True, status='applied', operation='upsert_approval_state', idempotency_key=idempotency_key, effect_applied=True)
            result_row['result'] = _build_rollout_result_envelope(disposition='applied', status='applied', reason=reason, code='SAFE_APPLIED', state=spec.get('state'), workflow_state=spec.get('workflow_state'), transition_rule=transition_rule.get('transition_rule'), dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
            result_row['status'] = 'applied'
            result['summary']['applied_count'] += 1
            result['items'].append(result_row)
            bump('applied', 'applied')
        except Exception as exc:
            result_row['apply'] = _build_rollout_apply_envelope(attempted=True, persisted=False, status='error', operation='upsert_approval_state', idempotency_key=idempotency_key)
            result_row['result'] = _build_rollout_result_envelope(disposition='error', status='error', reason=str(exc), code='APPLY_EXCEPTION', transition_rule=transition_rule.get('transition_rule'), dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
            result_row['status'] = 'error'
            result['summary']['error_count'] += 1
            result['items'].append(result_row)
            bump('error', 'error')

    if executed_rows:
        merge_persisted_approval_state(payload, executed_rows)
    if result['summary']['error_count']:
        result['status'] = 'error'
    elif result['summary']['applied_count'] and not result['dry_run']:
        result['status'] = 'executed'
    return payload

def build_approval_audit_overview(*, stale_rows: Optional[List[Dict]] = None,
                                  decision_diffs: Optional[List[Dict]] = None,
                                  timeline_summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    stale_rows = stale_rows or []
    decision_diffs = decision_diffs or []
    overview = {
        'stale_pending': {
            'count': len(stale_rows),
            'items': stale_rows,
        },
        'decision_diff': {
            'count': len(decision_diffs),
            'items': decision_diffs,
        },
    }
    if timeline_summary:
        overview['timeline_summary'] = timeline_summary
    return overview
