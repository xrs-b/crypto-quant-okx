"""
通知总线 - 第一版
统一收口 signal / decision / trade / close / error 通知
"""
import json
from datetime import datetime
from typing import Dict, List, Optional
from urllib import request, error


class NotificationManager:
    def __init__(self, config, database=None, logger=None):
        self.config = config
        self.db = database
        self.logger = logger
        self.discord_cfg = config.get('notification.discord', {}) if hasattr(config, 'get') else (config.get('notification', {}).get('discord', {}) if isinstance(config, dict) else {})

    def _is_enabled(self, kind: str) -> bool:
        if not self.discord_cfg.get('enabled', False):
            return False
        if kind == 'signal':
            return bool(self.discord_cfg.get('notify_signals', True))
        if kind in {'trade', 'close', 'decision'}:
            return bool(self.discord_cfg.get('notify_trades', True))
        if kind == 'error':
            return bool(self.discord_cfg.get('notify_errors', True))
        return True

    def _store_event(self, level: str, event_type: str, message: str, details: Dict = None):
        if self.db:
            try:
                self.db.log(level.upper(), f'notify:{event_type}', {'message': message, 'details': details or {}})
            except Exception:
                pass
        if self.logger:
            try:
                self.logger.info(f'[NOTIFY:{event_type}] {message}')
            except Exception:
                pass

    def _send_discord(self, content: str) -> bool:
        webhook_url = self.discord_cfg.get('webhook_url')
        if not webhook_url:
            return False
        payload_dict = {'content': content}
        if self.discord_cfg.get('webhook_username'):
            payload_dict['username'] = self.discord_cfg.get('webhook_username')
        payload = json.dumps(payload_dict).encode('utf-8')
        req = request.Request(webhook_url, data=payload, headers={'Content-Type': 'application/json'})
        try:
            with request.urlopen(req, timeout=10) as resp:
                return 200 <= getattr(resp, 'status', 204) < 300
        except error.URLError:
            return False

    def send(self, event_type: str, title: str, lines: List[str], level: str = 'info', details: Dict = None) -> Dict:
        body = '\n'.join([f'**{title}**', *[f'- {line}' for line in lines if line]])
        self._store_event(level, event_type, body, details)
        delivered = False
        enabled = self._is_enabled(event_type)
        if enabled:
            delivered = self._send_discord(body)
        return {'delivered': delivered, 'enabled': enabled, 'message': body}

    def notify_signal(self, signal, passed: bool, reason: str = None, details: Dict = None) -> Dict:
        title = '📡 可靠信号' if passed else '🧪 信号已生成'
        direction = '做多' if signal.signal_type == 'buy' else '做空' if signal.signal_type == 'sell' else '观望'
        lines = [
            f'币种：{signal.symbol}',
            f'价格：{signal.price}',
            f'方向：{direction}',
            f'强度：{signal.strength}%',
            f'策略：{", ".join(signal.strategies_triggered or []) or "--"}',
            f'时间：{getattr(signal, "timestamp", datetime.now().isoformat())}',
            f'状态：{"通过初筛" if passed else "未通过"}',
            f'原因：{reason or "--"}',
        ]
        return self.send('signal', title, lines, 'info', {'signal': signal.to_dict() if hasattr(signal, 'to_dict') else {}, 'details': details or {}})

    def notify_decision(self, signal, allowed: bool, reason: str = None, details: Dict = None) -> Dict:
        title = '🤖 机器人决策：通过' if allowed else '🛑 机器人决策：拒绝'
        direction = '做多' if signal.signal_type == 'buy' else '做空' if signal.signal_type == 'sell' else '观望'
        lines = [
            f'币种：{signal.symbol}',
            f'方向：{direction}',
            f'当前价格：{signal.price}',
            f'信号强度：{signal.strength}%',
            f'触发策略：{", ".join(signal.strategies_triggered or []) or "--"}',
            f'决策结果：{"允许执行" if allowed else "拒绝执行"}',
            f'原因：{reason or "--"}',
        ]
        return self.send('decision', title, lines, 'info' if allowed else 'warning', {'signal': signal.to_dict() if hasattr(signal, 'to_dict') else {}, 'details': details or {}})

    def notify_trade_open(self, symbol: str, side: str, price: float, quantity: float, trade_id: int = None, signal=None) -> Dict:
        lines = [
            f'币种：{symbol}',
            f'方向：{"做多" if side == "long" else "做空"}',
            f'价格：{price}',
            f'数量：{quantity}',
            f'Trade ID：{trade_id or "--"}',
            f'信号强度：{getattr(signal, "strength", "--")}',
        ]
        return self.send('trade', '✅ 开仓执行成功', lines, 'info', {'trade_id': trade_id, 'symbol': symbol, 'side': side})

    def notify_trade_close(self, symbol: str, side: str, close_price: float, reason: str, pnl: float = None) -> Dict:
        lines = [
            f'币种：{symbol}',
            f'方向：{"做多" if side == "long" else "做空"}',
            f'平仓价：{close_price}',
            f'原因：{reason}',
            f'PnL：{pnl if pnl is not None else "--"}',
        ]
        return self.send('close', '📦 平仓执行', lines, 'info', {'symbol': symbol, 'side': side, 'reason': reason, 'pnl': pnl})

    def notify_error(self, title: str, message: str, details: Dict = None) -> Dict:
        return self.send('error', f'❌ {title}', [message], 'error', details or {})

    def notify_runtime(self, phase: str, lines: List[str], details: Dict = None) -> Dict:
        title_map = {
            'start': '⏱️ 机器人周期开始',
            'end': '✅ 机器人周期完成',
            'skip': '⏭️ 机器人周期跳过',
            'daemon': '🔁 守护模式启动',
        }
        level_map = {'start': 'info', 'end': 'info', 'skip': 'warning', 'daemon': 'info'}
        return self.send('decision', title_map.get(phase, '🤖 机器人运行状态'), lines, level_map.get(phase, 'info'), details or {})

    def test_discord(self) -> Dict:
        now = datetime.now().isoformat()
        return self.send('decision', '🔔 Discord 通知测试', [f'时间：{now}', '如果你见到呢条消息，代表 webhook 推送链路正常'], 'info', {'time': now, 'kind': 'notify-test'})
