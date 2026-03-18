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
from signals import Signal, SignalDetector, SignalValidator, SignalRecorder
from trading import TradingExecutor, RiskManager
from strategies.strategy_library import StrategyManager
from bot.run import build_exchange_diagnostics, build_exchange_smoke_plan, execute_exchange_smoke, reconcile_exchange_positions
from dashboard.api import app


class FakeExchange:
    def __init__(self, price=50000):
        self.price = price
        self.closed_orders = []

    def fetch_ticker(self, symbol):
        return {'last': self.price}

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
        notifier.notify_trade_open_failed('BTC/USDT', 'long', 50000, '交易所拒绝', signal, {'code': 'mock'})
        notifier.notify_trade_close_failed('BTC/USDT', 'long', '平仓失败', {'code': 'mock'})
        notifier.notify_reconcile_issue({'summary': {'exchange_positions': 2, 'local_positions': 1, 'open_trades': 1, 'exchange_missing_local_position': 1, 'local_position_missing_exchange': 0, 'open_trade_missing_exchange': 0, 'exchange_missing_open_trade': 1}})
        runtime = notifier.notify_runtime('skip', ['币种：BTC/USDT', '原因：测试跳过'])
        duplicate_runtime = notifier.notify_runtime('skip', ['币种：BTC/USDT', '原因：测试跳过'])
        probe = notifier.test_discord()
        self.assertEqual(len(db.logs), 8)
        self.assertGreaterEqual(len(db.outbox), 8)
        self.assertEqual(db.outbox[0]['channel'], 'discord')
        self.assertEqual(runtime['outbox_status'], 'disabled')
        self.assertEqual(duplicate_runtime['outbox_status'], 'disabled')
        self.assertIn('notify:signal', db.logs[0]['message'])
        self.assertIn('notify:runtime', db.logs[5]['message'])
        self.assertIn('【信号概览】', db.logs[0]['details']['message'])
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


class TestDashboardApi(unittest.TestCase):
    def test_daily_summary_handles_null_pnl(self):
        client = app.test_client()
        resp = client.get('/api/daily/summary')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json.get('success'))
        self.assertIsInstance(resp.json.get('data'), list)

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
        finally:
            dashboard_api.db = old_db
            if os.path.exists('data/test_dashboard_signals.db'):
                os.remove('data/test_dashboard_signals.db')


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
        self.executor.trading_config['take_profit'] = 0.5
        self.executor.trading_config['trailing_stop'] = 0.05
        self.db.update_position(symbol='BTC/USDT', side='long', entry_price=100, quantity=1, leverage=1, current_price=100)
        self.assertFalse(self.executor.check_take_profit('BTC/USDT', 110))
        self.assertEqual(self.executor._trade_cache['BTC/USDT']['highest_price'], 110)
        pos = self.db.get_positions()[0]
        self.assertEqual(pos['peak_price'], 110)
        self.executor._trade_cache.clear()
        self.assertFalse(self.executor.check_take_profit('BTC/USDT', 108))
        self.assertTrue(self.executor.check_take_profit('BTC/USDT', 104))

    def test_trailing_stop_tracks_low_for_short(self):
        self.executor.trading_config['take_profit'] = 0.5
        self.executor.trading_config['trailing_stop'] = 0.05
        self.db.update_position(symbol='BTC/USDT', side='short', entry_price=100, quantity=1, leverage=1, current_price=100)
        self.assertFalse(self.executor.check_take_profit('BTC/USDT', 95))
        self.assertEqual(self.executor._trade_cache['BTC/USDT']['lowest_price'], 95)
        pos = self.db.get_positions()[0]
        self.assertEqual(pos['trough_price'], 95)
        self.executor._trade_cache.clear()
        self.assertFalse(self.executor.check_take_profit('BTC/USDT', 96))
        self.assertTrue(self.executor.check_take_profit('BTC/USDT', 99.8))

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
