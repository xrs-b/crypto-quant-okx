#!/usr/bin/env python3
"""
优化版趋势策略 - 自适应RSI阈值
基于市场波动性(ATR)自动调整RSI超买超卖阈值
"""

import pandas as pd
import pandas_ta as ta
from typing import Dict


class Signal:
    NONE = 0
    BUY = 1
    SELL = -1


class OptimizedTrendStrategy:
    """
    优化趋势策略 - 自适应RSI
    
    核心改进：
    1. 根据ATR自动调整RSI阈值
    2. 高波动市场：放宽阈值 (更保守)
    3. 低波动市场：收紧阈值 (更激进)
    """
    
    def __init__(self, params: Dict = None):
        # 默认参数
        self.params = params or {
            'rsi_period': 14,
            'macd_fast': 12,
            'macd_slow': 26,
            'macd_signal': 9,
            # 基础阈值
            'rsi_oversold_base': 30,
            'rsi_overbought_base': 70,
            # ATR乘数 - 用于计算自适应阈值
            'atr_multiplier': 1.5,
        }
    
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成交易信号"""
        df = df.copy()
        
        # 计算RSI
        df['RSI'] = ta.rsi(df['close'], length=self.params['rsi_period'])
        
        # 计算MACD
        macd = ta.macd(
            df['close'],
            fast=self.params['macd_fast'],
            slow=self.params['macd_slow'],
            signal=self.params['macd_signal']
        )
        df['MACD'] = macd['MACD_12_26_9']
        df['MACD_signal'] = macd['MACDs_12_26_9']
        df['MACD_hist'] = macd['MACDh_12_26_9']
        
        # 计算ATR (用于自适应阈值)
        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        
        # 计算ATR占价格的百分比 (波动率)
        df['ATR_pct'] = (df['ATR'] / df['close']) * 100
        
        # 自适应RSI阈值
        # 波动率高 → 阈值范围大
        # 波动率低 → 阈值范围小
        atr_pct = df['ATR_pct'].iloc[-1] if len(df) > 0 else 1.0
        
        # 根据波动率调整阈值
        # 正常波动(1-2%): 30/70
        # 高波动(>2%): 25/75 (更保守)
        # 低波动(<1%): 35/65 (更激进)
        if atr_pct > 2.0:
            # 高波动市场 - 扩大阈值范围
            rsi_oversold = self.params['rsi_oversold_base'] - 5
            rsi_overbought = self.params['rsi_overbought_base'] + 5
        elif atr_pct < 1.0:
            # 低波动市场 - 收窄阈值范围
            rsi_oversold = self.params['rsi_oversold_base'] + 5
            rsi_overbought = self.params['rsi_overbought_base'] - 5
        else:
            # 正常波动
            rsi_oversold = self.params['rsi_oversold_base']
            rsi_overbought = self.params['rsi_overbought_base']
        
        df['rsi_oversold'] = rsi_oversold
        df['rsi_overbought'] = rsi_overbought
        
        # 生成信号
        df['signal'] = Signal.NONE
        
        # 买入条件: RSI超卖 + MACD金叉 (MACD从负转正或MACD线上穿signal线)
        buy_condition = (
            (df['RSI'] < rsi_oversold) &
            (df['MACD'] > df['MACD_signal']) &
            (df['MACD'].shift(1) <= df['MACD_signal'].shift(1))
        )
        
        # 卖出条件: RSI超买 + MACD死叉
        sell_condition = (
            (df['RSI'] > rsi_overbought) &
            (df['MACD'] < df['MACD_signal']) &
            (df['MACD'].shift(1) >= df['MACD_signal'].shift(1))
        )
        
        df.loc[buy_condition, 'signal'] = Signal.BUY
        df.loc[sell_condition, 'signal'] = Signal.SELL
        
        # 信号原因
        df['signal_reason'] = ''
        df.loc[buy_condition, 'signal_reason'] = f'RSI<{rsi_oversold}+MACD金叉 (波动率:{atr_pct:.1f}%)'
        df.loc[sell_condition, 'signal_reason'] = f'RSI>{rsi_overbought}+MACD死叉 (波动率:{atr_pct:.1f}%)'
        
        return df
    
    def get_params(self) -> Dict:
        return self.params


# 如果直接运行
if __name__ == '__main__':
    from data.data_loader import DataLoader
    
    # 测试
    loader = DataLoader()
    df = loader.fetch_ohlcv('SOL/USDT', '1h', limit=50)
    
    strategy = OptimizedTrendStrategy()
    df = strategy.generate_signals(df)
    
    latest = df.iloc[-1]
    
    print("=" * 50)
    print("优化版趋势策略 - 自适应RSI")
    print("=" * 50)
    print(f"RSI: {latest['RSI']:.1f}")
    print(f"MACD: {latest['MACD']:.2f}")
    print(f"ATR: {latest['ATR']:.2f}")
    print(f"波动率: {latest['ATR_pct']:.2f}%")
    print(f"当前阈值: {latest['rsi_oversold']:.0f} / {latest['rsi_overbought']:.0f}")
    print(f"信号: {latest['signal']} ({latest['signal_reason']})")
    print("=" * 50)
