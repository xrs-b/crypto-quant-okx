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
            'stage_transition': {'from': previous_stage, 'to': target_stage},
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
