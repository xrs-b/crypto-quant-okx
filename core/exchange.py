"""
交易所API封装模块 - 优化版
"""
import ccxt
from datetime import datetime
from typing import Dict, List, Optional, Any


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

    def get_contract_size(self, symbol: str) -> float:
        market = self.get_market(symbol)
        if not market:
            raise ValueError(f'无可用市场: {symbol}')
        return float(market.get('contractSize') or 1.0)

    def contracts_to_coin_quantity(self, symbol: str, contracts: float) -> float:
        return float(contracts or 0) * self.get_contract_size(symbol)

    def estimate_notional_usdt(self, symbol: str, contracts: float, price: float) -> float:
        return self.contracts_to_coin_quantity(symbol, contracts) * float(price or 0)

    def normalize_contract_amount(self, symbol: str, desired_notional_usdt: float, price: float) -> float:
        """把目标名义价值换算成 OKX 需要的 amount（合约张数）"""
        market = self.get_market(symbol)
        if not market:
            raise ValueError(f'无可用市场: {symbol}')
        contract_size = float(market.get('contractSize') or 1.0)
        raw_amount = desired_notional_usdt / max(contract_size * price, 1e-10)
        amount = float(self.exchange.amount_to_precision(market['symbol'], raw_amount))
        amount_limits = (market.get('limits', {}).get('amount', {}) or {})
        min_amount = float(amount_limits.get('min') or 0.0)
        max_amount = float(amount_limits.get('max') or 0.0)
        if amount < min_amount:
            amount = min_amount
        if max_amount and amount > max_amount:
            amount = float(self.exchange.amount_to_precision(market['symbol'], max_amount))
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


    def normalize_symbol(self, symbol: str) -> str:
        raw = str(symbol or '').strip()
        if not raw:
            return raw
        if ':' in raw:
            raw = raw.split(':')[0]
        info_style = raw.replace('-', '/')
        if info_style.endswith('/SWAP'):
            info_style = info_style[:-5]
        if info_style.endswith('/USDT/SWAP'):
            info_style = info_style[:-5]
        if info_style.count('/') >= 2 and info_style.endswith('/SWAP'):
            parts = info_style.split('/')
            info_style = '/'.join(parts[:2])
        return info_style

    def normalize_side(self, side: str, fallback: str = 'long') -> str:
        side = str(side or '').lower()
        if side in {'buy', 'long'}:
            return 'long'
        if side in {'sell', 'short'}:
            return 'short'
        return fallback

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        try:
            if value in (None, ''):
                return default
            return float(value)
        except Exception:
            return default

    def _safe_int(self, value: Any, default: int = 0) -> int:
        try:
            if value in (None, ''):
                return default
            return int(float(value))
        except Exception:
            return default

    def _parse_time_ms(self, value: Any) -> Optional[int]:
        if value in (None, ''):
            return None
        try:
            if isinstance(value, (int, float)):
                value = int(value)
                return value if value > 10**11 else value * 1000
            s = str(value).strip()
            if s.isdigit():
                value = int(s)
                return value if value > 10**11 else value * 1000
            return int(datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp() * 1000)
        except Exception:
            return None

    def normalize_position(self, pos: Dict) -> Optional[Dict]:
        symbol = self.normalize_symbol(pos.get('symbol') or pos.get('info', {}).get('instId') or '')
        contracts = self._safe_float(pos.get('contracts') or pos.get('info', {}).get('pos') or 0)
        if not symbol or contracts <= 0:
            return None
        side = self.normalize_side(pos.get('side') or pos.get('info', {}).get('posSide') or 'long')
        contract_size = self.get_contract_size(symbol) if symbol else 1.0
        entry_price = self._safe_float(pos.get('entryPrice') or pos.get('entry_price') or pos.get('info', {}).get('avgPx') or pos.get('average') or 0)
        current_price = self._safe_float(pos.get('markPrice') or pos.get('last') or pos.get('info', {}).get('markPx') or pos.get('info', {}).get('last') or entry_price)
        leverage = self._safe_int(pos.get('leverage') or pos.get('info', {}).get('lever') or self.leverage, self.leverage)
        coin_quantity = self.contracts_to_coin_quantity(symbol, contracts)
        realized_pnl = self._safe_float(pos.get('realizedPnl') or pos.get('info', {}).get('realizedPnl') or pos.get('info', {}).get('uplLastPx') or 0)
        return {
            'symbol': symbol,
            'side': side,
            'quantity': contracts,
            'contracts': contracts,
            'contract_size': contract_size,
            'coin_quantity': coin_quantity,
            'entry_price': entry_price,
            'current_price': current_price,
            'leverage': leverage,
            'realized_pnl': realized_pnl,
            'raw': pos,
        }

    def fetch_positions(self) -> List[Dict]:
        positions = self.exchange.fetch_positions()
        normalized = []
        for pos in positions or []:
            row = self.normalize_position(pos)
            if row:
                normalized.append(row)
        return normalized

    def _fetch_my_trades_candidates(self, symbol: str = None, since: int = None, limit: int = 200) -> List[Dict]:
        candidates = []
        if symbol:
            normalized = self.normalize_symbol(symbol)
            try:
                candidates.append(self.get_order_symbol(normalized))
            except Exception:
                pass
            candidates.extend([normalized, symbol])
        seen = set()
        trades = []
        for candidate in candidates or [None]:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                if candidate is None:
                    rows = self.exchange.fetch_my_trades(since=since, limit=limit)
                else:
                    rows = self.exchange.fetch_my_trades(candidate, since=since, limit=limit)
                if rows:
                    trades.extend(rows)
                    break
            except Exception:
                continue
        return trades

    def normalize_trade_fill(self, trade: Dict) -> Dict:
        info = trade.get('info', {}) or {}
        symbol = self.normalize_symbol(trade.get('symbol') or info.get('instId') or '')
        contracts = self._safe_float(trade.get('amount') or info.get('fillSz') or info.get('sz') or 0)
        contract_size = self.get_contract_size(symbol) if symbol else 1.0
        coin_quantity = self.contracts_to_coin_quantity(symbol, contracts) if symbol else contracts
        side = str(trade.get('side') or info.get('side') or '').lower()
        pos_side = self.normalize_side(info.get('posSide') or side or 'long')
        price = self._safe_float(trade.get('price') or info.get('fillPx') or info.get('px') or 0)
        realized_pnl = self._safe_float(
            trade.get('realizedPnl') or trade.get('pnl') or info.get('fillPnl') or info.get('pnl') or 0
        )
        timestamp = trade.get('timestamp') or self._parse_time_ms(info.get('fillTime') or info.get('ts') or info.get('cTime'))
        fee_cost = self._safe_float((trade.get('fee') or {}).get('cost') if isinstance(trade.get('fee'), dict) else 0)
        reduce_only = bool(info.get('reduceOnly') in (True, 'true', '1', 1) or info.get('execType') in {'T', 'M'} and info.get('tradeId'))
        return {
            'symbol': symbol,
            'side': side,
            'pos_side': pos_side,
            'price': price,
            'quantity': contracts,
            'contracts': contracts,
            'contract_size': contract_size,
            'coin_quantity': coin_quantity,
            'realized_pnl': realized_pnl,
            'fee': fee_cost,
            'timestamp': timestamp,
            'datetime': trade.get('datetime') or info.get('fillTime'),
            'order_id': trade.get('order') or trade.get('orderId') or info.get('ordId'),
            'trade_id': trade.get('id') or info.get('tradeId') or info.get('billId'),
            'reduce_only': reduce_only,
            'raw': trade,
        }

    def build_close_summary(self, fills: List[Dict], open_trade: Dict = None, fallback_price: float = None, source: str = 'exchange_fills') -> Optional[Dict]:
        rows = [self.normalize_trade_fill(fill) if 'raw' not in fill or 'contracts' not in fill else fill for fill in (fills or [])]
        rows = [row for row in rows if row and self._safe_float(row.get('quantity')) > 0]
        if not rows and fallback_price is None:
            return None
        total_qty = sum(self._safe_float(row.get('quantity')) for row in rows)
        total_coin_qty = sum(self._safe_float(row.get('coin_quantity')) for row in rows)
        weighted_exit = None
        if total_qty > 0:
            weighted_exit = sum(self._safe_float(row.get('price')) * self._safe_float(row.get('quantity')) for row in rows) / total_qty
        exit_price = weighted_exit if weighted_exit is not None else self._safe_float(fallback_price)
        realized_pnl = None
        if rows:
            realized_pnl = sum(self._safe_float(row.get('realized_pnl')) for row in rows)
            if all(abs(self._safe_float(row.get('realized_pnl'))) < 1e-12 for row in rows):
                realized_pnl = None
        pnl_percent = None
        leverage = self._safe_int((open_trade or {}).get('leverage') or self.leverage, self.leverage)
        entry_price = self._safe_float((open_trade or {}).get('entry_price'))
        base_coin_qty = self._safe_float((open_trade or {}).get('coin_quantity')) or total_coin_qty
        if realized_pnl is None and entry_price > 0 and exit_price > 0 and base_coin_qty > 0 and open_trade:
            side = self.normalize_side((open_trade or {}).get('side'))
            direction = 1 if side == 'long' else -1
            realized_pnl = (exit_price - entry_price) * base_coin_qty * direction
            source = f'{source}_fallback_pnl'
        if realized_pnl is not None and entry_price > 0 and base_coin_qty > 0:
            margin = (entry_price * base_coin_qty) / leverage if leverage > 0 else 0
            pnl_percent = (realized_pnl / margin * 100) if margin > 0 else None
        close_time = None
        timestamps = [self._parse_time_ms(row.get('timestamp')) for row in rows if self._parse_time_ms(row.get('timestamp'))]
        if timestamps:
            close_time = datetime.fromtimestamp(max(timestamps) / 1000).isoformat()
        return {
            'exit_price': exit_price if exit_price else None,
            'pnl': realized_pnl,
            'pnl_percent': pnl_percent,
            'quantity': total_qty if total_qty > 0 else None,
            'coin_quantity': total_coin_qty if total_coin_qty > 0 else None,
            'contract_size': self._safe_float(rows[0].get('contract_size'), 1.0) if rows else self.get_contract_size((open_trade or {}).get('symbol')) if open_trade and open_trade.get('symbol') else 1.0,
            'close_time': close_time,
            'source': source,
            'fills': rows,
        }

    def fetch_closed_trade_summary(self, open_trade: Dict, fallback_price: float = None, lookback_hours: int = 168) -> Optional[Dict]:
        if not open_trade or not open_trade.get('symbol'):
            return None
        symbol = self.normalize_symbol(open_trade.get('symbol'))
        side = self.normalize_side(open_trade.get('side'))
        expected_close_side = 'sell' if side == 'long' else 'buy'
        open_time_ms = self._parse_time_ms(open_trade.get('open_time'))
        close_time_ms = self._parse_time_ms(open_trade.get('close_time'))
        target_qty = self._safe_float(open_trade.get('quantity') or 0)
        since_floor = int((datetime.now().timestamp() - lookback_hours * 3600) * 1000)
        since = max((open_time_ms or 0) - 6 * 3600 * 1000, since_floor)
        rows = self._fetch_my_trades_candidates(symbol=symbol, since=since, limit=400)
        candidates = []
        for trade in rows or []:
            row = self.normalize_trade_fill(trade)
            if row.get('symbol') != symbol:
                continue
            ts = self._parse_time_ms(row.get('timestamp'))
            if open_time_ms and ts and ts + 1000 < open_time_ms:
                continue
            if close_time_ms and ts and ts > close_time_ms + 6 * 3600 * 1000:
                continue
            trade_side = str(row.get('side') or '').lower()
            if trade_side and trade_side != expected_close_side and row.get('pos_side') != side:
                continue
            if row.get('reduce_only') or row.get('realized_pnl') not in (None, 0):
                candidates.append(row)
            elif trade_side == expected_close_side and ts:
                candidates.append(row)
        if not candidates:
            return None
        candidates.sort(key=lambda x: self._parse_time_ms(x.get('timestamp')) or 0, reverse=True)
        picked = []
        accumulated = 0.0
        tolerance = max(target_qty * 0.05, 1e-8) if target_qty > 0 else 0.0
        for row in candidates:
            picked.append(row)
            accumulated += self._safe_float(row.get('quantity'))
            if target_qty > 0 and accumulated + tolerance >= target_qty:
                break
            if target_qty <= 0 and len(picked) >= 20:
                break
        picked.sort(key=lambda x: self._parse_time_ms(x.get('timestamp')) or 0)
        summary = self.build_close_summary(picked, open_trade=open_trade, fallback_price=fallback_price, source='exchange_fills')
        if summary and target_qty > 0 and summary.get('quantity') and summary['quantity'] > target_qty * 1.5:
            summary['quantity'] = target_qty
            contract_size = self._safe_float(summary.get('contract_size') or open_trade.get('contract_size') or 1.0, 1.0)
            summary['coin_quantity'] = self._safe_float(open_trade.get('coin_quantity')) or target_qty * contract_size
            if summary.get('exit_price') and open_trade.get('entry_price'):
                direction = 1 if side == 'long' else -1
                summary['pnl'] = (summary['exit_price'] - self._safe_float(open_trade.get('entry_price'))) * summary['coin_quantity'] * direction
                leverage = self._safe_int(open_trade.get('leverage') or self.leverage, self.leverage)
                margin = (self._safe_float(open_trade.get('entry_price')) * summary['coin_quantity']) / leverage if leverage > 0 else 0
                summary['pnl_percent'] = (summary['pnl'] / margin * 100) if margin > 0 else None
            summary['source'] = 'exchange_fills_capped_fallback_pnl'
        return summary

    def fetch_ticker(self, symbol: str) -> Dict:
        return self.exchange.fetch_ticker(symbol)

    def fetch_ohlcv(self, symbol: str, timeframe: str = '1h', since: int = None, limit: int = 100) -> List:
        return self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)

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

    def _is_posside_error(self, message: str) -> bool:
        return 'Parameter posSide error' in message or '51000' in message and 'posSide' in message

    def create_order(self, symbol: str, side: str, amount: float, posSide: str = None) -> Dict:
        contract_symbol = self.get_order_symbol(symbol)
        params = self._build_order_params(posSide=posSide)
        try:
            return self._submit_market_order(contract_symbol, side, amount, params)
        except Exception as e:
            message = str(e)
            if self._is_posside_error(message):
                fallback_params = {'tdMode': 'isolated'}
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
            if self._is_posside_error(message) or '51169' in message:
                fallback_params = {'tdMode': 'isolated', 'reduceOnly': True}
                return self._submit_market_order(contract_symbol, side, amount, fallback_params)
            print(f'平仓错误: {e}')
            raise

    def get_leverage(self, symbol: str) -> int:
        """获取配置的杠杆倍数"""
        return self.leverage

    def get_actual_leverage(self, symbol: str) -> int:
        """获取实际杠杆（优先从交易所API获取，若失败则用配置）
        
        Returns:
            int: 实际杠杆倍数，若获取失败返回配置的默认值
        """
        # 尝试从持仓中获取实际杠杆
        try:
            positions = self.fetch_positions()
            for pos in positions:
                pos_symbol = pos.get('symbol') or pos.get('info', {}).get('instId') or ''
                # 处理 BTC-USD-SWAP 格式
                if ':' in pos_symbol:
                    pos_symbol = pos_symbol.split(':')[0]
                if pos_symbol == symbol:
                    # 从持仓信息获取实际杠杆
                    lev = pos.get('leverage')
                    if lev:
                        return int(lev)
        except Exception:
            pass
        
        # 回退到配置的杠杆
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
