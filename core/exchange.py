"""
交易所API封装模块 - 优化版
"""
import ccxt
from typing import Dict, List, Optional


class Exchange:
    """交易所API封装类"""

    def __init__(self, config: Dict):
        self.config = config
        exchange_id = config.get('exchange', {}).get('name', 'okx')
        api_config = config.get('api', {})
        self.leverage = config.get('trading', {}).get('leverage', 10)

        self.exchange = getattr(ccxt, exchange_id)({
            'apiKey': api_config.get('key', ''),
            'secret': api_config.get('secret', ''),
            'password': api_config.get('passphrase', ''),
            'enableRateLimit': True,
            'timeout': 30000,
            'testnet': config.get('exchange', {}).get('mode', 'testnet') == 'testnet',
            'options': {
                'defaultType': 'swap',
                'marginMode': 'isolated'
            }
        })
        self._markets = None
        self._set_default_leverage()

    def _load_markets(self):
        if self._markets is None:
            self._markets = self.exchange.load_markets()
        return self._markets

    def get_market(self, symbol: str) -> Optional[Dict]:
        markets = self._load_markets()
        candidates = [f'{symbol}:USDT', symbol]
        for candidate in candidates:
            market = markets.get(candidate)
            if market:
                return market
        return None

    def is_futures_symbol(self, symbol: str) -> bool:
        market = self.get_market(symbol)
        return bool(market and market.get('swap') and market.get('linear'))

    def get_order_symbol(self, symbol: str) -> str:
        market = self.get_market(symbol)
        if not market:
            raise ValueError(f'无可用市场: {symbol}')
        return market['symbol']

    def normalize_contract_amount(self, symbol: str, desired_notional_usdt: float, price: float) -> float:
        """把目标名义价值换算成 OKX 需要的 amount（合约张数）"""
        market = self.get_market(symbol)
        if not market:
            raise ValueError(f'无可用市场: {symbol}')
        contract_size = float(market.get('contractSize') or 1.0)
        raw_amount = desired_notional_usdt / max(contract_size * price, 1e-10)
        amount = float(self.exchange.amount_to_precision(market['symbol'], raw_amount))
        min_amount = float((market.get('limits', {}).get('amount', {}) or {}).get('min') or 0.0)
        if amount < min_amount:
            amount = min_amount
        return amount

    def _set_default_leverage(self):
        symbols = self.config.get('symbols', {}).get('watch_list', [])
        for symbol in symbols:
            try:
                if self.is_futures_symbol(symbol):
                    self.set_leverage(symbol, self.leverage)
            except Exception:
                pass

    def fetch_balance(self) -> Dict:
        return self.exchange.fetch_balance({'type': 'future'})

    def fetch_positions(self) -> List[Dict]:
        positions = self.exchange.fetch_positions()
        return [p for p in positions if float(p.get('contracts', 0) or 0) > 0]

    def fetch_ticker(self, symbol: str) -> Dict:
        return self.exchange.fetch_ticker(symbol)

    def fetch_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> List:
        return self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

    def set_leverage(self, symbol: str, leverage: int = 10):
        market = self.get_market(symbol)
        if not market or not market.get('swap'):
            return
        try:
            self.exchange.set_leverage(leverage, market['symbol'], {'marginMode': 'isolated'})
        except Exception:
            pass

    def _is_oneway_mode(self) -> bool:
        mode = str(self.config.get('exchange', {}).get('position_mode', 'oneway')).lower()
        return mode in {'oneway', 'one-way', 'net', 'single'}

    def _build_order_params(self, posSide: str = None, reduce_only: bool = False, include_pos_side: bool = True) -> Dict:
        params = {'tdMode': 'isolated'}
        if reduce_only:
            params['reduceOnly'] = True
        if posSide and include_pos_side and not self._is_oneway_mode():
            params['posSide'] = posSide
        return params

    def _submit_market_order(self, contract_symbol: str, side: str, amount: float, params: Dict) -> Dict:
        if side in ['buy', 'long']:
            return self.exchange.create_market_buy_order(contract_symbol, amount, params)
        return self.exchange.create_market_sell_order(contract_symbol, amount, params)

    def create_order(self, symbol: str, side: str, amount: float, posSide: str = None) -> Dict:
        contract_symbol = self.get_order_symbol(symbol)
        params = self._build_order_params(posSide=posSide)
        try:
            return self._submit_market_order(contract_symbol, side, amount, params)
        except Exception as e:
            message = str(e)
            if 'posSide' in params and 'Parameter posSide error' in message:
                fallback_params = self._build_order_params(posSide=posSide, include_pos_side=False)
                return self._submit_market_order(contract_symbol, side, amount, fallback_params)
            print(f'开仓错误: {e}')
            raise

    def close_order(self, symbol: str, side: str, amount: float, posSide: str = None) -> Dict:
        contract_symbol = self.get_order_symbol(symbol)
        params = self._build_order_params(posSide=posSide, reduce_only=True)
        try:
            return self._submit_market_order(contract_symbol, side, amount, params)
        except Exception as e:
            message = str(e)
            if 'posSide' in params and ('Parameter posSide error' in message or '51169' in message):
                fallback_params = self._build_order_params(posSide=posSide, reduce_only=True, include_pos_side=False)
                return self._submit_market_order(contract_symbol, side, amount, fallback_params)
            print(f'平仓错误: {e}')
            raise

    def get_leverage(self, symbol: str) -> int:
        return self.leverage

    def format_symbol(self, symbol: str) -> str:
        return self.get_order_symbol(symbol)


class Position:
    """持仓数据类"""

    def __init__(self, data: Dict, current_price: float = None):
        self.symbol = data['symbol']
        self.side = data.get('side', 'long')
        self.entry_price = float(data.get('entryPrice', 0) or 0)
        self.contracts = float(data.get('contracts', 0) or 0)
        self.leverage = int(data.get('leverage', 1))
        self.current_price = current_price or self.entry_price
        self.notional_value = self.contracts * self.current_price
        self.margin_used = self.notional_value / self.leverage

    @property
    def unrealized_pnl(self) -> float:
        if self.side == 'long':
            return (self.current_price - self.entry_price) * self.contracts
        return (self.entry_price - self.current_price) * self.contracts

    @property
    def unrealized_pnl_percent(self) -> float:
        if self.entry_price == 0:
            return 0
        pnl = (self.current_price - self.entry_price) / self.entry_price * 100
        if self.side == 'short':
            pnl = -pnl
        return pnl * self.leverage

    def to_dict(self) -> Dict:
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
