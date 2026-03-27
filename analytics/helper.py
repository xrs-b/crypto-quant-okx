"""Approval/workflow persistence helpers."""
from __future__ import annotations

from typing import Dict, List, Optional, Any


TERMINAL_APPROVAL_STATES = {'approved', 'rejected', 'deferred', 'expired'}


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
        row['persisted_state'] = persisted.get('state')
        row['persisted_decision'] = persisted.get('decision')
        row['persisted_updated_at'] = persisted.get('updated_at')
        row['reason'] = persisted.get('reason') or row.get('reason')
        row['actor'] = persisted.get('actor') or row.get('actor')
        row['replay_source'] = persisted.get('replay_source') or row.get('replay_source')
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
