#!/usr/bin/env python3
"""连续执行最小 testnet smoke，并输出带验收结论的聚合结果。"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SMOKE_RESULT_PREFIX = 'SMOKE_RESULT_JSON='


def _extract_smoke_result(stdout: str) -> dict:
    for line in reversed((stdout or '').splitlines()):
        if line.startswith(SMOKE_RESULT_PREFIX):
            payload = line[len(SMOKE_RESULT_PREFIX):].strip()
            if not payload:
                return {}
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return {'parse_error': 'invalid_smoke_result_json', 'raw': payload}
    return {}


def _build_acceptance(smoke_result: dict, *, cli_ok: bool, preview_only: bool) -> dict:
    smoke_result = dict(smoke_result or {})
    reconcile = dict(smoke_result.get('reconcile_summary') or {})
    checks = {
        'cli_exit_ok': bool(cli_ok),
        'smoke_result_present': True if preview_only else bool(smoke_result),
        'opened': True if preview_only else bool(smoke_result.get('opened')),
        'closed': True if preview_only else bool(smoke_result.get('closed')),
        'cleanup_not_needed': True if preview_only else not bool(smoke_result.get('cleanup_needed', False)),
        'no_residual_position': True if preview_only else not bool(smoke_result.get('residual_position_detected', False)),
        'open_order_confirmed': True if preview_only else bool(reconcile.get('open_order_confirmed')),
        'close_order_confirmed': True if preview_only else bool(reconcile.get('close_order_confirmed')),
        'no_execution_error': not bool(smoke_result.get('error')),
    }
    failed_checks = [name for name, passed in checks.items() if not passed]
    status = 'pass' if not failed_checks else 'fail'
    if preview_only:
        status = 'preview_pass' if not failed_checks else 'preview_fail'
    return {
        'status': status,
        'passed': not failed_checks,
        'preview_only': bool(preview_only),
        'checks': checks,
        'failed_checks': failed_checks,
        'failure_compensation_hint': smoke_result.get('failure_compensation_hint'),
        'error': smoke_result.get('error'),
        'cleanup_needed': bool(smoke_result.get('cleanup_needed', False)),
        'residual_position_detected': bool(smoke_result.get('residual_position_detected', False)),
        'reconcile_summary': reconcile,
    }


def run_once(project_dir: Path, symbol: str, side: str, execute: bool) -> dict:
    cmd = [sys.executable, 'bot/run.py', '--exchange-smoke', '--symbol', symbol, '--side', side]
    if execute:
        cmd.append('--execute')
    proc = subprocess.run(cmd, cwd=str(project_dir), capture_output=True, text=True)
    smoke_result = _extract_smoke_result(proc.stdout)
    acceptance = _build_acceptance(smoke_result, cli_ok=proc.returncode == 0, preview_only=not execute)
    return {
        'returncode': proc.returncode,
        'stdout': proc.stdout,
        'stderr': proc.stderr,
        'ok': proc.returncode == 0,
        'smoke_result': smoke_result,
        'acceptance': acceptance,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Run minimal testnet smoke acceptance loop')
    parser.add_argument('--project-dir', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--symbol', default='BTC/USDT')
    parser.add_argument('--side', default='long', choices=['long', 'short'])
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--interval-seconds', type=int, default=30)
    parser.add_argument('--preview-only', action='store_true')
    parser.add_argument('--output', type=str)
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    rows = []
    for idx in range(args.runs):
        row = run_once(project_dir, args.symbol, args.side, execute=not args.preview_only)
        row['run_no'] = idx + 1
        rows.append(row)
        if idx < args.runs - 1 and args.interval_seconds > 0:
            time.sleep(args.interval_seconds)

    acceptance_rows = [row.get('acceptance') or {} for row in rows]
    overall_failed_checks = sorted({check for row in acceptance_rows for check in (row.get('failed_checks') or [])})
    summary = {
        'schema_version': 'testnet_smoke_acceptance_v2',
        'runs': args.runs,
        'symbol': args.symbol,
        'side': args.side,
        'preview_only': bool(args.preview_only),
        'pass_count': sum(1 for row in rows if row['ok']),
        'fail_count': sum(1 for row in rows if not row['ok']),
        'all_passed': all(row['ok'] for row in rows),
        'acceptance': {
            'passed_runs': sum(1 for row in acceptance_rows if row.get('passed')),
            'failed_runs': sum(1 for row in acceptance_rows if not row.get('passed')),
            'all_passed': all(row.get('passed') for row in acceptance_rows),
            'status': 'pass' if all(row.get('passed') for row in acceptance_rows) else 'fail',
            'overall_failed_checks': overall_failed_checks,
            'cleanup_needed_runs': [row.get('run_no') for row in rows if ((row.get('smoke_result') or {}).get('cleanup_needed'))],
            'residual_position_runs': [row.get('run_no') for row in rows if ((row.get('smoke_result') or {}).get('residual_position_detected'))],
            'runs_requiring_follow_up': [row.get('run_no') for row in rows if (row.get('acceptance') or {}).get('failure_compensation_hint')],
            'latest_reconcile_summary': (rows[-1].get('smoke_result') or {}).get('reconcile_summary') if rows else {},
        },
        'results': rows,
    }
    output = json.dumps(summary, ensure_ascii=False, indent=2)
    print(output)
    if args.output:
        Path(args.output).write_text(output, encoding='utf-8')
    return 0 if summary['all_passed'] and summary['acceptance']['all_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
