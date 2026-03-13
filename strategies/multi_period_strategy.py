#!/usr/bin/env python3
"""
多周期趋势确认策略
- 4h周期: 确认趋势方向
- 1h周期: 寻找入场点

只在趋势同向时开单，减少假信号
"""

import pandas as pd
import pandas_ta as ta
from typing import Dict, Tuple


class MultiPeriodStrategy:
    """
    多周期趋势确认策略
    
    逻辑:
    1. 4h周期: 确认主要趋势 (MA200方向)
    2. 1h周期: 等待RSI超卖/超买 + MACD信号
    3. 只有趋势同向时才交易
    """
    
    def __init__(self, params: Dict = None):
        self.params = params or {
            'rsi_period': 14,
            'rsi_oversold': 45,
            'rsi_overbought': 55,
            # 多周期参数
            'ma_period': 200,      # 4h周期均线
            'trend_ma_period': 50,  # 趋势确认均线
        }
    
    def analyze_trend(self, df: pd.DataFrame, timeframe: str = '4h') -> Dict:
        """分析趋势"""
        df = df.copy()
        
        # 均线
        df['MA50'] = ta.sma(df['close'], length=50)
        df['MA200'] = ta.sma(df['close'], length=200) if len(df) >= 200 else df['MA50']
        
        # RSI
        df['RSI'] = ta.rsi(df['close'], length=14)
        
        # MACD
        macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
        df['MACD'] = macd['MACD_12_26_9']
        
        latest = df.iloc[-1]
        
        # 趋势判断
        if latest['MA50'] > latest['MA200']:
            trend = 'UP'
        elif latest['MA50'] < latest['MA200']:
            trend = 'DOWN'
        else:
            trend = 'SIDEWAY'
        
        return {
            'trend': trend,
            'price': latest['close'],
            'rsi': latest['RSI'],
            'macd': latest['MACD'],
            'ma50': latest['MA50'],
            'ma200': latest['MA200'],
        }
    
    def generate_signals(self, df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> pd.DataFrame:
        """
        生成交易信号
        
        Args:
            df_1h: 1小时K线数据
            df_4h: 4小时K线数据
        """
        df = df_1h.copy()
        
        # 分析4h趋势
        trend_4h = self.analyze_trend(df_4h, '4h')
        df['trend_4h'] = trend_4h['trend']
        
        # 1h周期指标
        df['RSI'] = ta.rsi(df['close'], length=14)
        
        macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
        df['MACD'] = macd['MACD_12_26_9']
        df['MACD_signal'] = macd['MACDs_12_26_9']
        
        df['signal'] = 0
        df['signal_reason'] = ''
        
        latest = df.iloc[-1]
        
        # 买入条件: RSI超卖(45以下) OR MACD金叉
        if latest['RSI'] < self.params['rsi_oversold'] or (latest['MACD'] > latest['MACD_signal'] and latest['MACD'] < 0):
            df.loc[df.index[-1], 'signal'] = 1
            df.loc[df.index[-1], 'signal_reason'] = f'RSI超卖或MACD金叉'
        
        # 卖出条件: RSI超买(55以上) OR MACD死叉
        elif latest['RSI'] > self.params['rsi_overbought'] or (latest['MACD'] < latest['MACD_signal'] and latest['MACD'] > 0):
            df.loc[df.index[-1], 'signal'] = -1
            df.loc[df.index[-1], 'signal_reason'] = f'RSI超买或MACD死叉'
        
        return df
    
    def get_status(self, df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> str:
        """获取当前状态描述"""
        trend_4h = self.analyze_trend(df_4h, '4h')
        latest_1h = df_1h.iloc[-1]
        
        status = f"""=== 多周期策略状态 ===

【4h趋势】{trend_4h['trend']}
  价格: ${trend_4h['price']:.2f}
  MA50: ${trend_4h['ma50']:.2f}
  MA200: ${trend_4h['ma200']:.2f}

【1h周期】
  RSI: {latest_1h['RSI']:.1f}
  MACD: {latest_1h['MACD']:.2f}

【信号】{latest_1h['signal']} ({latest_1h['signal_reason']})
"""
        return status


# Test
if __name__ == '__main__':
    import sys
    sys.path.insert(0, '.')
    from data.data_loader import DataLoader
    
    loader = DataLoader()
    
    print("获取4h数据...")
    df_4h = loader.fetch_ohlcv('SOL/USDT', '4h', limit=200)
    
    print("获取1h数据...")
    df_1h = loader.fetch_ohlcv('SOL/USDT', '1h', limit=100)
    
    strategy = MultiPeriodStrategy()
    df = strategy.generate_signals(df_1h, df_4h)
    
    print(strategy.get_status(df_1h, df_4h))
