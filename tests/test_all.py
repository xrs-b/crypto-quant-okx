"""
OKX量化交易系统 - 测试套件
"""
import sys
import os
import json
import sqlite3
import tempfile
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
from strategies.strategy_library import StrategyManager
from bot.run import build_exchange_diagnostics, build_exchange_smoke_plan, build_runtime_health_summary, maybe_send_daily_health_summary, execute_exchange_smoke, reconcile_exchange_positions
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
        finally:
            if os.path.exists('data/test_health_summary.db'):
                os.remove('data/test_health_summary.db')

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
    
    def tearDown(self):
        import os
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
        self.config = Config()
        self.db = Database('data/test_risk.db')
        self.risk_mgr = RiskManager(self.config, self.db)
    
    def tearDown(self):
        import os
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
        self.config = Config()
        self.db = Database('data/test_risk_budget_details.db')
        self.risk_mgr = RiskManager(self.config, self.db)

    def tearDown(self):
        if os.path.exists('data/test_risk_budget_details.db'):
            os.remove('data/test_risk_budget_details.db')

    def test_can_open_position_returns_entry_plan(self):
        can_open, reason, details = self.risk_mgr.can_open_position('BTC/USDT')
        self.assertTrue(can_open)
        self.assertIn('entry_plan', details['exposure_limit'])
        self.assertIn('position_ratio', details['exposure_limit'])


    def test_can_open_position_step4_keeps_default_observe_only(self):
        can_open, reason, details = self.risk_mgr.can_open_position('BTC/USDT', side='long')
        self.assertTrue(can_open)
        self.assertEqual(details['adaptive_risk_snapshot']['effective_state'], 'disabled')
        self.assertEqual(details['exposure_limit']['entry_plan']['risk_budget']['base_entry_margin_ratio'], 0.1)

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
        self.assertAlmostEqual(details['exposure_limit']['entry_plan']['risk_budget']['base_entry_margin_ratio'], 0.1, places=6)
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
        self.assertAlmostEqual(details['exposure_limit']['entry_plan']['risk_budget']['base_entry_margin_ratio'], 0.1, places=6)
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
        first_queue = delivery['orchestration_ready']['queue'][0]
        self.assertIn('primary_action', first_queue)
        self.assertIn('next_actions', first_queue)
        self.assertIn('blocking_chain', first_queue)
        self.assertIn('rollback_candidate', first_queue)
        self.assertIn(first_queue['decision'], {'expand', 'tighten', 'rollback', 'hold'})
        self.assertIn('expand', delivery['orchestration_ready']['queues'])
        self.assertIn('rollback', delivery['orchestration_ready']['queues'])
        self.assertTrue(delivery['orchestration_ready']['prioritized_queue'])
        self.assertTrue(delivery['orchestration_ready']['next_actions'])
        self.assertTrue(delivery['orchestration_ready']['review_checkpoints'])
        self.assertIn('repricing_review', delivery['orchestration_ready']['action_catalog'])
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
        self.assertEqual(payload['approval_state']['summary']['approval_count'], len(payload['approval_state']['items']))
        approval_row = payload['approval_queue'][0]
        self.assertTrue(any(
            row['playbook_id'] == approval_row['playbook_id']
            for row in payload['by_bucket'][approval_row['bucket_id']]['approvals']['actions']
        ))
        self.assertIn('state', payload['by_bucket'][approval_row['bucket_id']])

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

from analytics.helper import build_workflow_approval_records, merge_persisted_approval_state, build_approval_audit_overview


class TestApprovalPersistence(unittest.TestCase):
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
            self.assertEqual(timeline[1]['event_type'], 'decision_recorded')
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
            self.assertTrue(any(step['state'] == 'expired' for step in summary['decision_path']))

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
