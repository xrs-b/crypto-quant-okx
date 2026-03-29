"""
OKX量化交易系统 - 测试套件
"""
import sys
import os
import json
import sqlite3
import tempfile
import copy
from pathlib import Path
import yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import patch
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from core.config import Config
from core.database import Database
from core.regime import build_regime_snapshot, normalize_regime_snapshot
from core.regime_policy import resolve_regime_policy, build_observe_only_payload, build_risk_effective_snapshot, build_execution_effective_snapshot
from core.exchange import Exchange
from core.notifier import NotificationManager
from signals import Signal, SignalDetector, SignalValidator, SignalRecorder, EntryDecider
from trading import TradingExecutor, RiskManager
from trading.executor import build_observability_context
from analytics.backtest import StrategyBacktester, build_regime_policy_calibration_report, build_calibration_report_ready_payload, build_joint_governance_ready_payload, build_governance_workflow_ready_payload, export_calibration_payload
from analytics.helper import build_orchestration_result_digest
from strategies.strategy_library import StrategyManager
from bot.run import build_exchange_diagnostics, build_exchange_smoke_plan, build_approval_hygiene_summary, build_runtime_health_summary, maybe_run_approval_hygiene, maybe_run_adaptive_rollout_orchestration, maybe_send_daily_health_summary, execute_exchange_smoke, reconcile_exchange_positions
from dashboard.api import app
from core.risk_budget import get_risk_budget_config, compute_entry_plan, summarize_margin_usage, summarize_risk_hint_changes
from core.presets import PresetManager


class FakeExchange:
    def __init__(self, price=50000):
        self.price = price
        self.closed_orders = []

    def fetch_ticker(self, symbol):
        return {'last': self.price}

    def fetch_closed_trade_summary(self, trade, fallback_price=None):
        return None

    def close_order(self, symbol, side, amount, posSide=None):
        self.closed_orders.append({
            'symbol': symbol,
            'side': side,
            'amount': amount,
            'posSide': posSide,
        })
        return {'id': 'fake-close'}


class FakeExecutorExchange:
    def __init__(self):
        self.order_amounts = []

    def fetch_balance(self):
        return {'free': {'USDT': 1000}}

    def is_futures_symbol(self, symbol):
        return True

    def normalize_contract_amount(self, symbol, desired_notional, price):
        return 10.0

    def create_order(self, symbol, side, amount, posSide=None):
        self.order_amounts.append(amount)
        if len(self.order_amounts) == 1:
            raise Exception('okx 51202 Market order amount exceeds the maximum amount.')
        return {'id': 'fake-open'}


class CloseMismatchExchangeStub:
    def fetch_ticker(self, symbol):
        return {'last': 50000}

    def close_order(self, symbol, side, amount, posSide=None):
        raise Exception('okx 51169 Order failed because you don\'t have any positions in this direction for this contract to reduce or close.')

    def fetch_positions(self):
        return []


class RawOrderExchangeStub:
    def __init__(self):
        self.calls = []

    def create_market_buy_order(self, symbol, amount, params):
        self.calls.append({'side': 'buy', 'symbol': symbol, 'amount': amount, 'params': dict(params)})
        if len(self.calls) == 1 and params.get('posSide') == 'long':
            raise Exception('okx 51000 Parameter posSide error')
        return {'id': 'buy-ok'}

    def create_market_sell_order(self, symbol, amount, params):
        self.calls.append({'side': 'sell', 'symbol': symbol, 'amount': amount, 'params': dict(params)})
        if len(self.calls) == 1 and params.get('posSide') == 'long':
            raise Exception('okx 51169 Order failed because you don\'t have any positions in this direction for this contract to reduce or close.')
        return {'id': 'sell-ok'}


class OnewayPosSideErrorStub:
    def __init__(self):
        self.calls = []

    def create_market_buy_order(self, symbol, amount, params):
        self.calls.append({'side': 'buy', 'symbol': symbol, 'amount': amount, 'params': dict(params)})
        if len(self.calls) == 1:
            raise Exception('okx 51000 Parameter posSide error')
        return {'id': 'buy-ok'}


class DiagnosticExchangeStub:
    def fetch_balance(self):
        return {'free': {'USDT': 321.5}}

    def is_futures_symbol(self, symbol):
        return symbol == 'BTC/USDT'

    def get_order_symbol(self, symbol):
        return f'{symbol}:USDT'

    def get_contract_size(self, symbol):
        return 0.01

    def contracts_to_coin_quantity(self, symbol, contracts):
        return contracts * self.get_contract_size(symbol)

    def estimate_notional_usdt(self, symbol, contracts, price):
        return self.contracts_to_coin_quantity(symbol, contracts) * price

    def fetch_ticker(self, symbol):
        return {'last': 50000}

    def normalize_contract_amount(self, symbol, desired_notional, price):
        return round(desired_notional / price, 6)


class AmountPrecisionExchangeStub:
    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.4f}"


class AmountLimitExchangeWrapper:
    def __init__(self):
        self.exchange = AmountPrecisionExchangeStub()
        self.config = {'exchange': {'name': 'okx'}, 'trading': {'leverage': 10}}

    def get_market(self, symbol):
        return {
            'symbol': 'BTC/USDT:USDT',
            'contractSize': 0.01,
            'limits': {'amount': {'min': 1, 'max': 5}},
        }


class ExecutableExchangeStub(DiagnosticExchangeStub):
    def __init__(self):
        self.open_calls = []
        self.close_calls = []

    def create_order(self, symbol, side, amount, posSide=None):
        self.open_calls.append({'symbol': symbol, 'side': side, 'amount': amount, 'posSide': posSide})
        return {'id': 'open-ok', 'symbol': symbol, 'side': side}

    def close_order(self, symbol, side, amount, posSide=None):
        self.close_calls.append({'symbol': symbol, 'side': side, 'amount': amount, 'posSide': posSide})
        return {'id': 'close-ok', 'symbol': symbol, 'side': side}


class FakeLogDB:
    def __init__(self):
        self.logs = []
        self.outbox = []

    def log(self, level, message, details=None):
        self.logs.append({'level': level, 'message': message, 'details': details or {}})

    def enqueue_notification(self, channel, event_type, title, message, details=None):
        self.outbox.append({'id': len(self.outbox) + 1, 'channel': channel, 'event_type': event_type, 'title': title, 'message': message, 'details': details or {}, 'status': 'pending'})
        return len(self.outbox)

    def update_notification_outbox(self, notification_id, status, details=None):
        for item in self.outbox:
            if item['id'] == notification_id:
                item['status'] = status
                if details is not None:
                    item['details'] = details
                return

    def get_notification_outbox(self, status='pending', limit=50):
        rows = self.outbox
        if status != 'all':
            rows = [item for item in rows if item['status'] == status]
        return rows[:limit]


class PositionSyncExchangeStub:
    def fetch_positions(self):
        return [
            {'symbol': 'BTC/USDT:USDT', 'side': 'long', 'contracts': 2, 'entryPrice': 50000, 'markPrice': 50500, 'leverage': 10},
            {'symbol': 'XRP/USDT:USDT', 'side': 'short', 'contracts': 100, 'entryPrice': 1.5, 'markPrice': 1.45, 'leverage': 5},
        ]


class TestConfig(unittest.TestCase):
    """配置模块测试"""

    def setUp(self):
        self.config = Config()

    def test_config_load(self):
        """测试配置加载"""
        self.assertIsNotNone(self.config.all)
        self.assertIsNotNone(self.config.symbols)
        self.assertIsNotNone(self.config.strategies_config)

    def test_symbols_list(self):
        """测试币种列表"""
        symbols = self.config.symbols
        all_symbols = set(symbols)
        all_symbols.update(self.config.get('symbols.candidate_watch_list', []))
        all_symbols.update(self.config.get('symbols.paused_watch_list', []))
        self.assertGreaterEqual(len(symbols), 1)
        self.assertTrue(all('/' in symbol for symbol in symbols))
        self.assertGreaterEqual(len(all_symbols), len(symbols))

    def test_trading_params(self):
        """测试交易参数"""
        self.assertGreater(self.config.leverage, 0)
        self.assertGreater(self.config.position_size, 0)
        self.assertLessEqual(self.config.position_size, 1)
        self.assertGreater(self.config.stop_loss, 0)
        self.assertGreater(self.config.take_profit, self.config.stop_loss)

    def test_symbol_override_value_and_section(self):
        cfg = Config()
        cfg._config['strategies'] = {'composite': {'min_strength': 28, 'min_strategy_count': 1}}
        cfg._config['market_filters'] = {'min_volatility': 0.0045, 'block_counter_trend': True}
        cfg._config['symbol_overrides'] = {
            'XRP/USDT': {
                'strategies': {'composite': {'min_strength': 24}},
                'market_filters': {'min_volatility': 0.0038}
            }
        }
        self.assertEqual(cfg.get_symbol_value('XRP/USDT', 'strategies.composite.min_strength', 0), 24)
        self.assertEqual(cfg.get_symbol_value('BTC/USDT', 'strategies.composite.min_strength', 0), 28)
        section = cfg.get_symbol_section('XRP/USDT', 'market_filters')
        self.assertEqual(section['min_volatility'], 0.0038)
        self.assertTrue(section['block_counter_trend'])

    def test_position_mode(self):
        """测试持仓模式配置"""
        self.assertIn(self.config.position_mode, ['oneway', 'hedge', 'one-way', 'net', 'single'])

    def test_preset_apply_keeps_local_notification_secrets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / 'config'
            presets_dir = cfg_dir / 'presets'
            cfg_dir.mkdir(parents=True, exist_ok=True)
            presets_dir.mkdir(parents=True, exist_ok=True)

            cfg_path = cfg_dir / 'config.yaml'
            local_path = cfg_dir / 'config.local.yaml'
            preset_path = presets_dir / 'safe-mode.yaml'

            cfg_path.write_text(
                "symbols:\n"
                "  watch_list: [\"ETH/USDT\"]\n"
                "notification:\n"
                "  discord:\n"
                "    enabled: true\n",
                encoding='utf-8'
            )
            local_path.write_text(
                "notification:\n"
                "  discord:\n"
                "    channel_id: local-private-channel\n"
                "    bot_token: local-private-token\n"
                "    webhook_url: https://discord.com/api/webhooks/local/private\n",
                encoding='utf-8'
            )
            preset_path.write_text(
                "symbols:\n"
                "  watch_list: [\"BTC/USDT\"]\n"
                "notification:\n"
                "  discord:\n"
                "    enabled: true\n"
                "    notify_signals: false\n"
                "  telegram:\n"
                "    enabled: false\n",
                encoding='utf-8'
            )

            with patch('pathlib.Path.home', return_value=Path(tmpdir)):
                manager = PresetManager(Config(str(cfg_path)))
                result = manager.apply_preset('safe-mode', auto_restart=False)
                self.assertEqual(result['applied'], 'safe-mode')

                reloaded = Config(str(cfg_path))
            self.assertEqual(reloaded.get('symbols.watch_list'), ['BTC/USDT'])
            self.assertEqual(reloaded.get('notification.discord.channel_id'), 'local-private-channel')
            self.assertEqual(reloaded.get('notification.discord.bot_token'), 'local-private-token')
            self.assertEqual(reloaded.get('notification.discord.webhook_url'), 'https://discord.com/api/webhooks/local/private')
            self.assertFalse(reloaded.get('notification.discord.notify_signals'))

    def test_env_placeholder_resolution_and_local_override(self):
        old_api_key = os.environ.get('OKX_API_KEY')
        old_bot_token = os.environ.get('DISCORD_BOT_TOKEN')
        os.environ['OKX_API_KEY'] = 'env-okx-key'
        os.environ['DISCORD_BOT_TOKEN'] = 'env-discord-token'
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, 'config.yaml')
            local_path = os.path.join(tmpdir, 'config.local.yaml')
            with open(cfg_path, 'w', encoding='utf-8') as f:
                f.write(
                    "api:\n"
                    "  key: ${OKX_API_KEY}\n"
                    "  secret: ${OKX_API_SECRET:-fallback-secret}\n"
                    "notification:\n"
                    "  discord:\n"
                    "    bot_token: ${DISCORD_BOT_TOKEN:-}\n"
                    "    channel_id: public-channel\n"
                )
            with open(local_path, 'w', encoding='utf-8') as f:
                f.write(
                    "notification:\n"
                    "  discord:\n"
                    "    channel_id: local-private-channel\n"
                )
            with patch('pathlib.Path.home', return_value=Path(tmpdir)):
                cfg = Config(cfg_path)
            self.assertEqual(cfg.get('api.key'), 'env-okx-key')
            self.assertEqual(cfg.get('api.secret'), 'fallback-secret')
            self.assertEqual(cfg.get('notification.discord.bot_token'), 'env-discord-token')
            self.assertEqual(cfg.get('notification.discord.channel_id'), 'local-private-channel')
        if old_api_key is None:
            os.environ.pop('OKX_API_KEY', None)
        else:
            os.environ['OKX_API_KEY'] = old_api_key
        if old_bot_token is None:
            os.environ.pop('DISCORD_BOT_TOKEN', None)
        else:
            os.environ['DISCORD_BOT_TOKEN'] = old_bot_token

    def test_home_local_override_is_disabled_by_default(self):
        old_enable = os.environ.pop('CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL', None)
        old_path = os.environ.pop('CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG', None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                cfg_path = os.path.join(tmpdir, 'config.yaml')
                with open(cfg_path, 'w', encoding='utf-8') as f:
                    f.write(
                        "notification:\n"
                        "  discord:\n"
                        "    channel_id: project-channel\n"
                    )
                home_local_path = Path(tmpdir) / '.crypto-quant-okx.local.yaml'
                home_local_path.write_text(
                    "notification:\n"
                    "  discord:\n"
                    "    channel_id: legacy-home-channel\n",
                    encoding='utf-8'
                )
                with patch('pathlib.Path.home', return_value=Path(tmpdir)):
                    cfg = Config(cfg_path)
                self.assertEqual(cfg.get('notification.discord.channel_id'), 'project-channel')
        finally:
            if old_enable is not None:
                os.environ['CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL'] = old_enable
            if old_path is not None:
                os.environ['CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG'] = old_path

    def test_home_local_override_can_be_enabled_explicitly(self):
        old_enable = os.environ.get('CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL')
        old_path = os.environ.pop('CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG', None)
        os.environ['CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL'] = '1'
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                cfg_path = os.path.join(tmpdir, 'config.yaml')
                with open(cfg_path, 'w', encoding='utf-8') as f:
                    f.write(
                        "notification:\n"
                        "  discord:\n"
                        "    channel_id: project-channel\n"
                    )
                home_local_path = Path(tmpdir) / '.crypto-quant-okx.local.yaml'
                home_local_path.write_text(
                    "notification:\n"
                    "  discord:\n"
                    "    channel_id: legacy-home-channel\n",
                    encoding='utf-8'
                )
                with patch('pathlib.Path.home', return_value=Path(tmpdir)):
                    cfg = Config(cfg_path)
                self.assertEqual(cfg.get('notification.discord.channel_id'), 'legacy-home-channel')
        finally:
            if old_enable is None:
                os.environ.pop('CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL', None)
            else:
                os.environ['CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL'] = old_enable
            if old_path is not None:
                os.environ['CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG'] = old_path


class TestAdaptiveRegimeM0(unittest.TestCase):
    def setUp(self):
        # Disable local config override to ensure test isolation
        import os
        import shutil
        from pathlib import Path
        self._old_enable = os.environ.pop('CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL', None)
        self._old_path = os.environ.pop('CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG', None)

        # Also temporarily move local config to avoid loading it
        self._local_config_path = Path('config/config.local.yaml')
        self._backup_path = Path('config/config.local.yaml.test_backup')
        if self._local_config_path.exists():
            shutil.move(str(self._local_config_path), str(self._backup_path))

    def tearDown(self):
        import os
        import shutil
        from pathlib import Path
        # Restore local config
        if self._backup_path.exists():
            shutil.move(str(self._backup_path), str(self._local_config_path))

        if self._old_enable is not None:
            os.environ['CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL'] = self._old_enable
        if self._old_path is not None:
            os.environ['CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG'] = self._old_path

    def test_regime_snapshot_schema_helper_keeps_legacy_fields(self):
        snapshot = normalize_regime_snapshot({
            'regime': 'trend',
            'confidence': 0.72,
            'indicators': {'ema_gap': 0.02, 'ema_direction': 1, 'volatility': 0.01},
            'details': 'legacy snapshot',
        })
        self.assertEqual(snapshot['regime'], 'trend')
        self.assertEqual(snapshot['name'], 'trend')
        self.assertEqual(snapshot['family'], 'trend')
        self.assertEqual(snapshot['direction'], 'up')
        self.assertIn('stability_score', snapshot)
        self.assertIn('transition_risk', snapshot)

    def test_config_default_and_observe_only_policy_do_not_change_behavior(self):
        cfg = Config()
        regime_snapshot = build_regime_snapshot(
            regime='range',
            confidence=0.6,
            indicators={'ema_gap': 0.001, 'ema_direction': -1, 'volatility': 0.01},
            details='区间震荡',
        )
        policy = resolve_regime_policy(cfg, 'BTC/USDT', regime_snapshot)
        self.assertEqual(cfg.get_adaptive_regime_mode(), 'observe_only')
        self.assertFalse(cfg.is_adaptive_regime_enabled())
        self.assertEqual(policy['mode'], 'observe_only')
        self.assertFalse(policy['is_effective'])
        self.assertEqual(policy['effective_overrides'], {})
        self.assertEqual(policy['signal_weight_overrides'], {})
        self.assertEqual(policy['validation_overrides'], {})


class TestSignalValidator(unittest.TestCase):
    def test_validate_returns_standardized_filter_meta_for_hold_signal(self):
        cfg = Config()
        validator = SignalValidator(cfg, None)
        signal = Signal(symbol='BTC/USDT', signal_type='hold', price=50000, strength=0)
        passed, reason, details = validator.validate(signal)
        self.assertFalse(passed)
        self.assertEqual(reason, '无可执行方向')
        self.assertEqual(details['filter_meta']['code'], 'NO_DIRECTION')
        self.assertEqual(details['filter_meta']['group'], 'signal')
        self.assertTrue(details['filter_meta']['action_hint'])

    def test_validate_uses_symbol_specific_composite_override(self):
        cfg = Config()
        cfg._config.setdefault('strategies', {}).setdefault('composite', {})['min_strength'] = 28
        cfg._config.setdefault('symbol_overrides', {})['XRP/USDT'] = {
            'strategies': {'composite': {'min_strength': 20, 'min_strategy_count': 1}}
        }
        validator = SignalValidator(cfg, None)
        signal = Signal(symbol='XRP/USDT', signal_type='buy', price=1.0, strength=24, strategies_triggered=['RSI'])
        passed, reason, details = validator.validate(signal)
        self.assertTrue(passed)
        self.assertIsNone(reason)
        self.assertTrue(details['strength_check']['passed'])

    def test_validator_includes_observe_only_regime_and_policy_snapshots(self):
        cfg = Config()
        validator = SignalValidator(cfg, None)
        signal = Signal(
            symbol='BTC/USDT', signal_type='buy', price=50000, strength=50, strategies_triggered=['RSI', 'MACD'],
            regime_snapshot=build_regime_snapshot('trend', 0.8, {'ema_gap': 0.03, 'ema_direction': 1, 'volatility': 0.01}, '趋势上涨'),
            adaptive_policy_snapshot=resolve_regime_policy(cfg, 'BTC/USDT', build_regime_snapshot('trend', 0.8, {'ema_gap': 0.03, 'ema_direction': 1, 'volatility': 0.01}, '趋势上涨')),
            market_context={'trend': 'bullish', 'volatility': 0.01, 'atr_ratio': 0.01, 'volatility_too_low': False, 'volatility_too_high': False, 'regime': 'trend', 'regime_confidence': 0.8, 'regime_details': '趋势上涨'}
        )
        passed, reason, details = validator.validate(signal)
        self.assertTrue(passed)
        self.assertTrue(details['observe_only'])
        self.assertEqual(details['regime_snapshot']['regime'], 'trend')
        self.assertEqual(details['adaptive_policy_snapshot']['mode'], 'observe_only')
        self.assertIn('adaptive_regime_observe_only', details)
        self.assertTrue(details['adaptive_regime_observe_only']['summary'])
        self.assertIn('observe_only', details['adaptive_regime_observe_only']['tags'])
        self.assertIn('adaptive_risk_snapshot', details)
        self.assertIn('adaptive_risk_hints', details)

    def test_validator_step3_emits_risk_hints_without_changing_entry_plan_inputs(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'risk_hints_enabled': True,
                'enforce_conservative_only': True,
            },
            'regimes': {
                'high_vol': {
                    'risk_overrides': {
                        'base_entry_margin_ratio': 0.05,
                        'symbol_margin_cap_ratio': 0.10,
                        'leverage_cap': 5,
                        'total_margin_cap_ratio': 0.35,
                    }
                }
            }
        }
        validator = SignalValidator(cfg, None)
        signal = Signal(
            symbol='BTC/USDT', signal_type='buy', price=50000, strength=50, strategies_triggered=['RSI', 'MACD'],
            market_context={'trend': 'bullish', 'volatility': 0.06, 'atr_ratio': 0.06, 'volatility_too_low': False, 'volatility_too_high': False, 'regime': 'high_vol', 'regime_confidence': 0.8, 'regime_details': '高波动'}
        )
        passed, reason, details = validator.validate(signal)
        self.assertTrue(passed)
        self.assertEqual(details['adaptive_risk_snapshot']['effective_state'], 'hints_only')
        self.assertTrue(details['adaptive_risk_hints']['would_tighten'])
        self.assertIn('base_entry_margin_ratio', details['adaptive_risk_hints']['would_tighten_fields'])
        self.assertIn('WOULD_TIGHTEN_LEVERAGE_CAP', details['adaptive_risk_hints']['hint_codes'])
        self.assertEqual(
            details['exposure_check']['entry_plan']['risk_budget']['base_entry_margin_ratio'],
            get_risk_budget_config(cfg, 'BTC/USDT')['base_entry_margin_ratio']
        )
        self.assertTrue(any(item['key'] == 'total_margin_cap_ratio' and item['reason'] == 'non_conservative_override' for item in details['adaptive_risk_snapshot']['ignored_overrides']))
        json.dumps(details, ensure_ascii=False)

    def test_validator_step1_emits_hints_without_changing_pass_result(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'regimes': {'high_vol': {'validation_overrides': {'min_strength': 80, 'min_strategy_count': 3}}}
        }
        validator = SignalValidator(cfg, None)
        signal = Signal(
            symbol='BTC/USDT', signal_type='buy', price=50000, strength=50, strategies_triggered=['RSI', 'MACD'],
            market_context={'trend': 'bullish', 'volatility': 0.06, 'atr_ratio': 0.06, 'volatility_too_low': False, 'volatility_too_high': False, 'regime': 'high_vol', 'regime_confidence': 0.8, 'regime_details': '高波动'}
        )
        passed, reason, details = validator.validate(signal)
        self.assertTrue(passed)
        hints = details['adaptive_validation_hints']
        self.assertEqual(hints['baseline_result'], 'pass')
        self.assertEqual(hints['hinted_result'], 'block')
        self.assertTrue(hints['would_change_result'])
        self.assertIn('WOULD_RAISE_MIN_STRENGTH', hints['hint_codes'])
        self.assertIn('WOULD_RAISE_MIN_STRATEGY_COUNT', hints['hint_codes'])
        self.assertEqual(details['adaptive_validation_snapshot']['effective_state'], 'hints_only')
        self.assertTrue(details['adaptive_validation_snapshot']['observe_only'])

    def test_validator_step1_preserves_existing_block_reason_when_baseline_fails(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'regimes': {'trend': {'validation_overrides': {'min_strength': 99}}}
        }
        validator = SignalValidator(cfg, None)
        signal = Signal(symbol='BTC/USDT', signal_type='hold', price=50000, strength=0)
        passed, reason, details = validator.validate(signal)
        self.assertFalse(passed)
        self.assertEqual(reason, '无可执行方向')
        self.assertIn('adaptive_validation_snapshot', details)
        self.assertIn('adaptive_validation_hints', details)
        self.assertEqual(details['adaptive_validation_hints']['baseline_result'], 'block')

    def test_validator_step1_records_ignored_reason_and_is_json_serializable(self):
        cfg = Config()
        cfg._config['market_filters'] = {'block_counter_trend': True}
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {'rollout_symbols': ['ETH/USDT']},
            'regimes': {'trend': {'validation_overrides': {'block_counter_trend': False}}}
        }
        validator = SignalValidator(cfg, None)
        signal = Signal(
            symbol='BTC/USDT', signal_type='buy', price=50000, strength=50, strategies_triggered=['RSI', 'MACD'],
            market_context={'trend': 'bullish', 'volatility': 0.02, 'atr_ratio': 0.02, 'volatility_too_low': False, 'volatility_too_high': False, 'regime': 'trend', 'regime_confidence': 0.8, 'regime_details': '趋势'}
        )
        passed, reason, details = validator.validate(signal)
        self.assertTrue(passed)
        ignored = details['adaptive_validation_snapshot']['ignored_overrides']
        self.assertTrue(any(item['reason'] == 'non_conservative_override' for item in ignored))
        self.assertTrue(any(item['reason'] == 'rollout_symbol_not_matched' for item in ignored))
        json.dumps(details, ensure_ascii=False)

    def test_validator_step2_default_safe_does_not_enforce_effective_gate(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'regimes': {'high_vol': {'validation_overrides': {'min_strength': 80}}}
        }
        validator = SignalValidator(cfg, None)
        signal = Signal(
            symbol='BTC/USDT', signal_type='buy', price=50000, strength=50, strategies_triggered=['RSI', 'MACD'],
            market_context={'trend': 'bullish', 'volatility': 0.06, 'atr_ratio': 0.06, 'volatility_too_low': False, 'volatility_too_high': False, 'regime': 'high_vol', 'regime_confidence': 0.8, 'regime_details': '高波动'}
        )
        passed, reason, details = validator.validate(signal)
        self.assertTrue(passed)
        self.assertFalse(details['adaptive_validation_observability']['enforced'])
        self.assertTrue(details['adaptive_validation_snapshot']['observe_only'])
        self.assertEqual(details['adaptive_validation_enforcement']['summary'], 'adaptive enforcement inactive; validator stays on baseline path')

    def test_validator_step2_can_enforce_threshold_tightening_for_rollout_symbol(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'validator_enforcement_enabled': True,
                'rollout_symbols': ['BTC/USDT'],
                'validator_enforcement_categories': ['thresholds'],
            },
            'regimes': {'high_vol': {'validation_overrides': {'min_strength': 80}}}
        }
        validator = SignalValidator(cfg, None)
        signal = Signal(
            symbol='BTC/USDT', signal_type='buy', price=50000, strength=50, strategies_triggered=['RSI', 'MACD'],
            market_context={'trend': 'bullish', 'volatility': 0.06, 'atr_ratio': 0.06, 'volatility_too_low': False, 'volatility_too_high': False, 'regime': 'high_vol', 'regime_confidence': 0.8, 'regime_details': '高波动'}
        )
        passed, reason, details = validator.validate(signal)
        self.assertFalse(passed)
        self.assertEqual(reason, 'adaptive 生效后信号强度不足')
        self.assertTrue(details['adaptive_validation_observability']['enforced'])
        self.assertEqual(details['adaptive_validation_observability']['block_code'], 'ADAPTIVE_WEAK_SIGNAL_STRENGTH')
        self.assertEqual(details['adaptive_validation_enforcement']['decision'], 'block')
        self.assertTrue(any(item['key'] == 'min_strength' for item in details['adaptive_validation_enforcement']['applied']))

    def test_validator_step2_never_loosens_baseline_even_when_enforcement_enabled(self):
        cfg = Config()
        cfg._config['strategies'] = {'composite': {'min_strength': 28, 'min_strategy_count': 2}}
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'validator_enforcement_enabled': True,
                'rollout_symbols': ['BTC/USDT'],
                'validator_enforcement_categories': ['thresholds'],
            },
            'regimes': {'trend': {'validation_overrides': {'min_strength': 20, 'min_strategy_count': 1}}}
        }
        validator = SignalValidator(cfg, None)
        signal = Signal(
            symbol='BTC/USDT', signal_type='buy', price=50000, strength=24, strategies_triggered=['RSI'],
            market_context={'trend': 'bullish', 'volatility': 0.02, 'atr_ratio': 0.02, 'volatility_too_low': False, 'volatility_too_high': False, 'regime': 'trend', 'regime_confidence': 0.8, 'regime_details': '趋势'}
        )
        passed, reason, details = validator.validate(signal)
        self.assertFalse(passed)
        self.assertEqual(reason, '触发策略数不足')
        self.assertTrue(details['adaptive_validation_snapshot']['effective']['min_strength'] >= 28)
        self.assertTrue(details['adaptive_validation_snapshot']['effective']['min_strategy_count'] >= 2)
        ignored = details['adaptive_validation_snapshot']['ignored_overrides']
        self.assertTrue(any(item['key'] == 'min_strength' and item['reason'] == 'non_conservative_override' for item in ignored))
        self.assertTrue(any(item['key'] == 'min_strategy_count' and item['reason'] == 'non_conservative_override' for item in ignored))

    def test_validator_step2_rollout_symbol_miss_keeps_hints_only(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'validator_enforcement_enabled': True,
                'rollout_symbols': ['ETH/USDT'],
                'validator_enforcement_categories': ['thresholds'],
            },
            'regimes': {'high_vol': {'validation_overrides': {'min_strength': 80}}}
        }
        validator = SignalValidator(cfg, None)
        signal = Signal(
            symbol='BTC/USDT', signal_type='buy', price=50000, strength=50, strategies_triggered=['RSI', 'MACD'],
            market_context={'trend': 'bullish', 'volatility': 0.06, 'atr_ratio': 0.06, 'volatility_too_low': False, 'volatility_too_high': False, 'regime': 'high_vol', 'regime_confidence': 0.8, 'regime_details': '高波动'}
        )
        passed, reason, details = validator.validate(signal)
        self.assertTrue(passed)
        self.assertFalse(details['adaptive_validation_observability']['enforced'])
        self.assertFalse(details['adaptive_validation_snapshot']['rollout_match'])
        self.assertEqual(details['adaptive_validation_enforcement']['decision'], 'pass')

    def test_validator_step2_observability_contains_baseline_effective_applied_ignored_and_block_reason(self):
        cfg = Config()
        cfg._config['market_filters'] = {'block_high_volatility': False, 'block_low_volatility': False}
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'validator_enforcement_enabled': True,
                'rollout_symbols': ['BTC/USDT'],
                'validator_enforcement_categories': ['market_guards'],
            },
            'regimes': {'high_vol': {'validation_overrides': {'block_high_volatility': True}}}
        }
        validator = SignalValidator(cfg, None)
        signal = Signal(
            symbol='BTC/USDT', signal_type='buy', price=50000, strength=50, strategies_triggered=['RSI', 'MACD'],
            market_context={'trend': 'bullish', 'volatility': 0.08, 'atr_ratio': 0.08, 'volatility_too_low': False, 'volatility_too_high': True, 'regime': 'high_vol', 'regime_confidence': 0.8, 'regime_details': '高波动放大'}
        )
        passed, reason, details = validator.validate(signal)
        self.assertFalse(passed)
        observability = details['adaptive_validation_observability']
        enforcement = details['adaptive_validation_enforcement']
        self.assertEqual(observability['block_code'], 'ADAPTIVE_HIGH_VOLATILITY')
        self.assertEqual(observability['effective_result'], 'block')
        self.assertIn('baseline', enforcement)
        self.assertIn('effective', enforcement)
        self.assertIn('applied_overrides', enforcement)
        self.assertIn('ignored_overrides', enforcement)
        self.assertTrue(any(item['key'] == 'block_high_volatility' for item in enforcement['applied']))
        self.assertTrue(enforcement['block_reasons'])


class TestEntryDecider(unittest.TestCase):
    def _make_signal(self, **overrides):
        base = dict(
            symbol='BTC/USDT',
            signal_type='buy',
            price=50000,
            strength=72,
            strategies_triggered=['MACD', 'Volume'],
            reasons=[
                {'strategy': 'MACD', 'action': 'buy', 'strength': 30, 'confidence': 0.8},
                {'strategy': 'Volume', 'action': 'buy', 'strength': 20, 'confidence': 0.7},
            ],
            direction_score={'buy': 38.0, 'sell': 0.0, 'net': 38.0},
            market_context={'trend': 'bullish', 'volatility': 0.012, 'atr_ratio': 0.012, 'volatility_too_low': False, 'volatility_too_high': False},
            regime_info={'regime': 'trend', 'confidence': 0.7},
        )
        base.update(overrides)
        return Signal(**base)

    def test_sideways_mean_reversion_buy_gets_watch(self):
        decider = EntryDecider({})
        signal = Signal(
            symbol='XRP/USDT',
            signal_type='buy',
            price=1.48,
            strength=58,
            strategies_triggered=['RSI', 'Bollinger', 'ML'],
            reasons=[
                {'strategy': 'RSI', 'action': 'buy', 'strength': 44.8, 'confidence': 0.95},
                {'strategy': 'Bollinger', 'action': 'buy', 'strength': 16.8, 'confidence': 0.68},
                {'strategy': 'ML', 'action': 'buy', 'strength': 25.2, 'confidence': 0.71},
            ],
            direction_score={'buy': 73.0, 'sell': 0.0, 'net': 73.0},
            market_context={'trend': 'sideways', 'volatility': 0.01, 'atr_ratio': 0.01, 'volatility_too_low': False, 'volatility_too_high': False},
            regime_info={'regime': 'range', 'confidence': 0.7}
        )
        result = decider.decide(signal)
        self.assertEqual(result.decision, 'watch')
        self.assertTrue(any('横盘' in reason for reason in result.watch_reasons))

    def test_conflicted_signal_loses_allow(self):
        decider = EntryDecider({})
        signal = Signal(
            symbol='XRP/USDT',
            signal_type='buy',
            price=1.49,
            strength=59,
            strategies_triggered=['RSI', 'Bollinger', 'Volume', 'ML'],
            reasons=[
                {'strategy': 'RSI', 'action': 'buy', 'strength': 44.8, 'confidence': 0.95, 'value': 28.9},
                {'strategy': 'Bollinger', 'action': 'buy', 'strength': 16.8, 'confidence': 0.68},
                {'strategy': 'Volume', 'action': 'sell', 'strength': 28.8, 'confidence': 0.72},
                {'strategy': 'ML', 'action': 'buy', 'strength': 23.4, 'confidence': 0.66},
            ],
            direction_score={'buy': 58.0, 'sell': 20.0, 'net': 48.0},
            market_context={'trend': 'sideways', 'volatility': 0.012, 'atr_ratio': 0.012, 'volatility_too_low': False, 'volatility_too_high': False},
            regime_info={'regime': 'range', 'confidence': 0.7}
        )
        result = decider.decide(signal)
        self.assertEqual(result.decision, 'block')
        self.assertGreaterEqual(result.breakdown.signal_conflict_score, 34)

    def test_single_strategy_weak_signal_gets_blocked(self):
        decider = EntryDecider({})
        signal = Signal(
            symbol='BTC/USDT',
            signal_type='buy',
            price=84000,
            strength=20,
            strategies_triggered=['MACD'],
            reasons=[
                {'strategy': 'MACD', 'action': 'buy', 'strength': 20, 'confidence': 0.75},
            ],
            direction_score={'buy': 20.0, 'sell': 0.0, 'net': 20.0},
            market_context={'trend': 'bullish', 'volatility': 0.012, 'atr_ratio': 0.012, 'volatility_too_low': False, 'volatility_too_high': False},
            regime_info={'regime': 'trend', 'confidence': 0.7}
        )
        result = decider.decide(signal)
        self.assertEqual(result.decision, 'block')
        self.assertTrue(any('单策略弱信号' in reason for reason in result.watch_reasons))

    def test_sideways_falling_knife_buy_gets_blocked(self):
        decider = EntryDecider({})
        signal = Signal(
            symbol='XRP/USDT',
            signal_type='buy',
            price=1.48,
            strength=80,
            strategies_triggered=['RSI', 'Bollinger', 'ML'],
            reasons=[
                {'strategy': 'RSI', 'action': 'buy', 'strength': 44.8, 'confidence': 0.95, 'value': 27.0},
                {'strategy': 'Bollinger', 'action': 'buy', 'strength': 16.8, 'confidence': 0.68},
                {'strategy': 'ML', 'action': 'buy', 'strength': 30.6, 'confidence': 0.85},
            ],
            direction_score={'buy': 92.2, 'sell': 0.0, 'net': 92.2},
            market_context={'trend': 'sideways', 'volatility': 0.012, 'atr_ratio': 0.012, 'volatility_too_low': False, 'volatility_too_high': False},
            regime_info={'regime': 'range', 'confidence': 0.7}
        )
        result = decider.decide(signal)
        self.assertEqual(result.decision, 'block')
        self.assertTrue(any('接飞刀' in reason or '摸顶' in reason for reason in result.watch_reasons))

    def test_entry_decider_returns_observe_only_snapshots_without_affecting_decision_shape(self):
        decider = EntryDecider({})
        signal = self._make_signal()
        result = decider.decide(signal)
        payload = result.to_dict()
        self.assertTrue(payload['observe_only'])
        self.assertIn('regime_snapshot', payload)
        self.assertIn('adaptive_policy_snapshot', payload)
        self.assertEqual(payload['adaptive_policy_snapshot']['mode'], 'observe_only')
        self.assertEqual(payload['breakdown']['observe_only_phase'], 'observe_only')
        self.assertTrue(payload['breakdown']['observe_only_summary'])
        self.assertIn('observe_only', payload['breakdown']['observe_only_tags'])

    def test_entry_decider_observe_only_mode_does_not_apply_adaptive_overrides(self):
        baseline = EntryDecider({}).decide(self._make_signal())
        decider = EntryDecider({
            'adaptive_regime': {
                'enabled': True,
                'mode': 'observe_only',
                'defaults': {
                    'decision_overrides': {
                        'allow_score_min': 90,
                        'downgrade_allow_to_watch': True,
                    }
                }
            }
        })
        result = decider.decide(self._make_signal())
        self.assertTrue(result.observe_only)
        self.assertEqual(result.decision, baseline.decision)
        self.assertEqual(result.score, baseline.score)
        self.assertEqual(result.breakdown.adaptive_applied_overrides, [])
        self.assertFalse(result.breakdown.adaptive_policy_is_effective)

    def test_entry_decider_decision_only_mode_applies_conservative_override(self):
        decider = EntryDecider({
            'adaptive_regime': {
                'enabled': True,
                'mode': 'decision_only',
                'defaults': {
                    'decision_overrides': {
                        'allow_score_min': 80,
                        'downgrade_allow_to_watch': True,
                    }
                },
                'regimes': {
                    'trend': {
                        'decision_overrides': {
                            'allow_score_min': 82,
                        }
                    }
                }
            }
        })
        result = decider.decide(self._make_signal())
        self.assertFalse(result.observe_only)
        self.assertEqual(result.decision, 'watch')
        self.assertTrue(result.breakdown.adaptive_policy_is_effective)
        self.assertIn('allow_score_min', result.breakdown.adaptive_applied_overrides)
        self.assertIn('downgrade_allow_to_watch', result.breakdown.adaptive_applied_overrides)
        self.assertEqual(result.breakdown.adaptive_effective_thresholds['allow_score_min'], 82)
        self.assertEqual(result.adaptive_policy_snapshot['mode'], 'decision_only')
        self.assertIn('decision', result.adaptive_policy_snapshot['effective_overrides'])

    def test_entry_decider_conservative_override_never_loosens_baseline(self):
        decider = EntryDecider({
            'adaptive_regime': {
                'enabled': True,
                'mode': 'decision_only',
                'defaults': {
                    'decision_overrides': {
                        'allow_score_min': 60,
                        'block_score_max': 40,
                        'max_conflict_ratio_allow': 0.5,
                    }
                }
            }
        })
        result = decider.decide(self._make_signal())
        self.assertEqual(result.breakdown.adaptive_effective_thresholds['allow_score_min'], 68)
        self.assertEqual(result.breakdown.adaptive_effective_thresholds['block_score_max'], 35)
        self.assertEqual(result.breakdown.adaptive_effective_thresholds['max_conflict_ratio_allow'], 0.35)
        self.assertTrue(any('loosen baseline' in note for note in result.breakdown.adaptive_decision_notes))

    def test_entry_decider_output_contains_effective_override_info(self):
        decider = EntryDecider({
            'adaptive_regime': {
                'enabled': True,
                'mode': 'decision_only',
                'defaults': {
                    'decision_overrides': {
                        'allow_score_min': 81,
                    }
                }
            }
        })
        payload = decider.decide(self._make_signal()).to_dict()
        self.assertIn('adaptive_effective_overrides', payload['breakdown'])
        self.assertIn('adaptive_applied_overrides', payload['breakdown'])
        self.assertIn('adaptive_effective_thresholds', payload['breakdown'])
        self.assertEqual(payload['breakdown']['adaptive_effective_overrides']['decision']['allow_score_min'], 81)
        self.assertIn('allow_score_min', payload['breakdown']['adaptive_applied_overrides'])

    def test_entry_decider_step2_supports_finer_grained_allow_guards(self):
        decider = EntryDecider({
            'adaptive_regime': {
                'enabled': True,
                'mode': 'guarded_execute',
                'defaults': {
                    'decision_overrides': {
                        'min_signal_strength_for_allow': 90,
                        'decision_notes': ['regime prefers stronger confirmation'],
                        'decision_tags': ['adaptive:needs-stronger-signal'],
                    }
                }
            }
        })
        result = decider.decide(self._make_signal())
        self.assertEqual(result.decision, 'watch')
        self.assertIn('min_signal_strength_for_allow', result.breakdown.adaptive_applied_overrides)
        self.assertIn('decision_notes', result.breakdown.adaptive_applied_overrides)
        self.assertIn('decision_tags', result.breakdown.adaptive_applied_overrides)
        self.assertIn('regime prefers stronger confirmation', result.breakdown.adaptive_decision_notes)
        self.assertIn('adaptive:needs-stronger-signal', result.breakdown.adaptive_decision_tags)
        self.assertTrue(any('adaptive 保守阈值' in reason for reason in result.watch_reasons))

    def test_entry_decider_step2_conditional_override_can_force_block_with_reason_tag_and_note(self):
        decider = EntryDecider({
            'adaptive_regime': {
                'enabled': True,
                'mode': 'full',
                'defaults': {
                    'decision_overrides': {
                        'conditional_overrides': [
                            {
                                'metric': 'signal_conflict_score',
                                'operator': '>=',
                                'value': 20,
                                'action': 'block',
                                'reason': 'adaptive 冲突阈值命中，继续收紧',
                                'tag': 'adaptive:conflict-tighten',
                                'note': 'conflict block fired in step2',
                            }
                        ]
                    }
                }
            }
        })
        signal = self._make_signal(
            strategies_triggered=['MACD', 'Volume', 'ML'],
            reasons=[
                {'strategy': 'MACD', 'action': 'buy', 'strength': 30, 'confidence': 0.8},
                {'strategy': 'Volume', 'action': 'sell', 'strength': 18, 'confidence': 0.7},
                {'strategy': 'ML', 'action': 'buy', 'strength': 16, 'confidence': 0.7},
            ],
            direction_score={'buy': 38.0, 'sell': 10.0, 'net': 28.0},
            market_context={'trend': 'bullish', 'volatility': 0.012, 'atr_ratio': 0.012, 'volatility_too_low': False, 'volatility_too_high': False},
        )
        result = decider.decide(signal)
        self.assertEqual(result.decision, 'block')
        self.assertTrue(any('adaptive 冲突阈值命中' in reason for reason in result.watch_reasons))
        self.assertTrue(any(rule['action'] == 'block' for rule in result.breakdown.adaptive_triggered_rules))
        self.assertIn('adaptive:conflict-tighten', result.breakdown.adaptive_decision_tags)
        self.assertIn('conflict block fired in step2', result.breakdown.adaptive_decision_notes)

    def test_entry_decider_step2_records_ignored_override_reasons(self):
        decider = EntryDecider({
            'adaptive_regime': {
                'enabled': True,
                'mode': 'decision_only',
                'defaults': {
                    'decision_overrides': {
                        'allow_score_min': 60,
                        'decision_tags': 'not-a-list',
                        'conditional_overrides': [
                            {
                                'metric': 'unknown_metric',
                                'operator': '>=',
                                'value': 1,
                                'action': 'block',
                            },
                            {
                                'metric': 'signal_strength_score',
                                'operator': 'approx',
                                'value': 70,
                                'action': 'watch',
                            },
                        ]
                    }
                }
            }
        })
        result = decider.decide(self._make_signal())
        ignored = result.breakdown.adaptive_ignored_overrides
        self.assertTrue(any(item['key'] == 'allow_score_min' and 'loosen baseline' in item['reason'] for item in ignored))
        self.assertTrue(any(item['key'] == 'decision_tags' and 'not a list' in item['reason'] for item in ignored))
        self.assertTrue(any(item['key'] == 'conditional_overrides[0]' and 'metric not supported' in item['reason'] for item in ignored))
        self.assertTrue(any(item['key'] == 'conditional_overrides[1]' and 'operator not supported' in item['reason'] for item in ignored))

    def test_entry_decider_step2_output_contains_ignored_and_triggered_fields(self):
        decider = EntryDecider({
            'adaptive_regime': {
                'enabled': True,
                'mode': 'decision_only',
                'defaults': {
                    'decision_overrides': {
                        'conditional_overrides': [
                            {
                                'metric': 'signal_strength_score',
                                'operator': '<=',
                                'value': 80,
                                'action': 'watch',
                                'reason': 'adaptive 条件式观望命中',
                            }
                        ],
                        'decision_notes': ['payload fields should be visible'],
                        'decision_tags': ['adaptive:payload-check'],
                    }
                }
            }
        })
        payload = decider.decide(self._make_signal()).to_dict()
        self.assertIn('adaptive_ignored_overrides', payload['breakdown'])
        self.assertIn('adaptive_triggered_rules', payload['breakdown'])
        self.assertIn('adaptive_decision_notes', payload['breakdown'])
        self.assertIn('adaptive_decision_tags', payload['breakdown'])
        self.assertTrue(any(rule['reason'] == 'adaptive 条件式观望命中' for rule in payload['breakdown']['adaptive_triggered_rules']))
        self.assertIn('adaptive:payload-check', payload['breakdown']['adaptive_decision_tags'])

    def test_entry_decider_step3_output_contains_stable_adaptive_decision_audit(self):
        decider = EntryDecider({
            'adaptive_regime': {
                'enabled': True,
                'mode': 'full',
                'defaults': {
                    'decision_overrides': {
                        'allow_score_min': 81,
                        'decision_notes': ['step3 audit note'],
                        'decision_tags': ['adaptive:audit'],
                        'conditional_overrides': [
                            {
                                'metric': 'signal_strength_score',
                                'operator': '<=',
                                'value': 80,
                                'action': 'watch',
                                'reason': 'adaptive audit watch fired',
                            }
                        ],
                    }
                }
            }
        })
        payload = decider.decide(self._make_signal()).to_dict()['breakdown']
        audit = payload['adaptive_decision_audit']
        self.assertEqual(sorted(audit.keys()), ['applied', 'effective', 'ignored', 'triggered'])
        self.assertEqual(audit['effective']['mode'], 'full')
        self.assertTrue(audit['effective']['is_effective'])
        self.assertEqual(audit['effective']['thresholds']['allow_score_min'], 81)
        self.assertIn('decision_notes', audit['applied'])
        self.assertIn('adaptive:audit', audit['effective']['tags'])
        self.assertTrue(any(item['status'] == 'triggered' and item['stage'] == 'conditional_rule' for item in audit['triggered']))
        self.assertEqual(audit['triggered'], payload['adaptive_triggered_rules'])
        self.assertEqual(audit['applied'], payload['adaptive_applied_overrides'])

    def test_entry_decider_step3_ignored_overrides_include_stage_and_status(self):
        decider = EntryDecider({
            'adaptive_regime': {
                'enabled': True,
                'mode': 'decision_only',
                'defaults': {
                    'decision_overrides': {
                        'allow_score_min': 60,
                        'decision_tags': 'not-a-list',
                        'conditional_overrides': [
                            {
                                'metric': 'unknown_metric',
                                'operator': '>=',
                                'value': 1,
                                'action': 'block',
                            },
                        ]
                    }
                }
            }
        })
        payload = decider.decide(self._make_signal()).to_dict()['breakdown']
        ignored = payload['adaptive_decision_audit']['ignored']
        self.assertTrue(all(item['status'] == 'ignored' for item in ignored))
        self.assertTrue(any(item['key'] == 'allow_score_min' and item['stage'] == 'threshold_rule' for item in ignored))
        self.assertTrue(any(item['key'] == 'decision_tags' and item['stage'] == 'decision_metadata' for item in ignored))
        self.assertTrue(any(item['key'] == 'conditional_overrides[0]' and item['stage'] == 'conditional_rule' for item in ignored))
        self.assertEqual(ignored, payload['adaptive_ignored_overrides'])


class TestSignalValidatorRegimeFilter(unittest.TestCase):
    """Regime Layer v1 过滤测试"""

    def test_regime_risk_anomaly_blocks_signal(self):
        """风险异常应该拦截信号"""
        cfg = Config()
        validator = SignalValidator(cfg, None)

        # 模拟 risk_anomaly regime
        signal = Signal(
            symbol='BTC/USDT',
            signal_type='buy',
            price=50000,
            strength=50,
            strategies_triggered=['RSI', 'MACD'],
            market_context={
                'trend': 'sideways',
                'volatility': 0.02,
                'regime': 'risk_anomaly',
                'regime_confidence': 0.85,
                'regime_details': '价格日内波动异常(12.5%)'
            }
        )

        passed, reason, details = validator.validate(signal)
        self.assertFalse(passed)
        self.assertIn('风险异常', reason)
        self.assertEqual(details.get('regime_check', {}).get('regime'), 'risk_anomaly')

    def test_regime_low_vol_blocks_by_default(self):
        """低波动默认应该拦截"""
        cfg = Config()
        validator = SignalValidator(cfg, None)

        signal = Signal(
            symbol='BTC/USDT',
            signal_type='buy',
            price=50000,
            strength=50,
            strategies_triggered=['RSI', 'MACD'],
            market_context={
                'trend': 'sideways',
                'volatility': 0.005,
                'regime': 'low_vol',
                'regime_confidence': 0.7,
                'regime_details': '低波动盘整(vol=0.50%)'
            }
        )

        passed, reason, details = validator.validate(signal)
        self.assertFalse(passed)
        self.assertIn('低波动', reason)

    def test_regime_high_vol_passes_by_default(self):
        """高波动默认放行（但带警告）"""
        cfg = Config()
        validator = SignalValidator(cfg, None)

        signal = Signal(
            symbol='BTC/USDT',
            signal_type='buy',
            price=50000,
            strength=50,
            strategies_triggered=['RSI', 'MACD'],
            market_context={
                'trend': 'bullish',
                'volatility': 0.06,
                'regime': 'high_vol',
                'regime_confidence': 0.7,
                'regime_details': '高波动趋势中(上涨, vol=6.5%)'
            }
        )

        passed, reason, details = validator.validate(signal)
        self.assertTrue(passed)  # 默认放行
        self.assertEqual(details.get('regime_check', {}).get('regime'), 'high_vol')

    def test_regime_unknown_falls_back(self):
        """regime 未知时回退旧逻辑"""
        cfg = Config()
        validator = SignalValidator(cfg, None)

        # regime unknown 或 confidence 低于 0.5 应该跳过过滤
        signal = Signal(
            symbol='BTC/USDT',
            signal_type='buy',
            price=50000,
            strength=50,
            strategies_triggered=['RSI'],
            market_context={
                'trend': 'sideways',
                'volatility': 0.02,
                'regime': 'unknown',
                'regime_confidence': 0.0
            }
        )

        passed, reason, details = validator.validate(signal)
        # 应该跳过 regime 过滤，回退到旧逻辑
        self.assertTrue(details.get('regime_check', {}).get('fallback', False))

    def test_regime_trend_passes(self):
        """趋势市场应该放行"""
        cfg = Config()
        validator = SignalValidator(cfg, None)

        signal = Signal(
            symbol='BTC/USDT',
            signal_type='buy',
            price=50000,
            strength=50,
            strategies_triggered=['MACD', 'RSI'],
            market_context={
                'trend': 'bullish',
                'volatility': 0.025,
                'regime': 'trend',
                'regime_confidence': 0.75,
                'regime_details': '趋势上涨(gap=2.5%)'
            }
        )

        passed, reason, details = validator.validate(signal)
        self.assertTrue(passed)
        self.assertEqual(details.get('regime_check', {}).get('regime'), 'trend')


class TestExchangeAmountLimits(unittest.TestCase):
    def test_normalize_contract_amount_respects_max_limit(self):
        ex = Exchange.__new__(Exchange)
        ex.exchange = AmountPrecisionExchangeStub()
        ex.get_market = lambda symbol: {
            'symbol': 'BTC/USDT:USDT',
            'contractSize': 0.01,
            'limits': {'amount': {'min': 1, 'max': 5}},
        }
        amount = ex.normalize_contract_amount('BTC/USDT', desired_notional_usdt=10000, price=50000)
        self.assertEqual(amount, 5.0)


class TestDatabase(unittest.TestCase):
    """数据库模块测试"""

    def setUp(self):
        self.db = Database('data/test_bot.db')

    def tearDown(self):
        import os
        if os.path.exists('data/test_bot.db'):
            os.remove('data/test_bot.db')

    def test_signal_record(self):
        """测试信号记录"""
        signal_id = self.db.record_signal(
            symbol='BTC/USDT',
            signal_type='buy',
            price=50000,
            strength=75,
            reasons=[{'strategy': 'RSI', 'action': 'buy', 'value': 30}],
            strategies_triggered=['RSI', 'MACD']
        )
        self.assertIsNotNone(signal_id)
        self.assertGreater(signal_id, 0)

    def test_signal_query(self):
        """测试信号查询"""
        # 记录信号
        self.db.record_signal(
            symbol='BTC/USDT',
            signal_type='buy',
            price=50000,
            strength=75,
            reasons=[],
            strategies_triggered=['RSI']
        )

        # 查询
        signals = self.db.get_signals(limit=10)
        self.assertGreater(len(signals), 0)

    def test_signal_query_parses_structured_filter_fields(self):
        signal_id = self.db.record_signal(
            symbol='BTC/USDT',
            signal_type='hold',
            price=50000,
            strength=12,
            reasons=[],
            strategies_triggered=[]
        )
        self.db.update_signal(
            signal_id,
            filtered=1,
            filter_reason='无可执行方向',
            filter_code='NO_DIRECTION',
            filter_group='signal',
            action_hint='继续观察方向分数',
            filter_details=json.dumps({'filter_meta': {'code': 'NO_DIRECTION'}}, ensure_ascii=False)
        )
        signals = self.db.get_signals(limit=10)
        row = next(s for s in signals if s['id'] == signal_id)
        self.assertEqual(row['filter_code'], 'NO_DIRECTION')
        self.assertEqual(row['filter_group'], 'signal')
        self.assertEqual(row['action_hint'], '继续观察方向分数')
        self.assertEqual(row['filter_details']['filter_meta']['code'], 'NO_DIRECTION')

    def test_signal_mark_executed(self):
        """测试信号可标记为已执行并关联 trade_id"""
        signal_id = self.db.record_signal(
            symbol='BTC/USDT',
            signal_type='buy',
            price=50000,
            strength=75,
            reasons=[],
            strategies_triggered=['RSI']
        )
        self.db.update_signal(signal_id, executed=1, trade_id=99)
        signals = self.db.get_signals(limit=10)
        row = next(s for s in signals if s['id'] == signal_id)
        self.assertTrue(row['executed'])
        self.assertEqual(row['trade_id'], 99)

    def test_trade_record(self):
        """测试交易记录"""
        trade_id = self.db.record_trade(
            symbol='BTC/USDT',
            side='long',
            entry_price=50000,
            quantity=0.1,
            leverage=10
        )
        self.assertIsNotNone(trade_id)

    def test_get_latest_open_trade(self):
        """测试获取最新未平仓交易"""
        self.db.record_trade(
            symbol='BTC/USDT',
            side='long',
            entry_price=50000,
            quantity=0.1,
            leverage=10
        )
        latest_trade_id = self.db.record_trade(
            symbol='BTC/USDT',
            side='long',
            entry_price=51000,
            quantity=0.2,
            leverage=10
        )

        trade = self.db.get_latest_open_trade('BTC/USDT', 'long')
        self.assertIsNotNone(trade)
        self.assertEqual(trade['id'], latest_trade_id)

    def test_mark_trade_stale_closed(self):
        trade_id = self.db.record_trade(
            symbol='BTC/USDT',
            side='long',
            entry_price=50000,
            quantity=0.1,
            leverage=10
        )
        changed = self.db.mark_trade_stale_closed(trade_id, '交易所无仓', close_price=49999)
        self.assertTrue(changed)
        trade = self.db.get_trades(symbol='BTC/USDT', limit=5)[0]
        self.assertEqual(trade['status'], 'closed')
        self.assertEqual(trade['exit_price'], 49999)
        self.assertIn('自动收口', trade.get('notes') or '')

    def test_get_latest_trade_time(self):
        self.assertIsNone(self.db.get_latest_trade_time('BTC/USDT'))
        self.db.record_trade(symbol='BTC/USDT', side='long', entry_price=50000, quantity=0.1, leverage=10)
        latest = self.db.get_latest_trade_time('BTC/USDT')
        self.assertIsNotNone(latest)
        self.assertLess((datetime.utcnow() - latest).total_seconds(), 120)

    def test_position_update(self):
        """测试持仓更新"""
        self.db.update_position(
            symbol='BTC/USDT',
            side='long',
            entry_price=50000,
            quantity=0.1,
            leverage=10,
            current_price=51000
        )

        positions = self.db.get_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]['symbol'], 'BTC/USDT')

    def test_strategy_analysis(self):
        """测试策略分析记录"""
        signal_id = self.db.record_signal(
            symbol='ETH/USDT',
            signal_type='buy',
            price=3000,
            strength=80,
            reasons=[],
            strategies_triggered=['RSI']
        )

        self.db.record_strategy_analysis(
            signal_id=signal_id,
            strategy_name='RSI',
            triggered=True,
            strength=30,
            confidence=0.8,
            action='buy',
            details='RSI=28'
        )

        stats = self.db.get_strategy_stats(days=30)
        self.assertGreater(len(stats), 0)


class TestSignalDetector(unittest.TestCase):
    """信号检测器测试"""

    def setUp(self):
        self.config = Config()
        self.detector = SignalDetector(self.config.all)
        self.df = self._create_test_data()

    def _create_test_data(self):
        """创建测试数据"""
        dates = pd.date_range('2024-01-01', periods=50, freq='1h')
        np.random.seed(42)
        close = 50000 + np.random.randn(50).cumsum() * 100

        df = pd.DataFrame({
            0: dates,
            1: close + np.random.rand(50) * 100,
            2: close + 200 + np.random.rand(50) * 100,
            3: close - 200 - np.random.rand(50) * 100,
            4: close,
            5: np.random.randint(1000, 10000, 50)
        })

        # 添加指标
        delta = pd.Series(close).diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(14).mean()
        avg_loss = loss.rolling(14).mean()
        rs = avg_gain / (avg_loss + 1e-10)
        df['RSI'] = 100 - (100 / (1 + rs))

        ema12 = pd.Series(close).ewm(span=12).mean()
        ema26 = pd.Series(close).ewm(span=26).mean()
        df['MACD'] = ema12 - ema26
        df['MACD_signal'] = df['MACD'].ewm(span=9).mean()

        df['BB_mid'] = pd.Series(close).rolling(20).mean()
        std = pd.Series(close).rolling(20).std()
        df['BB_upper'] = df['BB_mid'] + 2 * std
        df['BB_lower'] = df['BB_mid'] - 2 * std

        return df

    def test_signal_analysis(self):
        """测试信号分析"""
        current_price = self.df[4].iloc[-1]
        signal = self.detector.analyze('BTC/USDT', self.df, current_price, None)

        self.assertIsNotNone(signal)
        self.assertIn(signal.signal_type, ['buy', 'sell', 'hold'])
        self.assertGreaterEqual(signal.strength, 0)
        self.assertLessEqual(signal.strength, 100)

    def test_indicators_captured(self):
        """测试指标捕获"""
        current_price = self.df[4].iloc[-1]
        signal = self.detector.analyze('BTC/USDT', self.df, current_price, None)

        self.assertIn('RSI', signal.indicators)
        self.assertIn('MACD', signal.indicators)

    def test_symbol_override_affects_detector_thresholds(self):
        cfg = Config()
        cfg._config.setdefault('strategies', {}).setdefault('composite', {}).update({'min_strength': 28, 'min_strategy_count': 1})
        cfg._config.setdefault('market_filters', {}).update({'min_volatility': 0.0045, 'max_volatility': 0.05})
        cfg._config.setdefault('symbol_overrides', {})['XRP/USDT'] = {
            'strategies': {'composite': {'min_strength': 20}},
            'market_filters': {'min_volatility': 0.1}
        }
        detector = SignalDetector(cfg.all)
        current_price = self.df[4].iloc[-1]
        signal = detector.analyze('XRP/USDT', self.df, current_price, None)
        self.assertLess(signal.market_context['volatility'], 0.1)
        self.assertTrue(signal.market_context['volatility_too_low'])

    def test_regime_info_populated(self):
        """测试 regime 信息被正确填充"""
        detector = SignalDetector(self.config.all)
        current_price = self.df[4].iloc[-1]
        signal = detector.analyze('BTC/USDT', self.df, current_price, None)

        # 验证 regime_info 字段存在
        self.assertIn('regime', signal.regime_info)
        self.assertIn('confidence', signal.regime_info)
        self.assertIn('regime', signal.market_context)
        self.assertIn('regime_confidence', signal.market_context)
        # regime 应该是有效值之一
        valid_regimes = ['trend', 'range', 'high_vol', 'low_vol', 'risk_anomaly', 'unknown']
        self.assertIn(signal.market_context['regime'], valid_regimes)

    def test_detector_attaches_unified_observe_only_snapshots(self):
        current_price = self.df[4].iloc[-1]
        signal = self.detector.analyze('BTC/USDT', self.df, current_price, None)
        self.assertEqual(signal.regime_snapshot['regime'], signal.regime_info['regime'])
        self.assertEqual(signal.market_context['regime_snapshot']['regime'], signal.regime_snapshot['regime'])
        self.assertEqual(signal.market_context['adaptive_policy_snapshot']['mode'], 'observe_only')
        self.assertFalse(signal.adaptive_policy_snapshot['is_effective'])


class TestStrategies(unittest.TestCase):
    """策略测试"""

    def setUp(self):
        self.config = Config()
        self.manager = StrategyManager(self.config.all)

    def test_strategy_count(self):
        """测试策略数量"""
        strategies = self.manager.get_enabled_strategies()
        self.assertGreaterEqual(len(strategies), 6)

    def test_all_strategies(self):
        """测试所有策略"""
        names = self.manager.get_strategy_names()
        expected = ['RSI', 'MACD', 'MA_Cross', 'Bollinger', 'Volume', 'Pattern',
                   'TrendStrength', 'Divergence']

        for name in expected:
            self.assertIn(name, names)


class TestExchange(unittest.TestCase):
    """交易所适配容错测试"""

    def test_create_order_fallback_without_posside(self):
        ex = Exchange.__new__(Exchange)
        ex.config = {'exchange': {'position_mode': 'hedge'}}
        ex.exchange = RawOrderExchangeStub()
        ex.get_order_symbol = lambda symbol: 'BTC/USDT:USDT'

        result = ex.create_order('BTC/USDT', 'buy', 1.5, posSide='long')
        self.assertEqual(result['id'], 'buy-ok')
        self.assertEqual(len(ex.exchange.calls), 2)
        self.assertIn('posSide', ex.exchange.calls[0]['params'])
        self.assertNotIn('posSide', ex.exchange.calls[1]['params'])

    def test_close_order_fallback_without_posside_keeps_reduce_only(self):
        ex = Exchange.__new__(Exchange)
        ex.config = {'exchange': {'position_mode': 'hedge'}}
        ex.exchange = RawOrderExchangeStub()
        ex.get_order_symbol = lambda symbol: 'BTC/USDT:USDT'

        result = ex.close_order('BTC/USDT', 'sell', 1.5, posSide='long')
        self.assertEqual(result['id'], 'sell-ok')
        self.assertEqual(len(ex.exchange.calls), 2)
        self.assertTrue(ex.exchange.calls[0]['params'].get('reduceOnly'))
        self.assertTrue(ex.exchange.calls[1]['params'].get('reduceOnly'))
        self.assertNotIn('posSide', ex.exchange.calls[1]['params'])

    def test_oneway_mode_omits_posside_from_start(self):
        ex = Exchange.__new__(Exchange)
        ex.config = {'exchange': {'position_mode': 'oneway'}}
        ex.exchange = RawOrderExchangeStub()
        ex.get_order_symbol = lambda symbol: 'BTC/USDT:USDT'

        result = ex.create_order('BTC/USDT', 'buy', 1.5, posSide='long')
        self.assertEqual(result['id'], 'buy-ok')
        self.assertEqual(len(ex.exchange.calls), 1)
        self.assertNotIn('posSide', ex.exchange.calls[0]['params'])

    def test_oneway_mode_retries_with_minimal_params_on_51000(self):
        ex = Exchange.__new__(Exchange)
        ex.config = {'exchange': {'position_mode': 'oneway'}}
        ex.exchange = OnewayPosSideErrorStub()
        ex.get_order_symbol = lambda symbol: 'BTC/USDT:USDT'

        result = ex.create_order('BTC/USDT', 'buy', 1.5, posSide='long')
        self.assertEqual(result['id'], 'buy-ok')
        self.assertEqual(len(ex.exchange.calls), 2)
        self.assertEqual(ex.exchange.calls[1]['params'], {'tdMode': 'isolated'})


class TestNotifications(unittest.TestCase):
    def test_notify_signal_and_decision_store_logs(self):
        cfg = Config()
        cfg._config.setdefault('notification', {}).setdefault('discord', {})
        cfg._config['notification']['discord'].update({'enabled': False, 'bot_token': '', 'channel_id': '', 'webhook_url': ''})
        db = FakeLogDB()
        notifier = NotificationManager(cfg, db, None)
        from signals.detector import Signal
        signal = Signal(
            symbol='BTC/USDT', signal_type='buy', price=50000, strength=88, strategies_triggered=['RSI', 'MACD'],
            regime_snapshot=build_regime_snapshot('trend', 0.82, {'ema_gap': 0.03, 'ema_direction': 1, 'volatility': 0.01}, '趋势上涨'),
            adaptive_policy_snapshot=resolve_regime_policy(cfg, 'BTC/USDT', build_regime_snapshot('trend', 0.82, {'ema_gap': 0.03, 'ema_direction': 1, 'volatility': 0.01}, '趋势上涨')),
        )
        notifier.notify_signal(signal, True, None, {'passed': True})
        notifier.notify_decision(signal, False, '风险拒绝', {'risk_gate': {'passed': False, 'reason': '风险拒绝'}, 'adaptive_policy_snapshot': signal.adaptive_policy_snapshot, 'regime_snapshot': signal.regime_snapshot})
        notifier.notify_trade_open('XRP/USDT', 'long', 1.48, 61040, trade_id=99, signal=signal, quantity_details={'contracts': 61040, 'contract_size': 0.01, 'coin_quantity': 610.4, 'notional_usdt': 903.39})
        notifier.notify_trade_open_failed('BTC/USDT', 'long', 50000, '交易所拒绝', signal, {'code': 'mock'})
        notifier.notify_trade_close_failed('BTC/USDT', 'long', '平仓失败', {'code': 'mock'})
        notifier.notify_reconcile_issue({'summary': {'exchange_positions': 2, 'local_positions': 1, 'open_trades': 1, 'exchange_missing_local_position': 1, 'local_position_missing_exchange': 0, 'open_trade_missing_exchange': 0, 'exchange_missing_open_trade': 1}})
        runtime = notifier.notify_runtime('skip', ['币种：BTC/USDT', '原因：测试跳过'])
        duplicate_runtime = notifier.notify_runtime('skip', ['币种：BTC/USDT', '原因：测试跳过'])
        probe = notifier.test_discord()
        self.assertEqual(len(db.logs), 9)
        self.assertGreaterEqual(len(db.outbox), 9)
        self.assertEqual(db.outbox[0]['channel'], 'discord')
        self.assertEqual(runtime['outbox_status'], 'disabled')
        self.assertEqual(duplicate_runtime['outbox_status'], 'disabled')
        self.assertIn('notify:signal', db.logs[0]['message'])
        self.assertIn('notify:runtime', db.logs[6]['message'])
        self.assertIn('【信号概览】', db.logs[0]['details']['message'])
        self.assertIn('下单张数：61,040', db.logs[2]['details']['message'])
        self.assertIn('折算数量：610.4 XRP', db.logs[2]['details']['message'])
        self.assertIn('通知等级：⚠️ 重要', db.logs[0]['details']['message'])
        self.assertIn('--------------------------------------------------------------', db.logs[0]['details']['message'])
        self.assertIn('【Adaptive Regime（Observe-only）】', db.logs[0]['details']['message'])
        self.assertIn('只增强观察与汇总展示，不改变真实交易执行', db.logs[0]['details']['message'])
        self.assertIn('【风控拦截】', db.logs[1]['details']['message'])
        self.assertIn('通知等级：🚨 紧急', db.logs[1]['details']['message'])
        self.assertIn('风险拒绝', db.logs[1]['details']['message'])
        self.assertFalse(runtime['enabled'])
        self.assertTrue(duplicate_runtime['suppressed'])
        self.assertFalse(probe['delivered'])
        self.assertEqual(probe['outbox_status'], 'disabled')

    def test_discord_bot_fallback_channel(self):
        cfg = Config()
        cfg._config.setdefault('notification', {}).setdefault('discord', {})
        cfg._config['notification']['discord'].update({'enabled': True, 'webhook_url': '', 'bot_token': 'x', 'channel_id': '123'})
        db = FakeLogDB()
        notifier = NotificationManager(cfg, db, None)
        notifier._send_discord_bot = lambda content: True
        notifier._send_discord_webhook = lambda content: False
        result = notifier.send('decision', '测试', ['bot fallback'])
        self.assertTrue(result['enabled'])
        self.assertTrue(result['delivered'])
        self.assertEqual(result['outbox_status'], 'delivered')
        self.assertEqual(result['priority'], 'normal')
        self.assertEqual(db.outbox[-1]['status'], 'delivered')
        self.assertEqual(db.outbox[-1]['details']['delivery']['path'], 'direct')

    def test_notify_test_bypasses_category_switch_and_reports_direct_delivery(self):
        cfg = Config()
        cfg._config.setdefault('notification', {}).setdefault('discord', {})
        cfg._config['notification']['discord'].update({
            'enabled': True,
            'notify_trades': False,
            'webhook_url': '',
            'bot_token': 'x',
            'channel_id': '123'
        })
        db = FakeLogDB()
        notifier = NotificationManager(cfg, db, None)
        notifier._send_discord = lambda content: True
        result = notifier.test_discord()
        self.assertTrue(result['enabled'])
        self.assertTrue(result['delivered'])
        self.assertEqual(result['outbox_status'], 'delivered')
        self.assertEqual(db.outbox[-1]['status'], 'delivered')
        self.assertEqual(db.outbox[-1]['details']['event_type'], 'notify-test')
        self.assertEqual(db.outbox[-1]['details']['delivery']['path'], 'direct')

    def test_notification_keeps_pending_when_direct_send_fails(self):
        cfg = Config()
        cfg._config.setdefault('notification', {}).setdefault('discord', {})
        cfg._config['notification']['discord'].update({'enabled': True, 'webhook_url': '', 'bot_token': 'x', 'channel_id': '123'})
        db = FakeLogDB()
        notifier = NotificationManager(cfg, db, None)
        notifier._send_discord_bot = lambda content: False
        notifier._send_discord_webhook = lambda content: False
        result = notifier.send('error', '失败测试', ['等待 bridge 兜底'])
        self.assertTrue(result['enabled'])
        self.assertFalse(result['delivered'])
        self.assertEqual(result['outbox_status'], 'pending')
        self.assertEqual(db.outbox[-1]['status'], 'pending')
        self.assertEqual(db.outbox[-1]['details']['delivery']['path'], 'bridge_pending')

    def test_relay_pending_outbox_delivers_messages(self):
        cfg = Config()
        cfg._config.setdefault('notification', {}).setdefault('discord', {})
        cfg._config['notification']['discord'].update({'enabled': True, 'webhook_url': '', 'bot_token': 'x', 'channel_id': '123'})
        db = FakeLogDB()
        notifier = NotificationManager(cfg, db, None)
        notifier._send_discord_bot = lambda content: False
        notifier._send_discord_webhook = lambda content: False
        notifier.send('error', '失败测试', ['等待 relay'])
        notifier._send_discord = lambda content: True
        relay = notifier.relay_pending_outbox(limit=10)
        self.assertEqual(relay['scanned'], 1)
        self.assertEqual(relay['delivered'], 1)
        self.assertEqual(db.outbox[-1]['status'], 'delivered')
        self.assertTrue(db.outbox[-1]['details']['delivery']['relay_attempted'])
        self.assertEqual(db.outbox[-1]['details']['delivery']['path'], 'relay')

    def test_duplicate_notifications_get_aggregate_summary(self):
        cfg = Config()
        cfg._config.setdefault('notification', {}).setdefault('discord', {})
        cfg._config['notification']['discord'].update({'enabled': False})
        db = FakeLogDB()
        notifier = NotificationManager(cfg, db, None)
        first = notifier.send('runtime', '测试聚合', ['同类消息'])
        second = notifier.send('runtime', '测试聚合', ['同类消息'])
        key = next(iter(notifier._recent_messages.keys()))
        notifier._recent_messages[key] = notifier._recent_messages[key] - 301
        third = notifier.send('runtime', '测试聚合', ['同类消息'])
        self.assertFalse(first['suppressed'])
        self.assertTrue(second['suppressed'])
        self.assertEqual(second['aggregate_summary'], None)
        self.assertEqual(third['aggregate_summary'], '最近已合并 1 次同类通知（约 300s 内）')
        self.assertIn('【聚合摘要】', third['message'])

    def test_notify_loss_streak_lock_with_buttons(self):
        """测试熔断通知支持 Discord 按钮 (MVP: link button to dashboard)"""
        cfg = Config()
        cfg._config.setdefault('notification', {}).setdefault('discord', {})
        cfg._config['notification']['discord'].update({
            'enabled': True,
            'webhook_url': '',
            'bot_token': 'fake_token',
            'channel_id': '123456'
        })
        cfg._config.setdefault('dashboard', {}).update({
            'host': '0.0.0.0',
            'port': 5555
        })
        db = FakeLogDB()
        notifier = NotificationManager(cfg, db, None)
        # Mock the bot send to capture components
        captured_components = []
        original_send = notifier._send_discord_bot
        notifier._send_discord_bot = lambda content, components=None: (captured_components.append(components) or True)

        result = notifier.notify_loss_streak_lock(
            current=3,
            max_count=3,
            recover_at='2024-01-01T12:00:00',
            details={'test': 'data'}
        )

        self.assertTrue(result['enabled'])
        self.assertIn('连亏熔断已触发', result['message'])
        self.assertIn('连续亏损：3/3', result['message'])
        # Verify components were passed (link button to dashboard)
        self.assertIsNotNone(captured_components[0])
        self.assertEqual(captured_components[0][0]['type'], 1)  # ACTION_ROW
        self.assertEqual(captured_components[0][0]['components'][0]['type'], 2)  # BUTTON
        self.assertEqual(captured_components[0][0]['components'][0]['style'], 5)  # LINK style
        self.assertIn('Dashboard', captured_components[0][0]['components'][0]['label'])

        # Restore original
        notifier._send_discord_bot = original_send

    def test_notify_loss_streak_lock_includes_approval_actions_metadata(self):
        """测试熔断通知包含 approval_actions 元数据，供 OpenClaw bridge 解析"""
        cfg = Config()
        cfg._config.setdefault('notification', {}).setdefault('discord', {})
        cfg._config['notification']['discord'].update({
            'enabled': True,
            'webhook_url': 'https://fakewebhook',
            'bot_token': '',
            'channel_id': ''
        })
        cfg._config.setdefault('dashboard', {}).update({
            'host': '0.0.0.0',
            'port': 5555
        })
        db = FakeLogDB()
        notifier = NotificationManager(cfg, db, None)

        result = notifier.notify_loss_streak_lock(
            current=3,
            max_count=3,
            recover_at='2024-01-01T12:00:00',
            details={'test': 'data'}
        )

        # Verify approval_actions is in outbox details
        self.assertTrue(len(db.outbox) > 0)
        outbox_item = db.outbox[-1]
        self.assertIn('approval_actions', outbox_item.get('details', {}))

        approval_actions = outbox_item['details']['approval_actions']
        self.assertEqual(approval_actions['type'], 'loss_streak_reset')
        self.assertEqual(approval_actions['endpoint'], '/api/risk/loss-streak/reset')
        self.assertEqual(approval_actions['method'], 'POST')
        self.assertTrue(approval_actions.get('idempotent', False))
        self.assertIn('payload', approval_actions)


class TestReconcilePositions(unittest.TestCase):
    def test_reconcile_exchange_positions(self):
        db = Database('data/test_reconcile.db')
        try:
            db.update_position('SOL/USDT', 'long', 100, 1, 5, 100)
            db.record_trade(symbol='BTC/USDT', side='long', entry_price=50000, quantity=2, leverage=10)
            db.record_trade(symbol='ETH/USDT', side='long', entry_price=3000, quantity=1, leverage=5)
            report = reconcile_exchange_positions(PositionSyncExchangeStub(), db)
            positions = db.get_positions()
            symbols = {p['symbol'] for p in positions}
            self.assertEqual(report['synced'], 2)
            self.assertEqual(report['removed'], 1)
            self.assertIn('BTC/USDT', symbols)
            self.assertIn('XRP/USDT', symbols)
            self.assertNotIn('SOL/USDT', symbols)
            self.assertEqual(report['summary']['exchange_positions'], 2)
            self.assertEqual(report['summary']['open_trades'], 2)
            self.assertEqual(report['summary']['stale_open_trades_closed'], 1)
            self.assertEqual(report['summary']['open_trade_missing_exchange'], 0)
            closed_eth = db.get_trades(symbol='ETH/USDT', limit=5)[0]
            self.assertEqual(closed_eth['status'], 'closed')
            self.assertIn('自动收口', closed_eth.get('notes') or '')
            self.assertEqual(report['summary']['exchange_missing_open_trade'], 0)
            self.assertEqual(report['created_open_trades'][0]['symbol'], 'XRP/USDT')
        finally:
            if os.path.exists('data/test_reconcile.db'):
                os.remove('data/test_reconcile.db')


class FakeSignalAnalysisExchange:
    def __init__(self, candles_by_symbol=None):
        self.candles_by_symbol = candles_by_symbol or {}

    def fetch_ohlcv(self, symbol, timeframe='1h', since=None, limit=100):
        return self.candles_by_symbol.get(symbol, [])


class FakeHealthNotifier:
    def __init__(self):
        self.calls = []

    def send(self, event_type, title, lines, level='info', details=None, priority='normal'):
        payload = {
            'event_type': event_type,
            'title': title,
            'lines': lines,
            'level': level,
            'details': details or {},
            'priority': priority,
        }
        self.calls.append(payload)
        return {'delivered': False, 'message': '\n'.join(lines), 'title': title}

    def notify_runtime(self, event_type, lines, details=None):
        payload = {
            'event_type': event_type,
            'title': event_type,
            'lines': lines,
            'details': details or {},
        }
        self.calls.append(payload)
        return {'delivered': False, 'message': '\n'.join(lines), 'title': event_type}


class TestDashboardApi(unittest.TestCase):
    def test_daily_summary_handles_null_pnl(self):
        client = app.test_client()
        resp = client.get('/api/daily/summary')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json.get('success'))
        self.assertIsInstance(resp.json.get('data'), list)

    def test_system_checklist_returns_runtime_and_model_checks(self):
        client = app.test_client()
        resp = client.get('/api/system/checklist')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json.get('success'))
        data = resp.json.get('data') or {}
        self.assertIn('checks', data)
        self.assertIn('models', data)
        self.assertIsInstance(data.get('checks'), list)

    def test_risk_loss_streak_reset_endpoint(self):
        client = app.test_client()
        resp = client.post('/api/risk/loss-streak/reset', json={'note': 'unit-test'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json.get('success'))
        self.assertIn('message', resp.json.get('data') or {})

    def test_evaluate_filtered_signal_outcome_classifies_buy_cases(self):
        import dashboard.api as dashboard_api

        base_signal = {
            'symbol': 'BTC/USDT',
            'signal_type': 'buy',
            'price': 100.0,
            'created_at': '2026-03-16T00:00:00'
        }
        avoided = dashboard_api._evaluate_filtered_signal_outcome(
            base_signal,
            [[0, 100, 100.5, 97.0, 98.0, 1]],
            window_hours=24,
            min_move_pct=1.5,
            now=datetime.fromisoformat('2026-03-18T00:00:00')
        )
        missed = dashboard_api._evaluate_filtered_signal_outcome(
            base_signal,
            [[0, 100, 104.0, 99.4, 103.0, 1]],
            window_hours=24,
            min_move_pct=1.5,
            now=datetime.fromisoformat('2026-03-18T00:00:00')
        )
        self.assertEqual(avoided['status'], 'avoided_loss')
        self.assertEqual(missed['status'], 'missed_profit')

    def test_filter_effectiveness_api_groups_outcomes_by_filter_code(self):
        import dashboard.api as dashboard_api

        test_db = Database('data/test_dashboard_effectiveness.db')
        old_db = dashboard_api.db
        old_exchange_client = dashboard_api._exchange_client
        dashboard_api.db = test_db
        dashboard_api._exchange_client = FakeSignalAnalysisExchange({
            'BTC/USDT': [[0, 100, 100.4, 97.0, 98.0, 1]],
            'ETH/USDT': [[0, 100, 104.0, 99.5, 103.0, 1]],
        })
        try:
            old_time = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')

            btc_id = test_db.record_signal(
                symbol='BTC/USDT',
                signal_type='buy',
                price=100,
                strength=70,
                reasons=[],
                strategies_triggered=['trend_follow']
            )
            test_db.update_signal(
                btc_id,
                filtered=1,
                filter_reason='信号逆大趋势',
                filter_code='COUNTER_TREND',
                filter_group='market',
                action_hint='继续等趋势一致',
                created_at=old_time
            )

            eth_id = test_db.record_signal(
                symbol='ETH/USDT',
                signal_type='buy',
                price=100,
                strength=68,
                reasons=[],
                strategies_triggered=['breakout']
            )
            test_db.update_signal(
                eth_id,
                filtered=1,
                filter_reason='信号强度不足',
                filter_code='WEAK_SIGNAL_STRENGTH',
                filter_group='signal',
                action_hint='等更强确认',
                created_at=old_time
            )

            client = app.test_client()
            resp = client.get('/api/signals/filter-effectiveness?days=30&window_hours=24&limit=20&min_move_pct=1.5')
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json.get('success'))
            rows = resp.json.get('data') or []
            summary = resp.json.get('summary') or {}

            counter_trend = next(row for row in rows if row['code'] == 'COUNTER_TREND')
            weak_strength = next(row for row in rows if row['code'] == 'WEAK_SIGNAL_STRENGTH')
            self.assertEqual(counter_trend['avoided_loss'], 1)
            self.assertEqual(counter_trend['missed_profit'], 0)
            self.assertEqual(counter_trend['effectiveness_rate'], 100.0)
            self.assertEqual(weak_strength['missed_profit'], 1)
            self.assertEqual(weak_strength['avoided_loss'], 0)
            self.assertEqual(weak_strength['effectiveness_rate'], 0.0)
            self.assertEqual(summary['analyzed'], 2)
            self.assertEqual(summary['avoided_loss'], 1)
            self.assertEqual(summary['missed_profit'], 1)
        finally:
            dashboard_api.db = old_db
            dashboard_api._exchange_client = old_exchange_client
            if os.path.exists('data/test_dashboard_effectiveness.db'):
                os.remove('data/test_dashboard_effectiveness.db')

    def test_filter_effectiveness_by_symbol_api_returns_tuning_bias(self):
        import dashboard.api as dashboard_api

        test_db = Database('data/test_dashboard_effectiveness_symbol.db')
        old_db = dashboard_api.db
        old_exchange_client = dashboard_api._exchange_client
        dashboard_api.db = test_db
        dashboard_api._exchange_client = FakeSignalAnalysisExchange({
            'BTC/USDT': [[0, 100, 100.3, 97.0, 98.0, 1]],
            'XRP/USDT': [[0, 100, 104.5, 99.6, 104.0, 1]],
        })
        try:
            old_time = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
            btc_id = test_db.record_signal(
                symbol='BTC/USDT',
                signal_type='buy',
                price=100,
                strength=71,
                reasons=[],
                strategies_triggered=['trend_follow']
            )
            test_db.update_signal(
                btc_id,
                filtered=1,
                filter_reason='信号逆大趋势',
                filter_code='COUNTER_TREND',
                filter_group='market',
                action_hint='继续等趋势一致',
                created_at=old_time
            )
            xrp_id = test_db.record_signal(
                symbol='XRP/USDT',
                signal_type='buy',
                price=100,
                strength=66,
                reasons=[],
                strategies_triggered=['trend_follow']
            )
            test_db.update_signal(
                xrp_id,
                filtered=1,
                filter_reason='信号逆大趋势',
                filter_code='COUNTER_TREND',
                filter_group='market',
                action_hint='继续等趋势一致',
                created_at=old_time
            )

            client = app.test_client()
            resp = client.get('/api/signals/filter-effectiveness/by-symbol?days=30&window_hours=24&limit=30&min_move_pct=1.5')
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json.get('success'))
            rows = resp.json.get('data') or []
            summary = resp.json.get('summary') or {}

            btc = next(row for row in rows if row['symbol'] == 'BTC/USDT' and row['code'] == 'COUNTER_TREND')
            xrp = next(row for row in rows if row['symbol'] == 'XRP/USDT' and row['code'] == 'COUNTER_TREND')
            self.assertEqual(btc['tuning_bias'], 'keep_strict')
            self.assertEqual(xrp['tuning_bias'], 'consider_relax')
            self.assertEqual(btc['avoided_loss'], 1)
            self.assertEqual(xrp['missed_profit'], 1)
            self.assertEqual(summary['keep_strict'], 1)
            self.assertEqual(summary['consider_relax'], 1)
        finally:
            dashboard_api.db = old_db
            dashboard_api._exchange_client = old_exchange_client
            if os.path.exists('data/test_dashboard_effectiveness_symbol.db'):
                os.remove('data/test_dashboard_effectiveness_symbol.db')

    def test_parameter_advice_api_returns_symbol_level_suggestions(self):
        import dashboard.api as dashboard_api

        test_db = Database('data/test_dashboard_param_advice.db')
        old_db = dashboard_api.db
        old_exchange_client = dashboard_api._exchange_client
        dashboard_api.db = test_db
        dashboard_api._exchange_client = FakeSignalAnalysisExchange({
            'XRP/USDT': [[0, 100, 104.5, 99.6, 104.0, 1]],
            'BTC/USDT': [[0, 100, 100.3, 97.0, 98.0, 1]],
        })
        try:
            old_time = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
            xrp_id = test_db.record_signal(
                symbol='XRP/USDT',
                signal_type='buy',
                price=100,
                strength=22,
                reasons=[],
                strategies_triggered=['trend_follow']
            )
            test_db.update_signal(
                xrp_id,
                filtered=1,
                filter_reason='信号强度不足',
                filter_code='WEAK_SIGNAL_STRENGTH',
                filter_group='signal',
                action_hint='等更强确认',
                created_at=old_time
            )
            btc_id = test_db.record_signal(
                symbol='BTC/USDT',
                signal_type='buy',
                price=100,
                strength=72,
                reasons=[],
                strategies_triggered=['trend_follow']
            )
            test_db.update_signal(
                btc_id,
                filtered=1,
                filter_reason='信号逆大趋势',
                filter_code='COUNTER_TREND',
                filter_group='market',
                action_hint='继续等趋势一致',
                created_at=old_time
            )

            client = app.test_client()
            resp = client.get('/api/signals/parameter-advice?days=30&window_hours=24&limit=30&min_move_pct=1.5')
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json.get('success'))
            rows = resp.json.get('data') or []
            summary = resp.json.get('summary') or {}

            xrp = next(row for row in rows if row['symbol'] == 'XRP/USDT' and row['parameter'] == 'strategies.composite.min_strength')
            btc = next(row for row in rows if row['symbol'] == 'BTC/USDT' and row['parameter'] == 'market_filters.block_counter_trend')
            self.assertEqual(xrp['action'], 'relax')
            self.assertLess(xrp['suggested_value'], xrp['current_value'])
            self.assertEqual(btc['action'], 'keep')
            self.assertEqual(summary['relax'], 1)
            self.assertEqual(summary['keep'], 1)
        finally:
            dashboard_api.db = old_db
            dashboard_api._exchange_client = old_exchange_client
            if os.path.exists('data/test_dashboard_param_advice.db'):
                os.remove('data/test_dashboard_param_advice.db')

    def test_parameter_advice_draft_api_returns_yaml_and_skips_review_only_items(self):
        import dashboard.api as dashboard_api

        test_db = Database('data/test_dashboard_param_draft.db')
        old_db = dashboard_api.db
        old_exchange_client = dashboard_api._exchange_client
        dashboard_api.db = test_db
        dashboard_api._exchange_client = FakeSignalAnalysisExchange({
            'XRP/USDT': [[0, 100, 104.5, 99.6, 104.0, 1]],
            'DOGE/USDT': [[0, 100, 104.2, 99.7, 103.8, 1]],
        })
        try:
            old_time = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
            xrp_id = test_db.record_signal(
                symbol='XRP/USDT',
                signal_type='buy',
                price=100,
                strength=22,
                reasons=[],
                strategies_triggered=['trend_follow']
            )
            test_db.update_signal(
                xrp_id,
                filtered=1,
                filter_reason='信号强度不足',
                filter_code='WEAK_SIGNAL_STRENGTH',
                filter_group='signal',
                action_hint='等更强确认',
                created_at=old_time
            )
            doge_id = test_db.record_signal(
                symbol='DOGE/USDT',
                signal_type='buy',
                price=100,
                strength=70,
                reasons=[],
                strategies_triggered=['trend_follow']
            )
            test_db.update_signal(
                doge_id,
                filtered=1,
                filter_reason='信号逆大趋势',
                filter_code='COUNTER_TREND',
                filter_group='market',
                action_hint='继续等趋势一致',
                created_at=old_time
            )

            client = app.test_client()
            resp = client.get('/api/signals/parameter-advice/draft?days=30&window_hours=24&limit=30&min_move_pct=1.5')
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json.get('success'))
            data = resp.json.get('data') or {}
            summary = resp.json.get('summary') or {}
            draft = data.get('draft') or {}
            yaml_text = data.get('yaml') or ''

            self.assertIn('XRP/USDT', (draft.get('symbol_overrides') or {}))
            self.assertEqual(draft['symbol_overrides']['XRP/USDT']['strategies']['composite']['min_strength'], 26)
            self.assertIn('symbol_overrides:', yaml_text)
            self.assertIn('XRP/USDT:', yaml_text)
            self.assertEqual(summary['draft_symbols'], 1)
            self.assertEqual(summary['draft_parameters'], 1)
            self.assertEqual(summary['skipped'], 1)
            self.assertEqual((draft.get('skipped') or [])[0]['symbol'], 'DOGE/USDT')
        finally:
            dashboard_api.db = old_db
            dashboard_api._exchange_client = old_exchange_client
            if os.path.exists('data/test_dashboard_param_draft.db'):
                os.remove('data/test_dashboard_param_draft.db')

    def test_apply_parameter_advice_draft_writes_local_override_file(self):
        import dashboard.api as dashboard_api

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, 'config.yaml')
            local_path = os.path.join(tmpdir, 'config.local.yaml')
            with open(cfg_path, 'w', encoding='utf-8') as f:
                f.write(
                    "strategies:\n"
                    "  composite:\n"
                    "    min_strength: 28\n"
                )
            with open(local_path, 'w', encoding='utf-8') as f:
                f.write(
                    "notification:\n"
                    "  discord:\n"
                    "    enabled: true\n"
                )

            old_config = dashboard_api.config
            old_db = dashboard_api.db
            old_risk_manager = dashboard_api.risk_manager
            old_ml_engine = dashboard_api.ml_engine
            old_backtester = dashboard_api.backtester
            old_signal_quality_analyzer = dashboard_api.signal_quality_analyzer
            old_optimizer = dashboard_api.optimizer
            old_governance = dashboard_api.governance
            old_preset_manager = dashboard_api.preset_manager
            old_exchange_client = dashboard_api._exchange_client
            dashboard_api.config = Config(cfg_path)
            dashboard_api.db = Database(os.path.join(tmpdir, 'test.db'))
            try:
                client = app.test_client()
                resp = client.post('/api/signals/parameter-advice/apply-draft', json={
                    'draft': {
                        'symbol_overrides': {
                            'XRP/USDT': {
                                'strategies': {
                                    'composite': {
                                        'min_strength': 26
                                    }
                                }
                            }
                        },
                        'skipped': []
                    },
                    'note': 'unit test apply'
                })
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.json.get('success'))
                with open(local_path, 'r', encoding='utf-8') as f:
                    applied = yaml.safe_load(f) or {}
                self.assertTrue(applied['notification']['discord']['enabled'])
                self.assertEqual(applied['symbol_overrides']['XRP/USDT']['strategies']['composite']['min_strength'], 26)
                self.assertEqual(resp.json['data']['applied_symbol_count'], 1)
                history_resp = client.get('/api/signals/overrides/history?limit=5')
                self.assertEqual(history_resp.status_code, 200)
                self.assertTrue(history_resp.json.get('success'))
                history_rows = history_resp.json.get('data') or []
                self.assertEqual(history_rows[0]['action'], 'apply')
                self.assertIn('XRP/USDT', history_rows[0]['symbols'])
            finally:
                dashboard_api.config = old_config
                dashboard_api.db = old_db
                dashboard_api.risk_manager = old_risk_manager
                dashboard_api.ml_engine = old_ml_engine
                dashboard_api.backtester = old_backtester
                dashboard_api.signal_quality_analyzer = old_signal_quality_analyzer
                dashboard_api.optimizer = old_optimizer
                dashboard_api.governance = old_governance
                dashboard_api.preset_manager = old_preset_manager
                dashboard_api._exchange_client = old_exchange_client

    def test_override_status_and_rollback_restore_local_config(self):
        import dashboard.api as dashboard_api

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, 'config.yaml')
            local_path = os.path.join(tmpdir, 'config.local.yaml')
            backups_dir = os.path.join(tmpdir, 'backups')
            os.makedirs(backups_dir, exist_ok=True)
            with open(cfg_path, 'w', encoding='utf-8') as f:
                f.write(
                    "strategies:\n"
                    "  composite:\n"
                    "    min_strength: 28\n"
                    "    min_strategy_count: 1\n"
                    "market_filters:\n"
                    "  min_volatility: 0.0045\n"
                    "  max_volatility: 0.05\n"
                    "  block_counter_trend: true\n"
                    "trading:\n"
                    "  cooldown_minutes: 15\n"
                )
            with open(local_path, 'w', encoding='utf-8') as f:
                f.write(
                    "symbol_overrides:\n"
                    "  XRP/USDT:\n"
                    "    strategies:\n"
                    "      composite:\n"
                    "        min_strength: 26\n"
                )
            backup_name = 'config.local-20260318-140000.yaml'
            backup_path = os.path.join(backups_dir, backup_name)
            with open(backup_path, 'w', encoding='utf-8') as f:
                f.write(
                    "symbol_overrides:\n"
                    "  BTC/USDT:\n"
                    "    strategies:\n"
                    "      composite:\n"
                    "        min_strength: 24\n"
                )

            old_config = dashboard_api.config
            old_db = dashboard_api.db
            old_risk_manager = dashboard_api.risk_manager
            old_ml_engine = dashboard_api.ml_engine
            old_backtester = dashboard_api.backtester
            old_signal_quality_analyzer = dashboard_api.signal_quality_analyzer
            old_optimizer = dashboard_api.optimizer
            old_governance = dashboard_api.governance
            old_preset_manager = dashboard_api.preset_manager
            old_exchange_client = dashboard_api._exchange_client
            dashboard_api.config = Config(cfg_path)
            dashboard_api.db = Database(os.path.join(tmpdir, 'test.db'))
            try:
                client = app.test_client()
                status_resp = client.get('/api/signals/overrides/status')
                self.assertEqual(status_resp.status_code, 200)
                self.assertTrue(status_resp.json.get('success'))
                rows = status_resp.json['data']['rows']
                self.assertEqual(rows[0]['symbol'], 'XRP/USDT')
                self.assertEqual(rows[0]['effective']['strategies']['composite']['min_strength'], 26)

                backups_resp = client.get('/api/signals/overrides/backups')
                self.assertEqual(backups_resp.status_code, 200)
                self.assertTrue(any(item['name'] == backup_name for item in backups_resp.json.get('data') or []))

                rollback_resp = client.post('/api/signals/overrides/rollback', json={'backup_name': backup_name})
                self.assertEqual(rollback_resp.status_code, 200)
                self.assertTrue(rollback_resp.json.get('success'))
                with open(local_path, 'r', encoding='utf-8') as f:
                    restored = yaml.safe_load(f) or {}
                self.assertIn('BTC/USDT', restored.get('symbol_overrides', {}))
                self.assertNotIn('XRP/USDT', restored.get('symbol_overrides', {}))
                history_resp = client.get('/api/signals/overrides/history?limit=5')
                self.assertEqual(history_resp.status_code, 200)
                history_rows = history_resp.json.get('data') or []
                self.assertEqual(history_rows[0]['action'], 'rollback')
                self.assertIn('BTC/USDT', history_rows[0]['symbols'])
            finally:
                dashboard_api.config = old_config
                dashboard_api.db = old_db
                dashboard_api.risk_manager = old_risk_manager
                dashboard_api.ml_engine = old_ml_engine
                dashboard_api.backtester = old_backtester
                dashboard_api.signal_quality_analyzer = old_signal_quality_analyzer
                dashboard_api.optimizer = old_optimizer
                dashboard_api.governance = old_governance
                dashboard_api.preset_manager = old_preset_manager
                dashboard_api._exchange_client = old_exchange_client

    def test_signal_coin_breakdown_groups_watch_filtered_and_executed_by_symbol(self):
        import dashboard.api as dashboard_api

        test_db = Database('data/test_dashboard_signals.db')
        old_db = dashboard_api.db
        dashboard_api.db = test_db
        try:
            buy_id = test_db.record_signal(
                symbol='BTC/USDT',
                signal_type='buy',
                price=50000,
                strength=82,
                reasons=[],
                strategies_triggered=['trend_follow']
            )
            test_db.update_signal(
                buy_id,
                filtered=1,
                filter_reason='信号逆大趋势',
                filter_code='COUNTER_TREND',
                filter_group='market',
                action_hint='当前方向逆大趋势，除非策略明确允许，否则继续观望',
                filter_details=json.dumps({'filter_meta': {'code': 'COUNTER_TREND'}}, ensure_ascii=False)
            )

            hold_id = test_db.record_signal(
                symbol='BTC/USDT',
                signal_type='hold',
                price=50010,
                strength=18,
                reasons=[],
                strategies_triggered=[]
            )
            test_db.update_signal(
                hold_id,
                filtered=1,
                filter_reason='无可执行方向',
                filter_code='NO_DIRECTION',
                filter_group='signal',
                action_hint='先观察方向分数与触发策略，当前未形成可执行方向',
                filter_details=json.dumps({'filter_meta': {'code': 'NO_DIRECTION'}}, ensure_ascii=False)
            )

            executed_id = test_db.record_signal(
                symbol='ETH/USDT',
                signal_type='buy',
                price=3000,
                strength=76,
                reasons=[],
                strategies_triggered=['breakout']
            )
            test_db.update_signal(executed_id, executed=1)

            client = app.test_client()
            resp = client.get('/api/signals/coin-breakdown?days=30')
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json.get('success'))
            rows = resp.json.get('data') or []
            summary = resp.json.get('summary') or {}

            btc = next(row for row in rows if row['symbol'] == 'BTC/USDT')
            eth = next(row for row in rows if row['symbol'] == 'ETH/USDT')

            self.assertEqual(btc['hold_signals'], 1)
            self.assertEqual(btc['filtered_signals'], 2)
            self.assertEqual(btc['executed_signals'], 0)
            self.assertEqual(btc['latest_status'], 'watch')
            self.assertEqual({item['reason'] for item in btc['top_filter_reasons']}, {'信号逆大趋势', '无可执行方向'})
            self.assertEqual({item['code'] for item in btc['top_filter_codes']}, {'COUNTER_TREND', 'NO_DIRECTION'})
            self.assertEqual(btc['dominant_filter_group'], 'market')
            self.assertTrue(btc['diagnostic_action'])
            self.assertEqual(eth['executed_signals'], 1)
            self.assertEqual(eth['latest_status'], 'executed')
            self.assertEqual(summary['symbols'], 2)
            self.assertEqual(summary['executed_symbols'], 1)

            # 验证新增的诊断层字段
            self.assertIn('blocker_dimension', btc)
            self.assertIn('dominant_blocker', btc)
            self.assertIn('samples_24h', btc)
            self.assertIn('samples_48h', btc)
            self.assertIn('sample_status', btc)
            self.assertIn('recommendation', btc)
            self.assertIn('dimension_breakdown', summary)
            self.assertIn('sample_status', summary)
        finally:
            dashboard_api.db = old_db
            if os.path.exists('data/test_dashboard_signals.db'):
                os.remove('data/test_dashboard_signals.db')

    def test_coin_breakdown_diagnostic_dimension_classification(self):
        """测试 coin-breakdown 诊断层字段：阻塞维度分类"""
        import dashboard.api as dashboard_api
        test_db = Database('data/test_diagnostic_dimension.db')
        old_db = dashboard_api.db
        dashboard_api.db = test_db
        try:
            # 添加不同过滤码的信号
            # NO_DIRECTION -> direction
            id1 = test_db.record_signal(
                symbol='BTC/USDT',
                signal_type='hold',
                price=50000,
                strength=10,
                reasons=[],
                strategies_triggered=[]
            )
            test_db.update_signal(id1, filtered=1, filter_reason='无可执行方向', filter_code='NO_DIRECTION', filter_group='signal', action_hint='等方向明确')

            # COUNTER_TREND -> trend
            id2 = test_db.record_signal(
                symbol='BTC/USDT',
                signal_type='buy',
                price=50000,
                strength=60,
                reasons=[],
                strategies_triggered=['RSI']
            )
            test_db.update_signal(id2, filtered=1, filter_reason='信号逆大趋势', filter_code='COUNTER_TREND', filter_group='market', action_hint='等趋势一致')

            # VOLATILITY_LOW -> volatility
            id3 = test_db.record_signal(
                symbol='ETH/USDT',
                signal_type='buy',
                price=3000,
                strength=65,
                reasons=[],
                strategies_triggered=['MACD']
            )
            test_db.update_signal(id3, filtered=1, filter_reason='波动率过低', filter_code='VOLATILITY_LOW', filter_group='market', action_hint='等待波动恢复')

            client = app.test_client()
            resp = client.get('/api/signals/coin-breakdown?days=30')
            self.assertEqual(resp.status_code, 200)
            rows = resp.json.get('data') or []

            btc = next(row for row in rows if row['symbol'] == 'BTC/USDT')
            eth = next(row for row in rows if row['symbol'] == 'ETH/USDT')

            # 验证 BTC 阻塞维度
            self.assertIn('direction', btc['blocker_dimension'])
            self.assertIn('trend', btc['blocker_dimension'])
            self.assertEqual(btc['dominant_blocker'], 'direction')

            # 验证 ETH 阻塞维度
            self.assertIn('volatility', eth['blocker_dimension'])
            self.assertEqual(eth['dominant_blocker'], 'volatility')

            # 验证 recommendation 包含维度信息
            self.assertIn('等方向明确', btc['recommendation'])
            self.assertIn('等待波动率恢复', eth['recommendation'])

        finally:
            dashboard_api.db = old_db
            if os.path.exists('data/test_diagnostic_dimension.db'):
                os.remove('data/test_diagnostic_dimension.db')

    def test_coin_breakdown_sample_status_24h_vs_48h(self):
        """测试 coin-breakdown 样本状态与 24h vs 48h 对比"""
        import dashboard.api as dashboard_api
        test_db = Database('data/test_sample_status.db')
        old_db = dashboard_api.db
        dashboard_api.db = test_db
        try:
            # 添加信号 - 样本不足的情况（只有1个信号）
            test_db.record_signal(
                symbol='BTC/USDT',
                signal_type='hold',
                price=50000,
                strength=10,
                reasons=[],
                strategies_triggered=[]
            )

            client = app.test_client()
            resp = client.get('/api/signals/coin-breakdown?days=30')
            self.assertEqual(resp.status_code, 200)
            rows = resp.json.get('data') or []
            summary = resp.json.get('summary') or {}

            btc = next(row for row in rows if row['symbol'] == 'BTC/USDT')

            # 验证样本状态
            self.assertEqual(btc['samples_24h'], 1)
            self.assertEqual(btc['samples_48h'], 1)
            self.assertEqual(btc['sample_status'], 'insufficient')

            # 验证全局样本统计
            self.assertIn('sample_status', summary)
            self.assertEqual(summary['sample_status']['insufficient'], 1)

        finally:
            dashboard_api.db = old_db
            if os.path.exists('data/test_sample_status.db'):
                os.remove('data/test_sample_status.db')


class TestDiagnostics(unittest.TestCase):
    """只读诊断输出测试"""

    def test_build_exchange_diagnostics(self):
        cfg = Config()
        cfg._config['symbols'] = {'watch_list': ['BTC/USDT', 'DOGE/USDT']}
        cfg._config.setdefault('exchange', {})['position_mode'] = 'oneway'
        cfg._config.setdefault('trading', {})['position_size'] = 0.1
        cfg._config['trading']['leverage'] = 10
        report = build_exchange_diagnostics(cfg, DiagnosticExchangeStub())

        self.assertEqual(report['position_mode'], 'oneway')
        self.assertEqual(report['available_usdt'], 321.5)
        self.assertEqual(len(report['symbols']), 2)
        btc = next(x for x in report['symbols'] if x['symbol'] == 'BTC/USDT')
        doge = next(x for x in report['symbols'] if x['symbol'] == 'DOGE/USDT')
        self.assertTrue(btc['is_futures_symbol'])
        self.assertEqual(btc['order_symbol'], 'BTC/USDT:USDT')
        self.assertNotIn('posSide', btc['order_params_preview'])
        self.assertFalse(doge['is_futures_symbol'])
        self.assertEqual(doge['reason'], 'not-swap-market')

    def test_build_exchange_smoke_plan(self):
        cfg = Config()
        cfg._config['symbols'] = {'watch_list': ['BTC/USDT']}
        cfg._config.setdefault('exchange', {})['position_mode'] = 'hedge'
        cfg._config.setdefault('trading', {})['position_size'] = 0.1
        cfg._config['trading']['leverage'] = 10
        plan = build_exchange_smoke_plan(cfg, DiagnosticExchangeStub(), symbol='BTC/USDT', side='long')

        self.assertEqual(plan['symbol'], 'BTC/USDT')
        self.assertTrue(plan['execute_ready'])
        self.assertEqual(plan['open_preview']['side'], 'buy')
        self.assertEqual(plan['close_preview']['side'], 'sell')
        self.assertIn('posSide', plan['open_preview']['params'])
        self.assertTrue(plan['close_preview']['params']['reduceOnly'])

    def test_execute_exchange_smoke_in_testnet(self):
        cfg = Config()
        cfg._config['symbols'] = {'watch_list': ['BTC/USDT']}
        cfg._config.setdefault('exchange', {})['position_mode'] = 'hedge'
        cfg._config['exchange']['mode'] = 'testnet'
        cfg._config.setdefault('trading', {})['position_size'] = 0.1
        cfg._config['trading']['leverage'] = 10
        ex = ExecutableExchangeStub()
        db = Database('data/test_smoke.db')
        try:
            result = execute_exchange_smoke(cfg, ex, symbol='BTC/USDT', side='long', db=db)
            self.assertTrue(result['opened'])
            self.assertTrue(result['closed'])
            self.assertEqual(ex.open_calls[0]['side'], 'buy')
            self.assertEqual(ex.close_calls[0]['side'], 'sell')
            self.assertEqual(ex.open_calls[0]['posSide'], 'long')
            self.assertIsNotNone(result.get('smoke_run_id'))
            rows = db.get_smoke_runs(limit=5)
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]['success'])
            self.assertEqual(rows[0]['symbol'], 'BTC/USDT')
        finally:
            if os.path.exists('data/test_smoke.db'):
                os.remove('data/test_smoke.db')

    def test_execute_exchange_smoke_rejects_real_mode(self):
        cfg = Config()
        cfg._config['symbols'] = {'watch_list': ['BTC/USDT']}
        cfg._config.setdefault('exchange', {})['position_mode'] = 'oneway'
        cfg._config['exchange']['mode'] = 'real'
        cfg._config.setdefault('trading', {})['position_size'] = 0.1
        cfg._config['trading']['leverage'] = 10
        result = execute_exchange_smoke(cfg, ExecutableExchangeStub(), symbol='BTC/USDT', side='short')

        self.assertFalse(result['opened'])
        self.assertIn('testnet', result['error'])


class TestHealthSummary(unittest.TestCase):
    def test_build_approval_hygiene_summary_reports_stale_counts(self):
        cfg = Config()
        cfg._config.setdefault('runtime', {}).setdefault('approval_hygiene', {}).update({
            'enabled': True,
            'auto_cleanup_enabled': False,
            'stale_after_minutes': 60,
            'limit': 10,
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / 'approval_hygiene_summary.db'
            db = Database(str(db_path))
            item_id = 'pool_switch::btc-focused'
            db.sync_approval_items([
                {
                    'item_id': item_id,
                    'approval_type': 'pool_switch',
                    'target': 'btc-focused',
                    'title': '切换主池',
                    'approval_state': 'pending',
                    'workflow_state': 'pending',
                }
            ], replay_source='workflow_replay')
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE approval_state SET created_at = datetime('now', '-180 minutes'), updated_at = datetime('now', '-180 minutes'), last_seen_at = datetime('now', '-180 minutes') WHERE item_id = ?", (item_id,))
                conn.commit()
            summary = build_approval_hygiene_summary(cfg, db)
            self.assertEqual(summary['stale_count'], 1)
            self.assertEqual(summary['decision_diff_count'], 0)
            self.assertEqual(summary['audit_overview']['stale_pending']['count'], 1)
            self.assertEqual(summary['stale_items'][0]['item_id'], item_id)
            self.assertEqual(summary['operator_action_summary']['policy_counts']['review_schedule'], 1)
            self.assertIn('review_schedule_queue', summary['operator_action_summary']['routes'])

    def test_maybe_run_approval_hygiene_auto_cleanup_expires_stale_items(self):
        import bot.run as bot_run
        cfg = Config()
        cfg._config.setdefault('runtime', {}).setdefault('approval_hygiene', {}).update({
            'enabled': True,
            'auto_cleanup_enabled': True,
            'stale_after_minutes': 60,
            'limit': 10,
            'actor': 'system:test-approval-hygiene',
        })
        notifier = FakeHealthNotifier()
        old_runtime_path = bot_run.RUNTIME_STATE_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            bot_run.RUNTIME_STATE_PATH = Path(tmpdir) / 'runtime_state.json'
            db_path = Path(tmpdir) / 'approval_hygiene_cleanup.db'
            db = Database(str(db_path))
            item_id = 'pool_switch::btc-focused'
            db.sync_approval_items([
                {
                    'item_id': item_id,
                    'approval_type': 'pool_switch',
                    'target': 'btc-focused',
                    'title': '切换主池',
                    'approval_state': 'pending',
                    'workflow_state': 'pending',
                }
            ], replay_source='workflow_replay')
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE approval_state SET created_at = datetime('now', '-180 minutes'), updated_at = datetime('now', '-180 minutes'), last_seen_at = datetime('now', '-180 minutes') WHERE item_id = ?", (item_id,))
                conn.commit()
            try:
                result = maybe_run_approval_hygiene(cfg, db, notifier)
                self.assertTrue(result['ran_cleanup'])
                self.assertEqual(result['cleanup']['expired_count'], 1)
                self.assertEqual(db.get_approval_state(item_id)['state'], 'expired')
                self.assertTrue(any(call['event_type'] == 'approval_hygiene' for call in notifier.calls))
                state = json.loads(bot_run.RUNTIME_STATE_PATH.read_text())
                self.assertEqual(state['approval_hygiene']['expired_count'], 1)
            finally:
                bot_run.RUNTIME_STATE_PATH = old_runtime_path

    def test_build_runtime_health_summary_contains_core_sections(self):
        cfg = Config()
        db = Database('data/test_health_summary.db')
        try:
            summary = build_runtime_health_summary(cfg, db)
            self.assertIn('title', summary)
            self.assertIn('lines', summary)
            self.assertIn('details', summary)
            self.assertTrue(any('环境' in line for line in summary['lines']))
            self.assertTrue(any('最近一轮' in line for line in summary['lines']))
            self.assertTrue(any('Adaptive Regime' in line for line in summary['lines']))
            self.assertTrue(any('observe-only' in line for line in summary['lines']))
            self.assertTrue(any('Approval Hygiene' in line for line in summary['lines']))
            self.assertTrue(any('Operator routing' in line for line in summary['lines']))
            self.assertTrue(any('Adaptive Rollout Orchestration' in line for line in summary['lines']))
            self.assertIn('approval_hygiene', summary['details'])
            self.assertIn('adaptive_rollout_orchestration', summary['details'])
        finally:
            if os.path.exists('data/test_health_summary.db'):
                os.remove('data/test_health_summary.db')

    def test_build_runtime_health_summary_surfaces_recovery_rerun_observability(self):
        import bot.run as bot_run
        cfg = Config()
        db = Database('data/test_health_summary_recovery_rerun.db')
        old_runtime_path = bot_run.RUNTIME_STATE_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            bot_run.RUNTIME_STATE_PATH = Path(tmpdir) / 'runtime_state.json'
            bot_run.RUNTIME_STATE_PATH.write_text(json.dumps({
                'adaptive_rollout_orchestration': {
                    'last_run_at': '2026-03-29T06:00:00',
                    'summary': {
                        'gate_status': 'ready',
                        'gate_blocked': False,
                        'auto_approval_executed_count': 0,
                        'controlled_rollout_executed_count': 1,
                        'review_queue_queued_count': 0,
                        'recovery_retry_scheduled_count': 1,
                        'recovery_retry_reentered_executor_count': 1,
                        'recovery_rollback_queued_count': 1,
                        'recovery_manual_annotation_count': 1,
                    },
                    'runtime_summary': {
                        'rerun_observability': {
                            'primary_reason': 'post_recovery_state_transition',
                            'recovery_triggered': True,
                            'recovery_reasons': ['recovery_retry_scheduled', 'recovery_retry_reentered_executor'],
                            'result_counts': {'rerun_pass_count': 1},
                        },
                        'next_step': {'summary': 'drain recovery follow-ups'},
                    },
                    'cooldown': {'min_interval_seconds': 300, 'active': False, 'remaining_seconds': 0},
                },
            }, ensure_ascii=False, indent=2))
            try:
                summary = build_runtime_health_summary(cfg, db)
                self.assertTrue(any('Recovery rerun：triggered=yes' in line for line in summary['lines']))
                self.assertTrue(any('Recovery lane：retry 1 ｜ reentered 1 ｜ rollback 1 ｜ manual-note 1' in line for line in summary['lines']))
            finally:
                bot_run.RUNTIME_STATE_PATH = old_runtime_path
        if os.path.exists('data/test_health_summary_recovery_rerun.db'):
            os.remove('data/test_health_summary_recovery_rerun.db')

    def test_maybe_run_adaptive_rollout_orchestration_persists_runtime_summary(self):
        import bot.run as bot_run
        cfg = Config()
        cfg._config.setdefault('runtime', {}).setdefault('adaptive_rollout_orchestration', {}).update({
            'enabled': True,
            'use_cache': True,
            'notify_on_activity': True,
            'max_items': 3,
        })
        db = Database('data/test_runtime_adaptive_rollout.db')
        notifier = FakeHealthNotifier()
        old_runtime_path = bot_run.RUNTIME_STATE_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            bot_run.RUNTIME_STATE_PATH = Path(tmpdir) / 'runtime_state.json'
            original_strategy_backtester = bot_run.StrategyBacktester
            original_export_calibration_payload = bot_run.export_calibration_payload
            original_execute_adaptive_rollout_orchestration = bot_run.execute_adaptive_rollout_orchestration
            original_build_runtime_orchestration_summary = bot_run.build_runtime_orchestration_summary

            class StubBacktester:
                def __init__(self, config):
                    self.config = config

                def run_all(self, use_cache=True, **kwargs):
                    return {'summary': {'symbols': 1}, 'delivery': {'workflow_ready': {'items': []}}}

            try:
                bot_run.StrategyBacktester = StubBacktester
                bot_run.export_calibration_payload = lambda report, view='workflow_ready': {
                    'schema_version': 'm5_governance_workflow_ready_v2',
                    'summary': {'item_count': 1},
                    'approval_state': {'items': []},
                    'workflow_state': {'item_states': []},
                }
                bot_run.execute_adaptive_rollout_orchestration = lambda payload, db, config=None, replay_source='workflow_ready': {
                    **payload,
                    'adaptive_rollout_orchestration': {
                        'schema_version': 'm5_adaptive_rollout_orchestration_v2',
                        'summary': {
                            'pass_count': 2,
                            'rerun_triggered': True,
                            'rerun_reason': 'post_recovery_state_transition',
                            'rerun_reasons': ['recovery_retry_scheduled', 'recovery_retry_reentered_executor'],
                            'gate_status': 'ready',
                            'gate_blocked': False,
                            'gate_blocking_issues': [],
                            'auto_approval_executed_count': 1,
                            'controlled_rollout_executed_count': 1,
                            'review_queue_queued_count': 1,
                            'recovery_retry_scheduled_count': 1,
                            'recovery_retry_reentered_executor_count': 1,
                            'recovery_rollback_queued_count': 0,
                            'recovery_manual_annotation_count': 0,
                            'testnet_bridge_status': 'disabled',
                            'testnet_bridge_follow_up_required': False,
                        },
                        'passes': [
                            {'label': 'pre_auto_approval', 'dry_run': True, 'rollout_executor_applied_count': 0},
                            {'label': 'post_recovery_queue', 'dry_run': False, 'rollout_executor_applied_count': 1},
                        ],
                    },
                }
                bot_run.build_runtime_orchestration_summary = lambda payload, max_items=5, **kwargs: {
                    'schema_version': 'm5_runtime_orchestration_summary_v1',
                    'headline': {'text': 'runtime orchestration ok'},
                    'summary': {
                        'recent_progress_count': 1,
                        'rerun_observability': {
                            'primary_reason': 'post_recovery_state_transition',
                            'triggered': True,
                            'recovery_triggered': True,
                            'recovery_reasons': ['recovery_retry_scheduled', 'recovery_retry_reentered_executor'],
                            'result_counts': {'rerun_pass_count': 1},
                        },
                    },
                    'next_step': {'action': 'review_followups', 'summary': 'review queued follow-ups'},
                    'stuck_points': [],
                    'follow_ups': {'required': True, 'summary': {'retry_queue_count': 1}},
                }
                result = maybe_run_adaptive_rollout_orchestration(cfg, db, notifier)
                self.assertTrue(result['ran'])
                self.assertEqual(result['summary']['auto_approval_executed_count'], 1)
                state = json.loads(bot_run.RUNTIME_STATE_PATH.read_text())
                self.assertIn('adaptive_rollout_orchestration', state)
                self.assertEqual(state['adaptive_rollout_orchestration']['summary']['controlled_rollout_executed_count'], 1)
                self.assertTrue(state['adaptive_rollout_orchestration']['summary']['recovery_rerun_triggered'])
                self.assertEqual(state['adaptive_rollout_orchestration']['summary']['recovery_retry_reentered_executor_count'], 1)
                self.assertEqual(state['adaptive_rollout_orchestration']['runtime_summary']['rerun_observability']['primary_reason'], 'post_recovery_state_transition')
                self.assertEqual(state['adaptive_rollout_orchestration']['runtime_summary']['next_step']['action'], 'review_followups')
                self.assertTrue(any(call['event_type'] == 'adaptive_rollout_orchestration' for call in notifier.calls))
            finally:
                bot_run.StrategyBacktester = original_strategy_backtester
                bot_run.export_calibration_payload = original_export_calibration_payload
                bot_run.execute_adaptive_rollout_orchestration = original_execute_adaptive_rollout_orchestration
                bot_run.build_runtime_orchestration_summary = original_build_runtime_orchestration_summary
                bot_run.RUNTIME_STATE_PATH = old_runtime_path
        if os.path.exists('data/test_runtime_adaptive_rollout.db'):
            os.remove('data/test_runtime_adaptive_rollout.db')


    def test_maybe_run_adaptive_rollout_orchestration_respects_runtime_cooldown(self):
        import bot.run as bot_run
        cfg = Config()
        cfg._config.setdefault('runtime', {}).setdefault('adaptive_rollout_orchestration', {}).update({
            'enabled': True,
            'use_cache': True,
            'notify_on_activity': True,
            'min_interval_seconds': 900,
            'max_items': 3,
        })
        db = Database('data/test_runtime_adaptive_rollout_cooldown.db')
        notifier = FakeHealthNotifier()
        old_runtime_path = bot_run.RUNTIME_STATE_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            bot_run.RUNTIME_STATE_PATH = Path(tmpdir) / 'runtime_state.json'
            bot_run.RUNTIME_STATE_PATH.write_text(json.dumps({
                'adaptive_rollout_orchestration': {
                    'last_run_at': (datetime.now() - timedelta(seconds=120)).isoformat(),
                }
            }, ensure_ascii=False, indent=2))
            original_strategy_backtester = bot_run.StrategyBacktester
            try:
                class ExplodingBacktester:
                    def __init__(self, config):
                        raise AssertionError('cooldown should skip backtester execution')
                bot_run.StrategyBacktester = ExplodingBacktester
                result = maybe_run_adaptive_rollout_orchestration(cfg, db, notifier)
                self.assertFalse(result['ran'])
                self.assertEqual(result['reason'], 'cooldown_active')
                self.assertGreater(result['remaining_seconds'], 0)
                state = json.loads(bot_run.RUNTIME_STATE_PATH.read_text())
                cooldown = state['adaptive_rollout_orchestration']['cooldown']
                self.assertTrue(cooldown['active'])
                self.assertEqual(cooldown['min_interval_seconds'], 900)
                self.assertEqual(state['adaptive_rollout_orchestration']['last_skip_reason'], 'cooldown_active')
                self.assertEqual(notifier.calls, [])
            finally:
                bot_run.StrategyBacktester = original_strategy_backtester
                bot_run.RUNTIME_STATE_PATH = old_runtime_path
        if os.path.exists('data/test_runtime_adaptive_rollout_cooldown.db'):
            os.remove('data/test_runtime_adaptive_rollout_cooldown.db')

    def test_maybe_run_adaptive_rollout_orchestration_force_bypasses_runtime_cooldown(self):
        import bot.run as bot_run
        cfg = Config()
        cfg._config.setdefault('runtime', {}).setdefault('adaptive_rollout_orchestration', {}).update({
            'enabled': True,
            'use_cache': True,
            'notify_on_activity': True,
            'min_interval_seconds': 900,
            'max_items': 3,
        })
        db = Database('data/test_runtime_adaptive_rollout_force.db')
        notifier = FakeHealthNotifier()
        old_runtime_path = bot_run.RUNTIME_STATE_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            bot_run.RUNTIME_STATE_PATH = Path(tmpdir) / 'runtime_state.json'
            bot_run.RUNTIME_STATE_PATH.write_text(json.dumps({
                'adaptive_rollout_orchestration': {
                    'last_run_at': (datetime.now() - timedelta(seconds=120)).isoformat(),
                }
            }, ensure_ascii=False, indent=2))
            original_strategy_backtester = bot_run.StrategyBacktester
            original_export_calibration_payload = bot_run.export_calibration_payload
            original_execute_adaptive_rollout_orchestration = bot_run.execute_adaptive_rollout_orchestration
            original_build_runtime_orchestration_summary = bot_run.build_runtime_orchestration_summary
            try:
                class StubBacktester:
                    def __init__(self, config):
                        self.config = config
                    def run_all(self, use_cache=True, **kwargs):
                        return {'summary': {'symbols': 1}, 'delivery': {'workflow_ready': {'items': []}}}
                bot_run.StrategyBacktester = StubBacktester
                bot_run.export_calibration_payload = lambda report, view='workflow_ready': {
                    'schema_version': 'm5_governance_workflow_ready_v2',
                    'summary': {'item_count': 1},
                    'approval_state': {'items': []},
                    'workflow_state': {'item_states': []},
                }
                bot_run.execute_adaptive_rollout_orchestration = lambda payload, db, config=None, replay_source='workflow_ready': {
                    **payload,
                    'adaptive_rollout_orchestration': {
                        'schema_version': 'm5_adaptive_rollout_orchestration_v2',
                        'summary': {
                            'pass_count': 1,
                            'rerun_triggered': False,
                            'rerun_reason': None,
                            'gate_status': 'ready',
                            'gate_blocked': False,
                            'gate_blocking_issues': [],
                            'auto_approval_executed_count': 1,
                            'controlled_rollout_executed_count': 0,
                            'review_queue_queued_count': 0,
                            'recovery_retry_scheduled_count': 0,
                            'recovery_rollback_queued_count': 0,
                            'testnet_bridge_status': 'disabled',
                            'testnet_bridge_follow_up_required': False,
                        },
                    },
                }
                bot_run.build_runtime_orchestration_summary = lambda payload, max_items=5, **kwargs: {
                    'schema_version': 'm5_runtime_orchestration_summary_v1',
                    'headline': {'text': 'runtime orchestration ok'},
                    'summary': {'recent_progress_count': 1},
                    'next_step': {'action': 'observe', 'summary': 'observe'},
                    'stuck_points': [],
                    'follow_ups': [],
                }
                result = maybe_run_adaptive_rollout_orchestration(cfg, db, notifier, force=True)
                self.assertTrue(result['ran'])
                state = json.loads(bot_run.RUNTIME_STATE_PATH.read_text())
                self.assertFalse(state['adaptive_rollout_orchestration']['cooldown']['active'])
                self.assertEqual(state['adaptive_rollout_orchestration']['cooldown']['min_interval_seconds'], 900)
            finally:
                bot_run.StrategyBacktester = original_strategy_backtester
                bot_run.export_calibration_payload = original_export_calibration_payload
                bot_run.execute_adaptive_rollout_orchestration = original_execute_adaptive_rollout_orchestration
                bot_run.build_runtime_orchestration_summary = original_build_runtime_orchestration_summary
                bot_run.RUNTIME_STATE_PATH = old_runtime_path
        if os.path.exists('data/test_runtime_adaptive_rollout_force.db'):
            os.remove('data/test_runtime_adaptive_rollout_force.db')

    def test_maybe_send_daily_health_summary_force_sends(self):
        import bot.run as bot_run
        cfg = Config()
        db = Database('data/test_health_summary_force.db')
        notifier = FakeHealthNotifier()
        old_runtime_path = bot_run.RUNTIME_STATE_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            bot_run.RUNTIME_STATE_PATH = Path(tmpdir) / 'runtime_state.json'
            try:
                result = maybe_send_daily_health_summary(cfg, db, notifier, force=True)
                self.assertTrue(result['sent'])
                self.assertEqual(len(notifier.calls), 1)
                state = json.loads(bot_run.RUNTIME_STATE_PATH.read_text())
                self.assertIn('health_summary', state)
            finally:
                bot_run.RUNTIME_STATE_PATH = old_runtime_path
        if os.path.exists('data/test_health_summary_force.db'):
            os.remove('data/test_health_summary_force.db')


class TestRiskManagerLock(unittest.TestCase):
    def test_loss_streak_lock_triggers_and_manual_reset_clears(self):
        cfg = Config()
        cfg._config.setdefault('trading', {}).update({'max_consecutive_losses': 3, 'loss_streak_lock_enabled': True, 'loss_streak_cooldown_hours': 12, 'min_trade_interval': 0, 'max_trades_per_day': 99})
        db = Database('data/test_risk_lock.db')
        try:
            for i in range(3):
                trade_id = db.record_trade(symbol='XRP/USDT', side='long', entry_price=1.5, quantity=100, leverage=10)
                db.close_trade(trade_id, exit_price=1.49, pnl=-1, pnl_percent=-1, notes='loss')
            rm = RiskManager(cfg, db)
            can_open, reason, details = rm.can_open_position('XRP/USDT')
            self.assertFalse(can_open)
            self.assertIn('冷却中', reason)
            self.assertTrue(details['loss_streak_limit']['locked'])
            self.assertTrue(details['loss_streak_limit']['recover_at'])
            state = rm.manual_reset_loss_streak(note='unit-test')
            self.assertEqual(state['current_streak'], 0)
            self.assertFalse(state['lock_active'])
        finally:
            if os.path.exists('data/test_risk_lock.db'):
                os.remove('data/test_risk_lock.db')


class TestTradingExecutor(unittest.TestCase):
    """交易执行器测试"""

    def setUp(self):
        self.config = Config()
        self.db = Database('data/test_executor.db')
        self.executor = TradingExecutor(self.config, None, self.db)
        # Patch balance to avoid real exchange API calls in unit tests
        self.executor._get_balance_summary = lambda: {'total': 10000.0, 'free': 10000.0, 'used': 0.0}
        # Also patch RiskManager class for tests that instantiate it directly
        self._orig_get_balance = RiskManager._get_balance_summary
        RiskManager._get_balance_summary = lambda self: {'total': 10000.0, 'free': 10000.0, 'used': 0.0}

    def tearDown(self):
        import os
        # Restore patched balance method
        RiskManager._get_balance_summary = self._orig_get_balance
        if os.path.exists('data/test_executor.db'):
            os.remove('data/test_executor.db')

    def test_portfolio_status(self):
        """测试投资组合状态"""
        status = self.executor.get_portfolio_status()

        self.assertIn('total_positions', status)
        self.assertIn('trade_stats', status)
        self.assertEqual(status['total_positions'], 0)

    def test_close_position_updates_latest_open_trade(self):
        """测试平仓时会关闭对应 open trade，而不是误用 position.id"""
        self.executor.exchange = FakeExchange(price=51000)
        first_trade_id = self.db.record_trade(
            symbol='BTC/USDT',
            side='long',
            entry_price=50000,
            quantity=0.1,
            leverage=10
        )
        second_trade_id = self.db.record_trade(
            symbol='BTC/USDT',
            side='long',
            entry_price=50500,
            quantity=0.2,
            leverage=10
        )
        self.db.update_position(
            symbol='BTC/USDT',
            side='long',
            entry_price=50500,
            quantity=0.2,
            leverage=10,
            current_price=51000
        )

        closed = self.executor.close_position('BTC/USDT', reason='unit-test', close_price=51000)
        self.assertTrue(closed)

        trades = self.db.get_trades(symbol='BTC/USDT', limit=10)
        latest = next(t for t in trades if t['id'] == second_trade_id)
        earlier = next(t for t in trades if t['id'] == first_trade_id)
        self.assertEqual(latest['status'], 'closed')
        self.assertEqual(earlier['status'], 'open')
        self.assertEqual(len(self.db.get_positions()), 0)
        self.assertEqual(self.executor.exchange.closed_orders[-1]['posSide'], 'long')

    def test_open_position_reduces_amount_after_max_order_error(self):
        """测试遇到 51202 时会自动缩量再试"""
        self.executor.exchange = FakeExecutorExchange()
        trade_id = self.executor.open_position('BTC/USDT', 'long', 50000, signal_id=1)
        self.assertIsNotNone(trade_id)
        self.assertEqual(self.executor.exchange.order_amounts[0], 10.0)
        self.assertEqual(self.executor.exchange.order_amounts[1], 5.0)

    def test_symbol_cooldown_survives_executor_restart_via_db(self):
        self.db.record_trade(symbol='BTC/USDT', side='long', entry_price=50000, quantity=0.1, leverage=10)
        fresh_executor = TradingExecutor(self.config, FakeExecutorExchange(), self.db)
        fresh_executor.trading_config['cooldown_minutes'] = 15
        self.assertFalse(fresh_executor._check_cooldown('BTC/USDT'))

    def test_trailing_stop_tracks_high_for_long(self):
        # 旧行为测试：设置 trailing_activation=None 保持始终激活
        self.executor.trading_config['take_profit'] = 0.5
        self.executor.trading_config['trailing_stop'] = 0.05
        self.executor.trading_config['trailing_activation'] = None
        self.db.update_position(symbol='BTC/USDT', side='long', entry_price=100, quantity=1, leverage=1, current_price=100)
        self.assertFalse(self.executor.check_take_profit('BTC/USDT', 110))
        self.assertEqual(self.executor._trade_cache['BTC/USDT']['highest_price'], 110)
        pos = self.db.get_positions()[0]
        self.assertEqual(pos['peak_price'], 110)
        self.executor._trade_cache.clear()
        self.assertFalse(self.executor.check_take_profit('BTC/USDT', 108))
        self.assertTrue(self.executor.check_take_profit('BTC/USDT', 104))

    def test_trailing_stop_tracks_low_for_short(self):
        # 旧行为测试：设置 trailing_activation=None 保持始终激活
        self.executor.trading_config['take_profit'] = 0.5
        self.executor.trading_config['trailing_stop'] = 0.05
        self.executor.trading_config['trailing_activation'] = None
        self.db.update_position(symbol='BTC/USDT', side='short', entry_price=100, quantity=1, leverage=1, current_price=100)
        self.assertFalse(self.executor.check_take_profit('BTC/USDT', 95))
        self.assertEqual(self.executor._trade_cache['BTC/USDT']['lowest_price'], 95)
        pos = self.db.get_positions()[0]
        self.assertEqual(pos['trough_price'], 95)
        self.executor._trade_cache.clear()
        self.assertFalse(self.executor.check_take_profit('BTC/USDT', 96))
        self.assertTrue(self.executor.check_take_profit('BTC/USDT', 99.8))

    def test_trailing_stop_activation_threshold_long(self):
        """盈利触发型追踪止损 - 多仓"""
        self.executor.trading_config['take_profit'] = 0.5
        self.executor.trading_config['trailing_stop'] = 0.05  # 5%
        self.executor.trading_config['trailing_activation'] = 0.10  # 10% 盈利才激活
        self.db.update_position(symbol='BTC/USDT', side='long', entry_price=100, quantity=1, leverage=1, current_price=100)

        # 5% 盈利，未达到 10% 激活阈值，不触发追踪
        self.assertFalse(self.executor.check_take_profit('BTC/USDT', 105))
        # 但仍然记录最高价
        self.assertEqual(self.executor._trade_cache['BTC/USDT']['highest_price'], 105)

        # 15% 盈利，达到激活阈值，激活追踪
        self.assertFalse(self.executor.check_take_profit('BTC/USDT', 115))

        # 价格回落，但未触及追踪止损
        self.assertFalse(self.executor.check_take_profit('BTC/USDT', 112))

        # 价格继续回落，触发追踪止损 (115 * 0.95 = 109.25)
        self.assertTrue(self.executor.check_take_profit('BTC/USDT', 109))

    def test_trailing_stop_activation_threshold_short(self):
        """盈利触发型追踪止损 - 空仓"""
        self.executor.trading_config['take_profit'] = 0.5
        self.executor.trading_config['trailing_stop'] = 0.05  # 5%
        self.executor.trading_config['trailing_activation'] = 0.10  # 10% 盈利才激活
        self.db.update_position(symbol='BTC/USDT', side='short', entry_price=100, quantity=1, leverage=1, current_price=100)

        # 5% 盈利，未达到 10% 激活阈值，不触发追踪
        self.assertFalse(self.executor.check_take_profit('BTC/USDT', 95))
        self.assertEqual(self.executor._trade_cache['BTC/USDT']['lowest_price'], 95)

        # 15% 盈利，达到激活阈值，激活追踪
        self.assertFalse(self.executor.check_take_profit('BTC/USDT', 85))

        # 价格回升，但未触及追踪止损
        self.assertFalse(self.executor.check_take_profit('BTC/USDT', 88))

        # 价格继续回升，触发追踪止损 (85 * 1.05 = 89.25)
        self.assertTrue(self.executor.check_take_profit('BTC/USDT', 90))

    def test_close_position_auto_reconciles_51169_when_exchange_has_no_position(self):
        db = Database('data/test_close_mismatch.db')
        try:
            db.record_trade(symbol='BTC/USDT', side='long', entry_price=50000, quantity=1, leverage=10)
            db.update_position(symbol='BTC/USDT', side='long', entry_price=50000, quantity=1, leverage=10, current_price=50010)
            ex = CloseMismatchExchangeStub()
            executor = TradingExecutor(self.config, ex, db)
            closed = executor.close_position('BTC/USDT', reason='止盈', close_price=50010)
            self.assertTrue(closed)
            self.assertEqual(db.get_positions(), [])
            trade = db.get_trades(symbol='BTC/USDT', limit=5)[0]
            self.assertEqual(trade['status'], 'closed')
            self.assertIn('自动收口', trade.get('notes') or '')
        finally:
            if os.path.exists('data/test_close_mismatch.db'):
                os.remove('data/test_close_mismatch.db')

    def test_partial_take_profit_triggers_and_closes_half(self):
        """测试部分止盈触发并平掉50%仓位"""
        # 启用部分止盈，阈值 2%，比例 50%
        self.executor.trading_config['partial_tp_enabled'] = True
        self.executor.trading_config['partial_tp_threshold'] = 0.02
        self.executor.trading_config['partial_tp_ratio'] = 0.5
        self.executor.trading_config['take_profit'] = 0.10

        # 设置持仓：10张合约，成本 100
        self.db.update_position(
            symbol='BTC/USDT', side='long', entry_price=100,
            quantity=10, leverage=1, current_price=100,
            coin_quantity=10, contract_size=1
        )
        self.db.record_trade(
            symbol='BTC/USDT', side='long', entry_price=100,
            quantity=10, leverage=1
        )

        # 价格涨到 102（2%涨幅，杠杆后2%盈利），达到部分止盈阈值
        self.executor.exchange = FakeExchange(price=102)

        # 触发部分止盈
        result = self.executor.check_take_profit('BTC/USDT', 102)
        self.assertTrue(result)

        # 验证：仓位应该还剩 5 张
        positions = self.db.get_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]['quantity'], 5)

        # 验证：缓存标记已设置
        self.assertTrue(self.executor._trade_cache['BTC/USDT']['partial_tp_executed'])

        # 再次检查不应该再触发部分止盈（因为已执行过）
        result = self.executor.check_take_profit('BTC/USDT', 102)
        self.assertFalse(result)

    def test_partial_take_profit_disabled_by_default(self):
        """测试默认禁用部分止盈（安全回退）"""
        # 默认不启用部分止盈
        self.executor.trading_config['partial_tp_enabled'] = False
        self.executor.trading_config['take_profit'] = 0.02

        self.db.update_position(
            symbol='BTC/USDT', side='long', entry_price=100,
            quantity=10, leverage=1, current_price=100,
            coin_quantity=10, contract_size=1
        )
        self.db.record_trade(
            symbol='BTC/USDT', side='long', entry_price=100,
            quantity=10, leverage=1
        )

        # 价格涨到 102，触发普通止盈
        self.executor.exchange = FakeExchange(price=102)

        result = self.executor.check_take_profit('BTC/USDT', 102)
        # 应该触发止盈（因为 take_profit=0.02）
        self.assertTrue(result)

        # 实际执行平仓（模拟 runtime 行为）
        self.executor.close_position('BTC/USDT', '止盈', close_price=102)

        # 仓位应该已全部平掉
        positions = self.db.get_positions()
        self.assertEqual(len(positions), 0)

    def test_partial_take_profit_new_position_clears_flag(self):
        """测试新开仓位清除部分止盈标记"""
        # 启用部分止盈
        self.executor.trading_config['partial_tp_enabled'] = True
        self.executor.trading_config['partial_tp_threshold'] = 0.02
        self.executor.trading_config['partial_tp_ratio'] = 0.5

        # 模拟之前已有部分止盈标记
        self.executor._trade_cache['BTC/USDT'] = {'partial_tp_executed': True}

        # 调用 _seed_trailing_anchor（模拟新开仓）
        self.executor._seed_trailing_anchor('BTC/USDT', 'long', 100)

        # 标记应该被清除
        self.assertNotIn('partial_tp_executed', self.executor._trade_cache.get('BTC/USDT', {}))

    def test_close_position_partial_updates_remaining(self):
        """测试部分平仓后更新剩余仓位"""
        self.executor.exchange = FakeExchange(price=110)

        self.db.update_position(
            symbol='BTC/USDT', side='long', entry_price=100,
            quantity=10, leverage=1, current_price=100,
            coin_quantity=10, contract_size=1
        )
        self.db.record_trade(
            symbol='BTC/USDT', side='long', entry_price=100,
            quantity=10, leverage=1
        )

        # 部分平仓 5 张
        result = self.executor.close_position(
            'BTC/USDT', reason='partial_tp', close_price=110, close_quantity=5
        )

        self.assertTrue(result)

        # 验证剩余 5 张
        positions = self.db.get_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]['quantity'], 5)
        self.assertEqual(positions[0]['coin_quantity'], 5)

    def test_risk_manager_observability_passes_through_observe_only_snapshots(self):
        regime_snapshot = build_regime_snapshot('trend', 0.8, {'ema_gap': 0.02, 'ema_direction': 1, 'volatility': 0.01}, '趋势上涨')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        can_open, reason, details = RiskManager(self.config, self.db).can_open_position(
            'BTC/USDT',
            side='long',
            signal_id=123,
            plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot}
        )
        self.assertTrue(can_open)
        self.assertEqual(details['observability']['regime_snapshot']['regime'], 'trend')
        self.assertEqual(details['observability']['adaptive_policy_snapshot']['mode'], 'observe_only')
        self.assertTrue(details['observability']['observe_only_summary'])
        self.assertIn('observe_only', details['observability']['observe_only_tags'])
        self.assertIn('adaptive_risk_snapshot', details['observability'])
        self.assertIn('adaptive_risk_hints', details['observability'])

    def test_prepare_open_execution_step1_emits_execution_hints_without_mutating_live_inputs(self):
        self.config._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': False,
                'layering_profile_enforcement_enabled': False,
                'enforce_conservative_only': True,
            },
            'regimes': {
                'high_vol': {
                    'execution_overrides': {
                        'layer_ratios': [0.05, 0.05, 0.03],
                        'layer_max_total_ratio': 0.13,
                        'min_add_interval_seconds': 600,
                        'profit_only_add': True,
                    }
                }
            }
        }
        self.executor.exchange = FakeExecutorExchange()
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        layered_plan = self.executor._get_layer_plan('BTC/USDT', 'long', signal_id=888)
        layered_plan.update({'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        approved, reason, details = self.executor._prepare_open_execution(
            'BTC/USDT', 'long', 50000, signal_id=888,
            plan_context=layered_plan
        )
        self.assertTrue(approved)
        plan_context = details['plan_context']
        self.assertEqual(plan_context['layer_ratio'], 0.06)
        self.assertEqual(plan_context['layer_ratios'], [0.06, 0.06, 0.04])
        self.assertEqual(plan_context['entry_plan']['effective_entry_margin_ratio'], 0.06)
        hints = plan_context['observability']['adaptive_execution_hints']
        self.assertEqual(hints['effective_state'], 'hints_only')
        self.assertIn('layer_ratios', hints['would_tighten_fields'])
        self.assertIn('min_add_interval_seconds', hints['would_tighten_fields'])
        self.assertIn('profit_only_add', hints['would_tighten_fields'])
        self.assertEqual(hints['effective_hint']['layer_ratios'], [0.05, 0.05, 0.03])
        self.assertEqual(hints['baseline']['layer_ratios'], [0.06, 0.06, 0.04])
        self.assertFalse(hints['execution_profile_really_enforced'])
        json.dumps(plan_context['observability'], ensure_ascii=False)

    def test_prepare_open_execution_step2_enforces_guardrails_but_keeps_layer_ratios_baseline(self):
        self.config._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'enforce_conservative_only': True,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {
                'high_vol': {
                    'execution_overrides': {
                        'layer_ratios': [0.05, 0.05, 0.03],
                        'layer_max_total_ratio': 0.13,
                        'max_layers_per_signal': 1,
                        'min_add_interval_seconds': 600,
                        'profit_only_add': True,
                    }
                }
            }
        }
        self.executor.exchange = FakeExecutorExchange()
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        layered_plan = self.executor._get_layer_plan('BTC/USDT', 'long', signal_id=889, plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        layered_plan.update({'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        approved, reason, details = self.executor._prepare_open_execution(
            'BTC/USDT', 'long', 50000, signal_id=889,
            plan_context=layered_plan
        )
        self.assertTrue(approved)
        plan_context = details['plan_context']
        self.assertEqual(plan_context['layer_ratio'], 0.06)
        self.assertEqual(plan_context['layer_ratios'], [0.06, 0.06, 0.04])
        self.assertEqual(plan_context['max_total_ratio'], 0.13)
        hints = plan_context['observability']['adaptive_execution_hints']
        self.assertTrue(hints['execution_profile_really_enforced'])
        self.assertEqual(hints['enforced_profile']['layer_max_total_ratio'], 0.13)
        self.assertEqual(hints['enforced_profile']['max_layers_per_signal'], 1)
        self.assertEqual(hints['enforced_profile']['min_add_interval_seconds'], 600)
        self.assertTrue(hints['enforced_profile']['profit_only_add'])
        self.assertEqual(hints['enforced_profile']['layer_ratios'], [0.06, 0.06, 0.04])
        self.assertIn('layer_max_total_ratio', hints['enforced_fields'])
        self.assertNotIn('layer_ratios', hints['enforced_fields'])

    def test_prepare_open_execution_step3_default_safe_keeps_layering_live_off(self):
        self.config._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_hints_enabled': True,
                'layering_profile_enforcement_enabled': False,
                'layering_plan_shape_enforcement_enabled': False,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {
                'high_vol': {'execution_overrides': {'layer_max_total_ratio': 0.13, 'min_add_interval_seconds': 600, 'profit_only_add': True}}
            }
        }
        self.executor.exchange = FakeExecutorExchange()
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        layered_plan = self.executor._get_layer_plan('BTC/USDT', 'long', signal_id=891, plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        layered_plan.update({'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        approved, reason, details = self.executor._prepare_open_execution('BTC/USDT', 'long', 50000, signal_id=891, plan_context=layered_plan)
        self.assertTrue(approved)
        hints = details['plan_context']['observability']['adaptive_execution_hints']
        self.assertFalse(hints['layering_profile_really_enforced'])
        self.assertFalse(hints['plan_shape_really_enforced'])
        self.assertEqual(hints['live'], hints['baseline'])
        self.assertIn('layer_max_total_ratio', hints['hinted_only_fields'])

    def test_prepare_open_execution_step3_enforces_guarded_layering_fields(self):
        self.config._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_hints_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': False,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {
                'high_vol': {
                    'execution_overrides': {
                        'layer_ratios': [0.05, 0.05, 0.03],
                        'layer_max_total_ratio': 0.13,
                        'max_layers_per_signal': 1,
                        'min_add_interval_seconds': 600,
                        'profit_only_add': True,
                    }
                }
            }
        }
        self.executor.exchange = FakeExecutorExchange()
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        layered_plan = self.executor._get_layer_plan('BTC/USDT', 'long', signal_id=892, plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        layered_plan.update({'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        approved, reason, details = self.executor._prepare_open_execution('BTC/USDT', 'long', 50000, signal_id=892, plan_context=layered_plan)
        self.assertTrue(approved)
        plan_context = details['plan_context']
        hints = plan_context['observability']['adaptive_execution_hints']
        self.assertTrue(hints['layering_profile_really_enforced'])
        self.assertFalse(hints['plan_shape_really_enforced'])
        self.assertEqual(plan_context['max_total_ratio'], 0.13)
        self.assertEqual(plan_context['layer_ratios'], [0.06, 0.06, 0.04])
        self.assertIn('layer_max_total_ratio', hints['layering_enforced_fields'])
        self.assertIn('layer_ratios', hints['hinted_only_fields'])

    def test_prepare_open_execution_step3_rollout_miss_falls_back_to_baseline_layering(self):
        self.config._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': False,
                'rollout_symbols': ['ETH/USDT'],
            },
            'regimes': {
                'high_vol': {'execution_overrides': {'layer_max_total_ratio': 0.13, 'min_add_interval_seconds': 600}}
            }
        }
        self.executor.exchange = FakeExecutorExchange()
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        layered_plan = self.executor._get_layer_plan('BTC/USDT', 'long', signal_id=893, plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        layered_plan.update({'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        approved, reason, details = self.executor._prepare_open_execution('BTC/USDT', 'long', 50000, signal_id=893, plan_context=layered_plan)
        self.assertTrue(approved)
        hints = details['plan_context']['observability']['adaptive_execution_hints']
        self.assertFalse(hints['rollout_match'])
        self.assertEqual(hints['live'], hints['baseline'])
        self.assertFalse(hints['layering_profile_really_enforced'])

    def test_executor_step4_batch2_uses_live_layer_ratios_only_when_shape_enforcement_enabled(self):
        self.config._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': True,
                'layering_plan_shape_rollout_symbols': ['BTC/USDT'],
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {'high_vol': {'execution_overrides': {'layer_ratios': [0.05, 0.05], 'layer_max_total_ratio': 0.13, 'max_layers_per_signal': 1, 'min_add_interval_seconds': 600, 'profit_only_add': True}}}
        }
        self.executor.exchange = FakeExecutorExchange()
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        layered_plan = self.executor._get_layer_plan('BTC/USDT', 'long', signal_id=894, plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        layered_plan.update({'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        approved, _, details = self.executor._prepare_open_execution('BTC/USDT', 'long', 50000, signal_id=894, plan_context=layered_plan)
        self.assertTrue(approved)
        plan_context = details['plan_context']
        hints = plan_context['observability']['adaptive_execution_hints']
        self.assertEqual(plan_context['layer_ratios'], [0.05, 0.05])
        self.assertEqual(plan_context['layer_ratio'], 0.05)
        self.assertEqual(plan_context['layer_count'], 2)
        self.assertTrue(hints['plan_shape_really_enforced'])
        self.assertEqual(hints['live_layer_shape_source'], 'adaptive_live')
        self.assertIn('layer_ratios', hints['plan_shape_enforced_fields'])

    def test_executor_step4_batch2_falls_back_to_baseline_shape_on_rollout_miss(self):
        self.config._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': True,
                'layering_plan_shape_rollout_symbols': ['ETH/USDT'],
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {'high_vol': {'execution_overrides': {'layer_ratios': [0.05, 0.05], 'layer_max_total_ratio': 0.13, 'max_layers_per_signal': 1, 'min_add_interval_seconds': 600, 'profit_only_add': True}}}
        }
        self.executor.exchange = FakeExecutorExchange()
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        layered_plan = self.executor._get_layer_plan('BTC/USDT', 'long', signal_id=895, plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        layered_plan.update({'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        approved, _, details = self.executor._prepare_open_execution('BTC/USDT', 'long', 50000, signal_id=895, plan_context=layered_plan)
        self.assertTrue(approved)
        plan_context = details['plan_context']
        hints = plan_context['observability']['adaptive_execution_hints']
        self.assertEqual(plan_context['layer_ratios'], [0.06, 0.06, 0.04])
        self.assertFalse(hints['plan_shape_really_enforced'])
        self.assertFalse(hints['shape_live_rollout_match'])
        self.assertEqual(hints['plan_shape_validation']['reason'], 'plan_shape_rollout_miss')

    def test_executor_step4_batch2_preserves_guardrails_when_shape_is_live(self):
        self.config._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': True,
                'layering_plan_shape_rollout_fraction': 1.0,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {'high_vol': {'execution_overrides': {'layer_ratios': [0.05, 0.05], 'layer_max_total_ratio': 0.13, 'max_layers_per_signal': 1, 'min_add_interval_seconds': 600, 'profit_only_add': True}}}
        }
        self.executor.exchange = FakeExecutorExchange()
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        layered_plan = self.executor._get_layer_plan('BTC/USDT', 'long', signal_id=896, plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        layered_plan.update({'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        approved, _, details = self.executor._prepare_open_execution('BTC/USDT', 'long', 50000, signal_id=896, plan_context=layered_plan)
        self.assertTrue(approved)
        plan_context = details['plan_context']
        hints = plan_context['observability']['adaptive_execution_hints']
        self.assertEqual(plan_context['layer_ratios'], [0.05, 0.05])
        self.assertEqual(plan_context['max_total_ratio'], 0.13)
        self.assertEqual(hints['live']['max_layers_per_signal'], 1)
        self.assertEqual(hints['live']['min_add_interval_seconds'], 600)
        self.assertTrue(hints['live']['profit_only_add'])

    def test_executor_step4_batch2_does_not_mutate_lock_or_intent_semantics(self):
        self.config._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': True,
                'layering_plan_shape_rollout_fraction': 1.0,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {'high_vol': {'execution_overrides': {'layer_ratios': [0.05, 0.05], 'layer_max_total_ratio': 0.13, 'max_layers_per_signal': 1, 'min_add_interval_seconds': 600, 'profit_only_add': True}}}
        }
        self.executor.exchange = FakeExecutorExchange()
        lock_symbol, lock_side = self.executor._get_direction_lock_scope_key('BTC/USDT', 'long')
        self.db.acquire_direction_lock(lock_symbol, lock_side, owner='existing-lock')
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        layered_plan = self.executor._get_layer_plan('BTC/USDT', 'long', signal_id=897, plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        layered_plan.update({'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        approved, reason, details = self.executor._prepare_open_execution('BTC/USDT', 'long', 50000, signal_id=897, plan_context=layered_plan)
        self.assertFalse(approved)
        self.assertIn('方向锁', reason)
        self.assertEqual(details['lock']['owner'], 'existing-lock')

    def test_execution_profile_rollout_guard_keeps_live_execution_on_baseline(self):
        self.config._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': False,
                'rollout_symbols': ['ETH/USDT'],
            },
            'regimes': {
                'high_vol': {
                    'execution_overrides': {
                        'layer_max_total_ratio': 0.13,
                        'min_add_interval_seconds': 600,
                        'profit_only_add': True,
                    }
                }
            }
        }
        self.executor.exchange = FakeExecutorExchange()
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        layered_plan = self.executor._get_layer_plan('BTC/USDT', 'long', signal_id=890, plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        layered_plan.update({'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot})
        approved, reason, details = self.executor._prepare_open_execution('BTC/USDT', 'long', 50000, signal_id=890, plan_context=layered_plan)
        self.assertTrue(approved)
        plan_context = details['plan_context']
        self.assertEqual(plan_context['max_total_ratio'], 0.16)
        hints = plan_context['observability']['adaptive_execution_hints']
        self.assertFalse(hints['rollout_match'])
        self.assertFalse(hints['execution_profile_really_enforced'])
        self.assertEqual(hints['enforced_profile']['layer_max_total_ratio'], 0.16)

    def test_risk_manager_observability_contains_risk_hints_only_snapshot(self):
        self.config._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'risk_hints_enabled': True,
                'enforce_conservative_only': True,
            },
            'regimes': {
                'high_vol': {
                    'risk_overrides': {
                        'base_entry_margin_ratio': 0.05,
                        'symbol_margin_cap_ratio': 0.10,
                        'leverage_cap': 5,
                    }
                }
            }
        }
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.04}, '高波动')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        can_open, reason, details = RiskManager(self.config, self.db).can_open_position(
            'BTC/USDT',
            side='long',
            signal_id=124,
            plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot}
        )
        self.assertTrue(can_open)
        self.assertEqual(details['adaptive_risk_snapshot']['effective_state'], 'hints_only')
        self.assertTrue(details['adaptive_risk_hints']['would_tighten'])
        self.assertIn('base_entry_margin_ratio', details['adaptive_risk_hints']['would_tighten_fields'])
        self.assertIn('WOULD_TIGHTEN_BASE_ENTRY_MARGIN_RATIO', details['adaptive_risk_hints']['hint_codes'])


class TestRiskBudgetSizing(unittest.TestCase):
    def setUp(self):
        # Disable local config override to ensure test isolation
        import os
        self._old_enable = os.environ.pop('CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL', None)
        self._old_path = os.environ.pop('CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG', None)

        # Also temporarily move local config to avoid loading it
        import shutil
        from pathlib import Path
        self._local_config_path = Path('config/config.local.yaml')
        self._backup_path = Path('config/config.local.yaml.test_backup')
        if self._local_config_path.exists():
            shutil.move(str(self._local_config_path), str(self._backup_path))

        self.config = Config()

    def _make_signal(self, strength=50, strategies=None):
        from signals import Signal
        return Signal(
            symbol='BTC/USDT',
            signal_type='buy',
            price=50000,
            strength=strength,
            reasons=[],
            strategies_triggered=strategies or [],
        )

    def tearDown(self):
        import os
        # Restore local config
        import shutil
        from pathlib import Path
        if self._backup_path.exists():
            shutil.move(str(self._backup_path), str(self._local_config_path))

        if self._old_enable is not None:
            os.environ['CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL'] = self._old_enable
        if self._old_path is not None:
            os.environ['CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG'] = self._old_path

    def test_compute_entry_plan_respects_soft_cap_and_min_floor(self):
        cfg = Config()
        risk_budget = get_risk_budget_config(cfg)
        plan = compute_entry_plan(
            total_balance=1000,
            free_balance=600,
            current_total_margin=260,
            current_symbol_margin=20,
            risk_budget=risk_budget,
        )
        self.assertFalse(plan['blocked'])
        self.assertAlmostEqual(plan['effective_entry_margin_ratio'], 0.04, places=4)
        self.assertTrue(plan['soft_cap_reached'])

    def test_compute_entry_plan_blocks_when_remaining_budget_below_min_entry(self):
        cfg = Config()
        risk_budget = get_risk_budget_config(cfg)
        plan = compute_entry_plan(
            total_balance=1000,
            free_balance=500,
            current_total_margin=290,
            current_symbol_margin=20,
            risk_budget=risk_budget,
        )
        self.assertTrue(plan['blocked'])
        self.assertIn('最小开仓门槛', plan['block_reason'])


    def test_derive_quality_bucket_high_strength(self):
        """Signal with strength >= 60 returns high bucket."""
        from signals import Signal
        from core.risk_budget import derive_quality_bucket
        sig = Signal(symbol='BTC/USDT', signal_type='buy', price=100, strength=65, reasons=[], strategies_triggered=[])
        self.assertEqual(derive_quality_bucket(sig), 'high')

    def test_derive_quality_bucket_high_strategies(self):
        """Signal with >= 3 strategies returns high bucket regardless of strength."""
        from signals import Signal
        from core.risk_budget import derive_quality_bucket
        sig = Signal(symbol='BTC/USDT', signal_type='buy', price=100, strength=50, reasons=[], strategies_triggered=['a', 'b', 'c'])
        self.assertEqual(derive_quality_bucket(sig), 'high')

    def test_derive_quality_bucket_low_strength_one_strategy(self):
        """Signal with strength <= 30 and <= 1 strategies returns low bucket."""
        from signals import Signal
        from core.risk_budget import derive_quality_bucket
        sig = Signal(symbol='BTC/USDT', signal_type='buy', price=100, strength=25, reasons=[], strategies_triggered=['momentum'])
        self.assertEqual(derive_quality_bucket(sig), 'low')

    def test_derive_quality_bucket_normal_middle(self):
        """Signal with strength between 30-60 and 2 strategies returns normal."""
        from signals import Signal
        from core.risk_budget import derive_quality_bucket
        sig = Signal(symbol='BTC/USDT', signal_type='buy', price=100, strength=45, reasons=[], strategies_triggered=['a', 'b'])
        self.assertEqual(derive_quality_bucket(sig), 'normal')

    def test_derive_quality_bucket_none_signal(self):
        """No signal object returns normal bucket."""
        from core.risk_budget import derive_quality_bucket
        self.assertEqual(derive_quality_bucket(None), 'normal')

    def test_compute_entry_plan_quality_scaling_high_boosts_ratio(self):
        """High quality signal gets high_quality_multiplier applied to entry ratio."""
        plan = compute_entry_plan(
            total_balance=10000,
            free_balance=8000,
            current_total_margin=0,
            current_symbol_margin=0,
            risk_budget={
                'base_entry_margin_ratio': 0.08,
                'min_entry_margin_ratio': 0.04,
                'max_entry_margin_ratio': 0.10,
                'total_margin_cap_ratio': 0.30,
                'symbol_margin_cap_ratio': 0.12,
                'total_margin_soft_cap_ratio': 0.25,
                'quality_scaling_enabled': True,
                'high_quality_multiplier': 1.15,
                'low_quality_multiplier': 0.75,
            },
            signal=self._make_signal(strength=65, strategies=['a', 'b', 'c']),
        )
        self.assertEqual(plan['quality_bucket'], 'high')
        self.assertAlmostEqual(plan['quality_multiplier'], 1.15, places=4)
        self.assertFalse(plan['blocked'])
        self.assertFalse(plan['soft_cap_reached'])
        # target ratio: 0.08 * 1.15 = 0.092
        self.assertAlmostEqual(plan['target_entry_margin_ratio'], 0.092, places=3)

    def test_compute_entry_plan_quality_scaling_low_reduces_ratio(self):
        """Low quality signal gets low_quality_multiplier applied to entry ratio."""
        plan = compute_entry_plan(
            total_balance=10000,
            free_balance=8000,
            current_total_margin=0,
            current_symbol_margin=0,
            risk_budget={
                'base_entry_margin_ratio': 0.08,
                'min_entry_margin_ratio': 0.04,
                'max_entry_margin_ratio': 0.10,
                'total_margin_cap_ratio': 0.30,
                'symbol_margin_cap_ratio': 0.12,
                'total_margin_soft_cap_ratio': 0.25,
                'quality_scaling_enabled': True,
                'high_quality_multiplier': 1.15,
                'low_quality_multiplier': 0.75,
            },
            signal=self._make_signal(strength=25, strategies=['a']),
        )
        self.assertEqual(plan['quality_bucket'], 'low')
        self.assertAlmostEqual(plan['quality_multiplier'], 0.75, places=4)
        self.assertFalse(plan['blocked'])
        # target ratio: 0.08 * 0.75 = 0.06
        self.assertAlmostEqual(plan['target_entry_margin_ratio'], 0.06, places=3)

    def test_compute_entry_plan_quality_scaling_disabled_uses_base(self):
        """Quality scaling disabled ignores multiplier, uses base ratio."""
        plan = compute_entry_plan(
            total_balance=10000,
            free_balance=8000,
            current_total_margin=0,
            current_symbol_margin=0,
            risk_budget={
                'base_entry_margin_ratio': 0.08,
                'min_entry_margin_ratio': 0.04,
                'max_entry_margin_ratio': 0.10,
                'total_margin_cap_ratio': 0.30,
                'symbol_margin_cap_ratio': 0.12,
                'total_margin_soft_cap_ratio': 0.25,
                'quality_scaling_enabled': False,
                'high_quality_multiplier': 1.15,
                'low_quality_multiplier': 0.75,
            },
            signal=self._make_signal(strength=65, strategies=['a', 'b', 'c']),
        )
        self.assertEqual(plan['quality_bucket'], 'high')
        self.assertAlmostEqual(plan['quality_multiplier'], 1.0, places=4)
        self.assertAlmostEqual(plan['target_entry_margin_ratio'], 0.08, places=3)

    def test_compute_entry_plan_quality_scaling_high_capped_by_max(self):
        """High quality multiplier respecting max_entry_margin_ratio cap."""
        plan = compute_entry_plan(
            total_balance=10000,
            free_balance=8000,
            current_total_margin=0,
            current_symbol_margin=0,
            risk_budget={
                'base_entry_margin_ratio': 0.10,
                'min_entry_margin_ratio': 0.04,
                'max_entry_margin_ratio': 0.10,
                'total_margin_cap_ratio': 0.30,
                'symbol_margin_cap_ratio': 0.12,
                'total_margin_soft_cap_ratio': 0.25,
                'quality_scaling_enabled': True,
                'high_quality_multiplier': 1.15,
                'low_quality_multiplier': 0.75,
            },
            signal=self._make_signal(strength=65, strategies=['a', 'b', 'c']),
        )
        self.assertEqual(plan['quality_bucket'], 'high')
        # 0.10 * 1.15 = 0.115, but max is 0.10, so capped to 0.10
        self.assertAlmostEqual(plan['target_entry_margin_ratio'], 0.10, places=3)

    def test_compute_entry_plan_quality_scaling_soft_cap_still_applies(self):
        """Soft cap override still applies after quality multiplier."""
        plan = compute_entry_plan(
            total_balance=10000,
            free_balance=8000,
            current_total_margin=2600,
            current_symbol_margin=0,
            risk_budget={
                'base_entry_margin_ratio': 0.08,
                'min_entry_margin_ratio': 0.04,
                'max_entry_margin_ratio': 0.10,
                'total_margin_cap_ratio': 0.30,
                'symbol_margin_cap_ratio': 0.12,
                'total_margin_soft_cap_ratio': 0.25,
                'quality_scaling_enabled': True,
                'high_quality_multiplier': 1.15,
                'low_quality_multiplier': 0.75,
            },
            signal=self._make_signal(strength=65, strategies=['a', 'b', 'c']),
        )
        # 0.08 * 1.15 = 0.092, but soft cap reached (2600/10000=0.26 > 0.25)
        # should be capped to min 0.04
        self.assertTrue(plan['soft_cap_reached'])
        self.assertAlmostEqual(plan['target_entry_margin_ratio'], 0.04, places=3)

    def test_compute_entry_plan_normal_quality_no_multiplier(self):
        """Normal quality signal gets multiplier 1.0."""
        plan = compute_entry_plan(
            total_balance=10000,
            free_balance=8000,
            current_total_margin=0,
            current_symbol_margin=0,
            risk_budget={
                'base_entry_margin_ratio': 0.08,
                'min_entry_margin_ratio': 0.04,
                'max_entry_margin_ratio': 0.10,
                'total_margin_cap_ratio': 0.30,
                'symbol_margin_cap_ratio': 0.12,
                'total_margin_soft_cap_ratio': 0.25,
                'quality_scaling_enabled': True,
                'high_quality_multiplier': 1.15,
                'low_quality_multiplier': 0.75,
            },
            signal=self._make_signal(strength=45, strategies=['a', 'b']),
        )
        self.assertEqual(plan['quality_bucket'], 'normal')
        self.assertAlmostEqual(plan['quality_multiplier'], 1.0, places=4)
        self.assertAlmostEqual(plan['target_entry_margin_ratio'], 0.08, places=3)


    def test_build_risk_effective_snapshot_defaults_to_disabled_hints(self):
        snapshot = build_risk_effective_snapshot(self.config if hasattr(self, 'config') else Config(), 'BTC/USDT')
        self.assertEqual(snapshot['effective_state'], 'disabled')
        self.assertTrue(snapshot['observe_only'])
        self.assertEqual(snapshot['baseline']['base_entry_margin_ratio'], snapshot['effective']['base_entry_margin_ratio'])
        self.assertEqual(snapshot['applied_overrides'], {})

    def test_build_risk_effective_snapshot_conservative_only_merge_and_ignored_reasons(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'risk_hints_enabled': True,
                'enforce_conservative_only': True,
            },
            'defaults': {'policy_version': 'adaptive_policy_v1_m3_step3'},
            'regimes': {
                'high_vol': {
                    'risk_overrides': {
                        'base_entry_margin_ratio': 0.05,
                        'symbol_margin_cap_ratio': 0.10,
                        'leverage_cap': 5,
                        'total_margin_cap_ratio': 0.35,
                    }
                }
            }
        }
        signal = Signal(symbol='BTC/USDT', signal_type='buy', price=100, strength=55, reasons=[], strategies_triggered=['x'])
        signal.market_context = {'regime': 'high_vol', 'regime_confidence': 0.9}
        snapshot = build_risk_effective_snapshot(cfg, 'BTC/USDT', signal=signal)
        self.assertEqual(snapshot['effective_state'], 'hints_only')
        self.assertTrue(snapshot['enabled'])
        self.assertAlmostEqual(snapshot['effective']['base_entry_margin_ratio'], 0.05, places=6)
        self.assertAlmostEqual(snapshot['effective']['symbol_margin_cap_ratio'], 0.10, places=6)
        self.assertEqual(snapshot['effective']['leverage_cap'], 5)
        self.assertIn('base_entry_margin_ratio', snapshot['applied_overrides'])
        self.assertTrue(any(row['key'] == 'total_margin_cap_ratio' for row in snapshot['ignored_overrides']))
        self.assertIn('IGNORED_NON_CONSERVATIVE_OVERRIDE', snapshot['hint_codes'])

    def test_build_risk_effective_snapshot_step4_enforcement_rolls_out_safely(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'risk_hints_enabled': True,
                'risk_enforcement_enabled': True,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {
                'high_vol': {
                    'risk_overrides': {
                        'base_entry_margin_ratio': 0.05,
                        'symbol_margin_cap_ratio': 0.10,
                        'leverage_cap': 5,
                    }
                }
            }
        }
        signal = Signal(symbol='BTC/USDT', signal_type='buy', price=100, strength=55, reasons=[], strategies_triggered=['x'])
        signal.market_context = {'regime': 'high_vol', 'regime_confidence': 0.9}
        snapshot = build_risk_effective_snapshot(cfg, 'BTC/USDT', signal=signal)
        self.assertEqual(snapshot['effective_state'], 'effective')
        self.assertFalse(snapshot['observe_only'])
        self.assertEqual(snapshot['enforced_fields'], ['symbol_margin_cap_ratio', 'base_entry_margin_ratio', 'leverage_cap'])
        self.assertTrue(any(row['field'] == 'base_entry_margin_ratio' and row['enforced'] for row in snapshot['field_decisions']))

    def test_build_execution_effective_snapshot_is_serializable(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'enforce_conservative_only': True,
            },
            'regimes': {
                'high_vol': {
                    'execution_overrides': {
                        'min_add_interval_seconds': 999,
                        'profit_only_add': True,
                    }
                }
            }
        }
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        snapshot = build_execution_effective_snapshot(
            cfg, 'BTC/USDT', regime_snapshot=regime_snapshot, policy_snapshot=resolve_regime_policy(cfg, 'BTC/USDT', regime_snapshot)
        )
        self.assertEqual(snapshot['effective_state'], 'hints_only')
        self.assertIn('min_add_interval_seconds', snapshot['would_tighten_fields'])
        json.dumps(snapshot, ensure_ascii=False)

    def test_summarize_risk_hint_changes_is_serializable(self):
        summary = summarize_risk_hint_changes(
            {'base_entry_margin_ratio': 0.08, 'leverage_cap': 10},
            {'base_entry_margin_ratio': 0.05, 'leverage_cap': 5},
        )
        self.assertTrue(summary['would_tighten'])
        self.assertIn('base_entry_margin_ratio', summary['would_tighten_fields'])
        self.assertIn('WOULD_TIGHTEN_BASE_ENTRY_MARGIN_RATIO', summary['hint_codes'])
        payload = json.dumps(summary, ensure_ascii=False)
        self.assertIn('leverage_cap', payload)


class TestRiskManager(unittest.TestCase):
    """风险管理器测试"""

    def setUp(self):
        import os
        import shutil
        from pathlib import Path
        self._old_enable = os.environ.pop('CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL', None)
        self._old_path = os.environ.pop('CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG', None)

        # Temporarily move local config
        self._local_config_path = Path('config/config.local.yaml')
        self._backup_path = Path('config/config.local.yaml.test_backup')
        if self._local_config_path.exists():
            shutil.move(str(self._local_config_path), str(self._backup_path))

        self.config = Config()
        self.db = Database('data/test_risk.db')
        self.risk_mgr = RiskManager(self.config, self.db)
        # Patch balance to avoid real exchange API calls in unit tests
        self.risk_mgr._get_balance_summary = lambda: {'total': 10000.0, 'free': 10000.0, 'used': 0.0}

    def tearDown(self):
        import os
        import shutil
        from pathlib import Path
        # Restore local config
        if self._backup_path.exists():
            shutil.move(str(self._backup_path), str(self._local_config_path))

        if self._old_enable is not None:
            os.environ['CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL'] = self._old_enable
        if self._old_path is not None:
            os.environ['CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG'] = self._old_path

        if os.path.exists('data/test_risk.db'):
            os.remove('data/test_risk.db')

    def test_can_open_position(self):
        """测试开仓检查"""
        can_open, reason, details = self.risk_mgr.can_open_position('BTC/USDT')

        self.assertTrue(can_open)
        self.assertIsNone(reason)

    def test_today_trade_count_uses_open_time(self):
        self.db.record_trade(symbol='BTC/USDT', side='long', entry_price=50000, quantity=0.1, leverage=10)
        self.assertEqual(self.risk_mgr._get_today_trade_count(), 1)

    def test_last_trade_time_reads_from_db(self):
        self.db.record_trade(symbol='BTC/USDT', side='long', entry_price=50000, quantity=0.1, leverage=10)
        self.assertIsNotNone(self.risk_mgr._get_last_trade_time())

    def test_daily_drawdown_prefers_close_time(self):
        trade_id = self.db.record_trade(symbol='BTC/USDT', side='long', entry_price=50000, quantity=0.1, leverage=10)
        self.db.close_trade(trade_id=trade_id, exit_price=49000, pnl=-100, pnl_percent=-2, notes='unit-test')
        ratio = self.risk_mgr._get_daily_drawdown_ratio()
        self.assertGreater(ratio, 0)

class TestRiskManagerBudgetDetails(unittest.TestCase):
    def setUp(self):
        import os
        import shutil
        from pathlib import Path
        self._old_enable = os.environ.pop('CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL', None)
        self._old_path = os.environ.pop('CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG', None)

        # Temporarily move local config
        self._local_config_path = Path('config/config.local.yaml')
        self._backup_path = Path('config/config.local.yaml.test_backup')
        if self._local_config_path.exists():
            shutil.move(str(self._local_config_path), str(self._backup_path))

        self.config = Config()
        self.db = Database('data/test_risk_budget_details.db')
        self.risk_mgr = RiskManager(self.config, self.db)
        # Patch balance to avoid real exchange API calls in unit tests
        self.risk_mgr._get_balance_summary = lambda: {'total': 10000.0, 'free': 10000.0, 'used': 0.0}
    
    def _make_signal(self, **overrides):
        from signals import Signal
        defaults = {
            'symbol': 'BTC/USDT',
            'signal_type': 'buy',
            'price': 50000,
            'strength': 55,
            'reasons': ['test signal'],
            'strategies_triggered': ['trend_follow', 'momentum'],
        }
        defaults.update(overrides)
        return Signal(**defaults)

    def tearDown(self):
        import os
        import shutil
        from pathlib import Path
        # Restore local config
        if self._backup_path.exists():
            shutil.move(str(self._backup_path), str(self._local_config_path))

        if self._old_enable is not None:
            os.environ['CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL'] = self._old_enable
        if self._old_path is not None:
            os.environ['CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG'] = self._old_path

        if os.path.exists('data/test_risk_budget_details.db'):
            os.remove('data/test_risk_budget_details.db')

    def test_can_open_position_returns_entry_plan(self):
        can_open, reason, details = self.risk_mgr.can_open_position('BTC/USDT', signal_id=2001)
        self.assertTrue(can_open)
        self.assertIn('entry_plan', details['exposure_limit'])
        self.assertIn('position_ratio', details['exposure_limit'])


    def test_can_open_position_step4_keeps_default_observe_only(self):
        can_open, reason, details = self.risk_mgr.can_open_position('BTC/USDT', side='long', signal_id=2002)
        self.assertTrue(can_open)
        self.assertEqual(details['adaptive_risk_snapshot']['effective_state'], 'disabled')
        self.assertEqual(details['exposure_limit']['entry_plan']['risk_budget']['base_entry_margin_ratio'], 0.08)

    def test_can_open_position_step4_enforces_tighter_budget_when_rollout_matches(self):
        self.config._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'risk_hints_enabled': True,
                'risk_enforcement_enabled': True,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {
                'high_vol': {
                    'risk_overrides': {
                        'base_entry_margin_ratio': 0.05,
                        'symbol_margin_cap_ratio': 0.10,
                        'leverage_cap': 5,
                        'total_margin_cap_ratio': 0.20,
                    }
                }
            }
        }
        self.config._config.setdefault('trading', {})['leverage'] = 10
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        can_open, reason, details = self.risk_mgr.can_open_position(
            'BTC/USDT', side='long', signal_id=501,
            plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot}
        )
        self.assertTrue(can_open)
        self.assertEqual(details['adaptive_risk_snapshot']['effective_state'], 'effective')
        self.assertAlmostEqual(details['exposure_limit']['entry_plan']['risk_budget']['base_entry_margin_ratio'], 0.05, places=6)
        self.assertEqual(details['exposure_limit']['planned_leverage'], 5)
        self.assertEqual(details['adaptive_risk_hints']['enforced_fields'], ['total_margin_cap_ratio', 'symbol_margin_cap_ratio', 'base_entry_margin_ratio', 'leverage_cap'])

    def test_can_open_position_step4_never_relaxes_baseline(self):
        self.config._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'risk_hints_enabled': True,
                'risk_enforcement_enabled': True,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {
                'high_vol': {
                    'risk_overrides': {
                        'base_entry_margin_ratio': 0.12,
                        'symbol_margin_cap_ratio': 0.20,
                        'leverage_cap': 20,
                    }
                }
            }
        }
        self.config._config.setdefault('trading', {})['leverage'] = 10
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        can_open, reason, details = self.risk_mgr.can_open_position(
            'BTC/USDT', side='long', signal_id=502,
            plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot}
        )
        self.assertTrue(can_open)
        self.assertEqual(details['adaptive_risk_snapshot']['effective_state'], 'effective')
        self.assertAlmostEqual(details['exposure_limit']['entry_plan']['risk_budget']['base_entry_margin_ratio'], 0.08, places=6)
        self.assertEqual(details['exposure_limit']['planned_leverage'], 10)
        self.assertTrue(any(row['key'] == 'base_entry_margin_ratio' and row['reason'] == 'non_conservative_override' for row in details['adaptive_risk_snapshot']['ignored_overrides']))

    def test_can_open_position_step4_rollout_miss_stays_hints_only(self):
        self.config._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'risk_hints_enabled': True,
                'risk_enforcement_enabled': True,
                'rollout_symbols': ['ETH/USDT'],
            },
            'regimes': {
                'high_vol': {
                    'risk_overrides': {'base_entry_margin_ratio': 0.05}
                }
            }
        }
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        can_open, reason, details = self.risk_mgr.can_open_position(
            'BTC/USDT', side='long', signal_id=503,
            plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot}
        )
        self.assertTrue(can_open)
        self.assertEqual(details['adaptive_risk_snapshot']['effective_state'], 'hints_only')
        self.assertAlmostEqual(details['exposure_limit']['entry_plan']['risk_budget']['base_entry_margin_ratio'], 0.08, places=6)
        self.assertFalse(details['adaptive_risk_hints']['rollout_match'])



class TestLayerPlanAndIntents(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.db = Database('data/test_layer_plan.db')
        self.risk_mgr = RiskManager(self.config, self.db)

    def tearDown(self):
        if os.path.exists('data/test_layer_plan.db'):
            os.remove('data/test_layer_plan.db')

    def test_open_intents_are_counted_in_margin_usage(self):
        self.db.create_open_intent(symbol='BTC/USDT', side='long', signal_id=101, planned_margin=60, leverage=3)
        usage = summarize_margin_usage([], 'BTC/USDT', pending_intents=self.db.get_active_open_intents())
        self.assertEqual(usage['current_total_margin'], 60)
        self.assertEqual(usage['current_symbol_margin'], 60)

    def test_can_open_position_blocks_duplicate_signal_id(self):
        self.db.create_open_intent(symbol='BTC/USDT', side='long', signal_id=999, planned_margin=60, leverage=3)
        can_open, reason, details = self.risk_mgr.can_open_position('BTC/USDT', side='long', signal_id=999)
        self.assertFalse(can_open)
        self.assertIn('signal_id', reason)
        self.assertFalse(details['hard_intercept']['passed'])

    def test_layer_plan_progresses_to_second_layer(self):
        self.db.save_layer_plan_state(
            'BTC/USDT', 'long', status='active', current_layer=1, root_signal_id=1,
            plan_data={'filled_layers': [1], 'pending_layers': [], 'layer_ratios': [0.06, 0.06, 0.04], 'max_total_ratio': 0.16}
        )
        with patch.object(RiskManager, '_get_balance_summary', return_value={'total': 1000.0, 'free': 1000.0, 'used': 0.0}):
            can_open, reason, details = self.risk_mgr.can_open_position('BTC/USDT', side='long', signal_id=2)
        self.assertTrue(can_open)
        self.assertEqual(details['layer_eligibility']['layer_plan']['layer_no'], 2)
        self.assertAlmostEqual(details['layer_eligibility']['layer_plan']['layer_ratio'], 0.06, places=4)

    def test_sync_layer_plan_state_from_open_trade_and_intent(self):
        self.db.record_trade('BTC/USDT', 'long', 50000, 2, leverage=3, signal_id=1001, layer_no=1, root_signal_id=1001)
        self.db.create_open_intent(symbol='BTC/USDT', side='long', signal_id=1002, root_signal_id=1001, planned_margin=60, leverage=3, layer_no=2, status='pending')
        state = self.db.sync_layer_plan_state('BTC/USDT', 'long')
        self.assertEqual(state['status'], 'active')
        self.assertEqual(state['current_layer'], 1)
        self.assertEqual(state['plan_data']['filled_layers'], [1])
        self.assertEqual(state['plan_data']['pending_layers'], [2])
        self.assertEqual(state['root_signal_id'], 1001)

    def test_cleanup_orphan_execution_state_resets_flat_plan(self):
        self.db.create_open_intent(symbol='BTC/USDT', side='long', signal_id=2001, planned_margin=60, leverage=3, layer_no=1, status='pending')
        self.db.acquire_direction_lock('BTC/USDT', 'long', owner='test-lock')
        self.db.save_layer_plan_state('BTC/USDT', 'long', status='pending', current_layer=0, root_signal_id=2001, plan_data={'filled_layers': [], 'pending_layers': [1], 'layer_ratios': [0.06, 0.06, 0.04], 'max_total_ratio': 0.16})
        conn = self.db._get_connection()
        conn.execute("UPDATE open_intents SET updated_at = datetime('now', '-30 minutes')")
        conn.execute("UPDATE direction_locks SET updated_at = datetime('now', '-30 minutes')")
        conn.commit()
        conn.close()
        report = self.db.cleanup_orphan_execution_state(stale_after_minutes=15)
        self.assertEqual(len(report['removed_intents']), 1)
        self.assertEqual(len(report['removed_locks']), 1)
        self.assertEqual(len(report['plan_resets']), 1)
        state = self.db.get_layer_plan_state('BTC/USDT', 'long')
        self.assertEqual(state['status'], 'idle')
        self.assertEqual(state['plan_data']['filled_layers'], [])
        self.assertEqual(state['plan_data']['pending_layers'], [])

    def test_cleanup_orphan_execution_state_heals_stale_intent_when_position_exists(self):
        self.db.record_trade('BTC/USDT', 'long', 50000, 2, leverage=3, signal_id=2101, layer_no=1, root_signal_id=2101)
        self.db.update_position('BTC/USDT', 'long', 50000, 2, leverage=3, current_price=50100)
        self.db.create_open_intent(symbol='BTC/USDT', side='long', signal_id=2102, root_signal_id=2101, planned_margin=60, leverage=3, layer_no=2, status='submitted')
        conn = self.db._get_connection()
        conn.execute("UPDATE open_intents SET updated_at = datetime('now', '-30 minutes')")
        conn.commit()
        conn.close()
        report = self.db.cleanup_orphan_execution_state(stale_after_minutes=15)
        self.assertEqual(len(report['removed_intents']), 0)
        self.assertEqual(len(report['healed_intents']), 1)
        self.assertEqual(self.db.get_active_open_intents('BTC/USDT', 'long'), [])
        state = self.db.get_layer_plan_state('BTC/USDT', 'long')
        self.assertEqual(state['status'], 'active')
        self.assertEqual(state['plan_data']['filled_layers'], [1])
        self.assertEqual(state['plan_data']['pending_layers'], [])

    def test_cleanup_orphan_execution_state_heals_stale_lock_after_fill(self):
        self.db.record_trade('BTC/USDT', 'long', 50000, 2, leverage=3, signal_id=2201, layer_no=1, root_signal_id=2201)
        self.db.update_position('BTC/USDT', 'long', 50000, 2, leverage=3, current_price=50200)
        self.db.acquire_direction_lock('BTC/USDT', 'long', owner='stale-owner')
        conn = self.db._get_connection()
        conn.execute("UPDATE direction_locks SET updated_at = datetime('now', '-30 minutes')")
        conn.commit()
        conn.close()
        report = self.db.cleanup_orphan_execution_state(stale_after_minutes=15)
        self.assertEqual(len(report['removed_locks']), 0)
        self.assertEqual(len(report['healed_locks']), 1)
        self.assertIsNone(self.db.get_direction_lock('BTC/USDT', 'long'))

class TestDashboardRiskBudgetAPI(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_config_form_contains_risk_budget_fields(self):
        resp = self.client.get('/api/config/form')
        data = resp.get_json()
        self.assertTrue(data['success'])
        trading_fields = data['data']['trading']['fields']
        self.assertIn('trading.total_margin_cap_ratio', trading_fields)
        self.assertIn('trading.base_entry_margin_ratio', trading_fields)
        self.assertIn('trading.add_position_enabled', trading_fields)

    def test_sizing_preview_exposes_entry_plan(self):
        resp = self.client.get('/api/risk/sizing-preview?symbol=BTC/USDT&side=long')
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertIn('entry_plan', data['data'])
        self.assertIn('soft_exposure', data['data']['config'])

    def test_execution_state_endpoint_exposes_observability(self):
        resp = self.client.get('/api/system/execution-state')
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertIn('active_intents', data['data'])
        self.assertIn('direction_locks', data['data'])
        self.assertIn('layer_plans', data['data'])
        self.assertIn('summary', data['data'])


class TestMFEAnalyzer(unittest.TestCase):
    """MFE/MAE 分析器测试"""

    def test_sample_data_report(self):
        """测试示例数据报告生成"""
        from analytics.mfe_mae import MFEAnalyzer

        analyzer = MFEAnalyzer(db=None)
        report = analyzer.generate_analysis_report()

        self.assertEqual(report['status'], 'sample')
        self.assertIn('stop_loss', report)
        self.assertIn('take_profit', report)
        self.assertIn('trailing_stop', report)
        self.assertIsNotNone(report['stop_loss']['recommended_sl_pct'])
        self.assertIsNotNone(report['take_profit']['recommended_tp_pct'])

    def test_calculate_mfe_mae_from_positions(self):
        """测试从持仓计算 MFE/MAE"""
        from analytics.mfe_mae import MFEAnalyzer

        analyzer = MFEAnalyzer(db=None)

        # 测试多头持仓
        positions = [
            {'symbol': 'BTC/USDT', 'side': 'long', 'entry_price': 50000,
             'peak_price': 55000, 'trough_price': 48000, 'coin_quantity': 0.1},
        ]

        results = analyzer.calculate_mfe_mae_from_positions(positions)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['mfe_pct'], 10.0)  # (55000-50000)/50000 * 100
        self.assertEqual(results[0]['mae_pct'], 4.0)   # (50000-48000)/50000 * 100

        # 测试空头持仓
        short_positions = [
            {'symbol': 'ETH/USDT', 'side': 'short', 'entry_price': 3000,
             'peak_price': 3200, 'trough_price': 2800, 'coin_quantity': 1},
        ]

        results = analyzer.calculate_mfe_mae_from_positions(short_positions)

        self.assertEqual(results[0]['mfe_pct'], 6.67)  # (3000-2800)/3000 * 100
        self.assertEqual(results[0]['mae_pct'], 6.67)  # (3200-3000)/3000 * 100

    def test_stop_loss_recommendation(self):
        """测试止损建议计算"""
        from analytics.mfe_mae import MFEAnalyzer

        analyzer = MFEAnalyzer(db=None)

        # 测试样本不足
        result = analyzer.get_stop_loss_recommendation([1, 2])
        self.assertIsNone(result['recommended_sl_pct'])
        self.assertIn('样本不足', result['reason'])

        # 测试足够样本
        mae_pcts = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        result = analyzer.get_stop_loss_recommendation(mae_pcts)

        self.assertIsNotNone(result['recommended_sl_pct'])
        self.assertEqual(result['sample_size'], 10)
        # 75% 分位数应该是 7.5% 左右
        self.assertAlmostEqual(result['recommended_sl_pct'], 7.5, delta=1)

    def test_take_profit_recommendation(self):
        """测试止盈建议计算"""
        from analytics.mfe_mae import MFEAnalyzer

        analyzer = MFEAnalyzer(db=None)

        mfe_pcts = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0]
        result = analyzer.get_take_profit_recommendation(mfe_pcts)

        self.assertIsNotNone(result['recommended_tp_pct'])
        self.assertEqual(result['sample_size'], 10)
        # 50% 分位数应该是 12%（索引5，即第6个元素）
        self.assertAlmostEqual(result['recommended_tp_pct'], 12.0, delta=1)

    def test_trailing_stop_suggestion(self):
        """测试追踪止损建议"""
        from analytics.mfe_mae import MFEAnalyzer

        analyzer = MFEAnalyzer(db=None)

        mfe_pcts = [4.0, 8.0, 12.0]
        mae_pcts = [2.0, 4.0, 6.0]

        result = analyzer.get_trailing_stop_suggestion(mfe_pcts, mae_pcts)

        self.assertIsNotNone(result['activation_threshold_pct'])
        self.assertIsNotNone(result['trailing_distance_pct'])
        # avg_mfe = 8, 50% = 4
        self.assertEqual(result['activation_threshold_pct'], 4.0)
        # avg_mae = 4
        self.assertEqual(result['trailing_distance_pct'], 4.0)

    def test_api_endpoint_sample(self):
        """测试 API 端点（示例数据场景）"""
        from dashboard.api import app

        with app.test_client() as client:
            resp = client.get('/api/trades/mfe-mae')
            self.assertEqual(resp.status_code, 200)

            data = json.loads(resp.data)
            # 因为没有足够数据，应该返回示例
            self.assertIn('status', data)

    def test_api_recommendations_endpoint(self):
        """测试建议端点"""
        from dashboard.api import app

        with app.test_client() as client:
            resp = client.get('/api/trades/mfe-mae/recommendations')
            self.assertEqual(resp.status_code, 200)

            data = json.loads(resp.data)
            self.assertIn('stop_loss', data)
            self.assertIn('take_profit', data)
            self.assertIn('trailing_stop', data)


class TestRecommendationProvider(unittest.TestCase):
    """MFE/MAE 建议提供者测试"""

    def test_default_values_no_db(self):
        """测试无数据库时的默认值"""
        from analytics.recommendation import RecommendationProvider

        provider = RecommendationProvider(db=None, config=None)

        self.assertEqual(provider.get_stop_loss(), 0.02)
        self.assertEqual(provider.get_take_profit(), 0.04)
        self.assertIsNotNone(provider.get_trailing_stop())

    def test_fallback_with_insufficient_data(self):
        """测试样本不足时的回退"""
        from analytics.recommendation import RecommendationProvider

        # 使用真实 db 但样本不足
        test_db = Database('data/trading.db')
        provider = RecommendationProvider(db=test_db, config=None)

        rec = provider.get_recommendations_for_symbol('BTC/USDT')

        # 应该回退到默认值
        self.assertTrue(rec.get('is_fallback'))
        self.assertEqual(rec.get('source'), 'default')

    def test_get_all_recommendations(self):
        """测试获取完整建议"""
        from analytics.recommendation import RecommendationProvider

        provider = RecommendationProvider(db=None, config=None)
        result = provider.get_all_recommendations()

        self.assertIn('_meta', result)
        self.assertIn('defaults', result['_meta'])
        self.assertEqual(result['_meta']['defaults']['stop_loss'], 0.02)

    def test_trading_executor_integration(self):
        """测试交易执行器集成"""
        from core.config import Config
        from core.database import Database
        from trading.executor import TradingExecutor

        # 使用内存数据库测试
        test_db = Database(':memory:')

        # Mock exchange
        class MockExchange:
            pass

        config = Config()
        executor = TradingExecutor(config, MockExchange(), test_db)

        # 验证 recommendation provider 已初始化
        self.assertIsNotNone(executor._recommendation_provider)


class TestBacktestObserveOnlyTags(unittest.TestCase):
    def test_backtest_result_reserves_regime_and_policy_tag_outputs(self):
        cfg = Config()
        backtester = StrategyBacktester(cfg)
        data = pd.DataFrame({
            'timestamp': list(range(180)),
            'datetime': pd.date_range('2024-01-01', periods=180, freq='1h'),
            'open': np.linspace(100, 140, 180),
            'high': np.linspace(101, 141, 180),
            'low': np.linspace(99, 139, 180),
            'close': np.linspace(100, 140, 180) + np.sin(np.linspace(0, 10, 180)),
            'volume': np.linspace(1000, 2000, 180),
        })
        result = backtester._run_symbol('BTC/USDT', data)
        self.assertIn('regime_tags', result)
        self.assertIn('policy_tags', result)
        self.assertIn('observe_only_tags', result)
        self.assertIn('all_trades', result)
        self.assertIn('regime_policy_calibration', result)
        if result['recent_trades']:
            trade = result['recent_trades'][-1]
            self.assertIn('observe_only', trade)
            self.assertIn('snapshots', trade['observe_only'])
            self.assertIn('regime_snapshot', trade['observe_only']['snapshots'])
            self.assertIn('adaptive_policy_snapshot', trade['observe_only']['snapshots'])
            self.assertTrue(trade['observe_only']['summary'])
            self.assertIn('observe_only', trade['observe_only']['tags'])

    def test_backtest_aggregate_summary_includes_observe_only_banner(self):
        cfg = Config()
        backtester = StrategyBacktester(cfg)
        summary = backtester._aggregate_results([
            {
                'symbol': 'BTC/USDT',
                'trades': 2,
                'wins': 1,
                'losses': 1,
                'win_rate': 50.0,
                'total_return_pct': 3.2,
                'avg_return_pct': 1.6,
                'max_drawdown_pct': -1.1,
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 2.4},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v1', 'return_pct': 0.8},
                ],
                'recent_trades': [],
                'regime_tags': ['trend'],
                'policy_tags': ['adaptive_policy_v1_m1'],
                'observe_only_tags': ['observe_only', 'adaptive_regime'],
            }
        ])
        self.assertTrue(summary['summary']['observe_only'])
        self.assertIn('observe-only', summary['summary']['observe_only_banner'])
        self.assertIn('observe_only', summary['summary']['observe_only_tags'])
        self.assertIn('observe_only_summary_view', summary['summary'])
        self.assertIn('top_tags', summary['summary']['observe_only_summary_view'])
        self.assertIn('trend', summary['summary']['regime_tags'])
        self.assertIn('adaptive_policy_v1_m1', summary['summary']['policy_tags'])
        self.assertIn('calibration_report', summary)
        self.assertTrue(summary['summary']['calibration_ready'])


    def test_signal_quality_summarize_includes_observe_only_summary_view(self):
        from analytics.backtest import SignalQualityAnalyzer
        analyzer = SignalQualityAnalyzer.__new__(SignalQualityAnalyzer)
        summary = analyzer._summarize([
            {
                'symbol': 'BTC/USDT',
                'created_at': '2026-03-29T10:00:00',
                'avg_quality_pct': 1.25,
                'recent_trades': [
                    {
                        'observe_only': {
                            'summary': 'high_vol[up] conf=0.88 stable=0.35 risk=0.84 | policy=adaptive_policy_v1_m4_testnet_live state=effective',
                            'tags': ['observe_only', 'adaptive_regime', 'regime:high_vol', 'adaptive_policy'],
                            'snapshots': {
                                'regime_snapshot': {'name': 'high_vol', 'family': 'vol', 'direction': 'up', 'confidence': 0.88},
                                'adaptive_policy_snapshot': {'mode': 'guarded_execute', 'policy_version': 'adaptive_policy_v1_m4_testnet_live', 'policy_source': 'adaptive_regime.defaults', 'state': 'effective'},
                            },
                        }
                    }
                ],
            }
        ])
        self.assertIn('observe_only_summary_view', summary['summary'])
        self.assertEqual(summary['summary']['signals_scored'], 1)
        self.assertEqual(summary['summary']['observe_only_summary_view']['count'], 1)
        self.assertTrue(any('observe_only' in row['value'] for row in summary['summary']['observe_only_summary_view']['top_tags']))


class TestRegimePolicyCalibrationReport(unittest.TestCase):
    def test_calibration_report_groups_trades_by_regime_and_policy(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 3.0},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 1.0},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': -2.0},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': -1.0},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': 0.5},
                ],
            }
        ])
        self.assertTrue(report['summary']['calibration_ready'])
        self.assertEqual(report['summary']['trade_count'], 5)
        by_regime = {row['bucket']: row for row in report['by_regime']}
        by_policy = {row['bucket']: row for row in report['by_policy_version']}
        pair = {(row['bucket'], row['secondary_bucket']): row for row in report['by_regime_policy']}
        self.assertEqual(by_regime['trend_up']['trade_count'], 2)
        self.assertEqual(by_policy['policy_v2']['trade_count'], 3)
        self.assertAlmostEqual(pair[('range', 'policy_v2')]['avg_return_pct'], -0.8333, places=4)
        recommendation = next(item for item in report['recommendations'] if item['regime'] == 'range' and item['policy_version'] == 'policy_v2')
        self.assertIn('priority', recommendation)
        self.assertIn('confidence', recommendation)
        self.assertIn('suggested_action', recommendation)
        self.assertIn('aligned_with_rollout_gate', recommendation)
        self.assertIn('actions', recommendation)
        self.assertIn('rollout_plan', recommendation)
        self.assertIn('summary_line', recommendation)

    def test_calibration_report_marks_sample_gap_when_bucket_is_too_small(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'ETH/USDT',
                'all_trades': [
                    {'regime_tag': 'high_vol', 'policy_tag': 'policy_v3', 'return_pct': 1.2},
                ],
            }
        ])
        self.assertEqual(report['summary']['trade_count'], 1)
        rec = next(item for item in report['recommendations'] if item['regime'] == 'high_vol' and item['policy_version'] == 'policy_v3')
        self.assertEqual(rec['type'], 'collect_more_samples')
        self.assertEqual(rec['blocking_issue'], 'insufficient_sample')
        self.assertEqual(rec['gate_decision'], 'hold')
        self.assertEqual(rec['governance_mode'], 'observe')
        self.assertEqual(rec['rollout_plan']['mode'], 'freeze')
        self.assertEqual(rec['actions'][0]['type'], 'collect_more_samples')
        self.assertEqual(report['rollout_gates'][0]['decision'], 'hold')
        self.assertEqual(report['rollout_gates'][0]['reason'], 'sample_gap')

    def test_calibration_report_exposes_strategy_fit_views(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['MACD', 'Volume'], 'return_pct': 1.4},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['MACD'], 'return_pct': 1.1},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['RSI'], 'return_pct': -0.4},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'strategy_tags': ['RSI', 'Bollinger'], 'return_pct': 0.8},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'strategy_tags': ['Bollinger'], 'return_pct': 0.7},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'strategy_tags': ['RSI'], 'return_pct': -0.2},
                ],
            }
        ])
        self.assertTrue(report['summary']['strategy_fit_ready'])
        self.assertEqual(report['summary']['strategies'], 4)
        self.assertEqual(report['summary']['top_strategy'], 'RSI')
        by_strategy = {row['bucket']: row for row in report['by_strategy']}
        self.assertEqual(by_strategy['RSI']['trade_count'], 3)
        regime_fit = {row['regime']: row for row in report['strategy_fit']['regime_strategy_fit']}
        self.assertEqual(regime_fit['trend_up']['best_strategy'], 'Volume')
        self.assertEqual(regime_fit['range']['best_strategy'], 'Bollinger')
        self.assertEqual(regime_fit['trend_up']['worst_strategy'], 'RSI')
        self.assertEqual(report['delivery']['views']['tables']['by_strategy'], report['by_strategy'])
        self.assertEqual(report['delivery']['views']['tables']['regime_strategy_fit'], report['strategy_fit']['regime_strategy_fit'])
        self.assertEqual(report['delivery']['render_ready']['headline']['top_strategy'], 'RSI')

    def test_calibration_report_builds_policy_ab_diff_and_rollout_gate(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.5},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.7},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.4},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.5},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.0},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': -2.0},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': -1.5},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': -1.0},
                ],
            }
        ])
        self.assertTrue(report['summary']['policy_ab_ready'])
        self.assertIn('rollout_gate_summary', report['summary'])
        self.assertEqual(report['summary']['rollout_gate_summary']['expand'], 2)
        self.assertEqual(report['summary']['rollout_gate_summary']['rollback'], 1)

        diffs = {row['candidate_policy_version']: row for row in report['policy_ab_diffs']}
        self.assertIn('policy_v1', diffs)
        self.assertEqual(diffs['policy_v1']['baseline_policy_version'], 'policy_v2')
        self.assertAlmostEqual(diffs['policy_v1']['delta_avg_return_pct'], 0.6666, places=4)
        trend_delta = {item['regime']: item for item in diffs['policy_v1']['regime_deltas']}
        self.assertAlmostEqual(trend_delta['trend_up']['delta_avg_return_pct'], -0.7, places=4)

        gates = {(row['regime'], row['policy_version']): row for row in report['rollout_gates']}
        self.assertEqual(gates[('trend_up', 'policy_v2')]['decision'], 'expand')
        self.assertEqual(gates[('range', 'policy_v2')]['decision'], 'rollback')

    def test_calibration_report_builds_expand_recommendation_aligned_with_ab_advantage(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.4},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.3},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.1},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.1},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.0},
                ],
            }
        ])
        rec = next(item for item in report['recommendations'] if item['regime'] == 'trend_up' and item['policy_version'] == 'policy_v2')
        self.assertEqual(rec['type'], 'expand_guarded')
        self.assertEqual(rec['gate_decision'], 'expand')
        self.assertEqual(rec['category'], 'validated_edge')
        self.assertIsNone(rec['blocking_issue'])
        self.assertEqual(rec['governance_mode'], 'rollout')
        self.assertEqual(rec['rollout_plan']['max_rollout_pct'], 35)
        self.assertTrue(rec['aligned_with_rollout_gate'])
        self.assertTrue(rec['evidence']['baseline_comparison']['candidate_beats_baseline'])

    def test_calibration_report_builds_tighten_recommendation_when_underperforming_vs_baseline(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 1.1},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.9},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.7},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.8},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': -0.5},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 0.1},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 0.2},
                ],
            }
        ])
        rec = next(item for item in report['recommendations'] if item['regime'] == 'trend_up' and item['policy_version'] == 'policy_v2')
        self.assertEqual(rec['type'], 'tighten_thresholds')
        self.assertEqual(rec['priority'], 'high')
        self.assertEqual(rec['gate_decision'], 'tighten')
        self.assertEqual(rec['blocking_issue'], 'negative_return')
        self.assertEqual(rec['governance_mode'], 'tighten')
        self.assertEqual(rec['actions'][0]['type'], 'repricing_review')
        self.assertLess(rec['evidence']['baseline_comparison']['delta_avg_return_pct'], 0)

    def test_calibration_report_marks_instability_for_positive_but_low_win_rate_hold(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.1},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.3},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': 2.0},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': -0.8},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': -0.4},
                ],
            }
        ])
        rec = next(item for item in report['recommendations'] if item['regime'] == 'range' and item['policy_version'] == 'policy_v2')
        self.assertEqual(rec['gate_decision'], 'hold')
        self.assertEqual(rec['type'], 'repricing_review')
        self.assertEqual(rec['category'], 'instability')
        self.assertEqual(rec['blocking_issue'], 'mixed_signal')
        self.assertEqual(rec['governance_mode'], 'review')
        self.assertEqual(rec['actions'][0]['type'], 'repricing_review')

    def test_calibration_report_uses_freeze_recommendation_for_rollback_gate(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_down', 'policy_tag': 'policy_v1', 'return_pct': 0.3},
                    {'regime_tag': 'trend_down', 'policy_tag': 'policy_v1', 'return_pct': 0.2},
                    {'regime_tag': 'trend_down', 'policy_tag': 'policy_v1', 'return_pct': 0.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'return_pct': -2.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'return_pct': -1.8},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'return_pct': -1.1},
                ],
            }
        ])
        rec = next(item for item in report['recommendations'] if item['regime'] == 'panic' and item['policy_version'] == 'policy_v2')
        self.assertEqual(rec['gate_decision'], 'rollback')
        self.assertEqual(rec['type'], 'rollout_freeze')
        self.assertEqual(rec['priority'], 'critical')
        self.assertEqual(rec['governance_mode'], 'rollback')
        self.assertEqual(rec['actions'][0]['type'], 'rollout_freeze')
        self.assertEqual(rec['rollout_plan']['mode'], 'rollback')

    def test_calibration_report_summary_exposes_governance_breakdown(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.3},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.1},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.1},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.0},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 0.9},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': -1.2},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': -0.8},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': 0.2},
                ],
            }
        ])
        summary = report['summary']['recommendation_summary']
        self.assertIn('by_type', summary)
        self.assertIn('by_governance_mode', summary)
        self.assertIn('top_actions', summary)
        self.assertGreaterEqual(summary['by_type']['expand_guarded'], 1)
        self.assertGreaterEqual(summary['by_governance_mode']['rollout'], 1)
        self.assertGreaterEqual(summary['top_actions']['repricing_review'], 1)
        self.assertTrue(summary['top_priority_items'])
        self.assertIn('summary_line', summary['top_priority_items'][0])

    def test_calibration_report_builds_strategy_governance_recommendations_and_summary(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 1.4},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 1.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': 1.1},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['MeanRevert'], 'return_pct': -0.7},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['MeanRevert'], 'return_pct': -0.5},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['MeanRevert'], 'return_pct': -0.4},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'strategy_tags': ['Scalp'], 'return_pct': 0.4},
                ],
            }
        ])
        strategy_fit = report['strategy_fit']
        self.assertIn('strategy_recommendations', strategy_fit)
        self.assertIn('strategy_governance', strategy_fit)
        breakout = next(item for item in strategy_fit['strategy_recommendations'] if item['regime'] == 'trend_up' and item['strategy'] == 'Breakout')
        mean_revert = next(item for item in strategy_fit['strategy_recommendations'] if item['regime'] == 'trend_up' and item['strategy'] == 'MeanRevert')
        scalp = next(item for item in strategy_fit['strategy_recommendations'] if item['regime'] == 'range' and item['strategy'] == 'Scalp')
        self.assertEqual(breakout['type'], 'expand_guarded')
        self.assertEqual(breakout['governance_mode'], 'rollout')
        self.assertEqual(mean_revert['type'], 'rollout_freeze')
        self.assertEqual(mean_revert['blocking_issue'], 'strategy_negative_return_and_low_win_rate')
        self.assertEqual(mean_revert['actions'][1]['type'], 'deweight_strategy')
        self.assertEqual(scalp['type'], 'collect_more_samples')
        strategy_summary = report['summary']['strategy_governance_summary']
        self.assertGreaterEqual(strategy_summary['by_type']['expand_guarded'], 1)
        self.assertGreaterEqual(strategy_summary['by_type']['rollout_freeze'], 1)
        self.assertGreaterEqual(strategy_summary['by_governance_mode']['observe'], 1)
        self.assertGreaterEqual(strategy_summary['top_actions']['collect_more_samples'], 1)

    def test_calibration_report_exposes_delivery_payload_for_render_and_orchestration(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.4},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.3},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.1},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.0},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': -1.0},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': -0.8},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'return_pct': 0.3},
                ],
            }
        ])
        delivery = report['delivery']
        self.assertEqual(delivery['schema_version'], 'm5_delivery_v1')
        self.assertEqual(delivery['summary']['bucket_count'], len(report['by_regime_policy']))
        self.assertEqual(delivery['views']['tables']['rollout_gates'], report['rollout_gates'])
        self.assertEqual(delivery['views']['tables']['recommendations'], report['recommendations'])
        self.assertTrue(delivery['render_ready']['sections']['priority_queue'])
        self.assertTrue(delivery['orchestration_ready']['queue'])
        first_item = delivery['views']['items'][0]
        self.assertIn('bucket_id', first_item)
        self.assertIn('metrics', first_item)
        self.assertIn('gate', first_item)
        self.assertIn('recommendation', first_item)
        self.assertTrue(first_item['status']['ready_for_rollout_orchestration'])
        self.assertIn('orchestration', first_item)
        self.assertTrue(first_item['orchestration']['action_queue'])
        self.assertIn('review_checkpoints', first_item['orchestration'])
        self.assertIn('stage_model', first_item['orchestration'])
        self.assertIn('queue_progression', first_item['orchestration'])
        self.assertIn('execution_window', first_item['orchestration'])
        first_queue = delivery['orchestration_ready']['queue'][0]
        self.assertIn('primary_action', first_queue)
        self.assertIn('next_actions', first_queue)
        self.assertIn('blocking_chain', first_queue)
        self.assertIn('rollback_candidate', first_queue)
        self.assertIn('stage_model', first_queue)
        self.assertIn('queue_progression', first_queue)
        self.assertIn('execution_window', first_queue)
        self.assertIn(first_queue['decision'], {'expand', 'tighten', 'rollback', 'hold'})
        self.assertIn('expand', delivery['orchestration_ready']['queues'])
        self.assertIn('rollback', delivery['orchestration_ready']['queues'])
        self.assertTrue(delivery['orchestration_ready']['prioritized_queue'])
        self.assertTrue(delivery['orchestration_ready']['next_actions'])
        self.assertTrue(delivery['orchestration_ready']['review_checkpoints'])
        self.assertIn('repricing_review', delivery['orchestration_ready']['action_catalog'])
        self.assertTrue(delivery['orchestration_ready']['stage_transitions'])
        self.assertTrue(delivery['orchestration_ready']['queue_progression'])
        self.assertTrue(delivery['orchestration_ready']['execution_windows'])
        self.assertEqual(report['summary']['delivery_ready']['schema_version'], 'm5_delivery_v1')
        self.assertGreaterEqual(report['summary']['delivery_ready']['priority_queue_size'], 1)
        self.assertGreaterEqual(report['summary']['delivery_ready']['next_action_bucket_count'], 1)

    def test_calibration_report_orchestration_ready_exposes_ordered_actions_and_dependencies(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'return_pct': 0.05},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'return_pct': -1.5},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'return_pct': -0.8},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v3', 'return_pct': 0.4},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v3', 'return_pct': 0.3},
                ],
            }
        ])
        delivery = report['delivery']
        rollback_item = next(item for item in delivery['views']['items'] if item['regime'] == 'panic' and item['policy_version'] == 'policy_v2')
        action_queue = rollback_item['orchestration']['action_queue']
        self.assertEqual(action_queue[0]['type'], 'rollout_freeze')
        self.assertEqual(action_queue[1]['type'], 'rollback_to_baseline')
        self.assertEqual(action_queue[1]['depends_on'], [action_queue[0]['id']])
        self.assertEqual(action_queue[2]['type'], 'repricing_review')
        self.assertEqual(action_queue[2]['depends_on'], [action_queue[1]['id']])
        self.assertTrue(rollback_item['orchestration']['rollback_candidate']['eligible'])

        sample_gap_item = next(item for item in delivery['views']['items'] if item['regime'] == 'range' and item['policy_version'] == 'policy_v3')
        sample_gap_actions = sample_gap_item['orchestration']['action_queue']
        self.assertEqual(sample_gap_actions[0]['type'], 'collect_more_samples')
        self.assertIn('missing_1_samples', [row['value'] for row in sample_gap_item['orchestration']['blocking_chain']])

        next_actions = delivery['orchestration_ready']['next_actions']
        rollback_next = next(row for row in next_actions if row['bucket_id'] == rollback_item['bucket_id'])
        self.assertEqual(rollback_next['next_actions'][0]['type'], 'rollout_freeze')
        blocking_chain = delivery['orchestration_ready']['blocking_chain']
        self.assertTrue(any(row['bucket_id'] == sample_gap_item['bucket_id'] for row in blocking_chain))
        review_row = next(row for row in delivery['orchestration_ready']['review_checkpoints'] if row['bucket_id'] == rollback_item['bucket_id'])
        self.assertTrue(any(checkpoint['type'] == 'trade_count' for checkpoint in review_row['checkpoints']))
        self.assertTrue(any(checkpoint['type'] == 'thresholds' for checkpoint in review_row['checkpoints']))
        self.assertIn('scheduled_review', review_row)
        self.assertEqual(rollback_item['orchestration']['stage_model']['target_stage'], 'rollback_prepare')
        self.assertIn(rollback_item['orchestration']['queue_progression']['state'], {'ready', 'blocked'})
        rollback_candidates = delivery['orchestration_ready']['rollback_candidates']
        self.assertTrue(any(row['bucket_id'] == rollback_item['bucket_id'] for row in rollback_candidates))

    def test_calibration_report_builds_joint_governance_and_conflict_resolution(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.4},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': 1.4},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': 1.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': 1.1},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['MeanRevert'], 'return_pct': -0.7},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['MeanRevert'], 'return_pct': -0.5},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['MeanRevert'], 'return_pct': -0.4},
                ],
            }
        ])
        joint = report['joint_governance']
        self.assertTrue(joint['items'])
        breakout = next(item for item in joint['items'] if item['policy_version'] == 'policy_v2' and item['strategy'] == 'Breakout')
        mean_revert = next(item for item in joint['items'] if item['policy_version'] == 'policy_v2' and item['strategy'] == 'MeanRevert')
        self.assertEqual(breakout['conflict_resolution']['category'], 'policy_blocking_precedence')
        self.assertEqual(breakout['conflict_resolution']['blocking_precedence'], 'policy')
        self.assertEqual(breakout['final_governance_decision']['decision'], 'observe')
        self.assertEqual(mean_revert['conflict_resolution']['category'], 'strategy_blocking_precedence')
        self.assertEqual(mean_revert['conflict_resolution']['blocking_precedence'], 'strategy')
        self.assertEqual(mean_revert['final_governance_decision']['decision'], 'freeze')
        self.assertEqual(mean_revert['combined_actions'][0]['type'], 'joint_freeze')
        joint_summary = report['summary']['joint_governance_summary']
        self.assertGreaterEqual(joint_summary['by_conflict_category']['strategy_blocking_precedence'], 1)
        self.assertGreaterEqual(joint_summary['by_conflict_category']['policy_blocking_precedence'], 1)
        self.assertGreaterEqual(joint_summary['by_final_decision']['freeze'], 1)

    def test_calibration_report_exposes_joint_governance_delivery_for_consumers(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])
        delivery = report['delivery']
        self.assertIn('joint_governance', delivery['views']['tables'])
        self.assertTrue(delivery['render_ready']['sections']['joint_priority_queue'])
        self.assertTrue(delivery['orchestration_ready']['joint_priority_queue'])
        self.assertTrue(delivery['orchestration_ready']['joint_action_playbook'])
        self.assertTrue(delivery['orchestration_ready']['joint_approval_queue'])
        joint_row = delivery['orchestration_ready']['joint_priority_queue'][0]
        self.assertIn('conflict_category', joint_row)
        self.assertIn('final_decision', joint_row)
        next_action_row = delivery['orchestration_ready']['joint_next_actions'][0]
        self.assertTrue(next_action_row['combined_actions'])
        playbook_row = delivery['orchestration_ready']['joint_action_playbook'][0]
        self.assertIn('preconditions', playbook_row)
        self.assertIn('rollback_plan', playbook_row)
        self.assertIn('execution_window', playbook_row)
        self.assertIn('stage_model', playbook_row)
        self.assertIn('queue_progression', playbook_row)
        self.assertIn('scheduled_review', playbook_row)
        approval_row = delivery['orchestration_ready']['joint_approval_queue'][0]
        self.assertEqual(approval_row['status'], 'awaiting_manual_approval')
        self.assertGreaterEqual(report['summary']['delivery_ready']['joint_priority_queue_size'], 1)
        self.assertGreaterEqual(report['summary']['delivery_ready']['joint_next_action_bucket_count'], 1)
        self.assertGreaterEqual(report['summary']['delivery_ready']['joint_action_playbook_size'], 1)
        self.assertGreaterEqual(report['summary']['delivery_ready']['joint_approval_required_count'], 1)

    def test_joint_governance_prefers_policy_blocking_and_strategy_policy_fit_guardrails(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 1.0},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.9},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.8},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.4},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.3},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.2},
                ],
            }
        ])
        joint = report['joint_governance']
        conflict = next(item for item in joint['items'] if item['policy_version'] == 'policy_v2' and item['strategy'] == 'Breakout')
        self.assertEqual(conflict['conflict_resolution']['category'], 'policy_blocking_precedence')
        self.assertEqual(conflict['conflict_resolution']['blocking_precedence'], 'policy')
        self.assertEqual(conflict['final_governance_decision']['decision'], 'freeze')
        self.assertEqual(conflict['conflict_resolution']['strategy_preferred_policy_version'], 'policy_v1')
        self.assertTrue(any(action['type'] == 'prefer_strategy_best_policy' for action in conflict['combined_actions']))

    def test_calibration_report_exposes_strategy_governance_delivery_for_report_and_orchestration(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 1.4},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 1.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': 1.1},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['MeanRevert'], 'return_pct': -0.7},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['MeanRevert'], 'return_pct': -0.5},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['MeanRevert'], 'return_pct': -0.4},
                    {'regime_tag': 'range', 'policy_tag': 'policy_v2', 'strategy_tags': ['Scalp'], 'return_pct': 0.4},
                ],
            }
        ])
        strategy_delivery = report['strategy_fit']['strategy_governance']
        self.assertTrue(strategy_delivery['items'])
        self.assertTrue(strategy_delivery['priority_queue'])
        first_item = strategy_delivery['items'][0]
        self.assertEqual(first_item['scope'], 'strategy')
        self.assertIn('fit', first_item)
        self.assertIn('orchestration', first_item)
        self.assertIn('strategy_recommendations', report['delivery']['views']['tables'])
        self.assertTrue(report['delivery']['render_ready']['sections']['strategy_priority_queue'])
        self.assertTrue(report['delivery']['orchestration_ready']['strategy_priority_queue'])
        self.assertGreaterEqual(report['summary']['delivery_ready']['strategy_priority_queue_size'], 1)
        self.assertGreaterEqual(report['summary']['delivery_ready']['strategy_next_action_bucket_count'], 1)
        freeze_queue = next(row for row in strategy_delivery['priority_queue'] if row['strategy'] == 'MeanRevert')
        self.assertEqual(freeze_queue['primary_action'], 'rollout_freeze')
        next_action_row = next(row for row in report['delivery']['orchestration_ready']['strategy_next_actions'] if row['strategy'] == 'MeanRevert')
        self.assertEqual(next_action_row['next_actions'][0]['type'], 'rollout_freeze')

    def test_calibration_report_ready_payload_flattens_delivery_for_consumers(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.4},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.3},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.1},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.0},
                ],
            }
        ])
        payload = build_calibration_report_ready_payload(report)
        self.assertEqual(payload['schema_version'], 'm5_report_ready_v1')
        self.assertEqual(payload['delivery_schema_version'], 'm5_delivery_v1')
        self.assertEqual(payload['delivery_ready'], report['summary']['delivery_ready'])
        self.assertEqual(payload['views']['items'], report['delivery']['views']['items'])
        self.assertEqual(payload['render_ready'], report['delivery']['render_ready'])
        self.assertEqual(payload['orchestration_ready'], report['delivery']['orchestration_ready'])
        self.assertEqual(payload['governance_ready'], report['delivery']['governance_ready'])
        self.assertEqual(payload['joint_governance'], report['delivery']['governance_ready']['items'])
        self.assertEqual(payload['priority_queue'], report['delivery']['governance_ready']['priority_queue'])
        self.assertEqual(payload['next_actions'], report['delivery']['governance_ready']['next_actions'])
        self.assertEqual(payload['blocking_items'], report['delivery']['governance_ready']['blocking_items'])
        self.assertEqual(payload['action_playbook'], report['delivery']['governance_ready']['action_playbook'])
        self.assertEqual(payload['approval_ready'], report['delivery']['governance_ready']['approval_ready'])
        self.assertEqual(payload['bucket_index'], report['delivery']['governance_ready']['bucket_index'])
        self.assertEqual(payload['workflow_ready']['actions'], report['delivery']['governance_ready']['action_playbook']['items'])
        self.assertEqual(payload['workflow_ready']['approval_queue'], report['delivery']['governance_ready']['approval_ready']['items'])
        self.assertEqual(payload['tables']['governance_ready'], report['delivery']['governance_ready'])
        self.assertEqual(payload['tables']['workflow_ready'], payload['workflow_ready'])
        self.assertEqual(payload['tables']['joint_governance'], report['delivery']['views']['tables']['joint_governance'])

    def test_joint_governance_ready_payload_provides_direct_consumer_entrypoint(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])
        payload = build_joint_governance_ready_payload(report)
        self.assertEqual(payload['schema_version'], 'm5_joint_governance_ready_v2')
        self.assertEqual(payload['delivery_schema_version'], 'm5_delivery_v1')
        self.assertEqual(payload['items'], report['joint_governance']['items'])
        self.assertEqual(payload['priority_queue'], report['delivery']['orchestration_ready']['joint_priority_queue'])
        self.assertEqual(payload['next_actions'], report['delivery']['orchestration_ready']['joint_next_actions'])
        self.assertEqual(payload['blocking_items'], report['delivery']['render_ready']['sections']['joint_blocking_items'])
        self.assertTrue(payload['action_playbook']['items'])
        self.assertTrue(payload['approval_ready']['items'])
        self.assertEqual(payload['tables']['joint_priority_queue'], payload['priority_queue'])
        self.assertEqual(payload['tables']['joint_action_playbook'], payload['action_playbook']['items'])
        self.assertIn(payload['priority_queue'][0]['bucket_id'], payload['bucket_index'])
        self.assertEqual(report['summary']['governance_ready']['schema_version'], payload['schema_version'])
        self.assertEqual(report['summary']['governance_ready']['item_count'], payload['summary']['item_count'])

    def test_joint_governance_ready_payload_exposes_action_playbook_and_approval_ready(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 1.0},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.9},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.8},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.4},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.3},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.2},
                ],
            }
        ])
        payload = build_joint_governance_ready_payload(report)
        self.assertTrue(payload['action_playbook']['items'])
        self.assertTrue(payload['approval_ready']['items'])
        playbook_row = payload['action_playbook']['items'][0]
        self.assertIn(playbook_row['risk_level'], {'critical', 'high', 'medium', 'low'})
        self.assertIn('preconditions', playbook_row)
        self.assertIn('rollback_plan', playbook_row)
        self.assertIn('execution_window', playbook_row)
        self.assertIn('stage_model', playbook_row)
        self.assertIn('queue_progression', playbook_row)
        self.assertIn('scheduled_review', playbook_row)
        self.assertTrue(payload['approval_ready']['summary']['approver_roles'])
        approval_row = payload['approval_ready']['items'][0]
        bucket_ready = payload['approval_ready']['by_bucket'][approval_row['bucket_id']]
        self.assertTrue(bucket_ready['actions'])
        self.assertEqual(bucket_ready['actions'][0]['playbook_id'], approval_row['playbook_id'])

    def test_governance_workflow_ready_payload_exposes_workflow_entrypoint(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])
        payload = build_governance_workflow_ready_payload(report)
        self.assertEqual(payload['schema_version'], 'm5_governance_workflow_ready_v2')
        self.assertEqual(payload['governance_schema_version'], report['delivery']['governance_ready']['schema_version'])
        self.assertEqual(payload['actions'], report['delivery']['governance_ready']['action_playbook']['items'])
        self.assertEqual(payload['approval_queue'], report['delivery']['governance_ready']['approval_ready']['items'])
        self.assertEqual(payload['queues']['priority'], report['delivery']['governance_ready']['priority_queue'])
        self.assertEqual(payload['queues']['approvals'], payload['approval_queue'])
        self.assertTrue(payload['filters']['risk_levels'])
        self.assertTrue(payload['filters']['owner_hints'])
        self.assertIn('pending_approval', payload['filters']['statuses'])
        self.assertIn('pending', payload['filters']['workflow_states'])
        self.assertIn('pending', payload['filters']['approval_states'])
        self.assertEqual(payload['summary']['action_count'], len(payload['actions']))
        self.assertEqual(payload['summary']['approval_count'], len(payload['approval_queue']))
        self.assertTrue(payload['workflow_state']['item_states'])
        self.assertTrue(payload['approval_state']['items'])
        self.assertEqual(payload['workflow_state']['summary']['item_count'], len(payload['workflow_state']['item_states']))
        self.assertTrue(payload['summary']['orchestration_summary'])
        self.assertEqual(payload['approval_state']['summary']['approval_count'], len(payload['approval_state']['items']))
        approval_row = payload['approval_queue'][0]
        self.assertTrue(any(
            row['playbook_id'] == approval_row['playbook_id']
            for row in payload['by_bucket'][approval_row['bucket_id']]['approvals']['actions']
        ))
        self.assertIn('state', payload['by_bucket'][approval_row['bucket_id']])
        workflow_item = payload['workflow_state']['item_states'][0]
        self.assertIn('stage_model', workflow_item)
        self.assertIn('queue_progression', workflow_item)
        self.assertIn('scheduled_review', workflow_item)

    def test_governance_workflow_ready_payload_exposes_auto_approval_policy(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])
        payload = build_governance_workflow_ready_payload(report)
        approval_item = payload['approval_state']['items'][0]
        workflow_item = payload['workflow_state']['item_states'][0]
        self.assertIn(approval_item['auto_approval_decision'], {'freeze', 'manual_review', 'defer', 'auto_approve'})
        self.assertIn('requires_manual', approval_item)
        self.assertIn('auto_approval_eligible', approval_item)
        self.assertIn('blocked_by', approval_item)
        self.assertEqual(payload['auto_approval_policy']['schema_version'], 'm5_auto_approval_policy_v1')
        self.assertIn('auto_approval', payload['approval_state']['summary'])
        self.assertIn('auto_approval', payload['workflow_state']['summary'])
        self.assertEqual(workflow_item['auto_approval_decision'], approval_item['auto_approval_decision'])

    def test_export_calibration_payload_supports_delivery_governance_ready_and_full_views(self):
        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.4},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.3},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.2},
                ],
            }
        ])
        self.assertEqual(export_calibration_payload(report, view='delivery'), report['delivery'])
        self.assertEqual(export_calibration_payload(report, view='governance_ready'), report['delivery']['governance_ready'])
        self.assertEqual(export_calibration_payload(report, view='workflow_ready'), build_governance_workflow_ready_payload(report))
        self.assertEqual(export_calibration_payload(report, view='full'), report)

    def test_backtest_calibration_report_api_returns_report_ready_view(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.4},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.3},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.2},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.1},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v2', 'return_pct': 1.0},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            response = client.get('/api/backtest/calibration-report')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload['success'])
            self.assertEqual(payload['view'], 'report_ready')
            self.assertEqual(payload['data']['schema_version'], 'm5_report_ready_v1')
            self.assertEqual(payload['data']['delivery_ready'], report['summary']['delivery_ready'])
            self.assertEqual(payload['data']['priority_queue'], report['delivery']['governance_ready']['priority_queue'])
            self.assertEqual(payload['data']['blocking_items'], report['delivery']['governance_ready']['blocking_items'])
            self.assertEqual(payload['summary']['delivery_ready'], report['summary']['delivery_ready'])
            self.assertEqual(payload['summary']['governance_ready'], report['summary']['governance_ready'])
        finally:
            dashboard_api.backtester = old_backtester

    def test_backtest_calibration_report_api_supports_delivery_view(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.4},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.3},
                    {'regime_tag': 'trend_up', 'policy_tag': 'policy_v1', 'return_pct': 0.2},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            response = client.get('/api/backtest/calibration-report?view=delivery')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'delivery')
            self.assertEqual(payload['data']['schema_version'], 'm5_delivery_v1')
            self.assertEqual(payload['data']['views']['items'], report['delivery']['views']['items'])
        finally:
            dashboard_api.backtester = old_backtester

    def test_backtest_calibration_report_api_supports_workflow_ready_view(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            response = client.get('/api/backtest/calibration-report?view=workflow_ready')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'workflow_ready')
            self.assertEqual(payload['data']['schema_version'], 'm5_governance_workflow_ready_v2')
            self.assertEqual(payload['data']['actions'], report['delivery']['governance_ready']['action_playbook']['items'])
            self.assertEqual(payload['data']['approval_queue'], report['delivery']['governance_ready']['approval_ready']['items'])
            self.assertTrue(payload['data']['workflow_state']['item_states'])
            self.assertTrue(payload['data']['approval_state']['items'])
            self.assertIn('auto_approval_policy', payload['data'])
            self.assertIn('auto_approval_decision', payload['data']['approval_state']['items'][0])
            self.assertEqual(payload['summary']['workflow_ready'], payload['data']['summary'])
            self.assertEqual(payload['summary']['governance_ready'], report['summary']['governance_ready'])
        finally:
            dashboard_api.backtester = old_backtester

    def test_backtest_workflow_state_api_returns_workflow_state_layer(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            response = client.get('/api/backtest/workflow-state')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload['success'])
            self.assertEqual(payload['view'], 'workflow_state')
            self.assertEqual(payload['data']['schema_version'], 'm5_governance_workflow_ready_v2')
            self.assertTrue(payload['data']['workflow_state']['item_states'])
            self.assertTrue(payload['data']['approval_state']['items'])
            self.assertEqual(payload['summary'], payload['data']['summary'])
            self.assertEqual(payload['data']['consumer_view']['schema_version'], 'm5_workflow_consumer_view_v1')
            self.assertIn('rollout_stage_progression', payload['data']['consumer_view'])
        finally:
            dashboard_api.backtester = old_backtester

    def test_backtest_workflow_consumer_view_api_returns_unified_snapshot(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            response = client.get('/api/backtest/workflow-consumer-view')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload['success'])
            self.assertEqual(payload['view'], 'workflow_consumer_view')
            self.assertEqual(payload['data']['schema_version'], 'm5_workflow_consumer_view_v1')
            self.assertIn('rollout_stage_progression', payload['data'])
            self.assertEqual(payload['summary'], payload['data']['summary'])
        finally:
            dashboard_api.backtester = old_backtester

    def test_backtest_workflow_attention_view_api_returns_manual_and_blocked_snapshot(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            response = client.get('/api/backtest/workflow-attention-view?max_items=2')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload['success'])
            self.assertEqual(payload['view'], 'workflow_attention_view')
            self.assertEqual(payload['data']['schema_version'], 'm5_workflow_attention_view_v1')
            self.assertIn('summary', payload['data'])
            self.assertIn('items', payload['data'])
            self.assertIn('by_bucket', payload['data'])
            self.assertIn('manual_approval', payload['data']['by_bucket'])
            self.assertIn('blocked_follow_up', payload['data']['by_bucket'])
            self.assertLessEqual(len(payload['data']['items']), 2)
            self.assertEqual(payload['summary'], payload['data']['summary'])
        finally:
            dashboard_api.backtester = old_backtester


    def test_backtest_workflow_operator_digest_api_returns_low_intervention_summary(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        old_get_recent_transition_journal = dashboard_api.db.get_recent_transition_journal
        old_get_transition_journal_summary = dashboard_api.db.get_transition_journal_summary
        dashboard_api.backtester = StubBacktester()
        dashboard_api.db.get_recent_transition_journal = lambda **kwargs: [
            {
                'item_id': 'playbook::ready',
                'approval_id': 'approval::ready',
                'title': 'Ready observe item',
                'timestamp': '2026-03-28T08:30:00',
                'trigger': 'state_transition',
                'actor': 'system',
                'source': 'workflow_loop',
                'from': {'workflow_state': 'pending'},
                'to': {'workflow_state': 'ready'},
                'changed_fields': ['workflow_state'],
                'reason': 'promoted to ready',
                'changed': True,
            }
        ]
        dashboard_api.db.get_transition_journal_summary = lambda **kwargs: {
            'count': 1,
            'latest_timestamp': '2026-03-28T08:30:00',
            'changed_only': True,
            'changed_field_counts': {'workflow_state': 1},
            'workflow_transition_counts': {'pending->ready': 1},
        }
        try:
            client = app.test_client()
            response = client.get('/api/backtest/workflow-operator-digest?max_items=3')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload['success'])
            self.assertEqual(payload['view'], 'workflow_operator_digest')
            self.assertEqual(payload['data']['schema_version'], 'm5_workflow_operator_digest_v1')
            self.assertIn('headline', payload['data'])
            self.assertIn('attention', payload['data'])
            self.assertIn('next_actions', payload['data'])
            self.assertIn('transition_journal', payload['data'])
            self.assertEqual(payload['data']['transition_journal']['latest']['workflow_transition'], 'pending->ready')
            self.assertLessEqual(len(payload['data']['stage_progression']['items']), 3)
            self.assertEqual(payload['summary'], payload['data']['summary'])
            self.assertIn('related_summary', payload)
            self.assertIn('control_plane_readiness', payload['related_summary'])
        finally:
            dashboard_api.backtester = old_backtester
            dashboard_api.db.get_recent_transition_journal = old_get_recent_transition_journal
            dashboard_api.db.get_transition_journal_summary = old_get_transition_journal_summary

    def test_backtest_calibration_report_api_supports_operator_digest_view(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        old_get_recent_transition_journal = dashboard_api.db.get_recent_transition_journal
        old_get_transition_journal_summary = dashboard_api.db.get_transition_journal_summary
        dashboard_api.backtester = StubBacktester()
        dashboard_api.db.get_recent_transition_journal = lambda **kwargs: [
            {
                'item_id': 'playbook::manual',
                'approval_id': 'approval::manual',
                'title': 'Manual gate item',
                'timestamp': '2026-03-28T08:31:00',
                'trigger': 'approval_pending',
                'actor': 'system',
                'source': 'approval_flow',
                'from': {'workflow_state': 'pending'},
                'to': {'workflow_state': 'blocked_by_approval'},
                'changed_fields': ['workflow_state'],
                'reason': 'manual gate required',
                'changed': True,
            }
        ]
        dashboard_api.db.get_transition_journal_summary = lambda **kwargs: {
            'count': 1,
            'latest_timestamp': '2026-03-28T08:31:00',
            'changed_only': True,
            'changed_field_counts': {'workflow_state': 1},
            'workflow_transition_counts': {'pending->blocked_by_approval': 1},
        }
        try:
            client = app.test_client()
            response = client.get('/api/backtest/calibration-report?view=operator_digest')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'operator_digest')
            self.assertEqual(payload['data']['schema_version'], 'm5_workflow_operator_digest_v1')
            self.assertEqual(payload['summary']['operator_digest'], payload['data']['summary'])
            self.assertIn('headline', payload['data'])
            self.assertEqual(payload['data']['transition_journal']['latest']['workflow_transition'], 'pending->blocked_by_approval')
        finally:
            dashboard_api.backtester = old_backtester
            dashboard_api.db.get_recent_transition_journal = old_get_recent_transition_journal
            dashboard_api.db.get_transition_journal_summary = old_get_transition_journal_summary

    def test_backtest_workflow_alert_digest_api_returns_severity_layered_summary(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        old_get_recent_transition_journal = dashboard_api.db.get_recent_transition_journal
        old_get_transition_journal_summary = dashboard_api.db.get_transition_journal_summary
        dashboard_api.backtester = StubBacktester()
        dashboard_api.db.get_recent_transition_journal = lambda **kwargs: [
            {
                'item_id': 'playbook::recover',
                'approval_id': 'approval::recover',
                'title': 'Recover item',
                'timestamp': '2026-03-28T08:33:00',
                'trigger': 'recovery_blocked',
                'actor': 'system',
                'source': 'recovery_loop',
                'from': {'workflow_state': 'execution_failed'},
                'to': {'workflow_state': 'blocked'},
                'changed_fields': ['workflow_state'],
                'reason': 'requires guarded recovery',
                'changed': True,
            }
        ]
        dashboard_api.db.get_transition_journal_summary = lambda **kwargs: {
            'count': 1,
            'latest_timestamp': '2026-03-28T08:33:00',
            'changed_only': True,
            'changed_field_counts': {'workflow_state': 1},
            'workflow_transition_counts': {'execution_failed->blocked': 1},
        }
        try:
            client = app.test_client()
            response = client.get('/api/backtest/workflow-alert-digest?max_items=5')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload['success'])
            self.assertEqual(payload['view'], 'workflow_alert_digest')
            self.assertEqual(payload['data']['schema_version'], 'm5_workflow_alert_digest_v1')
            self.assertIn('severity_counts', payload['data']['summary'])
            self.assertIn('alerts', payload['data'])
            self.assertIn('control_plane_readiness', payload['related_summary'])
        finally:
            dashboard_api.backtester = old_backtester
            dashboard_api.db.get_recent_transition_journal = old_get_recent_transition_journal
            dashboard_api.db.get_transition_journal_summary = old_get_transition_journal_summary

    def test_backtest_calibration_report_api_supports_workflow_alert_digest_view(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        old_get_recent_transition_journal = dashboard_api.db.get_recent_transition_journal
        old_get_transition_journal_summary = dashboard_api.db.get_transition_journal_summary
        dashboard_api.backtester = StubBacktester()
        dashboard_api.db.get_recent_transition_journal = lambda **kwargs: []
        dashboard_api.db.get_transition_journal_summary = lambda **kwargs: {
            'count': 0,
            'latest_timestamp': None,
            'changed_only': True,
            'changed_field_counts': {},
            'workflow_transition_counts': {},
        }
        try:
            client = app.test_client()
            response = client.get('/api/backtest/calibration-report?view=workflow_alert_digest')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'workflow_alert_digest')
            self.assertEqual(payload['data']['schema_version'], 'm5_workflow_alert_digest_v1')
            self.assertEqual(payload['summary']['workflow_alert_digest'], payload['data']['summary'])
        finally:
            dashboard_api.backtester = old_backtester
            dashboard_api.db.get_recent_transition_journal = old_get_recent_transition_journal
            dashboard_api.db.get_transition_journal_summary = old_get_transition_journal_summary

    def test_backtest_dashboard_summary_cards_api_returns_backend_card_payload(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            response = client.get('/api/backtest/dashboard-summary-cards?max_items=2')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload['success'])
            self.assertEqual(payload['view'], 'dashboard_summary_cards')
            self.assertEqual(payload['data']['schema_version'], 'm5_dashboard_summary_cards_v1')
            self.assertIn('cards', payload['data'])
            self.assertIn('card_index', payload['data'])
            self.assertIn('workflow_overview', payload['data']['card_index'])
            self.assertEqual(payload['summary'], payload['data']['summary'])
        finally:
            dashboard_api.backtester = old_backtester

    def test_backtest_calibration_report_api_supports_dashboard_summary_cards_view(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            response = client.get('/api/backtest/calibration-report?view=dashboard_summary_cards')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'dashboard_summary_cards')
            self.assertEqual(payload['data']['schema_version'], 'm5_dashboard_summary_cards_v1')
            self.assertEqual(payload['summary']['dashboard_summary_cards'], payload['data']['summary'])
            self.assertIn('workflow_overview', payload['data']['card_index'])
        finally:
            dashboard_api.backtester = old_backtester

    def test_backtest_adaptive_rollout_orchestration_api_runs_mainline_execution_entrypoint(self):
        import dashboard.api as dashboard_api_module
        original_run_all = dashboard_api_module.backtester.run_all
        original_persist = dashboard_api_module._persist_workflow_approval_payload
        original_export = dashboard_api_module.export_calibration_payload
        original_execute = dashboard_api_module.execute_adaptive_rollout_orchestration
        original_get_recent_transition_journal = dashboard_api_module.db.get_recent_transition_journal
        original_get_transition_journal_summary = dashboard_api_module.db.get_transition_journal_summary
        try:
            dashboard_api_module.backtester.run_all = lambda symbols: {'calibration_report': {'summary': {'trade_count': 1, 'calibration_ready': True}}}
            dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': {
                'workflow_state': {'item_states': [{'item_id': 'playbook::eligible', 'title': 'Eligible item', 'action_type': 'joint_observe', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'workflow_state': 'ready', 'blocking_reasons': []}], 'summary': {'item_count': 1}},
                'approval_state': {'items': [{'approval_id': 'approval::eligible', 'playbook_id': 'playbook::eligible', 'title': 'Eligible item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []}], 'summary': {'pending_count': 1}},
            }
            dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='adaptive_rollout_orchestration_api': payload
            dashboard_api_module.execute_adaptive_rollout_orchestration = lambda payload, db, config=None, replay_source='adaptive_rollout_orchestration_api': {
                **payload,
                'adaptive_rollout_orchestration': {
                    'schema_version': 'm5_adaptive_rollout_orchestration_v2',
                    'passes': [{'label': 'pre_auto_approval', 'dry_run': False, 'rollout_executor_applied_count': 1}],
                    'summary': {'pass_count': 1, 'rerun_triggered': False, 'rollout_executor_applied_count': 1, 'controlled_rollout_executed_count': 1, 'auto_approval_executed_count': 1, 'review_queue_queued_count': 0, 'gate_status': 'ready'},
                },
                'rollout_executor': {'status': 'controlled', 'summary': {'applied_count': 1}, 'items': []},
                'controlled_rollout_execution': {'mode': 'state_apply', 'executed_count': 1, 'items': []},
                'auto_approval_execution': {'mode': 'controlled', 'executed_count': 1, 'items': []},
            }
            dashboard_api_module.db.get_recent_transition_journal = lambda **kwargs: []
            dashboard_api_module.db.get_transition_journal_summary = lambda **kwargs: {'count': 0, 'latest_timestamp': None, 'changed_only': True, 'changed_field_counts': {}, 'workflow_transition_counts': {}}
            client = dashboard_api_module.app.test_client()
            response = client.get('/api/backtest/adaptive-rollout-orchestration?max_items=2')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'adaptive_rollout_orchestration')
            self.assertEqual(payload['data']['schema_version'], 'm5_adaptive_rollout_orchestration_v2')
            self.assertEqual(payload['summary']['pass_count'], 1)
            self.assertEqual(payload['summary']['max_items'], 2)
            self.assertEqual(payload['runtime_orchestration_summary']['schema_version'], 'm5_runtime_orchestration_summary_v1')
            self.assertIn('control_plane_readiness', payload['related_summary'])
        finally:
            dashboard_api_module.backtester.run_all = original_run_all
            dashboard_api_module._persist_workflow_approval_payload = original_persist
            dashboard_api_module.export_calibration_payload = original_export
            dashboard_api_module.execute_adaptive_rollout_orchestration = original_execute
            dashboard_api_module.db.get_recent_transition_journal = original_get_recent_transition_journal
            dashboard_api_module.db.get_transition_journal_summary = original_get_transition_journal_summary

    def test_backtest_runtime_orchestration_summary_api_returns_runtime_entrypoint(self):
        import dashboard.api as dashboard_api_module
        original_run_all = dashboard_api_module.backtester.run_all
        original_persist = dashboard_api_module._persist_workflow_approval_payload
        original_export = dashboard_api_module.export_calibration_payload
        original_get_recent_transition_journal = dashboard_api_module.db.get_recent_transition_journal
        original_get_transition_journal_summary = dashboard_api_module.db.get_transition_journal_summary
        try:
            dashboard_api_module.backtester.run_all = lambda symbols: {'calibration_report': {'summary': {'trade_count': 1, 'calibration_ready': True}}}
            dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': {
                'adaptive_rollout_orchestration': {'schema_version': 'm5_adaptive_rollout_orchestration_v1', 'passes': [{'label': 'pre_auto_approval', 'dry_run': True, 'rollout_executor_applied_count': 1}], 'summary': {'pass_count': 1, 'rerun_triggered': False, 'rollout_executor_applied_count': 1, 'controlled_rollout_executed_count': 0, 'auto_approval_executed_count': 0, 'review_queue_queued_count': 1}},
                'workflow_state': {'item_states': [{'item_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'risk_level': 'high', 'approval_required': True, 'requires_manual': True, 'workflow_state': 'blocked_by_approval', 'blocking_reasons': []}], 'summary': {'item_count': 1}},
                'approval_state': {'items': [{'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'high', 'approval_required': True, 'requires_manual': True, 'blocked_by': []}], 'summary': {'pending_count': 1}},
                'rollout_executor': {'status': 'controlled', 'summary': {'applied_count': 1}, 'items': []},
                'controlled_rollout_execution': {'mode': 'state_apply', 'executed_count': 0, 'items': []},
                'auto_approval_execution': {'mode': 'controlled', 'executed_count': 0, 'items': []},
            }
            dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='runtime_orchestration_summary_api': payload
            dashboard_api_module.db.get_recent_transition_journal = lambda **kwargs: [
                {'item_id': 'playbook::manual', 'approval_id': 'approval::manual', 'title': 'Manual gate item', 'timestamp': '2026-03-28T09:31:00', 'trigger': 'approval_pending', 'actor': 'system', 'source': 'workflow_loop', 'from': {'workflow_state': 'pending'}, 'to': {'workflow_state': 'blocked_by_approval'}, 'changed_fields': ['workflow_state'], 'reason': 'manual gate required', 'changed': True}
            ]
            dashboard_api_module.db.get_transition_journal_summary = lambda **kwargs: {'count': 1, 'latest_timestamp': '2026-03-28T09:31:00', 'changed_only': True, 'changed_field_counts': {'workflow_state': 1}, 'workflow_transition_counts': {'pending->blocked_by_approval': 1}}
            client = dashboard_api_module.app.test_client()
            response = client.get('/api/backtest/runtime-orchestration-summary?max_items=2')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'runtime_orchestration_summary')
            self.assertEqual(payload['data']['schema_version'], 'm5_runtime_orchestration_summary_v1')
            self.assertIn('recent_progress', payload['data'])
            self.assertIn('stuck_points', payload['data'])
            self.assertIn('next_step', payload['data'])
            self.assertIn('follow_ups', payload['data'])
            self.assertEqual(payload['summary'], payload['data']['summary'])
            self.assertIn('control_plane_readiness', payload['related_summary'])
        finally:
            dashboard_api_module.backtester.run_all = original_run_all
            dashboard_api_module._persist_workflow_approval_payload = original_persist
            dashboard_api_module.export_calibration_payload = original_export
            dashboard_api_module.db.get_recent_transition_journal = original_get_recent_transition_journal
            dashboard_api_module.db.get_transition_journal_summary = original_get_transition_journal_summary

    def test_build_orchestration_result_digest_summarizes_recent_automatic_actions(self):
        payload = {
            'adaptive_rollout_orchestration': {
                'schema_version': 'm5_adaptive_rollout_orchestration_v2',
                'summary': {
                    'pass_count': 2,
                    'auto_approval_executed_count': 1,
                    'controlled_rollout_executed_count': 1,
                    'review_queue_completed_count': 1,
                    'review_queue_rollback_escalated_count': 1,
                    'recovery_retry_scheduled_count': 1,
                    'recovery_retry_reentered_executor_count': 1,
                    'recovery_rollback_queued_count': 1,
                    'testnet_bridge_status': 'completed',
                    'testnet_bridge_follow_up_required': True,
                },
            },
            'workflow_state': {'item_states': [], 'summary': {}},
            'approval_state': {'items': [], 'summary': {}},
            'auto_approval_execution': {
                'items': [{'item_id': 'playbook::auto', 'approval_id': 'approval::auto', 'title': 'Auto item', 'action': 'approved', 'workflow_state': 'ready'}],
            },
            'controlled_rollout_execution': {
                'items': [{'item_id': 'playbook::rollout', 'approval_id': 'approval::rollout', 'title': 'Rollout item', 'action': 'applied', 'workflow_state': 'review_pending'}],
            },
            'auto_promotion_review_execution': {
                'items': [
                    {'item_id': 'playbook::review', 'approval_id': 'approval::review', 'title': 'Review item', 'action': 'completed', 'queue_kind': 'post_promotion_review_queue', 'workflow_state': 'ready'},
                    {'item_id': 'playbook::rollback', 'approval_id': 'approval::rollback', 'title': 'Rollback item', 'action': 'rollback_escalated', 'queue_kind': 'rollback_review_queue', 'workflow_state': 'rollback_prepare'},
                ],
            },
            'recovery_execution': {
                'items': [
                    {'item_id': 'playbook::retry', 'approval_id': 'approval::retry', 'title': 'Retry item', 'action': 'retry_scheduled', 'queue_bucket': 'retry_queue', 'workflow_state': 'queued', 'reentered_executor': True, 'follow_up_execution': {'follow_up': 'retry_execution'}},
                    {'item_id': 'playbook::manual_recovery', 'approval_id': 'approval::manual_recovery', 'title': 'Manual recovery item', 'action': 'manual_recovery_annotated', 'queue_bucket': 'manual_recovery', 'workflow_state': 'execution_failed'},
                ],
            },
            'testnet_bridge_execution_evidence': {
                'status': 'completed',
                'symbol': 'BTC/USDT',
                'executed_this_round': True,
                'follow_up_required': True,
                'summary': {'latest_execution_at': '2026-03-29T09:00:00Z'},
            },
            'workflow_operator_digest': {
                'transition_journal': {
                    'summary': {'count': 1, 'latest_timestamp': '2026-03-29T09:05:00Z'},
                    'latest': {'workflow_transition': 'review_pending->ready'},
                    'recent_transitions': [
                        {'item_id': 'playbook::review', 'approval_id': 'approval::review', 'title': 'Review item', 'timestamp': '2026-03-29T09:05:00Z', 'trigger': 'review_completed', 'reason': 'review passed', 'changed': True, 'changed_fields': ['workflow_state'], 'from': {'workflow_state': 'review_pending'}, 'to': {'workflow_state': 'ready'}}
                    ],
                },
                'summary': {},
            },
            'workbench_governance_view': {'recent_adjustments': [], 'summary': {}},
            'workflow_recovery_view': {'summary': {'manual_recovery_count': 1}, 'queues': {}},
            'unified_workbench_overview': {'dominant_line': 'rollout', 'overall_state': 'active', 'transition_journal': {'summary': {'count': 1, 'latest_timestamp': '2026-03-29T09:05:00Z'}, 'latest': {'workflow_transition': 'review_pending->ready'}, 'recent_transitions': []}},
        }
        digest = build_orchestration_result_digest(payload, max_items=8)
        self.assertEqual(digest['schema_version'], 'm5_orchestration_result_digest_v1')
        self.assertEqual(digest['summary']['auto_approval_executed_count'], 1)
        self.assertEqual(digest['summary']['controlled_rollout_executed_count'], 1)
        self.assertEqual(digest['summary']['review_queue_completed_count'], 1)
        self.assertEqual(digest['summary']['recovery_retry_reentered_executor_count'], 1)
        self.assertEqual(digest['summary']['testnet_bridge_status'], 'completed')
        self.assertTrue(any(row['action_type'] == 'review_completed' for row in digest['recent_actions']))
        self.assertTrue(any(row['action_type'] == 'rollback_escalated' for row in digest['recent_actions']))
        self.assertTrue(any(row['action_type'] == 'recovery_retry_reentered_executor' for row in digest['recent_actions']))
        self.assertTrue(any(row['action_type'] == 'testnet_bridge_executed' for row in digest['critical_actions']))

    def test_backtest_orchestration_result_digest_api_returns_recent_actions_lane(self):
        import dashboard.api as dashboard_api_module
        original_run_all = dashboard_api_module.backtester.run_all
        original_persist = dashboard_api_module._persist_workflow_approval_payload
        original_export = dashboard_api_module.export_calibration_payload
        original_get_recent_transition_journal = dashboard_api_module.db.get_recent_transition_journal
        original_get_transition_journal_summary = dashboard_api_module.db.get_transition_journal_summary
        try:
            dashboard_api_module.backtester.run_all = lambda symbols: {'calibration_report': {'summary': {'trade_count': 1, 'calibration_ready': True}}}
            dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': {
                'adaptive_rollout_orchestration': {'schema_version': 'm5_adaptive_rollout_orchestration_v2', 'summary': {'pass_count': 1, 'auto_approval_executed_count': 1, 'controlled_rollout_executed_count': 1, 'review_queue_completed_count': 1, 'review_queue_rollback_escalated_count': 0, 'recovery_retry_scheduled_count': 0, 'recovery_retry_reentered_executor_count': 0, 'recovery_rollback_queued_count': 0, 'testnet_bridge_status': 'completed'}},
                'workflow_state': {'item_states': [], 'summary': {}},
                'approval_state': {'items': [], 'summary': {}},
                'auto_approval_execution': {'items': [{'item_id': 'playbook::auto', 'approval_id': 'approval::auto', 'title': 'Auto item', 'action': 'approved'}]},
                'controlled_rollout_execution': {'items': [{'item_id': 'playbook::rollout', 'approval_id': 'approval::rollout', 'title': 'Rollout item', 'action': 'applied'}]},
                'auto_promotion_review_execution': {'items': [{'item_id': 'playbook::review', 'approval_id': 'approval::review', 'title': 'Review item', 'action': 'completed', 'queue_kind': 'post_promotion_review_queue'}]},
                'recovery_execution': {'items': []},
                'testnet_bridge_execution_evidence': {'status': 'completed', 'symbol': 'BTC/USDT', 'executed_this_round': True, 'follow_up_required': False, 'summary': {'latest_execution_at': '2026-03-29T09:00:00Z'}},
            }
            dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='orchestration_result_digest_api': payload
            dashboard_api_module.db.get_recent_transition_journal = lambda **kwargs: []
            dashboard_api_module.db.get_transition_journal_summary = lambda **kwargs: {'count': 0, 'latest_timestamp': None, 'changed_only': True, 'changed_field_counts': {}, 'workflow_transition_counts': {}}
            client = dashboard_api_module.app.test_client()
            response = client.get('/api/backtest/orchestration-result-digest?max_items=5')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'orchestration_result_digest')
            self.assertEqual(payload['data']['schema_version'], 'm5_orchestration_result_digest_v1')
            self.assertIn('recent_actions', payload['data'])
            self.assertTrue(any(row['action_type'] == 'auto_approval_executed' for row in payload['data']['recent_actions']))
            self.assertEqual(payload['summary'], payload['data']['summary'])
            self.assertIn('follow_up_summary', payload['related_summary'])
        finally:
            dashboard_api_module.backtester.run_all = original_run_all
            dashboard_api_module._persist_workflow_approval_payload = original_persist
            dashboard_api_module.export_calibration_payload = original_export
            dashboard_api_module.db.get_recent_transition_journal = original_get_recent_transition_journal
            dashboard_api_module.db.get_transition_journal_summary = original_get_transition_journal_summary

    def test_backtest_calibration_report_api_supports_orchestration_result_digest_view(self):
        import dashboard.api as dashboard_api_module
        original_run_all = dashboard_api_module.backtester.run_all
        original_persist = dashboard_api_module._persist_workflow_approval_payload
        original_export = dashboard_api_module.export_calibration_payload
        original_get_recent_transition_journal = dashboard_api_module.db.get_recent_transition_journal
        original_get_transition_journal_summary = dashboard_api_module.db.get_transition_journal_summary
        try:
            dashboard_api_module.backtester.run_all = lambda symbols: {'symbols': ['BTC-USDT'], 'calibration_report': {'summary': {'trade_count': 1, 'calibration_ready': True}}}
            dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': {
                'adaptive_rollout_orchestration': {'schema_version': 'm5_adaptive_rollout_orchestration_v2', 'summary': {'pass_count': 1, 'auto_approval_executed_count': 1, 'controlled_rollout_executed_count': 0, 'review_queue_completed_count': 0, 'review_queue_rollback_escalated_count': 0, 'recovery_retry_scheduled_count': 0, 'recovery_retry_reentered_executor_count': 0, 'recovery_rollback_queued_count': 0, 'testnet_bridge_status': 'disabled'}},
                'workflow_state': {'item_states': [], 'summary': {}},
                'approval_state': {'items': [], 'summary': {}},
                'auto_approval_execution': {'items': [{'item_id': 'playbook::auto', 'approval_id': 'approval::auto', 'title': 'Auto item', 'action': 'approved'}]},
                'controlled_rollout_execution': {'items': []},
                'auto_promotion_review_execution': {'items': []},
                'recovery_execution': {'items': []},
            }
            dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='calibration_report:orchestration_result_digest': payload
            dashboard_api_module.db.get_recent_transition_journal = lambda **kwargs: []
            dashboard_api_module.db.get_transition_journal_summary = lambda **kwargs: {'count': 0, 'latest_timestamp': None, 'changed_only': True, 'changed_field_counts': {}, 'workflow_transition_counts': {}}
            client = dashboard_api_module.app.test_client()
            response = client.get('/api/backtest/calibration-report?view=orchestration_result_digest')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'orchestration_result_digest')
            self.assertEqual(payload['data']['schema_version'], 'm5_orchestration_result_digest_v1')
            self.assertEqual(payload['summary']['orchestration_result_digest'], payload['data']['summary'])
        finally:
            dashboard_api_module.backtester.run_all = original_run_all
            dashboard_api_module._persist_workflow_approval_payload = original_persist
            dashboard_api_module.export_calibration_payload = original_export
            dashboard_api_module.db.get_recent_transition_journal = original_get_recent_transition_journal
            dashboard_api_module.db.get_transition_journal_summary = original_get_transition_journal_summary

    def test_backtest_calibration_report_api_supports_adaptive_rollout_orchestration_view(self):
        import dashboard.api as dashboard_api_module
        original_run_all = dashboard_api_module.backtester.run_all
        original_persist = dashboard_api_module._persist_workflow_approval_payload
        original_export = dashboard_api_module.export_calibration_payload
        original_execute = dashboard_api_module.execute_adaptive_rollout_orchestration
        try:
            dashboard_api_module.backtester.run_all = lambda symbols: {'symbols': ['BTC-USDT'], 'calibration_report': {'summary': {'trade_count': 1, 'calibration_ready': True}}}
            dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': {
                'workflow_state': {'item_states': [], 'summary': {}},
                'approval_state': {'items': [], 'summary': {}},
            }
            dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='calibration_report:adaptive_rollout_orchestration': payload
            dashboard_api_module.execute_adaptive_rollout_orchestration = lambda payload, db, config=None, replay_source='calibration_report:adaptive_rollout_orchestration': {
                **payload,
                'adaptive_rollout_orchestration': {
                    'schema_version': 'm5_adaptive_rollout_orchestration_v2',
                    'passes': [],
                    'summary': {'pass_count': 0, 'rerun_triggered': False, 'rollout_executor_applied_count': 0, 'controlled_rollout_executed_count': 0, 'auto_approval_executed_count': 0, 'review_queue_queued_count': 0, 'gate_status': 'blocked'},
                },
            }
            client = dashboard_api_module.app.test_client()
            response = client.get('/api/backtest/calibration-report?view=adaptive_rollout_orchestration')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'adaptive_rollout_orchestration')
            self.assertEqual(payload['data']['adaptive_rollout_orchestration']['schema_version'], 'm5_adaptive_rollout_orchestration_v2')
            self.assertEqual(payload['summary']['adaptive_rollout_orchestration'], payload['data']['adaptive_rollout_orchestration']['summary'])
        finally:
            dashboard_api_module.backtester.run_all = original_run_all
            dashboard_api_module._persist_workflow_approval_payload = original_persist
            dashboard_api_module.export_calibration_payload = original_export
            dashboard_api_module.execute_adaptive_rollout_orchestration = original_execute

    def test_backtest_calibration_report_api_supports_runtime_orchestration_summary_view(self):
        import dashboard.api as dashboard_api_module
        original_run_all = dashboard_api_module.backtester.run_all
        original_persist = dashboard_api_module._persist_workflow_approval_payload
        original_export = dashboard_api_module.export_calibration_payload
        original_get_recent_transition_journal = dashboard_api_module.db.get_recent_transition_journal
        original_get_transition_journal_summary = dashboard_api_module.db.get_transition_journal_summary
        try:
            dashboard_api_module.backtester.run_all = lambda symbols: {'symbols': ['BTC-USDT'], 'calibration_report': {'summary': {'trade_count': 1, 'calibration_ready': True}}}
            dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': {
                'adaptive_rollout_orchestration': {'schema_version': 'm5_adaptive_rollout_orchestration_v1', 'passes': [], 'summary': {'pass_count': 0, 'rerun_triggered': False, 'rollout_executor_applied_count': 0, 'controlled_rollout_executed_count': 0, 'auto_approval_executed_count': 0, 'review_queue_queued_count': 0}},
                'workflow_state': {'item_states': [], 'summary': {}},
                'approval_state': {'items': [], 'summary': {}},
                'rollout_executor': {'status': 'disabled', 'summary': {}},
                'controlled_rollout_execution': {'mode': 'disabled', 'items': []},
                'auto_approval_execution': {'mode': 'disabled', 'items': []},
            }
            dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='calibration_report:runtime_orchestration_summary': payload
            dashboard_api_module.db.get_recent_transition_journal = lambda **kwargs: []
            dashboard_api_module.db.get_transition_journal_summary = lambda **kwargs: {'count': 0, 'latest_timestamp': None, 'changed_only': True, 'changed_field_counts': {}, 'workflow_transition_counts': {}}
            client = dashboard_api_module.app.test_client()
            response = client.get('/api/backtest/calibration-report?view=runtime_orchestration_summary')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'runtime_orchestration_summary')
            self.assertEqual(payload['data']['schema_version'], 'm5_runtime_orchestration_summary_v1')
            self.assertEqual(payload['summary']['runtime_orchestration_summary'], payload['data']['summary'])
        finally:
            dashboard_api_module.backtester.run_all = original_run_all
            dashboard_api_module._persist_workflow_approval_payload = original_persist
            dashboard_api_module.export_calibration_payload = original_export
            dashboard_api_module.db.get_recent_transition_journal = original_get_recent_transition_journal
            dashboard_api_module.db.get_transition_journal_summary = original_get_transition_journal_summary


    def test_backtest_production_rollout_readiness_api_returns_fixed_gate(self):
        import dashboard.api as dashboard_api_module
        original_run_all = dashboard_api_module.backtester.run_all
        original_persist = dashboard_api_module._persist_workflow_approval_payload
        original_export = dashboard_api_module.export_calibration_payload
        original_get_recent_transition_journal = dashboard_api_module.db.get_recent_transition_journal
        original_get_transition_journal_summary = dashboard_api_module.db.get_transition_journal_summary
        try:
            dashboard_api_module.backtester.run_all = lambda symbols: {'calibration_report': {'summary': {'trade_count': 1, 'calibration_ready': True}}}
            dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': {
                'adaptive_rollout_orchestration': {'schema_version': 'm5_adaptive_rollout_orchestration_v1', 'passes': [], 'summary': {'pass_count': 0, 'rerun_triggered': False, 'rollout_executor_applied_count': 0, 'controlled_rollout_executed_count': 0, 'auto_approval_executed_count': 0, 'review_queue_queued_count': 1}},
                'workflow_state': {'item_states': [{'item_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'risk_level': 'high', 'approval_required': True, 'requires_manual': True, 'workflow_state': 'blocked_by_approval', 'blocking_reasons': []}], 'summary': {'item_count': 1}},
                'approval_state': {'items': [{'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'high', 'approval_required': True, 'requires_manual': True, 'blocked_by': []}], 'summary': {'pending_count': 1}},
                'rollout_executor': {'status': 'controlled', 'summary': {'applied_count': 0}, 'items': []},
                'controlled_rollout_execution': {'mode': 'state_apply', 'executed_count': 0, 'items': []},
                'auto_approval_execution': {'mode': 'controlled', 'executed_count': 0, 'items': []},
            }
            dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='production_rollout_readiness_api': payload
            dashboard_api_module.db.get_recent_transition_journal = lambda **kwargs: []
            dashboard_api_module.db.get_transition_journal_summary = lambda **kwargs: {'count': 0, 'latest_timestamp': None, 'changed_only': True, 'changed_field_counts': {}, 'workflow_transition_counts': {}}
            client = dashboard_api_module.app.test_client()
            response = client.get('/api/backtest/production-rollout-readiness?max_items=2')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'production_rollout_readiness')
            self.assertEqual(payload['data']['schema_version'], 'm5_production_rollout_readiness_v1')
            self.assertIn('runbook_actions', payload['data'])
            self.assertIn('control_plane_readiness', payload['related_summary'])
            self.assertEqual(payload['summary'], payload['data']['summary'])
        finally:
            dashboard_api_module.backtester.run_all = original_run_all
            dashboard_api_module._persist_workflow_approval_payload = original_persist
            dashboard_api_module.export_calibration_payload = original_export
            dashboard_api_module.db.get_recent_transition_journal = original_get_recent_transition_journal
            dashboard_api_module.db.get_transition_journal_summary = original_get_transition_journal_summary

    def test_backtest_calibration_report_api_supports_production_rollout_readiness_view(self):
        import dashboard.api as dashboard_api_module
        original_run_all = dashboard_api_module.backtester.run_all
        original_persist = dashboard_api_module._persist_workflow_approval_payload
        original_export = dashboard_api_module.export_calibration_payload
        original_get_recent_transition_journal = dashboard_api_module.db.get_recent_transition_journal
        original_get_transition_journal_summary = dashboard_api_module.db.get_transition_journal_summary
        try:
            dashboard_api_module.backtester.run_all = lambda symbols: {'symbols': ['BTC-USDT'], 'calibration_report': {'summary': {'trade_count': 1, 'calibration_ready': True}}}
            dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': {
                'adaptive_rollout_orchestration': {'schema_version': 'm5_adaptive_rollout_orchestration_v1', 'passes': [], 'summary': {'pass_count': 0, 'rerun_triggered': False, 'rollout_executor_applied_count': 0, 'controlled_rollout_executed_count': 0, 'auto_approval_executed_count': 0, 'review_queue_queued_count': 0}},
                'workflow_state': {'item_states': [], 'summary': {}},
                'approval_state': {'items': [], 'summary': {}},
                'rollout_executor': {'status': 'disabled', 'summary': {}},
                'controlled_rollout_execution': {'mode': 'disabled', 'items': []},
                'auto_approval_execution': {'mode': 'disabled', 'items': []},
            }
            dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='calibration_report:production_rollout_readiness': payload
            dashboard_api_module.db.get_recent_transition_journal = lambda **kwargs: []
            dashboard_api_module.db.get_transition_journal_summary = lambda **kwargs: {'count': 0, 'latest_timestamp': None, 'changed_only': True, 'changed_field_counts': {}, 'workflow_transition_counts': {}}
            client = dashboard_api_module.app.test_client()
            response = client.get('/api/backtest/calibration-report?view=production_rollout_readiness')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'production_rollout_readiness')
            self.assertEqual(payload['data']['schema_version'], 'm5_production_rollout_readiness_v1')
            self.assertEqual(payload['summary']['production_rollout_readiness'], payload['data']['summary'])
        finally:
            dashboard_api_module.backtester.run_all = original_run_all
            dashboard_api_module._persist_workflow_approval_payload = original_persist
            dashboard_api_module.export_calibration_payload = original_export
            dashboard_api_module.db.get_recent_transition_journal = original_get_recent_transition_journal
            dashboard_api_module.db.get_transition_journal_summary = original_get_transition_journal_summary

    def test_backtest_workbench_governance_view_api_returns_low_intervention_snapshot(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        old_get_recent_transition_journal = dashboard_api.db.get_recent_transition_journal
        old_get_transition_journal_summary = dashboard_api.db.get_transition_journal_summary
        dashboard_api.backtester = StubBacktester()
        dashboard_api.db.get_recent_transition_journal = lambda **kwargs: [
            {
                'item_id': 'playbook::manual',
                'approval_id': 'approval::manual',
                'title': 'Manual gate item',
                'timestamp': '2026-03-28T08:32:00',
                'trigger': 'approval_pending',
                'actor': 'system',
                'source': 'approval_flow',
                'from': {'workflow_state': 'pending'},
                'to': {'workflow_state': 'blocked_by_approval'},
                'changed_fields': ['workflow_state'],
                'reason': 'manual gate required',
                'changed': True,
            }
        ]
        dashboard_api.db.get_transition_journal_summary = lambda **kwargs: {
            'count': 1,
            'latest_timestamp': '2026-03-28T08:32:00',
            'changed_only': True,
            'changed_field_counts': {'workflow_state': 1},
            'workflow_transition_counts': {'pending->blocked_by_approval': 1},
        }
        try:
            client = app.test_client()
            response = client.get('/api/backtest/workbench-governance-view?operator_action=review_schedule&operator_route=manual_approval_queue&follow_up=await_manual_approval&max_items=2&max_adjustments=3')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload['success'])
            self.assertEqual(payload['view'], 'workbench_governance_view')
            self.assertEqual(payload['data']['schema_version'], 'm5_workbench_governance_view_v2')
            self.assertIn('lanes', payload['data'])
            self.assertIn('rollout', payload['data'])
            self.assertIn('recent_adjustments', payload['data'])
            self.assertEqual(payload['summary'], payload['data']['summary'])
            self.assertEqual(payload['data']['applied_filters']['operator_actions'], ['review_schedule'])
            self.assertEqual(payload['data']['applied_filters']['operator_routes'], ['manual_approval_queue'])
            self.assertEqual(payload['data']['applied_filters']['operator_follow_ups'], ['await_manual_approval'])
            self.assertEqual(payload['data']['lanes']['manual_approval']['operator_action_policy_summary']['dominant_action'], 'review_schedule')
            self.assertEqual(payload['data']['transition_journal']['latest']['workflow_transition'], 'pending->blocked_by_approval')
            self.assertLessEqual(len(payload['data']['rollout']['items']), 2)
            self.assertLessEqual(len(payload['data']['recent_adjustments']), 3)
        finally:
            dashboard_api.backtester = old_backtester
            dashboard_api.db.get_recent_transition_journal = old_get_recent_transition_journal
            dashboard_api.db.get_transition_journal_summary = old_get_transition_journal_summary

    def test_backtest_auto_promotion_candidates_api_returns_candidate_list_and_filters(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {'summary': {'symbols': len(symbols)}, 'symbols': [{'symbol': 'BTC/USDT'}], 'calibration_report': report}

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            response = client.get('/api/backtest/auto-promotion-candidates?candidate_status=ready&limit=10')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'auto_promotion_candidate_view')
            self.assertEqual(payload['data']['schema_version'], 'm5_auto_promotion_candidate_view_v1')
            self.assertEqual(payload['data']['applied_filters']['candidate_status'], 'ready')
            self.assertGreaterEqual(payload['data']['summary']['candidate_count'], payload['data']['summary']['ready_count'])
            self.assertTrue(all(row['can_auto_promote'] for row in payload['data']['items']))
        finally:
            dashboard_api.backtester = old_backtester

    def test_backtest_calibration_report_api_supports_auto_promotion_candidate_view(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([{
            'symbol': 'BTC/USDT',
            'all_trades': [
                {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
            ],
        }])

        class StubBacktester:
            def run_all(self, symbols):
                return {'summary': {'symbols': len(symbols)}, 'symbols': [{'symbol': 'BTC/USDT'}], 'calibration_report': report}

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            response = client.get('/api/backtest/calibration-report?view=auto_promotion_candidate_view&candidate_status=ready')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'auto_promotion_candidate_view')
            self.assertEqual(payload['data']['schema_version'], 'm5_auto_promotion_candidate_view_v1')
            self.assertEqual(payload['summary']['auto_promotion_candidate_view'], payload['data']['summary'])
        finally:
            dashboard_api.backtester = old_backtester

    def test_backtest_unified_workbench_overview_api_returns_three_line_snapshot(self):
        import dashboard.api as dashboard_api_module
        original_run_all = dashboard_api_module.backtester.run_all
        original_persist = dashboard_api_module._persist_workflow_approval_payload
        original_get_recent_transition_journal = dashboard_api_module.db.get_recent_transition_journal
        original_get_transition_journal_summary = dashboard_api_module.db.get_transition_journal_summary
        try:
            dashboard_api_module.backtester.run_all = lambda symbols: {'calibration_report': {'summary': {}, 'workflow_ready': {}}}
            dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': {
                'workflow_state': {
                    'item_states': [
                        {'item_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'risk_level': 'high', 'approval_required': True, 'requires_manual': True, 'workflow_state': 'blocked_by_approval', 'blocking_reasons': []},
                        {'item_id': 'playbook::recover', 'title': 'Recover item', 'action_type': 'joint_stage_prepare', 'risk_level': 'critical', 'approval_required': False, 'requires_manual': False, 'workflow_state': 'execution_failed', 'blocking_reasons': ['critical_risk'], 'state_machine': _build_state_machine_semantics(item_id='approval::recover', approval_state='pending', workflow_state='execution_failed', execution_status='error', retryable=False, blocked_by=['critical_risk'])},
                    ],
                    'summary': {'item_count': 2},
                },
                'approval_state': {
                    'items': [
                        {'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'high', 'approval_required': True, 'requires_manual': True, 'blocked_by': []},
                        {'approval_id': 'approval::recover', 'playbook_id': 'playbook::recover', 'title': 'Recover item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'critical', 'approval_required': False, 'requires_manual': False, 'blocked_by': ['critical_risk']},
                    ],
                    'summary': {'pending_count': 2},
                },
                'rollout_executor': {'status': 'controlled', 'summary': {}},
                'controlled_rollout_execution': {'mode': 'state_apply', 'executed_count': 0, 'items': []},
                'auto_approval_execution': {'mode': 'controlled', 'executed_count': 0, 'items': []},
            }
            dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='unified_workbench_overview_api': payload
            dashboard_api_module.db.get_recent_transition_journal = lambda **kwargs: [
                {
                    'item_id': 'playbook::recover',
                    'approval_id': 'approval::recover',
                    'title': 'Recover item',
                    'timestamp': '2026-03-28T08:33:00',
                    'trigger': 'recovery_blocked',
                    'actor': 'system',
                    'source': 'recovery_loop',
                    'from': {'workflow_state': 'execution_failed'},
                    'to': {'workflow_state': 'blocked'},
                    'changed_fields': ['workflow_state'],
                    'reason': 'requires guarded recovery',
                    'changed': True,
                }
            ]
            dashboard_api_module.db.get_transition_journal_summary = lambda **kwargs: {
                'count': 1,
                'latest_timestamp': '2026-03-28T08:33:00',
                'changed_only': True,
                'changed_field_counts': {'workflow_state': 1},
                'workflow_transition_counts': {'execution_failed->blocked': 1},
            }
            client = dashboard_api_module.app.test_client()
            response = client.get('/api/backtest/unified-workbench-overview?max_items=2')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'unified_workbench_overview')
            self.assertEqual(payload['data']['schema_version'], 'm5_unified_workbench_overview_v1')
            self.assertIn('approval', payload['data']['lines'])
            self.assertIn('rollout', payload['data']['lines'])
            self.assertIn('recovery', payload['data']['lines'])
            self.assertIn('stage_loop', payload['data']['summary'])
            self.assertIn('stage_loop', payload['data']['lines']['rollout'])
            self.assertEqual(payload['data']['transition_journal']['latest']['workflow_transition'], 'execution_failed->blocked')
            self.assertIn('related_summary', payload)
            self.assertIn('control_plane_readiness', payload['related_summary'])
        finally:
            dashboard_api_module.backtester.run_all = original_run_all
            dashboard_api_module._persist_workflow_approval_payload = original_persist
            dashboard_api_module.db.get_recent_transition_journal = original_get_recent_transition_journal
            dashboard_api_module.db.get_transition_journal_summary = original_get_transition_journal_summary

    def test_backtest_calibration_report_api_supports_unified_workbench_overview(self):
        import dashboard.api as dashboard_api_module
        original_run_all = dashboard_api_module.backtester.run_all
        original_persist = dashboard_api_module._persist_workflow_approval_payload
        original_export = dashboard_api_module.export_calibration_payload
        original_get_recent_transition_journal = dashboard_api_module.db.get_recent_transition_journal
        original_get_transition_journal_summary = dashboard_api_module.db.get_transition_journal_summary
        try:
            dashboard_api_module.backtester.run_all = lambda symbols: {'symbols': ['BTC-USDT'], 'calibration_report': {'summary': {'trade_count': 1, 'calibration_ready': True}}}
            dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': {
                'workflow_state': {'item_states': [], 'summary': {}},
                'approval_state': {'items': [], 'summary': {}},
                'rollout_executor': {'status': 'disabled', 'summary': {}},
                'controlled_rollout_execution': {'mode': 'disabled', 'items': []},
                'auto_approval_execution': {'mode': 'disabled', 'items': []},
            }
            dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='calibration_report:unified_workbench_overview': payload
            dashboard_api_module.db.get_recent_transition_journal = lambda **kwargs: []
            dashboard_api_module.db.get_transition_journal_summary = lambda **kwargs: {
                'count': 0,
                'latest_timestamp': None,
                'changed_only': True,
                'changed_field_counts': {},
                'workflow_transition_counts': {},
            }
            client = dashboard_api_module.app.test_client()
            response = client.get('/api/backtest/calibration-report?view=unified_workbench_overview')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'unified_workbench_overview')
            self.assertEqual(payload['data']['schema_version'], 'm5_unified_workbench_overview_v1')
            self.assertEqual(payload['summary']['unified_workbench_overview'], payload['data']['summary'])
            self.assertIn('transition_journal', payload['data'])
        finally:
            dashboard_api_module.backtester.run_all = original_run_all
            dashboard_api_module._persist_workflow_approval_payload = original_persist
            dashboard_api_module.export_calibration_payload = original_export
            dashboard_api_module.db.get_recent_transition_journal = original_get_recent_transition_journal
            dashboard_api_module.db.get_transition_journal_summary = original_get_transition_journal_summary

    def test_backtest_calibration_report_api_supports_workbench_governance_view(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        old_get_recent_transition_journal = dashboard_api.db.get_recent_transition_journal
        old_get_transition_journal_summary = dashboard_api.db.get_transition_journal_summary
        dashboard_api.backtester = StubBacktester()
        dashboard_api.db.get_recent_transition_journal = lambda **kwargs: []
        dashboard_api.db.get_transition_journal_summary = lambda **kwargs: {
            'count': 0,
            'latest_timestamp': None,
            'changed_only': True,
            'changed_field_counts': {},
            'workflow_transition_counts': {},
        }
        try:
            client = app.test_client()
            response = client.get('/api/backtest/calibration-report?view=workbench_governance_view')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'workbench_governance_view')
            self.assertEqual(payload['data']['schema_version'], 'm5_workbench_governance_view_v2')
            self.assertEqual(payload['summary']['workbench_governance_view'], payload['data']['summary'])
            self.assertIn('lanes', payload['data'])
            self.assertIn('transition_journal', payload['data'])
        finally:
            dashboard_api.backtester = old_backtester
            dashboard_api.db.get_recent_transition_journal = old_get_recent_transition_journal
            dashboard_api.db.get_transition_journal_summary = old_get_transition_journal_summary


    def test_backtest_workbench_governance_items_api_supports_lane_and_action_filters(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            seed_response = client.get('/api/backtest/workbench-governance-items?lane=manual_approval&limit=10')
            self.assertEqual(seed_response.status_code, 200)
            seed_payload = seed_response.get_json()
            self.assertGreater(seed_payload['data']['summary']['matched_count'], 0)
            selected_action = seed_payload['data']['items'][0]['action_type']
            selected_operator_action = seed_payload['data']['items'][0]['operator_action']
            selected_operator_route = seed_payload['data']['items'][0]['operator_route']
            selected_follow_up = seed_payload['data']['items'][0]['operator_follow_up']
            response = client.get(f'/api/backtest/workbench-governance-items?lane=manual_approval&action={selected_action}&operator_action={selected_operator_action}&operator_route={selected_operator_route}&follow_up={selected_follow_up}&limit=10')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'workbench_governance_filter_view')
            self.assertEqual(payload['data']['schema_version'], 'm5_workbench_governance_filter_view_v1')
            self.assertGreater(payload['data']['summary']['matched_count'], 0)
            self.assertTrue(all(row['lane_id'] == 'manual_approval' for row in payload['data']['items']))
            self.assertTrue(all(row['action_type'] == selected_action for row in payload['data']['items']))
            self.assertTrue(all(row['operator_action'] == selected_operator_action for row in payload['data']['items']))
            self.assertTrue(all(row['operator_route'] == selected_operator_route for row in payload['data']['items']))
            self.assertTrue(all(row['operator_follow_up'] == selected_follow_up for row in payload['data']['items']))
            self.assertEqual(payload['data']['applied_filters']['operator_actions'], [selected_operator_action])
            self.assertEqual(payload['data']['applied_filters']['operator_routes'], [selected_operator_route])
            self.assertEqual(payload['data']['applied_filters']['operator_follow_ups'], [selected_follow_up])
            self.assertIn('available_filters', payload['data'])
        finally:
            dashboard_api.backtester = old_backtester

    def test_backtest_rollout_control_plane_api_returns_manifest(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([{
            'symbol': 'BTC/USDT',
            'all_trades': [
                {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
            ],
        }])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            response = client.get('/api/backtest/rollout-control-plane')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'rollout_control_plane_manifest')
            self.assertEqual(payload['data']['schema_version'], 'm5_rollout_control_plane_manifest_v1')
            self.assertTrue(payload['data']['compatibility']['compatible'])
            self.assertIn('joint_review_schedule', payload['data']['registries']['action_types'])
            self.assertIn('stage_profiles', payload['data']['registries'])
            self.assertEqual(payload['data']['registries']['stage_profiles']['guarded_prepare']['stage_index'], 2)
            self.assertIn('stage_prepare_safe', payload['data']['registries']['handler_stage_map'])
            self.assertIn('related_summary', payload)
            self.assertIn('control_plane_readiness', payload['related_summary'])
        finally:
            dashboard_api.backtester = old_backtester

    def test_forward_readiness_api_exposes_control_plane_related_summary(self):
        import dashboard.api as dashboard_api_module
        original_get_signals = dashboard_api_module.db.get_signals
        try:
            dashboard_api_module.db.get_signals = lambda limit=5000: [
                {'signal_type': 'buy', 'symbol': 'BTC/USDT', 'filtered': 0, 'created_at': '2026-03-28T08:00:00'},
                {'signal_type': 'sell', 'symbol': 'ETH/USDT', 'filtered': 0, 'created_at': '2026-03-28T09:00:00'},
                {'signal_type': 'buy', 'symbol': 'SOL/USDT', 'filtered': 1, 'filter_reason': '风险过高', 'created_at': '2026-03-28T10:00:00'},
            ]
            client = dashboard_api_module.app.test_client()
            response = client.get('/api/forward/readiness?limit=10')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload['success'])
            self.assertIn('summary', payload)
            self.assertIn('control_plane_readiness', payload['summary'])
            self.assertIn('control_plane_manifest', payload['data'])
            self.assertIn('related_summary', payload['data'])
            self.assertEqual(payload['data']['control_plane_readiness']['schema_version'], 'm5_control_plane_readiness_summary_v1')
        finally:
            dashboard_api_module.db.get_signals = original_get_signals

    def test_backtest_workbench_governance_detail_api_returns_why_and_next_step(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            list_response = client.get('/api/backtest/workbench-governance-items?lane=manual_approval&limit=1')
            self.assertEqual(list_response.status_code, 200)
            item = list_response.get_json()['data']['items'][0]
            response = client.get(f"/api/backtest/workbench-governance-detail?item_id={item['item_id']}&lane={item['lane_id']}&operator_action={item['operator_action']}&operator_route={item['operator_route']}&follow_up={item['operator_follow_up']}")
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'workbench_governance_detail_view')
            self.assertEqual(payload['data']['schema_version'], 'm5_workbench_governance_detail_view_v3')
            self.assertTrue(payload['data']['found'])
            self.assertEqual(payload['data']['item']['item_id'], item['item_id'])
            self.assertTrue(payload['data']['item']['why'])
            self.assertTrue(payload['data']['item']['next_step'])
            self.assertIn('drilldown', payload['data'])
            self.assertIn('queue', payload['data']['drilldown'])
            self.assertIn('timeline', payload['data']['drilldown'])
            self.assertIn('timeline', payload['data']['summary'])
            self.assertEqual(payload['data']['summary']['operator_action'], item['operator_action'])
            self.assertEqual(payload['data']['summary']['operator_route'], item['operator_route'])
            self.assertEqual(payload['data']['summary']['follow_up'], item['operator_follow_up'])
            self.assertIn('operator_action', payload['data']['drilldown'])
            self.assertIn('approval', payload['data']['drilldown'])
            self.assertIn('rollout', payload['data']['drilldown'])
            self.assertTrue(payload['data']['summary']['next_transition'])
        finally:
            dashboard_api.backtester = old_backtester

    def test_backtest_workbench_governance_merged_timeline_api_returns_combined_view(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([{
            'symbol': 'BTC/USDT',
            'all_trades': [
                {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
            ],
        }])

        class StubBacktester:
            def run_all(self, symbols):
                return {'summary': {'symbols': len(symbols)}, 'symbols': [{'symbol': 'BTC/USDT'}], 'calibration_report': report}

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            list_response = client.get('/api/backtest/workbench-governance-items?lane=manual_approval&limit=1')
            item = list_response.get_json()['data']['items'][0]
            response = client.get(f"/api/backtest/workbench-governance-merged-timeline?item_id={item['item_id']}&approval_id={item['approval_id']}&lane={item['lane_id']}")
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'workbench_merged_timeline')
            self.assertEqual(payload['data']['schema_version'], 'm5_workbench_merged_timeline_v1')
            self.assertGreaterEqual(payload['summary']['event_count'], payload['summary']['executor_event_count'])
            self.assertIn('approval_db', payload['summary']['phases'])
        finally:
            dashboard_api.backtester = old_backtester

    def test_backtest_workbench_governance_timeline_summary_api_returns_bucket_and_action_aggregation(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([{
            'symbol': 'BTC/USDT',
            'all_trades': [
                {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
            ],
        }])

        class StubBacktester:
            def run_all(self, symbols):
                return {'summary': {'symbols': len(symbols)}, 'symbols': [{'symbol': 'BTC/USDT'}], 'calibration_report': report}

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            seed_response = client.get('/api/backtest/workbench-governance-items?lane=manual_approval&limit=1')
            self.assertEqual(seed_response.status_code, 200)
            seed_item = seed_response.get_json()['data']['items'][0]
            response = client.get(
                f"/api/backtest/workbench-governance-timeline-summary?bucket=manual_approval&operator_action={seed_item['operator_action']}&operator_route={seed_item['operator_route']}&follow_up={seed_item['operator_follow_up']}&max_items_per_group=5"
            )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'workbench_timeline_summary_aggregation')
            self.assertEqual(payload['data']['schema_version'], 'm5_workbench_timeline_summary_aggregation_v2')
            self.assertGreaterEqual(payload['summary']['item_count'], 1)
            self.assertTrue(payload['data']['groups']['by_bucket'])
            self.assertEqual(payload['data']['applied_filters']['bucket_tags'], ['manual_approval'])
            self.assertEqual(payload['data']['applied_filters']['operator_actions'], ['review_schedule'])
            self.assertEqual(payload['data']['applied_filters']['operator_routes'], [seed_item['operator_route']])
            self.assertEqual(payload['data']['applied_filters']['operator_follow_ups'], ['await_manual_approval'])
            manual_bucket = next(group for group in payload['data']['groups']['by_bucket'] if group['group_id'] == 'manual_approval')
            self.assertGreaterEqual(manual_bucket['merged_timeline_summary']['event_count_total'], manual_bucket['merged_timeline_summary']['executor_event_count_total'])
            self.assertEqual(manual_bucket['operator_action_policy_summary']['dominant_action'], seed_item['operator_action'])
            self.assertEqual(manual_bucket['operator_action_policy_summary']['dominant_route'], seed_item['operator_route'])
            self.assertEqual(manual_bucket['operator_action_policy_summary']['dominant_follow_up'], seed_item['operator_follow_up'])
            self.assertTrue(payload['data']['groups']['by_operator_action'])
            self.assertTrue(payload['data']['groups']['by_operator_route'])
            self.assertTrue(payload['data']['groups']['by_follow_up'])
            self.assertTrue(manual_bucket['items'])
            self.assertIn('timeline', manual_bucket['items'][0])
        finally:
            dashboard_api.backtester = old_backtester

    def test_backtest_calibration_report_api_supports_governance_ready_view(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        old_backtester = dashboard_api.backtester
        dashboard_api.backtester = StubBacktester()
        try:
            client = app.test_client()
            response = client.get('/api/backtest/calibration-report?view=governance_ready')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'governance_ready')
            self.assertEqual(payload['data']['schema_version'], 'm5_joint_governance_ready_v2')
            self.assertEqual(payload['data']['priority_queue'], report['delivery']['governance_ready']['priority_queue'])
            self.assertEqual(payload['summary']['governance_ready'], report['summary']['governance_ready'])
            self.assertEqual(payload['summary']['joint_governance_summary'], report['summary']['joint_governance_summary'])
        finally:
            dashboard_api.backtester = old_backtester


def run_tests():
    """运行所有测试"""
    print("\n" + "="*60)
    print("🧪 OKX量化交易系统 - 测试套件")
    print("="*60 + "\n")

    # 创建测试套件
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # 添加测试
    suite.addTests(loader.loadTestsFromTestCase(TestConfig))
    suite.addTests(loader.loadTestsFromTestCase(TestDatabase))
    suite.addTests(loader.loadTestsFromTestCase(TestSignalDetector))
    suite.addTests(loader.loadTestsFromTestCase(TestStrategies))
    suite.addTests(loader.loadTestsFromTestCase(TestTradingExecutor))
    suite.addTests(loader.loadTestsFromTestCase(TestRiskManager))
    suite.addTests(loader.loadTestsFromTestCase(TestRiskBudgetSizing))
    suite.addTests(loader.loadTestsFromTestCase(TestMFEAnalyzer))
    suite.addTests(loader.loadTestsFromTestCase(TestRecommendationProvider))

    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # 输出总结
    print("\n" + "="*60)
    if result.wasSuccessful():
        print("✅ 所有测试通过!")
    else:
        print(f"❌ {len(result.failures)} 失败, {len(result.errors)} 错误")
    print("="*60 + "\n")

    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)




class TestObserveOnlyNormalization(unittest.TestCase):
    def test_build_observe_only_payload_exposes_canonical_object(self):
        regime_snapshot = build_regime_snapshot('trend', 0.81, {'ema_gap': 0.03}, '趋势上涨')
        payload = build_observe_only_payload(Config(), 'BTC/USDT:USDT', regime_snapshot=regime_snapshot)
        self.assertIn('observe_only', payload)
        self.assertTrue(payload['observe_only']['enabled'])
        self.assertEqual(payload['observe_only_summary'], payload['observe_only']['summary'])
        self.assertEqual(payload['observe_only_phase'], payload['observe_only']['phase'])
        self.assertIn('top_tags', payload['observe_only'])

class TestLayeringConfig(unittest.TestCase):
    def test_default_layering_is_backward_compatible(self):
        cfg = Config()
        layering = cfg.get_layering_config()
        self.assertEqual(layering['layer_count'], 3)
        self.assertEqual(layering['layer_ratios'], [0.06, 0.06, 0.04])
        self.assertEqual(layering['layer_max_total_ratio'], 0.16)

    def test_invalid_layering_config_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, 'config.yaml')
            with open(cfg_path, 'w', encoding='utf-8') as f:
                f.write("""trading:
  layering:
    layer_count: 2
    layer_ratios: [0.06, 0.06, 0.04]
""")
            with self.assertRaises(ValueError):
                Config(cfg_path)


class TestLayeringBehavior(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.db = Database('data/test_layering_behavior.db')
        self.cfg._config.setdefault('trading', {}).setdefault('layering', {})
        self.cfg._config['trading']['layering'].update({
            'profit_only_add': True,
            'min_add_interval_seconds': 60,
            'max_layers_per_signal': 1,
            'allow_same_bar_multiple_adds': False,
        })
        self.executor = TradingExecutor(self.cfg, None, self.db)

    def tearDown(self):
        if os.path.exists('data/test_layering_behavior.db'):
            os.remove('data/test_layering_behavior.db')

    def test_profit_only_add_blocks_when_not_in_profit(self):
        self.db.record_trade('BTC/USDT', 'long', 100, 1, leverage=1, signal_id=1, layer_no=1, root_signal_id=1)
        self.db.save_layer_plan_state('BTC/USDT', 'long', status='active', current_layer=1, root_signal_id=1, plan_data={'filled_layers':[1], 'pending_layers':[], 'layer_ratios':[0.06,0.06,0.04], 'max_total_ratio':0.16})
        ok, reason, _ = self.executor._check_layering_runtime_guards('BTC/USDT', 'long', signal_id=2, plan_context={'current_price': 99})
        self.assertFalse(ok)
        self.assertIn('浮盈', reason)

    def test_same_bar_multiple_adds_blocked(self):
        self.db.save_layer_plan_state('BTC/USDT', 'long', status='active', current_layer=1, root_signal_id=1, plan_data={'filled_layers':[1], 'pending_layers':[], 'layer_ratios':[0.06,0.06,0.04], 'max_total_ratio':0.16, 'signal_bar_markers': {'bar-1': 'ts'}})
        ok, reason, _ = self.executor._check_layering_runtime_guards('BTC/USDT', 'long', signal_id=2, plan_context={'signal_bar_marker': 'bar-1'})
        self.assertFalse(ok)
        self.assertIn('同一 bar', reason)

    def test_adaptive_live_guardrails_can_tighten_runtime_layering_checks(self):
        self.cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_hints_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': False,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {
                'high_vol': {
                    'execution_overrides': {
                        'layer_ratios': [0.05, 0.05, 0.03],
                        'max_layers_per_signal': 1,
                        'min_add_interval_seconds': 600,
                        'profit_only_add': True,
                        'allow_same_bar_multiple_adds': False,
                    }
                }
            }
        }
        regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        policy_snapshot = resolve_regime_policy(self.cfg, 'BTC/USDT', regime_snapshot)
        self.db.record_trade('BTC/USDT', 'long', 100, 1, leverage=1, signal_id=1, layer_no=1, root_signal_id=1)
        self.db.save_layer_plan_state(
            'BTC/USDT', 'long', status='active', current_layer=1, root_signal_id=1,
            plan_data={
                'filled_layers': [1],
                'pending_layers': [],
                'layer_ratios': [0.06, 0.06, 0.04],
                'max_total_ratio': 0.16,
                'signal_layer_counts': {'2': 1},
                'signal_bar_markers': {'bar-2': 'ts'},
                'last_filled_at': datetime.utcnow().isoformat(),
            }
        )
        ok, reason, details = self.executor._check_layering_runtime_guards(
            'BTC/USDT', 'long', signal_id=2,
            plan_context={'current_price': 99, 'signal_bar_marker': 'bar-2', 'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot}
        )
        self.assertFalse(ok)
        self.assertIn('浮盈', reason)
        self.assertEqual(details['layering']['max_layers_per_signal'], 1)
        self.assertEqual(details['layering']['min_add_interval_seconds'], 600)
        self.assertTrue(details['layering']['profit_only_add'])
        self.assertEqual(details['layering']['layer_ratios'], [0.06, 0.06, 0.04])
        self.assertTrue(details['layering_profile_really_enforced'])
        self.assertFalse(details['plan_shape_really_enforced'])
        self.assertEqual(details['observability']['deny_reason'], 'profit_only_add')


class TestExecutionObservability(unittest.TestCase):
    def test_adaptive_execution_hints_expose_baseline_effective_live_enforced_applied_ignored(self):
        cfg = Config()
        db = Database('data/test_execution_observability.db')
        try:
            cfg._config['adaptive_regime'] = {
                'enabled': True,
                'mode': 'guarded_execute',
                'guarded_execute': {
                    'execution_profile_hints_enabled': True,
                    'execution_profile_enforcement_enabled': True,
                    'layering_profile_hints_enabled': True,
                    'layering_profile_enforcement_enabled': True,
                    'layering_plan_shape_enforcement_enabled': False,
                    'rollout_symbols': ['BTC/USDT'],
                },
                'regimes': {
                    'high_vol': {
                        'execution_overrides': {
                            'layer_ratios': [0.05, 0.05, 0.03],
                            'layer_max_total_ratio': 0.13,
                            'min_add_interval_seconds': 600,
                            'profit_only_add': True,
                            'allow_same_bar_multiple_adds': True,
                        }
                    }
                }
            }
            executor = TradingExecutor(cfg, FakeExecutorExchange(), db)
            regime_snapshot = build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
            policy_snapshot = resolve_regime_policy(cfg, 'BTC/USDT', regime_snapshot)
            approved, _, details = executor._prepare_open_execution(
                'BTC/USDT', 'long', 50000, signal_id=998,
                plan_context=executor._get_layer_plan('BTC/USDT', 'long', signal_id=998, plan_context={'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot}) | {'regime_snapshot': regime_snapshot, 'adaptive_policy_snapshot': policy_snapshot}
            )
            self.assertTrue(approved)
            enriched = details['plan_context']['observability']['adaptive_execution_hints']
            self.assertIn('baseline', enriched)
            self.assertIn('effective', enriched)
            self.assertIn('live', enriched)
            self.assertIn('enforced', enriched)
            self.assertIn('applied', enriched)
            self.assertIn('ignored', enriched)
            self.assertIn('ignored_fields', enriched)
            self.assertEqual(enriched['effective']['layer_max_total_ratio'], 0.13)
            self.assertEqual(enriched['live']['layer_max_total_ratio'], 0.13)
            self.assertEqual(enriched['enforced']['layer_max_total_ratio'], 0.13)
            self.assertIn('layer_ratios', enriched['applied'])
            self.assertIn('allow_same_bar_multiple_adds', enriched['ignored_fields'])
        finally:
            if os.path.exists('data/test_execution_observability.db'):
                os.remove('data/test_execution_observability.db')

    def test_build_observability_context_rounds_expected_fields(self):
        ctx = build_observability_context(
            symbol='BTC/USDT:USDT', side='long', signal_id=12, root_signal_id=10, layer_no=2,
            deny_reason='risk_budget', current_symbol_exposure=0.0612345, projected_symbol_exposure=0.1212345,
            current_total_exposure=0.0912345, projected_total_exposure=0.1512345,
        )
        self.assertEqual(ctx['signal_id'], 12)
        self.assertEqual(ctx['root_signal_id'], 10)
        self.assertEqual(ctx['layer_no'], 2)
        self.assertEqual(ctx['deny_reason'], 'risk_budget')
        self.assertEqual(ctx['current_symbol_exposure'], 0.061234)
        self.assertEqual(ctx['projected_total_exposure'], 0.151234)

    def test_execution_state_snapshot_contains_exposure_and_signal_digest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, 'test.db'))
            db.update_position(symbol='BTC/USDT:USDT', side='long', entry_price=50000, quantity=10, contract_size=0.01, coin_quantity=0.1, leverage=10, current_price=51000)
            signal_id = db.record_signal(symbol='BTC/USDT:USDT', signal_type='buy', price=50000, strength=80, reasons=[], strategies_triggered=[])
            regime_snapshot = build_regime_snapshot('trend', 0.81, {'ema_gap': 0.03, 'ema_direction': 1, 'volatility': 0.01}, '趋势上涨')
            adaptive_policy_snapshot = resolve_regime_policy(Config(), 'BTC/USDT:USDT', regime_snapshot)
            db.update_signal(signal_id, filter_details=json.dumps({
                'observability': {'signal_id': signal_id, 'root_signal_id': signal_id, 'layer_no': 1, 'deny_reason': 'direction_lock', 'current_symbol_exposure': 0.05, 'projected_symbol_exposure': 0.11, 'current_total_exposure': 0.05, 'projected_total_exposure': 0.11},
                'regime_snapshot': regime_snapshot,
                'adaptive_policy_snapshot': adaptive_policy_snapshot,
                'adaptive_regime_observe_only': {
                    'phase': adaptive_policy_snapshot.get('phase'),
                    'state': adaptive_policy_snapshot.get('state'),
                    'summary': adaptive_policy_snapshot.get('summary'),
                    'tags': list(adaptive_policy_snapshot.get('tags') or []),
                },
                'entry_decision': {
                    'decision': 'block',
                    'score': 52,
                    'reason_summary': '观测态样本',
                    'breakdown': {
                        'observe_only_phase': adaptive_policy_snapshot.get('phase'),
                        'observe_only_state': adaptive_policy_snapshot.get('state'),
                        'observe_only_summary': adaptive_policy_snapshot.get('summary'),
                        'observe_only_tags': list(adaptive_policy_snapshot.get('tags') or []),
                    },
                },
            }, ensure_ascii=False), filtered=1, filter_reason='方向锁占用中')
            db.create_open_intent(symbol='BTC/USDT:USDT', side='long', signal_id=signal_id, root_signal_id=signal_id, planned_margin=60, leverage=10, layer_no=1, plan_context={'foo': 'bar'})
            snapshot = db.get_execution_state_snapshot()
            self.assertIn('exposure', snapshot)
            self.assertGreater(snapshot['exposure']['projected_total_margin'], snapshot['exposure']['current_total_margin'])
            self.assertTrue(snapshot['signal_decisions'])
            self.assertEqual(snapshot['signal_decisions'][0]['deny_reason'], 'direction_lock')
            self.assertIn('summary', snapshot['signal_decisions'][0]['observe_only'])
            self.assertEqual(snapshot['signal_decisions'][0]['policy_mode'], 'observe_only')
            self.assertTrue(snapshot['signal_decisions'][0]['observe_only_summary'])
            self.assertIn('observe_only', snapshot['signal_decisions'][0]['observe_only_tags'])
            self.assertIn('observe_only_summary', snapshot)
            self.assertIn('top_tags', snapshot['observe_only_summary'])
            self.assertIn('recent_decisions', snapshot['summary'])
            self.assertTrue(snapshot['summary']['observe_only_banner'])

from analytics.helper import build_workflow_approval_records, merge_persisted_approval_state, build_approval_audit_overview, attach_auto_approval_policy, execute_controlled_rollout_layer, execute_controlled_auto_approval_layer, execute_auto_promotion_review_queue_layer, execute_recovery_queue_layer, execute_testnet_bridge_layer, execute_adaptive_rollout_orchestration, execute_rollout_executor, build_rollout_control_plane_manifest, build_control_plane_readiness_summary, build_workflow_consumer_view, build_workflow_recovery_view, build_workflow_attention_view, build_workflow_operator_digest, build_workflow_alert_digest, build_dashboard_summary_cards, build_runtime_orchestration_summary, build_production_rollout_readiness, build_workbench_governance_view, build_workbench_governance_detail_view, build_workbench_merged_timeline, build_workbench_timeline_summary_aggregation, build_unified_workbench_overview, build_auto_promotion_candidate_view, build_auto_promotion_execution_summary, build_auto_promotion_review_queue_consumption, build_auto_promotion_review_queue_filter_view, build_auto_promotion_review_queue_detail_view, _build_follow_up_policy_gate, _build_state_machine_semantics, _build_safe_rollout_action_registry


class TestApprovalPersistence(unittest.TestCase):
    def test_build_workflow_operator_digest_highlights_manual_and_ready_items(self):
        payload = build_workflow_operator_digest({
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'risk_level': 'medium',
                        'approval_required': True,
                        'requires_manual': True,
                        'workflow_state': 'blocked_by_approval',
                        'blocking_reasons': [],
                        'queue_progression': {'status': 'awaiting_approval'},
                    },
                    {
                        'item_id': 'playbook::ready',
                        'title': 'Ready observe item',
                        'action_type': 'joint_observe',
                        'risk_level': 'low',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'ready',
                        'blocking_reasons': [],
                    },
                ],
                'summary': {},
            },
            'approval_state': {
                'items': [
                    {
                        'approval_id': 'approval::manual',
                        'playbook_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'approval_state': 'pending',
                        'decision_state': 'pending',
                        'risk_level': 'medium',
                        'approval_required': True,
                        'requires_manual': True,
                        'blocked_by': [],
                    },
                    {
                        'approval_id': 'approval::ready',
                        'playbook_id': 'playbook::ready',
                        'title': 'Ready observe item',
                        'action_type': 'joint_observe',
                        'approval_state': 'pending',
                        'decision_state': 'ready',
                        'risk_level': 'low',
                        'approval_required': False,
                        'requires_manual': False,
                        'blocked_by': [],
                    },
                ],
                'summary': {},
            },
            'rollout_executor': {'status': 'controlled', 'summary': {}},
        }, transition_journal_overview={
            'schema_version': 'm5_transition_journal_overview_v1',
            'summary': {
                'count': 2,
                'latest_timestamp': '2026-03-28T08:00:00',
                'changed_only': True,
                'changed_field_counts': {'workflow_state': 2},
                'workflow_transition_counts': {'pending->ready': 1, 'blocked_by_approval->ready': 1},
            },
            'recent_transitions': [
                {
                    'item_id': 'playbook::ready',
                    'approval_id': 'approval::ready',
                    'title': 'Ready observe item',
                    'timestamp': '2026-03-28T08:00:00',
                    'trigger': 'state_transition',
                    'actor': 'system',
                    'source': 'workflow_loop',
                    'from': {'workflow_state': 'pending'},
                    'to': {'workflow_state': 'ready'},
                    'changed_fields': ['workflow_state'],
                    'reason': 'observe item promoted',
                    'changed': True,
                }
            ],
            'breakdown': {'changed_field_counts': {'workflow_state': 2}, 'trigger_counts': {'state_transition': 1}, 'actor_counts': {'system': 1}, 'source_counts': {'workflow_loop': 1}},
        })
        self.assertEqual(payload['schema_version'], 'm5_workflow_operator_digest_v1')
        self.assertEqual(payload['headline']['status'], 'attention_required')
        self.assertEqual(payload['summary']['manual_approval_count'], 1)
        self.assertEqual(payload['summary']['ready_count'], 1)
        self.assertEqual(payload['summary']['operator_action_counts']['review_schedule'], 1)
        self.assertEqual(payload['attention']['manual_approval'][0]['item_id'], 'playbook::manual')
        self.assertEqual(payload['attention']['ready'][0]['item_id'], 'playbook::ready')
        self.assertEqual(payload['next_actions'][0]['kind'], 'review_schedule')
        self.assertEqual(payload['operator_action_policies'][0]['operator_action_policy']['action'], 'review_schedule')
        self.assertEqual(payload['group_summaries']['by_lane'][0]['group_id'], 'manual_approval')
        self.assertEqual(payload['group_summaries']['by_lane'][0]['summary']['status_overview']['manual'], 1)
        self.assertIn('gate_consumption', payload['summary'])
        self.assertIn('rollback_candidates', payload['attention'])
        self.assertEqual(payload['next_actions'][0]['summary']['dominant_route'], 'manual_approval_queue')
        self.assertIn('rollout_advisory', payload['summary'])
        self.assertIn('auto_promotion_candidates', payload['attention'])
        self.assertEqual(payload['summary']['transition_count'], 2)
        self.assertEqual(payload['transition_journal']['latest']['workflow_transition'], 'pending->ready')
        self.assertEqual(payload['transition_journal']['recent_transitions'][0]['trigger'], 'state_transition')
        self.assertTrue(payload['next_actions'])
        # follow_up_policy_gate integration
        self.assertIn('follow_up_policy_gate_summary', payload['summary'])
        gate_sum = payload['summary']['follow_up_policy_gate_summary']
        self.assertEqual(gate_sum['item_count'], 2)
        self.assertIn(gate_sum['dominant_decision'], {'review', 'observe', 'retry', 'rollback', 'escalate'})
        self.assertIn('headline', gate_sum)
        self.assertIn('dominant_action', gate_sum)
        self.assertIn('dominant_route', gate_sum)
        # each attention item has follow_up_policy_gate attached
        for item in payload['attention'].get('manual_approval', []) + payload['attention'].get('ready', []):
            self.assertIn('follow_up_policy_gate', item, f"item {item.get('item_id')} missing follow_up_policy_gate")
            self.assertEqual(item['follow_up_policy_gate']['schema_version'], 'm5_follow_up_policy_gate_v1')
            self.assertIn('decision', item['follow_up_policy_gate'])
        # operator_digest headline exposes follow_up_decision
        self.assertIn('follow_up_decision', payload['headline'])
        self.assertIn('follow_up_action', payload['headline'])

    def test_build_workflow_operator_digest_surfaces_validation_gate_freeze(self):
        payload = build_workflow_operator_digest({
            'workflow_state': {'item_states': [{'item_id': 'playbook::ready', 'title': 'Ready observe item', 'action_type': 'joint_observe', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'workflow_state': 'ready', 'blocking_reasons': []}], 'summary': {}},
            'approval_state': {'items': [{'approval_id': 'approval::ready', 'playbook_id': 'playbook::ready', 'title': 'Ready observe item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'ready', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []}], 'summary': {}},
            'rollout_executor': {'status': 'controlled', 'summary': {}},
            'validation_replay': {'summary': {'coverage_matrix': {'schema_version': 'm5_validation_coverage_matrix_v1', 'ready_for_low_intervention_gate': False, 'required_capability_count': 8, 'covered_required_count': 7, 'passing_required_count': 6, 'missing_required': ['testnet_bridge_controlled_execute'], 'failing_required': ['transition_policy_contract']}, 'readiness': {'low_intervention_gate_ready': False, 'missing_required_capabilities': ['testnet_bridge_controlled_execute'], 'failing_required_capabilities': ['transition_policy_contract'], 'failing_case_count': 1}}},
        })
        self.assertEqual(payload['headline']['status'], 'attention_required')
        self.assertFalse(payload['headline']['validation_gate']['ready'])
        self.assertTrue(payload['summary']['validation_gate']['freeze_auto_advance'])
        self.assertIn('missing_required:testnet_bridge_controlled_execute', payload['summary']['validation_gate']['reasons'])

    def test_validation_gate_freeze_reroutes_operator_policy_and_stage_loop(self):
        consumer = build_workflow_consumer_view({
            'workflow_state': {'item_states': [{'item_id': 'playbook::guarded', 'title': 'Guarded rollout candidate', 'action_type': 'joint_stage_prepare', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'workflow_state': 'ready', 'blocking_reasons': [], 'current_rollout_stage': 'observe', 'target_rollout_stage': 'guarded_prepare'}], 'summary': {}},
            'approval_state': {'items': [{'approval_id': 'approval::guarded', 'playbook_id': 'playbook::guarded', 'title': 'Guarded rollout candidate', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'ready', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []}], 'summary': {}},
            'validation_replay': {'summary': {'coverage_matrix': {'schema_version': 'm5_validation_coverage_matrix_v1', 'ready_for_low_intervention_gate': False, 'required_capability_count': 8, 'covered_required_count': 7, 'passing_required_count': 7, 'missing_required': ['testnet_bridge_controlled_execute'], 'failing_required': []}, 'readiness': {'low_intervention_gate_ready': False, 'missing_required_capabilities': ['testnet_bridge_controlled_execute'], 'failing_required_capabilities': [], 'failing_case_count': 0}}},
        })
        item = consumer['workflow_state']['item_states'][0]
        self.assertEqual(item['operator_action_policy']['action'], 'review_schedule')
        self.assertEqual(item['operator_action_policy']['route'], 'validation_review_queue')
        self.assertEqual(item['operator_action_policy']['follow_up'], 'review_validation_freeze')
        self.assertIn('validation_gate_freeze', item['operator_action_policy']['reason_codes'])
        self.assertEqual(item['lane_routing']['lane_id'], 'blocked')
        self.assertEqual(item['lane_routing']['lane_reason'], 'validation_gate_frozen_review_queue')
        self.assertEqual(item['stage_loop']['loop_state'], 'review_pending')
        self.assertEqual(item['stage_loop']['recommended_action'], 'review_schedule')
        self.assertIn('validation_gate_frozen', item['stage_loop']['waiting_on'])

    def test_validation_gate_regression_promotes_rollback_candidate_routing(self):
        consumer = build_workflow_consumer_view({
            'workflow_state': {'item_states': [{'item_id': 'playbook::regressed', 'title': 'Regressed rollout candidate', 'action_type': 'joint_stage_prepare', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'workflow_state': 'queued', 'blocking_reasons': [], 'current_rollout_stage': 'guarded_prepare', 'target_rollout_stage': 'controlled_apply'}], 'summary': {}},
            'approval_state': {'items': [{'approval_id': 'approval::regressed', 'playbook_id': 'playbook::regressed', 'title': 'Regressed rollout candidate', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'ready', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []}], 'summary': {}},
            'validation_replay': {'summary': {'coverage_matrix': {'schema_version': 'm5_validation_coverage_matrix_v1', 'ready_for_low_intervention_gate': False, 'required_capability_count': 8, 'covered_required_count': 8, 'passing_required_count': 7, 'missing_required': [], 'failing_required': ['transition_policy_contract']}, 'readiness': {'low_intervention_gate_ready': False, 'missing_required_capabilities': [], 'failing_required_capabilities': ['transition_policy_contract'], 'failing_case_count': 1}}},
        })
        item = consumer['workflow_state']['item_states'][0]
        self.assertEqual(item['operator_action_policy']['action'], 'freeze_followup')
        self.assertEqual(item['operator_action_policy']['route'], 'rollback_candidate_queue')
        self.assertEqual(item['operator_action_policy']['follow_up'], 'rollback_candidate_review')
        self.assertIn('validation_gate_regression', item['operator_action_policy']['reason_codes'])
        self.assertEqual(item['lane_routing']['lane_id'], 'rollback_candidate')
        self.assertTrue(item['lane_routing']['validation_regression'])
        self.assertEqual(item['stage_loop']['loop_state'], 'rollback_prepare')
        self.assertIn('validation_gate_regressed', item['stage_loop']['waiting_on'])
        workbench = build_workbench_governance_view({
            'consumer_view': consumer,
            'workflow_state': consumer['workflow_state'],
            'approval_state': consumer['approval_state'],
            'validation_replay': {'summary': {'coverage_matrix': {'schema_version': 'm5_validation_coverage_matrix_v1', 'ready_for_low_intervention_gate': False, 'required_capability_count': 8, 'covered_required_count': 8, 'passing_required_count': 7, 'missing_required': [], 'failing_required': ['transition_policy_contract']}, 'readiness': {'low_intervention_gate_ready': False, 'missing_required_capabilities': [], 'failing_required_capabilities': ['transition_policy_contract'], 'failing_case_count': 1}}},
        }, max_items=5)
        rollback_lane = workbench['lanes']['rollback_candidate']
        self.assertEqual(rollback_lane['items'][0]['operator_action'], 'freeze_followup')
        self.assertEqual(rollback_lane['items'][0]['operator_follow_up'], 'rollback_candidate_review')
        self.assertEqual(rollback_lane['stage_loop']['dominant_path'], 'rollback_prepare')


    def test_build_dashboard_summary_cards_aggregates_digest_attention_and_execution(self):
        payload = build_dashboard_summary_cards({
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'risk_level': 'medium',
                        'approval_required': True,
                        'requires_manual': True,
                        'workflow_state': 'blocked_by_approval',
                        'blocking_reasons': [],
                    },
                    {
                        'item_id': 'playbook::queued',
                        'title': 'Queued observe item',
                        'action_type': 'joint_stage_prepare',
                        'risk_level': 'low',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'queued',
                        'blocking_reasons': [],
                    },
                    {
                        'item_id': 'playbook::ready',
                        'title': 'Ready observe item',
                        'action_type': 'joint_observe',
                        'risk_level': 'low',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'ready',
                        'blocking_reasons': [],
                    },
                ],
                'summary': {'queued_count': 1},
            },
            'approval_state': {
                'items': [
                    {
                        'approval_id': 'approval::manual',
                        'playbook_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'approval_state': 'pending',
                        'decision_state': 'pending',
                        'risk_level': 'medium',
                        'approval_required': True,
                        'requires_manual': True,
                        'blocked_by': [],
                    },
                ],
                'summary': {'pending_count': 1, 'roles': ['operator']},
            },
            'rollout_executor': {'status': 'controlled', 'summary': {'by_disposition': {'queued': 1}}},
            'controlled_rollout_execution': {'mode': 'state_apply', 'executed_count': 1, 'skipped_count': 2},
            'auto_approval_execution': {'mode': 'dry_run', 'executed_count': 0, 'skipped_count': 1},
            'validation_replay': {'summary': {'coverage_matrix': {'schema_version': 'm5_validation_coverage_matrix_v1', 'ready_for_low_intervention_gate': False, 'required_capability_count': 8, 'covered_required_count': 7, 'passing_required_count': 6, 'missing_required': ['testnet_bridge_controlled_execute'], 'failing_required': ['transition_policy_contract']}, 'readiness': {'low_intervention_gate_ready': False, 'missing_required_capabilities': ['testnet_bridge_controlled_execute'], 'failing_required_capabilities': ['transition_policy_contract'], 'failing_case_count': 1}}},
        }, max_items=2)
        self.assertEqual(payload['schema_version'], 'm5_dashboard_summary_cards_v1')
        self.assertEqual(payload['summary']['manual_approval_count'], 1)
        self.assertEqual(payload['summary']['queued_count'], 0)
        self.assertEqual(payload['summary']['ready_count'], 0)
        self.assertEqual(payload['summary']['rollback_candidate_count'], 2)
        self.assertEqual(payload['summary']['bridge_mode'], 'state_apply')
        self.assertEqual(payload['summary']['approval_roles'], ['operator'])
        self.assertEqual(payload['summary']['operator_action_counts']['review_schedule'], 1)
        self.assertEqual(payload['summary']['operator_action_counts']['freeze_followup'], 2)
        self.assertIn('gate_consumption', payload['summary'])
        self.assertIn('validation_gate', payload['summary'])
        self.assertIn('validation_gate_consumption', payload['summary'])
        self.assertIn('rollback_candidate', payload['card_index']['workflow_overview']['metrics'])
        self.assertIn('validation_gate', payload['card_index'])
        self.assertEqual(payload['card_index']['workflow_overview']['metrics']['manual'], 1)
        self.assertEqual(payload['card_index']['execution_status']['metrics']['bridge_executed'], 1)
        self.assertLessEqual(len(payload['card_index']['key_alerts']['items']), 2)
        self.assertLessEqual(len(payload['card_index']['stage_progression']['items']), 2)

    def test_build_workbench_governance_view_aggregates_lanes_rollout_and_recent_adjustments(self):
        payload = build_workbench_governance_view({
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'risk_level': 'high',
                        'approval_required': True,
                        'requires_manual': True,
                        'workflow_state': 'blocked_by_approval',
                        'blocking_reasons': [],
                        'current_rollout_stage': 'guarded',
                        'target_rollout_stage': 'expanded',
                    },
                    {
                        'item_id': 'playbook::queued',
                        'title': 'Queued stage item',
                        'action_type': 'joint_stage_prepare',
                        'risk_level': 'medium',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'queued',
                        'blocking_reasons': [],
                        'auto_approval_decision': 'auto_approve',
                        'auto_approval_eligible': True,
                        'current_rollout_stage': 'observe',
                        'target_rollout_stage': 'guarded',
                    },
                    {
                        'item_id': 'playbook::ready',
                        'title': 'Ready auto item',
                        'action_type': 'joint_observe',
                        'risk_level': 'low',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'ready',
                        'blocking_reasons': [],
                        'auto_approval_decision': 'auto_approve',
                        'auto_approval_eligible': True,
                        'current_rollout_stage': 'observe',
                        'target_rollout_stage': 'observe',
                    },
                ],
                'summary': {},
            },
            'approval_state': {
                'items': [
                    {
                        'approval_id': 'approval::manual',
                        'playbook_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'approval_state': 'pending',
                        'decision_state': 'pending',
                        'risk_level': 'high',
                        'approval_required': True,
                        'requires_manual': True,
                        'blocked_by': [],
                    },
                    {
                        'approval_id': 'approval::queued',
                        'playbook_id': 'playbook::queued',
                        'title': 'Queued stage item',
                        'action_type': 'joint_stage_prepare',
                        'approval_state': 'pending',
                        'decision_state': 'queued',
                        'risk_level': 'medium',
                        'approval_required': False,
                        'requires_manual': False,
                        'blocked_by': [],
                    },
                ],
                'summary': {},
            },
            'rollout_executor': {
                'status': 'controlled',
                'summary': {'by_status': {'applied': 1, 'queued': 1}},
                'items': [
                    {
                        'item_id': 'approval::queued',
                        'playbook_id': 'playbook::queued',
                        'action_type': 'joint_stage_prepare',
                        'status': 'queued',
                        'plan': {'rollout_stage': 'observe', 'target_rollout_stage': 'guarded'},
                        'result': {'reason': 'queued for guarded rollout'},
                    }
                ],
            },
            'controlled_rollout_execution': {
                'mode': 'state_apply',
                'executed_count': 1,
                'skipped_count': 0,
                'items': [
                    {
                        'item_id': 'approval::ready',
                        'playbook_id': 'playbook::ready',
                        'action_type': 'joint_observe',
                        'action': 'state_applied',
                        'state': 'ready',
                        'workflow_state': 'ready',
                        'reason': 'controlled rollout applied',
                    }
                ],
            },
            'auto_approval_execution': {
                'mode': 'controlled',
                'executed_count': 1,
                'skipped_count': 0,
                'items': [
                    {
                        'item_id': 'approval::ready',
                        'playbook_id': 'playbook::ready',
                        'action_type': 'joint_observe',
                        'action': 'approved',
                        'state': 'approved',
                        'workflow_state': 'ready',
                        'reason': 'auto approved',
                    }
                ],
            },
        }, max_items=2, max_adjustments=5, transition_journal_overview={
            'schema_version': 'm5_transition_journal_overview_v1',
            'summary': {
                'count': 3,
                'latest_timestamp': '2026-03-28T08:10:00',
                'changed_only': True,
                'changed_field_counts': {'workflow_state': 3},
                'workflow_transition_counts': {'pending->queued': 1, 'queued->ready': 1, 'blocked_by_approval->ready': 1},
            },
            'recent_transitions': [
                {
                    'item_id': 'playbook::queued',
                    'approval_id': 'approval::queued',
                    'title': 'Queued stage item',
                    'timestamp': '2026-03-28T08:10:00',
                    'trigger': 'queue_promoted',
                    'actor': 'system',
                    'source': 'rollout_executor',
                    'from': {'workflow_state': 'pending'},
                    'to': {'workflow_state': 'queued'},
                    'changed_fields': ['workflow_state'],
                    'reason': 'queued for guarded rollout',
                    'changed': True,
                }
            ],
            'breakdown': {'changed_field_counts': {'workflow_state': 3}, 'trigger_counts': {'queue_promoted': 1}, 'actor_counts': {'system': 1}, 'source_counts': {'rollout_executor': 1}},
        })
        self.assertEqual(payload['schema_version'], 'm5_workbench_governance_view_v2')
        self.assertEqual(payload['summary']['auto_batch_count'], 2)
        self.assertIn('gate_consumption', payload['summary'])
        self.assertIn('rollback_candidate', payload['lanes'])
        self.assertEqual(payload['summary']['blocked_count'], 0)
        self.assertEqual(payload['summary']['queued_count'], 0)
        self.assertEqual(payload['summary']['ready_count'], 0)
        self.assertIn('playbook::ready', [row['item_id'] for row in payload['lanes']['auto_batch']['items']])
        self.assertEqual(payload['lanes']['manual_approval']['items'][0]['item_id'], 'playbook::manual')
        self.assertTrue(payload['rollout']['frontier'])
        self.assertEqual(payload['recent_adjustments'][0]['source'], 'auto_approval_execution')
        self.assertGreaterEqual(payload['summary']['recent_adjustment_count'], 2)
        self.assertEqual(payload['summary']['transition_count'], 3)
        self.assertEqual(payload['transition_journal']['latest']['workflow_transition'], 'pending->queued')
        self.assertEqual(payload['summary']['stage_loop']['path_counts']['auto_advance'], 2)
        self.assertEqual(payload['summary']['stage_loop']['path_counts']['review_pending'], 1)
        self.assertEqual(payload['lanes']['manual_approval']['stage_loop']['dominant_path'], 'review_pending')
        self.assertEqual(payload['lanes']['manual_approval']['items'][0]['stage_loop']['loop_state'], 'review_pending')
        self.assertEqual(payload['lanes']['manual_approval']['low_intervention_summary']['dominant_follow_up'], 'await_manual_approval')
        self.assertIn('rollout_advisory', payload['summary'])
        self.assertIn('auto_promotion_candidate_queue', payload['rollout'])
        self.assertEqual(next(row for row in payload['group_summaries']['by_operator_route'] if row['group_id'] == 'manual_approval_queue')['summary']['dominant_follow_up'], 'await_manual_approval')

    def test_build_workflow_operator_digest_exposes_control_plane_readiness(self):
        digest = build_workflow_operator_digest({
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::queued',
                        'title': 'Queued stage item',
                        'action_type': 'joint_stage_prepare',
                        'risk_level': 'medium',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'queued',
                        'blocking_reasons': [],
                        'auto_approval_decision': 'auto_approve',
                        'auto_approval_eligible': True,
                        'current_rollout_stage': 'observe',
                        'target_rollout_stage': 'guarded',
                        'state_machine': _build_state_machine_semantics(
                            item_id='approval::queued',
                            approval_state='approved',
                            workflow_state='queued',
                            validation_gate={
                                'enabled': True,
                                'ready': False,
                                'freeze_auto_advance': True,
                                'rollback_on_regression': False,
                                'reasons': ['coverage_gap'],
                                'missing_required_capabilities': ['transition_policy_contract'],
                            },
                        ),
                    },
                ],
                'summary': {'item_count': 1},
            },
            'approval_state': {'items': [], 'summary': {}},
        }, max_items=3)
        self.assertEqual(digest['control_plane_readiness']['schema_version'], 'm5_control_plane_readiness_summary_v1')
        self.assertEqual(digest['summary']['control_plane_readiness']['relation'], 'validation_freeze_blocks_auto_promotion')
        self.assertFalse(digest['control_plane_readiness']['can_continue_auto_promotion'])
        self.assertIn('control_plane_readiness', digest['related_summary'])

    def test_build_unified_workbench_overview_summarizes_approval_rollout_and_recovery_lines(self):
        payload = build_unified_workbench_overview({
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'risk_level': 'high',
                        'approval_required': True,
                        'requires_manual': True,
                        'workflow_state': 'blocked_by_approval',
                        'blocking_reasons': [],
                        'current_rollout_stage': 'guarded',
                        'target_rollout_stage': 'expanded',
                        'state_machine': _build_state_machine_semantics(item_id='approval::manual', approval_state='pending', workflow_state='blocked_by_approval', dispatch_route='manual_review_queue', next_transition='await_manual_approval'),
                    },
                    {
                        'item_id': 'playbook::queued',
                        'title': 'Queued stage item',
                        'action_type': 'joint_stage_prepare',
                        'risk_level': 'medium',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'queued',
                        'blocking_reasons': [],
                        'auto_approval_decision': 'auto_approve',
                        'auto_approval_eligible': True,
                        'current_rollout_stage': 'observe',
                        'target_rollout_stage': 'guarded',
                    },
                    {
                        'item_id': 'playbook::recover',
                        'title': 'Recovery item',
                        'action_type': 'joint_stage_prepare',
                        'workflow_state': 'execution_failed',
                        'risk_level': 'critical',
                        'approval_required': False,
                        'requires_manual': False,
                        'blocking_reasons': ['critical_risk'],
                        'state_machine': _build_state_machine_semantics(item_id='approval::recover', approval_state='pending', workflow_state='execution_failed', execution_status='error', retryable=False, blocked_by=['critical_risk']),
                    },
                ],
                'summary': {'item_count': 3},
            },
            'approval_state': {
                'items': [
                    {
                        'approval_id': 'approval::manual',
                        'playbook_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'approval_state': 'pending',
                        'decision_state': 'pending',
                        'risk_level': 'high',
                        'approval_required': True,
                        'requires_manual': True,
                        'blocked_by': [],
                    },
                    {
                        'approval_id': 'approval::recover',
                        'playbook_id': 'playbook::recover',
                        'title': 'Recovery item',
                        'action_type': 'joint_stage_prepare',
                        'approval_state': 'pending',
                        'decision_state': 'pending',
                        'risk_level': 'critical',
                        'approval_required': False,
                        'requires_manual': False,
                        'blocked_by': ['critical_risk'],
                    },
                ],
                'summary': {'pending_count': 2},
            },
            'rollout_executor': {'status': 'controlled', 'summary': {'by_status': {'queued': 1}}},
            'controlled_rollout_execution': {'mode': 'state_apply', 'executed_count': 1, 'items': []},
            'auto_approval_execution': {'mode': 'controlled', 'executed_count': 1, 'items': []},
            'validation_replay': {'summary': {'coverage_matrix': {'schema_version': 'm5_validation_coverage_matrix_v1', 'ready_for_low_intervention_gate': False, 'required_capability_count': 8, 'covered_required_count': 7, 'passing_required_count': 6, 'missing_required': ['testnet_bridge_controlled_execute'], 'failing_required': ['transition_policy_contract']}, 'readiness': {'low_intervention_gate_ready': False, 'missing_required_capabilities': ['testnet_bridge_controlled_execute'], 'failing_required_capabilities': ['transition_policy_contract'], 'failing_case_count': 1}}},
        }, max_items=2, transition_journal_overview={
            'schema_version': 'm5_transition_journal_overview_v1',
            'summary': {
                'count': 2,
                'latest_timestamp': '2026-03-28T08:20:00',
                'changed_only': True,
                'changed_field_counts': {'workflow_state': 2},
                'workflow_transition_counts': {'blocked_by_approval->ready': 1, 'execution_failed->blocked': 1},
            },
            'recent_transitions': [
                {
                    'item_id': 'playbook::recover',
                    'approval_id': 'approval::recover',
                    'title': 'Recovery item',
                    'timestamp': '2026-03-28T08:20:00',
                    'trigger': 'recovery_blocked',
                    'actor': 'system',
                    'source': 'recovery_loop',
                    'from': {'workflow_state': 'execution_failed'},
                    'to': {'workflow_state': 'blocked'},
                    'changed_fields': ['workflow_state'],
                    'reason': 'requires guarded recovery',
                    'changed': True,
                }
            ],
            'breakdown': {'changed_field_counts': {'workflow_state': 2}, 'trigger_counts': {'recovery_blocked': 1}, 'actor_counts': {'system': 1}, 'source_counts': {'recovery_loop': 1}},
        })
        self.assertEqual(payload['schema_version'], 'm5_unified_workbench_overview_v1')
        self.assertEqual(payload['headline']['status'], 'attention_required')
        self.assertIn('control_plane_readiness', payload['summary'])
        self.assertIn('related_summary', payload)
        self.assertTrue(payload['control_plane_readiness']['control_plane_compatible'])
        self.assertEqual(payload['headline']['dominant_line'], 'approval')
        self.assertEqual(payload['summary']['line_states']['approval'], 'attention_required')
        self.assertIn('gate_consumption', payload['summary'])
        self.assertEqual(payload['summary']['line_states']['rollout'], 'blocked')
        self.assertEqual(payload['summary']['line_states']['recovery'], 'recovery_required')
        self.assertFalse(payload['summary']['validation_gate']['ready'])
        self.assertIn('missing_required:testnet_bridge_controlled_execute', payload['summary']['validation_gate']['reasons'])
        self.assertEqual(payload['lines']['approval']['counts']['pending'], 2)
        self.assertEqual(payload['lines']['rollout']['counts']['queued'], 0)
        self.assertEqual(payload['lines']['rollout']['counts']['validation_gap_count'], 2)
        self.assertEqual(payload['lines']['recovery']['counts']['manual_recovery'], 1)
        self.assertEqual(payload['lines']['approval']['next_actions'][0]['kind'], 'review_schedule')
        self.assertEqual(payload['lines']['recovery']['next_actions'][0]['kind'], 'manual_recovery')
        self.assertTrue(payload['top_key_alerts'])
        self.assertTrue(payload['top_next_actions'])
        self.assertEqual(payload['summary']['transition_count'], 2)
        self.assertEqual(payload['summary']['stage_loop']['rollout']['path_counts']['review_pending'], 1)
        self.assertEqual(payload['summary']['stage_loop']['rollout']['path_counts']['rollback_prepare'], 2)
        self.assertEqual(payload['summary']['stage_loop']['rollout']['path_counts']['hold'], 0)
        self.assertEqual(payload['lines']['rollout']['stage_loop']['dominant_path'], 'rollback_prepare')
        self.assertIn('rollout_advisory', payload['summary'])
        self.assertIn('rollout_advisory', payload['lines']['rollout'])
        self.assertIn('auto_promotion_candidate_queue', payload['lines']['rollout'])
        self.assertEqual(payload['transition_journal']['latest']['workflow_transition'], 'execution_failed->blocked')
        self.assertEqual(payload['upstreams']['workflow_recovery_view']['summary']['manual_recovery_count'], 1)
        # follow_up_policy_gate_summary flows through unified_overview
        self.assertIn('follow_up_policy_gate_summary', payload)
        gate_sum = payload['follow_up_policy_gate_summary']
        self.assertEqual(gate_sum['item_count'], 3)
        self.assertIn('dominant_decision', gate_sum)
        self.assertIn('dominant_action', gate_sum)
        self.assertIn('dominant_route', gate_sum)
        self.assertIn('headline', gate_sum)
        self.assertIn('summary', gate_sum)
        self.assertIn('follow_up_policy_gate_summary', payload['summary'])
        self.assertEqual(payload['summary']['follow_up_policy_gate_summary']['item_count'], 3)

    def test_build_production_rollout_readiness_surfaces_fixed_gate_and_runbook_actions(self):
        payload = build_production_rollout_readiness({
            'adaptive_rollout_orchestration': {
                'schema_version': 'm5_adaptive_rollout_orchestration_v1',
                'passes': [],
                'summary': {'pass_count': 0, 'rerun_triggered': False, 'rollout_executor_applied_count': 0, 'controlled_rollout_executed_count': 0, 'auto_approval_executed_count': 0, 'review_queue_queued_count': 1},
            },
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'risk_level': 'high',
                        'approval_required': True,
                        'requires_manual': True,
                        'workflow_state': 'blocked_by_approval',
                        'blocking_reasons': [],
                        'state_machine': _build_state_machine_semantics(item_id='approval::manual', approval_state='pending', workflow_state='blocked_by_approval', dispatch_route='manual_review_queue', next_transition='await_manual_approval'),
                    },
                    {
                        'item_id': 'playbook::rollback',
                        'title': 'Rollback review item',
                        'action_type': 'joint_stage_prepare',
                        'risk_level': 'critical',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'execution_failed',
                        'blocking_reasons': ['drawdown_guard'],
                        'rollback_gate': {'candidate': True, 'triggered': ['drawdown_guard']},
                        'state_machine': _build_state_machine_semantics(item_id='approval::rollback', approval_state='pending', workflow_state='execution_failed', execution_status='error', retryable=False, blocked_by=['drawdown_guard']),
                    },
                ],
                'summary': {'item_count': 2},
            },
            'approval_state': {
                'items': [
                    {'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'high', 'approval_required': True, 'requires_manual': True, 'blocked_by': []},
                    {'approval_id': 'approval::rollback', 'playbook_id': 'playbook::rollback', 'title': 'Rollback review item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'critical', 'approval_required': False, 'requires_manual': False, 'blocked_by': ['drawdown_guard']},
                ],
                'summary': {'pending_count': 2},
            },
            'auto_promotion_review_execution': {
                'summary': {'rollback_review_queue_count': 1, 'review_due_count': 1},
                'items': [{'item_id': 'playbook::rollback', 'queue_kind': 'rollback_review_queue'}],
            },
            'rollout_executor': {'status': 'controlled', 'summary': {'applied_count': 0}, 'items': []},
            'controlled_rollout_execution': {'mode': 'state_apply', 'executed_count': 0, 'items': []},
            'auto_approval_execution': {'mode': 'controlled', 'executed_count': 0, 'items': []},
            'validation_replay': {'summary': {'coverage_matrix': {'schema_version': 'm5_validation_coverage_matrix_v1', 'ready_for_low_intervention_gate': False, 'required_capability_count': 8, 'covered_required_count': 7, 'passing_required_count': 6, 'missing_required': ['testnet_bridge_controlled_execute'], 'failing_required': []}, 'readiness': {'low_intervention_gate_ready': False, 'missing_required_capabilities': ['testnet_bridge_controlled_execute'], 'failing_required_capabilities': [], 'failing_case_count': 0}}},
        }, transition_journal_overview={'schema_version': 'm5_transition_journal_overview_v1', 'summary': {'count': 0, 'latest_timestamp': None, 'changed_only': True, 'changed_field_counts': {}, 'workflow_transition_counts': {}}, 'recent_transitions': [], 'breakdown': {}})
        self.assertEqual(payload['schema_version'], 'm5_production_rollout_readiness_v1')
        self.assertFalse(payload['production_ready'])
        self.assertEqual(payload['status'], 'blocked')
        self.assertIn('manual_approval_backlog', payload['blocking_issues'])
        self.assertIn('rollback_review_backlog', payload['blocking_issues'])
        self.assertIn('validation_gate_not_ready', payload['blocking_issues'])
        self.assertTrue(payload['runbook_actions'])
        self.assertEqual(payload['runbook_actions'][0]['kind'], 'manual_approval_clearance')
        self.assertIn('control_plane_readiness', payload['related_summary'])
        self.assertIn('workflow_alert_digest', payload['upstreams'])

    def test_build_runtime_orchestration_summary_surfaces_recent_progress_stuck_points_and_followups(self):
        payload = build_runtime_orchestration_summary({
            'adaptive_rollout_orchestration': {
                'schema_version': 'm5_adaptive_rollout_orchestration_v1',
                'passes': [
                    {'label': 'pre_auto_approval', 'dry_run': True, 'rollout_executor_applied_count': 1},
                    {'label': 'post_auto_approval', 'dry_run': False, 'rollout_executor_applied_count': 2},
                ],
                'summary': {
                    'pass_count': 2,
                    'rerun_triggered': True,
                    'rerun_reason': 'auto_approval_promoted_ready_items',
                    'rerun_reasons': [],
                    'rollout_executor_applied_count': 3,
                    'controlled_rollout_executed_count': 1,
                    'auto_approval_executed_count': 1,
                    'review_queue_queued_count': 2,
                },
            },
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'risk_level': 'high',
                        'approval_required': True, 'requires_manual': True, 'workflow_state': 'blocked_by_approval', 'blocking_reasons': [],
                        'queue_progression': {'status': 'awaiting_approval', 'dispatch_route': 'manual_approval_queue', 'next_transition': 'manual_review'},
                        'state_machine': _build_state_machine_semantics(item_id='playbook::manual', approval_state='pending', workflow_state='blocked_by_approval', blocked_by=[], retryable=False),
                    },
                    {
                        'item_id': 'playbook::promo', 'title': 'Promotion item', 'action_type': 'joint_stage_prepare', 'risk_level': 'low',
                        'approval_required': False, 'requires_manual': False, 'workflow_state': 'review_pending', 'blocking_reasons': [],
                        'current_rollout_stage': 'guarded_prepare', 'target_rollout_stage': 'controlled_apply',
                        'scheduled_review': {'review_due_at': '2026-03-28T10:00:00'},
                        'auto_promotion_execution': {
                            'reason_codes': ['auto_advance_allowed'],
                            'candidate_summary': {'risk_label': 'low', 'risk_score': 0.1, 'manual_fallback_required': False, 'why_promotable': ['stability_window_passed']},
                            'before': {'rollout_stage': 'guarded_prepare', 'workflow_state': 'ready'},
                            'after': {'rollout_stage': 'controlled_apply', 'workflow_state': 'review_pending', 'state': 'approved'},
                            'event_log': [{'created_at': '2026-03-28T09:30:00', 'actor': 'system', 'source': 'controlled_rollout', 'event_type': 'auto_promoted'}],
                        },
                    },
                    {
                        'item_id': 'playbook::rollback', 'title': 'Rollback candidate', 'action_type': 'joint_stage_prepare', 'risk_level': 'critical',
                        'approval_required': False, 'requires_manual': False, 'workflow_state': 'execution_failed', 'blocking_reasons': ['drawdown_guard'],
                        'current_rollout_stage': 'controlled_apply', 'target_rollout_stage': 'rollback_prepare',
                        'rollback_gate': {'candidate': True, 'triggered': ['drawdown_guard']},
                        'state_machine': _build_state_machine_semantics(item_id='playbook::rollback', approval_state='pending', workflow_state='execution_failed', blocked_by=['drawdown_guard'], retryable=False),
                    },
                ],
                'summary': {'item_count': 3, 'ready_count': 0},
            },
            'approval_state': {
                'items': [
                    {'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'high', 'approval_required': True, 'requires_manual': True, 'blocked_by': []},
                    {'approval_id': 'approval::promo', 'playbook_id': 'playbook::promo', 'title': 'Promotion item', 'action_type': 'joint_stage_prepare', 'approval_state': 'approved', 'decision_state': 'approved', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': [], 'scheduled_review': {'review_due_at': '2026-03-28T10:00:00'}},
                    {'approval_id': 'approval::rollback', 'playbook_id': 'playbook::rollback', 'title': 'Rollback candidate', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'critical', 'approval_required': False, 'requires_manual': False, 'blocked_by': ['drawdown_guard']},
                ],
                'summary': {'pending_count': 2, 'approved_count': 1, 'rejected_count': 0, 'deferred_count': 0},
            },
            'rollout_executor': {'status': 'controlled', 'summary': {'applied_count': 3}, 'items': [{'item_id': 'playbook::promo', 'title': 'Promotion item', 'status': 'applied', 'action_type': 'joint_stage_prepare'}]},
            'auto_approval_execution': {'mode': 'controlled', 'executed_count': 1, 'skipped_count': 0, 'items': [{'item_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'action': 'approved', 'state': 'approved', 'workflow_state': 'ready'}]},
            'controlled_rollout_execution': {'mode': 'state_apply', 'executed_count': 1, 'skipped_count': 0, 'items': [{'item_id': 'approval::promo', 'playbook_id': 'playbook::promo', 'title': 'Promotion item', 'action_type': 'joint_stage_prepare', 'action': 'applied', 'state': 'approved', 'workflow_state': 'review_pending', 'rollout_stage': 'controlled_apply'}]},
        }, transition_journal_overview={
            'schema_version': 'm5_transition_journal_overview_v1',
            'summary': {'count': 2, 'latest_timestamp': '2026-03-28T09:30:00', 'changed_only': True, 'changed_field_counts': {'workflow_state': 2}, 'workflow_transition_counts': {'pending->ready': 1, 'ready->review_pending': 1}},
            'recent_transitions': [
                {'item_id': 'playbook::promo', 'approval_id': 'approval::promo', 'title': 'Promotion item', 'timestamp': '2026-03-28T09:30:00', 'trigger': 'auto_promoted', 'actor': 'system', 'source': 'controlled_rollout', 'from': {'workflow_state': 'ready'}, 'to': {'workflow_state': 'review_pending'}, 'changed_fields': ['workflow_state'], 'reason': 'promotion applied', 'changed': True},
            ],
        })
        self.assertEqual(payload['schema_version'], 'm5_runtime_orchestration_summary_v1')
        self.assertEqual(payload['summary']['pass_count'], 2)
        self.assertEqual(payload['summary']['rerun_count'], 1)
        self.assertEqual(payload['summary']['rerun_observability']['primary_reason'], 'auto_approval_promoted_ready_items')
        self.assertTrue(payload['summary']['follow_up_required'])
        self.assertEqual(payload['next_step']['route'], 'manual_approval_queue')
        self.assertEqual(payload['follow_ups']['summary']['rollback_candidate_count'], 1)
        self.assertEqual(payload['follow_ups']['summary']['post_promotion_review_queue_count'], 1)
        self.assertTrue(any(row['source'] == 'manual_approval' for row in payload['stuck_points']))
        self.assertTrue(any(row['source'] == 'rollback_candidate' for row in payload['stuck_points']))
        self.assertTrue(any(row['kind'] == 'orchestration_rerun' for row in payload['recent_progress']))
        self.assertEqual(payload['transition_journal']['latest']['workflow_transition'], 'ready->review_pending')
        self.assertIn('control_plane_readiness', payload['related_summary'])


    def test_build_unified_workbench_overview_accepts_legacy_filter_kwargs(self):
        payload = build_unified_workbench_overview({
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'risk_level': 'high',
                        'approval_required': True,
                        'requires_manual': True,
                        'workflow_state': 'blocked_by_approval',
                    },
                    {
                        'item_id': 'playbook::ready',
                        'title': 'Ready item',
                        'action_type': 'joint_stage_prepare',
                        'risk_level': 'low',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'ready',
                    },
                ],
                'summary': {'item_count': 2},
            },
            'approval_state': {
                'items': [
                    {
                        'approval_id': 'approval::manual',
                        'playbook_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'approval_state': 'pending',
                        'decision_state': 'pending',
                        'risk_level': 'high',
                        'approval_required': True,
                        'requires_manual': True,
                    },
                ],
                'summary': {'pending_count': 1},
            },
        }, lane_ids='manual_approval')
        self.assertEqual(payload['summary']['filtered_item_count'], 1)
        self.assertEqual(payload['upstreams']['workbench_governance_view']['applied_filters']['lane_ids'], ['manual_approval'])
        self.assertEqual(payload['lines']['approval']['counts']['manual_approval'], 1)

    def test_rollout_stage_advisory_flows_into_digest_workbench_and_unified_overview(self):
        payload = {
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::promo',
                        'title': 'Promotion ready item',
                        'action_type': 'joint_stage_prepare',
                        'risk_level': 'low',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'ready',
                        'blocking_reasons': [],
                        'auto_approval_decision': 'auto_approve',
                        'auto_approval_eligible': True,
                        'current_rollout_stage': 'guarded_prepare',
                        'target_rollout_stage': 'controlled_apply',
                        'stage_handler': {
                            'stage_key': 'guarded_prepare',
                            'advisory': {
                                'recommended_stage': 'controlled_apply',
                                'recommended_action': 'promote_to_controlled_apply',
                                'urgency': 'high',
                                'confidence': 0.93,
                                'reasons': ['auto_advance_allowed'],
                                'ready_for_live_promotion': True,
                            },
                        },
                        'state_machine': {
                            'auto_advance_gate': {'allowed': True, 'readiness_score': 100, 'blockers': []},
                            'rollback_gate': {'candidate': False, 'triggered': []},
                        },
                    },
                ],
                'summary': {'item_count': 1},
            },
            'approval_state': {
                'items': [
                    {
                        'approval_id': 'approval::promo',
                        'playbook_id': 'playbook::promo',
                        'title': 'Promotion ready item',
                        'action_type': 'joint_stage_prepare',
                        'approval_state': 'pending',
                        'decision_state': 'ready',
                        'risk_level': 'low',
                        'approval_required': False,
                        'requires_manual': False,
                        'blocked_by': [],
                    },
                ],
                'summary': {'pending_count': 1},
            },
            'rollout_executor': {'status': 'controlled', 'summary': {}},
        }
        digest = build_workflow_operator_digest(payload, max_items=5)
        self.assertEqual(digest['summary']['rollout_advisory']['auto_promotion_candidate_count'], 1)
        self.assertTrue(digest['attention']['auto_promotion_candidates'])
        self.assertEqual(digest['attention']['auto_promotion_candidates'][0]['recommended_action'], 'promote_to_controlled_apply')
        workbench = build_workbench_governance_view(payload, max_items=5)
        self.assertEqual(workbench['summary']['rollout_advisory']['dominant_action'], 'promote_to_controlled_apply')
        self.assertEqual(workbench['rollout']['auto_promotion_candidate_queue'][0]['item_id'], 'playbook::promo')
        overview = build_unified_workbench_overview(payload, max_items=5)
        self.assertEqual(overview['summary']['rollout_advisory']['ready_for_live_promotion_count'], 1)
        self.assertEqual(overview['lines']['rollout']['counts']['ready_for_live_promotion'], 1)
        self.assertEqual(overview['lines']['rollout']['auto_promotion_candidate_queue'][0]['recommended_stage'], 'controlled_apply')

    def test_build_auto_promotion_candidate_view_exposes_ready_and_blocked_items(self):
        payload = {
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::promo',
                        'title': 'Promotion ready item',
                        'action_type': 'joint_stage_prepare',
                        'risk_level': 'low',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'ready',
                        'blocking_reasons': [],
                        'auto_approval_decision': 'auto_approve',
                        'auto_approval_eligible': True,
                        'current_rollout_stage': 'guarded_prepare',
                        'target_rollout_stage': 'controlled_apply',
                        'stage_handler': {'stage_key': 'guarded_prepare', 'advisory': {'recommended_stage': 'controlled_apply', 'recommended_action': 'promote_to_controlled_apply', 'urgency': 'high', 'confidence': 0.93, 'reasons': ['auto_advance_allowed'], 'ready_for_live_promotion': True}},
                        'state_machine': {'auto_advance_gate': {'allowed': True, 'readiness_score': 100, 'blockers': []}, 'rollback_gate': {'candidate': False, 'triggered': []}},
                    },
                    {
                        'item_id': 'playbook::blocked',
                        'title': 'Blocked promotion item',
                        'action_type': 'joint_stage_prepare',
                        'risk_level': 'medium',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'queued',
                        'blocking_reasons': ['validation_gap'],
                        'auto_approval_decision': 'auto_approve',
                        'auto_approval_eligible': True,
                        'current_rollout_stage': 'guarded_prepare',
                        'target_rollout_stage': 'controlled_apply',
                        'stage_handler': {'stage_key': 'guarded_prepare', 'advisory': {'recommended_stage': 'guarded_prepare', 'recommended_action': 'hold_until_blockers_clear', 'urgency': 'medium', 'confidence': 0.8, 'reasons': ['validation_gap'], 'ready_for_live_promotion': False}},
                        'state_machine': {'auto_advance_gate': {'allowed': False, 'readiness_score': 40, 'blockers': ['validation_gate:not_ready']}, 'rollback_gate': {'candidate': False, 'triggered': []}},
                        'validation_gate': {'enabled': True, 'ready': False, 'freeze_auto_advance': True, 'rollback_on_regression': True, 'reasons': ['missing_required:testnet_bridge_controlled_execute']},
                    },
                ],
                'summary': {'item_count': 2},
            },
            'approval_state': {
                'items': [
                    {'approval_id': 'approval::promo', 'playbook_id': 'playbook::promo', 'title': 'Promotion ready item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'ready', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []},
                    {'approval_id': 'approval::blocked', 'playbook_id': 'playbook::blocked', 'title': 'Blocked promotion item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'queued', 'risk_level': 'medium', 'approval_required': False, 'requires_manual': False, 'blocked_by': ['validation_gap']},
                ],
                'summary': {'pending_count': 2},
            },
            'rollout_executor': {'status': 'controlled', 'summary': {}},
        }
        candidate_view = build_auto_promotion_candidate_view(payload, limit=10)
        self.assertEqual(candidate_view['schema_version'], 'm5_auto_promotion_candidate_view_v1')
        self.assertEqual(candidate_view['summary']['candidate_count'], 2)
        self.assertEqual(candidate_view['summary']['ready_count'], 1)
        self.assertEqual(candidate_view['summary']['blocked_count'], 1)
        self.assertEqual(candidate_view['ready_items'][0]['item_id'], 'playbook::promo')
        self.assertTrue(candidate_view['ready_items'][0]['can_auto_promote'])
        self.assertIn('auto_advance_allowed', candidate_view['ready_items'][0]['why_promotable'])
        self.assertEqual(candidate_view['blocked_items'][0]['item_id'], 'playbook::blocked')
        self.assertIn('validation_gate:not_ready', candidate_view['blocked_items'][0]['missing_requirements'])
        self.assertTrue(candidate_view['blocked_items'][0]['manual_fallback_required'])

    def test_build_auto_promotion_candidate_view_filters_ready_status_and_manual_fallback(self):
        payload = {
            'workflow_state': {
                'item_states': [
                    {'item_id': 'playbook::promo', 'title': 'Promotion ready item', 'action_type': 'joint_stage_prepare', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'workflow_state': 'ready', 'blocking_reasons': [], 'auto_approval_decision': 'auto_approve', 'auto_approval_eligible': True, 'current_rollout_stage': 'guarded_prepare', 'target_rollout_stage': 'controlled_apply', 'stage_handler': {'stage_key': 'guarded_prepare', 'advisory': {'recommended_stage': 'controlled_apply', 'recommended_action': 'promote_to_controlled_apply', 'reasons': ['auto_advance_allowed'], 'ready_for_live_promotion': True}}, 'state_machine': {'auto_advance_gate': {'allowed': True, 'readiness_score': 100, 'blockers': []}, 'rollback_gate': {'candidate': False, 'triggered': []}}},
                    {'item_id': 'playbook::manual', 'title': 'Manual fallback item', 'action_type': 'joint_expand_guarded', 'risk_level': 'high', 'approval_required': True, 'requires_manual': True, 'workflow_state': 'blocked_by_approval', 'blocking_reasons': [], 'current_rollout_stage': 'guarded_prepare', 'target_rollout_stage': 'controlled_apply', 'stage_handler': {'stage_key': 'guarded_prepare', 'advisory': {'recommended_stage': 'controlled_apply', 'recommended_action': 'promote_to_controlled_apply', 'reasons': ['manual_gate_required'], 'ready_for_live_promotion': False}}, 'state_machine': {'auto_advance_gate': {'allowed': False, 'readiness_score': 20, 'blockers': ['manual_gate_required']}, 'rollback_gate': {'candidate': False, 'triggered': []}}},
                ],
                'summary': {'item_count': 2},
            },
            'approval_state': {
                'items': [
                    {'approval_id': 'approval::promo', 'playbook_id': 'playbook::promo', 'title': 'Promotion ready item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'ready', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []},
                    {'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual fallback item', 'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'high', 'approval_required': True, 'requires_manual': True, 'blocked_by': []},
                ],
                'summary': {'pending_count': 2},
            },
            'rollout_executor': {'status': 'controlled', 'summary': {}},
        }
        ready_only = build_auto_promotion_candidate_view(payload, candidate_status='ready', limit=10)
        self.assertEqual(ready_only['summary']['candidate_count'], 1)
        self.assertEqual(ready_only['items'][0]['item_id'], 'playbook::promo')
        manual_only = build_auto_promotion_candidate_view(payload, manual_fallback_required=True, limit=10)
        self.assertEqual(manual_only['summary']['candidate_count'], 1)
        self.assertEqual(manual_only['items'][0]['item_id'], 'playbook::manual')
        self.assertEqual(manual_only['applied_filters']['candidate_status'], None)

    def test_execute_auto_promotion_review_queue_layer_queues_due_and_rollback_reviews(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, 'auto_promotion_review.db'))
            payload = {
                'workflow_state': {
                    'item_states': [
                        {
                            'item_id': 'playbook::post', 'title': 'Post review item', 'action_type': 'joint_stage_prepare', 'workflow_state': 'ready', 'risk_level': 'low',
                            'scheduled_review': {'review_due_at': '2024-03-28T08:00:00+00:00', 'review_after_hours': 24},
                            'auto_promotion_execution': {'reason_codes': ['auto_advance_allowed'], 'after': {'rollout_stage': 'controlled_apply'}},
                            'rollback_gate': {'candidate': False, 'triggered': []},
                        },
                        {
                            'item_id': 'playbook::rollback', 'title': 'Rollback review item', 'action_type': 'joint_stage_prepare', 'workflow_state': 'queued', 'risk_level': 'low',
                            'scheduled_review': {'review_due_at': '2026-03-29T08:00:00+00:00', 'review_after_hours': 24},
                            'auto_promotion_execution': {'reason_codes': ['auto_advance_allowed'], 'after': {'rollout_stage': 'controlled_apply'}},
                            'rollback_gate': {'candidate': True, 'triggered': ['validation_gate_regressed']},
                        },
                    ],
                    'summary': {'item_count': 2},
                },
                'approval_state': {
                    'items': [
                        {'approval_id': 'approval::post', 'playbook_id': 'playbook::post', 'title': 'Post review item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'ready', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []},
                        {'approval_id': 'approval::rollback', 'playbook_id': 'playbook::rollback', 'title': 'Rollback review item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'queued', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []},
                    ],
                    'summary': {'pending_count': 2},
                },
            }
            db.sync_approval_items([
                {'item_id': 'approval::post', 'approval_type': 'joint_stage_prepare', 'target': 'playbook::post', 'title': 'Post review item', 'approval_state': 'pending', 'workflow_state': 'ready'},
                {'item_id': 'approval::rollback', 'approval_type': 'joint_stage_prepare', 'target': 'playbook::rollback', 'title': 'Rollback review item', 'approval_state': 'pending', 'workflow_state': 'queued'},
            ], replay_source='unit-test')
            executed = execute_auto_promotion_review_queue_layer(payload, db, settings={'enabled': True, 'mode': 'controlled'}, replay_source='unit-test')
            summary = executed['auto_promotion_review_execution']['summary']
            self.assertEqual(executed['auto_promotion_review_execution']['completed_count'], 1)
            self.assertEqual(executed['auto_promotion_review_execution']['rollback_escalated_count'], 1)
            self.assertEqual(summary['queue_count'], 2)
            post_state = db.get_approval_state('approval::post')
            self.assertEqual(post_state['workflow_state'], 'ready')
            self.assertEqual(post_state['details']['auto_promotion_review_execution']['review_status'], 'post_promotion_review_completed')
            self.assertEqual(post_state['details']['auto_promotion_review_execution']['review_resolution'], 'completed_no_regression')
            self.assertTrue(post_state['details']['auto_promotion_review_execution']['completed_at'])
            rollback_item = next(row for row in executed['auto_promotion_review_execution']['items'] if row['queue_kind'] == 'rollback_review_queue')
            self.assertEqual(rollback_item['workflow_state'], 'rollback_prepare')


    def test_execute_auto_promotion_review_queue_layer_dry_run_completion_does_not_persist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, 'auto_promotion_review_dry_run.db'))
            payload = {
                'workflow_state': {
                    'item_states': [
                        {
                            'item_id': 'playbook::post', 'title': 'Post review item', 'action_type': 'joint_stage_prepare', 'workflow_state': 'ready', 'risk_level': 'low',
                            'scheduled_review': {'review_due_at': '2024-03-28T08:00:00+00:00', 'review_after_hours': 24},
                            'auto_promotion_execution': {'reason_codes': ['auto_advance_allowed'], 'after': {'rollout_stage': 'controlled_apply'}},
                            'rollback_gate': {'candidate': False, 'triggered': []},
                        },
                    ],
                    'summary': {'item_count': 1},
                },
                'approval_state': {
                    'items': [
                        {'approval_id': 'approval::post', 'playbook_id': 'playbook::post', 'title': 'Post review item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'ready', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []},
                    ],
                    'summary': {'pending_count': 1},
                },
            }
            db.sync_approval_items([
                {'item_id': 'approval::post', 'approval_type': 'joint_stage_prepare', 'target': 'playbook::post', 'title': 'Post review item', 'approval_state': 'pending', 'workflow_state': 'ready'},
            ], replay_source='unit-test')
            executed = execute_auto_promotion_review_queue_layer(payload, db, settings={'enabled': True, 'mode': 'dry_run'}, replay_source='unit-test')
            self.assertEqual(executed['auto_promotion_review_execution']['completed_count'], 0)
            self.assertEqual(executed['auto_promotion_review_execution']['items'][0]['action'], 'dry_run_complete')
            post_state = db.get_approval_state('approval::post')
            self.assertEqual(post_state['workflow_state'], 'ready')
            self.assertFalse(post_state['details'].get('auto_promotion_review_execution'))



    def test_execute_auto_promotion_review_queue_layer_applies_budget_and_fairness_fence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, 'auto_promotion_review_budget.db'))
            payload = {
                'workflow_state': {
                    'item_states': [
                        {'item_id': 'playbook::post-a', 'title': 'Post review A', 'action_type': 'joint_stage_prepare', 'workflow_state': 'ready', 'risk_level': 'low', 'scheduled_review': {'review_due_at': '2024-03-28T08:00:00+00:00', 'review_after_hours': 24}, 'auto_promotion_execution': {'reason_codes': ['auto_advance_allowed'], 'after': {'rollout_stage': 'controlled_apply'}}, 'rollback_gate': {'candidate': False, 'triggered': []}},
                        {'item_id': 'playbook::post-b', 'title': 'Post review B', 'action_type': 'joint_stage_prepare', 'workflow_state': 'ready', 'risk_level': 'low', 'scheduled_review': {'review_due_at': '2024-03-28T08:00:00+00:00', 'review_after_hours': 24}, 'auto_promotion_execution': {'reason_codes': ['auto_advance_allowed'], 'after': {'rollout_stage': 'controlled_apply'}}, 'rollback_gate': {'candidate': False, 'triggered': []}},
                        {'item_id': 'playbook::rollback', 'title': 'Rollback review', 'action_type': 'joint_stage_prepare', 'workflow_state': 'queued', 'risk_level': 'low', 'scheduled_review': {'review_due_at': '2026-03-29T08:00:00+00:00', 'review_after_hours': 24}, 'auto_promotion_execution': {'reason_codes': ['auto_advance_allowed'], 'after': {'rollout_stage': 'controlled_apply'}}, 'rollback_gate': {'candidate': True, 'triggered': ['validation_gate_regressed']}},
                    ],
                    'summary': {'item_count': 3},
                },
                'approval_state': {
                    'items': [
                        {'approval_id': 'approval::post-a', 'playbook_id': 'playbook::post-a', 'title': 'Post review A', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'ready', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []},
                        {'approval_id': 'approval::post-b', 'playbook_id': 'playbook::post-b', 'title': 'Post review B', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'ready', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []},
                        {'approval_id': 'approval::rollback', 'playbook_id': 'playbook::rollback', 'title': 'Rollback review', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'queued', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []},
                    ],
                    'summary': {'pending_count': 3},
                },
            }
            db.sync_approval_items([
                {'item_id': 'approval::post-a', 'approval_type': 'joint_stage_prepare', 'target': 'playbook::post-a', 'title': 'Post review A', 'approval_state': 'pending', 'workflow_state': 'ready'},
                {'item_id': 'approval::post-b', 'approval_type': 'joint_stage_prepare', 'target': 'playbook::post-b', 'title': 'Post review B', 'approval_state': 'pending', 'workflow_state': 'ready'},
                {'item_id': 'approval::rollback', 'approval_type': 'joint_stage_prepare', 'target': 'playbook::rollback', 'title': 'Rollback review', 'approval_state': 'pending', 'workflow_state': 'queued'},
            ], replay_source='unit-test')
            executed = execute_auto_promotion_review_queue_layer(
                payload,
                db,
                settings={'enabled': True, 'mode': 'controlled', 'max_mutations_per_round': 2, 'max_mutations_per_queue_per_round': 1},
                replay_source='unit-test',
            )
            result = executed['auto_promotion_review_execution']
            self.assertEqual(result['completed_count'], 1)
            self.assertEqual(result['rollback_escalated_count'], 1)
            self.assertEqual(result['summary']['executed_count'], 2)
            self.assertEqual(result['summary']['fairness_skipped_count'], 1)
            self.assertTrue(result['budget']['exhausted'])
            self.assertEqual(result['budget']['executed_by_queue']['rollback_review_queue'], 1)
            self.assertEqual(result['budget']['executed_by_queue']['post_promotion_review_queue'], 1)
            skipped = [row for row in result['items'] if row['action'] == 'skipped']
            self.assertEqual(skipped[0]['reason'], 'fairness_queue_cap_reached')
            self.assertEqual(skipped[0]['fairness']['queue_kind'], 'post_promotion_review_queue')
            self.assertIn('fairness=round_robin', result['summary']['budget']['summary'])

    def test_execute_recovery_queue_layer_applies_budget_and_fairness_fence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, 'recovery_budget.db'))
            payload = {
                'workflow_state': {
                    'item_states': [
                        {'item_id': 'playbook::retry', 'title': 'Retry item', 'action_type': 'joint_stage_prepare', 'workflow_state': 'execution_failed', 'risk_level': 'low'},
                        {'item_id': 'playbook::rollback', 'title': 'Rollback item', 'action_type': 'joint_stage_prepare', 'workflow_state': 'queued', 'risk_level': 'low'},
                        {'item_id': 'playbook::manual', 'title': 'Manual item', 'action_type': 'joint_stage_prepare', 'workflow_state': 'execution_failed', 'risk_level': 'low'},
                    ],
                },
                'approval_state': {
                    'items': [
                        {'approval_id': 'approval::retry', 'playbook_id': 'playbook::retry', 'title': 'Retry item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending'},
                        {'approval_id': 'approval::rollback', 'playbook_id': 'playbook::rollback', 'title': 'Rollback item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending'},
                        {'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending'},
                    ],
                },
                'workflow_recovery_view': {
                    'queues': {
                        'retry_queue': [
                            {'item_id': 'playbook::retry', 'approval_id': 'approval::retry', 'title': 'Retry item', 'action_type': 'joint_stage_prepare', 'recovery_orchestration': {'retry_schedule': {'retry_count': 0}, 'target_route': 'retry_queue', 'routing_reason': ['retry']}}
                        ],
                        'rollback_candidates': [
                            {'item_id': 'playbook::rollback', 'approval_id': 'approval::rollback', 'title': 'Rollback item', 'action_type': 'joint_stage_prepare', 'recovery_orchestration': {'rollback_route': 'rollback_candidate_queue', 'routing_reason': ['rollback']}}
                        ],
                        'manual_recovery': [
                            {'item_id': 'playbook::manual', 'approval_id': 'approval::manual', 'title': 'Manual item', 'action_type': 'joint_stage_prepare', 'recovery_orchestration': {'manual_recovery': {'required': True}, 'routing_reason': ['manual']}}
                        ],
                    },
                    'summary': {'retry_queue_count': 1, 'rollback_candidate_count': 1, 'manual_recovery_count': 1},
                },
            }
            db.sync_approval_items([
                {'item_id': 'approval::retry', 'approval_type': 'joint_stage_prepare', 'target': 'playbook::retry', 'title': 'Retry item', 'approval_state': 'pending', 'workflow_state': 'execution_failed'},
                {'item_id': 'approval::rollback', 'approval_type': 'joint_stage_prepare', 'target': 'playbook::rollback', 'title': 'Rollback item', 'approval_state': 'pending', 'workflow_state': 'queued'},
                {'item_id': 'approval::manual', 'approval_type': 'joint_stage_prepare', 'target': 'playbook::manual', 'title': 'Manual item', 'approval_state': 'pending', 'workflow_state': 'execution_failed'},
            ], replay_source='unit-test')
            executed = execute_recovery_queue_layer(
                payload,
                db,
                settings={'enabled': True, 'mode': 'controlled', 'max_mutations_per_round': 2, 'max_mutations_per_queue_per_round': 1, 'retry_executor': {'enabled': False}},
                replay_source='unit-test',
                now='2026-03-29T09:00:00+00:00',
            )
            result = executed['recovery_execution']
            self.assertEqual(result['scheduled_retry_count'], 1)
            self.assertEqual(result['rollback_queued_count'], 1)
            self.assertEqual(result['manual_recovery_annotated_count'], 0)
            self.assertEqual(result['summary']['executed_count'], 2)
            self.assertEqual(result['summary']['fairness_skipped_count'], 0)
            self.assertTrue(result['budget']['exhausted'])
            self.assertEqual(result['budget']['executed_by_queue']['retry_queue'], 1)
            self.assertEqual(result['budget']['executed_by_queue']['rollback_candidates'], 1)
            skipped = next(row for row in result['items'] if row['action'] == 'skipped')
            self.assertEqual(skipped['queue_bucket'], 'manual_recovery')
            self.assertEqual(skipped['reason'], 'pass_budget_exhausted')
            self.assertEqual(skipped['fairness']['why_skipped'], 'pass_budget_exhausted')

    def test_build_runtime_orchestration_summary_surfaces_followup_budget_fairness(self):
        payload = build_runtime_orchestration_summary({
            'adaptive_rollout_orchestration': {'summary': {'pass_count': 1, 'rerun_triggered': True, 'rerun_reason': 'post_review_queue_state_transition', 'rollout_executor_applied_count': 1}},
            'auto_promotion_review_execution': {'budget': {'summary': 'budget=2 executed=2 remaining=0 exhausted=true / fairness=round_robin per_queue_cap=1', 'exhausted': True, 'skipped_by_fairness': 1}},
            'recovery_execution': {'budget': {'summary': 'budget=2 executed=2 remaining=0 exhausted=true / fairness=round_robin per_queue_cap=1', 'exhausted': True, 'skipped_by_budget': 1}},
            'workflow_operator_digest': {'transition_journal': {'schema_version': 'm5_transition_journal_consumer_v1', 'headline': {'status': 'steady', 'message': '0 recent transition(s)', 'latest_timestamp': None, 'latest_transition': None}, 'summary': {'count': 0, 'latest_timestamp': None, 'changed_only': True, 'changed_field_counts': {}, 'workflow_transition_counts': {}}, 'recent_transitions': [], 'latest': {}, 'overview': {'schema_version': 'm5_transition_journal_overview_v1', 'summary': {'count': 0, 'changed_field_counts': {}}, 'recent_transitions': [], 'breakdown': {'changed_field_counts': {}, 'trigger_counts': {}, 'actor_counts': {}, 'source_counts': {}}}}, 'attention': {'manual_approval': [], 'blocked': []}},
            'workflow_recovery_view': {'queues': {'rollback_candidates': [], 'manual_recovery': []}, 'summary': {'retry_queue_count': 1, 'manual_recovery_count': 0, 'rollback_candidate_count': 0}},
            'unified_workbench_overview': {'lines': {'approval': {'next_actions': []}, 'rollout': {'next_actions': []}, 'recovery': {'next_actions': []}}, 'dominant_line': 'recovery', 'overall_state': 'active'},
            'workbench_governance_view': {'recent_adjustments': []},
            'approval_state': {'items': []},
            'workflow_state': {'item_states': []},
        }, max_items=3)
        self.assertIn('review_followup_budget', payload['follow_ups']['summary'])
        self.assertIn('recovery_followup_budget', payload['follow_ups']['summary'])
        self.assertTrue(payload['follow_ups']['summary']['review_followup_budget']['exhausted'])
        self.assertEqual(payload['follow_ups']['summary']['review_followup_budget']['skipped_by_fairness'], 1)


    def test_execute_adaptive_rollout_orchestration_reruns_executor_after_recovery_transitions(self):
        import analytics.helper as helper_module

        original_executor = helper_module.execute_rollout_executor
        original_readiness = helper_module.build_production_rollout_readiness
        original_auto_approval = helper_module.execute_controlled_auto_approval_layer
        original_controlled_rollout = helper_module.execute_controlled_rollout_layer
        original_bridge = helper_module.execute_testnet_bridge_layer
        original_review = helper_module.execute_auto_promotion_review_queue_layer
        original_recovery = helper_module.execute_recovery_queue_layer
        try:
            executor_calls = []

            def fake_executor(payload, db, config=None, settings=None, replay_source='workflow_ready'):
                payload = dict(payload)
                rollout_executor = dict(payload.get('rollout_executor') or {})
                summary = dict(rollout_executor.get('summary') or {})
                dry_run = bool((settings or {}).get('dry_run')) if settings else False
                label = 'dry_run' if dry_run else f"apply_{len(executor_calls)}"
                executor_calls.append(label)
                summary['applied_count'] = 0 if dry_run else 1
                rollout_executor['summary'] = summary
                payload['rollout_executor'] = rollout_executor
                return payload

            helper_module.execute_rollout_executor = fake_executor
            helper_module.build_production_rollout_readiness = lambda payload, max_items=10: {
                'schema_version': 'm5_production_rollout_readiness_v1',
                'status': 'ready',
                'production_ready': True,
                'can_enable_low_intervention_runtime': True,
                'blocking_issues': [],
                'headline': {},
                'runbook_actions': [],
            }
            helper_module.execute_controlled_auto_approval_layer = lambda payload, db, config=None, replay_source='workflow_ready': {**payload, 'auto_approval_execution': {'executed_count': 0, 'items': []}}
            helper_module.execute_controlled_rollout_layer = lambda payload, db, config=None, replay_source='workflow_ready': {**payload, 'controlled_rollout_execution': {'executed_count': 0, 'items': []}}
            helper_module.execute_testnet_bridge_layer = lambda payload, db, config=None, replay_source='workflow_ready': {**payload, 'testnet_bridge_execution': {'status': 'disabled', 'audit': {'real_trade_execution': False}}}
            helper_module.execute_auto_promotion_review_queue_layer = lambda payload, db, config=None, replay_source='workflow_ready': {**payload, 'auto_promotion_review_execution': {'queued_count': 0, 'completed_count': 0, 'rollback_escalated_count': 0, 'items': []}}
            helper_module.execute_recovery_queue_layer = lambda payload, db, config=None, replay_source='workflow_ready': {**payload, 'recovery_execution': {'scheduled_retry_count': 1, 'rollback_queued_count': 1, 'manual_recovery_annotated_count': 1, 'retry_reentered_executor_count': 1, 'items': []}}

            payload = {'approval_state': {'items': []}, 'workflow_state': {'item_states': []}}
            executed = execute_adaptive_rollout_orchestration(payload, db=None)
            summary = executed['adaptive_rollout_orchestration']['summary']

            self.assertEqual(executor_calls, ['dry_run', 'apply_1'])
            self.assertTrue(summary['rerun_triggered'])
            self.assertEqual(summary['rerun_reason'], 'post_recovery_state_transition')
            self.assertIn('recovery_retry_scheduled', summary['rerun_reasons'])
            self.assertIn('recovery_retry_reentered_executor', summary['rerun_reasons'])
            self.assertIn('recovery_rollback_candidate_queued', summary['rerun_reasons'])
            self.assertIn('recovery_manual_follow_up_annotated', summary['rerun_reasons'])
            self.assertEqual(summary['recovery_retry_scheduled_count'], 1)
            self.assertEqual(summary['recovery_retry_reentered_executor_count'], 1)
            self.assertEqual(summary['recovery_rollback_queued_count'], 1)
            self.assertEqual(summary['recovery_manual_annotation_count'], 1)
            self.assertEqual(summary['pass_count'], 2)
            self.assertEqual(executed['adaptive_rollout_orchestration']['passes'][-1]['label'], 'post_recovery_queue')
        finally:
            helper_module.execute_rollout_executor = original_executor
            helper_module.build_production_rollout_readiness = original_readiness
            helper_module.execute_controlled_auto_approval_layer = original_auto_approval
            helper_module.execute_controlled_rollout_layer = original_controlled_rollout
            helper_module.execute_testnet_bridge_layer = original_bridge
            helper_module.execute_auto_promotion_review_queue_layer = original_review
            helper_module.execute_recovery_queue_layer = original_recovery

    def test_execute_adaptive_rollout_orchestration_reruns_executor_after_review_queue_transitions(self):
        import analytics.helper as helper_module

        original_executor = helper_module.execute_rollout_executor
        original_readiness = helper_module.build_production_rollout_readiness
        original_auto_approval = helper_module.execute_controlled_auto_approval_layer
        original_controlled_rollout = helper_module.execute_controlled_rollout_layer
        original_bridge = helper_module.execute_testnet_bridge_layer
        original_review = helper_module.execute_auto_promotion_review_queue_layer
        original_recovery = helper_module.execute_recovery_queue_layer
        try:
            executor_calls = []

            def fake_executor(payload, db, config=None, settings=None, replay_source='workflow_ready'):
                payload = dict(payload)
                rollout_executor = dict(payload.get('rollout_executor') or {})
                summary = dict(rollout_executor.get('summary') or {})
                dry_run = bool((settings or {}).get('dry_run')) if settings else False
                label = 'dry_run' if dry_run else f"apply_{len(executor_calls)}"
                executor_calls.append(label)
                summary['applied_count'] = 0 if dry_run else 1
                rollout_executor['summary'] = summary
                payload['rollout_executor'] = rollout_executor
                return payload

            helper_module.execute_rollout_executor = fake_executor
            helper_module.build_production_rollout_readiness = lambda payload, max_items=10: {
                'schema_version': 'm5_production_rollout_readiness_v1',
                'status': 'ready',
                'production_ready': True,
                'can_enable_low_intervention_runtime': True,
                'blocking_issues': [],
                'headline': {},
                'runbook_actions': [],
            }
            helper_module.execute_controlled_auto_approval_layer = lambda payload, db, config=None, replay_source='workflow_ready': {**payload, 'auto_approval_execution': {'executed_count': 0, 'items': []}}
            helper_module.execute_controlled_rollout_layer = lambda payload, db, config=None, replay_source='workflow_ready': {**payload, 'controlled_rollout_execution': {'executed_count': 0, 'items': []}}
            helper_module.execute_testnet_bridge_layer = lambda payload, db, config=None, replay_source='workflow_ready': {**payload, 'testnet_bridge_execution': {'status': 'disabled', 'audit': {'real_trade_execution': False}}}
            helper_module.execute_auto_promotion_review_queue_layer = lambda payload, db, config=None, replay_source='workflow_ready': {**payload, 'auto_promotion_review_execution': {'queued_count': 0, 'completed_count': 1, 'rollback_escalated_count': 1, 'items': []}}
            helper_module.execute_recovery_queue_layer = lambda payload, db, config=None, replay_source='workflow_ready': {**payload, 'recovery_execution': {'scheduled_retry_count': 0, 'rollback_queued_count': 0, 'manual_recovery_annotated_count': 0}}

            payload = {'approval_state': {'items': []}, 'workflow_state': {'item_states': []}}
            executed = execute_adaptive_rollout_orchestration(payload, db=None)
            summary = executed['adaptive_rollout_orchestration']['summary']

            self.assertEqual(executor_calls, ['dry_run', 'apply_1'])
            self.assertTrue(summary['rerun_triggered'])
            self.assertEqual(summary['rerun_reason'], 'post_review_queue_state_transition')
            self.assertIn('post_promotion_review_completed', summary['rerun_reasons'])
            self.assertIn('rollback_review_escalated', summary['rerun_reasons'])
            self.assertEqual(summary['review_queue_completed_count'], 1)
            self.assertEqual(summary['review_queue_rollback_escalated_count'], 1)
            self.assertEqual(summary['pass_count'], 2)
            self.assertEqual(executed['adaptive_rollout_orchestration']['passes'][-1]['label'], 'post_review_queue')
        finally:
            helper_module.execute_rollout_executor = original_executor
            helper_module.build_production_rollout_readiness = original_readiness
            helper_module.execute_controlled_auto_approval_layer = original_auto_approval
            helper_module.execute_controlled_rollout_layer = original_controlled_rollout
            helper_module.execute_testnet_bridge_layer = original_bridge
            helper_module.execute_auto_promotion_review_queue_layer = original_review
            helper_module.execute_recovery_queue_layer = original_recovery

    def test_auto_promotion_review_execution_api_returns_execution_summary(self):
        import dashboard.api as dashboard_api

        class StubBacktester:
            def run_all(self, symbols):
                return {'summary': {'symbols': len(symbols)}, 'symbols': [{'symbol': 'BTC/USDT'}], 'calibration_report': {'summary': {}, 'workflow_ready': {}}}

        old_backtester = dashboard_api.backtester
        old_export = dashboard_api.export_calibration_payload
        old_persist = dashboard_api._persist_workflow_approval_payload
        dashboard_api.backtester = StubBacktester()
        dashboard_api.export_calibration_payload = lambda report, view='workflow_ready': {'workflow_state': {'item_states': []}, 'approval_state': {'items': []}}
        dashboard_api._persist_workflow_approval_payload = lambda payload, replay_source='auto_promotion_review_execution_api': {**payload, 'auto_promotion_review_execution': {'enabled': True, 'mode': 'controlled', 'queued_count': 1, 'skipped_count': 0, 'summary': {'queue_count': 2, 'review_due_count': 1}, 'items': [{'item_id': 'playbook::post', 'queue_kind': 'post_promotion_review_queue', 'action': 'queued'}]}}
        try:
            client = app.test_client()
            response = client.get('/api/backtest/auto-promotion-review-execution')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload['success'])
            self.assertEqual(payload['view'], 'auto_promotion_review_execution')
            self.assertEqual(payload['summary']['queue_count'], 2)
            self.assertEqual(payload['data']['queued_count'], 1)
        finally:
            dashboard_api.backtester = old_backtester
            dashboard_api.export_calibration_payload = old_export
            dashboard_api._persist_workflow_approval_payload = old_persist

    def test_gate_consumption_flows_into_operator_digest_workbench_and_timeline_layers(self):
        gate_payload = {
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::auto',
                        'title': 'Auto advance item',
                        'action_type': 'joint_stage_prepare',
                        'risk_level': 'low',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'ready',
                        'blocking_reasons': [],
                        'current_rollout_stage': 'observe',
                        'target_rollout_stage': 'guarded_prepare',
                        'state_machine': {
                            'auto_advance_gate': {'allowed': True, 'readiness_score': 100, 'blockers': []},
                            'rollback_gate': {'candidate': False, 'triggered': []},
                        },
                    },
                    {
                        'item_id': 'playbook::rollback',
                        'title': 'Rollback candidate item',
                        'action_type': 'joint_review_schedule',
                        'risk_level': 'critical',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'execution_failed',
                        'blocking_reasons': ['critical_risk'],
                        'current_rollout_stage': 'guarded',
                        'target_rollout_stage': 'rollback_prepare',
                        'state_machine': {
                            'auto_advance_gate': {'allowed': False, 'readiness_score': 10, 'blockers': ['risk_level:critical', 'blocked_by:critical_risk']},
                            'rollback_gate': {'candidate': True, 'triggered': ['execution_error', 'critical_risk'], 'next_action': 'prepare_rollback_review'},
                            'recovery_orchestration': {'queue_bucket': 'rollback_candidate'},
                            'recovery_policy': {'rollback_candidate': True},
                        },
                    },
                ],
                'summary': {'item_count': 2},
            },
            'approval_state': {
                'items': [
                    {'approval_id': 'approval::auto', 'playbook_id': 'playbook::auto', 'title': 'Auto advance item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'ready', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []},
                    {'approval_id': 'approval::rollback', 'playbook_id': 'playbook::rollback', 'title': 'Rollback candidate item', 'action_type': 'joint_review_schedule', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'critical', 'approval_required': False, 'requires_manual': False, 'blocked_by': ['critical_risk']},
                ],
                'summary': {'pending_count': 2},
            },
            'rollout_executor': {'status': 'controlled', 'summary': {}},
        }
        consumer = build_workflow_consumer_view(gate_payload)
        auto_item = next(row for row in consumer['workflow_state']['item_states'] if row['item_id'] == 'playbook::auto')
        rollback_item = next(row for row in consumer['workflow_state']['item_states'] if row['item_id'] == 'playbook::rollback')
        self.assertEqual(auto_item['lane_routing']['lane_id'], 'auto_batch')
        self.assertEqual(auto_item['queue_name'], 'rollout_readiness_queue')
        self.assertEqual(auto_item['dispatch_route'], 'rollout_readiness_queue')
        self.assertEqual(auto_item['route_family'], 'rollout_readiness_queue')
        self.assertEqual(rollback_item['lane_routing']['lane_id'], 'rollback_candidate')
        self.assertEqual(rollback_item['lane_routing']['route_family'], 'retry_queue')
        digest = build_workflow_operator_digest(gate_payload, max_items=5)
        self.assertEqual(digest['summary']['gate_consumption']['auto_advance_allowed_count'], 1)
        self.assertEqual(digest['summary']['gate_consumption']['rollback_candidate_count'], 1)
        self.assertEqual(digest['summary']['gate_consumption']['dominant_auto_advance_blocker'], 'blocked_by:critical_risk')
        self.assertEqual(digest['summary']['stage_loop']['path_counts']['auto_advance'], 1)
        self.assertEqual(digest['summary']['stage_loop']['path_counts']['rollback_prepare'], 1)
        self.assertEqual(digest['summary']['stage_loop']['dominant_path'], 'auto_advance')
        self.assertEqual(digest['attention']['rollback_candidates'][0]['item_id'], 'playbook::rollback')
        workbench = build_workbench_governance_view(gate_payload, max_items=5)
        self.assertEqual(workbench['summary']['rollback_candidate_count'], 1)
        self.assertEqual(workbench['summary']['gate_consumption']['dominant_rollback_trigger'], 'critical_risk')
        self.assertEqual(workbench['summary']['stage_loop']['path_counts']['auto_advance'], 1)
        self.assertEqual(workbench['summary']['stage_loop']['path_counts']['rollback_prepare'], 1)
        self.assertIn('rollback_candidate', workbench['lanes'])
        self.assertEqual(workbench['lanes']['rollback_candidate']['stage_loop']['dominant_path'], 'rollback_prepare')
        self.assertEqual(workbench['lanes']['rollback_candidate']['items'][0]['stage_loop']['recommended_action'], 'rollback_prepare')
        self.assertEqual(workbench['lanes']['rollback_candidate']['items'][0]['rollback_gate']['next_action'], 'prepare_rollback_review')
        timeline = build_workbench_timeline_summary_aggregation(gate_payload, max_items_per_group=5)
        self.assertEqual(timeline['summary']['gate_consumption']['auto_advance_allowed_count'], 1)
        self.assertEqual(timeline['summary']['stage_loop']['path_counts']['auto_advance'], 1)
        self.assertEqual(timeline['summary']['stage_loop']['path_counts']['rollback_prepare'], 1)
        rollback_group = next(row for row in timeline['groups']['by_bucket'] if row['group_id'] == 'rollback_candidate')
        self.assertEqual(rollback_group['gate_consumption']['rollback_candidate_count'], 1)
        self.assertEqual(rollback_group['stage_loop']['dominant_path'], 'rollback_prepare')
        self.assertEqual(rollback_group['items'][0]['stage_loop']['loop_state'], 'rollback_prepare')
        self.assertEqual(rollback_group['items'][0]['rollback_gate']['next_action'], 'prepare_rollback_review')

    def test_build_workbench_governance_detail_view_adds_queue_approval_rollout_drilldown(self):
        payload = {
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'risk_level': 'high',
                        'approval_required': True,
                        'requires_manual': True,
                        'workflow_state': 'blocked_by_approval',
                        'blocking_reasons': ['missing_operator_ack'],
                        'queue_progression': {
                            'status': 'blocked_by_approval',
                            'gate_reason': 'manual_review_required',
                            'next_action': 'queue_for_manual_review',
                            'dispatch_route': 'manual_review_queue',
                        },
                        'stage_model': {'next_on_approval': 'expand_guarded'},
                        'current_rollout_stage': 'guarded',
                        'target_rollout_stage': 'expanded',
                    }
                ],
                'summary': {},
            },
            'approval_state': {
                'items': [
                    {
                        'approval_id': 'approval::manual',
                        'playbook_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'approval_state': 'pending',
                        'decision_state': 'pending',
                        'risk_level': 'high',
                        'approval_required': True,
                        'requires_manual': True,
                        'blocked_by': ['missing_operator_ack'],
                    }
                ],
                'summary': {},
            },
            'rollout_executor': {
                'status': 'controlled',
                'summary': {'by_status': {'blocked_by_approval': 1}},
                'items': [
                    {
                        'item_id': 'approval::manual',
                        'playbook_id': 'playbook::manual',
                        'action_type': 'joint_expand_guarded',
                        'status': 'blocked_by_approval',
                        'plan': {
                            'handler_key': 'queue_only::live_trading_change',
                            'executor_class': 'live_trading_change',
                            'dispatch_mode': 'queue_only',
                            'dispatch_route': 'manual_review_queue',
                            'transition_rule': 'manual_gate_before_dispatch',
                            'next_transition': 'await_manual_approval',
                            'rollback_hint': 'revert_to_previous_stage_if_manual_review_rejects',
                            'rollout_stage': 'guarded',
                            'target_rollout_stage': 'expanded',
                            'queue_plan': {
                                'queue_name': 'manual_review_queue',
                                'queue_priority': 'approval_blocked',
                                'dispatch_route': 'manual_review_queue',
                                'next_transition': 'await_manual_approval',
                                'queue_transition': {
                                    'rollback_hint': 'revert_to_previous_stage_if_manual_review_rejects',
                                },
                                'approval_hook': {
                                    'status': 'blocked_by_approval',
                                    'gate_reason': 'manual_review_required',
                                    'next_action': 'queue_for_manual_review',
                                },
                            },
                        },
                        'dispatch': {
                            'handler_key': 'queue_only::live_trading_change',
                            'executor_class': 'live_trading_change',
                            'mode': 'queue_only',
                            'dispatch_route': 'manual_review_queue',
                            'transition_rule': 'manual_gate_before_dispatch',
                            'next_transition': 'await_manual_approval',
                            'rollback_hint': 'revert_to_previous_stage_if_manual_review_rejects',
                        },
                        'result': {
                            'disposition': 'blocked_by_approval',
                            'status': 'blocked_by_approval',
                            'dispatch_route': 'manual_review_queue',
                            'transition_rule': 'manual_gate_before_dispatch',
                            'next_transition': 'await_manual_approval',
                            'rollback_hint': 'revert_to_previous_stage_if_manual_review_rejects',
                        },
                    }
                ],
            },
            'controlled_rollout_execution': {'mode': 'disabled', 'items': []},
            'auto_approval_execution': {'mode': 'disabled', 'items': []},
        }
        detail = build_workbench_governance_detail_view(payload, item_id='playbook::manual', lane_id='manual_approval')
        self.assertTrue(detail['found'])
        self.assertEqual(detail['schema_version'], 'm5_workbench_governance_detail_view_v3')
        self.assertEqual(detail['summary']['operator_action'], 'review_schedule')
        self.assertEqual(detail['summary']['operator_route'], 'manual_approval_queue')
        self.assertEqual(detail['summary']['follow_up'], 'await_manual_approval')
        self.assertIn('approval_gate_pending', detail['summary']['operator_reason_codes'])
        self.assertEqual(detail['drilldown']['operator_action']['action'], 'review_schedule')
        self.assertEqual(detail['drilldown']['operator_action']['route'], 'manual_approval_queue')
        self.assertEqual(detail['drilldown']['operator_action']['follow_up'], 'await_manual_approval')
        self.assertEqual(detail['drilldown']['queue']['queue_name'], 'manual_review_queue')
        self.assertEqual(detail['drilldown']['queue']['route'], 'manual_review_queue')
        self.assertEqual(detail['drilldown']['approval']['current_transition'], 'manual_gate_before_dispatch')
        self.assertEqual(detail['drilldown']['approval']['next_transition'], 'await_manual_approval')
        self.assertEqual(detail['drilldown']['rollout']['handler']['handler_key'], 'queue_only::live_trading_change')
        self.assertEqual(detail['drilldown']['decision_path']['next_transition'], 'await_manual_approval')
        self.assertEqual(detail['drilldown']['timeline']['schema_version'], 'm5_workbench_executor_action_timeline_v1')
        self.assertEqual(detail['drilldown']['timeline']['summary']['dispatch_route'], 'manual_review_queue')
        self.assertIn('blocked_by_approval', detail['drilldown']['timeline']['summary']['audit_event_types'])
        self.assertEqual(detail['drilldown']['timeline']['summary']['decision_path']['approval_path'], 'blocked_by_approval')
        self.assertEqual(detail['drilldown']['timeline']['events'][1]['detail']['approval_hook']['gate_reason'], 'manual_review_required')
        self.assertIn('missing_operator_ack', detail['summary']['blocking_points'])
        self.assertEqual(detail['summary']['timeline']['event_count'], 5)
        self.assertEqual(detail['summary']['timeline']['current_status'], 'blocked_by_approval')
        self.assertTrue(detail['summary']['rollback_hints'])

    def test_build_workbench_merged_timeline_combines_approval_db_and_executor_events(self):
        item = {
            'item_id': 'playbook::manual', 'approval_id': 'approval::manual', 'lane_id': 'manual_approval',
            'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'workflow_state': 'blocked_by_approval',
            'current_rollout_stage': 'guarded', 'target_rollout_stage': 'expanded', 'blocked_by': ['missing_operator_ack'],
        }
        workflow_item = {
            'item_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded',
            'workflow_state': 'blocked_by_approval', 'blocking_reasons': ['missing_operator_ack'],
            'queue_progression': {'status': 'awaiting_approval', 'dispatch_route': 'manual_review_queue', 'next_action': 'queue_for_manual_review'},
        }
        approval_item = {
            'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual gate item',
            'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'blocked_by': ['missing_operator_ack'],
        }
        executor_item = {
            'status': 'blocked_by_approval',
            'plan': {'handler_key': 'queue_only::live_trading_change', 'executor_class': 'live_trading_change', 'dispatch_mode': 'queue_only', 'dispatch_route': 'manual_review_queue', 'transition_rule': 'manual_gate_before_dispatch', 'next_transition': 'await_manual_approval'},
            'dispatch': {'status': 'blocked_by_approval', 'handler_key': 'queue_only::live_trading_change', 'executor_class': 'live_trading_change', 'mode': 'queue_only', 'dispatch_route': 'manual_review_queue', 'reason': 'manual_review_required', 'code': 'APPROVAL_GATED_QUEUE'},
            'result': {'status': 'blocked_by_approval', 'disposition': 'blocked_by_approval', 'dispatch_route': 'manual_review_queue', 'reason': 'manual_review_required', 'code': 'APPROVAL_GATED_QUEUE'},
        }
        approval_timeline = [
            {'id': 1, 'item_id': 'approval::manual', 'event_type': 'snapshot_sync', 'state': 'pending', 'workflow_state': 'pending', 'decision': 'pending', 'reason': 'initial sync', 'created_at': '2026-03-27T10:00:00Z'},
            {'id': 2, 'item_id': 'approval::manual', 'event_type': 'rollout_executor_queue_blocked', 'state': 'pending', 'workflow_state': 'blocked_by_approval', 'decision': 'pending', 'reason': 'manual_review_required', 'created_at': '2026-03-27T10:01:00Z'},
        ]
        merged = build_workbench_merged_timeline(item, workflow_item, approval_item, executor_item, approval_timeline=approval_timeline)
        self.assertEqual(merged['schema_version'], 'm5_workbench_merged_timeline_v1')
        self.assertEqual(merged['summary']['approval_event_count'], 2)
        self.assertEqual(merged['summary']['executor_event_count'], 5)
        self.assertEqual(merged['summary']['event_count'], 7)
        self.assertEqual(merged['events'][0]['source'], 'approval_timeline')
        self.assertEqual(merged['events'][0]['event_type'], 'snapshot_sync')
        self.assertEqual(merged['events'][0]['provenance']['origin'], 'approval_db')
        self.assertEqual(merged['events'][0]['timestamp_info']['source'], 'approval_event_created_at')
        self.assertEqual(merged['events'][-1]['source'], 'executor_timeline')
        self.assertEqual(merged['events'][-1]['provenance']['origin'], 'executor')
        self.assertEqual(merged['events'][-1]['normalized_event_type'], 'executor_result_recorded')
        self.assertIn('approval_db', merged['summary']['phases'])
        self.assertIn('dispatch', merged['summary']['phases'])
        self.assertIn('approval_db', merged['summary']['provenance_origins'])
        self.assertIn('executor', merged['summary']['provenance_origins'])
        self.assertIn('approval_timeline', merged['summary']['provenance_sources'])
        self.assertIn('executor_timeline', merged['summary']['provenance_sources'])
        self.assertIn('snapshot_sync', merged['summary']['normalized_event_types'])

    def test_build_workbench_timeline_summary_aggregation_groups_bucket_and_action_views(self):
        payload = {
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded',
                        'risk_level': 'medium', 'approval_required': True, 'requires_manual': True,
                        'workflow_state': 'blocked_by_approval', 'blocking_reasons': ['missing_operator_ack'],
                        'queue_progression': {'status': 'awaiting_approval', 'dispatch_route': 'manual_review_queue', 'next_action': 'queue_for_manual_review'},
                        'current_rollout_stage': 'guarded', 'target_rollout_stage': 'expanded',
                    },
                    {
                        'item_id': 'playbook::ready', 'title': 'Ready item', 'action_type': 'joint_observe',
                        'risk_level': 'low', 'approval_required': False, 'requires_manual': False,
                        'workflow_state': 'ready', 'blocking_reasons': [],
                        'queue_progression': {'status': 'ready', 'dispatch_route': 'safe_state_apply', 'next_action': 'ready_for_rollout_or_execution'},
                        'current_rollout_stage': 'observe', 'target_rollout_stage': 'ready',
                    },
                ],
                'summary': {},
            },
            'approval_state': {
                'items': [
                    {
                        'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending',
                        'risk_level': 'medium', 'approval_required': True, 'requires_manual': True, 'blocked_by': ['missing_operator_ack'],
                    },
                ],
                'summary': {},
            },
            'workflow_operator_digest': {
                'headline': {'status': 'attention', 'message': 'need review'},
                'attention': {
                    'manual_approval': [{'item_id': 'playbook::manual'}],
                    'blocked': [{'item_id': 'playbook::manual'}],
                    'ready': [{'item_id': 'playbook::ready'}],
                    'queued': [],
                    'auto_advance_candidates': [{'item_id': 'playbook::ready'}],
                },
            },
            'rollout_executor': {
                'status': 'active',
                'items': [
                    {
                        'playbook_id': 'playbook::manual',
                        'status': 'blocked_by_approval',
                        'plan': {'handler_key': 'queue_only::live_trading_change', 'executor_class': 'live_trading_change', 'dispatch_mode': 'queue_only', 'dispatch_route': 'manual_review_queue'},
                        'dispatch': {'status': 'blocked_by_approval', 'handler_key': 'queue_only::live_trading_change', 'executor_class': 'live_trading_change', 'mode': 'queue_only', 'dispatch_route': 'manual_review_queue', 'reason': 'manual_review_required', 'code': 'APPROVAL_GATED_QUEUE'},
                        'result': {'status': 'blocked_by_approval', 'disposition': 'blocked_by_approval', 'dispatch_route': 'manual_review_queue', 'reason': 'manual_review_required', 'code': 'APPROVAL_GATED_QUEUE'},
                    },
                    {
                        'playbook_id': 'playbook::ready',
                        'status': 'applied',
                        'plan': {'handler_key': 'apply::observe_ready', 'executor_class': 'state_transition', 'dispatch_mode': 'apply', 'dispatch_route': 'safe_state_apply'},
                        'dispatch': {'status': 'applied', 'handler_key': 'apply::observe_ready', 'executor_class': 'state_transition', 'mode': 'apply', 'dispatch_route': 'safe_state_apply', 'reason': 'safe_apply_ready', 'code': 'SAFE_APPLY_READY'},
                        'result': {'status': 'applied', 'disposition': 'applied', 'dispatch_route': 'safe_state_apply', 'reason': 'safe_apply_ready', 'code': 'SAFE_APPLY_READY'},
                    },
                ],
            },
            'controlled_rollout_execution': {'mode': 'disabled', 'items': []},
            'auto_approval_execution': {'mode': 'disabled', 'items': []},
        }
        aggregation = build_workbench_timeline_summary_aggregation(
            payload,
            approval_timeline_fetcher=lambda approval_id, limit: [
                {'id': 1, 'item_id': approval_id, 'event_type': 'snapshot_sync', 'state': 'pending', 'workflow_state': 'pending', 'decision': 'pending', 'reason': 'initial sync', 'created_at': '2026-03-27T10:00:00Z'},
                {'id': 2, 'item_id': approval_id, 'event_type': 'rollout_executor_queue_blocked', 'state': 'pending', 'workflow_state': 'blocked_by_approval', 'decision': 'pending', 'reason': 'manual_review_required', 'created_at': '2026-03-27T10:01:00Z'},
            ] if approval_id == 'approval::manual' else [],
        )
        self.assertEqual(aggregation['schema_version'], 'm5_workbench_timeline_summary_aggregation_v2')
        self.assertEqual(aggregation['summary']['item_count'], 2)
        manual_bucket = next(group for group in aggregation['groups']['by_bucket'] if group['group_id'] == 'manual_approval')
        self.assertEqual(manual_bucket['item_count'], 1)
        self.assertIn('manual_review_queue', manual_bucket['timeline_summary']['dispatch_routes'])
        self.assertEqual(manual_bucket['headline'], manual_bucket['low_intervention_summary']['headline'])
        self.assertEqual(manual_bucket['low_intervention_summary']['status_overview']['manual'], 1)
        self.assertEqual(manual_bucket['operator_action_policy_summary']['action_counts']['review_schedule'], 1)
        self.assertEqual(manual_bucket['operator_action_policy_summary']['route_counts']['manual_approval_queue'], 1)
        self.assertEqual(manual_bucket['operator_action_policy_summary']['follow_up_counts']['await_manual_approval'], 1)
        self.assertEqual(manual_bucket['merged_timeline_summary']['approval_event_count_total'], 2)
        self.assertIn('approval_db', manual_bucket['merged_timeline_summary']['provenance_origins'])
        self.assertIn('approval_timeline', manual_bucket['merged_timeline_summary']['provenance_sources'])
        action_group = next(group for group in aggregation['groups']['by_action_type'] if group['group_id'] == 'joint_expand_guarded')
        self.assertEqual(action_group['item_count'], 1)
        self.assertEqual(action_group['operator_action_policy_summary']['dominant_action'], 'review_schedule')
        self.assertIn('approval_db', action_group['merged_timeline_summary']['phases'])
        self.assertIn('executor_dispatch_decided', action_group['merged_timeline_summary']['event_types'])
        self.assertIn('executor_dispatch_decided', action_group['merged_timeline_summary']['normalized_event_types'])
        operator_action_group = next(group for group in aggregation['groups']['by_operator_action'] if group['group_id'] == 'review_schedule')
        self.assertEqual(operator_action_group['operator_action_policy_summary']['route_counts']['manual_approval_queue'], 1)
        self.assertEqual(operator_action_group['operator_action_policy_summary']['route_counts']['safe_state_apply'], 1)
        operator_route_group = next(group for group in aggregation['groups']['by_operator_route'] if group['group_id'] == 'manual_approval_queue')
        self.assertEqual(operator_route_group['operator_action_policy_summary']['dominant_follow_up'], 'await_manual_approval')
        follow_up_group = next(group for group in aggregation['groups']['by_follow_up'] if group['group_id'] == 'await_manual_approval')
        self.assertEqual(follow_up_group['operator_action_policy_summary']['dominant_action'], 'review_schedule')
        self.assertEqual(aggregation['summary']['operator_action_policy_summary']['action_counts']['review_schedule'], 2)
        ready_item = next(row for row in aggregation['items'] if row['item_id'] == 'playbook::ready')
        self.assertEqual(ready_item['timeline']['dispatch_route'], 'safe_state_apply')
        self.assertIn('executor', ready_item['timeline']['provenance_origins'])
        self.assertEqual(ready_item['merged_timeline']['approval_event_count'], 0)

    def test_build_workflow_attention_view_groups_manual_and_blocked_items(self):
        payload = build_workflow_attention_view({
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'risk_level': 'medium',
                        'approval_required': True,
                        'requires_manual': True,
                        'workflow_state': 'blocked_by_approval',
                        'blocking_reasons': [],
                        'queue_progression': {'status': 'awaiting_approval'},
                    },
                    {
                        'item_id': 'playbook::blocked',
                        'title': 'Blocked follow-up item',
                        'action_type': 'joint_stage_prepare',
                        'risk_level': 'high',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'blocked',
                        'blocking_reasons': ['executor_unavailable'],
                    },
                    {
                        'item_id': 'playbook::ready',
                        'title': 'Ready item',
                        'action_type': 'joint_observe',
                        'risk_level': 'low',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'ready',
                        'blocking_reasons': [],
                    },
                ],
                'summary': {},
            },
            'approval_state': {
                'items': [
                    {
                        'approval_id': 'approval::manual',
                        'playbook_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'approval_state': 'pending',
                        'decision_state': 'pending',
                        'risk_level': 'medium',
                        'approval_required': True,
                        'requires_manual': True,
                        'blocked_by': [],
                    },
                ],
                'summary': {},
            },
            'rollout_executor': {'status': 'controlled', 'summary': {}},
        }, max_items=10)
        self.assertEqual(payload['schema_version'], 'm5_workflow_attention_view_v1')
        self.assertEqual(payload['summary']['manual_approval_count'], 1)
        self.assertEqual(payload['summary']['blocked_follow_up_count'], 2)
        self.assertEqual(payload['headline']['status'], 'attention_required')
        self.assertEqual(payload['by_bucket']['manual_approval']['items'][0]['item_id'], 'playbook::manual')
        self.assertEqual(payload['by_bucket']['manual_approval']['operator_action_policy_summary']['dominant_action'], 'review_schedule')
        self.assertEqual(payload['by_bucket']['blocked_follow_up']['operator_action_policy_summary']['action_counts']['review_schedule'], 1)
        blocked_ids = [row['item_id'] for row in payload['by_bucket']['blocked_follow_up']['items']]
        self.assertIn('playbook::manual', blocked_ids)
        self.assertIn('playbook::blocked', blocked_ids)
        self.assertNotIn('playbook::ready', [row['item_id'] for row in payload['items']])
        self.assertIn('manual_approval', payload['filters']['bucket_ids'])
        self.assertIn('blocked_follow_up', payload['filters']['bucket_ids'])


    def test_build_workflow_alert_digest_prioritizes_validation_gate_and_manual_approval(self):
        payload = build_workflow_alert_digest({
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'risk_level': 'medium',
                        'approval_required': True,
                        'requires_manual': True,
                        'workflow_state': 'blocked_by_approval',
                        'blocking_reasons': [],
                    },
                    {
                        'item_id': 'playbook::rollback',
                        'title': 'Rollback item',
                        'action_type': 'joint_stage_prepare',
                        'risk_level': 'critical',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'blocked',
                        'blocking_reasons': ['critical_risk'],
                        'state_machine': _build_state_machine_semantics(item_id='approval::rollback', approval_state='pending', workflow_state='blocked', execution_status='error', retryable=False, blocked_by=['critical_risk']),
                        'rollback_gate': {'candidate': True, 'triggered': ['validation_gate_regressed']},
                        'stage_loop': {'loop_state': 'rollback_prepare', 'recommended_action': 'rollback_prepare', 'waiting_on': ['validation_gate_regressed']},
                    },
                ],
                'summary': {'item_count': 2},
            },
            'approval_state': {
                'items': [
                    {
                        'approval_id': 'approval::manual',
                        'playbook_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'approval_state': 'pending',
                        'decision_state': 'pending',
                        'risk_level': 'medium',
                        'approval_required': True,
                        'requires_manual': True,
                        'blocked_by': [],
                    },
                ],
                'summary': {'pending_count': 1},
            },
            'rollout_executor': {'status': 'controlled', 'summary': {}},
            'validation_gate': {
                'enabled': True,
                'ready': False,
                'freeze_auto_advance': True,
                'regression_detected': True,
                'headline': 'validation_gate_regressed',
                'gap_count': 1,
                'failing_case_count': 1,
                'status': 'frozen',
            },
        }, max_items=10)
        self.assertEqual(payload['schema_version'], 'm5_workflow_alert_digest_v1')
        self.assertTrue(any(row['kind'] == 'validation_gate' and row['severity'] == 'critical' for row in payload['alerts']))
        self.assertGreaterEqual(payload['summary']['severity_counts']['critical'], 1)
        self.assertIn('manual_approval', payload['summary']['kind_counts'])
        # follow_up_policy_gate flows through alert_digest via operator_digest upstream
        self.assertIn('follow_up_policy_gate_summary', payload['summary'])
        gate_sum = payload['summary']['follow_up_policy_gate_summary']
        self.assertIn('item_count', gate_sum)
        self.assertIn('dominant_decision', gate_sum)
        self.assertIn('headline', gate_sum)
        self.assertIn('follow_up_decision', payload['headline'])
        self.assertIn('follow_up_action', payload['headline'])

    def test_state_machine_semantics_exposes_execution_timeline_and_recovery_policy(self):
        semantics = _build_state_machine_semantics(
            item_id='approval::recover',
            approval_state='pending',
            workflow_state='execution_failed',
            queue_status='deferred',
            dispatch_route='retry_queue',
            next_transition='retry_after_blockers_clear',
            blocked_by=['exchange_retry_window'],
            retryable=True,
            rollback_hint='restore_previous_state_from_approval_timeline',
            execution_status='recovered',
            last_transition={'from_execution_status': 'error', 'to_execution_status': 'recovered', 'rule': 'safe_apply_ready'},
        )
        self.assertEqual(semantics['execution_timeline']['latest_status'], 'recovered')
        self.assertEqual(semantics['execution_timeline']['recovered_from_status'], 'error')
        self.assertEqual(semantics['execution_timeline']['retry_count'], 1)
        self.assertEqual(semantics['recovery_policy']['policy'], 'recovered_monitoring')
        self.assertTrue(semantics['recovery_policy']['rollback_candidate'])
        self.assertEqual(semantics['recovery_policy']['recommended_action'], 'observe_only_followup')
        self.assertEqual(semantics['recovery_orchestration']['queue_bucket'], 'recovered_monitoring')

    def test_workflow_recovery_view_routes_retry_rollback_and_manual_items(self):
        payload = {
            'workflow_state': {
                'item_states': [
                    {'item_id': 'playbook::retry', 'title': 'Retry item', 'workflow_state': 'execution_failed', 'action_type': 'joint_observe', 'state_machine': _build_state_machine_semantics(item_id='approval::retry', approval_state='pending', workflow_state='execution_failed', execution_status='error', retryable=True, blocked_by=[], rollback_hint='restore_previous_state_from_approval_timeline', dispatch_route='retry_queue', next_transition='retry_execution')},
                    {'item_id': 'playbook::rollback', 'title': 'Rollback item', 'workflow_state': 'rollback_pending', 'action_type': 'joint_stage_prepare', 'state_machine': _build_state_machine_semantics(item_id='approval::rollback', approval_state='pending', workflow_state='rollback_pending', execution_status='error', retryable=False, rollback_hint='revert_stage_metadata_to_previous_stage', dispatch_route='rollback_candidate_queue', next_transition='freeze_and_review')},
                    {'item_id': 'playbook::manual', 'title': 'Manual item', 'workflow_state': 'execution_failed', 'action_type': 'joint_expand_guarded', 'state_machine': _build_state_machine_semantics(item_id='approval::manual', approval_state='pending', workflow_state='execution_failed', execution_status='error', retryable=False, blocked_by=['critical_risk'])},
                ],
                'summary': {},
            },
            'approval_state': {
                'items': [
                    {'approval_id': 'approval::retry', 'playbook_id': 'playbook::retry', 'approval_state': 'pending'},
                    {'approval_id': 'approval::rollback', 'playbook_id': 'playbook::rollback', 'approval_state': 'pending'},
                    {'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'approval_state': 'pending'},
                ],
                'summary': {},
            },
        }
        recovery = build_workflow_recovery_view(payload)
        self.assertEqual(recovery['schema_version'], 'm5_workflow_recovery_view_v1')
        self.assertEqual(recovery['summary']['retry_queue_count'], 1)
        self.assertEqual(recovery['summary']['rollback_candidate_count'], 1)
        self.assertEqual(recovery['summary']['manual_recovery_count'], 1)
        self.assertEqual(recovery['queues']['retry_queue'][0]['item_id'], 'playbook::retry')
        self.assertEqual(recovery['queues']['rollback_candidates'][0]['item_id'], 'playbook::rollback')
        self.assertEqual(recovery['queues']['manual_recovery'][0]['item_id'], 'playbook::manual')

    def test_attach_auto_approval_policy_marks_low_risk_items_auto_approvable(self):
        payload = attach_auto_approval_policy({
            'workflow_state': {
                'item_states': [{
                    'item_id': 'playbook::observe',
                    'action_type': 'joint_observe',
                    'decision': 'expand',
                    'governance_mode': 'rollout',
                    'risk_level': 'low',
                    'approval_required': False,
                    'blocking_reasons': [],
                    'preconditions': [],
                    'confidence': 'medium',
                }],
                'summary': {},
            },
            'approval_state': {
                'items': [],
                'summary': {},
            },
        })
        row = payload['workflow_state']['item_states'][0]
        self.assertEqual(row['auto_approval_decision'], 'auto_approve')
        self.assertTrue(row['auto_approval_eligible'])
        self.assertFalse(row['requires_manual'])

    def test_approval_state_persists_terminal_decision_across_replay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'approval_state.db'))
            item_id = 'pool_switch::btc-focused'
            db.upsert_approval_state(
                item_id=item_id,
                approval_type='pool_switch',
                target='btc-focused',
                title='切换主池',
                decision='pending',
                state='pending',
                workflow_state='pending',
                replay_source='unit-test',
                details={'step': 'initial'},
            )
            db.record_approval('pool_switch', 'btc-focused', 'approved', {
                'item_id': item_id,
                'state': 'approved',
                'reason': 'looks good',
                'actor': 'tester',
                'replay_source': 'unit-test',
            })
            replayed = db.sync_approval_items([
                {
                    'item_id': item_id,
                    'approval_type': 'pool_switch',
                    'target': 'btc-focused',
                    'title': '切换主池',
                    'approval_state': 'pending',
                    'workflow_state': 'pending',
                }
            ], replay_source='workflow_replay', preserve_terminal=True)[0]
            self.assertEqual(replayed['state'], 'approved')
            self.assertEqual(replayed['decision'], 'approved')
            self.assertEqual(replayed['reason'], 'looks good')
            history = db.get_approval_history(limit=5)
            self.assertEqual(history[0]['decision'], 'approved')


    def test_approval_event_log_and_timeline_query(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'approval_events.db'))
            item_id = 'pool_switch::btc-focused'
            db.sync_approval_items([
                {
                    'item_id': item_id,
                    'approval_type': 'pool_switch',
                    'target': 'btc-focused',
                    'title': '切换主池',
                    'approval_state': 'pending',
                    'workflow_state': 'pending',
                }
            ], replay_source='workflow_replay')
            db.record_approval('pool_switch', 'btc-focused', 'approved', {
                'item_id': item_id,
                'state': 'approved',
                'workflow_state': 'ready',
                'reason': 'manual approve',
                'actor': 'tester',
                'replay_source': 'manual_review',
            })
            timeline = db.get_approval_timeline(item_id=item_id, ascending=True)
            self.assertEqual(len(timeline), 2)
            self.assertEqual(timeline[0]['event_type'], 'snapshot_sync')
            self.assertEqual(timeline[0]['normalized_event_type'], 'snapshot_sync')
            self.assertEqual(timeline[0]['provenance']['origin'], 'approval_db')
            self.assertEqual(timeline[0]['provenance']['source'], 'approval_timeline')
            self.assertEqual(timeline[0]['timestamp_info']['source'], 'approval_event_created_at')
            self.assertEqual(timeline[1]['event_type'], 'decision_recorded')
            self.assertEqual(timeline[1]['normalized_event_type'], 'decision_recorded')
            self.assertEqual(timeline[1]['decision'], 'approved')
            self.assertEqual(timeline[1]['actor'], 'tester')

    def test_approval_snapshot_rebuild_and_recovery_from_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'approval_recovery.db'))
            item_id = 'pool_switch::btc-focused'
            db.sync_approval_items([
                {
                    'item_id': item_id,
                    'approval_type': 'pool_switch',
                    'target': 'btc-focused',
                    'title': '切换主池',
                    'approval_state': 'pending',
                    'workflow_state': 'pending',
                }
            ], replay_source='workflow_replay')
            db.record_approval('pool_switch', 'btc-focused', 'approved', {
                'item_id': item_id,
                'state': 'approved',
                'workflow_state': 'ready',
                'reason': 'looks good',
                'actor': 'tester',
                'replay_source': 'manual_review',
            })
            rebuilt = db.rebuild_approval_snapshot(item_id)
            self.assertEqual(rebuilt['state'], 'approved')
            self.assertEqual(rebuilt['decision'], 'approved')
            self.assertEqual(rebuilt['reason'], 'looks good')

            sqlite_path = Path(tmpdir) / 'approval_recovery.db'
            with sqlite3.connect(sqlite_path) as conn:
                conn.execute('DELETE FROM approval_state WHERE item_id = ?', (item_id,))
                conn.commit()
            recovered = db.recover_approval_state(item_id)
            self.assertEqual(recovered['item_id'], item_id)
            self.assertEqual(recovered['state'], 'approved')
            self.assertEqual(recovered['decision'], 'approved')

    def test_workflow_helper_merges_persisted_decision_back_into_payload(self):
        payload = build_governance_workflow_ready_payload(build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ]))
        approval_record = build_workflow_approval_records(payload)[0]
        self.assertIn('stage_model', approval_record)
        self.assertIn('queue_progression', approval_record)
        self.assertIn('scheduled_review', approval_record)
        merged = merge_persisted_approval_state(payload, [{
            'item_id': approval_record['item_id'],
            'state': 'approved',
            'decision': 'approved',
            'reason': 'manual approve',
            'actor': 'tester',
            'updated_at': '2026-03-27 13:00:00',
            'replay_source': 'unit-test',
        }])
        approval_item = merged['approval_state']['items'][0]
        workflow_item = next(row for row in merged['workflow_state']['item_states'] if row['item_id'] == approval_item['playbook_id'])
        self.assertEqual(approval_item['approval_state'], 'approved')
        self.assertEqual(workflow_item['workflow_state'], 'ready')
        self.assertEqual(workflow_item['persisted_decision'], 'approved')

    def test_database_timeline_summary_surfaces_execution_timeline_and_recovery_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'approval_timeline_recovery.db'))
            item_id = 'approval::recover'
            db.upsert_approval_state(
                item_id=item_id,
                approval_type='workflow_approval',
                target='playbook::recover',
                title='Recoverable rollout item',
                decision='pending',
                state='pending',
                workflow_state='retry_pending',
                replay_source='unit-test',
                details={
                    'execution_status': 'recovered',
                    'retryable': True,
                    'rollback_hint': 'restore_previous_state_from_approval_timeline',
                    'dispatch_route': 'retry_queue',
                    'next_transition': 'retry_after_blockers_clear',
                    'last_transition': {
                        'from_execution_status': 'error',
                        'to_execution_status': 'recovered',
                        'rule': 'safe_apply_ready',
                    },
                    'recovered_from_execution_status': 'error',
                },
                event_type='rollout_executor_apply',
            )
            summary = db.get_approval_timeline_summary(item_id)
            self.assertEqual(summary['execution_timeline']['latest_status'], 'recovered')
            self.assertEqual(summary['execution_timeline']['recovered_from_status'], 'error')
            self.assertEqual(summary['recovery_policy']['policy'], 'recovered_monitoring')
            self.assertTrue(summary['recovery_policy']['rollback_candidate'])
            self.assertIn('exec=recovered', summary['summary_line'])
            self.assertIn('recovery=recovered_monitoring', summary['summary_line'])

    def test_stale_pending_cleanup_and_timeline_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'approval_cleanup.db'))
            item_id = 'pool_switch::btc-focused'
            db.sync_approval_items([
                {
                    'item_id': item_id,
                    'approval_type': 'pool_switch',
                    'target': 'btc-focused',
                    'title': '切换主池',
                    'approval_state': 'pending',
                    'workflow_state': 'pending',
                }
            ], replay_source='workflow_replay')
            with sqlite3.connect(Path(tmpdir) / 'approval_cleanup.db') as conn:
                conn.execute("UPDATE approval_state SET created_at = datetime('now', '-180 minutes'), updated_at = datetime('now', '-180 minutes'), last_seen_at = datetime('now', '-180 minutes') WHERE item_id = ?", (item_id,))
                conn.commit()
            stale_rows = db.get_stale_approval_states(stale_after_minutes=60)
            self.assertEqual(stale_rows[0]['item_id'], item_id)
            preview = db.cleanup_stale_approval_states(stale_after_minutes=60, dry_run=True)
            self.assertEqual(preview['matched_count'], 1)
            self.assertEqual(preview['items'][0]['action'], 'would_expire')
            result = db.cleanup_stale_approval_states(stale_after_minutes=60, dry_run=False)
            self.assertEqual(result['expired_count'], 1)
            state_row = db.get_approval_state(item_id)
            self.assertEqual(state_row['state'], 'expired')
            summary = db.get_approval_timeline_summary(item_id)
            self.assertEqual(summary['current']['state'], 'expired')
            self.assertIn('stale_cleanup', summary['event_counts'])
            self.assertIn('stale_cleanup', summary['normalized_event_types'])
            self.assertIn('approval_db', summary['provenance_origins'])
            self.assertIn('approval_timeline', summary['provenance_sources'])
            self.assertIn('approval_event_created_at', summary['timestamp_sources'])
            self.assertTrue(any(step['state'] == 'expired' for step in summary['decision_path']))
            self.assertIn('provenance', summary['decision_path'][-1])
            self.assertIn('timestamp_info', summary['decision_path'][-1])

    def test_recent_approval_decision_diff_tracks_state_transitions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'approval_diff.db'))
            item_id = 'pool_switch::btc-focused'
            db.sync_approval_items([
                {
                    'item_id': item_id,
                    'approval_type': 'pool_switch',
                    'target': 'btc-focused',
                    'title': '切换主池',
                    'approval_state': 'pending',
                    'workflow_state': 'pending',
                }
            ], replay_source='workflow_replay')
            db.record_approval('pool_switch', 'btc-focused', 'deferred', {
                'item_id': item_id,
                'state': 'deferred',
                'workflow_state': 'deferred',
                'reason': 'wait more data',
                'actor': 'tester',
            })
            diffs = db.get_recent_approval_decision_diff(limit=5)
            self.assertEqual(len(diffs), 1)
            self.assertEqual(diffs[0]['from']['state'], 'pending')
            self.assertEqual(diffs[0]['to']['state'], 'deferred')
            self.assertIn('state', diffs[0]['changed_fields'])
            overview = build_approval_audit_overview(decision_diffs=diffs)
            self.assertEqual(overview['decision_diff']['count'], 1)

    def test_rollout_executor_skeleton_defaults_to_disabled(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_disabled.db'))
            payload = {
                'workflow_state': {
                    'item_states': [{
                        'item_id': 'playbook::observe',
                        'title': 'Safe observe item',
                        'action_type': 'joint_observe',
                        'decision': 'expand',
                        'governance_mode': 'rollout',
                        'risk_level': 'low',
                        'approval_required': False,
                        'blocking_reasons': [],
                        'preconditions': [],
                        'workflow_state': 'pending',
                        'confidence': 'high',
                    }],
                    'summary': {},
                },
                'approval_state': {
                    'items': [{
                        'approval_id': 'approval::observe',
                        'playbook_id': 'playbook::observe',
                        'title': 'Safe observe item',
                        'action_type': 'joint_observe',
                        'approval_state': 'pending',
                        'decision_state': 'pending',
                        'risk_level': 'low',
                        'approval_required': False,
                        'blocked_by': [],
                    }],
                    'summary': {},
                },
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=StubConfig())
            state_row = db.get_approval_state('approval::observe')
            executor = result['rollout_executor']
            self.assertEqual(executor['status'], 'disabled')
            self.assertEqual(executor['summary']['skipped_count'], 1)
            self.assertEqual(state_row['state'], 'pending')
            self.assertEqual(state_row['workflow_state'], 'pending')
            self.assertTrue(executor['supported_action_map']['executable'])
            self.assertTrue(executor['supported_action_map']['queue_only'])

    def test_approval_state_machine_api_exposes_recovery_policy_and_execution_timeline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'approval_state_machine_api.db'))
            item_id = 'approval::recover'
            db.upsert_approval_state(
                item_id=item_id,
                approval_type='workflow_approval',
                target='playbook::recover',
                title='Recoverable rollout item',
                decision='pending',
                state='pending',
                workflow_state='retry_pending',
                replay_source='unit-test',
                details={
                    'execution_status': 'recovered',
                    'retryable': True,
                    'rollback_hint': 'restore_previous_state_from_approval_timeline',
                    'dispatch_route': 'retry_queue',
                    'last_transition': {'from_execution_status': 'error', 'to_execution_status': 'recovered'},
                    'recovered_from_execution_status': 'error',
                },
                event_type='rollout_executor_apply',
            )
            old_db = app.config.get('db_instance')
            app.config['db_instance'] = db
            import dashboard.api as dashboard_api_module
            old_module_db = dashboard_api_module.db
            dashboard_api_module.db = db
            try:
                with app.test_client() as client:
                    response = client.get('/api/approvals/state-machine')
                    self.assertEqual(response.status_code, 200)
                    payload = response.get_json()
                    self.assertEqual(payload['data'][0]['execution_timeline']['latest_status'], 'recovered')
                    self.assertEqual(payload['data'][0]['recovery_policy']['policy'], 'recovered_monitoring')
                    self.assertEqual(payload['summary']['recovered_count'], 1)
                    self.assertEqual(payload['summary']['recovery_policy_counts']['recovered_monitoring'], 1)
            finally:
                app.config['db_instance'] = old_db
                dashboard_api_module.db = old_module_db

    def test_workflow_recovery_view_api_exposes_retry_and_manual_buckets(self):
        sample_payload = {
            'summary': {},
            'workflow_state': {
                'item_states': [
                    {'item_id': 'playbook::retry', 'title': 'Retry item', 'workflow_state': 'execution_failed', 'action_type': 'joint_observe', 'state_machine': _build_state_machine_semantics(item_id='approval::retry', approval_state='pending', workflow_state='execution_failed', execution_status='error', retryable=True, rollback_hint='restore_previous_state_from_approval_timeline', dispatch_route='retry_queue', next_transition='retry_execution')},
                    {'item_id': 'playbook::manual', 'title': 'Manual item', 'workflow_state': 'execution_failed', 'action_type': 'joint_expand_guarded', 'state_machine': _build_state_machine_semantics(item_id='approval::manual', approval_state='pending', workflow_state='execution_failed', execution_status='error', retryable=False, blocked_by=['critical_risk'])},
                ],
                'summary': {},
            },
            'approval_state': {
                'items': [
                    {'approval_id': 'approval::retry', 'playbook_id': 'playbook::retry', 'approval_state': 'pending'},
                    {'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'approval_state': 'pending'},
                ],
                'summary': {},
            },
        }
        import dashboard.api as dashboard_api_module
        old_export = dashboard_api_module.export_calibration_payload
        old_run_all = dashboard_api_module.backtester.run_all
        old_persist = dashboard_api_module._persist_workflow_approval_payload
        dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': sample_payload
        dashboard_api_module.backtester.run_all = lambda symbols: {'calibration_report': {}}
        dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='workflow_recovery_view_api': payload
        try:
            with app.test_client() as client:
                response = client.get('/api/backtest/workflow-recovery-view')
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload['view'], 'workflow_recovery_view')
                self.assertEqual(payload['data']['schema_version'], 'm5_workflow_recovery_view_v1')
                self.assertEqual(payload['summary']['retry_queue_count'], 1)
                self.assertEqual(payload['summary']['manual_recovery_count'], 1)
        finally:
            dashboard_api_module.export_calibration_payload = old_export
            dashboard_api_module.backtester.run_all = old_run_all
            dashboard_api_module._persist_workflow_approval_payload = old_persist

    def test_rollout_executor_skeleton_dry_run_plans_without_persisting(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'dry_run',
                'actor': 'system:test-rollout-executor',
                'source': 'unit_test_rollout_executor',
                'reason_prefix': 'unit-test rollout executor',
                'allowed_action_types': ['joint_observe'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_dry_run.db'))
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::observe', 'title': 'Safe observe item', 'action_type': 'joint_observe',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high'}], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::observe', 'playbook_id': 'playbook::observe', 'title': 'Safe observe item',
                    'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            executor = result['rollout_executor']
            item = executor['items'][0]
            state_row = db.get_approval_state('approval::observe')
            self.assertEqual(executor['status'], 'dry_run')
            self.assertTrue(executor['dry_run'])
            self.assertEqual(executor['summary']['applied_count'], 1)
            self.assertEqual(item['result']['disposition'], 'dry_run')
            self.assertTrue(item['apply']['attempted'])
            self.assertFalse(item['apply']['persisted'])
            self.assertEqual(state_row['state'], 'pending')
            self.assertEqual(item['audit']['safe_boundary']['real_trade_execution'], False)

    def test_rollout_executor_skeleton_dispatches_safe_actions_and_queues_sensitive_actions(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'actor': 'system:test-rollout-executor',
                'source': 'unit_test_rollout_executor',
                'reason_prefix': 'unit-test rollout executor',
                'allowed_action_types': ['joint_observe', 'joint_expand_guarded'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_dispatch.db'))
            payload = {
                'workflow_state': {
                    'item_states': [
                        {'item_id': 'playbook::observe', 'title': 'Safe observe item', 'action_type': 'joint_observe', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high'},
                        {'item_id': 'playbook::expand', 'title': 'Guarded expand item', 'action_type': 'joint_expand_guarded', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high'},
                    ],
                    'summary': {},
                },
                'approval_state': {
                    'items': [
                        {'approval_id': 'approval::observe', 'playbook_id': 'playbook::observe', 'title': 'Safe observe item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []},
                        {'approval_id': 'approval::expand', 'playbook_id': 'playbook::expand', 'title': 'Guarded expand item', 'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []},
                    ],
                    'summary': {},
                },
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            executor = result['rollout_executor']
            observe = db.get_approval_state('approval::observe')
            expand = db.get_approval_state('approval::expand')
            observe_item = next(row for row in executor['items'] if row['item_id'] == 'approval::observe')
            expand_item = next(row for row in executor['items'] if row['item_id'] == 'approval::expand')
            self.assertEqual(observe['state'], 'ready')
            self.assertEqual(observe['workflow_state'], 'ready')
            self.assertEqual(observe['details']['execution_layer'], 'rollout_executor_skeleton')
            self.assertFalse(observe['details']['real_trade_execution'])
            self.assertEqual(observe_item['result']['disposition'], 'applied')
            self.assertEqual(expand['state'], 'pending')
            self.assertEqual(expand_item['result']['disposition'], 'queued')
            self.assertEqual(expand_item['dispatch']['reason'], 'live_rollout_parameter_change_not_supported')
            self.assertEqual(expand_item['plan']['queue_plan']['blocked_reason'], 'live_rollout_parameter_change_not_supported')
            self.assertEqual(executor['summary']['applied_count'], 1)
            self.assertEqual(executor['summary']['queued_count'], 1)

    def test_rollout_executor_skeleton_records_audit_fields_for_safe_stage_prepare(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'actor': 'system:test-rollout-executor',
                'source': 'unit_test_rollout_executor',
                'reason_prefix': 'unit-test rollout executor',
                'allowed_action_types': ['joint_stage_prepare'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_audit.db'))
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high',
                    'current_rollout_stage': 'observe', 'target_rollout_stage': 'guarded_prepare'}], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::stage', 'playbook_id': 'playbook::stage', 'title': 'Stage prepare item',
                    'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            executor_item = result['rollout_executor']['items'][0]
            state_row = db.get_approval_state('approval::stage')
            timeline = db.get_approval_timeline(item_id='approval::stage', ascending=True)
            self.assertEqual(executor_item['audit']['executor'], 'rollout_executor_skeleton')
            self.assertEqual(executor_item['plan']['executor_class'], 'stage_metadata')
            self.assertEqual(executor_item['plan']['transition_rule'], 'stage_prepare_ready_for_safe_apply')
            self.assertEqual(executor_item['plan']['dispatch_route'], 'stage_metadata_apply')
            self.assertEqual(executor_item['plan']['next_transition'], 'promote_to_target_stage')
            self.assertTrue(executor_item['plan']['retryable'])
            self.assertTrue(executor_item['plan']['rollback_capable'])
            self.assertEqual(state_row['details']['stage_transition']['from'], 'observe')
            self.assertEqual(state_row['details']['stage_transition']['to'], 'guarded_prepare')
            self.assertEqual(state_row['details']['transition_rule'], 'stage_prepare_ready_for_safe_apply')
            self.assertEqual(state_row['details']['dispatch_route'], 'stage_metadata_apply')
            self.assertEqual(state_row['details']['execution_mode'], 'controlled')
            self.assertEqual(timeline[-1]['event_type'], 'controlled_rollout_stage_prepare')
            self.assertEqual(result['rollout_executor']['stage_progression']['summary']['applied_count'], 1)
            self.assertEqual(result['rollout_executor']['stage_progression']['items'][0]['stage_progression']['next_transition'], 'promote_to_target_stage')

    def test_rollout_executor_skeleton_exposes_handler_map_and_queue_plan(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'actor': 'system:test-rollout-executor',
                'source': 'unit_test_rollout_executor',
                'reason_prefix': 'unit-test rollout executor',
                'allowed_action_types': ['joint_expand_guarded'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_queue_plan.db'))
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::expand', 'title': 'Guarded expand item', 'action_type': 'joint_expand_guarded',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high',
                    'queue_name': 'manual_rollout_review'}], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::expand', 'playbook_id': 'playbook::expand', 'title': 'Guarded expand item',
                    'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            executor = result['rollout_executor']
            item = executor['items'][0]
            handler = executor['supported_action_map']['handlers']['joint_expand_guarded']
            self.assertEqual(handler['handler_key'], 'queue_only::live_trading_change')
            self.assertEqual(item['status'], 'queued')
            self.assertEqual(item['dispatch']['code'], 'QUEUE_ONLY')
            self.assertEqual(item['plan']['transition_rule'], 'manual_gate_before_dispatch')
            self.assertEqual(item['plan']['dispatch_route'], 'manual_review_queue')
            self.assertEqual(item['plan']['next_transition'], 'await_manual_approval')
            self.assertTrue(item['plan']['retryable'])
            self.assertEqual(item['plan']['queue_plan']['queue_name'], 'manual_rollout_review')
            self.assertEqual(item['plan']['queue_plan']['dispatch_route'], 'manual_review_queue')
            self.assertFalse(item['plan']['queue_plan']['real_trade_execution'])
            self.assertEqual(executor['summary']['by_disposition']['queued'], 1)

    def test_rollout_executor_skeleton_marks_queue_only_items_as_blocked_by_approval_when_manual_gate_remains(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'actor': 'system:test-rollout-executor',
                'source': 'unit_test_rollout_executor',
                'reason_prefix': 'unit-test rollout executor',
                'allowed_action_types': ['joint_expand_guarded'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_queue_gate.db'))
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::expand-manual', 'title': 'Guarded expand manual', 'action_type': 'joint_expand_guarded',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'medium', 'approval_required': True,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high',
                    'queue_name': 'manual_rollout_review',
                    'queue_progression': {'status': 'awaiting_approval', 'reason': 'manual_approval_pending'}}], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::expand-manual', 'playbook_id': 'playbook::expand-manual', 'title': 'Guarded expand manual',
                    'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending',
                    'risk_level': 'medium', 'approval_required': True, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            item = result['rollout_executor']['items'][0]
            queue_plan = item['plan']['queue_plan']
            self.assertEqual(item['status'], 'blocked_by_approval')
            self.assertEqual(item['result']['disposition'], 'blocked_by_approval')
            self.assertEqual(item['dispatch']['code'], 'APPROVAL_GATED_QUEUE')
            self.assertEqual(queue_plan['approval_hook']['status'], 'blocked_by_approval')
            self.assertEqual(queue_plan['approval_hook']['next_action'], 'await_manual_approval')
            self.assertEqual(queue_plan['transition_rule'], 'manual_gate_before_dispatch')
            self.assertEqual(queue_plan['dispatch_route'], 'manual_review_queue')
            self.assertEqual(queue_plan['next_transition'], 'await_manual_approval')
            self.assertTrue(queue_plan['retryable'])
            self.assertEqual(queue_plan['queue_transition']['to_queue_status'], 'blocked_by_approval')
            self.assertEqual(queue_plan['queue_progression']['status'], 'blocked_by_approval')
            self.assertEqual(queue_plan['queue_priority'], 'approval_blocked')
            self.assertEqual(result['rollout_executor']['summary']['by_disposition']['blocked_by_approval'], 1)

    def test_rollout_executor_skeleton_marks_queue_only_items_as_deferred_when_preconditions_are_open(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'actor': 'system:test-rollout-executor',
                'source': 'unit_test_rollout_executor',
                'reason_prefix': 'unit-test rollout executor',
                'allowed_action_types': ['joint_expand_guarded'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_queue_deferred.db'))
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::expand-deferred', 'title': 'Guarded expand deferred', 'action_type': 'joint_expand_guarded',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': ['waiting_metrics'], 'preconditions': [{'type': 'wait', 'value': 'more_samples', 'status': 'open'}],
                    'workflow_state': 'pending', 'confidence': 'medium', 'queue_name': 'manual_rollout_review'}], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::expand-deferred', 'playbook_id': 'playbook::expand-deferred', 'title': 'Guarded expand deferred',
                    'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': ['waiting_metrics', 'more_samples']}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            item = result['rollout_executor']['items'][0]
            queue_plan = item['plan']['queue_plan']
            self.assertEqual(item['status'], 'deferred')
            self.assertEqual(item['result']['disposition'], 'deferred')
            self.assertEqual(item['dispatch']['code'], 'QUEUE_DEFERRED')
            self.assertEqual(queue_plan['approval_hook']['status'], 'deferred')
            self.assertEqual(queue_plan['approval_hook']['next_action'], 'wait_for_preconditions')
            self.assertEqual(queue_plan['transition_rule'], 'defer_until_blockers_clear')
            self.assertEqual(queue_plan['dispatch_route'], 'deferred_review_queue')
            self.assertEqual(queue_plan['next_transition'], 'retry_after_blockers_clear')
            self.assertTrue(queue_plan['retryable'])
            self.assertEqual(queue_plan['queue_transition']['to_queue_status'], 'deferred')
            self.assertTrue(queue_plan['queue_transition']['transition_reason'].startswith('blocked_by:'))
            self.assertEqual(queue_plan['queue_priority'], 'deferred_review')
            self.assertEqual(result['rollout_executor']['summary']['by_disposition']['deferred'], 1)

    def test_rollout_executor_consumes_queue_plan_into_persisted_queue_state(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'actor': 'system:test-rollout-executor',
                'source': 'unit_test_rollout_executor',
                'reason_prefix': 'unit-test rollout executor',
                'allowed_action_types': ['joint_expand_guarded'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_queue_consume.db'))
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::expand', 'title': 'Guarded expand item', 'action_type': 'joint_expand_guarded',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high',
                    'queue_name': 'manual_rollout_review'}], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::expand', 'playbook_id': 'playbook::expand', 'title': 'Guarded expand item',
                    'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            item = result['rollout_executor']['items'][0]
            state_row = db.get_approval_state('approval::expand')
            timeline = db.get_approval_timeline(item_id='approval::expand', ascending=True)
            self.assertEqual(item['status'], 'queued')
            self.assertEqual(item['apply']['operation'], 'queue_plan_consume')
            self.assertEqual(state_row['workflow_state'], 'queued')
            self.assertTrue(state_row['details']['queue_plan_consumed'])
            self.assertEqual(state_row['details']['execution_layer'], 'rollout_queue_executor')
            self.assertEqual(state_row['details']['dispatch_route'], 'manual_review_queue')
            self.assertEqual(state_row['details']['queue_plan']['queue_name'], 'manual_rollout_review')
            self.assertEqual(timeline[-1]['event_type'], 'rollout_executor_queue_promoted')
            self.assertEqual(result['workflow_state']['item_states'][0]['workflow_state'], 'queued')
            self.assertEqual(result['rollout_executor']['status'], 'executed')

    def test_rollout_executor_consumes_approval_gate_and_deferred_queue_states(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'allowed_action_types': ['joint_expand_guarded'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_queue_gate_consume.db'))
            payload = {
                'workflow_state': {'item_states': [
                    {'item_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'medium', 'approval_required': True, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'queue_progression': {'status': 'awaiting_approval', 'reason': 'manual_approval_pending'}},
                    {'item_id': 'playbook::deferred', 'title': 'Deferred item', 'action_type': 'joint_expand_guarded', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': ['waiting_metrics'], 'preconditions': [{'type': 'wait', 'value': 'more_samples', 'status': 'open'}], 'workflow_state': 'pending', 'confidence': 'medium'}
                ], 'summary': {}},
                'approval_state': {'items': [
                    {'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'medium', 'approval_required': True, 'blocked_by': []},
                    {'approval_id': 'approval::deferred', 'playbook_id': 'playbook::deferred', 'title': 'Deferred item', 'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': ['waiting_metrics', 'more_samples']}
                ], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            manual_row = db.get_approval_state('approval::manual')
            deferred_row = db.get_approval_state('approval::deferred')
            self.assertEqual(manual_row['workflow_state'], 'blocked_by_approval')
            self.assertEqual(manual_row['state'], 'pending')
            self.assertEqual(manual_row['details']['queue_result_action'], 'blocked_by_approval')
            self.assertEqual(deferred_row['workflow_state'], 'deferred')
            self.assertEqual(deferred_row['state'], 'deferred')
            self.assertEqual(deferred_row['details']['queue_result_action'], 'deferred')
            self.assertEqual(result['rollout_executor']['summary']['by_disposition']['blocked_by_approval'], 1)
            self.assertEqual(result['rollout_executor']['summary']['by_disposition']['deferred'], 1)


    def test_rollout_executor_persists_recovered_execution_status_after_retryable_safe_apply(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}
            def get(self, key, default=None):
                return self.values.get(key, default)
        with tempfile.TemporaryDirectory() as tmpdir:
            config = StubConfig({
                'governance.rollout_executor': {
                    'enabled': True,
                    'mode': 'controlled',
                    'allowed_action_types': ['joint_observe'],
                    'actor': 'system:test-rollout-executor',
                    'source': 'unit_test_rollout_executor',
                    'reason_prefix': 'unit-test rollout executor',
                }
            })
            db = Database(str(Path(tmpdir) / 'rollout_executor_recovered.db'))
            db.upsert_approval_state(
                item_id='approval::observe',
                approval_type='joint_observe',
                target='playbook::observe',
                title='Recoverable observe item',
                decision='pending',
                state='pending',
                workflow_state='execution_failed',
                details={'execution_status': 'error', 'transition_rule': 'retry_safe_apply'},
                replay_source='seed',
            )
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::observe', 'title': 'Recoverable observe item', 'action_type': 'joint_observe', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high'}], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::observe', 'playbook_id': 'playbook::observe', 'title': 'Recoverable observe item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'auto_approval_decision': 'auto_approve', 'auto_approval_eligible': True, 'requires_manual': False}], 'summary': {}},
            }
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            state_row = db.get_approval_state('approval::observe')
            executor_item = result['rollout_executor']['items'][0]
            self.assertEqual(state_row['details']['execution_status'], 'recovered')
            self.assertEqual(state_row['details']['recovered_from_execution_status'], 'error')
            self.assertEqual(state_row['details']['last_transition']['to_execution_status'], 'applied')
            self.assertEqual(executor_item['execution_status'], 'recovered')
            self.assertEqual(result['approval_state']['items'][0]['state_machine']['execution_status'], 'recovered')

    def test_rollout_executor_skeleton_marks_idempotent_safe_apply_as_skip(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'actor': 'system:test-rollout-executor',
                'source': 'unit_test_rollout_executor',
                'reason_prefix': 'unit-test rollout executor',
                'allowed_action_types': ['joint_observe'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_idempotent.db'))
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::observe', 'title': 'Safe observe item', 'action_type': 'joint_observe',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high'}], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::observe', 'playbook_id': 'playbook::observe', 'title': 'Safe observe item',
                    'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            first = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            second = execute_rollout_executor(first, db, config=config, replay_source='unit-test-replay-2')
            item = second['rollout_executor']['items'][0]
            self.assertEqual(item['status'], 'skipped')
            self.assertEqual(item['result']['code'], 'IDEMPOTENT_ALREADY_APPLIED')
            self.assertEqual(item['apply']['status'], 'idempotent_skip')
            self.assertTrue(item['apply']['idempotency_key'].startswith('rollout_executor::approval::observe::joint_observe'))
            self.assertEqual(second['rollout_executor']['summary']['by_disposition']['skipped'], 1)

    def test_rollout_executor_registry_exposes_safe_stage_handlers_and_fallback(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'dry_run',
                'allowed_action_types': ['joint_stage_prepare', 'joint_metadata_annotate'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_registry.db'))
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high',
                    'current_rollout_stage': 'observe', 'target_rollout_stage': 'guarded_prepare'}], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::stage', 'playbook_id': 'playbook::stage', 'title': 'Stage prepare item',
                    'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            registry = result['rollout_executor']['action_registry']
            self.assertIn('observe_ready', registry['handlers'])
            self.assertIn('stage_prepare_safe', registry['handlers'])
            self.assertEqual(registry['actions']['joint_stage_prepare']['handler']['route'], 'stage_metadata_apply')
            self.assertEqual(registry['actions']['joint_stage_prepare']['handler']['stage_family'], 'stage')
            self.assertEqual(registry['actions']['joint_stage_prepare']['stage_handler_profile']['stage_key'], 'guarded_prepare')
            self.assertEqual(registry['actions']['joint_stage_prepare']['stage_handler_profile']['stage_index'], 2)
            self.assertEqual(registry['handlers']['stage_prepare_safe']['supported_stages'], ['guarded_prepare', 'controlled_apply'])
            self.assertEqual(registry['handlers']['stage_prepare_safe']['stage_profiles'][0]['stage_key'], 'guarded_prepare')
            self.assertEqual(registry['fallback_handler']['handler_key'], 'unsupported::unsupported_action')


    def test_rollout_executor_registry_exposes_gate_policy_and_idempotency_rules(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'dry_run',
                'allowed_action_types': ['joint_stage_prepare'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_gate_registry.db'))
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high',
                    'current_rollout_stage': 'observe', 'target_rollout_stage': 'guarded_prepare'}], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::stage', 'playbook_id': 'playbook::stage', 'title': 'Stage prepare item',
                    'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            registry = result['rollout_executor']['action_registry']
            entry = registry['actions']['joint_stage_prepare']
            self.assertEqual(entry['gate_policy']['auto_advance']['mode'], 'very_safe_apply')
            self.assertIn('allowlisted_action', entry['gate_policy']['preconditions'])
            self.assertIn('idempotent-skip', entry['idempotency_rule'])
            self.assertTrue(entry['gate_policy']['rollback']['capable'])

    def test_rollout_executor_richer_safe_stage_handlers_preserve_observability_fields(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'allowed_action_types': ['joint_queue_promote_safe', 'joint_review_schedule', 'joint_metadata_annotate'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_richer_handlers.db'))
            payload = {
                'workflow_state': {'item_states': [
                    {'item_id': 'playbook::queue', 'title': 'Queue promotion item', 'action_type': 'joint_queue_promote_safe', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'bucket_id': 'bucket::priority'},
                    {'item_id': 'playbook::review', 'title': 'Review schedule item', 'action_type': 'joint_review_schedule', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'review_after_hours': 3},
                    {'item_id': 'playbook::annotate', 'title': 'Metadata annotation item', 'action_type': 'joint_metadata_annotate', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'annotations': {'operator_note': 'safe'}, 'annotation_tags': ['safe', 'audit']},
                ], 'summary': {}},
                'approval_state': {'items': [
                    {'approval_id': 'approval::queue', 'playbook_id': 'playbook::queue', 'title': 'Queue promotion item', 'action_type': 'joint_queue_promote_safe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'bucket_id': 'bucket::priority'},
                    {'approval_id': 'approval::review', 'playbook_id': 'playbook::review', 'title': 'Review schedule item', 'action_type': 'joint_review_schedule', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'review_after_hours': 3},
                    {'approval_id': 'approval::annotate', 'playbook_id': 'playbook::annotate', 'title': 'Metadata annotation item', 'action_type': 'joint_metadata_annotate', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'annotations': {'operator_note': 'safe'}, 'annotation_tags': ['safe', 'audit']},
                ], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            queue_row = db.get_approval_state('approval::queue')
            review_row = db.get_approval_state('approval::review')
            annotate_row = db.get_approval_state('approval::annotate')
            self.assertEqual(queue_row['details']['queue_handler']['route'], 'queue_metadata_apply')
            self.assertEqual(queue_row['details']['safe_handler_disposition'], 'apply')
            self.assertEqual(review_row['details']['review_handler']['route'], 'review_metadata_apply')
            self.assertEqual(review_row['details']['observability']['handler_key'], 'apply::review_schedule_safe')
            self.assertEqual(annotate_row['details']['metadata_handler']['annotation_tags'], ['safe', 'audit'])
            self.assertTrue(annotate_row['details']['serialization_ready'])


    def test_rollout_executor_exposes_auto_advance_and_rollback_gates(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'allowed_action_types': ['joint_stage_prepare', 'joint_review_schedule'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_gates.db'))
            payload = {
                'workflow_state': {'item_states': [
                    {'item_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'current_rollout_stage': 'observe', 'target_rollout_stage': 'guarded_prepare'},
                    {'item_id': 'playbook::review', 'title': 'Overdue review item', 'action_type': 'joint_review_schedule', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'critical', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'execution_failed', 'confidence': 'high', 'review_due_at': '2020-01-01T00:00:00Z'},
                ], 'summary': {}},
                'approval_state': {'items': [
                    {'approval_id': 'approval::stage', 'playbook_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []},
                    {'approval_id': 'approval::review', 'playbook_id': 'playbook::review', 'title': 'Overdue review item', 'action_type': 'joint_review_schedule', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'critical', 'approval_required': False, 'blocked_by': [], 'review_due_at': '2020-01-01T00:00:00Z'},
                ], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            items = {row['item_id']: row for row in result['rollout_executor']['items']}
            safe_gate = items['approval::stage']['plan']['auto_advance_gate']
            self.assertTrue(safe_gate['allowed'])
            self.assertEqual(safe_gate['readiness_score'], 100)
            self.assertFalse(safe_gate['manual_required'])
            rollback_gate = items['approval::review']['plan']['rollback_gate']
            self.assertTrue(rollback_gate['candidate'])
            self.assertIn('execution_error', rollback_gate['triggered'])
            self.assertIn('review_overdue', rollback_gate['triggered'])
            self.assertIn('critical_risk', rollback_gate['triggered'])
            blocked_gate = items['approval::review']['plan']['auto_advance_gate']
            self.assertFalse(blocked_gate['allowed'])
            self.assertIn('risk_level:critical', blocked_gate['blockers'])

    def test_rollout_executor_validation_gate_blocks_auto_advance_when_replay_not_ready(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}
            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({'governance.rollout_executor': {'enabled': True, 'mode': 'controlled', 'allowed_action_types': ['joint_stage_prepare']}})
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_validation_gate.db'))
            payload = {
                'validation_replay': {'summary': {'coverage_matrix': {'schema_version': 'm5_validation_coverage_matrix_v1', 'ready_for_low_intervention_gate': False, 'required_capability_count': 8, 'covered_required_count': 7, 'passing_required_count': 7, 'missing_required': ['testnet_bridge_controlled_execute'], 'failing_required': []}, 'readiness': {'low_intervention_gate_ready': False, 'missing_required_capabilities': ['testnet_bridge_controlled_execute'], 'failing_required_capabilities': [], 'failing_case_count': 0}}},
                'workflow_state': {'item_states': [{'item_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'current_rollout_stage': 'observe', 'target_rollout_stage': 'guarded_prepare'}], 'summary': {}},
                'approval_state': {'items': [{'approval_id': 'approval::stage', 'playbook_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            gate = result['rollout_executor']['items'][0]['plan']['auto_advance_gate']
            self.assertFalse(gate['allowed'])
            self.assertEqual(gate['readiness_score'], 80)
            self.assertIn('validation_gate:not_ready', gate['blockers'])
            self.assertIn('validation_gate_ready', gate['required_flags'])
            self.assertFalse(gate['required_flags']['validation_gate_ready'])
            self.assertFalse(result['rollout_executor']['validation_gate']['ready'])
            self.assertEqual(result['rollout_executor']['items'][0]['status'], 'skipped')
            self.assertEqual(result['rollout_executor']['items'][0]['result']['reason'], 'validation_gate_gap')
            self.assertEqual(result['rollout_executor']['items'][0]['plan']['execution_gate']['effect'], 'blocked_gap')

    def test_rollout_executor_validation_gate_regression_opens_rollback_candidate(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}
            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({'governance.rollout_executor': {'enabled': True, 'mode': 'controlled', 'allowed_action_types': ['joint_stage_prepare']}})
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_validation_regression.db'))
            payload = {
                'validation_replay': {'summary': {'coverage_matrix': {'schema_version': 'm5_validation_coverage_matrix_v1', 'ready_for_low_intervention_gate': False, 'required_capability_count': 8, 'covered_required_count': 8, 'passing_required_count': 7, 'missing_required': [], 'failing_required': ['transition_policy_contract']}, 'readiness': {'low_intervention_gate_ready': False, 'missing_required_capabilities': [], 'failing_required_capabilities': ['transition_policy_contract'], 'failing_case_count': 1}}},
                'workflow_state': {'item_states': [{'item_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'ready', 'confidence': 'high', 'current_rollout_stage': 'guarded_prepare', 'target_rollout_stage': 'controlled_apply'}], 'summary': {}},
                'approval_state': {'items': [{'approval_id': 'approval::stage', 'playbook_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'ready', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            rollback_gate = result['rollout_executor']['items'][0]['plan']['rollback_gate']
            self.assertTrue(rollback_gate['candidate'])
            self.assertIn('validation_gate_regressed', rollback_gate['triggered'])
            self.assertFalse(rollback_gate['validation_gate']['ready'])
            self.assertEqual(result['rollout_executor']['items'][0]['result']['reason'], 'validation_gate_regression')
            self.assertEqual(result['rollout_executor']['items'][0]['plan']['execution_gate']['effect'], 'blocked_regression')

    def test_rollout_executor_validation_gate_stale_snapshot_freezes_without_regression(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}
            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({'governance.rollout_executor': {'enabled': True, 'mode': 'controlled', 'allowed_action_types': ['joint_stage_prepare']}})
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_validation_stale.db'))
            payload = {
                'validation_replay': {'summary': {
                    'generated_at': '2020-01-01T00:00:00Z',
                    'coverage_matrix': {'schema_version': 'm5_validation_coverage_matrix_v1', 'ready_for_low_intervention_gate': True, 'required_capability_count': 8, 'covered_required_count': 8, 'passing_required_count': 8, 'missing_required': [], 'failing_required': []},
                    'readiness': {'low_intervention_gate_ready': True, 'missing_required_capabilities': [], 'failing_required_capabilities': [], 'failing_case_count': 0},
                    'freshness_policy': {'enabled': True, 'max_age_minutes': 30},
                }},
                'workflow_state': {'item_states': [{'item_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'ready', 'confidence': 'high', 'current_rollout_stage': 'guarded_prepare', 'target_rollout_stage': 'controlled_apply'}], 'summary': {}},
                'approval_state': {'items': [{'approval_id': 'approval::stage', 'playbook_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'ready', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            item = result['rollout_executor']['items'][0]
            gate = item['plan']['execution_gate']
            rollback_gate = item['plan']['rollback_gate']
            self.assertEqual(gate['primary_reason'], 'validation_gate_stale')
            self.assertEqual(gate['effect'], 'blocked_stale')
            self.assertIn('validation_stale', gate['validation_gate']['reasons'])
            self.assertTrue(gate['validation_gate']['stale'])
            self.assertFalse(gate['validation_gate']['ready'])
            self.assertFalse(rollback_gate['candidate'])
            self.assertNotIn('validation_gate_regressed', rollback_gate['triggered'])
            self.assertEqual(item['result']['reason'], 'validation_gate_stale')


    def test_rollout_executor_stage_handlers_expose_owner_waiting_points_and_next_transition(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'allowed_action_types': ['joint_stage_prepare', 'joint_review_schedule'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_stage_handlers.db'))
            payload = {
                'workflow_state': {'item_states': [
                    {
                        'item_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare',
                        'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                        'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high',
                        'current_rollout_stage': 'observe', 'target_rollout_stage': 'guarded_prepare'
                    },
                    {
                        'item_id': 'playbook::review', 'title': 'Review item', 'action_type': 'joint_review_schedule',
                        'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'critical', 'approval_required': False,
                        'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'execution_failed', 'confidence': 'high',
                        'current_rollout_stage': 'controlled_apply', 'target_rollout_stage': 'review_pending', 'review_due_at': '2020-01-01T00:00:00Z'
                    },
                ], 'summary': {}},
                'approval_state': {'items': [
                    {
                        'approval_id': 'approval::stage', 'playbook_id': 'playbook::stage', 'title': 'Stage prepare item',
                        'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending',
                        'risk_level': 'low', 'approval_required': False, 'blocked_by': []
                    },
                    {
                        'approval_id': 'approval::review', 'playbook_id': 'playbook::review', 'title': 'Review item',
                        'action_type': 'joint_review_schedule', 'approval_state': 'pending', 'decision_state': 'pending',
                        'risk_level': 'critical', 'approval_required': False, 'blocked_by': [], 'review_due_at': '2020-01-01T00:00:00Z'
                    },
                ], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            items = {row['item_id']: row for row in result['rollout_executor']['items']}
            stage_handler = items['approval::stage']['plan']['stage_handler']
            self.assertEqual(stage_handler['stage_key'], 'guarded_prepare')
            self.assertEqual(stage_handler['owner'], 'system')
            self.assertTrue(stage_handler['auto_progression'])
            self.assertEqual(stage_handler['next_transition'], 'promote_to_controlled_apply')
            self.assertEqual(stage_handler['stage_profile']['stage_key'], 'guarded_prepare')
            self.assertEqual(stage_handler['stage_profile']['stage_index'], 2)
            self.assertEqual(stage_handler['lifecycle']['phase'], 'system_guarded')
            self.assertEqual(stage_handler['operator_handoff']['lane'], 'auto_batch')
            self.assertFalse(stage_handler['progression']['blocked'])
            review_handler = items['approval::review']['plan']['stage_handler']
            self.assertEqual(review_handler['stage_key'], 'review_pending')
            self.assertEqual(review_handler['owner'], 'operator')
            self.assertTrue(review_handler['rollback_candidate'])
            self.assertIn('review_overdue', review_handler['waiting_on'])
            self.assertIn('rollback_gate_triggered', review_handler['waiting_on'])
            self.assertEqual(review_handler['why_stopped'], 'review_checkpoint_overdue')
            self.assertEqual(review_handler['stage_profile']['stage_key'], 'review_pending')
            self.assertEqual(review_handler['operator_handoff']['lane'], 'rollback_candidate')
            self.assertEqual(items['approval::review']['plan']['rollback_gate']['next_action'], 'prepare_rollback_review')
            self.assertEqual(items['approval::stage']['plan']['stage_loop']['loop_state'], 'auto_advance')
            self.assertEqual(items['approval::review']['plan']['stage_loop']['loop_state'], 'rollback_prepare')
            self.assertEqual(items['approval::review']['result']['stage_loop']['recommended_action'], 'rollback_prepare')
            stage_progression = result['rollout_executor']['stage_progression']['items'][1]['stage_progression']
            self.assertEqual(stage_progression['stage_handler']['responsible_actor'], 'operator')
            self.assertEqual(stage_progression['stage_loop']['loop_state'], 'rollback_prepare')
            self.assertEqual(result['rollout_executor']['stage_progression']['summary']['auto_advance_count'], 1)
            self.assertEqual(result['rollout_executor']['stage_progression']['summary']['rollback_prepare_count'], 1)


    def test_rollout_executor_stage_advisory_promotes_live_when_signals_are_fresh_and_safe(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'allowed_action_types': ['joint_stage_prepare'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_stage_advisory_promote.db'))
            payload = {
                'validation_replay': {'summary': {
                    'generated_at': datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
                    'coverage_matrix': {'schema_version': 'm5_validation_coverage_matrix_v1', 'ready_for_low_intervention_gate': True, 'required_capability_count': 8, 'covered_required_count': 8, 'passing_required_count': 8, 'missing_required': [], 'failing_required': []},
                    'readiness': {'low_intervention_gate_ready': True, 'missing_required_capabilities': [], 'failing_required_capabilities': [], 'failing_case_count': 0},
                    'freshness_policy': {'enabled': True, 'max_age_minutes': 30},
                }},
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'ready', 'confidence': 'high',
                    'current_rollout_stage': 'guarded_prepare', 'target_rollout_stage': 'controlled_apply'
                }], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::stage', 'playbook_id': 'playbook::stage', 'title': 'Stage prepare item',
                    'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'ready',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': []
                }], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            item = result['rollout_executor']['items'][0]
            advisory = item['plan']['stage_handler']['advisory']
            self.assertEqual(advisory['recommended_stage'], 'controlled_apply')
            self.assertEqual(advisory['recommended_action'], 'move_to_review_pending')
            self.assertEqual(advisory['urgency'], 'high')
            self.assertTrue(advisory['ready_for_live_promotion'])
            self.assertIn('auto_advance_allowed', advisory['reasons'])
            self.assertEqual(result['rollout_executor']['stage_progression']['summary']['by_advisory_action']['move_to_review_pending'], 1)

    def test_rollout_executor_stage_advisory_freezes_when_validation_is_stale(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'allowed_action_types': ['joint_stage_prepare'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_stage_advisory_freeze.db'))
            payload = {
                'validation_replay': {'summary': {
                    'generated_at': '2020-01-01T00:00:00Z',
                    'coverage_matrix': {'schema_version': 'm5_validation_coverage_matrix_v1', 'ready_for_low_intervention_gate': True, 'required_capability_count': 8, 'covered_required_count': 8, 'passing_required_count': 8, 'missing_required': [], 'failing_required': []},
                    'readiness': {'low_intervention_gate_ready': True, 'missing_required_capabilities': [], 'failing_required_capabilities': [], 'failing_case_count': 0},
                    'freshness_policy': {'enabled': True, 'max_age_minutes': 30},
                }},
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'ready', 'confidence': 'high',
                    'current_rollout_stage': 'guarded_prepare', 'target_rollout_stage': 'controlled_apply'
                }], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::stage', 'playbook_id': 'playbook::stage', 'title': 'Stage prepare item',
                    'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'ready',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': []
                }], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            advisory = result['rollout_executor']['items'][0]['plan']['stage_handler']['advisory']
            self.assertEqual(advisory['recommended_action'], 'freeze_auto_advance')
            self.assertEqual(advisory['urgency'], 'high')
            self.assertFalse(advisory['ready_for_live_promotion'])
            self.assertIn('validation_stale', advisory['reasons'])
            self.assertEqual(result['rollout_executor']['stage_progression']['summary']['by_advisory_stage']['guarded_prepare'], 1)


    def test_rollout_executor_stage_prepare_alias_maps_to_canonical_stage_handler(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({'governance.rollout_executor': {'enabled': True, 'mode': 'controlled', 'allowed_action_types': ['joint_stage_prepare']}})
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_stage_alias.db'))
            payload = {
                'workflow_state': {'item_states': [
                    {'item_id': 'playbook::stage', 'title': 'Legacy prepared alias item', 'action_type': 'joint_stage_prepare', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'current_rollout_stage': 'observe', 'target_rollout_stage': 'prepared'}
                ], 'summary': {}},
                'approval_state': {'items': [
                    {'approval_id': 'approval::stage', 'playbook_id': 'playbook::stage', 'title': 'Legacy prepared alias item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'target_rollout_stage': 'prepared'}
                ], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            item = result['rollout_executor']['items'][0]
            self.assertEqual(item['plan']['stage_handler']['stage_key'], 'guarded_prepare')
            self.assertEqual(item['plan']['stage_handler']['requested_target_stage'], 'prepared')
            self.assertEqual(item['plan']['stage_handler']['target_stage'], 'guarded_prepare')
            self.assertEqual(item['result']['stage_loop']['loop_state'], 'auto_advance')
            self.assertEqual(item['result']['stage_loop']['recommended_action'], 'auto_advance')
            self.assertEqual(item['plan']['stage_handler']['next_transition'], 'promote_to_controlled_apply')
            self.assertEqual(item['plan']['stage_handler']['advisory']['recommended_action'], 'promote_to_controlled_apply')
            self.assertEqual(result['rollout_executor']['stage_progression']['items'][0]['stage_progression']['stage_handler']['stage_profile']['canonical_stage'], 'guarded_prepare')

    def test_rollout_executor_stage_loop_marks_review_schedule_as_review_pending(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'allowed_action_types': ['joint_review_schedule'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_stage_loop_review.db'))
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::review', 'title': 'Review item', 'action_type': 'joint_review_schedule',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high',
                    'current_rollout_stage': 'controlled_apply', 'target_rollout_stage': 'review_pending', 'review_after_hours': 2
                }], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::review', 'playbook_id': 'playbook::review', 'title': 'Review item',
                    'action_type': 'joint_review_schedule', 'approval_state': 'pending', 'decision_state': 'pending',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'review_after_hours': 2
                }], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            item = result['rollout_executor']['items'][0]
            self.assertEqual(item['plan']['stage_loop']['loop_state'], 'review_pending')
            self.assertEqual(item['result']['stage_loop']['recommended_action'], 'review_pending')
            self.assertEqual(result['rollout_executor']['stage_progression']['summary']['review_pending_count'], 1)

    def test_rollout_executor_marks_unknown_actions_as_unsupported_with_fallback_handler(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'allowed_action_types': ['joint_observe', 'joint_unknown_safe'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_executor_unsupported.db'))
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::unknown', 'title': 'Unknown action item', 'action_type': 'joint_unknown_safe',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high'}], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::unknown', 'playbook_id': 'playbook::unknown', 'title': 'Unknown action item',
                    'action_type': 'joint_unknown_safe', 'approval_state': 'pending', 'decision_state': 'pending',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            item = result['rollout_executor']['items'][0]
            self.assertEqual(item['status'], 'skipped')
            self.assertEqual(item['dispatch']['code'], 'ACTION_NOT_SUPPORTED')
            self.assertEqual(item['plan']['safe_handler']['handler_key'], 'unsupported::unsupported_action')
            self.assertEqual(item['audit']['handler_disposition'], 'unsupported')
            self.assertEqual(item['audit']['handler_route'], 'unsupported_hold')

    def test_rollout_executor_registry_exposes_transition_policy_metadata(self):
        registry = _build_safe_rollout_action_registry(['joint_stage_prepare', 'joint_review_schedule', 'joint_expand_guarded'])
        stage_entry = registry['actions']['joint_stage_prepare']
        review_entry = registry['actions']['joint_review_schedule']
        queue_entry = registry['actions']['joint_expand_guarded']
        self.assertEqual(stage_entry['transition_policy']['schema_version'], 'm5_rollout_transition_policy_v1')
        self.assertEqual(stage_entry['transition_policy']['dispatch_route'], 'stage_metadata_apply')
        self.assertEqual(review_entry['transition_policy']['default_target_stage'], 'review_pending')
        self.assertTrue(review_entry['transition_policy']['use_stage_handler_next_transition'])
        self.assertEqual(queue_entry['transition_policy']['dispatch_route'], 'stage_promotion_queue')
        self.assertEqual(queue_entry['transition_policy']['transition_rule'], 'queue_only_followup_required')

    def test_rollout_control_plane_manifest_exposes_versions_and_compatibility(self):
        manifest = build_rollout_control_plane_manifest()
        self.assertEqual(manifest['schema_version'], 'm5_rollout_control_plane_manifest_v1')
        self.assertEqual(manifest['versions']['action_registry'], 'm5_safe_rollout_action_registry_v1')
        self.assertEqual(manifest['versions']['stage_handler_registry'], 'm5_rollout_stage_handler_registry_v1')
        self.assertTrue(manifest['compatibility']['compatible'])
        self.assertIn('joint_stage_prepare', manifest['registries']['action_types'])
        self.assertIn('review_schedule_safe', manifest['registries']['stage_handlers'])

    def test_control_plane_readiness_summary_relates_manifest_to_validation_and_readiness(self):
        manifest = build_rollout_control_plane_manifest()
        summary = build_control_plane_readiness_summary(
            control_plane_manifest=manifest,
            validation_gate={
                'enabled': True,
                'ready': False,
                'freeze_auto_advance': True,
                'rollback_on_regression': False,
                'reasons': ['coverage_gap'],
                'missing_required_capabilities': ['transition_policy_contract'],
            },
            readiness={'status': 'WEAK_READY', 'readiness_pct': 56.0},
        )
        self.assertEqual(summary['schema_version'], 'm5_control_plane_readiness_summary_v1')
        self.assertTrue(summary['control_plane_compatible'])
        self.assertTrue(summary['replay_safe'])
        self.assertEqual(summary['relation'], 'validation_freeze_blocks_auto_promotion')
        self.assertFalse(summary['can_continue_auto_promotion'])
        self.assertEqual(summary['readiness_status'], 'WEAK_READY')
        self.assertIn('validation_gate_frozen', summary['blocking_issues'])
        self.assertEqual(summary['upgrade_window'], manifest['contracts']['upgrade_window'])
        self.assertEqual(summary['rollback_window'], manifest['contracts']['rollback_window'])


    def test_rollout_executor_persists_control_plane_contract_snapshot(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'allowed_action_types': ['joint_stage_prepare'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_contract_snapshot.db'))
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'ready', 'confidence': 'high',
                    'current_rollout_stage': 'observe', 'target_rollout_stage': 'guarded_prepare'
                }], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::stage', 'playbook_id': 'playbook::stage', 'title': 'Stage prepare item',
                    'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'ready',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': []
                }], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            row = db.get_approval_state('approval::stage')
            contract = row['details']['control_plane_contract']
            self.assertEqual(contract['schema_version'], 'm5_control_plane_contract_snapshot_v1')
            self.assertEqual(contract['action_type'], 'joint_stage_prepare')
            self.assertEqual(contract['handler_key'], 'apply::stage_prepare_safe')
            self.assertEqual(contract['versions']['action_registry'], result['rollout_executor']['control_plane_manifest']['versions']['action_registry'])
            self.assertEqual(contract['dispatch_route'], 'stage_metadata_apply')

    def test_control_plane_readiness_summary_blocks_on_persisted_contract_drift(self):
        manifest = build_rollout_control_plane_manifest()
        payload = {
            'workflow_state': {'item_states': [{
                'item_id': 'playbook::stale',
                'title': 'Stale contract item',
                'control_plane_contract': {
                    'schema_version': 'm5_control_plane_contract_snapshot_v1',
                    'generation': 'm5',
                    'action_type': 'joint_stage_prepare',
                    'handler_key': 'apply::stage_prepare_safe',
                    'versions': {
                        'action_registry': 'm5_safe_rollout_action_registry_v0',
                        'stage_handler_registry': 'm5_rollout_stage_handler_registry_v1',
                        'transition_policy': 'm5_rollout_transition_policy_v1',
                        'gate_policy': 'm5_rollout_gate_policy_v1',
                        'control_plane_manifest': 'm5_rollout_control_plane_manifest_v1',
                    },
                },
            }], 'summary': {}},
            'approval_state': {'items': [], 'summary': {}},
        }
        summary = build_control_plane_readiness_summary(payload=payload, control_plane_manifest=manifest)
        self.assertFalse(summary['control_plane_compatible'])
        self.assertFalse(summary['replay_safe'])
        self.assertEqual(summary['persisted_control_plane_contracts']['review_required_count'], 1)
        self.assertEqual(summary['persisted_control_plane_contracts']['version_drift_count'], 1)
        self.assertIn('persisted_control_plane_contract_review_required', summary['blocking_issues'])
        self.assertIn('persisted_control_plane_version_drift', summary['blocking_issues'])
        self.assertFalse(summary['can_continue_auto_promotion'])


    def test_build_workflow_operator_digest_exposes_control_plane_contract_drift_summary(self):
        payload = build_workflow_operator_digest({
            'workflow_state': {'item_states': [{
                'item_id': 'playbook::stale',
                'title': 'Stale contract item',
                'action_type': 'joint_stage_prepare',
                'workflow_state': 'queued',
                'approval_required': False,
                'requires_manual': False,
                'risk_level': 'medium',
                'control_plane_contract': {
                    'schema_version': 'm5_control_plane_contract_snapshot_v1',
                    'generation': 'm5',
                    'action_type': 'joint_stage_prepare',
                    'handler_key': 'apply::stage_prepare_safe',
                    'versions': {
                        'action_registry': 'm5_safe_rollout_action_registry_v0',
                        'stage_handler_registry': 'm5_rollout_stage_handler_registry_v1',
                        'transition_policy': 'm5_rollout_transition_policy_v1',
                        'gate_policy': 'm5_rollout_gate_policy_v1',
                        'control_plane_manifest': 'm5_rollout_control_plane_manifest_v1',
                    },
                },
            }], 'summary': {'item_count': 1}},
            'approval_state': {'items': [], 'summary': {}},
        }, max_items=3)
        drift = payload['control_plane_contract_drift']
        self.assertEqual(drift['schema_version'], 'm5_control_plane_contract_drift_summary_v1')
        self.assertEqual(drift['review_required_count'], 1)
        self.assertEqual(drift['frozen_item_count'], 1)
        self.assertGreaterEqual(drift['version_drift_count'], 1)
        self.assertEqual(drift['items'][0]['item_id'], 'playbook::stale')
        self.assertTrue(drift['items'][0]['frozen_by_contract_drift'])
        self.assertEqual(payload['summary']['control_plane_contract_drift']['review_required_count'], 1)
        self.assertEqual(payload['attention']['control_plane_contract_drift'][0]['item_id'], 'playbook::stale')

    def test_build_workbench_governance_view_surfaces_contract_drift_by_lane(self):
        payload = build_workbench_governance_view({
            'workflow_state': {'item_states': [{
                'item_id': 'playbook::blocked',
                'title': 'Blocked drift item',
                'action_type': 'joint_stage_prepare',
                'workflow_state': 'blocked',
                'approval_required': False,
                'requires_manual': False,
                'risk_level': 'high',
                'blocking_reasons': ['persisted_control_plane_contract_review_required'],
                'control_plane_contract': {
                    'schema_version': 'm5_control_plane_contract_snapshot_v1',
                    'generation': 'm5',
                    'action_type': 'joint_stage_prepare',
                    'handler_key': 'apply::stage_prepare_safe',
                    'versions': {
                        'action_registry': 'm5_safe_rollout_action_registry_v0',
                        'stage_handler_registry': 'm5_rollout_stage_handler_registry_v1',
                        'transition_policy': 'm5_rollout_transition_policy_v1',
                        'gate_policy': 'm5_rollout_gate_policy_v1',
                        'control_plane_manifest': 'm5_rollout_control_plane_manifest_v1',
                    },
                },
            }], 'summary': {'item_count': 1}},
            'approval_state': {'items': [], 'summary': {}},
        }, max_items=3)
        self.assertEqual(payload['control_plane_contract_drift']['review_required_count'], 1)
        self.assertEqual(payload['summary']['control_plane_contract_drift']['frozen_item_count'], 1)
        self.assertEqual(payload['lanes']['blocked']['control_plane_contract_drift']['review_required_count'], 1)
        self.assertIn(payload['lanes']['blocked']['control_plane_contract_drift']['items'][0]['dominant_drift_type'], {'registry_drift', 'version_drift'})

    def test_workflow_alert_and_unified_workbench_surface_contract_drift_consumption(self):
        base_payload = {
            'workflow_state': {'item_states': [{
                'item_id': 'playbook::drift',
                'title': 'Drifted item',
                'action_type': 'joint_stage_prepare',
                'workflow_state': 'queued',
                'approval_required': False,
                'requires_manual': False,
                'risk_level': 'medium',
                'control_plane_contract': {
                    'schema_version': 'm5_control_plane_contract_snapshot_v1',
                    'generation': 'm5',
                    'action_type': 'joint_stage_prepare',
                    'handler_key': 'queue_only::missing_handler',
                    'versions': {
                        'action_registry': 'm5_safe_rollout_action_registry_v1',
                        'stage_handler_registry': 'm5_rollout_stage_handler_registry_v1',
                        'transition_policy': 'm5_rollout_transition_policy_v1',
                        'gate_policy': 'm5_rollout_gate_policy_v1',
                        'control_plane_manifest': 'm5_rollout_control_plane_manifest_v1',
                    },
                },
            }], 'summary': {'item_count': 1}},
            'approval_state': {'items': [], 'summary': {}},
        }
        alert_digest = build_workflow_alert_digest(dict(base_payload), max_items=5)
        self.assertTrue(any(row['kind'] == 'control_plane_contract_drift' and row['item_id'] == 'playbook::drift' for row in alert_digest['alerts']))
        self.assertEqual(alert_digest['summary']['control_plane_contract_drift']['dominant_drift_type'], 'registry_drift')

        overview = build_unified_workbench_overview(dict(base_payload), max_items=5)
        self.assertEqual(overview['control_plane_contract_drift']['review_required_count'], 1)
        self.assertEqual(overview['summary']['control_plane_contract_drift']['dominant_drift_type'], 'registry_drift')
        self.assertEqual(overview['lines']['rollout']['counts']['contract_drift_frozen'], 1)
        self.assertEqual(overview['lines']['rollout']['key_alerts'][0]['item_id'], 'playbook::drift')

    def test_rollout_executor_plan_and_stage_progression_expose_transition_policy(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.rollout_executor': {
                'enabled': True,
                'mode': 'controlled',
                'allowed_action_types': ['joint_review_schedule'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'rollout_transition_policy.db'))
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::review', 'title': 'Review item', 'action_type': 'joint_review_schedule',
                    'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                    'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high',
                    'current_rollout_stage': 'controlled_apply', 'target_rollout_stage': 'review_pending', 'review_after_hours': 2
                }], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::review', 'playbook_id': 'playbook::review', 'title': 'Review item',
                    'action_type': 'joint_review_schedule', 'approval_state': 'pending', 'decision_state': 'pending',
                    'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'review_after_hours': 2
                }], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_rollout_executor(payload, db, config=config, replay_source='unit-test-replay')
            item = result['rollout_executor']['items'][0]
            self.assertEqual(item['plan']['transition_policy']['schema_version'], 'm5_rollout_transition_policy_v1')
            self.assertEqual(item['plan']['transition_policy']['transition_rule'], 'schedule_review_checkpoint')
            self.assertEqual(item['plan']['transition_policy']['dispatch_route'], 'review_metadata_apply')
            self.assertEqual(item['audit']['transition_policy']['default_target_stage'], 'review_pending')
            stage_progression = result['rollout_executor']['stage_progression']['items'][0]['stage_progression']
            self.assertEqual(stage_progression['transition_policy']['transition_rule'], 'schedule_review_checkpoint')
            self.assertEqual(stage_progression['transition_policy']['schema_version'], 'm5_rollout_transition_policy_v1')

    def test_controlled_rollout_execution_defaults_to_disabled(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'controlled_rollout_disabled.db'))
            payload = {
                'workflow_state': {
                    'item_states': [{
                        'item_id': 'playbook::observe',
                        'title': 'Safe observe item',
                        'action_type': 'joint_observe',
                        'decision': 'expand',
                        'governance_mode': 'rollout',
                        'risk_level': 'low',
                        'approval_required': False,
                        'blocking_reasons': [],
                        'preconditions': [],
                        'workflow_state': 'pending',
                        'confidence': 'high',
                    }],
                    'summary': {},
                },
                'approval_state': {
                    'items': [{
                        'approval_id': 'approval::observe',
                        'playbook_id': 'playbook::observe',
                        'title': 'Safe observe item',
                        'action_type': 'joint_observe',
                        'approval_state': 'pending',
                        'decision_state': 'pending',
                        'risk_level': 'low',
                        'approval_required': False,
                        'blocked_by': [],
                    }],
                    'summary': {},
                },
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_controlled_rollout_layer(payload, db, config=StubConfig())
            state_row = db.get_approval_state('approval::observe')
            self.assertEqual(state_row['state'], 'pending')
            self.assertEqual(state_row['decision'], 'pending')
            self.assertEqual(state_row['workflow_state'], 'pending')
            self.assertEqual(result['controlled_rollout_execution']['executed_count'], 0)
            self.assertEqual(result['controlled_rollout_execution']['skipped_count'], 1)

    def test_controlled_rollout_execution_applies_safe_ready_state_without_auto_approval(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.controlled_rollout_execution': {
                'enabled': True,
                'mode': 'state_apply',
                'auto_promote_ready_candidates': True,
                'actor': 'system:test-controlled-rollout',
                'source': 'unit_test_controlled_rollout',
                'reason_prefix': 'unit-test controlled rollout state apply',
                'allowed_action_types': ['joint_observe'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'controlled_rollout_enabled.db'))
            payload = {
                'workflow_state': {
                    'item_states': [
                        {
                            'item_id': 'playbook::observe',
                            'title': 'Safe observe item',
                            'action_type': 'joint_observe',
                            'decision': 'expand',
                            'governance_mode': 'rollout',
                            'risk_level': 'low',
                            'approval_required': False,
                            'blocking_reasons': [],
                            'preconditions': [],
                            'workflow_state': 'pending',
                            'confidence': 'high',
                        },
                        {
                            'item_id': 'playbook::expand',
                            'title': 'Guarded expand item',
                            'action_type': 'joint_expand_guarded',
                            'decision': 'expand',
                            'governance_mode': 'rollout',
                            'risk_level': 'low',
                            'approval_required': False,
                            'blocking_reasons': [],
                            'preconditions': [],
                            'workflow_state': 'pending',
                            'confidence': 'high',
                        },
                    ],
                    'summary': {},
                },
                'approval_state': {
                    'items': [
                        {'approval_id': 'approval::observe', 'playbook_id': 'playbook::observe', 'title': 'Safe observe item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []},
                        {'approval_id': 'approval::expand', 'playbook_id': 'playbook::expand', 'title': 'Guarded expand item', 'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []},
                    ],
                    'summary': {},
                },
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_controlled_rollout_layer(payload, db, config=config, replay_source='unit-test-replay')
            observe = db.get_approval_state('approval::observe')
            expand = db.get_approval_state('approval::expand')
            self.assertEqual(observe['state'], 'ready')
            self.assertEqual(observe['decision'], 'pending')
            self.assertEqual(observe['workflow_state'], 'ready')
            self.assertEqual(observe['actor'], 'system:test-controlled-rollout')
            self.assertEqual(observe['replay_source'], 'unit-test-replay')
            self.assertEqual(observe['details']['source'], 'unit_test_controlled_rollout')
            self.assertEqual(observe['details']['execution_layer'], 'controlled_rollout_state_apply')
            self.assertFalse(observe['details']['real_trade_execution'])
            timeline = db.get_approval_timeline(item_id='approval::observe', ascending=True)
            self.assertEqual(timeline[-1]['event_type'], 'controlled_rollout_state_apply')
            self.assertEqual(timeline[-1]['state'], 'ready')
            self.assertEqual(timeline[-1]['decision'], 'pending')
            self.assertEqual(timeline[-1]['workflow_state'], 'ready')
            self.assertEqual(timeline[-1]['actor'], 'system:test-controlled-rollout')
            self.assertEqual(timeline[-1]['source'], 'unit-test-replay')
            self.assertEqual(expand['state'], 'pending')
            self.assertEqual(expand['workflow_state'], 'pending')
            self.assertEqual(result['controlled_rollout_execution']['executed_count'], 1)
            self.assertGreaterEqual(result['controlled_rollout_execution']['skipped_count'], 1)

    def test_controlled_rollout_execution_supports_safe_extended_action_types(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.controlled_rollout_execution': {
                'enabled': True,
                'mode': 'state_apply',
                'auto_promote_ready_candidates': True,
                'actor': 'system:test-controlled-rollout',
                'source': 'unit_test_controlled_rollout',
                'reason_prefix': 'unit-test controlled rollout state apply',
                'default_review_after_hours': 6,
                'allowed_action_types': [
                    'joint_observe',
                    'joint_queue_promote_safe',
                    'joint_stage_prepare',
                    'joint_review_schedule',
                    'joint_metadata_annotate',
                ],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'controlled_rollout_extended.db'))
            payload = {
                'workflow_state': {
                    'item_states': [
                        {'item_id': 'playbook::queue', 'title': 'Queue promotion item', 'action_type': 'joint_queue_promote_safe', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'bucket_id': 'bucket::priority'},
                        {'item_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'current_rollout_stage': 'observe'},
                        {'item_id': 'playbook::review', 'title': 'Review schedule item', 'action_type': 'joint_review_schedule', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'review_after_hours': 4},
                        {'item_id': 'playbook::annotate', 'title': 'Metadata annotation item', 'action_type': 'joint_metadata_annotate', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'annotations': {'operator_note': 'safe for staged rollout'}, 'annotation_tags': ['safe', 'audit']},
                    ],
                    'summary': {},
                },
                'approval_state': {
                    'items': [
                        {'approval_id': 'approval::queue', 'playbook_id': 'playbook::queue', 'title': 'Queue promotion item', 'action_type': 'joint_queue_promote_safe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'bucket_id': 'bucket::priority'},
                        {'approval_id': 'approval::stage', 'playbook_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []},
                        {'approval_id': 'approval::review', 'playbook_id': 'playbook::review', 'title': 'Review schedule item', 'action_type': 'joint_review_schedule', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'review_after_hours': 4},
                        {'approval_id': 'approval::annotate', 'playbook_id': 'playbook::annotate', 'title': 'Metadata annotation item', 'action_type': 'joint_metadata_annotate', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'annotations': {'operator_note': 'safe for staged rollout'}, 'annotation_tags': ['safe', 'audit']},
                    ],
                    'summary': {},
                },
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_controlled_rollout_layer(payload, db, config=config, replay_source='unit-test-replay')

            queue_row = db.get_approval_state('approval::queue')
            self.assertEqual(queue_row['state'], 'ready')
            self.assertEqual(queue_row['workflow_state'], 'ready')
            self.assertEqual(queue_row['details']['queue_action'], 'promote_safe')
            self.assertEqual(queue_row['details']['queue_name'], 'bucket::priority')
            self.assertFalse(queue_row['details']['real_trade_execution'])
            self.assertFalse(queue_row['details']['dangerous_live_parameter_change'])
            queue_timeline = db.get_approval_timeline(item_id='approval::queue', ascending=True)
            self.assertEqual(queue_timeline[-1]['event_type'], 'controlled_rollout_queue_promote')

            stage_row = db.get_approval_state('approval::stage')
            self.assertEqual(stage_row['state'], 'ready')
            self.assertEqual(stage_row['workflow_state'], 'ready')
            self.assertEqual(stage_row['details']['rollout_stage'], 'prepared')
            self.assertEqual(stage_row['details']['stage_transition']['from'], 'observe')
            self.assertEqual(stage_row['details']['stage_transition']['to'], 'prepared')
            self.assertIn('stage_model', stage_row['details'])
            self.assertEqual(stage_row['details']['canonical_rollout_stage'], 'guarded_prepare')
            self.assertEqual(stage_row['details']['canonical_target_rollout_stage'], 'guarded_prepare')
            self.assertEqual(stage_row['details']['stage_transition']['canonical_to'], 'guarded_prepare')
            self.assertTrue(stage_row['details']['stage_transition']['alias_applied'])
            self.assertEqual(stage_row['details']['stage_handler']['stage_key'], 'guarded_prepare')
            self.assertEqual(stage_row['details']['stage_handler']['stage_profile']['canonical_stage'], 'guarded_prepare')

            review_row = db.get_approval_state('approval::review')
            self.assertEqual(review_row['state'], 'pending')
            self.assertEqual(review_row['workflow_state'], 'pending')
            self.assertEqual(review_row['details']['review_status'], 'scheduled')
            self.assertEqual(review_row['details']['review_after_hours'], 4)
            self.assertIn('T', review_row['details']['review_due_at'])
            self.assertIn('scheduled_review', review_row['details'])
            review_timeline = db.get_approval_timeline(item_id='approval::review', ascending=True)
            self.assertEqual(review_timeline[-1]['event_type'], 'controlled_rollout_review_schedule')

            annotate_row = db.get_approval_state('approval::annotate')
            self.assertEqual(annotate_row['state'], 'pending')
            self.assertEqual(annotate_row['workflow_state'], 'pending')
            self.assertEqual(annotate_row['details']['annotations']['operator_note'], 'safe for staged rollout')
            self.assertEqual(annotate_row['details']['annotation_tags'], ['safe', 'audit'])
            annotate_timeline = db.get_approval_timeline(item_id='approval::annotate', ascending=True)
            self.assertEqual(annotate_timeline[-1]['event_type'], 'controlled_rollout_metadata_annotate')

            self.assertEqual(result['controlled_rollout_execution']['executed_count'], 4)
            self.assertEqual(result['controlled_rollout_execution']['skipped_count'], 0)

    def test_controlled_rollout_execution_honors_pass_budget_and_surfaces_budget_summary(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.controlled_rollout_execution': {
                'enabled': True,
                'mode': 'state_apply',
                'auto_promote_ready_candidates': True,
                'allowed_action_types': ['joint_stage_prepare'],
                'max_executed_per_pass': 1,
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'controlled_rollout_budget.db'))
            payload = {
                'workflow_state': {
                    'item_states': [
                        {'item_id': 'playbook::stage-1', 'title': 'Stage prepare item 1', 'action_type': 'joint_stage_prepare', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'current_rollout_stage': 'observe', 'target_rollout_stage': 'controlled_apply'},
                        {'item_id': 'playbook::stage-2', 'title': 'Stage prepare item 2', 'action_type': 'joint_stage_prepare', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'current_rollout_stage': 'observe', 'target_rollout_stage': 'controlled_apply'},
                    ],
                    'summary': {},
                },
                'approval_state': {
                    'items': [
                        {'approval_id': 'approval::stage-1', 'playbook_id': 'playbook::stage-1', 'title': 'Stage prepare item 1', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'current_rollout_stage': 'observe', 'target_rollout_stage': 'controlled_apply'},
                        {'approval_id': 'approval::stage-2', 'playbook_id': 'playbook::stage-2', 'title': 'Stage prepare item 2', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'current_rollout_stage': 'observe', 'target_rollout_stage': 'controlled_apply'},
                    ],
                    'summary': {},
                },
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_controlled_rollout_layer(payload, db, config=config, replay_source='unit-test-replay')

            first = db.get_approval_state('approval::stage-1')
            second = db.get_approval_state('approval::stage-2')
            self.assertEqual(first['state'], 'ready')
            self.assertEqual(first['workflow_state'], 'ready')
            self.assertEqual(second['state'], 'pending')
            self.assertEqual(second['workflow_state'], 'pending')
            self.assertEqual(result['controlled_rollout_execution']['executed_count'], 1)
            self.assertEqual(result['controlled_rollout_execution']['skipped_count'], 1)
            budget = result['controlled_rollout_execution']['budget']
            self.assertEqual(budget['max_executed_per_pass'], 1)
            self.assertEqual(budget['remaining_slots'], 0)
            self.assertTrue(budget['exhausted'])
            self.assertEqual(budget['skipped_by_budget'], 1)
            self.assertEqual(budget['last_executed_item_id'], 'approval::stage-1')
            reasons = {row['item_id']: row['reason'] for row in result['controlled_rollout_execution']['items'] if row['action'] == 'skipped'}
            self.assertEqual(reasons['approval::stage-2'], 'pass_budget_exhausted')

            execution_summary = build_auto_promotion_execution_summary(result, max_items=5)
            self.assertEqual(execution_summary['summary']['budget']['max_executed_per_pass'], 1)
            self.assertTrue(execution_summary['summary']['budget']['exhausted'])
            self.assertEqual(execution_summary['summary']['budget']['skipped_by_budget'], 1)

            runtime_summary = build_runtime_orchestration_summary({
                **result,
                'adaptive_rollout_orchestration': {
                    'schema_version': 'm5_adaptive_rollout_orchestration_v2',
                    'passes': [{'label': 'pre_auto_approval', 'dry_run': True, 'rollout_executor_applied_count': 0}],
                    'summary': {
                        'pass_count': 1,
                        'rerun_triggered': False,
                        'rerun_reason': None,
                        'rollout_executor_applied_count': 0,
                        'controlled_rollout_executed_count': 1,
                        'auto_approval_executed_count': 0,
                        'review_queue_queued_count': 0,
                        'review_queue_completed_count': 0,
                        'review_queue_rollback_escalated_count': 0,
                    },
                },
            }, max_items=5)
            self.assertEqual(runtime_summary['summary']['controlled_rollout_budget']['max_executed_per_pass'], 1)
            self.assertTrue(runtime_summary['summary']['controlled_rollout_budget']['exhausted'])
            self.assertEqual(runtime_summary['summary']['controlled_rollout_budget']['skipped_by_budget'], 1)

    def test_controlled_rollout_execution_respects_extended_action_allowlist_and_manual_gates(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.controlled_rollout_execution': {
                'enabled': True,
                'mode': 'state_apply',
                'auto_promote_ready_candidates': True,
                'allowed_action_types': ['joint_queue_promote_safe'],
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'controlled_rollout_gatekeeping.db'))
            payload = {
                'workflow_state': {
                    'item_states': [
                        {'item_id': 'playbook::queue-ok', 'title': 'Queue ok', 'action_type': 'joint_queue_promote_safe', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high'},
                        {'item_id': 'playbook::review', 'title': 'Review not allowlisted', 'action_type': 'joint_review_schedule', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high'},
                        {'item_id': 'playbook::manual', 'title': 'Queue manual gate', 'action_type': 'joint_queue_promote_safe', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': True, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high'},
                    ],
                    'summary': {},
                },
                'approval_state': {
                    'items': [
                        {'approval_id': 'approval::queue-ok', 'playbook_id': 'playbook::queue-ok', 'title': 'Queue ok', 'action_type': 'joint_queue_promote_safe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []},
                        {'approval_id': 'approval::review', 'playbook_id': 'playbook::review', 'title': 'Review not allowlisted', 'action_type': 'joint_review_schedule', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []},
                        {'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Queue manual gate', 'action_type': 'joint_queue_promote_safe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': True, 'blocked_by': []},
                    ],
                    'summary': {},
                },
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_controlled_rollout_layer(payload, db, config=config, replay_source='unit-test-replay')

            self.assertEqual(db.get_approval_state('approval::queue-ok')['state'], 'ready')
            self.assertEqual(db.get_approval_state('approval::review')['state'], 'pending')
            self.assertEqual(db.get_approval_state('approval::manual')['state'], 'pending')
            reasons = {row['item_id']: row['reason'] for row in result['controlled_rollout_execution']['items'] if row['action'] == 'skipped'}
            self.assertEqual(reasons['approval::review'], 'action_type_not_allowlisted:joint_review_schedule')
            self.assertEqual(reasons['approval::manual'], 'approval_required')
            self.assertEqual(result['controlled_rollout_execution']['executed_count'], 1)
            self.assertEqual(result['controlled_rollout_execution']['skipped_count'], 2)

    def test_controlled_rollout_execution_preserves_terminal_state(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.controlled_rollout_execution': {
                'enabled': True,
                'mode': 'state_apply',
                'auto_promote_ready_candidates': True,
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'controlled_rollout_terminal.db'))
            payload = {
                'workflow_state': {'item_states': [{'item_id': 'playbook::observe', 'title': 'Safe observe item', 'action_type': 'joint_observe', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high'}], 'summary': {}},
                'approval_state': {'items': [{'approval_id': 'approval::observe', 'playbook_id': 'playbook::observe', 'title': 'Safe observe item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            db.record_approval('joint_observe', 'playbook::observe', 'approved', {'item_id': 'approval::observe', 'state': 'approved', 'workflow_state': 'approved', 'reason': 'manual final approval', 'actor': 'tester', 'replay_source': 'manual-test'})
            result = execute_controlled_rollout_layer(payload, db, config=config, replay_source='unit-test-replay')
            observe = db.get_approval_state('approval::observe')
            self.assertEqual(observe['state'], 'approved')
            self.assertEqual(observe['decision'], 'approved')
            self.assertEqual(observe['workflow_state'], 'approved')
            self.assertEqual(result['controlled_rollout_execution']['executed_count'], 0)
            self.assertGreaterEqual(result['controlled_rollout_execution']['skipped_count'], 1)

    def test_controlled_auto_approval_execution_defaults_to_disabled(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'auto_approval_disabled.db'))
            payload = {
                'workflow_state': {
                    'item_states': [{
                        'item_id': 'playbook::low-risk',
                        'title': 'Low risk observe item',
                        'action_type': 'joint_observe',
                        'decision': 'expand',
                        'governance_mode': 'rollout',
                        'risk_level': 'low',
                        'approval_required': False,
                        'blocking_reasons': [],
                        'preconditions': [],
                        'workflow_state': 'pending',
                        'confidence': 'medium',
                    }],
                    'summary': {},
                },
                'approval_state': {
                    'items': [{
                        'approval_id': 'approval::low-risk',
                        'playbook_id': 'playbook::low-risk',
                        'title': 'Low risk observe item',
                        'action_type': 'joint_observe',
                        'approval_state': 'pending',
                        'decision_state': 'pending',
                        'risk_level': 'low',
                        'approval_required': False,
                        'blocked_by': [],
                    }],
                    'summary': {},
                },
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_controlled_auto_approval_layer(payload, db, config=StubConfig())
            state_row = db.get_approval_state('approval::low-risk')
            self.assertEqual(state_row['state'], 'pending')
            self.assertEqual(result['auto_approval_execution']['executed_count'], 0)
            self.assertEqual(result['auto_approval_execution']['skipped_count'], 1)

    def test_controlled_auto_approval_execution_approves_only_eligible_items(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.auto_approval_execution': {
                'enabled': True,
                'mode': 'controlled',
                'actor': 'system:test-auto-approval',
                'source': 'unit_test_auto_approval',
                'reason_prefix': 'unit-test controlled auto approval',
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'auto_approval_enabled.db'))
            payload = {
                'workflow_state': {
                    'item_states': [
                        {
                            'item_id': 'playbook::eligible',
                            'title': 'Eligible low risk item',
                            'action_type': 'joint_observe',
                            'decision': 'expand',
                            'governance_mode': 'rollout',
                            'risk_level': 'low',
                            'approval_required': False,
                            'blocking_reasons': [],
                            'preconditions': [],
                            'workflow_state': 'pending',
                            'confidence': 'high',
                        },
                        {
                            'item_id': 'playbook::manual',
                            'title': 'Manual review item',
                            'action_type': 'joint_expand_guarded',
                            'decision': 'expand',
                            'governance_mode': 'rollout',
                            'risk_level': 'medium',
                            'approval_required': True,
                            'blocking_reasons': [],
                            'preconditions': [],
                            'workflow_state': 'pending',
                            'confidence': 'high',
                        },
                        {
                            'item_id': 'playbook::freeze',
                            'title': 'Freeze item',
                            'action_type': 'joint_freeze',
                            'decision': 'freeze',
                            'governance_mode': 'rollback',
                            'risk_level': 'critical',
                            'approval_required': True,
                            'blocking_reasons': [],
                            'preconditions': [],
                            'workflow_state': 'pending',
                            'confidence': 'high',
                        },
                        {
                            'item_id': 'playbook::defer',
                            'title': 'Deferred item',
                            'action_type': 'joint_observe',
                            'decision': 'observe',
                            'governance_mode': 'review',
                            'risk_level': 'low',
                            'approval_required': False,
                            'blocking_reasons': ['blocking_issue'],
                            'preconditions': [{'type': 'wait', 'value': 'more_samples', 'status': 'open'}],
                            'workflow_state': 'pending',
                            'confidence': 'medium',
                        },
                    ],
                    'summary': {},
                },
                'approval_state': {
                    'items': [
                        {'approval_id': 'approval::eligible', 'playbook_id': 'playbook::eligible', 'title': 'Eligible low risk item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []},
                        {'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual review item', 'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'medium', 'approval_required': True, 'blocked_by': []},
                        {'approval_id': 'approval::freeze', 'playbook_id': 'playbook::freeze', 'title': 'Freeze item', 'action_type': 'joint_freeze', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'critical', 'approval_required': True, 'blocked_by': []},
                        {'approval_id': 'approval::defer', 'playbook_id': 'playbook::defer', 'title': 'Deferred item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': ['blocking_issue']},
                    ],
                    'summary': {},
                },
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            db.record_approval('joint_observe', 'playbook::defer', 'deferred', {
                'item_id': 'approval::defer',
                'state': 'deferred',
                'workflow_state': 'deferred',
                'reason': 'already deferred',
                'actor': 'tester',
                'replay_source': 'unit-test-manual',
            })
            result = execute_controlled_auto_approval_layer(payload, db, config=config, replay_source='unit-test-replay')
            eligible = db.get_approval_state('approval::eligible')
            manual = db.get_approval_state('approval::manual')
            freeze = db.get_approval_state('approval::freeze')
            deferred = db.get_approval_state('approval::defer')
            self.assertEqual(eligible['state'], 'approved')
            self.assertEqual(eligible['decision'], 'approved')
            self.assertEqual(eligible['workflow_state'], 'ready')
            self.assertEqual(eligible['actor'], 'system:test-auto-approval')
            self.assertEqual(eligible['replay_source'], 'unit-test-replay')
            self.assertEqual(eligible['details']['source'], 'unit_test_auto_approval')
            self.assertEqual(eligible['details']['execution_layer'], 'controlled_auto_approval')
            timeline = db.get_approval_timeline(item_id='approval::eligible', ascending=True)
            self.assertEqual(timeline[-1]['event_type'], 'decision_recorded')
            self.assertEqual(timeline[-1]['decision'], 'approved')
            self.assertEqual(timeline[-1]['state'], 'approved')
            self.assertEqual(timeline[-1]['workflow_state'], 'ready')
            self.assertEqual(timeline[-1]['actor'], 'system:test-auto-approval')
            self.assertEqual(timeline[-1]['source'], 'unit-test-replay')
            self.assertEqual(timeline[-1]['details']['source'], 'unit_test_auto_approval')
            self.assertEqual(manual['state'], 'pending')
            self.assertEqual(freeze['state'], 'pending')
            self.assertEqual(deferred['state'], 'deferred')
            self.assertEqual(result['auto_approval_execution']['executed_count'], 1)
            self.assertGreaterEqual(result['auto_approval_execution']['skipped_count'], 3)

    def test_controlled_auto_approval_execution_honors_pass_budget_and_surfaces_budget_summary(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.auto_approval_execution': {
                'enabled': True,
                'mode': 'controlled',
                'actor': 'system:test-auto-approval-budget',
                'source': 'unit_test_auto_approval_budget',
                'reason_prefix': 'unit-test controlled auto approval budget',
                'max_executed_per_pass': 1,
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'auto_approval_budget.db'))
            payload = {
                'workflow_state': {
                    'item_states': [
                        {
                            'item_id': 'playbook::eligible-1', 'title': 'Eligible low risk item 1', 'action_type': 'joint_observe',
                            'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                            'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high',
                        },
                        {
                            'item_id': 'playbook::eligible-2', 'title': 'Eligible low risk item 2', 'action_type': 'joint_observe',
                            'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False,
                            'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high',
                        },
                    ],
                    'summary': {},
                },
                'approval_state': {
                    'items': [
                        {'approval_id': 'approval::eligible-1', 'playbook_id': 'playbook::eligible-1', 'title': 'Eligible low risk item 1', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []},
                        {'approval_id': 'approval::eligible-2', 'playbook_id': 'playbook::eligible-2', 'title': 'Eligible low risk item 2', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []},
                    ],
                    'summary': {},
                },
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_controlled_auto_approval_layer(payload, db, config=config, replay_source='unit-test-replay')
            self.assertEqual(result['auto_approval_execution']['executed_count'], 1)
            self.assertEqual(result['auto_approval_execution']['skipped_count'], 1)
            budget = result['auto_approval_execution']['budget']
            self.assertEqual(budget['max_executed_per_pass'], 1)
            self.assertEqual(budget['remaining_slots'], 0)
            self.assertTrue(budget['exhausted'])
            self.assertEqual(budget['skipped_by_budget'], 1)
            self.assertEqual(budget['last_executed_item_id'], 'approval::eligible-1')
            reasons = {row['item_id']: row['reason'] for row in result['auto_approval_execution']['items'] if row['action'] == 'skipped'}
            self.assertEqual(reasons['approval::eligible-2'], 'pass_budget_exhausted')
            self.assertEqual(db.get_approval_state('approval::eligible-1')['state'], 'approved')
            self.assertEqual(db.get_approval_state('approval::eligible-2')['state'], 'pending')

            runtime_summary = build_runtime_orchestration_summary({
                **result,
                'adaptive_rollout_orchestration': {
                    'schema_version': 'm5_adaptive_rollout_orchestration_v2',
                    'passes': [{'label': 'pre_auto_approval', 'dry_run': True, 'rollout_executor_applied_count': 0}],
                    'summary': {
                        'pass_count': 1,
                        'rerun_triggered': False,
                        'rerun_reason': None,
                        'rollout_executor_applied_count': 0,
                        'controlled_rollout_executed_count': 0,
                        'auto_approval_executed_count': 1,
                        'review_queue_queued_count': 0,
                        'review_queue_completed_count': 0,
                        'review_queue_rollback_escalated_count': 0,
                    },
                },
            }, max_items=5)
            self.assertEqual(runtime_summary['summary']['auto_approval_budget']['max_executed_per_pass'], 1)
            self.assertTrue(runtime_summary['summary']['auto_approval_budget']['exhausted'])
            self.assertEqual(runtime_summary['summary']['auto_approval_budget']['skipped_by_budget'], 1)

    def test_controlled_rollout_execution_prioritizes_oldest_pending_candidate_under_budget(self):
        import sqlite3

        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.controlled_rollout_execution': {
                'enabled': True,
                'mode': 'state_apply',
                'auto_promote_ready_candidates': True,
                'allowed_action_types': ['joint_stage_prepare'],
                'max_executed_per_pass': 1,
                'selection_policy': 'oldest_pending_first',
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / 'controlled_rollout_oldest_first.db'
            db = Database(str(db_path))
            payload = {
                'workflow_state': {'item_states': [
                    {'item_id': 'playbook::stage-newer', 'title': 'Stage newer', 'action_type': 'joint_stage_prepare', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'current_rollout_stage': 'observe', 'target_rollout_stage': 'controlled_apply'},
                    {'item_id': 'playbook::stage-older', 'title': 'Stage older', 'action_type': 'joint_stage_prepare', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high', 'current_rollout_stage': 'observe', 'target_rollout_stage': 'controlled_apply'},
                ], 'summary': {}},
                'approval_state': {'items': [
                    {'approval_id': 'approval::stage-newer', 'playbook_id': 'playbook::stage-newer', 'title': 'Stage newer', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'current_rollout_stage': 'observe', 'target_rollout_stage': 'controlled_apply'},
                    {'approval_id': 'approval::stage-older', 'playbook_id': 'playbook::stage-older', 'title': 'Stage older', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'current_rollout_stage': 'observe', 'target_rollout_stage': 'controlled_apply'},
                ], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE approval_state SET updated_at = ?, last_seen_at = ? WHERE item_id = ?", ('2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z', 'approval::stage-older'))
                conn.execute("UPDATE approval_state SET updated_at = ?, last_seen_at = ? WHERE item_id = ?", ('2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'approval::stage-newer'))
                conn.commit()

            result = execute_controlled_rollout_layer(payload, db, config=config, replay_source='unit-test-replay')
            self.assertEqual(result['controlled_rollout_execution']['budget']['last_executed_item_id'], 'approval::stage-older')
            self.assertEqual(result['controlled_rollout_execution']['selection_policy']['policy'], 'oldest_pending_first')
            self.assertEqual(result['controlled_rollout_execution']['selection_policy']['ordered_item_ids'][0], 'approval::stage-older')
            self.assertEqual(db.get_approval_state('approval::stage-older')['state'], 'ready')
            self.assertEqual(db.get_approval_state('approval::stage-newer')['state'], 'pending')

    def test_controlled_auto_approval_execution_prioritizes_oldest_pending_item_under_budget(self):
        import sqlite3

        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}

            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({
            'governance.auto_approval_execution': {
                'enabled': True,
                'mode': 'controlled',
                'max_executed_per_pass': 1,
                'selection_policy': 'oldest_pending_first',
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / 'auto_approval_oldest_first.db'
            db = Database(str(db_path))
            payload = {
                'workflow_state': {'item_states': [
                    {'item_id': 'playbook::eligible-newer', 'title': 'Eligible newer', 'action_type': 'joint_observe', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high'},
                    {'item_id': 'playbook::eligible-older', 'title': 'Eligible older', 'action_type': 'joint_observe', 'decision': 'expand', 'governance_mode': 'rollout', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'preconditions': [], 'workflow_state': 'pending', 'confidence': 'high'},
                ], 'summary': {}},
                'approval_state': {'items': [
                    {'approval_id': 'approval::eligible-newer', 'playbook_id': 'playbook::eligible-newer', 'title': 'Eligible newer', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'auto_approval_decision': 'auto_approve', 'auto_approval_eligible': True},
                    {'approval_id': 'approval::eligible-older', 'playbook_id': 'playbook::eligible-older', 'title': 'Eligible older', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'auto_approval_decision': 'auto_approve', 'auto_approval_eligible': True},
                ], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE approval_state SET updated_at = ?, last_seen_at = ? WHERE item_id = ?", ('2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z', 'approval::eligible-older'))
                conn.execute("UPDATE approval_state SET updated_at = ?, last_seen_at = ? WHERE item_id = ?", ('2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'approval::eligible-newer'))
                conn.commit()

            result = execute_controlled_auto_approval_layer(payload, db, config=config, replay_source='unit-test-replay')
            self.assertEqual(result['auto_approval_execution']['budget']['last_executed_item_id'], 'approval::eligible-older')
            self.assertEqual(result['auto_approval_execution']['selection_policy']['policy'], 'oldest_pending_first')
            self.assertEqual(result['auto_approval_execution']['selection_policy']['ordered_item_ids'][0], 'approval::eligible-older')
            self.assertEqual(db.get_approval_state('approval::eligible-older')['state'], 'approved')
            self.assertEqual(db.get_approval_state('approval::eligible-newer')['state'], 'pending')

    def test_adaptive_rollout_orchestration_closes_auto_approval_to_rollout_loop_in_same_cycle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'adaptive_rollout_orchestration.db'))
            payload = {
                'workflow_state': {'item_states': [{
                    'item_id': 'playbook::observe', 'title': 'Observe candidate', 'workflow_state': 'ready',
                    'action_type': 'joint_observe', 'risk_level': 'low', 'approval_required': False,
                    'blocked_by': [], 'lane_id': 'ready', 'current_rollout_stage': 'observe', 'target_rollout_stage': 'observe',
                }], 'summary': {}},
                'approval_state': {'items': [{
                    'approval_id': 'approval::observe', 'playbook_id': 'playbook::observe', 'title': 'Observe candidate',
                    'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending',
                    'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': [],
                    'auto_approval_decision': 'auto_approve', 'auto_approval_eligible': True,
                    'current_rollout_stage': 'observe', 'target_rollout_stage': 'observe',
                }], 'summary': {}},
            }

            class StubConfig:
                def get(self, key, default=None):
                    mapping = {
                        'governance.controlled_rollout_execution': {
                            'enabled': True, 'mode': 'state_apply', 'auto_promote_ready_candidates': True,
                            'allowed_action_types': ['joint_observe'], 'actor': 'system:controlled-rollout',
                            'source': 'controlled_rollout_execution',
                        },
                        'governance.auto_approval_execution': {
                            'enabled': True, 'mode': 'controlled', 'actor': 'system:auto-approval',
                            'source': 'auto_approval_execution',
                        },
                        'governance.rollout_executor': {
                            'enabled': True, 'mode': 'controlled', 'dry_run': False, 'allowed_action_types': ['joint_observe'],
                            'actor': 'system:rollout-executor', 'source': 'rollout_executor',
                        },
                        'governance.auto_promotion_review_execution': {
                            'enabled': True, 'mode': 'controlled', 'execute_due_post_promotion_reviews': True,
                            'escalate_rollback_review_queue': True,
                        },
                        'governance.adaptive_rollout_orchestration': {
                            'enforce_production_gate': False,
                        },
                    }
                    return mapping.get(key, default)

            result = execute_adaptive_rollout_orchestration(payload, db, config=StubConfig(), replay_source='unit-test-replay')

            orchestration = result['adaptive_rollout_orchestration']
            self.assertEqual(orchestration['summary']['pass_count'], 2)
            self.assertTrue(orchestration['summary']['rerun_triggered'])
            self.assertEqual(orchestration['summary']['rerun_reason'], 'auto_approval_promoted_ready_items')
            self.assertEqual(orchestration['summary']['auto_approval_executed_count'], 1)
            self.assertEqual(orchestration['summary']['controlled_rollout_executed_count'], 1)
            self.assertEqual(result['controlled_rollout_execution']['executed_count'], 1)
            state_row = db.get_approval_state('approval::observe')
            self.assertEqual(state_row['state'], 'approved')
            self.assertEqual(state_row['workflow_state'], 'ready')
            self.assertEqual(state_row['details']['execution_layer'], 'controlled_rollout_state_apply')

    def test_controlled_auto_approval_execution_validation_gate_gap_blocks_auto_progression(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}
            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({'governance.auto_approval_execution': {'enabled': True, 'mode': 'controlled'}})
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'auto_approval_validation_gap.db'))
            payload = {
                'validation_replay': {'summary': {'coverage_matrix': {'schema_version': 'm5_validation_coverage_matrix_v1', 'ready_for_low_intervention_gate': False, 'required_capability_count': 8, 'covered_required_count': 7, 'passing_required_count': 7, 'missing_required': ['testnet_bridge_controlled_execute'], 'failing_required': []}, 'readiness': {'low_intervention_gate_ready': False, 'missing_required_capabilities': ['testnet_bridge_controlled_execute'], 'failing_required_capabilities': [], 'failing_case_count': 0}}},
                'workflow_state': {'item_states': [{'item_id': 'playbook::eligible', 'title': 'Eligible low risk item', 'action_type': 'joint_observe', 'risk_level': 'low', 'approval_required': False, 'workflow_state': 'pending'}], 'summary': {}},
                'approval_state': {'items': [{'approval_id': 'approval::eligible', 'playbook_id': 'playbook::eligible', 'title': 'Eligible low risk item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_controlled_auto_approval_layer(payload, db, config=config, replay_source='unit-test-replay')
            self.assertEqual(db.get_approval_state('approval::eligible')['state'], 'pending')
            self.assertEqual(result['auto_approval_execution']['items'][0]['reason'], 'validation_gate_gap')
            self.assertEqual(result['auto_approval_execution']['execution_gate']['effect'], 'blocked_gap')

    def test_controlled_rollout_execution_validation_gate_regression_blocks_state_apply(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}
            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({'governance.controlled_rollout_execution': {'enabled': True, 'mode': 'state_apply', 'auto_promote_ready_candidates': True, 'allowed_action_types': ['joint_stage_prepare']}})
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'controlled_rollout_validation_regression.db'))
            payload = {
                'validation_replay': {'summary': {'coverage_matrix': {'schema_version': 'm5_validation_coverage_matrix_v1', 'ready_for_low_intervention_gate': False, 'required_capability_count': 8, 'covered_required_count': 8, 'passing_required_count': 7, 'missing_required': [], 'failing_required': ['transition_policy_contract']}, 'readiness': {'low_intervention_gate_ready': False, 'missing_required_capabilities': [], 'failing_required_capabilities': ['transition_policy_contract'], 'failing_case_count': 1}}},
                'workflow_state': {'item_states': [{'item_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare', 'risk_level': 'low', 'approval_required': False, 'workflow_state': 'pending', 'current_rollout_stage': 'observe'}], 'summary': {}},
                'approval_state': {'items': [{'approval_id': 'approval::stage', 'playbook_id': 'playbook::stage', 'title': 'Stage prepare item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': []}], 'summary': {}},
            }
            payload = attach_auto_approval_policy(payload)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_controlled_rollout_layer(payload, db, config=config, replay_source='unit-test-replay')
            self.assertEqual(db.get_approval_state('approval::stage')['state'], 'pending')
            self.assertEqual(result['controlled_rollout_execution']['items'][0]['reason'], 'validation_gate_regression')
            self.assertEqual(result['controlled_rollout_execution']['execution_gate']['effect'], 'blocked_regression')

    def _make_controlled_auto_promotion_payload(self, *, blocked=False, manual=False, approval_required=False, workflow_state='ready', current_stage='guarded_prepare', target_stage='controlled_apply'):
        workflow_item = {
            'item_id': 'playbook::promo',
            'title': 'Promotion ready item',
            'action_type': 'joint_stage_prepare',
            'risk_level': 'low',
            'approval_required': approval_required,
            'requires_manual': manual,
            'workflow_state': workflow_state,
            'current_rollout_stage': current_stage,
            'target_rollout_stage': target_stage,
            'blocking_reasons': ['validation_gap'] if blocked else [],
            'auto_approval_decision': 'auto_approve',
            'auto_approval_eligible': True,
            'stage_handler': {
                'stage_key': current_stage,
                'advisory': {
                    'recommended_stage': target_stage,
                    'recommended_action': 'promote_to_controlled_apply',
                    'urgency': 'high',
                    'confidence': 0.93,
                    'reasons': ['auto_advance_allowed'],
                    'ready_for_live_promotion': not (blocked or manual or approval_required),
                    'auto_promotion_candidate': not (blocked or manual or approval_required),
                },
            },
            'state_machine': {
                'auto_advance_gate': {
                    'allowed': not (blocked or manual or approval_required),
                    'readiness_score': 100 if not (blocked or manual or approval_required) else 30,
                    'blockers': ([] if not blocked else ['blocked_by:validation_gap']) + ([] if not manual else ['manual_gate_required']) + ([] if not approval_required else ['approval_required']),
                },
                'rollback_gate': {'candidate': False, 'triggered': []},
            },
            'validation_gate': {'enabled': True, 'ready': True, 'freeze_auto_advance': False, 'rollback_on_regression': False, 'reasons': []},
            'lane_routing': {'lane_id': 'auto_batch' if not (blocked or manual or approval_required) else 'blocked', 'queue_name': None, 'dispatch_route': 'stage_metadata_apply', 'route_family': 'safe_apply', 'next_transition': 'promote_to_controlled_apply'},
            'stage_loop': {'loop_state': 'auto_advance' if not (blocked or manual or approval_required) else 'review_pending', 'recommended_action': 'promote_to_controlled_apply', 'waiting_on': [] if not blocked else ['blocked_by:validation_gap']},
        }
        approval_item = {
            'approval_id': 'approval::promo',
            'playbook_id': 'playbook::promo',
            'title': 'Promotion ready item',
            'action_type': 'joint_stage_prepare',
            'approval_state': 'pending',
            'decision_state': workflow_state,
            'risk_level': 'low',
            'approval_required': approval_required,
            'requires_manual': manual,
            'blocked_by': ['validation_gap'] if blocked else [],
            'auto_approval_decision': 'auto_approve',
            'auto_approval_eligible': True,
        }
        return {
            'validation_gate': {'enabled': True, 'ready': True, 'freeze_auto_advance': False, 'rollback_on_regression': False, 'reasons': []},
            'workflow_state': {'item_states': [workflow_item], 'summary': {}},
            'approval_state': {'items': [approval_item], 'summary': {}},
        }

    def test_controlled_rollout_execution_default_switch_keeps_ready_candidate_inert(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}
            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({'governance.controlled_rollout_execution': {'enabled': True, 'mode': 'state_apply', 'allowed_action_types': ['joint_stage_prepare']}})
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'controlled_rollout_default_switch.db'))
            payload = TestApprovalPersistence()._make_controlled_auto_promotion_payload()
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_controlled_rollout_layer(payload, db, config=config, replay_source='unit-test-replay')
            persisted = db.get_approval_state('approval::promo')
            self.assertEqual(persisted['state'], 'pending')
            self.assertEqual(result['controlled_rollout_execution']['executed_count'], 0)
            self.assertEqual(result['controlled_rollout_execution']['safety_switch'], 'auto_promote_ready_candidates_disabled')


    def test_auto_promotion_execution_summary_surfaces_recent_execution_and_rollback_candidates(self):
        payload = TestApprovalPersistence()._make_controlled_auto_promotion_payload()
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'auto_promotion_summary.db'))
            executed = execute_controlled_rollout_layer(payload, db, settings={
                'enabled': True,
                'mode': 'state_apply',
                'auto_promote_ready_candidates': True,
                'allowed_action_types': ['joint_stage_prepare'],
                'actor': 'system:test-auto-promotion-summary',
                'source': 'unit_test_auto_promotion_summary',
            })
            consumer = build_workflow_consumer_view(executed)
            workflow_item = consumer['workflow_state']['item_states'][0]
            workflow_item['rollback_gate'] = {'candidate': True, 'triggered': ['review_overdue']}
            summary = build_auto_promotion_execution_summary(executed, max_items=5)
            self.assertEqual(summary['summary']['event_count'], 1)
            self.assertEqual(summary['summary']['stage_transition_counts']['guarded_prepare->controlled_apply'], 1)
            self.assertEqual(summary['summary']['rollback_review_candidate_count'], 1)
            self.assertEqual(summary['recent_executions'][0]['actor'], 'system:test-auto-promotion-summary')
            self.assertTrue(summary['recent_executions'][0]['review_due_at'])
            self.assertEqual(summary['rollback_review_candidates'][0]['rollback_triggered'], ['review_overdue'])

    def test_database_auto_promotion_activity_summary_reads_controlled_rollout_events(self):
        payload = TestApprovalPersistence()._make_controlled_auto_promotion_payload()
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'auto_promotion_activity.db'))
            execute_controlled_rollout_layer(payload, db, settings={
                'enabled': True,
                'mode': 'state_apply',
                'auto_promote_ready_candidates': True,
                'allowed_action_types': ['joint_stage_prepare'],
                'actor': 'system:test-db-auto-promotion',
                'source': 'unit_test_db_auto_promotion',
            })
            persisted = db.get_approval_state('approval::promo')
            self.assertTrue(persisted['details']['scheduled_review']['review_due_at'])
            self.assertEqual(persisted['details']['scheduled_review']['queue_kind'], 'post_promotion_review_queue')
            self.assertIn('post_apply_samples', persisted['details']['scheduled_review']['observation_targets'])
            summary = db.get_auto_promotion_activity_summary(limit=10)
            self.assertEqual(summary['event_count'], 1)
            self.assertEqual(summary['stage_transition_counts']['guarded_prepare->controlled_apply'], 1)
            self.assertEqual(summary['target_stage_counts']['controlled_apply'], 1)
            self.assertIn('auto_advance_allowed', summary['reason_code_counts'])
            self.assertEqual(summary['recent_items'][0]['actor'], 'system:test-db-auto-promotion')
            self.assertEqual(summary['post_promotion_review_queue_count'], 1)
            self.assertEqual(summary['rollback_review_queue_count'], 0)
            self.assertIn('post_apply_samples', summary['review_queues']['post_promotion_review_queue'][0]['observation_targets'])

    def test_auto_promotion_execution_summary_builds_post_promotion_and_rollback_review_queues(self):
        payload = TestApprovalPersistence()._make_controlled_auto_promotion_payload()
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'auto_promotion_review_queues.db'))
            executed = execute_controlled_rollout_layer(payload, db, settings={
                'enabled': True,
                'mode': 'state_apply',
                'auto_promote_ready_candidates': True,
                'allowed_action_types': ['joint_stage_prepare'],
                'actor': 'system:test-auto-promotion-queues',
                'source': 'unit_test_auto_promotion_queues',
            })
            consumer = build_workflow_consumer_view(executed)
            workflow_item = consumer['workflow_state']['item_states'][0]
            summary = build_auto_promotion_execution_summary(executed, max_items=5)
            self.assertEqual(summary['summary']['post_promotion_review_queue_count'], 1)
            self.assertTrue(summary['review_queues']['post_promotion_review_queue'][0]['review_due_at'])
            self.assertEqual(summary['review_queues']['post_promotion_review_queue'][0]['recommended_action'], 'run_scheduled_review')
            self.assertEqual(summary['review_queues']['post_promotion_review_queue'][0]['queue_kind'], 'post_promotion_review_queue')
            self.assertIn('post_apply_samples', summary['review_queues']['post_promotion_review_queue'][0]['observation_targets'])
            workflow_item['rollback_gate'] = {'candidate': True, 'triggered': ['review_overdue']}
            summary = build_auto_promotion_execution_summary(executed, max_items=5)
            self.assertEqual(summary['summary']['rollback_review_queue_count'], 1)
            self.assertEqual(summary['review_queues']['rollback_review_queue'][0]['recommended_action'], 'prepare_rollback_review')
            self.assertIn('rollback_trigger:review_overdue', summary['review_queues']['rollback_review_queue'][0]['observation_targets'])

    def test_auto_promotion_review_queue_consumption_flows_into_workbench_and_overview(self):
        payload = self._make_controlled_auto_promotion_payload()
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'auto_promotion_review_queue_consumption.db'))
            executed = execute_controlled_rollout_layer(payload, db, settings={
                'enabled': True,
                'mode': 'state_apply',
                'auto_promote_ready_candidates': True,
                'allowed_action_types': ['joint_stage_prepare'],
                'actor': 'system:test-auto-promotion-overview',
                'source': 'unit_test_auto_promotion_overview',
            })
            consumer = build_workflow_consumer_view(executed)
            workflow_item = consumer['workflow_state']['item_states'][0]
            self.assertTrue(workflow_item['scheduled_review']['review_due_at'])
            workflow_item['rollback_gate'] = {'candidate': True, 'triggered': ['review_overdue']}
            review_queue_consumption = build_auto_promotion_review_queue_consumption(
                build_auto_promotion_execution_summary(executed, max_items=5),
                max_items=5,
                label='unit_test',
            )
            self.assertEqual(review_queue_consumption['summary']['rollback_review_queue_count'], 1)
            self.assertEqual(review_queue_consumption['summary']['post_promotion_review_queue_count'], 0)
            self.assertEqual(review_queue_consumption['summary']['dominant_action'], 'prepare_rollback_review')
            workbench = build_workbench_governance_view(executed, max_items=5)
            self.assertEqual(workbench['summary']['auto_promotion_review_queues']['rollback_review_queue_count'], 1)
            self.assertEqual(workbench['rollout']['auto_promotion_review_queues']['items'][0]['queue_kind'], 'rollback_review_queue')
            self.assertEqual(workbench['rollout']['follow_up_review_queue'][0]['recommended_action'], 'prepare_rollback_review')
            overview = build_unified_workbench_overview(executed, max_items=5)
            self.assertEqual(overview['summary']['auto_promotion_review_queues']['rollback_review_queue_count'], 1)
            self.assertEqual(overview['lines']['rollout']['counts']['rollback_review_queue'], 1)
            self.assertEqual(overview['lines']['rollout']['counts']['promotion_review_due'], 1)
            self.assertEqual(overview['lines']['rollout']['auto_promotion_review_queues']['summary']['dominant_action'], 'prepare_rollback_review')
            self.assertEqual(overview['lines']['rollout']['follow_up_review_queue'][0]['queue_kind'], 'rollback_review_queue')
            self.assertIn('rollback_review_queue', [row['route'] for row in overview['lines']['rollout']['next_actions']])
            self.assertEqual(overview['lines']['rollout']['key_alerts'][0]['recommended_action'], 'prepare_rollback_review')

    def test_recovery_execution_schedules_retry_and_rollback_and_manual_annotation(self):
        payload = {
            'workflow_state': {'item_states': [
                {'item_id': 'playbook::retry', 'title': 'Retry item', 'action_type': 'joint_observe', 'workflow_state': 'execution_failed', 'risk_level': 'low', 'state_machine': _build_state_machine_semantics(item_id='approval::retry', approval_state='pending', workflow_state='execution_failed', execution_status='error', retryable=True, rollback_hint='restore_previous_state_from_approval_timeline', dispatch_route='retry_queue', next_transition='retry_execution')},
                {'item_id': 'playbook::rollback', 'title': 'Rollback item', 'action_type': 'joint_stage_prepare', 'workflow_state': 'rollback_pending', 'risk_level': 'high', 'state_machine': _build_state_machine_semantics(item_id='approval::rollback', approval_state='pending', workflow_state='rollback_pending', execution_status='error', retryable=False, rollback_hint='revert_stage_metadata_to_previous_stage', dispatch_route='rollback_candidate_queue', next_transition='freeze_and_review')},
                {'item_id': 'playbook::manual', 'title': 'Manual recovery item', 'action_type': 'joint_observe', 'workflow_state': 'execution_failed', 'risk_level': 'medium'},
            ], 'summary': {}},
            'approval_state': {'items': [
                {'approval_id': 'approval::retry', 'playbook_id': 'playbook::retry', 'title': 'Retry item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'risk_level': 'low'},
                {'approval_id': 'approval::rollback', 'playbook_id': 'playbook::rollback', 'title': 'Rollback item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'risk_level': 'high'},
                {'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual recovery item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'risk_level': 'medium'},
            ], 'summary': {}},
            'workflow_recovery_view': {'summary': {'retry_queue_count': 1, 'rollback_candidate_count': 1, 'manual_recovery_count': 1}, 'queues': {
                'retry_queue': [{'item_id': 'playbook::retry', 'approval_id': 'approval::retry', 'title': 'Retry item', 'action_type': 'joint_observe', 'workflow_state': 'execution_failed', 'approval_state': 'pending', 'risk_level': 'low', 'recovery_orchestration': {'queue_bucket': 'retry_queue', 'target_route': 'retry_queue', 'retry_schedule': {'retry_count': 0, 'should_retry_at': '2026-03-29T00:00:00Z'}, 'manual_recovery': {}, 'routing_reason_codes': ['retryable_execution_failure']}}],
                'rollback_candidates': [{'item_id': 'playbook::rollback', 'approval_id': 'approval::rollback', 'title': 'Rollback item', 'action_type': 'joint_stage_prepare', 'workflow_state': 'rollback_pending', 'approval_state': 'pending', 'risk_level': 'high', 'recovery_orchestration': {'queue_bucket': 'rollback_candidate', 'target_route': 'rollback_candidate_queue', 'retry_schedule': {}, 'manual_recovery': {}, 'routing_reason_codes': ['rollback_pending_guardrail']}}],
                'manual_recovery': [{'item_id': 'playbook::manual', 'approval_id': 'approval::manual', 'title': 'Manual recovery item', 'action_type': 'joint_observe', 'workflow_state': 'execution_failed', 'approval_state': 'pending', 'risk_level': 'medium', 'recovery_orchestration': {'queue_bucket': 'manual_recovery', 'target_route': 'manual_recovery_queue', 'retry_schedule': {}, 'manual_recovery': {'required': True, 'route': 'manual_recovery_queue'}, 'routing_reason_codes': ['manual_recovery_required']}}],
            }},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'recovery_execution.db'))
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            executed = execute_recovery_queue_layer(payload, db, settings={'enabled': True, 'mode': 'controlled', 'actor': 'system:test-recovery', 'source': 'unit_test_recovery'}, replay_source='unit-test-replay', now='2026-03-30T00:00:00Z')
            summary = executed['recovery_execution']
            self.assertEqual(summary['scheduled_retry_count'], 1)
            self.assertEqual(summary['rollback_queued_count'], 1)
            self.assertEqual(summary['manual_recovery_annotated_count'], 1)
            self.assertEqual(summary['retry_reentered_executor_count'], 1)
            self.assertTrue(summary['executor_pass']['attempted'])
            self.assertEqual(summary['executor_pass']['applied_count'], 1)
            retry_item = next(row for row in summary['items'] if row['item_id'] == 'playbook::retry')
            self.assertEqual(retry_item['retry_source'], 'retry_queue')
            self.assertEqual(retry_item['retry_attempt'], 1)
            self.assertTrue(retry_item['reentered_executor'])
            self.assertEqual(retry_item['executor_reentry']['disposition'], 'applied')
            self.assertEqual(retry_item['executor_reentry']['dispatch_route'], 'safe_state_apply')
            retry_row = db.get_approval_state('approval::retry')
            self.assertEqual(retry_row['workflow_state'], 'ready')
            self.assertEqual(retry_row['details']['recovery_execution']['retry_attempt'], 1)
            self.assertTrue(retry_row['details']['recovery_execution']['reentered_executor'])
            self.assertEqual(retry_row['details']['recovery_execution']['executor_reentry']['disposition'], 'applied')
            rollback_row = db.get_approval_state('approval::rollback')
            self.assertEqual(rollback_row['workflow_state'], 'rollback_pending')
            self.assertEqual(rollback_row['details']['recovery_execution']['route'], 'rollback_candidate_queue')
            manual_row = db.get_approval_state('approval::manual')
            self.assertEqual(manual_row['details']['recovery_execution']['status'], 'manual_recovery_annotated')

    def test_recovery_execution_respects_retry_due_time_and_dry_run(self):
        payload = {
            'workflow_state': {'item_states': [
                {'item_id': 'playbook::retry', 'title': 'Retry item', 'action_type': 'joint_observe', 'workflow_state': 'execution_failed', 'risk_level': 'low', 'state_machine': _build_state_machine_semantics(item_id='approval::retry', approval_state='pending', workflow_state='execution_failed', execution_status='error', retryable=True, rollback_hint='restore_previous_state_from_approval_timeline', dispatch_route='retry_queue', next_transition='retry_execution', last_transition={'from_execution_status': 'error'})},
            ], 'summary': {}},
            'approval_state': {'items': [
                {'approval_id': 'approval::retry', 'playbook_id': 'playbook::retry', 'title': 'Retry item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'risk_level': 'low'},
            ], 'summary': {}},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'recovery_execution_due.db'))
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            dry_run = execute_recovery_queue_layer(payload, db, settings={'enabled': True, 'mode': 'dry_run'}, replay_source='unit-test-replay', now='2026-03-28T00:00:00Z')
            self.assertEqual(dry_run['recovery_execution']['scheduled_retry_count'], 0)
            self.assertEqual(dry_run['recovery_execution']['skipped_count'], 1)
            persisted = db.get_approval_state('approval::retry')
            self.assertEqual(persisted['workflow_state'], 'execution_failed')

    def test_recovery_execution_only_reenters_due_retry_items_into_executor(self):
        payload = {
            'workflow_state': {'item_states': [
                {'item_id': 'playbook::retry_due', 'title': 'Retry due item', 'action_type': 'joint_observe', 'workflow_state': 'execution_failed', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'confidence': 'high', 'state_machine': _build_state_machine_semantics(item_id='approval::retry_due', approval_state='pending', workflow_state='execution_failed', execution_status='error', retryable=True, rollback_hint='restore_previous_state_from_approval_timeline', dispatch_route='retry_queue', next_transition='retry_execution')},
                {'item_id': 'playbook::retry_later', 'title': 'Retry later item', 'action_type': 'joint_observe', 'workflow_state': 'execution_failed', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'confidence': 'high', 'state_machine': _build_state_machine_semantics(item_id='approval::retry_later', approval_state='pending', workflow_state='execution_failed', execution_status='error', retryable=True, rollback_hint='restore_previous_state_from_approval_timeline', dispatch_route='retry_queue', next_transition='retry_execution')},
            ], 'summary': {}},
            'approval_state': {'items': [
                {'approval_id': 'approval::retry_due', 'playbook_id': 'playbook::retry_due', 'title': 'Retry due item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'auto_approval_eligible': True, 'auto_approval_decision': 'auto_approve'},
                {'approval_id': 'approval::retry_later', 'playbook_id': 'playbook::retry_later', 'title': 'Retry later item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'auto_approval_eligible': True, 'auto_approval_decision': 'auto_approve'},
            ], 'summary': {}},
            'workflow_recovery_view': {'summary': {'retry_queue_count': 2, 'rollback_candidate_count': 0, 'manual_recovery_count': 0}, 'queues': {
                'retry_queue': [
                    {'item_id': 'playbook::retry_due', 'approval_id': 'approval::retry_due', 'title': 'Retry due item', 'action_type': 'joint_observe', 'workflow_state': 'execution_failed', 'approval_state': 'pending', 'risk_level': 'low', 'recovery_orchestration': {'queue_bucket': 'retry_queue', 'target_route': 'retry_queue', 'retry_schedule': {'retry_count': 1, 'should_retry_at': '2026-03-29T00:00:00Z'}, 'manual_recovery': {}, 'routing_reason_codes': ['retryable_execution_failure']}},
                    {'item_id': 'playbook::retry_later', 'approval_id': 'approval::retry_later', 'title': 'Retry later item', 'action_type': 'joint_observe', 'workflow_state': 'execution_failed', 'approval_state': 'pending', 'risk_level': 'low', 'recovery_orchestration': {'queue_bucket': 'retry_queue', 'target_route': 'retry_queue', 'retry_schedule': {'retry_count': 2, 'should_retry_at': '2026-04-02T00:00:00Z'}, 'manual_recovery': {}, 'routing_reason_codes': ['retryable_execution_failure']}},
                ],
                'rollback_candidates': [],
                'manual_recovery': [],
            }},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'recovery_execution_reenter.db'))
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            executed = execute_recovery_queue_layer(payload, db, settings={'enabled': True, 'mode': 'controlled', 'actor': 'system:test-recovery', 'source': 'unit_test_recovery', 'retry_executor': {'enabled': True, 'mode': 'controlled', 'allowed_action_types': ['joint_observe']}}, replay_source='unit-test-replay', now='2026-03-30T00:00:00Z')
            summary = executed['recovery_execution']
            self.assertEqual(summary['scheduled_retry_count'], 1)
            self.assertEqual(summary['skipped_count'], 1)
            self.assertEqual(summary['retry_reentered_executor_count'], 1)
            self.assertEqual(summary['executor_pass']['eligible_count'], 1)
            self.assertEqual(summary['executor_pass']['applied_count'], 1)
            due_item = next(row for row in summary['items'] if row['item_id'] == 'playbook::retry_due')
            later_item = next(row for row in summary['items'] if row['item_id'] == 'playbook::retry_later')
            self.assertEqual(due_item['retry_attempt'], 2)
            self.assertTrue(due_item['reentered_executor'])
            self.assertEqual(due_item['executor_reentry']['result_code'], 'SAFE_APPLIED')
            self.assertEqual(later_item['action'], 'skipped')
            self.assertFalse(later_item['reentered_executor'])
            self.assertEqual(later_item['reason'], 'retry_not_due')
            due_row = db.get_approval_state('approval::retry_due')
            later_row = db.get_approval_state('approval::retry_later')
            self.assertEqual(due_row['workflow_state'], 'ready')
            self.assertEqual(due_row['details']['recovery_execution']['retry_attempt'], 2)
            self.assertEqual(later_row['workflow_state'], 'execution_failed')

    def test_recovery_execution_escalates_failed_retry_reentry_into_follow_up_semantics(self):
        payload = {
            'workflow_state': {'item_states': [
                {'item_id': 'playbook::retry_exhausted', 'title': 'Retry exhausted item', 'action_type': 'joint_observe', 'workflow_state': 'execution_failed', 'risk_level': 'low', 'approval_required': False, 'blocking_reasons': [], 'confidence': 'high', 'state_machine': _build_state_machine_semantics(item_id='approval::retry_exhausted', approval_state='pending', workflow_state='execution_failed', execution_status='error', retryable=True, rollback_hint='restore_previous_state_from_approval_timeline', dispatch_route='retry_queue', next_transition='retry_execution')},
            ], 'summary': {}},
            'approval_state': {'items': [
                {'approval_id': 'approval::retry_exhausted', 'playbook_id': 'playbook::retry_exhausted', 'title': 'Retry exhausted item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'blocked_by': [], 'auto_approval_eligible': True, 'auto_approval_decision': 'auto_approve'},
            ], 'summary': {}},
            'workflow_recovery_view': {'summary': {'retry_queue_count': 1, 'rollback_candidate_count': 0, 'manual_recovery_count': 0}, 'queues': {
                'retry_queue': [
                    {'item_id': 'playbook::retry_exhausted', 'approval_id': 'approval::retry_exhausted', 'title': 'Retry exhausted item', 'action_type': 'joint_observe', 'workflow_state': 'execution_failed', 'approval_state': 'pending', 'risk_level': 'low', 'recovery_orchestration': {'queue_bucket': 'retry_queue', 'target_route': 'retry_queue', 'retry_schedule': {'retry_count': 3, 'should_retry_at': '2026-03-29T00:00:00Z'}, 'manual_recovery': {}, 'routing_reason_codes': ['retryable_execution_failure']}},
                ],
                'rollback_candidates': [],
                'manual_recovery': [],
            }},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'recovery_execution_escalation.db'))
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            executed = execute_recovery_queue_layer(payload, db, settings={'enabled': True, 'mode': 'controlled', 'actor': 'system:test-recovery', 'source': 'unit_test_recovery', 'retry_executor': {'enabled': True, 'mode': 'controlled', 'allowed_action_types': ['joint_stage_prepare']}}, replay_source='unit-test-replay', now='2026-03-30T00:00:00Z')
            summary = executed['recovery_execution']
            self.assertEqual(summary['scheduled_retry_count'], 1)
            self.assertEqual(summary['retry_reentered_executor_count'], 1)
            self.assertEqual(summary['executor_pass']['skipped_count'], 1)
            self.assertEqual(summary['executor_pass']['follow_up_summary']['attention_required_count'], 1)
            self.assertEqual(summary['executor_pass']['follow_up_summary']['dominant_route'], 'manual_recovery_queue')
            retry_item = next(row for row in summary['items'] if row['item_id'] == 'playbook::retry_exhausted')
            self.assertTrue(retry_item['reentered_executor'])
            self.assertEqual(retry_item['follow_up_execution']['action'], 'escalate_manual_recovery')
            self.assertEqual(retry_item['follow_up_execution']['route'], 'manual_recovery_queue')
            self.assertEqual(retry_item['follow_up_execution']['escalation_policy'], 'manual_recovery')
            self.assertEqual(retry_item['follow_up_execution']['policy_gate']['decision'], 'review')
            persisted = db.get_approval_state('approval::retry_exhausted')
            self.assertEqual(persisted['details']['recovery_execution']['follow_up_execution']['route'], 'manual_recovery_queue')
            self.assertEqual(persisted['details']['recovery_execution']['follow_up_execution']['severity'], 'high')
            self.assertEqual(persisted['details']['recovery_execution']['follow_up_execution']['policy_gate']['family'], 'recovery_executor')

    def test_follow_up_policy_gate_unifies_review_retry_and_rollback_decisions(self):
        rollback_gate = _build_follow_up_policy_gate(
            item_id='playbook::rollback',
            family='auto_promotion_review',
            current_phase='post_promotion_follow_up',
            queue_kind='rollback_review_queue',
            disposition='rollback_review',
            rollback_candidate=True,
            reason_codes=['validation_gate_regressed'],
        )
        self.assertEqual(rollback_gate['decision'], 'rollback')
        self.assertEqual(rollback_gate['action'], 'prepare_rollback_review')
        self.assertEqual(rollback_gate['route'], 'rollback_review_queue')
        retry_gate = _build_follow_up_policy_gate(
            item_id='playbook::retry',
            family='recovery_executor',
            current_phase='recovery_follow_up',
            queue_kind='retry_queue',
            disposition='error',
            retryable=True,
            retry_attempt=1,
            reason_codes=['retryable_execution_failure'],
        )
        self.assertEqual(retry_gate['decision'], 'retry')
        self.assertEqual(retry_gate['route'], 'retry_queue')
        review_gate = _build_follow_up_policy_gate(
            item_id='playbook::review',
            family='auto_promotion_review',
            current_phase='post_promotion_follow_up',
            queue_kind='post_promotion_review_queue',
            disposition='review_due',
            review_due={'due_status': 'overdue', 'is_due': True},
            is_due=True,
            observation_targets=['post_apply_samples'],
        )
        self.assertEqual(review_gate['decision'], 'review')
        self.assertEqual(review_gate['action'], 'run_scheduled_review')
        self.assertIn('scheduled_follow_up_review', review_gate['reason_codes'])

    def test_auto_promotion_review_queue_execution_persists_follow_up_policy_gate(self):
        payload = self._make_controlled_auto_promotion_payload()
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'auto_promotion_review_policy_gate.db'))
            executed = execute_controlled_rollout_layer(payload, db, settings={
                'enabled': True,
                'mode': 'state_apply',
                'auto_promote_ready_candidates': True,
                'allowed_action_types': ['joint_stage_prepare'],
                'actor': 'system:test-auto-promotion-policy-gate',
                'source': 'unit_test_auto_promotion_policy_gate',
            })
            consumer = build_workflow_consumer_view(executed)
            workflow_item = consumer['workflow_state']['item_states'][0]
            workflow_item['scheduled_review'] = {'review_due_at': '2026-03-29T00:00:00Z', 'review_after_hours': 24}
            reviewed = execute_auto_promotion_review_queue_layer(
                executed,
                db,
                settings={'enabled': True, 'mode': 'controlled'},
                replay_source='unit-test-review-policy-gate',
            )
            summary_item = reviewed['auto_promotion_review_execution']['items'][0]
            persisted = db.get_approval_state(summary_item['approval_id'])
            policy_gate = persisted['details']['auto_promotion_review_execution']['follow_up_policy_gate']
            self.assertEqual(policy_gate['family'], 'auto_promotion_review_execution')
            self.assertEqual(policy_gate['decision'], 'review')
            self.assertIn(policy_gate['action'], {'run_scheduled_review', 'monitor_post_promotion_window'})
            self.assertTrue(policy_gate['summary'])

    def test_auto_promotion_review_queue_filter_view_supports_queue_due_target_and_trigger_filters(self):
        payload = self._make_controlled_auto_promotion_payload()
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'auto_promotion_review_queue_filters.db'))
            executed = execute_controlled_rollout_layer(payload, db, settings={
                'enabled': True,
                'mode': 'state_apply',
                'auto_promote_ready_candidates': True,
                'allowed_action_types': ['joint_stage_prepare'],
                'actor': 'system:test-auto-promotion-filter',
                'source': 'unit_test_auto_promotion_filter',
            })
            consumer = build_workflow_consumer_view(executed)
            workflow_item = consumer['workflow_state']['item_states'][0]
            workflow_item['scheduled_review'] = {'review_due_at': '2026-03-29T00:00:00Z', 'review_after_hours': 24}
            workflow_item['rollback_gate'] = {'candidate': True, 'triggered': ['review_overdue', 'validation_gate_regressed']}
            filtered = build_auto_promotion_review_queue_filter_view(
                executed,
                queue_kinds='rollback_review_queue',
                due_statuses='overdue',
                observation_targets='post_apply_samples',
                rollback_triggers='review_overdue',
                now='2026-03-30T00:00:00Z',
                limit=5,
            )
            self.assertEqual(filtered['schema_version'], 'm5_auto_promotion_review_queue_filter_view_v1')
            self.assertEqual(filtered['summary']['matched_count'], 1)
            self.assertEqual(filtered['items'][0]['queue_kind'], 'rollback_review_queue')
            self.assertEqual(filtered['items'][0]['due_status'], 'overdue')
            self.assertIn('post_apply_samples', filtered['items'][0]['observation_targets'])
            self.assertIn('review_overdue', filtered['items'][0]['rollback_triggered'])
            self.assertEqual(filtered['items'][0]['next_step'], 'prepare_rollback_review')

    def test_auto_promotion_review_queue_detail_view_explains_why_next_step_and_due(self):
        payload = self._make_controlled_auto_promotion_payload()
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'auto_promotion_review_queue_detail.db'))
            executed = execute_controlled_rollout_layer(payload, db, settings={
                'enabled': True,
                'mode': 'state_apply',
                'auto_promote_ready_candidates': True,
                'allowed_action_types': ['joint_stage_prepare'],
                'actor': 'system:test-auto-promotion-detail',
                'source': 'unit_test_auto_promotion_detail',
            })
            consumer = build_workflow_consumer_view(executed)
            workflow_item = consumer['workflow_state']['item_states'][0]
            workflow_item['scheduled_review'] = {'review_due_at': '2026-03-29T00:00:00Z', 'review_after_hours': 24}
            workflow_item['rollback_gate'] = {'candidate': True, 'triggered': ['review_overdue']}
            detail = build_auto_promotion_review_queue_detail_view(
                executed,
                item_id=workflow_item['item_id'],
                now='2026-03-30T00:00:00Z',
            )
            self.assertTrue(detail['found'])
            self.assertEqual(detail['summary']['queue_kind'], 'rollback_review_queue')
            self.assertEqual(detail['summary']['due_status'], 'overdue')
            self.assertEqual(detail['summary']['next_step'], 'prepare_rollback_review')
            self.assertIn('review_overdue', detail['summary']['rollback_triggered'])
            self.assertIn('rollback_candidate', detail['summary']['why_in_queue'])
            self.assertIn('scheduled review is overdue', detail['summary']['queue_reason'])


    def test_controlled_rollout_execution_applies_ready_candidate_with_full_audit(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}
            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({'governance.controlled_rollout_execution': {'enabled': True, 'mode': 'state_apply', 'auto_promote_ready_candidates': True, 'allowed_action_types': ['joint_stage_prepare'], 'actor': 'system:test-controlled-rollout', 'source': 'unit_test_controlled_rollout'}})
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'controlled_rollout_ready_candidate.db'))
            payload = self._make_controlled_auto_promotion_payload()
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_controlled_rollout_layer(payload, db, config=config, replay_source='unit-test-replay')
            persisted = db.get_approval_state('approval::promo')
            details = persisted['details']
            self.assertEqual(result['controlled_rollout_execution']['executed_count'], 1)
            self.assertEqual(persisted['state'], 'ready')
            self.assertEqual(persisted['workflow_state'], 'ready')
            self.assertEqual(details['rollout_stage'], 'controlled_apply')
            self.assertEqual(details['stage_transition']['from'], 'guarded_prepare')
            self.assertEqual(details['stage_transition']['to'], 'controlled_apply')
            self.assertEqual(details['auto_promotion_execution']['before']['rollout_stage'], 'guarded_prepare')
            self.assertEqual(details['auto_promotion_execution']['after']['rollout_stage'], 'controlled_apply')
            self.assertEqual(details['auto_promotion_execution']['event_log'][0]['actor'], 'system:test-controlled-rollout')
            self.assertEqual(details['auto_promotion_execution']['event_log'][0]['source'], 'unit_test_controlled_rollout')
            self.assertTrue(details['real_trade_execution'] is False)
            self.assertTrue(details['dangerous_live_parameter_change'] is False)
            self.assertTrue(details['auto_promotion_execution']['rollback_hint'])
            timeline = db.get_approval_timeline(item_id='approval::promo', ascending=True)
            self.assertEqual(timeline[-1]['event_type'], 'controlled_rollout_stage_prepare')

    def test_controlled_rollout_execution_rejects_blocked_candidate(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}
            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({'governance.controlled_rollout_execution': {'enabled': True, 'mode': 'state_apply', 'auto_promote_ready_candidates': True, 'allowed_action_types': ['joint_stage_prepare']}})
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'controlled_rollout_blocked_candidate.db'))
            payload = self._make_controlled_auto_promotion_payload(blocked=True)
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_controlled_rollout_layer(payload, db, config=config, replay_source='unit-test-replay')
            persisted = db.get_approval_state('approval::promo')
            self.assertEqual(persisted['state'], 'pending')
            self.assertEqual(result['controlled_rollout_execution']['items'][0]['reason'], 'candidate_not_ready')
            self.assertEqual(result['controlled_rollout_execution']['executed_count'], 0)

    def test_controlled_rollout_execution_protects_terminal_stage(self):
        class StubConfig:
            def __init__(self, values=None):
                self.values = values or {}
            def get(self, key, default=None):
                return self.values.get(key, default)

        config = StubConfig({'governance.controlled_rollout_execution': {'enabled': True, 'mode': 'state_apply', 'auto_promote_ready_candidates': True, 'allowed_action_types': ['joint_stage_prepare']}})
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'controlled_rollout_terminal_stage.db'))
            payload = self._make_controlled_auto_promotion_payload(current_stage='review_pending', target_stage='review_pending')
            db.sync_approval_items(build_workflow_approval_records(payload), replay_source='unit-test')
            result = execute_controlled_rollout_layer(payload, db, config=config, replay_source='unit-test-replay')
            persisted = db.get_approval_state('approval::promo')
            self.assertEqual(persisted['state'], 'pending')
            self.assertEqual(result['controlled_rollout_execution']['items'][0]['reason'], 'terminal_stage:review_pending')

    def test_approval_state_api_and_replay_endpoint_expose_persisted_rows(self):
        import dashboard.api as dashboard_api

        report = build_regime_policy_calibration_report([
            {
                'symbol': 'BTC/USDT',
                'all_trades': [
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.3},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.2},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v1', 'strategy_tags': ['Breakout'], 'return_pct': 0.1},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.4},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -1.0},
                    {'regime_tag': 'panic', 'policy_tag': 'policy_v2', 'strategy_tags': ['Breakout'], 'return_pct': -0.8},
                ],
            }
        ])

        class StubBacktester:
            def run_all(self, symbols):
                return {
                    'summary': {'symbols': len(symbols)},
                    'symbols': [{'symbol': 'BTC/USDT'}],
                    'calibration_report': report,
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = Database(str(Path(tmpdir) / 'api_approval_state.db'))
            old_db = dashboard_api.db
            old_backtester = dashboard_api.backtester
            dashboard_api.db = test_db
            dashboard_api.backtester = StubBacktester()
            try:
                client = app.test_client()
                replay_resp = client.get('/api/approvals/replay')
                self.assertEqual(replay_resp.status_code, 200)
                replay_payload = replay_resp.get_json()
                self.assertTrue(replay_payload['data']['approval_state']['items'])
                approval_item = replay_payload['data']['approval_state']['items'][0]

                execute_resp = client.post('/api/approvals/execute', json={
                    'type': approval_item['action_type'],
                    'target': approval_item['playbook_id'],
                    'item_id': approval_item['approval_id'],
                    'decision': 'deferred',
                    'reason': 'wait more data',
                    'actor': 'unit-test',
                })
                self.assertEqual(execute_resp.status_code, 200)

                state_resp = client.get('/api/approvals/state?state=deferred')
                self.assertEqual(state_resp.status_code, 200)
                state_payload = state_resp.get_json()
                self.assertEqual(state_payload['summary']['deferred'], 1)
                self.assertEqual(state_payload['data'][0]['item_id'], approval_item['approval_id'])

                timeline_resp = client.get(f"/api/approvals/timeline?item_id={approval_item['approval_id']}")
                self.assertEqual(timeline_resp.status_code, 200)
                timeline_payload = timeline_resp.get_json()
                self.assertGreaterEqual(timeline_payload['summary']['count'], 2)
                self.assertEqual(timeline_payload['data'][0]['event_type'], 'decision_recorded')

                timeline_summary_resp = client.get(f"/api/approvals/timeline-summary?item_id={approval_item['approval_id']}")
                self.assertEqual(timeline_summary_resp.status_code, 200)
                timeline_summary_payload = timeline_summary_resp.get_json()
                self.assertEqual(timeline_summary_payload['data']['current']['state'], 'deferred')
                self.assertTrue(timeline_summary_payload['data']['decision_path'])

                decision_diff_resp = client.get('/api/approvals/decision-diff?limit=5')
                self.assertEqual(decision_diff_resp.status_code, 200)
                decision_diff_payload = decision_diff_resp.get_json()
                self.assertGreaterEqual(decision_diff_payload['summary']['count'], 1)

                transition_journal_resp = client.get(f"/api/approvals/transition-journal?item_id={approval_item['approval_id']}&limit=5")
                self.assertEqual(transition_journal_resp.status_code, 200)
                transition_journal_payload = transition_journal_resp.get_json()
                self.assertGreaterEqual(transition_journal_payload['summary']['count'], 1)
                self.assertTrue(transition_journal_payload['data']['recent_transitions'])
                first_transition = transition_journal_payload['data']['recent_transitions'][0]
                self.assertIn('from', first_transition)
                self.assertIn('to', first_transition)
                self.assertIn('trigger', first_transition)
                self.assertIn('actor', first_transition)

                with sqlite3.connect(Path(tmpdir) / 'api_approval_state.db') as conn:
                    conn.execute("UPDATE approval_state SET created_at = datetime('now', '-180 minutes'), updated_at = datetime('now', '-180 minutes'), last_seen_at = datetime('now', '-180 minutes'), state = 'pending', decision = 'pending', workflow_state = 'pending' WHERE item_id = ?", (approval_item['approval_id'],))
                    conn.commit()
                stale_resp = client.get('/api/approvals/stale?stale_after_minutes=60')
                self.assertEqual(stale_resp.status_code, 200)
                stale_payload = stale_resp.get_json()
                self.assertGreaterEqual(stale_payload['summary']['count'], 1)

                cleanup_preview_resp = client.get('/api/approvals/cleanup?stale_after_minutes=60')
                self.assertEqual(cleanup_preview_resp.status_code, 200)
                cleanup_preview_payload = cleanup_preview_resp.get_json()
                self.assertEqual(cleanup_preview_payload['data']['matched_count'], 1)
                cleanup_resp = client.post('/api/approvals/cleanup', json={'stale_after_minutes': 60})
                self.assertEqual(cleanup_resp.status_code, 200)
                cleanup_payload = cleanup_resp.get_json()
                self.assertEqual(cleanup_payload['data']['expired_count'], 1)

                audit_overview_resp = client.get(f"/api/approvals/audit-overview?item_id={approval_item['approval_id']}&stale_after_minutes=60")
                self.assertEqual(audit_overview_resp.status_code, 200)
                audit_overview_payload = audit_overview_resp.get_json()
                self.assertIn('stale_pending', audit_overview_payload['data'])
                self.assertIn('decision_diff', audit_overview_payload['data'])
                self.assertIn('timeline_summary', audit_overview_payload['data'])
                self.assertIn('transition_journal', audit_overview_payload['data'])
                self.assertIn('recent_transitions', audit_overview_payload['data']['transition_journal'])

                with sqlite3.connect(Path(tmpdir) / 'api_approval_state.db') as conn:
                    conn.execute('DELETE FROM approval_state WHERE item_id = ?', (approval_item['approval_id'],))
                    conn.commit()
                recover_resp = client.post('/api/approvals/recover', json={'item_id': approval_item['approval_id']})
                self.assertEqual(recover_resp.status_code, 200)
                recover_payload = recover_resp.get_json()
                self.assertEqual(recover_payload['data']['state'], 'deferred')
            finally:
                dashboard_api.db = old_db
                dashboard_api.backtester = old_backtester

    def test_execute_adaptive_rollout_orchestration_blocks_mutations_when_production_gate_not_ready(self):
        class DummyConfig:
            def __init__(self, mapping):
                self.mapping = mapping
            def get(self, key, default=None):
                parts = key.split('.')
                value = self.mapping
                for part in parts:
                    if isinstance(value, dict) and part in value:
                        value = value[part]
                    else:
                        return default
                return value

        payload = {
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::eligible',
                        'title': 'Eligible low risk item',
                        'action_type': 'joint_observe',
                        'risk_level': 'low',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'ready',
                        'blocking_reasons': [],
                    },
                    {
                        'item_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'risk_level': 'high',
                        'approval_required': True,
                        'requires_manual': True,
                        'workflow_state': 'blocked_by_approval',
                        'blocking_reasons': [],
                    },
                ],
                'summary': {'item_count': 2},
            },
            'approval_state': {
                'items': [
                    {'approval_id': 'approval::eligible', 'playbook_id': 'playbook::eligible', 'title': 'Eligible low risk item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []},
                    {'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'high', 'approval_required': True, 'requires_manual': True, 'blocked_by': []},
                ],
                'summary': {'pending_count': 2},
            },
        }
        cfg = DummyConfig({
            'governance': {
                'auto_approval_execution': {
                    'enabled': True,
                    'mode': 'controlled',
                },
                'adaptive_rollout_orchestration': {
                    'enforce_production_gate': True,
                },
            }
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'adaptive_gate_blocked.db'))
            result = execute_adaptive_rollout_orchestration(copy.deepcopy(payload), db, config=cfg, replay_source='test_gate_blocked')
            orchestration = result['adaptive_rollout_orchestration']
            self.assertEqual(orchestration['schema_version'], 'm5_adaptive_rollout_orchestration_v2')
            self.assertTrue(orchestration['summary']['gate_enforced'])
            self.assertTrue(orchestration['summary']['gate_blocked'])
            self.assertEqual(orchestration['summary']['gate_status'], 'review_required')
            self.assertIn('manual_approval_backlog', orchestration['summary']['gate_blocking_issues'])
            self.assertEqual(result['auto_approval_execution']['mode'], 'gated_off')
            self.assertEqual(result['auto_approval_execution']['executed_count'], 0)
            self.assertIsNone(db.get_approval_state('approval::eligible'))
            self.assertEqual(result['approval_state']['items'][0]['approval_state'], 'pending')

    def test_execute_testnet_bridge_layer_blocks_without_controlled_rollout_execution(self):
        class DummyConfig:
            def __init__(self, mapping):
                self.mapping = mapping
                self.exchange_mode = 'testnet'
                self.position_mode = 'hedge'
                self.symbols = ['BTC/USDT']
                self.all = mapping
            def get(self, key, default=None):
                parts = key.split('.')
                value = self.mapping
                for part in parts:
                    if isinstance(value, dict) and part in value:
                        value = value[part]
                    else:
                        return default
                return value

        class BridgeExchangeStub:
            def fetch_balance(self):
                return {'free': {'USDT': 1000}}
            def is_futures_symbol(self, symbol):
                return True
            def fetch_ticker(self, symbol):
                return {'last': 50000}
            def normalize_contract_amount(self, symbol, desired_notional, price):
                return 0.001
            def get_order_symbol(self, symbol):
                return symbol

        cfg = DummyConfig({
            'exchange': {'mode': 'testnet'},
            'governance': {'testnet_bridge_execution': {'enabled': True, 'mode': 'controlled_execute', 'require_controlled_rollout_execution': True}},
        })
        payload = {
            'approval_state': {'items': []},
            'controlled_rollout_execution': {'executed_count': 0},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'testnet_bridge_blocked.db'))
            executed = execute_testnet_bridge_layer(copy.deepcopy(payload), db, config=cfg, exchange=BridgeExchangeStub())
            bridge = executed['testnet_bridge_execution']
            self.assertEqual(bridge['status'], 'blocked')
            self.assertIn('controlled_rollout_execution_missing', bridge['blocking_reasons'])
            self.assertIsNone(bridge['result'])

    def test_execute_testnet_bridge_layer_runs_minimal_smoke_when_guardrails_pass(self):
        class DummyConfig:
            def __init__(self, mapping):
                self.mapping = mapping
                self.exchange_mode = 'testnet'
                self.position_mode = 'hedge'
                self.symbols = ['BTC/USDT']
                self.all = mapping
            def get(self, key, default=None):
                parts = key.split('.')
                value = self.mapping
                for part in parts:
                    if isinstance(value, dict) and part in value:
                        value = value[part]
                    else:
                        return default
                return value

        class BridgeExchangeStub:
            def fetch_balance(self):
                return {'free': {'USDT': 1000}}
            def is_futures_symbol(self, symbol):
                return True
            def fetch_ticker(self, symbol):
                return {'last': 50000}
            def normalize_contract_amount(self, symbol, desired_notional, price):
                return 0.001
            def get_order_symbol(self, symbol):
                return symbol

        calls = []
        def fake_bridge_runner(cfg, exchange, symbol=None, side='long', db=None):
            calls.append({'symbol': symbol, 'side': side, 'db': bool(db)})
            return {
                'opened': True,
                'closed': True,
                'open_status': 'filled',
                'close_status': 'filled',
                'cleanup_needed': False,
                'residual_position_detected': False,
                'reconcile_summary': {'open_order_confirmed': True, 'close_order_confirmed': True},
                'failure_compensation_hint': None,
            }

        cfg = DummyConfig({
            'exchange': {'mode': 'testnet'},
            'governance': {'testnet_bridge_execution': {'enabled': True, 'mode': 'controlled_execute', 'require_controlled_rollout_execution': True}},
        })
        payload = {
            'approval_state': {'items': []},
            'controlled_rollout_execution': {'executed_count': 1},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'testnet_bridge_execute.db'))
            executed = execute_testnet_bridge_layer(copy.deepcopy(payload), db, config=cfg, exchange=BridgeExchangeStub(), bridge_runner=fake_bridge_runner)
            bridge = executed['testnet_bridge_execution']
            self.assertEqual(bridge['status'], 'controlled_execute')
            self.assertTrue(bridge['audit']['real_trade_execution'])
            self.assertEqual(bridge['open_status'], 'filled')
            self.assertEqual(bridge['close_status'], 'filled')
            self.assertEqual(calls[0]['symbol'], 'BTC/USDT')
            self.assertTrue(executed['workflow_state']['summary']['testnet_bridge_execution']['executed_this_round'])
            self.assertTrue(executed['workflow_state']['summary']['testnet_bridge_execution']['reconcile_completed'])
            self.assertTrue(executed['approval_state']['summary']['testnet_bridge_execution']['cleanup_completed'])

    def test_testnet_bridge_execution_evidence_surfaces_across_digest_runtime_and_overview(self):
        payload = {
            'workflow_state': {
                'item_states': [{
                    'item_id': 'playbook::ready',
                    'title': 'Ready observe item',
                    'action_type': 'joint_observe',
                    'risk_level': 'low',
                    'approval_required': False,
                    'requires_manual': False,
                    'workflow_state': 'ready',
                    'blocking_reasons': [],
                }],
                'summary': {},
            },
            'approval_state': {
                'items': [{
                    'approval_id': 'approval::ready',
                    'playbook_id': 'playbook::ready',
                    'title': 'Ready observe item',
                    'action_type': 'joint_observe',
                    'approval_state': 'pending',
                    'decision_state': 'ready',
                    'risk_level': 'low',
                    'approval_required': False,
                    'requires_manual': False,
                    'blocked_by': [],
                }],
                'summary': {},
            },
            'testnet_bridge_execution': {
                'schema_version': 'm5_testnet_bridge_execution_v1',
                'enabled': True,
                'mode': 'controlled_execute',
                'status': 'error',
                'plan_only': False,
                'symbol': 'BTC/USDT',
                'side': 'long',
                'open_status': 'filled',
                'close_status': 'submitted',
                'cleanup_needed': True,
                'residual_position_detected': True,
                'failure_compensation_hint': 'manual_testnet_cleanup_required',
                'blocking_reasons': [],
                'audit': {'real_trade_execution': True, 'dangerous_live_parameter_change': False},
                'result': {
                    'opened': True,
                    'closed': True,
                    'open_status': 'filled',
                    'close_status': 'submitted',
                    'cleanup_needed': True,
                    'residual_position_detected': True,
                    'cleanup_result': {'status': 'manual_required'},
                    'reconcile_summary': {
                        'open_order_confirmed': True,
                        'close_order_confirmed': False,
                        'residual_position_detected': True,
                        'cleanup_attempted': True,
                        'cleanup_succeeded': False,
                        'residual_quantity': 0.001,
                    },
                },
                'error': 'cleanup_required_but_cleanup_not_confirmed',
            },
            'adaptive_rollout_orchestration': {'summary': {'pass_count': 1}},
        }
        consumer = build_workflow_consumer_view(copy.deepcopy(payload))
        operator_digest = build_workflow_operator_digest(copy.deepcopy(payload))
        runtime_summary = build_runtime_orchestration_summary(copy.deepcopy(payload))
        overview = build_unified_workbench_overview(copy.deepcopy(payload))
        cards = build_dashboard_summary_cards(copy.deepcopy(payload))

        self.assertTrue(consumer['summary']['testnet_bridge_execution']['executed_this_round'])
        self.assertTrue(operator_digest['summary']['testnet_bridge_execution']['follow_up_required'])
        self.assertTrue(runtime_summary['summary']['testnet_bridge_execution']['follow_up_required'])
        self.assertTrue(overview['summary']['testnet_bridge_execution']['pending_exposure'])
        self.assertEqual(overview['lines']['rollout']['counts']['testnet_follow_up_required'], 1)
        self.assertEqual(cards['summary']['testnet_bridge_execution']['status'], 'error')
        self.assertEqual(cards['card_index']['execution_status']['metrics']['testnet_follow_up'], 1)

    def test_testnet_bridge_evidence_gate_blocks_alerts_and_readiness_on_residual_exposure(self):
        payload = {
            'workflow_state': {
                'item_states': [{
                    'item_id': 'playbook::ready',
                    'title': 'Ready observe item',
                    'action_type': 'joint_observe',
                    'risk_level': 'low',
                    'approval_required': False,
                    'requires_manual': False,
                    'workflow_state': 'ready',
                    'blocking_reasons': [],
                }],
                'summary': {'item_count': 1},
            },
            'approval_state': {
                'items': [{
                    'approval_id': 'approval::ready',
                    'playbook_id': 'playbook::ready',
                    'title': 'Ready observe item',
                    'action_type': 'joint_observe',
                    'approval_state': 'pending',
                    'decision_state': 'ready',
                    'risk_level': 'low',
                    'approval_required': False,
                    'requires_manual': False,
                    'blocked_by': [],
                }],
                'summary': {'pending_count': 0},
            },
            'rollout_executor': {'status': 'controlled', 'summary': {}},
            'controlled_rollout_execution': {'mode': 'state_apply', 'executed_count': 1, 'items': []},
            'auto_approval_execution': {'mode': 'controlled', 'executed_count': 1, 'items': []},
            'validation_gate': {'enabled': False},
            'testnet_bridge_execution': {
                'schema_version': 'm5_testnet_bridge_execution_v1',
                'enabled': True,
                'mode': 'controlled_execute',
                'status': 'error',
                'plan_only': False,
                'symbol': 'BTC/USDT',
                'side': 'long',
                'open_status': 'filled',
                'close_status': 'submitted',
                'cleanup_needed': True,
                'residual_position_detected': True,
                'failure_compensation_hint': 'manual_testnet_cleanup_required',
                'blocking_reasons': [],
                'audit': {'real_trade_execution': True, 'dangerous_live_parameter_change': False},
                'result': {
                    'opened': True,
                    'closed': True,
                    'open_status': 'filled',
                    'close_status': 'submitted',
                    'cleanup_needed': True,
                    'residual_position_detected': True,
                    'cleanup_result': {'status': 'manual_required'},
                    'reconcile_summary': {
                        'open_order_confirmed': True,
                        'close_order_confirmed': False,
                        'residual_position_detected': True,
                        'cleanup_attempted': True,
                        'cleanup_succeeded': False,
                        'residual_quantity': 0.001,
                    },
                },
                'error': 'cleanup_required_but_cleanup_not_confirmed',
            },
            'adaptive_rollout_orchestration': {'summary': {'pass_count': 1}},
        }
        alert_digest = build_workflow_alert_digest(copy.deepcopy(payload), max_items=10)
        overview = build_unified_workbench_overview(copy.deepcopy(payload), max_items=5)
        readiness = build_production_rollout_readiness(copy.deepcopy(payload), max_items=5)

        self.assertTrue(any(row['kind'] == 'testnet_bridge_evidence_gate' and row['severity'] == 'critical' for row in alert_digest['alerts']))
        self.assertEqual(alert_digest['summary']['testnet_bridge_evidence_gate']['status'], 'blocked')
        self.assertFalse(readiness['can_enable_low_intervention_runtime'])
        self.assertIn('testnet_bridge_pending_exposure', readiness['blocking_issues'])
        self.assertIn('testnet_bridge_residual_exposure', readiness['blocking_issues'])
        self.assertTrue(any(row['kind'] == 'clear_testnet_residual_exposure' for row in readiness['runbook_actions']))
        self.assertEqual(overview['summary']['testnet_bridge_evidence_gate']['status'], 'blocked')
        self.assertEqual(overview['lines']['rollout']['counts']['testnet_bridge_gate_blocked'], 1)
        self.assertEqual(overview['lines']['rollout']['counts']['testnet_recent_execute_success'], 0)

    def test_testnet_bridge_evidence_gate_marks_recent_clean_execute_ready(self):
        payload = {
            'workflow_state': {
                'item_states': [{
                    'item_id': 'playbook::ready',
                    'title': 'Ready observe item',
                    'action_type': 'joint_observe',
                    'risk_level': 'low',
                    'approval_required': False,
                    'requires_manual': False,
                    'workflow_state': 'ready',
                    'blocking_reasons': [],
                }],
                'summary': {'item_count': 1},
            },
            'approval_state': {
                'items': [{
                    'approval_id': 'approval::ready',
                    'playbook_id': 'playbook::ready',
                    'title': 'Ready observe item',
                    'action_type': 'joint_observe',
                    'approval_state': 'pending',
                    'decision_state': 'ready',
                    'risk_level': 'low',
                    'approval_required': False,
                    'requires_manual': False,
                    'blocked_by': [],
                }],
                'summary': {'pending_count': 0},
            },
            'rollout_executor': {'status': 'controlled', 'summary': {}},
            'controlled_rollout_execution': {'mode': 'state_apply', 'executed_count': 1, 'items': []},
            'auto_approval_execution': {'mode': 'controlled', 'executed_count': 1, 'items': []},
            'validation_gate': {'enabled': False},
            'testnet_bridge_execution': {
                'schema_version': 'm5_testnet_bridge_execution_v1',
                'enabled': True,
                'mode': 'controlled_execute',
                'status': 'controlled_execute',
                'plan_only': False,
                'symbol': 'BTC/USDT',
                'side': 'long',
                'open_status': 'filled',
                'close_status': 'filled',
                'cleanup_needed': False,
                'residual_position_detected': False,
                'blocking_reasons': [],
                'audit': {'real_trade_execution': True, 'dangerous_live_parameter_change': False},
                'result': {
                    'opened': True,
                    'closed': True,
                    'open_status': 'filled',
                    'close_status': 'filled',
                    'cleanup_needed': False,
                    'residual_position_detected': False,
                    'cleanup_result': {'status': 'flattened'},
                    'reconcile_summary': {
                        'open_order_confirmed': True,
                        'close_order_confirmed': True,
                        'residual_position_detected': False,
                        'cleanup_attempted': False,
                        'cleanup_succeeded': True,
                        'residual_quantity': 0.0,
                    },
                },
            },
            'adaptive_rollout_orchestration': {'summary': {'pass_count': 1}},
        }
        alert_digest = build_workflow_alert_digest(copy.deepcopy(payload), max_items=10)
        overview = build_unified_workbench_overview(copy.deepcopy(payload), max_items=5)
        readiness = build_production_rollout_readiness(copy.deepcopy(payload), max_items=5)

        self.assertFalse(any(row['kind'] == 'testnet_bridge_evidence_gate' for row in alert_digest['alerts']))
        self.assertEqual(overview['summary']['testnet_bridge_evidence_gate']['status'], 'ready')
        self.assertTrue(overview['summary']['testnet_bridge_evidence_gate']['recent_execute_succeeded'])
        self.assertEqual(overview['lines']['rollout']['counts']['testnet_recent_execute_success'], 1)
        self.assertEqual(overview['lines']['rollout']['counts']['testnet_bridge_gate_blocked'], 0)
        self.assertTrue(readiness['summary']['testnet_bridge_evidence_gate']['can_enable_low_intervention'])
        self.assertNotIn('testnet_bridge_no_recent_execution_evidence', readiness['blocking_issues'])

    def test_execute_adaptive_rollout_orchestration_runs_testnet_bridge_after_controlled_rollout(self):
        class DummyConfig:
            def __init__(self, mapping):
                self.mapping = mapping
                self.exchange_mode = 'testnet'
                self.position_mode = 'hedge'
                self.symbols = ['BTC/USDT']
                self.all = mapping
            def get(self, key, default=None):
                parts = key.split('.')
                value = self.mapping
                for part in parts:
                    if isinstance(value, dict) and part in value:
                        value = value[part]
                    else:
                        return default
                return value

        payload = {
            'workflow_state': {
                'item_states': [{
                    'item_id': 'playbook::eligible',
                    'title': 'Eligible low risk item',
                    'action_type': 'joint_observe',
                    'risk_level': 'low',
                    'approval_required': False,
                    'requires_manual': False,
                    'workflow_state': 'ready',
                    'blocking_reasons': [],
                }],
                'summary': {'item_count': 1},
            },
            'approval_state': {
                'items': [{
                    'approval_id': 'approval::eligible',
                    'playbook_id': 'playbook::eligible',
                    'title': 'Eligible low risk item',
                    'action_type': 'joint_observe',
                    'approval_state': 'pending',
                    'decision_state': 'pending',
                    'risk_level': 'low',
                    'approval_required': False,
                    'requires_manual': False,
                    'blocked_by': [],
                }],
                'summary': {'pending_count': 1},
            },
            'validation_gate': {'enabled': False},
        }
        cfg = DummyConfig({
            'exchange': {'mode': 'testnet'},
            'governance': {
                'auto_approval_execution': {'enabled': True, 'mode': 'controlled'},
                'controlled_rollout_execution': {'enabled': True, 'mode': 'state_apply', 'auto_promote_ready_candidates': True, 'allowed_action_types': ['joint_observe']},
                'testnet_bridge_execution': {'enabled': True, 'mode': 'controlled_execute', 'require_controlled_rollout_execution': True},
                'adaptive_rollout_orchestration': {'enforce_production_gate': False},
            },
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'adaptive_orchestration_bridge.db'))
            import analytics.helper as helper_module
            old_bridge = helper_module.execute_testnet_bridge_layer
            def fake_bridge(payload, db, *, config=None, settings=None, replay_source='workflow_ready', bridge_runner=None, exchange=None):
                payload = copy.deepcopy(payload)
                payload['testnet_bridge_execution'] = {
                    'schema_version': 'm5_testnet_bridge_execution_v1',
                    'enabled': True,
                    'mode': 'controlled_execute',
                    'status': 'controlled_execute',
                    'plan_only': False,
                    'gating': {'controlled_rollout_executed_count': 1},
                    'blocking_reasons': [],
                    'audit': {'real_trade_execution': True, 'dangerous_live_parameter_change': False},
                    'result': {'opened': True, 'closed': True, 'open_status': 'filled', 'close_status': 'filled'},
                }
                return payload
            helper_module.execute_testnet_bridge_layer = fake_bridge
            try:
                result = execute_adaptive_rollout_orchestration(copy.deepcopy(payload), db, config=cfg, replay_source='test_orchestration_bridge')
            finally:
                helper_module.execute_testnet_bridge_layer = old_bridge
            orchestration = result['adaptive_rollout_orchestration']
            self.assertEqual(result['testnet_bridge_execution']['status'], 'controlled_execute')
            self.assertEqual(orchestration['summary']['testnet_bridge_status'], 'controlled_execute')
            self.assertTrue(orchestration['summary']['testnet_bridge_real_trade_execution'])

    def test_execute_adaptive_rollout_orchestration_can_bypass_production_gate_when_disabled(self):
        class DummyConfig:
            def __init__(self, mapping):
                self.mapping = mapping
            def get(self, key, default=None):
                parts = key.split('.')
                value = self.mapping
                for part in parts:
                    if isinstance(value, dict) and part in value:
                        value = value[part]
                    else:
                        return default
                return value

        payload = {
            'workflow_state': {
                'item_states': [
                    {
                        'item_id': 'playbook::eligible',
                        'title': 'Eligible low risk item',
                        'action_type': 'joint_observe',
                        'risk_level': 'low',
                        'approval_required': False,
                        'requires_manual': False,
                        'workflow_state': 'ready',
                        'blocking_reasons': [],
                    },
                    {
                        'item_id': 'playbook::manual',
                        'title': 'Manual gate item',
                        'action_type': 'joint_expand_guarded',
                        'risk_level': 'high',
                        'approval_required': True,
                        'requires_manual': True,
                        'workflow_state': 'blocked_by_approval',
                        'blocking_reasons': [],
                    },
                ],
                'summary': {'item_count': 2},
            },
            'approval_state': {
                'items': [
                    {'approval_id': 'approval::eligible', 'playbook_id': 'playbook::eligible', 'title': 'Eligible low risk item', 'action_type': 'joint_observe', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'low', 'approval_required': False, 'requires_manual': False, 'blocked_by': []},
                    {'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'high', 'approval_required': True, 'requires_manual': True, 'blocked_by': []},
                ],
                'summary': {'pending_count': 2},
            },
        }
        cfg = DummyConfig({
            'governance': {
                'auto_approval_execution': {
                    'enabled': True,
                    'mode': 'controlled',
                },
                'adaptive_rollout_orchestration': {
                    'enforce_production_gate': False,
                },
            }
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'adaptive_gate_bypass.db'))
            result = execute_adaptive_rollout_orchestration(copy.deepcopy(payload), db, config=cfg, replay_source='test_gate_bypass')
            orchestration = result['adaptive_rollout_orchestration']
            self.assertFalse(orchestration['summary']['gate_enforced'])
            self.assertFalse(orchestration['summary']['gate_blocked'])
            self.assertEqual(result['auto_approval_execution']['executed_count'], 1)
            self.assertEqual(db.get_approval_state('approval::eligible')['state'], 'approved')
            self.assertIsNone(db.get_approval_state('approval::manual'))


class TestUnifiedWorkflowStateMachine(unittest.TestCase):
    def test_merge_persisted_approval_state_builds_state_machine_semantics(self):
        payload = {
            'workflow_state': {
                'item_states': [{
                    'item_id': 'playbook::queue',
                    'title': 'Queue item',
                    'action_type': 'joint_expand_guarded',
                    'workflow_state': 'pending',
                    'queue_progression': {'status': 'ready_to_queue', 'dispatch_route': 'manual_review_queue', 'next_transition': 'await_manual_approval'},
                    'current_rollout_stage': 'observe',
                    'target_rollout_stage': 'guarded',
                    'blocking_reasons': [],
                }],
                'summary': {},
            },
            'approval_state': {
                'items': [{
                    'approval_id': 'approval::queue',
                    'playbook_id': 'playbook::queue',
                    'title': 'Queue item',
                    'action_type': 'joint_expand_guarded',
                    'approval_state': 'pending',
                    'decision_state': 'pending',
                    'queue_progression': {'status': 'ready_to_queue', 'dispatch_route': 'manual_review_queue', 'next_transition': 'await_manual_approval'},
                    'rollout_stage': 'observe',
                    'target_rollout_stage': 'guarded',
                    'blocked_by': [],
                }],
                'summary': {},
            },
        }
        merged = merge_persisted_approval_state(payload, [{
            'item_id': 'approval::queue',
            'state': 'pending',
            'decision': 'pending',
            'workflow_state': 'queued',
            'updated_at': '2026-03-27 10:00:00',
            'details': {
                'queue_progression': {'status': 'ready_to_queue', 'dispatch_route': 'manual_review_queue', 'next_transition': 'await_manual_approval'},
                'dispatch_route': 'manual_review_queue',
                'next_transition': 'await_manual_approval',
            },
        }])
        approval_item = merged['approval_state']['items'][0]
        workflow_item = merged['workflow_state']['item_states'][0]
        self.assertEqual(approval_item['state_machine']['workflow_state'], 'queued')
        self.assertEqual(approval_item['state_machine']['phase'], 'queue')
        self.assertTrue(approval_item['state_machine']['retryable'])
        self.assertEqual(workflow_item['state_machine']['dispatch_route'], 'manual_review_queue')
        self.assertEqual(workflow_item['state_machine']['operator_action_policy']['action'], 'observe_only_followup')
        self.assertEqual(merged['workflow_state']['summary']['queued_count'], 1)
        self.assertEqual(merged['workflow_state']['summary']['state_machine']['rollback_candidate_count'], 1)
        self.assertEqual(merged['workflow_state']['summary']['state_machine']['operator_action_counts']['observe_only_followup'], 1)


    def test_merge_persisted_approval_state_replays_validation_gate_into_workflow_summary(self):
        payload = {
            'workflow_state': {
                'item_states': [{
                    'item_id': 'playbook::stage',
                    'title': 'Stage item',
                    'action_type': 'joint_stage_prepare',
                    'workflow_state': 'ready',
                    'blocking_reasons': [],
                }],
                'summary': {},
            },
            'approval_state': {
                'items': [{
                    'approval_id': 'approval::stage',
                    'playbook_id': 'playbook::stage',
                    'title': 'Stage item',
                    'action_type': 'joint_stage_prepare',
                    'approval_state': 'pending',
                    'decision_state': 'ready',
                    'blocked_by': [],
                }],
                'summary': {},
            },
        }
        merged = merge_persisted_approval_state(payload, [{
            'item_id': 'approval::stage',
            'state': 'pending',
            'decision': 'pending',
            'workflow_state': 'ready',
            'updated_at': '2026-03-28 12:00:00',
            'details': {
                'execution_status': 'blocked',
                'auto_advance_gate': {
                    'allowed': False,
                    'blockers': ['validation_gate:not_ready'],
                    'validation_gate': {
                        'enabled': True,
                        'ready': False,
                        'freeze_auto_advance': True,
                        'rollback_on_regression': True,
                        'reasons': ['missing_required:testnet_bridge_controlled_execute'],
                    },
                },
                'rollback_gate': {
                    'candidate': True,
                    'triggered': ['validation_gate_regressed'],
                    'validation_gate': {
                        'enabled': True,
                        'ready': False,
                        'freeze_auto_advance': True,
                        'rollback_on_regression': True,
                        'reasons': ['missing_required:testnet_bridge_controlled_execute'],
                    },
                },
                'stage_loop': {'loop_state': 'rollback_prepare', 'recommended_action': 'rollback_prepare'},
            },
        }])
        approval_item = merged['approval_state']['items'][0]
        workflow_item = merged['workflow_state']['item_states'][0]
        self.assertFalse(approval_item['validation_gate']['ready'])
        self.assertTrue(workflow_item['rollback_gate']['candidate'])
        self.assertEqual(workflow_item['stage_loop']['loop_state'], 'rollback_prepare')

    def test_database_get_approval_state_includes_state_machine_details(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'state_machine.db'))
            row = db.upsert_approval_state(
                item_id='approval::stage',
                approval_type='joint_stage_prepare',
                target='playbook::stage',
                title='Stage item',
                decision='pending',
                state='ready',
                workflow_state='ready',
                details={
                    'dispatch_route': 'stage_metadata_apply',
                    'next_transition': 'promote_to_target_stage',
                    'rollout_stage': 'observe',
                    'target_rollout_stage': 'guarded_prepare',
                    'rollback_hint': 'revert_stage_metadata_to_previous_stage',
                },
                replay_source='unit-test',
            )
            self.assertEqual(row['details']['state_machine']['phase'], 'queue')
            fetched = db.get_approval_state('approval::stage')
            self.assertEqual(fetched['details']['state_machine']['dispatch_route'], 'stage_metadata_apply')
            self.assertTrue(fetched['details']['state_machine']['rollback_candidate'])
            self.assertEqual(fetched['details']['state_machine']['operator_action_policy']['action'], 'review_schedule')
            self.assertEqual(fetched['details']['execution_status'], 'applied')
            self.assertEqual(fetched['details']['state_machine']['execution_status'], 'applied')

    def test_database_transition_journal_tracks_from_to_trigger_actor_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / 'transition_journal.db'))
            db.upsert_approval_state(
                item_id='approval::journal', approval_type='joint_stage_prepare', target='playbook::journal', title='Journal item',
                decision='pending', state='pending', workflow_state='pending', details={'dispatch_route': 'manual_review_queue'}, replay_source='unit-test-bootstrap',
            )
            db.upsert_approval_state(
                item_id='approval::journal', approval_type='joint_stage_prepare', target='playbook::journal', title='Journal item',
                decision='pending', state='ready', workflow_state='ready', reason='stage prepared', actor='system:test', replay_source='unit-test-transition',
                details={'dispatch_route': 'stage_metadata_apply', 'transition_rule': 'promote_to_target_stage', 'rollout_stage': 'observe', 'target_rollout_stage': 'guarded'},
            )
            rows = db.get_recent_transition_journal(item_id='approval::journal', limit=5)
            self.assertTrue(rows)
            latest = rows[0]
            self.assertEqual(latest['from']['workflow_state'], 'pending')
            self.assertEqual(latest['to']['workflow_state'], 'ready')
            self.assertEqual(latest['trigger'], 'promote_to_target_stage')
            self.assertEqual(latest['actor'], 'system:test')
            self.assertEqual(latest['source'], 'unit-test-transition')
            self.assertIn('workflow_state', latest['changed_fields'])


    def test_backtest_auto_promotion_summary_api_returns_execution_summary(self):
        import dashboard.api as dashboard_api_module
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = Database(str(Path(tmpdir) / 'api_auto_promotion_summary.db'))
            test_db.upsert_approval_state(
                item_id='approval::promo',
                approval_type='joint_stage_prepare',
                target='playbook::promo',
                title='Promotion item',
                decision='pending',
                state='approved',
                workflow_state='ready',
                reason='controlled auto promotion executed',
                actor='system:test-api-auto-promotion',
                replay_source='unit_test_api_auto_promotion',
                details={
                    'auto_promotion_execution': {
                        'before': {'state': 'pending', 'workflow_state': 'ready', 'rollout_stage': 'guarded_prepare'},
                        'after': {'state': 'approved', 'workflow_state': 'ready', 'rollout_stage': 'controlled_apply'},
                        'reason_codes': ['auto_advance_allowed'],
                        'candidate_summary': {'risk_label': 'low'},
                        'event_log': [{'event_type': 'controlled_rollout_state_apply', 'actor': 'system:test-api-auto-promotion', 'source': 'unit_test_api_auto_promotion', 'created_at': '2026-03-28T00:00:00Z'}],
                        'rollback_hint': 'revert_stage_metadata_to_previous_stage',
                    },
                    'rollback_gate': {'candidate': True, 'triggered': ['review_overdue']},
                },
                event_type='controlled_rollout_state_apply',
            )
            old_db = dashboard_api_module.db
            dashboard_api_module.db = test_db
            try:
                with dashboard_api_module.app.test_client() as client:
                    response = client.get('/api/backtest/auto-promotion-summary?limit=5')
                    self.assertEqual(response.status_code, 200)
                    payload = response.get_json()
                    self.assertEqual(payload['view'], 'auto_promotion_execution_summary')
                    self.assertEqual(payload['data']['event_count'], 1)
                    self.assertEqual(payload['data']['rollback_review_candidate_count'], 1)
                    self.assertEqual(payload['data']['stage_transition_counts']['guarded_prepare->controlled_apply'], 1)
            finally:
                dashboard_api_module.db = old_db

    def test_auto_promotion_review_queues_api_returns_post_promotion_and_rollback_review_views(self):
        import dashboard.api as dashboard_api_module
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = Database(str(Path(tmpdir) / 'api_auto_promotion_review_queues.db'))
            test_db.upsert_approval_state(
                item_id='approval::promo',
                approval_type='joint_stage_prepare',
                target='playbook::promo',
                title='Promotion item',
                decision='pending',
                state='approved',
                workflow_state='ready',
                reason='controlled auto promotion executed',
                actor='system:test-api-auto-promotion-queues',
                replay_source='unit_test_api_auto_promotion_queues',
                details={
                    'review_due_at': '2026-03-29T00:00:00Z',
                    'auto_promotion_execution': {
                        'before': {'state': 'pending', 'workflow_state': 'ready', 'rollout_stage': 'guarded_prepare'},
                        'after': {'state': 'approved', 'workflow_state': 'ready', 'rollout_stage': 'controlled_apply'},
                        'reason_codes': ['auto_advance_allowed'],
                        'candidate_summary': {'risk_label': 'low'},
                        'event_log': [{'event_type': 'controlled_rollout_state_apply', 'actor': 'system:test-api-auto-promotion-queues', 'source': 'unit_test_api_auto_promotion_queues', 'created_at': '2026-03-28T00:00:00Z'}],
                        'rollback_hint': 'revert_stage_metadata_to_previous_stage',
                    },
                    'rollback_gate': {'candidate': True, 'triggered': ['review_overdue']},
                },
                event_type='controlled_rollout_state_apply',
            )
            old_db = dashboard_api_module.db
            dashboard_api_module.db = test_db
            try:
                with dashboard_api_module.app.test_client() as client:
                    response = client.get('/api/backtest/auto-promotion-review-queues?limit=5')
                    self.assertEqual(response.status_code, 200)
                    payload = response.get_json()
                    self.assertEqual(payload['view'], 'auto_promotion_review_queues')
                    self.assertEqual(payload['data']['summary']['post_promotion_review_queue_count'], 0)
                    self.assertEqual(payload['data']['summary']['rollback_review_queue_count'], 1)
                    self.assertEqual(payload['data']['review_queues']['rollback_review_queue'][0]['recommended_action'], 'prepare_rollback_review')
            finally:
                dashboard_api_module.db = old_db

    def test_approval_state_machine_api_returns_phase_summary(self):
        import dashboard.api as dashboard_api
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = Database(str(Path(tmpdir) / 'api_state_machine.db'))
            old_db = dashboard_api.db
            dashboard_api.db = test_db
            try:
                test_db.upsert_approval_state(
                    item_id='approval::queue',
                    approval_type='joint_expand_guarded',
                    target='playbook::queue',
                    title='Queue item',
                    decision='pending',
                    state='pending',
                    workflow_state='queued',
                    details={'queue_progression': {'status': 'ready_to_queue', 'dispatch_route': 'manual_review_queue'}, 'auto_advance_gate': {'validation_gate': {'enabled': True, 'ready': False, 'freeze_auto_advance': True, 'rollback_on_regression': True, 'reasons': ['missing_required:testnet_bridge_controlled_execute']}}, 'rollback_gate': {'candidate': True, 'triggered': ['validation_gate_regressed'], 'validation_gate': {'enabled': True, 'ready': False, 'freeze_auto_advance': True, 'rollback_on_regression': True, 'reasons': ['missing_required:testnet_bridge_controlled_execute']}}},
                    replay_source='unit-test',
                )
                client = app.test_client()
                resp = client.get('/api/approvals/state-machine?limit=10')
                self.assertEqual(resp.status_code, 200)
                payload = resp.get_json()
                self.assertTrue(payload['success'])
                self.assertEqual(payload['summary']['phase_counts']['queue'], 1)
                self.assertEqual(payload['summary']['workflow_state_counts']['queued'], 1)
                self.assertEqual(payload['summary']['execution_status_counts']['queued'], 1)
                self.assertEqual(payload['summary']['validation_status_counts']['frozen'], 1)
                self.assertEqual(payload['summary']['validation_freeze_reason_counts']['missing_required:testnet_bridge_controlled_execute'], 1)
                self.assertEqual(payload['summary']['rollback_trigger_counts']['validation_gate_regressed'], 1)
                self.assertEqual(payload['data'][0]['execution_status'], 'queued')
                self.assertFalse(payload['data'][0]['validation_gate']['ready'])
                self.assertEqual(payload['data'][0]['state_machine']['workflow_state'], 'queued')
            finally:
                dashboard_api.db = old_db


class TestUnifiedWorkbenchOverviewAPI(unittest.TestCase):
    def test_auto_promotion_review_items_api_supports_queue_filters(self):
        import dashboard.api as dashboard_api_module

        class StubBacktester:
            def run_all(self, symbols):
                return {'summary': {'symbols': len(symbols)}, 'symbols': [{'symbol': 'BTC/USDT'}], 'calibration_report': {}}

        with app.test_client() as client:
            original_backtester = dashboard_api_module.backtester
            original_persist = dashboard_api_module._persist_workflow_approval_payload
            original_export = dashboard_api_module.export_calibration_payload
            original_filter = dashboard_api_module.build_auto_promotion_review_queue_filter_view
            try:
                dashboard_api_module.backtester = StubBacktester()
                dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': {}
                dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='auto_promotion_review_queue_filter_api': payload
                dashboard_api_module.build_auto_promotion_review_queue_filter_view = lambda payload, **kwargs: {
                    'schema_version': 'm5_auto_promotion_review_queue_filter_view_v1',
                    'summary': {'matched_count': 1},
                    'applied_filters': {'queue_kinds': ['rollback_review_queue'], 'due_statuses': ['overdue'], 'rollback_triggers': ['review_overdue']},
                    'items': [{'item_id': 'playbook::promo', 'queue_kind': 'rollback_review_queue', 'next_step': 'prepare_rollback_review'}],
                }
                response = client.get('/api/backtest/auto-promotion-review-items?queue_kind=rollback_review_queue&due_status=overdue&rollback_trigger=review_overdue&now=2026-03-30T00:00:00Z&limit=5')
                self.assertEqual(response.status_code, 200)
                body = response.get_json()
                self.assertEqual(body['view'], 'auto_promotion_review_queue_filter_view')
                self.assertEqual(body['data']['summary']['matched_count'], 1)
                self.assertEqual(body['data']['items'][0]['queue_kind'], 'rollback_review_queue')
                self.assertEqual(body['data']['items'][0]['next_step'], 'prepare_rollback_review')
            finally:
                dashboard_api_module.backtester = original_backtester
                dashboard_api_module._persist_workflow_approval_payload = original_persist
                dashboard_api_module.export_calibration_payload = original_export
                dashboard_api_module.build_auto_promotion_review_queue_filter_view = original_filter

    def test_auto_promotion_review_detail_api_returns_follow_up_explanation(self):
        import dashboard.api as dashboard_api_module

        class StubBacktester:
            def run_all(self, symbols):
                return {'summary': {'symbols': len(symbols)}, 'symbols': [{'symbol': 'BTC/USDT'}], 'calibration_report': {}}

        with app.test_client() as client:
            original_backtester = dashboard_api_module.backtester
            original_persist = dashboard_api_module._persist_workflow_approval_payload
            original_export = dashboard_api_module.export_calibration_payload
            original_detail = dashboard_api_module.build_auto_promotion_review_queue_detail_view
            try:
                dashboard_api_module.backtester = StubBacktester()
                dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': {}
                dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='auto_promotion_review_queue_detail_api': payload
                dashboard_api_module.build_auto_promotion_review_queue_detail_view = lambda payload, **kwargs: {
                    'schema_version': 'm5_auto_promotion_review_queue_detail_view_v1',
                    'found': True,
                    'item': {'item_id': 'playbook::promo', 'queue_kind': 'rollback_review_queue', 'next_step': 'prepare_rollback_review'},
                    'summary': {'item_id': 'playbook::promo', 'queue_kind': 'rollback_review_queue', 'next_step': 'prepare_rollback_review', 'rollback_triggered': ['review_overdue']},
                    'alternatives': [],
                }
                response = client.get('/api/backtest/auto-promotion-review-detail?item_id=playbook::promo&queue_kind=rollback_review_queue&now=2026-03-30T00:00:00Z')
                self.assertEqual(response.status_code, 200)
                body = response.get_json()
                self.assertEqual(body['view'], 'auto_promotion_review_queue_detail_view')
                self.assertEqual(body['data']['summary']['queue_kind'], 'rollback_review_queue')
                self.assertEqual(body['data']['summary']['next_step'], 'prepare_rollback_review')
                self.assertIn('review_overdue', body['data']['summary']['rollback_triggered'])
            finally:
                dashboard_api_module.backtester = original_backtester
                dashboard_api_module._persist_workflow_approval_payload = original_persist
                dashboard_api_module.export_calibration_payload = original_export
                dashboard_api_module.build_auto_promotion_review_queue_detail_view = original_detail

    def test_backtest_unified_workbench_overview_api_returns_three_line_snapshot(self):
        import dashboard.api as dashboard_api_module
        original_run_all = dashboard_api_module.backtester.run_all
        original_persist = dashboard_api_module._persist_workflow_approval_payload
        original_export = dashboard_api_module.export_calibration_payload
        try:
            dashboard_api_module.backtester.run_all = lambda symbols: {'calibration_report': {'summary': {}, 'workflow_ready': {}}}
            dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': {
                'workflow_state': {
                    'item_states': [
                        {'item_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'risk_level': 'high', 'approval_required': True, 'requires_manual': True, 'workflow_state': 'blocked_by_approval', 'blocking_reasons': []},
                        {'item_id': 'playbook::recover', 'title': 'Recover item', 'action_type': 'joint_stage_prepare', 'risk_level': 'critical', 'approval_required': False, 'requires_manual': False, 'workflow_state': 'execution_failed', 'blocking_reasons': ['critical_risk'], 'state_machine': _build_state_machine_semantics(item_id='approval::recover', approval_state='pending', workflow_state='execution_failed', execution_status='error', retryable=False, blocked_by=['critical_risk'])},
                    ],
                    'summary': {'item_count': 2},
                },
                'approval_state': {
                    'items': [
                        {'approval_id': 'approval::manual', 'playbook_id': 'playbook::manual', 'title': 'Manual gate item', 'action_type': 'joint_expand_guarded', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'high', 'approval_required': True, 'requires_manual': True, 'blocked_by': []},
                        {'approval_id': 'approval::recover', 'playbook_id': 'playbook::recover', 'title': 'Recover item', 'action_type': 'joint_stage_prepare', 'approval_state': 'pending', 'decision_state': 'pending', 'risk_level': 'critical', 'approval_required': False, 'requires_manual': False, 'blocked_by': ['critical_risk']},
                    ],
                    'summary': {'pending_count': 2},
                },
                'rollout_executor': {'status': 'controlled', 'summary': {}},
                'controlled_rollout_execution': {'mode': 'state_apply', 'executed_count': 0, 'items': []},
                'auto_approval_execution': {'mode': 'controlled', 'executed_count': 0, 'items': []},
            }
            dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='unified_workbench_overview_api': payload
            client = dashboard_api_module.app.test_client()
            response = client.get('/api/backtest/unified-workbench-overview?max_items=2')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'unified_workbench_overview')
            self.assertEqual(payload['data']['schema_version'], 'm5_unified_workbench_overview_v1')
            self.assertIn('approval', payload['data']['lines'])
            self.assertIn('rollout', payload['data']['lines'])
            self.assertIn('recovery', payload['data']['lines'])
        finally:
            dashboard_api_module.backtester.run_all = original_run_all
            dashboard_api_module._persist_workflow_approval_payload = original_persist
            dashboard_api_module.export_calibration_payload = original_export

    def test_backtest_calibration_report_api_supports_unified_workbench_overview(self):
        import dashboard.api as dashboard_api_module
        original_run_all = dashboard_api_module.backtester.run_all
        original_persist = dashboard_api_module._persist_workflow_approval_payload
        original_export = dashboard_api_module.export_calibration_payload
        try:
            dashboard_api_module.backtester.run_all = lambda symbols: {'symbols': ['BTC-USDT'], 'calibration_report': {'summary': {'trade_count': 1, 'calibration_ready': True}}}
            dashboard_api_module.export_calibration_payload = lambda report, view='workflow_ready': {
                'workflow_state': {'item_states': [], 'summary': {}},
                'approval_state': {'items': [], 'summary': {}},
                'rollout_executor': {'status': 'disabled', 'summary': {}},
                'controlled_rollout_execution': {'mode': 'disabled', 'items': []},
                'auto_approval_execution': {'mode': 'disabled', 'items': []},
            }
            dashboard_api_module._persist_workflow_approval_payload = lambda payload, replay_source='calibration_report:unified_workbench_overview': payload
            client = dashboard_api_module.app.test_client()
            response = client.get('/api/backtest/calibration-report?view=unified_workbench_overview')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['view'], 'unified_workbench_overview')
            self.assertEqual(payload['data']['schema_version'], 'm5_unified_workbench_overview_v1')
            self.assertEqual(payload['summary']['unified_workbench_overview'], payload['data']['summary'])
        finally:
            dashboard_api_module.backtester.run_all = original_run_all
            dashboard_api_module._persist_workflow_approval_payload = original_persist
            dashboard_api_module.export_calibration_payload = original_export


class TestQualityScalingPipeline(unittest.TestCase):
    """Integration tests for end-to-end quality scaling pipeline."""

    def setUp(self):
        self.config = Config()
        self.db = Database('data/test_quality.db')

    def tearDown(self):
        if os.path.exists('data/test_quality.db'):
            os.remove('data/test_quality.db')

    def test_risk_manager_can_open_accepts_signal_kwarg(self):
        """can_open_position now accepts signal= kwarg for quality scaling."""
        risk_mgr = RiskManager(self.config, self.db)
        from signals import Signal
        sig = Signal(symbol='BTC/USDT', signal_type='buy', price=50000, strength=75, reasons=['test'], strategies_triggered=['x', 'y', 'z'])
        # Should not raise TypeError about unexpected keyword argument
        can_open, reason, details = risk_mgr.can_open_position('BTC/USDT', side='long', signal_id=901, signal=sig)
        self.assertIsNotNone(details)
        # adaptive_risk_snapshot should be present even on early-return
        self.assertIn('adaptive_risk_snapshot', details)

    def test_quality_bucket_high_boosts_entry_ratio(self):
        """High quality signal (strength>=60, strategies>=3) uses high_quality_multiplier."""
        from core.risk_budget import compute_entry_plan, derive_quality_bucket
        from signals import Signal
        sig = Signal(symbol='BTC/USDT', signal_type='buy', price=50000, strength=75, reasons=['ma'], strategies_triggered=['a', 'b', 'c'])
        self.assertEqual(derive_quality_bucket(sig), 'high')
        plan = compute_entry_plan(
            total_balance=10000, free_balance=9000,
            current_total_margin=0, current_symbol_margin=0,
            risk_budget={
                'base_entry_margin_ratio': 0.08,
                'quality_scaling_enabled': True,
                'high_quality_multiplier': 1.15,
                'low_quality_multiplier': 0.75,
            },
            signal=sig
        )
        # 0.08 * 1.15 = 0.092
        self.assertAlmostEqual(plan['effective_entry_margin_ratio'], 0.092, places=4)
        self.assertEqual(plan['quality_bucket'], 'high')
        self.assertEqual(plan['quality_multiplier'], 1.15)

    def test_quality_bucket_low_reduces_entry_ratio(self):
        """Low quality signal (strength<=30, strategies<=1) uses low_quality_multiplier."""
        from core.risk_budget import compute_entry_plan, derive_quality_bucket
        from signals import Signal
        sig = Signal(symbol='BTC/USDT', signal_type='buy', price=50000, strength=20, reasons=['test'], strategies_triggered=['test'])
        self.assertEqual(derive_quality_bucket(sig), 'low')
        plan = compute_entry_plan(
            total_balance=10000, free_balance=9000,
            current_total_margin=0, current_symbol_margin=0,
            risk_budget={
                'base_entry_margin_ratio': 0.08,
                'quality_scaling_enabled': True,
                'high_quality_multiplier': 1.15,
                'low_quality_multiplier': 0.75,
            },
            signal=sig
        )
        # 0.08 * 0.75 = 0.06
        self.assertAlmostEqual(plan['effective_entry_margin_ratio'], 0.06, places=4)
        self.assertEqual(plan['quality_bucket'], 'low')
        self.assertEqual(plan['quality_multiplier'], 0.75)

    def test_quality_scaling_disabled_uses_base_entry(self):
        """When quality_scaling_enabled=False, always uses base_entry_margin_ratio."""
        from core.risk_budget import compute_entry_plan
        from signals import Signal
        sig = Signal(symbol='BTC/USDT', signal_type='buy', price=50000, strength=75, reasons=['x'], strategies_triggered=['a', 'b', 'c'])
        plan = compute_entry_plan(
            total_balance=10000, free_balance=9000,
            current_total_margin=0, current_symbol_margin=0,
            risk_budget={
                'base_entry_margin_ratio': 0.08,
                'quality_scaling_enabled': False,
                'high_quality_multiplier': 1.15,
                'low_quality_multiplier': 0.75,
            },
            signal=sig
        )
        # No multiplier applied
        self.assertAlmostEqual(plan['effective_entry_margin_ratio'], 0.08, places=4)
        self.assertEqual(plan['quality_bucket'], 'high')

    def test_adaptive_risk_snapshot_includes_quality_bucket(self):
        """build_risk_effective_snapshot surfaces quality_bucket in its output."""
        from core.regime_policy import build_risk_effective_snapshot
        from signals import Signal
        sig = Signal(symbol='BTC/USDT', signal_type='buy', price=50000, strength=75, reasons=['x'], strategies_triggered=['a', 'b', 'c'])
        snapshot = build_risk_effective_snapshot(self.config, 'BTC/USDT', signal=sig)
        self.assertIn('quality_bucket', snapshot)
        self.assertEqual(snapshot['quality_bucket'], 'high')
        self.assertIn('quality_scaling_enabled', snapshot)


class TestCloseOutcomeDigest(unittest.TestCase):
    def setUp(self):
        self.db = Database('data/test_close_outcome_digest.db')

    def tearDown(self):
        if os.path.exists('data/test_close_outcome_digest.db'):
            os.remove('data/test_close_outcome_digest.db')

    def _record_closed_trade(self, *, symbol, entry_price, exit_price, pnl, signal_id, reason, strategy_tag='ema'):
        trade_id = self.db.record_trade(
            symbol=symbol, side='long', entry_price=entry_price, quantity=0.1, leverage=5,
            signal_id=signal_id, layer_no=1, root_signal_id=signal_id,
            plan_context={'layer_no': 1, 'root_signal_id': signal_id, 'strategy_tags': [strategy_tag]}
        )
        self.db.reconcile_trade_close(
            trade_id,
            {'exit_price': exit_price, 'pnl': pnl, 'close_time': f'2026-03-30T00:0{signal_id % 10}:00', 'source': 'exchange_fill', 'fills': [{'id': f'f{signal_id}'}]},
            reason=reason,
        )

    def test_build_close_outcome_digest_aggregates_core_dimensions(self):
        from analytics.helper import build_close_outcome_digest
        trades = [
            {'id': 1, 'symbol': 'BTC/USDT', 'status': 'closed', 'close_time': '2026-03-30T00:02:00', 'close_decision': 'win', 'outcome_quality': 'positive', 'close_reason_category': 'take_profit', 'regime_tag': 'trend', 'policy_tag': 'policy_v1', 'pnl': 12.5, 'return_pct': 1.2},
            {'id': 2, 'symbol': 'ETH/USDT', 'status': 'closed', 'close_time': '2026-03-30T00:03:00', 'close_decision': 'loss', 'outcome_quality': 'bounded_loss', 'close_reason_category': 'stop_loss', 'regime_tag': 'range', 'policy_tag': 'policy_v2', 'pnl': -5.0, 'return_pct': -0.4},
            {'id': 3, 'symbol': 'BTC/USDT', 'status': 'closed', 'close_time': '2026-03-30T00:04:00', 'close_decision': 'win', 'outcome_quality': 'positive', 'close_reason_category': 'take_profit', 'regime_tag': 'trend', 'policy_tag': 'policy_v1', 'pnl': 8.0, 'return_pct': 0.9},
        ]
        digest = build_close_outcome_digest(trades, label='unit-test')
        self.assertEqual(digest['schema_version'], 'trade_close_outcome_digest_v1')
        self.assertEqual(digest['trade_count'], 3)
        self.assertEqual(digest['by_close_decision']['win'], 2)
        self.assertEqual(digest['by_close_reason_category']['take_profit'], 2)
        self.assertEqual(digest['dominant_regime_tag'], 'trend')
        self.assertEqual(digest['dominant_policy_tag'], 'policy_v1')
        self.assertAlmostEqual(digest['net_pnl'], 15.5)
        self.assertEqual(digest['recent_closes'][0]['trade_id'], 3)

    def test_database_close_outcome_digest_rolls_up_closed_trades(self):
        self._record_closed_trade(symbol='BTC/USDT', entry_price=50000, exit_price=51000, pnl=100, signal_id=11, reason='止盈收口')
        self._record_closed_trade(symbol='BTC/USDT', entry_price=50000, exit_price=49500, pnl=-50, signal_id=12, reason='止损离场')
        digest = self.db.get_close_outcome_digest(symbol='BTC/USDT', limit=10)
        self.assertEqual(digest['trade_count'], 2)
        self.assertEqual(digest['by_close_decision']['win'], 1)
        self.assertEqual(digest['by_close_decision']['loss'], 1)
        self.assertEqual(digest['by_close_reason_category']['take_profit'], 1)
        self.assertEqual(digest['by_close_reason_category']['stop_loss'], 1)

    def test_trades_api_returns_close_outcome_digest_summary(self):
        import dashboard.api as dashboard_api
        test_db = Database('data/test_dashboard_close_outcome_digest.db')
        old_db = dashboard_api.db
        dashboard_api.db = test_db
        try:
            trade_id = test_db.record_trade(symbol='BTC/USDT', side='long', entry_price=50000, quantity=0.1, leverage=5, signal_id=21, layer_no=1, root_signal_id=21, plan_context={'layer_no': 1, 'root_signal_id': 21, 'strategy_tags': ['ema']})
            test_db.reconcile_trade_close(trade_id, {'exit_price': 51000, 'pnl': 100, 'close_time': '2026-03-30T00:21:00', 'source': 'exchange_fill', 'fills': [{'id': 'f21'}]}, reason='止盈收口')
            client = dashboard_api.app.test_client()
            response = client.get('/api/trades?symbol=BTC/USDT&limit=10')
            payload = response.get_json()
            self.assertTrue(payload['success'])
            self.assertIn('summary', payload)
            self.assertEqual(payload['summary']['close_outcome_digest']['trade_count'], 1)
            self.assertEqual(payload['summary']['close_outcome_digest']['by_close_decision']['win'], 1)
        finally:
            dashboard_api.db = old_db
            if os.path.exists('data/test_dashboard_close_outcome_digest.db'):
                os.remove('data/test_dashboard_close_outcome_digest.db')

    def test_close_outcome_digest_flows_into_runtime_dashboard_and_workbench_summaries(self):
        from analytics.helper import build_dashboard_summary_cards, build_runtime_orchestration_summary, build_workbench_governance_view
        close_outcome_digest = {
            'schema_version': 'trade_close_outcome_digest_v1',
            'trade_count': 2,
            'by_close_decision': {'win': 1, 'loss': 1},
            'by_outcome_quality': {'positive': 1, 'bounded_loss': 1},
            'by_close_reason_category': {'take_profit': 1, 'stop_loss': 1},
            'by_regime_tag': {'trend': 2},
            'by_policy_tag': {'policy_v1': 2},
            'recent_closes': [],
            'headline': 'closed=2',
        }
        payload = {
            'consumer_view': {'workflow_state': {'summary': {}, 'item_states': []}, 'approval_state': {'summary': {}, 'items': []}, 'rollout_stage_progression': {'summary': {}, 'items': []}, 'rollout_executor': {}, 'auto_approval_execution': {}, 'controlled_rollout_execution': {}, 'validation_gate': {'enabled': False, 'ready': None, 'headline': 'validation_gate_disabled', 'gap_count': 0, 'failing_case_count': 0, 'regression_detected': False}},
            'attention_view': {'summary': {}, 'headline': {}, 'items': []},
            'operator_digest': {'headline': {'status': 'steady', 'message': 'ok'}, 'summary': {}, 'next_actions': [], 'attention': {}, 'control_plane_manifest': {}, 'control_plane_readiness': {}},
            'workflow_alert_digest': {'headline': {'status': 'steady', 'message': 'ok'}, 'summary': {}, 'alerts': []},
            'workflow_recovery_view': {'summary': {}, 'queues': {'rollback_candidates': [], 'manual_recovery': []}},
            'unified_workbench_overview': {'dominant_line': 'approval', 'overall_state': 'steady', 'headline': {}, 'summary': {}, 'lines': {}, 'transition_journal': {'summary': {'count': 0, 'latest_timestamp': None}, 'latest': {}, 'recent_transitions': []}},
            'adaptive_rollout_orchestration': {'summary': {}},
            'close_outcome_digest': close_outcome_digest,
        }
        dashboard_summary = build_dashboard_summary_cards(dict(payload), max_items=2)
        runtime_summary = build_runtime_orchestration_summary(dict(payload), max_items=2)
        workbench_summary = build_workbench_governance_view(dict(payload), max_items=2)
        self.assertEqual(dashboard_summary['summary']['close_outcome_digest']['trade_count'], 2)
        self.assertEqual(runtime_summary['summary']['close_outcome_digest']['by_close_reason_category']['take_profit'], 1)
        self.assertEqual(workbench_summary['summary']['close_outcome_digest']['by_policy_tag']['policy_v1'], 2)

    def test_close_outcome_feedback_loop_builds_tighten_recommendation_and_runtime_next_action(self):
        from analytics.helper import build_close_outcome_feedback_loop, build_runtime_orchestration_summary, build_workflow_operator_digest
        close_outcome_digest = {
            'schema_version': 'trade_close_outcome_digest_v1',
            'trade_count': 4,
            'win_count': 1,
            'loss_count': 3,
            'win_rate': 25.0,
            'net_pnl': -12.5,
            'avg_return_pct': -1.8,
            'by_close_decision': {'loss': 3, 'win': 1},
            'by_outcome_quality': {'adverse': 1, 'bounded_loss': 2, 'positive': 1},
            'by_close_reason_category': {'stop_loss': 2, 'manual_close': 1, 'take_profit': 1},
            'by_regime_tag': {'trend': 4},
            'by_policy_tag': {'policy_v2': 4},
            'dominant_regime_tag': 'trend',
            'dominant_policy_tag': 'policy_v2',
            'dominant_close_reason_category': 'stop_loss',
            'recent_closes': [{'trade_id': 4, 'symbol': 'BTC/USDT'}],
        }
        feedback = build_close_outcome_feedback_loop(close_outcome_digest, label='unit-test')
        self.assertEqual(feedback['governance_mode'], 'tighten')
        self.assertEqual(feedback['next_action']['route'], 'review_schedule_queue')
        self.assertEqual(feedback['control_plane']['line'], 'rollout')
        self.assertEqual(feedback['control_plane']['action_policy'], 'guarded_tighten_review')
        self.assertFalse(feedback['control_plane']['safe_execution']['allow_live_execution'])
        self.assertIn('close_outcome_policy_tighten', feedback['reason_codes'])

        payload = {
            'consumer_view': {'workflow_state': {'summary': {}, 'item_states': []}, 'approval_state': {'summary': {}, 'items': []}, 'rollout_stage_progression': {'summary': {}, 'items': []}, 'rollout_executor': {}, 'auto_approval_execution': {}, 'controlled_rollout_execution': {}, 'validation_gate': {'enabled': False, 'ready': None, 'headline': 'validation_gate_disabled', 'gap_count': 0, 'failing_case_count': 0, 'regression_detected': False}},
            'attention_view': {'summary': {}, 'headline': {}, 'items': []},
            'operator_digest': {'headline': {'status': 'steady', 'message': 'ok'}, 'summary': {}, 'next_actions': [], 'attention': {}, 'control_plane_manifest': {}, 'control_plane_readiness': {}},
            'workflow_alert_digest': {'headline': {'status': 'steady', 'message': 'ok'}, 'summary': {}, 'alerts': []},
            'workflow_recovery_view': {'summary': {}, 'queues': {'rollback_candidates': [], 'manual_recovery': []}},
            'adaptive_rollout_orchestration': {'summary': {}},
            'close_outcome_digest': close_outcome_digest,
        }
        operator_digest = build_workflow_operator_digest(dict(payload), max_items=2)
        close_action = next(row for row in operator_digest['next_actions'] if row['kind'] == 'close_outcome_policy_review')
        self.assertEqual(close_action['line'], 'rollout')
        self.assertEqual(close_action['action_policy'], 'guarded_tighten_review')
        self.assertFalse(close_action['safe_execution']['allow_parameter_auto_apply'])

        runtime_summary = build_runtime_orchestration_summary(dict(payload), max_items=2)
        self.assertEqual(runtime_summary['close_outcome_feedback_loop']['governance_mode'], 'tighten')
        self.assertEqual(runtime_summary['close_outcome_control_plane']['line'], 'rollout')
        self.assertEqual(runtime_summary['next_step']['route'], 'review_schedule_queue')
        self.assertEqual(runtime_summary['related_summary']['close_outcome_feedback_loop']['next_action']['follow_up'], 'review_policy_thresholds')

    def test_close_outcome_feedback_loop_flows_into_governance_and_production_readiness(self):
        from analytics.helper import build_workbench_governance_view, build_unified_workbench_overview, build_production_rollout_readiness
        close_outcome_digest = {
            'schema_version': 'trade_close_outcome_digest_v1',
            'trade_count': 5,
            'win_count': 0,
            'loss_count': 5,
            'win_rate': 0.0,
            'net_pnl': -30.0,
            'avg_return_pct': -3.2,
            'by_close_decision': {'loss': 5},
            'by_outcome_quality': {'adverse': 4, 'bounded_loss': 1},
            'by_close_reason_category': {'stop_loss': 5},
            'by_regime_tag': {'range': 5},
            'by_policy_tag': {'policy_v3': 5},
            'dominant_regime_tag': 'range',
            'dominant_policy_tag': 'policy_v3',
            'dominant_close_reason_category': 'stop_loss',
            'recent_closes': [{'trade_id': 5, 'symbol': 'ETH/USDT'}],
        }
        payload = {
            'consumer_view': {'workflow_state': {'summary': {}, 'item_states': []}, 'approval_state': {'summary': {}, 'items': []}, 'rollout_stage_progression': {'summary': {}, 'items': []}, 'rollout_executor': {}, 'auto_approval_execution': {}, 'controlled_rollout_execution': {}, 'validation_gate': {'enabled': False, 'ready': True, 'headline': 'validation_gate_ready', 'gap_count': 0, 'failing_case_count': 0, 'regression_detected': False}},
            'attention_view': {'summary': {}, 'headline': {}, 'items': []},
            'operator_digest': {'headline': {'status': 'steady', 'message': 'ok'}, 'summary': {}, 'next_actions': [], 'attention': {}, 'control_plane_manifest': {}, 'control_plane_readiness': {'can_continue_auto_promotion': True, 'blocking_issues': []}},
            'workflow_alert_digest': {'headline': {'status': 'steady', 'message': 'ok'}, 'summary': {'severity_counts': {}}, 'alerts': []},
            'workflow_recovery_view': {'summary': {}, 'queues': {'rollback_candidates': [], 'manual_recovery': [], 'retry_queue': []}},
            'adaptive_rollout_orchestration': {'summary': {}},
            'close_outcome_digest': close_outcome_digest,
            'testnet_bridge_execution_evidence': {'summary': {}, 'status': 'disabled', 'follow_up_required': False},
        }
        workbench = build_workbench_governance_view(dict(payload), max_items=2)
        self.assertEqual(workbench['governance_recommendation']['governance_mode'], 'rollback')
        unified = build_unified_workbench_overview(dict(payload), max_items=2)
        self.assertEqual(unified['close_outcome_feedback_loop']['next_action']['route'], 'rollback_candidate_queue')
        self.assertEqual(unified['close_outcome_control_plane']['line'], 'recovery')
        self.assertEqual(unified['close_outcome_control_plane']['action_policy'], 'freeze_and_manual_review')
        readiness = build_production_rollout_readiness(dict(payload), max_items=2)
        self.assertIn('close_outcome_feedback_requires_rollback_review', readiness['blocking_issues'])
        self.assertEqual(readiness['summary']['close_outcome_control_plane']['route'], 'rollback_candidate_queue')
        self.assertEqual(readiness['runbook_actions'][0]['route'], 'rollback_candidate_queue')


class TestTradeOutcomeAttribution(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.db = Database('data/test_trade_outcome_attribution.db')
        self.executor = TradingExecutor(self.config, None, self.db)
        self.executor.exchange = FakeExecutorExchange()

    def tearDown(self):
        if os.path.exists('data/test_trade_outcome_attribution.db'):
            os.remove('data/test_trade_outcome_attribution.db')

    def test_reconcile_trade_close_persists_structured_outcome_attribution(self):
        regime_snapshot = build_regime_snapshot('trend', 0.82, {'ema_gap': 0.02}, '趋势上涨')
        policy_snapshot = resolve_regime_policy(self.config, 'BTC/USDT', regime_snapshot)
        trade_id = self.db.record_trade(
            symbol='BTC/USDT', side='long', entry_price=50000, quantity=0.1, leverage=5,
            signal_id=101, layer_no=1, root_signal_id=101,
            plan_context={
                'layer_no': 1,
                'root_signal_id': 101,
                'strategy_tags': ['ema_trend'],
                'regime_snapshot': regime_snapshot,
                'adaptive_policy_snapshot': policy_snapshot,
            }
        )
        changed = self.db.reconcile_trade_close(trade_id, {'exit_price': 51000, 'pnl': 100, 'close_time': '2026-03-30T00:00:00', 'source': 'exchange_fill', 'fills': [{'id': 'f1'}]}, reason='止盈收口')
        self.assertTrue(changed)
        trade = self.db.get_trades(symbol='BTC/USDT', limit=1)[0]
        outcome = trade['outcome_attribution']
        self.assertEqual(outcome['schema_version'], 'trade_outcome_attribution_v1')
        self.assertEqual(outcome['close_source'], 'exchange_fill')
        self.assertEqual(outcome['close_reason_category'], 'take_profit')
        self.assertEqual(trade['regime_tag'], 'trend')
        self.assertEqual(trade['policy_tag'], policy_snapshot.get('policy_version'))
        self.assertEqual(trade['strategy_tags'], ['ema_trend'])
        self.assertEqual(trade['close_decision'], 'win')
        self.assertEqual(trade['return_pct'], trade['pnl_percent'])

    def test_close_trade_with_outcome_enrichment_persists_local_close_attribution(self):
        trade_id = self.db.record_trade(
            symbol='BTC/USDT', side='long', entry_price=50000, quantity=0.1, leverage=10,
            signal_id=201, layer_no=1, root_signal_id=201,
            plan_context={'layer_no': 1, 'root_signal_id': 201, 'strategy_tags': ['breakout_v1']}
        )
        changed = self.db.close_trade_with_outcome_enrichment(
            trade_id=trade_id, exit_price=49500, pnl=-50, pnl_percent=-10,
            notes='手动止损', close_source='local_market_close'
        )
        self.assertTrue(changed)
        trade = self.db.get_trades(symbol='BTC/USDT', limit=1)[0]
        self.assertEqual(trade['outcome_attribution']['close_reason_category'], 'stop_loss')
        self.assertEqual(trade['close_decision'], 'loss')
        self.assertEqual(trade['pnl_bucket'], 'large')
        self.assertEqual(trade['outcome_attribution']['strategy_tags'], ['breakout_v1'])

    def test_executor_close_path_emits_outcome_attribution(self):
        class CloseCapableExchange(FakeExecutorExchange):
            def close_order(self, symbol, side, quantity, posSide=None):
                return {'symbol': symbol, 'side': side, 'quantity': quantity, 'posSide': posSide}

            def fetch_closed_trade_summary(self, trade, fallback_price=None):
                return None

        self.executor.exchange = CloseCapableExchange()
        self.db.update_position(symbol='BTC/USDT', side='long', entry_price=100, quantity=1, leverage=2, current_price=100, coin_quantity=1, contract_size=1)
        self.db.record_trade(
            symbol='BTC/USDT', side='long', entry_price=100, quantity=1, leverage=2,
            signal_id=301, layer_no=1, root_signal_id=301,
            plan_context={'layer_no': 1, 'root_signal_id': 301, 'strategy_tags': ['mean_revert']}
        )
        result = self.executor.close_position('BTC/USDT', reason='manual_take_profit', close_price=105)
        self.assertTrue(result)
        trade = self.db.get_trades(symbol='BTC/USDT', limit=1)[0]
        self.assertEqual(trade['status'], 'closed')
        self.assertEqual(trade['close_decision'], 'win')
        self.assertEqual(trade['outcome_attribution']['close_source'], 'local_market_close')
        self.assertEqual(trade['outcome_attribution']['strategy_tags'], ['mean_revert'])
