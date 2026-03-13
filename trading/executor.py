"""
交易执行模块
"""
from typing import Dict, Optional
from core.config import Config
from core.exchange import Exchange
from core.database import Database
from core.logger import trade_logger


class TradingExecutor:
    """交易执行器"""
    
    def __init__(self, config: Config, exchange: Exchange, db: Database):
        self.config = config
        self.exchange = exchange
        self.db = db
        self.position_config = config.get('position', {})
        self.risk_config = config.get('risk', {})
    
    def open_position(self, symbol: str, side: str, 
                    signal_price: float, signal_id: int = None) -> Optional[int]:
        """开仓"""
        
        # 获取余额
        balance = self.exchange.fetch_balance()
        available = balance['free'].get('USDT', 0)
        
        if available < self.position_config.get('min_balance', 1000):
            trade_logger.warning(f"余额不足: {available}")
            return None
        
        # 计算开仓数量
        leverage = self.position_config.get('leverage', 10)
        position_size = self.position_config.get('single_limit', 0.1)
        
        # 考虑币种权重
        symbol_base = symbol.split('/')[0]
        symbol_configs = self.config.get('symbols.configs', {})
        weight = symbol_configs.get(symbol_base, {}).get('weight', 0.2)
        
        # 实际使用仓位
        actual_position_size = position_size * weight
        amount = available * actual_position_size * leverage / signal_price
        
        # 最小数量限制
        if amount < 1:
            amount = 1
        
        amount = round(amount, 2)
        
        try:
            # 开仓
            posSide = 'long' if side == 'long' else 'short'
            order = self.exchange.create_order(symbol, side, amount, posSide)
            
            # 记录交易
            trade_data = {
                'symbol': symbol,
                'side': side,
                'entry_price': signal_price,
                'quantity': amount,
                'leverage': leverage
            }
            trade_id = self.db.add_trade(trade_data)
            
            # 更新持仓
            self.db.update_position(
                symbol, side, signal_price, amount, leverage, signal_price
            )
            
            trade_logger.trade(
                symbol, side, signal_price, amount, trade_id
            )
            
            return trade_id
            
        except Exception as e:
            trade_logger.error(f"开仓失败: {e}", exc_info=True)
            return None
    
    def close_position(self, symbol: str, reason: str = 'manual',
                     close_price: float = None) -> bool:
        """平仓"""
        
        # 获取持仓
        positions = self.db.get_positions()
        position = next((p for p in positions if p['symbol'] == symbol), None)
        
        if not position:
            trade_logger.warning(f"无持仓: {symbol}")
            return False
        
        side = position['side']
        quantity = position['quantity']
        entry_price = position['entry_price']
        
        # 获取当前价格
        if close_price is None:
            ticker = self.exchange.fetch_ticker(symbol)
            close_price = ticker['last']
        
        try:
            # 平仓
            posSide = side
            order = self.exchange.close_order(symbol, side, quantity, posSide)
            
            # 更新交易记录
            trade_id = position.get('id')
            if trade_id:
                self.db.close_trade(trade_id, close_price, reason)
            
            # 删除持仓
            self.db.close_position(symbol)
            
            # 计算盈亏
            if side == 'long':
                pnl = (close_price - entry_price) * quantity
            else:
                pnl = (entry_price - close_price) * quantity
            
            trade_logger.close(symbol, close_price, pnl, reason)
            
            return True
            
        except Exception as e:
            trade_logger.error(f"平仓失败: {e}", exc_info=True)
            return False
    
    def check_stop_loss(self, symbol: str, current_price: float) -> bool:
        """检查止损"""
        
        positions = self.db.get_positions()
        position = next((p for p in positions if p['symbol'] == symbol), None)
        
        if not position:
            return False
        
        side = position['side']
        entry_price = position['entry_price']
        leverage = position['leverage']
        
        # 计算盈亏比例
        if side == 'long':
            pnl_percent = (current_price - entry_price) / entry_price * 100
        else:
            pnl_percent = (entry_price - current_price) / entry_price * 100
        
        # 杠杆后盈亏
        leveraged_pnl = pnl_percent * leverage
        
        stop_loss = self.risk_config.get('stop_loss', 0.02)
        
        if leveraged_pnl <= -stop_loss * 100:
            trade_logger.info(f"触发止损: {symbol} 亏损{leveraged_pnl:.2f}%")
            return True
        
        return False
    
    def check_take_profit(self, symbol: str, current_price: float,
                         highest_price: float = None) -> bool:
        """检查止盈"""
        
        positions = self.db.get_positions()
        position = next((p for p in positions if p['symbol'] == symbol), None)
        
        if not position:
            return False
        
        side = position['side']
        entry_price = position['entry_price']
        leverage = position['leverage']
        
        # 使用追踪止损
        trailing_stop = self.risk_config.get('trailing_stop', 0.02)
        
        # 计算最高/最低价
        if highest_price is None:
            highest_price = current_price
        
        if side == 'long':
            # 多仓：追踪最高价
            stop_price = highest_price * (1 - trailing_stop)
            if current_price <= stop_price:
                trade_logger.info(f"触发追踪止损: {symbol}")
                return True
        else:
            # 空仓：追踪最低价
            lowest_price = current_price
            stop_price = lowest_price * (1 + trailing_stop)
            if current_price >= stop_price:
                trade_logger.info(f"触发追踪止损: {symbol}")
                return True
        
        # 检查普通止盈
        take_profit = self.risk_config.get('take_profit', 0.04)
        
        if side == 'long':
            pnl_percent = (current_price - entry_price) / entry_price * 100
        else:
            pnl_percent = (entry_price - current_price) / entry_price * 100
        
        leveraged_pnl = pnl_percent * leverage
        
        if leveraged_pnl >= take_profit * 100:
            trade_logger.info(f"触发止盈: {symbol} 盈利{leveraged_pnl:.2f}%")
            return True
        
        return False
