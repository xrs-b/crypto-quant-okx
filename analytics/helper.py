"""Approval/workflow persistence helpers."""
from __future__ import annotations

from typing import Dict, List, Optional, Any


TERMINAL_APPROVAL_STATES = {'approved', 'rejected', 'deferred', 'expired'}
AUTO_APPROVAL_DECISIONS = {'auto_approve', 'manual_review', 'freeze', 'defer'}


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
        if row.get('approval_required') and approval_row.get('approval_state') == 'approved' and row.get('workflow_state') == 'pending':
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
