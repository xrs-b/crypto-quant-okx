#!/usr/bin/env python3
"""CLI for observe-only MTF breakout bucket analysis."""
import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from analytics.mtf_breakout_report import analyze_mtf_breakout_report, format_mtf_breakout_report  # noqa: E402


DEFAULT_DB_PATH = PROJECT_DIR / 'data' / 'trading.db'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Analyze observe-only MTF breakout evidence buckets from signals/trades.')
    parser.add_argument('--db-path', default=str(DEFAULT_DB_PATH), help='SQLite db path (default: data/trading.db)')
    parser.add_argument('--limit', type=int, default=500, help='Analyze at most the latest N signals in scope (default: 500)')
    parser.add_argument('--hours', type=float, default=None, help='Restrict scope to the latest N hours by signal created_at')
    parser.add_argument('--symbol', dest='symbols', action='append', default=[], help='Restrict analysis to a symbol. Repeatable.')
    parser.add_argument('--fetch-limit', type=int, default=None, help='Internal fetch cap before in-memory filtering. Useful with --hours.')
    parser.add_argument('--min-outcome-samples', type=int, default=3, help='Minimum linked closed trades before rendering return distribution (default: 3)')
    parser.add_argument('--json', action='store_true', help='Print JSON instead of human-readable text')
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    report = analyze_mtf_breakout_report(
        args.db_path,
        limit=args.limit,
        hours=args.hours,
        symbols=args.symbols,
        fetch_limit=args.fetch_limit,
        min_outcome_samples=args.min_outcome_samples,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    else:
        print(format_mtf_breakout_report(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
