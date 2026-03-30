from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


_REASON_CODE_SPECS: Dict[str, Dict[str, str]] = {
    'PERMIT_FINAL_EXECUTION_GRANTED': {
        'disposition': 'permit',
        'stage': 'final_execution_permit',
        'family': 'permit',
        'legacy_code': 'FINAL_EXECUTION_PERMIT_GRANTED',
    },
    'DENY_ENV_TESTNET_ONLY': {
        'disposition': 'deny',
        'stage': 'final_execution_permit',
        'family': 'environment',
        'legacy_code': 'TESTNET_ONLY_EXECUTION_PERMIT',
    },
    'SKIP_SIGNAL_FILTERED': {
        'disposition': 'skip',
        'stage': 'candidate_skip',
        'family': 'signal_filter',
        'legacy_code': 'SIGNAL_FILTERED',
    },
    'DENY_RISK_GATE_BLOCKED': {
        'disposition': 'deny',
        'stage': 'risk_gate',
        'family': 'risk_gate',
        'legacy_code': 'RISK_GATE_BLOCKED',
    },
    'DENY_GUARD_SCOPED_FREEZE': {
        'disposition': 'deny',
        'stage': 'risk_gate',
        'family': 'close_outcome_guard',
        'legacy_code': 'SCOPED_WINDOW_FREEZE',
    },
    'SKIP_GUARD_SCOPED_TIGHTEN': {
        'disposition': 'skip',
        'stage': 'candidate_skip',
        'family': 'close_outcome_guard',
        'legacy_code': 'SCOPED_WINDOW_TIGHTEN',
    },
    'SKIP_GUARD_SCOPED_REVIEW': {
        'disposition': 'skip',
        'stage': 'candidate_skip',
        'family': 'close_outcome_guard',
        'legacy_code': 'SCOPED_WINDOW_REVIEW',
    },
    'SKIP_PRE_EXECUTION_INELIGIBLE': {
        'disposition': 'skip',
        'stage': 'execution_quota',
        'family': 'pre_execution',
        'legacy_code': 'INELIGIBLE_BEFORE_EXECUTION_QUOTA',
    },
    'DEFER_EXECUTION_CYCLE_QUOTA_EXHAUSTED': {
        'disposition': 'defer',
        'stage': 'execution_quota',
        'family': 'execution_quota',
        'legacy_code': 'EXECUTION_QUOTA_EXHAUSTED',
    },
    'DEFER_EXECUTION_CLUSTER_CAP_REACHED': {
        'disposition': 'defer',
        'stage': 'execution_quota',
        'family': 'execution_quota',
        'legacy_code': 'SYMBOL_CLUSTER_CAP_REACHED',
    },
    'DEFER_EXECUTION_SIDE_CAP_REACHED': {
        'disposition': 'defer',
        'stage': 'execution_quota',
        'family': 'execution_quota',
        'legacy_code': 'SIDE_EXECUTION_CAP_REACHED',
    },
    'DEFER_EXECUTION_REGIME_CAP_REACHED': {
        'disposition': 'defer',
        'stage': 'execution_quota',
        'family': 'execution_quota',
        'legacy_code': 'REGIME_EXECUTION_CAP_REACHED',
    },
    'PERMIT_EXECUTION_QUOTA_PASSED': {
        'disposition': 'permit',
        'stage': 'execution_quota',
        'family': 'execution_quota',
        'legacy_code': 'EXECUTION_QUOTA_PASSED',
    },
    'SKIP_STRATEGY_COOLDOWN_ACTIVE': {
        'disposition': 'skip',
        'stage': 'strategy_selection',
        'family': 'strategy_cooldown',
        'legacy_code': 'STRATEGY_COOLDOWN_ACTIVE',
    },
    'SKIP_STRATEGY_RECOVERY_WINDOW_ACTIVE': {
        'disposition': 'skip',
        'stage': 'strategy_selection',
        'family': 'strategy_cooldown',
        'legacy_code': 'STRATEGY_RECOVERY_WINDOW_ACTIVE',
    },
    'SKIP_STRATEGY_REACTIVATION_CONFIRM_REQUIRED': {
        'disposition': 'skip',
        'stage': 'strategy_selection',
        'family': 'strategy_reactivation',
        'legacy_code': 'STRATEGY_REACTIVATION_CONFIRM_REQUIRED',
    },
    'DEWEIGHT_STRATEGY_REACTIVATION_PROBATION_ACTIVE': {
        'disposition': 'defer',
        'stage': 'strategy_selection',
        'family': 'strategy_reactivation',
        'legacy_code': 'STRATEGY_REACTIVATION_PROBATION_ACTIVE',
    },
    # === Strategy Reactivation Confirm Evidence (adaptive strategy selection v5) ===
    'REACT_EVIDENCE_OUTCOME_IMPROVING': {
        'disposition': 'permit',
        'stage': 'strategy_selection',
        'family': 'strategy_reactivation',
        'legacy_code': 'REACTIVATION_EVIDENCE_OUTCOME_IMPROVING',
    },
    'REACT_EVIDENCE_REGIME_MATCHED': {
        'disposition': 'permit',
        'stage': 'strategy_selection',
        'family': 'strategy_reactivation',
        'legacy_code': 'REACTIVATION_EVIDENCE_REGIME_MATCHED',
    },
    'REACT_EVIDENCE_SIGNAL_STRONG': {
        'disposition': 'permit',
        'stage': 'strategy_selection',
        'family': 'strategy_reactivation',
        'legacy_code': 'REACTIVATION_EVIDENCE_SIGNAL_STRONG',
    },
    'REACT_EVIDENCE_REPEATED_TRIGGER': {
        'disposition': 'permit',
        'stage': 'strategy_selection',
        'family': 'strategy_reactivation',
        'legacy_code': 'REACTIVATION_EVIDENCE_REPEATED_TRIGGER',
    },
    'REACT_CONFIRM_FAIL_OUTCOME': {
        'disposition': 'skip',
        'stage': 'strategy_selection',
        'family': 'strategy_reactivation',
        'legacy_code': 'REACTIVATION_CONFIRM_FAIL_OUTCOME',
    },
    'REACT_CONFIRM_FAIL_REGIME': {
        'disposition': 'skip',
        'stage': 'strategy_selection',
        'family': 'strategy_reactivation',
        'legacy_code': 'REACTIVATION_CONFIRM_FAIL_REGIME',
    },
    'REACT_CONFIRM_FAIL_SIGNAL_STRENGTH': {
        'disposition': 'skip',
        'stage': 'strategy_selection',
        'family': 'strategy_reactivation',
        'legacy_code': 'REACTIVATION_CONFIRM_FAIL_SIGNAL_STRENGTH',
    },
    'REACT_CONFIRM_FAIL_REPEATED': {
        'disposition': 'skip',
        'stage': 'strategy_selection',
        'family': 'strategy_reactivation',
        'legacy_code': 'REACTIVATION_CONFIRM_FAIL_REPEATED',
    },
    'REACT_CONFIRM_FAIL_THRESHOLD': {
        'disposition': 'skip',
        'stage': 'strategy_selection',
        'family': 'strategy_reactivation',
        'legacy_code': 'REACTIVATION_CONFIRM_FAIL_THRESHOLD',
    },
    'REACT_CONFIRM_ALL_EVIDENCE_PASSED': {
        'disposition': 'permit',
        'stage': 'strategy_selection',
        'family': 'strategy_reactivation',
        'legacy_code': 'REACTIVATION_CONFIRM_ALL_EVIDENCE_PASSED',
    },
    'REACT_EVIDENCE_PARTIAL_PASS': {
        'disposition': 'permit',
        'stage': 'strategy_selection',
        'family': 'strategy_reactivation',
        'legacy_code': 'REACTIVATION_EVIDENCE_PARTIAL_PASS',
    },
    'PERMIT_FINAL_STRATEGY_CONTRACT_READY': {
        'disposition': 'permit',
        'stage': 'strategy_selection',
        'family': 'final_strategy_contract',
        'legacy_code': 'FINAL_STRATEGY_CONTRACT_READY',
    },
    'SKIP_FINAL_STRATEGY_ALL_BLOCKED': {
        'disposition': 'skip',
        'stage': 'strategy_selection',
        'family': 'final_strategy_contract',
        'legacy_code': 'FINAL_STRATEGY_ALL_BLOCKED',
    },
    'DEWEIGHT_FINAL_STRATEGY_PROBATION_ONLY': {
        'disposition': 'defer',
        'stage': 'strategy_selection',
        'family': 'final_strategy_contract',
        'legacy_code': 'FINAL_STRATEGY_PROBATION_ONLY',
    },
    'SKIP_FINAL_STRATEGY_LOW_EVIDENCE': {
        'disposition': 'skip',
        'stage': 'strategy_selection',
        'family': 'final_strategy_contract',
        'legacy_code': 'FINAL_STRATEGY_LOW_EVIDENCE',
    },
    'SKIP_FINAL_STRATEGY_DIRECTION_CONFLICT': {
        'disposition': 'skip',
        'stage': 'strategy_selection',
        'family': 'final_strategy_contract',
        'legacy_code': 'FINAL_STRATEGY_DIRECTION_CONFLICT',
    },
    'SKIP_FINAL_STRATEGY_SIGNAL_MISMATCH': {
        'disposition': 'skip',
        'stage': 'strategy_selection',
        'family': 'final_strategy_contract',
        'legacy_code': 'FINAL_STRATEGY_SIGNAL_MISMATCH',
    },
    'SKIP_FINAL_STRATEGY_NO_SELECTED_TRIGGER': {
        'disposition': 'skip',
        'stage': 'strategy_selection',
        'family': 'final_strategy_contract',
        'legacy_code': 'FINAL_STRATEGY_NO_SELECTED_TRIGGER',
    },
}

