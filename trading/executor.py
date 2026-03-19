"""
交易执行模块 - 增强版
"""
import time
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from core.config import Config
from core.exchange import Exchange
from core.database import Database
from core.logger import trade_logger


class TradingExecutor:
    """交易执行器 - 增强版"""
    
    def __init__(self, config: Config, exchange: Exchange, db: Database):
        self.config = config
        self.exchange = exchange
        self.db = db
        self.trading_config = config.get('trading', {})
        self._trade_cache = {}  # 交易缓存

    def _exchange_has_position(self, symbol: str, side: str) -> bool:
        try:
            positions = self.exchange.fetch_positions()
        except Exception:
            return True
        for pos in positions or []:
            pos_symbol = pos.get('symbol') or pos.get('info', {}).get('instId') or ''
            if pos_symbol and ':' in pos_symbol:
                pos_symbol = pos_symbol.split(':')[0]
            pos_side = str(pos.get('side') or pos.get('info', {}).get('posSide') or '').lower()
            if pos_side in {'buy', 'long'}:
                pos_side = 'long'
            elif pos_side in {'sell', 'short'}:
                pos_side = 'short'
            contracts = float(pos.get('contracts', 0) or 0)
            if pos_symbol == symbol and pos_side == side and contracts > 0:
                return True
        return False

    def _close_local_position_as_stale(self, symbol: str, side: str, close_price: float, reason: str) -> bool:
        trade = self.db.get_latest_open_trade(symbol, side)
        trade_id = trade.get('id') if trade else None
        if trade_id:
            self.db.mark_trade_stale_closed(trade_id, reason, close_price=close_price)
        self.db.close_position(symbol)
        trade_logger.warning(f"{symbol}: 检测到交易所已无对应仓位，自动收口本地持仓/交易")
        return True
    
    def open_position(self, symbol: str, side: str, 
                    current_price: float, signal_id: int = None) -> Optional[int]:
        """开仓"""
        
        # 检查交易冷却
        if not self._check_cooldown(symbol):
            trade_logger.warning(f"{symbol}: 交易冷却中")
            return None
        
        # 获取余额
        try:
            balance = self.exchange.fetch_balance()
            available = balance.get('free', {}).get('USDT', 0)
        except Exception as e:
            trade_logger.error(f"获取余额失败: {e}")
            return None
        
        if available < 100:
            trade_logger.warning(f"余额不足: {available}")
            return None
        
        # 计算开仓数量（按目标名义价值 -> 合约张数）
        leverage = self.trading_config.get('leverage', 10)
        position_ratio = self.trading_config.get('position_size', 0.1)
        desired_notional = available * position_ratio * leverage
        try:
            if not self.exchange.is_futures_symbol(symbol):
                trade_logger.warning(f"{symbol}: 非U本位合约，跳过")
                return None
            amount = self.exchange.normalize_contract_amount(symbol, desired_notional, current_price)
            contract_size = self.exchange.get_contract_size(symbol) if hasattr(self.exchange, 'get_contract_size') else 1.0
            coin_quantity = self.exchange.contracts_to_coin_quantity(symbol, amount) if hasattr(self.exchange, 'contracts_to_coin_quantity') else amount * contract_size
        except Exception as e:
            trade_logger.error(f"计算下单数量失败: {e}")
            return None
        
        # 重试机制
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                # 开仓
                order = self.exchange.create_order(
                    symbol, 
                    'buy' if side == 'long' else 'sell', 
                    amount,
                    posSide=side
                )
                
                # 记录交易
                trade_id = self.db.record_trade(
                    symbol=symbol,
                    side=side,
                    entry_price=current_price,
                    quantity=amount,
                    contract_size=contract_size,
                    coin_quantity=coin_quantity,
                    leverage=leverage,
                    signal_id=signal_id,
                    notes=f"开仓尝试 #{attempt + 1}"
                )
                
                # 更新持仓
                self.db.update_position(
                    symbol=symbol,
                    side=side,
                    entry_price=current_price,
                    quantity=amount,
                    contract_size=contract_size,
                    coin_quantity=coin_quantity,
                    leverage=leverage,
                    current_price=current_price
                )
                
                # 更新冷却时间
                self._update_cooldown(symbol)
                self._seed_trailing_anchor(symbol, side, current_price)
                
                trade_logger.trade(
                    symbol, side, current_price, amount, trade_id
                )
                
                return trade_id
                
            except Exception as e:
                trade_logger.error(f"开仓失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if '51202' in str(e):
                    amount = round(amount * 0.5, 8)
                    trade_logger.warning(f"{symbol}: 市价单数量超过上限，自动缩量后重试 -> {amount}")
                    if amount <= 0:
                        return None
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    return None
    
    def close_position(self, symbol: str, reason: str = 'manual',
                     close_price: float = None) -> bool:
        """平仓 - U本位合约"""
        
        # 获取持仓
        positions = self.db.get_positions()
        position = None
        for p in positions:
            if p['symbol'] == symbol:
                position = p
                break
        
        if not position:
            trade_logger.warning(f"无持仓: {symbol}")
            return False
        
        side = position['side']  # 'long' or 'short'
        quantity = position['quantity']
        coin_quantity = float(position.get('coin_quantity', 0) or 0)
        contract_size = float(position.get('contract_size', 1) or 1)
        entry_price = position['entry_price']
        
        # 获取当前价格
        if close_price is None:
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                close_price = ticker['last']
            except Exception as e:
                trade_logger.error(f"获取价格失败: {e}")
                return False
        
        # 重试机制
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # U本位平仓 - 通过创建反向订单平仓
                # 多仓平空，空仓平多
                close_side = 'sell' if side == 'long' else 'buy'
                
                self.exchange.close_order(
                    symbol, 
                    close_side,
                    quantity,
                    posSide=side
                )
                
                # 计算盈亏
                if side == 'long':
                    pnl = (close_price - entry_price) * coin_quantity
                    pnl_percent = (close_price - entry_price) / entry_price * 100
                else:
                    pnl = (entry_price - close_price) * coin_quantity
                    pnl_percent = (entry_price - close_price) / entry_price * 100
                
                # 杠杆后盈亏
                leverage = position.get('leverage', 1)
                leveraged_pnl_percent = pnl_percent * leverage
                
                # 更新交易记录（positions.id ≠ trades.id，需回查最新 open trade）
                trade = self.db.get_latest_open_trade(symbol, side)
                trade_id = trade.get('id') if trade else None
                if trade_id:
                    self.db.close_trade(
                        trade_id=trade_id,
                        exit_price=close_price,
                        pnl=pnl,
                        pnl_percent=leveraged_pnl_percent,
                        notes=f"平仓原因: {reason}"
                    )
                else:
                    trade_logger.warning(f"{symbol}: 未找到可关闭的 open trade 记录，持仓会先从本地移除")
                
                # 删除持仓
                self.db.close_position(symbol)
                
                # 更新冷却时间
                self._update_cooldown(symbol)
                self._clear_trade_cache(symbol)
                
                trade_logger.close(symbol, close_price, pnl, reason)
                
                return True
                
            except Exception as e:
                message = str(e)
                trade_logger.error(f"平仓失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if '51169' in message and not self._exchange_has_position(symbol, side):
                    self._clear_trade_cache(symbol)
                    return self._close_local_position_as_stale(symbol, side, close_price, f'{reason} | 交易所已无对应仓位')
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    return False
        
        return False
    
    def check_stop_loss(self, symbol: str, current_price: float) -> bool:
        """检查止损"""
        
        positions = self.db.get_positions()
        position = None
        for p in positions:
            if p['symbol'] == symbol:
                position = p
                break
        
        if not position:
            return False
        
        side = position['side']
        entry_price = position['entry_price']
        leverage = position.get('leverage', 1)
        
        stop_loss = self.trading_config.get('stop_loss', 0.02)
        
        # 计算盈亏比例
        if side == 'long':
            pnl_percent = (current_price - entry_price) / entry_price
        else:
            pnl_percent = (entry_price - current_price) / entry_price
        
        # 杠杆后盈亏
        leveraged_pnl = pnl_percent * leverage
        
        if leveraged_pnl <= -stop_loss:
            trade_logger.info(f"触发止损: {symbol} 亏损{leveraged_pnl*100:.2f}%")
            return True
        
        return False
    
    def check_take_profit(self, symbol: str, current_price: float,
                         highest_price: float = None) -> bool:
        """检查止盈/追踪止损"""
        
        positions = self.db.get_positions()
        position = None
        for p in positions:
            if p['symbol'] == symbol:
                position = p
                break
        
        if not position:
            self._clear_trade_cache(symbol)
            return False
        
        side = position['side']
        entry_price = position['entry_price']
        leverage = position.get('leverage', 1)
        
        # 追踪止损
        trailing_stop = self.trading_config.get('trailing_stop', 0.015)
        cache = self._trade_cache.setdefault(symbol, {})
        
        if side == 'long':
            anchor = highest_price if highest_price is not None else cache.get('highest_price', position.get('peak_price') or entry_price)
            anchor = max(float(anchor or entry_price), float(position.get('peak_price') or entry_price), float(current_price or entry_price))
            cache['highest_price'] = anchor
            self.db.update_position(symbol, side, entry_price, position['quantity'], leverage, current_price, peak_price=anchor, trough_price=position.get('trough_price'), contract_size=position.get('contract_size', 1), coin_quantity=position.get('coin_quantity'))
            stop_price = anchor * (1 - trailing_stop)
            if current_price <= stop_price and current_price > entry_price:
                pnl_percent = (current_price - entry_price) / entry_price * leverage
                trade_logger.info(f"触发追踪止损: {symbol} 盈利{pnl_percent*100:.2f}%")
                return True
        else:
            anchor = cache.get('lowest_price', position.get('trough_price') or entry_price)
            anchor = min(float(anchor or entry_price), float(position.get('trough_price') or entry_price), float(current_price or entry_price))
            cache['lowest_price'] = anchor
            self.db.update_position(symbol, side, entry_price, position['quantity'], leverage, current_price, peak_price=position.get('peak_price'), trough_price=anchor, contract_size=position.get('contract_size', 1), coin_quantity=position.get('coin_quantity'))
            stop_price = anchor * (1 + trailing_stop)
            if current_price >= stop_price and current_price < entry_price:
                pnl_percent = (entry_price - current_price) / entry_price * leverage
                trade_logger.info(f"触发追踪止损: {symbol} 盈利{pnl_percent*100:.2f}%")
                return True
        
        # 普通止盈
        take_profit = self.trading_config.get('take_profit', 0.04)
        
        if side == 'long':
            pnl_percent = (current_price - entry_price) / entry_price
        else:
            pnl_percent = (entry_price - current_price) / entry_price
        
        leveraged_pnl = pnl_percent * leverage
        
        if leveraged_pnl >= take_profit:
            trade_logger.info(f"触发止盈: {symbol} 盈利{leveraged_pnl*100:.2f}%")
            return True
        
        return False
    
    def update_positions(self) -> Dict[str, Any]:
        """更新所有持仓状态"""
        positions = self.db.get_positions()
        updated = {}
        
        for position in positions:
            symbol = position['symbol']
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                
                # 更新持仓
                self.db.update_position(
                    symbol=symbol,
                    side=position['side'],
                    entry_price=position['entry_price'],
                    quantity=position['quantity'],
                    contract_size=position.get('contract_size', 1),
                    coin_quantity=position.get('coin_quantity'),
                    leverage=position['leverage'],
                    current_price=current_price
                )
                
                updated[symbol] = {
                    'current_price': current_price,
                    'updated': True
                }
                
            except Exception as e:
                trade_logger.error(f"更新{symbol}持仓失败: {e}")
                updated[symbol] = {'error': str(e)}
        
        return updated
    
    def get_portfolio_status(self) -> Dict[str, Any]:
        """获取投资组合状态"""
        positions = self.db.get_positions()
        
        total_pnl = 0
        total_value = 0
        
        for p in positions:
            unrealized_pnl = p.get('unrealized_pnl', 0)
            value = p.get('coin_quantity', 0) * p.get('current_price', 0)
            total_pnl += unrealized_pnl
            total_value += value
        
        # 获取交易统计
        trade_stats = self.db.get_trade_stats(days=30)
        
        return {
            'total_positions': len(positions),
            'total_value': total_value,
            'unrealized_pnl': total_pnl,
            'trade_stats': trade_stats,
            'positions': positions
        }
    
    def _check_cooldown(self, symbol: str) -> bool:
        """检查交易冷却（优先数据库，避免守护跨周期失效）"""
        cooldown_minutes = self.trading_config.get('cooldown_minutes', 15)
        last_trade = self.db.get_latest_trade_time(symbol)
        if not last_trade and symbol in self._trade_cache:
            last_trade = self._trade_cache[symbol].get('last_trade')
        if last_trade:
            diff_minutes = (datetime.utcnow() - last_trade).total_seconds() / 60
            if diff_minutes < cooldown_minutes:
                return False
        return True
    
    def _update_cooldown(self, symbol: str):
        """更新冷却时间"""
        if symbol not in self._trade_cache:
            self._trade_cache[symbol] = {}
        self._trade_cache[symbol]['last_trade'] = datetime.now()

    def _seed_trailing_anchor(self, symbol: str, side: str, price: float):
        if symbol not in self._trade_cache:
            self._trade_cache[symbol] = {}
        if side == 'long':
            self._trade_cache[symbol]['highest_price'] = price
        else:
            self._trade_cache[symbol]['lowest_price'] = price

    def _clear_trade_cache(self, symbol: str):
        self._trade_cache.pop(symbol, None)


class RiskManager:
    """风险管理器"""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.trading_config = config.get('trading', {})
        self._exchange = None

    def _loss_guard_enabled(self) -> bool:
        return bool(self.trading_config.get('loss_streak_lock_enabled', True))

    def _loss_guard_hours(self) -> int:
        return int(self.trading_config.get('loss_streak_cooldown_hours', 12) or 12)

    def _sync_loss_streak_guard(self) -> Dict[str, Any]:
        state = self.db.get_risk_guard_state('loss_streak')
        now = datetime.now()
        changed = False
        just_triggered = False
        auto_recovered = False

        trades = self.db.get_trades(status='closed', limit=200)
        new_trades = [t for t in reversed(trades) if int(t.get('id', 0) or 0) > int(state.get('last_trade_id', 0) or 0)]
        for trade in new_trades:
            state['last_trade_id'] = int(trade.get('id', 0) or state.get('last_trade_id', 0) or 0)
            pnl = trade.get('pnl')
            if pnl is None:
                changed = True
                continue
            pnl_value = float(pnl or 0)
            if pnl_value < 0:
                state['current_streak'] = int(state.get('current_streak', 0) or 0) + 1
                state.setdefault('details', {})['last_loss_at'] = trade.get('close_time') or trade.get('open_time')
            else:
                state['current_streak'] = 0
                state.setdefault('details', {})['last_win_at'] = trade.get('close_time') or trade.get('open_time')
                if state.get('lock_active'):
                    state['lock_active'] = 0
                    state['lock_until'] = None
                    state['triggered_at'] = None
            changed = True

        lock_until = state.get('lock_until')
        if state.get('lock_active') and lock_until:
            try:
                lock_dt = datetime.fromisoformat(str(lock_until))
            except Exception:
                lock_dt = None
            if lock_dt and now >= lock_dt:
                state['lock_active'] = 0
                state['lock_until'] = None
                state['triggered_at'] = None
                state['current_streak'] = 0
                state['reset_at'] = now.isoformat()
                auto_recovered = True
                changed = True

        max_consecutive_losses = int(self.trading_config.get('max_consecutive_losses', 3))
        if self._loss_guard_enabled() and not state.get('lock_active') and int(state.get('current_streak', 0) or 0) >= max_consecutive_losses:
            state['lock_active'] = 1
            state['triggered_at'] = now.isoformat()
            state['lock_until'] = (now + timedelta(hours=self._loss_guard_hours())).isoformat()
            state.setdefault('details', {})['max_consecutive_losses'] = max_consecutive_losses
            just_triggered = True
            changed = True

        if changed:
            self.db.save_risk_guard_state(state)
        state['just_triggered'] = just_triggered
        state['auto_recovered'] = auto_recovered
        return state

    def manual_reset_loss_streak(self, note: str = None) -> Dict[str, Any]:
        state = self.db.get_risk_guard_state('loss_streak')
        state['current_streak'] = 0
        state['lock_active'] = 0
        state['lock_until'] = None
        state['triggered_at'] = None
        state['reset_at'] = datetime.now().isoformat()
        details = state.get('details', {}) or {}
        if note:
            details['manual_reset_note'] = note
        state['details'] = details
        self.db.save_risk_guard_state(state)
        return state

    def can_open_position(self, symbol: str) -> tuple:
        """检查是否可以开仓"""
        details = {}

        max_trades = int(self.trading_config.get('max_trades_per_day', 10))
        today_trades = self._get_today_trade_count()
        if today_trades >= max_trades:
            details['daily_limit'] = {'passed': False, 'reason': f'已达每日交易上限({today_trades})'}
            return False, f"已达每日交易上限({today_trades}/{max_trades})", details
        details['daily_limit'] = {'passed': True, 'count': today_trades, 'max': max_trades}

        min_interval = int(self.trading_config.get('min_trade_interval', 300))
        last_trade = self._get_last_trade_time()
        if last_trade:
            diff_seconds = (datetime.utcnow() - last_trade).total_seconds()
            if diff_seconds < min_interval:
                details['global_cooldown'] = {'passed': False, 'remaining': int(min_interval - diff_seconds)}
                return False, f"全局冷却中({int(diff_seconds)}s)", details
        details['global_cooldown'] = {'passed': True}

        max_consecutive_losses = int(self.trading_config.get('max_consecutive_losses', 3))
        loss_guard = self._sync_loss_streak_guard()
        consecutive_losses = int(loss_guard.get('current_streak', 0) or 0)
        if loss_guard.get('lock_active'):
            details['loss_streak_limit'] = {
                'passed': False,
                'current': consecutive_losses,
                'max': max_consecutive_losses,
                'locked': True,
                'recover_at': loss_guard.get('lock_until'),
                'triggered_at': loss_guard.get('triggered_at'),
                'cooldown_hours': self._loss_guard_hours(),
                'just_triggered': bool(loss_guard.get('just_triggered')),
                'auto_recovered': bool(loss_guard.get('auto_recovered')),
            }
            return False, f"连续亏损熔断冷却中({consecutive_losses}/{max_consecutive_losses})", details
        details['loss_streak_limit'] = {
            'passed': True,
            'current': consecutive_losses,
            'max': max_consecutive_losses,
            'locked': False,
            'recover_at': loss_guard.get('lock_until'),
            'triggered_at': loss_guard.get('triggered_at'),
            'cooldown_hours': self._loss_guard_hours(),
            'just_triggered': bool(loss_guard.get('just_triggered')),
            'auto_recovered': bool(loss_guard.get('auto_recovered')),
        }

        max_daily_drawdown = float(self.trading_config.get('max_daily_drawdown', 0.03))
        daily_drawdown_ratio = self._get_daily_drawdown_ratio()
        if daily_drawdown_ratio >= max_daily_drawdown:
            details['daily_drawdown_limit'] = {
                'passed': False,
                'current': round(daily_drawdown_ratio, 4),
                'max': max_daily_drawdown
            }
            return False, f"日内回撤熔断({daily_drawdown_ratio*100:.2f}%/{max_daily_drawdown*100:.2f}%)", details
        details['daily_drawdown_limit'] = {
            'passed': True,
            'current': round(daily_drawdown_ratio, 4),
            'max': max_daily_drawdown
        }

        max_exposure = float(self.trading_config.get('max_exposure', 0.3))
        position_ratio = float(self.trading_config.get('position_size', 0.1))
        current_exposure = self._get_current_exposure()
        projected_exposure = current_exposure + position_ratio
        if projected_exposure > max_exposure:
            details['exposure_limit'] = {
                'passed': False,
                'current': round(current_exposure, 4),
                'projected': round(projected_exposure, 4),
                'max': max_exposure
            }
            return False, f"开仓后将超过最大持仓比例({projected_exposure*100:.0f}%)", details
        details['exposure_limit'] = {
            'passed': True,
            'current': round(current_exposure, 4),
            'projected': round(projected_exposure, 4),
            'max': max_exposure
        }

        return True, None, details

    def get_risk_status(self) -> Dict[str, Any]:
        """供仪表盘显示的风险状态"""
        balance = self._get_balance_summary()
        current_exposure = self._get_current_exposure()
        daily_drawdown = self._get_daily_drawdown_ratio()
        loss_guard = self._sync_loss_streak_guard()
        consecutive_losses = int(loss_guard.get('current_streak', 0) or 0)
        status = 'locked' if loss_guard.get('lock_active') else ('guarded' if (daily_drawdown > 0 or consecutive_losses > 0) else 'normal')
        return {
            'today_trades': self._get_today_trade_count(),
            'last_trade_time': self._get_last_trade_time().isoformat() if self._get_last_trade_time() else None,
            'current_exposure': round(current_exposure, 4),
            'max_exposure': float(self.trading_config.get('max_exposure', 0.3)),
            'position_size': float(self.trading_config.get('position_size', 0.1)),
            'daily_drawdown': round(daily_drawdown, 4),
            'max_daily_drawdown': float(self.trading_config.get('max_daily_drawdown', 0.03)),
            'consecutive_losses': consecutive_losses,
            'max_consecutive_losses': int(self.trading_config.get('max_consecutive_losses', 3)),
            'loss_streak_lock_enabled': self._loss_guard_enabled(),
            'loss_streak_cooldown_hours': self._loss_guard_hours(),
            'loss_streak_locked': bool(loss_guard.get('lock_active')),
            'loss_streak_recover_at': loss_guard.get('lock_until'),
            'loss_streak_triggered_at': loss_guard.get('triggered_at'),
            'balance': balance,
            'status': status
        }

    def _get_balance_summary(self) -> Dict[str, float]:
        try:
            from core.exchange import Exchange
            if self._exchange is None:
                self._exchange = Exchange(self.config.all)
            balance = self._exchange.fetch_balance()
            total = float(balance.get('total', {}).get('USDT', 0) or 0)
            free = float(balance.get('free', {}).get('USDT', 0) or 0)
            used = max(0.0, total - free)
            return {'total': round(total, 2), 'free': round(free, 2), 'used': round(used, 2)}
        except Exception:
            return {'total': 0.0, 'free': 0.0, 'used': 0.0}

    def _parse_trade_time(self, value: str) -> Optional[datetime]:
        if not value:
            return None
        return datetime.fromisoformat(value)

    def _get_today_trade_count(self) -> int:
        trades = self.db.get_trades(limit=1000)
        today = datetime.utcnow().date()
        count = 0
        for trade in trades:
            opened_at = self._parse_trade_time(trade.get('open_time', ''))
            if opened_at and opened_at.date() == today:
                count += 1
        return count

    def _get_last_trade_time(self) -> Optional[datetime]:
        return self.db.get_latest_trade_time()

    def _get_consecutive_losses(self) -> int:
        trades = self.db.get_trades(status='closed', limit=20)
        count = 0
        for trade in trades:
            pnl = float(trade.get('pnl', 0) or 0)
            if pnl < 0:
                count += 1
            else:
                break
        return count

    def _get_daily_drawdown_ratio(self) -> float:
        trades = self.db.get_trades(status='closed', limit=1000)
        today = datetime.utcnow().date()
        today_pnl = 0.0
        for trade in trades:
            realized_at = self._parse_trade_time(trade.get('close_time') or trade.get('open_time') or '')
            if realized_at and realized_at.date() == today:
                today_pnl += float(trade.get('pnl', 0) or 0)
        if today_pnl >= 0:
            return 0.0
        balance_total = self._get_balance_summary().get('total', 0.0) or 1.0
        return abs(today_pnl) / balance_total

    def _get_current_exposure(self) -> float:
        positions = self.db.get_positions()
        total_balance = self._get_balance_summary().get('total', 0.0) or 1.0
        total_margin_used = 0.0
        for p in positions:
            qty = float(p.get('coin_quantity', 0) or 0)
            px = float(p.get('current_price', 0) or p.get('entry_price', 0) or 0)
            lev = max(1, int(p.get('leverage', 1) or 1))
            total_margin_used += (qty * px) / lev if qty and px else 0.0
        return total_margin_used / total_balance if total_balance > 0 else 0.0
