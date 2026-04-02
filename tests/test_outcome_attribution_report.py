import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analytics.outcome_attribution_report import analyze_outcome_attribution, format_outcome_attribution_report
from core.database import Database


def _seed_trade(db_path: str, *, symbol: str, dominant_strategy: str, regime_tag: str,
                close_reason_code: str, instant_stopout: bool = False, pre_arm_exit: bool = False,
                signal_age_seconds_at_entry: float = None, stale_signal_ttl_seconds: float = None,
                entry_drift_pct_from_signal: float = None, entry_drift_tolerance_bps: float = None,
                close_time: datetime = None):
    close_time = close_time or datetime.now(timezone.utc)
    open_time = close_time - timedelta(minutes=10)
    outcome = {
        'schema_version': 'trade_outcome_attribution_v1',
        'dominant_strategy': dominant_strategy,
        'strategy_tags': [dominant_strategy],
        'regime_tag': regime_tag,
        'close_reason_code': close_reason_code,
        'close_reason_category': 'stop_loss' if 'stop' in close_reason_code else 'take_profit',
        'close_decision': 'loss' if 'stop' in close_reason_code else 'win',
        'instant_stopout': instant_stopout,
        'pre_arm_exit': pre_arm_exit,
        'signal_age_seconds_at_entry': signal_age_seconds_at_entry,
        'stale_signal_ttl_seconds': stale_signal_ttl_seconds,
        'entry_drift_pct_from_signal': entry_drift_pct_from_signal,
        'entry_drift_tolerance_bps': entry_drift_tolerance_bps,
        'holding_seconds': 600,
        'holding_minutes': 10,
    }
    plan_context = {
        'strategy_tags': [dominant_strategy],
        'regime_snapshot': {'name': regime_tag},
    }
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


def test_analyze_outcome_attribution_groups_and_breaches(tmp_path: Path):
    db_path = tmp_path / 'analysis.db'
    Database(str(db_path))
    now = datetime.now(timezone.utc)
    _seed_trade(
        str(db_path),
        symbol='XRP/USDT',
        dominant_strategy='RSI',
        regime_tag='range',
        close_reason_code='stop_loss',
        instant_stopout=True,
        pre_arm_exit=True,
        signal_age_seconds_at_entry=420,
        stale_signal_ttl_seconds=300,
        entry_drift_pct_from_signal=0.9,
        entry_drift_tolerance_bps=50,
        close_time=now - timedelta(hours=1),
    )
    _seed_trade(
        str(db_path),
        symbol='SOL/USDT',
        dominant_strategy='ML',
        regime_tag='trend',
        close_reason_code='take_profit',
        instant_stopout=False,
        pre_arm_exit=False,
        signal_age_seconds_at_entry=120,
        stale_signal_ttl_seconds=300,
        entry_drift_pct_from_signal=0.1,
        entry_drift_tolerance_bps=50,
        close_time=now - timedelta(hours=2),
    )

    report = analyze_outcome_attribution(
        str(db_path),
        limit=20,
        hours=24,
        focus_symbols=['XRP/USDT', 'SOL/USDT'],
    )

    summary = report['summary']
    assert summary['closed_rows_in_scope'] == 2
    assert summary['structured_rows_in_scope'] == 2
    assert summary['instant_stopout_count'] == 1
    assert summary['pre_arm_exit_count'] == 1
    assert summary['stale_signal_breach_count'] == 1
    assert summary['drift_breach_count'] == 1

    by_symbol = {row['group']: row for row in report['by_symbol']}
    assert by_symbol['XRP/USDT']['instant_stopout_count'] == 1
    assert by_symbol['XRP/USDT']['pre_arm_exit_count'] == 1
    assert by_symbol['XRP/USDT']['stale_signal_breach_count'] == 1
    assert by_symbol['XRP/USDT']['drift_breach_count'] == 1
    assert by_symbol['SOL/USDT']['instant_stopout_count'] == 0
    assert by_symbol['SOL/USDT']['close_reason_code_counts']['take_profit'] == 1

    by_strategy = {row['group']: row for row in report['by_strategy']}
    assert by_strategy['RSI']['trade_count'] == 1
    assert by_strategy['ML']['trade_count'] == 1

    rendered = format_outcome_attribution_report(report)
    assert 'Outcome attribution analysis report' in rendered
    assert 'XRP/USDT' in rendered
    assert 'SOL/USDT' in rendered


def test_analyze_outcome_attribution_respects_symbol_and_hour_filters(tmp_path: Path):
    db_path = tmp_path / 'analysis_filters.db'
    Database(str(db_path))
    now = datetime.now(timezone.utc)
    _seed_trade(
        str(db_path),
        symbol='XRP/USDT',
        dominant_strategy='RSI',
        regime_tag='range',
        close_reason_code='stop_loss',
        signal_age_seconds_at_entry=100,
        stale_signal_ttl_seconds=300,
        entry_drift_pct_from_signal=0.1,
        entry_drift_tolerance_bps=50,
        close_time=now - timedelta(hours=1),
    )
    _seed_trade(
        str(db_path),
        symbol='SOL/USDT',
        dominant_strategy='ML',
        regime_tag='trend',
        close_reason_code='take_profit',
        signal_age_seconds_at_entry=120,
        stale_signal_ttl_seconds=300,
        entry_drift_pct_from_signal=0.1,
        entry_drift_tolerance_bps=50,
        close_time=now - timedelta(hours=30),
    )

    report = analyze_outcome_attribution(
        str(db_path),
        limit=10,
        hours=24,
        symbols=['XRP/USDT'],
        focus_symbols=['XRP/USDT'],
    )

    assert report['summary']['closed_rows_in_scope'] == 1
    assert report['summary']['structured_rows_in_scope'] == 1
    assert [row['group'] for row in report['by_symbol']] == ['XRP/USDT']
    assert report['focus_symbols'][0]['symbol'] == 'XRP/USDT'
    assert report['focus_symbols'][0]['trade_count'] == 1
