"""
交易所API封装模块
"""
import ccxt
from typing import Dict, List, Optional
from datetime import datetime


class Exchange:
    """交易所API封装类"""
    
    def __init__(self, config: Dict):
        self.config = config
        
        # 创建交易所实例
        exchange_id = config.get('exchange.name', 'okx')
        
        self.exchange = getattr(ccxt, exchange_id)({
            'apiKey': config.get('api.key', ''),
            'secret': config.get('api.secret', ''),
            'password': config.get('api.passphrase', ''),
            'enableRateLimit': True,
            'timeout': 30000,
            'testnet': config.get('exchange.mode', 'testnet') == 'testnet',
            'options': {
                'defaultType': config.get('exchange.default_type', 'swap')
            }
        })
    
    def fetch_balance(self) -> Dict:
        """获取余额"""
        return self.exchange.fetch_balance({'type': 'future'})
    
    def fetch_positions(self) -> List[Dict]:
        """获取持仓"""
        positions = self.exchange.fetch_positions()
        return [p for p in positions if float(p.get('contracts', 0) or 0) > 0]
    
    def fetch_ticker(self, symbol: str) -> Dict:
        """获取行情"""
        return self.exchange.fetch_ticker(symbol)
    
    def fetch_ohlcv(self, symbol: str, timeframe: str = '1h', 
                    limit: int = 100) -> List:
        """获取K线数据"""
        return self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    
    def create_order(self, symbol: str, side: str, amount: float,
                    posSide: str = None) -> Dict:
        """创建订单"""
        # 转换symbol格式
        if ':' not in symbol:
            symbol = symbol + ':USDT'
        
        params = {}
        if posSide:
            params['posSide'] = posSide
        
        if side == 'long':
            order = self.exchange.create_market_buy_order(symbol, amount, params)
        else:
            order = self.exchange.create_market_sell_order(symbol, amount, params)
        
        return order
    
    def close_order(self, symbol: str, side: str, amount: float,
                   posSide: str = None) -> Dict:
        """平仓"""
        # 转换symbol格式
        if ':' not in symbol:
            symbol = symbol + ':USDT'
        
        params = {}
        if posSide:
            params['posSide'] = posSide
        
        if side == 'long':
            order = self.exchange.create_market_sell_order(symbol, amount, params)
        else:
            order = self.exchange.create_market_buy_order(symbol, amount, params)
        
        return order
    
    def get_leverage(self, symbol: str) -> int:
        """获取杠杆"""
        return self.config.get('position.leverage', 10)
    
    def format_symbol(self, symbol: str) -> str:
        """格式化symbol"""
        if ':' not in symbol:
            return symbol + ':USDT'
        return symbol


class Position:
    """持仓数据类"""
    
    def __init__(self, data: Dict, current_price: float = None):
        self.symbol = data['symbol']
        self.side = data.get('side', 'long')
        self.entry_price = float(data.get('entryPrice', 0) or 0)
        self.contracts = float(data.get('contracts', 0) or 0)
        self.leverage = int(data.get('leverage', 1))
        self.current_price = current_price or self.entry_price
        
        # 计算仓位价值
        self.notional_value = self.contracts * self.current_price
        # 计算实际占用保证金
        self.margin_used = self.notional_value / self.leverage
    
    @property
    def unrealized_pnl(self) -> float:
        """未实现盈亏"""
        if self.side == 'long':
            return (self.current_price - self.entry_price) * self.contracts
        else:
            return (self.entry_price - self.current_price) * self.contracts
    
    @property
    def unrealized_pnl_percent(self) -> float:
        """未实现盈亏比例"""
        if self.entry_price == 0:
            return 0
        pnl = (self.current_price - self.entry_price) / self.entry_price * 100
        if self.side == 'short':
            pnl = -pnl
        return pnl * self.leverage
    
    def to_dict(self) -> Dict:
        """转为字典"""
        return {
            'symbol': self.symbol,
            'side': self.side,
            'entry_price': self.entry_price,
            'current_price': self.current_price,
            'contracts': self.contracts,
            'notional_value': self.notional_value,
            'margin_used': self.margin_used,
            'unrealized_pnl': self.unrealized_pnl,
            'unrealized_pnl_percent': self.unrealized_pnl_percent,
            'leverage': self.leverage
        }
