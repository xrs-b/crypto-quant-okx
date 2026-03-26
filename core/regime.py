"""
Regime Layer v1 - 市场状态识别层
轻量版：基于趋势、波动率的简单分类
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


REGIME_DETECTOR_VERSION = "regime_v1_m0"


class Regime(Enum):
    """市场状态枚举"""
    TREND = "trend"           # 趋势明显 (上涨/下跌)
    RANGE = "range"           # 震荡/盘整
    HIGH_VOL = "high_vol"     # 高波动
    LOW_VOL = "low_vol"       # 低波动
    RISK_ANOMALY = "risk_anomaly"  # 风险异常
    UNKNOWN = "unknown"       # 未知/数据不足


REGIME_FAMILY_MAP = {
    Regime.TREND: "trend",
    Regime.RANGE: "range",
    Regime.HIGH_VOL: "vol",
    Regime.LOW_VOL: "vol",
    Regime.RISK_ANOMALY: "risk",
    Regime.UNKNOWN: "unknown",
}


@dataclass
class RegimeResult:
    """Regime 检测结果"""
    regime: Regime
    confidence: float  # 0-1, 置信度
    indicators: Dict[str, Any] = field(default_factory=dict)
    details: str = ""      # 简要说明
    detected_at: Optional[str] = None
    detector_version: str = REGIME_DETECTOR_VERSION
    features: Optional[Dict[str, Any]] = None
    name: Optional[str] = None
    family: Optional[str] = None
    direction: Optional[str] = None
    stability_score: Optional[float] = None
    transition_risk: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        features = self.features if self.features is not None else dict(self.indicators or {})
        snapshot = build_regime_snapshot(
            regime=self.regime,
            confidence=self.confidence,
            indicators=self.indicators,
            details=self.details,
            features=features,
            detected_at=self.detected_at,
            detector_version=self.detector_version,
            name=self.name,
            family=self.family,
            direction=self.direction,
            stability_score=self.stability_score,
            transition_risk=self.transition_risk,
        )
        return snapshot


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _round_snapshot_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 5)
    if isinstance(value, dict):
        return {k: _round_snapshot_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round_snapshot_value(v) for v in value]
    return value


def _coerce_regime(value: Any) -> Regime:
    if isinstance(value, Regime):
        return value
    if isinstance(value, str):
        try:
            return Regime(value)
        except ValueError:
            return Regime.UNKNOWN
    return Regime.UNKNOWN


def get_regime_family(regime: Any) -> str:
    return REGIME_FAMILY_MAP.get(_coerce_regime(regime), "unknown")


def get_regime_direction(regime: Any, indicators: Optional[Dict[str, Any]] = None) -> str:
    indicators = indicators or {}
    normalized = _coerce_regime(regime)
    ema_direction = indicators.get('ema_direction')
    if normalized in {Regime.RANGE, Regime.LOW_VOL}:
        return 'neutral'
    if normalized in {Regime.RISK_ANOMALY, Regime.UNKNOWN}:
        return 'unknown'
    if ema_direction is None:
        return 'unknown'
    return 'up' if float(ema_direction) >= 0 else 'down'


def compute_regime_stability_score(regime: Any, indicators: Optional[Dict[str, Any]] = None, confidence: float = 0.0) -> float:
    indicators = indicators or {}
    normalized = _coerce_regime(regime)
    ema_gap = abs(float(indicators.get('ema_gap', 0.0) or 0.0))
    volatility = float(indicators.get('volatility', 0.0) or 0.0)
    base_conf = max(0.0, min(1.0, float(confidence or 0.0)))

    if normalized == Regime.TREND:
        score = 0.45 + min(0.35, ema_gap * 8) + base_conf * 0.2 - min(0.15, volatility * 1.5)
    elif normalized == Regime.RANGE:
        score = 0.50 + base_conf * 0.2 - min(0.20, ema_gap * 6) - min(0.10, volatility)
    elif normalized == Regime.LOW_VOL:
        score = 0.60 + base_conf * 0.2 - min(0.15, ema_gap * 5)
    elif normalized == Regime.HIGH_VOL:
        score = 0.35 + base_conf * 0.15 + min(0.15, ema_gap * 5) - min(0.20, volatility * 2)
    elif normalized == Regime.RISK_ANOMALY:
        score = 0.15 + base_conf * 0.10 - min(0.10, volatility)
    else:
        score = 0.0
    return round(max(0.0, min(1.0, score)), 3)


def compute_regime_transition_risk(regime: Any, indicators: Optional[Dict[str, Any]] = None, confidence: float = 0.0) -> float:
    stability = compute_regime_stability_score(regime, indicators, confidence)
    indicators = indicators or {}
    volatility = float(indicators.get('volatility', 0.0) or 0.0)
    extra_risk = min(0.35, volatility * 3)
    risk = 1.0 - stability + extra_risk
    return round(max(0.0, min(1.0, risk)), 3)


def build_regime_snapshot(
    regime: Any,
    confidence: float,
    indicators: Optional[Dict[str, Any]] = None,
    details: str = "",
    *,
    features: Optional[Dict[str, Any]] = None,
    detected_at: Optional[str] = None,
    detector_version: str = REGIME_DETECTOR_VERSION,
    name: Optional[str] = None,
    family: Optional[str] = None,
    direction: Optional[str] = None,
    stability_score: Optional[float] = None,
    transition_risk: Optional[float] = None,
) -> Dict[str, Any]:
    normalized = _coerce_regime(regime)
    indicators = dict(indicators or {})
    features = dict(features or indicators)
    confidence = round(max(0.0, min(1.0, float(confidence or 0.0))), 3)
    family = family or get_regime_family(normalized)
    direction = direction or get_regime_direction(normalized, indicators)
    stability_score = compute_regime_stability_score(normalized, indicators, confidence) if stability_score is None else round(float(stability_score), 3)
    transition_risk = compute_regime_transition_risk(normalized, indicators, confidence) if transition_risk is None else round(float(transition_risk), 3)

    return {
        'regime': normalized.value,
        'name': name or normalized.value,
        'family': family,
        'direction': direction,
        'confidence': confidence,
        'stability_score': stability_score,
        'transition_risk': transition_risk,
        'indicators': _round_snapshot_value(indicators),
        'features': _round_snapshot_value(features),
        'details': details,
        'detected_at': detected_at or utc_now_iso(),
        'detector_version': detector_version or REGIME_DETECTOR_VERSION,
    }


def normalize_regime_snapshot(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    snapshot = dict(snapshot or {})
    regime_value = snapshot.get('name') or snapshot.get('regime') or Regime.UNKNOWN.value
    indicators = dict(snapshot.get('indicators') or snapshot.get('features') or {})
    return build_regime_snapshot(
        regime=regime_value,
        confidence=snapshot.get('confidence', 0.0),
        indicators=indicators,
        details=snapshot.get('details', ''),
        features=snapshot.get('features') or indicators,
        detected_at=snapshot.get('detected_at'),
        detector_version=snapshot.get('detector_version', REGIME_DETECTOR_VERSION),
        name=snapshot.get('name') or regime_value,
        family=snapshot.get('family'),
        direction=snapshot.get('direction'),
        stability_score=snapshot.get('stability_score'),
        transition_risk=snapshot.get('transition_risk'),
    )


class RegimeDetector:
    """Regime 检测器 - 轻量版 v1"""

    DEFAULT_CONFIG = {
        'ema_short': 20,
        'ema_long': 60,
        'ema_gap_threshold': 0.015,
        'volatility_lookback': 20,
        'high_vol_threshold': 0.04,
        'low_vol_threshold': 0.008,
        'price_change_threshold': 0.08,
        'volume_spike_ratio': 2.5,
        'min_data_points': 30,
    }

    def __init__(self, config: Optional[Dict] = None):
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}

    def detect(self, df: pd.DataFrame, price: float = None) -> RegimeResult:
        if not self._validate_data(df):
            return RegimeResult(
                regime=Regime.UNKNOWN,
                confidence=0.0,
                indicators={},
                details="数据不足，回退旧逻辑",
            )

        indicators = self._calculate_indicators(df, price)
        regime, confidence, details = self._classify(indicators)
        return RegimeResult(
            regime=regime,
            confidence=confidence,
            indicators=indicators,
            details=details,
        )

    def _validate_data(self, df: pd.DataFrame) -> bool:
        required_cols = ['close']
        if not all(col in df.columns for col in required_cols):
            return False
        min_points = self.config.get('min_data_points', 30)
        return len(df) >= min_points

    def _calculate_indicators(self, df: pd.DataFrame, price: float = None) -> Dict:
        close = df['close']
        high = df.get('high', close)
        low = df.get('low', close)
        volume = df.get('volume', pd.Series([1] * len(df)))
        current_price = price if price else close.iloc[-1]

        ema_short = self.config.get('ema_short', 20)
        ema_long = self.config.get('ema_long', 60)
        ema_s = close.ewm(span=ema_short, adjust=False).mean()
        ema_l = close.ewm(span=ema_long, adjust=False).mean()
        ema_gap = (ema_s.iloc[-1] - ema_l.iloc[-1]) / ema_l.iloc[-1]

        volatility_lookback = self.config.get('volatility_lookback', 20)
        returns = close.pct_change().dropna()
        if len(returns) >= volatility_lookback:
            volatility = returns.tail(volatility_lookback).std()
        else:
            volatility = returns.std() if len(returns) > 0 else 0

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

        price_change_1d = (current_price - close.iloc[-2]) / close.iloc[-2] if len(close) >= 2 else 0
        price_change_5d = (current_price - close.iloc[-6]) / close.iloc[-6] if len(close) >= 6 else 0
        vol_ma = volume.tail(20).mean()
        vol_ratio = volume.iloc[-1] / vol_ma if vol_ma > 0 else 1
        trend_strength = abs(ema_gap) * 10

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
        ema_gap = indicators.get('ema_gap', 0)
        volatility = indicators.get('volatility', 0)
        price_change_1d = abs(indicators.get('price_change_1d', 0))
        volume_ratio = indicators.get('volume_ratio', 1)

        ema_gap_threshold = self.config.get('ema_gap_threshold', 0.015)
        high_vol_threshold = self.config.get('high_vol_threshold', 0.04)
        low_vol_threshold = self.config.get('low_vol_threshold', 0.008)
        price_change_threshold = self.config.get('price_change_threshold', 0.08)
        volume_spike_ratio = self.config.get('volume_spike_ratio', 2.5)

        if price_change_1d > price_change_threshold:
            return Regime.RISK_ANOMALY, 0.85, f"价格日内波动异常({price_change_1d*100:.1f}%)"
        if volume_ratio > volume_spike_ratio and volatility > high_vol_threshold:
            return Regime.RISK_ANOMALY, 0.75, f"量价齐升异常(vol={volume_ratio:.1f}x)"
        if volatility > high_vol_threshold:
            direction = "上涨" if ema_gap > 0 else "下跌"
            return Regime.HIGH_VOL, 0.7, f"高波动趋势中({direction}, vol={volatility*100:.1f}%)"
        if volatility < low_vol_threshold:
            return Regime.LOW_VOL, 0.7, f"低波动盘整(vol={volatility*100:.2f}%)"
        if abs(ema_gap) > ema_gap_threshold:
            direction = "上涨" if ema_gap > 0 else "下跌"
            confidence = min(0.9, 0.5 + abs(ema_gap) * 10)
            return Regime.TREND, confidence, f"趋势{direction}(gap={ema_gap*100:.2f}%)"
        return Regime.RANGE, 0.6, f"区间震荡(gap={ema_gap*100:.2f}%, vol={volatility*100:.2f}%)"


def detect_regime(df: pd.DataFrame, price: float = None, config: Dict = None) -> RegimeResult:
    detector = RegimeDetector(config)
    return detector.detect(df, price)
