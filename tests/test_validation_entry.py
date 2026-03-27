import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from validation.shadow_runner import (
    ValidationCaseError,
    collect_validation_case_paths,
    format_validation_report_markdown,
    load_validation_case,
    run_shadow_validation_case,
    run_shadow_validation_replay,
)

EXECUTION_FIXTURE = 'tests/fixtures/validation/execution/high-vol-tighten-long-001.yaml'
WORKFLOW_FIXTURE = 'tests/fixtures/validation/workflow/governance-approval-replay-001.yaml'
WORKFLOW_EXECUTOR_FIXTURE = 'tests/fixtures/validation/workflow/queue-executor-dry-run-001.yaml'
WORKFLOW_TESTNET_BRIDGE_FIXTURE = 'tests/fixtures/validation/workflow/testnet-bridge-plan-001.yaml'
WORKFLOW_TESTNET_BRIDGE_EXECUTE_FIXTURE = 'tests/fixtures/validation/workflow/testnet-bridge-execute-001.yaml'
WORKFLOW_TESTNET_BRIDGE_REAL_MODE_BLOCKED_FIXTURE = 'tests/fixtures/validation/workflow/testnet-bridge-real-mode-blocked-001.yaml'
WORKFLOW_TESTNET_BRIDGE_CLEANUP_NEEDED_FIXTURE = 'tests/fixtures/validation/workflow/testnet-bridge-cleanup-needed-001.yaml'
WORKFLOW_TESTNET_BRIDGE_BLOCKED_PENDING_FIXTURE = 'tests/fixtures/validation/workflow/testnet-bridge-pending-approval-blocked-001.yaml'
FIXTURE_DIR = 'tests/fixtures/validation'


