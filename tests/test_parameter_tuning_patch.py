import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from analytics.parameter_tuning_patch import (
    build_parameter_tuning_patch_payload,
    format_parameter_tuning_patch_text,
    format_parameter_tuning_patch_yaml,
)
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


def test_parameter_tuning_patch_builds_symbol_yaml_draft(tmp_path: Path):
    db_path = tmp_path / 'parameter_tuning_patch.db'
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

    config_path = tmp_path / 'config.yaml'
    config_path.with_name('config.local.yaml').write_text(
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

    payload = build_parameter_tuning_patch_payload(
        str(db_path),
        config_path=str(config_path),
        view='hours',
        hours=24,
        symbols=['XRP/USDT'],
    )

    xrp = payload['views']['hours']['symbols'][0]
    reviews = {item['parameter']: item for item in xrp['change_reviews']}

    assert set(reviews) == {
        'stale_signal_ttl_seconds',
        'entry_drift_tolerance_bps',
        'exit_min_hold_seconds',
        'exit_arm_profit_threshold',
    }
    assert reviews['stale_signal_ttl_seconds']['current'] == 900
    assert reviews['stale_signal_ttl_seconds']['suggested'] < reviews['stale_signal_ttl_seconds']['current']
    assert reviews['entry_drift_tolerance_bps']['current'] == 30
    assert reviews['entry_drift_tolerance_bps']['suggested'] < reviews['entry_drift_tolerance_bps']['current']
    assert reviews['exit_min_hold_seconds']['current'] == 1200
    assert reviews['exit_min_hold_seconds']['suggested'] < reviews['exit_min_hold_seconds']['current']
    assert reviews['exit_arm_profit_threshold']['current'] == 0.03
    assert reviews['exit_arm_profit_threshold']['suggested'] < reviews['exit_arm_profit_threshold']['current']

    yaml_patch = xrp['yaml_patch']
    assert yaml_patch['symbol_overrides']['XRP/USDT']['trading']['layering']['stale_signal_ttl_seconds'] == reviews['stale_signal_ttl_seconds']['suggested']
    assert yaml_patch['symbol_overrides']['XRP/USDT']['trading']['layering']['entry_drift_tolerance_bps'] == reviews['entry_drift_tolerance_bps']['suggested']
    assert yaml_patch['symbol_overrides']['XRP/USDT']['trading']['exit_min_hold_seconds'] == reviews['exit_min_hold_seconds']['suggested']
    assert yaml_patch['symbol_overrides']['XRP/USDT']['trading']['exit_arm_profit_threshold'] == reviews['exit_arm_profit_threshold']['suggested']

    rendered_text = format_parameter_tuning_patch_text(payload)
    assert 'XRP/USDT patch draft' in rendered_text
    assert 'current -> suggested' in rendered_text
    assert 'symbol_overrides.XRP/USDT.trading.layering.stale_signal_ttl_seconds' in rendered_text

    rendered_yaml = format_parameter_tuning_patch_yaml(payload)
    rendered_yaml_payload = yaml.safe_load(rendered_yaml)
    assert rendered_yaml_payload['schema_version'] == 'parameter_tuning_patch_v1'
    assert rendered_yaml_payload['views']['hours']['symbols'][0]['yaml_patch']['symbol_overrides']['XRP/USDT']['trading']['exit_min_hold_seconds'] == reviews['exit_min_hold_seconds']['suggested']
