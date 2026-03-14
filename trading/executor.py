"""
交易执行模块 - 增强版
"""
import time
from typing import Dict, List, Optional, Any
from datetime import datetime
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
                    leverage=leverage,
                    current_price=current_price
                )
                
                # 更新冷却时间
                self._update_cooldown(symbol)
                
                trade_logger.trade(
                    symbol, side, current_price, amount, trade_id
                )
                
                return trade_id
                
            except Exception as e:
                trade_logger.error(f"开仓失败 (尝试 {attempt + 1}/{max_retries}): {e}")
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
                    pnl = (close_price - entry_price) * quantity
                    pnl_percent = (close_price - entry_price) / entry_price * 100
                else:
                    pnl = (entry_price - close_price) * quantity
                    pnl_percent = (entry_price - close_price) / entry_price * 100
                
                # 杠杆后盈亏
                leverage = position.get('leverage', 1)
                leveraged_pnl_percent = pnl_percent * leverage
                
                # 更新交易记录
                trade_id = position.get('id')
                if trade_id:
                    self.db.close_trade(
                        trade_id=trade_id,
                        exit_price=close_price,
                        pnl=pnl,
                        pnl_percent=leveraged_pnl_percent,
                        notes=f"平仓原因: {reason}"
                    )
                
                # 删除持仓
                self.db.close_position(symbol)
                
                # 更新冷却时间
                self._update_cooldown(symbol)
                
                trade_logger.close(symbol, close_price, pnl, reason)
                
                return True
                
            except Exception as e:
                trade_logger.error(f"平仓失败 (尝试 {attempt + 1}/{max_retries}): {e}")
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
            return False
        
        side = position['side']
        entry_price = position['entry_price']
        leverage = position.get('leverage', 1)
        
        # 追踪止损
        trailing_stop = self.trading_config.get('trailing_stop', 0.015)
        
        # 追踪最高价/最低价
        if highest_price is None:
            highest_price = current_price
        
        if side == 'long':
            # 多仓追踪最高价
            stop_price = highest_price * (1 - trailing_stop)
            if current_price <= stop_price:
                pnl_percent = (current_price - entry_price) / entry_price * leverage
                trade_logger.info(f"触发追踪止损: {symbol} 盈利{pnl_percent*100:.2f}%")
                return True
        else:
            # 空仓追踪最低价
            lowest_price = current_price
            stop_price = lowest_price * (1 + trailing_stop)
            if current_price >= stop_price:
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
            value = p.get('quantity', 0) * p.get('current_price', 0)
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
        """检查交易冷却"""
        cooldown_minutes = self.trading_config.get('cooldown_minutes', 15)
        
        if symbol in self._trade_cache:
            last_trade = self._trade_cache[symbol].get('last_trade')
            if last_trade:
                diff_minutes = (datetime.now() - last_trade).total_seconds() / 60
                if diff_minutes < cooldown_minutes:
                    return False
        
        return True
    
    def _update_cooldown(self, symbol: str):
        """更新冷却时间"""
        if symbol not in self._trade_cache:
            self._trade_cache[symbol] = {}
        self._trade_cache[symbol]['last_trade'] = datetime.now()


class RiskManager:
    """风险管理器"""
    
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.trading_config = config.get('trading', {})
        self._exchange = None
    
    def can_open_position(self, symbol: str) -> tuple:
        """
        检查是否可以开仓
        
        Returns:
            (can_open: bool, reason: str, details: dict)
        """
        details = {}
        
        # 1. 检查每日交易次数
        max_trades = self.trading_config.get('max_trades_per_day', 10)
        today_trades = self._get_today_trade_count()
        
        if today_trades >= max_trades:
            details['daily_limit'] = {'passed': False, 'reason': f'已达每日交易上限({today_trades})'}
            return False, f"已达每日交易上限({today_trades}/{max_trades})", details
        
        details['daily_limit'] = {'passed': True, 'count': today_trades, 'max': max_trades}
        
        # 2. 检查全局冷却
        min_interval = self.trading_config.get('min_trade_interval', 300)
        last_trade = self._get_last_trade_time()
        if last_trade:
            diff_seconds = (datetime.now() - last_trade).total_seconds()
            if diff_seconds < min_interval:
                details['global_cooldown'] = {
                    'passed': False, 
                    'remaining': min_interval - diff_seconds
                }
                return False, f"全局冷却中({int(diff_seconds)}s)", details
        
        details['global_cooldown'] = {'passed': True}
        
        # 3. 检查总持仓比例
        max_exposure = self.trading_config.get('max_exposure', 0.3)
        current_exposure = self._get_current_exposure()
        
        if current_exposure >= max_exposure:
            details['exposure_limit'] = {'passed': False, 'current': current_exposure}
            return False, f"已达最大持仓比例({current_exposure*100:.0f}%)", details
        
        details['exposure_limit'] = {'passed': True, 'current': current_exposure}
        
        return True, None, details
    
    def _get_today_trade_count(self) -> int:
        """获取今日交易次数（不只看 open，避免低估）"""
        trades = self.db.get_trades(limit=1000)
        today = datetime.now().date()
        count = 0
        for trade in trades:
            open_time = trade.get('open_time', '')
            if open_time and datetime.fromisoformat(open_time).date() == today:
                count += 1
        return count
    
    def _get_last_trade_time(self) -> Optional[datetime]:
        """获取上次交易时间"""
        trades = self.db.get_trades(limit=1)
        
        if trades:
            open_time = trades[0].get('open_time', '')
            if open_time:
                return datetime.fromisoformat(open_time)
        
        return None
    
    def _get_current_exposure(self) -> float:
        """获取当前持仓风险占比（按保证金占用，而不是按全仓名义价值）"""
        positions = self.db.get_positions()
        try:
            from core.exchange import Exchange
            if self._exchange is None:
                self._exchange = Exchange(self.config.all)
            balance = self._exchange.fetch_balance()
            total_balance = float(balance.get('total', {}).get('USDT', 0) or balance.get('free', {}).get('USDT', 1) or 1)
        except Exception:
            total_balance = 1.0

        total_margin_used = 0.0
        for p in positions:
            qty = float(p.get('quantity', 0) or 0)
            px = float(p.get('current_price', 0) or p.get('entry_price', 0) or 0)
            lev = max(1, int(p.get('leverage', 1) or 1))
            total_margin_used += (qty * px) / lev if qty and px else 0.0

        return total_margin_used / total_balance if total_balance > 0 else 0.0