class TestShadowValidationEntry(unittest.TestCase):
    def test_case_loader_accepts_yaml_fixture(self):
        case = load_validation_case(EXECUTION_FIXTURE)
        self.assertEqual(case.case_id, 'high-vol-tighten-long-001')
        self.assertEqual(case.case_type, 'shadow_execution')

    def test_case_loader_accepts_workflow_fixture(self):
        case = load_validation_case(WORKFLOW_FIXTURE)
        self.assertEqual(case.case_id, 'governance-approval-replay-001')
        self.assertEqual(case.case_type, 'shadow_workflow')

    def test_case_loader_rejects_missing_required_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_case = Path(tmpdir) / 'bad.yaml'
            bad_case.write_text(yaml.safe_dump({'case_type': 'shadow_execution', 'input': {'signal': {'symbol': 'BTC/USDT', 'signal_type': 'buy'}}}), encoding='utf-8')
            with self.assertRaises(ValidationCaseError):
                load_validation_case(str(bad_case))

    def test_case_loader_rejects_workflow_without_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_case = Path(tmpdir) / 'bad-workflow.yaml'
            bad_case.write_text(yaml.safe_dump({'case_id': 'wf-bad', 'case_type': 'shadow_workflow', 'mode': 'workflow_dry_run', 'input': {}}), encoding='utf-8')
            with self.assertRaises(ValidationCaseError):
                load_validation_case(str(bad_case))

    def test_shadow_runner_outputs_baseline_adaptive_and_diff(self):
        report = run_shadow_validation_case(EXECUTION_FIXTURE)
        self.assertEqual(report['case_id'], 'high-vol-tighten-long-001')
        self.assertIn(report['status'], {'pass', 'fail'})
        self.assertEqual(report['audit']['real_trade_execution'], False)
        self.assertIn('baseline', report)
        self.assertIn('adaptive', report)
        self.assertIn('diff', report)
        self.assertTrue(report['diff']['risk']['would_tighten'])
        self.assertTrue(report['diff']['execution']['execution_profile_really_enforced'])
        self.assertTrue(report['diff']['execution']['layering_profile_really_enforced'])
        self.assertFalse(report['adaptive']['validator']['passed'])
        self.assertEqual(report['adaptive']['validator']['reason'], 'adaptive 生效后信号强度不足')

    def test_shadow_workflow_runner_outputs_workflow_and_replay(self):
        report = run_shadow_validation_case(WORKFLOW_FIXTURE)
        self.assertEqual(report['case_type'], 'shadow_workflow')
        self.assertEqual(report['status'], 'pass')
        self.assertEqual(report['audit']['real_trade_execution'], False)
        self.assertFalse(report['audit']['dangerous_live_parameter_change'])
        self.assertEqual(report['diff']['workflow']['action_count'], 12)
        self.assertEqual(report['diff']['workflow']['approval_count'], 12)
        self.assertGreaterEqual(report['diff']['replay']['synced_count'], 12)
        self.assertEqual(report['artifacts']['approval_replay']['states'][0]['replay_source'], 'shadow_workflow_fixture')

    def test_shadow_workflow_runner_supports_rollout_executor_dry_run_fixture(self):
        report = run_shadow_validation_case(WORKFLOW_EXECUTOR_FIXTURE)
        self.assertEqual(report['case_type'], 'shadow_workflow')
        self.assertEqual(report['status'], 'pass')
        self.assertEqual(report['diff']['executor']['planned_count'], 1)
        self.assertEqual(report['diff']['executor']['dry_run_count'], 1)
        self.assertEqual(report['artifacts']['rollout_executor']['status'], 'dry_run')
        self.assertEqual(report['artifacts']['rollout_executor']['items'][0]['plan']['queue_plan']['dispatch_route'], 'manual_review_queue')

    def test_shadow_workflow_runner_supports_testnet_bridge_plan_fixture(self):
        report = run_shadow_validation_case(WORKFLOW_TESTNET_BRIDGE_FIXTURE)
        self.assertEqual(report['case_type'], 'shadow_workflow')
        self.assertEqual(report['status'], 'pass')
        self.assertTrue(report['diff']['testnet_bridge']['enabled'])
        self.assertTrue(report['diff']['testnet_bridge']['plan_only'])
        self.assertTrue(report['diff']['testnet_bridge']['execute_ready'])
        self.assertEqual(report['artifacts']['testnet_bridge']['mode'], 'plan_only')
        self.assertFalse(report['audit']['real_trade_execution'])
        self.assertEqual(report['artifacts']['workflow_consumer_view']['schema_version'], 'm5_workflow_consumer_view_v1')

    def test_shadow_workflow_runner_supports_testnet_bridge_controlled_execute_fixture(self):
        report = run_shadow_validation_case(WORKFLOW_TESTNET_BRIDGE_EXECUTE_FIXTURE)
        self.assertEqual(report['case_type'], 'shadow_workflow')
        self.assertEqual(report['status'], 'pass')
        self.assertEqual(report['diff']['testnet_bridge']['mode'], 'controlled_execute')
        self.assertEqual(report['diff']['testnet_bridge']['status'], 'controlled_execute')
        self.assertFalse(report['diff']['testnet_bridge']['blocked'])
        self.assertEqual(report['diff']['testnet_bridge']['open_status'], 'filled')
        self.assertEqual(report['diff']['testnet_bridge']['close_status'], 'filled')
        self.assertFalse(report['diff']['testnet_bridge']['cleanup_needed'])
        self.assertFalse(report['diff']['testnet_bridge']['residual_position_detected'])
        self.assertTrue(report['artifacts']['testnet_bridge']['result']['opened'])
        self.assertTrue(report['artifacts']['testnet_bridge']['result']['closed'])
        self.assertTrue(report['artifacts']['testnet_bridge']['result']['reconcile_summary']['open_order_confirmed'])
        self.assertTrue(report['artifacts']['testnet_bridge']['result']['reconcile_summary']['close_order_confirmed'])
        self.assertTrue(report['audit']['real_trade_execution'])
        self.assertTrue(report['artifacts']['testnet_bridge']['audit']['rollback_expected'])
        self.assertEqual(report['artifacts']['testnet_bridge_summary']['status'], 'controlled_execute')
        self.assertTrue(report['artifacts']['testnet_bridge_summary']['close_confirmed'])

    def test_shadow_workflow_runner_blocks_testnet_bridge_when_pending_approvals_exist(self):
        report = run_shadow_validation_case(WORKFLOW_TESTNET_BRIDGE_BLOCKED_PENDING_FIXTURE)
        self.assertEqual(report['case_type'], 'shadow_workflow')
        self.assertEqual(report['status'], 'pass')
        self.assertEqual(report['diff']['testnet_bridge']['status'], 'blocked')
        self.assertTrue(report['diff']['testnet_bridge']['blocked'])
        self.assertIn('workflow_pending_approvals_present', report['artifacts']['testnet_bridge']['blocking_reasons'])
        self.assertFalse(report['audit']['real_trade_execution'])

    def test_shadow_workflow_runner_surfaces_cleanup_needed_bridge_trail(self):
        report = run_shadow_validation_case(WORKFLOW_TESTNET_BRIDGE_CLEANUP_NEEDED_FIXTURE)
        self.assertEqual(report['case_type'], 'shadow_workflow')
        self.assertEqual(report['status'], 'pass')
        self.assertEqual(report['diff']['testnet_bridge']['status'], 'error')
        self.assertTrue(report['diff']['testnet_bridge']['cleanup_needed'])
        self.assertTrue(report['diff']['testnet_bridge']['residual_position_detected'])
        self.assertEqual(report['diff']['testnet_bridge']['open_status'], 'filled')
        self.assertEqual(report['diff']['testnet_bridge']['close_status'], 'submitted')
        self.assertEqual(report['diff']['testnet_bridge']['failure_compensation_hint'], 'manual_testnet_cleanup_required')
        self.assertEqual(report['artifacts']['testnet_bridge']['error'], 'cleanup_required_but_cleanup_not_confirmed')
        self.assertEqual(report['artifacts']['testnet_bridge']['result']['cleanup_result']['status'], 'manual_required')
        self.assertTrue(report['audit']['real_trade_execution'])

    def test_shadow_workflow_runner_blocks_testnet_bridge_when_real_mode_requested(self):
        report = run_shadow_validation_case(WORKFLOW_TESTNET_BRIDGE_REAL_MODE_BLOCKED_FIXTURE)
        self.assertEqual(report['case_type'], 'shadow_workflow')
        self.assertEqual(report['status'], 'pass')
        self.assertEqual(report['diff']['testnet_bridge']['status'], 'blocked')
        self.assertTrue(report['diff']['testnet_bridge']['blocked'])
        self.assertIn('exchange_mode_not_testnet', report['artifacts']['testnet_bridge']['blocking_reasons'])
        self.assertFalse(report['audit']['real_trade_execution'])
        self.assertIsNone(report['artifacts']['testnet_bridge']['result'])

    def test_collect_validation_case_paths_supports_directory(self):
        paths = collect_validation_case_paths([FIXTURE_DIR])
        self.assertIn(EXECUTION_FIXTURE, paths)
        self.assertIn(WORKFLOW_FIXTURE, paths)

    def test_validation_replay_outputs_aggregated_summary(self):
        replay = run_shadow_validation_replay([FIXTURE_DIR])
        self.assertEqual(replay['mode'], 'validation_replay')
        self.assertGreaterEqual(replay['summary']['case_count'], 2)
        self.assertEqual(replay['summary']['fail_count'], 0)
        self.assertEqual(replay['summary']['pass_count'], replay['summary']['case_count'])
        self.assertIn('shadow_execution', replay['summary']['case_types'])
        self.assertIn('shadow_workflow', replay['summary']['case_types'])
        self.assertEqual(replay['summary']['testnet_bridge']['case_count'], 5)
        self.assertEqual(replay['summary']['testnet_bridge']['status_counts']['plan_only'], 1)
        self.assertEqual(replay['summary']['testnet_bridge']['status_counts']['controlled_execute'], 1)
        self.assertEqual(replay['summary']['testnet_bridge']['status_counts']['blocked'], 2)
        self.assertEqual(replay['summary']['testnet_bridge']['status_counts']['error'], 1)
        self.assertIn('testnet-bridge-cleanup-needed-001', replay['summary']['testnet_bridge']['case_ids_requiring_cleanup'])

    def test_cli_validation_entry_prints_report_and_writes_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / 'report.json'
            proc = subprocess.run(
                [sys.executable, 'bot/run.py', '--validation-entry', 'run', '--case', EXECUTION_FIXTURE, '--validation-output', str(output_path)],
                cwd='/Volumes/MacHD/Projects/crypto-quant-okx',
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn('Shadow Validation Report', proc.stdout)
            payload = json.loads(output_path.read_text(encoding='utf-8'))
            self.assertEqual(payload['case_id'], 'high-vol-tighten-long-001')
            self.assertFalse(payload['audit']['real_trade_execution'])
            self.assertIn('diff', payload)

    def test_cli_validation_replay_prints_summary_and_writes_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / 'replay.json'
            proc = subprocess.run(
                [sys.executable, 'bot/run.py', '--validation-replay', '--case', FIXTURE_DIR, '--validation-output', str(output_path)],
                cwd='/Volumes/MacHD/Projects/crypto-quant-okx',
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn('Shadow Validation Replay Summary', proc.stdout)
            payload = json.loads(output_path.read_text(encoding='utf-8'))
            self.assertEqual(payload['mode'], 'validation_replay')
            self.assertGreaterEqual(payload['summary']['case_count'], 2)
            self.assertEqual(payload['summary']['fail_count'], 0)
            self.assertIn('testnet_bridge', payload['summary'])

    def test_validation_markdown_formatter_surfaces_bridge_summary(self):
        report = run_shadow_validation_replay([FIXTURE_DIR])
        markdown = format_validation_report_markdown(report)
        self.assertIn('# Shadow Validation Replay Report', markdown)
        self.assertIn('## Testnet Bridge', markdown)
        self.assertIn('testnet-bridge-cleanup-needed-001', markdown)

    def test_cli_validation_replay_writes_markdown_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / 'replay.md'
            subprocess.run(
                [sys.executable, 'bot/run.py', '--validation-replay', '--case', FIXTURE_DIR, '--validation-output', str(output_path)],
                cwd='/Volumes/MacHD/Projects/crypto-quant-okx',
                capture_output=True,
                text=True,
                check=True,
            )
            markdown = output_path.read_text(encoding='utf-8')
            self.assertIn('# Shadow Validation Replay Report', markdown)
            self.assertIn('### Blocking Reasons', markdown)
            self.assertIn('workflow_pending_approvals_present', markdown)


if __name__ == '__main__':
    unittest.main()
