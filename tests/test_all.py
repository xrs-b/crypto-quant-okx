"""
OKX量化交易系统 - 测试套件
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import pandas as pd
import numpy as np
from datetime import datetime

from core.config import Config
from core.database import Database
from core.exchange import Exchange
from core.notifier import NotificationManager
from signals import SignalDetector, SignalValidator, SignalRecorder
from trading import TradingExecutor, RiskManager
from strategies.strategy_library import StrategyManager
from bot.run import build_exchange_diagnostics, build_exchange_smoke_plan, execute_exchange_smoke, reconcile_exchange_positions


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

    def test_position_mode(self):
        """测试持仓模式配置"""
        self.assertIn(self.config.position_mode, ['oneway', 'hedge', 'one-way', 'net', 'single'])


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
            self.assertEqual(report['summary']['open_trades'], 2)
            self.assertEqual(report['summary']['open_trade_missing_exchange'], 1)
            self.assertEqual(report['diff']['open_trade_missing_exchange'][0]['symbol'], 'ETH/USDT')
            self.assertEqual(report['summary']['exchange_missing_open_trade'], 1)
            self.assertEqual(report['diff']['exchange_missing_open_trade'][0]['symbol'], 'XRP/USDT')
        finally:
            if os.path.exists('data/test_reconcile.db'):
                os.remove('data/test_reconcile.db')


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
