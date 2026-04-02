import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from analytics.parameter_tuning_apply_helper import (
    apply_patch_plan,
    build_apply_plan,
    build_apply_preview,
    collect_symbol_patch_draft,
)
from analytics.parameter_tuning_patch import build_parameter_tuning_patch_payload
from core.database import Database


def _seed_trade(
    db_path: str,
    *,
    symbol: str,
    close_reason_code: str,
    instant_stopout: bool = False,
    pre_arm_exit: bool = False,
    signal_age_seconds_at_entry: float = None,
    stale_signal_ttl_seconds: float = None,
    entry_drift_pct_from_signal: float = None,
    entry_drift_tolerance_bps: float = None,
    exit_guard_state: dict = None,
    close_time: datetime = None,
):
    close_time = close_time or datetime.now(timezone.utc)
    open_time = close_time - timedelta(minutes=10)
    outcome = {
        'schema_version': 'trade_outcome_attribution_v1',
        'dominant_strategy': 'RSI',
        'strategy_tags': ['RSI'],
        'regime_tag': 'range',
        'close_reason_code': close_reason_code,
        'close_reason_category': 'stop_loss' if 'stop' in close_reason_code else 'take_profit',
        'close_decision': 'loss' if 'stop' in close_reason_code else 'win',
        'instant_stopout': instant_stopout,
        'pre_arm_exit': pre_arm_exit,
        'signal_age_seconds_at_entry': signal_age_seconds_at_entry,
        'stale_signal_ttl_seconds': stale_signal_ttl_seconds,
        'entry_drift_pct_from_signal': entry_drift_pct_from_signal,
        'entry_drift_tolerance_bps': entry_drift_tolerance_bps,
        'exit_guard_state': exit_guard_state,
        'holding_seconds': 600,
        'holding_minutes': 10,
    }
    plan_context = {'strategy_tags': ['RSI'], 'regime_snapshot': {'name': 'range'}}
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO trades (
            symbol, side, entry_price, exit_price, quantity, contract_size, coin_quantity,
            leverage, pnl, pnl_percent, status, open_time, close_time, plan_context, outcome_attribution
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?, ?)
        """,
        (
            symbol,
            'long',
            100.0,
            99.0 if 'stop' in close_reason_code else 102.0,
            1.0,
            1.0,
            1.0,
            1,
            -1.0 if 'stop' in close_reason_code else 2.0,
            -1.0 if 'stop' in close_reason_code else 2.0,
            open_time.isoformat(),
            close_time.isoformat(),
            json.dumps(plan_context),
            json.dumps(outcome),
        ),
    )
    conn.commit()
    conn.close()


def _build_fixture_db(tmp_path: Path) -> Path:
    db_path = tmp_path / 'parameter_tuning_apply.db'
    Database(str(db_path))
    now = datetime.now(timezone.utc)

    for idx in range(4):
        _seed_trade(
            str(db_path),
            symbol='XRP/USDT',
            close_reason_code='stop_loss',
            instant_stopout=(idx < 2),
            pre_arm_exit=True,
            signal_age_seconds_at_entry=420 + idx * 15,
            stale_signal_ttl_seconds=300,
            entry_drift_pct_from_signal=0.95 + idx * 0.05,
            entry_drift_tolerance_bps=50,
            exit_guard_state={
                'exit_armed': False,
                'pre_arm_exit': True,
                'hold_seconds': 140 + idx * 10,
                'min_hold_seconds': 300,
                'profit_threshold': 0.02,
                'armed_by': None,
            },
            close_time=now - timedelta(hours=1, minutes=idx),
        )
    return db_path


def test_apply_helper_preview_targets_config_local_and_stays_dry_run(tmp_path: Path):
    db_path = _build_fixture_db(tmp_path)
    config_path = tmp_path / 'config.yaml'
    target_config = tmp_path / 'config.local.yaml'
    target_config.write_text(
        yaml.safe_dump(
            {
                'trading': {'exit_min_hold_seconds': 600},
                'symbol_overrides': {'XRP/USDT': {'trading': {'exit_min_hold_seconds': 1200}}},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )

    plan = build_apply_plan(
        str(db_path),
        config_path=str(config_path),
        view='hours',
        symbols=['XRP/USDT'],
    )
    assert plan['target_config_path'] == str(target_config.resolve())

    preview = build_apply_preview(plan)
    assert preview['has_changes'] is True
    assert preview['exists'] is True
    assert 'exit_min_hold_seconds' in preview['diff_text']
    assert target_config.read_text(encoding='utf-8') == preview['before_text']


def test_apply_helper_apply_writes_backup_and_updates_config_local(tmp_path: Path):
    db_path = _build_fixture_db(tmp_path)
    config_path = tmp_path / 'config.yaml'
    target_config = tmp_path / 'config.local.yaml'
    target_config.write_text(
        yaml.safe_dump(
            {
                'trading': {
                    'exit_min_hold_seconds': 600,
                    'exit_arm_profit_threshold': 0.01,
                },
                'symbol_overrides': {
                    'XRP/USDT': {
                        'trading': {
                            'exit_min_hold_seconds': 1200,
                            'exit_arm_profit_threshold': 0.03,
                        }
                    }
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )

    plan = build_apply_plan(
        str(db_path),
        config_path=str(config_path),
        view='hours',
        symbols=['XRP/USDT'],
    )
    result = apply_patch_plan(plan)

    assert result['applied'] is True
    backup_path = Path(result['backup_path'])
    assert backup_path.exists()
    assert backup_path.read_text(encoding='utf-8') == result['before_text']

    applied_yaml = yaml.safe_load(target_config.read_text(encoding='utf-8'))
    xrp_trading = applied_yaml['symbol_overrides']['XRP/USDT']['trading']
    assert xrp_trading['layering']['stale_signal_ttl_seconds'] < 900
    assert xrp_trading['layering']['entry_drift_tolerance_bps'] < 30
    assert xrp_trading['exit_min_hold_seconds'] < 1200
    assert xrp_trading['exit_arm_profit_threshold'] < 0.03


def test_apply_helper_detects_cross_scope_conflicts(tmp_path: Path):
    db_path = _build_fixture_db(tmp_path)
    config_path = tmp_path / 'config.yaml'
    config_local_path = tmp_path / 'config.local.yaml'
    config_local_path.write_text(
        yaml.safe_dump(
            {
                'symbol_overrides': {
                    'XRP/USDT': {
                        'trading': {
                            'exit_min_hold_seconds': 1200,
                            'exit_arm_profit_threshold': 0.03,
                        }
                    }
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )

    payload = build_parameter_tuning_patch_payload(
        str(db_path),
        config_path=str(config_path),
        view='both',
        symbols=['XRP/USDT'],
    )
    trades_symbol = payload['views']['trades']['symbols'][0]
    trades_symbol['yaml_patch']['symbol_overrides']['XRP/USDT']['trading']['exit_min_hold_seconds'] = 990

    selection = collect_symbol_patch_draft(payload, symbols=['XRP/USDT'])
    assert selection['conflicts']
    assert selection['conflicts'][0]['path'].endswith('exit_min_hold_seconds')