_REASON_CODE_ALIASES: Dict[str, str] = {}
for canonical, spec in _REASON_CODE_SPECS.items():
    _REASON_CODE_ALIASES[canonical] = canonical
    legacy = str(spec.get('legacy_code') or '').strip()
    if legacy:
        _REASON_CODE_ALIASES[legacy] = canonical


def normalize_reason_code(code: Any, *, fallback: Optional[str] = None) -> str:
    raw = str(code or '').strip()
    if raw:
        return _REASON_CODE_ALIASES.get(raw, raw)
    return _REASON_CODE_ALIASES.get(str(fallback or '').strip(), str(fallback or '').strip())


def reason_code_spec(code: Any, *, fallback: Optional[str] = None) -> Dict[str, str]:
    canonical = normalize_reason_code(code, fallback=fallback)
    spec = dict(_REASON_CODE_SPECS.get(canonical) or {})
    spec['code'] = canonical
    spec['legacy_code'] = spec.get('legacy_code') or canonical
    spec['disposition'] = spec.get('disposition') or 'unknown'
    spec['stage'] = spec.get('stage') or 'unknown'
    spec['family'] = spec.get('family') or 'unknown'
    return spec


def build_reason_code_details(code: Any, *, fallback: Optional[str] = None, include_legacy: bool = True) -> Dict[str, str]:
    spec = reason_code_spec(code, fallback=fallback)
    payload = {
        'reason_code': spec['code'],
        'reason_code_family': spec['family'],
        'reason_code_stage': spec['stage'],
        'reason_code_disposition': spec['disposition'],
    }
    if include_legacy:
        payload['legacy_reason_code'] = spec['legacy_code']
    return payload


