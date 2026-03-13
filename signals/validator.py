"""
信号验证模块
"""
from typing import Dict, List
from core.config import Config
from core.exchange import Exchange


class SignalValidator:
    """信号验证器 - 过滤不符合条件的信号"""
    
    def __init__(self, config: Config, exchange: Exchange):
        self.config = config
        self.exchange = exchange
        self.filters = config.get('filters', {})
    
    def validate(self, signal, current_positions: Dict, 
                tracking_data: Dict = None) -> tuple:
        """
        验证信号
        
        Returns:
            (passed: bool, reason: str, details: list)
        """
        details = []
        
        # 1. 检查是否已有持仓
        existing = current_positions.get(signal.symbol)
        if existing:
            # 如果同方向，不开仓
            side = 'long' if signal.signal_type == 'buy' else 'short'
            if existing.get('side') == side:
                return False, f"已有相同方向持仓", details
        
        # 2. 检查最小价格变动
        if self.filters.get('min_price_change', 0) > 0:
            min_change = self.filters['min_price_change']
            if tracking_data and signal.symbol in tracking_data:
                last_price = tracking_data[signal.symbol].get('last_price')
                if last_price:
                    price_change = abs(signal.price - last_price) / last_price
                    if price_change < min_change:
                        return False, f"价格变动{price_change*100:.2f}%<{min_change*100}%", details
                    details.append({
                        'name': '价格变动',
                        'passed': True,
                        'value': f"{price_change*100:.2f}%"
                    })
        
        # 3. 趋势确认
        if self.filters.get('trend_confirmation', False):
            # 需要MACD趋势与信号一致
            # 这个在detector中已经处理
            details.append({
                'name': '趋势确认',
                'passed': True,
                'value': '已通过'
            })
        
        # 4. 仓位检查
        position_config = self.config.get('position', {})
        total_limit = position_config.get('total_limit', 0.3)
        
        # 计算当前总仓位
        total_margin = sum(
            pos.get('margin_used', 0) 
            for pos in current_positions.values()
        )
        
        # 估算新仓位需要的保证金
        # 这里简化处理，假设每次开单笔仓位的保证金
        if total_margin > 0:
            margin_percent = total_margin / 100  # 简化
        
        # 这个在trading模块中检查
        
        return True, None, details


class SignalRecorder:
    """信号记录器"""
    
    def __init__(self, database):
        self.db = database
    
    def record(self, signal, filter_result: tuple = None):
        """记录信号"""
        passed, reason, details = filter_result or (True, None, [])
        
        signal_data = {
            'symbol': signal.symbol,
            'signal_type': signal.signal_type,
            'price': signal.price,
            'strength': signal.strength,
            'reasons': signal.reasons,
            'strategies_triggered': signal.strategies_triggered,
            'filtered': not passed,
            'filter_reason': reason,
            'executed': signal.executed,
            'strategy_details': signal.reasons
        }
        
        signal_id = self.db.add_signal(signal_data)
        
        return signal_id
    
    def mark_executed(self, signal_id: int, trade_id: int = None):
        """标记信号已执行"""
        self.db.update_signal_executed(signal_id, trade_id)
