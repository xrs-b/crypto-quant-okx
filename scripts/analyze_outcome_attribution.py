#!/usr/bin/env python3
"""CLI for structured trade outcome attribution analysis."""
import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from analytics.outcome_attribution_report import analyze_outcome_attribution, format_outcome_attribution_report  # noqa: E402


DEFAULT_DB_PATH = PROJECT_DIR / 'data' / 'trading.db'
DEFAULT_FOCUS_SYMBOLS = ['XRP/USDT', 'SOL/USDT']


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Analyze structured outcome_attribution samples from closed trades.')
    parser.add_argument('--db-path', default=str(DEFAULT_DB_PATH), help='SQLite db path (default: data/trading.db)')
    parser.add_argument('--limit', type=int, default=200, help='Analyze at most the latest N closed trades in scope (default: 200)')
    parser.add_argument('--hours', type=float, default=None, help='Restrict scope to the latest N hours')
    parser.add_argument('--symbol', dest='symbols', action='append', default=[], help='Restrict analysis to a symbol. Repeatable.')
    parser.add_argument('--focus-symbol', dest='focus_symbols', action='append', default=[], help='Emit extra focus summary for a symbol. Repeatable.')
    parser.add_argument('--fetch-limit', type=int, default=None, help='Internal fetch cap before in-memory filtering. Useful with --hours.')
    parser.add_argument('--json', action='store_true', help='Print JSON instead of human-readable text')
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    focus_symbols = args.focus_symbols or DEFAULT_FOCUS_SYMBOLS
    report = analyze_outcome_attribution(
        args.db_path,
        limit=args.limit,
        hours=args.hours,
        symbols=args.symbols,
        focus_symbols=focus_symbols,
        fetch_limit=args.fetch_limit,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    else:
        print(format_outcome_attribution_report(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
