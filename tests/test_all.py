"""
OKX量化交易系统 - 测试套件
"""
import sys
import os
import json
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
from core.exchange import Exchange
from core.notifier import NotificationManager
from signals import Signal, SignalDetector, SignalValidator, SignalRecorder, EntryDecider
from trading import TradingExecutor, RiskManager
from trading.executor import build_observability_context
from strategies.strategy_library import StrategyManager
from bot.run import build_exchange_diagnostics, build_exchange_smoke_plan, build_runtime_health_summary, maybe_send_daily_health_summary, execute_exchange_smoke, reconcile_exchange_positions
from dashboard.api import app
from core.risk_budget import get_risk_budget_config, compute_entry_plan, summarize_margin_usage


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
            with patch('core.config.Path.home', return_value=Path(tmpdir)):
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
            'strategies': {'composite': {'min_strength': 20}}
        }
        validator = SignalValidator(cfg, None)
        signal = Signal(symbol='XRP/USDT', signal_type='buy', price=1.0, strength=24, strategies_triggered=['RSI'])
        passed, reason, details = validator.validate(signal)
        self.assertTrue(passed)
        self.assertIsNone(reason)
        self.assertTrue(details['strength_check']['passed'])


class TestEntryDecider(unittest.TestCase):
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
            strategies_triggered=['RSI'],
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
            strategies_triggered=['RSI'],
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
            strategies_triggered=['MACD'],
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
        cfg._config['notification']['discord'].update({'enabled': False})
        db = FakeLogDB()
        notifier = NotificationManager(cfg, db, None)
        from signals.detector import Signal
        signal = Signal(symbol='BTC/USDT', signal_type='buy', price=50000, strength=88, strategies_triggered=['RSI', 'MACD'])
        notifier.notify_signal(signal, True, None, {'passed': True})
        notifier.notify_decision(signal, False, '风险拒绝', {'risk_gate': {'passed': False, 'reason': '风险拒绝'}})
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
        self.assertIn('====================', db.logs[0]['details']['message'])
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
            'port': 8050
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
            'port': 8050
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
            self.assertEqual(report['summary']['open_trades'], 1)
            self.assertEqual(report['summary']['stale_open_trades_closed'], 1)
            self.assertEqual(report['summary']['open_trade_missing_exchange'], 0)
            closed_eth = db.get_trades(symbol='ETH/USDT', limit=5)[0]
            self.assertEqual(closed_eth['status'], 'closed')
            self.assertIn('自动收口', closed_eth.get('notes') or '')
            self.assertEqual(report['summary']['exchange_missing_open_trade'], 1)
            self.assertEqual(report['diff']['exchange_missing_open_trade'][0]['symbol'], 'XRP/USDT')
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
        state = self.db.get_layer_plan_state('BTC/USDT', 'long')
        self.assertEqual(state['status'], 'idle')
        self.assertEqual(state['plan_data']['filled_layers'], [])
        self.assertEqual(state['plan_data']['pending_layers'], [])

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


class TestExecutionObservability(unittest.TestCase):
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
            db.update_signal(signal_id, filter_details=json.dumps({'observability': {'signal_id': signal_id, 'root_signal_id': signal_id, 'layer_no': 1, 'deny_reason': 'direction_lock', 'current_symbol_exposure': 0.05, 'projected_symbol_exposure': 0.11, 'current_total_exposure': 0.05, 'projected_total_exposure': 0.11}}, ensure_ascii=False), filtered=1, filter_reason='方向锁占用中')
            db.create_open_intent(symbol='BTC/USDT:USDT', side='long', signal_id=signal_id, root_signal_id=signal_id, planned_margin=60, leverage=10, layer_no=1, plan_context={'foo': 'bar'})
            snapshot = db.get_execution_state_snapshot()
            self.assertIn('exposure', snapshot)
            self.assertGreater(snapshot['exposure']['projected_total_margin'], snapshot['exposure']['current_total_margin'])
            self.assertTrue(snapshot['signal_decisions'])
            self.assertEqual(snapshot['signal_decisions'][0]['deny_reason'], 'direction_lock')
