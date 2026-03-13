"""
信号检测模块
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from dataclasses import dataclass, field


@dataclass
class Signal:
    """信号数据类"""
    symbol: str
    signal_type: str  # buy / sell
    price: float
    strength: int  # 0-100
    reasons: List[Dict] = field(default_factory=list)
    strategies_triggered: List[str] = field(default_factory=list)
    filtered: bool = False
    filter_reason: str = None
    executed: bool = False


class SignalDetector:
    """信号检测器"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.strategies_config = config.get('strategies', {})
    
    def analyze(self, symbol: str, df: pd.DataFrame, 
                current_price: float, ml_prediction: tuple = None) -> Signal:
        """分析信号"""
        
        signal = Signal(
            symbol=symbol,
            signal_type='hold',
            price=current_price,
            strength=0
        )
        
        total_strength = 0
        max_strength = 0
        triggered_strategies = []
        
        # 1. RSI策略
        if self.strategies_config.get('rsi', {}).get('enabled', True):
            rsi_result = self._analyze_rsi(df, current_price)
            if rsi_result:
                total_strength += rsi_result['strength']
                max_strength += 30
                signal.reasons.append(rsi_result)
                triggered_strategies.append('RSI')
        
        # 2. MACD策略
        if self.strategies_config.get('macd', {}).get('enabled', True):
            macd_result = self._analyze_macd(df, current_price)
            if macd_result:
                total_strength += macd_result['strength']
                max_strength += 20
                signal.reasons.append(macd_result)
                triggered_strategies.append('MACD')
        
        # 3. 均线交叉策略
        if self.strategies_config.get('ma_cross', {}).get('enabled', True):
            ma_result = self._analyze_ma_cross(df, current_price)
            if ma_result:
                total_strength += ma_result['strength']
                max_strength += 25
                signal.reasons.append(ma_result)
                triggered_strategies.append('MA_Cross')
        
        # 4. 布林带策略
        if self.strategies_config.get('bollinger', {}).get('enabled', True):
            bb_result = self._analyze_bollinger(df, current_price)
            if bb_result:
                total_strength += bb_result['strength']
                max_strength += 20
                signal.reasons.append(bb_result)
                triggered_strategies.append('Bollinger')
        
        # 5. ML预测
        if self.config.get('ml', {}).get('enabled', True) and ml_prediction:
            ml_result = self._analyze_ml(ml_prediction, current_price)
            if ml_result:
                total_strength += ml_result['strength']
                max_strength += 50
                signal.reasons.append(ml_result)
                triggered_strategies.append('ML')
        
        # 计算最终强度
        signal.strength = min(100, int(total_strength))
        signal.strategies_triggered = triggered_strategies
        
        # 判断信号类型
        composite_config = self.strategies_config.get('composite', {})
        min_strength = composite_config.get('min_strength', 70)
        
        if signal.strength >= min_strength:
            # 统计买入/卖出理由
            buy_count = sum(1 for r in signal.reasons if r.get('action') == 'buy')
            sell_count = sum(1 for r in signal.reasons if r.get('action') == 'sell')
            
            if buy_count > sell_count:
                signal.signal_type = 'buy'
            elif sell_count > buy_count:
                signal.signal_type = 'sell'
        
        return signal
    
    def _analyze_rsi(self, df: pd.DataFrame, price: float) -> Optional[Dict]:
        """分析RSI"""
        config = self.strategies_config.get('rsi', {})
        period = config.get('period', 14)
        oversold = config.get('oversold', 35)
        overbought = config.get('overbought', 65)
        
        if 'RSI' not in df.columns:
            return None
        
        rsi = df['RSI'].iloc[-1]
        
        if rsi < 30:
            return {
                'strategy': 'RSI',
                'type': 'rsi',
                'action': 'buy',
                'value': rsi,
                'detail': f'RSI严重超卖({rsi:.1f}<30)',
                'triggered': True,
                'strength': 30,
                'confidence': 0.9
            }
        elif rsi < oversold:
            return {
                'strategy': 'RSI',
                'type': 'rsi',
                'action': 'buy',
                'value': rsi,
                'detail': f'RSI超卖({rsi:.1f}<{oversold})',
                'triggered': True,
                'strength': 20,
                'confidence': 0.7
            }
        elif rsi > 70:
            return {
                'strategy': 'RSI',
                'type': 'rsi',
                'action': 'sell',
                'value': rsi,
                'detail': f'RSI严重超买({rsi:.1f}>70)',
                'triggered': True,
                'strength': 30,
                'confidence': 0.9
            }
        elif rsi > overbought:
            return {
                'strategy': 'RSI',
                'type': 'rsi',
                'action': 'sell',
                'value': rsi,
                'detail': f'RSI超买({rsi:.1f}>{overbought})',
                'triggered': True,
                'strength': 20,
                'confidence': 0.7
            }
        
        return None
    
    def _analyze_macd(self, df: pd.DataFrame, price: float) -> Optional[Dict]:
        """分析MACD"""
        if 'MACD' not in df.columns or 'MACD_signal' not in df.columns:
            return None
        
        macd = df['MACD'].iloc[-1]
        signal = df['MACD_signal'].iloc[-1]
        
        # 金叉
        macd_prev = df['MACD'].iloc[-2]
        signal_prev = df['MACD_signal'].iloc[-2]
        
        if macd_prev <= signal_prev and macd > signal:
            return {
                'strategy': 'MACD',
                'type': 'macd',
                'action': 'buy',
                'value': macd,
                'detail': 'MACD金叉',
                'triggered': True,
                'strength': 20,
                'confidence': 0.7
            }
        elif macd_prev >= signal_prev and macd < signal:
            return {
                'strategy': 'MACD',
                'type': 'macd',
                'action': 'sell',
                'value': macd,
                'detail': 'MACD死叉',
                'triggered': True,
                'strength': 20,
                'confidence': 0.7
            }
        
        return None
    
    def _analyze_ma_cross(self, df: pd.DataFrame, price: float) -> Optional[Dict]:
        """分析均线交叉"""
        config = self.strategies_config.get('ma_cross', {})
        fast_period = config.get('fast_period', 5)
        slow_period = config.get('slow_period', 20)
        
        if len(df) < slow_period:
            return None
        
        ma_fast = df[4].rolling(fast_period).mean().iloc[-1]
        ma_slow = df[4].rolling(slow_period).mean().iloc[-1]
        ma_fast_prev = df[4].rolling(fast_period).mean().iloc[-2]
        ma_slow_prev = df[4].rolling(slow_period).mean().iloc[-2]
        
        # 金叉
        if ma_fast_prev <= ma_slow_prev and ma_fast > ma_slow:
            return {
                'strategy': 'MA_Cross',
                'type': 'ma',
                'action': 'buy',
                'value': ma_fast,
                'detail': f'MA{fast_period}上穿MA{slow_period}',
                'triggered': True,
                'strength': 25,
                'confidence': 0.7
            }
        elif ma_fast_prev >= ma_slow_prev and ma_fast < ma_slow:
            return {
                'strategy': 'MA_Cross',
                'type': 'ma',
                'action': 'sell',
                'value': ma_fast,
                'detail': f'MA{fast_period}下穿MA{slow_period}',
                'triggered': True,
                'strength': 25,
                'confidence': 0.7
            }
        
        return None
    
    def _analyze_bollinger(self, df: pd.DataFrame, price: float) -> Optional[Dict]:
        """分析布林带"""
        if 'BB_lower' not in df.columns or 'BB_upper' not in df.columns:
            return None
        
        lower = df['BB_lower'].iloc[-1]
        upper = df['BB_upper'].iloc[-1]
        
        if price <= lower:
            return {
                'strategy': 'Bollinger',
                'type': 'bollinger',
                'action': 'buy',
                'value': price,
                'detail': '价格触及布林下轨',
                'triggered': True,
                'strength': 20,
                'confidence': 0.6
            }
        elif price >= upper:
            return {
                'strategy': 'Bollinger',
                'type': 'bollinger',
                'action': 'sell',
                'value': price,
                'detail': '价格触及布林上轨',
                'triggered': True,
                'strength': 20,
                'confidence': 0.6
            }
        
        return None
    
    def _analyze_ml(self, prediction: tuple, price: float) -> Optional[Dict]:
        """分析机器学习预测"""
        if not prediction:
            return None
        
        pred, prob = prediction
        
        if pred == 1:  # 预测涨
            return {
                'strategy': 'ML',
                'type': 'ml',
                'action': 'buy',
                'value': prob,
                'detail': f'ML预测上涨概率{prob*100:.1f}%',
                'triggered': prob > 0.6,
                'strength': int(prob * 50),
                'confidence': prob
            }
        else:  # 预测跌
            return {
                'strategy': 'ML',
                'type': 'ml',
                'action': 'sell',
                'value': prob,
                'detail': f'ML预测下跌概率{(1-prob)*100:.1f}%',
                'triggered': prob < 0.4,
                'strength': int((1-prob) * 50),
                'confidence': 1 - prob
            }
