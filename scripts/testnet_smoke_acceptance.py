#!/usr/bin/env python3
"""连续执行最小 testnet smoke，并输出简要 acceptance 汇总。"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def run_once(project_dir: Path, symbol: str, side: str, execute: bool) -> dict:
    cmd = [sys.executable, 'bot/run.py', '--exchange-smoke', '--symbol', symbol, '--side', side]
    if execute:
        cmd.append('--execute')
    proc = subprocess.run(cmd, cwd=str(project_dir), capture_output=True, text=True)
    return {
        'returncode': proc.returncode,
        'stdout': proc.stdout,
        'stderr': proc.stderr,
        'ok': proc.returncode == 0,
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

    summary = {
        'schema_version': 'testnet_smoke_acceptance_v1',
        'runs': args.runs,
        'symbol': args.symbol,
        'side': args.side,
        'preview_only': bool(args.preview_only),
        'pass_count': sum(1 for row in rows if row['ok']),
        'fail_count': sum(1 for row in rows if not row['ok']),
        'all_passed': all(row['ok'] for row in rows),
        'results': rows,
    }
    output = json.dumps(summary, ensure_ascii=False, indent=2)
    print(output)
    if args.output:
        Path(args.output).write_text(output, encoding='utf-8')
    return 0 if summary['all_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
