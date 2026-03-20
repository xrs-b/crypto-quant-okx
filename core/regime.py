"""
Regime Layer v1 - 市场状态识别层
轻量版：基于趋势、波动率的简单分类
"""
import pandas as pd
import numpy as np
from typing import Dict, Optional
from dataclasses import dataclass
from enum import Enum


class Regime(Enum):
    """市场状态枚举"""
    TREND = "trend"           # 趋势明显 (上涨/下跌)
    RANGE = "range"           # 震荡/盘整
    HIGH_VOL = "high_vol"     # 高波动
    LOW_VOL = "low_vol"       # 低波动
    RISK_ANOMALY = "risk_anomaly"  # 风险异常
    UNKNOWN = "unknown"       # 未知/数据不足


@dataclass
class RegimeResult:
    """Regime 检测结果"""
    regime: Regime
    confidence: float  # 0-1, 置信度
    indicators: Dict  # 原始指标值
    details: str       # 简要说明
    
    def to_dict(self) -> Dict:
        return {
            'regime': self.regime.value,
            'confidence': round(self.confidence, 3),
            'indicators': {k: round(v, 5) if isinstance(v, float) else v 
                          for k, v in self.indicators.items()},
            'details': self.details
        }


class RegimeDetector:
    """Regime 检测器 - 轻量版 v1"""
    
    # 配置阈值
    DEFAULT_CONFIG = {
        # 趋势判断
        'ema_short': 20,
        'ema_long': 60,
        'ema_gap_threshold': 0.015,  # 1.5% EMA差距视为趋势
        
        # 波动率判断
        'volatility_lookback': 20,
        'high_vol_threshold': 0.04,   # 4% 日波动率高
        'low_vol_threshold': 0.008,   # 0.8% 日波动率低
        
        # 异常检测
        'price_change_threshold': 0.08,  # 8% 单日涨跌幅异常
        'volume_spike_ratio': 2.5,        # 2.5倍成交量为异常
        
        # 置信度
        'min_data_points': 30,  # 最小数据点数
    }
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
    
    def detect(self, df: pd.DataFrame, price: float = None) -> RegimeResult:
        """
        检测当前市场状态
        
        Args:
            df: K线数据 (需要 OHLCV + EMA)
            price: 当前价格 (可选)
            
        Returns:
            RegimeResult: 检测结果
        """
        # 数据验证
        if not self._validate_data(df):
            return RegimeResult(
                regime=Regime.UNKNOWN,
                confidence=0.0,
                indicators={},
                details="数据不足，回退旧逻辑"
            )
        
        # 计算指标
        indicators = self._calculate_indicators(df, price)
        
        # 分类判断
        regime, confidence, details = self._classify(indicators)
        
        return RegimeResult(
            regime=regime,
            confidence=confidence,
            indicators=indicators,
            details=details
        )
    
    def _validate_data(self, df: pd.DataFrame) -> bool:
        """验证数据是否足够"""
        required_cols = ['close']
        if not all(col in df.columns for col in required_cols):
            return False
        
        min_points = self.config.get('min_data_points', 30)
        return len(df) >= min_points
    
    def _calculate_indicators(self, df: pd.DataFrame, price: float = None) -> Dict:
        """计算各类指标"""
        close = df['close']
        high = df.get('high', close)
        low = df.get('low', close)
        volume = df.get('volume', pd.Series([1] * len(df)))
        
        current_price = price if price else close.iloc[-1]
        
        # 1. 趋势指标 - EMA gap
        ema_short = self.config.get('ema_short', 20)
        ema_long = self.config.get('ema_long', 60)
        
        ema_s = close.ewm(span=ema_short, adjust=False).mean()
        ema_l = close.ewm(span=ema_long, adjust=False).mean()
        
        ema_gap = (ema_s.iloc[-1] - ema_l.iloc[-1]) / ema_l.iloc[-1]
        
        # 2. 波动率指标
        volatility_lookback = self.config.get('volatility_lookback', 20)
        returns = close.pct_change().dropna()
        
        if len(returns) >= volatility_lookback:
            volatility = returns.tail(volatility_lookback).std()
        else:
            volatility = returns.std() if len(returns) > 0 else 0
        
        # ATR 比率
        if 'high' in df.columns and 'low' in df.columns:
            tr = np.maximum(
                high - low,
                np.abs(high - close.shift(1)),
                np.abs(low - close.shift(1))
            )
            atr = tr.ewm(span=14).mean()
            atr_ratio = atr.iloc[-1] / current_price
        else:
            atr_ratio = volatility
        
        # 3. 价格变化
        price_change_1d = (current_price - close.iloc[-2]) / close.iloc[-2] if len(close) >= 2 else 0
        price_change_5d = (current_price - close.iloc[-6]) / close.iloc[-6] if len(close) >= 6 else 0
        
        # 4. 成交量异常
        vol_ma = volume.tail(20).mean()
        vol_ratio = volume.iloc[-1] / vol_ma if vol_ma > 0 else 1
        
        # 5. 趋势强度 (简化 ADX)
        trend_strength = abs(ema_gap) * 10  # 缩放到类似百分比
        
        return {
            'ema_gap': ema_gap,
            'volatility': volatility,
            'atr_ratio': atr_ratio,
            'price_change_1d': price_change_1d,
            'price_change_5d': price_change_5d,
            'volume_ratio': vol_ratio,
            'trend_strength': min(trend_strength, 1.0),
            'ema_direction': 1 if ema_gap > 0 else -1,
        }
    
    def _classify(self, indicators: Dict) -> tuple:
        """基于指标分类"""
        ema_gap = indicators.get('ema_gap', 0)
        volatility = indicators.get('volatility', 0)
        price_change_1d = abs(indicators.get('price_change_1d', 0))
        volume_ratio = indicators.get('volume_ratio', 1)
        
        ema_gap_threshold = self.config.get('ema_gap_threshold', 0.015)
        high_vol_threshold = self.config.get('high_vol_threshold', 0.04)
        low_vol_threshold = self.config.get('low_vol_threshold', 0.008)
        price_change_threshold = self.config.get('price_change_threshold', 0.08)
        volume_spike_ratio = self.config.get('volume_spike_ratio', 2.5)
        
        # 1. 风险异常检测 (优先级最高)
        if price_change_1d > price_change_threshold:
            return Regime.RISK_ANOMALY, 0.85, f"价格日内波动异常({price_change_1d*100:.1f}%)"
        
        if volume_ratio > volume_spike_ratio and volatility > high_vol_threshold:
            return Regime.RISK_ANOMALY, 0.75, f"量价齐升异常(vol={volume_ratio:.1f}x)"
        
        # 2. 高波动
        if volatility > high_vol_threshold:
            direction = "上涨" if ema_gap > 0 else "下跌"
            return Regime.HIGH_VOL, 0.7, f"高波动趋势中({direction}, vol={volatility*100:.1f}%)"
        
        # 3. 低波动
        if volatility < low_vol_threshold:
            return Regime.LOW_VOL, 0.7, f"低波动盘整(vol={volatility*100:.2f}%)"
        
        # 4. 趋势判断
        if abs(ema_gap) > ema_gap_threshold:
            direction = "上涨" if ema_gap > 0 else "下跌"
            confidence = min(0.9, 0.5 + abs(ema_gap) * 10)
            return Regime.TREND, confidence, f"趋势{direction}(gap={ema_gap*100:.2f}%)"
        
        # 5. 默认震荡
        return Regime.RANGE, 0.6, f"区间震荡(gap={ema_gap*100:.2f}%, vol={volatility*100:.2f}%)"


def detect_regime(df: pd.DataFrame, price: float = None, config: Dict = None) -> RegimeResult:
    """
    便捷函数：检测市场状态
    
    Args:
        df: K线数据
        price: 当前价格
        config: 配置
        
    Returns:
        RegimeResult
    """
    detector = RegimeDetector(config)
    return detector.detect(df, price)
