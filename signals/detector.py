"""
信号检测模块 - 增强版
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict
from datetime import datetime


@dataclass
class Signal:
    """信号数据类 - 增强版"""
    symbol: str
    signal_type: str  # buy / sell / hold
    price: float
    strength: int = 0  # 0-100

    # 详细分析数据
    reasons: List[Dict] = field(default_factory=list)
    strategies_triggered: List[str] = field(default_factory=list)

    # 过滤信息
    filtered: bool = False
    filter_reason: str = None
    filter_details: Dict = field(default_factory=dict)

    # 执行信息
    executed: bool = False
    trade_id: int = None

    # 元数据
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    indicators: Dict = field(default_factory=dict)  # 实时指标值
    direction_score: Dict = field(default_factory=dict)
    market_context: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)


class SignalDetector:
    """信号检测器 - 增强版"""

    def __init__(self, config: Dict):
        self.config = config
        self.strategies_config = config.get('strategies', {})

    def analyze(self, symbol: str, df: pd.DataFrame,
                current_price: float, ml_prediction: tuple = None) -> Signal:
        """分析信号：改为“方向评分 + 门槛判断”"""
        indicators = self._get_current_indicators(df)
        signal = Signal(
            symbol=symbol,
            signal_type='hold',
            price=current_price,
            strength=0,
            indicators=indicators
        )
        signal.market_context = self._analyze_market_context(df, current_price)

        triggered_strategies = []

        analyzers = [
            ('RSI', self.strategies_config.get('rsi', {}).get('enabled', True), lambda: self._analyze_rsi(df, current_price)),
            ('MACD', self.strategies_config.get('macd', {}).get('enabled', True), lambda: self._analyze_macd(df, current_price)),
            ('MA_Cross', self.strategies_config.get('ma_cross', {}).get('enabled', True), lambda: self._analyze_ma_cross(df, current_price)),
            ('Bollinger', self.strategies_config.get('bollinger', {}).get('enabled', True), lambda: self._analyze_bollinger(df, current_price)),
            ('Volume', self.strategies_config.get('volume', {}).get('enabled', True), lambda: self._analyze_volume(df, current_price)),
            ('Pattern', self.strategies_config.get('pattern', {}).get('enabled', True), lambda: self._analyze_pattern(df, current_price)),
        ]

        if self.config.get('ml', {}).get('enabled', True) and ml_prediction:
            analyzers.append(('ML', True, lambda: self._analyze_ml(ml_prediction, current_price)))

        for strategy_name, enabled, fn in analyzers:
            if not enabled:
                continue
            result = fn()
            if result:
                result = self._apply_regime_weighting(result, signal.market_context)
                signal.reasons.append(result)
                triggered_strategies.append(strategy_name)

        signal.strategies_triggered = triggered_strategies

        # 方向评分：按 strength * confidence 加权
        buy_score = 0.0
        sell_score = 0.0
        for reason in signal.reasons:
            weighted = float(reason.get('strength', 0)) * float(reason.get('confidence', 0.5))
            if reason.get('action') == 'buy':
                buy_score += weighted
            elif reason.get('action') == 'sell':
                sell_score += weighted

        signal.direction_score = {
            'buy': round(buy_score, 2),
            'sell': round(sell_score, 2),
            'net': round(abs(buy_score - sell_score), 2),
            'triggered_count': len(triggered_strategies)
        }

        composite_config = self.strategies_config.get('composite', {})
        min_strength = composite_config.get('min_strength', 20)
        min_strategy_count = composite_config.get('min_strategy_count', 1)

        dominant_action = 'hold'
        dominant_score = 0.0
        opposing_score = 0.0
        if buy_score > sell_score:
            dominant_action = 'buy'
            dominant_score = buy_score
            opposing_score = sell_score
        elif sell_score > buy_score:
            dominant_action = 'sell'
            dominant_score = sell_score
            opposing_score = buy_score

        # 净强度：主方向减去一半反方向噪音
        net_strength = max(0.0, dominant_score - opposing_score * 0.5)
        signal.strength = min(100, int(round(net_strength)))

        # 市场环境修正：逆趋势 / 波动异常会降置信，减少假信号
        signal.strength = self._adjust_strength_by_market_context(signal, dominant_action, signal.strength)

        # 只有有明确方向 + 达门槛，先判为 buy/sell
        if dominant_action != 'hold' and len(triggered_strategies) >= min_strategy_count and signal.strength >= min_strength:
            signal.signal_type = dominant_action

        return signal

    def _get_current_indicators(self, df: pd.DataFrame) -> Dict:
        indicators = {}
        close = df[4]
        if 'RSI' in df.columns:
            indicators['RSI'] = round(df['RSI'].iloc[-1], 2)
        if 'MACD' in df.columns:
            indicators['MACD'] = round(df['MACD'].iloc[-1], 4)
            indicators['MACD_signal'] = round(df['MACD_signal'].iloc[-1], 4)
            indicators['MACD_histogram'] = round(df['MACD'].iloc[-1] - df['MACD_signal'].iloc[-1], 4)
        if 'BB_upper' in df.columns:
            indicators['BB_upper'] = round(df['BB_upper'].iloc[-1], 2)
            indicators['BB_mid'] = round(df['BB_mid'].iloc[-1], 2)
            indicators['BB_lower'] = round(df['BB_lower'].iloc[-1], 2)
        if len(df) >= 5:
            indicators['MA5'] = round(close.rolling(5).mean().iloc[-1], 2)
        if len(df) >= 20:
            indicators['MA20'] = round(close.rolling(20).mean().iloc[-1], 2)
            indicators['volume'] = int(df[5].iloc[-1])
            indicators['volume_ma20'] = round(df[5].rolling(20).mean().iloc[-1], 0)
            returns = close.pct_change()
            indicators['volatility_20'] = round(float(returns.rolling(20).std().iloc[-1] or 0), 5)
            indicators['atr_ratio'] = round(float(self._calc_atr_ratio(df, 14)), 5)
        if len(df) >= 60:
            indicators['MA60'] = round(close.rolling(60).mean().iloc[-1], 2)
        return indicators

    def _calc_atr_ratio(self, df: pd.DataFrame, period: int = 14) -> float:
        if len(df) < period + 1:
            return 0.0
        high = df[2]
        low = df[3]
        close = df[4]
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean().iloc[-1]
        last_close = float(close.iloc[-1] or 1)
        return float(atr / last_close) if last_close else 0.0

    def _analyze_market_context(self, df: pd.DataFrame, current_price: float) -> Dict:
        close = df[4]
        ma20 = float(close.rolling(20).mean().iloc[-1]) if len(df) >= 20 else current_price
        ma60 = float(close.rolling(60).mean().iloc[-1]) if len(df) >= 60 else ma20
        trend_gap = ((ma20 - ma60) / ma60) if ma60 else 0.0
        volatility = float(close.pct_change().rolling(20).std().iloc[-1] or 0.0) if len(df) >= 20 else 0.0
        atr_ratio = self._calc_atr_ratio(df, 14)

        if trend_gap > 0.01:
            trend = 'bullish'
        elif trend_gap < -0.01:
            trend = 'bearish'
        else:
            trend = 'sideways'

        vol_cfg = self.config.get('market_filters', {})
        min_volatility = float(vol_cfg.get('min_volatility', 0.003))
        max_volatility = float(vol_cfg.get('max_volatility', 0.05))
        too_low = volatility < min_volatility
        too_high = volatility > max_volatility or atr_ratio > max_volatility

        return {
            'trend': trend,
            'trend_gap': round(trend_gap, 5),
            'volatility': round(volatility, 5),
            'atr_ratio': round(atr_ratio, 5),
            'volatility_too_low': too_low,
            'volatility_too_high': too_high,
            'market_ok': not too_low and not too_high,
        }

    def _apply_regime_weighting(self, reason: Dict, context: Dict) -> Dict:
        reason = dict(reason)
        trend = context.get('trend', 'sideways')
        strategy = reason.get('strategy')

        trend_following = {'MACD', 'MA_Cross', 'Volume', 'ML'}
        mean_reversion = {'RSI', 'Bollinger', 'Pattern'}

        multiplier = 1.0
        if trend in ['bullish', 'bearish']:
            if strategy in trend_following:
                multiplier *= 1.15
            if strategy in mean_reversion:
                multiplier *= 0.88
        elif trend == 'sideways':
            if strategy in mean_reversion:
                multiplier *= 1.12
            if strategy in trend_following:
                multiplier *= 0.9

        reason['strength'] = float(reason.get('strength', 0)) * multiplier
        reason.setdefault('metadata', {})['regime_multiplier'] = round(multiplier, 3)
        reason['metadata']['market_trend'] = trend
        return reason

    def _adjust_strength_by_market_context(self, signal: Signal, dominant_action: str, base_strength: int) -> int:
        context = signal.market_context or {}
        adjusted = float(base_strength)
        trend = context.get('trend', 'sideways')
        too_low = context.get('volatility_too_low', False)
        too_high = context.get('volatility_too_high', False)

        if dominant_action == 'buy' and trend == 'bearish':
            adjusted *= 0.65
        elif dominant_action == 'sell' and trend == 'bullish':
            adjusted *= 0.65
        elif dominant_action in ['buy', 'sell'] and trend != 'sideways':
            adjusted *= 1.08

        if too_low:
            adjusted *= 0.7
        if too_high:
            adjusted *= 0.78

        return min(100, max(0, int(round(adjusted))))

    def _analyze_rsi(self, df: pd.DataFrame, price: float) -> Optional[Dict]:
        config = self.strategies_config.get('rsi', {})
        period = config.get('period', 14)
        oversold = config.get('oversold', 35)
        overbought = config.get('overbought', 65)
        if 'RSI' not in df.columns:
            return None
        rsi = df['RSI'].iloc[-1]
        if rsi < 30:
            return {'strategy': 'RSI', 'type': 'rsi', 'action': 'buy', 'value': rsi, 'threshold': 30,
                    'detail': f'RSI严重超卖({rsi:.1f}<30)', 'triggered': True,
                    'strength': config.get('strength_weight', 30), 'confidence': 0.95,
                    'metadata': {'period': period, 'oversold': oversold}}
        elif rsi < oversold:
            return {'strategy': 'RSI', 'type': 'rsi', 'action': 'buy', 'value': rsi, 'threshold': oversold,
                    'detail': f'RSI超卖({rsi:.1f}<{oversold})', 'triggered': True,
                    'strength': config.get('strength_weight', 30) * 0.8, 'confidence': 0.75,
                    'metadata': {'period': period, 'oversold': oversold}}
        elif rsi > 70:
            return {'strategy': 'RSI', 'type': 'rsi', 'action': 'sell', 'value': rsi, 'threshold': 70,
                    'detail': f'RSI严重超买({rsi:.1f}>70)', 'triggered': True,
                    'strength': config.get('strength_weight', 30), 'confidence': 0.95,
                    'metadata': {'period': period, 'overbought': overbought}}
        elif rsi > overbought:
            return {'strategy': 'RSI', 'type': 'rsi', 'action': 'sell', 'value': rsi, 'threshold': overbought,
                    'detail': f'RSI超买({rsi:.1f}>{overbought})', 'triggered': True,
                    'strength': config.get('strength_weight', 30) * 0.8, 'confidence': 0.75,
                    'metadata': {'period': period, 'overbought': overbought}}
        return None

    def _analyze_macd(self, df: pd.DataFrame, price: float) -> Optional[Dict]:
        config = self.strategies_config.get('macd', {})
        if 'MACD' not in df.columns or 'MACD_signal' not in df.columns:
            return None
        macd = df['MACD'].iloc[-1]
        signal = df['MACD_signal'].iloc[-1]
        macd_prev = df['MACD'].iloc[-2] if len(df) > 1 else macd
        signal_prev = df['MACD_signal'].iloc[-2] if len(df) > 1 else signal
        if macd_prev <= signal_prev and macd > signal:
            below_zero = macd < 0
            return {'strategy': 'MACD', 'type': 'macd', 'action': 'buy', 'value': macd, 'signal_value': signal,
                    'detail': 'MACD金叉' + ('(零轴下方,强势)' if below_zero else ''), 'triggered': True,
                    'strength': config.get('strength_weight', 25) * (1.15 if below_zero else 1.0),
                    'confidence': 0.8 if below_zero else 0.65,
                    'metadata': {'crossover': 'bullish'}}
        elif macd_prev >= signal_prev and macd < signal:
            above_zero = macd > 0
            return {'strategy': 'MACD', 'type': 'macd', 'action': 'sell', 'value': macd, 'signal_value': signal,
                    'detail': 'MACD死叉' + ('(零轴上方,强势)' if above_zero else ''), 'triggered': True,
                    'strength': config.get('strength_weight', 25) * (1.15 if above_zero else 1.0),
                    'confidence': 0.8 if above_zero else 0.65,
                    'metadata': {'crossover': 'bearish'}}
        return None

    def _analyze_ma_cross(self, df: pd.DataFrame, price: float) -> Optional[Dict]:
        config = self.strategies_config.get('ma_cross', {})
        fast_period = config.get('fast_period', 5)
        slow_period = config.get('slow_period', 20)
        if len(df) < slow_period + 1:
            return None
        ma_fast = df[4].rolling(fast_period).mean().iloc[-1]
        ma_slow = df[4].rolling(slow_period).mean().iloc[-1]
        ma_fast_prev = df[4].rolling(fast_period).mean().iloc[-2]
        ma_slow_prev = df[4].rolling(slow_period).mean().iloc[-2]
        if ma_fast_prev <= ma_slow_prev and ma_fast > ma_slow:
            deviation = (ma_fast - ma_slow) / ma_slow * 100
            return {'strategy': 'MA_Cross', 'type': 'ma', 'action': 'buy', 'value': ma_fast, 'slow_value': ma_slow,
                    'detail': f'MA{fast_period}上穿MA{slow_period}', 'triggered': True,
                    'strength': config.get('strength_weight', 25), 'confidence': 0.65 + min(0.15, deviation / 10),
                    'metadata': {'deviation': deviation}}
        elif ma_fast_prev >= ma_slow_prev and ma_fast < ma_slow:
            deviation = (ma_slow - ma_fast) / ma_slow * 100
            return {'strategy': 'MA_Cross', 'type': 'ma', 'action': 'sell', 'value': ma_fast, 'slow_value': ma_slow,
                    'detail': f'MA{fast_period}下穿MA{slow_period}', 'triggered': True,
                    'strength': config.get('strength_weight', 25), 'confidence': 0.65 + min(0.15, deviation / 10),
                    'metadata': {'deviation': deviation}}
        return None

    def _analyze_bollinger(self, df: pd.DataFrame, price: float) -> Optional[Dict]:
        config = self.strategies_config.get('bollinger', {})
        if 'BB_lower' not in df.columns or 'BB_upper' not in df.columns:
            return None
        lower = df['BB_lower'].iloc[-1]
        upper = df['BB_upper'].iloc[-1]
        if upper == lower:
            return None
        position = (price - lower) / (upper - lower)
        if price <= lower:
            return {'strategy': 'Bollinger', 'type': 'bollinger', 'action': 'buy', 'value': price,
                    'detail': '价格触及布林下轨', 'triggered': True,
                    'strength': config.get('strength_weight', 20), 'confidence': 0.68,
                    'metadata': {'position': position}}
        elif price >= upper:
            return {'strategy': 'Bollinger', 'type': 'bollinger', 'action': 'sell', 'value': price,
                    'detail': '价格触及布林上轨', 'triggered': True,
                    'strength': config.get('strength_weight', 20), 'confidence': 0.68,
                    'metadata': {'position': position}}
        elif position < 0.15:
            return {'strategy': 'Bollinger', 'type': 'bollinger', 'action': 'buy', 'value': price,
                    'detail': f'接近布林下轨({position*100:.0f}%)', 'triggered': True,
                    'strength': config.get('strength_weight', 20) * 0.6, 'confidence': 0.52,
                    'metadata': {'position': position}}
        elif position > 0.85:
            return {'strategy': 'Bollinger', 'type': 'bollinger', 'action': 'sell', 'value': price,
                    'detail': f'接近布林上轨({position*100:.0f}%)', 'triggered': True,
                    'strength': config.get('strength_weight', 20) * 0.6, 'confidence': 0.52,
                    'metadata': {'position': position}}
        return None

    def _analyze_volume(self, df: pd.DataFrame, price: float) -> Optional[Dict]:
        config = self.strategies_config.get('volume', {})
        period = config.get('volume_ma_period', 20)
        multiplier = config.get('volume_multiplier', 1.5)
        if len(df) < period + 1:
            return None
        volume = df[5].iloc[-1]
        volume_ma = df[5].rolling(period).mean().iloc[-1]
        if volume_ma <= 0:
            return None
        if volume > volume_ma * multiplier:
            price_change = (df[4].iloc[-1] - df[4].iloc[-2]) / df[4].iloc[-2]
            change_pct = price_change * 100
            if price_change > 0.008:
                return {'strategy': 'Volume', 'type': 'volume', 'action': 'buy', 'value': volume,
                        'detail': f'成交量放大({volume/volume_ma:.1f}倍),量价齐升', 'triggered': True,
                        'strength': config.get('strength_weight', 15), 'confidence': 0.72,
                        'metadata': {'change_pct': change_pct}}
            elif price_change < -0.008:
                return {'strategy': 'Volume', 'type': 'volume', 'action': 'sell', 'value': volume,
                        'detail': f'成交量放大({volume/volume_ma:.1f}倍),放量下跌', 'triggered': True,
                        'strength': config.get('strength_weight', 15), 'confidence': 0.72,
                        'metadata': {'change_pct': change_pct}}
        return None

    def _analyze_pattern(self, df: pd.DataFrame, price: float) -> Optional[Dict]:
        config = self.strategies_config.get('pattern', {})
        if len(df) < 5:
            return None
        opens = df[1].values
        closes = df[4].values
        highs = df[2].values
        lows = df[3].values
        open_p, close_p, high_p, low_p = opens[-1], closes[-1], highs[-1], lows[-1]
        body = abs(close_p - open_p)
        total_range = max(1e-10, high_p - low_p)
        upper_shadow = high_p - max(open_p, close_p)
        lower_shadow = min(open_p, close_p) - low_p
        if lower_shadow > body * 2 and upper_shadow < body * 0.3:
            return {'strategy': 'Pattern', 'type': 'pattern', 'action': 'buy', 'value': close_p,
                    'detail': '锤子线(看涨反转)', 'triggered': True,
                    'strength': config.get('strength_weight', 20), 'confidence': 0.62,
                    'metadata': {'pattern': 'hammer'}}
        if upper_shadow > body * 2 and lower_shadow < body * 0.3:
            return {'strategy': 'Pattern', 'type': 'pattern', 'action': 'sell', 'value': close_p,
                    'detail': '上吊线(看跌反转)', 'triggered': True,
                    'strength': config.get('strength_weight', 20), 'confidence': 0.62,
                    'metadata': {'pattern': 'hanging_man'}}
        return None

    def _analyze_ml(self, prediction: tuple, price: float) -> Optional[Dict]:
        if not prediction:
            return None
        pred, prob = prediction
        min_confidence = self.config.get('ml', {}).get('min_confidence', 0.6)
        if pred == 1 and prob >= min_confidence:
            return {'strategy': 'ML', 'type': 'ml', 'action': 'buy', 'value': prob,
                    'detail': f'ML预测上涨概率{prob*100:.1f}%', 'triggered': True,
                    'strength': int(prob * 40), 'confidence': prob,
                    'metadata': {'prediction': 'up'}}
        if pred == 0 and (1 - prob) >= min_confidence:
            down_conf = 1 - prob
            return {'strategy': 'ML', 'type': 'ml', 'action': 'sell', 'value': down_conf,
                    'detail': f'ML预测下跌概率{down_conf*100:.1f}%', 'triggered': True,
                    'strength': int(down_conf * 40), 'confidence': down_conf,
                    'metadata': {'prediction': 'down'}}
        return None
