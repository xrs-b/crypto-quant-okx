#!/usr/bin/env python3
"""Minimal verification for Phase A exit arming rollout."""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import Config
from core.regime_policy import build_execution_baseline_snapshot


def main() -> int:
    cfg = Config()
    symbols = ['BTC/USDT', 'XRP/USDT', 'SOL/USDT']

    print(f'config_path={cfg.config_path}')
    print('global_exit_min_hold_seconds=', cfg.get('trading.exit_min_hold_seconds'))
    print('global_exit_arm_profit_threshold=', cfg.get('trading.exit_arm_profit_threshold'))

    for symbol in symbols:
        trading_cfg = cfg.get_symbol_section(symbol, 'trading')
        print(
            f'{symbol}: trading.exit_min_hold_seconds={trading_cfg.get("exit_min_hold_seconds")}, '
            f'trading.exit_arm_profit_threshold={trading_cfg.get("exit_arm_profit_threshold")}'
        )

    baseline_keys = sorted(build_execution_baseline_snapshot(cfg, 'XRP/USDT').keys())
    print('execution_baseline_snapshot_keys=', baseline_keys)

    if 'exit_min_hold_seconds' not in baseline_keys and 'exit_arm_profit_threshold' not in baseline_keys:
        print('note=exit arming fields are not part of execution baseline snapshot; runtime rollout should be treated as globally enabled unless executor path is expanded later.')
    else:
        print('note=exit arming fields detected in execution baseline snapshot.')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