def merge_reason_codes(*codes: Iterable[Any], primary: Any = None, fallback: Optional[str] = None, include_legacy: bool = True) -> List[str]:
    merged: List[str] = []

    def _add(value: Any):
        normalized = normalize_reason_code(value)
        if normalized and normalized not in merged:
            merged.append(normalized)
        if include_legacy:
            legacy = str(value or '').strip()
            if legacy and legacy not in merged:
                merged.append(legacy)

    primary_code = normalize_reason_code(primary, fallback=fallback)
    if primary_code:
        _add(primary)
        if primary_code != str(primary or '').strip():
            _add(primary_code)
    for group in codes:
        for code in group or []:
            _add(code)
    return merged


def build_final_execution_operator_hint(bundle: Dict[str, Any] | None) -> Dict[str, Any]:
    payload = dict(bundle or {})
    decision = str(payload.get('decision') or payload.get('reason_code_disposition') or '').strip().lower() or 'observe'
    reason_code = normalize_reason_code(payload.get('reason_code')) if payload.get('reason_code') else None
    family = str(payload.get('reason_code_family') or '').strip().lower() or 'unknown'
    final_gate = str(payload.get('final_gate') or payload.get('reason_code_stage') or 'final_execution_permit').strip().lower()
    exchange_mode = str(payload.get('exchange_mode') or '').strip().lower()
    allowed = bool(payload.get('allowed', False))
    testnet_only = bool(payload.get('testnet_only', True))
    guardrail = dict(payload.get('guardrail_evidence') or {})
    close_guard = dict(guardrail.get('close_outcome_guard') or {})
    scope_mode = str(payload.get('scope_mode') or close_guard.get('mode') or '').strip().lower() or 'observe'

    checklist: List[str] = []
    wait_for: List[str] = []
    relax: List[str] = []
    freeze: List[str] = []

    action_label = 'keep_observing'
    urgency = 'medium'
    operator_message = '继续观察运行态，再等下一轮证据。'
    next_step = 'monitor_runtime_diagnose_bundle'

    if decision == 'permit' and allowed:
        action_label = 'permit_testnet_execution'
        urgency = 'low'
        operator_message = '许可已通过；仅可在 testnet 下提交执行，并持续观察回放与风控证据。'
        next_step = 'submit_testnet_order_and_monitor_feedback'
        checklist.extend([
            '确认 exchange_mode=testnet，禁止切到实盘',
            '确认 final_execution_permit.allowed=true 且 selected_for_execution=true',
            '提交后观察成交、风控回放与 close outcome feedback loop',
        ])
        wait_for.append('testnet execution acknowledgement')
    elif decision == 'deny':
        action_label = 'deny_and_hold'
        urgency = 'high' if family in {'environment', 'risk_gate', 'close_outcome_guard'} else 'medium'
        operator_message = '当前应拒绝执行并冻结推进，先处理阻断原因，不能绕过 testnet-only 边界。'
        next_step = 'freeze_execution_until_blocker_clears'
        checklist.append('检查最终阻断阶段与 reason_code 是否匹配')
        freeze.append('freeze new execution attempts for this candidate')
        if reason_code == 'DENY_ENV_TESTNET_ONLY' or exchange_mode not in {'', 'testnet'}:
            checklist.append('核对配置是否误切到非 testnet 模式')
            wait_for.append('exchange_mode switched back to testnet')
            freeze.append('freeze all non-testnet execution paths')
        elif family in {'risk_gate', 'close_outcome_guard'}:
            checklist.append('检查 close_outcome_guard.reason_codes、scope_window 与 risk_reason')
            wait_for.append('new risk sample or guardrail recovery evidence')
            if reason_code == 'DENY_GUARD_SCOPED_FREEZE':
                freeze.append('freeze scoped auto-promotion / execution for affected symbol-family-window')
                next_step = 'keep_scope_frozen_until_guard_recovers'
    elif decision == 'skip':
        action_label = 'skip_this_cycle'
        urgency = 'medium'
        operator_message = '本轮跳过即可，不要强行开仓；先看过滤/守门原因，再等更好样本。'
        next_step = 'observe_skip_reason_before_retry'
        checklist.append('检查 candidate_skip / filter details，确认不是遗漏配置问题')
        wait_for.append('fresh signal or wider evidence window')
        if family == 'signal_filter':
            relax.append('only relax signal thresholds after repeated false negatives are confirmed')
        elif family == 'close_outcome_guard':
            checklist.append('检查 scoped window 是否过紧、样本量是否不足')
            if reason_code == 'SKIP_GUARD_SCOPED_TIGHTEN':
                relax.append('consider widening scoped guard window after enough clean closes arrive')
                next_step = 'wait_for_clean_close_samples_then_relax_scope'
            elif reason_code == 'SKIP_GUARD_SCOPED_REVIEW':
                wait_for.append('operator review outcome for scoped window')
                next_step = 'review_scope_then_resume_if_cleared'
    elif decision == 'defer':
        action_label = 'defer_to_next_cycle'
        urgency = 'medium'
        operator_message = '当前先 defer，不要抢跑；等待配额或分组拥塞释放后再进入下一轮。'
        next_step = 'wait_for_quota_or_cluster_capacity'
        checklist.append('检查 execution_contract / quota counters / selected_*_counts 是否触顶')
        wait_for.append('next execution cycle or quota reset')
        if reason_code == 'DEFER_EXECUTION_CLUSTER_CAP_REACHED':
            wait_for.append('cluster capacity frees up')
            next_step = 'wait_for_cluster_capacity_then_retry'
        elif reason_code == 'DEFER_EXECUTION_SIDE_CAP_REACHED':
            wait_for.append('side capacity frees up')
            next_step = 'wait_for_side_capacity_then_retry'
        elif reason_code == 'DEFER_EXECUTION_REGIME_CAP_REACHED':
            wait_for.append('regime capacity frees up')
            next_step = 'wait_for_regime_capacity_then_retry'
    else:
        checklist.append('检查 diagnose bundle 是否完整，并持续观察下一轮运行')
        wait_for.append('next runtime cycle')

    if testnet_only:
        freeze.append('never bypass testnet-only boundary')

    summary = f"{action_label}:{reason_code or 'UNKNOWN'}:{next_step}"
    return {
        'schema_version': 'final_execution_operator_hint_v1',
        'decision': decision,
        'action_label': action_label,
        'urgency': urgency,
        'operator_message': operator_message,
        'next_step': next_step,
        'checklist': checklist,
        'wait_for': wait_for,
        'relax_candidates': relax,
        'freeze_candidates': freeze,
        'scope_mode': scope_mode,
        'final_gate': final_gate,
        'reason_code': reason_code,
        'summary': summary,
        'testnet_only': testnet_only,
    }
