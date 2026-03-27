import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from validation.shadow_runner import load_validation_case, run_shadow_validation_case, ValidationCaseError


FIXTURE = 'tests/fixtures/validation/execution/high-vol-tighten-long-001.yaml'


class TestShadowValidationEntry(unittest.TestCase):
    def test_case_loader_accepts_yaml_fixture(self):
        case = load_validation_case(FIXTURE)
        self.assertEqual(case.case_id, 'high-vol-tighten-long-001')
        self.assertEqual(case.case_type, 'shadow_execution')

    def test_case_loader_rejects_missing_required_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_case = Path(tmpdir) / 'bad.yaml'
            bad_case.write_text(yaml.safe_dump({'case_type': 'shadow_execution', 'input': {'signal': {'symbol': 'BTC/USDT', 'signal_type': 'buy'}}}), encoding='utf-8')
            with self.assertRaises(ValidationCaseError):
                load_validation_case(str(bad_case))

    def test_shadow_runner_outputs_baseline_adaptive_and_diff(self):
        report = run_shadow_validation_case(FIXTURE)
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

    def test_cli_validation_entry_prints_report_and_writes_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / 'report.json'
            proc = subprocess.run(
                [sys.executable, 'bot/run.py', '--validation-entry', 'run', '--case', FIXTURE, '--validation-output', str(output_path)],
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


if __name__ == '__main__':
    unittest.main()
