"""
策略基类 - 所有策略的父类
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict
import pandas as pd
import pandas_ta as ta


class Signal:
    """交易信号"""
    NONE = 0
    BUY = 1
    SELL = -1


class BaseStrategy(ABC):
    """策略基类"""
    
    def __init__(self, name: str = "BaseStrategy"):
        self.name = name
        self.params = {}
    
    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成交易信号
        
        Args:
            df: 包含OHLCV数据的DataFrame
            
        Returns:
            添加了 'signal' 列的DataFrame
        """
        pass
    
    @abstractmethod
    def get_params(self) -> Dict:
        """获取策略参数"""
        pass
    
    def set_params(self, **params):
        """设置策略参数"""
        self.params.update(params)
    
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算技术指标
        
        Args:
            df: 原始K线数据
            
        Returns:
            添加了技术指标的DataFrame
        """
        # 使用pandas_ta计算指标
        # RSI
        df['RSI'] = ta.rsi(df['close'], length=14)
        
        # MACD
        macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
        df['MACD'] = macd['MACD_12_26_9']
        df['MACD_signal'] = macd['MACDs_12_26_9']
        df['MACD_hist'] = macd['MACDh_12_26_9']
        
        # 移动平均线
        df['MA7'] = ta.sma(df['close'], length=7)
        df['MA20'] = ta.sma(df['close'], length=20)
        df['MA50'] = ta.sma(df['close'], length=50)
        df['MA200'] = ta.sma(df['close'], length=200)
        
        # 布林带 - 兼容新旧版本
        bbands = ta.bbands(df['close'], length=20, std=2)
        # 处理不同版本的列名
        bbands_cols = list(bbands.columns)
        if 'BBU_20_2.0' in bbands_cols:
            df['BB_upper'] = bbands['BBU_20_2.0']
            df['BB_middle'] = bbands['BBM_20_2.0']
            df['BB_lower'] = bbands['BBL_20_2.0']
        else:
            # 新版本格式
            df['BB_upper'] = bbands[bbands_cols[0]]
            df['BB_middle'] = bbands[bbands_cols[1]]
            df['BB_lower'] = bbands[bbands_cols[2]]
        
        # ATR (真實波幅)
        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        
        # OBV (能量潮)
        df['OBV'] = ta.obv(df['close'], df['volume'])
        
        # 成交量均线
        df['Volume_MA'] = ta.sma(df['volume'], length=20)
        
        return df
    
    def calculate_position_size(
        self, 
        account_balance: float, 
        risk_percent: float,
        stop_loss_pct: float
    ) -> float:
        """计算仓位大小
        
        Args:
            account_balance: 账户余额
            risk_percent: 风险比例 (如0.02 = 2%)
            stop_loss_p比例
            
        Returnsct: 止损:
            仓位大小 (数量)
        """
        risk_amount = account_balance * risk_percent
        position_size = risk_amount / stop_loss_pct
        return position_size


class TrendStrategy(BaseStrategy):
    """趋势策略 - 结合RSI、MACD、均线"""
    
    def __init__(self):
        super().__init__("TrendStrategy")
        
        # 默认参数 (最优参数 - RSI 28/72)
        self.params = {
            'rsi_period': 14,
            'rsi_oversold': 28,
            'rsi_overbought': 72,
            'macd_fast': 12,
            'macd_slow': 26,
            'macd_signal': 9,
            'ma_short': 20,
            'ma_long': 50,
            'trend_ma': 200,  # 大趋势用200日均线
        }
    
    def get_params(self) -> Dict:
        return self.params
    
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成交易信号
        
        买入条件:
        1. RSI从超卖回升 (>30)
        2. MACD金叉 (MACD线从下方穿过信号线)
        3. 价格站上20日均线
        4. 价格在200日均线上方 (大趋势向上)
        
        卖出条件:
        1. RSI从超买回落 (<70)
        2. MACD死叉 (MACD线从上方穿过信号线)
        3. 价格跌破20日均线
        4. 或者价格跌破200日均线 (大趋势向下)
        """
        # 先计算指标
        df = self.calculate_indicators(df)
        
        # 初始化信号列
        df['signal'] = Signal.NONE
        df['signal_reason'] = ''
        
        # 买入条件
        buy_condition = (
            # RSI从超卖回升
            (df['RSI'] > self.params['rsi_oversold']) & 
            (df['RSI'].shift(1) <= self.params['rsi_oversold']) &
            # MACD金叉
            (df['MACD'] > df['MACD_signal']) &
            (df['MACD'].shift(1) <= df['MACD_signal'].shift(1)) &
            # 价格在20日均线上方
            (df['close'] > df['MA20']) &
            # 大趋势向上 (价格在200日均线上方)
            (df['close'] > df['MA200'])
        )
        
        # 卖出条件
        sell_condition = (
            # RSI从超买回落
            (df['RSI'] < self.params['rsi_overbought']) & 
            (df['RSI'].shift(1) >= self.params['rsi_overbought']) |
            # MACD死叉
            (df['MACD'] < df['MACD_signal']) &
            (df['MACD'].shift(1) >= df['MACD_signal'].shift(1)) |
            # 价格跌破20日均线
            (df['close'] < df['MA20']) |
            # 大趋势向下 (价格跌破200日均线)
            (df['close'] < df['MA200'])
        )
        
        # 设置信号
        df.loc[buy_condition, 'signal'] = Signal.BUY
        df.loc[buy_condition, 'signal_reason'] = 'RSI+MACD+MA bullish'
        
        df.loc[sell_condition, 'signal'] = Signal.SELL
        df.loc[sell_condition, 'signal_reason'] = 'RSI+MACD+MA bearish'
        
        return df
    
    def get_signal_description(self, signal: int) -> str:
        """获取信号描述"""
        if signal == Signal.BUY:
            return "买入信号"
        elif signal == Signal.SELL:
            return "卖出信号"
        else:
            return "持有/观望"


# 简单趋势策略 - 只用RSI + MACD
class SimpleTrendStrategy(BaseStrategy):
    """简单趋势策略 - RSI + MACD"""
    
    def __init__(self):
        super().__init__("SimpleTrendStrategy")
        
        self.params = {
            'rsi_period': 14,
            'rsi_oversold': 35,  # 放宽一点
            'rsi_overbought': 65,
        }
    
    def get_params(self) -> Dict:
        return self.params
    
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成交易信号"""
        df = self.calculate_indicators(df)
        
        df['signal'] = Signal.NONE
        df['signal_reason'] = ''
        
        # 买入: RSI超卖 + MACD金叉
        buy = (
            (df['RSI'] < self.params['rsi_oversold']) &
            (df['MACD'] > df['MACD_signal'])
        )
        
        # 卖出: RSI超买 + MACD死叉
        sell = (
            (df['RSI'] > self.params['rsi_overbought']) &
            (df['MACD'] < df['MACD_signal'])
        )
        
        df.loc[buy, 'signal'] = Signal.BUY
        df.loc[buy, 'signal_reason'] = 'RSI oversold + MACD bullish'
        
        df.loc[sell, 'signal'] = Signal.SELL
        df.loc[sell, 'signal_reason'] = 'RSI overbought + MACD bearish'
        
        return df
