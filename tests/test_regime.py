"""
Regime Layer v1 测试
"""
import pandas as pd
import numpy as np
import unittest
from datetime import datetime, timedelta

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.regime import RegimeDetector, Regime, detect_regime


def generate_test_data(regime_type: str, n: int = 100) -> pd.DataFrame:
    """生成测试数据"""
    np.random.seed(42)
    dates = pd.date_range(end=datetime.now(), periods=n, freq='1h')
    
    if regime_type == 'trend_up':
        # 上涨趋势
        base = 1000
        trend = np.linspace(0, 200, n)
        noise = np.random.normal(0, 10, n)
        close = base + trend + noise
        
    elif regime_type == 'trend_down':
        # 下跌趋势
        base = 1200
        trend = np.linspace(0, -200, n)
        noise = np.random.normal(0, 10, n)
        close = base + trend + noise
        
    elif regime_type == 'range':
        # 震荡
        base = 1000
        cycle = np.sin(np.linspace(0, 10 * np.pi, n)) * 50
        noise = np.random.normal(0, 15, n)
        close = base + cycle + noise
        
    elif regime_type == 'high_vol':
        # 高波动
        base = 1000
        trend = np.linspace(0, 50, n)
        noise = np.random.normal(0, 80, n)  # 大噪声
        close = base + trend + noise
        
    elif regime_type == 'low_vol':
        # 低波动
        base = 1000
        trend = np.sin(np.linspace(0, 2 * np.pi, n)) * 20
        noise = np.random.normal(0, 3, n)
        close = base + trend + noise
        
    else:
        close = np.random.uniform(900, 1100, n) + np.random.normal(0, 5, n)
    
    # 生成 OHLCV
    data = {
        'close': close,
        'open': close * (1 + np.random.uniform(-0.01, 0.01, n)),
        'high': close * (1 + np.abs(np.random.uniform(0, 0.02, n))),
        'low': close * (1 - np.abs(np.random.uniform(0, 0.02, n))),
        'volume': np.random.uniform(1000, 5000, n),
    }
    
    df = pd.DataFrame(data, index=dates)
    return df


class TestRegimeDetector(unittest.TestCase):
    """Regime 检测器测试"""
    
    def setUp(self):
        self.detector = RegimeDetector()
    
    def test_trend_up(self):
        """测试上涨趋势"""
        df = generate_test_data('trend_up', 100)
        result = self.detector.detect(df)
        
        print(f"\n[Trend Up] Regime: {result.regime}, Confidence: {result.confidence}")
        print(f"  Details: {result.details}")
        print(f"  Indicators: {result.indicators}")
        
        # 趋势应该被检测到
        self.assertIn(result.regime, [Regime.TREND, Regime.HIGH_VOL])
    
    def test_trend_down(self):
        """测试下跌趋势"""
        df = generate_test_data('trend_down', 100)
        result = self.detector.detect(df)
        
        print(f"\n[Trend Down] Regime: {result.regime}, Confidence: {result.confidence}")
        print(f"  Details: {result.details}")
        
        self.assertIn(result.regime, [Regime.TREND, Regime.HIGH_VOL])
    
    def test_range(self):
        """测试震荡市场"""
        df = generate_test_data('range', 100)
        result = self.detector.detect(df)
        
        print(f"\n[Range] Regime: {result.regime}, Confidence: {result.confidence}")
        print(f"  Details: {result.details}")
        
        # 震荡或低波动
        self.assertIn(result.regime, [Regime.RANGE, Regime.LOW_VOL, Regime.TREND])
    
    def test_high_vol(self):
        """测试高波动"""
        df = generate_test_data('high_vol', 100)
        result = self.detector.detect(df)
        
        print(f"\n[High Vol] Regime: {result.regime}, Confidence: {result.confidence}")
        print(f"  Details: {result.details}")
        
        self.assertIn(result.regime, [Regime.HIGH_VOL, Regime.RISK_ANOMALY])
    
    def test_low_vol(self):
        """测试低波动"""
        df = generate_test_data('low_vol', 100)
        result = self.detector.detect(df)
        
        print(f"\n[Low Vol] Regime: {result.regime}, Confidence: {result.confidence}")
        print(f"  Details: {result.details}")
        
        self.assertIn(result.regime, [Regime.LOW_VOL, Regime.RANGE])
    
    def test_insufficient_data(self):
        """测试数据不足"""
        df = generate_test_data('trend_up', 10)  # 太少数据
        result = self.detector.detect(df)
        
        print(f"\n[Insufficient] Regime: {result.regime}, Confidence: {result.confidence}")
        
        self.assertEqual(result.regime, Regime.UNKNOWN)
    
    def test_empty_data(self):
        """测试空数据"""
        df = pd.DataFrame()
        result = self.detector.detect(df)
        
        self.assertEqual(result.regime, Regime.UNKNOWN)
    
    def test_convenience_function(self):
        """测试便捷函数"""
        df = generate_test_data('trend_up', 100)
        result = detect_regime(df)
        
        self.assertIsInstance(result.regime, Regime)
        self.assertIsInstance(result.confidence, float)


def run_tests():
    """运行测试"""
    print("=" * 60)
    print("Regime Layer v1 Tests")
    print("=" * 60)
    
    # 创建测试套件
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestRegimeDetector)
    
    # 运行
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    print("\n" + "=" * 60)
    if result.wasSuccessful():
        print("✅ All tests passed!")
    else:
        print(f"❌ {len(result.failures)} failures, {len(result.errors)} errors")
    print("=" * 60)
    
    return result.wasSuccessful()


if __name__ == '__main__':
    run_tests()
