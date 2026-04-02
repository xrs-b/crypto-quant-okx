import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from analytics.parameter_tuning_overview import build_parameter_tuning_overview_payload
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



def _seed_linked_closed_trade(conn, *, signal_id: int, symbol: str, close_time: datetime, return_pct: float, mtf_breakout: dict):
    close_reason_code = 'take_profit' if return_pct > 0 else 'stop_loss' if return_pct < 0 else 'manual_close'
    outcome = {
        'schema_version': 'trade_outcome_attribution_v1',
        'dominant_strategy': 'RSI',
        'strategy_tags': ['RSI'],
        'regime_tag': 'range',
        'close_decision': 'win' if return_pct > 0 else 'loss' if return_pct < 0 else 'flat',
        'close_reason_code': close_reason_code,
        'close_reason_category': 'take_profit' if return_pct > 0 else 'stop_loss' if return_pct < 0 else 'manual',
        'return_pct': return_pct,
        'mtf_breakout': mtf_breakout,
    }
    plan_context = {
        'strategy_tags': ['RSI'],
        'regime_snapshot': {'name': 'range'},
        'observability': {'mtf_breakout': mtf_breakout},
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
            return_pct,
            return_pct,
            (close_time - timedelta(minutes=20)).isoformat(),
            close_time.isoformat(),
            json.dumps(plan_context, ensure_ascii=False),
            json.dumps(outcome, ensure_ascii=False),
        ),
    )



def _seed_overview_fixture(db_path: Path):
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

    conn = sqlite3.connect(db_path)
    s1 = _seed_signal(conn, symbol='XRP/USDT', created_at=now - timedelta(minutes=45), decision='allow', mtf_breakout=strong, executed=True)
    s2 = _seed_signal(conn, symbol='SOL/USDT', created_at=now - timedelta(minutes=30), decision='watch', mtf_breakout=weak)
    s3 = _seed_signal(conn, symbol='XRP/USDT', created_at=now - timedelta(minutes=15), decision='block', mtf_breakout=none, filtered=True)
    _seed_linked_closed_trade(conn, signal_id=s1, symbol='XRP/USDT', close_time=now - timedelta(minutes=20), return_pct=2.2, mtf_breakout=strong)
    _seed_linked_closed_trade(conn, signal_id=s2, symbol='SOL/USDT', close_time=now - timedelta(minutes=10), return_pct=-1.1, mtf_breakout=weak)
    _seed_linked_closed_trade(conn, signal_id=s1, symbol='XRP/USDT', close_time=now - timedelta(minutes=5), return_pct=1.3, mtf_breakout=strong)
    conn.commit()
    conn.close()


def test_parameter_tuning_overview_combines_summary_advice_and_patch(tmp_path: Path):
    db_path = tmp_path / 'parameter_tuning_overview.db'
    _seed_overview_fixture(db_path)

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

    payload = build_parameter_tuning_overview_payload(
        str(db_path),
        config_path=str(config_path),
        view='hours',
        hours=24,
        symbols=['XRP/USDT', 'SOL/USDT'],
    )

    assert payload['schema_version'] == 'parameter_tuning_overview_v1'
    assert payload['mode'] == 'read_only_overview'
    assert payload['symbols'] == ['XRP/USDT', 'SOL/USDT']
    assert payload['issue_summary']['schema_version'] == 'outcome_issue_summary_payload_v1'
    assert payload['parameter_advice']['schema_version'] == 'parameter_tuning_advice_v1'
    assert payload['patch_preview']['schema_version'] == 'parameter_tuning_patch_v1'
    assert payload['mtf_breakout_summary']['summary']['signal_rows_in_scope'] == 3
    assert payload['mtf_breakout_summary']['summary']['signals_with_mtf_evidence'] == 2
    assert payload['mtf_breakout_summary']['summary']['signals_without_mtf_evidence'] == 1
    assert payload['mtf_breakout_summary']['summary']['allow_count'] == 1
    assert payload['mtf_breakout_summary']['summary']['watch_count'] == 1
    assert payload['mtf_breakout_summary']['summary']['block_count'] == 1

    rendered = payload['text']
    assert 'A. 问题摘要 / Issue summary' in rendered
    assert 'B. 参数建议 / Parameter advice' in rendered
    assert 'C. Patch 预览 / Patch preview' in rendered
    assert 'D. MTF Breakout 观察摘要 / Observe-only summary' in rendered
    assert 'MTF evidence coverage: 2/3' in rendered
    assert 'Evidence vs no evidence' in rendered
    assert 'By score bucket' in rendered
    assert 'By state' in rendered
    assert 'By 4h anchor aligned' in rendered
    assert rendered.index('A. 问题摘要 / Issue summary') < rendered.index('B. 参数建议 / Parameter advice') < rendered.index('C. Patch 预览 / Patch preview') < rendered.index('D. MTF Breakout 观察摘要 / Observe-only summary')
    assert 'never edits config' in rendered
    assert 'no config writes' in rendered


def test_parameter_tuning_overview_cli_supports_json(tmp_path: Path):
    db_path = tmp_path / 'parameter_tuning_overview_cli.db'
    _seed_overview_fixture(db_path)

    result = subprocess.run(
        [
            sys.executable,
            'scripts/parameter_tuning_overview.py',
            '--db-path',
            str(db_path),
            '--view',
            'hours',
            '--hours',
            '24',
            '--symbol',
            'XRP/USDT',
            '--symbol',
            'SOL/USDT',
            '--json',
        ],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload['schema_version'] == 'parameter_tuning_overview_v1'
    assert payload['symbols'] == ['XRP/USDT', 'SOL/USDT']
    assert 'issue_summary' in payload
    assert 'parameter_advice' in payload
    assert 'patch_preview' in payload
    assert 'mtf_breakout_summary' in payload
    assert payload['mtf_breakout_summary']['summary']['signal_rows_in_scope'] == 3
