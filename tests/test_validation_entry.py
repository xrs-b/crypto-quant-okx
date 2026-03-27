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
    load_validation_case,
    run_shadow_validation_case,
    run_shadow_validation_replay,
)

EXECUTION_FIXTURE = 'tests/fixtures/validation/execution/high-vol-tighten-long-001.yaml'
WORKFLOW_FIXTURE = 'tests/fixtures/validation/workflow/governance-approval-replay-001.yaml'
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


if __name__ == '__main__':
    unittest.main()
