"""Approval/workflow persistence helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any


TERMINAL_APPROVAL_STATES = {'approved', 'rejected', 'deferred', 'expired'}
AUTO_APPROVAL_DECISIONS = {'auto_approve', 'manual_review', 'freeze', 'defer'}
SAFE_ROLLOUT_STAGE_HANDLER_REGISTRY = {
    'observe_ready': {
        'handler_key': 'apply::observe_ready',
        'executor_class': 'state_transition',
        'stage_family': 'observe',
        'route': 'safe_state_apply',
        'disposition': 'apply',
        'safe_boundary': 'state_only',
        'description': 'Mark low-risk observe items ready without changing live trading behaviour.',
    },
    'queue_promote_safe': {
        'handler_key': 'apply::queue_promote_safe',
        'executor_class': 'queue_metadata',
        'stage_family': 'queue',
        'route': 'queue_metadata_apply',
        'disposition': 'apply',
        'safe_boundary': 'metadata_only',
        'description': 'Apply queue progression metadata only; never triggers real execution.',
    },
    'stage_prepare_safe': {
        'handler_key': 'apply::stage_prepare_safe',
        'executor_class': 'stage_metadata',
        'stage_family': 'stage',
        'route': 'stage_metadata_apply',
        'disposition': 'apply',
        'safe_boundary': 'metadata_only',
        'description': 'Apply rollout stage metadata and transition hints only.',
    },
    'review_schedule_safe': {
        'handler_key': 'apply::review_schedule_safe',
        'executor_class': 'review_metadata',
        'stage_family': 'review',
        'route': 'review_metadata_apply',
        'disposition': 'apply',
        'safe_boundary': 'metadata_only',
        'description': 'Schedule review checkpoints without executing trading changes.',
    },
    'metadata_annotate_safe': {
        'handler_key': 'apply::metadata_annotate_safe',
        'executor_class': 'annotation_metadata',
        'stage_family': 'metadata',
        'route': 'metadata_annotation_apply',
        'disposition': 'apply',
        'safe_boundary': 'metadata_only',
        'description': 'Persist audit annotations/tags only.',
    },
    'queue_only_live_trading_change': {
        'handler_key': 'queue_only::live_trading_change',
        'executor_class': 'live_trading_change',
        'stage_family': 'manual_gate',
        'route': 'manual_review_queue',
        'disposition': 'queue_only',
        'safe_boundary': 'queue_only',
        'description': 'Sensitive live trading changes stay queued for humans.',
    },
    'queue_only_governance_control': {
        'handler_key': 'queue_only::governance_control',
        'executor_class': 'governance_control',
        'stage_family': 'manual_gate',
        'route': 'manual_review_queue',
        'disposition': 'queue_only',
        'safe_boundary': 'queue_only',
        'description': 'Governance control actions are queue-only.',
    },
    'queue_only_strategy_weight_change': {
        'handler_key': 'queue_only::strategy_weight_change',
        'executor_class': 'strategy_weight_change',
        'stage_family': 'manual_gate',
        'route': 'manual_review_queue',
        'disposition': 'queue_only',
        'safe_boundary': 'queue_only',
        'description': 'Strategy weight changes stay queued for review.',
    },
    'queue_only_policy_switch': {
        'handler_key': 'queue_only::policy_switch',
        'executor_class': 'policy_switch',
        'stage_family': 'manual_gate',
        'route': 'manual_review_queue',
        'disposition': 'queue_only',
        'safe_boundary': 'queue_only',
        'description': 'Policy switch actions are intentionally not auto-applied.',
    },
    'queue_only_rollout_control': {
        'handler_key': 'queue_only::rollout_control',
        'executor_class': 'rollout_control',
        'stage_family': 'manual_gate',
        'route': 'manual_review_queue',
        'disposition': 'queue_only',
        'safe_boundary': 'queue_only',
        'description': 'Rollout freeze remains queue-only and fully auditable.',
    },
    'unsupported_action': {
        'handler_key': 'unsupported::unsupported_action',
        'executor_class': 'unsupported',
        'stage_family': 'unsupported',
        'route': 'unsupported_hold',
        'disposition': 'unsupported',
        'safe_boundary': 'hold_only',
        'description': 'Unknown actions are held and surfaced as unsupported.',
    },
}

CONTROLLED_ROLLOUT_ACTION_SPECS = {
    'joint_observe': {
        'state': 'ready',
        'workflow_state': 'ready',
        'event_type': 'controlled_rollout_state_apply',
        'result_action': 'state_applied',
        'effect': 'safe_state_transition',
        'safe_handler': 'observe_ready',
    },
    'joint_queue_promote_safe': {
        'state': 'ready',
        'workflow_state': 'ready',
        'event_type': 'controlled_rollout_queue_promote',
        'result_action': 'queue_promoted',
        'effect': 'safe_queue_promotion',
        'safe_handler': 'queue_promote_safe',
    },
    'joint_stage_prepare': {
        'state': 'ready',
        'workflow_state': 'ready',
        'event_type': 'controlled_rollout_stage_prepare',
        'result_action': 'stage_prepared',
        'effect': 'safe_rollout_stage_transition',
        'safe_handler': 'stage_prepare_safe',
    },
    'joint_review_schedule': {
        'state': 'pending',
        'workflow_state': 'pending',
        'event_type': 'controlled_rollout_review_schedule',
        'result_action': 'review_scheduled',
        'effect': 'safe_review_scheduling',
        'safe_handler': 'review_schedule_safe',
    },
    'joint_metadata_annotate': {
        'state': 'pending',
        'workflow_state': 'pending',
        'event_type': 'controlled_rollout_metadata_annotate',
        'result_action': 'metadata_annotated',
        'effect': 'safe_metadata_annotation',
        'safe_handler': 'metadata_annotate_safe',
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
        'safe_handler': 'queue_only_live_trading_change',
    },
    'joint_freeze': {
        'dispatch_mode': 'queue_only',
        'executor_class': 'governance_control',
        'audit_code': 'QUEUE_ONLY_FREEZE',
        'blocked_reason': 'freeze_apply_not_automated_in_executor',
        'requires_approval': True,
        'rollback_capable': False,
        'safe_handler': 'queue_only_governance_control',
    },
    'joint_deweight': {
        'dispatch_mode': 'queue_only',
        'executor_class': 'strategy_weight_change',
        'audit_code': 'QUEUE_ONLY_DEWEIGHT',
        'blocked_reason': 'strategy_weight_change_not_automated_in_executor',
        'requires_approval': True,
        'rollback_capable': False,
        'safe_handler': 'queue_only_strategy_weight_change',
    },
    'prefer_strategy_best_policy': {
        'dispatch_mode': 'queue_only',
        'executor_class': 'policy_switch',
        'audit_code': 'QUEUE_ONLY_POLICY_SWITCH',
        'blocked_reason': 'policy_switch_not_automated_in_executor',
        'requires_approval': True,
        'rollback_capable': False,
        'safe_handler': 'queue_only_policy_switch',
    },
    'rollout_freeze': {
        'dispatch_mode': 'queue_only',
        'executor_class': 'rollout_control',
        'audit_code': 'QUEUE_ONLY_ROLLOUT_FREEZE',
        'blocked_reason': 'rollout_freeze_not_automated_in_executor',
        'requires_approval': True,
        'rollback_capable': False,
        'safe_handler': 'queue_only_rollout_control',
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


def _resolve_safe_rollout_handler(spec: Optional[Dict[str, Any]] = None, action_type: Optional[str] = None) -> Dict[str, Any]:
    handler_name = str((spec or {}).get('safe_handler') or '').strip()
    handler = SAFE_ROLLOUT_STAGE_HANDLER_REGISTRY.get(handler_name)
    if handler:
        return dict(handler)
    fallback = dict(SAFE_ROLLOUT_STAGE_HANDLER_REGISTRY['unsupported_action'])
    fallback['requested_action_type'] = str(action_type or '').strip().lower()
    fallback['requested_handler'] = handler_name or None
    return fallback


def _build_safe_rollout_action_registry(allowed_action_types: Optional[List[str]] = None) -> Dict[str, Any]:
    allow = set(_dedupe_strings(allowed_action_types or []))
    handlers = {key: dict(value) for key, value in SAFE_ROLLOUT_STAGE_HANDLER_REGISTRY.items()}
    actions = {}
    executable = []
    queue_only = []
    unsupported = []
    for action_type, spec in ROLLOUT_EXECUTOR_ACTION_SPECS.items():
        handler = _resolve_safe_rollout_handler(spec, action_type)
        entry = {
            'action_type': action_type,
            'allowlisted': action_type in allow,
            'dispatch_mode': spec.get('dispatch_mode') or handler.get('disposition'),
            'executor_class': spec.get('executor_class') or handler.get('executor_class'),
            'handler': handler,
            'handler_key': handler.get('handler_key'),
            'route': handler.get('route'),
            'stage_family': handler.get('stage_family'),
            'rollback_capable': bool(spec.get('rollback_capable', False)),
            'audit_code': spec.get('audit_code'),
        }
        actions[action_type] = entry
        disposition = entry['dispatch_mode']
        if disposition == 'apply':
            executable.append(entry)
        elif disposition == 'queue_only':
            queue_only.append(entry)
        else:
            unsupported.append(entry)
    return {
        'actions': actions,
        'handlers': handlers,
        'executable': executable,
        'queue_only': queue_only,
        'unsupported': unsupported,
        'fallback_handler': dict(SAFE_ROLLOUT_STAGE_HANDLER_REGISTRY['unsupported_action']),
    }


def _build_controlled_rollout_action_details(action_type: str, row: Dict, workflow_item: Dict, spec: Dict, settings: Dict) -> Dict[str, Any]:
    now_iso = _utc_now_iso()
    handler = _resolve_safe_rollout_handler(spec, action_type)
    details: Dict[str, Any] = {
        'effect': spec.get('effect') or 'safe_state_transition',
        'action_type': action_type,
        'safe_apply': True,
        'real_trade_execution': False,
        'dangerous_live_parameter_change': False,
        'safe_handler': handler,
        'safe_handler_key': handler.get('handler_key'),
        'safe_handler_stage_family': handler.get('stage_family'),
        'safe_handler_route': handler.get('route'),
        'safe_handler_disposition': handler.get('disposition'),
        'serialization_ready': True,
        'observability': {
            'handler_key': handler.get('handler_key'),
            'stage_family': handler.get('stage_family'),
            'route': handler.get('route'),
            'disposition': handler.get('disposition'),
        },
    }

    if action_type == 'joint_queue_promote_safe':
        queue_name = row.get('target_queue') or workflow_item.get('queue_name') or row.get('bucket_id') or workflow_item.get('bucket_id') or 'priority_queue'
        details.update({
            'queue_name': queue_name,
            'queue_action': 'promote_safe',
            'queue_priority': row.get('queue_priority') or workflow_item.get('queue_priority') or 'expedite_safe',
            'queue_handler': {
                'queue_name': queue_name,
                'queue_action': 'promote_safe',
                'disposition': handler.get('disposition'),
                'route': handler.get('route'),
            },
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
            'stage_handler': {
                'current_stage': previous_stage,
                'target_stage': target_stage,
                'disposition': handler.get('disposition'),
                'route': handler.get('route'),
            },
        })
    elif action_type == 'joint_review_schedule':
        review_after_hours = int(row.get('review_after_hours') or workflow_item.get('review_after_hours') or settings.get('default_review_after_hours') or 24)
        review_after_hours = max(1, min(review_after_hours, 24 * 14))
        due_at = row.get('review_due_at') or workflow_item.get('review_due_at')
        if not due_at:
            due_at = (datetime.now(timezone.utc) + timedelta(hours=review_after_hours)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        scheduled_review = workflow_item.get('scheduled_review') or row.get('scheduled_review') or {
            'type': 'time_window',
            'target_trade_count': row.get('target_trade_count') or workflow_item.get('target_trade_count'),
        }
        details.update({
            'review_status': 'scheduled',
            'review_scheduled_at': now_iso,
            'review_due_at': due_at,
            'review_after_hours': review_after_hours,
            'scheduled_review': scheduled_review,
            'review_handler': {
                'review_status': 'scheduled',
                'review_due_at': due_at,
                'review_after_hours': review_after_hours,
                'route': handler.get('route'),
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
            'metadata_handler': {
                'annotation_count': len(annotations) if isinstance(annotations, dict) else 1,
                'annotation_tags': tags,
                'route': handler.get('route'),
            },
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
    registry = _build_safe_rollout_action_registry(allowed_action_types)
    handlers = {}
    for action_type, entry in (registry.get('actions') or {}).items():
        row = {
            'action_type': action_type,
            'dispatch_mode': entry.get('dispatch_mode'),
            'executor_class': entry.get('executor_class'),
            'handler_key': entry.get('handler_key'),
            'allowlisted': bool(entry.get('allowlisted')),
            'audit_code': entry.get('audit_code'),
            'rollback_capable': bool(entry.get('rollback_capable', False)),
            'handler': entry.get('handler'),
            'route': entry.get('route'),
            'stage_family': entry.get('stage_family'),
        }
        spec = ROLLOUT_EXECUTOR_ACTION_SPECS.get(action_type) or {}
        if spec.get('blocked_reason'):
            row['blocked_reason'] = spec.get('blocked_reason')
        handlers[action_type] = dict(row)
    return {
        'executable': registry.get('executable') or [],
        'queue_only': registry.get('queue_only') or [],
        'unsupported': registry.get('unsupported') or [],
        'handlers': handlers,
        'stage_handlers': registry.get('handlers') or {},
        'fallback_handler': registry.get('fallback_handler') or {},
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


def _consume_rollout_queue_plan(*, db: Any, row: Dict[str, Any], workflow_item: Dict[str, Any], approval_id: str,
                                action_type: str, current_state: str, current_workflow_state: str,
                                queue_plan: Dict[str, Any], transition_rule: Dict[str, Any],
                                execution_settings: Dict[str, Any], replay_source: str,
                                handler: Dict[str, Any], handler_key: str, dispatch_mode: str,
                                executor_class: str, audit_code: str, idempotency_key: str) -> Dict[str, Any]:
    queue_status = str(((queue_plan or {}).get('queue_progression') or {}).get('status') or 'ready_to_queue').strip().lower() or 'ready_to_queue'
    persisted_state = current_state
    persisted_workflow_state = current_workflow_state
    event_type = 'rollout_executor_queue_consumed'
    result_action = 'queued'

    if queue_status == 'ready_to_queue':
        persisted_workflow_state = 'queued'
        event_type = 'rollout_executor_queue_promoted'
        result_action = 'queued'
    elif queue_status == 'blocked_by_approval':
        persisted_workflow_state = 'blocked_by_approval'
        event_type = 'rollout_executor_queue_blocked'
        result_action = 'blocked_by_approval'
    elif queue_status == 'deferred':
        persisted_state = 'deferred'
        persisted_workflow_state = 'deferred'
        event_type = 'rollout_executor_queue_deferred'
        result_action = 'deferred'

    reason = ((queue_plan or {}).get('queue_transition') or {}).get('transition_reason') or ((queue_plan or {}).get('approval_hook') or {}).get('gate_reason') or (queue_plan or {}).get('blocked_reason') or 'queue_plan_consumed'
    details = {
        'queue_result_action': result_action,
        'item_id': approval_id,
        'approval_id': approval_id,
        'playbook_id': row.get('playbook_id'),
        'title': row.get('title') or workflow_item.get('title'),
        'action_type': action_type,
        'state': persisted_state,
        'workflow_state': persisted_workflow_state,
        'reason': reason,
        'actor': execution_settings['actor'],
        'source': execution_settings['source'],
        'replay_source': replay_source,
        'execution_layer': 'rollout_queue_executor',
        'execution_mode': execution_settings['mode'],
        'dispatch_mode': dispatch_mode,
        'executor_class': executor_class,
        'handler_key': handler_key,
        'audit_code': audit_code,
        'queue_plan_consumed': True,
        'queue_plan': queue_plan,
        'approval_hook': (queue_plan or {}).get('approval_hook') or {},
        'queue_transition': (queue_plan or {}).get('queue_transition') or {},
        'queue_progression': (queue_plan or {}).get('queue_progression') or {},
        'transition_rule': transition_rule.get('transition_rule'),
        'dispatch_route': transition_rule.get('dispatch_route'),
        'next_transition': transition_rule.get('next_transition'),
        'retryable': bool(transition_rule.get('retryable', True)),
        'rollback_hint': transition_rule.get('rollback_hint'),
        'queue_name': queue_plan.get('queue_name'),
        'queue_priority': queue_plan.get('queue_priority'),
        'safe_handler_route': handler.get('route'),
        'safe_handler_disposition': handler.get('disposition'),
        'safe_handler_stage_family': handler.get('stage_family'),
        'idempotency_key': idempotency_key,
        'real_trade_execution': False,
        'dangerous_live_parameter_change': False,
        'serialization_ready': True,
    }
    db.upsert_approval_state(
        item_id=approval_id,
        approval_type=row.get('action_type') or workflow_item.get('action_type') or 'workflow_approval',
        target=row.get('playbook_id'),
        title=row.get('title') or workflow_item.get('title'),
        decision=row.get('persisted_decision') or row.get('approval_state') or current_state or 'pending',
        state=persisted_state,
        workflow_state=persisted_workflow_state,
        reason=reason,
        actor=execution_settings['actor'],
        replay_source=replay_source,
        details=details,
        preserve_terminal=True,
        event_type=event_type,
        append_event=True,
    )
    persisted_row = db.get_approval_state(approval_id)
    return persisted_row or {}


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
    action_registry = _build_safe_rollout_action_registry(execution_settings.get('allowed_action_types'))

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
        'action_registry': action_registry,
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
        handler = _resolve_safe_rollout_handler(spec, action_type)
        dispatch_mode = (spec or {}).get('dispatch_mode') or handler.get('disposition') or 'unsupported'
        executor_class = (spec or {}).get('executor_class') or handler.get('executor_class') or 'unsupported'
        handler_key = handler.get('handler_key') or f'{dispatch_mode}::{executor_class}'
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
            'safe_handler': handler,
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
            'handler_key': handler_key,
            'handler_route': handler.get('route'),
            'handler_disposition': handler.get('disposition'),
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
            dispatch_code = 'QUEUE_ONLY'
            dispatch_reason = spec.get('blocked_reason') or 'queue_only_action'
            apply_status = 'queued'
            disposition = 'queued'
            item_status = 'queued'
            queue_consumed_row = None
            if queue_status == 'ready_to_queue':
                result['summary']['queued_count'] += 1
            elif queue_status == 'blocked_by_approval':
                dispatch_code = 'APPROVAL_GATED_QUEUE'
                dispatch_reason = approval_hook.get('gate_reason') or 'approval_gated_dispatch'
                apply_status = 'approval_gated'
                disposition = 'blocked_by_approval'
                item_status = 'blocked_by_approval'
                result['summary']['skipped_count'] += 1
            else:
                dispatch_code = 'QUEUE_DEFERRED'
                dispatch_reason = approval_hook.get('gate_reason') or 'queue_deferred'
                apply_status = 'deferred'
                disposition = 'deferred'
                item_status = 'deferred'
                result['summary']['skipped_count'] += 1
            result_row['dispatch'] = _build_rollout_dispatch_envelope(mode=dispatch_mode, executor_class=executor_class, handler_key=handler_key, allowed=True, status=item_status, reason=dispatch_reason, code=dispatch_code, queue_name=queue_plan['queue_name'], dispatch_route=transition_rule.get('dispatch_route'), transition_rule=transition_rule.get('transition_rule'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
            if result['dry_run']:
                result_row['apply'] = _build_rollout_apply_envelope(attempted=True, persisted=False, status='dry_run', operation='queue_plan_consume', idempotency_key=idempotency_key)
                result_row['result'] = _build_rollout_result_envelope(disposition='dry_run' if queue_status == 'ready_to_queue' else disposition, status='dry_run', reason=dispatch_reason, code='DRY_RUN_ONLY', state=current_state, workflow_state=current_workflow_state, transition_rule=transition_rule.get('transition_rule'), dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
                result_row['status'] = 'dry_run'
                result['summary']['dry_run_count'] += 1
                bump('dry_run', 'dry_run')
            else:
                try:
                    queue_consumed_row = _consume_rollout_queue_plan(
                        db=db,
                        row=row,
                        workflow_item=workflow_item,
                        approval_id=approval_id,
                        action_type=action_type,
                        current_state=current_state,
                        current_workflow_state=current_workflow_state,
                        queue_plan=queue_plan,
                        transition_rule=transition_rule,
                        execution_settings=execution_settings,
                        replay_source=replay_source,
                        handler=handler,
                        handler_key=handler_key,
                        dispatch_mode=dispatch_mode,
                        executor_class=executor_class,
                        audit_code=(spec or {}).get('audit_code') or 'QUEUE_ONLY',
                        idempotency_key=idempotency_key,
                    )
                    if queue_consumed_row:
                        executed_rows.append(queue_consumed_row)
                    result_row['apply'] = _build_rollout_apply_envelope(attempted=True, persisted=True, status=apply_status, operation='queue_plan_consume', idempotency_key=idempotency_key, effect_applied=True)
                    result_row['result'] = _build_rollout_result_envelope(disposition=disposition, status=item_status, reason=dispatch_reason, code=dispatch_code, state=(queue_consumed_row or {}).get('state'), workflow_state=(queue_consumed_row or {}).get('workflow_state'), transition_rule=transition_rule.get('transition_rule'), dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
                    result_row['status'] = item_status
                    bump(disposition, item_status)
                except Exception as exc:
                    result_row['apply'] = _build_rollout_apply_envelope(attempted=True, persisted=False, status='error', operation='queue_plan_consume', idempotency_key=idempotency_key)
                    result_row['result'] = _build_rollout_result_envelope(disposition='error', status='error', reason=str(exc), code='QUEUE_CONSUME_EXCEPTION', transition_rule=transition_rule.get('transition_rule'), dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
                    result_row['status'] = 'error'
                    result['summary']['error_count'] += 1
                    bump('error', 'error')
            result['items'].append(result_row)
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
            'safe_handler_route': handler.get('route'),
            'safe_handler_disposition': handler.get('disposition'),
            'safe_handler_stage_family': handler.get('stage_family'),
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
    elif not result['dry_run'] and (result['summary']['applied_count'] or result['summary']['queued_count'] or result['summary']['by_status'].get('blocked_by_approval') or result['summary']['by_status'].get('deferred')):
        result['status'] = 'executed'
    result['stage_progression'] = build_rollout_stage_progression(payload, result)
    return payload



def build_rollout_stage_progression(payload: Optional[Dict] = None, executor: Optional[Dict] = None) -> Dict[str, Any]:
    payload = payload or {}
    executor = executor or (payload.get('rollout_executor') or {})
    workflow_items = ((payload.get('workflow_state') or {}).get('item_states') or [])
    approval_items = ((payload.get('approval_state') or {}).get('items') or [])
    executor_items = executor.get('items') or []

    workflow_lookup = {row.get('item_id'): row for row in workflow_items if row.get('item_id')}
    approval_lookup = {row.get('approval_id') or row.get('item_id'): row for row in approval_items if row.get('approval_id') or row.get('item_id')}

    stage_items = []
    summary = {
        'item_count': 0,
        'applied_count': 0,
        'queued_count': 0,
        'blocked_count': 0,
        'deferred_count': 0,
        'ready_stage_count': 0,
        'by_stage': {},
        'by_next_transition': {},
        'by_dispatch_route': {},
        'by_status': {},
    }

    def bump(bucket: Dict[str, int], key: Optional[str]):
        label = str(key or 'unknown')
        bucket[label] = bucket.get(label, 0) + 1

    for row in executor_items:
        item_id = row.get('item_id')
        workflow_item = workflow_lookup.get(row.get('playbook_id')) or {}
        approval_item = approval_lookup.get(item_id) or {}
        plan = row.get('plan') or {}
        dispatch = row.get('dispatch') or {}
        result = row.get('result') or {}
        rollout_stage = plan.get('rollout_stage') or workflow_item.get('current_rollout_stage') or approval_item.get('rollout_stage') or 'pending'
        target_rollout_stage = plan.get('target_rollout_stage') or workflow_item.get('target_rollout_stage') or approval_item.get('target_rollout_stage') or rollout_stage
        next_transition = plan.get('next_transition') or result.get('next_transition') or dispatch.get('next_transition') or 'unknown'
        dispatch_route = plan.get('dispatch_route') or result.get('dispatch_route') or dispatch.get('dispatch_route') or 'unknown'
        status = row.get('status') or result.get('status') or dispatch.get('status') or 'planned'
        stage_row = {
            'item_id': item_id,
            'playbook_id': row.get('playbook_id'),
            'action_type': row.get('action_type'),
            'status': status,
            'disposition': result.get('disposition') or status,
            'rollout_stage': rollout_stage,
            'target_rollout_stage': target_rollout_stage,
            'stage_progression': {
                'current_stage': rollout_stage,
                'target_stage': target_rollout_stage,
                'dispatch_route': dispatch_route,
                'next_transition': next_transition,
                'transition_rule': plan.get('transition_rule') or result.get('transition_rule') or dispatch.get('transition_rule'),
                'retryable': bool(plan.get('retryable', result.get('retryable', True))),
                'rollback_hint': plan.get('rollback_hint') or result.get('rollback_hint') or dispatch.get('rollback_hint'),
                'readiness': plan.get('readiness') or workflow_item.get('workflow_state') or approval_item.get('workflow_state') or 'pending',
            },
            'queue_plan': plan.get('queue_plan') or {},
            'dispatch': dispatch,
            'result': result,
        }
        stage_items.append(stage_row)
        summary['item_count'] += 1
        if status == 'applied':
            summary['applied_count'] += 1
        elif status == 'queued':
            summary['queued_count'] += 1
        elif status in {'blocked_by_approval', 'blocked'}:
            summary['blocked_count'] += 1
        elif status == 'deferred':
            summary['deferred_count'] += 1
        if target_rollout_stage not in {None, '', 'pending'}:
            summary['ready_stage_count'] += 1
        bump(summary['by_stage'], f"{rollout_stage}->{target_rollout_stage}")
        bump(summary['by_next_transition'], next_transition)
        bump(summary['by_dispatch_route'], dispatch_route)
        bump(summary['by_status'], status)

    return {
        'schema_version': 'm5_rollout_stage_progression_v1',
        'summary': summary,
        'items': stage_items,
    }


def build_workflow_consumer_view(payload: Optional[Dict] = None) -> Dict[str, Any]:
    payload = payload or {}
    workflow_state = payload.get('workflow_state') or {}
    approval_state = payload.get('approval_state') or {}
    queues = payload.get('queues') or {}
    rollout_executor = payload.get('rollout_executor') or {}
    controlled_rollout = payload.get('controlled_rollout_execution') or {}
    auto_approval = payload.get('auto_approval_execution') or {}
    stage_progression = build_rollout_stage_progression(payload, rollout_executor)
    approval_items = approval_state.get('items') or []
    workflow_items = workflow_state.get('item_states') or []
    view = {
        'schema_version': 'm5_workflow_consumer_view_v1',
        'summary': {
            'workflow_item_count': len(workflow_items),
            'approval_item_count': len(approval_items),
            'pending_approval_count': sum(1 for row in approval_items if row.get('approval_state') == 'pending'),
            'ready_workflow_count': sum(1 for row in workflow_items if row.get('workflow_state') == 'ready'),
            'queue_count': sum(len(v or []) for v in queues.values() if isinstance(v, list)),
            'rollout_executor_status': rollout_executor.get('status') or 'disabled',
            'rollout_stage_progression': stage_progression.get('summary') or {},
            'controlled_rollout_executed_count': controlled_rollout.get('executed_count', 0),
            'auto_approval_executed_count': auto_approval.get('executed_count', 0),
        },
        'workflow_state': workflow_state,
        'approval_state': approval_state,
        'queues': queues,
        'rollout_executor': rollout_executor,
        'rollout_stage_progression': stage_progression,
        'controlled_rollout_execution': controlled_rollout,
        'auto_approval_execution': auto_approval,
    }
    payload['consumer_view'] = view
    return view


def build_workflow_attention_view(payload: Optional[Dict] = None, *, max_items: int = 50) -> Dict[str, Any]:
    payload = payload or {}
    consumer_view = payload.get('consumer_view') or build_workflow_consumer_view(payload)
    workflow_items = ((consumer_view.get('workflow_state') or {}).get('item_states') or [])
    approval_items = ((consumer_view.get('approval_state') or {}).get('items') or [])
    rollout_executor = consumer_view.get('rollout_executor') or {}
    auto_approval = consumer_view.get('auto_approval_execution') or {}
    controlled_rollout = consumer_view.get('controlled_rollout_execution') or {}

    approval_by_playbook = {row.get('playbook_id'): row for row in approval_items if row.get('playbook_id')}

    def _sort_key(row: Dict[str, Any]):
        risk_rank = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        return (
            risk_rank.get(str(row.get('risk_level') or '').lower(), 9),
            0 if row.get('requires_manual') else 1,
            0 if row.get('approval_required') else 1,
            str(row.get('title') or row.get('item_id') or ''),
        )

    item_rows = []
    for workflow_item in workflow_items:
        item_id = workflow_item.get('item_id')
        approval_item = approval_by_playbook.get(item_id) or {}
        workflow_state = workflow_item.get('workflow_state') or approval_item.get('workflow_state') or 'pending'
        approval_state = approval_item.get('approval_state') or 'not_required'
        blocked_by = list(dict.fromkeys((workflow_item.get('blocking_reasons') or []) + (approval_item.get('blocked_by') or [])))
        requires_manual = bool(workflow_item.get('requires_manual', approval_item.get('requires_manual')))
        approval_required = bool(workflow_item.get('approval_required', approval_item.get('approval_required')))
        in_manual_bucket = requires_manual and approval_state == 'pending'
        in_blocked_bucket = workflow_state in {'blocked_by_approval', 'blocked', 'deferred'} or bool(blocked_by)
        bucket_tags = []
        if in_manual_bucket:
            bucket_tags.append('manual_approval')
        if in_blocked_bucket:
            bucket_tags.append('blocked_follow_up')
        if not bucket_tags:
            continue
        item_rows.append({
            'item_id': item_id,
            'approval_id': approval_item.get('approval_id'),
            'title': workflow_item.get('title') or approval_item.get('title') or item_id,
            'action_type': workflow_item.get('action_type') or approval_item.get('action_type'),
            'workflow_state': workflow_state,
            'approval_state': approval_state,
            'decision_state': approval_item.get('decision_state') or workflow_item.get('decision_state') or workflow_state,
            'risk_level': workflow_item.get('risk_level') or approval_item.get('risk_level') or 'unknown',
            'requires_manual': requires_manual,
            'approval_required': approval_required,
            'auto_approval_decision': workflow_item.get('auto_approval_decision') or approval_item.get('auto_approval_decision') or 'manual_review',
            'auto_approval_eligible': bool(workflow_item.get('auto_approval_eligible', approval_item.get('auto_approval_eligible'))),
            'blocked_by': blocked_by,
            'blocking_reason_count': len(blocked_by),
            'queue_progression': workflow_item.get('queue_progression') or {},
            'stage_model': workflow_item.get('stage_model') or {},
            'current_rollout_stage': workflow_item.get('current_rollout_stage'),
            'target_rollout_stage': workflow_item.get('target_rollout_stage'),
            'scheduled_review': workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or {},
            'owner_hint': workflow_item.get('owner_hint') or approval_item.get('owner_hint'),
            'bucket_tags': bucket_tags,
            'in_manual_approval_bucket': in_manual_bucket,
            'in_blocked_follow_up_bucket': in_blocked_bucket,
        })

    item_rows.sort(key=_sort_key)

    def _bucket_payload(bucket_id: str, title: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        items = sorted(items, key=_sort_key)
        return {
            'bucket_id': bucket_id,
            'title': title,
            'count': len(items),
            'items': items[:max_items],
            'filters': {
                'workflow_states': sorted({row.get('workflow_state') or 'pending' for row in items}),
                'approval_states': sorted({row.get('approval_state') or 'not_required' for row in items}),
                'risk_levels': sorted({row.get('risk_level') or 'unknown' for row in items}),
                'action_types': sorted({row.get('action_type') or 'unknown' for row in items}),
            },
        }

    manual_items = [row for row in item_rows if row.get('in_manual_approval_bucket')]
    blocked_items = [row for row in item_rows if row.get('in_blocked_follow_up_bucket')]
    by_bucket = {
        'manual_approval': _bucket_payload('manual_approval', 'Needs manual approval', manual_items),
        'blocked_follow_up': _bucket_payload('blocked_follow_up', 'Blocked or deferred follow-up', blocked_items),
    }

    attention = {
        'schema_version': 'm5_workflow_attention_view_v1',
        'headline': {
            'status': 'attention_required' if item_rows else 'steady',
            'message': f"{len(manual_items)} manual approval / {len(blocked_items)} blocked follow-up",
            'rollout_executor_status': rollout_executor.get('status') or 'disabled',
        },
        'summary': {
            'attention_item_count': len(item_rows),
            'manual_approval_count': len(manual_items),
            'blocked_follow_up_count': len(blocked_items),
            'overlap_count': sum(1 for row in item_rows if len(row.get('bucket_tags') or []) > 1),
            'workflow_item_count': len(workflow_items),
            'approval_item_count': len(approval_items),
            'rollout_executor_status': rollout_executor.get('status') or 'disabled',
            'auto_approval_executed_count': auto_approval.get('executed_count', 0),
            'controlled_rollout_executed_count': controlled_rollout.get('executed_count', 0),
        },
        'filters': {
            'bucket_ids': ['manual_approval', 'blocked_follow_up'],
            'workflow_states': sorted({row.get('workflow_state') or 'pending' for row in item_rows}),
            'approval_states': sorted({row.get('approval_state') or 'not_required' for row in item_rows}),
            'risk_levels': sorted({row.get('risk_level') or 'unknown' for row in item_rows}),
            'action_types': sorted({row.get('action_type') or 'unknown' for row in item_rows}),
            'owner_hints': sorted({row.get('owner_hint') for row in item_rows if row.get('owner_hint')}),
            'auto_approval_decisions': sorted({row.get('auto_approval_decision') or 'manual_review' for row in item_rows}),
        },
        'items': item_rows[:max_items],
        'by_bucket': by_bucket,
        'execution': {
            'rollout_executor': {
                'status': rollout_executor.get('status') or 'disabled',
                'summary': rollout_executor.get('summary') or {},
            },
            'auto_approval_execution': auto_approval,
            'controlled_rollout_execution': controlled_rollout,
        },
    }
    payload['attention_view'] = attention
    return attention


def build_workflow_operator_digest(payload: Optional[Dict] = None, *, max_items: int = 5) -> Dict[str, Any]:
    payload = payload or {}
    consumer_view = payload.get('consumer_view') or build_workflow_consumer_view(payload)
    workflow_items = ((consumer_view.get('workflow_state') or {}).get('item_states') or [])
    approval_items = ((consumer_view.get('approval_state') or {}).get('items') or [])
    stage_items = ((consumer_view.get('rollout_stage_progression') or {}).get('items') or [])
    rollout_executor = consumer_view.get('rollout_executor') or {}
    auto_approval = consumer_view.get('auto_approval_execution') or {}
    controlled_rollout = consumer_view.get('controlled_rollout_execution') or {}

    workflow_lookup = {row.get('item_id'): row for row in workflow_items if row.get('item_id')}
    approval_by_playbook = {row.get('playbook_id'): row for row in approval_items if row.get('playbook_id')}

    manual_approval_items = []
    blocked_items = []
    ready_items = []
    queued_items = []
    deferred_items = []
    auto_advance_items = []

    def _sort_key(row: Dict[str, Any]):
        risk_rank = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        return (
            risk_rank.get(str(row.get('risk_level') or '').lower(), 9),
            0 if row.get('requires_manual') else 1,
            0 if row.get('approval_required') else 1,
            str(row.get('title') or row.get('item_id') or ''),
        )

    for workflow_item in workflow_items:
        item_id = workflow_item.get('item_id')
        approval_item = approval_by_playbook.get(item_id) or {}
        workflow_state = workflow_item.get('workflow_state') or 'pending'
        blocked_by = list(dict.fromkeys((workflow_item.get('blocking_reasons') or []) + (approval_item.get('blocked_by') or [])))
        row = {
            'item_id': item_id,
            'approval_id': approval_item.get('approval_id'),
            'title': workflow_item.get('title') or approval_item.get('title') or item_id,
            'action_type': workflow_item.get('action_type') or approval_item.get('action_type'),
            'workflow_state': workflow_state,
            'approval_state': approval_item.get('approval_state') or 'not_required',
            'decision_state': approval_item.get('decision_state') or workflow_item.get('decision_state') or workflow_state,
            'risk_level': workflow_item.get('risk_level') or approval_item.get('risk_level') or 'unknown',
            'requires_manual': bool(workflow_item.get('requires_manual', approval_item.get('requires_manual'))),
            'approval_required': bool(workflow_item.get('approval_required', approval_item.get('approval_required'))),
            'auto_approval_decision': workflow_item.get('auto_approval_decision') or approval_item.get('auto_approval_decision') or 'manual_review',
            'blocked_by': blocked_by,
            'queue_progression': workflow_item.get('queue_progression') or {},
            'stage_model': workflow_item.get('stage_model') or {},
            'current_rollout_stage': workflow_item.get('current_rollout_stage'),
            'target_rollout_stage': workflow_item.get('target_rollout_stage'),
        }
        if row['requires_manual'] and row['approval_state'] == 'pending':
            manual_approval_items.append(row)
        if workflow_state in {'blocked_by_approval', 'blocked', 'deferred'} or blocked_by:
            blocked_items.append(row)
        if workflow_state == 'ready':
            ready_items.append(row)
        if workflow_state == 'queued':
            queued_items.append(row)
        if workflow_state == 'deferred':
            deferred_items.append(row)
        if row['auto_approval_decision'] == 'auto_approve' and not row['requires_manual'] and not blocked_by and workflow_state in {'ready', 'queued'}:
            auto_advance_items.append(row)

    blocked_items.sort(key=_sort_key)
    manual_approval_items.sort(key=_sort_key)
    ready_items.sort(key=_sort_key)
    queued_items.sort(key=_sort_key)
    deferred_items.sort(key=_sort_key)
    auto_advance_items.sort(key=_sort_key)

    next_actions = []
    if manual_approval_items:
        next_actions.append({
            'kind': 'manual_approval',
            'priority': 'high',
            'count': len(manual_approval_items),
            'message': f"{len(manual_approval_items)} item(s) waiting for manual approval",
            'items': manual_approval_items[:max_items],
        })
    if blocked_items:
        next_actions.append({
            'kind': 'blocked_followup',
            'priority': 'high' if manual_approval_items else 'medium',
            'count': len(blocked_items),
            'message': f"{len(blocked_items)} item(s) still blocked or deferred",
            'items': blocked_items[:max_items],
        })
    if ready_items:
        next_actions.append({
            'kind': 'ready_to_consume',
            'priority': 'medium',
            'count': len(ready_items),
            'message': f"{len(ready_items)} item(s) already ready for rollout/governance consumption",
            'items': ready_items[:max_items],
        })
    if queued_items:
        next_actions.append({
            'kind': 'queued_watch',
            'priority': 'medium',
            'count': len(queued_items),
            'message': f"{len(queued_items)} item(s) are queued and should be watched for progression",
            'items': queued_items[:max_items],
        })

    headline_status = 'attention_required' if manual_approval_items or blocked_items else 'steady'
    if ready_items and not manual_approval_items and not blocked_items:
        headline_status = 'ready_to_consume'

    digest = {
        'schema_version': 'm5_workflow_operator_digest_v1',
        'headline': {
            'status': headline_status,
            'message': (
                f"{len(manual_approval_items)} manual approval / {len(blocked_items)} blocked / {len(ready_items)} ready / {len(queued_items)} queued"
            ),
            'rollout_executor_status': rollout_executor.get('status') or 'disabled',
        },
        'summary': {
            'workflow_item_count': len(workflow_items),
            'approval_item_count': len(approval_items),
            'manual_approval_count': len(manual_approval_items),
            'blocked_count': len(blocked_items),
            'ready_count': len(ready_items),
            'queued_count': len(queued_items),
            'deferred_count': len(deferred_items),
            'auto_advance_candidate_count': len(auto_advance_items),
            'rollout_executor_status': rollout_executor.get('status') or 'disabled',
            'auto_approval_executed_count': auto_approval.get('executed_count', 0),
            'controlled_rollout_executed_count': controlled_rollout.get('executed_count', 0),
            'stage_progression': (consumer_view.get('rollout_stage_progression') or {}).get('summary') or {},
        },
        'attention': {
            'manual_approval': manual_approval_items[:max_items],
            'blocked': blocked_items[:max_items],
            'queued': queued_items[:max_items],
            'ready': ready_items[:max_items],
            'auto_advance_candidates': auto_advance_items[:max_items],
        },
        'next_actions': next_actions,
        'execution': {
            'rollout_executor': {
                'status': rollout_executor.get('status') or 'disabled',
                'summary': rollout_executor.get('summary') or {},
            },
            'auto_approval_execution': auto_approval,
            'controlled_rollout_execution': controlled_rollout,
        },
        'stage_progression': {
            'summary': (consumer_view.get('rollout_stage_progression') or {}).get('summary') or {},
            'items': stage_items[:max_items],
        },
    }
    payload['operator_digest'] = digest
    return digest

def build_dashboard_summary_cards(payload: Optional[Dict] = None, *, max_items: int = 3) -> Dict[str, Any]:
    payload = payload or {}
    consumer_view = payload.get('consumer_view') or build_workflow_consumer_view(payload)
    attention_view = payload.get('attention_view') or build_workflow_attention_view(payload, max_items=max_items)
    operator_digest = payload.get('operator_digest') or build_workflow_operator_digest(payload, max_items=max_items)

    workflow_summary = (consumer_view.get('workflow_state') or {}).get('summary') or {}
    approval_summary = (consumer_view.get('approval_state') or {}).get('summary') or {}
    stage_summary = (consumer_view.get('rollout_stage_progression') or {}).get('summary') or {}
    rollout_executor = consumer_view.get('rollout_executor') or {}
    auto_approval = consumer_view.get('auto_approval_execution') or {}
    controlled_rollout = consumer_view.get('controlled_rollout_execution') or {}
    digest_summary = operator_digest.get('summary') or {}
    attention_summary = attention_view.get('summary') or {}

    cards = [
        {
            'card_id': 'workflow_overview',
            'title': 'Workflow overview',
            'status': operator_digest.get('headline', {}).get('status') or 'steady',
            'headline': operator_digest.get('headline', {}).get('message') or '',
            'metrics': {
                'manual': digest_summary.get('manual_approval_count', 0),
                'blocked': digest_summary.get('blocked_count', 0),
                'ready': digest_summary.get('ready_count', 0),
                'queued': digest_summary.get('queued_count', 0),
                'deferred': digest_summary.get('deferred_count', 0),
                'auto_advance': digest_summary.get('auto_advance_candidate_count', 0),
            },
            'highlights': [
                f"workflow={digest_summary.get('workflow_item_count', 0)}",
                f"approvals={digest_summary.get('approval_item_count', 0)}",
                f"pending={approval_summary.get('pending_count', 0)}",
            ],
        },
        {
            'card_id': 'key_alerts',
            'title': 'Key alerts',
            'status': 'attention_required' if attention_summary.get('attention_item_count', 0) else 'steady',
            'headline': attention_view.get('headline', {}).get('message') or 'No manual/blocking alerts',
            'metrics': {
                'attention': attention_summary.get('attention_item_count', 0),
                'manual': attention_summary.get('manual_approval_count', 0),
                'blocked': attention_summary.get('blocked_follow_up_count', 0),
                'overlap': attention_summary.get('overlap_count', 0),
            },
            'items': attention_view.get('items') or [],
        },
        {
            'card_id': 'next_actions',
            'title': 'Next actions',
            'status': 'attention_required' if operator_digest.get('next_actions') else 'steady',
            'headline': f"{len(operator_digest.get('next_actions') or [])} action lane(s)" if operator_digest.get('next_actions') else 'No pending next action lanes',
            'metrics': {
                'lane_count': len(operator_digest.get('next_actions') or []),
                'top_priority_count': sum(1 for row in (operator_digest.get('next_actions') or []) if row.get('priority') == 'high'),
            },
            'items': operator_digest.get('next_actions') or [],
        },
        {
            'card_id': 'execution_status',
            'title': 'Executor / bridge status',
            'status': rollout_executor.get('status') or 'disabled',
            'headline': f"executor={rollout_executor.get('status') or 'disabled'} / auto={auto_approval.get('mode') or 'disabled'} / bridge={controlled_rollout.get('mode') or 'disabled'}",
            'metrics': {
                'executor_items': len(rollout_executor.get('items') or []),
                'executor_applied': stage_summary.get('applied_count', 0),
                'executor_blocked': stage_summary.get('blocked_count', 0),
                'executor_queued': stage_summary.get('queued_count', 0),
                'bridge_executed': controlled_rollout.get('executed_count', 0),
                'bridge_skipped': controlled_rollout.get('skipped_count', 0),
                'auto_executed': auto_approval.get('executed_count', 0),
                'auto_skipped': auto_approval.get('skipped_count', 0),
            },
            'details': {
                'rollout_executor': {
                    'status': rollout_executor.get('status') or 'disabled',
                    'summary': rollout_executor.get('summary') or {},
                },
                'controlled_rollout_execution': controlled_rollout,
                'auto_approval_execution': auto_approval,
            },
        },
        {
            'card_id': 'stage_progression',
            'title': 'Rollout stage progression',
            'status': 'attention_required' if stage_summary.get('blocked_count', 0) else ('in_progress' if stage_summary.get('queued_count', 0) else 'steady'),
            'headline': f"applied={stage_summary.get('applied_count', 0)} / queued={stage_summary.get('queued_count', 0)} / blocked={stage_summary.get('blocked_count', 0)}",
            'metrics': {
                'item_count': stage_summary.get('item_count', 0),
                'ready_stage_count': stage_summary.get('ready_stage_count', 0),
                'applied': stage_summary.get('applied_count', 0),
                'queued': stage_summary.get('queued_count', 0),
                'blocked': stage_summary.get('blocked_count', 0),
                'deferred': stage_summary.get('deferred_count', 0),
            },
            'items': (consumer_view.get('rollout_stage_progression') or {}).get('items') or [],
        },
    ]

    payload_cards = {card['card_id']: card for card in cards}
    summary_cards = {
        'schema_version': 'm5_dashboard_summary_cards_v1',
        'headline': operator_digest.get('headline') or {},
        'summary': {
            'card_count': len(cards),
            'workflow_item_count': digest_summary.get('workflow_item_count', 0),
            'approval_item_count': digest_summary.get('approval_item_count', 0),
            'manual_approval_count': digest_summary.get('manual_approval_count', 0),
            'blocked_count': digest_summary.get('blocked_count', 0),
            'ready_count': digest_summary.get('ready_count', 0),
            'queued_count': digest_summary.get('queued_count', 0),
            'deferred_count': digest_summary.get('deferred_count', 0),
            'auto_advance_candidate_count': digest_summary.get('auto_advance_candidate_count', 0),
            'executor_status': rollout_executor.get('status') or 'disabled',
            'bridge_mode': controlled_rollout.get('mode') or 'disabled',
            'auto_approval_mode': auto_approval.get('mode') or 'disabled',
            'attention_item_count': attention_summary.get('attention_item_count', 0),
            'pending_approval_count': approval_summary.get('pending_count', 0),
            'approval_roles': approval_summary.get('roles') or [],
            'stage_progression': stage_summary,
            'workflow_state_summary': workflow_summary,
            'approval_state_summary': approval_summary,
        },
        'cards': cards,
        'card_index': payload_cards,
        'key_alerts': attention_view.get('items') or [],
        'next_actions': operator_digest.get('next_actions') or [],
        'attention': operator_digest.get('attention') or {},
        'execution': {
            'rollout_executor': {
                'status': rollout_executor.get('status') or 'disabled',
                'summary': rollout_executor.get('summary') or {},
            },
            'controlled_rollout_execution': controlled_rollout,
            'auto_approval_execution': auto_approval,
        },
        'stage_progression': consumer_view.get('rollout_stage_progression') or {},
        'workflow_consumer_view': consumer_view,
        'workflow_attention_view': attention_view,
        'workflow_operator_digest': operator_digest,
    }
    payload['dashboard_summary_cards'] = summary_cards
    return summary_cards


def build_workbench_governance_view(payload: Optional[Dict] = None, *, max_items: int = 5,
                                    max_adjustments: int = 10) -> Dict[str, Any]:
    payload = payload or {}
    consumer_view = payload.get('consumer_view') or build_workflow_consumer_view(payload)
    attention_view = payload.get('attention_view') or build_workflow_attention_view(payload, max_items=max_items)
    operator_digest = payload.get('operator_digest') or build_workflow_operator_digest(payload, max_items=max_items)
    stage_progression = consumer_view.get('rollout_stage_progression') or {}
    stage_summary = stage_progression.get('summary') or {}
    rollout_executor = consumer_view.get('rollout_executor') or {}
    auto_approval = consumer_view.get('auto_approval_execution') or {}
    controlled_rollout = consumer_view.get('controlled_rollout_execution') or {}
    workflow_items = ((consumer_view.get('workflow_state') or {}).get('item_states') or [])
    approval_items = ((consumer_view.get('approval_state') or {}).get('items') or [])

    workflow_lookup = {row.get('item_id'): row for row in workflow_items if row.get('item_id')}
    approval_by_playbook = {row.get('playbook_id'): row for row in approval_items if row.get('playbook_id')}

    def _item_snapshot(row: Dict[str, Any]) -> Dict[str, Any]:
        item_id = row.get('item_id') or row.get('playbook_id')
        workflow_item = workflow_lookup.get(item_id) or row
        approval_item = approval_by_playbook.get(item_id) or {}
        blocked_by = list(dict.fromkeys((workflow_item.get('blocking_reasons') or []) + (approval_item.get('blocked_by') or []) + (row.get('blocked_by') or [])))
        return {
            'item_id': item_id,
            'approval_id': approval_item.get('approval_id') or row.get('approval_id'),
            'title': workflow_item.get('title') or approval_item.get('title') or row.get('title') or item_id,
            'action_type': workflow_item.get('action_type') or approval_item.get('action_type') or row.get('action_type'),
            'workflow_state': workflow_item.get('workflow_state') or approval_item.get('workflow_state') or row.get('workflow_state') or 'pending',
            'approval_state': approval_item.get('approval_state') or row.get('approval_state') or 'not_required',
            'risk_level': workflow_item.get('risk_level') or approval_item.get('risk_level') or row.get('risk_level') or 'unknown',
            'auto_approval_decision': workflow_item.get('auto_approval_decision') or approval_item.get('auto_approval_decision') or row.get('auto_approval_decision') or 'manual_review',
            'auto_approval_eligible': bool(workflow_item.get('auto_approval_eligible', approval_item.get('auto_approval_eligible', row.get('auto_approval_eligible')))),
            'requires_manual': bool(workflow_item.get('requires_manual', approval_item.get('requires_manual', row.get('requires_manual')))),
            'approval_required': bool(workflow_item.get('approval_required', approval_item.get('approval_required', row.get('approval_required')))),
            'blocked_by': blocked_by,
            'queue_progression': workflow_item.get('queue_progression') or row.get('queue_progression') or {},
            'current_rollout_stage': workflow_item.get('current_rollout_stage') or row.get('current_rollout_stage'),
            'target_rollout_stage': workflow_item.get('target_rollout_stage') or row.get('target_rollout_stage'),
        }

    def _sort_key(row: Dict[str, Any]):
        risk_rank = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        return (
            risk_rank.get(str(row.get('risk_level') or '').lower(), 9),
            0 if row.get('requires_manual') else 1,
            0 if row.get('approval_required') else 1,
            str(row.get('title') or row.get('item_id') or ''),
        )

    ready_items = sorted((_item_snapshot(row) for row in (operator_digest.get('attention') or {}).get('ready') or []), key=_sort_key)
    queued_items = sorted((_item_snapshot(row) for row in (operator_digest.get('attention') or {}).get('queued') or []), key=_sort_key)
    blocked_items = sorted((_item_snapshot(row) for row in (operator_digest.get('attention') or {}).get('blocked') or []), key=_sort_key)
    auto_batch_items = sorted((_item_snapshot(row) for row in (operator_digest.get('attention') or {}).get('auto_advance_candidates') or []), key=_sort_key)
    manual_items = sorted((_item_snapshot(row) for row in (operator_digest.get('attention') or {}).get('manual_approval') or []), key=_sort_key)

    stage_frontier = []
    for key, count in sorted((stage_summary.get('by_stage') or {}).items(), key=lambda item: (-item[1], item[0]))[:max_items]:
        current_stage, target_stage = (key.split('->', 1) + ['unknown'])[:2]
        stage_frontier.append({
            'stage_path': key,
            'current_stage': current_stage,
            'target_stage': target_stage,
            'count': count,
        })

    recent_adjustments = []

    def _push_adjustment(source: str, rows: List[Dict[str, Any]], *, status_field: str = 'action'):
        for row in rows or []:
            status = row.get(status_field) or row.get('status') or row.get('disposition')
            if status in {'skipped', 'planned', 'dry_run', 'disabled', None, ''}:
                continue
            snapshot = _item_snapshot({
                'item_id': row.get('playbook_id') or row.get('item_id'),
                'approval_id': row.get('item_id'),
                'title': row.get('title'),
                'action_type': row.get('action_type'),
                'workflow_state': row.get('workflow_state'),
                'approval_state': row.get('state'),
                'current_rollout_stage': row.get('rollout_stage'),
                'target_rollout_stage': row.get('target_rollout_stage'),
            })
            recent_adjustments.append({
                'source': source,
                'item_id': snapshot.get('item_id'),
                'approval_id': snapshot.get('approval_id'),
                'title': snapshot.get('title'),
                'action_type': snapshot.get('action_type'),
                'status': status,
                'state': row.get('state') or snapshot.get('approval_state'),
                'workflow_state': row.get('workflow_state') or snapshot.get('workflow_state'),
                'current_rollout_stage': row.get('rollout_stage') or snapshot.get('current_rollout_stage'),
                'target_rollout_stage': row.get('target_rollout_stage') or snapshot.get('target_rollout_stage'),
                'reason': row.get('reason'),
            })

    _push_adjustment('auto_approval_execution', auto_approval.get('items') or [])
    _push_adjustment('controlled_rollout_execution', controlled_rollout.get('items') or [])
    _push_adjustment('rollout_executor', rollout_executor.get('items') or [], status_field='status')

    def _bucket(bucket_id: str, title: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            'bucket_id': bucket_id,
            'title': title,
            'count': len(items),
            'items': items[:max_items],
            'filters': {
                'workflow_states': sorted({row.get('workflow_state') or 'pending' for row in items}),
                'approval_states': sorted({row.get('approval_state') or 'not_required' for row in items}),
                'risk_levels': sorted({row.get('risk_level') or 'unknown' for row in items}),
                'action_types': sorted({row.get('action_type') or 'unknown' for row in items}),
            },
        }

    view = {
        'schema_version': 'm5_workbench_governance_view_v1',
        'headline': {
            'status': operator_digest.get('headline', {}).get('status') or 'steady',
            'message': operator_digest.get('headline', {}).get('message') or '',
            'rollout_executor_status': rollout_executor.get('status') or 'disabled',
        },
        'summary': {
            'workflow_item_count': len(workflow_items),
            'approval_item_count': len(approval_items),
            'auto_batch_count': len(auto_batch_items),
            'manual_approval_count': len(manual_items),
            'blocked_count': len(blocked_items),
            'queued_count': len(queued_items),
            'ready_count': len(ready_items),
            'recent_adjustment_count': len(recent_adjustments),
            'rollout_executor_status': rollout_executor.get('status') or 'disabled',
            'stage_progression': stage_summary,
        },
        'lanes': {
            'auto_batch': _bucket('auto_batch', 'Auto-batch candidates', auto_batch_items),
            'blocked': _bucket('blocked', 'Blocked follow-up', blocked_items),
            'queued': _bucket('queued', 'Queued items', queued_items),
            'ready': _bucket('ready', 'Ready items', ready_items),
            'manual_approval': _bucket('manual_approval', 'Manual approval items', manual_items),
        },
        'rollout': {
            'summary': stage_summary,
            'frontier': stage_frontier,
            'items': (stage_progression.get('items') or [])[:max_items],
        },
        'recent_adjustments': recent_adjustments[:max_adjustments],
        'upstreams': {
            'workflow_consumer_view': consumer_view,
            'workflow_attention_view': attention_view,
            'workflow_operator_digest': operator_digest,
        },
    }
    payload['workbench_governance_view'] = view
    return view


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
