"""
OKX量化交易机器人 - 主程序入口
使用方法:
    .venv/bin/python3 bot/run.py                 # 运行交易
    .venv/bin/flask --app dashboard.api:app run --host 0.0.0.0 --port 5555  # 运行仪表盘
    python bot/run.py --train         # 训练模型
    python bot/run.py --collect        # 收集数据
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import json
import time
from collections import Counter
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

from core.config import Config
from core.database import Database
from core.exchange import Exchange
from core.logger import logger
from core.notifier import NotificationManager
from core.presets import PresetManager
from core.paths import DATA_DIR
from core.reason_codes import build_reason_code_details, build_final_execution_operator_hint, merge_reason_codes
from signals import SignalDetector, SignalValidator, SignalRecorder, EntryDecider
from trading import TradingExecutor, RiskManager
from ml.engine import MLEngine, ModelTrainer, DataCollector
from analytics import StrategyBacktester, SignalQualityAnalyzer, ParameterOptimizer, GovernanceEngine, build_approval_audit_overview, execute_adaptive_rollout_orchestration, build_runtime_orchestration_summary, build_close_outcome_feedback_loop
from analytics.backtest import export_calibration_payload
from validation import format_validation_report_markdown, run_shadow_validation_case, run_shadow_validation_replay


def run_notification_relay(interval: int = 30, once: bool = False, limit: int = 20):
    cfg = Config()
    db = Database(cfg.db_path)
    notifier = NotificationManager(cfg, db, logger)
    print(f"\n📮 Outbox relay 启动 | interval={interval}s | limit={limit} | once={'yes' if once else 'no'}\n")
    state = load_runtime_state()
    state['relay'] = {
        'running': True,
        'interval_seconds': interval,
        'last_started_at': datetime.now().isoformat(),
        'last_result': state.get('relay', {}).get('last_result'),
    }
    save_runtime_state(state)
    while True:
        now = datetime.now().isoformat()
        result = notifier.relay_pending_outbox(limit=limit)
        relay_state = load_runtime_state()
        relay_state['relay'] = {
            'running': not once,
            'interval_seconds': interval,
            'last_started_at': relay_state.get('relay', {}).get('last_started_at') or now,
            'last_checked_at': now,
            'last_result': {'time': now, **result},
        }
        save_runtime_state(relay_state)
        print(json.dumps({'time': now, **result}, ensure_ascii=False))
        if once:
            break
        time.sleep(interval)

RUNTIME_STATE_PATH = DATA_DIR / 'runtime_state.json'


def load_runtime_state() -> dict:
    try:
        return json.loads(RUNTIME_STATE_PATH.read_text()) if RUNTIME_STATE_PATH.exists() else {}
    except Exception:
        return {}


def save_runtime_state(state: dict):
    RUNTIME_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_STATE_PATH.write_text(json.dumps(state or {}, ensure_ascii=False, indent=2))


def _build_approval_hygiene_operator_action_summary(stale_rows: list, decision_diffs: list, *, auto_cleanup_enabled: bool = False) -> dict:
    policy_counts = {
        'review_schedule': 0,
        'escalate': 0,
        'freeze_followup': 0,
        'observe_only_followup': 0,
    }
    routed_items = []

    for row in stale_rows or []:
        state = str(row.get('state') or row.get('approval_state') or '').strip().lower()
        risk = str(row.get('risk_level') or '').strip().lower()
        blocked = list(row.get('blocked_by') or [])
        if risk in {'critical', 'high'} or blocked:
            action = 'escalate'
            route = 'operator_escalation'
        elif auto_cleanup_enabled and state in {'pending', 'ready', 'replayed'}:
            action = 'observe_only_followup'
            route = 'stale_cleanup_audit'
        else:
            action = 'review_schedule'
            route = 'review_schedule_queue'
        policy_counts[action] = policy_counts.get(action, 0) + 1
        routed_items.append({
            'kind': 'stale',
            'approval_id': row.get('approval_id'),
            'item_id': row.get('playbook_id') or row.get('item_id'),
            'action': action,
            'route': route,
            'state': state or 'pending',
            'blocked_by': blocked,
        })

    for row in decision_diffs or []:
        workflow_state = str(row.get('workflow_state') or row.get('to_workflow_state') or row.get('current_workflow_state') or '').strip().lower()
        state = str(row.get('state') or row.get('to_state') or row.get('current_state') or '').strip().lower()
        if workflow_state in {'execution_failed', 'rollback_pending'}:
            action = 'freeze_followup'
            route = 'freeze_followup_queue'
        elif state in {'rejected', 'expired'}:
            action = 'review_schedule'
            route = 'review_schedule_queue'
        else:
            action = 'observe_only_followup'
            route = 'decision_diff_audit'
        policy_counts[action] = policy_counts.get(action, 0) + 1
        routed_items.append({
            'kind': 'decision_diff',
            'approval_id': row.get('approval_id'),
            'item_id': row.get('playbook_id') or row.get('item_id'),
            'action': action,
            'route': route,
            'state': state or workflow_state or 'changed',
            'blocked_by': list(row.get('blocked_by') or []),
        })

    return {
        'schema_version': 'm5_approval_hygiene_operator_action_summary_v1',
        'policy_counts': {k: v for k, v in policy_counts.items() if v},
        'routes': sorted({row['route'] for row in routed_items if row.get('route')}),
        'items': routed_items[:20],
    }


def append_runtime_history(event: dict, limit: int = 20):
    state = load_runtime_state()
    history = state.get('history', [])
    history.insert(0, event)
    state['history'] = history[:limit]
    save_runtime_state(state)


def build_approval_hygiene_summary(cfg: Config, db: Database, limit: int = 20) -> dict:
    hygiene_cfg = cfg.get('runtime.approval_hygiene', {}) or {}
    enabled = bool(hygiene_cfg.get('enabled', True))
    auto_cleanup_enabled = bool(hygiene_cfg.get('auto_cleanup_enabled', False))
    stale_after_minutes = max(1, int(hygiene_cfg.get('stale_after_minutes', 180) or 180))
    approval_type = hygiene_cfg.get('approval_type') or None
    sample_limit = max(1, int(hygiene_cfg.get('limit', limit) or limit))
    stale_rows = db.get_stale_approval_states(
        stale_after_minutes=stale_after_minutes,
        approval_type=approval_type,
        limit=sample_limit,
    )
    decision_diffs = db.get_recent_approval_decision_diff(
        limit=max(5, min(sample_limit, 50)),
        approval_type=approval_type,
    )
    operator_action_summary = _build_approval_hygiene_operator_action_summary(
        stale_rows,
        decision_diffs,
        auto_cleanup_enabled=auto_cleanup_enabled,
    )
    overview = build_approval_audit_overview(stale_rows=stale_rows, decision_diffs=decision_diffs)
    overview['operator_action_summary'] = operator_action_summary
    return {
        'enabled': enabled,
        'auto_cleanup_enabled': auto_cleanup_enabled,
        'stale_after_minutes': stale_after_minutes,
        'approval_type': approval_type,
        'limit': sample_limit,
        'stale_count': len(stale_rows),
        'decision_diff_count': len(decision_diffs),
        'stale_items': stale_rows,
        'decision_diffs': decision_diffs,
        'audit_overview': overview,
        'operator_action_summary': operator_action_summary,
    }


def maybe_run_approval_hygiene(cfg: Config, db: Database, notifier: NotificationManager = None, force: bool = False) -> dict:
    runtime = load_runtime_state()
    hygiene = build_approval_hygiene_summary(cfg, db)
    hygiene_cfg = cfg.get('runtime.approval_hygiene', {}) or {}
    enabled = bool(hygiene.get('enabled', True))
    auto_cleanup_enabled = bool(hygiene.get('auto_cleanup_enabled', False))
    actor = hygiene_cfg.get('actor') or 'system:approval-hygiene'

    result = {
        'enabled': enabled,
        'auto_cleanup_enabled': auto_cleanup_enabled,
        'force': bool(force),
        'summary': hygiene,
        'cleanup': None,
        'ran_cleanup': False,
    }
    if not enabled and not force:
        result['reason'] = 'disabled'
        return result

    if auto_cleanup_enabled or force:
        cleanup_result = db.cleanup_stale_approval_states(
            stale_after_minutes=hygiene['stale_after_minutes'],
            approval_type=hygiene.get('approval_type'),
            limit=hygiene.get('limit', 20),
            dry_run=not auto_cleanup_enabled and force,
            actor=actor,
        )
        result['cleanup'] = cleanup_result
        result['ran_cleanup'] = True
        if cleanup_result.get('expired_count', 0) > 0 and notifier is not None:
            notifier.notify_runtime(
                'approval_hygiene',
                [
                    f"过期审批自动收口：{cleanup_result.get('expired_count', 0)} 项",
                    f"stale 阈值：{cleanup_result.get('stale_after_minutes', hygiene['stale_after_minutes'])} 分钟",
                ],
                cleanup_result,
            )
        hygiene = build_approval_hygiene_summary(cfg, db)
        result['summary'] = hygiene

    runtime['approval_hygiene'] = {
        'last_run_at': datetime.now().isoformat(),
        'enabled': enabled,
        'auto_cleanup_enabled': auto_cleanup_enabled,
        'stale_after_minutes': hygiene.get('stale_after_minutes'),
        'approval_type': hygiene.get('approval_type'),
        'stale_count': hygiene.get('stale_count', 0),
        'decision_diff_count': hygiene.get('decision_diff_count', 0),
        'expired_count': ((result.get('cleanup') or {}).get('expired_count', 0)),
        'matched_count': ((result.get('cleanup') or {}).get('matched_count', 0)),
        'result': result.get('cleanup') or {},
    }
    save_runtime_state(runtime)
    return result


def maybe_run_adaptive_rollout_orchestration(cfg: Config, db: Database, notifier: NotificationManager = None, force: bool = False) -> dict:
    runtime = load_runtime_state()
    orchestrator_cfg = cfg.get('runtime.adaptive_rollout_orchestration', {}) or {}
    enabled = bool(orchestrator_cfg.get('enabled', False))
    use_cache = bool(orchestrator_cfg.get('use_cache', True))
    notify_on_activity = bool(orchestrator_cfg.get('notify_on_activity', True))
    max_items = max(1, min(int(orchestrator_cfg.get('max_items', 5) or 5), 20))
    actor = str(orchestrator_cfg.get('actor') or 'system:runtime-adaptive-rollout-orchestration')
    min_interval_seconds = max(0, int(orchestrator_cfg.get('min_interval_seconds', 0) or 0))

    result = {
        'enabled': enabled,
        'force': bool(force),
        'use_cache': use_cache,
        'max_items': max_items,
        'min_interval_seconds': min_interval_seconds,
        'ran': False,
        'summary': None,
        'workflow_ready': None,
        'adaptive_rollout_orchestration': None,
        'runtime_orchestration_summary': None,
    }
    if not enabled and not force:
        result['reason'] = 'disabled'
        return result

    orchestration_runtime = runtime.get('adaptive_rollout_orchestration', {}) if isinstance(runtime.get('adaptive_rollout_orchestration'), dict) else {}
    last_run_at_raw = orchestration_runtime.get('last_run_at')
    now = datetime.now()
    last_run_at = None
    if last_run_at_raw:
        try:
            last_run_at = datetime.fromisoformat(str(last_run_at_raw))
        except ValueError:
            last_run_at = None
    if min_interval_seconds > 0 and not force and last_run_at is not None:
        elapsed_seconds = max(0, int((now - last_run_at).total_seconds()))
        remaining_seconds = max(0, min_interval_seconds - elapsed_seconds)
        if elapsed_seconds < min_interval_seconds:
            result.update({
                'reason': 'cooldown_active',
                'last_run_at': last_run_at.isoformat(),
                'elapsed_seconds': elapsed_seconds,
                'remaining_seconds': remaining_seconds,
                'next_eligible_run_at': (last_run_at + timedelta(seconds=min_interval_seconds)).isoformat(),
            })
            runtime['adaptive_rollout_orchestration'] = {
                **orchestration_runtime,
                'last_skip_at': now.isoformat(),
                'last_skip_reason': 'cooldown_active',
                'cooldown': {
                    'active': True,
                    'min_interval_seconds': min_interval_seconds,
                    'elapsed_seconds': elapsed_seconds,
                    'remaining_seconds': remaining_seconds,
                    'next_eligible_run_at': result['next_eligible_run_at'],
                    'last_run_at': last_run_at.isoformat(),
                },
            }
            save_runtime_state(runtime)
            return result

    backtester = StrategyBacktester(cfg)
    report = backtester.run_all(use_cache=use_cache)
    workflow_ready = export_calibration_payload(report, view='workflow_ready')
    payload = execute_adaptive_rollout_orchestration(workflow_ready, db, config=cfg, replay_source='runtime_adaptive_rollout_orchestration')
    runtime_summary = build_runtime_orchestration_summary(payload, max_items=max_items)
    orchestration = payload.get('adaptive_rollout_orchestration') or {}
    orchestration_summary = orchestration.get('summary') or {}
    rerun_observability = ((runtime_summary.get('summary') or {}).get('rerun_observability') or {}) if isinstance(runtime_summary.get('summary'), dict) else {}
    controlled_rollout_budget = ((runtime_summary.get('summary') or {}).get('controlled_rollout_budget') or {}) if isinstance(runtime_summary.get('summary'), dict) else {}
    auto_approval_budget = ((runtime_summary.get('summary') or {}).get('auto_approval_budget') or {}) if isinstance(runtime_summary.get('summary'), dict) else {}

    close_outcome_hint = runtime_summary.get('close_outcome_orchestration_hint') or {}
    result.update({
        'ran': True,
        'summary': orchestration_summary,
        'workflow_ready': workflow_ready,
        'adaptive_rollout_orchestration': orchestration,
        'runtime_orchestration_summary': runtime_summary,
        'close_outcome_orchestration_hint': close_outcome_hint,
    })

    runtime['adaptive_rollout_orchestration'] = {
        'last_run_at': datetime.now().isoformat(),
        'last_skip_at': None,
        'last_skip_reason': None,
        'enabled': enabled,
        'use_cache': use_cache,
        'actor': actor,
        'cooldown': {
            'active': False,
            'min_interval_seconds': min_interval_seconds,
            'elapsed_seconds': None,
            'remaining_seconds': 0,
            'next_eligible_run_at': None,
            'last_run_at': datetime.now().isoformat(),
        },
        'summary': {
            'schema_version': orchestration.get('schema_version'),
            'pass_count': int(orchestration_summary.get('pass_count', 0) or 0),
            'rerun_triggered': bool(orchestration_summary.get('rerun_triggered', False)),
            'rerun_reason': orchestration_summary.get('rerun_reason'),
            'rerun_reasons': orchestration_summary.get('rerun_reasons') or [],
            'rerun_count': int((rerun_observability.get('result_counts') or {}).get('rerun_pass_count', 0) or 0),
            'recovery_rerun_triggered': bool(rerun_observability.get('recovery_triggered', False)),
            'recovery_rerun_reasons': rerun_observability.get('recovery_reasons') or [],
            'recovery_retry_reentered_executor_count': int(orchestration_summary.get('recovery_retry_reentered_executor_count', 0) or 0),
            'gate_status': orchestration_summary.get('gate_status'),
            'gate_blocked': bool(orchestration_summary.get('gate_blocked', False)),
            'gate_blocking_issues': orchestration_summary.get('gate_blocking_issues') or [],
            'auto_approval_executed_count': int(orchestration_summary.get('auto_approval_executed_count', 0) or 0),
            'auto_approval_budget': auto_approval_budget,
            'auto_approval_budget_exhausted': bool(auto_approval_budget.get('exhausted', False)),
            'controlled_rollout_executed_count': int(orchestration_summary.get('controlled_rollout_executed_count', 0) or 0),
            'controlled_rollout_budget': controlled_rollout_budget,
            'controlled_rollout_budget_exhausted': bool(controlled_rollout_budget.get('exhausted', False)),
            'review_queue_queued_count': int(orchestration_summary.get('review_queue_queued_count', 0) or 0),
            'recovery_retry_scheduled_count': int(orchestration_summary.get('recovery_retry_scheduled_count', 0) or 0),
            'recovery_rollback_queued_count': int(orchestration_summary.get('recovery_rollback_queued_count', 0) or 0),
            'testnet_bridge_status': orchestration_summary.get('testnet_bridge_status'),
            'testnet_bridge_follow_up_required': bool(orchestration_summary.get('testnet_bridge_follow_up_required', False)),
            'close_outcome_action': close_outcome_hint.get('action'),
            'close_outcome_route': close_outcome_hint.get('route'),
            'close_outcome_rerun_required': bool(close_outcome_hint.get('rerun_required', False)),
            'close_outcome_freeze_auto_promotion': bool(close_outcome_hint.get('freeze_auto_promotion', False)),
            'close_outcome_rerun_reason': close_outcome_hint.get('rerun_reason'),
        },
        'runtime_summary': {
            'schema_version': runtime_summary.get('schema_version'),
            'headline': runtime_summary.get('headline') or {},
            'summary': runtime_summary.get('summary') or {},
            'rerun_observability': rerun_observability,
            'next_step': runtime_summary.get('next_step') or {},
            'stuck_points': (runtime_summary.get('stuck_points') or [])[:max_items],
            'follow_ups': runtime_summary.get('follow_ups') or {},
            'close_outcome_orchestration_hint': close_outcome_hint,
        },
    }
    save_runtime_state(runtime)

    activity_total = sum([
        int(orchestration_summary.get('auto_approval_executed_count', 0) or 0),
        int(orchestration_summary.get('controlled_rollout_executed_count', 0) or 0),
        int(orchestration_summary.get('review_queue_queued_count', 0) or 0),
        int(orchestration_summary.get('recovery_retry_scheduled_count', 0) or 0),
        int(orchestration_summary.get('recovery_rollback_queued_count', 0) or 0),
    ])
    if notifier is not None and (force or (notify_on_activity and (activity_total > 0 or bool(orchestration_summary.get('gate_blocked', False))))):
        lines = [
            f"gate={orchestration_summary.get('gate_status') or '--'} ｜ blocked={'yes' if orchestration_summary.get('gate_blocked') else 'no'} ｜ passes={orchestration_summary.get('pass_count', 0)}",
            f"auto-approval={orchestration_summary.get('auto_approval_executed_count', 0)} ｜ rollout={orchestration_summary.get('controlled_rollout_executed_count', 0)} ｜ review-queue={orchestration_summary.get('review_queue_queued_count', 0)}",
            f"auto-approval-budget limit={auto_approval_budget.get('max_executed_per_pass', 0) or 'unlimited'} ｜ remaining={auto_approval_budget.get('remaining_slots', '--')} ｜ exhausted={'yes' if auto_approval_budget.get('exhausted') else 'no'} ｜ budget-skips={auto_approval_budget.get('skipped_by_budget', 0)}",
            f"rollout-budget limit={controlled_rollout_budget.get('max_executed_per_pass', 0) or 'unlimited'} ｜ remaining={controlled_rollout_budget.get('remaining_slots', '--')} ｜ exhausted={'yes' if controlled_rollout_budget.get('exhausted') else 'no'} ｜ budget-skips={controlled_rollout_budget.get('skipped_by_budget', 0)}",
            f"recovery retry={orchestration_summary.get('recovery_retry_scheduled_count', 0)} ｜ reentered={orchestration_summary.get('recovery_retry_reentered_executor_count', 0)} ｜ rollback={orchestration_summary.get('recovery_rollback_queued_count', 0)} ｜ bridge={orchestration_summary.get('testnet_bridge_status') or 'disabled'}",
        ]
        if rerun_observability.get('triggered'):
            lines.append(
                f"rerun={rerun_observability.get('primary_reason') or '--'} ｜ count={(rerun_observability.get('result_counts') or {}).get('rerun_pass_count', 0)} ｜ recovery={'yes' if rerun_observability.get('recovery_triggered') else 'no'} ｜ reasons={','.join(rerun_observability.get('reasons') or []) or '--'}"
            )
        if close_outcome_hint:
            lines.append(
                f"close-outcome={close_outcome_hint.get('action') or '--'} ｜ route={close_outcome_hint.get('route') or '--'} ｜ rerun={'yes' if close_outcome_hint.get('rerun_required') else 'no'} ｜ freeze={'yes' if close_outcome_hint.get('freeze_auto_promotion') else 'no'}"
            )
        next_step = runtime_summary.get('next_step') or {}
        if next_step:
            lines.append(f"next-step：{next_step.get('summary') or next_step.get('action') or '--'}")
        notifier.notify_runtime('adaptive_rollout_orchestration', lines, {
            'summary': orchestration_summary,
            'runtime_orchestration_summary': runtime_summary,
        })

    return result


def build_runtime_health_summary(cfg: Config, db: Database) -> dict:
    runtime = load_runtime_state()
    risk = RiskManager(cfg, db).get_risk_status()
    balance = risk.get('balance', {}) if isinstance(risk, dict) else {}
    mode = PresetManager(cfg).status()
    ml_engine = MLEngine(cfg.all)
    model_rows = []
    for symbol in cfg.symbols:
        metrics = ml_engine.get_model_metrics(symbol) or {}
        model_rows.append({
            'symbol': symbol,
            'test_accuracy': metrics.get('test_accuracy'),
            'f1': metrics.get('f1'),
            'model_file': metrics.get('model_file'),
        })

    last_summary = runtime.get('last_summary') or {}
    adaptive_cfg = cfg.get_adaptive_regime_config() if hasattr(cfg, 'get_adaptive_regime_config') else (cfg.get('adaptive_regime', {}) or {})
    adaptive_defaults = adaptive_cfg.get('defaults', {}) if isinstance(adaptive_cfg, dict) else {}
    adaptive_mode = adaptive_cfg.get('mode', 'observe_only') if isinstance(adaptive_cfg, dict) else 'observe_only'
    adaptive_enabled = bool(adaptive_cfg.get('enabled', False)) if isinstance(adaptive_cfg, dict) else False
    approval_hygiene = build_approval_hygiene_summary(cfg, db)
    approval_runtime = runtime.get('approval_hygiene', {}) if isinstance(runtime.get('approval_hygiene'), dict) else {}
    approval_mode = 'auto-cleanup' if approval_hygiene.get('auto_cleanup_enabled') else 'audit-only'
    orchestration_runtime = runtime.get('adaptive_rollout_orchestration', {}) if isinstance(runtime.get('adaptive_rollout_orchestration'), dict) else {}
    orchestration_cfg = cfg.get('runtime.adaptive_rollout_orchestration', {}) or {}
    orchestration_enabled = bool(orchestration_cfg.get('enabled', False))
    orchestration_summary = orchestration_runtime.get('summary', {}) if isinstance(orchestration_runtime.get('summary'), dict) else {}
    orchestration_runtime_summary = orchestration_runtime.get('runtime_summary', {}) if isinstance(orchestration_runtime.get('runtime_summary'), dict) else {}
    orchestration_cooldown = orchestration_runtime.get('cooldown', {}) if isinstance(orchestration_runtime.get('cooldown'), dict) else {}
    orchestration_rerun = orchestration_runtime_summary.get('rerun_observability', {}) if isinstance(orchestration_runtime_summary.get('rerun_observability'), dict) else {}
    controlled_rollout_budget = orchestration_summary.get('controlled_rollout_budget', {}) if isinstance(orchestration_summary.get('controlled_rollout_budget'), dict) else {}
    auto_approval_budget = orchestration_summary.get('auto_approval_budget', {}) if isinstance(orchestration_summary.get('auto_approval_budget'), dict) else {}
    lines = [
        f'环境：{cfg.exchange_mode}',
        f'监听币种：{", ".join(cfg.symbols) or "--"}',
        f'当前 preset：{mode.get("current_preset") or "manual"}',
        f'守护间隔：{runtime.get("interval_seconds") or cfg.get("runtime.interval_seconds", 300)} 秒',
        f'下次运行：{runtime.get("next_run_at") or "--"}',
        '---',
        f'最近一轮：signals {last_summary.get("signals", 0)} ｜ passed {last_summary.get("passed", 0)} ｜ opened {last_summary.get("opened", 0)} ｜ errors {last_summary.get("errors", 0)}',
        f'风险状态：{risk.get("status") or "--"} ｜ 暴露 {risk.get("current_exposure", 0)} / {risk.get("max_exposure", 0)}',
        f'余额：total {round(float(balance.get("total", 0) or 0), 2)} ｜ free {round(float(balance.get("free", 0) or 0), 2)}',
        '---',
        f'Adaptive Regime：mode={adaptive_mode} ｜ enabled={"yes" if adaptive_enabled else "no"} ｜ policy={adaptive_defaults.get("policy_version") or "--"}',
        '说明：当前仍为 observe-only 展示层，不改真实交易行为。',
        '---',
        f'Approval Hygiene：mode={approval_mode} ｜ stale={approval_hygiene.get("stale_count", 0)} ｜ decision diff={approval_hygiene.get("decision_diff_count", 0)}',
        f'审批卫生：stale>{approval_hygiene.get("stale_after_minutes", 0)} 分钟 ｜ 上次处理 {approval_runtime.get("last_run_at") or "--"} ｜ 上次过期收口 {approval_runtime.get("expired_count", 0)}',
        f'Operator routing：{approval_hygiene.get("operator_action_summary", {}).get("policy_counts") or {}}',
        '---',
        f'Adaptive Rollout Orchestration：enabled={"yes" if orchestration_enabled else "no"} ｜ gate={orchestration_summary.get("gate_status") or "--"} ｜ blocked={"yes" if orchestration_summary.get("gate_blocked") else "no"}',
        f'编排执行：auto-approval {orchestration_summary.get("auto_approval_executed_count", 0)} ｜ rollout {orchestration_summary.get("controlled_rollout_executed_count", 0)} ｜ review {orchestration_summary.get("review_queue_queued_count", 0)}',
        f'Auto-approval budget：limit={auto_approval_budget.get("max_executed_per_pass", 0) or "unlimited"} ｜ remaining={auto_approval_budget.get("remaining_slots", "--")} ｜ exhausted={"yes" if auto_approval_budget.get("exhausted") else "no"} ｜ skipped={auto_approval_budget.get("skipped_by_budget", 0)}',
        f'Rollout budget：limit={controlled_rollout_budget.get("max_executed_per_pass", 0) or "unlimited"} ｜ remaining={controlled_rollout_budget.get("remaining_slots", "--")} ｜ exhausted={"yes" if controlled_rollout_budget.get("exhausted") else "no"} ｜ skipped={controlled_rollout_budget.get("skipped_by_budget", 0)}',
        f'Recovery rerun：triggered={"yes" if orchestration_rerun.get("recovery_triggered") else "no"} ｜ reason={orchestration_rerun.get("primary_reason") or "--"} ｜ count={((orchestration_rerun.get("result_counts") or {}).get("rerun_pass_count", 0))}',
        f'Recovery lane：retry {orchestration_summary.get("recovery_retry_scheduled_count", 0)} ｜ reentered {orchestration_summary.get("recovery_retry_reentered_executor_count", 0)} ｜ rollback {orchestration_summary.get("recovery_rollback_queued_count", 0)} ｜ manual-note {orchestration_summary.get("recovery_manual_annotation_count", 0)}',
        f'编排节流：interval={orchestration_cooldown.get("min_interval_seconds", 0)}s ｜ cooldown={"active" if orchestration_cooldown.get("active") else "idle"} ｜ remaining={orchestration_cooldown.get("remaining_seconds", 0) or 0}s',
        f'上次编排：{orchestration_runtime.get("last_run_at") or "--"} ｜ next-step {((orchestration_runtime_summary.get("next_step") or {}).get("summary") or (orchestration_runtime_summary.get("next_step") or {}).get("action") or "--")}',
    ]
    if risk.get('loss_streak_locked'):
        lines.extend(['---', f'连亏熔断：{risk.get("consecutive_losses", 0)}/{risk.get("max_consecutive_losses", 0)} ｜ 自动恢复 {risk.get("loss_streak_recover_at") or "--"}'])
    if model_rows:
        model_text = ' ｜ '.join([
            f"{row['symbol']}: acc={row['test_accuracy'] if row['test_accuracy'] is not None else '--'}, f1={row['f1'] if row['f1'] is not None else '--'}"
            for row in model_rows
        ])
        lines.extend(['---', f'ML 状态：{model_text}'])
    return {
        'title': '🩺 每日健康汇总',
        'lines': lines,
        'details': {
            'runtime': runtime,
            'risk': risk,
            'mode': mode,
            'models': model_rows,
            'approval_hygiene': approval_hygiene,
            'adaptive_rollout_orchestration': {
                'enabled': orchestration_enabled,
                'runtime': orchestration_runtime,
                'summary': orchestration_summary,
                'runtime_summary': orchestration_runtime_summary,
            },
        }
    }


def maybe_send_daily_health_summary(cfg: Config, db: Database, notifier: NotificationManager, force: bool = False) -> dict:
    runtime = load_runtime_state()
    health_cfg = cfg.get('runtime.health_summary', {}) or {}
    enabled = bool(health_cfg.get('enabled', True))
    hour = int(health_cfg.get('hour', 20) or 20)
    now = datetime.now()
    health_state = runtime.get('health_summary', {}) if isinstance(runtime.get('health_summary'), dict) else {}
    today = now.strftime('%Y-%m-%d')

    if not force:
        if not enabled:
            return {'sent': False, 'reason': 'disabled'}
        if health_state.get('last_sent_date') == today:
            return {'sent': False, 'reason': 'already-sent'}
        if now.hour < hour:
            return {'sent': False, 'reason': 'before-hour', 'hour': hour}

    summary = build_runtime_health_summary(cfg, db)
    result = notifier.send('runtime', summary['title'], summary['lines'], 'info', summary['details'], priority='normal')
    runtime['health_summary'] = {
        'last_sent_at': now.isoformat(),
        'last_sent_date': today,
        'hour': hour,
        'result': result,
    }
    save_runtime_state(runtime)
    return {'sent': True, 'summary': summary, 'result': result}


def build_exchange_diagnostics(cfg: Config, exchange: Exchange) -> dict:
    """构建交易所诊断信息（只读，不下单）"""
    report = {
        'exchange_mode': cfg.exchange_mode,
        'position_mode': cfg.position_mode,
        'symbols': [],
        'balance_error': None,
    }

    available = 0
    try:
        balance = exchange.fetch_balance()
        available = float((balance.get('free') or {}).get('USDT', 0) or 0)
        report['available_usdt'] = round(available, 4)
    except Exception as e:
        report['available_usdt'] = 0
        report['balance_error'] = str(e)

    desired_notional = available * float(cfg.position_size or 0) * float(cfg.leverage or 0)

    for symbol in cfg.symbols:
        row = {'symbol': symbol}
        try:
            row['is_futures_symbol'] = bool(exchange.is_futures_symbol(symbol))
            if row['is_futures_symbol']:
                row['order_symbol'] = exchange.get_order_symbol(symbol)
                ticker = exchange.fetch_ticker(symbol)
                row['last_price'] = ticker.get('last')
                if row['last_price']:
                    row['sample_amount'] = exchange.normalize_contract_amount(symbol, desired_notional, row['last_price'])
                preview = {'tdMode': 'isolated'}
                if str(cfg.position_mode).lower() not in {'oneway', 'one-way', 'net', 'single'}:
                    preview['posSide'] = 'long'
                row['order_params_preview'] = preview
            else:
                row['reason'] = 'not-swap-market'
        except Exception as e:
            row['error'] = str(e)
        report['symbols'].append(row)

    return report


def build_exchange_smoke_plan(cfg: Config, exchange: Exchange, symbol: str = None, side: str = 'long') -> dict:
    """构建最小 testnet 验收计划；默认只预演，不落单"""
    selected_symbol = symbol or (cfg.symbols[0] if cfg.symbols else None)
    plan = {
        'exchange_mode': cfg.exchange_mode,
        'position_mode': cfg.position_mode,
        'symbol': selected_symbol,
        'side': side,
        'execute_ready': False,
        'steps': [
            '读取余额',
            '检查目标是否为 U 本位永续',
            '获取最新价格',
            '换算最小验收仓位数量',
            '预览开仓参数',
            '预览平仓参数',
        ]
    }
    if not selected_symbol:
        plan['error'] = '未配置任何 watch_list 币种'
        return plan
    try:
        balance = exchange.fetch_balance()
        available = float((balance.get('free') or {}).get('USDT', 0) or 0)
        plan['available_usdt'] = round(available, 4)
        plan['is_futures_symbol'] = bool(exchange.is_futures_symbol(selected_symbol))
        if not plan['is_futures_symbol']:
            plan['error'] = '目标币种不是可用合约'
            return plan
        ticker = exchange.fetch_ticker(selected_symbol)
        last_price = float(ticker.get('last') or 0)
        plan['last_price'] = last_price
        smoke_notional = max(5.0, available * 0.01)
        plan['smoke_notional'] = round(smoke_notional, 4)
        plan['sample_amount'] = exchange.normalize_contract_amount(selected_symbol, smoke_notional, last_price)
        preview_open = {'tdMode': 'isolated'}
        preview_close = {'tdMode': 'isolated', 'reduceOnly': True}
        if str(cfg.position_mode).lower() not in {'oneway', 'one-way', 'net', 'single'}:
            preview_open['posSide'] = side
            preview_close['posSide'] = side
        plan['open_preview'] = {
            'symbol': exchange.get_order_symbol(selected_symbol),
            'side': 'buy' if side == 'long' else 'sell',
            'amount': plan['sample_amount'],
            'params': preview_open,
        }
        plan['close_preview'] = {
            'symbol': exchange.get_order_symbol(selected_symbol),
            'side': 'sell' if side == 'long' else 'buy',
            'amount': plan['sample_amount'],
            'params': preview_close,
        }
        plan['execute_ready'] = True
    except Exception as e:
        plan['error'] = str(e)
    return plan




def backfill_closed_trades_from_exchange(exchange: Exchange, db: Database, limit: int = 50) -> dict:
    report = {'checked': 0, 'patched': 0, 'errors': []}
    report['mapping_repair'] = db.repair_trade_quantity_mappings(symbols=['BTC/USDT', 'XRP/USDT'])
    for trade in db.get_trades_missing_close_details(limit=limit):
        report['checked'] += 1
        try:
            fallback_price = trade.get('exit_price') or None
            if not fallback_price:
                try:
                    ticker = exchange.fetch_ticker(trade['symbol'])
                    fallback_price = ticker.get('last')
                except Exception:
                    fallback_price = None
            summary = exchange.fetch_closed_trade_summary(trade, fallback_price=fallback_price)
            if not summary and fallback_price:
                summary = exchange.build_close_summary([], open_trade=trade, fallback_price=fallback_price, source='ticker_fallback')
            if summary and db.reconcile_trade_close(trade['id'], summary, reason='历史已关闭交易补齐'):
                report['patched'] += 1
        except Exception as e:
            report['errors'].append({'trade_id': trade.get('id'), 'symbol': trade.get('symbol'), 'error': str(e)})
    return report

def reconcile_exchange_positions(exchange: Exchange, db: Database) -> dict:
    """交易所持仓与本地 DB / open trades 三方对账（第二轮）"""
    report = {
        'synced': 0,
        'removed': 0,
        'exchange_positions': [],
        'local_before': db.get_positions(),
        'local_open_trades': db.get_trades(status='open', limit=200),
    }
    exchange_positions = exchange.fetch_positions()
    report['history_backfill'] = backfill_closed_trades_from_exchange(exchange, db, limit=50)
    normalized_symbols = []
    normalized_keys = []
    created_open_trades = []
    for pos in exchange_positions:
        if pos.get('contract_size') is not None:
            normalized = pos
        elif hasattr(exchange, 'normalize_position'):
            normalized = exchange.normalize_position(pos)
        else:
            raw_symbol = pos.get('symbol')
            normalized = {
                'symbol': raw_symbol.split(':')[0] if isinstance(raw_symbol, str) and ':' in raw_symbol else raw_symbol,
                'side': pos.get('side'),
                'quantity': pos.get('quantity', pos.get('contracts', 0)),
                'contracts': pos.get('contracts', pos.get('quantity', 0)),
                'entry_price': pos.get('entry_price', pos.get('entryPrice', 0)),
                'current_price': pos.get('current_price', pos.get('markPrice', pos.get('entryPrice', 0))),
                'leverage': pos.get('leverage', 1),
                'contract_size': pos.get('contract_size', 1),
                'coin_quantity': pos.get('coin_quantity', (pos.get('contracts', pos.get('quantity', 0)) or 0) * (pos.get('contract_size', 1) or 1)),
                'realized_pnl': pos.get('realized_pnl'),
            }
        if not normalized:
            continue
        symbol = normalized['symbol']
        side = normalized['side']
        contracts = float(normalized.get('quantity') or normalized.get('contracts') or 0)
        if contracts <= 0:
            continue
        entry_price = float(normalized.get('entry_price') or 0)
        current_price = float(normalized.get('current_price') or entry_price or 0)
        leverage = int(float(normalized.get('leverage') or 1))
        contract_size = float(normalized.get('contract_size') or 1)
        coin_quantity = float(normalized.get('coin_quantity') or contracts * contract_size)
        db.update_position(symbol, side, entry_price, contracts, leverage, current_price, contract_size=contract_size, coin_quantity=coin_quantity)
        existing_open_trade = db.get_latest_open_trade(symbol, side)
        if not existing_open_trade:
            trade_id = db.record_trade(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                quantity=contracts,
                leverage=leverage,
                notes='对账补建 open trade（以交易所当前持仓为准）',
                contract_size=contract_size,
                coin_quantity=coin_quantity,
            )
            created_open_trades.append({'trade_id': trade_id, 'symbol': symbol, 'side': side, 'quantity': contracts, 'coin_quantity': coin_quantity, 'leverage': leverage})
        else:
            db.sync_trade_with_exchange_snapshot(
                existing_open_trade['id'],
                quantity=contracts,
                contract_size=contract_size,
                coin_quantity=coin_quantity,
                leverage=leverage,
                entry_price=entry_price,
                notes='交易所持仓对账同步',
            )
        normalized_symbols.append(symbol)
        normalized_keys.append(f'{symbol}::{side}')
        report['exchange_positions'].append({'symbol': symbol, 'side': side, 'quantity': contracts, 'coin_quantity': coin_quantity, 'entry_price': entry_price, 'current_price': current_price, 'leverage': leverage, 'realized_pnl': normalized.get('realized_pnl')})
        report['synced'] += 1
    report['removed'] = db.remove_positions_not_in(normalized_symbols)
    report['local_after'] = db.get_positions()
    report['local_open_trades'] = db.get_trades(status='open', limit=200)

    local_after = report['local_after']
    local_open_trades = report['local_open_trades']
    local_position_keys = {f"{p.get('symbol')}::{p.get('side')}" for p in local_after}
    open_trade_keys = {f"{t.get('symbol')}::{t.get('side')}" for t in local_open_trades}
    exchange_key_set = set(normalized_keys)

    report['diff'] = {
        'exchange_missing_local_position': [row for row in report['exchange_positions'] if f"{row['symbol']}::{row['side']}" not in local_position_keys],
        'local_position_missing_exchange': [row for row in local_after if f"{row.get('symbol')}::{row.get('side')}" not in exchange_key_set],
        'open_trade_missing_exchange': [row for row in local_open_trades if f"{row.get('symbol')}::{row.get('side')}" not in exchange_key_set],
        'exchange_missing_open_trade': [row for row in report['exchange_positions'] if f"{row['symbol']}::{row['side']}" not in open_trade_keys],
    }
    healed_open_trades = []
    for row in list(report['diff']['exchange_missing_open_trade']):
        symbol = row.get('symbol')
        side = row.get('side')
        if not symbol or not side:
            continue
        if db.get_latest_open_trade(symbol, side):
            continue
        matched_local = next((p for p in local_after if p.get('symbol') == symbol and p.get('side') == side), None)
        base_row = matched_local or row
        trade_id = db.record_trade(
            symbol=symbol,
            side=side,
            entry_price=float(base_row.get('entry_price') or 0),
            quantity=float(base_row.get('quantity') or 0),
            leverage=int(float(base_row.get('leverage') or 1)),
            notes='对账自愈补建 open trade（exchange/local snapshot fallback）',
            contract_size=float(base_row.get('contract_size') or 1),
            coin_quantity=float(base_row.get('coin_quantity') or 0),
        )
        healed_open_trades.append({'trade_id': trade_id, 'symbol': symbol, 'side': side})
    if healed_open_trades:
        report['local_open_trades'] = db.get_trades(status='open', limit=200)
        local_open_trades = report['local_open_trades']
        open_trade_keys = {f"{t.get('symbol')}::{t.get('side')}" for t in local_open_trades}
        report['diff']['exchange_missing_open_trade'] = [row for row in report['exchange_positions'] if f"{row['symbol']}::{row['side']}" not in open_trade_keys]
    stale_closed = []
    for row in report['diff']['open_trade_missing_exchange']:
        trade_id = row.get('id')
        symbol = row.get('symbol')
        side = row.get('side')
        current_price = None
        matched_local = next((p for p in local_after if p.get('symbol') == symbol and p.get('side') == side), None)
        if matched_local:
            current_price = matched_local.get('current_price') or matched_local.get('entry_price')
        if trade_id:
            summary = None
            try:
                summary = exchange.fetch_closed_trade_summary(row, fallback_price=current_price)
            except Exception:
                summary = None
            if not summary and current_price:
                summary = exchange.build_close_summary([], open_trade=row, fallback_price=current_price, source='ticker_fallback')
            changed = db.reconcile_trade_close(trade_id, summary or {'exit_price': current_price, 'source': 'reconcile_fallback', 'fills': []}, reason='自动收口: 交易所无对应持仓，对账自动收口')
            if changed:
                stale_closed.append({'trade_id': trade_id, 'symbol': symbol, 'side': side, 'close_source': (summary or {}).get('source', 'reconcile_fallback')})
    if stale_closed:
        report['local_open_trades'] = db.get_trades(status='open', limit=200)
        local_open_trades = report['local_open_trades']
        open_trade_keys = {f"{t.get('symbol')}::{t.get('side')}" for t in local_open_trades}
        report['diff']['open_trade_missing_exchange'] = [row for row in local_open_trades if f"{row.get('symbol')}::{row.get('side')}" not in exchange_key_set]
        report['diff']['exchange_missing_open_trade'] = [row for row in report['exchange_positions'] if f"{row['symbol']}::{row['side']}" not in open_trade_keys]
    touched_pairs = {(row.get('symbol'), row.get('side')) for row in report['exchange_positions'] if row.get('symbol') and row.get('side')}
    touched_pairs.update((row.get('symbol'), row.get('side')) for row in stale_closed if row.get('symbol') and row.get('side'))
    touched_pairs.update((row.get('symbol'), row.get('side')) for row in created_open_trades if row.get('symbol') and row.get('side'))
    touched_pairs.update((row.get('symbol'), row.get('side')) for row in healed_open_trades if row.get('symbol') and row.get('side'))
    if not touched_pairs:
        touched_pairs.update((row.get('symbol'), row.get('side')) for row in report.get('local_before', []) if row.get('symbol') and row.get('side'))
    report['layer_state_sync'] = [db.sync_layer_plan_state(symbol, side, reset_if_flat=True) for symbol, side in sorted(touched_pairs)]
    report['orphan_cleanup'] = db.cleanup_orphan_execution_state(stale_after_minutes=15)

    report['summary'] = {
        'exchange_positions': len(report['exchange_positions']),
        'local_positions': len(local_after),
        'open_trades': len(local_open_trades),
        'created_open_trades': len(created_open_trades),
        'healed_open_trades': len(healed_open_trades),
        'exchange_missing_local_position': len(report['diff']['exchange_missing_local_position']),
        'local_position_missing_exchange': len(report['diff']['local_position_missing_exchange']),
        'open_trade_missing_exchange': len(report['diff']['open_trade_missing_exchange']),
        'exchange_missing_open_trade': len(report['diff']['exchange_missing_open_trade']),
        'stale_open_trades_closed': len(stale_closed),
        'history_backfilled': int((report.get('history_backfill') or {}).get('patched', 0) or 0),
        'layer_states_synced': len(report['layer_state_sync']),
        'orphan_intents_cleaned': len((report.get('orphan_cleanup') or {}).get('removed_intents', [])),
        'orphan_intents_healed': len((report.get('orphan_cleanup') or {}).get('healed_intents', [])),
        'orphan_locks_cleaned': len((report.get('orphan_cleanup') or {}).get('removed_locks', [])),
        'orphan_locks_healed': len((report.get('orphan_cleanup') or {}).get('healed_locks', [])),
        'layer_plan_resets': len((report.get('orphan_cleanup') or {}).get('plan_resets', [])),
    }
    report['stale_closed'] = stale_closed
    report['created_open_trades'] = created_open_trades
    report['healed_open_trades'] = healed_open_trades
    return report


def execute_exchange_smoke(cfg: Config, exchange: Exchange, symbol: str = None, side: str = 'long', db: Database = None) -> dict:
    """执行最小 testnet 开平仓验收。只允许 testnet。"""
    plan = build_exchange_smoke_plan(cfg, exchange, symbol=symbol, side=side)
    result = {
        'plan': plan,
        'opened': False,
        'closed': False,
        'open_status': 'not_started',
        'close_status': 'not_started',
        'cleanup_needed': False,
        'residual_position_detected': False,
        'reconcile_summary': {
            'open_order_confirmed': False,
            'close_order_confirmed': False,
            'residual_position_detected': False,
            'cleanup_attempted': False,
            'cleanup_succeeded': False,
            'residual_quantity': 0.0,
        },
        'failure_compensation_hint': None,
    }

    def _call_exchange_method(name, *args, **kwargs):
        fn = getattr(exchange, name, None)
        if not callable(fn):
            return None
        return fn(*args, **kwargs)

    def _normalize_status(payload, default_status):
        if isinstance(payload, dict):
            status = str(payload.get('status') or default_status)
            return {**payload, 'status': status}
        if payload is None:
            return {'status': default_status}
        return {'status': str(payload)}

    def _normalize_residual(payload):
        if isinstance(payload, dict):
            quantity = float(payload.get('quantity') or payload.get('contracts') or payload.get('residual_quantity') or 0.0)
            detected = bool(payload.get('detected', quantity > 0))
            return {**payload, 'detected': detected, 'quantity': quantity}
        quantity = float(payload or 0.0)
        return {'detected': quantity > 0, 'quantity': quantity}

    if plan.get('error'):
        result['error'] = plan['error']
        result['open_status'] = 'plan_error'
        result['close_status'] = 'plan_error'
        result['failure_compensation_hint'] = 'fix_smoke_plan_before_execute'
    elif str(cfg.exchange_mode).lower() != 'testnet':
        result['error'] = '只允许在 testnet 模式执行 smoke 验收'
        result['open_status'] = 'blocked'
        result['close_status'] = 'blocked'
        result['failure_compensation_hint'] = 'real_mode_blocked_no_execution'
    else:
        try:
            open_side = 'buy' if side == 'long' else 'sell'
            close_side = 'sell' if side == 'long' else 'buy'
            amount = plan['sample_amount']
            open_order = exchange.create_order(plan['symbol'], open_side, amount, posSide=side)
            result['opened'] = True
            result['open_order'] = open_order
            open_status_payload = _normalize_status(_call_exchange_method('confirm_smoke_open', plan['symbol'], side, amount, open_order=open_order), 'filled')
            result['open_status'] = open_status_payload.get('status', 'filled')
            result['open_confirmation'] = open_status_payload
            result['reconcile_summary']['open_order_confirmed'] = result['open_status'] in {'filled', 'closed', 'confirmed', 'opened'}

            close_order = exchange.close_order(plan['symbol'], close_side, amount, posSide=side)
            result['closed'] = True
            result['close_order'] = close_order
            close_status_payload = _normalize_status(_call_exchange_method('confirm_smoke_close', plan['symbol'], side, amount, close_order=close_order, open_order=open_order), 'filled')
            result['close_status'] = close_status_payload.get('status', 'filled')
            result['close_confirmation'] = close_status_payload
            result['reconcile_summary']['close_order_confirmed'] = result['close_status'] in {'filled', 'closed', 'confirmed'}

            residual_payload = _normalize_residual(_call_exchange_method('detect_smoke_residual_position', plan['symbol'], side, amount, open_order=open_order, close_order=close_order))
            result['residual_position'] = residual_payload
            result['residual_position_detected'] = residual_payload.get('detected', False)
            result['reconcile_summary']['residual_position_detected'] = result['residual_position_detected']
            result['reconcile_summary']['residual_quantity'] = residual_payload.get('quantity', 0.0)

            cleanup_needed = (not result['reconcile_summary']['close_order_confirmed']) or result['residual_position_detected']
            result['cleanup_needed'] = cleanup_needed
            if cleanup_needed:
                cleanup_payload = _normalize_status(_call_exchange_method('cleanup_smoke_position', plan['symbol'], side, amount, open_order=open_order, close_order=close_order, residual_position=residual_payload), 'not_attempted')
                result['cleanup_result'] = cleanup_payload
                result['reconcile_summary']['cleanup_attempted'] = cleanup_payload.get('status') != 'not_attempted'
                result['reconcile_summary']['cleanup_succeeded'] = cleanup_payload.get('status') in {'flattened', 'clean', 'confirmed', 'success'}
                if not result['reconcile_summary']['cleanup_succeeded']:
                    if result['residual_position_detected']:
                        result['failure_compensation_hint'] = 'manual_testnet_cleanup_required'
                    else:
                        result['failure_compensation_hint'] = 'retry_close_confirmation_or_manual_cleanup'
            if not result['cleanup_needed']:
                result['failure_compensation_hint'] = None
        except Exception as e:
            result['error'] = str(e)
            result['cleanup_needed'] = bool(result.get('opened') and not result.get('closed'))
            result['failure_compensation_hint'] = 'inspect_open_order_and_force_flatten_on_testnet' if result['cleanup_needed'] else 'inspect_bridge_execution_error'
            if result['opened'] and result['open_status'] == 'not_started':
                result['open_status'] = 'submitted'
            if result['close_status'] == 'not_started':
                result['close_status'] = 'error'

    if db is not None:
        details = {
            'plan': plan,
            'opened': result.get('opened', False),
            'closed': result.get('closed', False),
            'open_order': result.get('open_order'),
            'close_order': result.get('close_order'),
            'open_status': result.get('open_status'),
            'close_status': result.get('close_status'),
            'cleanup_needed': result.get('cleanup_needed', False),
            'residual_position_detected': result.get('residual_position_detected', False),
            'reconcile_summary': result.get('reconcile_summary') or {},
            'failure_compensation_hint': result.get('failure_compensation_hint'),
        }
        smoke_run_id = db.record_smoke_run(
            exchange_mode=cfg.exchange_mode,
            position_mode=cfg.position_mode,
            symbol=plan.get('symbol') or symbol or '--',
            side=side,
            amount=plan.get('sample_amount'),
            success=bool(result.get('opened') and result.get('closed') and not result.get('error') and not result.get('cleanup_needed')),
            error=result.get('error'),
            details=details,
        )
        result['smoke_run_id'] = smoke_run_id
    return result


class RuntimeGuard:
    def __init__(self, lock_path: str = '/tmp/crypto_quant_okx_bot.lock'):
        self.lock_path = Path(lock_path)
        self.locked = False

    def acquire(self) -> bool:
        if self.lock_path.exists():
            try:
                pid = int(self.lock_path.read_text().strip() or 0)
                if pid > 0:
                    os.kill(pid, 0)
                    return False
            except Exception:
                pass
            try:
                self.lock_path.unlink()
            except Exception:
                return False
        self.lock_path.write_text(str(os.getpid()))
        self.locked = True
        return True

    def release(self):
        if self.locked and self.lock_path.exists():
            try:
                self.lock_path.unlink()
            except Exception:
                pass
        self.locked = False


class TradingBot:
    """交易机器人主类"""
    
    def __init__(self):
        self.config = Config()
        self.db = Database(self.config.db_path)
        self.exchange = Exchange(self.config.all)
        self.detector = SignalDetector(self.config.all)
        self.validator = SignalValidator(self.config, self.exchange)
        self.recorder = SignalRecorder(self.db)
        self.entry_decider = EntryDecider(self.config.all)  # Entry Decision Layer MVP
        self.executor = TradingExecutor(self.config, self.exchange, self.db)
        self.risk_mgr = RiskManager(self.config, self.db)
        self.ml = MLEngine(self.config.all)
        self.notifier = NotificationManager(self.config, self.db, logger)
        
        logger.info("交易机器人初始化完成")

    def _strategy_selection_config(self, symbol: str = None) -> dict:
        raw = self.config.get_symbol_value(symbol, 'adaptive_regime.strategy_selection', None) if symbol else self.config.get('adaptive_regime.strategy_selection', None)
        if not isinstance(raw, dict):
            raw = {}
        return {
            'enabled': bool(raw.get('enabled', True)),
            'lookback_limit': max(int(raw.get('lookback_limit', 30) or 30), 1),
            'min_trades_for_preference': max(int(raw.get('min_trades_for_preference', 2) or 2), 1),
            'min_selected_strategies': max(int(raw.get('min_selected_strategies', 1) or 1), 1),
            'max_selected_strategies': max(int(raw.get('max_selected_strategies', 3) or 3), 1),
            'base_budget_ratio': min(max(float(raw.get('base_budget_ratio', 1.0) or 1.0), 0.0), 1.0),
            'min_budget_ratio': min(max(float(raw.get('min_budget_ratio', 0.2) or 0.2), 0.0), 1.0),
            'max_budget_ratio': min(max(float(raw.get('max_budget_ratio', 1.0) or 1.0), 0.0), 1.0),
            'slot_bonus_decay': max(float(raw.get('slot_bonus_decay', 0.12) or 0.12), 0.0),
            'slot_penalty_decay': max(float(raw.get('slot_penalty_decay', 0.1) or 0.1), 0.0),
            'regime_slot_caps': dict(raw.get('regime_slot_caps') or {}),
            'regime_budget_overrides': dict(raw.get('regime_budget_overrides') or {}),
            'deweight_negative_return_multiplier': float(raw.get('deweight_negative_return_multiplier', 0.7) or 0.7),
            'deweight_loss_streak_multiplier': float(raw.get('deweight_loss_streak_multiplier', 0.75) or 0.75),
            'deweight_mismatch_multiplier': float(raw.get('deweight_mismatch_multiplier', 0.8) or 0.8),
            'deweight_low_sample_multiplier': float(raw.get('deweight_low_sample_multiplier', 0.9) or 0.9),
            'disable_on_rollback_match': bool(raw.get('disable_on_rollback_match', True)),
            'strategy_cooldown_enabled': bool(raw.get('strategy_cooldown_enabled', True)),
            'strategy_cooldown_hours': max(float(raw.get('strategy_cooldown_hours', 6) or 6), 0.0),
            'strategy_recovery_window_trades': max(int(raw.get('strategy_recovery_window_trades', 2) or 2), 0),
            'strategy_recovery_min_win_rate': min(max(float(raw.get('strategy_recovery_min_win_rate', 50.0) or 50.0), 0.0), 100.0),
            'strategy_recovery_min_avg_return_pct': float(raw.get('strategy_recovery_min_avg_return_pct', 0.0) or 0.0),
            'strategy_cooldown_scopes': list(raw.get('strategy_cooldown_scopes') or ['symbol_regime', 'symbol', 'regime', 'global']),
        }

    def _evaluate_strategy_cooldown(self, *, strategy: str, symbol: str, current_regime: str, current_policy: str,
                                    preferred_rows: list, strategy_rows: list, cfg: dict) -> dict:
        contract = {
            'strategy': strategy,
            'symbol': symbol,
            'regime_tag': current_regime,
            'policy_tag': current_policy,
            'cooldown_active': False,
            'recovery_window_active': False,
            'reason_code': None,
            'reason_codes': [],
            'cooldown_until': None,
            'remaining_minutes': 0,
            'scope': None,
            'scope_key': None,
            'cooldown_trade_id': None,
            'trigger_trade_time': None,
            'trigger_return_pct': None,
            'trigger_close_reason_category': None,
            'recovery_window_trades': [],
            'recovery_trade_count': 0,
            'recovery_win_rate': 0.0,
            'recovery_avg_return_pct': 0.0,
            'recovery_thresholds': {
                'min_trades': int(cfg.get('strategy_recovery_window_trades', 0) or 0),
                'min_win_rate': float(cfg.get('strategy_recovery_min_win_rate', 0.0) or 0.0),
                'min_avg_return_pct': float(cfg.get('strategy_recovery_min_avg_return_pct', 0.0) or 0.0),
            },
            'summary': 'strategy_cooldown_disabled',
        }
        if not cfg.get('strategy_cooldown_enabled', True):
            return contract

        scope_order = list(cfg.get('strategy_cooldown_scopes') or ['symbol_regime', 'symbol', 'regime', 'global'])
        scoped_rows = {
            'global': list(strategy_rows or []),
            'symbol': [row for row in (strategy_rows or []) if str(row.get('symbol') or '') == symbol],
            'regime': [row for row in (strategy_rows or []) if str(row.get('regime_tag') or 'unknown') == current_regime],
            'symbol_regime': [row for row in (strategy_rows or []) if str(row.get('symbol') or '') == symbol and str(row.get('regime_tag') or 'unknown') == current_regime],
        }
        active_trigger = None
        for scope in scope_order:
            rows = list(scoped_rows.get(scope) or [])
            if not rows:
                continue
            feedback = build_close_outcome_feedback_loop(
                close_outcome_digest={
                    'trade_count': len(rows),
                    'win_rate': round((sum(1 for item in rows if float(item.get('return_pct') or 0.0) > 0) / len(rows)) * 100, 4) if rows else 0.0,
                    'net_pnl': round(sum(float(item.get('pnl') or 0.0) for item in rows), 8),
                    'avg_return_pct': round(sum(float(item.get('return_pct') or 0.0) for item in rows) / len(rows), 8) if rows else 0.0,
                    'loss_count': sum(1 for item in rows if float(item.get('return_pct') or 0.0) < 0),
                    'by_close_reason_category': dict(Counter(str(item.get('close_reason_category') or 'unknown') for item in rows)),
                    'by_outcome_quality': dict(Counter(str(item.get('outcome_quality') or 'unknown') for item in rows)),
                    'dominant_policy_tag': current_policy,
                    'dominant_regime_tag': current_regime,
                    'dominant_close_reason_category': max(Counter(str(item.get('close_reason_category') or 'unknown') for item in rows).items(), key=lambda item: item[1])[0] if rows else 'unknown',
                },
                label=f'strategy_cooldown:{strategy}:{scope}',
                min_sample_size=max(1, int(cfg.get('min_trades_for_preference', 2) or 2)),
            )
            latest = rows[0]
            latest_return_pct = float(latest.get('return_pct') or 0.0)
            latest_close_reason = str(latest.get('close_reason_category') or 'unknown')
            latest_failed = latest_return_pct < 0 or latest_close_reason == 'stop_loss'
            mode = str(feedback.get('governance_mode') or ('tighten' if latest_failed else 'observe')).strip().lower()
            if mode not in {'rollback', 'tighten', 'review'} and not latest_failed:
                continue
            close_time_raw = latest.get('close_time') or latest.get('closed_at') or latest.get('updated_at')
            close_time = None
            try:
                close_time = datetime.fromisoformat(str(close_time_raw).replace('Z', '+00:00')) if close_time_raw else None
            except Exception:
                close_time = None
            cooldown_until = close_time + timedelta(hours=float(cfg.get('strategy_cooldown_hours', 6) or 6)) if close_time else None
            now = datetime.now(close_time.tzinfo) if close_time and close_time.tzinfo else datetime.now()
            cooldown_active = bool(cooldown_until and cooldown_until > now)
            recovery_rows = list(preferred_rows or [])[:max(int(cfg.get('strategy_recovery_window_trades', 0) or 0), 0)]
            recovery_trade_count = len(recovery_rows)
            recovery_win_rate = round((sum(1 for item in recovery_rows if float(item.get('return_pct') or 0.0) > 0) / recovery_trade_count) * 100, 4) if recovery_trade_count else 0.0
            recovery_avg_return_pct = round(sum(float(item.get('return_pct') or 0.0) for item in recovery_rows) / recovery_trade_count, 6) if recovery_trade_count else 0.0
            recovery_window_active = recovery_trade_count < contract['recovery_thresholds']['min_trades'] or recovery_win_rate < contract['recovery_thresholds']['min_win_rate'] or recovery_avg_return_pct < contract['recovery_thresholds']['min_avg_return_pct']
            if mode == 'rollback' or cooldown_active:
                reason_code = 'SKIP_STRATEGY_COOLDOWN_ACTIVE'
            else:
                reason_code = 'SKIP_STRATEGY_RECOVERY_WINDOW_ACTIVE'
            active_trigger = {
                **contract,
                'cooldown_active': cooldown_active,
                'recovery_window_active': recovery_window_active,
                'reason_code': reason_code if (cooldown_active or recovery_window_active) else None,
                'reason_codes': [code for code in list(feedback.get('reason_codes') or []) + ([reason_code] if reason_code else []) if code],
                'cooldown_until': cooldown_until.isoformat() if cooldown_until else None,
                'remaining_minutes': max(0, int((cooldown_until - now).total_seconds() // 60)) if cooldown_until and cooldown_until > now else 0,
                'scope': scope,
                'scope_key': f'{strategy}:{scope}:{symbol if scope in {"symbol", "symbol_regime"} else "*"}:{current_regime if scope in {"regime", "symbol_regime"} else "*"}',
                'cooldown_trade_id': latest.get('id'),
                'trigger_trade_time': close_time.isoformat() if close_time else close_time_raw,
                'trigger_return_pct': float(latest.get('return_pct') or 0.0),
                'trigger_close_reason_category': latest.get('close_reason_category'),
                'recovery_window_trades': [
                    {
                        'trade_id': item.get('id'),
                        'close_time': item.get('close_time') or item.get('closed_at'),
                        'return_pct': float(item.get('return_pct') or 0.0),
                        'close_reason_category': item.get('close_reason_category'),
                    }
                    for item in recovery_rows
                ],
                'recovery_trade_count': recovery_trade_count,
                'recovery_win_rate': recovery_win_rate,
                'recovery_avg_return_pct': recovery_avg_return_pct,
                'feedback_status': feedback.get('status'),
                'feedback_mode': mode,
                'feedback_reason_codes': list(feedback.get('reason_codes') or []),
                'summary': f'{strategy}:{mode}:scope={scope}:cooldown={"active" if cooldown_active else "idle"}:recovery={"active" if recovery_window_active else "clear"}',
            }
            if cooldown_active or recovery_window_active:
                return active_trigger
        return contract

    def _build_strategy_selection_contract(self, symbol: str, signal) -> dict:
        cfg = self._strategy_selection_config(symbol)
        regime_snapshot = getattr(signal, 'regime_snapshot', {}) or {}
        policy_snapshot = getattr(signal, 'adaptive_policy_snapshot', {}) or {}
        current_regime = str(regime_snapshot.get('name') or regime_snapshot.get('regime') or 'unknown')
        current_policy = str(policy_snapshot.get('policy_version') or policy_snapshot.get('version') or 'unknown')
        reasons = list(getattr(signal, 'reasons', []) or [])
        baseline_strategies = [str(item.get('strategy') or '').strip() for item in reasons if str(item.get('strategy') or '').strip()]
        unique_strategies = []
        for name in baseline_strategies:
            if name not in unique_strategies:
                unique_strategies.append(name)
        contract = {
            'schema_version': 'adaptive_strategy_selection_v3',
            'enabled': bool(cfg.get('enabled', True)),
            'symbol': symbol,
            'regime_tag': current_regime,
            'policy_tag': current_policy,
            'lookback_limit': cfg['lookback_limit'],
            'baseline_strategies': list(unique_strategies),
            'selected_strategies': list(unique_strategies),
            'strategy_weights': {name: 1.0 for name in unique_strategies},
            'strategy_budgets': {name: cfg['base_budget_ratio'] for name in unique_strategies},
            'strategy_slots': {name: index + 1 for index, name in enumerate(unique_strategies)},
            'strategy_stats': {},
            'strategy_cooldowns': {},
            'selection_reason_codes': ['STRATEGY_SELECTION_BASELINE'],
            'cooldown_summary': {
                'enabled': bool(cfg.get('strategy_cooldown_enabled', True)),
                'active_count': 0,
                'recovery_window_count': 0,
                'blocked_strategies': [],
                'reason_code_counts': {},
            },
            'budget_summary': {
                'slot_cap': len(unique_strategies),
                'selected_slots': len(unique_strategies),
                'selected_budget_ratio': round(len(unique_strategies) * cfg['base_budget_ratio'], 4),
                'available_budget_ratio': round(len(unique_strategies) * cfg['base_budget_ratio'], 4),
                'top_strategy': unique_strategies[0] if unique_strategies else None,
            },
            'ranking': [],
            'decision_summary': 'strategy_selection_disabled_or_no_candidates',
        }
        if not contract['enabled'] or not unique_strategies:
            return contract

        recent_trades = self.db.get_recent_close_outcome_trades(symbol=symbol, limit=cfg['lookback_limit']) if self.db else []
        stats = {}
        strategy_cooldowns = {}
        for strategy in unique_strategies:
            strategy_rows = []
            matched_rows = []
            for row in recent_trades:
                tags = [str(tag).strip() for tag in (row.get('strategy_tags') or []) if str(tag).strip()]
                if strategy not in tags:
                    continue
                strategy_rows.append(row)
                row_regime = str(row.get('regime_tag') or 'unknown')
                row_policy = str(row.get('policy_tag') or 'unknown')
                if row_regime == current_regime or row_policy == current_policy:
                    matched_rows.append(row)
            preferred_rows = matched_rows or strategy_rows
            trade_count = len(preferred_rows)
            avg_return_pct = round(sum(float(item.get('return_pct') or 0.0) for item in preferred_rows) / trade_count, 6) if trade_count else 0.0
            wins = sum(1 for item in preferred_rows if float(item.get('return_pct') or 0.0) > 0)
            losses = sum(1 for item in preferred_rows if float(item.get('return_pct') or 0.0) < 0)
            loss_streak = 0
            if preferred_rows:
                ordered = sorted(preferred_rows, key=lambda item: str(item.get('close_time') or ''), reverse=True)
                for item in ordered:
                    if float(item.get('return_pct') or 0.0) < 0:
                        loss_streak += 1
                    else:
                        break
            matched_mode = 'matched' if matched_rows else 'fallback'
            dominant_feedback_mode = 'observe'
            freeze_match = False
            for item in preferred_rows:
                mode = str((((item.get('close_outcome_feedback') or {}).get('governance_mode')) or item.get('close_outcome_mode') or 'observe')).strip().lower()
                if mode in {'rollback', 'tighten', 'review'}:
                    dominant_feedback_mode = mode
                    if mode == 'rollback':
                        freeze_match = True
                        break
            weight = 1.0
            reasons_applied = []
            if trade_count and trade_count < cfg['min_trades_for_preference']:
                weight *= cfg['deweight_low_sample_multiplier']
                reasons_applied.append('low_sample')
            if matched_mode == 'fallback' and (strategy_rows or preferred_rows):
                weight *= cfg['deweight_mismatch_multiplier']
                reasons_applied.append('regime_policy_fallback')
            if avg_return_pct < 0:
                weight *= cfg['deweight_negative_return_multiplier']
                reasons_applied.append('negative_avg_return')
            if loss_streak >= 2:
                weight *= cfg['deweight_loss_streak_multiplier']
                reasons_applied.append('recent_loss_streak')
            if cfg['disable_on_rollback_match'] and freeze_match:
                weight = 0.0
                reasons_applied.append('matched_rollback_feedback')
            weight = max(0.0, min(weight, 1.0))
            cooldown_contract = self._evaluate_strategy_cooldown(
                strategy=strategy,
                symbol=symbol,
                current_regime=current_regime,
                current_policy=current_policy,
                preferred_rows=preferred_rows,
                strategy_rows=strategy_rows,
                cfg=cfg,
            )
            strategy_cooldowns[strategy] = cooldown_contract
            if cooldown_contract.get('cooldown_active') or cooldown_contract.get('recovery_window_active'):
                weight = 0.0
                reasons_applied.append('strategy_cooldown_guard')
            stats[strategy] = {
                'trade_count': trade_count,
                'matched_trade_count': len(matched_rows),
                'fallback_trade_count': len(strategy_rows),
                'avg_return_pct': avg_return_pct,
                'win_count': wins,
                'loss_count': losses,
                'recent_loss_streak': loss_streak,
                'matched_mode': matched_mode,
                'dominant_feedback_mode': dominant_feedback_mode,
                'freeze_match': freeze_match,
                'weight': round(weight, 4),
                'reasons': reasons_applied,
                'cooldown_contract': cooldown_contract,
            }

        ranked = sorted(stats.items(), key=lambda item: (-float(item[1].get('weight', 0.0)), -int(item[1].get('trade_count', 0)), -float(item[1].get('avg_return_pct', 0.0)), item[0]))
        slot_cap_map = dict(cfg.get('regime_slot_caps') or {})
        raw_slot_cap = slot_cap_map.get(current_regime, slot_cap_map.get('default', cfg['max_selected_strategies']))
        try:
            slot_cap = max(cfg['min_selected_strategies'], min(int(raw_slot_cap or cfg['max_selected_strategies']), cfg['max_selected_strategies'], max(len(unique_strategies), 1)))
        except Exception:
            slot_cap = max(cfg['min_selected_strategies'], min(cfg['max_selected_strategies'], max(len(unique_strategies), 1)))
        selected = [name for name, detail in ranked if float(detail.get('weight', 0.0)) > 0][:slot_cap]
        if len(selected) < cfg['min_selected_strategies']:
            for name, _detail in ranked:
                if name not in selected:
                    selected.append(name)
                if len(selected) >= min(cfg['min_selected_strategies'], len(unique_strategies)):
                    break
        selected = selected[:max(cfg['min_selected_strategies'], min(slot_cap, len(unique_strategies)))]
        budget_override_map = dict(cfg.get('regime_budget_overrides') or {})
        regime_budget_scalar = budget_override_map.get(current_regime, budget_override_map.get('default', cfg['base_budget_ratio']))
        try:
            regime_budget_scalar = float(regime_budget_scalar)
        except Exception:
            regime_budget_scalar = cfg['base_budget_ratio']
        regime_budget_scalar = min(max(regime_budget_scalar, cfg['min_budget_ratio']), cfg['max_budget_ratio'])
        weights = {}
        budgets = {}
        slots = {}
        reason_codes = []
        ranking_rows = []
        for idx, (name, detail) in enumerate(ranked, start=1):
            slot_bonus = max(0.0, (slot_cap - idx) * cfg['slot_bonus_decay']) if idx <= slot_cap else 0.0
            slot_penalty = max(0.0, (idx - slot_cap) * cfg['slot_penalty_decay']) if idx > slot_cap else 0.0
            budget_ratio = min(cfg['max_budget_ratio'], max(cfg['min_budget_ratio'], regime_budget_scalar * max(float(detail.get('weight', 1.0)), 0.0) + slot_bonus - slot_penalty))
            if name not in selected:
                budget_ratio = min(budget_ratio, max(0.0, cfg['min_budget_ratio'] * 0.5))
            budgets[name] = round(budget_ratio, 4)
            weights[name] = round(min(float(detail.get('weight', 1.0)), budget_ratio), 4)
            slots[name] = idx if name in selected else 0
            detail.update({
                'budget_ratio': budgets[name],
                'slot_priority': slots[name],
                'slot_in_scope': idx <= slot_cap,
            })
            ranking_rows.append({'strategy': name, **detail})
        cooldown_reason_counts = Counter()
        blocked_strategies = []
        active_count = 0
        recovery_window_count = 0
        for name, cooldown in strategy_cooldowns.items():
            if cooldown.get('cooldown_active'):
                active_count += 1
            if cooldown.get('recovery_window_active'):
                recovery_window_count += 1
            if cooldown.get('reason_code'):
                cooldown_reason_counts[str(cooldown.get('reason_code'))] += 1
                blocked_strategies.append(name)
        if slot_cap < len(unique_strategies):
            reason_codes.append('REGIME_SLOT_CAP_APPLIED')
        if any(float(v) < 1.0 for v in budgets.values()):
            reason_codes.append('STRATEGY_BUDGET_DEWEIGHT_APPLIED')
        if any(not ((stats.get(name) or {}).get('matched_mode') == 'matched') for name in selected):
            reason_codes.append('STRATEGY_SELECTION_FALLBACK_USED')
        if active_count > 0:
            reason_codes.append('STRATEGY_COOLDOWN_ACTIVE')
        if recovery_window_count > 0:
            reason_codes.append('STRATEGY_RECOVERY_WINDOW_ACTIVE')
        contract.update({
            'selected_strategies': selected,
            'strategy_weights': weights,
            'strategy_budgets': budgets,
            'strategy_slots': slots,
            'strategy_stats': stats,
            'strategy_cooldowns': strategy_cooldowns,
            'selection_reason_codes': reason_codes or ['STRATEGY_SELECTION_BASELINE'],
            'cooldown_summary': {
                'enabled': bool(cfg.get('strategy_cooldown_enabled', True)),
                'active_count': active_count,
                'recovery_window_count': recovery_window_count,
                'blocked_strategies': blocked_strategies,
                'reason_code_counts': dict(cooldown_reason_counts),
            },
            'budget_summary': {
                'slot_cap': slot_cap,
                'selected_slots': len(selected),
                'selected_budget_ratio': round(sum(budgets.get(name, 0.0) for name in selected), 4),
                'available_budget_ratio': round(sum(budgets.values()), 4),
                'top_strategy': selected[0] if selected else None,
            },
            'ranking': ranking_rows,
            'decision_summary': f"selected={','.join(selected) or 'none'} / regime={current_regime} / policy={current_policy} / slot_cap={slot_cap} / budget={round(sum(budgets.get(name, 0.0) for name in selected), 2)} / cooldowns={active_count}+{recovery_window_count} / reasons={','.join(reason_codes or ['baseline'])}",
        })
        return contract

    def _max_open_candidates_per_cycle(self) -> int:
        raw = self.config.get('runtime.open_position.max_candidates_per_cycle', 1)
        try:
            return max(int(raw or 1), 1)
        except Exception:
            return 1

    def _open_position_diversification_config(self) -> dict:
        raw = self.config.get('runtime.open_position.diversification_fence', {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            'enabled': bool(raw.get('enabled', True)),
            'history_limit': max(int(raw.get('history_limit', 12) or 12), 1),
            'same_side_soft_limit': max(int(raw.get('same_side_soft_limit', 2) or 2), 1),
            'same_regime_soft_limit': max(int(raw.get('same_regime_soft_limit', 2) or 2), 1),
            'same_cluster_soft_limit': max(int(raw.get('same_cluster_soft_limit', 1) or 1), 1),
            'side_penalty': float(raw.get('side_penalty', 35) or 35),
            'regime_penalty': float(raw.get('regime_penalty', 25) or 25),
            'cluster_penalty': float(raw.get('cluster_penalty', 55) or 55),
            'cooldown_penalty': float(raw.get('cooldown_penalty', 20) or 20),
            'cooldown_cycles': max(int(raw.get('cooldown_cycles', 3) or 3), 0),
            'symbol_cluster_overrides': dict(raw.get('symbol_cluster_overrides') or {}),
        }

    def _open_position_execution_quota_config(self) -> dict:
        raw = self.config.get('runtime.open_position.execution_quota', {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        max_per_cycle = self._max_open_candidates_per_cycle()
        return {
            'enabled': bool(raw.get('enabled', True)),
            'max_new_positions_per_cycle': max(int(raw.get('max_new_positions_per_cycle', max_per_cycle) or max_per_cycle), 1),
            'max_same_cluster_per_cycle': max(int(raw.get('max_same_cluster_per_cycle', 1) or 1), 1),
            'max_same_side_per_cycle': max(int(raw.get('max_same_side_per_cycle', max_per_cycle) or max_per_cycle), 1),
            'max_same_regime_per_cycle': max(int(raw.get('max_same_regime_per_cycle', max_per_cycle) or max_per_cycle), 1),
        }

    def _symbol_cluster_key(self, symbol: str) -> str:
        overrides = (self._open_position_diversification_config().get('symbol_cluster_overrides') or {})
        if symbol in overrides:
            return str(overrides[symbol])
        base_asset = str(symbol or '').split('/')[0].upper()
        default_clusters = {
            'BTC': 'store_of_value',
            'ETH': 'core_layer1',
            'SOL': 'high_beta_layer1',
            'XRP': 'payments_beta',
            'BNB': 'exchange_beta',
            'DOGE': 'meme_beta',
            'PEPE': 'meme_beta',
        }
        return default_clusters.get(base_asset, f'alt:{base_asset or "unknown"}')

    def _candidate_diversification_context(self, symbol: str, signal, risk_details: dict = None) -> dict:
        regime_snapshot = getattr(signal, 'regime_snapshot', {}) or getattr(signal, 'regime_info', {}) or {}
        return {
            'symbol': symbol,
            'side': 'long' if getattr(signal, 'signal_type', None) == 'buy' else 'short' if getattr(signal, 'signal_type', None) == 'sell' else 'flat',
            'regime_tag': regime_snapshot.get('name') or regime_snapshot.get('regime') or 'unknown',
            'regime_family': regime_snapshot.get('family') or 'unknown',
            'regime_direction': regime_snapshot.get('direction') or 'unknown',
            'symbol_cluster': self._symbol_cluster_key(symbol),
            'scope_mode': str(((risk_details or {}).get('close_outcome_guard') or {}).get('mode') or 'observe').strip().lower() or 'observe',
        }

    def _notification_context(self, signal=None, details: dict = None) -> dict:
        payload = dict(details or {})
        observability = payload.get('observability') if isinstance(payload.get('observability'), dict) else {}
        if observability:
            if not isinstance(payload.get('regime_snapshot'), dict) and isinstance(observability.get('regime_snapshot'), dict):
                payload['regime_snapshot'] = dict(observability.get('regime_snapshot') or {})
            if not isinstance(payload.get('adaptive_policy_snapshot'), dict) and isinstance(observability.get('adaptive_policy_snapshot'), dict):
                payload['adaptive_policy_snapshot'] = dict(observability.get('adaptive_policy_snapshot') or {})
            if not isinstance(payload.get('observe_only'), dict) and isinstance(observability.get('observe_only'), dict):
                payload['observe_only'] = dict(observability.get('observe_only') or {})
            if not isinstance(payload.get('adaptive_regime_observe_only'), dict):
                obs_view = observability.get('observe_only') if isinstance(observability.get('observe_only'), dict) else {}
                if obs_view:
                    payload['adaptive_regime_observe_only'] = {
                        'phase': obs_view.get('phase'),
                        'state': obs_view.get('state'),
                        'summary': obs_view.get('summary'),
                        'tags': list(obs_view.get('tags') or []),
                        'notes': list(obs_view.get('notes') or []),
                    }
        if signal is None:
            return payload

        market_context = getattr(signal, 'market_context', None)
        if isinstance(market_context, dict) and market_context and not isinstance(payload.get('market_context'), dict):
            payload['market_context'] = dict(market_context)

        regime_info = getattr(signal, 'regime_info', None)
        if isinstance(regime_info, dict) and regime_info and not isinstance(payload.get('regime_info'), dict):
            payload['regime_info'] = dict(regime_info)

        regime_snapshot = payload.get('regime_snapshot') if isinstance(payload.get('regime_snapshot'), dict) else {}
        if not regime_snapshot:
            signal_regime_snapshot = getattr(signal, 'regime_snapshot', None)
            if isinstance(signal_regime_snapshot, dict) and signal_regime_snapshot:
                payload['regime_snapshot'] = dict(signal_regime_snapshot)
            elif isinstance(regime_info, dict) and regime_info:
                payload['regime_snapshot'] = dict(regime_info)
            elif isinstance(market_context, dict) and isinstance(market_context.get('regime_snapshot'), dict) and market_context.get('regime_snapshot'):
                payload['regime_snapshot'] = dict(market_context.get('regime_snapshot') or {})

        policy_snapshot = payload.get('adaptive_policy_snapshot') if isinstance(payload.get('adaptive_policy_snapshot'), dict) else {}
        if not policy_snapshot:
            signal_policy_snapshot = getattr(signal, 'adaptive_policy_snapshot', None)
            if isinstance(signal_policy_snapshot, dict) and signal_policy_snapshot:
                payload['adaptive_policy_snapshot'] = dict(signal_policy_snapshot)
            elif isinstance(market_context, dict) and isinstance(market_context.get('adaptive_policy_snapshot'), dict) and market_context.get('adaptive_policy_snapshot'):
                payload['adaptive_policy_snapshot'] = dict(market_context.get('adaptive_policy_snapshot') or {})

        if not isinstance(payload.get('adaptive_regime_observe_only'), dict):
            observe_only = payload.get('observe_only') if isinstance(payload.get('observe_only'), dict) else {}
            if observe_only:
                payload['adaptive_regime_observe_only'] = {
                    'phase': observe_only.get('phase'),
                    'state': observe_only.get('state'),
                    'summary': observe_only.get('summary'),
                    'tags': list(observe_only.get('tags') or []),
                    'notes': list(observe_only.get('notes') or []),
                }

        return payload

    def _candidate_diversification_history(self) -> list:
        state = load_runtime_state()
        history = state.get('candidate_selection_history') or []
        return history if isinstance(history, list) else []

    def _persist_candidate_diversification_history(self, selected_candidates: list):
        cfg = self._open_position_diversification_config()
        state = load_runtime_state()
        history = self._candidate_diversification_history()
        history.insert(0, {
            'time': datetime.now().isoformat(),
            'selected': [dict((row.get('ranking_contract') or {}).get('diversification_context') or {}) for row in (selected_candidates or [])],
        })
        state['candidate_selection_history'] = history[:cfg['history_limit']]
        save_runtime_state(state)

    def _apply_diversification_fence(self, ranked_candidates: list) -> list:
        cfg = self._open_position_diversification_config()
        if not cfg.get('enabled', True):
            return ranked_candidates
        history = self._candidate_diversification_history()
        recent_selected = []
        for item in history[:cfg['history_limit']]:
            recent_selected.extend(item.get('selected') or [])
        recent_side = Counter(str(item.get('side') or 'unknown') for item in recent_selected)
        recent_regime = Counter(str(item.get('regime_tag') or 'unknown') for item in recent_selected)
        recent_cluster = Counter(str(item.get('symbol_cluster') or 'unknown') for item in recent_selected)
        selected_contexts = []
        adjusted = []
        for row in ranked_candidates or []:
            ranking_contract = row.setdefault('ranking_contract', {})
            skip_contract = row.setdefault('skip_contract', {})
            context = dict(ranking_contract.get('diversification_context') or {})
            if skip_contract.get('status') == 'skipped' or not row.get('can_open'):
                ranking_contract['diversification'] = {'status': 'ineligible', 'reason_codes': ['INELIGIBLE_BEFORE_DIVERSIFICATION'], 'applied_penalty': 0.0}
                adjusted.append(row)
                continue
            penalty = 0.0
            reason_codes = []
            same_side_count = sum(1 for item in selected_contexts if item.get('side') == context.get('side')) + recent_side.get(str(context.get('side') or 'unknown'), 0)
            same_regime_count = sum(1 for item in selected_contexts if item.get('regime_tag') == context.get('regime_tag')) + recent_regime.get(str(context.get('regime_tag') or 'unknown'), 0)
            same_cluster_count = sum(1 for item in selected_contexts if item.get('symbol_cluster') == context.get('symbol_cluster')) + recent_cluster.get(str(context.get('symbol_cluster') or 'unknown'), 0)
            if same_side_count >= cfg['same_side_soft_limit']:
                penalty += cfg['side_penalty'] * max(1, same_side_count - cfg['same_side_soft_limit'] + 1)
                reason_codes.append('SIDE_CONCENTRATION_PENALTY')
            if same_regime_count >= cfg['same_regime_soft_limit']:
                penalty += cfg['regime_penalty'] * max(1, same_regime_count - cfg['same_regime_soft_limit'] + 1)
                reason_codes.append('REGIME_CONCENTRATION_PENALTY')
            if same_cluster_count >= cfg['same_cluster_soft_limit']:
                penalty += cfg['cluster_penalty'] * max(1, same_cluster_count - cfg['same_cluster_soft_limit'] + 1)
                reason_codes.append('CLUSTER_CONCENTRATION_PENALTY')
            recent_symbols = [str(item.get('symbol') or '') for item in recent_selected[:cfg.get('cooldown_cycles', 0)]]
            if cfg.get('cooldown_cycles', 0) > 0 and str(context.get('symbol') or '') in recent_symbols:
                penalty += cfg['cooldown_penalty']
                reason_codes.append('SYMBOL_COOLDOWN_PENALTY')
            if penalty > 0:
                ranking_contract['priority_score'] = round(float(ranking_contract.get('priority_score') or 0) - penalty, 4)
                ranking_contract['ranking_penalty'] = round(float(ranking_contract.get('ranking_penalty') or 0) + penalty, 4)
            ranking_contract['diversification'] = {
                'status': 'penalized' if penalty > 0 else 'clear',
                'reason_codes': reason_codes,
                'applied_penalty': round(penalty, 4),
                'recent_side_count': same_side_count,
                'recent_regime_count': same_regime_count,
                'recent_cluster_count': same_cluster_count,
            }
            adjusted.append(row)
            selected_contexts.append(context)
        return sorted(
            adjusted,
            key=lambda row: (
                0 if row.get('can_open') else 1,
                0 if (row.get('skip_contract') or {}).get('status') != 'skipped' else 1,
                -float((row.get('ranking_contract') or {}).get('priority_score') or 0),
                str(row.get('symbol') or ''),
            )
        )

    def _build_candidate_contract(self, *, symbol: str, current_price: float, signal, signal_id: int,
                                  passed: bool, reason: str, details: dict, entry_decision,
                                  can_open: bool = False, risk_reason: str = None, risk_details: dict = None) -> dict:
        risk_details = dict(risk_details or {})
        guard = dict(risk_details.get('close_outcome_guard') or {})
        scope_window = dict(guard.get('scope_window') or {})
        scope_context = dict(guard.get('scope_context') or {})
        scope_mode = str(guard.get('mode') or 'observe').strip().lower() or 'observe'
        side = 'long' if getattr(signal, 'signal_type', None) == 'buy' else 'short' if getattr(signal, 'signal_type', None) == 'sell' else None
        ml_confidence = None
        if isinstance(getattr(signal, 'indicators', None), dict):
            ml_confidence = signal.indicators.get('ML_Confidence')
        ranking_penalty = 0
        if scope_window:
            ranking_penalty += {'rollback': 1000, 'tighten': 250, 'review': 150}.get(scope_mode, 0)
        if entry_decision and getattr(entry_decision, 'decision', None) != 'allow':
            ranking_penalty += 500
        if not can_open:
            ranking_penalty += 900
        ml_score = 0.0
        if ml_confidence is not None:
            ml_score = float(ml_confidence or 0)
            if ml_score <= 1.0:
                ml_score *= 100.0
        strategy_budget_boost = float(((getattr(signal, 'market_context', {}) or {}).get('strategy_selection') or {}).get('budget_summary', {}).get('selected_budget_ratio', 0.0) or 0.0)
        priority_score = round(
            float(getattr(signal, 'strength', 0) or 0)
            + float(getattr(entry_decision, 'score', 0) or 0) * 1.5
            + ml_score * 0.05
            + strategy_budget_boost * 10.0
            - float(ranking_penalty),
            4,
        )
        diversification_context = self._candidate_diversification_context(symbol, signal, risk_details)
        strategy_selection = dict(((getattr(signal, 'market_context', {}) or {}).get('strategy_selection')) or {})
        strategy_budget_summary = dict(strategy_selection.get('budget_summary') or {})
        strategy_cooldown_summary = dict(strategy_selection.get('cooldown_summary') or {})
        ranking_contract = {
            'symbol': symbol,
            'signal_id': signal_id,
            'side': side,
            'price': current_price,
            'signal_type': getattr(signal, 'signal_type', None),
            'signal_strength': float(getattr(signal, 'strength', 0) or 0),
            'entry_decision': getattr(entry_decision, 'decision', None),
            'entry_score': float(getattr(entry_decision, 'score', 0) or 0),
            'ml_confidence': ml_confidence,
            'selected_strategies': list(strategy_selection.get('selected_strategies') or getattr(signal, 'strategies_triggered', []) or []),
            'strategy_selection_summary': strategy_selection.get('decision_summary'),
            'strategy_selection_reason_codes': list(strategy_selection.get('selection_reason_codes') or []),
            'strategy_budget_summary': strategy_budget_summary,
            'strategy_budget_ratio': float(strategy_budget_summary.get('selected_budget_ratio', 0.0) or 0.0),
            'strategy_slot_cap': int(strategy_budget_summary.get('slot_cap', len(strategy_selection.get('selected_strategies') or [])) or 0),
            'strategy_cooldown_summary': strategy_cooldown_summary,
            'strategy_cooldown_reason_codes': list((strategy_selection.get('cooldown_summary') or {}).get('reason_code_counts', {}).keys()),
            'close_outcome_scope_mode': scope_mode,
            'close_outcome_scope': scope_window.get('scope'),
            'close_outcome_scope_key': scope_window.get('scope_key'),
            'close_outcome_scope_active': bool(scope_window),
            'close_outcome_freeze_auto_promotion': bool(guard.get('freeze_auto_promotion', False)),
            'ranking_penalty': ranking_penalty,
            'priority_score': priority_score,
            'can_open': bool(can_open),
            'scoped_window_penalized': bool(scope_window) and scope_mode in {'rollback', 'tighten', 'review'},
            'diversification_context': diversification_context,
            'diversification': {'status': 'pending', 'reason_codes': [], 'applied_penalty': 0.0},
        }
        execution_contract = {
            'status': 'pending',
            'selected': False,
            'reason': None,
            'reason_code': None,
            'action': None,
            'quota': None,
            'cluster_key': diversification_context.get('symbol_cluster'),
            'side': diversification_context.get('side'),
            'regime_tag': diversification_context.get('regime_tag'),
        }
        skip_contract = {
            'symbol': symbol,
            'signal_id': signal_id,
            'side': side,
            'status': 'pending',
            'reason': None,
            'reason_code': None,
            'scope_mode': scope_mode,
            'scope': scope_window.get('scope'),
            'scope_key': scope_window.get('scope_key'),
            'scope_context': scope_context,
            'risk_reason': risk_reason,
            'filter_reason': reason,
            'action': None,
            'deferred': False,
            'defer_to_cycle': None,
            'execution_contract': execution_contract,
        }
        if not passed:
            skip_contract.update({'status': 'skipped', 'reason': reason, 'reason_code': 'SKIP_SIGNAL_FILTERED', 'action': 'filtered_before_ranking'})
        elif not can_open:
            reason_code = 'DENY_RISK_GATE_BLOCKED'
            action = 'blocked_by_open_gate'
            if scope_window and scope_mode == 'rollback':
                reason_code = 'DENY_GUARD_SCOPED_FREEZE'
                action = 'skip_scoped_freeze'
            skip_contract.update({'status': 'skipped', 'reason': risk_reason, 'reason_code': reason_code, 'action': action})
        elif scope_window and scope_mode == 'tighten':
            skip_contract.update({'status': 'skipped', 'reason': 'scoped_window_tighten_bypass', 'reason_code': 'SKIP_GUARD_SCOPED_TIGHTEN', 'action': 'bypass_tighten_candidate'})
            ranking_contract['can_open'] = False
            ranking_contract['ranking_penalty'] += 250
            ranking_contract['priority_score'] = round(ranking_contract['priority_score'] - 250, 4)
        elif scope_window and scope_mode == 'review':
            skip_contract.update({'status': 'skipped', 'reason': 'scoped_window_review_bypass', 'reason_code': 'SKIP_GUARD_SCOPED_REVIEW', 'action': 'bypass_review_candidate'})
            ranking_contract['can_open'] = False
            ranking_contract['ranking_penalty'] += 150
            ranking_contract['priority_score'] = round(ranking_contract['priority_score'] - 150, 4)
        if skip_contract.get('reason_code'):
            skip_contract.update(build_reason_code_details(skip_contract.get('reason_code')))
        return {
            'symbol': symbol,
            'current_price': current_price,
            'signal_id': signal_id,
            'signal': signal,
            'side': side,
            'passed': bool(passed),
            'filter_reason': reason,
            'filter_details': dict(details or {}),
            'entry_decision': entry_decision.to_dict() if entry_decision else {},
            'can_open': bool(ranking_contract.get('can_open')),
            'risk_reason': risk_reason,
            'risk_details': risk_details,
            'ranking_contract': ranking_contract,
            'execution_contract': execution_contract,
            'skip_contract': skip_contract,
        }

    def _apply_execution_quota_guardrails(self, ranked_candidates: list) -> tuple[list, list]:
        cfg = self._open_position_execution_quota_config()
        if not cfg.get('enabled', True):
            for row in ranked_candidates or []:
                row.setdefault('execution_contract', {}).update({'status': 'disabled', 'selected': False})
            return ranked_candidates, []

        selected = []
        selected_cluster = Counter()
        selected_side = Counter()
        selected_regime = Counter()
        cycle_quota = max(1, int(cfg.get('max_new_positions_per_cycle', self._max_open_candidates_per_cycle()) or self._max_open_candidates_per_cycle()))

        for row in ranked_candidates or []:
            skip_contract = row.setdefault('skip_contract', {})
            ranking_contract = row.setdefault('ranking_contract', {})
            execution_contract = row.setdefault('execution_contract', {})
            context = dict(ranking_contract.get('diversification_context') or {})
            cluster_key = str(context.get('symbol_cluster') or 'unknown')
            side_key = str(context.get('side') or 'unknown')
            regime_key = str(context.get('regime_tag') or 'unknown')
            quota_snapshot = {
                'selected_so_far': len(selected),
                'cycle_quota': cycle_quota,
                'cluster_key': cluster_key,
                'same_cluster_selected': int(selected_cluster.get(cluster_key, 0)),
                'cluster_cap': int(cfg.get('max_same_cluster_per_cycle', 1) or 1),
                'same_side_selected': int(selected_side.get(side_key, 0)),
                'side_cap': int(cfg.get('max_same_side_per_cycle', cycle_quota) or cycle_quota),
                'same_regime_selected': int(selected_regime.get(regime_key, 0)),
                'regime_cap': int(cfg.get('max_same_regime_per_cycle', cycle_quota) or cycle_quota),
            }
            execution_contract.update({'quota': quota_snapshot, 'cluster_key': cluster_key, 'side': side_key, 'regime_tag': regime_key})

            if not row.get('can_open') or skip_contract.get('status') == 'skipped':
                execution_contract.update({'status': 'ineligible', 'selected': False, 'reason_code': 'SKIP_PRE_EXECUTION_INELIGIBLE', 'action': 'skip_before_execution_quota'})
                execution_contract.update(build_reason_code_details(execution_contract.get('reason_code')))
                continue

            if len(selected) >= cycle_quota:
                execution_contract.update({'status': 'deferred', 'selected': False, 'reason': 'execution_quota_exhausted', 'reason_code': 'DEFER_EXECUTION_CYCLE_QUOTA_EXHAUSTED', 'action': 'defer_to_next_cycle'})
                skip_contract.update({'status': 'skipped', 'reason': 'execution_quota_exhausted', 'reason_code': 'DEFER_EXECUTION_CYCLE_QUOTA_EXHAUSTED', 'action': 'defer_to_next_cycle', 'deferred': True, 'defer_to_cycle': 'next'})
                execution_contract.update(build_reason_code_details(execution_contract.get('reason_code')))
                skip_contract.update(build_reason_code_details(skip_contract.get('reason_code')))
                row['can_open'] = False
                continue
            if selected_cluster.get(cluster_key, 0) >= quota_snapshot['cluster_cap']:
                execution_contract.update({'status': 'deferred', 'selected': False, 'reason': 'symbol_cluster_cap_reached', 'reason_code': 'DEFER_EXECUTION_CLUSTER_CAP_REACHED', 'action': 'defer_cluster_capped'})
                skip_contract.update({'status': 'skipped', 'reason': 'symbol_cluster_cap_reached', 'reason_code': 'DEFER_EXECUTION_CLUSTER_CAP_REACHED', 'action': 'defer_cluster_capped', 'deferred': True, 'defer_to_cycle': 'next'})
                execution_contract.update(build_reason_code_details(execution_contract.get('reason_code')))
                skip_contract.update(build_reason_code_details(skip_contract.get('reason_code')))
                row['can_open'] = False
                continue
            if selected_side.get(side_key, 0) >= quota_snapshot['side_cap']:
                execution_contract.update({'status': 'deferred', 'selected': False, 'reason': 'side_execution_cap_reached', 'reason_code': 'DEFER_EXECUTION_SIDE_CAP_REACHED', 'action': 'defer_side_capped'})
                skip_contract.update({'status': 'skipped', 'reason': 'side_execution_cap_reached', 'reason_code': 'DEFER_EXECUTION_SIDE_CAP_REACHED', 'action': 'defer_side_capped', 'deferred': True, 'defer_to_cycle': 'next'})
                execution_contract.update(build_reason_code_details(execution_contract.get('reason_code')))
                skip_contract.update(build_reason_code_details(skip_contract.get('reason_code')))
                row['can_open'] = False
                continue
            if selected_regime.get(regime_key, 0) >= quota_snapshot['regime_cap']:
                execution_contract.update({'status': 'deferred', 'selected': False, 'reason': 'regime_execution_cap_reached', 'reason_code': 'DEFER_EXECUTION_REGIME_CAP_REACHED', 'action': 'defer_regime_capped'})
                skip_contract.update({'status': 'skipped', 'reason': 'regime_execution_cap_reached', 'reason_code': 'DEFER_EXECUTION_REGIME_CAP_REACHED', 'action': 'defer_regime_capped', 'deferred': True, 'defer_to_cycle': 'next'})
                execution_contract.update(build_reason_code_details(execution_contract.get('reason_code')))
                skip_contract.update(build_reason_code_details(skip_contract.get('reason_code')))
                row['can_open'] = False
                continue

            selected.append(row)
            selected_cluster[cluster_key] += 1
            selected_side[side_key] += 1
            selected_regime[regime_key] += 1
            execution_contract.update({'status': 'selected', 'selected': True, 'reason': 'execution_quota_passed', 'reason_code': 'PERMIT_EXECUTION_QUOTA_PASSED', 'action': 'execute_this_cycle'})
            execution_contract.update(build_reason_code_details(execution_contract.get('reason_code')))

        return ranked_candidates, selected

    def _rank_open_candidates(self, candidates: list) -> tuple[list, list]:
        ranked = sorted(
            list(candidates or []),
            key=lambda row: (
                0 if row.get('can_open') else 1,
                0 if (row.get('skip_contract') or {}).get('status') != 'skipped' else 1,
                -float((row.get('ranking_contract') or {}).get('priority_score') or 0),
                str(row.get('symbol') or ''),
            )
        )
        ranked = self._apply_diversification_fence(ranked)
        ranked, selected = self._apply_execution_quota_guardrails(ranked)
        for index, row in enumerate(ranked, start=1):
            row.setdefault('ranking_contract', {})['rank'] = index
            row.setdefault('ranking_contract', {})['selected'] = row in selected
            row.setdefault('execution_contract', {})['selected'] = row in selected
        return ranked, selected

    def _build_candidate_runtime_summary(self, ranked_candidates: list, selected_candidates: list) -> dict:
        items = []
        skip_items = []
        reason_counts = Counter()
        execution_cfg = self._open_position_execution_quota_config()
        for row in ranked_candidates or []:
            ranking_contract = dict(row.get('ranking_contract') or {})
            execution_contract = dict(row.get('execution_contract') or {})
            skip_contract = dict(row.get('skip_contract') or {})
            items.append({
                'symbol': row.get('symbol'),
                'signal_id': row.get('signal_id'),
                'rank': ranking_contract.get('rank'),
                'selected': bool(ranking_contract.get('selected', False)),
                'can_open': bool(row.get('can_open', False)),
                'priority_score': ranking_contract.get('priority_score'),
                'signal_strength': ranking_contract.get('signal_strength'),
                'entry_score': ranking_contract.get('entry_score'),
                'close_outcome_scope_mode': ranking_contract.get('close_outcome_scope_mode'),
                'close_outcome_scope': ranking_contract.get('close_outcome_scope'),
                'close_outcome_scope_key': ranking_contract.get('close_outcome_scope_key'),
                'diversification': ranking_contract.get('diversification') or {},
                'diversification_context': ranking_contract.get('diversification_context') or {},
                'execution_contract': execution_contract,
                'skip_contract': skip_contract,
            })
            if skip_contract.get('status') == 'skipped':
                skip_items.append(skip_contract)
                if skip_contract.get('reason_code'):
                    reason_counts[str(skip_contract.get('reason_code'))] += 1
        return {
            'schema_version': 'open_candidate_ranking_v3',
            'selected_count': len(selected_candidates or []),
            'candidate_count': len(ranked_candidates or []),
            'max_open_candidates_per_cycle': self._max_open_candidates_per_cycle(),
            'execution_quota': {
                'enabled': bool(execution_cfg.get('enabled', True)),
                'max_new_positions_per_cycle': int(execution_cfg.get('max_new_positions_per_cycle', self._max_open_candidates_per_cycle()) or self._max_open_candidates_per_cycle()),
                'max_same_cluster_per_cycle': int(execution_cfg.get('max_same_cluster_per_cycle', 1) or 1),
                'max_same_side_per_cycle': int(execution_cfg.get('max_same_side_per_cycle', self._max_open_candidates_per_cycle()) or self._max_open_candidates_per_cycle()),
                'max_same_regime_per_cycle': int(execution_cfg.get('max_same_regime_per_cycle', self._max_open_candidates_per_cycle()) or self._max_open_candidates_per_cycle()),
                'selected_cluster_counts': dict(Counter(str(((row.get('execution_contract') or {}).get('cluster_key') or 'unknown')) for row in (selected_candidates or []))),
                'selected_side_counts': dict(Counter(str(((row.get('execution_contract') or {}).get('side') or 'unknown')) for row in (selected_candidates or []))),
                'selected_regime_counts': dict(Counter(str(((row.get('execution_contract') or {}).get('regime_tag') or 'unknown')) for row in (selected_candidates or []))),
                'reason_code_counts': dict(reason_counts),
            },
            'items': items,
            'skip_contracts': skip_items,
        }

    def _build_final_execution_permit_contract(self, row: dict) -> dict:
        def _stage_snapshot(stage: str, payload: dict, *, fallback_status: str = 'pending') -> dict:
            snapshot = dict(payload or {})
            if not snapshot:
                return {}
            snapshot_reason_code = str(snapshot.get('reason_code') or '').strip()
            if snapshot_reason_code:
                snapshot.update(build_reason_code_details(snapshot_reason_code))
            return {
                'stage': stage,
                'status': snapshot.get('status') or fallback_status,
                'selected': bool(snapshot.get('selected', False)) if 'selected' in snapshot else None,
                'allowed': snapshot.get('allowed'),
                'reason_code': snapshot.get('reason_code'),
                'legacy_reason_code': snapshot.get('legacy_reason_code'),
                'reason_code_disposition': snapshot.get('reason_code_disposition'),
                'reason_code_stage': snapshot.get('reason_code_stage'),
                'reason': snapshot.get('reason') or snapshot.get('filter_reason'),
                'action': snapshot.get('action'),
            }

        row = dict(row or {})
        risk_details = dict(row.get('risk_details') or {})
        guard = dict(risk_details.get('close_outcome_guard') or {})
        skip_contract = dict(row.get('skip_contract') or {})
        execution_contract = dict(row.get('execution_contract') or {})
        ranking_contract = dict(row.get('ranking_contract') or {})
        exchange_mode = str(getattr(self.config, 'exchange_mode', None) or self.config.get('exchange.mode', 'paper')).strip().lower()
        selected = bool(execution_contract.get('selected', False))
        allowed = True
        reason_code = 'PERMIT_FINAL_EXECUTION_GRANTED'
        reason = 'selected_candidate_ready_for_testnet_execution'
        action = 'submit_testnet_order'
        final_gate = 'final_execution_permit'
        decision_source = 'final_execution_permit'

        if exchange_mode != 'testnet':
            allowed = False
            reason_code = 'DENY_ENV_TESTNET_ONLY'
            reason = f'final_execution_permit_requires_testnet_mode:{exchange_mode or "unknown"}'
            action = 'deny_non_testnet_execution'
            final_gate = 'environment'
            decision_source = 'environment'
        elif skip_contract.get('status') == 'skipped':
            allowed = False
            reason_code = str(skip_contract.get('reason_code') or 'SKIP_CONTRACT_DENY')
            reason = str(skip_contract.get('reason') or skip_contract.get('filter_reason') or 'candidate_skipped_before_final_execution')
            action = str(skip_contract.get('action') or 'deny_skipped_candidate')
            skip_reason = build_reason_code_details(reason_code, include_legacy=False)
            if skip_reason.get('reason_code_disposition') == 'defer':
                final_gate = 'execution_quota'
                decision_source = 'execution_contract'
            else:
                final_gate = 'candidate_skip'
                decision_source = 'skip_contract'
        elif not selected:
            allowed = False
            reason_code = str(execution_contract.get('reason_code') or 'EXECUTION_CONTRACT_NOT_SELECTED')
            reason = str(execution_contract.get('reason') or 'candidate_not_selected_for_execution_this_cycle')
            action = str(execution_contract.get('action') or 'deny_unselected_candidate')
            final_gate = 'execution_quota'
            decision_source = 'execution_contract'
        elif not row.get('can_open'):
            allowed = False
            reason_code = 'DENY_RISK_GATE_BLOCKED'
            reason = str(row.get('risk_reason') or 'risk_gate_blocked_before_final_execution')
            action = 'deny_risk_gate_blocked_candidate'
            final_gate = 'risk_gate'
            decision_source = 'risk_gate'

        guard_reason_codes = list((guard.get('reason_codes') or []))
        feedback_loop = dict(guard.get('feedback_loop') or {})
        decision_contract = dict(feedback_loop.get('decision_contract') or {})
        contract = {
            'schema_version': 'final_execution_permit_v1',
            'symbol': row.get('symbol'),
            'signal_id': row.get('signal_id'),
            'side': row.get('side'),
            'status': 'permit' if allowed else 'deny',
            'allowed': allowed,
            'reason_code': reason_code,
            'reason': reason,
            'action': action,
            'exchange_mode': exchange_mode,
            'testnet_only': True,
            'selected_for_execution': selected,
            'scope_mode': ranking_contract.get('close_outcome_scope_mode') or guard.get('mode') or 'observe',
            'generated_at': datetime.now().isoformat(),
            'guardrail_evidence': {
                'close_outcome_guard': {
                    'enabled': bool(guard.get('enabled', False)),
                    'passed': bool(guard.get('passed', True)),
                    'mode': guard.get('mode'),
                    'reason': guard.get('reason'),
                    'action': guard.get('action'),
                    'route': guard.get('route'),
                    'freeze_auto_promotion': bool(guard.get('freeze_auto_promotion', False)),
                    'scope_window': dict(guard.get('scope_window') or {}),
                    'scope_context': dict(guard.get('scope_context') or {}),
                    'reason_codes': guard_reason_codes,
                    'trade_count': int(guard.get('trade_count', 0) or 0),
                    'min_sample_size': int(guard.get('min_sample_size', 0) or 0),
                },
                'close_outcome_decision_contract': decision_contract,
                'risk_reason': row.get('risk_reason'),
                'skip_contract': skip_contract,
                'execution_contract': execution_contract,
                'ranking_contract': {
                    'rank': ranking_contract.get('rank'),
                    'priority_score': ranking_contract.get('priority_score'),
                    'close_outcome_scope': ranking_contract.get('close_outcome_scope'),
                    'close_outcome_scope_key': ranking_contract.get('close_outcome_scope_key'),
                },
            },
        }
        contract.update(build_reason_code_details(contract.get('reason_code')))
        reason_codes = merge_reason_codes([contract.get('legacy_reason_code')], guard_reason_codes, primary=contract.get('reason_code'))
        contract['reason_codes'] = reason_codes
        contract['diagnose_replay'] = {
            'schema_version': 'final_execution_permit_replay_v1',
            'symbol': contract.get('symbol'),
            'signal_id': contract.get('signal_id'),
            'side': contract.get('side'),
            'status': contract.get('status'),
            'allowed': contract.get('allowed'),
            'reason_code': contract.get('reason_code'),
            'legacy_reason_code': contract.get('legacy_reason_code'),
            'reason_code_family': contract.get('reason_code_family'),
            'reason_code_stage': contract.get('reason_code_stage'),
            'reason_code_disposition': contract.get('reason_code_disposition'),
            'reason_codes': reason_codes,
            'reason': contract.get('reason'),
            'action': contract.get('action'),
            'exchange_mode': contract.get('exchange_mode'),
            'testnet_only': contract.get('testnet_only'),
            'selected_for_execution': contract.get('selected_for_execution'),
            'scope_mode': contract.get('scope_mode'),
            'guardrail_evidence': dict(contract.get('guardrail_evidence') or {}),
        }
        decision_path = [
            snapshot for snapshot in [
                _stage_snapshot('candidate_skip', skip_contract, fallback_status='ready'),
                _stage_snapshot('execution_quota', execution_contract, fallback_status='pending'),
                {
                    'stage': 'risk_gate',
                    'status': 'passed' if row.get('can_open') else 'blocked',
                    'selected': None,
                    'allowed': bool(row.get('can_open')),
                    'reason_code': 'DENY_RISK_GATE_BLOCKED' if not row.get('can_open') else None,
                    'legacy_reason_code': 'RISK_GATE_BLOCKED' if not row.get('can_open') else None,
                    'reason_code_disposition': 'deny' if not row.get('can_open') else 'permit',
                    'reason_code_stage': 'risk_gate',
                    'reason': row.get('risk_reason'),
                    'action': 'deny_risk_gate_blocked_candidate' if not row.get('can_open') else 'allow_candidate_forward',
                },
                {
                    'stage': 'final_execution_permit',
                    'status': contract.get('status'),
                    'selected': selected,
                    'allowed': contract.get('allowed'),
                    'reason_code': contract.get('reason_code'),
                    'legacy_reason_code': contract.get('legacy_reason_code'),
                    'reason_code_disposition': contract.get('reason_code_disposition'),
                    'reason_code_stage': contract.get('reason_code_stage'),
                    'reason': contract.get('reason'),
                    'action': contract.get('action'),
                },
            ] if snapshot
        ]
        contract['runtime_diagnose_bundle'] = {
            'schema_version': 'final_execution_diagnose_bundle_v1',
            'symbol': contract.get('symbol'),
            'signal_id': contract.get('signal_id'),
            'side': contract.get('side'),
            'generated_at': contract.get('generated_at'),
            'exchange_mode': contract.get('exchange_mode'),
            'testnet_only': contract.get('testnet_only'),
            'selected_for_execution': selected,
            'decision': contract.get('reason_code_disposition'),
            'final_status': contract.get('status'),
            'allowed': contract.get('allowed'),
            'reason_code': contract.get('reason_code'),
            'legacy_reason_code': contract.get('legacy_reason_code'),
            'reason_code_family': contract.get('reason_code_family'),
            'reason_code_stage': contract.get('reason_code_stage'),
            'reason_codes': reason_codes,
            'decision_source': decision_source,
            'final_gate': final_gate,
            'reason': contract.get('reason'),
            'action': contract.get('action'),
            'summary': f"{contract.get('reason_code_disposition')}:{contract.get('reason_code')}@{final_gate}",
            'decision_path': decision_path,
            'guardrail_evidence': dict(contract.get('guardrail_evidence') or {}),
            'diagnose_replay': dict(contract.get('diagnose_replay') or {}),
        }
        contract['runtime_diagnose_bundle']['operator_action_hint'] = build_final_execution_operator_hint(contract['runtime_diagnose_bundle'])
        contract['runtime_diagnose_bundle']['next_step_hint'] = dict(contract['runtime_diagnose_bundle']['operator_action_hint'])
        contract['runtime_diagnose_bundle']['next_step_summary'] = contract['runtime_diagnose_bundle']['operator_action_hint'].get('summary')
        return contract

    def _build_execution_plan_context(self, row: dict) -> dict:
        """把风控阶段算出的 adaptive/live context 原样带进 executor。"""
        row = dict(row or {})
        risk_details = dict(row.get('risk_details') or {})
        signal = row.get('signal')

        layer_plan = dict((risk_details.get('exposure_limit') or {}).get('layer_plan') or (risk_details.get('layer_eligibility') or {}).get('layer_plan') or {})
        observability = dict(risk_details.get('observability') or {})
        adaptive_risk_snapshot = dict(risk_details.get('adaptive_risk_snapshot') or observability.get('adaptive_risk_snapshot') or {})
        entry_plan = dict((risk_details.get('exposure_limit') or {}).get('entry_plan') or layer_plan.get('entry_plan') or {})

        regime_snapshot = dict(
            layer_plan.get('regime_snapshot')
            or observability.get('regime_snapshot')
            or getattr(signal, 'regime_snapshot', {})
            or getattr(signal, 'regime_info', {})
            or {}
        )
        adaptive_policy_snapshot = dict(
            layer_plan.get('adaptive_policy_snapshot')
            or observability.get('adaptive_policy_snapshot')
            or getattr(signal, 'adaptive_policy_snapshot', {})
            or {}
        )

        strategy_selection = dict(((getattr(signal, 'market_context', {}) or {}).get('strategy_selection')) or {})
        plan_context = dict(layer_plan)
        plan_context.update({
            'current_price': row.get('current_price'),
            'signal_id': row.get('signal_id'),
            'root_signal_id': row.get('signal_id'),
            'regime_snapshot': regime_snapshot,
            'adaptive_policy_snapshot': adaptive_policy_snapshot,
            'strategy_selection': strategy_selection,
            'strategy_tags': list(strategy_selection.get('selected_strategies') or getattr(signal, 'strategies_triggered', []) or []),
            'strategies_triggered': list(getattr(signal, 'strategies_triggered', []) or []),
            'final_execution_permit': self._build_final_execution_permit_contract(row),
        })
        if adaptive_risk_snapshot:
            plan_context['adaptive_risk_snapshot'] = adaptive_risk_snapshot
        if entry_plan:
            plan_context['entry_plan'] = entry_plan
            plan_context.setdefault('planned_margin', entry_plan.get('allowed_margin'))
            plan_context.setdefault('layer_ratio', entry_plan.get('effective_entry_margin_ratio') or plan_context.get('layer_ratio'))
        if observability:
            plan_context['observability'] = observability
        return plan_context
    
    def run(self):
        """运行交易循环"""
        started_at = datetime.now()
        summary = {'started_at': started_at.isoformat(), 'symbols': len(self.config.symbols), 'signals': 0, 'passed': 0, 'opened': 0, 'closed': 0, 'errors': 0}
        print(f"\n{'='*60}")
        print(f"🤖 OKX量化交易系统 v2.0")
        print(f"   时间: {started_at}")
        print(f"   币种: {', '.join(self.config.symbols)}")
        print(f"{'='*60}\n")
        # 开始类推送太噪音，改为只写运行历史，不主动推送
        state = load_runtime_state()
        state.update({'running': True, 'last_started_at': started_at.isoformat(), 'last_error': None})
        save_runtime_state(state)
        append_runtime_history({'type': 'start', 'time': started_at.isoformat(), 'message': '机器人周期开始'})
        
        # 获取余额
        try:
            balance = self.exchange.fetch_balance()
            available = balance.get('free', {}).get('USDT', 0)
            print(f"💰 账户余额: {available:.2f} USDT\n")
        except Exception as e:
            print(f"⚠️ 获取余额失败: {e}")
            available = 0
        
        # 先与交易所持仓对账
        reconcile_report = {'synced': 0, 'removed': 0}
        try:
            reconcile_report = reconcile_exchange_positions(self.exchange, self.db)
            reconcile_summary = reconcile_report.get('summary', {})
            reconcile_diff_count = sum(int(reconcile_summary.get(k, 0) or 0) for k in ['exchange_missing_local_position', 'local_position_missing_exchange', 'open_trade_missing_exchange', 'exchange_missing_open_trade'])
            if reconcile_diff_count > 0:
                self.notifier.notify_reconcile_issue(reconcile_report)
        except Exception as e:
            self.notifier.notify_error('持仓对账失败', str(e), {})
        # 获取当前持仓
        positions = self.db.get_positions()
        print(f"📊 当前持仓: {len(positions)}个")
        for p in positions:
            print(f"   {p['symbol']} | {p['side']} | {p['quantity']} | "
                  f"开仓: {p['entry_price']:.2f} | 当前: {p.get('current_price', 'N/A')}")
        print()

        candidate_contracts = []

        # 遍历所有监控的币种
        for symbol in self.config.symbols:
            print(f"=== 分析 {symbol} ===")
            
            try:
                if not self.exchange.is_futures_symbol(symbol):
                    self.notifier.notify_runtime('skip', [f'币种：{symbol}', '原因：暂无 U 本位永续合约'], {'symbol': symbol, 'reason': 'not-futures'})
                    print(f"   ⏭️ 跳过: {symbol} 暂无U本位永续合约")
                    print()
                    continue
                # 获取K线数据
                ohlcv = self.exchange.fetch_ohlcv(symbol, '1h', limit=100)
                df = pd.DataFrame(ohlcv)
                df = self._add_indicators(df)
                
                # 获取当前价格
                ticker = self.exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                
                # 获取ML预测
                ml_pred = None
                if self.ml.enabled:
                    ml_pred = self.ml.predict(symbol, ohlcv)
                
                # 分析信号
                signal = self.detector.analyze(symbol, df, current_price, ml_pred)
                strategy_selection = self._build_strategy_selection_contract(symbol, signal)
                signal = self.detector.apply_strategy_selection(signal, strategy_selection, symbol=symbol)
                signal.filter_details = signal.filter_details or {}
                signal.filter_details['strategy_selection'] = strategy_selection
                
                print(f"   价格: {current_price:.4f}")
                print(f"   信号: {signal.signal_type.upper()} | 强度: {signal.strength}%")
                print(f"   触发策略: {', '.join(signal.strategies_triggered) or '无'}")
                print(f"   🧠 策略选择: {', '.join(strategy_selection.get('selected_strategies') or []) or '无'} | {strategy_selection.get('decision_summary')}")
                
                # 详细指标
                indicators = signal.indicators
                if 'RSI' in indicators:
                    print(f"   RSI: {indicators.get('RSI', 'N/A')}")
                if 'MACD' in indicators:
                    print(f"   MACD: {indicators.get('MACD', 'N/A')}")
                
                # 获取当前持仓（每个 symbol 都重新读取，避免沿用本轮旧快照）
                positions = self.db.get_positions()
                current_positions = {p['symbol']: p for p in positions}
                
                # ===== Entry Decision Layer MVP =====
                tracking_data = {}
                ml_pred = ml_pred if 'ml_pred' in dir() else None
                entry_decision = self.entry_decider.decide(
                    signal, 
                    current_positions=current_positions,
                    tracking_data=tracking_data,
                    ml_prediction=ml_pred
                )
                signal.filter_details = signal.filter_details or {}
                signal.filter_details['entry_decision'] = entry_decision.to_dict()
                print(f"   🎯 开单决策: {entry_decision.decision.upper()} | 分数: {entry_decision.score}")
                
                # 验证信号
                passed, reason, details = self.validator.validate(signal, current_positions)

                # Entry Decision 真正接入执行门槛：watch/block 不再继续开仓
                if passed and signal.signal_type in ['buy', 'sell'] and entry_decision.decision != 'allow':
                    passed = False
                    reason = f"EntryDecision={entry_decision.decision}: {entry_decision.reason_summary}"
                    details = details or {}
                    details['entry_decision_gate'] = {
                        'passed': False,
                        'reason': reason,
                        'decision': entry_decision.decision,
                        'score': entry_decision.score,
                        'watch_reasons': entry_decision.watch_reasons,
                        'group': 'entry_decision',
                    }
                    details['filter_meta'] = {
                        'code': f"ENTRY_DECISION_{entry_decision.decision.upper()}",
                        'group': 'entry_decision',
                        'action_hint': '仅当 Entry Decision=allow 时才执行开仓，当前建议继续观望',
                    }

                summary['signals'] += 1
                if passed:
                    summary['passed'] += 1
                signal.filtered = not passed
                signal.filter_reason = reason
                
                if not passed:
                    print(f"   ❌ 信号过滤: {reason}")
                details = self._notification_context(signal, details)
                self.notifier.notify_signal(signal, passed, reason, details)
                
                # 记录信号
                signal_id = self.recorder.record(signal, (passed, reason, details))
                base_obs = {
                    'signal_id': signal_id,
                    'root_signal_id': signal_id,
                    'layer_no': None,
                    'deny_reason': reason if not passed else None,
                    'current_symbol_exposure': 0.0,
                    'projected_symbol_exposure': 0.0,
                    'current_total_exposure': 0.0,
                    'projected_total_exposure': 0.0,
                }
                merged_filter_details = dict(signal.filter_details or {})
                merged_filter_details['observability'] = {**base_obs, **dict(merged_filter_details.get('observability') or {})}
                self.db.update_signal(signal_id, filter_details=json.dumps(merged_filter_details, ensure_ascii=False))

                can_open = False
                risk_reason = None
                risk_details = {}
                if passed and signal.signal_type in ['buy', 'sell']:
                    can_open, risk_reason, risk_details = self.risk_mgr.can_open_position(symbol, side='long' if signal.signal_type == 'buy' else 'short', signal_id=signal_id, plan_context={'regime_snapshot': getattr(signal, 'regime_snapshot', {}) or getattr(signal, 'regime_info', {}) or {}, 'adaptive_policy_snapshot': getattr(signal, 'adaptive_policy_snapshot', {}) or {}})
                    risk_obs = dict((risk_details or {}).get('observability') or {})
                    if risk_obs:
                        merged_filter_details = dict(signal.filter_details or {})
                        merged_filter_details['observability'] = {**dict(merged_filter_details.get('observability') or {}), **risk_obs, 'deny_reason': None if can_open else (risk_reason or risk_obs.get('deny_reason'))}
                        self.db.update_signal(signal_id, filter_details=json.dumps(merged_filter_details, ensure_ascii=False), filter_reason=(None if can_open else risk_reason))
                    risk_details = self._notification_context(signal, risk_details)
                    self.notifier.notify_decision(signal, can_open, risk_reason, risk_details)
                    lock_info = (risk_details or {}).get('loss_streak_limit', {}) if isinstance(risk_details, dict) else {}
                    if lock_info.get('just_triggered'):
                        self.notifier.notify_loss_streak_lock(
                            current=lock_info.get('current', 0),
                            max_count=lock_info.get('max', 0),
                            recover_at=lock_info.get('recover_at'),
                            details={'symbol': symbol, 'risk_details': risk_details}
                        )
                    if not can_open:
                        print(f"   ⏸️ 风险检查阻止: {risk_reason}")

                candidate_contracts.append(self._build_candidate_contract(
                    symbol=symbol,
                    current_price=current_price,
                    signal=signal,
                    signal_id=signal_id,
                    passed=passed,
                    reason=reason,
                    details=details or {},
                    entry_decision=entry_decision,
                    can_open=can_open,
                    risk_reason=risk_reason,
                    risk_details=risk_details or {},
                ))
                
                print()
                
            except Exception as e:
                summary['errors'] += 1
                self.notifier.notify_error('处理币种出错', f'{symbol}: {e}', {'symbol': symbol})
                logger.error(f"处理{symbol}出错: {e}")
                print(f"   ⚠️ 错误: {e}\n")

        ranked_candidates, selected_candidates = self._rank_open_candidates(candidate_contracts)
        candidate_runtime_summary = self._build_candidate_runtime_summary(ranked_candidates, selected_candidates)
        summary['candidate_ranking'] = candidate_runtime_summary
        summary['candidate_skip_contracts'] = candidate_runtime_summary.get('skip_contracts') or []
        summary['candidate_selected'] = [
            {
                'symbol': row.get('symbol'),
                'signal_id': row.get('signal_id'),
                'rank': (row.get('ranking_contract') or {}).get('rank'),
                'priority_score': (row.get('ranking_contract') or {}).get('priority_score'),
                'diversification_context': (row.get('ranking_contract') or {}).get('diversification_context') or {},
                'diversification': (row.get('ranking_contract') or {}).get('diversification') or {},
                'execution_contract': (row.get('execution_contract') or {}),
                'final_execution_permit': self._build_final_execution_permit_contract(row),
            }
            for row in selected_candidates
        ]

        if ranked_candidates:
            print("=== 开仓候选排序 ===")
            for row in ranked_candidates:
                ranking = row.get('ranking_contract') or {}
                skip_contract = row.get('skip_contract') or {}
                status = 'SELECTED' if ranking.get('selected') else skip_contract.get('reason_code') or ('READY' if row.get('can_open') else 'SKIP')
                diversification = ranking.get('diversification') or {}
                fairness = ','.join(diversification.get('reason_codes') or []) or 'clear'
                execution_contract = row.get('execution_contract') or {}
                exec_status = execution_contract.get('reason_code') or execution_contract.get('status') or '--'
                print(
                    f"   #{ranking.get('rank')} {row.get('symbol')} | score={ranking.get('priority_score')} | scope={ranking.get('close_outcome_scope_mode')}:{ranking.get('close_outcome_scope') or '--'} | fair={fairness} | exec={exec_status} | {status}"
                )
            print()

        permit_reason_counts = Counter()
        final_execution_permits = []
        for row in selected_candidates:
            symbol = row.get('symbol')
            signal = row.get('signal')
            current_price = row.get('current_price')
            signal_id = row.get('signal_id')
            side = row.get('side')
            risk_details = row.get('risk_details') or {}
            execution_plan_context = self._build_execution_plan_context(row)
            final_permit = dict(execution_plan_context.get('final_execution_permit') or {})
            final_execution_permits.append(final_permit)
            permit_reason_counts[str(final_permit.get('reason_code') or 'UNKNOWN')] += 1
            row['final_execution_permit'] = final_permit

            if not final_permit.get('allowed', False):
                summary.setdefault('final_execution_denied', []).append({
                    'symbol': symbol,
                    'signal_id': signal_id,
                    'side': side,
                    'contract': final_permit,
                })
                print(f"   ⛔ 最终执行许可拒绝: {symbol} | {final_permit.get('reason_code')} | {final_permit.get('reason')}")
                continue

            trade_id = self.executor.open_position(
                symbol, side, current_price, signal_id,
                plan_context=execution_plan_context,
                root_signal_id=signal_id
            )
            if trade_id:
                summary['opened'] += 1
                self.recorder.mark_executed(signal_id, trade_id)
                latest_trade = self.db.get_latest_open_trade(symbol, side)
                contracts = latest_trade.get('quantity') if latest_trade else 0
                quantity_details = {}
                try:
                    contract_size = self.exchange.get_contract_size(symbol)
                    coin_quantity = self.exchange.contracts_to_coin_quantity(symbol, contracts)
                    quantity_details = {
                        'contracts': contracts,
                        'contract_size': contract_size,
                        'coin_quantity': coin_quantity,
                        'notional_usdt': self.exchange.estimate_notional_usdt(symbol, contracts, current_price),
                    }
                except Exception:
                    quantity_details = {}
                self.notifier.notify_trade_open(symbol, side, current_price, contracts, trade_id, signal, quantity_details=quantity_details)
                print(f"   ✅ 开{'多' if side == 'long' else '空'}成功! Trade ID: {trade_id}")
            else:
                self.notifier.notify_trade_open_failed(
                    symbol,
                    side,
                    current_price,
                    '交易所拒绝或执行器返回空结果',
                    signal,
                    self._notification_context(signal, {'signal_id': signal_id}),
                )
                print(f"   ❌ 开仓失败: {symbol}")
        
        summary['final_execution_permits'] = {
            'schema_version': 'final_execution_permit_summary_v1',
            'count': len(final_execution_permits),
            'permit_count': sum(1 for item in final_execution_permits if item.get('allowed')),
            'deny_count': sum(1 for item in final_execution_permits if not item.get('allowed')),
            'reason_code_counts': dict(permit_reason_counts),
            'items': final_execution_permits,
        }
        summary['final_execution_permit_replay'] = {
            'schema_version': 'final_execution_permit_replay_collection_v1',
            'count': len(final_execution_permits),
            'reason_code_counts': dict(permit_reason_counts),
            'items': [dict((item.get('diagnose_replay') or {})) for item in final_execution_permits],
            'denied_items': [dict((item.get('diagnose_replay') or {})) for item in final_execution_permits if not item.get('allowed')],
        }

        # 检查现有持仓的止盈止损
        print("=== 检查持仓 ===")
        positions = self.db.get_positions()
        
        for position in positions:
            symbol = position['symbol']
            
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                
                # 更新持仓价格
                self.db.update_position(
                    symbol, position['side'], position['entry_price'],
                    position['quantity'], position['leverage'], current_price
                )
                
                # 检查止损
                if self.executor.check_stop_loss(symbol, current_price):
                    closed = self.executor.close_position(symbol, '止损')
                    if closed:
                        summary['closed'] += 1
                        self.notifier.notify_trade_close(symbol, position['side'], current_price, '止损')
                    else:
                        self.notifier.notify_trade_close_failed(symbol, position['side'], '止损触发后平仓失败', {'symbol': symbol, 'reason': '止损'})
                    print(f"   🔴 止损: {symbol}")
                
                # 检查止盈
                elif self.executor.check_take_profit(symbol, current_price):
                    closed = self.executor.close_position(symbol, '止盈')
                    if closed:
                        summary['closed'] += 1
                        self.notifier.notify_trade_close(symbol, position['side'], current_price, '止盈')
                    else:
                        self.notifier.notify_trade_close_failed(symbol, position['side'], '止盈触发后平仓失败', {'symbol': symbol, 'reason': '止盈'})
                    print(f"   🟢 止盈: {symbol}")
                
            except Exception as e:
                summary['errors'] += 1
                self.notifier.notify_error('检查持仓失败', f'{symbol}: {e}', {'symbol': symbol})
                logger.error(f"检查持仓{symbol}出错: {e}")
        finished_at = datetime.now()
        summary['finished_at'] = finished_at.isoformat()
        self._persist_candidate_diversification_history(selected_candidates)
        state = load_runtime_state()
        state.update({'running': False, 'last_finished_at': finished_at.isoformat(), 'last_summary': summary})
        save_runtime_state(state)
        append_runtime_history({'type': 'end', 'time': finished_at.isoformat(), 'message': f'周期完成：信号 {summary["signals"]} / 开仓 {summary["opened"]} / 平仓 {summary["closed"]} / 错误 {summary["errors"]}'})
        end_lines = [
            f'开始：{summary["started_at"]}',
            f'结束：{summary["finished_at"]}',
            f'监听币种：{", ".join(self.config.symbols)}',
            f'持仓对账：同步 {reconcile_report.get("synced", 0) if isinstance(reconcile_report, dict) else 0} 条 ｜ 清理 {reconcile_report.get("removed", 0) if isinstance(reconcile_report, dict) else 0} 条',
            f'信号：{summary["signals"]} ｜ 通过：{summary["passed"]} ｜ 开仓：{summary["opened"]} ｜ 平仓：{summary["closed"]} ｜ 错误：{summary["errors"]}',
            f'候选排序：{(summary.get("candidate_ranking") or {}).get("candidate_count", 0)} ｜ 选中：{(summary.get("candidate_ranking") or {}).get("selected_count", 0)} ｜ skip：{len(summary.get("candidate_skip_contracts") or [])}',
            f'公平围栏：{", ".join(sorted({code for item in ((summary.get("candidate_ranking") or {}).get("items") or []) for code in (((item.get("diversification") or {}).get("reason_codes") or []))})) or "clear"}',
            f'执行配额：cycle={(summary.get("candidate_ranking") or {}).get("execution_quota", {}).get("max_new_positions_per_cycle", 0)} ｜ cluster-cap={(summary.get("candidate_ranking") or {}).get("execution_quota", {}).get("max_same_cluster_per_cycle", 0)} ｜ reasons={json.dumps((summary.get("candidate_ranking") or {}).get("execution_quota", {}).get("reason_code_counts", {}), ensure_ascii=False)}',
            f'最终许可：permit={(summary.get("final_execution_permits") or {}).get("permit_count", 0)} ｜ deny={(summary.get("final_execution_permits") or {}).get("deny_count", 0)} ｜ reasons={json.dumps((summary.get("final_execution_permits") or {}).get("reason_code_counts", {}), ensure_ascii=False)}'
        ]
        self.notifier.notify_runtime('end', end_lines, summary)
        print(f"\n✅ 交易循环完成! {finished_at}\n")
        return summary
    
    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """添加技术指标"""
        close = df[4]
        
        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(14).mean()
        avg_loss = loss.rolling(14).mean()
        rs = avg_gain / (avg_loss + 1e-10)
        df['RSI'] = 100 - (100 / (1 + rs))
        
        # MACD
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        df['MACD'] = ema12 - ema26
        df['MACD_signal'] = df['MACD'].ewm(span=9).mean()
        
        # 布林带
        df['BB_mid'] = close.rolling(20).mean()
        std = close.rolling(20).std()
        df['BB_upper'] = df['BB_mid'] + 2 * std
        df['BB_lower'] = df['BB_mid'] - 2 * std
        
        return df


def main():
    parser = argparse.ArgumentParser(description='OKX量化交易机器人')
    parser.add_argument('--dashboard', action='store_true', help='启动仪表盘')
    parser.add_argument('--daemon', action='store_true', help='守护模式定时运行交易循环')
    parser.add_argument('--interval-seconds', type=int, help='守护模式执行间隔秒数，默认取 config.runtime.interval_seconds')
    parser.add_argument('--train', action='store_true', help='训练模型')
    parser.add_argument('--collect', action='store_true', help='收集数据')
    parser.add_argument('--backtest', action='store_true', help='运行回测')
    parser.add_argument('--signal-quality', action='store_true', help='分析信号质量')
    parser.add_argument('--optimize', action='store_true', help='运行参数优化与币种分层')
    parser.add_argument('--list-presets', action='store_true', help='列出可用预设')
    parser.add_argument('--apply-preset', type=str, help='应用预设配置')
    parser.add_argument('--mode-status', action='store_true', help='显示当前模式状态')
    parser.add_argument('--daily-summary', action='store_true', help='生成日报摘要')
    parser.add_argument('--health-summary', action='store_true', help='立即生成一份健康汇总并发送通知')
    parser.add_argument('--approval-hygiene', action='store_true', help='执行审批卫生检查；开启 auto_cleanup 时会自动收口 stale 审批')
    parser.add_argument('--adaptive-rollout-orchestration', action='store_true', help='执行一轮 runtime adaptive rollout orchestration（workflow_ready -> executor/approval/review/recovery）')
    parser.add_argument('--cleanup-runtime-records', action='store_true', help='清理重复的治理/日报运行记录')
    parser.add_argument('--exchange-diagnose', action='store_true', help='只读诊断交易所/合约参数，不执行下单')
    parser.add_argument('--notify-test', action='store_true', help='测试 Discord/webhook 通知链路')
    parser.add_argument('--relay-outbox', action='store_true', help='补发 pending notification_outbox 到 Discord')
    parser.add_argument('--once', action='store_true', help='配合 relay-outbox，仅执行一轮后退出')
    parser.add_argument('--relay-limit', type=int, default=20, help='配合 relay-outbox，单轮最多处理几条 pending outbox')
    parser.add_argument('--reconcile-positions', action='store_true', help='只读/同步交易所持仓到本地 DB')
    parser.add_argument('--exchange-smoke', action='store_true', help='生成最小 testnet 验收计划；默认只预演')
    parser.add_argument('--validation-entry', type=str, choices=['run'], help='运行单个 shadow validation case')
    parser.add_argument('--validation-replay', action='store_true', help='运行 validation replay，支持目录/多 case 并输出聚合 summary')
    parser.add_argument('--case', type=str, nargs='+', help='validation case 文件或目录路径（json/yaml，可传多个）')
    parser.add_argument('--validation-output', type=str, help='可选：将 validation report / replay report 写入指定 json/md 文件（.md 导出人类可读摘要）')
    parser.add_argument('--execute', action='store_true', help='配合 smoke 验收命令，显式允许执行 testnet 开平仓')
    parser.add_argument('--symbol', type=str, help='指定 smoke/diagnose 目标币种')
    parser.add_argument('--side', type=str, default='long', choices=['long', 'short'], help='smoke 验收方向')
    parser.add_argument('--dry-run', action='store_true', help='配合清理命令，仅预览不删除')
    parser.add_argument('--port', type=int, default=5555, help='仪表盘端口（默认 5555）')
    
    args = parser.parse_args()
    
    if args.dashboard:
        # 启动仪表盘
        from dashboard.api import run_dashboard
        run_dashboard(port=args.port)

    elif args.daemon:
        cfg = Config()
        interval = args.interval_seconds or int(cfg.get('runtime.interval_seconds', 300))
        guard = RuntimeGuard()
        notifier = NotificationManager(cfg, Database(cfg.db_path), logger)
        save_runtime_state({'mode': 'daemon', 'running': False, 'interval_seconds': interval, 'next_run_at': datetime.now().isoformat()})
        notifier.notify_runtime('daemon', [f'守护间隔：{interval} 秒', f'监控币种：{", ".join(cfg.symbols)}'])
        print(f"\n🔁 守护模式启动，间隔 {interval} 秒\n")
        while True:
            if not guard.acquire():
                now = datetime.now().isoformat()
                state = load_runtime_state()
                state.update({'running': False, 'last_skip_at': now, 'next_run_at': datetime.fromtimestamp(time.time() + interval).isoformat()})
                save_runtime_state(state)
                append_runtime_history({'type': 'skip', 'time': now, 'message': '检测到已有交易周期正在运行，本轮跳过'})
                notifier.notify_runtime('skip', ['检测到已有交易周期正在运行，本轮跳过'])
                time.sleep(interval)
                continue
            try:
                bot = TradingBot()
                bot.run()
            except Exception as e:
                error_time = datetime.now().isoformat()
                state = load_runtime_state()
                db = Database(cfg.db_path)
                notification_stats = db.get_notification_outbox_stats()
                relay_state = state.get('relay', {}) if isinstance(state.get('relay'), dict) else {}
                error_context = {
                    'interval_seconds': interval,
                    'mode': state.get('mode') or 'daemon',
                    'next_run_at': state.get('next_run_at'),
                    'relay_running': relay_state.get('running', False),
                    'relay_last_checked_at': relay_state.get('last_checked_at'),
                    'relay_last_result': relay_state.get('last_result', {}),
                    'notification_stats': notification_stats,
                }
                state.update({'running': False, 'last_error': str(e), 'last_error_at': error_time, 'last_error_context': error_context})
                save_runtime_state(state)
                append_runtime_history({'type': 'error', 'time': error_time, 'message': str(e), 'context': error_context})
                notifier.notify_error('守护周期异常', str(e), {'interval': interval, 'context': error_context})
                logger.error(f'守护周期异常: {e}')
            finally:
                guard.release()
            state = load_runtime_state()
            state.update({'next_run_at': datetime.fromtimestamp(time.time() + interval).isoformat(), 'interval_seconds': interval})
            save_runtime_state(state)
            try:
                maybe_run_approval_hygiene(cfg, Database(cfg.db_path), notifier)
            except Exception as e:
                logger.error(f'执行 approval hygiene 失败: {e}')
            try:
                maybe_run_adaptive_rollout_orchestration(cfg, Database(cfg.db_path), notifier)
            except Exception as e:
                logger.error(f'执行 adaptive rollout orchestration 失败: {e}')
            try:
                maybe_send_daily_health_summary(cfg, Database(cfg.db_path), notifier)
            except Exception as e:
                logger.error(f'发送每日健康汇总失败: {e}')
            time.sleep(interval)
    
    elif args.train:
        # 训练模型
        print("\n🎯 开始训练模型...\n")
        cfg = Config()
        exchange = Exchange(cfg.all)
        collector = DataCollector(exchange, cfg.all)
        trainer = ModelTrainer(cfg.all)
        results = {}

        for symbol in cfg.symbols:
            if not exchange.is_futures_symbol(symbol):
                print(f"跳过 {symbol}: 暂无U本位永续合约")
                results[symbol] = False
                continue
            print(f"收集并训练 {symbol}...")
            if collector.collect_data(symbol, '1h', 1000):
                import pandas as pd
                csv_name = symbol.replace('/', '_').replace(':', '_')
                symbol_map = {
                    'BTC/USDT': 'BTC_USDT',
                    'ETH/USDT': 'ETH_USDT',
                    'SOL/USDT': 'SOL_USDT',
                    'XRP/USDT': 'XRP_USDT',
                    'HYPE/USDT': 'HYPE_USDT'
                }
                filename = symbol_map.get(symbol, csv_name)
                df = pd.read_csv(f"ml/data/{filename}_1h.csv")
                results[symbol] = trainer.train(symbol, df)
            else:
                results[symbol] = False

        print("\n训练结果:")
        for symbol, success in results.items():
            print(f"   {symbol}: {'✅ 成功' if success else '❌ 失败'}")

    elif args.collect:
        # 收集数据
        print("\n📊 开始收集数据...\n")
        config = Config()
        exchange = Exchange(config.all)
        collector = DataCollector(exchange, config.all)

        for symbol in config.symbols:
            if not exchange.is_futures_symbol(symbol):
                print(f"跳过 {symbol}: 暂无U本位永续合约")
                continue
            print(f"收集 {symbol}...")
            collector.collect_data(symbol, '1h', 1000)

    elif args.backtest:
        print("\n🧪 开始回测...\n")
        cfg = Config()
        backtester = StrategyBacktester(cfg)
        result = backtester.run_all()
        print("回测总览:")
        print(result['summary'])
        print("\n分币种结果:")
        for row in result['symbols']:
            print(f"  {row['symbol']}: trades={row['trades']} win_rate={row['win_rate']}% return={row['total_return_pct']}% dd={row['max_drawdown_pct']}%")

    elif args.signal_quality:
        print("\n🔎 开始分析信号质量...\n")
        cfg = Config()
        db = Database(cfg.db_path)
        analyzer = SignalQualityAnalyzer(cfg, db)
        result = analyzer.analyze()
        print("信号质量总览:")
        print(result['summary'])
        print("\n分币种质量:")
        for row in result['by_symbol']:
            print(f"  {row['symbol']}: signals={row['signals']} positive_rate={row['positive_rate']}% avg_quality={row['avg_quality_pct']}%")

    elif args.optimize:
        print("\n⚙️ 开始参数优化与币种分层...\n")
        cfg = Config()
        db = Database(cfg.db_path)
        optimizer = ParameterOptimizer(cfg, db)
        result = optimizer.run(use_cache=False)
        print("最佳实验:")
        print(result['best_experiment'])
        print("\n币种分层建议:")
        for row in result['symbol_advice']:
            print(f"  {row['symbol']}: {row['tier']} | backtest={row['backtest_return_pct']}% | quality={row['avg_quality_pct']}% | {row['action']}")
        print("\n单币种专项实验:")
        for symbol, rows in result.get('symbol_specific', {}).items():
            print(f"  [{symbol}]")
            for row in rows:
                print(f"    {row['name']}: score={row['score']} return={row['summary']['total_return_pct']}% win={row['summary']['win_rate']}% dd={row['summary']['max_drawdown_pct']}%")
        print("\n候选晋升判断:")
        for row in result.get('candidate_promotions', []):
            print(f"  {row['symbol']}: {row['decision']} | {row['reason']}")
        print("\n预设配置:")
        for preset in result.get('presets', []):
            print(f"  {preset['name']}: {preset['path']}")

    elif args.list_presets:
        pm = PresetManager(Config())
        print("\n📦 可用预设:\n")
        for row in pm.list_presets():
            print(f"  {row['name']}: watch={row['watch_list']} candidate={row['candidate_watch_list']} paused={row['paused_watch_list']}")

    elif args.apply_preset:
        pm = PresetManager(Config())
        result = pm.apply_preset(args.apply_preset, auto_restart=True)
        print("\n✅ 已应用预设:\n")
        print(result)

    elif args.mode_status:
        pm = PresetManager(Config())
        print("\n🧭 当前模式:\n")
        print(pm.status())

    elif args.daily_summary:
        cfg = Config()
        db = Database(cfg.db_path)
        gov = GovernanceEngine(cfg, db)
        print("\n📰 今日日报:\n")
        print(gov.generate_daily_summary())

    elif args.health_summary:
        cfg = Config()
        db = Database(cfg.db_path)
        notifier = NotificationManager(cfg, db, logger)
        result = maybe_send_daily_health_summary(cfg, db, notifier, force=True)
        print("\n🩺 健康汇总:\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.approval_hygiene:
        cfg = Config()
        db = Database(cfg.db_path)
        notifier = NotificationManager(cfg, db, logger)
        result = maybe_run_approval_hygiene(cfg, db, notifier, force=True)
        print("\n🧼 审批卫生结果:\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.adaptive_rollout_orchestration:
        cfg = Config()
        db = Database(cfg.db_path)
        notifier = NotificationManager(cfg, db, logger)
        result = maybe_run_adaptive_rollout_orchestration(cfg, db, notifier, force=True)
        print("\n🧭 Adaptive rollout orchestration 结果:\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.cleanup_runtime_records:
        cfg = Config()
        db = Database(cfg.db_path)
        print("\n🧹 清理运行期重复记录:\n")
        print(db.cleanup_duplicate_runtime_records(dry_run=args.dry_run))

    elif args.notify_test:
        cfg = Config()
        notifier = NotificationManager(cfg, Database(cfg.db_path), logger)
        result = notifier.test_discord()
        print("\n🔔 通知链路测试:\n")
        print(result['message'])
        print(f"delivered: {result['delivered']} | enabled: {result['enabled']}")

    elif args.relay_outbox:
        cfg = Config()
        interval = args.interval_seconds or int(cfg.get('runtime.notification_relay_interval_seconds', 30))
        run_notification_relay(interval=interval, once=args.once, limit=args.relay_limit)

    elif args.reconcile_positions:
        cfg = Config()
        exchange = Exchange(cfg.all)
        db = Database(cfg.db_path)
        report = reconcile_exchange_positions(exchange, db)
        print("\n🧭 持仓对账结果:\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("\n📌 对账摘要:\n")
        print(json.dumps(report.get('summary', {}), ensure_ascii=False, indent=2))

    elif args.exchange_diagnose:
        cfg = Config()
        exchange = Exchange(cfg.all)
        report = build_exchange_diagnostics(cfg, exchange)
        print("\n🩺 交易所只读诊断:\n")
        print(f"模式: {report['exchange_mode']} | 持仓模式: {report['position_mode']} | 可用USDT: {report.get('available_usdt', 0)}")
        if report.get('balance_error'):
            print(f"余额读取异常: {report['balance_error']}")
        for row in report['symbols']:
            if args.symbol and row['symbol'] != args.symbol:
                continue
            print(f"\n[{row['symbol']}]")
            if row.get('error'):
                print(f"  错误: {row['error']}")
                continue
            print(f"  futures: {'yes' if row.get('is_futures_symbol') else 'no'}")
            if row.get('order_symbol'):
                print(f"  order_symbol: {row['order_symbol']}")
            if row.get('last_price') is not None:
                print(f"  last_price: {row['last_price']}")
            if row.get('sample_amount') is not None:
                print(f"  sample_amount: {row['sample_amount']}")
            if row.get('order_params_preview'):
                print(f"  order_params_preview: {row['order_params_preview']}")
            if row.get('reason'):
                print(f"  reason: {row['reason']}")

    elif args.exchange_smoke:
        cfg = Config()
        exchange = Exchange(cfg.all)
        plan = build_exchange_smoke_plan(cfg, exchange, symbol=args.symbol, side=args.side)
        print("\n🧪 Testnet 最小验收计划:\n")
        if plan.get('error'):
            print(f"错误: {plan['error']}")
        else:
            print(f"模式: {plan['exchange_mode']} | 持仓模式: {plan['position_mode']} | 目标: {plan['symbol']} | 方向: {plan['side']}")
            print(f"可用USDT: {plan.get('available_usdt', 0)} | 最新价: {plan.get('last_price')} | 验收名义价值: {plan.get('smoke_notional')}")
            print(f"样例数量: {plan.get('sample_amount')} | 可执行: {'yes' if plan.get('execute_ready') else 'no'}")
            print('步骤:')
            for step in plan.get('steps', []):
                print(f"  - {step}")
            if plan.get('open_preview'):
                print(f"开仓预览: {plan['open_preview']}")
            if plan.get('close_preview'):
                print(f"平仓预览: {plan['close_preview']}")
        if args.execute:
            print("\n🚨 已显式允许执行 testnet 最小开平仓验收\n")
            db = Database(cfg.db_path)
            result = execute_exchange_smoke(cfg, exchange, symbol=args.symbol, side=args.side, db=db)
            if result.get('error'):
                print(f"执行结果: 失败 | {result['error']}")
            else:
                print(f"执行结果: 开仓 {'成功' if result.get('opened') else '失败'} / 平仓 {'成功' if result.get('closed') else '失败'}")
                if result.get('open_order'):
                    print(f"open_order: {result['open_order']}")
                if result.get('close_order'):
                    print(f"close_order: {result['close_order']}")
            if result.get('smoke_run_id'):
                print(f"smoke_run_id: {result['smoke_run_id']}")

    elif args.validation_entry:
        if not args.case or len(args.case) != 1:
            raise SystemExit('--validation-entry 需要配合单个 --case <file>')
        report = run_shadow_validation_case(args.case[0], base_config=Config())
        if args.validation_output:
            output_path = Path(args.validation_output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.suffix.lower() == '.md':
                output_path.write_text(format_validation_report_markdown(report), encoding='utf-8')
            else:
                output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print("\n🕶️ Shadow Validation Report:\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))

    elif args.validation_replay:
        if not args.case:
            raise SystemExit('--validation-replay 需要配合一个或多个 --case <file|dir>')
        report = run_shadow_validation_replay(args.case, base_config=Config())
        if args.validation_output:
            output_path = Path(args.validation_output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.suffix.lower() == '.md':
                output_path.write_text(format_validation_report_markdown(report), encoding='utf-8')
            else:
                output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print("\n🧪 Shadow Validation Replay Summary:\n")
        print(json.dumps(report['summary'], ensure_ascii=False, indent=2))

    else:
        # 运行交易
        bot = TradingBot()
        bot.run()


if __name__ == '__main__':
    main()

