import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analytics.parameter_tuning_advice import (
    build_parameter_tuning_advice_payload,
    format_parameter_tuning_advice,
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


def test_parameter_tuning_advice_builds_symbol_specific_recommendations(tmp_path: Path):
    db_path = tmp_path / 'parameter_tuning.db'
    Database(str(db_path))
    now = datetime.now(timezone.utc)

    for idx in range(4):
        _seed_trade(
            str(db_path),
            symbol='XRP/USDT',
            close_reason_code='stop_loss',
            instant_stopout=(idx < 2),
            pre_arm_exit=True,
            signal_age_seconds_at_entry=420 + idx * 10,
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

    for idx in range(4):
        _seed_trade(
            str(db_path),
            symbol='SOL/USDT',
            close_reason_code='take_profit',
            instant_stopout=False,
            pre_arm_exit=False,
            signal_age_seconds_at_entry=90 + idx * 5,
            stale_signal_ttl_seconds=300,
            entry_drift_pct_from_signal=0.10 + idx * 0.01,
            entry_drift_tolerance_bps=80,
            exit_guard_state={
                'exit_armed': True,
                'pre_arm_exit': False,
                'hold_seconds': 620 + idx * 30,
                'min_hold_seconds': 300,
                'profit_threshold': 0.01,
                'armed_by': 'hold',
            },
            close_time=now - timedelta(hours=2, minutes=idx),
        )

    payload = build_parameter_tuning_advice_payload(
        str(db_path),
        view='hours',
        hours=24,
        symbols=['XRP/USDT', 'SOL/USDT'],
    )

    xrp = payload['views']['hours']['symbols'][0]
    sol = payload['views']['hours']['symbols'][1]

    xrp_actions = {item['parameter']: item['action'] for item in xrp['recommendations']}
    assert xrp_actions['stale_signal_ttl_seconds'] == 'tighten'
    assert xrp_actions['entry_drift_tolerance_bps'] == 'tighten'
    assert xrp_actions['exit_min_hold_seconds'] == 'loosen'
    assert xrp_actions['exit_arm_profit_threshold'] == 'loosen'

    sol_actions = {item['parameter']: item['action'] for item in sol['recommendations']}
    assert sol_actions['stale_signal_ttl_seconds'] in {'hold', 'slightly_loosen_or_hold'}
    assert sol_actions['entry_drift_tolerance_bps'] in {'hold', 'slightly_loosen_or_hold'}
    assert sol_actions['exit_min_hold_seconds'] == 'hold'
    assert sol_actions['exit_arm_profit_threshold'] == 'hold'

    rendered = format_parameter_tuning_advice(payload)
    assert 'Automatic parameter tuning suggestions (advice only)' in rendered
    assert 'XRP/USDT' in rendered
    assert 'SOL/USDT' in rendered
    assert 'never edits config' in rendered
