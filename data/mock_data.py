"""
模拟数据生成器 - 用于测试策略
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta


def generate_price_data(
    initial_price: float = 50000,
    days: int = 365,
    interval: str = '1h',
    volatility: float = 0.03,
    trend: float = 0.0001
) -> pd.DataFrame:
    """生成模拟K线数据
    
    Args:
        initial_price: 初始价格
        days: 天数
        interval: K线周期
        volatility: 波动率
        trend: 趋势 (正=上涨, 负=下跌)
        
    Returns:
        DataFrame with OHLCV data
    """
    # 计算周期数
    interval_hours = {
        '1m': 1/60,
        '5m': 5/60,
        '15m': 15/60,
        '1h': 1,
        '4h': 4,
        '1d': 24
    }
    
    hours = interval_hours.get(interval, 1)
    periods = int(days * 24 / hours)
    
    # 生成日期
    start_date = datetime.now() - timedelta(days=days)
    dates = pd.date_range(start_date, periods=periods, freq=f'{int(hours*60)}min')
    
    # 生成价格 (几何布朗运动)
    np.random.seed(42)
    returns = np.random.normal(trend/24, volatility/np.sqrt(24), periods)
    prices = initial_price * np.exp(np.cumsum(returns))
    
    # 生成OHLC
    df = pd.DataFrame()
    df['datetime'] = dates
    df['close'] = prices
    
    # 生成open, high, low
    df['open'] = df['close'] * (1 + np.random.uniform(-0.005, 0.005, periods))
    df['high'] = np.maximum(df['open'], df['close']) * (1 + np.random.uniform(0, 0.01, periods))
    df['low'] = np.minimum(df['open'], df['close']) * (1 - np.random.uniform(0, 0.01, periods))
    
    # 生成成交量 (与价格变动相关)
    base_volume = 1000
    df['volume'] = base_volume * (1 + np.abs(returns) * 100) * np.random.uniform(0.5, 1.5, periods)
    
    df.set_index('datetime', inplace=True)
    df['timestamp'] = df.index.astype('int64') // 10**6
    
    return df


def generate_trending_price_data(
    initial_price: float = 50000,
    days: int = 180,
    interval: str = '1h',
    trend_type: str = 'bull'  # bull/bear/sideways
) -> pd.DataFrame:
    """生成趋势型价格数据
    
    Args:
        initial_price: 初始价格
        days: 天数
        interval: K线周期
        trend_type: 趋势类型 'bull'(牛市)/'bear'(熊市)/'sideways'(震荡)
    """
    if trend_type == 'bull':
        # 牛市：震荡上行
        trend = 0.0003
        volatility = 0.025
    elif trend_type == 'bear':
        # 熊市：震荡下行
        trend = -0.0002
        volatility = 0.03
    else:
        # 震荡：区间波动
        trend = 0
        volatility = 0.015
    
    return generate_price_data(initial_price, days, interval, volatility, trend)


def generate_volatile_price_data(
    initial_price: float = 50000,
    days: int = 90,
    interval: str = '1h'
) -> pd.DataFrame:
    """生成高波动价格数据 (模拟币圈暴涨暴跌)"""
    trend = 0.0001
    volatility = 0.05  # 高波动
    
    return generate_price_data(initial_price, days, interval, volatility, trend)


if __name__ == '__main__':
    # 测试生成数据
    print("生成模拟数据...")
    
    # 正常市场
    df1 = generate_price_data(50000, 365, '1h')
    print(f"正常市场: {len(df1)} 根K线")
    print(df1.tail())
    
    # 牛市
    df2 = generate_trending_price_data(50000, 180, '1h', 'bull')
    print(f"\n牛市: {len(df2)} 根K线")
    print(f"价格范围: ${df2['low'].min():.2f} - ${df2['high'].max():.2f}")
