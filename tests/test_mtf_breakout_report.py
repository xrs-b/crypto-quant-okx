import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analytics.mtf_breakout_report import analyze_mtf_breakout_report, format_mtf_breakout_report
from core.database import Database


def _seed_signal(conn, *, symbol: str, created_at: datetime, decision: str, mtf_breakout: dict, filtered: bool = False, executed: bool = False):
    filter_details = {
        'entry_decision': {
            'decision': decision,
            'score': 72,
            'breakdown': {
                'mtf_breakout_score': mtf_breakout.get('score', 0),
                'mtf_breakout_reason': mtf_breakout.get('reason', '--'),
                'mtf_breakout_observe_only': mtf_breakout.get('observe_only', True),
            },
        },
        'observability': {
            'mtf_breakout': mtf_breakout,
        },
    }
    cur = conn.execute(
        """
        INSERT INTO signals (
            symbol, signal_type, price, strength, reasons, strategies_triggered,
            filtered, filter_reason, filter_details, executed, created_at
        ) VALUES (?, 'buy', 100.0, 70, '[]', '["MACD"]', ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            1 if filtered else 0,
            'blocked' if filtered else None,
            json.dumps(filter_details, ensure_ascii=False),
            1 if executed else 0,
            created_at.isoformat(),
        ),
    )
    return int(cur.lastrowid)


def _seed_closed_trade(conn, *, signal_id: int, symbol: str, close_time: datetime, return_pct: float, mtf_breakout: dict):
    pnl = return_pct
    outcome = {
        'schema_version': 'trade_outcome_attribution_v1',
        'close_decision': 'win' if return_pct > 0 else 'loss' if return_pct < 0 else 'flat',
        'return_pct': return_pct,
        'close_reason_code': 'take_profit' if return_pct > 0 else 'stop_loss' if return_pct < 0 else 'manual_close',
        'mtf_breakout': mtf_breakout,
    }
    plan_context = {
        'observability': {
            'mtf_breakout': mtf_breakout,
        },
    }
    conn.execute(
        """
        INSERT INTO trades (
            signal_id, symbol, side, entry_price, exit_price, quantity, contract_size, coin_quantity,
            leverage, pnl, pnl_percent, status, open_time, close_time, plan_context, outcome_attribution
        ) VALUES (?, ?, 'long', 100.0, 101.0, 1.0, 1.0, 1.0, 1, ?, ?, 'closed', ?, ?, ?, ?)
        """,
        (
            signal_id,
            symbol,
            pnl,
            return_pct,
            (close_time - timedelta(minutes=20)).isoformat(),
            close_time.isoformat(),
            json.dumps(plan_context, ensure_ascii=False),
            json.dumps(outcome, ensure_ascii=False),
        ),
    )


def test_analyze_mtf_breakout_report_buckets_and_comparison(tmp_path: Path):
    db_path = tmp_path / 'mtf_breakout.db'
    Database(str(db_path))
    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc)

    strong = {
        'schema_version': 'mtf_breakout_observability_v1',
        'enabled': True,
        'observe_only': True,
        'score': 82,
        'score_bucket': '80-100',
        'direction': 'buy',
        'state': 'eligible_breakout',
        'has_evidence': True,
        'has_breakout': True,
        'eligible': True,
        'anchor_available': True,
        'anchor_trend': 'bullish',
        'anchor_aligned': True,
        'reason': '1h 向上突破；4h 对齐',
    }
    weak = {
        'schema_version': 'mtf_breakout_observability_v1',
        'enabled': True,
        'observe_only': True,
        'score': 35,
        'score_bucket': '1-39',
        'direction': 'sell',
        'state': 'breakout_counter_anchor',
        'has_evidence': True,
        'has_breakout': True,
        'eligible': False,
        'anchor_available': True,
        'anchor_trend': 'bullish',
        'anchor_aligned': False,
        'reason': '1h 向下突破；4h 未对齐',
    }
    none = {
        'schema_version': 'mtf_breakout_observability_v1',
        'enabled': True,
        'observe_only': True,
        'score': 0,
        'score_bucket': '0',
        'direction': 'hold',
        'state': 'no_breakout',
        'has_evidence': False,
        'has_breakout': False,
        'eligible': False,
        'anchor_available': False,
        'anchor_trend': 'unknown',
        'anchor_aligned': False,
        'reason': '无额外证据',
    }

    s1 = _seed_signal(conn, symbol='BTC/USDT', created_at=now - timedelta(hours=1), decision='allow', mtf_breakout=strong, executed=True)
    s2 = _seed_signal(conn, symbol='BTC/USDT', created_at=now - timedelta(hours=2), decision='watch', mtf_breakout=weak)
    s3 = _seed_signal(conn, symbol='ETH/USDT', created_at=now - timedelta(hours=3), decision='block', mtf_breakout=none, filtered=True)

    _seed_closed_trade(conn, signal_id=s1, symbol='BTC/USDT', close_time=now - timedelta(minutes=30), return_pct=2.5, mtf_breakout=strong)
    _seed_closed_trade(conn, signal_id=s2, symbol='BTC/USDT', close_time=now - timedelta(minutes=40), return_pct=-1.2, mtf_breakout=weak)
    _seed_closed_trade(conn, signal_id=s1, symbol='BTC/USDT', close_time=now - timedelta(minutes=20), return_pct=1.5, mtf_breakout=strong)
    conn.commit()
    conn.close()

    report = analyze_mtf_breakout_report(str(db_path), limit=20, hours=24, min_outcome_samples=2)

    assert report['summary']['signal_rows_in_scope'] == 3
    assert report['summary']['signals_with_mtf_evidence'] == 2
    assert report['summary']['signals_without_mtf_evidence'] == 1
    assert report['summary']['allow_count'] == 1
    assert report['summary']['watch_count'] == 1
    assert report['summary']['block_count'] == 1

    by_state = {row['bucket']: row for row in report['by_state']}
    assert by_state['eligible_breakout']['signal_count'] == 1
    assert by_state['breakout_counter_anchor']['signal_count'] == 1
    assert by_state['no_breakout']['signal_count'] == 1

    evidence = report['comparison']['with_mtf_evidence']
    assert evidence['signal_count'] == 2
    assert evidence['closed_trade_count'] == 3
    assert evidence['close_decision_counts']['win'] == 2
    assert evidence['close_decision_counts']['loss'] == 1
    assert evidence['return_pct']['count'] == 3

    no_evidence = report['comparison']['without_mtf_evidence']
    assert no_evidence['signal_count'] == 1
    assert no_evidence['closed_trade_count'] == 0

    rendered = format_mtf_breakout_report(report)
    assert 'MTF breakout observe-only report' in rendered
    assert 'Evidence vs no evidence' in rendered
    assert 'eligible_breakout' in rendered
