"""
通知总线 - 第一版
统一收口 signal / decision / trade / close / error 通知
"""
import hashlib
import json
from datetime import datetime
from typing import Dict, List, Optional
from urllib import request, error
import copy


class NotificationManager:
    def __init__(self, config, database=None, logger=None):
        self.config = config
        self.db = database
        self.logger = logger
        self.discord_cfg = config.get('notification.discord', {}) if hasattr(config, 'get') else (config.get('notification', {}).get('discord', {}) if isinstance(config, dict) else {})
        self._recent_messages = {}
        self._aggregate_messages = {}
        self._http_headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'OKXTradingBot/1.0 (+OpenClaw Notification Bridge)',
            'Accept': 'application/json',
        }

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

    def _store_event(self, level: str, event_type: str, message: str, details: Dict = None, title: str = None):
        outbox_id = None
        if self.db:
            try:
                payload = {'message': message, 'details': details or {}}
                self.db.log(level.upper(), f'notify:{event_type}', payload)
                outbox_id = self.db.enqueue_notification('discord', event_type, title or event_type, message, {
                    **(details or {}),
                    'event_type': event_type,
                    'level': level,
                })
            except Exception:
                pass
        if self.logger:
            try:
                self.logger.info(f'[NOTIFY:{event_type}] {message}')
            except Exception:
                pass
        return outbox_id

    def _update_outbox_status(self, outbox_id: int, status: str, extra: Dict = None):
        if not outbox_id or not self.db:
            return
        try:
            self.db.update_notification_outbox(outbox_id, status=status, details=extra)
        except Exception:
            pass

    def relay_pending_outbox(self, limit: int = 20) -> Dict:
        result = {'scanned': 0, 'delivered': 0, 'failed': 0, 'skipped': 0, 'items': []}
        if not self.db:
            return result
        try:
            rows = self.db.get_notification_outbox(status='pending', limit=limit)
        except Exception:
            return result
        result['scanned'] = len(rows)
        for row in rows:
            if row.get('channel') != 'discord':
                result['skipped'] += 1
                result['items'].append({'id': row.get('id'), 'status': 'skipped', 'reason': 'unsupported-channel'})
                continue
            existing = copy.deepcopy(row.get('details') or {})
            delivery = existing.get('delivery') or {}
            delivered = self._send_discord(row.get('message') or '')
            updated = {
                **existing,
                'delivery': {
                    **delivery,
                    'relay_attempted': True,
                    'delivered': delivered,
                    'path': 'relay' if delivered else 'bridge_pending',
                    'last_attempt_at': datetime.now().isoformat(),
                }
            }
            if delivered:
                self._update_outbox_status(row.get('id'), 'delivered', updated)
                result['delivered'] += 1
                result['items'].append({'id': row.get('id'), 'status': 'delivered'})
            else:
                self._update_outbox_status(row.get('id'), 'pending', updated)
                result['failed'] += 1
                result['items'].append({'id': row.get('id'), 'status': 'pending'})
        return result

    def _send_discord_webhook(self, content: str) -> bool:
        webhook_url = self.discord_cfg.get('webhook_url')
        if not webhook_url:
            return False
        payload_dict = {'content': content}
        if self.discord_cfg.get('webhook_username'):
            payload_dict['username'] = self.discord_cfg.get('webhook_username')
        payload = json.dumps(payload_dict).encode('utf-8')
        req = request.Request(webhook_url, data=payload, headers=self._http_headers)
        try:
            with request.urlopen(req, timeout=10) as resp:
                return 200 <= getattr(resp, 'status', 204) < 300
        except error.URLError:
            return False
        except error.HTTPError:
            return False

    def _send_discord_bot(self, content: str, components: List[Dict] = None) -> bool:
        bot_token = self.discord_cfg.get('bot_token')
        channel_id = self.discord_cfg.get('channel_id')
        if not bot_token or not channel_id:
            return False
        url = f'https://discord.com/api/v10/channels/{channel_id}/messages'
        payload_data = {'content': content}
        if components:
            payload_data['components'] = components
        payload = json.dumps(payload_data).encode('utf-8')
        headers = dict(self._http_headers)
        headers['Authorization'] = f'Bot {bot_token}'
        req = request.Request(url, data=payload, headers=headers)
        try:
            with request.urlopen(req, timeout=10) as resp:
                return 200 <= getattr(resp, 'status', 204) < 300
        except error.URLError:
            return False
        except error.HTTPError:
            return False

    def _send_discord(self, content: str, components: List[Dict] = None) -> bool:
        # Buttons/components require bot API, webhook doesn't support them
        if components:
            return self._send_discord_bot(content, components)
        return self._send_discord_webhook(content) or self._send_discord_bot(content)

    def _dedupe_window(self, event_type: str) -> int:
        windows = {
            'signal': 300,
            'decision': 90,
            'runtime': 300,
            'trade': 30,
            'close': 30,
            'error': 180,
        }
        return int(windows.get(event_type, 60))

    def _message_key(self, event_type: str, body: str) -> str:
        return f"{event_type}:{hashlib.md5(body.encode('utf-8')).hexdigest()}"

    def _consume_aggregate_summary(self, message_key: str, window: int) -> Optional[str]:
        bucket = self._aggregate_messages.pop(message_key, None)
        if not bucket or bucket.get('count', 0) <= 0:
            return None
        return f"最近已合并 {bucket['count']} 次同类通知（约 {window}s 内）"

    def _should_suppress(self, event_type: str, body: str) -> tuple[bool, str, Optional[str]]:
        now = datetime.now().timestamp()
        key = self._message_key(event_type, body)
        window = self._dedupe_window(event_type)
        last = self._recent_messages.get(key)
        self._recent_messages = {k: v for k, v in self._recent_messages.items() if now - v < 3600}
        if last and now - last < window:
            bucket = self._aggregate_messages.get(key) or {'count': 0, 'first_at': now}
            bucket['count'] = int(bucket.get('count', 0)) + 1
            bucket['last_at'] = now
            self._aggregate_messages[key] = bucket
            return True, key, None
        self._recent_messages[key] = now
        return False, key, self._consume_aggregate_summary(key, window)

    def _render_message(self, title: str, lines: List[str]) -> str:
        clean_lines = []
        for line in lines or []:
            if line is None:
                continue
            text = str(line).strip()
            if not text:
                continue
            if text == '---':
                clean_lines.append('')
                continue
            clean_lines.append(f'• {text}')
        divider = '--------------------------------------------------------------'
        return '\n'.join([divider, f'**{title}**', *clean_lines, divider])

    def _format_strategies(self, strategies: List[str]) -> str:
        return ' / '.join(strategies or []) or '--'

    def _format_price(self, price) -> str:
        if price in (None, '', '--'):
            return '--'
        try:
            return f'{float(price):,.6f}'.rstrip('0').rstrip('.')
        except Exception:
            return str(price)

    def _format_quantity(self, quantity) -> str:
        if quantity in (None, '', '--'):
            return '--'
        try:
            return f'{float(quantity):,.4f}'.rstrip('0').rstrip('.')
        except Exception:
            return str(quantity)

    def _format_time(self, value=None) -> str:
        if not value:
            value = datetime.now().isoformat()
        try:
            return datetime.fromisoformat(str(value).replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return str(value)

    def _priority_meta(self, priority: str = 'normal') -> Dict:
        mapping = {
            'normal': {'label': '普通', 'emoji': 'ℹ️'},
            'high': {'label': '重要', 'emoji': '⚠️'},
            'urgent': {'label': '紧急', 'emoji': '🚨'},
        }
        return mapping.get(priority, mapping['normal'])

    def _context_preview(self, details: Dict = None, limit: int = 3) -> str:
        if not details:
            return '--'
        pairs = []
        for key, value in list(details.items())[:limit]:
            if isinstance(value, dict):
                continue
            pairs.append(f'{key}={value}')
        return ' | '.join(pairs) if pairs else '--'

    def send(self, event_type: str, title: str, lines: List[str], level: str = 'info', details: Dict = None, priority: str = 'normal', components: List[Dict] = None) -> Dict:
        body = self._render_message(title, lines)
        suppressed, message_key, aggregate_summary = self._should_suppress(event_type, body)
        if aggregate_summary:
            lines = [*lines, '---', '【聚合摘要】', aggregate_summary]
            body = self._render_message(title, lines)
        outbox_id = self._store_event(level, event_type, body, details, title)
        delivered = False
        enabled = self._is_enabled(event_type)
        outbox_status = 'pending'
        if not enabled:
            outbox_status = 'disabled'
        elif suppressed:
            outbox_status = 'suppressed'
        else:
            delivered = self._send_discord(body, components)
            outbox_status = 'delivered' if delivered else 'pending'
        self._update_outbox_status(outbox_id, outbox_status, {
            **(details or {}),
            'event_type': event_type,
            'level': level,
            'title': title,
            'priority': priority,
            'message_key': message_key,
            'aggregate_summary': aggregate_summary,
            'delivery': {
                'enabled': enabled,
                'suppressed': suppressed,
                'delivered': delivered,
                'path': 'direct' if delivered else 'bridge_pending',
            }
        })
        return {'delivered': delivered, 'enabled': enabled, 'suppressed': suppressed, 'message': body, 'outbox_id': outbox_id, 'outbox_status': outbox_status, 'priority': priority, 'aggregate_summary': aggregate_summary}

    def notify_signal(self, signal, passed: bool, reason: str = None, details: Dict = None) -> Dict:
        signal_type = getattr(signal, 'signal_type', None)
        if signal_type not in {'buy', 'sell'}:
            return {'delivered': False, 'enabled': False, 'suppressed': True, 'message': '', 'outbox_id': None, 'outbox_status': 'suppressed', 'priority': 'normal', 'aggregate_summary': None}
        title = '📡 可靠信号' if passed else '🧪 信号已生成'
        direction = '🟢 做多' if signal_type == 'buy' else '🔴 做空'
        priority = 'high' if passed else 'normal'
        pmeta = self._priority_meta(priority)
        lines = [
            '【信号概览】',
            f'通知等级：{pmeta["emoji"]} {pmeta["label"]}',
            f'币种：{signal.symbol}',
            f'方向：{direction}',
            f'当前价格：{self._format_price(signal.price)}',
            f'信号强度：{signal.strength}% ',
            '---',
            '【触发依据】',
            f'策略组合：{self._format_strategies(signal.strategies_triggered or [])}',
            f'状态：{"通过初筛" if passed else "未通过初筛"}',
            f'原因：{reason or "--"}',
            '---',
            '【时间信息】',
            f'触发时间：{self._format_time(getattr(signal, "timestamp", None))}',
        ]
        return self.send('signal', title, lines, 'info', {'signal': signal.to_dict() if hasattr(signal, 'to_dict') else {}, 'details': details or {}}, priority=priority)

    def notify_decision(self, signal, allowed: bool, reason: str = None, details: Dict = None) -> Dict:
        title = '🤖 机器人决策：通过' if allowed else '🛑 机器人决策：拒绝'
        direction = '🟢 做多' if signal.signal_type == 'buy' else '🔴 做空' if signal.signal_type == 'sell' else '⚪ 观望'
        details = details or {}
        priority = 'high' if allowed else 'urgent'
        pmeta = self._priority_meta(priority)
        lines = [
            '【决策概览】',
            f'通知等级：{pmeta["emoji"]} {pmeta["label"]}',
            f'币种：{signal.symbol}',
            f'方向：{direction}',
            f'当前价格：{self._format_price(signal.price)}',
            f'信号强度：{signal.strength}%',
            '---',
            '【策略与结论】',
            f'触发策略：{self._format_strategies(signal.strategies_triggered or [])}',
            f'决策结果：{"✅ 允许执行" if allowed else "⛔ 拒绝执行"}',
            f'原因：{reason or "--"}',
        ]
        failed_checks = [f"{k}: {v.get('reason', '未通过')}" for k, v in details.items() if isinstance(v, dict) and not v.get('passed', True)]
        if failed_checks:
            lines.extend(['---', '【风控拦截】', f'拒绝明细：{" | ".join(failed_checks[:3])}'])
        return self.send('decision', title, lines, 'info' if allowed else 'warning', {'signal': signal.to_dict() if hasattr(signal, 'to_dict') else {}, 'details': details}, priority=priority)

    def notify_trade_open(self, symbol: str, side: str, price: float, quantity: float, trade_id: int = None, signal=None, quantity_details: Dict = None) -> Dict:
        priority = 'high'
        pmeta = self._priority_meta(priority)
        quantity_details = quantity_details or {}
        lines = [
            '【成交概览】',
            f'通知等级：{pmeta["emoji"]} {pmeta["label"]}',
            f'币种：{symbol}',
            f'方向：{"🟢 做多" if side == "long" else "🔴 做空"}',
            f'成交价格：{self._format_price(price)}',
        ]
        if quantity_details:
            lines.extend([
                f'下单张数：{self._format_quantity(quantity_details.get("contracts", quantity))}',
                f'每张面值：{self._format_quantity(quantity_details.get("contract_size"))}',
                f'折算数量：{self._format_quantity(quantity_details.get("coin_quantity"))} {symbol.split("/")[0]}',
                f'估算名义价值：{self._format_price(quantity_details.get("notional_usdt"))} USDT',
            ])
        else:
            lines.append(f'成交数量：{self._format_quantity(quantity)}')
        lines.extend([
            '---',
            '【关联信息】',
            f'Trade ID：{trade_id or "--"}',
            f'信号强度：{getattr(signal, "strength", "--")}',
            f'触发策略：{self._format_strategies(getattr(signal, "strategies_triggered", []) or [])}',
            f'建议动作：观察止盈止损是否按预期挂单/触发',
        ])
        return self.send('trade', '✅ 开仓执行成功', lines, 'info', {'trade_id': trade_id, 'symbol': symbol, 'side': side, 'quantity_details': quantity_details}, priority=priority)

    def notify_trade_open_failed(self, symbol: str, side: str, price: float, reason: str, signal=None, details: Dict = None) -> Dict:
        priority = 'urgent'
        pmeta = self._priority_meta(priority)
        lines = [
            '【失败概览】',
            f'通知等级：{pmeta["emoji"]} {pmeta["label"]}',
            f'币种：{symbol}',
            f'方向：{"🟢 做多" if side == "long" else "🔴 做空"}',
            f'尝试价格：{self._format_price(price)}',
            f'失败原因：{reason or "--"}',
            '---',
            '【关联信息】',
            f'信号强度：{getattr(signal, "strength", "--")}',
            f'触发策略：{self._format_strategies(getattr(signal, "strategies_triggered", []) or [])}',
            '---',
            '【建议动作】',
            '优先检查交易所返回码、仓位模式、下单参数与余额',
        ]
        return self.send('trade', '❌ 开仓执行失败', lines, 'error', details or {}, priority=priority)

    def notify_trade_close(self, symbol: str, side: str, close_price: float, reason: str, pnl: float = None) -> Dict:
        priority = 'high'
        pmeta = self._priority_meta(priority)
        pnl_value = None if pnl is None else float(pnl)
        pnl_icon = '🟢' if pnl_value is not None and pnl_value >= 0 else '🔴'
        pnl_text = '--' if pnl is None else f"{pnl_icon} {pnl_value:+.4f}"
        lines = [
            '【平仓概览】',
            f'通知等级：{pmeta["emoji"]} {pmeta["label"]}',
            f'币种：{symbol}',
            f'方向：{"🟢 做多" if side == "long" else "🔴 做空"}',
            f'平仓价格：{self._format_price(close_price)}',
            f'收益结果：{pnl_text}',
            '---',
            '【执行原因】',
            f'触发原因：{reason}',
            f'建议动作：复盘本次退出是否符合 TP / SL / 风控预期',
        ]
        return self.send('close', '📦 平仓执行', lines, 'info', {'symbol': symbol, 'side': side, 'reason': reason, 'pnl': pnl}, priority=priority)

    def notify_trade_close_failed(self, symbol: str, side: str, reason: str, details: Dict = None) -> Dict:
        priority = 'urgent'
        pmeta = self._priority_meta(priority)
        lines = [
            '【失败概览】',
            f'通知等级：{pmeta["emoji"]} {pmeta["label"]}',
            f'币种：{symbol}',
            f'方向：{"🟢 做多" if side == "long" else "🔴 做空"}',
            f'失败原因：{reason or "--"}',
            '---',
            '【建议动作】',
            '立即检查是否存在未平仓风险，并核对交易所真实持仓',
        ]
        return self.send('close', '❌ 平仓执行失败', lines, 'error', details or {}, priority=priority)

    def notify_reconcile_issue(self, report: Dict) -> Dict:
        summary = report.get('summary', {}) if isinstance(report, dict) else {}
        lines = [
            '【对账摘要】',
            f'交易所持仓：{summary.get("exchange_positions", 0)}',
            f'本地持仓：{summary.get("local_positions", 0)}',
            f'本地 open trades：{summary.get("open_trades", 0)}',
            '---',
            '【异常差异】',
            f'local 缺失：{summary.get("exchange_missing_local_position", 0)}',
            f'exchange 缺失：{summary.get("local_position_missing_exchange", 0)}',
            f'openTrade 缺失：{summary.get("exchange_missing_open_trade", 0)}',
            f'openTrade 脏记录：{summary.get("open_trade_missing_exchange", 0)}',
        ]
        healed = int(summary.get('healed_open_trades', 0) or 0)
        if healed > 0:
            lines.extend(['---', '【自动修复】', f'已自动补建 open trades：{healed}'])
        return self.send('reconcile', '⚠️ 持仓对账异常', lines, 'warning', report, priority='high')

    def notify_error(self, title: str, message: str, details: Dict = None) -> Dict:
        priority = 'urgent'
        pmeta = self._priority_meta(priority)
        lines = ['【异常说明】', f'通知等级：{pmeta["emoji"]} {pmeta["label"]}', message]
        if details:
            preview = self._context_preview(details)
            if preview and preview != '--':
                lines.extend(['---', '【上下文】', preview])
        lines.extend(['---', '【建议动作】', '先看最近错误日志、通知失败记录与运行状态'])
        return self.send('error', f'❌ {title}', lines, 'error', details or {}, priority=priority)

    def notify_loss_streak_lock(self, current: int, max_count: int, recover_at: str, details: Dict = None) -> Dict:
        priority = 'urgent'
        pmeta = self._priority_meta(priority)
        lines = [
            '【风控锁定】',
            f'通知等级：{pmeta["emoji"]} {pmeta["label"]}',
            f'连续亏损：{current}/{max_count}',
            f'自动恢复：{self._format_time(recover_at)}',
            '---',
            '【系统行为】',
            '新开仓已暂停，但信号分析会继续运行。',
            '---',
            '【建议动作】',
            '可在 dashboard 手动清零恢复；若不处理，系统会在冷却结束后自动恢复。',
        ]
        
        # Build Discord buttons with dashboard link (MVP: using link buttons for fallback)
        components = None
        # Use property if available, otherwise fall back to dict access
        if hasattr(self.config, 'dashboard_config'):
            dashboard_cfg = self.config.dashboard_config
            if callable(dashboard_cfg):
                dashboard_cfg = dashboard_cfg()
        else:
            dashboard_cfg = self.config.get('dashboard', {})
        dashboard_host = dashboard_cfg.get('host', '0.0.0.0')
        dashboard_port = dashboard_cfg.get('port', 8050)
        # Use localhost for local access, or bind address for remote
        dashboard_url = f'http://localhost:{dashboard_port}' if dashboard_host in ('0.0.0.0', '127.0.0.1') else f'http://{dashboard_host}:{dashboard_port}'
        
        # Approval actions metadata for OpenClaw bridge (protocol layer)
        # OpenClaw can parse this to build interactive buttons
        approval_actions = {
            'type': 'loss_streak_reset',
            'label': '手动清零恢复',
            'method': 'POST',
            'endpoint': '/api/risk/loss-streak/reset',
            'idempotent': True,
            'payload': {
                'note': 'discord-approval'
            }
        }
        
        # Only add buttons if bot_token is configured (required for components)
        if self.discord_cfg.get('bot_token') and self.discord_cfg.get('channel_id'):
            components = [
                {
                    'type': 1,  # ACTION_ROW
                    'components': [
                        {
                            'type': 2,  # BUTTON
                            'style': 5,  # LINK
                            'label': '🎛️ Dashboard 审批',
                            'url': dashboard_url,
                        },
                    ]
                }
            ]
        
        # Merge approval_actions into details for OpenClaw bridge to consume
        merged_details = {**(details or {}), 'approval_actions': approval_actions}
        
        return self.send('error', '🛑 连亏熔断已触发', lines, 'warning', merged_details, priority=priority, components=components)

    def notify_runtime(self, phase: str, lines: List[str], details: Dict = None) -> Dict:
        title_map = {
            'start': '⏱️ 机器人周期开始',
            'end': '✅ 机器人周期完成',
            'skip': '⏭️ 机器人周期跳过',
            'daemon': '🔁 守护模式启动',
        }
        level_map = {'start': 'info', 'end': 'info', 'skip': 'warning', 'daemon': 'info'}
        priority_map = {'start': 'normal', 'end': 'normal', 'skip': 'high', 'daemon': 'normal'}
        normalized_lines = []
        for line in lines or []:
            text = str(line)
            if '：' in text:
                key, value = text.split('：', 1)
                if any(token in key for token in ['时间', '开始', '结束', '完成', '触发时间']):
                    normalized_lines.append(f'{key}：{self._format_time(value.strip())}')
                    continue
            normalized_lines.append(text)
        return self.send('runtime', title_map.get(phase, '🤖 机器人运行状态'), normalized_lines, level_map.get(phase, 'info'), details or {}, priority=priority_map.get(phase, 'normal'))

    def test_discord(self) -> Dict:
        now = datetime.now().isoformat()
        return self.send('decision', '🔔 Discord 通知测试', [f'时间：{self._format_time(now)}', '如果你见到呢条消息，代表 webhook 推送链路正常'], 'info', {'time': now, 'kind': 'notify-test'})
