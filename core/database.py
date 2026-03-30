"""
数据库模块 - SQLite实现
"""
import sqlite3
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path
import pandas as pd

from core.reason_codes import build_reason_code_details, merge_reason_codes, normalize_reason_code
from core.regime_policy import normalize_observe_only_view, summarize_observe_only_collection


class Database:
    """数据库管理类"""
    
    def __init__(self, db_path: str = "data/trading.db"):
        self.db_path = db_path
        self._ensure_dir()
        self._init_db()
    
    def _ensure_dir(self):
        """确保目录存在"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
    
    def _get_connection(self):
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        try:
            if value in (None, ''):
                return default
            return float(value)
        except Exception:
            return default

    def _normalize_contract_fields(self, row: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(row or {})
        quantity = self._safe_float(data.get('quantity'))
        contract_size = self._safe_float(data.get('contract_size'), 1.0) or 1.0
        stored_coin = data.get('coin_quantity')
        stored_coin = None if stored_coin in (None, '') or pd.isna(stored_coin) else self._safe_float(stored_coin)
        expected_coin = quantity * contract_size if quantity > 0 and contract_size > 0 else stored_coin or 0.0
        data['quantity'] = quantity
        data['contract_size'] = contract_size
        data['coin_quantity'] = expected_coin if expected_coin > 0 else (stored_coin or 0.0)
        return data

    def _recalculate_trade_metrics(self, row: Dict[str, Any]) -> Dict[str, Any]:
        data = self._normalize_contract_fields(row)
        entry_price = self._safe_float(data.get('entry_price'))
        exit_price = self._safe_float(data.get('exit_price'))
        leverage = max(1, int(self._safe_float(data.get('leverage'), 1)))
        coin_quantity = self._safe_float(data.get('coin_quantity'))
        pnl = data.get('pnl')
        pnl = None if pnl in (None, '') or pd.isna(pnl) else self._safe_float(pnl)
        if pnl is None and entry_price > 0 and exit_price > 0 and coin_quantity > 0:
            direction = 1 if str(data.get('side') or '').lower() == 'long' else -1
            pnl = (exit_price - entry_price) * coin_quantity * direction
            data['pnl'] = pnl
        margin = (entry_price * coin_quantity) / leverage if entry_price > 0 and coin_quantity > 0 and leverage > 0 else 0.0
        data['margin'] = margin
        data['notional_value'] = coin_quantity * (exit_price or entry_price or 0.0)
        data['pnl_percent'] = (pnl / margin * 100) if pnl is not None and margin > 0 else None
        data['return_pct'] = data.get('pnl_percent')
        plan_context = self._safe_json_dict(data.get('plan_context'))
        outcome = self._safe_json_dict(data.get('outcome_attribution'))
        data['plan_context'] = plan_context
        data['outcome_attribution'] = outcome
        regime_snapshot = self._safe_json_dict(plan_context.get('regime_snapshot'))
        policy_snapshot = self._safe_json_dict(plan_context.get('adaptive_policy_snapshot'))
        observability = self._safe_json_dict(plan_context.get('observability'))
        strategy_tags = plan_context.get('strategy_tags') or outcome.get('strategy_tags') or []
        if isinstance(strategy_tags, str):
            strategy_tags = [strategy_tags]
        observe_only = normalize_observe_only_view(
            outcome.get('observe_only') or observability.get('observe_only') or {},
            regime_snapshot=regime_snapshot,
            policy_snapshot=policy_snapshot,
            fallback_summary=(outcome.get('observe_only') or {}).get('summary') or (observability.get('observe_only') or {}).get('summary'),
        )
        final_execution_permit = self._normalize_final_execution_permit(plan_context.get('final_execution_permit'))
        data['final_execution_permit'] = final_execution_permit
        data['final_execution_reason_code'] = final_execution_permit.get('reason_code')
        data['final_execution_allowed'] = bool(final_execution_permit.get('allowed', False)) if final_execution_permit else None
        data['final_execution_guardrail_evidence'] = self._safe_json_dict(final_execution_permit.get('guardrail_evidence')) if final_execution_permit else {}
        data.update({
            'regime_tag': outcome.get('regime_tag') or regime_snapshot.get('name') or regime_snapshot.get('regime') or policy_snapshot.get('regime_name'),
            'policy_tag': outcome.get('policy_tag') or policy_snapshot.get('policy_version'),
            'strategy_tags': strategy_tags,
            'dominant_strategy': outcome.get('dominant_strategy') or (strategy_tags[0] if strategy_tags else 'unknown'),
            'strategy_count': outcome.get('strategy_count') or len(strategy_tags),
            'observe_only': observe_only,
            'close_reason': outcome.get('close_reason'),
            'close_decision': outcome.get('close_decision'),
            'close_reason_category': outcome.get('close_reason_category'),
            'holding_minutes': outcome.get('holding_minutes'),
            'holding_seconds': outcome.get('holding_seconds'),
            'pnl_bucket': outcome.get('pnl_bucket'),
            'outcome_quality': outcome.get('outcome_quality'),
            'plan_layer_no': outcome.get('plan_layer_no') or plan_context.get('layer_no'),
            'root_signal_id': outcome.get('root_signal_id') or plan_context.get('root_signal_id') or data.get('root_signal_id'),
        })
        return data

    def _normalize_workflow_state(self, workflow_state: Optional[str], approval_state: Optional[str] = None) -> str:
        normalized = str(workflow_state or '').strip().lower()
        allowed = {'pending', 'ready', 'queued', 'blocked', 'blocked_by_approval', 'review_pending', 'executing', 'execution_failed', 'retry_pending', 'rollback_pending', 'rolled_back', 'approved', 'rejected', 'deferred', 'expired'}
        if normalized in allowed:
            return normalized
        approval_value = str(approval_state or '').strip().lower()
        if approval_value in {'approved', 'rejected', 'deferred', 'expired'}:
            return approval_value
        return 'pending'

    def _normalize_action_execution_status(self, execution_status: Optional[str], workflow_state: Optional[str] = None, queue_status: Optional[str] = None, executor_status: Optional[str] = None) -> Optional[str]:
        normalized = str(execution_status or '').strip().lower()
        allowed = {'queued', 'dispatching', 'applied', 'skipped', 'blocked', 'deferred', 'error', 'recovered', 'disabled', 'dry_run', 'planned'}
        if normalized in allowed:
            return normalized
        executor_value = str(executor_status or '').strip().lower()
        if executor_value in allowed:
            return executor_value
        queue_value = str(queue_status or '').strip().lower()
        queue_map = {'ready_to_queue': 'queued', 'queued': 'queued', 'blocked_by_approval': 'blocked', 'awaiting_approval': 'blocked', 'deferred': 'deferred'}
        if queue_value in queue_map:
            return queue_map[queue_value]
        workflow_value = str(workflow_state or '').strip().lower()
        workflow_map = {'queued': 'queued', 'executing': 'dispatching', 'ready': 'applied', 'blocked': 'blocked', 'blocked_by_approval': 'blocked', 'deferred': 'deferred', 'execution_failed': 'error', 'rolled_back': 'recovered'}
        return workflow_map.get(workflow_value)

    def _build_state_machine_details(self, *, item_id: Optional[str], decision: Optional[str], state: Optional[str], workflow_state: Optional[str], details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = dict(details or {})
        queue_progression = self._safe_json_dict(payload.get('queue_progression'))
        queue_transition = self._safe_json_dict(payload.get('queue_transition'))
        stage_transition = self._safe_json_dict(payload.get('stage_transition'))
        last_transition = self._safe_json_dict(payload.get('last_transition'))
        dispatch_route = payload.get('dispatch_route') or queue_progression.get('dispatch_route') or queue_transition.get('dispatch_route')
        next_transition = payload.get('next_transition') or queue_progression.get('next_transition') or queue_transition.get('next_transition')
        transition_rule = payload.get('transition_rule') or last_transition.get('rule') or last_transition.get('transition_rule')
        rollout_stage = payload.get('rollout_stage') or payload.get('current_rollout_stage') or stage_transition.get('to') or stage_transition.get('from')
        target_rollout_stage = payload.get('target_rollout_stage') or stage_transition.get('to') or rollout_stage
        blocked_by = payload.get('blocked_by') or payload.get('blocking_reasons') or []
        if not isinstance(blocked_by, list):
            blocked_by = [str(blocked_by)] if blocked_by else []
        normalized_state = self._normalize_approval_state(state, decision)
        normalized_workflow = self._normalize_workflow_state(workflow_state, normalized_state)
        execution_status = self._normalize_action_execution_status(
            payload.get('execution_status') or payload.get('queue_result_action') or payload.get('result_action') or payload.get('execution_mode'),
            workflow_state=normalized_workflow,
            queue_status=queue_progression.get('status'),
            executor_status=((payload.get('executor_result') or {}).get('status') if isinstance(payload.get('executor_result'), dict) else None),
        )
        phase = 'proposal'
        if normalized_state in {'approved', 'rejected', 'deferred', 'expired'} or normalized_workflow in {'approved', 'rejected', 'deferred', 'expired'}:
            phase = 'terminal'
        elif normalized_workflow in {'queued', 'ready'}:
            phase = 'queue'
        elif normalized_workflow in {'executing', 'execution_failed', 'retry_pending', 'rollback_pending', 'rolled_back'} or execution_status in {'dispatching', 'applied', 'error', 'recovered'}:
            phase = 'execution'
        elif normalized_workflow in {'blocked', 'blocked_by_approval', 'review_pending'} or normalized_state in {'pending', 'ready', 'replayed'}:
            phase = 'approval'
        terminal = normalized_state in {'approved', 'rejected', 'deferred', 'expired'} or normalized_workflow in {'approved', 'rejected', 'deferred', 'expired'}
        retryable = bool(payload.get('retryable', queue_progression.get('retryable')))
        recovered_from_execution_status = payload.get('recovered_from_execution_status') or payload.get('previous_execution_status')
        if not last_transition:
            last_transition = {
                'from_workflow_state': payload.get('previous_workflow_state') or normalized_workflow,
                'to_workflow_state': normalized_workflow,
                'from_execution_status': recovered_from_execution_status,
                'to_execution_status': execution_status,
                'rule': transition_rule,
                'dispatch_route': dispatch_route,
                'next_transition': next_transition,
            }
        if terminal:
            action, route, follow_up = 'observe_only_followup', 'terminal_observe_only', 'observe_only'
        elif normalized_workflow in {'execution_failed', 'retry_pending'} or execution_status == 'error':
            action, route, follow_up = ('retry', 'retry_queue', 'retry_execution') if retryable else ('escalate', 'operator_escalation', 'escalate_execution')
        elif normalized_workflow == 'rollback_pending':
            action, route, follow_up = 'freeze_followup', 'freeze_followup_queue', 'freeze_and_review'
        elif normalized_workflow in {'blocked', 'blocked_by_approval', 'review_pending'} or blocked_by or execution_status == 'blocked':
            action, route, follow_up = 'review_schedule', 'manual_approval_queue' if normalized_workflow == 'blocked_by_approval' or normalized_state in {'pending', 'ready', 'replayed'} else 'operator_escalation', 'await_manual_approval' if normalized_workflow == 'blocked_by_approval' or normalized_state in {'pending', 'ready', 'replayed'} else 'escalate_blocked_state'
        elif normalized_workflow == 'deferred' or execution_status == 'deferred':
            action, route, follow_up = 'review_schedule', 'review_schedule_queue', 'scheduled_review'
        elif normalized_workflow == 'ready' and ((not rollout_stage and not target_rollout_stage) or rollout_stage == target_rollout_stage):
            action, route, follow_up = 'observe_only_followup', dispatch_route or 'observe_only_followup', 'observe_only_ready'
        elif normalized_workflow == 'ready':
            action, route, follow_up = 'review_schedule', dispatch_route or 'rollout_readiness_queue', 'review_ready_for_rollout'
        elif normalized_workflow == 'queued' or execution_status == 'queued':
            action, route, follow_up = 'observe_only_followup', queue_progression.get('status') or dispatch_route or 'queue_observer', 'watch_queue_progression'
        else:
            action, route, follow_up = 'observe_only_followup', dispatch_route or 'observe_only_followup', 'observe_only'
        rollback_candidate = bool(payload.get('rollback_hint') or queue_transition.get('rollback_hint') or payload.get('rollback_capable'))
        rollback_hint = payload.get('rollback_hint') or queue_transition.get('rollback_hint')
        lane_id = 'ready'
        lane_reason = 'workflow_ready_default'
        if normalized_workflow in {'execution_failed', 'retry_pending', 'rollback_pending', 'rolled_back'} or rollback_candidate:
            lane_id, lane_reason = 'rollback_candidate', 'rollback_gate_or_recovery_state'
        elif normalized_workflow == 'blocked_by_approval' or route == 'manual_approval_queue' or follow_up == 'await_manual_approval':
            lane_id, lane_reason = 'manual_approval', 'manual_gate_pending'
        elif normalized_workflow in {'blocked', 'deferred', 'review_pending'} or blocked_by or route in {'operator_escalation', 'freeze_followup_queue', 'review_schedule_queue'}:
            lane_id, lane_reason = 'blocked', 'blocked_or_review_followup'
        elif execution_status == 'queued' or normalized_workflow == 'queued' or queue_progression.get('status') in {'queued', 'ready_to_queue'}:
            lane_id, lane_reason = 'queued', 'queue_progression_active'
        elif payload.get('auto_advance_gate', {}).get('allowed'):
            lane_id, lane_reason = 'auto_batch', 'gate_allows_auto_advance'
        payload['execution_status'] = execution_status
        payload['transition_rule'] = transition_rule
        payload['last_transition'] = last_transition
        execution_timeline = {
            'schema_version': 'm5_execution_timeline_v1',
            'item_id': item_id,
            'latest_status': execution_status or 'planned',
            'previous_status': recovered_from_execution_status or last_transition.get('from_execution_status'),
            'statuses': [value for value in [recovered_from_execution_status or last_transition.get('from_execution_status'), execution_status or 'planned'] if value],
            'attempt_count': 2 if (execution_status in {'error', 'recovered'} and (recovered_from_execution_status or last_transition.get('from_execution_status'))) else 1,
            'retry_count': 1 if (execution_status in {'error', 'recovered'} and retryable) else 0,
            'retryable': retryable,
            'recovered': execution_status == 'recovered',
            'recovered_from_status': recovered_from_execution_status or (last_transition.get('from_execution_status') if execution_status == 'recovered' else None),
            'dispatch_route': dispatch_route,
            'queue_status': queue_progression.get('status'),
            'workflow_state': normalized_workflow,
            'transition_rule': transition_rule,
            'next_transition': next_transition,
            'rollback_hint': rollback_hint,
            'recovery_stage': 'recovered' if execution_status == 'recovered' else ('retry_pending' if execution_status == 'error' and retryable else 'manual_recovery_required' if execution_status == 'error' else 'blocked_pending_recovery' if execution_status in {'blocked', 'deferred'} else 'steady'),
            'last_transition': last_transition,
            'summary': f"{execution_status or 'planned'} via {dispatch_route or queue_progression.get('status') or normalized_workflow}",
        }
        recovery_policy = {
            'schema_version': 'm5_recovery_policy_v1',
            'item_id': item_id,
            'policy': 'recovered_monitoring' if execution_status == 'recovered' else ('retry' if execution_status == 'error' and retryable else 'manual_recovery' if execution_status == 'error' else 'rollback_candidate' if normalized_workflow == 'rollback_pending' else 'blocked_recovery' if blocked_by else 'observe'),
            'owner': 'operator' if normalized_workflow == 'rollback_pending' or (execution_status == 'error' and not retryable) else 'runtime',
            'recommended_action': 'freeze_followup' if normalized_workflow == 'rollback_pending' else ('retry' if execution_status == 'error' and retryable else 'escalate' if execution_status == 'error' else 'review_schedule' if blocked_by else 'observe_only_followup'),
            'retryable': retryable,
            'recovered': execution_status == 'recovered',
            'recovered_from_status': recovered_from_execution_status or (last_transition.get('from_execution_status') if execution_status == 'recovered' else None),
            'rollback_candidate': rollback_candidate,
            'rollback_hint': rollback_hint,
            'blocked_by': blocked_by,
            'dispatch_route': dispatch_route,
            'next_transition': next_transition,
            'workflow_state': normalized_workflow,
            'execution_status': execution_status or 'planned',
            'summary': f"{('recovered_monitoring' if execution_status == 'recovered' else ('retry' if execution_status == 'error' and retryable else 'manual_recovery' if execution_status == 'error' else 'rollback_candidate' if normalized_workflow == 'rollback_pending' else 'blocked_recovery' if blocked_by else 'observe'))} -> {('freeze_followup' if normalized_workflow == 'rollback_pending' else ('retry' if execution_status == 'error' and retryable else 'escalate' if execution_status == 'error' else 'review_schedule' if blocked_by else 'observe_only_followup'))}",
        }
        retry_count = max(int(execution_timeline.get('retry_count') or 0), 0)
        retry_delay_min = [5, 15, 30, 60][min(retry_count, 3)]
        recovery_bucket = 'observe'
        recovery_route = 'observe_only_followup'
        manual_reason = None
        if execution_status == 'recovered':
            recovery_bucket = 'recovered_monitoring'
        elif normalized_workflow == 'rollback_pending' or (rollback_candidate and execution_status == 'error' and not retryable):
            recovery_bucket = 'rollback_candidate'
            recovery_route = 'rollback_candidate_queue'
        elif execution_status == 'error' or normalized_workflow in {'execution_failed', 'retry_pending'}:
            if retryable and retry_count < 3:
                recovery_bucket = 'retry_queue'
                recovery_route = 'retry_queue'
            elif rollback_candidate:
                recovery_bucket = 'rollback_candidate'
                recovery_route = 'rollback_candidate_queue'
            else:
                recovery_bucket = 'manual_recovery'
                recovery_route = 'manual_recovery_queue'
                manual_reason = 'execution_failed_without_safe_retry_or_rollback'
        elif blocked_by:
            if retryable:
                recovery_bucket = 'retry_queue'
                recovery_route = 'retry_queue'
            elif rollback_candidate:
                recovery_bucket = 'rollback_candidate'
                recovery_route = 'rollback_candidate_queue'
            else:
                recovery_bucket = 'manual_recovery'
                recovery_route = 'manual_recovery_queue'
                manual_reason = 'blocked_item_requires_operator_resolution'
        recovery_orchestration = {
            'schema_version': 'm5_recovery_orchestration_v1',
            'item_id': item_id,
            'queue_bucket': recovery_bucket,
            'target_route': recovery_route,
            'retry_stage': ('initial_retry' if retry_count == 0 else ('backoff_retry' if retry_count < 3 else 'final_retry_window')) if recovery_bucket == 'retry_queue' else None,
            'retry_schedule': {
                'attempt_count': execution_timeline.get('attempt_count') or 1,
                'retry_count': retry_count,
                'delay_minutes': retry_delay_min if recovery_bucket == 'retry_queue' else None,
            },
            'rollback_candidate': recovery_bucket == 'rollback_candidate' or rollback_candidate,
            'rollback_route': 'rollback_candidate_queue' if (recovery_bucket == 'rollback_candidate' or rollback_candidate) else None,
            'rollback_hint': rollback_hint,
            'manual_recovery': {
                'required': recovery_bucket == 'manual_recovery',
                'route': 'manual_recovery_queue' if recovery_bucket == 'manual_recovery' else None,
                'reason': manual_reason,
                'fallback_action': 'operator_review_and_safe_requeue' if recovery_bucket == 'manual_recovery' else None,
            },
            'dispatch_route': dispatch_route,
            'next_transition': next_transition,
            'workflow_state': normalized_workflow,
            'execution_status': execution_status or 'planned',
            'blocked_by': blocked_by,
            'policy': recovery_policy.get('policy'),
            'summary': f"{recovery_bucket} via {recovery_route}",
        }
        payload['state_machine'] = {
            'schema_version': 'm5_unified_state_machine_v2',
            'item_id': item_id,
            'decision': str(decision or 'pending').strip().lower() or 'pending',
            'approval_state': normalized_state,
            'workflow_state': normalized_workflow,
            'queue_status': queue_progression.get('status'),
            'dispatch_route': dispatch_route,
            'next_transition': next_transition,
            'transition_rule': transition_rule,
            'last_transition': last_transition,
            'execution_status': execution_status,
            'rollout_stage': rollout_stage,
            'target_rollout_stage': target_rollout_stage,
            'phase': phase,
            'blocked_by': blocked_by,
            'terminal': terminal,
            'retryable': retryable,
            'rollback_candidate': rollback_candidate,
            'rollback_hint': rollback_hint,
            'execution_timeline': execution_timeline,
            'recovery_policy': recovery_policy,
            'recovery_orchestration': recovery_orchestration,
            'operator_action_policy': {
                'schema_version': 'm5_operator_action_policy_v1',
                'item_id': item_id,
                'action': action,
                'route': route,
                'follow_up': follow_up,
                'retryable': retryable,
                'blocked_by': blocked_by,
                'summary': f"{action} via {route}",
            },
            'lane_routing': {
                'schema_version': 'm5_lane_routing_v1',
                'lane_id': lane_id,
                'lane_reason': lane_reason,
                'queue_name': queue_progression.get('queue_name') or dispatch_route or route,
                'queue_status': queue_progression.get('status') or normalized_workflow,
                'dispatch_route': dispatch_route or route,
                'route_family': route,
                'next_transition': next_transition or follow_up,
                'summary': f"{lane_id} via {route}",
            },
            'executor_result': {
                'status': payload.get('execution_mode') or payload.get('queue_result_action') or payload.get('result_action'),
                'layer': payload.get('execution_layer'),
                'route': dispatch_route,
            },
        }
        return payload

    def _normalize_event_type(self, event_type: Optional[str], category: str = 'approval') -> str:
        normalized = str(event_type or '').strip().lower()
        if normalized:
            return normalized
        fallback = str(category or 'unknown').strip().lower() or 'unknown'
        return f"{fallback}_event"

    def _build_event_provenance(self, *, origin: str, source: Optional[str] = None, family: Optional[str] = None,
                                phase: Optional[str] = None, producer: Optional[str] = None,
                                replay_source: Optional[str] = None, synthetic: bool = False) -> Dict[str, Any]:
        normalized_origin = str(origin or 'unknown').strip().lower() or 'unknown'
        normalized_source = str(source or '').strip() or normalized_origin
        normalized_family = str(family or normalized_origin).strip().lower() or normalized_origin
        normalized_phase = str(phase or normalized_family).strip().lower() or normalized_family
        normalized_producer = str(producer or normalized_source).strip() or normalized_source
        normalized_replay = str(replay_source or '').strip() or None
        return {
            'schema_version': 'm5_event_provenance_v1',
            'origin': normalized_origin,
            'source': normalized_source,
            'family': normalized_family,
            'phase': normalized_phase,
            'producer': normalized_producer,
            'replay_source': normalized_replay,
            'synthetic': bool(synthetic),
        }

    def _build_event_timestamp(self, *, value: Optional[str], source: Optional[str], phase: Optional[str],
                               field: Optional[str], fallback_fields: Optional[List[str]] = None) -> Dict[str, Any]:
        normalized_value = str(value or '').strip() or None
        normalized_source = str(source or '').strip() or ('missing' if normalized_value is None else 'observed')
        normalized_phase = str(phase or normalized_source).strip().lower() or normalized_source
        normalized_field = str(field or '').strip() or None
        return {
            'schema_version': 'm5_event_provenance_v1',
            'value': normalized_value,
            'source': normalized_source,
            'phase': normalized_phase,
            'field': normalized_field,
            'fallback_fields': [str(item).strip() for item in (fallback_fields or []) if str(item).strip()],
            'present': normalized_value is not None,
        }

    def _attach_approval_event_metadata(self, row: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(row or {})
        details = self._safe_json_dict(payload.get('details'))
        payload['details'] = details
        normalized_event_type = self._normalize_event_type(payload.get('event_type'), 'approval')
        payload['normalized_event_type'] = normalized_event_type
        payload['provenance'] = self._build_event_provenance(
            origin='approval_db',
            source='approval_timeline',
            family='approval',
            phase='approval_db',
            producer=payload.get('source') or payload.get('replay_source') or 'approval_db',
            replay_source=payload.get('source') if str(payload.get('source') or '').strip().startswith('workflow') else details.get('replay_source'),
            synthetic=False,
        )
        payload['timestamp_info'] = self._build_event_timestamp(
            value=payload.get('created_at') or payload.get('updated_at'),
            source='approval_event_created_at' if payload.get('created_at') else ('approval_event_updated_at' if payload.get('updated_at') else 'approval_event_missing'),
            phase='approval_db',
            field='created_at' if payload.get('created_at') else ('updated_at' if payload.get('updated_at') else None),
            fallback_fields=['updated_at'],
        )
        payload['timestamp'] = payload['timestamp_info'].get('value')
        return payload
    
    def _init_db(self):
        """初始化数据库表"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 信号记录表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                price REAL NOT NULL,
                strength INTEGER DEFAULT 0,
                reasons TEXT,
                strategies_triggered TEXT,
                filtered INTEGER DEFAULT 0,
                filter_reason TEXT,
                filter_code TEXT,
                filter_group TEXT,
                action_hint TEXT,
                filter_details TEXT,
                executed INTEGER DEFAULT 0,
                trade_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 交易记录表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                quantity REAL NOT NULL,
                contract_size REAL DEFAULT 1,
                coin_quantity REAL,
                leverage INTEGER DEFAULT 1,
                pnl REAL,
                pnl_percent REAL,
                status TEXT DEFAULT 'open',
                open_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                close_time TIMESTAMP,
                notes TEXT,
                close_source TEXT,
                close_fill_count INTEGER DEFAULT 0,
                layer_no INTEGER,
                root_signal_id INTEGER,
                plan_context TEXT,
                outcome_attribution TEXT,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            )
        """)
        
        # 持仓表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                current_price REAL,
                quantity REAL NOT NULL,
                contract_size REAL DEFAULT 1,
                coin_quantity REAL,
                leverage INTEGER DEFAULT 1,
                unrealized_pnl REAL DEFAULT 0,
                peak_price REAL,
                trough_price REAL,
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 每日总结表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE UNIQUE NOT NULL,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                losing_trades INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                total_volume REAL DEFAULT 0,
                signals_generated INTEGER DEFAULT 0,
                signals_filtered INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 策略分析表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS strategy_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                strategy_name TEXT NOT NULL,
                triggered INTEGER DEFAULT 0,
                strength INTEGER DEFAULT 0,
                confidence REAL,
                action TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            )
        """)
        
        # 系统日志表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 候选晋升/降级历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS candidate_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                decision TEXT NOT NULL,
                best_variant TEXT,
                score REAL,
                reason TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # preset 应用历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS preset_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                preset_name TEXT NOT NULL,
                watch_list TEXT,
                backup_path TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 治理决策历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS governance_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_type TEXT NOT NULL,
                level TEXT,
                approval_required INTEGER DEFAULT 0,
                recommended_preset TEXT,
                message TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 日报
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date DATE NOT NULL,
                summary TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 审批历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS approval_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                approval_type TEXT NOT NULL,
                target TEXT,
                decision TEXT NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 审批状态台账（可恢复 / 可重放 / 可审计）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS approval_state (
                item_id TEXT PRIMARY KEY,
                approval_type TEXT NOT NULL,
                target TEXT,
                title TEXT,
                decision TEXT NOT NULL DEFAULT 'pending',
                state TEXT NOT NULL DEFAULT 'pending',
                workflow_state TEXT,
                reason TEXT,
                actor TEXT,
                replay_source TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 审批事件日志（immutable event log）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS approval_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL,
                approval_type TEXT NOT NULL,
                target TEXT,
                title TEXT,
                event_type TEXT NOT NULL,
                decision TEXT NOT NULL DEFAULT 'pending',
                state TEXT NOT NULL DEFAULT 'pending',
                workflow_state TEXT,
                reason TEXT,
                actor TEXT,
                source TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 治理参数变更历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS governance_config_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_key TEXT NOT NULL,
                before_value TEXT,
                after_value TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # smoke 验收执行历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS smoke_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange_mode TEXT,
                position_mode TEXT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                amount REAL,
                success INTEGER DEFAULT 0,
                error TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # override 应用/回滚审计历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS override_audit_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                target_file TEXT,
                backup_path TEXT,
                symbols TEXT,
                parameter_count INTEGER DEFAULT 0,
                note TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 连亏熔断状态（单例）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS risk_guard_state (
                guard_key TEXT PRIMARY KEY,
                current_streak INTEGER DEFAULT 0,
                lock_active INTEGER DEFAULT 0,
                lock_until TEXT,
                triggered_at TEXT,
                reset_at TEXT,
                last_trade_id INTEGER DEFAULT 0,
                details TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 通知 outbox（给 OpenClaw bridge 消费）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                event_type TEXT NOT NULL,
                title TEXT,
                message TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                details TEXT,
                delivered_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Partial TP 触发历史表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS partial_tp_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                trigger_price REAL NOT NULL,
                close_ratio REAL NOT NULL,
                close_quantity REAL NOT NULL,
                pnl REAL,
                note TEXT,
                source TEXT DEFAULT 'partial_tp',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 开仓 intent（下单前先写入，完成后回收）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS open_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                root_signal_id INTEGER,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                planned_margin REAL DEFAULT 0,
                leverage INTEGER DEFAULT 1,
                layer_no INTEGER,
                plan_context TEXT,
                notes TEXT,
                trade_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # symbol+side 方向锁
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS direction_locks (
                lock_key TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                owner TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 分仓计划状态
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS layer_plan_states (
                plan_key TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT DEFAULT 'idle',
                current_layer INTEGER DEFAULT 0,
                root_signal_id INTEGER,
                plan_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 兼容旧库：补充信号诊断字段
        cursor.execute("PRAGMA table_info(signals)")
        signal_columns = {row[1] for row in cursor.fetchall()}
        if 'filter_code' not in signal_columns:
            cursor.execute("ALTER TABLE signals ADD COLUMN filter_code TEXT")
        if 'filter_group' not in signal_columns:
            cursor.execute("ALTER TABLE signals ADD COLUMN filter_group TEXT")
        if 'action_hint' not in signal_columns:
            cursor.execute("ALTER TABLE signals ADD COLUMN action_hint TEXT")
        if 'filter_details' not in signal_columns:
            cursor.execute("ALTER TABLE signals ADD COLUMN filter_details TEXT")

        # 兼容旧库：补充 trades / positions 单位字段与持仓追踪锚点字段
        cursor.execute("PRAGMA table_info(trades)")
        trade_columns = {row[1] for row in cursor.fetchall()}
        if 'contract_size' not in trade_columns:
            cursor.execute("ALTER TABLE trades ADD COLUMN contract_size REAL DEFAULT 1")
        if 'coin_quantity' not in trade_columns:
            cursor.execute("ALTER TABLE trades ADD COLUMN coin_quantity REAL")
        if 'close_source' not in trade_columns:
            cursor.execute("ALTER TABLE trades ADD COLUMN close_source TEXT")
        if 'close_fill_count' not in trade_columns:
            cursor.execute("ALTER TABLE trades ADD COLUMN close_fill_count INTEGER DEFAULT 0")
        if 'layer_no' not in trade_columns:
            cursor.execute("ALTER TABLE trades ADD COLUMN layer_no INTEGER")
        if 'root_signal_id' not in trade_columns:
            cursor.execute("ALTER TABLE trades ADD COLUMN root_signal_id INTEGER")
        if 'plan_context' not in trade_columns:
            cursor.execute("ALTER TABLE trades ADD COLUMN plan_context TEXT")
        if 'outcome_attribution' not in trade_columns:
            cursor.execute("ALTER TABLE trades ADD COLUMN outcome_attribution TEXT")

        cursor.execute("PRAGMA table_info(positions)")
        position_columns = {row[1] for row in cursor.fetchall()}
        if 'contract_size' not in position_columns:
            cursor.execute("ALTER TABLE positions ADD COLUMN contract_size REAL DEFAULT 1")
        if 'coin_quantity' not in position_columns:
            cursor.execute("ALTER TABLE positions ADD COLUMN coin_quantity REAL")
        if 'peak_price' not in position_columns:
            cursor.execute("ALTER TABLE positions ADD COLUMN peak_price REAL")
        if 'trough_price' not in position_columns:
            cursor.execute("ALTER TABLE positions ADD COLUMN trough_price REAL")

        cursor.execute("PRAGMA table_info(approval_state)")
        approval_state_columns = {row[1] for row in cursor.fetchall()}
        for ddl in [
            ('title', "ALTER TABLE approval_state ADD COLUMN title TEXT"),
            ('decision', "ALTER TABLE approval_state ADD COLUMN decision TEXT NOT NULL DEFAULT 'pending'"),
            ('state', "ALTER TABLE approval_state ADD COLUMN state TEXT NOT NULL DEFAULT 'pending'"),
            ('workflow_state', "ALTER TABLE approval_state ADD COLUMN workflow_state TEXT"),
            ('reason', "ALTER TABLE approval_state ADD COLUMN reason TEXT"),
            ('actor', "ALTER TABLE approval_state ADD COLUMN actor TEXT"),
            ('replay_source', "ALTER TABLE approval_state ADD COLUMN replay_source TEXT"),
            ('details', "ALTER TABLE approval_state ADD COLUMN details TEXT"),
            ('updated_at', "ALTER TABLE approval_state ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ('last_seen_at', "ALTER TABLE approval_state ADD COLUMN last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ]:
            if ddl[0] not in approval_state_columns:
                cursor.execute(ddl[1])

        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_smoke_runs_symbol ON smoke_runs(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_smoke_runs_created ON smoke_runs(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notification_outbox_status ON notification_outbox(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notification_outbox_created ON notification_outbox(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_partial_tp_history_symbol ON partial_tp_history(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_partial_tp_history_created ON partial_tp_history(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_open_intents_symbol_side_status ON open_intents(symbol, side, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_open_intents_signal_id ON open_intents(signal_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_layer_plan_states_symbol_side ON layer_plan_states(symbol, side)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_approval_state_type_target ON approval_state(approval_type, target)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_approval_state_state_updated ON approval_state(state, updated_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_approval_events_item_created ON approval_events(item_id, created_at, id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_approval_events_type_target ON approval_events(approval_type, target)")
        
        conn.commit()
        conn.close()
    
    # =========================================================================
    # 信号操作
    # =========================================================================
    
    def record_signal(self, symbol: str, signal_type: str, price: float,
                      strength: int, reasons: List[Dict], 
                      strategies_triggered: List[str]) -> int:
        """记录信号"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO signals (symbol, signal_type, price, strength, reasons, strategies_triggered)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (symbol, signal_type, price, strength, 
              json.dumps(reasons), json.dumps(strategies_triggered)))
        
        signal_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return signal_id
    
    def update_signal(self, signal_id: int, **kwargs):
        """更新信号"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        fields = []
        values = []
        for k, v in kwargs.items():
            fields.append(f"{k} = ?")
            values.append(v)
        
        values.append(signal_id)
        cursor.execute(f"UPDATE signals SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        conn.close()
    
    def get_signals(self, symbol: str = None, limit: int = 100, 
                    executed_only: bool = False) -> List[Dict]:
        """获取信号列表"""
        conn = self._get_connection()
        
        query = "SELECT * FROM signals"
        conditions = []
        params = []
        
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if executed_only:
            conditions.append("executed = 1")
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        
        # 转换JSON字段
        if not df.empty:
            df['reasons'] = df['reasons'].apply(lambda x: json.loads(x) if x else [])
            df['strategies_triggered'] = df['strategies_triggered'].apply(lambda x: json.loads(x) if x else [])
            if 'filter_details' in df.columns:
                df['filter_details'] = df['filter_details'].apply(
                    lambda x: json.loads(x) if isinstance(x, str) and x else ({ } if x is None or pd.isna(x) else x)
                )
            df['filtered'] = df['filtered'].astype(bool)
            df['executed'] = df['executed'].astype(bool)
        
        return df.to_dict('records')

    def get_trades_missing_close_details(self, limit: int = 200) -> List[Dict]:
        conn = self._get_connection()
        query = """
            SELECT * FROM trades
            WHERE status = 'closed'
              AND (exit_price IS NULL OR pnl IS NULL OR pnl_percent IS NULL OR close_source IS NULL OR close_source = '')
            ORDER BY close_time DESC, id DESC
            LIMIT ?
        """
        df = pd.read_sql_query(query, conn, params=(limit,))
        conn.close()
        if not df.empty:
            if 'contract_size' not in df.columns:
                df['contract_size'] = 1.0
            if 'coin_quantity' not in df.columns:
                df['coin_quantity'] = df['quantity'] * df['contract_size']
            else:
                df['coin_quantity'] = df.apply(lambda r: r['coin_quantity'] if pd.notna(r['coin_quantity']) else r['quantity'] * (r['contract_size'] if pd.notna(r['contract_size']) else 1.0), axis=1)
        return df.to_dict('records')
    
    # =========================================================================
    # 交易操作
    # =========================================================================
    
    def record_trade(self, symbol: str, side: str, entry_price: float,
                     quantity: float, leverage: int = 1,
                     signal_id: int = None, notes: str = None,
                     contract_size: float = 1.0, coin_quantity: float = None,
                     layer_no: int = None, root_signal_id: int = None, plan_context: Dict = None) -> int:
        """记录交易"""
        conn = self._get_connection()
        cursor = conn.cursor()
        quantity = self._safe_float(quantity)
        contract_size = self._safe_float(contract_size, 1.0) or 1.0
        leverage = max(1, int(self._safe_float(leverage, 1) or 1))
        if coin_quantity is None:
            coin_quantity = quantity * contract_size
        else:
            coin_quantity = self._safe_float(coin_quantity)
        
        cursor.execute("""
            INSERT INTO trades (symbol, side, entry_price, quantity, contract_size, coin_quantity, leverage, signal_id, notes, layer_no, root_signal_id, plan_context)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, side, entry_price, quantity, contract_size, coin_quantity, leverage, signal_id, notes, layer_no, root_signal_id, json.dumps(plan_context, ensure_ascii=False) if plan_context is not None else None))
        
        trade_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return trade_id
    
    def update_trade(self, trade_id: int, **kwargs):
        """更新交易"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        fields = []
        values = []
        for k, v in kwargs.items():
            fields.append(f"{k} = ?")
            values.append(v)
        
        values.append(trade_id)
        cursor.execute(f"UPDATE trades SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        conn.close()
    
    def _extract_strategy_tags(self, plan_context: Dict[str, Any], current: Dict[str, Any]) -> List[str]:
        tags = []
        seen = set()
        candidates = [
            (plan_context or {}).get('strategy_tags'),
            (plan_context or {}).get('strategies_triggered'),
            ((plan_context or {}).get('entry_plan') or {}).get('strategy_tags'),
            current.get('strategy_tags'),
        ]
        for candidate in candidates:
            values = candidate if isinstance(candidate, list) else ([candidate] if candidate else [])
            for value in values:
                tag = str(value or '').strip()
                if tag and tag not in seen:
                    seen.add(tag)
                    tags.append(tag)
        return tags

    def _build_trade_outcome_attribution(self, current: Dict[str, Any], summary: Dict = None, *, reason: str = None,
                                         exit_price: Any = None, pnl: Any = None, pnl_percent: Any = None,
                                         close_source: str = None, close_fill_count: Any = None) -> Dict[str, Any]:
        current = dict(current or {})
        summary = dict(summary or {})
        plan_context = self._safe_json_dict(current.get('plan_context'))
        observability = self._safe_json_dict(plan_context.get('observability'))
        regime_snapshot = self._safe_json_dict(plan_context.get('regime_snapshot'))
        policy_snapshot = self._safe_json_dict(plan_context.get('adaptive_policy_snapshot'))
        strategy_tags = self._extract_strategy_tags(plan_context, current)
        effective_close_source = close_source or summary.get('source') or current.get('close_source') or 'local_market_close'
        effective_fill_count = int(close_fill_count if close_fill_count is not None else len(summary.get('fills') or []))
        entry_price = self._safe_float(current.get('entry_price'))
        effective_exit_price = self._safe_float(exit_price if exit_price not in (None, '') else summary.get('exit_price') or current.get('exit_price'))
        effective_pnl = pnl if pnl not in (None, '') else summary.get('pnl', current.get('pnl'))
        effective_pnl = None if effective_pnl in (None, '') else self._safe_float(effective_pnl)
        effective_pnl_percent = pnl_percent if pnl_percent not in (None, '') else summary.get('pnl_percent', current.get('pnl_percent'))
        effective_pnl_percent = None if effective_pnl_percent in (None, '') else self._safe_float(effective_pnl_percent)
        if effective_pnl_percent is None and effective_pnl is not None:
            leverage = max(1, int(self._safe_float(current.get('leverage'), 1)))
            coin_quantity = self._safe_float(summary.get('coin_quantity') or current.get('coin_quantity'))
            margin = (entry_price * coin_quantity) / leverage if entry_price > 0 and coin_quantity > 0 else 0.0
            effective_pnl_percent = (effective_pnl / margin * 100) if margin > 0 else None
        close_time_raw = summary.get('close_time') or current.get('close_time') or datetime.now().isoformat()
        holding_seconds = None
        try:
            opened = datetime.fromisoformat(str(current.get('open_time')).replace('Z', '+00:00'))
            closed = datetime.fromisoformat(str(close_time_raw).replace('Z', '+00:00'))
            holding_seconds = max(0, int((closed - opened).total_seconds()))
        except Exception:
            holding_seconds = None
        holding_minutes = round(holding_seconds / 60, 2) if holding_seconds is not None else None
        reason_text = str(reason or '').strip()
        reason_lower = reason_text.lower()
        if 'stop' in reason_lower or '止损' in reason_text:
            reason_category = 'stop_loss'
        elif 'tp' in reason_lower or 'take_profit' in reason_lower or '止盈' in reason_text:
            reason_category = 'take_profit'
        elif 'reconcile' in reason_lower or '收口' in reason_text or '对账' in reason_text:
            reason_category = 'reconcile_close'
        elif 'manual' in reason_lower or '手动' in reason_text:
            reason_category = 'manual_close'
        elif 'stale' in reason_lower or '无对应仓位' in reason_text:
            reason_category = 'stale_close'
        else:
            reason_category = 'signal_or_rule_close' if reason_text else 'unknown'
        close_decision = 'win' if (effective_pnl or 0) > 0 else 'loss' if (effective_pnl or 0) < 0 else 'flat'
        abs_return = abs(float(effective_pnl_percent or 0.0))
        pnl_bucket = 'flat' if close_decision == 'flat' else ('large' if abs_return >= 5 else 'medium' if abs_return >= 1 else 'small')
        outcome_quality = 'positive' if close_decision == 'win' else ('bounded_loss' if reason_category == 'stop_loss' else 'adverse' if close_decision == 'loss' else 'aligned')
        observe_only = normalize_observe_only_view(
            observability.get('observe_only') or {},
            regime_snapshot=regime_snapshot,
            policy_snapshot=policy_snapshot,
            fallback_summary=(observability.get('observe_only') or {}).get('summary'),
        )
        return {
            'schema_version': 'trade_outcome_attribution_v1',
            'close_reason': reason_text or None,
            'close_reason_category': reason_category,
            'close_decision': close_decision,
            'close_source': effective_close_source,
            'close_fill_count': effective_fill_count,
            'regime_tag': regime_snapshot.get('name') or regime_snapshot.get('regime') or policy_snapshot.get('regime_name') or 'unknown',
            'policy_tag': policy_snapshot.get('policy_version') or 'unknown',
            'strategy_tags': strategy_tags,
            'dominant_strategy': strategy_tags[0] if strategy_tags else 'unknown',
            'strategy_count': len(strategy_tags),
            'observe_only': observe_only,
            'holding_seconds': holding_seconds,
            'holding_minutes': holding_minutes,
            'pnl_bucket': pnl_bucket,
            'outcome_quality': outcome_quality,
            'entry_price': entry_price,
            'exit_price': effective_exit_price,
            'pnl': effective_pnl,
            'return_pct': effective_pnl_percent,
            'plan_layer_no': plan_context.get('layer_no') or current.get('layer_no'),
            'root_signal_id': plan_context.get('root_signal_id') or current.get('root_signal_id'),
            'signal_id': current.get('signal_id'),
            'layer_ratio': plan_context.get('layer_ratio'),
            'planned_margin': plan_context.get('planned_margin'),
            'risk_budget': self._safe_json_dict((plan_context.get('entry_plan') or {}).get('risk_budget')),
        }

    def close_trade_with_outcome_enrichment(self, trade_id: int, exit_price: float, pnl: float,
                                            pnl_percent: float, notes: str = None, close_source: str = 'local_market_close',
                                            close_time: str = None, close_fill_count: int = 0) -> bool:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        current = dict(row)
        outcome = self._build_trade_outcome_attribution(
            current,
            reason=notes,
            exit_price=exit_price,
            pnl=pnl,
            pnl_percent=pnl_percent,
            close_source=close_source,
            close_fill_count=close_fill_count,
        )
        cursor.execute("""
            UPDATE trades 
            SET exit_price = ?, pnl = ?, pnl_percent = ?, 
                status = 'closed', close_time = COALESCE(?, CURRENT_TIMESTAMP), notes = ?,
                close_source = ?, close_fill_count = ?, outcome_attribution = ?
            WHERE id = ?
        """, (exit_price, pnl, pnl_percent, close_time, notes, close_source, int(close_fill_count or 0), json.dumps(outcome, ensure_ascii=False), trade_id))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    def close_trade(self, trade_id: int, exit_price: float, pnl: float, 
                    pnl_percent: float, notes: str = None, close_source: str = 'local_market_close',
                    close_time: str = None, close_fill_count: int = 0):
        """平仓"""
        self.close_trade_with_outcome_enrichment(
            trade_id=trade_id,
            exit_price=exit_price,
            pnl=pnl,
            pnl_percent=pnl_percent,
            notes=notes,
            close_source=close_source,
            close_time=close_time,
            close_fill_count=close_fill_count,
        )

    def reconcile_trade_close(self, trade_id: int, summary: Dict, reason: str = None) -> bool:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        current = dict(row)
        source = (summary or {}).get('source') or current.get('close_source') or 'reconcile_fallback'
        fills = (summary or {}).get('fills') or []
        fill_count = len(fills)
        existing_notes = current.get('notes') or ''
        extra_note = reason or ''
        if source:
            extra_note = f"{extra_note} | close_source={source}" if extra_note else f"close_source={source}"
        final_notes = existing_notes
        if extra_note:
            final_notes = f"{existing_notes} | {extra_note}" if existing_notes else extra_note
        close_time = (summary or {}).get('close_time') or current.get('close_time')
        quantity = self._safe_float((summary or {}).get('quantity') or current.get('quantity'))
        contract_size = self._safe_float((summary or {}).get('contract_size') or current.get('contract_size') or 1.0, 1.0) or 1.0
        summary_coin_quantity = (summary or {}).get('coin_quantity')
        if summary_coin_quantity in (None, ''):
            coin_quantity = quantity * contract_size if quantity > 0 else self._safe_float(current.get('coin_quantity'))
        else:
            coin_quantity = self._safe_float(summary_coin_quantity)
        pnl = (summary or {}).get('pnl')
        pnl = None if pnl in (None, '') else self._safe_float(pnl)
        pnl_percent = (summary or {}).get('pnl_percent')
        pnl_percent = None if pnl_percent in (None, '') else self._safe_float(pnl_percent)
        if pnl is not None and pnl_percent is None:
            leverage = max(1, int(self._safe_float(current.get('leverage'), 1)))
            entry_price = self._safe_float(current.get('entry_price'))
            margin = (entry_price * coin_quantity) / leverage if entry_price > 0 and coin_quantity > 0 else 0.0
            pnl_percent = (pnl / margin * 100) if margin > 0 else None
        outcome = self._build_trade_outcome_attribution(
            current,
            summary,
            reason=final_notes,
            close_source=source,
            close_fill_count=fill_count,
        )
        cursor.execute("""
            UPDATE trades
            SET status = 'closed',
                exit_price = ?,
                pnl = ?,
                pnl_percent = ?,
                quantity = ?,
                coin_quantity = ?,
                contract_size = ?,
                close_time = COALESCE(?, close_time, CURRENT_TIMESTAMP),
                notes = ?,
                close_source = ?,
                close_fill_count = ?,
                outcome_attribution = ?
            WHERE id = ?
        """, (
            (summary or {}).get('exit_price'),
            pnl,
            pnl_percent,
            quantity,
            coin_quantity,
            contract_size,
            close_time,
            final_notes,
            source,
            fill_count,
            json.dumps(outcome, ensure_ascii=False),
            trade_id,
        ))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed
    
    def get_trades(self, symbol: str = None, status: str = None,
                   limit: int = 100) -> List[Dict]:
        """获取交易列表"""
        conn = self._get_connection()
        
        query = "SELECT * FROM trades"
        conditions = []
        params = []
        
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if status:
            conditions.append("status = ?")
            params.append(status)
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY open_time DESC LIMIT ?"
        params.append(limit)
        
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        rows = df.to_dict('records')
        return [self._recalculate_trade_metrics(row) for row in rows]

    def get_latest_open_trade(self, symbol: str, side: str = None) -> Optional[Dict]:
        """获取某币种最新未平仓交易"""
        conn = self._get_connection()
        query = "SELECT * FROM trades WHERE symbol = ? AND status = 'open'"
        params = [symbol]
        if side:
            query += " AND side = ?"
            params.append(side)
        query += " ORDER BY open_time DESC, id DESC LIMIT 1"
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        if df.empty:
            return None
        return self._recalculate_trade_metrics(df.iloc[0].to_dict())

    def get_latest_trade_time(self, symbol: str = None) -> Optional[datetime]:
        conn = self._get_connection()
        cursor = conn.cursor()
        if symbol:
            cursor.execute("SELECT open_time FROM trades WHERE symbol = ? ORDER BY open_time DESC, id DESC LIMIT 1", (symbol,))
        else:
            cursor.execute("SELECT open_time FROM trades ORDER BY open_time DESC, id DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        if not row or not row[0]:
            return None
        return datetime.fromisoformat(row[0])

    def get_open_trades(self, symbol: str = None, side: str = None, limit: int = 200) -> List[Dict]:
        conn = self._get_connection()
        query = "SELECT * FROM trades WHERE status = 'open'"
        params = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if side:
            query += " AND side = ?"
            params.append(side)
        query += " ORDER BY open_time DESC, id DESC LIMIT ?"
        params.append(limit)
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        rows = df.to_dict('records')
        return [self._recalculate_trade_metrics(row) for row in rows]

    def mark_trade_stale_closed(self, trade_id: int, reason: str, close_price: float = None, close_source: str = 'reconcile_fallback'):
        summary = {'exit_price': close_price, 'source': close_source, 'fills': []}
        return self.reconcile_trade_close(trade_id, summary, reason=f"自动收口: {reason}")
    
    def get_recent_close_outcome_trades(self, symbol: str = None, limit: int = 200) -> List[Dict[str, Any]]:
        """获取最近已平仓交易（按 close_time 倒序），保留 outcome attribution 供风控窗口计算。"""
        conn = self._get_connection()
        query = "SELECT * FROM trades WHERE status = 'closed'"
        params = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY close_time DESC, open_time DESC, id DESC LIMIT ?"
        params.append(limit)
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        return [self._recalculate_trade_metrics(row) for row in df.to_dict('records')]

    def get_close_outcome_digest(self, symbol: str = None, limit: int = 200) -> Dict[str, Any]:
        """获取平仓 outcome 聚合摘要"""
        rows = self.get_recent_close_outcome_trades(symbol=symbol, limit=limit)
        from analytics.helper import build_close_outcome_digest
        return build_close_outcome_digest(rows, label='database_trade_close_outcomes')

    def get_trade_stats(self, days: int = 30) -> Dict:
        """获取交易统计"""
        conn = self._get_connection()
        
        query = """
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                SUM(pnl) as total_pnl,
                AVG(pnl_percent) as avg_pnl_percent
            FROM trades 
            WHERE status = 'closed' 
            AND open_time >= datetime('now', '-' || ? || ' days')
        """
        
        df = pd.read_sql_query(query, conn, params=(days,))
        conn.close()
        
        if not df.empty:
            row = df.iloc[0]
            total = row['total'] or 0
            wins = row['wins'] or 0
            return {
                'total_trades': int(total),
                'winning_trades': int(wins),
                'losing_trades': int(row['losses'] or 0),
                'win_rate': round(wins / total * 100, 2) if total > 0 else 0,
                'total_pnl': round(row['total_pnl'] or 0, 2),
                'avg_pnl_percent': round(row['avg_pnl_percent'] or 0, 2)
            }
        return {'total_trades': 0, 'winning_trades': 0, 'losing_trades': 0, 
                'win_rate': 0, 'total_pnl': 0, 'avg_pnl_percent': 0}
    
    # =========================================================================
    # 持仓操作
    # =========================================================================
    
    def update_position(self, symbol: str, side: str, entry_price: float,
                       quantity: float, leverage: int, current_price: float,
                       peak_price: float = None, trough_price: float = None,
                       contract_size: float = 1.0, coin_quantity: float = None):
        """更新持仓"""
        conn = self._get_connection()
        cursor = conn.cursor()
        quantity = self._safe_float(quantity)
        contract_size = self._safe_float(contract_size, 1.0) or 1.0
        leverage = max(1, int(self._safe_float(leverage, 1) or 1))
        entry_price = self._safe_float(entry_price)
        current_price = self._safe_float(current_price, entry_price)
        if coin_quantity is None:
            coin_quantity = quantity * contract_size
        else:
            coin_quantity = self._safe_float(coin_quantity)
        
        # 计算未实现盈亏（按折算币数量）
        if side == 'long':
            unrealized_pnl = (current_price - entry_price) * coin_quantity
        else:
            unrealized_pnl = (entry_price - current_price) * coin_quantity

        cursor.execute("SELECT opened_at, peak_price, trough_price FROM positions WHERE symbol = ?", (symbol,))
        existing = cursor.fetchone()
        opened_at = existing['opened_at'] if existing else None
        current_peak = existing['peak_price'] if existing else None
        current_trough = existing['trough_price'] if existing else None
        final_peak = peak_price if peak_price is not None else current_peak
        final_trough = trough_price if trough_price is not None else current_trough
        if side == 'long':
            anchor = float(current_price or entry_price)
            final_peak = max(float(final_peak or anchor), anchor)
            if final_trough is None:
                final_trough = entry_price
        else:
            anchor = float(current_price or entry_price)
            final_trough = min(float(final_trough or anchor), anchor)
            if final_peak is None:
                final_peak = entry_price
        
        cursor.execute("""
            INSERT OR REPLACE INTO positions 
            (id, symbol, side, entry_price, current_price, quantity, contract_size, coin_quantity, leverage,
             unrealized_pnl, peak_price, trough_price, opened_at, updated_at)
            VALUES ((SELECT id FROM positions WHERE symbol = ?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
        """, (symbol, symbol, side, entry_price, current_price, quantity, contract_size, coin_quantity, leverage, unrealized_pnl, final_peak, final_trough, opened_at))
        
        conn.commit()
        conn.close()
    
    def get_positions(self) -> List[Dict]:
        """获取当前持仓"""
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM positions", conn)
        conn.close()
        rows = df.to_dict('records')
        return [self._normalize_contract_fields(row) for row in rows]
    
    def close_position(self, symbol: str):
        """平仓(删除持仓记录)"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        conn.commit()
        conn.close()

    def repair_trade_quantity_mappings(self, symbols: List[str] = None, pnl_percent_abs_cap: float = 1000.0) -> Dict[str, Any]:
        conn = self._get_connection()
        cursor = conn.cursor()
        params = []
        query = "SELECT * FROM trades"
        if symbols:
            placeholders = ','.join(['?'] * len(symbols))
            query += f" WHERE symbol IN ({placeholders})"
            params.extend(symbols)
        updated = 0
        checked = 0
        samples = []
        for row in cursor.execute(query, params).fetchall():
            checked += 1
            current = dict(row)
            recalculated = self._recalculate_trade_metrics(current)
            existing_coin = self._safe_float(current.get('coin_quantity'))
            existing_pct = current.get('pnl_percent')
            existing_pct = None if existing_pct in (None, '') else self._safe_float(existing_pct)
            expected_coin = self._safe_float(recalculated.get('coin_quantity'))
            expected_pct = recalculated.get('pnl_percent')
            coin_mismatch = expected_coin > 0 and abs(existing_coin - expected_coin) > max(expected_coin * 0.001, 1e-8)
            pct_outlier = existing_pct is not None and abs(existing_pct) > pnl_percent_abs_cap
            pct_mismatch = expected_pct is not None and existing_pct is not None and abs(existing_pct - expected_pct) > 0.5
            if not (coin_mismatch or pct_outlier or pct_mismatch):
                continue
            cursor.execute(
                """
                UPDATE trades
                SET coin_quantity = ?,
                    contract_size = ?,
                    pnl_percent = ?
                WHERE id = ?
                """,
                (expected_coin, self._safe_float(recalculated.get('contract_size'), 1.0), expected_pct, current['id'])
            )
            updated += 1
            if len(samples) < 10:
                samples.append({
                    'id': current['id'],
                    'symbol': current.get('symbol'),
                    'coin_quantity_before': existing_coin,
                    'coin_quantity_after': expected_coin,
                    'pnl_percent_before': existing_pct,
                    'pnl_percent_after': expected_pct,
                })
        conn.commit()
        conn.close()
        return {'checked': checked, 'updated': updated, 'samples': samples}

    def sync_trade_with_exchange_snapshot(self, trade_id: int, *, quantity: float = None, contract_size: float = None, coin_quantity: float = None, leverage: int = None, entry_price: float = None, notes: str = None) -> bool:
        """用交易所持仓快照回写 open trade 的关键字段，避免沿用旧的本地错误杠杆/数量。"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        current = dict(row)
        payload = {
            'quantity': self._safe_float(quantity if quantity is not None else current.get('quantity')),
            'contract_size': self._safe_float(contract_size if contract_size is not None else current.get('contract_size'), 1.0) or 1.0,
            'coin_quantity': coin_quantity,
            'leverage': max(1, int(self._safe_float(leverage if leverage is not None else current.get('leverage'), 1) or 1)),
            'entry_price': self._safe_float(entry_price if entry_price is not None else current.get('entry_price')),
        }
        if coin_quantity is None:
            payload['coin_quantity'] = payload['quantity'] * payload['contract_size']
        else:
            payload['coin_quantity'] = self._safe_float(coin_quantity)
        existing_notes = current.get('notes') or ''
        if notes:
            payload['notes'] = f"{existing_notes} | {notes}" if existing_notes else notes
        else:
            payload['notes'] = existing_notes
        cursor.execute(
            """
            UPDATE trades
            SET entry_price = ?,
                quantity = ?,
                contract_size = ?,
                coin_quantity = ?,
                leverage = ?,
                notes = ?
            WHERE id = ? AND status = 'open'
            """,
            (payload['entry_price'], payload['quantity'], payload['contract_size'], payload['coin_quantity'], payload['leverage'], payload['notes'], trade_id)
        )
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    def remove_positions_not_in(self, symbols: List[str]) -> int:
        """删除不在指定 symbol 集合内的本地持仓"""
        conn = self._get_connection()
        cursor = conn.cursor()
        symbols = symbols or []
        if symbols:
            placeholders = ','.join(['?'] * len(symbols))
            cursor.execute(f"DELETE FROM positions WHERE symbol NOT IN ({placeholders})", symbols)
        else:
            cursor.execute("DELETE FROM positions")
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted
    
    # =========================================================================
    # 策略分析操作
    # =========================================================================
    
    def record_strategy_analysis(self, signal_id: int, strategy_name: str,
                                 triggered: bool, strength: int = 0,
                                 confidence: float = 0, action: str = None,
                                 details: str = None):
        """记录策略分析"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO strategy_analysis 
            (signal_id, strategy_name, triggered, strength, confidence, action, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (signal_id, strategy_name, triggered, strength, confidence, action, details))
        
        conn.commit()
        conn.close()
    
    def get_strategy_stats(self, days: int = 30) -> Dict:
        """获取策略统计"""
        conn = self._get_connection()
        
        query = """
            SELECT 
                strategy_name,
                COUNT(*) as total_signals,
                SUM(CASE WHEN triggered = 1 THEN 1 ELSE 0 END) as triggered_count,
                AVG(confidence) as avg_confidence
            FROM strategy_analysis
            WHERE created_at >= datetime('now', '-' || ? || ' days')
            GROUP BY strategy_name
        """
        
        df = pd.read_sql_query(query, conn, params=(days,))
        conn.close()
        return df.to_dict('records')
    
    # =========================================================================
    # 日志操作
    # =========================================================================
    
    def log(self, level: str, message: str, details: Dict = None):
        """记录日志"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO system_logs (level, message, details)
            VALUES (?, ?, ?)
        """, (level, message, json.dumps(details) if details else None))
        
        conn.commit()
        conn.close()
    
    # =========================================================================
    # 候选审查 / preset 历史
    # =========================================================================

    def record_candidate_review(self, symbol: str, decision: str, best_variant: str = None,
                                score: float = None, reason: str = None, details: Dict = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO candidate_reviews (symbol, decision, best_variant, score, reason, details)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (symbol, decision, best_variant, score, reason, json.dumps(details) if details else None))
        conn.commit()
        conn.close()

    def get_candidate_reviews(self, symbol: str = None, limit: int = 100) -> List[Dict]:
        conn = self._get_connection()
        query = "SELECT * FROM candidate_reviews"
        params = []
        if symbol:
            query += " WHERE symbol = ?"
            params.append(symbol)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        if not df.empty and 'details' in df.columns:
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def record_preset_history(self, preset_name: str, watch_list: List[str], backup_path: str = None, details: Dict = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO preset_history (preset_name, watch_list, backup_path, details)
            VALUES (?, ?, ?, ?)
        """, (preset_name, json.dumps(watch_list or []), backup_path, json.dumps(details) if details else None))
        conn.commit()
        conn.close()

    def get_preset_history(self, limit: int = 50) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM preset_history ORDER BY created_at DESC LIMIT ?", conn, params=(limit,))
        conn.close()
        if not df.empty:
            df['watch_list'] = df['watch_list'].apply(lambda x: json.loads(x) if x else [])
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def record_governance_decision(self, decision_type: str, level: str = None, approval_required: int = 0,
                                   recommended_preset: str = None, message: str = None, details: Dict = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        details_json = json.dumps(details) if details else None

        cursor.execute("""
            SELECT id, level, approval_required, recommended_preset, message, details
            FROM governance_decisions
            WHERE decision_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """, (decision_type,))
        last = cursor.fetchone()
        if last:
            same_as_latest = (
                (last['level'] or '') == (level or '') and
                int(last['approval_required'] or 0) == int(approval_required or 0) and
                (last['recommended_preset'] or '') == (recommended_preset or '') and
                (last['message'] or '') == (message or '') and
                (last['details'] or '') == (details_json or '')
            )
            if same_as_latest:
                conn.close()
                return False

        cursor.execute("""
            INSERT INTO governance_decisions (decision_type, level, approval_required, recommended_preset, message, details)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (decision_type, level, approval_required, recommended_preset, message, details_json))
        conn.commit()
        conn.close()
        return True

    def get_governance_decisions(self, limit: int = 50) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM governance_decisions ORDER BY created_at DESC LIMIT ?", conn, params=(limit,))
        conn.close()
        if not df.empty:
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def record_governance_config_change(self, config_key: str, before_value: Any = None, after_value: Any = None, details: Dict = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO governance_config_history (config_key, before_value, after_value, details)
            VALUES (?, ?, ?, ?)
            """,
            (
                config_key,
                json.dumps(before_value, ensure_ascii=False) if before_value is not None else None,
                json.dumps(after_value, ensure_ascii=False) if after_value is not None else None,
                json.dumps(details, ensure_ascii=False) if details else None,
            )
        )
        conn.commit()
        conn.close()

    def get_governance_config_history(self, limit: int = 50) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query(
            "SELECT * FROM governance_config_history ORDER BY created_at DESC, id DESC LIMIT ?",
            conn,
            params=(limit,)
        )
        conn.close()
        if not df.empty:
            for col in ['before_value', 'after_value', 'details']:
                df[col] = df[col].apply(lambda x: json.loads(x) if x else None)
        return df.to_dict('records')

    def record_daily_report(self, report_date: str, summary: Dict):
        conn = self._get_connection()
        cursor = conn.cursor()
        summary_json = json.dumps(summary)
        cursor.execute(
            "SELECT id, summary FROM daily_reports WHERE report_date = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (report_date,)
        )
        existing = cursor.fetchone()
        if existing:
            if (existing['summary'] or '') == summary_json:
                conn.close()
                return {'action': 'noop', 'id': existing['id']}
            cursor.execute(
                "UPDATE daily_reports SET summary = ?, created_at = CURRENT_TIMESTAMP WHERE id = ?",
                (summary_json, existing['id'])
            )
            conn.commit()
            conn.close()
            return {'action': 'updated', 'id': existing['id']}

        cursor.execute("INSERT INTO daily_reports (report_date, summary) VALUES (?, ?)", (report_date, summary_json))
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return {'action': 'inserted', 'id': row_id}

    def get_daily_reports(self, limit: int = 30) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM daily_reports ORDER BY created_at DESC LIMIT ?", conn, params=(limit,))
        conn.close()
        if not df.empty:
            df['summary'] = df['summary'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def get_latest_daily_report(self, report_date: str) -> Optional[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query(
            "SELECT * FROM daily_reports WHERE report_date = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            conn,
            params=(report_date,)
        )
        conn.close()
        if df.empty:
            return None
        row = df.iloc[0].to_dict()
        row['summary'] = json.loads(row['summary']) if row.get('summary') else {}
        return row

    def _safe_json_dict(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if value in (None, ''):
            return {}
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
            return dict(parsed or {}) if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _build_transition_journal_entry(self, *, item_id: str, approval_type: str, target: Optional[str] = None,
                                        title: Optional[str] = None, event_type: Optional[str] = None,
                                        previous_row: Optional[Dict[str, Any]] = None,
                                        decision: Optional[str] = None, state: Optional[str] = None,
                                        workflow_state: Optional[str] = None, reason: Optional[str] = None,
                                        actor: Optional[str] = None, source: Optional[str] = None,
                                        details: Optional[Dict[str, Any]] = None, created_at: Optional[str] = None) -> Dict[str, Any]:
        payload = self._safe_json_dict(details)
        previous = dict(previous_row or {})
        previous_details = self._safe_json_dict(previous.get('details'))
        normalized_decision = str(decision or 'pending').strip().lower() or 'pending'
        normalized_state = self._normalize_approval_state(state, normalized_decision)
        normalized_workflow = self._normalize_workflow_state(workflow_state, normalized_state) if workflow_state is not None else self._normalize_workflow_state(previous.get('workflow_state'), normalized_state)
        current_machine = ((payload.get('state_machine') or {}) if isinstance(payload.get('state_machine'), dict) else {})
        previous_machine = ((previous_details.get('state_machine') or {}) if isinstance(previous_details.get('state_machine'), dict) else {})
        current_execution = payload.get('execution_status') or current_machine.get('execution_status')
        previous_execution = previous_details.get('execution_status') or previous_machine.get('execution_status')
        current_stage = payload.get('target_rollout_stage') or payload.get('rollout_stage') or payload.get('current_rollout_stage') or current_machine.get('target_rollout_stage') or current_machine.get('rollout_stage')
        previous_stage = previous_details.get('target_rollout_stage') or previous_details.get('rollout_stage') or previous_details.get('current_rollout_stage') or previous_machine.get('target_rollout_stage') or previous_machine.get('rollout_stage')
        current_transition = self._safe_json_dict(payload.get('last_transition'))
        previous_transition = self._safe_json_dict(previous_details.get('last_transition'))
        trigger = payload.get('next_transition') or payload.get('transition_rule') or current_transition.get('next_transition') or current_transition.get('rule') or event_type
        changed_fields = []
        for field, before, after in (
            ('decision', previous.get('decision'), normalized_decision),
            ('state', previous.get('state'), normalized_state),
            ('workflow_state', previous.get('workflow_state'), normalized_workflow),
            ('execution_status', previous_execution, current_execution),
            ('rollout_stage', previous_stage, current_stage),
        ):
            if (before or None) != (after or None):
                changed_fields.append(field)
        return {
            'schema_version': 'm5_transition_journal_v1',
            'item_id': item_id,
            'approval_type': approval_type,
            'target': target,
            'title': title,
            'event_type': event_type,
            'trigger': trigger,
            'reason': reason,
            'actor': actor,
            'source': source,
            'timestamp': created_at,
            'changed': bool(changed_fields),
            'changed_fields': changed_fields,
            'from': {
                'decision': previous.get('decision'),
                'state': previous.get('state'),
                'workflow_state': previous.get('workflow_state'),
                'execution_status': previous_execution,
                'rollout_stage': previous_stage,
                'transition': previous_transition,
            },
            'to': {
                'decision': normalized_decision,
                'state': normalized_state,
                'workflow_state': normalized_workflow,
                'execution_status': current_execution,
                'rollout_stage': current_stage,
                'transition': current_transition,
            },
            'summary': f"{item_id}: {(previous.get('workflow_state') or previous.get('state') or 'new')} -> {(normalized_workflow or normalized_state)} ({event_type or 'transition'})",
        }

    def _is_terminal_approval_state(self, state: Optional[str]) -> bool:
        return str(state or '').strip().lower() in {'approved', 'rejected', 'deferred', 'expired'}

    def append_approval_event(self, item_id: str, approval_type: str, *, event_type: str,
                              target: str = None, title: str = None, decision: str = 'pending',
                              state: str = None, workflow_state: str = None, reason: str = None,
                              actor: str = None, source: str = None, details: Dict = None,
                              conn: sqlite3.Connection = None, previous_row: Dict = None) -> Dict:
        normalized_decision = str(decision or 'pending').strip().lower()
        normalized_state = self._normalize_approval_state(state, normalized_decision)
        workflow_state = self._normalize_workflow_state(workflow_state, normalized_state) if workflow_state is not None else workflow_state
        owns_conn = conn is None
        conn = conn or self._get_connection()
        cursor = conn.cursor()
        payload = dict(details or {})
        transition_journal = self._build_transition_journal_entry(
            item_id=item_id, approval_type=approval_type, target=target, title=title, event_type=event_type,
            previous_row=previous_row, decision=normalized_decision, state=normalized_state, workflow_state=workflow_state,
            reason=reason, actor=actor, source=source, details=payload, created_at=None,
        )
        payload['transition_journal'] = transition_journal
        cursor.execute(
            """
            INSERT INTO approval_events (item_id, approval_type, target, title, event_type, decision, state, workflow_state, reason, actor, source, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (item_id, approval_type, target, title, event_type, normalized_decision, normalized_state, workflow_state, reason, actor, source, json.dumps(payload, ensure_ascii=False))
        )
        event_id = cursor.lastrowid
        if owns_conn:
            conn.commit()
        cursor.execute("SELECT * FROM approval_events WHERE id = ? LIMIT 1", (event_id,))
        row = dict(cursor.fetchone())
        if owns_conn:
            conn.close()
        row['details'] = self._build_state_machine_details(item_id=row.get('item_id'), decision=row.get('decision'), state=row.get('state'), workflow_state=row.get('workflow_state'), details=self._safe_json_dict(row.get('details')))
        journal = self._safe_json_dict((row.get('details') or {}).get('transition_journal'))
        if journal:
            journal['timestamp'] = row.get('created_at')
            row['details']['transition_journal'] = journal
        return row

    def rebuild_approval_snapshot(self, item_id: str) -> Optional[Dict[str, Any]]:
        timeline = self.get_approval_timeline(item_id=item_id, limit=1000, ascending=True)
        if not timeline:
            return None
        snapshot = None
        terminal_locked = False
        for event in timeline:
            if snapshot is None:
                snapshot = {
                    'item_id': item_id,
                    'approval_type': event.get('approval_type'),
                    'target': event.get('target'),
                    'title': event.get('title'),
                    'decision': event.get('decision') or 'pending',
                    'state': event.get('state') or 'pending',
                    'workflow_state': event.get('workflow_state'),
                    'reason': event.get('reason'),
                    'actor': event.get('actor'),
                    'replay_source': event.get('source'),
                    'details': {},
                    'created_at': event.get('created_at'),
                    'updated_at': event.get('created_at'),
                    'last_seen_at': event.get('created_at'),
                }
            snapshot['approval_type'] = event.get('approval_type') or snapshot.get('approval_type')
            snapshot['target'] = event.get('target') if event.get('target') is not None else snapshot.get('target')
            snapshot['title'] = event.get('title') or snapshot.get('title')
            snapshot['details'] = {**(snapshot.get('details') or {}), **self._safe_json_dict(event.get('details'))}
            snapshot['last_seen_at'] = event.get('created_at') or snapshot.get('last_seen_at')
            if not terminal_locked:
                snapshot['decision'] = event.get('decision') or snapshot.get('decision')
                snapshot['state'] = self._normalize_approval_state(event.get('state'), event.get('decision'))
                snapshot['workflow_state'] = self._normalize_workflow_state(event.get('workflow_state') or snapshot.get('workflow_state'), snapshot.get('state'))
                snapshot['reason'] = event.get('reason') or snapshot.get('reason')
                snapshot['actor'] = event.get('actor') or snapshot.get('actor')
                snapshot['replay_source'] = event.get('source') or snapshot.get('replay_source')
                snapshot['updated_at'] = event.get('created_at') or snapshot.get('updated_at')
                if self._is_terminal_approval_state(snapshot.get('state')):
                    terminal_locked = True
        return snapshot

    def recover_approval_state(self, item_id: str) -> Optional[Dict[str, Any]]:
        snapshot = self.rebuild_approval_snapshot(item_id)
        if not snapshot:
            return None
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO approval_state (item_id, approval_type, target, title, decision, state, workflow_state, reason, actor, replay_source, details, created_at, updated_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                approval_type = excluded.approval_type,
                target = excluded.target,
                title = excluded.title,
                decision = excluded.decision,
                state = excluded.state,
                workflow_state = excluded.workflow_state,
                reason = excluded.reason,
                actor = excluded.actor,
                replay_source = excluded.replay_source,
                details = excluded.details,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at
            """,
            (snapshot['item_id'], snapshot.get('approval_type'), snapshot.get('target'), snapshot.get('title'), snapshot.get('decision'), snapshot.get('state'), snapshot.get('workflow_state'), snapshot.get('reason'), snapshot.get('actor'), snapshot.get('replay_source'), json.dumps(snapshot.get('details') or {}, ensure_ascii=False), snapshot.get('created_at'), snapshot.get('updated_at'), snapshot.get('last_seen_at'))
        )
        conn.commit()
        conn.close()
        return self.get_approval_state(item_id)

    def _normalize_approval_state(self, state: Optional[str], decision: Optional[str] = None) -> str:
        state_value = str(state or '').strip().lower()
        decision_value = str(decision or '').strip().lower()
        allowed = {'pending', 'approved', 'rejected', 'deferred', 'expired', 'blocked', 'ready', 'not_required', 'replayed'}
        if state_value in allowed:
            return state_value
        if decision_value in {'approved', 'rejected', 'deferred', 'expired'}:
            return decision_value
        return 'pending'

    def build_approval_item_id(self, approval_type: str, target: str = None, details: Dict = None) -> str:
        details = details or {}
        if details.get('item_id'):
            return str(details.get('item_id'))
        if details.get('approval_id'):
            return str(details.get('approval_id'))
        target_part = str(target or details.get('target') or '--')
        return f"{approval_type}::{target_part}"

    def upsert_approval_state(self, item_id: str, approval_type: str, target: str = None, title: str = None,
                              decision: str = 'pending', state: str = None, workflow_state: str = None,
                              reason: str = None, actor: str = None, replay_source: str = None,
                              details: Dict = None, preserve_terminal: bool = True,
                              event_type: str = 'snapshot_sync', append_event: bool = True) -> Dict:
        normalized_decision = str(decision or 'pending').strip().lower()
        normalized_state = self._normalize_approval_state(state, normalized_decision)
        workflow_state = self._normalize_workflow_state(workflow_state, normalized_state) if workflow_state is not None else workflow_state
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM approval_state WHERE item_id = ? LIMIT 1", (item_id,))
        existing = cursor.fetchone()
        existing_row = dict(existing) if existing else None
        if existing_row:
            existing_row['details'] = self._safe_json_dict(existing_row.get('details'))

        terminal_preserved = False
        if existing_row and preserve_terminal and self._is_terminal_approval_state(existing_row.get('state')) and normalized_state in {'pending', 'ready', 'replayed'}:
            normalized_state = existing_row.get('state')
            normalized_decision = existing_row.get('decision') or normalized_decision
            workflow_state = workflow_state or existing_row.get('workflow_state')
            reason = reason or existing_row.get('reason')
            actor = actor or existing_row.get('actor')
            terminal_preserved = True

        merged_details = dict(existing_row.get('details') or {}) if existing_row else {}
        if details:
            merged_details.update(details)
        merged_details = self._build_state_machine_details(item_id=item_id, decision=normalized_decision, state=normalized_state, workflow_state=workflow_state, details=merged_details)
        if terminal_preserved:
            merged_details.setdefault('terminal_preserved', True)

        if existing_row:
            cursor.execute(
                """
                UPDATE approval_state
                SET approval_type = ?,
                    target = ?,
                    title = ?,
                    decision = ?,
                    state = ?,
                    workflow_state = ?,
                    reason = ?,
                    actor = ?,
                    replay_source = ?,
                    details = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    last_seen_at = CURRENT_TIMESTAMP
                WHERE item_id = ?
                """,
                (approval_type, target, title, normalized_decision, normalized_state, workflow_state, reason, actor, replay_source, json.dumps(merged_details, ensure_ascii=False), item_id)
            )
        else:
            cursor.execute(
                """
                INSERT INTO approval_state (item_id, approval_type, target, title, decision, state, workflow_state, reason, actor, replay_source, details, created_at, updated_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (item_id, approval_type, target, title, normalized_decision, normalized_state, workflow_state, reason, actor, replay_source, json.dumps(merged_details, ensure_ascii=False))
            )

        if append_event:
            event_details = dict(merged_details)
            event_details.setdefault('snapshot_item_id', item_id)
            if terminal_preserved:
                event_details['terminal_preserved'] = True
            self.append_approval_event(
                item_id=item_id,
                approval_type=approval_type,
                target=target,
                title=title,
                event_type=event_type,
                decision=normalized_decision,
                state=normalized_state,
                workflow_state=workflow_state,
                reason=reason,
                actor=actor,
                source=replay_source,
                details=event_details,
                conn=conn,
                previous_row=existing_row,
            )

        conn.commit()
        cursor.execute("SELECT * FROM approval_state WHERE item_id = ? LIMIT 1", (item_id,))
        row = dict(cursor.fetchone())
        conn.close()
        row['details'] = self._safe_json_dict(row.get('details'))
        return row

    def record_approval(self, approval_type: str, target: str, decision: str, details: Dict = None):
        details = details or {}
        item_id = self.build_approval_item_id(approval_type, target, details)
        state = self._normalize_approval_state(details.get('state'), decision)
        self.upsert_approval_state(
            item_id=item_id,
            approval_type=approval_type,
            target=target,
            title=details.get('title') or details.get('message'),
            decision=decision,
            state=state,
            workflow_state=details.get('workflow_state'),
            reason=details.get('reason') or details.get('note'),
            actor=details.get('actor'),
            replay_source=details.get('replay_source') or 'manual_decision',
            details=details,
            preserve_terminal=False,
            event_type='decision_recorded',
            append_event=True,
        )
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO approval_history (approval_type, target, decision, details)
            VALUES (?, ?, ?, ?)
        """, (approval_type, target, decision, json.dumps(details, ensure_ascii=False) if details else None))
        conn.commit()
        conn.close()

    def sync_approval_items(self, items: List[Dict], replay_source: str = 'workflow_snapshot', preserve_terminal: bool = True) -> List[Dict]:
        synced = []
        for item in items or []:
            approval_type = str(item.get('approval_type') or item.get('action_type') or 'workflow_approval')
            target = item.get('target') or item.get('recommended_preset') or item.get('playbook_id')
            row = self.upsert_approval_state(
                item_id=str(item.get('item_id') or item.get('approval_id') or self.build_approval_item_id(approval_type, target, item)),
                approval_type=approval_type,
                target=target,
                title=item.get('title') or item.get('message'),
                decision=item.get('decision') or item.get('approval_state') or 'pending',
                state=item.get('state') or item.get('approval_state') or 'pending',
                workflow_state=item.get('workflow_state') or item.get('decision_state'),
                reason=item.get('reason'),
                actor=item.get('actor'),
                replay_source=item.get('replay_source') or replay_source,
                details=item,
                preserve_terminal=preserve_terminal,
            )
            synced.append(row)
        return synced

    def record_override_audit(self, action: str, target_file: str = None, backup_path: str = None,
                              symbols: List[str] = None, parameter_count: int = 0, note: str = None,
                              details: Dict = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO override_audit_history (action, target_file, backup_path, symbols, parameter_count, note, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action,
                target_file,
                backup_path,
                json.dumps(symbols or [], ensure_ascii=False),
                int(parameter_count or 0),
                note,
                json.dumps(details, ensure_ascii=False) if details else None,
            )
        )
        conn.commit()
        conn.close()

    def get_override_audit_history(self, limit: int = 50) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query(
            "SELECT * FROM override_audit_history ORDER BY created_at DESC, id DESC LIMIT ?",
            conn,
            params=(limit,)
        )
        conn.close()
        if not df.empty:
            df['symbols'] = df['symbols'].apply(lambda x: json.loads(x) if x else [])
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def get_approval_history(self, limit: int = 50) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM approval_history ORDER BY created_at DESC, id DESC LIMIT ?", conn, params=(limit,))
        conn.close()
        if not df.empty:
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def get_approval_timeline(self, item_id: str = None, approval_type: str = None, target: str = None,
                              limit: int = 100, ascending: bool = False) -> List[Dict]:
        conn = self._get_connection()
        query = "SELECT * FROM approval_events WHERE 1=1"
        params = []
        if item_id:
            query += " AND item_id = ?"
            params.append(item_id)
        if approval_type:
            query += " AND approval_type = ?"
            params.append(approval_type)
        if target is not None:
            query += " AND target = ?"
            params.append(target)
        order = "ASC" if ascending else "DESC"
        query += f" ORDER BY created_at {order}, id {order} LIMIT ?"
        params.append(limit)
        df = pd.read_sql_query(query, conn, params=tuple(params))
        conn.close()
        if not df.empty:
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return [self._attach_approval_event_metadata(row) for row in df.to_dict('records')]

    def get_stale_approval_states(self, stale_after_minutes: int = 60, approval_type: str = None,
                                  limit: int = 100) -> List[Dict]:
        stale_after_minutes = max(1, int(stale_after_minutes or 60))
        conn = self._get_connection()
        query = """
            SELECT *
            FROM approval_state
            WHERE state IN ('pending', 'ready', 'replayed')
              AND COALESCE(last_seen_at, updated_at, created_at) <= datetime('now', ?)
        """
        params = [f'-{stale_after_minutes} minutes']
        if approval_type:
            query += " AND approval_type = ?"
            params.append(approval_type)
        query += " ORDER BY COALESCE(last_seen_at, updated_at, created_at) ASC, item_id ASC LIMIT ?"
        params.append(limit)
        df = pd.read_sql_query(query, conn, params=tuple(params))
        conn.close()
        if df.empty:
            return []
        df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        rows = df.to_dict('records')
        now = datetime.utcnow()
        for row in rows:
            last_seen_raw = row.get('last_seen_at') or row.get('updated_at') or row.get('created_at')
            try:
                last_seen_dt = datetime.fromisoformat(str(last_seen_raw).replace('Z', '+00:00')) if last_seen_raw else None
            except Exception:
                last_seen_dt = None
            stale_minutes = int((now - last_seen_dt.replace(tzinfo=None)).total_seconds() // 60) if last_seen_dt else stale_after_minutes
            row['stale'] = True
            row['stale_after_minutes'] = stale_after_minutes
            row['stale_minutes'] = max(stale_minutes, stale_after_minutes)
        return rows

    def cleanup_stale_approval_states(self, stale_after_minutes: int = 60, approval_type: str = None,
                                      limit: int = 100, dry_run: bool = True,
                                      actor: str = 'system:stale-cleanup') -> Dict[str, Any]:
        stale_rows = self.get_stale_approval_states(stale_after_minutes=stale_after_minutes, approval_type=approval_type, limit=limit)
        result = {
            'dry_run': bool(dry_run),
            'stale_after_minutes': max(1, int(stale_after_minutes or 60)),
            'approval_type': approval_type,
            'matched_count': len(stale_rows),
            'expired_count': 0,
            'items': [],
        }
        for row in stale_rows:
            item = {
                'item_id': row.get('item_id'),
                'approval_type': row.get('approval_type'),
                'target': row.get('target'),
                'previous_state': row.get('state'),
                'previous_decision': row.get('decision'),
                'stale_minutes': row.get('stale_minutes'),
                'last_seen_at': row.get('last_seen_at') or row.get('updated_at') or row.get('created_at'),
                'action': 'would_expire' if dry_run else 'expired',
            }
            result['items'].append(item)
        if dry_run or not stale_rows:
            return result

        conn = self._get_connection()
        cursor = conn.cursor()
        for row in stale_rows:
            details = self._safe_json_dict(row.get('details'))
            details['stale_cleanup'] = {
                'expired_from_state': row.get('state'),
                'expired_from_decision': row.get('decision'),
                'stale_minutes': row.get('stale_minutes'),
                'stale_after_minutes': result['stale_after_minutes'],
                'expired_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            }
            cursor.execute(
                """
                UPDATE approval_state
                SET state = 'expired',
                    workflow_state = CASE WHEN workflow_state IN ('approved', 'rejected', 'deferred', 'expired') THEN workflow_state ELSE 'expired' END,
                    reason = COALESCE(reason, 'stale pending approval expired by cleanup'),
                    actor = ?,
                    replay_source = 'stale_cleanup',
                    details = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    last_seen_at = CURRENT_TIMESTAMP
                WHERE item_id = ?
                """,
                (actor, json.dumps(details, ensure_ascii=False), row.get('item_id'))
            )
            self.append_approval_event(
                item_id=row.get('item_id'),
                approval_type=row.get('approval_type'),
                target=row.get('target'),
                title=row.get('title'),
                event_type='stale_cleanup',
                decision=row.get('decision') or 'pending',
                state='expired',
                workflow_state='expired',
                reason='stale pending approval expired by cleanup',
                actor=actor,
                source='stale_cleanup',
                details=details,
                conn=conn,
            )
            result['expired_count'] += 1
        conn.commit()
        conn.close()
        return result

    def get_recent_transition_journal(self, limit: int = 20, approval_type: str = None, item_id: str = None, target: str = None, changed_only: bool = True) -> List[Dict]:
        timeline = self.get_approval_timeline(item_id=item_id, approval_type=approval_type, target=target, limit=max(20, int(limit or 20) * 10), ascending=False)
        rows = []
        for event in timeline:
            journal = self._safe_json_dict((event.get('details') or {}).get('transition_journal'))
            if not journal:
                journal = self._build_transition_journal_entry(
                    item_id=event.get('item_id'), approval_type=event.get('approval_type'), target=event.get('target'), title=event.get('title'),
                    event_type=event.get('event_type'), previous_row=None, decision=event.get('decision'), state=event.get('state'),
                    workflow_state=event.get('workflow_state'), reason=event.get('reason'), actor=event.get('actor'), source=event.get('source'),
                    details=event.get('details') or {}, created_at=event.get('created_at'),
                )
            journal['timestamp'] = journal.get('timestamp') or event.get('created_at')
            journal['event_id'] = event.get('id')
            journal['normalized_event_type'] = event.get('normalized_event_type')
            journal['provenance'] = event.get('provenance') or {}
            journal['timestamp_info'] = event.get('timestamp_info') or {}
            if changed_only and not journal.get('changed'):
                continue
            rows.append(journal)
            if len(rows) >= int(limit or 20):
                break
        return rows

    def get_transition_journal_summary(self, limit: int = 20, approval_type: str = None, item_id: str = None, target: str = None, changed_only: bool = True) -> Dict[str, Any]:
        items = self.get_recent_transition_journal(limit=limit, approval_type=approval_type, item_id=item_id, target=target, changed_only=changed_only)
        return {
            'count': len(items),
            'approval_type': approval_type,
            'item_id': item_id,
            'target': target,
            'changed_only': bool(changed_only),
            'latest_timestamp': items[0].get('timestamp') if items else None,
            'changed_field_counts': {field: sum(1 for row in items if field in (row.get('changed_fields') or [])) for field in sorted({field for row in items for field in (row.get('changed_fields') or [])})},
            'workflow_transition_counts': {f"{(row.get('from') or {}).get('workflow_state') or (row.get('from') or {}).get('state') or 'new'}->{(row.get('to') or {}).get('workflow_state') or (row.get('to') or {}).get('state') or 'unknown'}": sum(1 for item in items if f"{(item.get('from') or {}).get('workflow_state') or (item.get('from') or {}).get('state') or 'new'}->{(item.get('to') or {}).get('workflow_state') or (item.get('to') or {}).get('state') or 'unknown'}" == f"{(row.get('from') or {}).get('workflow_state') or (row.get('from') or {}).get('state') or 'new'}->{(row.get('to') or {}).get('workflow_state') or (row.get('to') or {}).get('state') or 'unknown'}") for row in items},
        }


    def get_auto_promotion_activity(self, limit: int = 20, item_id: str = None) -> List[Dict[str, Any]]:
        rows = self.get_approval_timeline(item_id=item_id, limit=max(1, int(limit or 20)), ascending=False)
        activity = []
        for row in rows:
            event_type = str(row.get('event_type') or '').strip().lower()
            if not event_type.startswith('controlled_rollout_'):
                continue
            details = row.get('details') or {}
            execution = details.get('auto_promotion_execution') or {}
            before = execution.get('before') or {}
            after = execution.get('after') or {}
            rollback_gate = details.get('rollback_gate') or {}
            activity.append({
                'item_id': row.get('item_id'),
                'approval_type': row.get('approval_type'),
                'target': row.get('target'),
                'title': row.get('title'),
                'event_type': row.get('event_type'),
                'created_at': row.get('created_at'),
                'actor': row.get('actor'),
                'source': row.get('source'),
                'reason': row.get('reason'),
                'before': before,
                'after': after,
                'reason_codes': execution.get('reason_codes') or [],
                'candidate_summary': execution.get('candidate_summary') or {},
                'rollback_hint': execution.get('rollback_hint') or details.get('rollback_hint'),
                'rollback_candidate': bool(rollback_gate.get('candidate')),
                'rollback_triggered': rollback_gate.get('triggered') or [],
            })
        return activity[:max(1, int(limit or 20))]

    def get_auto_promotion_activity_summary(self, limit: int = 20, item_id: str = None) -> Dict[str, Any]:
        items = self.get_auto_promotion_activity(limit=limit, item_id=item_id)
        stage_transition_counts: Dict[str, int] = {}
        target_stage_counts: Dict[str, int] = {}
        reason_code_counts: Dict[str, int] = {}
        rollback_trigger_counts: Dict[str, int] = {}
        risk_label_counts: Dict[str, int] = {}
        for row in items:
            before = row.get('before') or {}
            after = row.get('after') or {}
            transition_key = f"{before.get('rollout_stage') or 'unknown'}->{after.get('rollout_stage') or 'unknown'}"
            stage_transition_counts[transition_key] = stage_transition_counts.get(transition_key, 0) + 1
            target_stage = str(after.get('rollout_stage') or 'unknown')
            target_stage_counts[target_stage] = target_stage_counts.get(target_stage, 0) + 1
            risk_label = str((row.get('candidate_summary') or {}).get('risk_label') or 'unknown')
            risk_label_counts[risk_label] = risk_label_counts.get(risk_label, 0) + 1
            for code in row.get('reason_codes') or []:
                reason_code_counts[str(code)] = reason_code_counts.get(str(code), 0) + 1
            for trigger in row.get('rollback_triggered') or []:
                rollback_trigger_counts[str(trigger)] = rollback_trigger_counts.get(str(trigger), 0) + 1
        post_promotion_review_queue = []
        rollback_review_queue = []
        for row in items:
            review_due_at = ((row.get('details') or {}).get('review_due_at') if isinstance(row.get('details'), dict) else None) or row.get('review_due_at')
            observation_targets = ['post_apply_samples', 'validation_gate_health', 'transition_journal_drift']
            if row.get('rollback_candidate'):
                observation_targets.append('rollback_trigger_confirmation')
            queue_row = {
                'item_id': row.get('item_id'),
                'approval_type': row.get('approval_type'),
                'target': row.get('target'),
                'title': row.get('title'),
                'event_type': row.get('event_type'),
                'created_at': row.get('created_at'),
                'review_due_at': review_due_at,
                'observation_targets': observation_targets,
                'rollback_candidate': bool(row.get('rollback_candidate')),
                'rollback_triggered': row.get('rollback_triggered') or [],
                'recommended_action': 'prepare_rollback_review' if row.get('rollback_candidate') else ('run_scheduled_review' if review_due_at else 'monitor_post_promotion_window'),
            }
            if row.get('rollback_candidate'):
                rollback_review_queue.append({**queue_row, 'queue_kind': 'rollback_review_queue'})
            else:
                post_promotion_review_queue.append({**queue_row, 'queue_kind': 'post_promotion_review_queue'})
        rollback_candidates = [row for row in items if row.get('rollback_candidate')]
        return {
            'event_count': len(items),
            'item_id': item_id,
            'latest_created_at': items[0].get('created_at') if items else None,
            'stage_transition_counts': stage_transition_counts,
            'target_stage_counts': target_stage_counts,
            'reason_code_counts': reason_code_counts,
            'rollback_trigger_counts': rollback_trigger_counts,
            'risk_label_counts': risk_label_counts,
            'rollback_review_candidate_count': len(rollback_candidates),
            'post_promotion_review_queue_count': len(post_promotion_review_queue),
            'rollback_review_queue_count': len(rollback_review_queue),
            'recent_items': items,
            'rollback_review_candidates': rollback_candidates,
            'review_queues': {
                'post_promotion_review_queue': post_promotion_review_queue,
                'rollback_review_queue': rollback_review_queue,
            },
        }

    def get_recent_approval_decision_diff(self, limit: int = 20, approval_type: str = None) -> List[Dict]:
        query_limit = max(20, int(limit or 20) * 10)
        timeline = self.get_approval_timeline(approval_type=approval_type, limit=query_limit, ascending=True)
        diffs = []
        previous_by_item = {}
        for event in timeline:
            prev = previous_by_item.get(event.get('item_id'))
            current = {
                'decision': event.get('decision') or 'pending',
                'state': event.get('state') or 'pending',
                'workflow_state': event.get('workflow_state') or 'pending',
                'reason': event.get('reason'),
                'actor': event.get('actor'),
                'event_type': event.get('event_type'),
                'created_at': event.get('created_at'),
            }
            if prev:
                changed_fields = []
                for field in ('decision', 'state', 'workflow_state', 'reason'):
                    if (prev.get(field) or None) != (current.get(field) or None):
                        changed_fields.append(field)
                if changed_fields:
                    diffs.append({
                        'item_id': event.get('item_id'),
                        'approval_type': event.get('approval_type'),
                        'target': event.get('target'),
                        'title': event.get('title'),
                        'event_type': event.get('event_type'),
                        'changed_at': event.get('created_at'),
                        'actor': event.get('actor'),
                        'changed_fields': changed_fields,
                        'from': prev,
                        'to': current,
                        'summary': f"{event.get('item_id')}: {prev.get('state')} -> {current.get('state')} ({event.get('event_type')})",
                    })
            previous_by_item[event.get('item_id')] = current
        return list(reversed(diffs[-int(limit or 20):]))

    def get_approval_timeline_summary(self, item_id: str) -> Optional[Dict[str, Any]]:
        timeline = self.get_approval_timeline(item_id=item_id, limit=1000, ascending=True)
        if not timeline:
            return None
        state_row = self.get_approval_state(item_id) or self.rebuild_approval_snapshot(item_id) or {}
        first_event = timeline[0]
        last_event = timeline[-1]
        state_counts: Dict[str, int] = {}
        event_counts: Dict[str, int] = {}
        normalized_event_types: List[str] = []
        provenance_origins: List[str] = []
        provenance_sources: List[str] = []
        timestamp_sources: List[str] = []
        timestamp_phases: List[str] = []
        decision_path = []
        last_decision_key = None
        for event in timeline:
            state_value = event.get('state') or 'pending'
            event_type = event.get('event_type') or 'unknown'
            normalized_event_type = event.get('normalized_event_type') or self._normalize_event_type(event_type, 'approval')
            state_counts[state_value] = state_counts.get(state_value, 0) + 1
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
            if normalized_event_type not in normalized_event_types:
                normalized_event_types.append(normalized_event_type)
            provenance = event.get('provenance') or {}
            for value, bucket in ((provenance.get('origin'), provenance_origins), (provenance.get('source'), provenance_sources)):
                if value and value not in bucket:
                    bucket.append(value)
            timestamp_info = event.get('timestamp_info') or {}
            for value, bucket in ((timestamp_info.get('source'), timestamp_sources), (timestamp_info.get('phase'), timestamp_phases)):
                if value and value not in bucket:
                    bucket.append(value)
            decision_key = (event.get('decision') or 'pending', state_value, event.get('workflow_state') or 'pending')
            if decision_key != last_decision_key:
                decision_path.append({
                    'decision': decision_key[0],
                    'state': decision_key[1],
                    'workflow_state': decision_key[2],
                    'event_type': event_type,
                    'normalized_event_type': normalized_event_type,
                    'created_at': event.get('created_at'),
                    'timestamp': event.get('timestamp'),
                    'timestamp_info': event.get('timestamp_info') or {},
                    'provenance': provenance,
                    'actor': event.get('actor'),
                    'reason': event.get('reason'),
                })
                last_decision_key = decision_key
        stale_minutes = None
        stale = False
        if state_row.get('state') in {'pending', 'ready', 'replayed'}:
            stale_rows = self.get_stale_approval_states(stale_after_minutes=60, limit=1000)
            stale_lookup = {row.get('item_id'): row for row in stale_rows}
            stale_row = stale_lookup.get(item_id)
            if stale_row:
                stale = True
                stale_minutes = stale_row.get('stale_minutes')
        state_machine = ((state_row.get('details') or {}).get('state_machine') if isinstance(state_row.get('details'), dict) else {}) or {}
        execution_timeline = (state_machine.get('execution_timeline') or {}) if isinstance(state_machine, dict) else {}
        recovery_policy = (state_machine.get('recovery_policy') or {}) if isinstance(state_machine, dict) else {}
        summary_line = f"{state_row.get('approval_type') or first_event.get('approval_type')}::{state_row.get('target') or first_event.get('target') or '--'} | {state_row.get('state') or last_event.get('state')} | {len(timeline)} events"
        if execution_timeline.get('latest_status'):
            summary_line += f" | exec={execution_timeline.get('latest_status')}"
        if recovery_policy.get('policy'):
            summary_line += f" | recovery={recovery_policy.get('policy')}"
        if stale:
            summary_line += f" | stale {stale_minutes}m"
        return {
            'item_id': item_id,
            'approval_type': state_row.get('approval_type') or first_event.get('approval_type'),
            'target': state_row.get('target') or first_event.get('target'),
            'title': state_row.get('title') or first_event.get('title'),
            'current': state_row,
            'summary_line': summary_line,
            'first_seen_at': first_event.get('created_at'),
            'last_seen_at': last_event.get('created_at'),
            'event_count': len(timeline),
            'state_counts': state_counts,
            'event_counts': event_counts,
            'normalized_event_types': normalized_event_types,
            'provenance_origins': provenance_origins,
            'provenance_sources': provenance_sources,
            'timestamp_sources': timestamp_sources,
            'timestamp_phases': timestamp_phases,
            'decision_path': decision_path,
            'execution_timeline': execution_timeline,
            'recovery_policy': recovery_policy,
            'stale': stale,
            'stale_minutes': stale_minutes,
            'latest_reason': state_row.get('reason') or last_event.get('reason'),
            'latest_actor': state_row.get('actor') or last_event.get('actor'),
            'timeline_preview': timeline[-5:],
        }

    def get_approval_states(self, state: str = None, approval_type: str = None, limit: int = 100) -> List[Dict]:
        conn = self._get_connection()
        query = "SELECT * FROM approval_state WHERE 1=1"
        params = []
        if state:
            query += " AND state = ?"
            params.append(state)
        if approval_type:
            query += " AND approval_type = ?"
            params.append(approval_type)
        query += " ORDER BY updated_at DESC, item_id ASC LIMIT ?"
        params.append(limit)
        df = pd.read_sql_query(query, conn, params=tuple(params))
        conn.close()
        if not df.empty:
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def get_approval_state(self, item_id: str) -> Optional[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM approval_state WHERE item_id = ? LIMIT 1", conn, params=(item_id,))
        conn.close()
        if df.empty:
            return None
        row = df.iloc[0].to_dict()
        row['details'] = self._build_state_machine_details(item_id=row.get('item_id'), decision=row.get('decision'), state=row.get('state'), workflow_state=row.get('workflow_state'), details=(json.loads(row['details']) if row.get('details') else {}))
        return row

    def get_latest_approval(self, approval_type: str, target: str = None) -> Optional[Dict]:
        item_id = self.build_approval_item_id(approval_type, target)
        state_row = self.get_approval_state(item_id)
        if state_row:
            return {
                **state_row,
                'created_at': state_row.get('updated_at') or state_row.get('created_at'),
            }
        conn = self._get_connection()
        query = "SELECT * FROM approval_history WHERE approval_type = ?"
        params = [approval_type]
        if target is not None:
            query += " AND target = ?"
            params.append(target)
        query += " ORDER BY created_at DESC, id DESC LIMIT 1"
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        if df.empty:
            return None
        row = df.iloc[0].to_dict()
        row['details'] = json.loads(row['details']) if row.get('details') else {}
        return row

    def get_risk_guard_state(self, guard_key: str = 'loss_streak') -> Dict:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM risk_guard_state WHERE guard_key = ? LIMIT 1", conn, params=(guard_key,))
        conn.close()
        if df.empty:
            return {
                'guard_key': guard_key,
                'current_streak': 0,
                'lock_active': 0,
                'lock_until': None,
                'triggered_at': None,
                'reset_at': None,
                'last_trade_id': 0,
                'details': {},
            }
        row = df.iloc[0].to_dict()
        row['details'] = json.loads(row['details']) if row.get('details') else {}
        return row

    def save_risk_guard_state(self, state: Dict):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO risk_guard_state (guard_key, current_streak, lock_active, lock_until, triggered_at, reset_at, last_trade_id, details, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                state.get('guard_key', 'loss_streak'),
                int(state.get('current_streak', 0) or 0),
                int(bool(state.get('lock_active', 0))),
                state.get('lock_until'),
                state.get('triggered_at'),
                state.get('reset_at'),
                int(state.get('last_trade_id', 0) or 0),
                json.dumps(state.get('details', {}), ensure_ascii=False) if state.get('details') is not None else None,
            )
        )
        conn.commit()
        conn.close()

    def record_smoke_run(self, exchange_mode: str, position_mode: str, symbol: str, side: str,
                         amount: float = None, success: bool = False, error: str = None, details: Dict = None) -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO smoke_runs (exchange_mode, position_mode, symbol, side, amount, success, error, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exchange_mode,
                position_mode,
                symbol,
                side,
                amount,
                int(bool(success)),
                error,
                json.dumps(details, ensure_ascii=False) if details else None,
            )
        )
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return row_id

    def get_smoke_runs(self, limit: int = 20) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM smoke_runs ORDER BY created_at DESC, id DESC LIMIT ?", conn, params=(limit,))
        conn.close()
        if not df.empty:
            df['success'] = df['success'].astype(bool)
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def update_smoke_run_details(self, smoke_run_id: int, details: Dict):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE smoke_runs SET details = ? WHERE id = ?", (json.dumps(details, ensure_ascii=False), smoke_run_id))
        conn.commit()
        conn.close()

    def get_latest_smoke_run(self) -> Optional[Dict]:
        rows = self.get_smoke_runs(limit=1)
        return rows[0] if rows else None

    def enqueue_notification(self, channel: str, event_type: str, title: str, message: str, details: Dict = None) -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO notification_outbox (channel, event_type, title, message, details) VALUES (?, ?, ?, ?, ?)",
            (channel, event_type, title, message, json.dumps(details, ensure_ascii=False) if details else None)
        )
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return row_id

    def update_notification_outbox(self, notification_id: int, status: str, details: Dict = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        fields = ["status = ?"]
        params = [status]
        if details is not None:
            fields.append("details = ?")
            params.append(json.dumps(details, ensure_ascii=False))
        if status == 'delivered':
            fields.append("delivered_at = CURRENT_TIMESTAMP")
        params.append(notification_id)
        cursor.execute(f"UPDATE notification_outbox SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        conn.close()

    def get_notification_outbox(self, status: str = 'pending', limit: int = 50) -> List[Dict]:
        conn = self._get_connection()
        if status == 'all':
            df = pd.read_sql_query("SELECT * FROM notification_outbox ORDER BY created_at ASC, id ASC LIMIT ?", conn, params=(limit,))
        else:
            df = pd.read_sql_query("SELECT * FROM notification_outbox WHERE status = ? ORDER BY created_at ASC, id ASC LIMIT ?", conn, params=(status, limit))
        conn.close()
        if not df.empty:
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def get_notification_outbox_stats(self) -> Dict:
        conn = self._get_connection()
        cursor = conn.cursor()
        stats = {}
        for status in ['delivered', 'pending', 'suppressed', 'disabled']:
            cursor.execute("SELECT COUNT(*) FROM notification_outbox WHERE status = ?", (status,))
            stats[status] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM notification_outbox")
        stats['total'] = cursor.fetchone()[0]
        cursor.execute("SELECT MIN(created_at) FROM notification_outbox WHERE status = 'pending'")
        oldest_pending = cursor.fetchone()[0]
        cursor.execute("SELECT event_type, COUNT(*) AS count FROM notification_outbox WHERE status = 'pending' GROUP BY event_type ORDER BY count DESC LIMIT 3")
        top_pending = [{'event_type': row[0], 'count': row[1]} for row in cursor.fetchall()]
        cursor.execute("SELECT status, COUNT(*) AS count FROM notification_outbox WHERE status IN ('pending','suppressed','disabled') GROUP BY status ORDER BY count DESC")
        failure_breakdown = [{'status': row[0], 'count': row[1]} for row in cursor.fetchall()]
        rows = self.get_notification_outbox(status='all', limit=50)
        recent_failure_rows = [row for row in rows if row.get('status') in {'pending', 'suppressed', 'disabled'}]
        reason_counts = {}
        event_counts = {}
        for row in recent_failure_rows:
            event_type = row.get('event_type') or '--'
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
            details = row.get('details') or {}
            reason = details.get('aggregate_summary') or details.get('reason') or details.get('delivery', {}).get('path') or row.get('status') or '--'
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        top_failure_events = sorted([{'event_type': k, 'count': v} for k, v in event_counts.items()], key=lambda x: x['count'], reverse=True)[:3]
        top_failure_reasons = sorted([{'reason': k, 'count': v} for k, v in reason_counts.items()], key=lambda x: x['count'], reverse=True)[:3]
        conn.close()
        stats['oldest_pending_at'] = oldest_pending
        stats['top_pending_types'] = top_pending
        stats['failure_breakdown'] = failure_breakdown
        stats['top_failure_events'] = top_failure_events
        stats['top_failure_reasons'] = top_failure_reasons
        return stats

    def mark_notification_delivered(self, notification_id: int, status: str = 'delivered'):
        self.update_notification_outbox(notification_id, status=status)

    # =========================================================================
    # Partial TP 历史操作
    # =========================================================================

    def record_partial_tp(self, trade_id: int, symbol: str, side: str,
                         trigger_price: float, close_ratio: float,
                         close_quantity: float, pnl: float = None,
                         note: str = None) -> int:
        """记录 partial TP 触发历史"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO partial_tp_history (trade_id, symbol, side, trigger_price, close_ratio, close_quantity, pnl, note, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'partial_tp')
        """, (trade_id, symbol, side, trigger_price, close_ratio, close_quantity, pnl, note))
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return row_id

    def get_partial_tp_history(self, symbol: str = None, limit: int = 100) -> List[Dict]:
        """获取 partial TP 触发历史"""
        conn = self._get_connection()
        query = "SELECT * FROM partial_tp_history"
        conditions = []
        params = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        return df.to_dict('records')

    # =========================================================================
    # 开仓 intent / 方向锁 / 分仓计划
    # =========================================================================

    def get_trade_by_signal_id(self, signal_id: int) -> Optional[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM trades WHERE signal_id = ? ORDER BY id DESC LIMIT 1", conn, params=(signal_id,))
        conn.close()
        if df.empty:
            return None
        row = df.iloc[0].to_dict()
        if row.get('plan_context'):
            try:
                row['plan_context'] = json.loads(row['plan_context'])
            except Exception:
                pass
        return self._recalculate_trade_metrics(row)

    def _normalize_final_execution_permit(self, permit: Any) -> Dict[str, Any]:
        payload = self._safe_json_dict(permit)
        if not payload:
            return {}
        guardrail = self._safe_json_dict(payload.get('guardrail_evidence'))
        payload['allowed'] = bool(payload.get('allowed', False))
        payload['status'] = payload.get('status') or ('permit' if payload['allowed'] else 'deny')
        payload['reason_code'] = normalize_reason_code(payload.get('reason_code') or ('PERMIT_FINAL_EXECUTION_GRANTED' if payload['allowed'] else 'FINAL_EXECUTION_PERMIT_UNKNOWN'))
        payload.update(build_reason_code_details(payload.get('reason_code')))
        reason_codes = merge_reason_codes(
            [payload.get('legacy_reason_code')],
            self._safe_json_dict(guardrail.get('close_outcome_guard')).get('reason_codes') or [],
            primary=payload.get('reason_code'),
        )
        payload['reason_codes'] = reason_codes
        payload['guardrail_evidence'] = guardrail
        payload['diagnose_replay'] = {
            'schema_version': 'final_execution_permit_replay_v1',
            'status': payload['status'],
            'allowed': payload['allowed'],
            'reason_code': payload['reason_code'],
            'legacy_reason_code': payload.get('legacy_reason_code'),
            'reason_code_family': payload.get('reason_code_family'),
            'reason_code_stage': payload.get('reason_code_stage'),
            'reason_code_disposition': payload.get('reason_code_disposition'),
            'reason_codes': reason_codes,
            'exchange_mode': payload.get('exchange_mode'),
            'action': payload.get('action'),
            'selected_for_execution': bool(payload.get('selected_for_execution', False)),
            'testnet_only': bool(payload.get('testnet_only', True)),
            'guardrail_evidence': guardrail,
        }
        return payload

    def create_open_intent(self, *, symbol: str, side: str, signal_id: int = None, root_signal_id: int = None,
                           planned_margin: float = 0.0, leverage: int = 1, layer_no: int = None,
                           plan_context: Dict = None, notes: str = None, status: str = 'pending') -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO open_intents (signal_id, root_signal_id, symbol, side, status, planned_margin, leverage, layer_no, plan_context, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (signal_id, root_signal_id, symbol, side, status, planned_margin, leverage, layer_no,
             json.dumps(plan_context, ensure_ascii=False) if plan_context is not None else None, notes)
        )
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return row_id

    def update_open_intent(self, intent_id: int, **kwargs):
        conn = self._get_connection()
        cursor = conn.cursor()
        fields = []
        values = []
        for k, v in kwargs.items():
            if k == 'plan_context' and v is not None:
                v = json.dumps(v, ensure_ascii=False)
            fields.append(f"{k} = ?")
            values.append(v)
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(intent_id)
        cursor.execute(f"UPDATE open_intents SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        conn.close()

    def delete_open_intent(self, intent_id: int):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM open_intents WHERE id = ?", (intent_id,))
        conn.commit()
        conn.close()

    def get_open_intent_by_signal_id(self, signal_id: int, active_only: bool = True) -> Optional[Dict]:
        if signal_id is None:
            return None
        conn = self._get_connection()
        query = "SELECT * FROM open_intents WHERE signal_id = ?"
        params = [signal_id]
        if active_only:
            query += " AND status IN ('pending','submitted')"
        query += " ORDER BY id DESC LIMIT 1"
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        if df.empty:
            return None
        row = df.iloc[0].to_dict()
        if row.get('plan_context'):
            row['plan_context'] = json.loads(row['plan_context'])
        permit = self._normalize_final_execution_permit((row.get('plan_context') or {}).get('final_execution_permit'))
        row['final_execution_permit'] = permit
        row['final_execution_reason_code'] = permit.get('reason_code') if permit else None
        row['final_execution_allowed'] = bool(permit.get('allowed', False)) if permit else None
        return row

    def get_active_open_intents(self, symbol: str = None, side: str = None) -> List[Dict]:
        conn = self._get_connection()
        query = "SELECT * FROM open_intents WHERE status IN ('pending','submitted')"
        params = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if side:
            query += " AND side = ?"
            params.append(side)
        query += " ORDER BY created_at ASC, id ASC"
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        if not df.empty and 'plan_context' in df.columns:
            df['plan_context'] = df['plan_context'].apply(lambda x: json.loads(x) if x else {})
        rows = df.to_dict('records')
        for row in rows:
            permit = self._normalize_final_execution_permit((row.get('plan_context') or {}).get('final_execution_permit'))
            row['final_execution_permit'] = permit
            row['final_execution_reason_code'] = permit.get('reason_code') if permit else None
            row['final_execution_allowed'] = bool(permit.get('allowed', False)) if permit else None
        return rows

    def get_direction_lock(self, symbol: str, side: str) -> Optional[Dict]:
        conn = self._get_connection()
        key = f"{symbol}::{side}"
        df = pd.read_sql_query("SELECT * FROM direction_locks WHERE lock_key = ? LIMIT 1", conn, params=(key,))
        conn.close()
        return None if df.empty else df.iloc[0].to_dict()

    def acquire_direction_lock(self, symbol: str, side: str, owner: str = None) -> bool:
        conn = self._get_connection()
        cursor = conn.cursor()
        key = f"{symbol}::{side}"
        try:
            cursor.execute(
                "INSERT INTO direction_locks (lock_key, symbol, side, owner, updated_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (key, symbol, side, owner)
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

    def release_direction_lock(self, symbol: str, side: str, owner: str = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        key = f"{symbol}::{side}"
        if owner:
            cursor.execute("DELETE FROM direction_locks WHERE lock_key = ? AND (owner = ? OR owner IS NULL)", (key, owner))
        else:
            cursor.execute("DELETE FROM direction_locks WHERE lock_key = ?", (key,))
        conn.commit()
        conn.close()

    def get_layer_plan_state(self, symbol: str, side: str) -> Dict:
        conn = self._get_connection()
        key = f"{symbol}::{side}"
        df = pd.read_sql_query("SELECT * FROM layer_plan_states WHERE plan_key = ? LIMIT 1", conn, params=(key,))
        conn.close()
        if df.empty:
            return {
                'plan_key': key,
                'symbol': symbol,
                'side': side,
                'status': 'idle',
                'current_layer': 0,
                'root_signal_id': None,
                'plan_data': {'filled_layers': [], 'pending_layers': [], 'layer_ratios': [0.06, 0.06, 0.04], 'max_total_ratio': 0.16, 'last_filled_at': None, 'last_signal_id': None, 'signal_layer_counts': {}, 'signal_bar_markers': {}},
            }
        row = df.iloc[0].to_dict()
        row['plan_data'] = json.loads(row['plan_data']) if row.get('plan_data') else {}
        return row

    def save_layer_plan_state(self, symbol: str, side: str, *, status: str = 'idle', current_layer: int = 0,
                              root_signal_id: int = None, plan_data: Dict = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        key = f"{symbol}::{side}"
        cursor.execute(
            """
            INSERT OR REPLACE INTO layer_plan_states (plan_key, symbol, side, status, current_layer, root_signal_id, plan_data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM layer_plan_states WHERE plan_key = ?), CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
            """,
            (key, symbol, side, status, current_layer, root_signal_id, json.dumps(plan_data or {}, ensure_ascii=False), key)
        )
        conn.commit()
        conn.close()

    def sync_layer_plan_state(self, symbol: str, side: str, *, root_signal_id: int = None, reset_if_flat: bool = False) -> Dict:
        """根据当前 open trades / intents / positions 回写 layer plan 状态。"""
        conn = self._get_connection()
        df_trades = pd.read_sql_query(
            "SELECT id, signal_id, root_signal_id, layer_no, plan_context FROM trades WHERE status = 'open' AND symbol = ? AND side = ? ORDER BY id ASC",
            conn, params=(symbol, side)
        )
        df_intents = pd.read_sql_query(
            "SELECT id, signal_id, root_signal_id, layer_no, plan_context FROM open_intents WHERE status IN ('pending','submitted') AND symbol = ? AND side = ? ORDER BY id ASC",
            conn, params=(symbol, side)
        )
        df_positions = pd.read_sql_query(
            "SELECT symbol FROM positions WHERE symbol = ? AND side = ? LIMIT 1",
            conn, params=(symbol, side)
        )
        conn.close()

        state = self.get_layer_plan_state(symbol, side)
        plan_data = dict(state.get('plan_data') or {})
        existing_ratios = plan_data.get('layer_ratios') or [0.06, 0.06, 0.04]
        existing_cap = float(plan_data.get('max_total_ratio') or sum(existing_ratios) or 0.16)

        filled_layers = sorted({int(x) for x in df_trades['layer_no'].tolist() if pd.notna(x) and int(x) > 0}) if not df_trades.empty else []
        pending_layers = sorted({int(x) for x in df_intents['layer_no'].tolist() if pd.notna(x) and int(x) > 0}) if not df_intents.empty else []
        pending_layers = [x for x in pending_layers if x not in filled_layers]
        has_position = not df_positions.empty

        inferred_root_signal_id = root_signal_id
        if inferred_root_signal_id is None:
            for frame in (df_trades, df_intents):
                if frame.empty:
                    continue
                for col in ('root_signal_id', 'signal_id'):
                    if col in frame.columns:
                        values = [int(v) for v in frame[col].tolist() if pd.notna(v)]
                        if values:
                            inferred_root_signal_id = values[-1]
                            break
                if inferred_root_signal_id is not None:
                    break
        if inferred_root_signal_id is None:
            inferred_root_signal_id = state.get('root_signal_id')

        if reset_if_flat and not filled_layers and not pending_layers and not has_position:
            plan_data = {
                'filled_layers': [],
                'pending_layers': [],
                'layer_ratios': existing_ratios,
                'max_total_ratio': existing_cap,
                'last_reset_at': datetime.now().isoformat(timespec='seconds'),
            }
            self.save_layer_plan_state(symbol, side, status='idle', current_layer=0, root_signal_id=None, plan_data=plan_data)
            return self.get_layer_plan_state(symbol, side)

        plan_data['filled_layers'] = filled_layers
        plan_data['pending_layers'] = pending_layers
        plan_data['layer_ratios'] = existing_ratios
        plan_data['max_total_ratio'] = existing_cap
        plan_data['has_position'] = bool(has_position)
        status = 'active' if filled_layers or has_position else ('pending' if pending_layers else 'idle')
        current_layer = max(filled_layers or [0])
        self.save_layer_plan_state(symbol, side, status=status, current_layer=current_layer, root_signal_id=inferred_root_signal_id, plan_data=plan_data)
        return self.get_layer_plan_state(symbol, side)

    def cleanup_orphan_execution_state(self, stale_after_minutes: int = 15) -> Dict:
        """自愈执行态：清理孤儿 intent、陈旧方向锁，并把 layer plan 回写到真实仓位快照。"""
        conn = self._get_connection()
        cursor = conn.cursor()
        stale_expr = f"-{int(max(1, stale_after_minutes))} minutes"
        stale_intents = cursor.execute(
            """
            SELECT id, symbol, side, signal_id, root_signal_id, layer_no, notes FROM open_intents
            WHERE status IN ('pending','submitted')
              AND updated_at < datetime('now', ?)
            """,
            (stale_expr,)
        ).fetchall()
        removed_intents = []
        healed_intents = []
        removed_locks = []
        healed_locks = []
        plan_resets = []
        touched_keys = set()

        for row in stale_intents:
            symbol = row['symbol']
            side = row['side']
            has_trade = cursor.execute("SELECT 1 FROM trades WHERE status = 'open' AND symbol = ? AND side = ? LIMIT 1", (symbol, side)).fetchone()
            has_pos = cursor.execute("SELECT 1 FROM positions WHERE symbol = ? AND side = ? LIMIT 1", (symbol, side)).fetchone()
            if has_trade or has_pos:
                cursor.execute("DELETE FROM open_intents WHERE id = ?", (row['id'],))
                healed_intents.append({
                    'id': row['id'], 'symbol': symbol, 'side': side,
                    'signal_id': row['signal_id'], 'root_signal_id': row['root_signal_id'], 'layer_no': row['layer_no'],
                    'healed_by': 'live_position_or_trade', 'notes': row['notes'],
                })
                touched_keys.add((symbol, side))
                continue
            cursor.execute("DELETE FROM open_intents WHERE id = ?", (row['id'],))
            removed_intents.append({'id': row['id'], 'symbol': symbol, 'side': side, 'signal_id': row['signal_id'], 'root_signal_id': row['root_signal_id'], 'layer_no': row['layer_no']})
            touched_keys.add((symbol, side))

        stale_locks = cursor.execute(
            """
            SELECT lock_key, symbol, side, owner FROM direction_locks
            WHERE updated_at < datetime('now', ?)
            """,
            (stale_expr,)
        ).fetchall()
        for row in stale_locks:
            symbol = row['symbol']
            side = row['side']
            has_trade = cursor.execute("SELECT 1 FROM trades WHERE status = 'open' AND symbol = ? AND side = ? LIMIT 1", (symbol, side)).fetchone()
            has_pos = cursor.execute("SELECT 1 FROM positions WHERE symbol = ? AND side = ? LIMIT 1", (symbol, side)).fetchone()
            has_intent = cursor.execute("SELECT 1 FROM open_intents WHERE status IN ('pending','submitted') AND symbol = ? AND side = ? LIMIT 1", (symbol, side)).fetchone()
            if has_intent:
                continue
            cursor.execute("DELETE FROM direction_locks WHERE lock_key = ?", (row['lock_key'],))
            record = {'lock_key': row['lock_key'], 'symbol': symbol, 'side': side, 'owner': row['owner']}
            if has_trade or has_pos:
                record['healed_by'] = 'stale_lock_without_active_intent'
                healed_locks.append(record)
            else:
                removed_locks.append(record)
            touched_keys.add((symbol, side))

        candidate_states = cursor.execute(
            """
            SELECT plan_key, symbol, side, status, current_layer, root_signal_id, plan_data
            FROM layer_plan_states
            WHERE updated_at < datetime('now', ?)
               OR status != 'idle'
               OR current_layer != 0
            """,
            (stale_expr,)
        ).fetchall()
        for row in candidate_states:
            symbol = row['symbol']
            side = row['side']
            has_trade = cursor.execute("SELECT 1 FROM trades WHERE status = 'open' AND symbol = ? AND side = ? LIMIT 1", (symbol, side)).fetchone()
            has_pos = cursor.execute("SELECT 1 FROM positions WHERE symbol = ? AND side = ? LIMIT 1", (symbol, side)).fetchone()
            has_intent = cursor.execute("SELECT 1 FROM open_intents WHERE status IN ('pending','submitted') AND symbol = ? AND side = ? LIMIT 1", (symbol, side)).fetchone()
            if has_trade or has_pos or has_intent:
                touched_keys.add((symbol, side))
                continue
            try:
                plan_data = json.loads(row['plan_data']) if row['plan_data'] else {}
            except Exception:
                plan_data = {}
            if row['status'] == 'idle' and int(row['current_layer'] or 0) == 0 and not plan_data.get('filled_layers') and not plan_data.get('pending_layers'):
                continue
            plan_resets.append({
                'plan_key': row['plan_key'], 'symbol': symbol, 'side': side,
                'previous_status': row['status'], 'previous_current_layer': int(row['current_layer'] or 0),
                'previous_root_signal_id': row['root_signal_id'],
            })
            touched_keys.add((symbol, side))

        conn.commit()
        conn.close()
        synced = [self.sync_layer_plan_state(symbol, side, reset_if_flat=True) for symbol, side in sorted(touched_keys)]
        return {
            'removed_intents': removed_intents,
            'healed_intents': healed_intents,
            'removed_locks': removed_locks,
            'healed_locks': healed_locks,
            'plan_resets': plan_resets,
            'synced_states': synced,
        }

    def _safe_json_dict(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if value in (None, ''):
            return {}
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    def _build_execution_exposure_summary(self, positions: List[Dict[str, Any]], intents: List[Dict[str, Any]]) -> Dict[str, Any]:
        symbol_rows: Dict[str, Dict[str, float]] = {}
        current_total = 0.0
        projected_total = 0.0
        for row in positions or []:
            symbol = row.get('symbol') or '--'
            qty = self._safe_float(row.get('coin_quantity') or 0.0)
            px = self._safe_float(row.get('current_price') or row.get('entry_price') or 0.0)
            lev = max(1.0, self._safe_float(row.get('leverage') or 1.0))
            margin = (qty * px / lev) if qty > 0 and px > 0 else 0.0
            current_total += margin
            projected_total += margin
            bucket = symbol_rows.setdefault(symbol, {'symbol': symbol, 'current_margin': 0.0, 'projected_margin': 0.0})
            bucket['current_margin'] += margin
            bucket['projected_margin'] += margin
        for intent in intents or []:
            symbol = intent.get('symbol') or '--'
            planned = self._safe_float(intent.get('planned_margin') or intent.get('margin_used') or 0.0)
            projected_total += planned
            bucket = symbol_rows.setdefault(symbol, {'symbol': symbol, 'current_margin': 0.0, 'projected_margin': 0.0})
            bucket['projected_margin'] += planned
        symbol_rows_list = []
        for row in symbol_rows.values():
            row['current_margin'] = round(row['current_margin'], 4)
            row['projected_margin'] = round(row['projected_margin'], 4)
            row['pending_margin'] = round(max(0.0, row['projected_margin'] - row['current_margin']), 4)
            symbol_rows_list.append(row)
        symbol_rows_list.sort(key=lambda item: (-item['projected_margin'], item['symbol']))
        return {
            'current_total_margin': round(current_total, 4),
            'projected_total_margin': round(projected_total, 4),
            'pending_total_margin': round(max(0.0, projected_total - current_total), 4),
            'by_symbol': symbol_rows_list,
        }

    def _build_signal_decision_digest(self, limit: int = 8) -> List[Dict[str, Any]]:
        rows = self.get_signals(limit=limit)
        digest = []
        for row in rows:
            fd = self._safe_json_dict(row.get('filter_details'))
            obs = self._safe_json_dict(fd.get('observability'))
            entry_decision = self._safe_json_dict(fd.get('entry_decision'))
            policy_snapshot = self._safe_json_dict(fd.get('adaptive_policy_snapshot'))
            regime_snapshot = self._safe_json_dict(fd.get('regime_snapshot'))
            adaptive_observe = self._safe_json_dict(fd.get('adaptive_regime_observe_only'))
            breakdown = self._safe_json_dict(entry_decision.get('breakdown'))
            observe_only_view = normalize_observe_only_view(
                adaptive_observe or {
                    'phase': breakdown.get('observe_only_phase') or policy_snapshot.get('phase'),
                    'state': breakdown.get('observe_only_state') or policy_snapshot.get('state'),
                    'summary': breakdown.get('observe_only_summary') or policy_snapshot.get('summary'),
                    'tags': breakdown.get('observe_only_tags') or policy_snapshot.get('tags') or [],
                },
                regime_snapshot=regime_snapshot,
                policy_snapshot=policy_snapshot,
                fallback_summary=breakdown.get('observe_only_summary') or policy_snapshot.get('summary'),
            )
            digest.append({
                'id': row.get('id'),
                'created_at': row.get('created_at'),
                'symbol': row.get('symbol'),
                'signal_type': row.get('signal_type'),
                'executed': bool(row.get('executed')),
                'filtered': bool(row.get('filtered')),
                'decision': entry_decision.get('decision') or ('executed' if row.get('executed') else ('blocked' if row.get('filtered') else 'watch')),
                'decision_reason': row.get('filter_reason') or entry_decision.get('reason_summary') or '--',
                'signal_score': entry_decision.get('score'),
                'observe_only': observe_only_view,
                'observe_only_phase': observe_only_view.get('phase'),
                'observe_only_state': observe_only_view.get('state'),
                'observe_only_summary': observe_only_view.get('summary'),
                'observe_only_tags': list(observe_only_view.get('tags') or []),
                'regime_name': regime_snapshot.get('name') or regime_snapshot.get('regime') or policy_snapshot.get('regime_name'),
                'regime_confidence': regime_snapshot.get('confidence') if regime_snapshot else policy_snapshot.get('regime_confidence'),
                'policy_mode': policy_snapshot.get('mode'),
                'policy_version': policy_snapshot.get('policy_version'),
                'signal_id': obs.get('signal_id') or row.get('id'),
                'root_signal_id': obs.get('root_signal_id'),
                'layer_no': obs.get('layer_no'),
                'deny_reason': obs.get('deny_reason') or row.get('filter_reason'),
                'current_symbol_exposure': obs.get('current_symbol_exposure'),
                'projected_symbol_exposure': obs.get('projected_symbol_exposure'),
                'current_total_exposure': obs.get('current_total_exposure'),
                'projected_total_exposure': obs.get('projected_total_exposure'),
            })
        return digest

    def get_execution_state_snapshot(self) -> Dict:
        intents = self.get_active_open_intents()
        positions = self.get_positions()
        conn = self._get_connection()
        locks_df = pd.read_sql_query("SELECT * FROM direction_locks ORDER BY updated_at DESC, created_at DESC", conn)
        plans_df = pd.read_sql_query("SELECT * FROM layer_plan_states ORDER BY symbol ASC, side ASC", conn)
        conn.close()
        locks = locks_df.to_dict('records') if not locks_df.empty else []
        plans = plans_df.to_dict('records') if not plans_df.empty else []
        for row in intents:
            row['plan_context'] = self._safe_json_dict(row.get('plan_context'))
        for row in plans:
            row['plan_data'] = self._safe_json_dict(row.get('plan_data'))
        exposure = self._build_execution_exposure_summary(positions, intents)
        signal_digest = self._build_signal_decision_digest()
        observe_only_summary = summarize_observe_only_collection(signal_digest)
        from analytics.helper import build_close_outcome_scope_windows
        close_outcome_scope_windows = build_close_outcome_scope_windows(self.get_recent_close_outcome_trades(limit=50), label='execution_state_snapshot')
        recent_decisions = []
        for row in signal_digest[:5]:
            recent_decisions.append({
                'symbol': row.get('symbol'),
                'decision': row.get('decision'),
                'created_at': row.get('created_at'),
                'regime': (row.get('observe_only') or {}).get('regime', {}).get('name'),
                'policy_mode': (row.get('observe_only') or {}).get('policy', {}).get('mode'),
                'top_tags': (row.get('observe_only') or {}).get('top_tags') or [],
                'summary': (row.get('observe_only') or {}).get('summary'),
            })
        return {
            'active_intents': intents,
            'direction_locks': locks,
            'layer_plans': plans,
            'positions': positions,
            'exposure': exposure,
            'signal_decisions': signal_digest,
            'observe_only_summary': observe_only_summary,
            'close_outcome_scope_windows': close_outcome_scope_windows,
            'summary': {
                'active_intents': len(intents),
                'direction_locks': len(locks),
                'active_layer_plans': sum(1 for row in plans if row.get('status') != 'idle'),
                'open_positions': len(positions),
                'signals_with_decision': len(signal_digest),
                'observe_only_banner': observe_only_summary.get('banner'),
                'observe_only_top_tags': observe_only_summary.get('top_tags'),
                'close_outcome_active_scope_windows': int(close_outcome_scope_windows.get('active_window_count', 0) or 0),
                'recent_decisions': recent_decisions,
            }
        }

    # =========================================================================
    # 清理操作
    # =========================================================================
    
    def cleanup_old_data(self, signals_days: int = 90, 
                         trades_days: int = 365, logs_days: int = 30):
        """清理旧数据"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM signals WHERE created_at < datetime('now', '-' || ? || ' days')", (signals_days,))
        cursor.execute("DELETE FROM trades WHERE open_time < datetime('now', '-' || ? || ' days')", (trades_days,))
        cursor.execute("DELETE FROM system_logs WHERE created_at < datetime('now', '-' || ? || ' days')", (logs_days,))
        cursor.execute("DELETE FROM candidate_reviews WHERE created_at < datetime('now', '-90 days')")
        cursor.execute("DELETE FROM preset_history WHERE created_at < datetime('now', '-180 days')")
        cursor.execute("DELETE FROM governance_decisions WHERE created_at < datetime('now', '-180 days')")
        cursor.execute("DELETE FROM daily_reports WHERE created_at < datetime('now', '-365 days')")
        
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted

    def cleanup_duplicate_reports(self, dry_run: bool = True) -> Dict:
        """清理重复日报：每个 report_date 仅保留最新一条"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM daily_reports
            WHERE id NOT IN (
                SELECT MAX(id) FROM daily_reports GROUP BY report_date
            )
        """)
        duplicate_count = cursor.fetchone()[0]
        result = {'table': 'daily_reports', 'duplicates': duplicate_count, 'deleted': 0, 'dry_run': dry_run}
        if not dry_run and duplicate_count > 0:
            cursor.execute("""
                DELETE FROM daily_reports
                WHERE id NOT IN (
                    SELECT MAX(id) FROM daily_reports GROUP BY report_date
                )
            """)
            result['deleted'] = cursor.rowcount
            conn.commit()
        conn.close()
        return result

    def cleanup_duplicate_governance_decisions(self, dry_run: bool = True) -> Dict:
        """清理重复治理记录：相同 decision_type + preset + message 仅保留最新一条"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM governance_decisions
            WHERE id NOT IN (
                SELECT MAX(id)
                FROM governance_decisions
                GROUP BY decision_type, COALESCE(recommended_preset, ''), COALESCE(message, '')
            )
        """)
        duplicate_count = cursor.fetchone()[0]
        result = {'table': 'governance_decisions', 'duplicates': duplicate_count, 'deleted': 0, 'dry_run': dry_run}
        if not dry_run and duplicate_count > 0:
            cursor.execute("""
                DELETE FROM governance_decisions
                WHERE id NOT IN (
                    SELECT MAX(id)
                    FROM governance_decisions
                    GROUP BY decision_type, COALESCE(recommended_preset, ''), COALESCE(message, '')
                )
            """)
            result['deleted'] = cursor.rowcount
            conn.commit()
        conn.close()
        return result

    def cleanup_duplicate_runtime_records(self, dry_run: bool = True) -> Dict:
        """清理运行期重复记录（日报 + 治理决策）"""
        reports = self.cleanup_duplicate_reports(dry_run=dry_run)
        governance = self.cleanup_duplicate_governance_decisions(dry_run=dry_run)
        return {
            'dry_run': dry_run,
            'daily_reports': reports,
            'governance_decisions': governance,
            'total_duplicates': reports['duplicates'] + governance['duplicates'],
            'total_deleted': reports['deleted'] + governance['deleted'],
        }


# 全局数据库实例
db = Database()
