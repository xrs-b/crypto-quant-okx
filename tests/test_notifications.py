"""通知相关测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import pandas as pd
import numpy as np

from core.config import Config
from core.notifier import NotificationManager
from core.regime import build_regime_snapshot
from core.regime_policy import resolve_regime_policy
from signals import SignalDetector
from bot.run import TradingBot
from tests.test_all import FakeLogDB

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
        self.assertIn('【自适应市场状态（Observe-only）】', db.logs[0]['details']['message'])
        self.assertIn('仅增强观察与汇总展示', db.logs[0]['details']['message'])
        self.assertIn('不改变真实交易执行', db.logs[0]['details']['message'])
        self.assertIn('【风控拦截】', db.logs[1]['details']['message'])
        self.assertIn('通知等级：🚨 紧急', db.logs[1]['details']['message'])
        self.assertIn('风险拒绝', db.logs[1]['details']['message'])
        self.assertFalse(runtime['enabled'])
        self.assertTrue(duplicate_runtime['suppressed'])
        self.assertFalse(probe['delivered'])
        self.assertEqual(probe['outbox_status'], 'disabled')

    def test_notify_signal_live_detector_payload_keeps_real_regime_and_confidence(self):
        cfg = Config()
        cfg._config.setdefault('notification', {}).setdefault('discord', {})
        cfg._config['notification']['discord'].update({'enabled': False, 'bot_token': '', 'channel_id': '', 'webhook_url': ''})
        db = FakeLogDB()
        notifier = NotificationManager(cfg, db, None)
        detector = SignalDetector(cfg.all)

        dates = pd.date_range('2024-01-01', periods=80, freq='1h')
        close = pd.Series(np.linspace(50000, 56000, 80))
        df = pd.DataFrame({
            0: dates,
            1: close - 50,
            2: close + 120,
            3: close - 120,
            4: close,
            5: np.linspace(1000, 2400, 80),
        })
        signal = detector.analyze('BTC/USDT', df, float(df[4].iloc[-1]), None)
        signal.signal_type = 'buy'
        signal.strength = max(signal.strength, 42)
        details = {'regime_snapshot': signal.regime_snapshot, 'adaptive_policy_snapshot': signal.adaptive_policy_snapshot}

        notifier.notify_signal(signal, False, '测试 live regime payload', details)
        message = db.logs[-1]['details']['message']
        self.assertNotIn('市场状态：unknown ｜ 置信度：0%', message)
        self.assertIn(f"市场状态：{signal.regime_snapshot['name']}（置信度 {signal.regime_snapshot['confidence']:.0%}）", message)

    def test_notify_signal_observe_only_uses_market_context_fallback(self):
        cfg = Config()
        cfg._config.setdefault('notification', {}).setdefault('discord', {})
        cfg._config['notification']['discord'].update({'enabled': False, 'bot_token': '', 'channel_id': '', 'webhook_url': ''})
        db = FakeLogDB()
        notifier = NotificationManager(cfg, db, None)
        from signals.detector import Signal
        signal = Signal(
            symbol='BTC/USDT', signal_type='buy', price=50000, strength=88, strategies_triggered=['RSI'],
            market_context={
                'regime': 'trend',
                'regime_name': 'trend_up',
                'regime_confidence': 0.81,
                'adaptive_policy_snapshot': {
                    'mode': 'observe_only',
                    'phase': 'm1',
                    'state': 'shadow',
                    'summary': 'market-context fallback summary',
                    'tags': ['observe_only', 'market_context'],
                },
            },
        )
        notifier.notify_signal(signal, False, '测试回退', {'passed': False})
        message = db.logs[-1]['details']['message']
        self.assertIn('市场状态：trend_up（置信度 81%）', message)
        self.assertIn('策略模式：仅观察 [observe_only]', message)
        self.assertIn('阶段/状态：M1阶段 / 阴影', message)
        self.assertIn('摘要：market-context fallback summary', message)

    def test_trading_bot_notification_context_backfills_snapshots(self):
        bot = TradingBot.__new__(TradingBot)
        from signals.detector import Signal
        signal = Signal(
            symbol='BTC/USDT', signal_type='buy', price=50000, strength=88,
            market_context={
                'regime': 'trend',
                'regime_name': 'trend_up',
                'regime_confidence': 0.83,
                'regime_snapshot': {'regime': 'trend', 'name': 'trend_up', 'confidence': 0.83},
                'adaptive_policy_snapshot': {'mode': 'observe_only', 'summary': 'fallback from market context'},
            },
            regime_info={'regime': 'trend', 'name': 'trend_up', 'confidence': 0.83},
        )
        payload = bot._notification_context(signal, {'signal_id': 7})
        self.assertEqual(payload['signal_id'], 7)
        self.assertEqual(payload['regime_snapshot']['name'], 'trend_up')
        self.assertEqual(payload['adaptive_policy_snapshot']['mode'], 'observe_only')
        self.assertEqual(payload['market_context']['regime_confidence'], 0.83)
        self.assertEqual(payload['regime_info']['regime'], 'trend')

    def test_trading_bot_notification_context_pulls_regime_from_observability(self):
        """When details contain observability with real regime_snapshot, use it."""
        bot = TradingBot.__new__(TradingBot)
        from signals.detector import Signal
        signal = Signal(
            symbol='BTC/USDT', signal_type='buy', price=50000, strength=88,
            market_context={'regime': 'unknown'},
        )
        details = {
            'observability': {
                'regime_snapshot': {
                    'regime': 'high_vol',
                    'name': 'high_vol_up',
                    'confidence': 0.87,
                    'family': 'vol',
                    'direction': 'up',
                    'stability_score': 0.42,
                    'transition_risk': 0.31,
                    'indicators': {'volatility': 0.05},
                },
                'adaptive_policy_snapshot': {
                    'mode': 'guarded_execute',
                    'regime_name': 'high_vol_up',
                    'regime_confidence': 0.87,
                    'summary': 'high_vol[up] conf=0.87 stable=0.42 risk=0.31 | policy=adaptive_policy_v1_m4_testnet_live state=effective',
                    'tags': ['guarded_execute', 'effective'],
                },
                'observe_only': {
                    'summary': 'high_vol[up] conf=0.87 stable=0.42 risk=0.31 | policy=adaptive_policy_v1_m4_testnet_live state=effective',
                    'tags': ['guarded_execute', 'regime:high_vol'],
                    'phase': 'effective',
                    'state': 'high_vol',
                },
            }
        }
        payload = bot._notification_context(signal, details)
        self.assertEqual(payload['regime_snapshot']['name'], 'high_vol_up')
        self.assertEqual(payload['regime_snapshot']['confidence'], 0.87)
        self.assertEqual(payload['adaptive_policy_snapshot']['mode'], 'guarded_execute')
        self.assertEqual(payload['observe_only']['summary'].split('conf=')[1].split(' ')[0], '0.87')
        self.assertEqual(payload['adaptive_regime_observe_only']['summary'].split('conf=')[1].split(' ')[0], '0.87')

    def test_trading_bot_notification_context_pulls_fallback_from_policy_snapshot(self):
        """When signal has no regime_snapshot but has adaptive_policy_snapshot with regime info, reconstruct."""
        bot = TradingBot.__new__(TradingBot)
        from signals.detector import Signal
        signal = Signal(
            symbol='BTC/USDT', signal_type='buy', price=50000, strength=88,
            market_context={},
            adaptive_policy_snapshot={
                'mode': 'guarded_execute',
                'regime_name': 'range_bound',
                'regime_confidence': 0.76,
                'regime_family': 'range',
                'regime_direction': 'sideways',
                'summary': 'range_bound[sideways] conf=0.76 | policy=adaptive_policy_v1_m4_testnet_live state=effective',
            },
        )
        payload = bot._notification_context(signal, {})
        self.assertEqual(payload['adaptive_policy_snapshot']['regime_name'], 'range_bound')
        self.assertEqual(payload['adaptive_policy_snapshot']['regime_confidence'], 0.76)

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

    def test_notify_trade_open_failed_renders_exchange_error_summary_in_chinese(self):
        cfg = Config()
        cfg._config.setdefault('notification', {}).setdefault('discord', {})
        cfg._config['notification']['discord'].update({'enabled': False, 'bot_token': '', 'channel_id': '', 'webhook_url': ''})
        db = FakeLogDB()
        notifier = NotificationManager(cfg, db, None)
        from signals.detector import Signal
        signal = Signal(symbol='BTC/USDT', signal_type='buy', price=50000, strength=88, strategies_triggered=['RSI'])

        notifier.notify_trade_open_failed(
            'BTC/USDT',
            'long',
            50000,
            '交易所拒绝',
            signal,
            {
                'error_summary': {
                    'exchange_code': '51155',
                    'category': 'exchange_symbol_restricted',
                    'message': "You can't trade this pair or borrow this crypto due to local compliance restrictions.",
                    'hint': 'switch_testnet_symbol_or_account_region',
                    'raw_error': 'okx {...}',
                }
            },
        )
        message = db.logs[-1]['details']['message']
        self.assertIn('【错误摘要】', message)
        self.assertIn('交易所报码：51155', message)
        self.assertIn('错误分类：交易对/地区合规受限（exchange_symbol_restricted）', message)
        self.assertIn('处理建议：切换 testnet 币种，或检查账号地区/合规限制（switch_testnet_symbol_or_account_region）', message)

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

