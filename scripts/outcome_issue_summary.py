#!/usr/bin/env python3
"""Human-readable issue summary for recent XRP/SOL outcome attribution samples."""
import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from analytics.outcome_attribution_report import analyze_outcome_attribution, format_outcome_issue_summary  # noqa: E402


DEFAULT_DB_PATH = PROJECT_DIR / 'data' / 'trading.db'
DEFAULT_SYMBOLS = ['XRP/USDT', 'SOL/USDT']
DEFAULT_HOURS = 24.0
DEFAULT_LIMIT = 50


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Summarize recent outcome attribution issues for XRP/SOL without any UI.'
    )
    parser.add_argument('--db-path', default=str(DEFAULT_DB_PATH), help='SQLite db path (default: data/trading.db)')
    parser.add_argument('--view', choices=['both', 'hours', 'trades'], default='both', help='Which scope(s) to render')
    parser.add_argument('--hours', type=float, default=DEFAULT_HOURS, help='Recent N hours for the hours view (default: 24)')
    parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT, help='Latest N trades for the trades view (default: 50)')
    parser.add_argument('--symbol', dest='symbols', action='append', default=[], help='Restrict to symbol(s). Repeatable. Default: XRP/USDT + SOL/USDT')
    parser.add_argument('--fetch-limit', type=int, default=None, help='Internal fetch cap before in-memory filtering, useful for hours view')
    parser.add_argument('--json', action='store_true', help='Emit JSON payload instead of human-readable summary')
    return parser


def _build_view_report(db_path: str, *, hours: float | None, limit: int, symbols: list[str], fetch_limit: int | None):
    return analyze_outcome_attribution(
        db_path,
        limit=limit,
        hours=hours,
        symbols=symbols,
        focus_symbols=symbols,
        fetch_limit=fetch_limit,
    )


def _format_number(value: float | int) -> str:
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else str(numeric)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    symbols = args.symbols or list(DEFAULT_SYMBOLS)

    reports = {}
    if args.view in {'both', 'hours'}:
        reports['hours'] = _build_view_report(
            args.db_path,
            hours=args.hours,
            limit=max(args.limit, 1),
            symbols=symbols,
            fetch_limit=args.fetch_limit,
        )
    if args.view in {'both', 'trades'}:
        reports['trades'] = _build_view_report(
            args.db_path,
            hours=None,
            limit=args.limit,
            symbols=symbols,
            fetch_limit=args.fetch_limit,
        )

    if args.json:
        payload = {
            'symbols': symbols,
            'hours': args.hours,
            'limit': args.limit,
            'view': args.view,
            'reports': reports,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0

    blocks = []
    if 'hours' in reports:
        blocks.append(format_outcome_issue_summary(reports['hours'], title=f"Outcome issue summary — recent {_format_number(args.hours)}h"))
    if 'trades' in reports:
        blocks.append(format_outcome_issue_summary(reports['trades'], title=f'Outcome issue summary — latest {args.limit} trades'))
    print('\n\n'.join(blocks))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
