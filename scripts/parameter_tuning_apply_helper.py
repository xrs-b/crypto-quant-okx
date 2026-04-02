#!/usr/bin/env python3
"""Preview or manually apply parameter tuning patch drafts to local config."""
import argparse
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from analytics.parameter_tuning_apply_helper import (  # noqa: E402
    apply_patch_plan,
    build_apply_plan,
    build_apply_preview,
    format_apply_preview,
)
from scripts.outcome_issue_summary import DEFAULT_DB_PATH, DEFAULT_HOURS, DEFAULT_LIMIT, DEFAULT_SYMBOLS  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Preview or manually apply advice-only parameter tuning patch drafts to config.local.yaml.'
    )
    parser.add_argument('--db-path', default=str(DEFAULT_DB_PATH), help='SQLite db path (default: data/trading.db)')
    parser.add_argument('--config-path', default=None, help='Base config path used for reading effective values')
    parser.add_argument('--target-config', default=None, help='Where to write the selected patch (default: sibling config.local.yaml)')
    parser.add_argument('--view', choices=['both', 'hours', 'trades'], default='both', help='Which scope(s) to inspect')
    parser.add_argument('--scope', dest='scopes', action='append', choices=['hours', 'trades'], default=[], help='Limit apply/preview to specific rendered scope(s). Repeatable.')
    parser.add_argument('--hours', type=float, default=DEFAULT_HOURS, help='Recent N hours for the hours view (default: 24)')
    parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT, help='Latest N trades for the trades view (default: 50)')
    parser.add_argument('--symbol', dest='symbols', action='append', default=[], help='Restrict to symbol(s). Repeatable. Default: XRP/USDT + SOL/USDT')
    parser.add_argument('--fetch-limit', type=int, default=None, help='Internal fetch cap before in-memory filtering')
    parser.add_argument('--apply', action='store_true', help='Actually write the selected patch into the target config')
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    symbols = args.symbols or list(DEFAULT_SYMBOLS)
    scopes = args.scopes or None
    plan = build_apply_plan(
        args.db_path,
        config_path=args.config_path,
        target_config_path=args.target_config,
        view=args.view,
        hours=args.hours,
        limit=args.limit,
        symbols=symbols,
        fetch_limit=args.fetch_limit,
        scopes=scopes,
    )
    selection = plan.get('selection') or {}

    if selection.get('conflicts'):
        preview = build_apply_preview(plan)
        print(format_apply_preview(plan, preview, apply=False))
        return 2

    if args.apply:
        result = apply_patch_plan(plan)
        print(format_apply_preview(plan, result, apply=True))
        return 0

    preview = build_apply_preview(plan)
    print(format_apply_preview(plan, preview, apply=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
