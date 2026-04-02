#!/usr/bin/env python3
"""Advice-only config patch draft generator for parameter tuning."""
import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from analytics.parameter_tuning_patch import (  # noqa: E402
    build_parameter_tuning_patch_payload,
    format_parameter_tuning_patch_text,
    format_parameter_tuning_patch_yaml,
)
from scripts.outcome_issue_summary import DEFAULT_DB_PATH, DEFAULT_HOURS, DEFAULT_LIMIT, DEFAULT_SYMBOLS  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Generate advice-only parameter tuning patch drafts without writing config.'
    )
    parser.add_argument('--db-path', default=str(DEFAULT_DB_PATH), help='SQLite db path (default: data/trading.db)')
    parser.add_argument('--config-path', default=None, help='Base config path passed to Config (default: project config/config.yaml + local override merge)')
    parser.add_argument('--view', choices=['both', 'hours', 'trades'], default='both', help='Which scope(s) to render')
    parser.add_argument('--hours', type=float, default=DEFAULT_HOURS, help='Recent N hours for the hours view (default: 24)')
    parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT, help='Latest N trades for the trades view (default: 50)')
    parser.add_argument('--symbol', dest='symbols', action='append', default=[], help='Restrict to symbol(s). Repeatable. Default: XRP/USDT + SOL/USDT')
    parser.add_argument('--fetch-limit', type=int, default=None, help='Internal fetch cap before in-memory filtering, useful for hours view')
    parser.add_argument('--format', choices=['text', 'yaml', 'json'], default='text', help='Output format (default: text)')
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    payload = build_parameter_tuning_patch_payload(
        args.db_path,
        config_path=args.config_path,
        view=args.view,
        hours=args.hours,
        limit=args.limit,
        symbols=args.symbols or list(DEFAULT_SYMBOLS),
        fetch_limit=args.fetch_limit,
    )
    if args.format == 'json':
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0
    if args.format == 'yaml':
        print(format_parameter_tuning_patch_yaml(payload))
        return 0
    print(format_parameter_tuning_patch_text(payload))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
