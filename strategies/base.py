"""
策略基类
"""

from abc import ABC, abstractmethod
from typing import Dict
import pandas as pd


class Signal:
    """交易信号枚举"""
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
        """生成交易信号"""
        pass
    
    @abstractmethod
    def get_params(self) -> Dict:
        """获取策略参数"""
        pass
    
    def set_params(self, **params):
        """设置策略参数"""
        self.params.update(params)
