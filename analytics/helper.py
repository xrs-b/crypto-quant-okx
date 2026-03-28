"""Approval/workflow persistence helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any


TERMINAL_APPROVAL_STATES = {'approved', 'rejected', 'deferred', 'expired'}
AUTO_APPROVAL_DECISIONS = {'auto_approve', 'manual_review', 'freeze', 'defer'}

WORKFLOW_TERMINAL_STATES = {'approved', 'rejected', 'deferred', 'expired'}
WORKFLOW_ACTIVE_STATES = {'pending', 'ready', 'queued', 'blocked', 'blocked_by_approval', 'review_pending', 'executing', 'execution_failed', 'retry_pending', 'rollback_pending', 'rolled_back'}
WORKFLOW_ALLOWED_STATES = WORKFLOW_TERMINAL_STATES | WORKFLOW_ACTIVE_STATES
WORKFLOW_QUEUE_STATE_TO_WORKFLOW_STATE = {
    'ready_to_queue': 'queued',
    'blocked_by_approval': 'blocked_by_approval',
    'deferred': 'deferred',
    'terminal': 'approved',
}
EXECUTOR_RESULT_STATUS_TO_WORKFLOW_STATE = {
    'planned': 'pending',
    'dispatch_ready': 'ready',
    'applied': 'ready',
    'queued': 'queued',
    'blocked_by_approval': 'blocked_by_approval',
    'deferred': 'deferred',
    'error': 'execution_failed',
    'dry_run': 'pending',
    'disabled': 'pending',
    'skipped': 'pending',
}
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

ROLLOUT_TRANSITION_POLICY_VERSION = 'm5_rollout_transition_policy_v1'
ROLLOUT_ACTION_REGISTRY_VERSION = 'm5_safe_rollout_action_registry_v1'
ROLLOUT_STAGE_HANDLER_REGISTRY_VERSION = 'm5_rollout_stage_handler_registry_v1'
ROLLOUT_GATE_POLICY_VERSION = 'm5_rollout_gate_policy_v1'
ROLLOUT_CONTROL_PLANE_MANIFEST_VERSION = 'm5_rollout_control_plane_manifest_v1'

ROLLOUT_TRANSITION_TEMPLATES = {
    'preserve_terminal_state': {
        'dispatch_route': 'terminal_hold',
        'next_transition': 'preserve_terminal_state',
        'retryable': False,
        'rollback_hint': 'terminal_state_preserved_no_further_executor_apply',
    },
    'defer_until_blockers_clear': {
        'dispatch_route': 'deferred_review_queue',
        'next_transition': 'retry_after_blockers_clear',
        'retryable': True,
        'rollback_hint': 'keep_current_state_and_clear_blockers_before_retry',
    },
    'defer_or_freeze_by_policy': {
        'dispatch_route': 'deferred_hold_queue',
        'next_transition': 'manual_review_or_policy_refresh',
        'retryable': True,
        'rollback_hint': 'revert_to_observe_or_hold_until_next_review',
    },
    'manual_gate_before_dispatch': {
        'dispatch_route': 'manual_review_queue',
        'next_transition': 'await_manual_approval',
        'retryable': True,
        'rollback_hint': 'revert_to_previous_stage_if_manual_review_rejects',
    },
    'queue_only_followup_required': {
        'dispatch_route': 'operator_followup_queue',
        'next_transition': 'queue_for_followup_execution',
        'retryable': True,
        'rollback_hint': 'cancel_or_deprioritize_queue_item_to_rollback',
    },
    'safe_apply_ready': {
        'dispatch_route': 'safe_state_apply',
        'next_transition': 'mark_ready_for_followup',
        'retryable': True,
        'rollback_hint': 'restore_previous_state_from_approval_timeline',
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

ROLLOUT_STAGE_HANDLER_SPECS = {
    'observe': {
        'owner': 'system',
        'auto_progression': True,
        'enter_conditions': ['item_created', 'baseline_capture_ready'],
        'stay_conditions': ['monitor_samples', 'collect_observability'],
        'exit_conditions': ['readiness:ready', 'risk:low', 'auto_approval:auto_approve'],
        'review_due_strategy': 'promote_when_ready',
        'rollback_stage': 'observe',
    },
    'candidate': {
        'owner': 'system',
        'auto_progression': True,
        'enter_conditions': ['candidate_selected', 'sample_window_open'],
        'stay_conditions': ['await_more_samples', 'track_blockers'],
        'exit_conditions': ['queue_promoted', 'no_blockers'],
        'review_due_strategy': 'stay_candidate_until_review_ready',
        'rollback_stage': 'observe',
    },
    'guarded_prepare': {
        'owner': 'system',
        'auto_progression': True,
        'enter_conditions': ['stage_prepare_safe', 'metadata_apply_ready'],
        'stay_conditions': ['await_controlled_apply_window', 'preserve_idempotency'],
        'exit_conditions': ['controlled_apply_authorized', 'review_window_open'],
        'review_due_strategy': 'hold_until_controlled_apply_or_review',
        'rollback_stage': 'observe',
    },
    'controlled_apply': {
        'owner': 'system',
        'auto_progression': True,
        'enter_conditions': ['controlled_apply_started', 'safe_boundary_confirmed'],
        'stay_conditions': ['collect_post_apply_samples', 'watch_transition_journal'],
        'exit_conditions': ['review_checkpoint_scheduled', 'samples_collected'],
        'review_due_strategy': 'move_to_review_pending',
        'rollback_stage': 'guarded_prepare',
    },
    'review_pending': {
        'owner': 'operator',
        'auto_progression': False,
        'enter_conditions': ['review_scheduled'],
        'stay_conditions': ['await_review_checkpoint', 'compare_post_apply_metrics'],
        'exit_conditions': ['review_passed', 'manual_ack_or_policy_refresh'],
        'review_due_strategy': 'manual_review_required',
        'rollback_stage': 'rollback_prepare',
    },
    'rollback_prepare': {
        'owner': 'operator',
        'auto_progression': False,
        'enter_conditions': ['rollback_candidate', 'rollback_gate_triggered'],
        'stay_conditions': ['freeze_further_progression', 'prepare_recovery_context'],
        'exit_conditions': ['rollback_executed', 'manual_recovery_complete'],
        'review_due_strategy': 'immediate_manual_escalation',
        'rollback_stage': 'observe',
    },
}

ROLLOUT_EXECUTOR_ACTION_SPECS = {
    'joint_observe': {
        **CONTROLLED_ROLLOUT_ACTION_SPECS['joint_observe'],
        'dispatch_mode': 'apply',
        'executor_class': 'state_transition',
        'audit_code': 'SAFE_STATE_APPLY',
        'rollback_capable': True,
        'transition_policy': {
            'transition_rule': 'safe_apply_ready',
            'dispatch_route': 'safe_state_apply',
            'next_transition': 'mark_ready_for_followup',
            'retryable': True,
            'rollback_hint': 'restore_previous_state_from_approval_timeline',
        },
    },
    'joint_queue_promote_safe': {
        **CONTROLLED_ROLLOUT_ACTION_SPECS['joint_queue_promote_safe'],
        'dispatch_mode': 'apply',
        'executor_class': 'queue_metadata',
        'audit_code': 'SAFE_QUEUE_PROMOTE',
        'rollback_capable': True,
        'transition_policy': {
            'transition_rule': 'queue_promotion_ready_for_safe_apply',
            'dispatch_route': 'queue_metadata_apply',
            'next_transition': 'queue_safe_promotion',
            'retryable': True,
            'rollback_hint': 'demote_queue_priority_and_restore_previous_progression',
            'default_target_stage': 'candidate',
            'use_stage_handler_next_transition': True,
        },
    },
    'joint_stage_prepare': {
        **CONTROLLED_ROLLOUT_ACTION_SPECS['joint_stage_prepare'],
        'dispatch_mode': 'apply',
        'executor_class': 'stage_metadata',
        'audit_code': 'SAFE_STAGE_PREPARE',
        'rollback_capable': True,
        'transition_policy': {
            'transition_rule': 'stage_prepare_ready_for_safe_apply',
            'dispatch_route': 'stage_metadata_apply',
            'next_transition': 'promote_to_target_stage',
            'retryable': True,
            'rollback_hint': 'revert_stage_metadata_to_previous_stage',
        },
    },
    'joint_review_schedule': {
        **CONTROLLED_ROLLOUT_ACTION_SPECS['joint_review_schedule'],
        'dispatch_mode': 'apply',
        'executor_class': 'review_metadata',
        'audit_code': 'SAFE_REVIEW_SCHEDULE',
        'rollback_capable': True,
        'transition_policy': {
            'transition_rule': 'schedule_review_checkpoint',
            'dispatch_route': 'review_metadata_apply',
            'next_transition': 'wait_for_review_checkpoint',
            'retryable': True,
            'rollback_hint': 'clear_scheduled_review_and_return_to_previous_stage',
            'default_target_stage': 'review_pending',
            'use_stage_handler_next_transition': True,
        },
    },
    'joint_metadata_annotate': {
        **CONTROLLED_ROLLOUT_ACTION_SPECS['joint_metadata_annotate'],
        'dispatch_mode': 'apply',
        'executor_class': 'annotation_metadata',
        'audit_code': 'SAFE_METADATA_ANNOTATE',
        'rollback_capable': True,
        'transition_policy': {
            'transition_rule': 'safe_apply_ready',
            'dispatch_route': 'metadata_annotation_apply',
            'next_transition': 'persist_annotation_and_continue_review',
            'retryable': True,
            'rollback_hint': 'remove_or_replace_metadata_annotation_from_approval_timeline',
        },
    },
    'joint_expand_guarded': {
        'dispatch_mode': 'queue_only',
        'executor_class': 'live_trading_change',
        'audit_code': 'QUEUE_ONLY_GUARDED_EXPAND',
        'blocked_reason': 'live_rollout_parameter_change_not_supported',
        'requires_approval': True,
        'rollback_capable': False,
        'safe_handler': 'queue_only_live_trading_change',
        'transition_policy': {
            'transition_rule': 'queue_only_followup_required',
            'dispatch_route': 'stage_promotion_queue',
            'next_transition': 'queue_for_followup_execution',
            'retryable': True,
            'rollback_hint': 'cancel_or_deprioritize_queue_item_to_rollback',
        },
    },
    'joint_freeze': {
        'dispatch_mode': 'queue_only',
        'executor_class': 'governance_control',
        'audit_code': 'QUEUE_ONLY_FREEZE',
        'blocked_reason': 'freeze_apply_not_automated_in_executor',
        'requires_approval': True,
        'rollback_capable': False,
        'safe_handler': 'queue_only_governance_control',
        'transition_policy': {
            'transition_rule': 'queue_only_followup_required',
            'dispatch_route': 'operator_followup_queue',
            'next_transition': 'queue_for_followup_execution',
            'retryable': True,
            'rollback_hint': 'cancel_or_deprioritize_queue_item_to_rollback',
        },
    },
    'joint_deweight': {
        'dispatch_mode': 'queue_only',
        'executor_class': 'strategy_weight_change',
        'audit_code': 'QUEUE_ONLY_DEWEIGHT',
        'blocked_reason': 'strategy_weight_change_not_automated_in_executor',
        'requires_approval': True,
        'rollback_capable': False,
        'safe_handler': 'queue_only_strategy_weight_change',
        'transition_policy': {
            'transition_rule': 'queue_only_followup_required',
            'dispatch_route': 'operator_followup_queue',
            'next_transition': 'queue_for_followup_execution',
            'retryable': True,
            'rollback_hint': 'cancel_or_deprioritize_queue_item_to_rollback',
        },
    },
    'prefer_strategy_best_policy': {
        'dispatch_mode': 'queue_only',
        'executor_class': 'policy_switch',
        'audit_code': 'QUEUE_ONLY_POLICY_SWITCH',
        'blocked_reason': 'policy_switch_not_automated_in_executor',
        'requires_approval': True,
        'rollback_capable': False,
        'safe_handler': 'queue_only_policy_switch',
        'transition_policy': {
            'transition_rule': 'queue_only_followup_required',
            'dispatch_route': 'operator_followup_queue',
            'next_transition': 'queue_for_followup_execution',
            'retryable': True,
            'rollback_hint': 'cancel_or_deprioritize_queue_item_to_rollback',
        },
    },
    'rollout_freeze': {
        'dispatch_mode': 'queue_only',
        'executor_class': 'rollout_control',
        'audit_code': 'QUEUE_ONLY_ROLLOUT_FREEZE',
        'blocked_reason': 'rollout_freeze_not_automated_in_executor',
        'requires_approval': True,
        'rollback_capable': False,
        'safe_handler': 'queue_only_rollout_control',
        'transition_policy': {
            'transition_rule': 'queue_only_followup_required',
            'dispatch_route': 'operator_followup_queue',
            'next_transition': 'queue_for_followup_execution',
            'retryable': True,
            'rollback_hint': 'cancel_or_deprioritize_queue_item_to_rollback',
        },
    },
}


def _normalize_workflow_state(value: Optional[str], *, approval_state: Optional[str] = None, queue_status: Optional[str] = None, executor_status: Optional[str] = None) -> str:
    normalized = str(value or '').strip().lower()
    if normalized in WORKFLOW_ALLOWED_STATES:
        return normalized
    normalized_approval = str(approval_state or '').strip().lower()
    if normalized_approval in WORKFLOW_TERMINAL_STATES:
        return normalized_approval
    normalized_queue = str(queue_status or '').strip().lower()
    if normalized_queue in WORKFLOW_QUEUE_STATE_TO_WORKFLOW_STATE:
        return WORKFLOW_QUEUE_STATE_TO_WORKFLOW_STATE[normalized_queue]
    normalized_executor = str(executor_status or '').strip().lower()
    if normalized_executor in EXECUTOR_RESULT_STATUS_TO_WORKFLOW_STATE:
        return EXECUTOR_RESULT_STATUS_TO_WORKFLOW_STATE[normalized_executor]
    return 'pending'


ACTION_EXECUTION_ALLOWED_STATUSES = {'queued', 'dispatching', 'applied', 'skipped', 'blocked', 'deferred', 'error', 'recovered', 'disabled', 'dry_run', 'planned'}
ACTION_EXECUTION_TERMINAL_STATUSES = {'applied', 'skipped', 'error', 'recovered', 'disabled'}


def _build_execution_timeline(*, item_id: Optional[str] = None, execution_status: Optional[str] = None,
                              previous_execution_status: Optional[str] = None, workflow_state: Optional[str] = None,
                              queue_status: Optional[str] = None, dispatch_route: Optional[str] = None,
                              next_transition: Optional[str] = None, transition_rule: Optional[str] = None,
                              retryable: Optional[bool] = None, rollback_hint: Optional[str] = None,
                              recovered_from_execution_status: Optional[str] = None,
                              last_transition: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    normalized_status = str(execution_status or 'planned').strip().lower() or 'planned'
    previous_status = str(previous_execution_status or recovered_from_execution_status or '').strip().lower() or None
    transition = dict(last_transition or {}) if isinstance(last_transition, dict) else {}
    if not previous_status:
        previous_status = str(transition.get('from_execution_status') or '').strip().lower() or None
    if normalized_status == 'recovered' and not previous_status:
        previous_status = 'error'
    track = []
    if previous_status and previous_status != normalized_status:
        track.append(previous_status)
    track.append(normalized_status)
    track = _dedupe_strings(track)
    attempts = 1
    if normalized_status in {'error', 'recovered'} and previous_status:
        attempts = 2
    if normalized_status == 'recovered' and previous_status in {'blocked', 'deferred'}:
        attempts = max(attempts, 2)
    recovery_stage = 'steady'
    if normalized_status == 'error':
        recovery_stage = 'retry_pending' if bool(retryable) else 'manual_recovery_required'
    elif normalized_status == 'recovered':
        recovery_stage = 'recovered'
    elif normalized_status in {'blocked', 'deferred'}:
        recovery_stage = 'blocked_pending_recovery'
    elif normalized_status == 'queued' and previous_status in {'error', 'blocked', 'deferred'}:
        recovery_stage = 'retry_scheduled'
    return {
        'schema_version': 'm5_execution_timeline_v1',
        'item_id': item_id,
        'latest_status': normalized_status,
        'previous_status': previous_status,
        'statuses': track,
        'attempt_count': attempts,
        'retry_count': max(attempts - 1, 0),
        'retryable': bool(retryable),
        'recovered': normalized_status == 'recovered',
        'recovered_from_status': recovered_from_execution_status or (previous_status if normalized_status == 'recovered' else None),
        'dispatch_route': dispatch_route,
        'queue_status': queue_status,
        'workflow_state': workflow_state,
        'transition_rule': transition_rule,
        'next_transition': next_transition,
        'rollback_hint': rollback_hint,
        'recovery_stage': recovery_stage,
        'last_transition': transition,
        'summary': f"{normalized_status} via {dispatch_route or queue_status or workflow_state or 'unknown_route'}",
    }


def _build_recovery_policy(*, item_id: Optional[str] = None, workflow_state: Optional[str] = None,
                           execution_status: Optional[str] = None, retryable: Optional[bool] = None,
                           rollback_candidate: Optional[bool] = None, rollback_hint: Optional[str] = None,
                           blocked_by: Optional[List[str]] = None, next_transition: Optional[str] = None,
                           dispatch_route: Optional[str] = None, last_transition: Optional[Dict[str, Any]] = None,
                           recovered_from_execution_status: Optional[str] = None) -> Dict[str, Any]:
    normalized_workflow = str(workflow_state or 'pending').strip().lower() or 'pending'
    normalized_execution = str(execution_status or 'planned').strip().lower() or 'planned'
    blockers = _dedupe_strings(blocked_by or [])
    retry_flag = bool(retryable)
    rollback_flag = bool(rollback_candidate or rollback_hint)
    policy = 'observe'
    action = 'observe_only_followup'
    owner = 'runtime'
    if normalized_execution == 'recovered':
        policy = 'recovered_monitoring'
        action = 'observe_only_followup'
    elif normalized_execution == 'error' or normalized_workflow in {'execution_failed', 'retry_pending'}:
        policy = 'retry' if retry_flag else 'manual_recovery'
        action = 'retry' if retry_flag else 'escalate'
        owner = 'runtime' if retry_flag else 'operator'
    elif normalized_workflow == 'rollback_pending':
        policy = 'rollback_candidate'
        action = 'freeze_followup'
        owner = 'operator'
    elif normalized_workflow == 'rolled_back':
        policy = 'rollback_completed'
        action = 'review_schedule'
        owner = 'operator'
    elif blockers:
        policy = 'blocked_recovery'
        action = 'retry' if retry_flag else 'review_schedule'
        owner = 'runtime' if retry_flag else 'operator'
    return {
        'schema_version': 'm5_recovery_policy_v1',
        'item_id': item_id,
        'policy': policy,
        'owner': owner,
        'recommended_action': action,
        'retryable': retry_flag,
        'recovered': normalized_execution == 'recovered',
        'recovered_from_status': recovered_from_execution_status or ((last_transition or {}).get('from_execution_status') if isinstance(last_transition, dict) else None),
        'rollback_candidate': rollback_flag,
        'rollback_hint': rollback_hint,
        'blocked_by': blockers,
        'dispatch_route': dispatch_route,
        'next_transition': next_transition,
        'workflow_state': normalized_workflow,
        'execution_status': normalized_execution,
        'summary': f"{policy} -> {action}",
    }


def _build_recovery_orchestration(*, item_id: Optional[str] = None, workflow_state: Optional[str] = None,
                                  execution_status: Optional[str] = None, retryable: Optional[bool] = None,
                                  rollback_candidate: Optional[bool] = None, rollback_hint: Optional[str] = None,
                                  blocked_by: Optional[List[str]] = None, dispatch_route: Optional[str] = None,
                                  next_transition: Optional[str] = None, execution_timeline: Optional[Dict[str, Any]] = None,
                                  recovery_policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    normalized_workflow = str(workflow_state or 'pending').strip().lower() or 'pending'
    normalized_execution = str(execution_status or 'planned').strip().lower() or 'planned'
    blockers = _dedupe_strings(blocked_by or [])
    retry_flag = bool(retryable)
    rollback_flag = bool(rollback_candidate or rollback_hint)
    explicit_rollback = bool(rollback_hint)
    timeline = dict(execution_timeline or {}) if isinstance(execution_timeline, dict) else {}
    policy = dict(recovery_policy or {}) if isinstance(recovery_policy, dict) else {}
    retry_count = max(int(timeline.get('retry_count') or 0), 0)
    attempt_count = max(int(timeline.get('attempt_count') or 1), 1)

    base_delay_minutes = [5, 15, 30, 60]
    retry_delay_min = base_delay_minutes[min(retry_count, len(base_delay_minutes) - 1)]
    retry_stage = 'initial_retry' if retry_count == 0 else ('backoff_retry' if retry_count < 3 else 'final_retry_window')
    queue_bucket = 'observe'
    target_route = 'observe_only_followup'
    manual_reason = None
    routing_reason = []

    high_risk_blockers = {
        'critical_risk',
        'freeze_apply_not_automated_in_executor',
        'policy_switch_not_automated_in_executor',
        'rollout_freeze_not_automated_in_executor',
        'live_rollout_parameter_change_not_supported',
    }

    if normalized_execution == 'recovered':
        queue_bucket = 'recovered_monitoring'
        target_route = 'observe_only_followup'
        routing_reason.append('recovered_execution_watch_window')
    elif normalized_workflow == 'rollback_pending' or (explicit_rollback and normalized_execution in {'error', 'recovered'} and not retry_flag):
        queue_bucket = 'rollback_candidate'
        target_route = 'rollback_candidate_queue'
        routing_reason.append('rollback_path_preferred')
    elif normalized_execution == 'error' or normalized_workflow in {'execution_failed', 'retry_pending'}:
        if any(blocker in high_risk_blockers for blocker in blockers):
            queue_bucket = 'manual_recovery'
            target_route = 'manual_recovery_queue'
            manual_reason = 'high_risk_execution_failure_requires_operator_resolution'
            routing_reason.append('manual_recovery_required')
        elif retry_flag:
            queue_bucket = 'retry_queue'
            target_route = 'retry_queue'
            routing_reason.append('retryable_execution_failure')
        elif rollback_flag:
            queue_bucket = 'rollback_candidate'
            target_route = 'rollback_candidate_queue'
            routing_reason.append('retry_exhausted_or_not_safe')
        else:
            queue_bucket = 'manual_recovery'
            target_route = 'manual_recovery_queue'
            manual_reason = 'execution_failed_without_safe_retry_or_rollback'
            routing_reason.append('manual_recovery_required')
    elif blockers:
        if any(blocker in high_risk_blockers for blocker in blockers):
            queue_bucket = 'manual_recovery'
            target_route = 'manual_recovery_queue'
            manual_reason = 'high_risk_blocker_requires_operator_resolution'
            routing_reason.append('manual_blocker_resolution')
        elif retry_flag:
            queue_bucket = 'retry_queue'
            target_route = 'retry_queue'
            routing_reason.append('blockers_clear_then_retry')
        elif rollback_flag:
            queue_bucket = 'rollback_candidate'
            target_route = 'rollback_candidate_queue'
            routing_reason.append('blocked_item_has_rollback_path')
        else:
            queue_bucket = 'manual_recovery'
            target_route = 'manual_recovery_queue'
            manual_reason = 'blocked_item_requires_operator_resolution'
            routing_reason.append('manual_blocker_resolution')

    if queue_bucket == 'retry_queue' and retry_count >= 3:
        queue_bucket = 'manual_recovery'
        target_route = 'manual_recovery_queue'
        manual_reason = 'retry_budget_exhausted'
        routing_reason.append('retry_budget_exhausted')

    should_retry_at = None
    if queue_bucket == 'retry_queue':
        should_retry_at = (datetime.now(timezone.utc) + timedelta(minutes=retry_delay_min)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')

    return {
        'schema_version': 'm5_recovery_orchestration_v1',
        'item_id': item_id,
        'queue_bucket': queue_bucket,
        'target_route': target_route,
        'routing_reason_codes': routing_reason or ['observe_only'],
        'retry_stage': retry_stage if queue_bucket == 'retry_queue' else None,
        'retry_schedule': {
            'attempt_count': attempt_count,
            'retry_count': retry_count,
            'delay_minutes': retry_delay_min if queue_bucket == 'retry_queue' else None,
            'should_retry_at': should_retry_at,
            'retry_window': 'blocked_clear' if blockers else 'time_backoff',
        },
        'rollback_candidate': queue_bucket == 'rollback_candidate' or rollback_flag,
        'rollback_route': 'rollback_candidate_queue' if (queue_bucket == 'rollback_candidate' or rollback_flag) else None,
        'rollback_hint': rollback_hint,
        'manual_recovery': {
            'required': queue_bucket == 'manual_recovery',
            'route': 'manual_recovery_queue' if queue_bucket == 'manual_recovery' else None,
            'reason': manual_reason,
            'fallback_action': 'operator_review_and_safe_requeue' if queue_bucket == 'manual_recovery' else None,
        },
        'dispatch_route': dispatch_route,
        'next_transition': next_transition,
        'workflow_state': normalized_workflow,
        'execution_status': normalized_execution,
        'blocked_by': blockers,
        'policy': policy.get('policy') if policy else None,
        'summary': f"{queue_bucket} via {target_route}",
    }


def _normalize_action_execution_status(value: Optional[str], *, workflow_state: Optional[str] = None, queue_status: Optional[str] = None, executor_status: Optional[str] = None) -> Optional[str]:
    normalized = str(value or '').strip().lower()
    if normalized in ACTION_EXECUTION_ALLOWED_STATUSES:
        return normalized
    normalized_executor = str(executor_status or '').strip().lower()
    if normalized_executor in ACTION_EXECUTION_ALLOWED_STATUSES:
        return normalized_executor
    normalized_queue = str(queue_status or '').strip().lower()
    queue_map = {
        'ready_to_queue': 'queued',
        'queued': 'queued',
        'blocked_by_approval': 'blocked',
        'deferred': 'deferred',
        'awaiting_approval': 'blocked',
    }
    if normalized_queue in queue_map:
        return queue_map[normalized_queue]
    normalized_workflow = str(workflow_state or '').strip().lower()
    workflow_map = {
        'queued': 'queued',
        'executing': 'dispatching',
        'ready': 'applied',
        'blocked': 'blocked',
        'blocked_by_approval': 'blocked',
        'deferred': 'deferred',
        'execution_failed': 'error',
        'retry_pending': 'deferred',
        'rolled_back': 'recovered',
    }
    return workflow_map.get(normalized_workflow)


def _resolve_validation_gate_context(*rows: Any) -> Dict[str, Any]:
    for row in rows:
        if not isinstance(row, dict):
            continue
        gate = row.get('validation_gate')
        if gate:
            return _build_validation_gate_snapshot({'validation_gate': gate})
        for key in ('auto_advance_gate', 'rollback_gate'):
            nested = row.get(key) or {}
            if isinstance(nested, dict) and nested.get('validation_gate'):
                return _build_validation_gate_snapshot({'validation_gate': nested.get('validation_gate')})
        state_machine = row.get('state_machine') or {}
        if isinstance(state_machine, dict):
            gate = state_machine.get('validation_gate')
            if gate:
                return _build_validation_gate_snapshot({'validation_gate': gate})
            for key in ('auto_advance_gate', 'rollback_gate'):
                nested = state_machine.get(key) or {}
                if isinstance(nested, dict) and nested.get('validation_gate'):
                    return _build_validation_gate_snapshot({'validation_gate': nested.get('validation_gate')})
    return _build_validation_gate_snapshot({})


def _build_operator_action_policy(*, item_id: Optional[str] = None, approval_state: Optional[str] = None, workflow_state: Optional[str] = None, queue_status: Optional[str] = None, dispatch_route: Optional[str] = None, next_transition: Optional[str] = None, blocked_by: Optional[List[str]] = None, retryable: Optional[bool] = None, rollout_stage: Optional[str] = None, target_rollout_stage: Optional[str] = None, terminal: bool = False, validation_gate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    normalized_approval = str(approval_state or 'pending').strip().lower() or 'pending'
    normalized_workflow = str(workflow_state or 'pending').strip().lower() or 'pending'
    normalized_queue = str(queue_status or '').strip().lower() or None
    current_stage = str(rollout_stage or '').strip().lower() or None
    target_stage = str(target_rollout_stage or '').strip().lower() or current_stage
    blockers = _dedupe_strings(blocked_by or [])
    retry_flag = bool(retryable) if retryable is not None else normalized_workflow in {'queued', 'deferred', 'execution_failed', 'retry_pending'}
    validation = _build_validation_gate_snapshot({'validation_gate': validation_gate}) if validation_gate else _build_validation_gate_snapshot({})
    validation_enabled = bool(validation.get('enabled'))
    validation_freeze = bool(validation_enabled and validation.get('freeze_auto_advance'))
    validation_regression = bool(validation.get('regression_detected'))
    validation_gap = int(validation.get('gap_count') or 0)

    action = 'observe_only_followup'
    route = dispatch_route or 'observe_only_followup'
    priority = 'low'
    owner = 'runtime'
    follow_up = 'observe_only'
    rationale = []

    if terminal:
        action = 'observe_only_followup'
        route = 'terminal_observe_only'
        follow_up = 'observe_only'
        rationale.append('terminal_state_locked')
    elif validation_regression and normalized_workflow in {'ready', 'queued', 'review_pending', 'execution_failed', 'rollback_pending', 'rolled_back'}:
        action = 'freeze_followup'
        route = 'rollback_candidate_queue'
        priority = 'high'
        owner = 'operator'
        follow_up = 'rollback_candidate_review'
        rationale.extend(['validation_gate_regression', 'validation_gate_rollback_candidate'])
    elif normalized_workflow in {'execution_failed', 'retry_pending'}:
        action = 'retry' if retry_flag else 'escalate'
        route = 'retry_queue' if retry_flag else 'operator_escalation'
        priority = 'high'
        owner = 'operator' if not retry_flag else 'runtime'
        follow_up = 'retry_execution' if retry_flag else 'escalate_execution'
        rationale.append('execution_recovery_required')
    elif normalized_workflow == 'rollback_pending':
        action = 'freeze_followup'
        route = 'freeze_followup_queue'
        priority = 'high'
        owner = 'operator'
        follow_up = 'freeze_and_review'
        rationale.append('rollback_pending_guardrail')
    elif normalized_workflow == 'rolled_back':
        action = 'review_schedule'
        route = 'review_schedule_queue'
        priority = 'medium'
        owner = 'operator'
        follow_up = 'post_rollback_review'
        rationale.append('rollback_completed_needs_review')
    elif validation_freeze and normalized_workflow in {'ready', 'queued'}:
        action = 'review_schedule'
        route = 'review_schedule_queue'
        priority = 'high'
        owner = 'operator'
        follow_up = 'review_validation_freeze'
        rationale.extend(['validation_gate_freeze', 'validation_gate_gap_detected' if validation_gap else 'validation_gate_not_ready'])
    elif normalized_workflow in {'blocked', 'blocked_by_approval'} or blockers:
        high_risk_blockers = {'critical_risk', 'live_rollout_parameter_change_not_supported', 'freeze_apply_not_automated_in_executor', 'policy_switch_not_automated_in_executor', 'rollout_freeze_not_automated_in_executor'}
        if normalized_workflow == 'blocked_by_approval' or normalized_approval in {'pending', 'ready', 'replayed'}:
            action = 'review_schedule'
            route = 'manual_approval_queue'
            priority = 'high'
            owner = 'operator'
            follow_up = 'await_manual_approval'
            rationale.append('approval_gate_pending')
        elif any(blocker in high_risk_blockers for blocker in blockers):
            action = 'freeze_followup'
            route = 'freeze_followup_queue'
            priority = 'high'
            owner = 'operator'
            follow_up = 'freeze_and_review'
            rationale.append('high_risk_blocker_present')
        elif retry_flag:
            action = 'retry'
            route = 'retry_queue'
            priority = 'medium'
            owner = 'runtime'
            follow_up = 'retry_after_blockers_clear'
            rationale.append('retry_when_blockers_clear')
        else:
            action = 'escalate'
            route = 'operator_escalation'
            priority = 'high'
            owner = 'operator'
            follow_up = 'escalate_blocked_state'
            rationale.append('blocked_requires_operator')
    elif normalized_workflow == 'deferred':
        action = 'review_schedule'
        route = 'review_schedule_queue'
        priority = 'medium'
        owner = 'operator'
        follow_up = 'scheduled_review'
        rationale.append('deferred_review_window')
    elif normalized_workflow == 'queued':
        action = 'observe_only_followup'
        route = normalized_queue or dispatch_route or 'queue_observer'
        priority = 'medium'
        owner = 'runtime'
        follow_up = 'watch_queue_progression'
        rationale.append('queued_waiting_progression')
    elif normalized_workflow == 'ready':
        if (not current_stage and not target_stage) or current_stage == target_stage or (current_stage == 'observe' and target_stage == 'observe'):
            action = 'observe_only_followup'
            route = dispatch_route or 'observe_only_followup'
            priority = 'low'
            owner = 'runtime'
            follow_up = 'observe_only_ready'
            rationale.append('observe_only_ready_state')
        else:
            action = 'review_schedule'
            route = dispatch_route or 'rollout_readiness_queue'
            priority = 'medium'
            owner = 'operator'
            follow_up = 'review_ready_for_rollout'
            rationale.append('ready_for_guarded_rollout')
    elif normalized_workflow == 'review_pending' or normalized_queue == 'deferred':
        action = 'review_schedule'
        route = 'review_schedule_queue'
        priority = 'medium'
        owner = 'operator'
        follow_up = 'schedule_review'
        rationale.append('review_pending')
    elif next_transition and 'retry' in str(next_transition).lower() and retry_flag:
        action = 'retry'
        route = 'retry_queue'
        priority = 'medium'
        owner = 'runtime'
        follow_up = 'retry_transition'
        rationale.append('next_transition_retry')

    if validation_freeze and not validation_regression and 'validation_gate_freeze' in rationale:
        route = 'validation_review_queue'
    summary_reason = ' / '.join(rationale or ['steady_state_observe_only'])
    return {
        'schema_version': 'm5_operator_action_policy_v1',
        'item_id': item_id,
        'action': action,
        'route': route,
        'priority': priority,
        'owner': owner,
        'follow_up': follow_up,
        'queue_status': normalized_queue,
        'dispatch_route': dispatch_route,
        'next_transition': next_transition,
        'blocked_by': blockers,
        'retryable': retry_flag,
        'rollout_stage': current_stage,
        'target_rollout_stage': target_stage,
        'validation_gate': validation,
        'validation_status': validation.get('status'),
        'validation_freeze': validation_freeze,
        'validation_regression': validation_regression,
        'validation_gap_count': validation_gap,
        'reason_codes': rationale or ['steady_state_observe_only'],
        'summary': f"{action} via {route} ({summary_reason})",
    }


def _build_state_machine_semantics(*, item_id: Optional[str] = None, approval_state: Optional[str] = None, workflow_state: Optional[str] = None, decision_state: Optional[str] = None, queue_status: Optional[str] = None, dispatch_route: Optional[str] = None, next_transition: Optional[str] = None, executor_status: Optional[str] = None, rollout_stage: Optional[str] = None, target_rollout_stage: Optional[str] = None, blocked_by: Optional[List[str]] = None, retryable: Optional[bool] = None, rollback_hint: Optional[str] = None, execution_status: Optional[str] = None, transition_rule: Optional[str] = None, last_transition: Optional[Dict[str, Any]] = None, validation_gate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    normalized_approval = str(approval_state or decision_state or 'pending').strip().lower() or 'pending'
    normalized_workflow = _normalize_workflow_state(workflow_state, approval_state=normalized_approval, queue_status=queue_status, executor_status=executor_status)
    normalized_queue = str(queue_status or '').strip().lower() or None
    blocked = _dedupe_strings(blocked_by or [])
    normalized_execution = _normalize_action_execution_status(execution_status, workflow_state=normalized_workflow, queue_status=normalized_queue, executor_status=executor_status)
    terminal = normalized_approval in TERMINAL_APPROVAL_STATES or normalized_workflow in WORKFLOW_TERMINAL_STATES or normalized_execution in ACTION_EXECUTION_TERMINAL_STATUSES
    phase = 'proposal'
    if normalized_approval in {'pending', 'ready', 'replayed'} and normalized_workflow in {'pending', 'blocked', 'blocked_by_approval', 'review_pending'}:
        phase = 'approval'
    elif normalized_workflow in {'queued', 'ready'}:
        phase = 'queue'
    elif normalized_workflow in {'executing', 'execution_failed', 'retry_pending', 'rollback_pending', 'rolled_back'} or normalized_execution in {'dispatching', 'applied', 'error', 'recovered'}:
        phase = 'execution'
    elif terminal:
        phase = 'terminal'
    lifecycle_path = [phase]
    if normalized_queue:
        lifecycle_path.append(f'queue:{normalized_queue}')
    if normalized_execution:
        lifecycle_path.append(f'execution:{normalized_execution}')
    if dispatch_route:
        lifecycle_path.append(f'route:{dispatch_route}')
    if normalized_workflow not in {phase, normalized_queue, normalized_execution}:
        lifecycle_path.append(f'workflow:{normalized_workflow}')
    operator_action_policy = _build_operator_action_policy(
        item_id=item_id,
        approval_state=normalized_approval,
        workflow_state=normalized_workflow,
        queue_status=normalized_queue,
        dispatch_route=dispatch_route,
        next_transition=next_transition,
        blocked_by=blocked,
        retryable=retryable,
        rollout_stage=rollout_stage,
        target_rollout_stage=target_rollout_stage,
        terminal=terminal,
        validation_gate=validation_gate,
    )
    retry_flag = bool(retryable) if retryable is not None else normalized_workflow in {'queued', 'deferred', 'execution_failed', 'retry_pending'} or normalized_execution in {'queued', 'deferred', 'error'}
    rollback_flag = bool(rollback_hint or normalized_workflow in {'ready', 'queued', 'execution_failed', 'rollback_pending'} or normalized_execution in {'applied', 'queued', 'error', 'recovered'})
    recovered_from_execution_status = None
    if isinstance(last_transition, dict):
        recovered_from_execution_status = last_transition.get('from_execution_status')
    execution_timeline = _build_execution_timeline(
        item_id=item_id,
        execution_status=normalized_execution,
        previous_execution_status=recovered_from_execution_status,
        workflow_state=normalized_workflow,
        queue_status=normalized_queue,
        dispatch_route=dispatch_route,
        next_transition=next_transition,
        transition_rule=transition_rule,
        retryable=retry_flag,
        rollback_hint=rollback_hint,
        recovered_from_execution_status=recovered_from_execution_status,
        last_transition=last_transition,
    )
    recovery_policy = _build_recovery_policy(
        item_id=item_id,
        workflow_state=normalized_workflow,
        execution_status=normalized_execution,
        retryable=retry_flag,
        rollback_candidate=rollback_flag,
        rollback_hint=rollback_hint,
        blocked_by=blocked,
        next_transition=next_transition,
        dispatch_route=dispatch_route,
        last_transition=last_transition,
        recovered_from_execution_status=recovered_from_execution_status,
    )
    recovery_orchestration = _build_recovery_orchestration(
        item_id=item_id,
        workflow_state=normalized_workflow,
        execution_status=normalized_execution,
        retryable=retry_flag,
        rollback_candidate=rollback_flag,
        rollback_hint=rollback_hint,
        blocked_by=blocked,
        dispatch_route=dispatch_route,
        next_transition=next_transition,
        execution_timeline=execution_timeline,
        recovery_policy=recovery_policy,
    )
    return {
        'schema_version': 'm5_unified_state_machine_v2',
        'item_id': item_id,
        'approval_state': normalized_approval,
        'workflow_state': normalized_workflow,
        'decision_state': normalized_approval,
        'queue_status': normalized_queue,
        'dispatch_route': dispatch_route,
        'next_transition': next_transition,
        'transition_rule': str(transition_rule or '').strip().lower() or None,
        'last_transition': dict(last_transition or {}) if isinstance(last_transition, dict) else {},
        'executor_status': str(executor_status or '').strip().lower() or None,
        'execution_status': normalized_execution,
        'rollout_stage': str(rollout_stage or '').strip().lower() or None,
        'target_rollout_stage': str(target_rollout_stage or '').strip().lower() or (str(rollout_stage or '').strip().lower() or None),
        'phase': phase,
        'blocked': bool(blocked or normalized_workflow in {'blocked', 'blocked_by_approval', 'deferred', 'execution_failed'} or normalized_execution in {'blocked', 'deferred', 'error'}),
        'blocked_by': blocked,
        'terminal': terminal,
        'retryable': retry_flag,
        'rollback_candidate': rollback_flag,
        'rollback_hint': rollback_hint,
        'lifecycle_path': lifecycle_path,
        'operator_action_policy': operator_action_policy,
        'execution_timeline': execution_timeline,
        'recovery_policy': recovery_policy,
        'recovery_orchestration': recovery_orchestration,
        'validation_gate': _build_validation_gate_snapshot({'validation_gate': validation_gate}) if validation_gate else _build_validation_gate_snapshot({}),
    }



def _normalize_auto_approval_decision(value: Optional[str]) -> str:
    normalized = str(value or '').strip().lower()
    return normalized if normalized in AUTO_APPROVAL_DECISIONS else 'manual_review'


def _normalize_auto_approval_confidence(value: Optional[str]) -> str:
    normalized = str(value or '').strip().lower()
    return normalized if normalized in {'high', 'medium', 'low'} else 'low'


EVENT_PROVENANCE_SCHEMA_VERSION = 'm5_event_provenance_v1'


def _build_event_provenance(*, origin: str, source: Optional[str] = None, family: Optional[str] = None, phase: Optional[str] = None, producer: Optional[str] = None, replay_source: Optional[str] = None, synthetic: bool = False, schema_version: str = EVENT_PROVENANCE_SCHEMA_VERSION) -> Dict[str, Any]:
    normalized_origin = str(origin or 'unknown').strip().lower() or 'unknown'
    normalized_source = str(source or '').strip() or None
    normalized_family = str(family or normalized_origin).strip().lower() or normalized_origin
    normalized_phase = str(phase or normalized_family).strip().lower() or normalized_family
    normalized_producer = str(producer or normalized_source or normalized_origin).strip() or normalized_origin
    normalized_replay = str(replay_source or '').strip() or None
    return {
        'schema_version': schema_version,
        'origin': normalized_origin,
        'source': normalized_source or normalized_origin,
        'family': normalized_family,
        'phase': normalized_phase,
        'producer': normalized_producer,
        'replay_source': normalized_replay,
        'synthetic': bool(synthetic),
    }


def _build_event_timestamp(*, value: Optional[str] = None, source: Optional[str] = None, phase: Optional[str] = None, field: Optional[str] = None, fallback_fields: Optional[List[str]] = None, schema_version: str = EVENT_PROVENANCE_SCHEMA_VERSION) -> Dict[str, Any]:
    normalized_value = str(value or '').strip() or None
    normalized_source = str(source or '').strip() or None
    normalized_phase = str(phase or normalized_source or 'observed').strip().lower() or 'observed'
    normalized_field = str(field or '').strip() or None
    fallbacks = [str(item).strip() for item in (fallback_fields or []) if str(item).strip()]
    return {
        'schema_version': schema_version,
        'value': normalized_value,
        'source': normalized_source or ('missing' if normalized_value is None else 'observed'),
        'phase': normalized_phase,
        'field': normalized_field,
        'fallback_fields': fallbacks,
        'present': normalized_value is not None,
    }


def _normalize_event_type(event_type: Optional[str], *, category: Optional[str] = None) -> str:
    normalized = str(event_type or '').strip().lower()
    if normalized:
        return normalized
    fallback_category = str(category or 'unknown').strip().lower()
    return f'{fallback_category}_event' if fallback_category else 'unknown_event'


def _attach_unified_event_metadata(event: Dict[str, Any], *, normalized_event_type: Optional[str] = None, provenance: Optional[Dict[str, Any]] = None, timestamp: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = dict(event or {})
    normalized_type = _normalize_event_type(normalized_event_type or payload.get('event_type'), category=payload.get('phase') or payload.get('path_type'))
    payload['normalized_event_type'] = normalized_type
    if provenance is not None:
        payload['provenance'] = dict(provenance)
        payload.setdefault('source', payload['provenance'].get('source'))
    if timestamp is not None:
        payload['timestamp_info'] = dict(timestamp)
        payload['timestamp'] = payload['timestamp_info'].get('value')
    return payload


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


def _summarize_operator_action_policies(rows: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    rows = rows or []
    policies = []
    for row in rows:
        policy = (row.get('operator_action_policy') or row.get('policy') or {}) if isinstance(row, dict) else {}
        policies.append(policy if isinstance(policy, dict) else {})

    def _count(field: str, default: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for policy in policies:
            key = str(policy.get(field) or default).strip() or default
            counts[key] = counts.get(key, 0) + 1
        return counts

    action_counts = _count('action', 'observe_only_followup')
    route_counts = _count('route', 'observe_only_followup')
    follow_up_counts = _count('follow_up', 'observe_only')
    owner_counts = _count('owner', 'unassigned')
    priority_counts = _count('priority', 'normal')
    reason_codes = _dedupe_strings([code for policy in policies for code in (policy.get('reason_codes') or [])])
    dominant_action = max(sorted(action_counts.items()), key=lambda item: (item[1], item[0]))[0] if action_counts else None
    dominant_route = max(sorted(route_counts.items()), key=lambda item: (item[1], item[0]))[0] if route_counts else None
    dominant_follow_up = max(sorted(follow_up_counts.items()), key=lambda item: (item[1], item[0]))[0] if follow_up_counts else None
    dominant_owner = max(sorted(owner_counts.items()), key=lambda item: (item[1], item[0]))[0] if owner_counts else None
    dominant_priority = max(sorted(priority_counts.items()), key=lambda item: (item[1], item[0]))[0] if priority_counts else None
    combinations = {}
    for policy in policies:
        action = str(policy.get('action') or 'observe_only_followup').strip() or 'observe_only_followup'
        route = str(policy.get('route') or 'observe_only_followup').strip() or 'observe_only_followup'
        follow_up = str(policy.get('follow_up') or 'observe_only').strip() or 'observe_only'
        combo = f'{action}|{route}|{follow_up}'
        combinations[combo] = combinations.get(combo, 0) + 1
    return {
        'policy_count': len(policies),
        'action_counts': action_counts,
        'route_counts': route_counts,
        'follow_up_counts': follow_up_counts,
        'owner_counts': owner_counts,
        'priority_counts': priority_counts,
        'dominant_action': dominant_action,
        'dominant_route': dominant_route,
        'dominant_follow_up': dominant_follow_up,
        'dominant_owner': dominant_owner,
        'dominant_priority': dominant_priority,
        'reason_codes': reason_codes,
        'policy_combinations': combinations,
    }


def _resolve_rollout_advisory_snapshot(*, workflow_item: Optional[Dict[str, Any]] = None, approval_item: Optional[Dict[str, Any]] = None,
                                     row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    workflow_item = workflow_item or {}
    approval_item = approval_item or {}
    row = row or {}
    advisory = (
        (((workflow_item.get('plan') or {}).get('stage_handler') or {}).get('advisory') if isinstance(workflow_item.get('plan'), dict) else None)
        or ((workflow_item.get('stage_handler') or {}).get('advisory') if isinstance(workflow_item.get('stage_handler'), dict) else None)
        or (((approval_item.get('plan') or {}).get('stage_handler') or {}).get('advisory') if isinstance(approval_item.get('plan'), dict) else None)
        or ((approval_item.get('stage_handler') or {}).get('advisory') if isinstance(approval_item.get('stage_handler'), dict) else None)
        or (((row.get('plan') or {}).get('stage_handler') or {}).get('advisory') if isinstance(row.get('plan'), dict) else None)
        or ((row.get('stage_handler') or {}).get('advisory') if isinstance(row.get('stage_handler'), dict) else None)
        or ((row.get('stage_progression') or {}).get('advisory') if isinstance(row.get('stage_progression'), dict) else None)
        or row.get('rollout_advisory')
        or {}
    )
    if not isinstance(advisory, dict) or not advisory:
        stage_handler = (
            (((workflow_item.get('plan') or {}).get('stage_handler')) if isinstance(workflow_item.get('plan'), dict) else None)
            or workflow_item.get('stage_handler')
            or approval_item.get('stage_handler')
            or (((row.get('plan') or {}).get('stage_handler')) if isinstance(row.get('plan'), dict) else None)
            or row.get('stage_handler')
            or {}
        )
        if isinstance(stage_handler, dict):
            advisory = dict(stage_handler.get('advisory') or {})
    if not isinstance(advisory, dict):
        advisory = {}
    stage_loop = row.get('stage_loop') or _resolve_stage_loop_snapshot(workflow_item=workflow_item, approval_item=approval_item, row=row)
    auto_gate = _extract_rollout_gate_snapshot(workflow_item, approval_item, row).get('auto_advance_gate') or {}
    current_stage = workflow_item.get('current_rollout_stage') or approval_item.get('rollout_stage') or row.get('current_rollout_stage') or row.get('rollout_stage')
    target_stage = workflow_item.get('target_rollout_stage') or approval_item.get('target_rollout_stage') or row.get('target_rollout_stage') or current_stage
    normalized = {
        'recommended_stage': advisory.get('recommended_stage') or target_stage or current_stage or 'observe',
        'recommended_action': advisory.get('recommended_action') or stage_loop.get('recommended_action') or 'continue_observe',
        'urgency': advisory.get('urgency') or ('high' if stage_loop.get('loop_state') in {'review_pending', 'rollback_prepare'} else 'low'),
        'confidence': advisory.get('confidence'),
        'reasons': _dedupe_strings(advisory.get('reasons') or stage_loop.get('waiting_on') or auto_gate.get('blockers') or []),
        'ready_for_live_promotion': bool(advisory.get('ready_for_live_promotion', auto_gate.get('allowed') and stage_loop.get('loop_state') == 'auto_advance')),
    }
    normalized['current_stage'] = current_stage or normalized['recommended_stage']
    normalized['target_stage'] = target_stage or normalized['recommended_stage']
    normalized['stage_path'] = f"{normalized['current_stage'] or 'observe'}->{normalized['target_stage'] or normalized['recommended_stage'] or 'observe'}"
    normalized['auto_promotion_candidate'] = bool(normalized['ready_for_live_promotion'])
    return normalized


def _summarize_rollout_advisories(rows: Optional[List[Dict[str, Any]]], *, label: Optional[str] = None,
                                  max_items: int = 5) -> Dict[str, Any]:
    rows = list(rows or [])
    by_action: Dict[str, int] = {}
    by_stage: Dict[str, int] = {}
    by_urgency: Dict[str, int] = {}
    by_reason: Dict[str, int] = {}
    ready_items: List[Dict[str, Any]] = []
    advisory_items: List[Dict[str, Any]] = []
    for row in rows:
        advisory = _resolve_rollout_advisory_snapshot(workflow_item=row, approval_item=row, row=row)
        action = str(advisory.get('recommended_action') or 'continue_observe')
        stage = str(advisory.get('recommended_stage') or 'observe')
        urgency = str(advisory.get('urgency') or 'low')
        by_action[action] = by_action.get(action, 0) + 1
        by_stage[stage] = by_stage.get(stage, 0) + 1
        by_urgency[urgency] = by_urgency.get(urgency, 0) + 1
        for reason in advisory.get('reasons') or []:
            by_reason[str(reason)] = by_reason.get(str(reason), 0) + 1
        advisory_row = {
            'item_id': row.get('item_id') or row.get('playbook_id'),
            'approval_id': row.get('approval_id'),
            'title': row.get('title'),
            'action_type': row.get('action_type'),
            'workflow_state': row.get('workflow_state'),
            'lane_id': row.get('lane_id'),
            'queue_name': row.get('queue_name'),
            'current_stage': advisory.get('current_stage'),
            'target_stage': advisory.get('target_stage'),
            'stage_path': advisory.get('stage_path'),
            'recommended_stage': advisory.get('recommended_stage'),
            'recommended_action': advisory.get('recommended_action'),
            'urgency': advisory.get('urgency'),
            'confidence': advisory.get('confidence'),
            'ready_for_live_promotion': bool(advisory.get('ready_for_live_promotion')),
            'auto_promotion_candidate': bool(advisory.get('auto_promotion_candidate')),
            'reasons': advisory.get('reasons') or [],
        }
        advisory_items.append(advisory_row)
        if advisory_row['auto_promotion_candidate']:
            ready_items.append(advisory_row)
    def _sorted_counts(bucket: Dict[str, int]) -> Dict[str, int]:
        return dict(sorted(bucket.items(), key=lambda item: (-item[1], item[0])))
    def _top(bucket: Dict[str, int]) -> Optional[str]:
        return sorted(bucket.items(), key=lambda item: (-item[1], item[0]))[0][0] if bucket else None
    return {
        'label': label,
        'item_count': len(rows),
        'advisory_item_count': len(advisory_items),
        'auto_promotion_candidate_count': len(ready_items),
        'ready_for_live_promotion_count': len(ready_items),
        'by_action': _sorted_counts(by_action),
        'by_stage': _sorted_counts(by_stage),
        'by_urgency': _sorted_counts(by_urgency),
        'reason_counts': _sorted_counts(by_reason),
        'dominant_action': _top(by_action),
        'dominant_stage': _top(by_stage),
        'dominant_urgency': _top(by_urgency),
        'dominant_reason': _top(by_reason),
        'auto_promotion_candidates': ready_items[:max_items],
        'items': advisory_items[:max_items],
    }


def _build_auto_promotion_candidate_item(row: Dict[str, Any]) -> Dict[str, Any]:
    workflow_item = row or {}
    approval_item = row.get('approval_item') or {}
    item_id = workflow_item.get('item_id') or approval_item.get('playbook_id') or row.get('item_id')
    approval_id = approval_item.get('approval_id') or row.get('approval_id')
    blocked_by = _dedupe_strings((workflow_item.get('blocking_reasons') or []) + (approval_item.get('blocked_by') or []) + (row.get('blocked_by') or []))
    auto_gate = _extract_rollout_gate_snapshot(workflow_item, approval_item, row).get('auto_advance_gate') or {}
    rollback_gate = _extract_rollout_gate_snapshot(workflow_item, approval_item, row).get('rollback_gate') or {}
    validation_gate = _resolve_validation_gate_context(workflow_item, approval_item, row, auto_gate, rollback_gate)
    stage_loop = workflow_item.get('stage_loop') or row.get('stage_loop') or _resolve_stage_loop_snapshot(workflow_item=workflow_item, approval_item=approval_item, row=row)
    operator_action_policy = (workflow_item.get('operator_action_policy') or row.get('operator_action_policy') or _build_operator_action_policy(
        item_id=item_id,
        approval_state=approval_item.get('approval_state') or workflow_item.get('approval_state'),
        workflow_state=workflow_item.get('workflow_state'),
        queue_status=((workflow_item.get('queue_progression') or {}).get('status') if isinstance(workflow_item.get('queue_progression'), dict) else None),
        dispatch_route=((workflow_item.get('queue_progression') or {}).get('dispatch_route') if isinstance(workflow_item.get('queue_progression'), dict) else None),
        next_transition=((workflow_item.get('queue_progression') or {}).get('next_transition') if isinstance(workflow_item.get('queue_progression'), dict) else None),
        blocked_by=blocked_by,
        retryable=((workflow_item.get('state_machine') or {}).get('retryable') if isinstance(workflow_item.get('state_machine'), dict) else None),
        rollout_stage=workflow_item.get('current_rollout_stage') or workflow_item.get('rollout_stage'),
        target_rollout_stage=workflow_item.get('target_rollout_stage'),
        terminal=bool(((workflow_item.get('state_machine') or {}) if isinstance(workflow_item.get('state_machine'), dict) else {}).get('terminal')),
        validation_gate=validation_gate,
    ))
    lane_routing = workflow_item.get('lane_routing') or row.get('lane_routing') or _resolve_lane_routing(workflow_item=workflow_item, approval_item=approval_item, row=row, operator_action_policy=operator_action_policy, stage_loop=stage_loop)
    advisory = workflow_item.get('rollout_advisory') or row.get('rollout_advisory') or _resolve_rollout_advisory_snapshot(workflow_item=workflow_item, approval_item=approval_item, row=row)
    current_stage = workflow_item.get('current_rollout_stage') or workflow_item.get('rollout_stage') or approval_item.get('rollout_stage') or advisory.get('current_stage')
    target_stage = workflow_item.get('target_rollout_stage') or approval_item.get('target_rollout_stage') or advisory.get('target_stage') or current_stage
    risk_level = workflow_item.get('risk_level') or approval_item.get('risk_level') or row.get('risk_level') or 'unknown'
    auto_approval_decision = workflow_item.get('auto_approval_decision') or approval_item.get('auto_approval_decision') or row.get('auto_approval_decision') or 'manual_review'
    requires_manual = bool(workflow_item.get('requires_manual', approval_item.get('requires_manual', row.get('requires_manual'))))
    approval_required = bool(workflow_item.get('approval_required', approval_item.get('approval_required', row.get('approval_required'))))
    queue_progression = workflow_item.get('queue_progression') or row.get('queue_progression') or {}
    missing_requirements = _dedupe_strings(
        blocked_by
        + (auto_gate.get('blockers') or [])
        + (validation_gate.get('reasons') or [])
        + (([] if advisory.get('ready_for_live_promotion') else (advisory.get('reasons') or [])))
    )
    promotion_allowed = bool(
        advisory.get('ready_for_live_promotion')
        and advisory.get('auto_promotion_candidate')
        and auto_gate.get('allowed')
        and not rollback_gate.get('candidate')
        and not validation_gate.get('freeze_auto_advance')
        and not validation_gate.get('regression_detected')
        and not blocked_by
        and not requires_manual
        and not approval_required
    )
    why_promotable = _dedupe_strings([
        *(advisory.get('reasons') or []),
        *(operator_action_policy.get('reason_codes') or []),
        *(stage_loop.get('waiting_on') or []),
    ])
    if promotion_allowed and 'auto_advance_allowed' not in why_promotable:
        why_promotable.insert(0, 'auto_advance_allowed')
    risk_rank = _workbench_risk_rank(risk_level)
    if rollback_gate.get('candidate') or validation_gate.get('regression_detected'):
        risk_label = 'critical'
    elif validation_gate.get('freeze_auto_advance') or risk_rank <= 1:
        risk_label = 'high'
    elif risk_rank == 2:
        risk_label = 'medium'
    else:
        risk_label = 'low'
    manual_fallback_required = bool((not promotion_allowed) and (requires_manual or approval_required or operator_action_policy.get('owner') == 'operator' or lane_routing.get('lane_id') in {'manual_approval', 'blocked', 'rollback_candidate'}))
    return {
        'item_id': item_id,
        'approval_id': approval_id,
        'title': workflow_item.get('title') or approval_item.get('title') or row.get('title') or item_id,
        'action_type': workflow_item.get('action_type') or approval_item.get('action_type') or row.get('action_type') or 'unknown',
        'workflow_state': workflow_item.get('workflow_state') or approval_item.get('workflow_state') or row.get('workflow_state') or 'pending',
        'approval_state': approval_item.get('approval_state') or row.get('approval_state') or 'not_required',
        'lane_id': lane_routing.get('lane_id'),
        'queue_name': lane_routing.get('queue_name'),
        'dispatch_route': lane_routing.get('dispatch_route'),
        'current_rollout_stage': current_stage,
        'target_rollout_stage': target_stage,
        'recommended_stage': advisory.get('recommended_stage'),
        'recommended_action': advisory.get('recommended_action'),
        'stage_path': advisory.get('stage_path') or f"{current_stage or 'observe'}->{target_stage or current_stage or 'observe'}",
        'can_auto_promote': promotion_allowed,
        'ready_for_live_promotion': bool(advisory.get('ready_for_live_promotion')),
        'auto_promotion_candidate': bool(advisory.get('auto_promotion_candidate')),
        'why_promotable': why_promotable,
        'missing_requirements': [] if promotion_allowed else missing_requirements,
        'risk_level': risk_level,
        'risk_label': risk_label,
        'risk_score': max(0, 100 - int(auto_gate.get('readiness_score') or 0)),
        'manual_fallback_required': manual_fallback_required,
        'manual_fallback_reason_codes': _dedupe_strings(([] if not manual_fallback_required else (operator_action_policy.get('reason_codes') or ['manual_fallback_required']))),
        'operator_action_policy': operator_action_policy,
        'auto_advance_gate': auto_gate,
        'rollback_gate': rollback_gate,
        'validation_gate': validation_gate,
        'stage_loop': stage_loop,
        'lane_routing': lane_routing,
        'auto_approval_decision': auto_approval_decision,
        'auto_approval_eligible': bool(workflow_item.get('auto_approval_eligible', approval_item.get('auto_approval_eligible', row.get('auto_approval_eligible')))),
        'requires_manual': requires_manual,
        'approval_required': approval_required,
        'blocked_by': blocked_by,
        'queue_progression': queue_progression,
        'scheduled_review': workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or row.get('scheduled_review') or {},
        'summary': (
            f"ready:{bool(promotion_allowed)} | action={advisory.get('recommended_action') or operator_action_policy.get('action')} | "
            f"stage={current_stage or '--'}->{target_stage or '--'} | missing={len([] if promotion_allowed else missing_requirements)} | risk={risk_label}"
        ),
    }


def build_auto_promotion_candidate_view(payload: Optional[Dict] = None, *, lane_ids: Any = None, action_types: Any = None,
                                        risk_levels: Any = None, workflow_states: Any = None, approval_states: Any = None,
                                        current_rollout_stages: Any = None, target_rollout_stages: Any = None,
                                        candidate_status: Optional[str] = None, manual_fallback_required: Optional[bool] = None,
                                        q: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    payload = payload or {}
    consumer_view = payload.get('consumer_view') or build_workflow_consumer_view(payload)
    workflow_items = ((consumer_view.get('workflow_state') or {}).get('item_states') or [])
    approval_items = ((consumer_view.get('approval_state') or {}).get('items') or [])
    approval_by_playbook = {row.get('playbook_id'): row for row in approval_items if row.get('playbook_id')}
    items = []
    for workflow_item in workflow_items:
        item_id = workflow_item.get('item_id')
        approval_item = approval_by_playbook.get(item_id) or {}
        items.append(_build_auto_promotion_candidate_item({**workflow_item, 'approval_item': approval_item}))

    lane_filter = set(_normalize_filter_values(lane_ids))
    action_filter = set(_normalize_filter_values(action_types))
    risk_filter = set(_normalize_filter_values(risk_levels))
    workflow_filter = set(_normalize_filter_values(workflow_states))
    approval_filter = set(_normalize_filter_values(approval_states))
    current_stage_filter = set(_normalize_filter_values(current_rollout_stages))
    target_stage_filter = set(_normalize_filter_values(target_rollout_stages))
    q_norm = str(q or '').strip().lower()
    candidate_status_norm = str(candidate_status or '').strip().lower() or None

    def _matches(row: Dict[str, Any]) -> bool:
        if lane_filter and str(row.get('lane_id') or '').lower() not in lane_filter:
            return False
        if action_filter and str(row.get('action_type') or '').lower() not in action_filter:
            return False
        if risk_filter and str(row.get('risk_level') or '').lower() not in risk_filter:
            return False
        if workflow_filter and str(row.get('workflow_state') or '').lower() not in workflow_filter:
            return False
        if approval_filter and str(row.get('approval_state') or '').lower() not in approval_filter:
            return False
        if current_stage_filter and str(row.get('current_rollout_stage') or '').lower() not in current_stage_filter:
            return False
        if target_stage_filter and str(row.get('target_rollout_stage') or '').lower() not in target_stage_filter:
            return False
        if candidate_status_norm == 'ready' and not row.get('can_auto_promote'):
            return False
        if candidate_status_norm == 'blocked' and row.get('can_auto_promote'):
            return False
        if manual_fallback_required is not None and bool(row.get('manual_fallback_required')) != bool(manual_fallback_required):
            return False
        if q_norm:
            haystacks = [
                row.get('item_id'), row.get('approval_id'), row.get('title'), row.get('action_type'), row.get('workflow_state'), row.get('approval_state'),
                row.get('recommended_action'), row.get('recommended_stage'), row.get('risk_level'), row.get('risk_label'), row.get('summary'),
            ] + list(row.get('why_promotable') or []) + list(row.get('missing_requirements') or []) + list(row.get('manual_fallback_reason_codes') or [])
            if q_norm not in ' '.join(str(v or '').lower() for v in haystacks):
                return False
        return True

    filtered_items = sorted([row for row in items if _matches(row)], key=lambda row: (0 if row.get('can_auto_promote') else 1, _workbench_item_sort_key(row)))
    ready_items = [row for row in filtered_items if row.get('can_auto_promote')]
    blocked_items = [row for row in filtered_items if not row.get('can_auto_promote')]
    missing_counts: Dict[str, int] = {}
    why_counts: Dict[str, int] = {}
    risk_counts: Dict[str, int] = {}
    for row in filtered_items:
        for code in row.get('missing_requirements') or []:
            missing_counts[code] = missing_counts.get(code, 0) + 1
        for code in row.get('why_promotable') or []:
            why_counts[code] = why_counts.get(code, 0) + 1
        risk = str(row.get('risk_label') or 'unknown')
        risk_counts[risk] = risk_counts.get(risk, 0) + 1
    view = {
        'schema_version': 'm5_auto_promotion_candidate_view_v1',
        'summary': {
            'candidate_count': len(filtered_items),
            'ready_count': len(ready_items),
            'blocked_count': len(blocked_items),
            'manual_fallback_required_count': sum(1 for row in filtered_items if row.get('manual_fallback_required')),
            'risk_label_counts': dict(sorted(risk_counts.items())),
            'top_missing_requirements': [key for key, _ in sorted(missing_counts.items(), key=lambda item: (-item[1], item[0]))[:10]],
            'missing_requirement_counts': dict(sorted(missing_counts.items(), key=lambda item: (-item[1], item[0]))),
            'why_promotable_counts': dict(sorted(why_counts.items(), key=lambda item: (-item[1], item[0]))),
            'validation_gate': consumer_view.get('validation_gate') or _build_validation_gate_snapshot(payload),
        },
        'applied_filters': {
            'lane_ids': _normalize_filter_values(lane_ids),
            'action_types': _normalize_filter_values(action_types),
            'risk_levels': _normalize_filter_values(risk_levels),
            'workflow_states': _normalize_filter_values(workflow_states),
            'approval_states': _normalize_filter_values(approval_states),
            'current_rollout_stages': _normalize_filter_values(current_rollout_stages),
            'target_rollout_stages': _normalize_filter_values(target_rollout_stages),
            'candidate_status': candidate_status_norm,
            'manual_fallback_required': manual_fallback_required,
            'q': str(q or '').strip(),
        },
        'items': filtered_items[:limit],
        'ready_items': ready_items[:limit],
        'blocked_items': blocked_items[:limit],
    }
    payload['auto_promotion_candidate_view'] = view
    return view


def _build_low_intervention_group_summary(rows: Optional[List[Dict[str, Any]]], *, label: Optional[str] = None) -> Dict[str, Any]:
    rows = rows or []
    operator_policy_summary = _summarize_operator_action_policies(rows)

    def _count(values: List[Any], default: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for value in values:
            key = str(value or default).strip() or default
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def _top_key(counts: Dict[str, int]) -> Optional[str]:
        return max(sorted(counts.items()), key=lambda item: (item[1], item[0]))[0] if counts else None

    lane_counts = _count([row.get('lane_id') for row in rows], 'unknown')
    workflow_state_counts = _count([row.get('workflow_state') for row in rows], 'pending')
    approval_state_counts = _count([row.get('approval_state') for row in rows], 'not_required')
    risk_level_counts = _count([row.get('risk_level') for row in rows], 'unknown')
    action_type_counts = _count([row.get('action_type') for row in rows], 'unknown')
    priority_mix = _count([(row.get('operator_action_policy') or {}).get('priority') for row in rows], 'normal')
    bucket_counts = _count([tag for row in rows for tag in (row.get('bucket_tags') or [])], 'unbucketed')
    owner_counts = _count([row.get('owner_hint') for row in rows if row.get('owner_hint')], 'unassigned')

    manual_count = sum(1 for row in rows if bool(row.get('requires_manual')) and str(row.get('approval_state') or '').strip().lower() == 'pending')
    blocked_count = sum(1 for row in rows if str(row.get('workflow_state') or '').strip().lower() in {'blocked', 'blocked_by_approval', 'deferred'} or bool(row.get('blocked_by')))
    ready_count = sum(1 for row in rows if str(row.get('workflow_state') or '').strip().lower() == 'ready')
    queued_count = sum(1 for row in rows if str(row.get('workflow_state') or '').strip().lower() == 'queued')
    deferred_count = sum(1 for row in rows if str(row.get('workflow_state') or '').strip().lower() == 'deferred')
    auto_batch_count = sum(1 for row in rows if str(row.get('auto_approval_decision') or '').strip().lower() == 'auto_approve' and not bool(row.get('requires_manual')) and not bool(row.get('blocked_by')) and str(row.get('workflow_state') or '').strip().lower() in {'ready', 'queued'})

    gate_summary = _build_gate_consumption_summary(rows, label=label)
    advisory_summary = _summarize_rollout_advisories(rows, label=label)
    dominant_action = operator_policy_summary.get('dominant_action')
    dominant_route = operator_policy_summary.get('dominant_route')
    dominant_follow_up = operator_policy_summary.get('dominant_follow_up')
    dominant_priority = operator_policy_summary.get('dominant_priority')
    headline_bits = [f"{len(rows)} item(s)"]
    if dominant_action:
        headline_bits.append(f"action={dominant_action}")
    if dominant_route:
        headline_bits.append(f"route={dominant_route}")
    if dominant_follow_up:
        headline_bits.append(f"follow_up={dominant_follow_up}")
    headline_bits.append(f"blocked={blocked_count}/manual={manual_count}/ready={ready_count}")
    if gate_summary.get('auto_advance_allowed_count') or gate_summary.get('rollback_candidate_count'):
        headline_bits.append(
            f"auto={gate_summary.get('auto_advance_allowed_count', 0)}/rollback={gate_summary.get('rollback_candidate_count', 0)}"
        )
    if advisory_summary.get('dominant_action'):
        headline_bits.append(
            f"advisory={advisory_summary.get('dominant_action')}/{advisory_summary.get('dominant_urgency') or 'low'}"
        )

    return {
        'label': label,
        'item_count': len(rows),
        'headline': ' | '.join(headline_bits),
        'dominant_action': dominant_action,
        'dominant_route': dominant_route,
        'dominant_follow_up': dominant_follow_up,
        'dominant_priority': dominant_priority,
        'priority_mix': priority_mix,
        'status_overview': {
            'manual': manual_count,
            'blocked': blocked_count,
            'ready': ready_count,
            'queued': queued_count,
            'deferred': deferred_count,
            'auto_batch': auto_batch_count,
        },
        'lane_mix': lane_counts,
        'workflow_state_mix': workflow_state_counts,
        'approval_state_mix': approval_state_counts,
        'risk_level_mix': risk_level_counts,
        'action_type_mix': action_type_counts,
        'bucket_mix': bucket_counts,
        'owner_mix': owner_counts,
        'reason_codes': operator_policy_summary.get('reason_codes') or [],
        'gate_consumption': gate_summary,
        'rollout_advisory': advisory_summary,
    }


def _resolve_lane_routing(*, workflow_item: Optional[Dict[str, Any]] = None, approval_item: Optional[Dict[str, Any]] = None,
                          row: Optional[Dict[str, Any]] = None, operator_action_policy: Optional[Dict[str, Any]] = None,
                          stage_loop: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    workflow_item = workflow_item or {}
    approval_item = approval_item or {}
    row = row or {}
    state_machine = workflow_item.get('state_machine') or approval_item.get('state_machine') or row.get('state_machine') or {}
    queue_progression = workflow_item.get('queue_progression') or approval_item.get('queue_progression') or row.get('queue_progression') or {}
    queue_plan = row.get('queue_plan') or workflow_item.get('queue_plan') or approval_item.get('queue_plan') or {}
    stage_model = workflow_item.get('stage_model') or approval_item.get('stage_model') or row.get('stage_model') or {}
    auto_decision = str(workflow_item.get('auto_approval_decision') or approval_item.get('auto_approval_decision') or row.get('auto_approval_decision') or 'manual_review').strip().lower() or 'manual_review'
    workflow_state = str(workflow_item.get('workflow_state') or approval_item.get('workflow_state') or row.get('workflow_state') or 'pending').strip().lower() or 'pending'
    approval_state = str(approval_item.get('approval_state') or row.get('approval_state') or 'not_required').strip().lower() or 'not_required'
    current_stage = str(workflow_item.get('current_rollout_stage') or approval_item.get('rollout_stage') or row.get('current_rollout_stage') or row.get('rollout_stage') or stage_model.get('current_stage') or 'pending').strip().lower() or 'pending'
    target_stage = str(workflow_item.get('target_rollout_stage') or approval_item.get('target_rollout_stage') or row.get('target_rollout_stage') or row.get('rollout_stage') or stage_model.get('target_stage') or current_stage).strip().lower() or current_stage
    blocked_by = _dedupe_strings(list(workflow_item.get('blocking_reasons') or []) + list(approval_item.get('blocked_by') or []) + list(row.get('blocked_by') or []))
    requires_manual = bool(workflow_item.get('requires_manual', approval_item.get('requires_manual', row.get('requires_manual'))))
    approval_required = bool(workflow_item.get('approval_required', approval_item.get('approval_required', row.get('approval_required'))))
    gates = _extract_rollout_gate_snapshot(workflow_item, approval_item, row)
    auto_gate = gates.get('auto_advance_gate') or {}
    rollback_gate = gates.get('rollback_gate') or {}
    validation_gate = _resolve_validation_gate_context(workflow_item, approval_item, row, auto_gate, rollback_gate, state_machine)
    stage_loop = dict(stage_loop or row.get('stage_loop') or _resolve_stage_loop_snapshot(workflow_item=workflow_item, approval_item=approval_item, row=row))
    operator_action_policy = dict(operator_action_policy or state_machine.get('operator_action_policy') or row.get('operator_action_policy') or _build_operator_action_policy(
        item_id=workflow_item.get('item_id') or approval_item.get('playbook_id') or row.get('item_id'),
        approval_state=approval_state,
        workflow_state=workflow_state,
        queue_status=queue_progression.get('status'),
        dispatch_route=queue_progression.get('dispatch_route') or state_machine.get('dispatch_route'),
        next_transition=queue_progression.get('next_transition') or state_machine.get('next_transition'),
        blocked_by=blocked_by,
        retryable=state_machine.get('retryable'),
        rollout_stage=current_stage,
        target_rollout_stage=target_stage,
        terminal=bool(state_machine.get('terminal')),
        validation_gate=validation_gate,
    ))
    dispatch_route = queue_progression.get('dispatch_route') or queue_plan.get('dispatch_route') or operator_action_policy.get('route') or state_machine.get('dispatch_route')
    next_transition = queue_progression.get('next_transition') or queue_plan.get('next_transition') or stage_loop.get('next_transition') or operator_action_policy.get('follow_up') or state_machine.get('next_transition')
    queue_status = str(queue_progression.get('status') or queue_plan.get('status') or workflow_state or 'pending').strip().lower() or 'pending'
    queue_name = queue_plan.get('queue_name') or queue_progression.get('queue_name') or dispatch_route or operator_action_policy.get('route') or 'observe_only_followup'
    route_family = str(dispatch_route or operator_action_policy.get('route') or queue_name or 'observe_only_followup').strip().lower() or 'observe_only_followup'
    validation_freeze = bool((auto_gate.get('validation_gate') or {}).get('freeze_auto_advance') or validation_gate.get('freeze_auto_advance'))
    validation_regression = bool((rollback_gate.get('validation_gate') or {}).get('regression_detected') or 'validation_gate_regressed' in (rollback_gate.get('triggered') or []))
    if not validation_regression and validation_gate.get('regression_detected') and workflow_state in {'ready', 'queued', 'review_pending', 'execution_failed', 'rollback_pending', 'rolled_back'}:
        validation_regression = True
    lane_id = 'ready'
    lane_reason = 'workflow_ready_default'
    if rollback_gate.get('candidate') or validation_regression or stage_loop.get('loop_state') == 'rollback_prepare' or workflow_state in {'rollback_pending', 'execution_failed', 'rolled_back'}:
        lane_id = 'rollback_candidate'
        lane_reason = 'validation_gate_regression_or_recovery_state' if validation_regression else 'rollback_gate_or_recovery_state'
    elif (requires_manual and approval_state == 'pending') or workflow_state == 'blocked_by_approval' or route_family == 'manual_approval_queue' or (stage_loop.get('loop_state') == 'review_pending' and (approval_required or requires_manual)):
        lane_id = 'manual_approval'
        lane_reason = 'manual_gate_pending'
    elif validation_freeze and operator_action_policy.get('action') == 'review_schedule':
        lane_id = 'blocked'
        lane_reason = 'validation_gate_frozen_review_queue'
    elif blocked_by or workflow_state in {'blocked', 'deferred', 'review_pending'} or route_family in {'operator_escalation', 'freeze_followup_queue', 'review_schedule_queue', 'validation_review_queue', 'deferred_review_queue', 'deferred_hold_queue'}:
        lane_id = 'blocked'
        lane_reason = 'blocked_or_review_followup'
    elif auto_gate.get('allowed') or stage_loop.get('loop_state') == 'auto_advance' or (auto_decision == 'auto_approve' and not requires_manual and not blocked_by and workflow_state in {'ready', 'queued'} and not validation_freeze):
        lane_id = 'auto_batch'
        lane_reason = 'gate_allows_auto_advance'
    elif workflow_state == 'queued' or queue_status in {'queued', 'ready_to_queue'} or route_family in {'queue_observer', 'stage_promotion_queue', 'operator_followup_queue'}:
        lane_id = 'queued'
        lane_reason = 'queue_progression_active'
    lane_title_map = {
        'auto_batch': 'auto batch',
        'rollback_candidate': 'rollback candidate',
        'blocked': 'blocked',
        'queued': 'queued',
        'ready': 'ready',
        'manual_approval': 'manual approval',
    }
    summary_bits = [f"{lane_id} via {route_family} ({queue_status})"]
    if validation_regression:
        summary_bits.append('validation_gate=rollback_candidate')
    elif validation_freeze:
        summary_bits.append('validation_gate=freeze')
    elif validation_gate.get('enabled'):
        summary_bits.append('validation_gate=ready')
    return {
        'schema_version': 'm5_lane_routing_v1',
        'item_id': workflow_item.get('item_id') or approval_item.get('playbook_id') or row.get('item_id'),
        'lane_id': lane_id,
        'lane_title': lane_title_map.get(lane_id, lane_id.replace('_', ' ')),
        'lane_reason': lane_reason,
        'queue_name': queue_name,
        'queue_status': queue_status,
        'dispatch_route': dispatch_route,
        'route_family': route_family,
        'next_transition': next_transition,
        'workflow_state': workflow_state,
        'approval_state': approval_state,
        'current_rollout_stage': current_stage,
        'target_rollout_stage': target_stage,
        'auto_approval_decision': auto_decision,
        'auto_advance_allowed': bool(auto_gate.get('allowed')),
        'rollback_candidate': bool(rollback_gate.get('candidate') or validation_regression),
        'blocked_by': blocked_by,
        'stage_loop_state': stage_loop.get('loop_state'),
        'validation_gate': validation_gate,
        'validation_status': validation_gate.get('status'),
        'validation_freeze': validation_freeze,
        'validation_regression': validation_regression,
        'recommended_action': stage_loop.get('recommended_action') or operator_action_policy.get('action'),
        'operator_action': operator_action_policy.get('action'),
        'operator_route': operator_action_policy.get('route'),
        'operator_follow_up': operator_action_policy.get('follow_up'),
        'summary': ' / '.join(summary_bits),
    }


def _extract_rollout_gate_snapshot(*rows: Any) -> Dict[str, Any]:
    auto_gate: Dict[str, Any] = {}
    rollback_gate: Dict[str, Any] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        state_machine = row.get('state_machine') or {}
        if isinstance(state_machine, dict):
            auto_gate = auto_gate or (state_machine.get('auto_advance_gate') or {})
            rollback_gate = rollback_gate or (state_machine.get('rollback_gate') or {})
        auto_gate = auto_gate or (row.get('auto_advance_gate') or {})
        rollback_gate = rollback_gate or (row.get('rollback_gate') or {})
    return {
        'auto_advance_gate': dict(auto_gate or {}),
        'rollback_gate': dict(rollback_gate or {}),
    }


def _build_gate_consumption_summary(rows: Optional[List[Dict[str, Any]]], *, label: Optional[str] = None,
                                    max_items: int = 5, max_reasons: int = 10) -> Dict[str, Any]:
    rows = rows or []
    auto_allowed_items = []
    rollback_candidate_items = []
    blocker_counts: Dict[str, int] = {}
    trigger_counts: Dict[str, int] = {}
    readiness_scores: List[int] = []
    for row in rows:
        gates = _extract_rollout_gate_snapshot(row)
        auto_gate = gates.get('auto_advance_gate') or {}
        rollback_gate = gates.get('rollback_gate') or {}
        if auto_gate.get('allowed'):
            auto_allowed_items.append(row)
        if rollback_gate.get('candidate'):
            rollback_candidate_items.append(row)
        for blocker in auto_gate.get('blockers') or []:
            blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
        for trigger in rollback_gate.get('triggered') or []:
            trigger_counts[trigger] = trigger_counts.get(trigger, 0) + 1
        try:
            readiness_scores.append(int(auto_gate.get('readiness_score') or 0))
        except Exception:
            pass

    blocker_counts = dict(sorted(blocker_counts.items(), key=lambda item: (-item[1], item[0])))
    trigger_counts = dict(sorted(trigger_counts.items(), key=lambda item: (-item[1], item[0])))
    auto_items = sorted(auto_allowed_items, key=_workbench_item_sort_key)[:max_items] if rows else []
    rollback_items = sorted(rollback_candidate_items, key=_workbench_item_sort_key)[:max_items] if rows else []
    dominant_blocker = next(iter(blocker_counts), None)
    dominant_trigger = next(iter(trigger_counts), None)
    return {
        'label': label,
        'item_count': len(rows),
        'auto_advance_allowed_count': len(auto_allowed_items),
        'rollback_candidate_count': len(rollback_candidate_items),
        'blocked_auto_advance_count': max(0, len(rows) - len(auto_allowed_items)),
        'dominant_auto_advance_blocker': dominant_blocker,
        'dominant_rollback_trigger': dominant_trigger,
        'auto_advance_blocker_counts': blocker_counts,
        'rollback_trigger_counts': trigger_counts,
        'top_auto_advance_blockers': list(blocker_counts.keys())[:max_reasons],
        'top_rollback_triggers': list(trigger_counts.keys())[:max_reasons],
        'avg_readiness_score': round(sum(readiness_scores) / len(readiness_scores), 4) if readiness_scores else 0,
        'auto_advance_items': auto_items,
        'rollback_candidate_items': rollback_items,
    }


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
            'workflow_state': _normalize_workflow_state(workflow_item.get('workflow_state') or row.get('decision_state'), approval_state=row.get('approval_state') or 'pending'),
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
        row['queue_progression'] = persisted_details.get('queue_progression') or row.get('queue_progression') or {}
        row['stage_model'] = persisted_details.get('stage_model') or row.get('stage_model') or {}
        row['execution_status'] = persisted_details.get('execution_status') or ((persisted_details.get('state_machine') or {}).get('execution_status') if isinstance(persisted_details.get('state_machine'), dict) else None) or row.get('execution_status')
        row['transition_rule'] = persisted_details.get('transition_rule') or ((persisted_details.get('state_machine') or {}).get('transition_rule') if isinstance(persisted_details.get('state_machine'), dict) else None) or row.get('transition_rule')
        row['next_transition'] = persisted_details.get('next_transition') or ((persisted_details.get('state_machine') or {}).get('next_transition') if isinstance(persisted_details.get('state_machine'), dict) else None) or row.get('next_transition')
        row['last_transition'] = persisted_details.get('last_transition') or ((persisted_details.get('state_machine') or {}).get('last_transition') if isinstance(persisted_details.get('state_machine'), dict) else None) or row.get('last_transition') or {}
        row['auto_advance_gate'] = persisted_details.get('auto_advance_gate') or ((persisted_details.get('state_machine') or {}).get('auto_advance_gate') if isinstance(persisted_details.get('state_machine'), dict) else None) or row.get('auto_advance_gate') or {}
        row['rollback_gate'] = persisted_details.get('rollback_gate') or ((persisted_details.get('state_machine') or {}).get('rollback_gate') if isinstance(persisted_details.get('state_machine'), dict) else None) or row.get('rollback_gate') or {}
        row['validation_gate'] = persisted_details.get('validation_gate') or (row.get('auto_advance_gate') or {}).get('validation_gate') or (row.get('rollback_gate') or {}).get('validation_gate') or row.get('validation_gate') or {}
        row['stage_loop'] = persisted_details.get('stage_loop') or ((persisted_details.get('state_machine') or {}).get('stage_loop') if isinstance(persisted_details.get('state_machine'), dict) else None) or row.get('stage_loop') or {}
        row['auto_promotion_execution'] = persisted_details.get('auto_promotion_execution') or row.get('auto_promotion_execution') or {}
        row['promotion_execution_status'] = (row.get('auto_promotion_execution') or {}).get('after', {}).get('workflow_state') or row.get('promotion_execution_status')
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
        row['execution_status'] = approval_row.get('execution_status') or row.get('execution_status')
        row['transition_rule'] = approval_row.get('transition_rule') or row.get('transition_rule')
        row['next_transition'] = approval_row.get('next_transition') or row.get('next_transition')
        row['last_transition'] = approval_row.get('last_transition') or row.get('last_transition') or {}
        row['auto_advance_gate'] = approval_row.get('auto_advance_gate') or row.get('auto_advance_gate') or {}
        row['rollback_gate'] = approval_row.get('rollback_gate') or row.get('rollback_gate') or {}
        row['validation_gate'] = approval_row.get('validation_gate') or row.get('validation_gate') or {}
        row['stage_loop'] = approval_row.get('stage_loop') or row.get('stage_loop') or {}
        if approval_row.get('persisted_workflow_state'):
            row['workflow_state'] = _normalize_workflow_state(approval_row.get('persisted_workflow_state'), approval_state=approval_row.get('approval_state'))
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

    for row in approval_state.get('items') or []:
        semantics = _build_state_machine_semantics(
            item_id=row.get('approval_id') or row.get('item_id') or row.get('playbook_id'),
            approval_state=row.get('approval_state') or row.get('persisted_state') or row.get('state'),
            workflow_state=row.get('persisted_workflow_state') or row.get('workflow_state') or row.get('decision_state'),
            decision_state=row.get('decision_state') or row.get('approval_state'),
            queue_status=((row.get('queue_progression') or {}).get('status') if isinstance(row.get('queue_progression'), dict) else None),
            dispatch_route=((row.get('queue_progression') or {}).get('dispatch_route') if isinstance(row.get('queue_progression'), dict) else None),
            next_transition=((row.get('queue_progression') or {}).get('next_transition') if isinstance(row.get('queue_progression'), dict) else None),
            rollout_stage=row.get('rollout_stage') or row.get('current_rollout_stage'),
            target_rollout_stage=row.get('target_rollout_stage'),
            blocked_by=row.get('blocked_by') or [],
            execution_status=row.get('execution_status') or persisted_details.get('execution_status') if 'persisted_details' in locals() else row.get('execution_status'),
            transition_rule=row.get('transition_rule'),
            last_transition=row.get('last_transition'),
            validation_gate=row.get('validation_gate'),
        )
        row['state_machine'] = semantics
        row['workflow_state'] = semantics['workflow_state']
    for row in workflow_state.get('item_states') or []:
        semantics = _build_state_machine_semantics(
            item_id=row.get('item_id'),
            approval_state=row.get('approval_state') or row.get('persisted_approval_state') or 'pending',
            workflow_state=row.get('workflow_state'),
            decision_state=row.get('decision_state') or row.get('approval_state'),
            queue_status=((row.get('queue_progression') or {}).get('status') if isinstance(row.get('queue_progression'), dict) else None),
            dispatch_route=((row.get('queue_progression') or {}).get('dispatch_route') if isinstance(row.get('queue_progression'), dict) else None),
            next_transition=((row.get('queue_progression') or {}).get('next_transition') if isinstance(row.get('queue_progression'), dict) else None),
            rollout_stage=row.get('current_rollout_stage') or row.get('rollout_stage'),
            target_rollout_stage=row.get('target_rollout_stage'),
            blocked_by=(row.get('blocking_reasons') or []) + (row.get('blocked_by') or []),
            validation_gate=row.get('validation_gate'),
        )
        row['state_machine'] = semantics
        row['workflow_state'] = semantics['workflow_state']

    workflow_summary = workflow_state.get('summary') or {}
    workflow_items = workflow_state.get('item_states') or []
    workflow_summary['ready_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'ready')
    workflow_summary['pending_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'pending')
    workflow_summary['blocked_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'blocked')
    workflow_summary['approved_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'approved')
    workflow_summary['rejected_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'rejected')
    workflow_summary['deferred_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'deferred')
    workflow_summary['queued_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'queued')
    workflow_summary['retry_pending_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'retry_pending')
    workflow_summary['execution_failed_count'] = sum(1 for row in workflow_items if row.get('workflow_state') == 'execution_failed')
    action_counts = {}
    route_counts = {}
    follow_up_counts = {}
    for row in workflow_items:
        policy = ((row.get('state_machine') or {}).get('operator_action_policy') or {})
        action = policy.get('action') or 'observe_only_followup'
        route = policy.get('route') or 'observe_only_followup'
        follow_up = policy.get('follow_up') or 'observe_only'
        action_counts[action] = action_counts.get(action, 0) + 1
        route_counts[route] = route_counts.get(route, 0) + 1
        follow_up_counts[follow_up] = follow_up_counts.get(follow_up, 0) + 1
    workflow_summary['state_machine'] = {
        'schema_version': 'm5_unified_state_machine_v1',
        'terminal_count': sum(1 for row in workflow_items if (row.get('state_machine') or {}).get('terminal')),
        'blocked_count': sum(1 for row in workflow_items if (row.get('state_machine') or {}).get('blocked')),
        'rollback_candidate_count': sum(1 for row in workflow_items if (row.get('state_machine') or {}).get('rollback_candidate')),
        'retryable_count': sum(1 for row in workflow_items if (row.get('state_machine') or {}).get('retryable')),
        'recovered_count': sum(1 for row in workflow_items if ((row.get('state_machine') or {}).get('execution_timeline') or {}).get('recovered')),
        'recovery_policy_counts': {
            key: sum(1 for row in workflow_items if (((row.get('state_machine') or {}).get('recovery_policy') or {}).get('policy') or 'observe') == key)
            for key in sorted({(((row.get('state_machine') or {}).get('recovery_policy') or {}).get('policy') or 'observe') for row in workflow_items})
        },
        'recovery_queue_counts': {
            key: sum(1 for row in workflow_items if (((row.get('state_machine') or {}).get('recovery_orchestration') or {}).get('queue_bucket') or 'observe') == key)
            for key in sorted({(((row.get('state_machine') or {}).get('recovery_orchestration') or {}).get('queue_bucket') or 'observe') for row in workflow_items})
        },
        'execution_status_counts': {
            key: sum(1 for row in workflow_items if (((row.get('state_machine') or {}).get('execution_timeline') or {}).get('latest_status') or 'unknown') == key)
            for key in sorted({(((row.get('state_machine') or {}).get('execution_timeline') or {}).get('latest_status') or 'unknown') for row in workflow_items})
        },
        'phases': sorted({(row.get('state_machine') or {}).get('phase') or 'unknown' for row in workflow_items}),
        'operator_action_counts': action_counts,
        'operator_route_counts': route_counts,
        'follow_up_counts': follow_up_counts,
    }
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


def _recommend_rollout_stage_advisory(*, stage_key: str, current_stage: str, target_stage: str, workflow_state: Optional[str],
                                     waiting_on: Optional[List[str]] = None, rollback_candidate: bool = False,
                                     review_overdue: bool = False, validation_gate: Optional[Dict[str, Any]] = None,
                                     auto_advance_allowed: Optional[bool] = None, readiness_score: Optional[int] = None,
                                     eligible: Optional[bool] = None, auto_approve: Optional[bool] = None,
                                     low_risk: Optional[bool] = None, execution_status: Optional[str] = None) -> Dict[str, Any]:
    waiting = _dedupe_strings(waiting_on or [])
    normalized_workflow_state = str(workflow_state or '').strip().lower() or 'pending'
    validation_gate = dict(validation_gate or {})
    validation_enabled = bool(validation_gate.get('enabled'))
    validation_ready = validation_gate.get('ready')
    validation_stale = bool(validation_gate.get('stale'))
    validation_regressed = bool(validation_gate.get('rollback_on_regression')) and validation_ready is False and not validation_stale
    recommended_stage = target_stage or stage_key or current_stage or 'observe'
    recommended_action = 'continue_observe'
    urgency = 'low'
    confidence = 0.55
    reasons: List[str] = []

    if rollback_candidate or normalized_workflow_state in {'execution_failed', 'rollback_pending'} or validation_regressed:
        recommended_stage = 'rollback_prepare'
        recommended_action = 'prepare_rollback_review'
        urgency = 'critical'
        confidence = 0.97
        reasons.extend([reason for reason in waiting if reason.startswith('workflow_state:') or reason == 'rollback_gate_triggered'])
        if validation_regressed:
            reasons.append('validation_gate_regressed')
        if execution_status == 'error':
            reasons.append('execution_status:error')
    elif review_overdue:
        recommended_stage = 'review_pending'
        recommended_action = 'manual_review_now'
        urgency = 'high'
        confidence = 0.92
        reasons.append('review_overdue')
    elif validation_enabled and validation_ready is False:
        recommended_stage = current_stage or 'observe'
        recommended_action = 'freeze_auto_advance'
        urgency = 'high' if validation_stale else 'medium'
        confidence = 0.9 if validation_stale else 0.86
        reasons.extend(_dedupe_strings((validation_gate.get('reasons') or []) + (['validation_gate_not_ready'] if 'validation_gate_not_ready' not in (validation_gate.get('reasons') or []) else [])))
    elif waiting:
        recommended_stage = current_stage or stage_key or 'observe'
        recommended_action = 'hold_until_blockers_clear'
        urgency = 'medium'
        confidence = 0.8
        reasons.extend(waiting)
    elif auto_advance_allowed:
        recommended_action = {
            'observe': 'promote_candidate',
            'candidate': 'queue_safe_promotion',
            'guarded_prepare': 'promote_to_controlled_apply',
            'controlled_apply': 'move_to_review_pending',
            'review_pending': 'complete_review_checkpoint',
            'rollback_prepare': 'execute_or_queue_rollback',
        }.get(stage_key, 'continue_monitoring')
        urgency = 'medium' if stage_key in {'observe', 'candidate'} else 'high'
        confidence = 0.88
        if readiness_score is not None:
            confidence = min(0.99, max(confidence, round(float(readiness_score) / 100.0, 4)))
        reasons.extend([
            'auto_advance_allowed',
            f'eligible:{bool(eligible)}',
            f'auto_approve:{bool(auto_approve)}',
            f'low_risk:{bool(low_risk)}',
        ])
    else:
        recommended_action = 'collect_more_signal'
        urgency = 'low'
        confidence = 0.6
        if readiness_score is not None:
            reasons.append(f'readiness_score:{readiness_score}')

    reasons = _dedupe_strings(reasons)
    return {
        'recommended_stage': recommended_stage,
        'recommended_action': recommended_action,
        'urgency': urgency,
        'confidence': round(confidence, 4),
        'reasons': reasons,
        'ready_for_live_promotion': bool(auto_advance_allowed and not waiting and not rollback_candidate and not review_overdue and (validation_ready is not False)),
    }


def _resolve_rollout_stage_handler(*, current_stage: Optional[str], target_stage: Optional[str], action_type: Optional[str],
                                  workflow_state: Optional[str], blocked_by: Optional[List[str]] = None,
                                  review_due_at: Optional[str] = None, rollback_candidate: bool = False,
                                  validation_gate: Optional[Dict[str, Any]] = None, auto_advance_allowed: Optional[bool] = None,
                                  readiness_score: Optional[int] = None, eligible: Optional[bool] = None,
                                  auto_approve: Optional[bool] = None, low_risk: Optional[bool] = None,
                                  execution_status: Optional[str] = None) -> Dict[str, Any]:
    blocked = _dedupe_strings(blocked_by or [])
    normalized_current = str(current_stage or '').strip().lower() or 'observe'
    normalized_target = str(target_stage or '').strip().lower() or normalized_current
    stage_key = normalized_target if normalized_target in ROLLOUT_STAGE_HANDLER_SPECS else normalized_current
    spec = dict(ROLLOUT_STAGE_HANDLER_SPECS.get(stage_key) or ROLLOUT_STAGE_HANDLER_SPECS['observe'])
    owner = spec.get('owner') or ('operator' if rollback_candidate else 'system')
    review_overdue = False
    if review_due_at:
        try:
            due_dt = datetime.fromisoformat(str(review_due_at).replace('Z', '+00:00'))
            review_overdue = due_dt <= datetime.now(timezone.utc)
        except Exception:
            review_overdue = False
    waiting_on = []
    if blocked:
        waiting_on.extend([f'blocked_by:{code}' for code in blocked])
    normalized_workflow_state = str(workflow_state or '').strip().lower()
    if normalized_workflow_state in {'blocked', 'blocked_by_approval', 'execution_failed', 'rollback_pending'}:
        waiting_on.append(f'workflow_state:{normalized_workflow_state}')
    if review_overdue:
        waiting_on.append('review_overdue')
    if rollback_candidate and 'rollback_gate_triggered' not in waiting_on:
        waiting_on.append('rollback_gate_triggered')
    why_stopped = 'ready_for_progression'
    if review_overdue:
        why_stopped = 'review_checkpoint_overdue'
    elif rollback_candidate:
        why_stopped = 'rollback_gate_open'
    elif blocked:
        why_stopped = 'stage_blocked_by_preconditions'
    elif normalized_workflow_state in {'blocked_by_approval', 'execution_failed'}:
        why_stopped = f'workflow_state:{normalized_workflow_state}'
    next_transition = 'continue_monitoring'
    if rollback_candidate:
        next_transition = 'prepare_rollback_review'
    elif stage_key == 'observe':
        next_transition = 'promote_candidate' if not waiting_on else 'continue_observe'
    elif stage_key == 'candidate':
        next_transition = 'queue_safe_promotion' if not waiting_on else 'wait_for_candidate_readiness'
    elif stage_key == 'guarded_prepare':
        next_transition = 'promote_to_controlled_apply' if not waiting_on else 'hold_guarded_prepare'
    elif stage_key == 'controlled_apply':
        next_transition = 'move_to_review_pending' if not waiting_on else 'collect_post_apply_samples'
    elif stage_key == 'review_pending':
        next_transition = 'complete_review_checkpoint' if not waiting_on and not review_overdue else 'await_review_checkpoint'
    elif stage_key == 'rollback_prepare':
        next_transition = 'execute_or_queue_rollback'
    advisory = _recommend_rollout_stage_advisory(
        stage_key=stage_key,
        current_stage=normalized_current,
        target_stage=normalized_target,
        workflow_state=normalized_workflow_state,
        waiting_on=waiting_on,
        rollback_candidate=rollback_candidate,
        review_overdue=review_overdue,
        validation_gate=validation_gate,
        auto_advance_allowed=auto_advance_allowed,
        readiness_score=readiness_score,
        eligible=eligible,
        auto_approve=auto_approve,
        low_risk=low_risk,
        execution_status=execution_status,
    )
    return {
        'stage_key': stage_key,
        'current_stage': normalized_current,
        'target_stage': normalized_target,
        'owner': owner,
        'auto_progression': bool(spec.get('auto_progression', False) and owner == 'system' and not rollback_candidate),
        'enter_conditions': spec.get('enter_conditions') or [],
        'stay_conditions': spec.get('stay_conditions') or [],
        'exit_conditions': spec.get('exit_conditions') or [],
        'review_due_strategy': spec.get('review_due_strategy'),
        'rollback_stage': spec.get('rollback_stage') or 'observe',
        'rollback_candidate': bool(rollback_candidate),
        'review_due_at': review_due_at,
        'review_overdue': review_overdue,
        'waiting_on': waiting_on,
        'why_stopped': why_stopped,
        'next_transition': next_transition,
        'responsible_actor': owner,
        'action_type': str(action_type or '').strip().lower() or None,
        'advisory': advisory,
    }


def _build_rollout_gate_policy(spec: Optional[Dict[str, Any]], handler: Optional[Dict[str, Any]], *, allowlisted: bool) -> Dict[str, Any]:
    spec = dict(spec or {})
    handler = dict(handler or {})
    dispatch_mode = spec.get('dispatch_mode') or handler.get('disposition') or 'unsupported'
    safe_boundary = handler.get('safe_boundary') or ('metadata_only' if dispatch_mode == 'apply' else 'queue_only')
    policy = {
        'preconditions': [
            'allowlisted_action',
            'auto_approval_eligible',
            'auto_approval_decision_auto_approve',
            'risk_level_low',
            'no_blockers',
            'manual_gate_cleared',
        ],
        'auto_advance': {
            'allowed': bool(allowlisted and dispatch_mode == 'apply'),
            'mode': 'very_safe_apply' if dispatch_mode == 'apply' else 'manual_followup',
            'safe_boundary': safe_boundary,
            'required_flags': ['allowlisted', 'eligible', 'auto_approve', 'low_risk', 'no_blockers', 'no_manual_gate'],
        },
        'rollback': {
            'capable': bool(spec.get('rollback_capable', False)),
            'safe_boundary': safe_boundary,
            'trigger_factors': ['execution_error', 'critical_risk', 'blocked_transition', 'review_overdue', 'rollback_pending'],
        },
        'review': {
            'requires_review_window': dispatch_mode == 'apply',
            'default_review_after_hours': 24 if dispatch_mode == 'apply' else None,
        },
        'idempotency_rule': 'same action_type + target state/workflow_state only applies once; repeated executor runs must idempotent-skip',
    }
    if spec.get('blocked_reason'):
        policy['manual_gate_reason'] = spec.get('blocked_reason')
    return policy



def _evaluate_rollout_gates(*, action_type: str, row: Dict[str, Any], workflow_item: Dict[str, Any], spec: Optional[Dict[str, Any]],
                            handler: Dict[str, Any], allowlisted: bool, current_state: str, current_workflow_state: str,
                            auto_decision: str, eligible: bool, approval_required: bool, requires_manual: bool,
                            blocked_by: Optional[List[str]] = None, risk_level: Optional[str] = None,
                            transition_rule: Optional[Dict[str, Any]] = None, persisted_details: Optional[Dict[str, Any]] = None,
                            validation_gate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    spec = dict(spec or {})
    handler = dict(handler or {})
    transition_rule = dict(transition_rule or {})
    persisted_details = dict(persisted_details or {})
    blocked = _dedupe_strings(blocked_by or [])
    normalized_risk = str(risk_level or '').strip().lower() or 'unknown'
    review_due_at = row.get('review_due_at') or workflow_item.get('review_due_at') or persisted_details.get('review_due_at')
    review_overdue = False
    if review_due_at:
        try:
            due_dt = datetime.fromisoformat(str(review_due_at).replace('Z', '+00:00'))
            review_overdue = due_dt <= datetime.now(timezone.utc)
        except Exception:
            review_overdue = False
    execution_status = _normalize_action_execution_status(
        persisted_details.get('execution_status'),
        workflow_state=current_workflow_state,
        queue_status=((workflow_item.get('queue_progression') or {}).get('status') if isinstance(workflow_item.get('queue_progression'), dict) else None),
        executor_status=persisted_details.get('executor_status'),
    )
    validation_gate = dict(validation_gate or {})
    validation_gate_enabled = bool(validation_gate.get('enabled'))
    validation_gate_ready = validation_gate.get('ready')
    manual_required = bool(approval_required or requires_manual or auto_decision == 'manual_review')
    low_risk = normalized_risk == 'low'
    auto_approve = auto_decision == 'auto_approve'
    dispatch_mode = spec.get('dispatch_mode') or handler.get('disposition') or 'unsupported'
    safe_boundary = handler.get('safe_boundary') or ('metadata_only' if dispatch_mode == 'apply' else 'queue_only')
    validation_freeze = bool(validation_gate.get('freeze_auto_advance')) and dispatch_mode == 'apply'
    validation_ready = (validation_gate_ready is not False) if validation_gate_enabled else True
    readiness_score = 0
    readiness_score += 15 if allowlisted else 0
    readiness_score += 15 if eligible else 0
    readiness_score += 15 if auto_approve else 0
    readiness_score += 15 if low_risk else 0
    readiness_score += 10 if not blocked else 0
    readiness_score += 10 if not manual_required else 0
    readiness_score += 20 if validation_ready else 0
    readiness_score = max(0, min(readiness_score, 100))
    blockers = []
    if not allowlisted:
        blockers.append('action_not_allowlisted')
    if not eligible:
        blockers.append('not_auto_approval_eligible')
    if not auto_approve:
        blockers.append(f'auto_decision:{auto_decision or "unknown"}')
    if not low_risk:
        blockers.append(f'risk_level:{normalized_risk}')
    if blocked:
        blockers.extend([f'blocked_by:{code}' for code in blocked])
    if manual_required:
        blockers.append('manual_gate_required')
    if validation_freeze:
        blockers.append('validation_gate:not_ready')
        blockers.extend([f'validation:{reason}' for reason in (validation_gate.get('reasons') or [])])
    if dispatch_mode != 'apply':
        blockers.append(f'dispatch_mode:{dispatch_mode}')
    if current_state in TERMINAL_APPROVAL_STATES:
        blockers.append(f'terminal_state:{current_state}')
    auto_allowed = not blockers
    rollback_triggers = []
    if persisted_details.get('execution_status') == 'error' or current_workflow_state == 'execution_failed' or execution_status == 'error':
        rollback_triggers.append('execution_error')
    if current_workflow_state == 'rollback_pending':
        rollback_triggers.append('rollback_pending')
    if normalized_risk == 'critical':
        rollback_triggers.append('critical_risk')
    if blocked and transition_rule.get('dispatch_route') in {'stage_metadata_apply', 'safe_state_apply'}:
        rollback_triggers.append('blocked_transition')
    if review_overdue:
        rollback_triggers.append('review_overdue')
    if validation_gate_enabled and validation_gate.get('rollback_on_regression') and dispatch_mode == 'apply' and current_workflow_state in {'ready', 'queued', 'review_pending', 'execution_failed', 'rollback_pending'}:
        rollback_triggers.append('validation_gate_regressed')
    rollback_candidate = bool(spec.get('rollback_capable', False) and rollback_triggers)
    stage_handler = _resolve_rollout_stage_handler(
        current_stage=transition_rule.get('rollout_stage') or row.get('current_rollout_stage') or workflow_item.get('current_rollout_stage'),
        target_stage=transition_rule.get('target_rollout_stage') or row.get('target_rollout_stage') or workflow_item.get('target_rollout_stage'),
        action_type=action_type,
        workflow_state=current_workflow_state,
        blocked_by=blocked,
        review_due_at=review_due_at,
        rollback_candidate=rollback_candidate,
        validation_gate=validation_gate,
        auto_advance_allowed=auto_allowed,
        readiness_score=readiness_score,
        eligible=eligible,
        auto_approve=auto_approve,
        low_risk=low_risk,
        execution_status=execution_status,
    )
    return {
        'auto_advance_gate': {
            'allowed': auto_allowed,
            'mode': 'very_safe_apply' if auto_allowed else 'hold',
            'safe_boundary': safe_boundary,
            'readiness_score': readiness_score,
            'manual_required': manual_required,
            'cooldown_active': False,
            'review_window_open': not review_overdue,
            'blockers': blockers,
            'required_flags': {
                'allowlisted': allowlisted,
                'eligible': bool(eligible),
                'auto_approve': auto_approve,
                'low_risk': low_risk,
                'no_blockers': not blocked,
                'no_manual_gate': not manual_required,
                'validation_gate_ready': validation_ready,
            },
            'validation_gate': _build_validation_gate_snapshot({'validation_gate': validation_gate}) if validation_gate_enabled else {'enabled': False, 'ready': None, 'freeze_auto_advance': False, 'rollback_on_regression': False, 'reasons': []},
            'explain': 'auto advance requires very-safe apply + low risk + no blockers + approval-eligible + no manual gate + validation gate ready',
        },
        'rollback_gate': {
            'candidate': rollback_candidate,
            'capable': bool(spec.get('rollback_capable', False)),
            'safe_boundary': safe_boundary,
            'triggered': rollback_triggers,
            'next_action': 'prepare_rollback_review' if rollback_candidate else None,
            'rollback_hint': transition_rule.get('rollback_hint') or 'restore_previous_state_from_approval_timeline',
            'validation_gate': _build_validation_gate_snapshot({'validation_gate': validation_gate}) if validation_gate_enabled else {'enabled': False, 'ready': None, 'freeze_auto_advance': False, 'rollback_on_regression': False, 'reasons': []},
            'explain': 'rollback opens on execution error, rollback pending, critical risk, blocked transition, overdue review, or validation gate regression',
        },
        'stage_handler': stage_handler,
    }



def _build_safe_rollout_action_registry(allowed_action_types: Optional[List[str]] = None) -> Dict[str, Any]:
    allow = set(_dedupe_strings(allowed_action_types or []))
    handlers = {key: dict(value) for key, value in SAFE_ROLLOUT_STAGE_HANDLER_REGISTRY.items()}
    actions = {}
    executable = []
    queue_only = []
    unsupported = []
    for action_type, spec in ROLLOUT_EXECUTOR_ACTION_SPECS.items():
        handler = _resolve_safe_rollout_handler(spec, action_type)
        allowlisted = action_type in allow
        gate_policy = _build_rollout_gate_policy(spec, handler, allowlisted=allowlisted)
        entry = {
            'action_type': action_type,
            'allowlisted': allowlisted,
            'dispatch_mode': spec.get('dispatch_mode') or handler.get('disposition'),
            'executor_class': spec.get('executor_class') or handler.get('executor_class'),
            'handler': handler,
            'handler_key': handler.get('handler_key'),
            'route': handler.get('route'),
            'stage_family': handler.get('stage_family'),
            'rollback_capable': bool(spec.get('rollback_capable', False)),
            'audit_code': spec.get('audit_code'),
            'transition_policy': _build_rollout_transition_policy_snapshot(spec, action_type),
            'gate_policy': gate_policy,
            'preconditions': gate_policy.get('preconditions') or [],
            'idempotency_rule': gate_policy.get('idempotency_rule'),
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
        queue_name = row.get('target_queue') or row.get('bucket_id') or workflow_item.get('bucket_id') or workflow_item.get('queue_name') or 'priority_queue'
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
                **_resolve_rollout_stage_handler(
                    current_stage=previous_stage,
                    target_stage=target_stage,
                    action_type=action_type,
                    workflow_state=workflow_item.get('workflow_state') or row.get('workflow_state') or 'pending',
                    blocked_by=row.get('blocked_by') or workflow_item.get('blocked_by') or workflow_item.get('blocking_reasons') or [],
                ),
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
        'auto_promote_ready_candidates': bool(raw.get('auto_promote_ready_candidates', False)),
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
    candidate_view = build_auto_promotion_candidate_view(payload, limit=max(len(workflow_lookup) or 0, len(approval_items) or 0, 50))
    candidate_items = candidate_view.get('items') or []
    candidate_by_approval = {row.get('approval_id'): row for row in candidate_items if row.get('approval_id')}
    candidate_by_playbook = {row.get('item_id'): row for row in candidate_items if row.get('item_id')}

    result = {
        'enabled': execution_settings.get('enabled', False),
        'mode': execution_settings.get('mode'),
        'actor': execution_settings.get('actor'),
        'source': execution_settings.get('source'),
        'replay_source': replay_source,
        'allowed_action_types': execution_settings.get('allowed_action_types') or [],
        'auto_promote_ready_candidates': bool(execution_settings.get('auto_promote_ready_candidates', False)),
        'candidate_summary': candidate_view.get('summary') or {},
        'executed_count': 0,
        'skipped_count': 0,
        'items': [],
    }
    payload['controlled_rollout_execution'] = result

    if not result['enabled'] or result['mode'] != 'state_apply' or not result['auto_promote_ready_candidates'] or not approval_items:
        result['skipped_count'] = len(approval_items)
        if not result['auto_promote_ready_candidates']:
            result['safety_switch'] = 'auto_promote_ready_candidates_disabled'
        return payload

    validation_gate = _build_validation_gate_snapshot(payload)
    execution_gate = _build_validation_execution_gate(validation_gate, layer='controlled_rollout_state_apply')
    result['validation_gate'] = execution_gate['validation_gate']
    result['execution_gate'] = execution_gate

    allowed_action_types = set(execution_settings.get('allowed_action_types') or [])
    executed_rows = []
    for row in approval_items:
        approval_id = row.get('approval_id') or row.get('item_id') or row.get('playbook_id')
        workflow_item = workflow_lookup.get(row.get('playbook_id')) or {}
        candidate = candidate_by_approval.get(approval_id) or candidate_by_playbook.get(row.get('playbook_id')) or {}
        persisted_row = db.get_approval_state(approval_id) if approval_id else None
        persisted_details = (persisted_row or {}).get('details') or {}
        action_type = str(row.get('action_type') or workflow_item.get('action_type') or (persisted_row or {}).get('approval_type') or '').strip().lower()
        action_spec = CONTROLLED_ROLLOUT_ACTION_SPECS.get(action_type)
        current_state = str((persisted_row or {}).get('state') or row.get('approval_state') or row.get('persisted_state') or row.get('state') or 'pending').strip().lower()
        current_workflow_state = str((persisted_row or {}).get('workflow_state') or row.get('persisted_workflow_state') or workflow_item.get('workflow_state') or row.get('decision_state') or 'pending').strip().lower()
        auto_decision = _normalize_auto_approval_decision(row.get('auto_approval_decision') or candidate.get('auto_approval_decision'))
        blocked_by = _dedupe_strings(row.get('blocked_by') or workflow_item.get('blocked_by') or workflow_item.get('blocking_reasons') or candidate.get('blocked_by') or [])
        risk_level = str(row.get('risk_level') or workflow_item.get('risk_level') or candidate.get('risk_level') or '').strip().lower()
        requires_manual = bool(row.get('requires_manual') if row.get('requires_manual') is not None else candidate.get('requires_manual'))
        approval_required = bool(row.get('approval_required') if row.get('approval_required') is not None else workflow_item.get('approval_required', candidate.get('approval_required')))
        eligible = bool(row.get('auto_approval_eligible') if row.get('auto_approval_eligible') is not None else candidate.get('auto_approval_eligible'))

        target_state = str((action_spec or {}).get('state') or execution_settings['target_state']).strip().lower() or execution_settings['target_state']
        target_workflow_state = str((action_spec or {}).get('workflow_state') or execution_settings['target_workflow_state']).strip().lower() or execution_settings['target_workflow_state']
        event_type = str((action_spec or {}).get('event_type') or 'controlled_rollout_state_apply')
        result_action = str((action_spec or {}).get('result_action') or 'state_applied')

        before_stage = str(candidate.get('current_rollout_stage') or workflow_item.get('current_rollout_stage') or persisted_details.get('rollout_stage') or row.get('current_rollout_stage') or 'observe').strip().lower() or 'observe'
        default_after_stage = {
            'joint_queue_promote_safe': 'candidate',
            'joint_stage_prepare': 'prepared',
            'joint_review_schedule': 'review_pending',
        }.get(action_type, before_stage)
        candidate_stage = candidate.get('recommended_stage') or candidate.get('target_rollout_stage') if candidate.get('can_auto_promote') else None
        after_stage = str(candidate_stage or workflow_item.get('target_rollout_stage') or row.get('target_rollout_stage') or default_after_stage or persisted_details.get('target_rollout_stage')).strip().lower() or before_stage
        stage_terminal = before_stage in {'review_pending', 'rollback_prepare'} and after_stage == before_stage
        fallback_candidate_ready = bool(
            action_spec
            and current_state not in TERMINAL_APPROVAL_STATES
            and action_type in allowed_action_types
            and auto_decision == 'auto_approve'
            and eligible
            and risk_level == 'low'
            and not blocked_by
            and not approval_required
            and not requires_manual
            and not execution_gate.get('blocked')
        )
        candidate_ready = bool(candidate.get('can_auto_promote') or fallback_candidate_ready)
        skip_reason = None
        already_effect_applied = (
            current_state == target_state
            and current_workflow_state == target_workflow_state
            and persisted_details.get('effect') == (action_spec or {}).get('effect')
            and persisted_details.get('action_type') == action_type
            and str(persisted_details.get('rollout_stage') or '') == after_stage
        )

        if current_state in TERMINAL_APPROVAL_STATES:
            skip_reason = f'terminal_state:{current_state}'
        elif stage_terminal:
            skip_reason = f'terminal_stage:{before_stage}'
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
        elif execution_gate.get('blocked'):
            skip_reason = execution_gate.get('primary_reason') or 'validation_gate_blocked'
        elif not candidate_ready:
            skip_reason = 'candidate_not_ready'
        elif candidate.get('risk_label') not in {None, '', 'low'} or risk_level != 'low':
            skip_reason = f'risk_level:{risk_level or candidate.get("risk_label") or "unknown"}'
        elif candidate.get('manual_fallback_required'):
            skip_reason = 'manual_fallback_required'
        elif candidate.get('blocked_by') or blocked_by:
            skip_reason = 'blocked_by:' + ','.join(candidate.get('blocked_by') or blocked_by)

        if skip_reason:
            result['items'].append({
                'item_id': approval_id,
                'playbook_id': row.get('playbook_id'),
                'action_type': action_type,
                'action': 'skipped',
                'reason': skip_reason,
                'state': current_state,
                'workflow_state': current_workflow_state,
                'before_stage': before_stage,
                'after_stage': after_stage,
                'candidate': candidate,
                'validation_gate': execution_gate.get('validation_gate'),
                'execution_gate': execution_gate,
            })
            result['skipped_count'] += 1
            continue

        candidate_reasons = _dedupe_strings((candidate.get('why_promotable') or []) + (candidate.get('missing_requirements') or []))
        if fallback_candidate_ready and not candidate_reasons:
            candidate_reasons = ['auto_advance_allowed', 'derived_from_controlled_rollout_guardrails']
        reason = f"{execution_settings['reason_prefix']}: {row.get('reason') or 'ready auto-promotion candidate passed strict safety boundary'}"
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
            'risk_level': row.get('risk_level') or workflow_item.get('risk_level') or candidate.get('risk_level'),
            'action_type': action_type,
            'execution_layer': 'controlled_rollout_state_apply',
            'execution_mode': execution_settings['mode'],
            'validation_gate': execution_gate.get('validation_gate'),
            'execution_gate': execution_gate,
            'real_trade_execution': False,
            'dangerous_live_parameter_change': False,
            'auto_promotion_execution': {
                'enabled': True,
                'candidate_ready': True,
                'strict_boundary': 'very_safe_ready_low_risk_no_blocker_no_manual_fallback',
                'candidate_summary': {
                    'can_auto_promote': True,
                    'risk_label': candidate.get('risk_label') or 'low',
                    'risk_score': candidate.get('risk_score') if candidate else 0,
                    'manual_fallback_required': candidate.get('manual_fallback_required', False),
                    'why_promotable': (candidate.get('why_promotable') or candidate_reasons),
                },
                'before': {
                    'state': current_state,
                    'workflow_state': current_workflow_state,
                    'rollout_stage': before_stage,
                },
                'after': {
                    'state': target_state,
                    'workflow_state': target_workflow_state,
                    'rollout_stage': after_stage,
                },
                'reason_codes': candidate_reasons or ['auto_advance_allowed'],
                'rollback_hint': candidate.get('rollback_gate', {}).get('rollback_hint') or 'revert_stage_metadata_to_previous_stage',
                'event_log': [{
                    'event_type': event_type,
                    'actor': execution_settings['actor'],
                    'source': execution_settings['source'],
                    'reason': reason,
                    'before_stage': before_stage,
                    'after_stage': after_stage,
                    'before_state': current_state,
                    'after_state': target_state,
                    'before_workflow_state': current_workflow_state,
                    'after_workflow_state': target_workflow_state,
                    'reason_codes': candidate_reasons or ['auto_advance_allowed'],
                    'created_at': _utc_now_iso(),
                }],
            },
            'previous_state': current_state,
            'previous_workflow_state': current_workflow_state,
            'previous_rollout_stage': before_stage,
            'rollout_stage': after_stage,
            'target_rollout_stage': after_stage,
            'stage_transition': {'from': before_stage, 'to': after_stage},
            'rollback_hint': candidate.get('rollback_gate', {}).get('rollback_hint') or 'revert_stage_metadata_to_previous_stage',
            'last_transition': {
                'rule': 'controlled_auto_promotion_execute',
                'from_execution_status': persisted_details.get('execution_status'),
                'to_execution_status': 'applied',
                'from_workflow_state': current_workflow_state,
                'to_workflow_state': target_workflow_state,
                'dispatch_route': candidate.get('dispatch_route') or 'stage_metadata_apply',
                'next_transition': candidate.get('recommended_action') or 'continue_monitoring',
            },
            'execution_status': 'applied',
        }
        details.update(_build_controlled_rollout_action_details(action_type, {**row, 'current_rollout_stage': before_stage, 'target_rollout_stage': after_stage}, {**workflow_item, 'current_rollout_stage': before_stage, 'target_rollout_stage': after_stage}, action_spec, execution_settings))
        details['previous_rollout_stage'] = before_stage
        details['rollout_stage'] = after_stage
        details['target_rollout_stage'] = after_stage
        details['stage_transition'] = {'from': before_stage, 'to': after_stage}
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
            'before_stage': before_stage,
            'after_stage': after_stage,
            'reason_codes': candidate_reasons or ['auto_advance_allowed'],
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

    validation_gate = _build_validation_gate_snapshot(payload)
    execution_gate = _build_validation_execution_gate(validation_gate, layer='controlled_auto_approval')
    result['validation_gate'] = execution_gate['validation_gate']
    result['execution_gate'] = execution_gate

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
        elif execution_gate.get('blocked'):
            skip_reason = execution_gate.get('primary_reason') or 'validation_gate_blocked'

        if skip_reason:
            result['items'].append({
                'item_id': approval_id,
                'playbook_id': row.get('playbook_id'),
                'action': 'skipped',
                'reason': skip_reason,
                'state': current_state,
                'validation_gate': execution_gate.get('validation_gate'),
                'execution_gate': execution_gate,
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
            'validation_gate': execution_gate.get('validation_gate'),
            'execution_gate': execution_gate,
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
            'transition_policy': entry.get('transition_policy') or {},
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


def build_rollout_control_plane_manifest(payload: Optional[Dict] = None, executor: Optional[Dict] = None,
                                        *, allowed_action_types: Optional[List[str]] = None) -> Dict[str, Any]:
    payload = payload or {}
    executor = executor or (payload.get('rollout_executor') or {})
    registry = (executor.get('action_registry') if isinstance(executor, dict) else None) or _build_safe_rollout_action_registry(allowed_action_types)
    catalog = (executor.get('supported_action_map') if isinstance(executor, dict) else None) or _build_rollout_executor_catalog(allowed_action_types)
    action_entries = registry.get('actions') or {}
    stage_handlers = registry.get('handlers') or {}
    action_types = sorted(action_entries.keys())
    stage_handler_keys = sorted(stage_handlers.keys())
    generations = {
        'action_registry': ROLLOUT_ACTION_REGISTRY_VERSION,
        'stage_handler_registry': ROLLOUT_STAGE_HANDLER_REGISTRY_VERSION,
        'transition_policy': ROLLOUT_TRANSITION_POLICY_VERSION,
        'gate_policy': ROLLOUT_GATE_POLICY_VERSION,
        'stage_loop': 'm5_stage_loop_v1',
        'lane_routing': 'm5_lane_routing_v1',
        'operator_action_policy': 'm5_operator_action_policy_v1',
        'control_plane_manifest': ROLLOUT_CONTROL_PLANE_MANIFEST_VERSION,
    }
    version_rows = [{'component': key, 'version': value, 'generation': str(value).split('_', 1)[0]} for key, value in generations.items()]
    blockers = []
    if not action_types:
        blockers.append('missing_action_registry')
    if not stage_handler_keys:
        blockers.append('missing_stage_handler_registry')
    incompatible = [row['component'] for row in version_rows if row['generation'] != 'm5']
    blockers.extend([f'incompatible_generation:{name}' for name in incompatible])
    compatible = not blockers
    return {
        'schema_version': ROLLOUT_CONTROL_PLANE_MANIFEST_VERSION,
        'generation': 'm5',
        'headline': {
            'status': 'compatible' if compatible else 'review_required',
            'message': f"control-plane action_types={len(action_types)} / stage_handlers={len(stage_handler_keys)} / compatible={'yes' if compatible else 'no'}",
        },
        'versions': generations,
        'version_rows': version_rows,
        'registries': {
            'action_types': action_types,
            'stage_handlers': stage_handler_keys,
            'executable_action_types': sorted({row.get('action_type') for row in (registry.get('executable') or []) if row.get('action_type')}),
            'queue_only_action_types': sorted({row.get('action_type') for row in (registry.get('queue_only') or []) if row.get('action_type')}),
            'unsupported_action_types': sorted({row.get('action_type') for row in (registry.get('unsupported') or []) if row.get('action_type')}),
            'transition_routes': sorted({row.get('route') for row in action_entries.values() if row.get('route')}),
            'fallback_handler': (registry.get('fallback_handler') or {}).get('handler_key'),
            'action_count': len(action_types),
            'stage_handler_count': len(stage_handler_keys),
        },
        'contracts': {
            'supported_action_map_count': len(catalog.get('handlers') or {}),
            'workflow_state_contract': 'approval/workflow state replay remains compatible within same generation',
            'upgrade_window': 'same-generation manifest and registry changes are safe for replay-first rollout',
            'rollback_window': 'persisted approval timeline can roll back control-plane metadata within same generation',
            'execution_boundary': 'metadata_only_or_queue_only_never_real_trade_execution',
        },
        'compatibility': {
            'compatible': compatible,
            'status': 'compatible' if compatible else 'review_required',
            'blocking_issues': blockers,
            'replay_safe': compatible,
            'requires_manual_review': not compatible,
        },
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


def _parse_validation_timestamp(value: Any) -> Optional[datetime]:
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _extract_validation_gate(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    source = (
        payload.get('validation_gate')
        or payload.get('validation_summary')
        or ((payload.get('validation_replay') or {}).get('summary') if isinstance(payload.get('validation_replay'), dict) else None)
        or ((payload.get('validation') or {}).get('summary') if isinstance(payload.get('validation'), dict) else None)
        or {}
    )
    readiness = dict(source.get('readiness') or {}) if isinstance(source, dict) else {}
    coverage = dict(source.get('coverage_matrix') or {}) if isinstance(source, dict) else {}
    snapshot_like = isinstance(source, dict) and any(key in source for key in ('enabled', 'ready', 'freeze_auto_advance', 'rollback_on_regression', 'reasons', 'missing_required_capabilities', 'failing_required_capabilities', 'failing_case_count'))
    ready = bool(
        source.get('ready') if snapshot_like and source.get('ready') is not None else readiness.get('low_intervention_gate_ready', coverage.get('ready_for_low_intervention_gate', False))
    )
    missing = _dedupe_strings((source.get('missing_required_capabilities') if snapshot_like else None) or readiness.get('missing_required_capabilities') or coverage.get('missing_required') or [])
    failing = _dedupe_strings((source.get('failing_required_capabilities') if snapshot_like else None) or readiness.get('failing_required_capabilities') or coverage.get('failing_required') or [])
    failing_case_count = int((source.get('failing_case_count') if snapshot_like else None) or readiness.get('failing_case_count') or source.get('fail_count') or 0)
    freshness_policy = dict((source.get('freshness_policy') if isinstance(source, dict) else None) or {})
    max_age_minutes = freshness_policy.get('max_age_minutes')
    max_age_hours = freshness_policy.get('max_age_hours')
    if max_age_minutes in (None, '') and max_age_hours not in (None, ''):
        try:
            max_age_minutes = float(max_age_hours) * 60.0
        except Exception:
            max_age_minutes = None
    try:
        max_age_minutes = float(max_age_minutes) if max_age_minutes not in (None, '') else None
    except Exception:
        max_age_minutes = None
    freshness_enabled = bool(freshness_policy.get('enabled')) or (max_age_minutes is not None and max_age_minutes > 0)
    evaluated_at = _parse_validation_timestamp(
        (source.get('evaluated_at') if isinstance(source, dict) else None)
        or (source.get('generated_at') if isinstance(source, dict) else None)
        or readiness.get('evaluated_at')
        or readiness.get('generated_at')
        or ((source.get('summary') or {}).get('generated_at') if isinstance(source.get('summary'), dict) else None)
    )
    age_seconds = None
    stale = False
    now = datetime.now(timezone.utc)
    if freshness_enabled and evaluated_at:
        age_seconds = max(0.0, (now - evaluated_at).total_seconds())
        stale = bool(max_age_minutes is not None and age_seconds > (max_age_minutes * 60.0))
    elif freshness_enabled and not evaluated_at:
        stale = True
    effective_ready = ready and not stale if freshness_enabled else ready
    reasons = []
    reasons.extend([f'missing_required:{item}' for item in missing])
    reasons.extend([f'failing_required:{item}' for item in failing])
    if failing_case_count:
        reasons.append(f'failing_cases:{failing_case_count}')
    if freshness_enabled:
        if stale:
            reasons.append('validation_stale')
        if evaluated_at is None:
            reasons.append('validation_timestamp_missing')
    enabled = bool(source.get('enabled')) if snapshot_like else bool(source)
    summary = {
        'enabled': enabled,
        'ready': effective_ready,
        'freshness_policy': {
            'enabled': freshness_enabled,
            'max_age_minutes': max_age_minutes,
            'max_age_hours': (max_age_minutes / 60.0) if max_age_minutes is not None else None,
        },
        'evaluated_at': evaluated_at.isoformat().replace('+00:00', 'Z') if evaluated_at else None,
        'age_seconds': age_seconds,
        'stale': stale,
        'freeze_auto_advance': enabled and not effective_ready,
        'rollback_on_regression': enabled and not effective_ready and not stale,
        'coverage_schema_version': coverage.get('schema_version'),
        'required_capability_count': coverage.get('required_capability_count'),
        'covered_required_count': coverage.get('covered_required_count'),
        'passing_required_count': coverage.get('passing_required_count'),
        'missing_required_capabilities': missing,
        'failing_required_capabilities': failing,
        'failing_case_count': failing_case_count,
        'reasons': _dedupe_strings(reasons),
        'summary': source,
    }
    if not summary['enabled']:
        summary.update({
            'ready': None,
            'freeze_auto_advance': False,
            'rollback_on_regression': False,
            'reasons': [],
            'stale': False,
            'age_seconds': None,
        })
    return summary


def _build_validation_gate_snapshot(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    gate = _extract_validation_gate(payload)
    snapshot = {
        'enabled': gate.get('enabled', False),
        'ready': gate.get('ready'),
        'freeze_auto_advance': gate.get('freeze_auto_advance', False),
        'rollback_on_regression': gate.get('rollback_on_regression', False),
        'reasons': gate.get('reasons') or [],
        'missing_required_capabilities': gate.get('missing_required_capabilities') or [],
        'failing_required_capabilities': gate.get('failing_required_capabilities') or [],
        'failing_case_count': gate.get('failing_case_count', 0),
        'coverage_schema_version': gate.get('coverage_schema_version'),
        'required_capability_count': gate.get('required_capability_count'),
        'covered_required_count': gate.get('covered_required_count'),
        'passing_required_count': gate.get('passing_required_count'),
        'freshness_policy': gate.get('freshness_policy') or {'enabled': False, 'max_age_minutes': None, 'max_age_hours': None},
        'evaluated_at': gate.get('evaluated_at'),
        'age_seconds': gate.get('age_seconds'),
        'stale': bool(gate.get('stale', False)),
    }
    snapshot['regression_detected'] = bool(snapshot['enabled']) and snapshot.get('ready') is False and bool(snapshot.get('rollback_on_regression')) and bool((snapshot.get('failing_required_capabilities') or []) or int(snapshot.get('failing_case_count') or 0) > 0)
    snapshot['gap_count'] = len(snapshot.get('missing_required_capabilities') or []) + len(snapshot.get('failing_required_capabilities') or [])
    snapshot['status'] = (
        'disabled' if not snapshot['enabled'] else
        'ready' if snapshot.get('ready') else
        'frozen'
    )
    snapshot['headline'] = (
        'validation_gate_disabled' if not snapshot['enabled'] else
        'validation_gate_ready' if snapshot.get('ready') else
        'validation_gate_frozen'
    )
    return snapshot


def _build_validation_execution_gate(validation_gate: Optional[Dict[str, Any]] = None, *, layer: str = 'execution_apply') -> Dict[str, Any]:
    gate = _build_validation_gate_snapshot({'validation_gate': validation_gate}) if validation_gate else _build_validation_gate_snapshot({})
    missing = gate.get('missing_required_capabilities') or []
    failing = gate.get('failing_required_capabilities') or []
    failing_case_count = int(gate.get('failing_case_count') or 0)
    regression = bool(gate.get('regression_detected'))
    stale = bool(gate.get('stale'))
    frozen = bool(gate.get('enabled')) and bool(gate.get('freeze_auto_advance'))
    gap_only = frozen and not regression and not stale and bool(missing)
    plain_freeze = frozen and not regression and not stale and not gap_only
    reason_codes = []
    if regression:
        reason_codes.append('validation_gate_regression')
    elif stale:
        reason_codes.append('validation_gate_stale')
    elif gap_only:
        reason_codes.append('validation_gate_gap')
    elif plain_freeze:
        reason_codes.append('validation_gate_freeze')
    reason_codes.extend([f'missing_required:{item}' for item in missing])
    reason_codes.extend([f'failing_required:{item}' for item in failing])
    if failing_case_count:
        reason_codes.append(f'failing_cases:{failing_case_count}')
    blocked = frozen
    effect = 'allowed'
    if regression:
        effect = 'blocked_regression'
    elif stale:
        effect = 'blocked_stale'
    elif gap_only:
        effect = 'blocked_gap'
    elif plain_freeze:
        effect = 'blocked_freeze'
    explain = 'validation gate ready or disabled'
    if regression:
        explain = f'{layer} blocked by validation regression; rollback/review required before auto progression'
    elif stale:
        explain = f'{layer} blocked because validation evidence is stale; refresh replay/coverage before auto progression'
    elif gap_only:
        explain = f'{layer} blocked by validation capability gap; low-intervention auto progression frozen'
    elif plain_freeze:
        explain = f'{layer} blocked because validation gate is frozen and not ready'
    return {
        'layer': layer,
        'blocked': blocked,
        'effect': effect,
        'reason_codes': reason_codes,
        'primary_reason': reason_codes[0] if reason_codes else None,
        'explain': explain,
        'validation_gate': gate,
    }


def _collect_validation_gate_consumption(rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    rows = list(rows or [])
    freeze_reasons: Dict[str, int] = {}
    rollback_triggers: Dict[str, int] = {}
    statuses: Dict[str, int] = {}
    item_ids = set()
    last_gate = None
    for row in rows:
        state_machine = row.get('state_machine') or {}
        auto_gate = row.get('auto_advance_gate') or (state_machine.get('auto_advance_gate') if isinstance(state_machine, dict) else {}) or {}
        rollback_gate = row.get('rollback_gate') or (state_machine.get('rollback_gate') if isinstance(state_machine, dict) else {}) or {}
        validation_gate = (
            row.get('validation_gate')
            or auto_gate.get('validation_gate')
            or rollback_gate.get('validation_gate')
            or (state_machine.get('validation_gate') if isinstance(state_machine, dict) else None)
            or {}
        )
        if validation_gate:
            last_gate = _build_validation_gate_snapshot({'validation_gate': validation_gate})
            statuses[last_gate.get('status') or 'disabled'] = statuses.get(last_gate.get('status') or 'disabled', 0) + 1
        reasons = []
        if auto_gate.get('validation_gate'):
            reasons.extend((auto_gate.get('validation_gate') or {}).get('reasons') or [])
        if rollback_gate.get('validation_gate'):
            reasons.extend((rollback_gate.get('validation_gate') or {}).get('reasons') or [])
        if validation_gate and not reasons:
            reasons.extend((validation_gate.get('reasons') or []))
        for reason in reasons:
            freeze_reasons[str(reason)] = freeze_reasons.get(str(reason), 0) + 1
        triggered = list(rollback_gate.get('triggered') or [])
        for trigger in triggered:
            rollback_triggers[str(trigger)] = rollback_triggers.get(str(trigger), 0) + 1
        if auto_gate.get('validation_gate') and ((auto_gate.get('validation_gate') or {}).get('freeze_auto_advance') or 'validation_gate:not_ready' in (auto_gate.get('blockers') or [])):
            item_ids.add(row.get('item_id') or row.get('approval_id') or row.get('playbook_id'))
        if 'validation_gate_regressed' in triggered:
            item_ids.add(row.get('item_id') or row.get('approval_id') or row.get('playbook_id'))
    dominant_freeze_reason = sorted(freeze_reasons.items(), key=lambda item: (-item[1], item[0]))[0][0] if freeze_reasons else None
    dominant_rollback_trigger = sorted(rollback_triggers.items(), key=lambda item: (-item[1], item[0]))[0][0] if rollback_triggers else None
    return {
        'item_count': len([item for item in item_ids if item]),
        'freeze_reason_counts': freeze_reasons,
        'rollback_trigger_counts': rollback_triggers,
        'validation_status_counts': statuses,
        'dominant_freeze_reason': dominant_freeze_reason,
        'dominant_rollback_trigger': dominant_rollback_trigger,
        'latest_validation_gate': last_gate or _build_validation_gate_snapshot({}),
    }


def _build_stage_loop_envelope(*, stage_handler: Optional[Dict[str, Any]] = None, auto_advance_gate: Optional[Dict[str, Any]] = None,
                               rollback_gate: Optional[Dict[str, Any]] = None, dispatch_route: Optional[str] = None,
                               next_transition: Optional[str] = None, result_status: Optional[str] = None) -> Dict[str, Any]:
    # stable stage-loop consumer envelope for direct dashboard/api consumption
    stage_handler = dict(stage_handler or {})
    auto_advance_gate = dict(auto_advance_gate or {})
    rollback_gate = dict(rollback_gate or {})
    stage_key = str(stage_handler.get('stage_key') or 'unknown')
    owner = str(stage_handler.get('owner') or stage_handler.get('responsible_actor') or 'unknown')
    auto_allowed = bool(auto_advance_gate.get('allowed'))
    rollback_candidate = bool(rollback_gate.get('candidate'))
    validation_gate = _resolve_validation_gate_context(auto_advance_gate, rollback_gate, stage_handler)
    validation_freeze = bool((auto_advance_gate.get('validation_gate') or {}).get('freeze_auto_advance') or validation_gate.get('freeze_auto_advance'))
    validation_regression = bool((rollback_gate.get('validation_gate') or {}).get('regression_detected') or 'validation_gate_regressed' in (rollback_gate.get('triggered') or []))
    review_pending = stage_key == 'review_pending' or dispatch_route == 'review_metadata_apply' or next_transition in {'await_manual_approval', 'await_review_checkpoint'}
    waiting_on = list(stage_handler.get('waiting_on') or [])
    why_stopped = stage_handler.get('why_stopped')
    if validation_freeze and 'validation_gate_frozen' not in waiting_on:
        waiting_on.append('validation_gate_frozen')
    if validation_regression and 'validation_gate_regressed' not in waiting_on:
        waiting_on.append('validation_gate_regressed')
    if rollback_candidate or validation_regression or stage_key == 'rollback_prepare':
        loop_state = 'rollback_prepare'
        recommended_action = 'rollback_prepare'
        why_stopped = why_stopped or ('validation_gate_regressed' if validation_regression else 'rollback_gate_open')
    elif review_pending or validation_freeze:
        loop_state = 'review_pending' if review_pending or validation_freeze else 'hold'
        recommended_action = 'review_schedule' if validation_freeze else 'review_pending'
        why_stopped = why_stopped or ('validation_gate_frozen' if validation_freeze else stage_handler.get('why_stopped'))
    elif auto_allowed and owner in {'system', 'unknown'}:
        loop_state = 'auto_advance'
        recommended_action = 'auto_advance'
    else:
        loop_state = 'hold'
        recommended_action = 'hold'
    return {
        'loop_state': loop_state,
        'recommended_action': recommended_action,
        'stage_key': stage_key,
        'owner': owner,
        'dispatch_route': dispatch_route,
        'next_transition': next_transition,
        'result_status': result_status,
        'auto_advance_allowed': auto_allowed,
        'review_pending': review_pending or validation_freeze,
        'rollback_candidate': rollback_candidate or validation_regression,
        'validation_gate': validation_gate,
        'validation_status': validation_gate.get('status'),
        'validation_freeze': validation_freeze,
        'validation_regression': validation_regression,
        'waiting_on': waiting_on,
        'why_stopped': why_stopped,
        'safe_boundary': auto_advance_gate.get('safe_boundary') or rollback_gate.get('safe_boundary'),
    }


def _resolve_stage_loop_snapshot(*, workflow_item: Optional[Dict[str, Any]] = None, approval_item: Optional[Dict[str, Any]] = None,
                                 row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    workflow_item = workflow_item or {}
    approval_item = approval_item or {}
    row = row or {}
    stage_loop = (
        (((workflow_item.get('result') or {}).get('stage_loop')) if isinstance(workflow_item.get('result'), dict) else None)
        or (((workflow_item.get('plan') or {}).get('stage_loop')) if isinstance(workflow_item.get('plan'), dict) else None)
        or workflow_item.get('stage_loop')
        or (((approval_item.get('result') or {}).get('stage_loop')) if isinstance(approval_item.get('result'), dict) else None)
        or (((approval_item.get('plan') or {}).get('stage_loop')) if isinstance(approval_item.get('plan'), dict) else None)
        or approval_item.get('stage_loop')
        or (((row.get('result') or {}).get('stage_loop')) if isinstance(row.get('result'), dict) else None)
        or (((row.get('plan') or {}).get('stage_loop')) if isinstance(row.get('plan'), dict) else None)
        or row.get('stage_loop')
    )
    if isinstance(stage_loop, dict) and stage_loop.get('loop_state'):
        return dict(stage_loop)
    stage_handler = (
        (((workflow_item.get('plan') or {}).get('stage_handler')) if isinstance(workflow_item.get('plan'), dict) else None)
        or workflow_item.get('stage_handler')
        or approval_item.get('stage_handler')
        or (((row.get('plan') or {}).get('stage_handler')) if isinstance(row.get('plan'), dict) else None)
        or row.get('stage_handler')
        or {}
    )
    auto_advance_gate = (workflow_item.get('auto_advance_gate') or approval_item.get('auto_advance_gate') or row.get('auto_advance_gate') or {})
    rollback_gate = (workflow_item.get('rollback_gate') or approval_item.get('rollback_gate') or row.get('rollback_gate') or {})
    state_machine = workflow_item.get('state_machine') or approval_item.get('state_machine') or row.get('state_machine') or {}
    queue_progression = workflow_item.get('queue_progression') or approval_item.get('queue_progression') or row.get('queue_progression') or {}
    if not auto_advance_gate and not rollback_gate and isinstance(state_machine, dict):
        auto_advance_gate = state_machine.get('auto_advance_gate') or auto_advance_gate
        rollback_gate = state_machine.get('rollback_gate') or rollback_gate
    workflow_state = str(workflow_item.get('workflow_state') or approval_item.get('workflow_state') or row.get('workflow_state') or '').strip().lower()
    approval_required = bool(workflow_item.get('approval_required', approval_item.get('approval_required', row.get('approval_required'))))
    validation_gate = _resolve_validation_gate_context(workflow_item, approval_item, row, state_machine)
    if not auto_advance_gate:
        auto_decision = str(workflow_item.get('auto_approval_decision') or approval_item.get('auto_approval_decision') or row.get('auto_approval_decision') or '').strip().lower()
        requires_manual = bool(workflow_item.get('requires_manual', approval_item.get('requires_manual', row.get('requires_manual'))))
        blocked_by = list(workflow_item.get('blocking_reasons') or approval_item.get('blocked_by') or row.get('blocked_by') or [])
        if validation_gate.get('freeze_auto_advance'):
            auto_advance_gate = {'allowed': False, 'validation_gate': validation_gate, 'blockers': ['validation_gate:not_ready']}
        elif auto_decision == 'auto_approve' and not requires_manual and not blocked_by and workflow_state in {'ready', 'queued'}:
            auto_advance_gate = {'allowed': True, 'validation_gate': validation_gate if validation_gate.get('enabled') else {}}
    elif validation_gate.get('enabled') and not auto_advance_gate.get('validation_gate'):
        auto_advance_gate = {**auto_advance_gate, 'validation_gate': validation_gate}
    if not rollback_gate and validation_gate.get('regression_detected') and workflow_state in {'ready', 'queued', 'review_pending', 'execution_failed', 'rollback_pending'}:
        rollback_gate = {'candidate': True, 'triggered': ['validation_gate_regressed'], 'validation_gate': validation_gate}
    elif validation_gate.get('enabled') and rollback_gate and not rollback_gate.get('validation_gate'):
        rollback_gate = {**rollback_gate, 'validation_gate': validation_gate}
    dispatch_route = queue_progression.get('dispatch_route') or row.get('dispatch_route') or (state_machine.get('dispatch_route') if isinstance(state_machine, dict) else None)
    next_transition = queue_progression.get('next_transition') or row.get('next_transition') or (state_machine.get('next_transition') if isinstance(state_machine, dict) else None)
    if not next_transition and approval_required and workflow_state in {'blocked_by_approval', 'review_pending'}:
        next_transition = 'await_manual_approval'
    if not dispatch_route and workflow_state == 'review_pending':
        dispatch_route = 'review_metadata_apply'
    result_status = (
        (((workflow_item.get('result') or {}).get('status')) if isinstance(workflow_item.get('result'), dict) else None)
        or (((approval_item.get('result') or {}).get('status')) if isinstance(approval_item.get('result'), dict) else None)
        or (((row.get('result') or {}).get('status')) if isinstance(row.get('result'), dict) else None)
        or row.get('status')
    )
    return _build_stage_loop_envelope(
        stage_handler=stage_handler,
        auto_advance_gate=auto_advance_gate,
        rollback_gate=rollback_gate,
        dispatch_route=dispatch_route,
        next_transition=next_transition,
        result_status=result_status,
    )


def _summarize_stage_loop_rows(rows: Optional[List[Dict[str, Any]]] = None, *, label: Optional[str] = None,
                               max_items: int = 5) -> Dict[str, Any]:
    rows = list(rows or [])
    loop_counts: Dict[str, int] = {}
    action_counts: Dict[str, int] = {}
    waiting_on_counts: Dict[str, int] = {}
    stage_key_counts: Dict[str, int] = {}
    items_by_state = {'auto_advance': [], 'review_pending': [], 'rollback_prepare': [], 'hold': []}
    for row in rows:
        stage_loop = row.get('stage_loop') or _resolve_stage_loop_snapshot(workflow_item=row, approval_item=row, row=row)
        state = str(stage_loop.get('loop_state') or 'hold')
        action = str(stage_loop.get('recommended_action') or state or 'hold')
        loop_counts[state] = loop_counts.get(state, 0) + 1
        action_counts[action] = action_counts.get(action, 0) + 1
        if state in items_by_state:
            items_by_state[state].append(row)
        stage_key = stage_loop.get('stage_key')
        if stage_key:
            stage_key_counts[str(stage_key)] = stage_key_counts.get(str(stage_key), 0) + 1
        for waiting in stage_loop.get('waiting_on') or []:
            if waiting:
                waiting_on_counts[str(waiting)] = waiting_on_counts.get(str(waiting), 0) + 1
    dominant_loop_state = sorted(loop_counts.items(), key=lambda item: (-item[1], item[0]))[0][0] if loop_counts else None
    dominant_action = sorted(action_counts.items(), key=lambda item: (-item[1], item[0]))[0][0] if action_counts else dominant_loop_state
    path_counts = {
        'auto_advance': loop_counts.get('auto_advance', 0),
        'review_pending': loop_counts.get('review_pending', 0),
        'rollback_prepare': loop_counts.get('rollback_prepare', 0),
        'hold': loop_counts.get('hold', 0),
    }
    return {
        'label': label,
        'item_count': len(rows),
        'loop_state_counts': loop_counts,
        'recommended_action_counts': action_counts,
        'dominant_loop_state': dominant_loop_state,
        'dominant_path': dominant_action,
        'path_counts': path_counts,
        'stage_key_counts': stage_key_counts,
        'waiting_on_counts': waiting_on_counts,
        'items': {key: value[:max_items] for key, value in items_by_state.items() if value},
    }


def _build_rollout_transition_policy_snapshot(spec: Optional[Dict[str, Any]] = None, action_type: Optional[str] = None) -> Dict[str, Any]:
    spec = dict(spec or {})
    policy = dict(spec.get('transition_policy') or {})
    if not policy:
        template = dict(ROLLOUT_TRANSITION_TEMPLATES.get('safe_apply_ready') or {})
        policy = {
            'transition_rule': 'safe_apply_ready',
            **template,
        }
    rule = str(policy.get('transition_rule') or 'safe_apply_ready').strip() or 'safe_apply_ready'
    template = dict(ROLLOUT_TRANSITION_TEMPLATES.get(rule) or {})
    merged = {**template, **policy}
    merged['transition_rule'] = rule
    merged['schema_version'] = ROLLOUT_TRANSITION_POLICY_VERSION
    if action_type:
        merged['action_type'] = str(action_type).strip().lower()
    return merged


def _materialize_rollout_transition_rule(*, policy: Optional[Dict[str, Any]] = None, rollout_stage: Optional[str] = None,
                                         target_rollout_stage: Optional[str] = None, readiness: Optional[str] = None,
                                         stage_handler: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    policy = dict(policy or {})
    stage_handler = dict(stage_handler or {})
    next_transition = policy.get('next_transition')
    if policy.get('use_stage_handler_next_transition') and stage_handler.get('next_transition'):
        next_transition = stage_handler.get('next_transition')
    return {
        'transition_rule': policy.get('transition_rule') or 'safe_apply_ready',
        'dispatch_route': policy.get('dispatch_route') or 'safe_state_apply',
        'next_transition': next_transition or 'mark_ready_for_followup',
        'retryable': bool(policy.get('retryable', True)),
        'rollback_hint': policy.get('rollback_hint') or 'restore_previous_state_from_approval_timeline',
        'rollout_stage': rollout_stage,
        'target_rollout_stage': target_rollout_stage,
        'readiness': readiness,
        'transition_policy': policy,
        **({'stage_handler': stage_handler} if stage_handler else {}),
    }


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
        return _materialize_rollout_transition_rule(
            policy={'transition_rule': 'preserve_terminal_state'},
            rollout_stage=rollout_stage,
            target_rollout_stage=target_rollout_stage,
            readiness=readiness,
        )
    if blocked:
        return _materialize_rollout_transition_rule(
            policy={'transition_rule': 'defer_until_blockers_clear'},
            rollout_stage=rollout_stage,
            target_rollout_stage=target_rollout_stage,
            readiness=readiness,
        )
    if auto_decision in {'defer', 'freeze'}:
        return _materialize_rollout_transition_rule(
            policy={
                'transition_rule': 'defer_or_freeze_by_policy',
                'retryable': auto_decision == 'defer',
            },
            rollout_stage=rollout_stage,
            target_rollout_stage=target_rollout_stage,
            readiness=readiness,
        )
    if approval_required or requires_manual or auto_decision == 'manual_review':
        return _materialize_rollout_transition_rule(
            policy={'transition_rule': 'manual_gate_before_dispatch'},
            rollout_stage=rollout_stage,
            target_rollout_stage=target_rollout_stage,
            readiness=readiness,
        )
    if dispatch_mode == 'queue_only':
        queue_policy = _build_rollout_transition_policy_snapshot(spec, action_type)
        return _materialize_rollout_transition_rule(
            policy=queue_policy,
            rollout_stage=rollout_stage,
            target_rollout_stage=target_rollout_stage,
            readiness=readiness,
        )

    transition_policy = _build_rollout_transition_policy_snapshot(spec, action_type)
    default_target_stage = str(transition_policy.get('default_target_stage') or '').strip().lower()
    if default_target_stage and target_rollout_stage in {'', 'pending', rollout_stage}:
        target_rollout_stage = default_target_stage
    review_due_at = row.get('review_due_at') or workflow_item.get('review_due_at')
    if action_type in {'joint_stage_prepare', 'joint_queue_promote_safe', 'joint_review_schedule'}:
        stage_handler = _resolve_rollout_stage_handler(
            current_stage=rollout_stage,
            target_stage=target_rollout_stage,
            action_type=action_type,
            workflow_state=current_workflow_state,
            blocked_by=blocked,
            review_due_at=review_due_at,
        )
        return _materialize_rollout_transition_rule(
            policy=transition_policy,
            rollout_stage=rollout_stage,
            target_rollout_stage=target_rollout_stage,
            readiness=readiness,
            stage_handler=stage_handler,
        )
    return _materialize_rollout_transition_rule(
        policy=transition_policy,
        rollout_stage=rollout_stage,
        target_rollout_stage=target_rollout_stage,
        readiness=readiness,
    )



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
    queue_dispatch_route = 'deferred_review_queue' if queue_status == 'deferred' else 'manual_review_queue'
    queue_next_transition = 'retry_after_blockers_clear' if queue_status == 'deferred' else 'await_manual_approval'
    queue_progression = {
        'status': queue_status,
        'approval_state': hook.get('approval_state'),
        'workflow_state': hook.get('workflow_state'),
        'decision': hook.get('decision'),
        'gate_reason': hook.get('gate_reason') or spec.get('blocked_reason'),
        'next_action': hook.get('next_action') or action_type,
        'dispatch_route': queue_dispatch_route,
        'next_transition': queue_next_transition,
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
        'dispatch_route': queue_dispatch_route,
        'next_transition': queue_next_transition,
        'retryable': bool(transition.get('retryable', True)),
        'rollback_hint': transition.get('rollback_hint'),
        'queue_transition': {
            'from_state': hook.get('approval_state'),
            'from_workflow_state': hook.get('workflow_state'),
            'to_queue_status': queue_status,
            'transition_reason': queue_progression['gate_reason'],
            'transition_rule': transition.get('transition_rule'),
            'dispatch_route': queue_dispatch_route,
            'next_transition': queue_next_transition,
            'retryable': bool(transition.get('retryable', True)),
            'rollback_hint': transition.get('rollback_hint'),
        },
        'queue_progression': queue_progression,
        'stage_loop': _build_stage_loop_envelope(
            stage_handler=transition.get('stage_handler') or {},
            dispatch_route=transition.get('dispatch_route'),
            next_transition=transition.get('next_transition'),
            result_status=queue_status,
        ),
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
        'dispatch_route': (queue_plan or {}).get('dispatch_route') or transition_rule.get('dispatch_route'),
        'next_transition': (queue_plan or {}).get('next_transition') or transition_rule.get('next_transition'),
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
        'stage_loop': (queue_plan or {}).get('stage_loop') or _build_stage_loop_envelope(
            dispatch_route=(queue_plan or {}).get('dispatch_route') or transition_rule.get('dispatch_route'),
            next_transition=(queue_plan or {}).get('next_transition') or transition_rule.get('next_transition'),
            result_status=result_action,
        ),
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

    validation_gate = _build_validation_gate_snapshot(payload)
    result = {
        'schema_version': 'm5_rollout_executor_skeleton_v2',
        'enabled': execution_settings.get('enabled', False),
        'mode': execution_settings.get('mode'),
        'dry_run': execution_settings.get('dry_run', False),
        'actor': execution_settings.get('actor'),
        'source': execution_settings.get('source'),
        'replay_source': replay_source,
        'status': 'disabled',
        'validation_gate': validation_gate,
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
            'validation_gate': validation_gate,
        },
        'supported_action_map': catalog,
        'action_registry': action_registry,
        'items': [],
    }
    result['control_plane_manifest'] = build_rollout_control_plane_manifest(payload, result, allowed_action_types=execution_settings.get('allowed_action_types'))
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
        rollout_gates = _evaluate_rollout_gates(
            action_type=action_type,
            row=row,
            workflow_item=workflow_item,
            spec=spec,
            handler=handler,
            allowlisted=action_type in allowlisted,
            current_state=current_state,
            current_workflow_state=current_workflow_state,
            auto_decision=auto_decision,
            eligible=eligible,
            approval_required=approval_required,
            requires_manual=requires_manual,
            blocked_by=blocked_by,
            risk_level=risk_level,
            transition_rule=transition_rule,
            persisted_details=persisted_details,
            validation_gate=_extract_validation_gate(payload),
        )
        execution_gate = _build_validation_execution_gate((rollout_gates.get('auto_advance_gate') or {}).get('validation_gate') or validation_gate, layer='rollout_executor_apply')
        transition_policy_snapshot = _build_rollout_transition_policy_snapshot(spec, action_type)

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
            'stage_handler': rollout_gates.get('stage_handler') or transition_rule.get('stage_handler') or {},
            'transition_policy_snapshot': transition_policy_snapshot,
            'transition_policy': transition_rule.get('transition_policy') or transition_policy_snapshot,
            'execution_status': _normalize_action_execution_status(None, workflow_state=current_workflow_state, queue_status=((workflow_item.get('queue_progression') or {}).get('status') if isinstance(workflow_item.get('queue_progression'), dict) else None)),
            'auto_advance_gate': rollout_gates.get('auto_advance_gate') or {},
            'rollback_gate': rollout_gates.get('rollback_gate') or {},
            'validation_gate': execution_gate.get('validation_gate'),
            'execution_gate': execution_gate,
            'stage_loop': _build_stage_loop_envelope(
                stage_handler=rollout_gates.get('stage_handler') or transition_rule.get('stage_handler') or {},
                auto_advance_gate=rollout_gates.get('auto_advance_gate') or {},
                rollback_gate=rollout_gates.get('rollback_gate') or {},
                dispatch_route=transition_rule.get('dispatch_route'),
                next_transition=transition_rule.get('next_transition'),
                result_status='planned',
            ),
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
            'stage_handler': rollout_gates.get('stage_handler') or transition_rule.get('stage_handler') or {},
            'transition_policy_snapshot': transition_policy_snapshot,
            'transition_policy': transition_rule.get('transition_policy') or transition_policy_snapshot,
            'auto_advance_gate': rollout_gates.get('auto_advance_gate') or {},
            'rollback_gate': rollout_gates.get('rollback_gate') or {},
            'validation_gate': execution_gate.get('validation_gate'),
            'execution_gate': execution_gate,
            'stage_loop': _build_stage_loop_envelope(
                stage_handler=rollout_gates.get('stage_handler') or transition_rule.get('stage_handler') or {},
                auto_advance_gate=rollout_gates.get('auto_advance_gate') or {},
                rollback_gate=rollout_gates.get('rollback_gate') or {},
                dispatch_route=transition_rule.get('dispatch_route'),
                next_transition=transition_rule.get('next_transition'),
                result_status='planned',
            ),
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
            'execution_status': plan.get('execution_status'),
            'last_transition': {},
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
            result_row['plan']['dispatch_route'] = queue_plan.get('dispatch_route')
            result_row['plan']['next_transition'] = queue_plan.get('next_transition') or transition_rule.get('next_transition')
            result_row['plan']['stage_loop'] = _build_stage_loop_envelope(stage_handler=plan.get('stage_handler') or {}, auto_advance_gate=plan.get('auto_advance_gate') or {}, rollback_gate=plan.get('rollback_gate') or {}, dispatch_route=queue_plan.get('dispatch_route'), next_transition=queue_plan.get('next_transition') or transition_rule.get('next_transition'), result_status='planned')
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
            result_row['dispatch'] = _build_rollout_dispatch_envelope(mode=dispatch_mode, executor_class=executor_class, handler_key=handler_key, allowed=True, status=item_status, reason=dispatch_reason, code=dispatch_code, queue_name=queue_plan['queue_name'], dispatch_route=queue_plan.get('dispatch_route'), transition_rule=transition_rule.get('transition_rule'), next_transition=queue_plan.get('next_transition') or transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
            if result['dry_run']:
                result_row['apply'] = _build_rollout_apply_envelope(attempted=True, persisted=False, status='dry_run', operation='queue_plan_consume', idempotency_key=idempotency_key)
                result_row['result'] = _build_rollout_result_envelope(disposition='dry_run' if queue_status == 'ready_to_queue' else disposition, status='dry_run', reason=dispatch_reason, code='DRY_RUN_ONLY', state=current_state, workflow_state=current_workflow_state, transition_rule=transition_rule.get('transition_rule'), dispatch_route=queue_plan.get('dispatch_route'), next_transition=queue_plan.get('next_transition') or transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
                result_row['result']['stage_loop'] = _build_stage_loop_envelope(stage_handler=plan.get('stage_handler') or {}, auto_advance_gate=plan.get('auto_advance_gate') or {}, rollback_gate=plan.get('rollback_gate') or {}, dispatch_route=queue_plan.get('dispatch_route'), next_transition=queue_plan.get('next_transition') or transition_rule.get('next_transition'), result_status='dry_run')
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
                    result_row['result'] = _build_rollout_result_envelope(disposition=disposition, status=item_status, reason=dispatch_reason, code=dispatch_code, state=(queue_consumed_row or {}).get('state'), workflow_state=(queue_consumed_row or {}).get('workflow_state'), transition_rule=transition_rule.get('transition_rule'), dispatch_route=queue_plan.get('dispatch_route'), next_transition=queue_plan.get('next_transition') or transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
                    result_row['result']['stage_loop'] = _build_stage_loop_envelope(stage_handler=plan.get('stage_handler') or {}, auto_advance_gate=plan.get('auto_advance_gate') or {}, rollback_gate=plan.get('rollback_gate') or {}, dispatch_route=queue_plan.get('dispatch_route'), next_transition=queue_plan.get('next_transition') or transition_rule.get('next_transition'), result_status=item_status)
                    result_row['status'] = item_status
                    result_row['execution_status'] = 'blocked' if item_status == 'blocked_by_approval' else ('deferred' if item_status == 'deferred' else 'queued')
                    result_row['last_transition'] = {'rule': transition_rule.get('transition_rule'), 'to_execution_status': result_row['execution_status'], 'to_workflow_state': item_status, 'dispatch_route': queue_plan.get('dispatch_route'), 'next_transition': queue_plan.get('next_transition') or transition_rule.get('next_transition')}
                    bump(disposition, item_status)
                except Exception as exc:
                    result_row['apply'] = _build_rollout_apply_envelope(attempted=True, persisted=False, status='error', operation='queue_plan_consume', idempotency_key=idempotency_key)
                    result_row['result'] = _build_rollout_result_envelope(disposition='error', status='error', reason=str(exc), code='QUEUE_CONSUME_EXCEPTION', transition_rule=transition_rule.get('transition_rule'), dispatch_route=queue_plan.get('dispatch_route'), next_transition=queue_plan.get('next_transition') or transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
                    result_row['status'] = 'error'
                    result_row['execution_status'] = 'error'
                    result_row['last_transition'] = {'rule': transition_rule.get('transition_rule'), 'to_execution_status': 'error', 'to_workflow_state': current_workflow_state, 'dispatch_route': queue_plan.get('dispatch_route'), 'next_transition': queue_plan.get('next_transition') or transition_rule.get('next_transition')}
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
        elif execution_gate.get('blocked'):
            skip_reason = execution_gate.get('primary_reason') or 'validation_gate_blocked'
            skip_code = 'VALIDATION_GATE_BLOCKED'
        elif already_applied:
            skip_reason, skip_code = 'already_applied', 'IDEMPOTENT_ALREADY_APPLIED'
            result_row['apply'] = _build_rollout_apply_envelope(status='idempotent_skip', operation='noop', idempotency_key=idempotency_key)

        if skip_reason:
            result_row['dispatch']['status'] = 'skipped'
            result_row['dispatch']['reason'] = skip_reason
            result_row['dispatch']['code'] = skip_code
            result_row['result'] = _build_rollout_result_envelope(disposition='skipped', status='skipped', reason=skip_reason, code=skip_code, transition_rule=transition_rule.get('transition_rule'), dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
            result_row['result']['stage_loop'] = _build_stage_loop_envelope(stage_handler=plan.get('stage_handler') or {}, auto_advance_gate=plan.get('auto_advance_gate') or {}, rollback_gate=plan.get('rollback_gate') or {}, dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), result_status='skipped')
            result_row['status'] = 'skipped'
            result_row['execution_status'] = 'skipped'
            result_row['last_transition'] = {'rule': transition_rule.get('transition_rule'), 'to_execution_status': 'skipped', 'to_workflow_state': current_workflow_state, 'dispatch_route': transition_rule.get('dispatch_route'), 'next_transition': transition_rule.get('next_transition')}
            result['summary']['skipped_count'] += 1
            result['items'].append(result_row)
            bump('skipped', 'skipped')
            continue

        result_row['dispatch'] = _build_rollout_dispatch_envelope(mode=dispatch_mode, executor_class=executor_class, handler_key=handler_key, allowed=True, status='dispatching', reason='safe_apply_candidate', code='SAFE_APPLY_CANDIDATE', dispatch_route=transition_rule.get('dispatch_route'), transition_rule=transition_rule.get('transition_rule'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
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
            'transition_policy_snapshot': transition_policy_snapshot,
            'transition_policy': transition_rule.get('transition_policy') or transition_policy_snapshot,
            'dispatch_route': transition_rule.get('dispatch_route'),
            'next_transition': transition_rule.get('next_transition'),
            'retryable': bool(transition_rule.get('retryable', True)),
            'rollback_hint': transition_rule.get('rollback_hint'),
            'rollout_stage': transition_rule.get('rollout_stage'),
            'target_rollout_stage': transition_rule.get('target_rollout_stage'),
            'readiness': transition_rule.get('readiness'),
            'real_trade_execution': False,
            'dangerous_live_parameter_change': False,
            'execution_status': 'dispatching',
            'previous_execution_status': persisted_details.get('execution_status'),
            'auto_advance_gate': rollout_gates.get('auto_advance_gate') or {},
            'rollback_gate': rollout_gates.get('rollback_gate') or {},
            'validation_gate': execution_gate.get('validation_gate'),
            'execution_gate': execution_gate,
            'stage_loop': _build_stage_loop_envelope(
                stage_handler=rollout_gates.get('stage_handler') or transition_rule.get('stage_handler') or {},
                auto_advance_gate=rollout_gates.get('auto_advance_gate') or {},
                rollback_gate=rollout_gates.get('rollback_gate') or {},
                dispatch_route=transition_rule.get('dispatch_route'),
                next_transition=transition_rule.get('next_transition'),
                result_status='planned',
            ),
        }
        details.update(_build_controlled_rollout_action_details(action_type, row, workflow_item, spec, execution_settings))
        details['last_transition'] = {'rule': transition_rule.get('transition_rule'), 'from_execution_status': persisted_details.get('execution_status'), 'to_execution_status': 'applied', 'from_workflow_state': current_workflow_state, 'to_workflow_state': spec.get('workflow_state'), 'dispatch_route': transition_rule.get('dispatch_route'), 'next_transition': transition_rule.get('next_transition')}
        if persisted_details.get('execution_status') in {'error', 'blocked', 'deferred'}:
            details['execution_status'] = 'recovered'
            details['recovered_from_execution_status'] = persisted_details.get('execution_status')
        else:
            details['execution_status'] = 'applied'
        result_row['apply'] = _build_rollout_apply_envelope(attempted=True, persisted=False, status='applying', operation='upsert_approval_state', idempotency_key=idempotency_key)
        if result['dry_run']:
            result_row['apply']['status'] = 'dry_run'
            result_row['execution_status'] = 'dry_run'
            result_row['last_transition'] = {'rule': transition_rule.get('transition_rule'), 'to_execution_status': 'dry_run', 'to_workflow_state': spec.get('workflow_state'), 'dispatch_route': transition_rule.get('dispatch_route'), 'next_transition': transition_rule.get('next_transition')}
            result_row['result'] = _build_rollout_result_envelope(disposition='dry_run', status='dry_run', reason=reason, code='DRY_RUN_ONLY', state=spec.get('state'), workflow_state=spec.get('workflow_state'), transition_rule=transition_rule.get('transition_rule'), dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
            result_row['result']['stage_loop'] = _build_stage_loop_envelope(stage_handler=plan.get('stage_handler') or {}, auto_advance_gate=plan.get('auto_advance_gate') or {}, rollback_gate=plan.get('rollback_gate') or {}, dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), result_status='dry_run')
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
            result_row['result']['stage_loop'] = _build_stage_loop_envelope(stage_handler=plan.get('stage_handler') or {}, auto_advance_gate=plan.get('auto_advance_gate') or {}, rollback_gate=plan.get('rollback_gate') or {}, dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), result_status='applied')
            result_row['status'] = 'applied'
            result_row['execution_status'] = details.get('execution_status') or 'applied'
            result_row['last_transition'] = details.get('last_transition') or {}
            result['summary']['applied_count'] += 1
            result['items'].append(result_row)
            bump('applied', 'applied')
        except Exception as exc:
            result_row['apply'] = _build_rollout_apply_envelope(attempted=True, persisted=False, status='error', operation='upsert_approval_state', idempotency_key=idempotency_key)
            result_row['result'] = _build_rollout_result_envelope(disposition='error', status='error', reason=str(exc), code='APPLY_EXCEPTION', transition_rule=transition_rule.get('transition_rule'), dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), retryable=bool(transition_rule.get('retryable', True)), rollback_hint=transition_rule.get('rollback_hint'))
            result_row['result']['stage_loop'] = _build_stage_loop_envelope(stage_handler=plan.get('stage_handler') or {}, auto_advance_gate=plan.get('auto_advance_gate') or {}, rollback_gate=plan.get('rollback_gate') or {}, dispatch_route=transition_rule.get('dispatch_route'), next_transition=transition_rule.get('next_transition'), result_status='error')
            result_row['status'] = 'error'
            result_row['execution_status'] = 'error'
            result_row['last_transition'] = {'rule': transition_rule.get('transition_rule'), 'to_execution_status': 'error', 'to_workflow_state': current_workflow_state, 'dispatch_route': transition_rule.get('dispatch_route'), 'next_transition': transition_rule.get('next_transition')}
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
        'auto_advance_count': 0,
        'review_pending_count': 0,
        'rollback_prepare_count': 0,
        'by_stage': {},
        'by_next_transition': {},
        'by_dispatch_route': {},
        'by_status': {},
        'by_loop_state': {},
        'by_advisory_action': {},
        'by_advisory_stage': {},
        'by_advisory_urgency': {},
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
        stage_loop = result.get('stage_loop') or plan.get('stage_loop') or {}
        advisory = ((plan.get('stage_handler') or {}).get('advisory') or {})
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
                'transition_policy': plan.get('transition_policy') or {},
                'retryable': bool(plan.get('retryable', result.get('retryable', True))),
                'rollback_hint': plan.get('rollback_hint') or result.get('rollback_hint') or dispatch.get('rollback_hint'),
                'readiness': plan.get('readiness') or workflow_item.get('workflow_state') or approval_item.get('workflow_state') or 'pending',
                'stage_handler': plan.get('stage_handler') or {},
                'stage_loop': stage_loop,
                'advisory': advisory,
            },
            'queue_plan': plan.get('queue_plan') or {},
            'dispatch': dispatch,
            'result': result,
        }
        stage_row['lane_routing'] = _resolve_lane_routing(workflow_item=workflow_item, approval_item=approval_item, row=stage_row, stage_loop=stage_loop)
        stage_row['lane_id'] = stage_row['lane_routing'].get('lane_id')
        stage_row['queue_name'] = stage_row['lane_routing'].get('queue_name')
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
        bump(summary['by_loop_state'], stage_loop.get('loop_state'))
        bump(summary['by_advisory_action'], advisory.get('recommended_action'))
        bump(summary['by_advisory_stage'], advisory.get('recommended_stage'))
        bump(summary['by_advisory_urgency'], advisory.get('urgency'))
        if stage_loop.get('loop_state') == 'auto_advance':
            summary['auto_advance_count'] += 1
        elif stage_loop.get('loop_state') == 'review_pending':
            summary['review_pending_count'] += 1
        elif stage_loop.get('loop_state') == 'rollback_prepare':
            summary['rollback_prepare_count'] += 1

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
    recovery_view = payload.get('workflow_recovery_view') or build_workflow_recovery_view(payload)
    validation_gate = _build_validation_gate_snapshot(payload)
    approval_items = approval_state.get('items') or []
    workflow_items = workflow_state.get('item_states') or []
    approval_by_playbook = {row.get('playbook_id'): row for row in approval_items if row.get('playbook_id')}
    for workflow_item in workflow_items:
        approval_item = approval_by_playbook.get(workflow_item.get('item_id')) or {}
        if validation_gate.get('enabled') and not workflow_item.get('validation_gate'):
            workflow_item['validation_gate'] = dict(validation_gate)
        stage_loop = _resolve_stage_loop_snapshot(workflow_item=workflow_item, approval_item=approval_item, row=workflow_item)
        operator_action_policy = (workflow_item.get('state_machine') or {}).get('operator_action_policy') or _build_operator_action_policy(
            item_id=workflow_item.get('item_id'),
            approval_state=approval_item.get('approval_state') or workflow_item.get('approval_state'),
            workflow_state=workflow_item.get('workflow_state'),
            queue_status=((workflow_item.get('queue_progression') or {}).get('status') if isinstance(workflow_item.get('queue_progression'), dict) else None),
            dispatch_route=((workflow_item.get('queue_progression') or {}).get('dispatch_route') if isinstance(workflow_item.get('queue_progression'), dict) else None),
            next_transition=((workflow_item.get('queue_progression') or {}).get('next_transition') if isinstance(workflow_item.get('queue_progression'), dict) else None),
            blocked_by=list(dict.fromkeys((workflow_item.get('blocking_reasons') or []) + (approval_item.get('blocked_by') or []))),
            retryable=((workflow_item.get('state_machine') or {}).get('retryable') if isinstance(workflow_item.get('state_machine'), dict) else None),
            rollout_stage=workflow_item.get('current_rollout_stage'),
            target_rollout_stage=workflow_item.get('target_rollout_stage'),
            terminal=bool(((workflow_item.get('state_machine') or {}) if isinstance(workflow_item.get('state_machine'), dict) else {}).get('terminal')),
            validation_gate=_resolve_validation_gate_context(workflow_item, approval_item, {'validation_gate': validation_gate}),
        )
        lane_routing = _resolve_lane_routing(workflow_item=workflow_item, approval_item=approval_item, row=workflow_item, operator_action_policy=operator_action_policy, stage_loop=stage_loop)
        workflow_item['stage_loop'] = stage_loop
        workflow_item['operator_action_policy'] = operator_action_policy
        workflow_item['lane_routing'] = lane_routing
        workflow_item['lane_id'] = lane_routing.get('lane_id')
        workflow_item['queue_name'] = lane_routing.get('queue_name')
        workflow_item['dispatch_route'] = lane_routing.get('dispatch_route')
        workflow_item['route_family'] = lane_routing.get('route_family')
        workflow_item['next_transition'] = lane_routing.get('next_transition')
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
            'recovery_queue_summary': recovery_view.get('summary') or {},
            'controlled_rollout_executed_count': controlled_rollout.get('executed_count', 0),
            'auto_approval_executed_count': auto_approval.get('executed_count', 0),
            'validation_gate': validation_gate,
        },
        'workflow_state': workflow_state,
        'approval_state': approval_state,
        'queues': queues,
        'rollout_executor': rollout_executor,
        'rollout_stage_progression': stage_progression,
        'workflow_recovery_view': recovery_view,
        'controlled_rollout_execution': controlled_rollout,
        'auto_approval_execution': auto_approval,
        'validation_gate': validation_gate,
    }
    payload['consumer_view'] = view
    return view


def build_workflow_recovery_view(payload: Optional[Dict] = None, *, max_items: int = 50) -> Dict[str, Any]:
    payload = payload or {}
    workflow_items = ((payload.get('workflow_state') or {}).get('item_states') or [])
    approval_items = ((payload.get('approval_state') or {}).get('items') or [])
    approval_by_playbook = {row.get('playbook_id'): row for row in approval_items if row.get('playbook_id')}

    retry_queue = []
    rollback_candidates = []
    manual_recovery = []

    for workflow_item in workflow_items:
        item_id = workflow_item.get('item_id')
        approval_item = approval_by_playbook.get(item_id) or {}
        state_machine = workflow_item.get('state_machine') or approval_item.get('state_machine') or {}
        orchestration = (state_machine.get('recovery_orchestration') or {}) if isinstance(state_machine, dict) else {}
        bucket = orchestration.get('queue_bucket') or 'observe'
        row = {
            'item_id': item_id,
            'approval_id': approval_item.get('approval_id'),
            'title': workflow_item.get('title') or approval_item.get('title') or item_id,
            'action_type': workflow_item.get('action_type') or approval_item.get('action_type'),
            'workflow_state': workflow_item.get('workflow_state') or approval_item.get('workflow_state') or 'pending',
            'approval_state': approval_item.get('approval_state') or 'not_required',
            'risk_level': workflow_item.get('risk_level') or approval_item.get('risk_level') or 'unknown',
            'blocked_by': list(dict.fromkeys((workflow_item.get('blocking_reasons') or []) + (approval_item.get('blocked_by') or []))),
            'execution_timeline': state_machine.get('execution_timeline') or {},
            'recovery_policy': state_machine.get('recovery_policy') or {},
            'recovery_orchestration': orchestration,
        }
        if bucket == 'retry_queue':
            retry_queue.append(row)
        elif bucket == 'rollback_candidate':
            rollback_candidates.append(row)
        elif bucket == 'manual_recovery':
            manual_recovery.append(row)

    retry_queue = sorted(retry_queue, key=lambda row: (str(((row.get('recovery_orchestration') or {}).get('retry_schedule') or {}).get('should_retry_at') or ''), str(row.get('title') or row.get('item_id') or '')))
    rollback_candidates = sorted(rollback_candidates, key=lambda row: (str(row.get('risk_level') or 'unknown'), str(row.get('title') or row.get('item_id') or '')))
    manual_recovery = sorted(manual_recovery, key=lambda row: (str(row.get('risk_level') or 'unknown'), str(row.get('title') or row.get('item_id') or '')))

    next_retry_at = next(
        ((((row.get('recovery_orchestration') or {}).get('retry_schedule') or {}).get('should_retry_at'))
         for row in retry_queue
         if (((row.get('recovery_orchestration') or {}).get('retry_schedule') or {}).get('should_retry_at'))),
        None,
    )
    view = {
        'schema_version': 'm5_workflow_recovery_view_v1',
        'summary': {
            'retry_queue_count': len(retry_queue),
            'rollback_candidate_count': len(rollback_candidates),
            'manual_recovery_count': len(manual_recovery),
            'next_retry_at': next_retry_at,
        },
        'queues': {
            'retry_queue': retry_queue[:max_items],
            'rollback_candidates': rollback_candidates[:max_items],
            'manual_recovery': manual_recovery[:max_items],
        },
    }
    payload['workflow_recovery_view'] = view
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
        state_machine = workflow_item.get('state_machine') or approval_item.get('state_machine') or {}
        operator_action_policy = state_machine.get('operator_action_policy') or _build_operator_action_policy(
            item_id=item_id,
            approval_state=approval_state,
            workflow_state=workflow_state,
            queue_status=(workflow_item.get('queue_progression') or {}).get('status'),
            dispatch_route=(workflow_item.get('queue_progression') or {}).get('dispatch_route'),
            next_transition=(workflow_item.get('queue_progression') or {}).get('next_transition'),
            blocked_by=blocked_by,
            retryable=state_machine.get('retryable'),
            rollout_stage=workflow_item.get('current_rollout_stage'),
            target_rollout_stage=workflow_item.get('target_rollout_stage'),
            terminal=bool(state_machine.get('terminal')),
            validation_gate=_resolve_validation_gate_context(workflow_item, approval_item, {'validation_gate': consumer_view.get('validation_gate')}),
        )
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
            'operator_action_policy': operator_action_policy,
            'operator_action': operator_action_policy.get('action'),
            'operator_route': operator_action_policy.get('route'),
            'operator_follow_up': operator_action_policy.get('follow_up'),
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
            'operator_action_policy_summary': _summarize_operator_action_policies(items),
            'stage_loop': _summarize_stage_loop_rows(items, label=bucket_id, max_items=max_items),
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


def _build_transition_journal_consumer_view(*, overview: Optional[Dict[str, Any]] = None,
                                            transition_rows: Optional[List[Dict[str, Any]]] = None,
                                            summary: Optional[Dict[str, Any]] = None,
                                            max_items: int = 5) -> Dict[str, Any]:
    source = overview or build_transition_journal_overview(transition_rows=transition_rows, summary=summary)
    source_summary = source.get('summary') or {}
    recent_rows = list(source.get('recent_transitions') or [])[:max_items]
    latest = recent_rows[0] if recent_rows else {}
    transitions = []
    for row in recent_rows:
        from_state = (row.get('from') or {}).get('workflow_state') or (row.get('from') or {}).get('state') or 'new'
        to_state = (row.get('to') or {}).get('workflow_state') or (row.get('to') or {}).get('state') or 'unknown'
        transitions.append({
            'item_id': row.get('item_id'),
            'approval_id': row.get('approval_id'),
            'title': row.get('title') or row.get('item_id'),
            'timestamp': row.get('timestamp'),
            'trigger': row.get('trigger') or row.get('event_type') or 'unknown',
            'actor': row.get('actor') or 'unknown',
            'source': row.get('source') or 'unknown',
            'workflow_transition': f'{from_state}->{to_state}',
            'changed_fields': row.get('changed_fields') or [],
            'reason': row.get('reason'),
            'changed': bool(row.get('changed', False)),
        })
    return {
        'schema_version': 'm5_transition_journal_consumer_v1',
        'headline': {
            'status': 'recent_activity' if transitions else 'steady',
            'message': f"{source_summary.get('count', 0)} recent transition(s)",
            'latest_timestamp': source_summary.get('latest_timestamp'),
            'latest_transition': transitions[0].get('workflow_transition') if transitions else None,
        },
        'summary': {
            'count': source_summary.get('count', len(source.get('recent_transitions') or [])),
            'latest_timestamp': source_summary.get('latest_timestamp'),
            'changed_only': source_summary.get('changed_only', True),
            'changed_field_counts': source_summary.get('changed_field_counts') or {},
            'workflow_transition_counts': source_summary.get('workflow_transition_counts') or {},
        },
        'recent_transitions': transitions,
        'latest': transitions[0] if transitions else {},
        'overview': source,
    }



def _resolve_auto_promotion_observation_targets(*, row: Optional[Dict[str, Any]] = None, workflow_item: Optional[Dict[str, Any]] = None, approval_item: Optional[Dict[str, Any]] = None) -> List[str]:
    row = row or {}
    workflow_item = workflow_item or {}
    approval_item = approval_item or {}
    execution = (row.get('auto_promotion_execution') or workflow_item.get('auto_promotion_execution') or approval_item.get('auto_promotion_execution') or {})
    after = execution.get('after') or {}
    current_stage = str(after.get('rollout_stage') or workflow_item.get('current_rollout_stage') or approval_item.get('rollout_stage') or '').strip().lower()
    validation_gate = _resolve_validation_gate_context(row, workflow_item, approval_item)
    rollback_gate = workflow_item.get('rollback_gate') or approval_item.get('rollback_gate') or row.get('rollback_gate') or {}
    targets = []
    if current_stage == 'controlled_apply':
        targets.extend(['post_apply_samples', 'validation_gate_health', 'transition_journal_drift'])
    elif current_stage == 'review_pending':
        targets.extend(['scheduled_review_checkpoint', 'post_apply_metrics', 'manual_review_outcome'])
    else:
        targets.extend(['workflow_progression', 'risk_regression_watch'])
    if validation_gate.get('enabled'):
        targets.append('validation_gate_regression_watch')
    if rollback_gate.get('candidate'):
        targets.append('rollback_trigger_confirmation')
    for trigger in rollback_gate.get('triggered') or []:
        targets.append(f'rollback_trigger:{trigger}')
    return _dedupe_strings(targets)



def _build_auto_promotion_review_queue_item(*, row: Optional[Dict[str, Any]] = None, workflow_item: Optional[Dict[str, Any]] = None,
                                            approval_item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = row or {}
    workflow_item = workflow_item or {}
    approval_item = approval_item or {}
    execution = (row.get('auto_promotion_execution') or workflow_item.get('auto_promotion_execution') or approval_item.get('auto_promotion_execution') or {})
    scheduled_review = workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or row.get('scheduled_review') or {}
    rollback_gate = workflow_item.get('rollback_gate') or approval_item.get('rollback_gate') or row.get('rollback_gate') or {}
    review_due_at = scheduled_review.get('review_due_at') or execution.get('review_due_at') or row.get('review_due_at')
    rollback_candidate = bool(rollback_gate.get('candidate'))
    queue_kind = 'rollback_review_queue' if rollback_candidate else 'post_promotion_review_queue'
    queue_state = 'rollback_review' if rollback_candidate else ('review_due' if review_due_at else 'observe_window')
    observation_targets = _resolve_auto_promotion_observation_targets(row=row, workflow_item=workflow_item, approval_item=approval_item)
    rollback_triggered = _dedupe_strings(rollback_gate.get('triggered') or row.get('rollback_triggered') or [])
    recommended_action = 'prepare_rollback_review' if rollback_candidate else ('run_scheduled_review' if review_due_at else 'monitor_post_promotion_window')
    return {
        'item_id': row.get('item_id') or workflow_item.get('item_id') or approval_item.get('playbook_id'),
        'approval_id': row.get('approval_id') or approval_item.get('approval_id'),
        'title': row.get('title') or workflow_item.get('title') or approval_item.get('title'),
        'action_type': row.get('action_type') or workflow_item.get('action_type') or approval_item.get('action_type'),
        'workflow_state': row.get('workflow_state') or workflow_item.get('workflow_state') or approval_item.get('workflow_state'),
        'approval_state': row.get('approval_state') or approval_item.get('approval_state'),
        'current_rollout_stage': (execution.get('after') or {}).get('rollout_stage') or workflow_item.get('current_rollout_stage') or approval_item.get('rollout_stage'),
        'queue_kind': queue_kind,
        'queue_state': queue_state,
        'review_due_at': review_due_at,
        'review_window_hours': scheduled_review.get('review_after_hours'),
        'observation_targets': observation_targets,
        'rollback_candidate': rollback_candidate,
        'rollback_triggered': rollback_triggered,
        'recommended_action': recommended_action,
        'why': _dedupe_strings((execution.get('reason_codes') or []) + rollback_triggered + observation_targets),
        'summary': f"{queue_kind} | action={recommended_action} | targets={len(observation_targets)}",
        'why_in_queue': _dedupe_strings((execution.get('reason_codes') or []) + rollback_triggered + observation_targets),
        'queue_reason': 'rollback review candidate' if rollback_candidate else 'post-promotion follow-up',
        'next_step': recommended_action,
    }





def _parse_review_timestamp(value: Any) -> Optional[datetime]:
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return None


def _build_auto_promotion_review_due_snapshot(review_due_at: Any, *, now: Optional[Any] = None) -> Dict[str, Any]:
    due_at = _parse_review_timestamp(review_due_at)
    ref = _parse_review_timestamp(now) if now is not None else datetime.now(timezone.utc)
    if ref and ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    if due_at and due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    snapshot = {
        'review_due_at': review_due_at,
        'due_status': 'unscheduled' if not due_at else 'pending',
        'due_in_seconds': None,
        'due_in_hours': None,
        'is_due': False,
        'is_overdue': False,
    }
    if not due_at or not ref:
        return snapshot
    delta_seconds = int((due_at - ref).total_seconds())
    snapshot['due_in_seconds'] = delta_seconds
    snapshot['due_in_hours'] = round(delta_seconds / 3600.0, 4)
    if delta_seconds < 0:
        snapshot['due_status'] = 'overdue'
        snapshot['is_due'] = True
        snapshot['is_overdue'] = True
    elif delta_seconds == 0:
        snapshot['due_status'] = 'due_now'
        snapshot['is_due'] = True
    else:
        snapshot['due_status'] = 'scheduled'
    return snapshot


def _normalize_auto_promotion_review_queue_item(item: Dict[str, Any], *, now: Optional[Any] = None) -> Dict[str, Any]:
    item = dict(item or {})
    due = _build_auto_promotion_review_due_snapshot(item.get('review_due_at'), now=now)
    queue_kind = item.get('queue_kind') or ('rollback_review_queue' if item.get('rollback_candidate') else 'post_promotion_review_queue')
    rollback_triggered = _dedupe_strings(item.get('rollback_triggered') or [])
    observation_targets = _dedupe_strings(item.get('observation_targets') or [])
    why = _dedupe_strings(item.get('why') or [])
    why_in_queue = []
    if queue_kind == 'rollback_review_queue':
        why_in_queue.append('rollback_candidate')
    else:
        why_in_queue.append('post_promotion_follow_up')
    if due.get('due_status') and due.get('due_status') != 'unscheduled':
        why_in_queue.append(f"due_status:{due.get('due_status')}")
    why_in_queue.extend(rollback_triggered)
    why_in_queue.extend(observation_targets)
    why_in_queue.extend(why)
    why_in_queue = _dedupe_strings(why_in_queue)
    next_step = item.get('recommended_action') or ('prepare_rollback_review' if queue_kind == 'rollback_review_queue' else 'monitor_post_promotion_window')
    queue_reason = 'rollback trigger observed; item escalated into rollback review queue' if queue_kind == 'rollback_review_queue' else 'auto-promotion completed; item remains in post-promotion review window'
    if due.get('due_status') == 'overdue':
        queue_reason += '; scheduled review is overdue'
    elif due.get('due_status') in {'due_now', 'scheduled'}:
        queue_reason += '; scheduled review is active'
    item.update({
        'queue_kind': queue_kind,
        'queue_state': item.get('queue_state') or ('rollback_review' if queue_kind == 'rollback_review_queue' else due.get('due_status')),
        'review_due': due,
        'due_status': due.get('due_status'),
        'due_in_hours': due.get('due_in_hours'),
        'is_due': due.get('is_due'),
        'is_overdue': due.get('is_overdue'),
        'why': why_in_queue,
        'why_in_queue': why_in_queue,
        'queue_reason': queue_reason,
        'next_step': next_step,
        'next_action': next_step,
        'what_to_do_next': next_step,
    })
    return item


def build_auto_promotion_review_queue_detail_view(payload: Optional[Dict[str, Any]] = None, *, item_id: Optional[str] = None,
                                                  approval_id: Optional[str] = None, queue_kind: Optional[str] = None,
                                                  now: Optional[Any] = None) -> Dict[str, Any]:
    payload = payload or {}
    execution = build_auto_promotion_execution_summary(payload, max_items=10000)
    candidates = []
    for qk in ('rollback_review_queue', 'post_promotion_review_queue'):
        if queue_kind and qk != queue_kind:
            continue
        for row in (execution.get('review_queues') or {}).get(qk) or []:
            candidates.append(_normalize_auto_promotion_review_queue_item(row, now=now))
    matched = [row for row in candidates if (item_id and row.get('item_id') == item_id) or (approval_id and row.get('approval_id') == approval_id)]
    item = matched[0] if matched else None
    detail = {
        'schema_version': 'm5_auto_promotion_review_queue_detail_view_v1',
        'found': bool(item),
        'item': item,
        'alternatives': matched[1:10] if len(matched) > 1 else [],
        'summary': {
            'item_id': (item or {}).get('item_id'),
            'approval_id': (item or {}).get('approval_id'),
            'queue_kind': (item or {}).get('queue_kind'),
            'queue_state': (item or {}).get('queue_state'),
            'why_in_queue': (item or {}).get('why_in_queue') or [],
            'queue_reason': (item or {}).get('queue_reason'),
            'next_step': (item or {}).get('next_step'),
            'review_due_at': (item or {}).get('review_due_at'),
            'due_status': (item or {}).get('due_status'),
            'due_in_hours': (item or {}).get('due_in_hours'),
            'observation_targets': (item or {}).get('observation_targets') or [],
            'rollback_triggered': (item or {}).get('rollback_triggered') or [],
        },
    }
    payload['auto_promotion_review_queue_detail_view'] = detail
    return detail


def build_auto_promotion_review_queue_filter_view(payload: Optional[Dict[str, Any]] = None, *, queue_kinds: Any = None,
                                                  due_statuses: Any = None, observation_targets: Any = None,
                                                  rollback_triggers: Any = None, q: Optional[str] = None,
                                                  now: Optional[Any] = None, limit: int = 50) -> Dict[str, Any]:
    payload = payload or {}
    execution = build_auto_promotion_execution_summary(payload, max_items=10000)
    items = []
    for qk in ('rollback_review_queue', 'post_promotion_review_queue'):
        for row in (execution.get('review_queues') or {}).get(qk) or []:
            items.append(_normalize_auto_promotion_review_queue_item(row, now=now))
    queue_kind_set = set(_normalize_filter_values(queue_kinds))
    due_status_set = set(_normalize_filter_values(due_statuses))
    observation_target_set = set(_normalize_filter_values(observation_targets))
    rollback_trigger_set = set(_normalize_filter_values(rollback_triggers))
    q_text = str(q or '').strip().lower()
    filtered = []
    for row in items:
        if queue_kind_set and str(row.get('queue_kind') or '') not in queue_kind_set:
            continue
        if due_status_set and str(row.get('due_status') or '') not in due_status_set:
            continue
        row_targets = {str(v) for v in (row.get('observation_targets') or [])}
        if observation_target_set and not (row_targets & observation_target_set):
            continue
        row_triggers = {str(v) for v in (row.get('rollback_triggered') or [])}
        if rollback_trigger_set and not (row_triggers & rollback_trigger_set):
            continue
        if q_text:
            hay = ' '.join(str(v) for v in [row.get('item_id'), row.get('approval_id'), row.get('title'), row.get('queue_reason'), row.get('next_step')] + (row.get('why_in_queue') or []) + (row.get('observation_targets') or []) + (row.get('rollback_triggered') or []))
            if q_text not in hay.lower():
                continue
        filtered.append(row)
    filtered.sort(key=lambda row: (0 if row.get('queue_kind') == 'rollback_review_queue' else 1, 0 if row.get('is_overdue') else (1 if row.get('is_due') else 2), str(row.get('review_due_at') or ''), str(row.get('item_id') or '')))
    summary = {
        'matched_count': len(filtered),
        'returned_count': min(len(filtered), limit),
        'queue_kind_counts': {k: sum(1 for row in filtered if row.get('queue_kind') == k) for k in sorted({row.get('queue_kind') for row in filtered if row.get('queue_kind')})},
        'due_status_counts': {k: sum(1 for row in filtered if row.get('due_status') == k) for k in sorted({row.get('due_status') for row in filtered if row.get('due_status')})},
        'observation_target_counts': {},
        'rollback_trigger_counts': {},
    }
    for row in filtered:
        for target in row.get('observation_targets') or []:
            summary['observation_target_counts'][target] = summary['observation_target_counts'].get(target, 0) + 1
        for trigger in row.get('rollback_triggered') or []:
            summary['rollback_trigger_counts'][trigger] = summary['rollback_trigger_counts'].get(trigger, 0) + 1
    response = {
        'schema_version': 'm5_auto_promotion_review_queue_filter_view_v1',
        'summary': summary,
        'applied_filters': {
            'queue_kinds': _normalize_filter_values(queue_kinds),
            'due_statuses': _normalize_filter_values(due_statuses),
            'observation_targets': _normalize_filter_values(observation_targets),
            'rollback_triggers': _normalize_filter_values(rollback_triggers),
            'q': str(q or '').strip(),
        },
        'items': filtered[:limit],
    }
    payload['auto_promotion_review_queue_filter_view'] = response
    return response
def build_auto_promotion_execution_summary(payload: Optional[Dict] = None, *, max_items: int = 5) -> Dict[str, Any]:
    payload = payload or {}
    consumer_view = payload.get('consumer_view') or build_workflow_consumer_view(payload)
    controlled_rollout = consumer_view.get('controlled_rollout_execution') or payload.get('controlled_rollout_execution') or {}
    workflow_items = ((consumer_view.get('workflow_state') or {}).get('item_states') or [])
    approval_items = ((consumer_view.get('approval_state') or {}).get('items') or [])
    approval_by_playbook = {row.get('playbook_id'): row for row in approval_items if row.get('playbook_id')}
    workflow_by_item = {row.get('item_id'): row for row in workflow_items if row.get('item_id')}

    recent = []
    rollback_candidates = []
    review_queue_items = []
    reason_code_counts: Dict[str, int] = {}
    stage_transition_counts: Dict[str, int] = {}
    target_stage_counts: Dict[str, int] = {}
    risk_label_counts: Dict[str, int] = {}

    seen_keys = set()
    for workflow_item in workflow_items:
        execution = workflow_item.get('auto_promotion_execution') or {}
        if not execution:
            continue
        approval_item = approval_by_playbook.get(workflow_item.get('item_id')) or {}
        seen_keys.add((workflow_item.get('item_id'), approval_item.get('approval_id')))
        before = execution.get('before') or {}
        after = execution.get('after') or {}
        event_log = execution.get('event_log') or []
        latest_event = event_log[-1] if event_log else {}
        rollback_gate = workflow_item.get('rollback_gate') or approval_item.get('rollback_gate') or {}
        risk_label = ((execution.get('candidate_summary') or {}).get('risk_label') or workflow_item.get('risk_level') or approval_item.get('risk_level') or 'unknown')
        summary_row = {
            'item_id': workflow_item.get('item_id'),
            'approval_id': approval_item.get('approval_id'),
            'title': workflow_item.get('title') or approval_item.get('title') or workflow_item.get('item_id'),
            'action_type': workflow_item.get('action_type') or approval_item.get('action_type'),
            'workflow_state': workflow_item.get('workflow_state') or approval_item.get('workflow_state') or after.get('workflow_state'),
            'approval_state': approval_item.get('approval_state') or after.get('state'),
            'current_rollout_stage': workflow_item.get('current_rollout_stage') or after.get('rollout_stage'),
            'target_rollout_stage': workflow_item.get('target_rollout_stage') or after.get('rollout_stage'),
            'scheduled_review': workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or {},
            'before': before,
            'after': after,
            'reason_codes': execution.get('reason_codes') or [],
            'rollback_hint': execution.get('rollback_hint'),
            'risk_label': risk_label,
            'risk_score': (execution.get('candidate_summary') or {}).get('risk_score'),
            'manual_fallback_required': bool((execution.get('candidate_summary') or {}).get('manual_fallback_required', False)),
            'why_promotable': (execution.get('candidate_summary') or {}).get('why_promotable') or [],
            'actor': latest_event.get('actor'),
            'source': latest_event.get('source'),
            'created_at': latest_event.get('created_at'),
            'event_type': latest_event.get('event_type'),
            'rollback_candidate': bool(rollback_gate.get('candidate')),
            'rollback_triggered': rollback_gate.get('triggered') or [],
        }
        recent.append(summary_row)
        review_queue_items.append(_build_auto_promotion_review_queue_item(row=summary_row, workflow_item=workflow_item, approval_item=approval_item))
        transition_key = f"{before.get('rollout_stage') or 'unknown'}->{after.get('rollout_stage') or 'unknown'}"
        stage_transition_counts[transition_key] = stage_transition_counts.get(transition_key, 0) + 1
        target_key = str(after.get('rollout_stage') or workflow_item.get('target_rollout_stage') or 'unknown')
        target_stage_counts[target_key] = target_stage_counts.get(target_key, 0) + 1
        risk_label_counts[str(risk_label)] = risk_label_counts.get(str(risk_label), 0) + 1
        for code in execution.get('reason_codes') or []:
            reason_code_counts[str(code)] = reason_code_counts.get(str(code), 0) + 1
        if summary_row['rollback_candidate']:
            rollback_candidates.append(summary_row)

    for approval_item in approval_items:
        key = (approval_item.get('playbook_id'), approval_item.get('approval_id'))
        if key in seen_keys:
            continue
        execution = approval_item.get('auto_promotion_execution') or {}
        if not execution:
            continue
        workflow_item = workflow_by_item.get(approval_item.get('playbook_id')) or {}
        before = execution.get('before') or {}
        after = execution.get('after') or {}
        event_log = execution.get('event_log') or []
        latest_event = event_log[-1] if event_log else {}
        rollback_gate = workflow_item.get('rollback_gate') or approval_item.get('rollback_gate') or {}
        risk_label = ((execution.get('candidate_summary') or {}).get('risk_label') or workflow_item.get('risk_level') or approval_item.get('risk_level') or 'unknown')
        summary_row = {
            'item_id': approval_item.get('playbook_id'),
            'approval_id': approval_item.get('approval_id'),
            'title': approval_item.get('title') or approval_item.get('playbook_id'),
            'action_type': approval_item.get('action_type') or workflow_item.get('action_type'),
            'workflow_state': workflow_item.get('workflow_state') or approval_item.get('workflow_state') or after.get('workflow_state'),
            'approval_state': approval_item.get('approval_state') or after.get('state'),
            'current_rollout_stage': workflow_item.get('current_rollout_stage') or after.get('rollout_stage'),
            'target_rollout_stage': workflow_item.get('target_rollout_stage') or after.get('rollout_stage'),
            'scheduled_review': workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or {},
            'before': before,
            'after': after,
            'reason_codes': execution.get('reason_codes') or [],
            'rollback_hint': execution.get('rollback_hint'),
            'risk_label': risk_label,
            'risk_score': (execution.get('candidate_summary') or {}).get('risk_score'),
            'manual_fallback_required': bool((execution.get('candidate_summary') or {}).get('manual_fallback_required', False)),
            'why_promotable': (execution.get('candidate_summary') or {}).get('why_promotable') or [],
            'actor': latest_event.get('actor'),
            'source': latest_event.get('source'),
            'created_at': latest_event.get('created_at'),
            'event_type': latest_event.get('event_type'),
            'rollback_candidate': bool(rollback_gate.get('candidate')),
            'rollback_triggered': rollback_gate.get('triggered') or [],
        }
        recent.append(summary_row)
        review_queue_items.append(_build_auto_promotion_review_queue_item(row=summary_row, workflow_item=workflow_item, approval_item=approval_item))
        transition_key = f"{before.get('rollout_stage') or 'unknown'}->{after.get('rollout_stage') or 'unknown'}"
        stage_transition_counts[transition_key] = stage_transition_counts.get(transition_key, 0) + 1
        target_key = str(after.get('rollout_stage') or workflow_item.get('target_rollout_stage') or 'unknown')
        target_stage_counts[target_key] = target_stage_counts.get(target_key, 0) + 1
        risk_label_counts[str(risk_label)] = risk_label_counts.get(str(risk_label), 0) + 1
        for code in execution.get('reason_codes') or []:
            reason_code_counts[str(code)] = reason_code_counts.get(str(code), 0) + 1
        if summary_row['rollback_candidate']:
            rollback_candidates.append(summary_row)

    recent.sort(key=lambda row: (str(row.get('created_at') or ''), str(row.get('item_id') or '')), reverse=True)
    rollback_candidates.sort(key=lambda row: (str(row.get('created_at') or ''), str(row.get('item_id') or '')), reverse=True)
    review_queue_items.sort(key=lambda row: (0 if row.get('queue_kind') == 'rollback_review_queue' else 1, str(row.get('review_due_at') or ''), str(row.get('item_id') or '')))
    post_promotion_review_queue = [row for row in review_queue_items if row.get('queue_kind') == 'post_promotion_review_queue']
    rollback_review_queue = [row for row in review_queue_items if row.get('queue_kind') == 'rollback_review_queue']
    summary = {
        'event_count': len(recent),
        'executed_count': controlled_rollout.get('executed_count', len(recent)),
        'skipped_count': controlled_rollout.get('skipped_count', 0),
        'recent_execution_count': len(recent),
        'rollback_review_candidate_count': len(rollback_candidates),
        'post_promotion_review_queue_count': len(post_promotion_review_queue),
        'rollback_review_queue_count': len(rollback_review_queue),
        'latest_executed_at': recent[0].get('created_at') if recent else None,
        'stage_transition_counts': stage_transition_counts,
        'target_stage_counts': target_stage_counts,
        'reason_code_counts': reason_code_counts,
        'risk_label_counts': risk_label_counts,
    }
    return {
        'schema_version': 'm5_auto_promotion_execution_summary_v3',
        'summary': summary,
        'recent_executions': recent[:max_items],
        'rollback_review_candidates': rollback_candidates[:max_items],
        'review_queues': {
            'post_promotion_review_queue': post_promotion_review_queue[:max_items],
            'rollback_review_queue': rollback_review_queue[:max_items],
        },
        'execution': controlled_rollout,
    }


def build_auto_promotion_review_queue_consumption(auto_promotion_execution: Optional[Dict[str, Any]] = None, *, max_items: int = 5,
                                                  label: str = 'auto_promotion_review_queue_consumption') -> Dict[str, Any]:
    auto_promotion_execution = auto_promotion_execution or {}
    summary = auto_promotion_execution.get('summary') or {}
    review_queues = auto_promotion_execution.get('review_queues') or {}
    post_queue = list(review_queues.get('post_promotion_review_queue') or [])
    rollback_queue = list(review_queues.get('rollback_review_queue') or [])
    ordered_items = rollback_queue + [
        row for row in post_queue
        if row.get('item_id') not in {item.get('item_id') for item in rollback_queue}
    ]
    dominant_queue_kind = 'rollback_review_queue' if rollback_queue else ('post_promotion_review_queue' if post_queue else 'idle')
    dominant_action = 'prepare_rollback_review' if rollback_queue else ('run_scheduled_review' if post_queue else 'observe_only')
    review_due_items = [row for row in ordered_items if row.get('review_due_at')]
    observation_target_counts: Dict[str, int] = {}
    rollback_trigger_counts: Dict[str, int] = {}
    for row in ordered_items:
        for target in row.get('observation_targets') or []:
            observation_target_counts[str(target)] = observation_target_counts.get(str(target), 0) + 1
        for trigger in row.get('rollback_triggered') or []:
            rollback_trigger_counts[str(trigger)] = rollback_trigger_counts.get(str(trigger), 0) + 1
    dominant_target = None
    if observation_target_counts:
        dominant_target = sorted(observation_target_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    dominant_trigger = None
    if rollback_trigger_counts:
        dominant_trigger = sorted(rollback_trigger_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    status = 'rollback_attention' if rollback_queue else ('review_due' if review_due_items else ('observe_window' if post_queue else 'idle'))
    headline = (
        f"{len(rollback_queue)} rollback review / {len(post_queue)} post-promotion review / "
        f"{len(review_due_items)} review due"
    )
    next_actions = []
    if rollback_queue:
        next_actions.append({
            'kind': 'rollback_review_queue',
            'priority': 'high',
            'count': len(rollback_queue),
            'message': f"{len(rollback_queue)} item(s) escalated into rollback review queue",
            'route': 'rollback_review_queue',
            'follow_up': 'prepare_rollback_review',
            'items': rollback_queue[:max_items],
        })
    if post_queue:
        next_actions.append({
            'kind': 'post_promotion_review_queue',
            'priority': 'medium' if review_due_items else 'low',
            'count': len(post_queue),
            'message': f"{len(post_queue)} item(s) need post-promotion follow-up review",
            'route': 'post_promotion_review_queue',
            'follow_up': 'run_scheduled_review' if review_due_items else 'monitor_post_promotion_window',
            'items': post_queue[:max_items],
        })
    return {
        'schema_version': 'm5_auto_promotion_review_queue_consumption_v2',
        'label': label,
        'status': status,
        'headline': headline,
        'summary': {
            'total_count': len(ordered_items),
            'post_promotion_review_queue_count': summary.get('post_promotion_review_queue_count', len(post_queue)),
            'rollback_review_queue_count': summary.get('rollback_review_queue_count', len(rollback_queue)),
            'review_due_count': len(review_due_items),
            'dominant_queue_kind': dominant_queue_kind,
            'dominant_action': dominant_action,
            'dominant_observation_target': dominant_target,
            'dominant_rollback_trigger': dominant_trigger,
            'latest_executed_at': summary.get('latest_executed_at'),
            'observation_target_counts': observation_target_counts,
            'rollback_trigger_counts': rollback_trigger_counts,
        },
        'items': ordered_items[:max_items],
        'post_promotion_review_queue': post_queue[:max_items],
        'rollback_review_queue': rollback_queue[:max_items],
        'next_actions': next_actions,
    }


def build_workflow_operator_digest(payload: Optional[Dict] = None, *, max_items: int = 5,
                                  transition_journal_overview: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    consumer_view = payload.get('consumer_view') or build_workflow_consumer_view(payload)
    workflow_items = ((consumer_view.get('workflow_state') or {}).get('item_states') or [])
    approval_items = ((consumer_view.get('approval_state') or {}).get('items') or [])
    stage_items = ((consumer_view.get('rollout_stage_progression') or {}).get('items') or [])
    rollout_executor = consumer_view.get('rollout_executor') or {}
    auto_approval = consumer_view.get('auto_approval_execution') or {}
    controlled_rollout = consumer_view.get('controlled_rollout_execution') or {}
    validation_gate = consumer_view.get('validation_gate') or _build_validation_gate_snapshot(payload)
    auto_promotion_execution = build_auto_promotion_execution_summary(payload, max_items=max_items)
    transition_journal = _build_transition_journal_consumer_view(
        overview=transition_journal_overview or payload.get('transition_journal')
    ) if (transition_journal_overview or payload.get('transition_journal')) else {
        'schema_version': 'm5_transition_journal_consumer_v1',
        'headline': {'status': 'steady', 'message': '0 recent transition(s)', 'latest_timestamp': None, 'latest_transition': None},
        'summary': {'count': 0, 'latest_timestamp': None, 'changed_only': True, 'changed_field_counts': {}, 'workflow_transition_counts': {}},
        'recent_transitions': [],
        'latest': {},
        'overview': {'schema_version': 'm5_transition_journal_overview_v1', 'summary': {'count': 0, 'changed_field_counts': {}}, 'recent_transitions': [], 'breakdown': {'changed_field_counts': {}, 'trigger_counts': {}, 'actor_counts': {}, 'source_counts': {}}},
    }

    workflow_lookup = {row.get('item_id'): row for row in workflow_items if row.get('item_id')}
    approval_by_playbook = {row.get('playbook_id'): row for row in approval_items if row.get('playbook_id')}

    manual_approval_items = []
    blocked_items = []
    ready_items = []
    queued_items = []
    deferred_items = []
    auto_advance_items = []
    rollback_candidate_items = []

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
            'state_machine': workflow_item.get('state_machine') or {},
        }
        gates = _extract_rollout_gate_snapshot(workflow_item, approval_item, row.get('state_machine') or {})
        row['auto_advance_gate'] = gates.get('auto_advance_gate') or {}
        row['rollback_gate'] = gates.get('rollback_gate') or {}
        row['stage_loop'] = _resolve_stage_loop_snapshot(workflow_item=workflow_item, approval_item=approval_item, row=row)
        row['operator_action_policy'] = (row.get('state_machine') or {}).get('operator_action_policy') or _build_operator_action_policy(
            item_id=item_id,
            approval_state=row.get('approval_state'),
            workflow_state=workflow_state,
            queue_status=(row.get('queue_progression') or {}).get('status'),
            dispatch_route=(row.get('queue_progression') or {}).get('dispatch_route'),
            next_transition=(row.get('queue_progression') or {}).get('next_transition'),
            blocked_by=blocked_by,
            retryable=(row.get('state_machine') or {}).get('retryable'),
            rollout_stage=row.get('current_rollout_stage'),
            target_rollout_stage=row.get('target_rollout_stage'),
            terminal=bool((row.get('state_machine') or {}).get('terminal')),
            validation_gate=_resolve_validation_gate_context(workflow_item, approval_item, row, {'validation_gate': validation_gate}),
        )
        row['lane_routing'] = _resolve_lane_routing(workflow_item=workflow_item, approval_item=approval_item, row=row, operator_action_policy=row['operator_action_policy'], stage_loop=row['stage_loop'])
        row['rollout_advisory'] = _resolve_rollout_advisory_snapshot(workflow_item=workflow_item, approval_item=approval_item, row=row)
        row['lane_id'] = row['lane_routing'].get('lane_id')
        row['queue_name'] = row['lane_routing'].get('queue_name')
        row['dispatch_route'] = row['lane_routing'].get('dispatch_route')
        row['route_family'] = row['lane_routing'].get('route_family')
        row['next_transition'] = row['lane_routing'].get('next_transition')
        if row['lane_id'] == 'manual_approval':
            manual_approval_items.append(row)
        if row['lane_id'] == 'blocked':
            blocked_items.append(row)
        if row['lane_id'] == 'ready':
            ready_items.append(row)
        if row['lane_id'] == 'queued':
            queued_items.append(row)
        if workflow_state == 'deferred':
            deferred_items.append(row)
        if row['lane_id'] == 'auto_batch':
            auto_advance_items.append(row)
        if row['lane_id'] == 'rollback_candidate':
            rollback_candidate_items.append(row)

    blocked_items.sort(key=_sort_key)
    manual_approval_items.sort(key=_sort_key)
    ready_items.sort(key=_sort_key)
    queued_items.sort(key=_sort_key)
    deferred_items.sort(key=_sort_key)
    auto_advance_items.sort(key=_sort_key)
    rollback_candidate_items.sort(key=_sort_key)

    next_actions = []
    policy_rows = []
    for row in workflow_items:
        item_id = row.get('item_id')
        approval_item = approval_by_playbook.get(item_id) or {}
        policy = ((row.get('state_machine') or {}).get('operator_action_policy') or {})
        if not policy:
            policy = _build_operator_action_policy(
                item_id=item_id,
                approval_state=approval_item.get('approval_state') or row.get('approval_state'),
                workflow_state=row.get('workflow_state'),
                queue_status=((row.get('queue_progression') or {}).get('status') if isinstance(row.get('queue_progression'), dict) else None),
                dispatch_route=((row.get('queue_progression') or {}).get('dispatch_route') if isinstance(row.get('queue_progression'), dict) else None),
                next_transition=((row.get('queue_progression') or {}).get('next_transition') if isinstance(row.get('queue_progression'), dict) else None),
                blocked_by=(row.get('blocking_reasons') or []) + (approval_item.get('blocked_by') or []),
                retryable=(row.get('state_machine') or {}).get('retryable'),
                rollout_stage=row.get('current_rollout_stage') or row.get('rollout_stage'),
                target_rollout_stage=row.get('target_rollout_stage'),
                terminal=bool((row.get('state_machine') or {}).get('terminal')),
                validation_gate=_resolve_validation_gate_context(row, approval_item, {'validation_gate': validation_gate}),
            )
        gates = _extract_rollout_gate_snapshot(row, approval_item)
        enriched = {
            'item_id': item_id,
            'title': row.get('title') or approval_item.get('title') or item_id,
            'action_type': row.get('action_type') or approval_item.get('action_type'),
            'workflow_state': row.get('workflow_state') or 'pending',
            'approval_state': approval_item.get('approval_state') or row.get('approval_state') or 'not_required',
            'risk_level': row.get('risk_level') or approval_item.get('risk_level') or 'unknown',
            'operator_action_policy': policy,
            'auto_advance_gate': gates.get('auto_advance_gate') or {},
            'rollback_gate': gates.get('rollback_gate') or {},
            'stage_loop': row.get('stage_loop') or _resolve_stage_loop_snapshot(workflow_item=row, approval_item=approval_item, row=row),
            'rollout_advisory': row.get('rollout_advisory') or _resolve_rollout_advisory_snapshot(workflow_item=row, approval_item=approval_item, row=row),
        }
        policy_rows.append(enriched)

    grouped_actions = {}
    for row in policy_rows:
        policy = row.get('operator_action_policy') or {}
        key = policy.get('action') or 'observe_only_followup'
        grouped_actions.setdefault(key, []).append(row)
    action_priority = {'escalate': 0, 'freeze_followup': 1, 'retry': 2, 'review_schedule': 3, 'observe_only_followup': 4}
    action_messages = {
        'review_schedule': 'item(s) should be scheduled for review / approval routing',
        'retry': 'item(s) are safe to retry once blockers clear',
        'escalate': 'item(s) should be escalated to operator review',
        'freeze_followup': 'item(s) should stay frozen pending guarded follow-up',
        'observe_only_followup': 'item(s) are observe-only and can be monitored with low intervention',
    }
    for kind, items in sorted(grouped_actions.items(), key=lambda kv: (action_priority.get(kv[0], 9), kv[0])):
        sorted_items = sorted(items, key=_sort_key)
        top_policy = (sorted_items[0].get('operator_action_policy') or {}) if sorted_items else {}
        next_actions.append({
            'kind': kind,
            'priority': top_policy.get('priority') or ('high' if kind in {'escalate', 'freeze_followup'} else 'medium'),
            'count': len(sorted_items),
            'message': f"{len(sorted_items)} {action_messages.get(kind, 'item(s) need follow-up')}",
            'route': top_policy.get('route'),
            'follow_up': top_policy.get('follow_up'),
            'summary': _build_low_intervention_group_summary(sorted_items, label=kind),
            'items': sorted_items[:max_items],
        })

    gate_consumption = _build_gate_consumption_summary(policy_rows, label='workflow_operator_digest', max_items=max_items)
    advisory_consumption = _summarize_rollout_advisories(policy_rows, label='workflow_operator_digest', max_items=max_items)

    group_summaries = {
        'by_lane': [
            {
                'group_id': group_id,
                'summary': _build_low_intervention_group_summary(items, label=group_id),
            }
            for group_id, items in [
                ('manual_approval', manual_approval_items),
                ('blocked', blocked_items),
                ('queued', queued_items),
                ('ready', ready_items),
                ('auto_batch', auto_advance_items),
                ('rollback_candidate', rollback_candidate_items),
            ] if items
        ],
        'by_operator_action': [
            {
                'group_id': row.get('kind'),
                'summary': row.get('summary') or {},
            }
            for row in next_actions
        ],
    }

    headline_status = 'attention_required' if manual_approval_items or blocked_items else 'steady'
    if validation_gate.get('enabled') and validation_gate.get('freeze_auto_advance'):
        headline_status = 'attention_required'
    elif ready_items and not manual_approval_items and not blocked_items:
        headline_status = 'ready_to_consume'

    digest = {
        'schema_version': 'm5_workflow_operator_digest_v1',
        'headline': {
            'status': headline_status,
            'message': (
                f"{len(manual_approval_items)} manual approval / {len(blocked_items)} blocked / {len(ready_items)} ready / {len(queued_items)} queued / {len(auto_advance_items)} auto / {len(rollback_candidate_items)} rollback"
                + (f" / validation={('ready' if validation_gate.get('ready') else 'freeze')}" if validation_gate.get('enabled') else '')
            ),
            'rollout_executor_status': rollout_executor.get('status') or 'disabled',
            'validation_gate': validation_gate,
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
            'rollback_candidate_count': len(rollback_candidate_items),
            'stage_loop': _summarize_stage_loop_rows(policy_rows, label='workflow_operator_digest', max_items=max_items),
            'rollout_executor_status': rollout_executor.get('status') or 'disabled',
            'auto_approval_executed_count': auto_approval.get('executed_count', 0),
            'controlled_rollout_executed_count': controlled_rollout.get('executed_count', 0),
            'stage_progression': (consumer_view.get('rollout_stage_progression') or {}).get('summary') or {},
            'operator_action_counts': {row.get('kind'): row.get('count', 0) for row in next_actions},
            'operator_routes': sorted({row.get('route') for row in next_actions if row.get('route')}),
            'operator_follow_ups': sorted({row.get('follow_up') for row in next_actions if row.get('follow_up')}),
            'group_summaries': group_summaries,
            'gate_consumption': gate_consumption,
            'rollout_advisory': advisory_consumption,
            'auto_promotion_execution': auto_promotion_execution.get('summary') or {},
            'post_promotion_review_queue_count': ((auto_promotion_execution.get('summary') or {}).get('post_promotion_review_queue_count') or 0),
            'rollback_review_queue_count': ((auto_promotion_execution.get('summary') or {}).get('rollback_review_queue_count') or 0),
            'transition_count': (transition_journal.get('summary') or {}).get('count', 0),
            'latest_transition_at': (transition_journal.get('summary') or {}).get('latest_timestamp'),
            'latest_transition': transition_journal.get('latest') or {},
            'validation_gate': validation_gate,
        },
        'attention': {
            'manual_approval': manual_approval_items[:max_items],
            'blocked': blocked_items[:max_items],
            'queued': queued_items[:max_items],
            'ready': ready_items[:max_items],
            'auto_advance_candidates': auto_advance_items[:max_items],
            'rollback_candidates': rollback_candidate_items[:max_items],
            'auto_promotion_candidates': advisory_consumption.get('auto_promotion_candidates') or [],
            'recent_auto_promotions': auto_promotion_execution.get('recent_executions') or [],
            'auto_promotion_post_promotion_review_queue': ((auto_promotion_execution.get('review_queues') or {}).get('post_promotion_review_queue') or []),
            'auto_promotion_rollback_candidates': auto_promotion_execution.get('rollback_review_candidates') or [],
            'auto_promotion_rollback_review_queue': ((auto_promotion_execution.get('review_queues') or {}).get('rollback_review_queue') or []),
        },
        'next_actions': next_actions,
        'group_summaries': group_summaries,
        'operator_action_policies': policy_rows[:max_items],
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
            'rollout_advisory': advisory_consumption,
        },
        'stage_loop': _summarize_stage_loop_rows(policy_rows, label='workflow_operator_digest', max_items=max_items),
        'transition_journal': transition_journal,
        'auto_promotion_execution': auto_promotion_execution,
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
    validation_gate = consumer_view.get('validation_gate') or digest_summary.get('validation_gate') or _build_validation_gate_snapshot(payload)
    validation_consumption = _collect_validation_gate_consumption((consumer_view.get('workflow_state') or {}).get('item_states') or [])

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
                'rollback_candidate': digest_summary.get('rollback_candidate_count', 0),
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
            'card_id': 'validation_gate',
            'title': 'Validation gate',
            'status': 'attention_required' if validation_gate.get('enabled') and not validation_gate.get('ready') else ('steady' if validation_gate.get('enabled') else 'disabled'),
            'headline': validation_gate.get('headline') or 'validation_gate_disabled',
            'metrics': {
                'gap_count': validation_gate.get('gap_count', 0),
                'failing_cases': validation_gate.get('failing_case_count', 0),
                'freeze_items': validation_consumption.get('item_count', 0),
                'regression_detected': 1 if validation_gate.get('regression_detected') else 0,
            },
            'details': {
                'validation_gate': validation_gate,
                'consumption': validation_consumption,
            },
            'items': [
                {'kind': 'freeze_reason', 'value': key, 'count': value}
                for key, value in sorted((validation_consumption.get('freeze_reason_counts') or {}).items(), key=lambda item: (-item[1], item[0]))[:max_items]
            ] + [
                {'kind': 'rollback_trigger', 'value': key, 'count': value}
                for key, value in sorted((validation_consumption.get('rollback_trigger_counts') or {}).items(), key=lambda item: (-item[1], item[0]))[:max_items]
            ],
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
            'rollback_candidate_count': digest_summary.get('rollback_candidate_count', 0),
            'gate_consumption': digest_summary.get('gate_consumption') or {},
            'validation_gate': validation_gate,
            'validation_gate_consumption': validation_consumption,
            'executor_status': rollout_executor.get('status') or 'disabled',
            'bridge_mode': controlled_rollout.get('mode') or 'disabled',
            'auto_approval_mode': auto_approval.get('mode') or 'disabled',
            'attention_item_count': attention_summary.get('attention_item_count', 0),
            'pending_approval_count': approval_summary.get('pending_count', 0),
            'approval_roles': approval_summary.get('roles') or [],
            'stage_progression': stage_summary,
            'workflow_state_summary': workflow_summary,
            'approval_state_summary': approval_summary,
            'operator_action_counts': digest_summary.get('operator_action_counts') or {},
            'operator_routes': digest_summary.get('operator_routes') or [],
            'operator_follow_ups': digest_summary.get('operator_follow_ups') or [],
            'auto_promotion_execution': digest_summary.get('auto_promotion_execution') or {},
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



def _workbench_risk_rank(value: Optional[str]) -> int:
    return {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}.get(str(value or '').lower(), 9)


def _workbench_item_sort_key(row: Dict[str, Any]):
    return (
        _workbench_risk_rank(row.get('risk_level')),
        0 if row.get('requires_manual') else 1,
        0 if row.get('approval_required') else 1,
        str(row.get('lane_id') or ''),
        str(row.get('title') or row.get('item_id') or ''),
    )


def _build_workbench_item_catalog(payload: Optional[Dict] = None) -> Dict[str, Any]:
    payload = payload or {}
    consumer_view = payload.get('consumer_view') or build_workflow_consumer_view(payload)
    attention_view = payload.get('attention_view') or build_workflow_attention_view(payload)
    operator_digest = payload.get('operator_digest') or build_workflow_operator_digest(payload)
    stage_progression = consumer_view.get('rollout_stage_progression') or {}
    stage_summary = stage_progression.get('summary') or {}
    rollout_executor = consumer_view.get('rollout_executor') or {}
    auto_approval = consumer_view.get('auto_approval_execution') or {}
    controlled_rollout = consumer_view.get('controlled_rollout_execution') or {}
    workflow_items = ((consumer_view.get('workflow_state') or {}).get('item_states') or [])
    approval_items = ((consumer_view.get('approval_state') or {}).get('items') or [])
    workflow_lookup = {row.get('item_id'): row for row in workflow_items if row.get('item_id')}
    approval_by_playbook = {row.get('playbook_id'): row for row in approval_items if row.get('playbook_id')}

    attention_map = {}
    for bucket_id, rows in (operator_digest.get('attention') or {}).items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            item_id = row.get('item_id') or row.get('playbook_id')
            if item_id:
                attention_map.setdefault(item_id, set()).add(bucket_id)

    def _merge_snapshot(row: Dict[str, Any], lane_id: str) -> Dict[str, Any]:
        item_id = row.get('item_id') or row.get('playbook_id')
        workflow_item = workflow_lookup.get(item_id) or row
        approval_item = approval_by_playbook.get(item_id) or {}
        blocked_by = list(dict.fromkeys((workflow_item.get('blocking_reasons') or []) + (approval_item.get('blocked_by') or []) + (row.get('blocked_by') or [])))
        workflow_state = workflow_item.get('workflow_state') or approval_item.get('workflow_state') or row.get('workflow_state') or 'pending'
        approval_state = approval_item.get('approval_state') or row.get('approval_state') or 'not_required'
        current_stage = workflow_item.get('current_rollout_stage') or approval_item.get('rollout_stage') or row.get('current_rollout_stage') or row.get('rollout_stage') or 'pending'
        target_stage = workflow_item.get('target_rollout_stage') or approval_item.get('target_rollout_stage') or row.get('target_rollout_stage') or row.get('rollout_stage') or current_stage
        queue_progression = workflow_item.get('queue_progression') or row.get('queue_progression') or {}
        stage_model = workflow_item.get('stage_model') or approval_item.get('stage_model') or {}
        auto_decision = workflow_item.get('auto_approval_decision') or approval_item.get('auto_approval_decision') or row.get('auto_approval_decision') or 'manual_review'
        requires_manual = bool(workflow_item.get('requires_manual', approval_item.get('requires_manual', row.get('requires_manual'))))
        approval_required = bool(workflow_item.get('approval_required', approval_item.get('approval_required', row.get('approval_required'))))
        state_machine = workflow_item.get('state_machine') or approval_item.get('state_machine') or row.get('state_machine') or {}
        gates = _extract_rollout_gate_snapshot(workflow_item, approval_item, row)
        auto_advance_gate = gates.get('auto_advance_gate') or {}
        rollback_gate = gates.get('rollback_gate') or {}
        operator_action_policy = (state_machine.get('operator_action_policy') or row.get('operator_action_policy') or _build_operator_action_policy(
            item_id=item_id,
            approval_state=approval_state,
            workflow_state=workflow_state,
            queue_status=queue_progression.get('status'),
            dispatch_route=queue_progression.get('dispatch_route'),
            next_transition=queue_progression.get('next_transition'),
            blocked_by=blocked_by,
            retryable=state_machine.get('retryable'),
            rollout_stage=current_stage,
            target_rollout_stage=target_stage,
            terminal=bool(state_machine.get('terminal')),
            validation_gate=_resolve_validation_gate_context(workflow_item, approval_item, row, {'validation_gate': consumer_view.get('validation_gate')}),
        ))
        stage_loop = _resolve_stage_loop_snapshot(workflow_item=workflow_item, approval_item=approval_item, row=row)
        rollout_advisory = _resolve_rollout_advisory_snapshot(workflow_item=workflow_item, approval_item=approval_item, row=row)
        lane_routing = _resolve_lane_routing(workflow_item=workflow_item, approval_item=approval_item, row=row, operator_action_policy=operator_action_policy, stage_loop=stage_loop)
        lane_id = lane_routing.get('lane_id') or lane_id
        bucket_tags = sorted(attention_map.get(item_id) or set())
        if lane_id == 'blocked' and 'blocked' not in bucket_tags:
            bucket_tags.append('blocked')
        if lane_id == 'manual_approval' and 'manual_approval' not in bucket_tags:
            bucket_tags.append('manual_approval')
        if lane_id == 'auto_batch' and 'auto_batch' not in bucket_tags:
            bucket_tags.append('auto_batch')
        if lane_id == 'queued' and 'queued' not in bucket_tags:
            bucket_tags.append('queued')
        if lane_id == 'ready' and 'ready' not in bucket_tags:
            bucket_tags.append('ready')
        if auto_advance_gate.get('allowed') and 'auto_advance_allowed' not in bucket_tags:
            bucket_tags.append('auto_advance_allowed')
        if rollback_gate.get('candidate') and 'rollback_candidate' not in bucket_tags:
            bucket_tags.append('rollback_candidate')
        if rollout_advisory.get('auto_promotion_candidate') and 'auto_promotion_candidate' not in bucket_tags:
            bucket_tags.append('auto_promotion_candidate')
        why_parts = []
        if blocked_by:
            why_parts.append('blocked_by=' + ','.join(blocked_by))
        if approval_required:
            why_parts.append(f'approval={approval_state}')
        else:
            why_parts.append(f'workflow={workflow_state}')
        why_parts.append(f'rollout={current_stage}->{target_stage}')
        if auto_decision:
            why_parts.append(f'auto={auto_decision}')
        if queue_progression.get('next_action'):
            next_step = queue_progression.get('next_action')
        else:
            next_step = operator_action_policy.get('follow_up') or stage_model.get('next_on_approval') or 'observe'
        return {
            'item_id': item_id,
            'approval_id': approval_item.get('approval_id') or row.get('approval_id'),
            'title': workflow_item.get('title') or approval_item.get('title') or row.get('title') or item_id,
            'action_type': workflow_item.get('action_type') or approval_item.get('action_type') or row.get('action_type') or 'unknown',
            'workflow_state': workflow_state,
            'approval_state': approval_state,
            'decision_state': approval_item.get('decision_state') or workflow_item.get('decision_state') or workflow_state,
            'risk_level': workflow_item.get('risk_level') or approval_item.get('risk_level') or row.get('risk_level') or 'unknown',
            'auto_approval_decision': auto_decision,
            'auto_approval_eligible': bool(workflow_item.get('auto_approval_eligible', approval_item.get('auto_approval_eligible', row.get('auto_approval_eligible')))),
            'requires_manual': requires_manual,
            'approval_required': approval_required,
            'blocked_by': blocked_by,
            'owner_hint': workflow_item.get('owner_hint') or approval_item.get('owner_hint') or row.get('owner_hint'),
            'queue_progression': queue_progression,
            'stage_model': stage_model,
            'current_rollout_stage': current_stage,
            'target_rollout_stage': target_stage,
            'scheduled_review': workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or row.get('scheduled_review') or {},
            'auto_advance_gate': auto_advance_gate,
            'rollback_gate': rollback_gate,
            'stage_loop': stage_loop,
            'rollout_advisory': rollout_advisory,
            'lane_routing': lane_routing,
            'lane_id': lane_id,
            'lane_title': lane_routing.get('lane_title') or lane_id.replace('_', ' '),
            'queue_name': lane_routing.get('queue_name'),
            'dispatch_route': lane_routing.get('dispatch_route'),
            'route_family': lane_routing.get('route_family'),
            'next_transition': lane_routing.get('next_transition'),
            'bucket_tags': sorted(set(bucket_tags)),
            'operator_action_policy': operator_action_policy,
            'operator_action': operator_action_policy.get('action'),
            'operator_route': operator_action_policy.get('route'),
            'operator_follow_up': operator_action_policy.get('follow_up'),
            'why': why_parts,
            'why_summary': ' | '.join(why_parts),
            'next_step': next_step,
            'detail': {
                'workflow_item': workflow_item,
                'approval_item': approval_item,
                'attention_buckets': sorted(attention_map.get(item_id) or set()),
            },
        }

    lane_specs = {
        'auto_batch': (operator_digest.get('attention') or {}).get('auto_advance_candidates') or [],
        'blocked': (operator_digest.get('attention') or {}).get('blocked') or [],
        'queued': (operator_digest.get('attention') or {}).get('queued') or [],
        'ready': (operator_digest.get('attention') or {}).get('ready') or [],
        'manual_approval': (operator_digest.get('attention') or {}).get('manual_approval') or [],
        'rollback_candidate': (operator_digest.get('attention') or {}).get('rollback_candidates') or [],
    }

    items = []
    seen = set()
    lane_counts = {}
    for lane_id, rows in lane_specs.items():
        snapshots = sorted((_merge_snapshot(row, lane_id) for row in rows), key=_workbench_item_sort_key)
        lane_counts[lane_id] = len(snapshots)
        for snapshot in snapshots:
            key = (snapshot.get('lane_id'), snapshot.get('item_id'), snapshot.get('approval_id'))
            if key in seen:
                continue
            seen.add(key)
            items.append(snapshot)

    filters = {
        'lane_ids': sorted({row.get('lane_id') for row in items if row.get('lane_id')}),
        'action_types': sorted({row.get('action_type') or 'unknown' for row in items}),
        'risk_levels': sorted({row.get('risk_level') or 'unknown' for row in items}),
        'workflow_states': sorted({row.get('workflow_state') or 'pending' for row in items}),
        'approval_states': sorted({row.get('approval_state') or 'not_required' for row in items}),
        'current_rollout_stages': sorted({row.get('current_rollout_stage') or 'pending' for row in items}),
        'target_rollout_stages': sorted({row.get('target_rollout_stage') or 'pending' for row in items}),
        'bucket_tags': sorted({tag for row in items for tag in (row.get('bucket_tags') or [])}),
        'auto_approval_decisions': sorted({row.get('auto_approval_decision') or 'manual_review' for row in items}),
        'operator_actions': sorted({row.get('operator_action') or 'observe_only_followup' for row in items}),
        'operator_routes': sorted({row.get('operator_route') or 'observe_only_followup' for row in items}),
        'operator_follow_ups': sorted({row.get('operator_follow_up') or 'observe_only' for row in items}),
        'owner_hints': sorted({row.get('owner_hint') for row in items if row.get('owner_hint')}),
        'gate_tags': sorted({tag for row in items for tag in ([('auto_advance_allowed' if (row.get('auto_advance_gate') or {}).get('allowed') else None), ('rollback_candidate' if (row.get('rollback_gate') or {}).get('candidate') else None)]) if tag}),
    }

    catalog = {
        'schema_version': 'm5_workbench_governance_catalog_v1',
        'summary': {
            'item_count': len(items),
            'lane_counts': lane_counts,
            'rollout_executor_status': rollout_executor.get('status') or 'disabled',
            'stage_progression': stage_summary,
            'gate_consumption': _build_gate_consumption_summary(items, label='workbench_catalog'),
        },
        'filters': filters,
        'items': items,
        'upstreams': {
            'workflow_consumer_view': consumer_view,
            'workflow_attention_view': attention_view,
            'workflow_operator_digest': operator_digest,
        },
    }
    payload['workbench_governance_catalog'] = catalog
    return catalog


def _normalize_filter_values(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = str(value).split(',')
    return [str(v).strip().lower() for v in values if str(v).strip()]


def _filter_workbench_catalog_items(items: List[Dict[str, Any]], *, lane_ids: Any = None, action_types: Any = None,
                                    risk_levels: Any = None, workflow_states: Any = None, approval_states: Any = None,
                                    current_rollout_stages: Any = None, target_rollout_stages: Any = None,
                                    bucket_tags: Any = None, auto_approval_decisions: Any = None,
                                    operator_actions: Any = None, operator_routes: Any = None, operator_follow_ups: Any = None,
                                    owner_hints: Any = None, q: Optional[str] = None) -> List[Dict[str, Any]]:
    lane_filter = set(_normalize_filter_values(lane_ids))
    action_filter = set(_normalize_filter_values(action_types))
    risk_filter = set(_normalize_filter_values(risk_levels))
    workflow_filter = set(_normalize_filter_values(workflow_states))
    approval_filter = set(_normalize_filter_values(approval_states))
    current_stage_filter = set(_normalize_filter_values(current_rollout_stages))
    target_stage_filter = set(_normalize_filter_values(target_rollout_stages))
    bucket_filter = set(_normalize_filter_values(bucket_tags))
    auto_filter = set(_normalize_filter_values(auto_approval_decisions))
    operator_action_filter = set(_normalize_filter_values(operator_actions))
    operator_route_filter = set(_normalize_filter_values(operator_routes))
    operator_follow_up_filter = set(_normalize_filter_values(operator_follow_ups))
    owner_filter = set(_normalize_filter_values(owner_hints))
    q_norm = str(q or '').strip().lower()

    def _matches(row: Dict[str, Any]) -> bool:
        if lane_filter and str(row.get('lane_id') or '').lower() not in lane_filter:
            return False
        if action_filter and str(row.get('action_type') or '').lower() not in action_filter:
            return False
        if risk_filter and str(row.get('risk_level') or '').lower() not in risk_filter:
            return False
        if workflow_filter and str(row.get('workflow_state') or '').lower() not in workflow_filter:
            return False
        if approval_filter and str(row.get('approval_state') or '').lower() not in approval_filter:
            return False
        if current_stage_filter and str(row.get('current_rollout_stage') or '').lower() not in current_stage_filter:
            return False
        if target_stage_filter and str(row.get('target_rollout_stage') or '').lower() not in target_stage_filter:
            return False
        if auto_filter and str(row.get('auto_approval_decision') or '').lower() not in auto_filter:
            return False
        if operator_action_filter and str(row.get('operator_action') or '').lower() not in operator_action_filter:
            return False
        if operator_route_filter and str(row.get('operator_route') or '').lower() not in operator_route_filter:
            return False
        if operator_follow_up_filter and str(row.get('operator_follow_up') or '').lower() not in operator_follow_up_filter:
            return False
        if owner_filter and str(row.get('owner_hint') or '').lower() not in owner_filter:
            return False
        if bucket_filter and not bucket_filter.intersection({str(tag).lower() for tag in (row.get('bucket_tags') or [])}):
            return False
        if q_norm:
            haystacks = [
                row.get('item_id'), row.get('approval_id'), row.get('title'), row.get('action_type'), row.get('workflow_state'),
                row.get('approval_state'), row.get('risk_level'), row.get('owner_hint'), row.get('why_summary'), row.get('next_step'),
                row.get('operator_action'), row.get('operator_route'), row.get('operator_follow_up'),
            ] + list(row.get('blocked_by') or []) + list(row.get('bucket_tags') or [])
            if q_norm not in ' '.join(str(v or '').lower() for v in haystacks):
                return False
        return True

    return [row for row in items if _matches(row)]


def build_workbench_governance_filter_view(payload: Optional[Dict] = None, *, lane_ids: Any = None, action_types: Any = None,
                                           risk_levels: Any = None, workflow_states: Any = None, approval_states: Any = None,
                                           current_rollout_stages: Any = None, target_rollout_stages: Any = None, bucket_tags: Any = None,
                                           auto_approval_decisions: Any = None, operator_actions: Any = None, operator_routes: Any = None,
                                           operator_follow_ups: Any = None, owner_hints: Any = None, q: Optional[str] = None,
                                           limit: int = 50) -> Dict[str, Any]:
    payload = payload or {}
    catalog = payload.get('workbench_governance_catalog') or _build_workbench_item_catalog(payload)
    filtered_items = sorted(_filter_workbench_catalog_items(
        catalog.get('items') or [],
        lane_ids=lane_ids,
        action_types=action_types,
        risk_levels=risk_levels,
        workflow_states=workflow_states,
        approval_states=approval_states,
        current_rollout_stages=current_rollout_stages,
        target_rollout_stages=target_rollout_stages,
        bucket_tags=bucket_tags,
        auto_approval_decisions=auto_approval_decisions,
        operator_actions=operator_actions,
        operator_routes=operator_routes,
        operator_follow_ups=operator_follow_ups,
        owner_hints=owner_hints,
        q=q,
    ), key=_workbench_item_sort_key)
    response = {
        'schema_version': 'm5_workbench_governance_filter_view_v1',
        'summary': {
            'matched_count': len(filtered_items),
            'returned_count': min(len(filtered_items), limit),
            'lane_counts': dict(sorted({lane: sum(1 for row in filtered_items if row.get('lane_id') == lane) for lane in {row.get('lane_id') for row in filtered_items if row.get('lane_id')}}.items())),
        },
        'applied_filters': {
            'lane_ids': _normalize_filter_values(lane_ids),
            'action_types': _normalize_filter_values(action_types),
            'risk_levels': _normalize_filter_values(risk_levels),
            'workflow_states': _normalize_filter_values(workflow_states),
            'approval_states': _normalize_filter_values(approval_states),
            'current_rollout_stages': _normalize_filter_values(current_rollout_stages),
            'target_rollout_stages': _normalize_filter_values(target_rollout_stages),
            'bucket_tags': _normalize_filter_values(bucket_tags),
            'auto_approval_decisions': _normalize_filter_values(auto_approval_decisions),
            'operator_actions': _normalize_filter_values(operator_actions),
            'operator_routes': _normalize_filter_values(operator_routes),
            'operator_follow_ups': _normalize_filter_values(operator_follow_ups),
            'owner_hints': _normalize_filter_values(owner_hints),
            'q': str(q or '').strip(),
        },
        'available_filters': catalog.get('filters') or {},
        'items': filtered_items[:limit],
    }
    payload['workbench_governance_filter_view'] = response
    return response


def _build_workbench_queue_handler_drilldown(item: Dict[str, Any], workflow_item: Dict[str, Any], approval_item: Dict[str, Any],
                                            executor_item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    executor_item = executor_item or {}
    plan = executor_item.get('plan') or {}
    dispatch = executor_item.get('dispatch') or {}
    result = executor_item.get('result') or {}
    queue_progression = workflow_item.get('queue_progression') or approval_item.get('queue_progression') or {}
    queue_plan = plan.get('queue_plan') or result.get('queue_plan') or dispatch.get('queue_plan') or {}
    approval_hook = queue_plan.get('approval_hook') or {}
    queue_name = queue_plan.get('queue_name') or queue_progression.get('queue_name') or approval_item.get('bucket_id') or workflow_item.get('bucket_id') or item.get('lane_id') or 'unknown_queue'
    route = queue_plan.get('dispatch_route') or dispatch.get('dispatch_route') or result.get('dispatch_route') or queue_progression.get('dispatch_route') or 'unknown_route'
    handler = {
        'handler_key': plan.get('handler_key') or dispatch.get('handler_key') or result.get('handler_key') or approval_item.get('safe_handler_key') or 'queue::unresolved',
        'executor_class': plan.get('executor_class') or dispatch.get('executor_class') or result.get('executor_class') or approval_item.get('safe_handler_stage_family') or 'queue',
        'stage_family': (approval_item.get('safe_handler_stage_family') or plan.get('safe_handler', {}).get('stage_family') or 'queue'),
        'route': route,
        'disposition': plan.get('dispatch_mode') or dispatch.get('mode') or result.get('disposition') or approval_item.get('safe_handler_disposition') or 'observe',
    }
    why = [
        f"lane={item.get('lane_id') or '--'}",
        f"queue_status={queue_progression.get('status') or queue_plan.get('queue_progression', {}).get('status') or workflow_item.get('workflow_state') or '--'}",
    ]
    gate_reason = queue_progression.get('gate_reason') or queue_plan.get('blocked_reason') or approval_hook.get('gate_reason')
    if gate_reason:
        why.append(f'gate_reason={gate_reason}')
    if item.get('blocked_by'):
        why.append('blocked_by=' + ','.join(item.get('blocked_by') or []))
    return {
        'queue_name': queue_name,
        'route': route,
        'handler': handler,
        'status': queue_progression.get('status') or queue_plan.get('queue_progression', {}).get('status') or workflow_item.get('workflow_state') or 'pending',
        'priority': queue_plan.get('queue_priority') or queue_progression.get('queue_priority') or 'normal',
        'why': why,
        'why_summary': ' | '.join(why),
        'approval_hook': approval_hook,
        'queue_progression': queue_progression,
        'queue_transition': queue_plan.get('queue_transition') or {},
    }


def _build_workbench_approval_handler_drilldown(item: Dict[str, Any], workflow_item: Dict[str, Any], approval_item: Dict[str, Any],
                                               executor_item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    executor_item = executor_item or {}
    plan = executor_item.get('plan') or {}
    dispatch = executor_item.get('dispatch') or {}
    result = executor_item.get('result') or {}
    blocked_by = item.get('blocked_by') or []
    approval_required = bool(item.get('approval_required'))
    requires_manual = bool(item.get('requires_manual'))
    approval_state = item.get('approval_state') or approval_item.get('approval_state') or 'not_required'
    auto_decision = item.get('auto_approval_decision') or approval_item.get('auto_approval_decision') or workflow_item.get('auto_approval_decision') or 'manual_review'
    route = dispatch.get('dispatch_route') or result.get('dispatch_route') or plan.get('dispatch_route') or 'manual_review_queue'
    why = []
    if approval_required:
        why.append(f'approval_state={approval_state}')
    if requires_manual:
        why.append('requires_manual=true')
    if auto_decision:
        why.append(f'auto_decision={auto_decision}')
    if blocked_by:
        why.append('blocked_by=' + ','.join(blocked_by))
    current_transition = result.get('transition_rule') or dispatch.get('transition_rule') or plan.get('transition_rule') or ('manual_gate_before_dispatch' if approval_required or requires_manual else 'approval_not_required')
    next_transition = result.get('next_transition') or dispatch.get('next_transition') or plan.get('next_transition') or ('await_manual_approval' if approval_required or requires_manual else item.get('next_step'))
    return {
        'approval_state': approval_state,
        'decision_state': item.get('decision_state') or approval_item.get('decision_state') or workflow_item.get('decision_state') or approval_state,
        'auto_approval_decision': auto_decision,
        'route': route,
        'handler': {
            'handler_key': dispatch.get('handler_key') or plan.get('handler_key') or approval_item.get('safe_handler_key') or 'approval::review_gate',
            'executor_class': dispatch.get('executor_class') or plan.get('executor_class') or 'approval_gate',
            'route': route,
            'disposition': dispatch.get('mode') or result.get('disposition') or ('manual_review' if approval_required or requires_manual else 'observe'),
        },
        'why': why,
        'why_summary': ' | '.join(why) if why else 'approval_not_required',
        'current_transition': current_transition,
        'next_transition': next_transition,
        'blocking_points': blocked_by,
        'manual_gate': {
            'approval_required': approval_required,
            'requires_manual': requires_manual,
            'owner_hint': item.get('owner_hint') or approval_item.get('owner_hint') or workflow_item.get('owner_hint'),
        },
    }


def _build_workbench_rollout_handler_drilldown(item: Dict[str, Any], workflow_item: Dict[str, Any], approval_item: Dict[str, Any],
                                              executor_item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    executor_item = executor_item or {}
    plan = executor_item.get('plan') or {}
    dispatch = executor_item.get('dispatch') or {}
    result = executor_item.get('result') or {}
    stage_model = workflow_item.get('stage_model') or approval_item.get('stage_model') or item.get('stage_model') or {}
    current_stage = item.get('current_rollout_stage') or workflow_item.get('current_rollout_stage') or approval_item.get('rollout_stage') or plan.get('rollout_stage') or 'pending'
    target_stage = item.get('target_rollout_stage') or workflow_item.get('target_rollout_stage') or approval_item.get('target_rollout_stage') or plan.get('target_rollout_stage') or current_stage
    route = dispatch.get('dispatch_route') or result.get('dispatch_route') or plan.get('dispatch_route') or 'unknown_route'
    transition_rule = result.get('transition_rule') or dispatch.get('transition_rule') or plan.get('transition_rule') or 'unknown_transition_rule'
    next_transition = result.get('next_transition') or dispatch.get('next_transition') or plan.get('next_transition') or item.get('next_step') or 'observe'
    rollback_hint = result.get('rollback_hint') or dispatch.get('rollback_hint') or plan.get('rollback_hint') or 'restore_previous_stage_from_timeline'
    why = [
        f'stage={current_stage}->{target_stage}',
        f'route={route}',
        f'transition_rule={transition_rule}',
    ]
    if item.get('blocked_by'):
        why.append('blocked_by=' + ','.join(item.get('blocked_by') or []))
    return {
        'current_stage': current_stage,
        'target_stage': target_stage,
        'route': route,
        'handler': {
            'handler_key': dispatch.get('handler_key') or plan.get('handler_key') or approval_item.get('safe_handler_key') or 'rollout::unresolved',
            'executor_class': dispatch.get('executor_class') or plan.get('executor_class') or 'rollout_stage',
            'stage_family': approval_item.get('safe_handler_stage_family') or plan.get('safe_handler', {}).get('stage_family') or 'rollout',
            'disposition': dispatch.get('mode') or result.get('disposition') or plan.get('dispatch_mode') or 'observe',
            'route': route,
        },
        'why': why,
        'why_summary': ' | '.join(why),
        'transition_rule': transition_rule,
        'next_transition': next_transition,
        'blocking_points': item.get('blocked_by') or [],
        'rollback_hint': rollback_hint,
        'stage_model': stage_model,
        'stage_progression': {
            'current_stage': current_stage,
            'target_stage': target_stage,
            'readiness': plan.get('readiness') or workflow_item.get('workflow_state') or approval_item.get('workflow_state') or 'pending',
            'retryable': bool(plan.get('retryable', result.get('retryable', dispatch.get('retryable', True)))),
            'stage_handler': plan.get('stage_handler') or {},
        },
    }


def _normalize_workbench_approval_timeline_event(event: Dict[str, Any], *, item: Dict[str, Any], approval_item: Dict[str, Any], workflow_item: Dict[str, Any]) -> Dict[str, Any]:
    current = event.get('current') or {}
    details = event.get('details') or {}
    state = event.get('state') or current.get('state') or approval_item.get('approval_state') or 'pending'
    workflow_state = event.get('workflow_state') or current.get('workflow_state') or workflow_item.get('workflow_state') or approval_item.get('workflow_state') or 'pending'
    decision = event.get('decision') or current.get('decision') or approval_item.get('decision_state') or state
    event_type = _normalize_event_type(event.get('event_type'), category='approval_db')
    timestamp = _build_event_timestamp(
        value=event.get('created_at') or event.get('updated_at') or current.get('updated_at'),
        source='approval_event_created_at' if (event.get('created_at') or event.get('updated_at')) else 'approval_event_fallback',
        phase='approval_db',
        field='created_at' if event.get('created_at') else ('updated_at' if event.get('updated_at') else 'current.updated_at'),
        fallback_fields=['updated_at', 'current.updated_at'],
    )
    provenance = _build_event_provenance(
        origin='approval_db',
        source='approval_timeline',
        family='approval',
        phase='approval_db',
        producer=event.get('source') or event.get('replay_source') or 'approval_db',
        replay_source=event.get('replay_source') or details.get('replay_source'),
        synthetic=False,
    )
    return _attach_unified_event_metadata({
        'event_key': f"approval_db::{event.get('id') or event_type or state}",
        'event_type': event_type,
        'phase': 'approval_db',
        'status': state,
        'path_type': 'approval_db',
        'summary': event.get('reason') or current.get('reason') or event_type or 'approval timeline event',
        'sequence': event.get('id'),
        'source': 'approval_timeline',
        'detail': {
            'item_id': event.get('item_id') or item.get('approval_id') or item.get('item_id'),
            'approval_type': event.get('approval_type') or approval_item.get('action_type') or item.get('action_type'),
            'target': event.get('target') or approval_item.get('playbook_id') or item.get('item_id'),
            'decision': decision,
            'state': state,
            'workflow_state': workflow_state,
            'reason': event.get('reason') or current.get('reason'),
            'actor': event.get('actor') or current.get('actor'),
            'source': event.get('source') or event.get('replay_source'),
            'details': details,
        },
    }, normalized_event_type=event_type, provenance=provenance, timestamp=timestamp)


def build_workbench_merged_timeline(item: Dict[str, Any], workflow_item: Dict[str, Any], approval_item: Dict[str, Any],
                                    executor_item: Optional[Dict[str, Any]] = None,
                                    approval_timeline: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    executor_timeline = _build_workbench_executor_action_timeline(item, workflow_item, approval_item, executor_item)
    approval_timeline = approval_timeline or []
    approval_events = [
        _normalize_workbench_approval_timeline_event(event, item=item, approval_item=approval_item, workflow_item=workflow_item)
        for event in approval_timeline
    ]
    merged_events = approval_events + list(executor_timeline.get('events') or [])

    def _sort_key(row: Dict[str, Any]):
        ts = str(row.get('timestamp') or '')
        missing_ts_rank = 1 if not ts else 0
        source_rank = 0 if row.get('source') == 'approval_timeline' else 1
        seq = row.get('sequence')
        try:
            seq_value = int(seq)
        except Exception:
            seq_value = 10 ** 9
        return (missing_ts_rank, ts, source_rank, seq_value, str(row.get('event_key') or ''))

    merged_events.sort(key=_sort_key)
    event_types = _dedupe_strings([row.get('event_type') for row in merged_events])
    normalized_event_types = _dedupe_strings([row.get('normalized_event_type') for row in merged_events])
    phases = _dedupe_strings([row.get('phase') for row in merged_events])
    provenance_origins = _dedupe_strings([(row.get('provenance') or {}).get('origin') for row in merged_events])
    provenance_sources = _dedupe_strings([(row.get('provenance') or {}).get('source') for row in merged_events])
    timestamp_sources = _dedupe_strings([(row.get('timestamp_info') or {}).get('source') for row in merged_events])
    timestamp_phases = _dedupe_strings([(row.get('timestamp_info') or {}).get('phase') for row in merged_events])
    timestamp_range = {
        'first': next((row.get('timestamp') for row in merged_events if row.get('timestamp')), None),
        'last': next((row.get('timestamp') for row in reversed(merged_events) if row.get('timestamp')), None),
    }
    executor_summary = executor_timeline.get('summary') or {}
    summary = {
        'item_id': item.get('item_id'),
        'approval_id': item.get('approval_id'),
        'action_type': executor_summary.get('action_type') or item.get('action_type') or workflow_item.get('action_type') or approval_item.get('action_type') or 'unknown',
        'current_status': executor_summary.get('current_status'),
        'workflow_state': executor_summary.get('workflow_state') or workflow_item.get('workflow_state') or approval_item.get('workflow_state'),
        'approval_state': executor_summary.get('approval_state') or approval_item.get('approval_state') or item.get('approval_state'),
        'current_stage': executor_summary.get('current_stage'),
        'target_stage': executor_summary.get('target_stage'),
        'dispatch_route': executor_summary.get('dispatch_route'),
        'event_count': len(merged_events),
        'approval_event_count': len(approval_events),
        'executor_event_count': len(executor_timeline.get('events') or []),
        'event_types': event_types,
        'normalized_event_types': normalized_event_types,
        'phases': phases,
        'provenance_origins': provenance_origins,
        'provenance_sources': provenance_sources,
        'timestamp_range': timestamp_range,
        'timestamp_sources': timestamp_sources,
        'timestamp_phases': timestamp_phases,
        'decision_path': executor_summary.get('decision_path') or {},
        'blocking_points': executor_summary.get('blocking_points') or [],
        'result_summary': executor_summary.get('result_summary') or {},
        'audit_event_types': executor_summary.get('audit_event_types') or [],
    }
    return {
        'schema_version': 'm5_workbench_merged_timeline_v1',
        'locator': dict(executor_timeline.get('locator') or {}),
        'summary': summary,
        'events': merged_events,
        'sources': {
            'approval_timeline': {'event_count': len(approval_events), 'enabled': bool(approval_timeline), 'provenance': _build_event_provenance(origin='approval_db', source='approval_timeline', family='approval', phase='approval_db', producer='approval_db')},
            'executor_timeline': {'event_count': len(executor_timeline.get('events') or []), 'provenance': _build_event_provenance(origin='executor', source='executor_timeline', family='workflow_execution', phase='workflow', producer='workbench_executor_action_timeline', synthetic=True)},
        },
        'raw': {
            'approval_timeline': approval_timeline,
            'executor_timeline': executor_timeline,
        },
    }


def _build_workbench_executor_action_timeline(item: Dict[str, Any], workflow_item: Dict[str, Any], approval_item: Dict[str, Any],
                                             executor_item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    executor_item = executor_item or {}
    plan = executor_item.get('plan') or {}
    dispatch = executor_item.get('dispatch') or {}
    apply = executor_item.get('apply') or {}
    result = executor_item.get('result') or {}
    audit = executor_item.get('audit') or {}
    queue_progression = workflow_item.get('queue_progression') or approval_item.get('queue_progression') or {}
    queue_plan = plan.get('queue_plan') or result.get('queue_plan') or dispatch.get('queue_plan') or {}
    approval_hook = queue_plan.get('approval_hook') or {}
    stage_model = workflow_item.get('stage_model') or approval_item.get('stage_model') or item.get('stage_model') or {}

    route = dispatch.get('dispatch_route') or result.get('dispatch_route') or plan.get('dispatch_route') or queue_progression.get('dispatch_route') or 'unknown_route'
    current_stage = item.get('current_rollout_stage') or workflow_item.get('current_rollout_stage') or approval_item.get('rollout_stage') or plan.get('rollout_stage') or 'pending'
    target_stage = item.get('target_rollout_stage') or workflow_item.get('target_rollout_stage') or approval_item.get('target_rollout_stage') or plan.get('target_rollout_stage') or current_stage
    approval_state = item.get('approval_state') or approval_item.get('approval_state') or 'not_required'
    workflow_state = item.get('workflow_state') or workflow_item.get('workflow_state') or approval_item.get('workflow_state') or 'pending'
    execution_status = executor_item.get('status') or result.get('status') or dispatch.get('status') or apply.get('status') or workflow_state
    blocked_by = _dedupe_strings((item.get('blocked_by') or []) + (approval_item.get('blocked_by') or []) + (workflow_item.get('blocking_reasons') or []))
    audit_event_types = _dedupe_strings([
        audit.get('audit_code'),
        result.get('code'),
        dispatch.get('code'),
        approval_hook.get('status'),
        queue_progression.get('status'),
    ])

    event_timestamp_value = (
        ((item.get('scheduled_review') or workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or {}).get('last_transition_at'))
        or ((item.get('scheduled_review') or workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or {}).get('scheduled_at'))
        or ((item.get('scheduled_review') or workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or {}).get('next_recheck_at'))
    )

    def _executor_event(event_key: str, event_type: str, phase: str, status: str, path_type: str, summary: str, detail: Dict[str, Any], sequence: int) -> Dict[str, Any]:
        return _attach_unified_event_metadata({
            'event_key': event_key,
            'event_type': event_type,
            'phase': phase,
            'status': status,
            'path_type': path_type,
            'summary': summary,
            'source': 'executor_timeline',
            'sequence': sequence,
            'detail': detail,
        }, normalized_event_type=event_type, provenance=_build_event_provenance(
            origin='executor',
            source='executor_timeline',
            family='workflow_execution',
            phase=phase,
            producer='workbench_executor_action_timeline',
            synthetic=True,
        ), timestamp=_build_event_timestamp(
            value=event_timestamp_value,
            source='workflow_scheduled_review' if event_timestamp_value else 'synthetic_timeline_order',
            phase=phase,
            field='scheduled_review.last_transition_at' if ((item.get('scheduled_review') or workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or {}).get('last_transition_at')) else ('scheduled_review.scheduled_at' if ((item.get('scheduled_review') or workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or {}).get('scheduled_at')) else ('scheduled_review.next_recheck_at' if ((item.get('scheduled_review') or workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or {}).get('next_recheck_at')) else None)),
            fallback_fields=['scheduled_review.last_transition_at', 'scheduled_review.scheduled_at', 'scheduled_review.next_recheck_at'],
        ))

    events = [
        _executor_event(
            'action_selected', 'workflow_action_selected', 'workflow', workflow_state, 'decision',
            workflow_item.get('title') or item.get('title') or item.get('item_id'),
            {'action_type': item.get('action_type') or workflow_item.get('action_type') or approval_item.get('action_type'), 'risk_level': item.get('risk_level') or workflow_item.get('risk_level') or approval_item.get('risk_level') or 'unknown', 'queue_progression': queue_progression, 'scheduled_review': item.get('scheduled_review') or workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or {}},
            1,
        ),
        _executor_event(
            'approval_gate', 'approval_gate_evaluated', 'approval', approval_state, 'approval',
            approval_hook.get('gate_reason') or ('manual review required' if item.get('requires_manual') else 'approval state evaluated'),
            {'approval_required': bool(item.get('approval_required')), 'requires_manual': bool(item.get('requires_manual')), 'approval_state': approval_state, 'decision_state': item.get('decision_state') or approval_item.get('decision_state') or workflow_state, 'auto_approval_decision': item.get('auto_approval_decision') or approval_item.get('auto_approval_decision') or workflow_item.get('auto_approval_decision'), 'approval_hook': approval_hook, 'blocked_by': blocked_by},
            2,
        ),
        _executor_event(
            'executor_plan', 'executor_plan_built', 'executor_plan', 'planned' if plan else 'missing', 'execution',
            plan.get('handler_key') or dispatch.get('handler_key') or 'executor plan unresolved',
            {'handler_key': plan.get('handler_key') or dispatch.get('handler_key'), 'executor_class': plan.get('executor_class') or dispatch.get('executor_class'), 'dispatch_mode': plan.get('dispatch_mode') or dispatch.get('mode'), 'dispatch_route': route, 'transition_rule': plan.get('transition_rule') or dispatch.get('transition_rule') or result.get('transition_rule'), 'next_transition': plan.get('next_transition') or dispatch.get('next_transition') or result.get('next_transition'), 'readiness': plan.get('readiness') or workflow_state, 'queue_plan': queue_plan},
            3,
        ),
        _executor_event(
            'executor_dispatch', 'executor_dispatch_decided', 'dispatch', dispatch.get('status') or execution_status, 'dispatch',
            dispatch.get('reason') or result.get('reason') or 'dispatch decision recorded',
            {'route': route, 'dispatch_mode': dispatch.get('mode') or plan.get('dispatch_mode'), 'code': dispatch.get('code'), 'retryable': dispatch.get('retryable', result.get('retryable', True)), 'rollback_hint': dispatch.get('rollback_hint') or result.get('rollback_hint') or plan.get('rollback_hint')},
            4,
        ),
        _executor_event(
            'executor_result', 'executor_result_recorded', 'result', result.get('status') or execution_status, 'result',
            result.get('reason') or 'executor result recorded',
            {'disposition': result.get('disposition') or executor_item.get('status'), 'code': result.get('code'), 'state': result.get('state') or approval_state, 'workflow_state': result.get('workflow_state') or workflow_state, 'transition_rule': result.get('transition_rule') or dispatch.get('transition_rule') or plan.get('transition_rule'), 'next_transition': result.get('next_transition') or dispatch.get('next_transition') or plan.get('next_transition')},
            5,
        ),
    ]

    summary = {
        'item_id': item.get('item_id'), 'approval_id': item.get('approval_id'), 'action_type': item.get('action_type') or workflow_item.get('action_type') or approval_item.get('action_type') or 'unknown', 'current_status': execution_status, 'workflow_state': workflow_state, 'approval_state': approval_state, 'current_stage': current_stage, 'target_stage': target_stage, 'dispatch_route': route,
        'handler_key': plan.get('handler_key') or dispatch.get('handler_key') or (((approval_item.get('safe_handler_key') if isinstance(approval_item, dict) else None)) or 'unresolved'), 'executor_class': plan.get('executor_class') or dispatch.get('executor_class') or 'unknown',
        'decision_path': {'scheduled_review_path': queue_progression.get('next_action') or item.get('next_step') or 'observe', 'approval_path': approval_hook.get('status') or approval_state, 'dispatch_path': dispatch.get('mode') or plan.get('dispatch_mode') or 'observe', 'execution_path': result.get('disposition') or execution_status},
        'key_timestamps': {'scheduled_review_at': ((item.get('scheduled_review') or workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or {}).get('scheduled_at')), 'next_recheck_at': ((item.get('scheduled_review') or workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or {}).get('next_recheck_at')), 'last_transition_at': ((item.get('scheduled_review') or workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or {}).get('last_transition_at'))},
        'audit_event_types': audit_event_types,
        'result_summary': {'reason': result.get('reason') or dispatch.get('reason'), 'code': result.get('code') or dispatch.get('code'), 'retryable': bool(result.get('retryable', dispatch.get('retryable', True))), 'rollback_hint': result.get('rollback_hint') or dispatch.get('rollback_hint') or plan.get('rollback_hint')},
        'blocking_points': blocked_by, 'event_count': len(events),
        'provenance_origins': _dedupe_strings([(row.get('provenance') or {}).get('origin') for row in events]),
        'provenance_sources': _dedupe_strings([(row.get('provenance') or {}).get('source') for row in events]),
        'normalized_event_types': _dedupe_strings([row.get('normalized_event_type') for row in events]),
        'timestamp_sources': _dedupe_strings([(row.get('timestamp_info') or {}).get('source') for row in events]),
        'timestamp_phases': _dedupe_strings([(row.get('timestamp_info') or {}).get('phase') for row in events]),
    }

    return {'schema_version': 'm5_workbench_executor_action_timeline_v1', 'locator': {'item_id': item.get('item_id'), 'approval_id': item.get('approval_id'), 'lane_id': item.get('lane_id'), 'action_type': summary['action_type']}, 'summary': summary, 'events': events, 'stage_model': stage_model, 'raw': {'workflow_item': workflow_item, 'approval_item': approval_item, 'executor_item': executor_item}}


def _build_workbench_governance_drilldown(item: Dict[str, Any], payload: Optional[Dict] = None, approval_timeline: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    payload = payload or {}
    consumer_view = payload.get('consumer_view') or build_workflow_consumer_view(payload)
    workflow_items = ((consumer_view.get('workflow_state') or {}).get('item_states') or [])
    approval_items = ((consumer_view.get('approval_state') or {}).get('items') or [])
    executor_items = ((consumer_view.get('rollout_executor') or {}).get('items') or [])
    workflow_lookup = {row.get('item_id'): row for row in workflow_items if row.get('item_id')}
    approval_lookup = {row.get('playbook_id') or row.get('item_id'): row for row in approval_items if row.get('playbook_id') or row.get('item_id')}
    executor_lookup = {row.get('playbook_id') or row.get('item_id'): row for row in executor_items if row.get('playbook_id') or row.get('item_id')}

    workflow_item = workflow_lookup.get(item.get('item_id')) or (item.get('detail') or {}).get('workflow_item') or {}
    approval_item = approval_lookup.get(item.get('item_id')) or (item.get('detail') or {}).get('approval_item') or {}
    executor_item = executor_lookup.get(item.get('item_id')) or executor_lookup.get(item.get('approval_id')) or {}

    queue = _build_workbench_queue_handler_drilldown(item, workflow_item, approval_item, executor_item)
    approval = _build_workbench_approval_handler_drilldown(item, workflow_item, approval_item, executor_item)
    rollout = _build_workbench_rollout_handler_drilldown(item, workflow_item, approval_item, executor_item)
    timeline = _build_workbench_executor_action_timeline(item, workflow_item, approval_item, executor_item)
    merged_timeline = build_workbench_merged_timeline(item, workflow_item, approval_item, executor_item, approval_timeline=approval_timeline)
    next_transition = rollout.get('next_transition') or approval.get('next_transition') or item.get('next_step')
    blocking_points = _dedupe_strings((item.get('blocked_by') or []) + (approval.get('blocking_points') or []) + (rollout.get('blocking_points') or []))
    rollback_hints = _dedupe_strings([
        rollout.get('rollback_hint'),
        queue.get('queue_transition', {}).get('rollback_hint'),
    ])
    decision_path = {
        'route': queue.get('route') or rollout.get('route') or approval.get('route'),
        'why_summary': item.get('why_summary') or approval.get('why_summary') or rollout.get('why_summary') or queue.get('why_summary'),
        'why': _dedupe_strings((item.get('why') or []) + (queue.get('why') or []) + (approval.get('why') or []) + (rollout.get('why') or [])),
        'current_transition': approval.get('current_transition') or rollout.get('transition_rule'),
        'next_transition': next_transition,
        'blocking_points': blocking_points,
        'rollback_hints': rollback_hints,
    }
    return {
        'schema_version': 'm5_workbench_governance_drilldown_v2',
        'item_locator': {
            'item_id': item.get('item_id'),
            'approval_id': item.get('approval_id'),
            'lane_id': item.get('lane_id'),
            'action_type': item.get('action_type'),
        },
        'queue': queue,
        'approval': approval,
        'rollout': rollout,
        'decision_path': decision_path,
        'timeline': timeline,
        'merged_timeline': merged_timeline,
    }


def build_workbench_governance_detail_view(payload: Optional[Dict] = None, *, item_id: Optional[str] = None,
                                           approval_id: Optional[str] = None, lane_id: Optional[str] = None,
                                           operator_action: Optional[str] = None, operator_route: Optional[str] = None,
                                           follow_up: Optional[str] = None, approval_timeline: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    payload = payload or {}
    catalog = payload.get('workbench_governance_catalog') or _build_workbench_item_catalog(payload)
    candidates = catalog.get('items') or []
    if item_id:
        candidates = [row for row in candidates if row.get('item_id') == item_id]
    if approval_id:
        candidates = [row for row in candidates if row.get('approval_id') == approval_id]
    if lane_id:
        candidates = [row for row in candidates if row.get('lane_id') == lane_id]
    if operator_action:
        candidates = [row for row in candidates if (row.get('operator_action') or '').lower() == str(operator_action).strip().lower()]
    if operator_route:
        candidates = [row for row in candidates if (row.get('operator_route') or '').lower() == str(operator_route).strip().lower()]
    if follow_up:
        candidates = [row for row in candidates if (row.get('operator_follow_up') or '').lower() == str(follow_up).strip().lower()]
    if not candidates and (item_id or approval_id):
        consumer_view = payload.get('consumer_view') or build_workflow_consumer_view(payload)
        workflow_items = ((consumer_view.get('workflow_state') or {}).get('item_states') or [])
        approval_items = ((consumer_view.get('approval_state') or {}).get('items') or [])
        workflow_item = next((row for row in workflow_items if row.get('item_id') == item_id or row.get('item_id') == approval_id), {})
        approval_item = next((row for row in approval_items if row.get('approval_id') == approval_id or row.get('playbook_id') == item_id or row.get('item_id') == item_id), {})
        fallback_item_id = item_id or workflow_item.get('item_id') or approval_item.get('playbook_id') or approval_item.get('item_id')
        fallback_lane = lane_id or ('manual_approval' if bool(approval_item.get('approval_required') or workflow_item.get('approval_required') or workflow_item.get('requires_manual')) else 'ready')
        if fallback_item_id:
            blocked_by = _dedupe_strings((workflow_item.get('blocking_reasons') or []) + (approval_item.get('blocked_by') or []))
            current_stage = workflow_item.get('current_rollout_stage') or approval_item.get('rollout_stage') or 'pending'
            target_stage = workflow_item.get('target_rollout_stage') or approval_item.get('target_rollout_stage') or current_stage
            fallback = {
                'item_id': fallback_item_id,
                'approval_id': approval_item.get('approval_id') or approval_id,
                'title': workflow_item.get('title') or approval_item.get('title') or fallback_item_id,
                'action_type': workflow_item.get('action_type') or approval_item.get('action_type') or 'unknown',
                'workflow_state': workflow_item.get('workflow_state') or approval_item.get('workflow_state') or 'pending',
                'approval_state': approval_item.get('approval_state') or 'not_required',
                'decision_state': approval_item.get('decision_state') or workflow_item.get('decision_state') or workflow_item.get('workflow_state') or 'pending',
                'risk_level': workflow_item.get('risk_level') or approval_item.get('risk_level') or 'unknown',
                'auto_approval_decision': workflow_item.get('auto_approval_decision') or approval_item.get('auto_approval_decision') or 'manual_review',
                'auto_approval_eligible': bool(workflow_item.get('auto_approval_eligible', approval_item.get('auto_approval_eligible'))),
                'requires_manual': bool(workflow_item.get('requires_manual', approval_item.get('requires_manual'))),
                'approval_required': bool(workflow_item.get('approval_required', approval_item.get('approval_required'))),
                'blocked_by': blocked_by,
                'owner_hint': workflow_item.get('owner_hint') or approval_item.get('owner_hint'),
                'queue_progression': workflow_item.get('queue_progression') or approval_item.get('queue_progression') or {},
                'stage_model': workflow_item.get('stage_model') or approval_item.get('stage_model') or {},
                'current_rollout_stage': current_stage,
                'target_rollout_stage': target_stage,
                'scheduled_review': workflow_item.get('scheduled_review') or approval_item.get('scheduled_review') or {},
                'lane_id': fallback_lane,
                'lane_title': fallback_lane.replace('_', ' '),
                'bucket_tags': [fallback_lane],
                'why': ['fallback_detail_lookup'],
                'why_summary': 'fallback_detail_lookup',
                'next_step': (workflow_item.get('queue_progression') or {}).get('next_action') or ('await_manual_approval' if approval_item.get('approval_state') == 'pending' else 'observe'),
                'detail': {
                    'workflow_item': workflow_item,
                    'approval_item': approval_item,
                    'attention_buckets': [fallback_lane],
                },
            }
            candidates = [fallback]
    matched = sorted(candidates, key=_workbench_item_sort_key)
    item = matched[0] if matched else None
    drilldown = _build_workbench_governance_drilldown(item, payload, approval_timeline=approval_timeline) if item else None
    timeline_summary = ((drilldown or {}).get('timeline') or {}).get('summary') or {}
    merged_timeline_summary = ((drilldown or {}).get('merged_timeline') or {}).get('summary') or {}
    operator_action_policy = ((item or {}).get('operator_action_policy') or {})
    if drilldown is not None:
        drilldown['operator_action'] = {
            'policy': operator_action_policy,
            'action': operator_action_policy.get('action'),
            'route': operator_action_policy.get('route'),
            'follow_up': operator_action_policy.get('follow_up'),
            'priority': operator_action_policy.get('priority'),
            'owner': operator_action_policy.get('owner'),
            'reason_codes': operator_action_policy.get('reason_codes') or [],
            'why_summary': (item or {}).get('why_summary'),
            'next_step': (item or {}).get('next_step'),
        }
    summary = {
        'item_id': (item or {}).get('item_id'),
        'approval_id': (item or {}).get('approval_id'),
        'lane_id': (item or {}).get('lane_id'),
        'why_summary': (item or {}).get('why_summary'),
        'next_step': (item or {}).get('next_step'),
        'operator_action_policy': operator_action_policy,
        'operator_action': operator_action_policy.get('action'),
        'operator_route': operator_action_policy.get('route'),
        'follow_up': operator_action_policy.get('follow_up'),
        'operator_priority': operator_action_policy.get('priority'),
        'operator_owner': operator_action_policy.get('owner'),
        'operator_reason_codes': operator_action_policy.get('reason_codes') or [],
        'queue_name': ((drilldown or {}).get('queue') or {}).get('queue_name'),
        'queue_route': ((drilldown or {}).get('queue') or {}).get('route'),
        'approval_route': ((drilldown or {}).get('approval') or {}).get('route'),
        'rollout_route': ((drilldown or {}).get('rollout') or {}).get('route'),
        'handler_key': (((drilldown or {}).get('rollout') or {}).get('handler') or {}).get('handler_key') or (((drilldown or {}).get('queue') or {}).get('handler') or {}).get('handler_key'),
        'next_transition': ((drilldown or {}).get('decision_path') or {}).get('next_transition'),
        'blocking_points': ((drilldown or {}).get('decision_path') or {}).get('blocking_points') or [],
        'rollback_hints': ((drilldown or {}).get('decision_path') or {}).get('rollback_hints') or [],
        'timeline': {
            'current_status': timeline_summary.get('current_status'),
            'workflow_state': timeline_summary.get('workflow_state'),
            'approval_state': timeline_summary.get('approval_state'),
            'current_stage': timeline_summary.get('current_stage'),
            'target_stage': timeline_summary.get('target_stage'),
            'dispatch_route': timeline_summary.get('dispatch_route'),
            'audit_event_types': timeline_summary.get('audit_event_types') or [],
            'result_summary': timeline_summary.get('result_summary') or {},
            'decision_path': timeline_summary.get('decision_path') or {},
            'event_count': timeline_summary.get('event_count', 0),
        },
        'merged_timeline': {
            'event_count': merged_timeline_summary.get('event_count', 0),
            'approval_event_count': merged_timeline_summary.get('approval_event_count', 0),
            'executor_event_count': merged_timeline_summary.get('executor_event_count', 0),
            'event_types': merged_timeline_summary.get('event_types') or [],
            'phases': merged_timeline_summary.get('phases') or [],
            'timestamp_range': merged_timeline_summary.get('timestamp_range') or {},
        },
    }
    return {
        'schema_version': 'm5_workbench_governance_detail_view_v3',
        'found': bool(item),
        'item': item,
        'drilldown': drilldown,
        'alternatives': matched[1:10] if len(matched) > 1 else [],
        'summary': summary,
    }


def build_workbench_timeline_summary_aggregation(payload: Optional[Dict] = None, *, lane_ids: Any = None, action_types: Any = None,
                                                 risk_levels: Any = None, workflow_states: Any = None, approval_states: Any = None,
                                                 current_rollout_stages: Any = None, target_rollout_stages: Any = None, bucket_tags: Any = None,
                                                 auto_approval_decisions: Any = None, operator_actions: Any = None, operator_routes: Any = None,
                                                 operator_follow_ups: Any = None, owner_hints: Any = None, q: Optional[str] = None,
                                                 approval_timeline_fetcher: Optional[Any] = None, approval_timeline_limit: int = 200,
                                                 max_groups: int = 50, max_items_per_group: int = 20) -> Dict[str, Any]:
    payload = payload or {}
    catalog = payload.get('workbench_governance_catalog') or _build_workbench_item_catalog(payload)
    filtered_items = sorted(_filter_workbench_catalog_items(
        catalog.get('items') or [],
        lane_ids=lane_ids,
        action_types=action_types,
        risk_levels=risk_levels,
        workflow_states=workflow_states,
        approval_states=approval_states,
        current_rollout_stages=current_rollout_stages,
        target_rollout_stages=target_rollout_stages,
        bucket_tags=bucket_tags,
        auto_approval_decisions=auto_approval_decisions,
        operator_actions=operator_actions,
        operator_routes=operator_routes,
        operator_follow_ups=operator_follow_ups,
        owner_hints=owner_hints,
        q=q,
    ), key=_workbench_item_sort_key)
    deduped_items: List[Dict[str, Any]] = []
    seen_items = set()
    for row in filtered_items:
        dedupe_key = (row.get('item_id'), row.get('approval_id'))
        if dedupe_key in seen_items:
            continue
        seen_items.add(dedupe_key)
        deduped_items.append(row)

    approval_timeline_cache: Dict[str, List[Dict[str, Any]]] = {}

    def _approval_timeline_for(approval_id: Optional[str]) -> List[Dict[str, Any]]:
        if not approval_id or not approval_timeline_fetcher:
            return []
        if approval_id not in approval_timeline_cache:
            approval_timeline_cache[approval_id] = list(approval_timeline_fetcher(approval_id, approval_timeline_limit) or [])
        return approval_timeline_cache[approval_id]

    def _item_summary(row: Dict[str, Any]) -> Dict[str, Any]:
        detail = build_workbench_governance_detail_view(
            payload,
            item_id=row.get('item_id'),
            approval_id=row.get('approval_id'),
            lane_id=row.get('lane_id'),
            approval_timeline=_approval_timeline_for(row.get('approval_id')),
        )
        timeline = ((detail.get('drilldown') or {}).get('timeline') or {}).get('summary') or {}
        merged_timeline = ((detail.get('drilldown') or {}).get('merged_timeline') or {}).get('summary') or {}
        return {
            'item_id': row.get('item_id'),
            'approval_id': row.get('approval_id'),
            'title': row.get('title'),
            'lane_id': row.get('lane_id'),
            'action_type': row.get('action_type') or 'unknown',
            'workflow_state': row.get('workflow_state') or 'pending',
            'approval_state': row.get('approval_state') or 'not_required',
            'risk_level': row.get('risk_level') or 'unknown',
            'requires_manual': bool(row.get('requires_manual')),
            'approval_required': bool(row.get('approval_required')),
            'auto_approval_decision': row.get('auto_approval_decision') or 'manual_review',
            'current_rollout_stage': row.get('current_rollout_stage') or 'pending',
            'target_rollout_stage': row.get('target_rollout_stage') or row.get('current_rollout_stage') or 'pending',
            'bucket_tags': row.get('bucket_tags') or [],
            'blocked_by': row.get('blocked_by') or [],
            'auto_advance_gate': row.get('auto_advance_gate') or {},
            'rollback_gate': row.get('rollback_gate') or {},
            'why_summary': row.get('why_summary'),
            'next_step': row.get('next_step'),
            'operator_action_policy': row.get('operator_action_policy') or {},
            'operator_action': row.get('operator_action'),
            'operator_route': row.get('operator_route'),
            'operator_follow_up': row.get('operator_follow_up'),
            'execution_status': row.get('execution_status') or ((row.get('state_machine') or {}).get('execution_status') if isinstance(row.get('state_machine'), dict) else None),
            'last_transition': row.get('last_transition') or ((row.get('state_machine') or {}).get('last_transition') if isinstance(row.get('state_machine'), dict) else None) or {},
            'timeline': {
                'current_status': timeline.get('current_status'),
                'workflow_state': timeline.get('workflow_state'),
                'approval_state': timeline.get('approval_state'),
                'current_stage': timeline.get('current_stage'),
                'target_stage': timeline.get('target_stage'),
                'dispatch_route': timeline.get('dispatch_route'),
                'audit_event_types': timeline.get('audit_event_types') or [],
                'decision_path': timeline.get('decision_path') or {},
                'result_summary': timeline.get('result_summary') or {},
                'event_count': timeline.get('event_count', 0),
                'normalized_event_types': timeline.get('normalized_event_types') or [],
                'provenance_origins': timeline.get('provenance_origins') or [],
                'provenance_sources': timeline.get('provenance_sources') or [],
                'timestamp_sources': timeline.get('timestamp_sources') or [],
                'timestamp_phases': timeline.get('timestamp_phases') or [],
            },
            'stage_loop': row.get('stage_loop') or _resolve_stage_loop_snapshot(workflow_item=row, approval_item=row, row=row),
            'merged_timeline': {
                'event_count': merged_timeline.get('event_count', 0),
                'approval_event_count': merged_timeline.get('approval_event_count', 0),
                'executor_event_count': merged_timeline.get('executor_event_count', 0),
                'event_types': merged_timeline.get('event_types') or [],
                'normalized_event_types': merged_timeline.get('normalized_event_types') or [],
                'phases': merged_timeline.get('phases') or [],
                'provenance_origins': merged_timeline.get('provenance_origins') or [],
                'provenance_sources': merged_timeline.get('provenance_sources') or [],
                'timestamp_range': merged_timeline.get('timestamp_range') or {},
                'timestamp_sources': merged_timeline.get('timestamp_sources') or [],
                'timestamp_phases': merged_timeline.get('timestamp_phases') or [],
            },
        }

    item_summaries = [_item_summary(row) for row in deduped_items]

    def _aggregate_group(group_type: str, group_id: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        timeline_routes = _dedupe_strings([row.get('timeline', {}).get('dispatch_route') for row in rows])
        statuses = _dedupe_strings([row.get('timeline', {}).get('current_status') for row in rows])
        workflow_state_set = _dedupe_strings([row.get('workflow_state') for row in rows])
        approval_state_set = _dedupe_strings([row.get('approval_state') for row in rows])
        action_type_set = _dedupe_strings([row.get('action_type') for row in rows])
        risk_level_set = _dedupe_strings([row.get('risk_level') for row in rows])
        current_stage_set = _dedupe_strings([row.get('current_rollout_stage') for row in rows])
        target_stage_set = _dedupe_strings([row.get('target_rollout_stage') for row in rows])
        next_steps = _dedupe_strings([row.get('next_step') for row in rows])
        event_types = _dedupe_strings([event_type for row in rows for event_type in (row.get('merged_timeline', {}).get('event_types') or [])])
        normalized_event_types = _dedupe_strings([event_type for row in rows for event_type in (row.get('merged_timeline', {}).get('normalized_event_types') or [])])
        phases = _dedupe_strings([phase for row in rows for phase in (row.get('merged_timeline', {}).get('phases') or [])])
        provenance_origins = _dedupe_strings([value for row in rows for value in (row.get('merged_timeline', {}).get('provenance_origins') or [])])
        provenance_sources = _dedupe_strings([value for row in rows for value in (row.get('merged_timeline', {}).get('provenance_sources') or [])])
        timestamp_sources = _dedupe_strings([value for row in rows for value in (row.get('merged_timeline', {}).get('timestamp_sources') or [])])
        timestamp_phases = _dedupe_strings([value for row in rows for value in (row.get('merged_timeline', {}).get('timestamp_phases') or [])])
        audit_event_types = _dedupe_strings([event_type for row in rows for event_type in (row.get('timeline', {}).get('audit_event_types') or [])])
        timeline_event_total = sum(int(row.get('timeline', {}).get('event_count') or 0) for row in rows)
        merged_event_total = sum(int(row.get('merged_timeline', {}).get('event_count') or 0) for row in rows)
        approval_event_total = sum(int(row.get('merged_timeline', {}).get('approval_event_count') or 0) for row in rows)
        executor_event_total = sum(int(row.get('merged_timeline', {}).get('executor_event_count') or 0) for row in rows)
        ts_first = [row.get('merged_timeline', {}).get('timestamp_range', {}).get('first') for row in rows if row.get('merged_timeline', {}).get('timestamp_range', {}).get('first')]
        ts_last = [row.get('merged_timeline', {}).get('timestamp_range', {}).get('last') for row in rows if row.get('merged_timeline', {}).get('timestamp_range', {}).get('last')]
        operator_policy_summary = _summarize_operator_action_policies(rows)
        low_intervention_summary = _build_low_intervention_group_summary(rows, label=group_id)
        gate_consumption = _build_gate_consumption_summary(rows, label=group_id, max_items=max_items_per_group)
        return {
            'group_type': group_type,
            'group_id': group_id,
            'item_count': len(rows),
            'headline': low_intervention_summary.get('headline'),
            'low_intervention_summary': low_intervention_summary,
            'items': rows[:max_items_per_group],
            'filters': {
                'lane_ids': _dedupe_strings([row.get('lane_id') for row in rows]),
                'action_types': action_type_set,
                'workflow_states': workflow_state_set,
                'approval_states': approval_state_set,
                'risk_levels': risk_level_set,
                'current_rollout_stages': current_stage_set,
                'target_rollout_stages': target_stage_set,
                'bucket_tags': _dedupe_strings([tag for row in rows for tag in (row.get('bucket_tags') or [])]),
            },
            'timeline_summary': {
                'current_statuses': statuses,
                'dispatch_routes': timeline_routes,
                'next_steps': next_steps,
                'audit_event_types': audit_event_types,
                'event_count_total': timeline_event_total,
                'event_count_avg': round((timeline_event_total / len(rows)), 4) if rows else 0,
            },
            'operator_action_policy_summary': operator_policy_summary,
            'gate_consumption': gate_consumption,
            'stage_loop': _summarize_stage_loop_rows(rows, label=group_id, max_items=max_items_per_group),
            'merged_timeline_summary': {
                'event_count_total': merged_event_total,
                'approval_event_count_total': approval_event_total,
                'executor_event_count_total': executor_event_total,
                'event_types': event_types,
                'normalized_event_types': normalized_event_types,
                'phases': phases,
                'provenance_origins': provenance_origins,
                'provenance_sources': provenance_sources,
                'timestamp_range': {
                    'first': min(ts_first) if ts_first else None,
                    'last': max(ts_last) if ts_last else None,
                },
                'timestamp_sources': timestamp_sources,
                'timestamp_phases': timestamp_phases,
            },
        }

    bucket_map: Dict[str, List[Dict[str, Any]]] = {}
    action_map: Dict[str, List[Dict[str, Any]]] = {}
    lane_map: Dict[str, List[Dict[str, Any]]] = {}
    operator_action_map: Dict[str, List[Dict[str, Any]]] = {}
    operator_route_map: Dict[str, List[Dict[str, Any]]] = {}
    follow_up_map: Dict[str, List[Dict[str, Any]]] = {}
    for row in item_summaries:
        for bucket in row.get('bucket_tags') or [row.get('lane_id') or 'unbucketed']:
            bucket_map.setdefault(bucket, []).append(row)
        action_map.setdefault(row.get('action_type') or 'unknown', []).append(row)
        lane_map.setdefault(row.get('lane_id') or 'unknown', []).append(row)
        operator_action_map.setdefault(row.get('operator_action') or 'observe_only_followup', []).append(row)
        operator_route_map.setdefault(row.get('operator_route') or 'observe_only_followup', []).append(row)
        follow_up_map.setdefault(row.get('operator_follow_up') or 'observe_only', []).append(row)

    bucket_groups = [_aggregate_group('bucket', group_id, rows) for group_id, rows in sorted(bucket_map.items(), key=lambda item: (-len(item[1]), item[0]))[:max_groups]]
    action_groups = [_aggregate_group('action_type', group_id, rows) for group_id, rows in sorted(action_map.items(), key=lambda item: (-len(item[1]), item[0]))[:max_groups]]
    lane_groups = [_aggregate_group('lane', group_id, rows) for group_id, rows in sorted(lane_map.items(), key=lambda item: (-len(item[1]), item[0]))[:max_groups]]
    operator_action_groups = [_aggregate_group('operator_action', group_id, rows) for group_id, rows in sorted(operator_action_map.items(), key=lambda item: (-len(item[1]), item[0]))[:max_groups]]
    operator_route_groups = [_aggregate_group('operator_route', group_id, rows) for group_id, rows in sorted(operator_route_map.items(), key=lambda item: (-len(item[1]), item[0]))[:max_groups]]
    follow_up_groups = [_aggregate_group('follow_up', group_id, rows) for group_id, rows in sorted(follow_up_map.items(), key=lambda item: (-len(item[1]), item[0]))[:max_groups]]
    operator_policy_summary = _summarize_operator_action_policies(item_summaries)
    gate_consumption = _build_gate_consumption_summary(item_summaries, label='timeline_summary_aggregation', max_items=max_items_per_group)

    aggregation = {
        'schema_version': 'm5_workbench_timeline_summary_aggregation_v2',
        'summary': {
            'item_count': len(item_summaries),
            'bucket_group_count': len(bucket_groups),
            'action_group_count': len(action_groups),
            'lane_group_count': len(lane_groups),
            'operator_action_group_count': len(operator_action_groups),
            'operator_route_group_count': len(operator_route_groups),
            'follow_up_group_count': len(follow_up_groups),
            'approval_timeline_item_count': len(approval_timeline_cache),
            'timeline_event_count_total': sum(int(row.get('timeline', {}).get('event_count') or 0) for row in item_summaries),
            'merged_event_count_total': sum(int(row.get('merged_timeline', {}).get('event_count') or 0) for row in item_summaries),
            'approval_event_count_total': sum(int(row.get('merged_timeline', {}).get('approval_event_count') or 0) for row in item_summaries),
            'executor_event_count_total': sum(int(row.get('merged_timeline', {}).get('executor_event_count') or 0) for row in item_summaries),
            'operator_action_policy_summary': operator_policy_summary,
            'gate_consumption': gate_consumption,
            'stage_loop': _summarize_stage_loop_rows(item_summaries, label='timeline_summary_aggregation', max_items=max_items_per_group),
        },
        'applied_filters': {
            'lane_ids': _normalize_filter_values(lane_ids),
            'action_types': _normalize_filter_values(action_types),
            'risk_levels': _normalize_filter_values(risk_levels),
            'workflow_states': _normalize_filter_values(workflow_states),
            'approval_states': _normalize_filter_values(approval_states),
            'current_rollout_stages': _normalize_filter_values(current_rollout_stages),
            'target_rollout_stages': _normalize_filter_values(target_rollout_stages),
            'bucket_tags': _normalize_filter_values(bucket_tags),
            'auto_approval_decisions': _normalize_filter_values(auto_approval_decisions),
            'operator_actions': _normalize_filter_values(operator_actions),
            'operator_routes': _normalize_filter_values(operator_routes),
            'operator_follow_ups': _normalize_filter_values(operator_follow_ups),
            'owner_hints': _normalize_filter_values(owner_hints),
            'q': str(q or '').strip(),
        },
        'groups': {
            'by_bucket': bucket_groups,
            'by_action_type': action_groups,
            'by_lane': lane_groups,
            'by_operator_action': operator_action_groups,
            'by_operator_route': operator_route_groups,
            'by_follow_up': follow_up_groups,
        },
        'items': item_summaries,
    }
    payload['workbench_timeline_summary_aggregation'] = aggregation
    return aggregation


def build_workbench_governance_view(payload: Optional[Dict] = None, *, max_items: int = 5,
                                    max_adjustments: int = 10, filters: Optional[Dict[str, Any]] = None,
                                    transition_journal_overview: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    catalog = payload.get('workbench_governance_catalog') or _build_workbench_item_catalog(payload)
    consumer_view = (catalog.get('upstreams') or {}).get('workflow_consumer_view') or payload.get('consumer_view') or build_workflow_consumer_view(payload)
    attention_view = (catalog.get('upstreams') or {}).get('workflow_attention_view') or payload.get('attention_view') or build_workflow_attention_view(payload, max_items=max_items)
    operator_digest = (catalog.get('upstreams') or {}).get('workflow_operator_digest') or payload.get('operator_digest') or build_workflow_operator_digest(
        payload,
        max_items=max_items,
        transition_journal_overview=transition_journal_overview,
    )
    if transition_journal_overview and not ((operator_digest.get('transition_journal') or {}).get('summary') or {}).get('count'):
        operator_digest = build_workflow_operator_digest(
            payload,
            max_items=max_items,
            transition_journal_overview=transition_journal_overview,
        )
    transition_journal = operator_digest.get('transition_journal') or _build_transition_journal_consumer_view(
        overview=transition_journal_overview or payload.get('transition_journal')
    )
    stage_progression = consumer_view.get('rollout_stage_progression') or {}
    stage_summary = stage_progression.get('summary') or {}
    rollout_executor = consumer_view.get('rollout_executor') or {}
    auto_approval = consumer_view.get('auto_approval_execution') or {}
    controlled_rollout = consumer_view.get('controlled_rollout_execution') or {}
    workflow_items = ((consumer_view.get('workflow_state') or {}).get('item_states') or [])
    approval_items = ((consumer_view.get('approval_state') or {}).get('items') or [])

    filtered_view = build_workbench_governance_filter_view(payload, limit=10000, **(filters or {})) if filters else None
    active_items = (filtered_view or {}).get('items') or catalog.get('items') or []
    unique_active_items = []
    seen_active_item_keys = set()
    for row in active_items:
        key = row.get('item_id') or row.get('approval_id')
        if key in seen_active_item_keys:
            continue
        seen_active_item_keys.add(key)
        unique_active_items.append(row)

    lane_titles = {
        'auto_batch': 'Auto-batch candidates',
        'rollback_candidate': 'Rollback candidates',
        'blocked': 'Blocked follow-up',
        'queued': 'Queued items',
        'ready': 'Ready items',
        'manual_approval': 'Manual approval items',
    }

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
                'bucket_tags': sorted({tag for row in items for tag in (row.get('bucket_tags') or [])}),
            },
            'operator_action_policy_summary': _summarize_operator_action_policies(items),
            'stage_loop': _summarize_stage_loop_rows(items, label=bucket_id, max_items=max_items),
        }

    lanes = {}
    for lane_id, title in lane_titles.items():
        lane_items = sorted([row for row in active_items if row.get('lane_id') == lane_id], key=_workbench_item_sort_key)
        lane_bucket = _bucket(lane_id, title, lane_items)
        lane_bucket['low_intervention_summary'] = _build_low_intervention_group_summary(lane_items, label=lane_id)
        lane_bucket['headline'] = lane_bucket['low_intervention_summary'].get('headline')
        lanes[lane_id] = lane_bucket

    def _build_group_summary_rows(group_type: str, values: List[tuple]) -> List[Dict[str, Any]]:
        summaries = []
        for group_id, items in values:
            sorted_items = sorted(items, key=_workbench_item_sort_key)
            if not sorted_items:
                continue
            summary = _build_low_intervention_group_summary(sorted_items, label=group_id)
            stage_loop = _summarize_stage_loop_rows(sorted_items, label=group_id, max_items=max_items)
            summaries.append({
                'group_type': group_type,
                'group_id': group_id,
                'item_count': len(sorted_items),
                'headline': summary.get('headline'),
                'summary': summary,
                'stage_loop': stage_loop,
                'items': sorted_items[:max_items],
            })
        return summaries

    gate_consumption = _build_gate_consumption_summary(active_items, label='workbench_governance_view', max_items=max_items)
    advisory_consumption = _summarize_rollout_advisories(active_items, label='workbench_governance_view', max_items=max_items)
    auto_promotion_execution = build_auto_promotion_execution_summary(payload, max_items=max_items)
    auto_promotion_review_queues = build_auto_promotion_review_queue_consumption(
        auto_promotion_execution,
        max_items=max_items,
        label='workbench_governance_view',
    )

    group_summaries = {
        'by_lane': _build_group_summary_rows('lane', [(lane_id, [row for row in active_items if row.get('lane_id') == lane_id]) for lane_id in lane_titles.keys()]),
        'by_bucket': _build_group_summary_rows('bucket', sorted(({tag: [row for row in active_items if tag in (row.get('bucket_tags') or [])] for tag in sorted({tag for row in active_items for tag in (row.get('bucket_tags') or [])})}).items(), key=lambda item: (-len(item[1]), item[0]))),
        'by_operator_action': _build_group_summary_rows('operator_action', sorted(({key: [row for row in active_items if (row.get('operator_action') or 'observe_only_followup') == key] for key in sorted({row.get('operator_action') or 'observe_only_followup' for row in active_items})}).items(), key=lambda item: (-len(item[1]), item[0]))),
        'by_operator_route': _build_group_summary_rows('operator_route', sorted(({key: [row for row in active_items if (row.get('operator_route') or 'observe_only_followup') == key] for key in sorted({row.get('operator_route') or 'observe_only_followup' for row in active_items})}).items(), key=lambda item: (-len(item[1]), item[0]))),
        'by_follow_up': _build_group_summary_rows('follow_up', sorted(({key: [row for row in active_items if (row.get('operator_follow_up') or 'observe_only') == key] for key in sorted({row.get('operator_follow_up') or 'observe_only' for row in active_items})}).items(), key=lambda item: (-len(item[1]), item[0]))),
    }

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
            recent_adjustments.append({
                'source': source,
                'item_id': row.get('playbook_id') or row.get('item_id'),
                'approval_id': row.get('item_id'),
                'title': row.get('title'),
                'action_type': row.get('action_type'),
                'status': status,
                'state': row.get('state'),
                'workflow_state': row.get('workflow_state'),
                'current_rollout_stage': row.get('rollout_stage'),
                'target_rollout_stage': row.get('target_rollout_stage'),
                'reason': row.get('reason'),
            })

    _push_adjustment('auto_approval_execution', auto_approval.get('items') or [])
    _push_adjustment('controlled_rollout_execution', controlled_rollout.get('items') or [])
    _push_adjustment('rollout_executor', rollout_executor.get('items') or [], status_field='status')

    gate_consumption = ((operator_digest.get('summary') or {}).get('gate_consumption') or _build_gate_consumption_summary(active_items, label='workbench_governance_view', max_items=max_items))
    gate_consumption = _build_gate_consumption_summary(active_items, label='workbench_governance_view', max_items=max_items)
    view = {
        'schema_version': 'm5_workbench_governance_view_v2',
        'headline': {
            'status': operator_digest.get('headline', {}).get('status') or 'steady',
            'message': operator_digest.get('headline', {}).get('message') or '',
            'rollout_executor_status': rollout_executor.get('status') or 'disabled',
            'auto_advance_allowed_count': gate_consumption.get('auto_advance_allowed_count', 0),
            'rollback_candidate_count': gate_consumption.get('rollback_candidate_count', 0),
        },
        'summary': {
            'workflow_item_count': len(workflow_items),
            'approval_item_count': len(approval_items),
            'catalog_item_count': len(catalog.get('items') or []),
            'filtered_item_count': len(active_items),
            'auto_batch_count': lanes['auto_batch']['count'],
            'rollback_candidate_count': lanes['rollback_candidate']['count'],
            'manual_approval_count': lanes['manual_approval']['count'],
            'blocked_count': lanes['blocked']['count'],
            'queued_count': lanes['queued']['count'],
            'ready_count': lanes['ready']['count'],
            'recent_adjustment_count': len(recent_adjustments),
            'rollout_executor_status': rollout_executor.get('status') or 'disabled',
            'stage_progression': stage_summary,
            'group_summaries': {key: len(value) for key, value in group_summaries.items()},
            'gate_consumption': gate_consumption,
            'rollout_advisory': advisory_consumption,
            'auto_promotion_execution': auto_promotion_execution.get('summary') or {},
            'auto_promotion_review_queues': auto_promotion_review_queues.get('summary') or {},
            'stage_loop': _summarize_stage_loop_rows(unique_active_items, label='workbench_governance_view', max_items=max_items),
            'transition_count': (transition_journal.get('summary') or {}).get('count', 0),
            'latest_transition_at': (transition_journal.get('summary') or {}).get('latest_timestamp'),
        },
        'filters': catalog.get('filters') or {},
        'applied_filters': (filtered_view or {}).get('applied_filters') or {},
        'lanes': lanes,
        'group_summaries': group_summaries,
        'rollout': {
            'summary': stage_summary,
            'frontier': stage_frontier,
            'items': (stage_progression.get('items') or [])[:max_items],
            'stage_loop': _summarize_stage_loop_rows((stage_progression.get('items') or []), label='rollout_stage_progression', max_items=max_items),
            'rollout_advisory': advisory_consumption,
            'auto_promotion_candidate_queue': advisory_consumption.get('auto_promotion_candidates') or [],
            'auto_promotion_execution': auto_promotion_execution,
            'auto_promotion_review_queues': auto_promotion_review_queues,
            'follow_up_review_queue': auto_promotion_review_queues.get('items') or [],
        },
        'recent_adjustments': recent_adjustments[:max_adjustments],
        'transition_journal': transition_journal,
        'upstreams': {
            'workflow_consumer_view': consumer_view,
            'workflow_attention_view': attention_view,
            'workflow_operator_digest': operator_digest,
        },
    }
    payload['workbench_governance_view'] = view
    return view



def build_unified_workbench_overview(payload: Optional[Dict] = None, *, max_items: int = 5,
                                     max_adjustments: int = 10, filters: Optional[Dict[str, Any]] = None,
                                     approval_timeline_fetcher: Optional[Any] = None,
                                     approval_timeline_limit: int = 200,
                                     transition_journal_overview: Optional[Dict[str, Any]] = None,
                                     lane_ids: Any = None, action_types: Any = None,
                                     risk_levels: Any = None, workflow_states: Any = None,
                                     approval_states: Any = None, current_rollout_stages: Any = None,
                                     target_rollout_stages: Any = None, bucket_tags: Any = None,
                                     auto_approval_decisions: Any = None, operator_actions: Any = None,
                                     operator_routes: Any = None, operator_follow_ups: Any = None,
                                     owner_hints: Any = None, q: Optional[str] = None) -> Dict[str, Any]:
    payload = payload or {}
    merged_filters = dict(filters or {})
    compatibility_filters = {
        'lane_ids': lane_ids,
        'action_types': action_types,
        'risk_levels': risk_levels,
        'workflow_states': workflow_states,
        'approval_states': approval_states,
        'current_rollout_stages': current_rollout_stages,
        'target_rollout_stages': target_rollout_stages,
        'bucket_tags': bucket_tags,
        'auto_approval_decisions': auto_approval_decisions,
        'operator_actions': operator_actions,
        'operator_routes': operator_routes,
        'operator_follow_ups': operator_follow_ups,
        'owner_hints': owner_hints,
        'q': q,
    }
    for key, value in compatibility_filters.items():
        if value is not None and key not in merged_filters:
            merged_filters[key] = value
    consumer_view = payload.get('consumer_view') or build_workflow_consumer_view(payload)
    recovery_view = payload.get('workflow_recovery_view') or build_workflow_recovery_view(payload, max_items=max_items)
    operator_digest = payload.get('workflow_operator_digest') or payload.get('operator_digest') or build_workflow_operator_digest(
        payload,
        max_items=max_items,
        transition_journal_overview=transition_journal_overview,
    )
    workbench_view = payload.get('workbench_governance_view') or build_workbench_governance_view(
        payload,
        max_items=max_items,
        max_adjustments=max_adjustments,
        filters=merged_filters,
        transition_journal_overview=transition_journal_overview,
    )
    transition_journal = workbench_view.get('transition_journal') or operator_digest.get('transition_journal') or _build_transition_journal_consumer_view(
        overview=transition_journal_overview or payload.get('transition_journal')
    )
    timeline_summary = payload.get('workbench_timeline_summary_aggregation') or build_workbench_timeline_summary_aggregation(
        payload,
        approval_timeline_fetcher=approval_timeline_fetcher,
        approval_timeline_limit=approval_timeline_limit,
        max_items_per_group=max_items,
        **merged_filters,
    )

    workflow_summary = (consumer_view.get('workflow_state') or {}).get('summary') or {}
    approval_summary = (consumer_view.get('approval_state') or {}).get('summary') or {}
    recovery_summary = recovery_view.get('summary') or {}
    workbench_summary = workbench_view.get('summary') or {}
    timeline_groups = (timeline_summary.get('groups') or {}) if isinstance(timeline_summary, dict) else {}
    timeline_summary_meta = timeline_summary.get('summary') or {} if isinstance(timeline_summary, dict) else {}

    def _take(rows: Any, limit: int = max_items) -> List[Dict[str, Any]]:
        return list(rows or [])[:limit]

    def _state_rank(state: str) -> int:
        order = {
            'attention_required': 0,
            'recovery_required': 1,
            'blocked': 2,
            'active': 3,
            'ready_to_consume': 4,
            'steady': 5,
        }
        return order.get(str(state or '').strip().lower(), 99)

    approval_manual_items = _take(((operator_digest.get('attention') or {}).get('manual_approval') or []))
    approval_blocked_items = _take(((operator_digest.get('attention') or {}).get('blocked') or []))
    ready_items = _take(((operator_digest.get('attention') or {}).get('ready') or []))
    queued_items = _take(((operator_digest.get('attention') or {}).get('queued') or []))
    recent_adjustments = _take(workbench_view.get('recent_adjustments') or [])
    gate_consumption = ((workbench_summary.get('gate_consumption') or (operator_digest.get('summary') or {}).get('gate_consumption')) or {})
    rollout_advisory = (workbench_summary.get('rollout_advisory') or (operator_digest.get('summary') or {}).get('rollout_advisory') or _summarize_rollout_advisories(workbench_view.get('rollout', {}).get('items') or [], label='unified_workbench_overview', max_items=max_items))
    auto_promotion_execution = (workbench_view.get('rollout') or {}).get('auto_promotion_execution') or (operator_digest.get('auto_promotion_execution') or {}) or build_auto_promotion_execution_summary(payload, max_items=max_items)
    auto_promotion_review_queues = (workbench_view.get('rollout') or {}).get('auto_promotion_review_queues') or build_auto_promotion_review_queue_consumption(
        auto_promotion_execution,
        max_items=max_items,
        label='unified_workbench_overview',
    )
    validation_gate = consumer_view.get('validation_gate') or (operator_digest.get('summary') or {}).get('validation_gate') or _build_validation_gate_snapshot(payload)
    validation_consumption = _collect_validation_gate_consumption((consumer_view.get('workflow_state') or {}).get('item_states') or [])
    control_plane_manifest = build_rollout_control_plane_manifest(payload)
    workbench_stage_loop = (workbench_summary.get('stage_loop') or workbench_view.get('rollout', {}).get('stage_loop') or _summarize_stage_loop_rows(workbench_view.get('upstreams', {}).get('workflow_operator_digest', {}).get('operator_action_policies') or [], label='unified_workbench_rollout', max_items=max_items))
    retry_queue = _take(((recovery_view.get('queues') or {}).get('retry_queue') or []))
    rollback_candidates = _take(((recovery_view.get('queues') or {}).get('rollback_candidates') or []))
    manual_recovery = _take(((recovery_view.get('queues') or {}).get('manual_recovery') or []))

    approval_state = 'attention_required' if approval_summary.get('pending_count', 0) or workbench_summary.get('manual_approval_count', 0) else 'steady'
    rollout_state = 'blocked' if (workbench_summary.get('blocked_count', 0) or (validation_gate.get('enabled') and not validation_gate.get('ready'))) else ('active' if workbench_summary.get('queued_count', 0) or workbench_summary.get('ready_count', 0) or workbench_summary.get('recent_adjustment_count', 0) else 'steady')
    recovery_state = 'recovery_required' if any(recovery_summary.get(key, 0) for key in ('retry_queue_count', 'rollback_candidate_count', 'manual_recovery_count')) else 'steady'

    line_states = {'approval': approval_state, 'rollout': rollout_state, 'recovery': recovery_state}
    dominant_line = sorted(line_states.items(), key=lambda item: (_state_rank(item[1]), item[0]))[0][0] if line_states else 'approval'
    overall_state = line_states.get(dominant_line, 'steady')

    approval_next_actions = [
        row for row in (operator_digest.get('next_actions') or [])
        if row.get('kind') in {'review_schedule', 'escalate'} or row.get('route') == 'manual_approval_queue' or row.get('follow_up') == 'await_manual_approval'
    ]
    rollout_next_actions = [
        row for row in (operator_digest.get('next_actions') or [])
        if row.get('kind') in {'retry', 'freeze_followup', 'observe_only_followup'} or row.get('route') in {'rollout_readiness_queue', 'safe_state_apply', 'review_metadata_apply'}
    ]
    rollout_next_actions.extend(auto_promotion_review_queues.get('next_actions') or [])
    recovery_next_actions = []
    if manual_recovery:
        recovery_next_actions.append({
            'kind': 'manual_recovery',
            'priority': 'high',
            'count': len((recovery_view.get('queues') or {}).get('manual_recovery') or []),
            'message': f"{len((recovery_view.get('queues') or {}).get('manual_recovery') or [])} item(s) require manual recovery review",
            'route': 'manual_recovery_queue',
            'follow_up': 'operator_review_and_safe_requeue',
            'items': manual_recovery,
        })
    if rollback_candidates:
        recovery_next_actions.append({
            'kind': 'rollback_candidate_review',
            'priority': 'high',
            'count': len((recovery_view.get('queues') or {}).get('rollback_candidates') or []),
            'message': f"{len((recovery_view.get('queues') or {}).get('rollback_candidates') or [])} item(s) are rollback candidates",
            'route': 'rollback_candidate_queue',
            'follow_up': 'freeze_and_review',
            'items': rollback_candidates,
        })
    if retry_queue:
        recovery_next_actions.append({
            'kind': 'retry_recovery',
            'priority': 'medium',
            'count': len((recovery_view.get('queues') or {}).get('retry_queue') or []),
            'message': f"{len((recovery_view.get('queues') or {}).get('retry_queue') or [])} item(s) can be retried through recovery queue",
            'route': 'retry_queue',
            'follow_up': 'retry_execution',
            'items': retry_queue,
        })

    lines = {
        'approval': {
            'current_state': approval_state,
            'headline': {
                'status': approval_state,
                'message': f"{approval_summary.get('pending_count', 0)} pending approval / {approval_summary.get('approved_count', 0)} approved / {approval_summary.get('rejected_count', 0)} rejected / {approval_summary.get('deferred_count', 0)} deferred",
            },
            'counts': {
                'total': workbench_summary.get('manual_approval_count', 0) + max(0, approval_summary.get('approved_count', 0)) + max(0, approval_summary.get('rejected_count', 0)) + max(0, approval_summary.get('deferred_count', 0)),
                'pending': approval_summary.get('pending_count', 0),
                'approved': approval_summary.get('approved_count', 0),
                'rejected': approval_summary.get('rejected_count', 0),
                'deferred': approval_summary.get('deferred_count', 0),
                'manual_approval': workbench_summary.get('manual_approval_count', 0),
                'blocked_follow_up': workbench_summary.get('blocked_count', 0),
            },
            'key_alerts': approval_manual_items + [row for row in approval_blocked_items if row.get('item_id') not in {item.get('item_id') for item in approval_manual_items}][:max(0, max_items - len(approval_manual_items))],
            'next_actions': _take(approval_next_actions),
        },
        'rollout': {
            'current_state': rollout_state,
            'headline': {
                'status': rollout_state,
                'message': f"{workbench_summary.get('blocked_count', 0)} blocked / {workbench_summary.get('queued_count', 0)} queued / {workbench_summary.get('ready_count', 0)} ready / {workbench_summary.get('auto_batch_count', 0)} auto-batch / {gate_consumption.get('auto_advance_allowed_count', 0)} auto-advance / review={((auto_promotion_review_queues.get('summary') or {}).get('rollback_review_queue_count', 0))} rollback,{((auto_promotion_review_queues.get('summary') or {}).get('post_promotion_review_queue_count', 0))} post / validation={validation_gate.get('status')}",
            },
            'counts': {
                'total': workflow_summary.get('item_count', workbench_summary.get('filtered_item_count', 0)),
                'blocked': workbench_summary.get('blocked_count', 0),
                'queued': workbench_summary.get('queued_count', 0),
                'ready': workbench_summary.get('ready_count', 0),
                'auto_batch': workbench_summary.get('auto_batch_count', 0),
                'auto_advance_allowed': gate_consumption.get('auto_advance_allowed_count', 0),
                'recent_adjustments': workbench_summary.get('recent_adjustment_count', 0),
                'stage_paths': len((workbench_view.get('rollout') or {}).get('frontier') or []),
                'stage_loop': workbench_stage_loop.get('path_counts') or {},
                'validation_freeze_items': validation_consumption.get('item_count', 0),
                'validation_gap_count': validation_gate.get('gap_count', 0),
                'validation_regression': 1 if validation_gate.get('regression_detected') else 0,
                'ready_for_live_promotion': rollout_advisory.get('ready_for_live_promotion_count', 0),
                'auto_promotion_candidates': rollout_advisory.get('auto_promotion_candidate_count', 0),
                'auto_promotion_executed': (auto_promotion_execution.get('summary') or {}).get('executed_count', 0),
                'auto_promotion_rollback_review_candidates': (auto_promotion_execution.get('summary') or {}).get('rollback_review_candidate_count', 0),
                'post_promotion_review_queue': (auto_promotion_review_queues.get('summary') or {}).get('post_promotion_review_queue_count', 0),
                'rollback_review_queue': (auto_promotion_review_queues.get('summary') or {}).get('rollback_review_queue_count', 0),
                'promotion_review_due': (auto_promotion_review_queues.get('summary') or {}).get('review_due_count', 0),
            },
            'validation_gate': validation_gate,
            'rollout_advisory': rollout_advisory,
            'validation_consumption': validation_consumption,
            'headline_stage_frontier': _take((workbench_view.get('rollout') or {}).get('frontier') or []),
            'key_alerts': _take((auto_promotion_review_queues.get('items') or []) + approval_blocked_items + queued_items + ready_items),
            'next_actions': _take(rollout_next_actions),
            'stage_loop': workbench_stage_loop,
            'auto_promotion_candidate_queue': rollout_advisory.get('auto_promotion_candidates') or [],
            'auto_promotion_execution': auto_promotion_execution,
            'auto_promotion_review_queues': auto_promotion_review_queues,
            'follow_up_review_queue': auto_promotion_review_queues.get('items') or [],
        },
        'recovery': {
            'current_state': recovery_state,
            'headline': {
                'status': recovery_state,
                'message': f"{recovery_summary.get('manual_recovery_count', 0)} manual recovery / {recovery_summary.get('rollback_candidate_count', 0)} rollback candidate / {recovery_summary.get('retry_queue_count', 0)} retry queue / {gate_consumption.get('rollback_candidate_count', 0)} gate candidate",
            },
            'counts': {
                'retry_queue': recovery_summary.get('retry_queue_count', 0),
                'rollback_candidates': recovery_summary.get('rollback_candidate_count', 0),
                'manual_recovery': recovery_summary.get('manual_recovery_count', 0),
                'gate_rollback_candidates': gate_consumption.get('rollback_candidate_count', 0),
            },
            'key_alerts': _take(manual_recovery + rollback_candidates + retry_queue),
            'next_actions': _take(recovery_next_actions),
            'next_retry_at': recovery_summary.get('next_retry_at'),
        },
    }

    overview = {
        'schema_version': 'm5_unified_workbench_overview_v1',
        'headline': {
            'status': overall_state,
            'dominant_line': dominant_line,
            'message': f"approval={approval_state} / rollout={rollout_state} / recovery={recovery_state}",
        },
        'summary': {
            'workflow_item_count': workbench_summary.get('workflow_item_count', workflow_summary.get('item_count', 0)),
            'approval_item_count': workbench_summary.get('approval_item_count', approval_summary.get('item_count', 0)),
            'filtered_item_count': workbench_summary.get('filtered_item_count', workbench_summary.get('catalog_item_count', 0)),
            'line_states': line_states,
            'approval': lines['approval']['counts'],
            'rollout': lines['rollout']['counts'],
            'recovery': lines['recovery']['counts'],
            'stage_loop': {
                'approval': _summarize_stage_loop_rows(approval_manual_items + approval_blocked_items, label='approval_line', max_items=max_items),
                'rollout': workbench_stage_loop,
                'recovery': _summarize_stage_loop_rows(manual_recovery + rollback_candidates + retry_queue, label='recovery_line', max_items=max_items),
            },
            'operator_action_policy_summary': (operator_digest.get('summary') or {}).get('group_summaries') or {},
            'gate_consumption': gate_consumption,
            'rollout_advisory': rollout_advisory,
            'auto_promotion_execution': auto_promotion_execution.get('summary') or {},
            'auto_promotion_review_queues': auto_promotion_review_queues.get('summary') or {},
            'validation_gate': validation_gate,
            'validation_gate_consumption': validation_consumption,
            'control_plane_manifest': {
                'status': (control_plane_manifest.get('compatibility') or {}).get('status'),
                'compatible': (control_plane_manifest.get('compatibility') or {}).get('compatible'),
                'action_count': ((control_plane_manifest.get('registries') or {}).get('action_count')),
                'stage_handler_count': ((control_plane_manifest.get('registries') or {}).get('stage_handler_count')),
                'blocking_issues': (control_plane_manifest.get('compatibility') or {}).get('blocking_issues') or [],
            },
            'timeline_group_counts': {
                'bucket': len(timeline_groups.get('by_bucket') or []),
                'action_type': len(timeline_groups.get('by_action_type') or []),
                'lane': len(timeline_groups.get('by_lane') or []),
                'operator_action': len(timeline_groups.get('by_operator_action') or []),
                'operator_route': len(timeline_groups.get('by_operator_route') or []),
                'follow_up': len(timeline_groups.get('by_follow_up') or []),
            },
            'timeline_event_count_total': timeline_summary_meta.get('timeline_event_count_total', 0),
            'merged_event_count_total': timeline_summary_meta.get('merged_event_count_total', 0),
            'transition_count': (transition_journal.get('summary') or {}).get('count', 0),
            'latest_transition_at': (transition_journal.get('summary') or {}).get('latest_timestamp'),
        },
        'lines': lines,
        'top_key_alerts': _take(lines['approval']['key_alerts'] + [row for row in lines['recovery']['key_alerts'] if row.get('item_id') not in {item.get('item_id') for item in lines['approval']['key_alerts']}] + [row for row in lines['rollout']['key_alerts'] if row.get('item_id') not in {item.get('item_id') for item in lines['approval']['key_alerts'] + lines['recovery']['key_alerts']}]),
        'top_next_actions': _take(lines['approval']['next_actions'] + lines['recovery']['next_actions'] + lines['rollout']['next_actions']),
        'transition_journal': transition_journal,
        'control_plane_manifest': control_plane_manifest,
        'upstreams': {
            'workflow_consumer_view': consumer_view,
            'workflow_operator_digest': operator_digest,
            'workflow_recovery_view': recovery_view,
            'workbench_governance_view': workbench_view,
            'workbench_timeline_summary_aggregation': timeline_summary,
        },
    }
    payload['unified_workbench_overview'] = overview
    return overview

def build_transition_journal_overview(*, transition_rows: Optional[List[Dict]] = None, summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    transition_rows = transition_rows or []
    changed_field_counts: Dict[str, int] = {}
    trigger_counts: Dict[str, int] = {}
    actor_counts: Dict[str, int] = {}
    source_counts: Dict[str, int] = {}
    for row in transition_rows:
        for field in row.get('changed_fields') or []:
            changed_field_counts[field] = changed_field_counts.get(field, 0) + 1
        trigger = str(row.get('trigger') or row.get('event_type') or 'unknown').strip() or 'unknown'
        actor = str(row.get('actor') or 'unknown').strip() or 'unknown'
        source = str(row.get('source') or 'unknown').strip() or 'unknown'
        trigger_counts[trigger] = trigger_counts.get(trigger, 0) + 1
        actor_counts[actor] = actor_counts.get(actor, 0) + 1
        source_counts[source] = source_counts.get(source, 0) + 1
    return {
        'schema_version': 'm5_transition_journal_overview_v1',
        'summary': summary or {
            'count': len(transition_rows),
            'changed_field_counts': changed_field_counts,
        },
        'recent_transitions': transition_rows,
        'breakdown': {
            'changed_field_counts': changed_field_counts,
            'trigger_counts': trigger_counts,
            'actor_counts': actor_counts,
            'source_counts': source_counts,
        },
    }

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
