"""
策略模块 - 增强版策略库
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
import pandas as pd
import numpy as np


class BaseStrategy(ABC):
    """策略基类"""
    
    def __init__(self, config: Dict, name: str):
        self.config = config
        self.name = name
        self.strategy_config = config.get('strategies', {}).get(name.lower(), {})
    
    @abstractmethod
    def analyze(self, df: pd.DataFrame, current_price: float) -> Optional[Dict]:
        """分析并返回策略结果"""
        pass
    
    @property
    def enabled(self) -> bool:
        """是否启用"""
        return self.strategy_config.get('enabled', True)
    
    @property
    def weight(self) -> int:
        """策略权重"""
        return self.strategy_config.get('strength_weight', 20)


class RSIStrategy(BaseStrategy):
    """RSI策略"""
    
    def __init__(self, config: Dict):
        super().__init__(config, 'RSI')
    
    def analyze(self, df: pd.DataFrame, current_price: float) -> Optional[Dict]:
        if 'RSI' not in df.columns:
            return None
        
        period = self.strategy_config.get('period', 14)
        oversold = self.strategy_config.get('oversold', 35)
        overbought = self.strategy_config.get('overbought', 65)
        
        rsi = df['RSI'].iloc[-1]
        
        # 严重超卖 - 强烈买入信号
        if rsi < 30:
            return {
                'strategy': 'RSI',
                'action': 'buy',
                'value': rsi,
                'detail': f'RSI严重超卖({rsi:.1f}<30)',
                'triggered': True,
                'strength': self.weight,
                'confidence': 0.9,
                'metadata': {'period': period, 'rsi': rsi, 'zone': 'oversold_extreme'}
            }
        # 超卖 - 买入信号
        elif rsi < oversold:
            return {
                'strategy': 'RSI',
                'action': 'buy',
                'value': rsi,
                'detail': f'RSI超卖({rsi:.1f}<{oversold})',
                'triggered': True,
                'strength': int(self.weight * 0.7),
                'confidence': 0.7,
                'metadata': {'period': period, 'rsi': rsi, 'zone': 'oversold'}
            }
        # 严重超买 - 强烈卖出信号
        elif rsi > 70:
            return {
                'strategy': 'RSI',
                'action': 'sell',
                'value': rsi,
                'detail': f'RSI严重超买({rsi:.1f}>70)',
                'triggered': True,
                'strength': self.weight,
                'confidence': 0.9,
                'metadata': {'period': period, 'rsi': rsi, 'zone': 'overbought_extreme'}
            }
        # 超买 - 卖出信号
        elif rsi > overbought:
            return {
                'strategy': 'RSI',
                'action': 'sell',
                'value': rsi,
                'detail': f'RSI超买({rsi:.1f}>{overbought})',
                'triggered': True,
                'strength': int(self.weight * 0.7),
                'confidence': 0.7,
                'metadata': {'period': period, 'rsi': rsi, 'zone': 'overbought'}
            }
        
        return None


class MACDStrategy(BaseStrategy):
    """MACD策略"""
    
    def __init__(self, config: Dict):
        super().__init__(config, 'MACD')
    
    def analyze(self, df: pd.DataFrame, current_price: float) -> Optional[Dict]:
        if 'MACD' not in df.columns or 'MACD_signal' not in df.columns:
            return None
        
        macd = df['MACD'].iloc[-1]
        signal = df['MACD_signal'].iloc[-1]
        macd_prev = df['MACD'].iloc[-2] if len(df) > 1 else macd
        signal_prev = df['MACD_signal'].iloc[-2] if len(df) > 1 else signal
        
        # 金叉
        if macd_prev <= signal_prev and macd > signal:
            # 检查是否在零轴下方(更强势)
            below_zero = macd < 0
            return {
                'strategy': 'MACD',
                'action': 'buy',
                'value': macd,
                'detail': 'MACD金叉' + ('(零轴下方,强势)' if below_zero else ''),
                'triggered': True,
                'strength': self.weight * (1.2 if below_zero else 1.0),
                'confidence': 0.8 if below_zero else 0.7,
                'metadata': {'macd': macd, 'signal': signal, 'position': 'below_zero' if below_zero else 'above_zero'}
            }
        # 死叉
        elif macd_prev >= signal_prev and macd < signal:
            above_zero = macd > 0
            return {
                'strategy': 'MACD',
                'action': 'sell',
                'value': macd,
                'detail': 'MACD死叉' + ('(零轴上方,强势)' if above_zero else ''),
                'triggered': True,
                'strength': self.weight * (1.2 if above_zero else 1.0),
                'confidence': 0.8 if above_zero else 0.7,
                'metadata': {'macd': macd, 'signal': signal, 'position': 'above_zero' if above_zero else 'below_zero'}
            }
        
        return None


class MACrossStrategy(BaseStrategy):
    """均线交叉策略"""
    
    def __init__(self, config: Dict):
        super().__init__(config, 'MA_Cross')
    
    def analyze(self, df: pd.DataFrame, current_price: float) -> Optional[Dict]:
        fast_period = self.strategy_config.get('fast_period', 5)
        slow_period = self.strategy_config.get('slow_period', 20)
        
        if len(df) < slow_period + 1:
            return None
        
        ma_fast = df[4].rolling(fast_period).mean().iloc[-1]
        ma_slow = df[4].rolling(slow_period).mean().iloc[-1]
        ma_fast_prev = df[4].rolling(fast_period).mean().iloc[-2]
        ma_slow_prev = df[4].rolling(slow_period).mean().iloc[-2]
        
        # 金叉
        if ma_fast_prev <= ma_slow_prev and ma_fast > ma_slow:
            # 计算乖离率
            deviation = (ma_fast - ma_slow) / ma_slow * 100
            strong = deviation > 0.5
            return {
                'strategy': 'MA_Cross',
                'action': 'buy',
                'value': ma_fast,
                'detail': f'MA{fast_period}上穿MA{slow_period}' + (',强势' if strong else ''),
                'triggered': True,
                'strength': self.weight * (1.2 if strong else 1.0),
                'confidence': 0.75 if strong else 0.65,
                'metadata': {'fast': ma_fast, 'slow': ma_slow, 'deviation': deviation}
            }
        # 死叉
        elif ma_fast_prev >= ma_slow_prev and ma_fast < ma_slow:
            deviation = (ma_slow - ma_fast) / ma_slow * 100
            strong = deviation > 0.5
            return {
                'strategy': 'MA_Cross',
                'action': 'sell',
                'value': ma_fast,
                'detail': f'MA{fast_period}下穿MA{slow_period}' + (',强势' if strong else ''),
                'triggered': True,
                'strength': self.weight * (1.2 if strong else 1.0),
                'confidence': 0.75 if strong else 0.65,
                'metadata': {'fast': ma_fast, 'slow': ma_slow, 'deviation': deviation}
            }
        
        return None


class BollingerStrategy(BaseStrategy):
    """布林带策略"""
    
    def __init__(self, config: Dict):
        super().__init__(config, 'Bollinger')
    
    def analyze(self, df: pd.DataFrame, current_price: float) -> Optional[Dict]:
        if 'BB_lower' not in df.columns:
            return None
        
        period = self.strategy_config.get('period', 20)
        std_multiplier = self.strategy_config.get('std_multiplier', 2)
        
        lower = df['BB_lower'].iloc[-1]
        upper = df['BB_upper'].iloc[-1]
        mid = df['BB_mid'].iloc[-1]
        
        # 计算价格位置
        position = (current_price - lower) / (upper - lower) if upper != lower else 0.5
        
        # 触及下轨
        if current_price <= lower:
            return {
                'strategy': 'Bollinger',
                'action': 'buy',
                'value': current_price,
                'detail': '价格触及布林下轨,超卖',
                'triggered': True,
                'strength': self.weight,
                'confidence': 0.7,
                'metadata': {'lower': lower, 'upper': upper, 'position': position}
            }
        # 触及上轨
        elif current_price >= upper:
            return {
                'strategy': 'Bollinger',
                'action': 'sell',
                'value': current_price,
                'detail': '价格触及布林上轨,超买',
                'triggered': True,
                'strength': self.weight,
                'confidence': 0.7,
                'metadata': {'lower': lower, 'upper': upper, 'position': position}
            }
        # 接近下轨 (50%以下)
        elif position < 0.2:
            return {
                'strategy': 'Bollinger',
                'action': 'buy',
                'value': current_price,
                'detail': f'接近布林下轨({position*100:.0f}%)',
                'triggered': True,
                'strength': int(self.weight * 0.6),
                'confidence': 0.5,
                'metadata': {'lower': lower, 'upper': upper, 'position': position}
            }
        # 接近上轨 (80%以上)
        elif position > 0.8:
            return {
                'strategy': 'Bollinger',
                'action': 'sell',
                'value': current_price,
                'detail': f'接近布林上轨({position*100:.0f}%)',
                'triggered': True,
                'strength': int(self.weight * 0.6),
                'confidence': 0.5,
                'metadata': {'lower': lower, 'upper': upper, 'position': position}
            }
        
        return None


class VolumeStrategy(BaseStrategy):
    """成交量策略"""
    
    def __init__(self, config: Dict):
        super().__init__(config, 'Volume')
    
    def analyze(self, df: pd.DataFrame, current_price: float) -> Optional[Dict]:
        period = self.strategy_config.get('volume_ma_period', 20)
        multiplier = self.volume_multiplier = self.strategy_config.get('volume_multiplier', 1.5)
        
        if len(df) < period + 1:
            return None
        
        volume = df[5].iloc[-1]
        volume_ma = df[5].rolling(period).mean().iloc[-1]
        
        # 成交量放大
        if volume > volume_ma * multiplier:
            # 判断涨跌
            price_change = (df[4].iloc[-1] - df[4].iloc[-2]) / df[4].iloc[-2]
            change_pct = price_change * 100
            
            if price_change > 0.01:  # 上涨超过1%
                return {
                    'strategy': 'Volume',
                    'action': 'buy',
                    'value': volume,
                    'detail': f'成交量放大({volume/volume_ma:.1f}倍),上涨{change_pct:.1f}%,量价齐升',
                    'triggered': True,
                    'strength': self.weight,
                    'confidence': 0.75,
                    'metadata': {'volume': volume, 'volume_ma': volume_ma, 'change_pct': change_pct}
                }
            elif price_change < -0.01:  # 下跌超过1%
                return {
                    'strategy': 'Volume',
                    'action': 'sell',
                    'value': volume,
                    'detail': f'成交量放大({volume/volume_ma:.1f}倍),下跌{abs(change_pct):.1f}%,放量下跌',
                    'triggered': True,
                    'strength': self.weight,
                    'confidence': 0.75,
                    'metadata': {'volume': volume, 'volume_ma': volume_ma, 'change_pct': change_pct}
                }
        
        return None


class PatternStrategy(BaseStrategy):
    """K线形态策略"""
    
    def __init__(self, config: Dict):
        super().__init__(config, 'Pattern')
    
    def analyze(self, df: pd.DataFrame, current_price: float) -> Optional[Dict]:
        if len(df) < 5:
            return None
        
        # 获取最近几根K线
        opens = df[1].values
        closes = df[4].values
        highs = df[2].values
        lows = df[3].values
        volumes = df[5].values
        
        # 最新K线
        open_p = opens[-1]
        close_p = closes[-1]
        high_p = highs[-1]
        low_p = lows[-1]
        
        body = abs(close_p - open_p)
        body_size = body / close_p * 100  # 实体大小百分比
        
        upper_shadow = high_p - max(open_p, close_p)
        lower_shadow = min(open_p, close_p) - low_p
        total_range = high_p - low_p
        
        # 锤子线 (看涨)
        if lower_shadow > body * 2 and upper_shadow < body * 0.3 and body_size > 0.5:
            return {
                'strategy': 'Pattern',
                'action': 'buy',
                'value': close_p,
                'detail': '锤子线(看涨反转)',
                'triggered': True,
                'strength': self.weight,
                'confidence': 0.65,
                'metadata': {'pattern': 'hammer'}
            }
        
        # 上吊线 (看跌)
        if upper_shadow > body * 2 and lower_shadow < body * 0.3 and body_size > 0.5:
            return {
                'strategy': 'Pattern',
                'action': 'sell',
                'value': close_p,
                'detail': '上吊线(看跌反转)',
                'triggered': True,
                'strength': self.weight,
                'confidence': 0.65,
                'metadata': {'pattern': 'hanging_man'}
            }
        
        # 吞没形态 (看涨)
        if len(closes) >= 2:
            prev_body = abs(opens[-2] - closes[-2])
            if closes[-2] < opens[-2] and close_p > opens[-2] and open_p < closes[-2]:
                if body > prev_body * 1.5:
                    return {
                        'strategy': 'Pattern',
                        'action': 'buy',
                        'value': close_p,
                        'detail': '看涨吞没',
                        'triggered': True,
                        'strength': self.weight,
                        'confidence': 0.7,
                        'metadata': {'pattern': 'bullish_engulfing'}
                    }
            
            # 吞没形态 (看跌)
            if closes[-2] > opens[-2] and close_p < opens[-2] and open_p > closes[-2]:
                if body > prev_body * 1.5:
                    return {
                        'strategy': 'Pattern',
                        'action': 'sell',
                        'value': close_p,
                        'detail': '看跌吞没',
                        'triggered': True,
                        'strength': self.weight,
                        'confidence': 0.7,
                        'metadata': {'pattern': 'bearish_engulfing'}
                    }
        
        # 十字星
        if body < total_range * 0.1 and body_size < 0.3:
            # 判断是底部还是顶部
            if closes[-2] < closes[-3]:  # 下跌后出现
                return {
                    'strategy': 'Pattern',
                    'action': 'buy',
                    'value': close_p,
                    'detail': '十字星(可能反转)',
                    'triggered': True,
                    'strength': int(self.weight * 0.5),
                    'confidence': 0.5,
                    'metadata': {'pattern': 'doji'}
                }
        
        return None


class TrendStrengthStrategy(BaseStrategy):
    """趋势强度策略 - ADX"""
    
    def __init__(self, config: Dict):
        super().__init__(config, 'TrendStrength')
        self.strategy_config = config.get('strategies', {}).get('adx', {
            'enabled': True,
            'strength_weight': 20,
            'period': 14,
            'threshold': 25
        })
    
    def analyze(self, df: pd.DataFrame, current_price: float) -> Optional[Dict]:
        period = self.strategy_config.get('period', 14)
        threshold = self.strategy_config.get('threshold', 25)
        
        if len(df) < period + 1:
            return None
        
        # 计算ADX (简化版)
        highs = df[2].values
        lows = df[3].values
        closes = df[4].values
        
        # 计算True Range
        tr1 = highs[1:] - lows[1:]
        tr2 = abs(highs[1:] - closes[:-1])
        tr3 = abs(lows[1:] - closes[:-1])
        tr = np.maximum(np.maximum(tr1, tr2), tr3)
        
        # 简化ADX计算
        tr_ma = np.mean(tr[-period:])
        
        if tr_ma == 0:
            return None
        
        # 计算趋势强度
        price_range = np.std(closes[-period:]) * 2
        strength = min(100, int(price_range / tr_ma * 25))
        
        if strength > threshold:
            # 判断趋势方向
            ma_short = np.mean(closes[-5:])
            ma_long = np.mean(closes[-period:])
            
            if ma_short > ma_long:
                return {
                    'strategy': 'TrendStrength',
                    'action': 'buy',
                    'value': strength,
                    'detail': f'上升趋势强(ADX~{strength})',
                    'triggered': True,
                    'strength': self.weight,
                    'confidence': 0.7,
                    'metadata': {'adx': strength, 'trend': 'up'}
                }
            else:
                return {
                    'strategy': 'TrendStrength',
                    'action': 'sell',
                    'value': strength,
                    'detail': f'下降趋势强(ADX~{strength})',
                    'triggered': True,
                    'strength': self.weight,
                    'confidence': 0.7,
                    'metadata': {'adx': strength, 'trend': 'down'}
                }
        
        return None


class DivergenceStrategy(BaseStrategy):
    """背离策略 - 价格与RSI背离"""
    
    def __init__(self, config: Dict):
        super().__init__(config, 'Divergence')
        self.strategy_config = config.get('strategies', {}).get('divergence', {
            'enabled': True,
            'strength_weight': 30,
            'lookback': 20
        })
    
    def analyze(self, df: pd.DataFrame, current_price: float) -> Optional[Dict]:
        if 'RSI' not in df.columns or len(df) < 20:
            return None
        
        lookback = self.strategy_config.get('lookback', 20)
        
        rsi = df['RSI'].values
        closes = df[4].values
        
        # 找最近20个周期的最高/最低点
        price_high_idx = np.argmax(closes[-lookback:])
        price_low_idx = np.argmin(closes[-lookback:])
        
        rsi_at_price_high = rsi[-(lookback - price_high_idx)] if price_high_idx < len(rsi) else rsi[-1]
        rsi_at_price_low = rsi[-(lookback - price_low_idx)] if price_low_idx < len(rsi) else rsi[-1]
        
        current_rsi = rsi[-1]
        
        # 顶背离: 价格创新高, RSI未创新高
        if closes[-1] > closes[-(lookback - price_high_idx)] and current_rsi < rsi_at_price_high:
            return {
                'strategy': 'Divergence',
                'action': 'sell',
                'value': current_rsi,
                'detail': '顶背离(价格新高,RSI背离)',
                'triggered': True,
                'strength': self.weight,
                'confidence': 0.75,
                'metadata': {'type': 'bearish_divergence', 'rsi': current_rsi}
            }
        
        # 底背离: 价格创新低, RSI未创新低
        if closes[-1] < closes[-(lookback - price_low_idx)] and current_rsi > rsi_at_price_low:
            return {
                'strategy': 'Divergence',
                'action': 'buy',
                'value': current_rsi,
                'detail': '底背离(价格新低,RSI背离)',
                'triggered': True,
                'strength': self.weight,
                'confidence': 0.75,
                'metadata': {'type': 'bullish_divergence', 'rsi': current_rsi}
            }
        
        return None


class StrategyManager:
    """策略管理器"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.strategies = self._init_strategies()
    
    def _init_strategies(self) -> Dict[str, BaseStrategy]:
        """初始化所有策略"""
        return {
            'RSI': RSIStrategy(self.config),
            'MACD': MACDStrategy(self.config),
            'MA_Cross': MACrossStrategy(self.config),
            'Bollinger': BollingerStrategy(self.config),
            'Volume': VolumeStrategy(self.config),
            'Pattern': PatternStrategy(self.config),
            'TrendStrength': TrendStrengthStrategy(self.config),
            'Divergence': DivergenceStrategy(self.config),
        }
    
    def get_enabled_strategies(self) -> Dict[str, BaseStrategy]:
        """获取启用的策略"""
        return {name: s for name, s in self.strategies.items() if s.enabled}
    
    def analyze_all(self, df: pd.DataFrame, current_price: float) -> List[Dict]:
        """运行所有策略"""
        results = []
        
        for name, strategy in self.get_enabled_strategies().items():
            result = strategy.analyze(df, current_price)
            if result:
                results.append(result)
        
        return results
    
    def get_strategy_names(self) -> List[str]:
        """获取策略名称列表"""
        return list(self.strategies.keys())
