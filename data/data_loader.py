"""
数据加载器 - 使用CCXT获取交易所数据
支持现货和合约市场
"""

import ccxt
import pandas as pd
import yaml
from datetime import datetime, timedelta
from typing import Optional
import os


class DataLoader:
    """交易所数据加载器"""
    
    def __init__(self, config_path: str = None):
        """初始化数据加载器
        
        Args:
            config_path: 配置文件路径
        """
        self.config = self._load_config(config_path)
        self.exchange = self._init_exchange()
    
    def _load_config(self, config_path: str = None) -> dict:
        """加载配置文件"""
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(__file__), 
                '../config/config.yaml'
            )
        
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    
    def _init_exchange(self) -> ccxt.Exchange:
        """初始化交易所连接"""
        config = self.config['exchange']
        api_config = self.config.get('api', {})
        
        # 创建交易所实例
        exchange_class = getattr(ccxt, config['name'])
        
        exchange_params = {
            'enableRateLimit': True,
            'options': {
                'defaultType': config.get('default_type', 'spot')
            }
        }
        
        # 添加API密钥 (如果有)
        if api_config.get('key'):
            exchange_params['apiKey'] = api_config['key']
            exchange_params['secret'] = api_config['secret']
            if api_config.get('passphrase'):
                exchange_params['password'] = api_config['passphrase']
        
        # 测试网模式
        if config.get('testnet', False):
            exchange_params['urls'] = {
                'api': {
                    'public': 'https://testnet.binance.vision/api',
                    'private': 'https://testnet.binance.vision/api',
                }
            }
        
        return exchange_class(exchange_params)
    
    def fetch_ohlcv(
        self, 
        symbol: str = None, 
        timeframe: str = None,
        limit: int = 500,
        since: Optional[int] = None
    ) -> pd.DataFrame:
        """获取K线数据 (OHLCV)
        
        Args:
            symbol: 交易对，如 'BTC/USDT'
            timeframe: 时间周期，如 '1h', '4h', '1d'
            limit: 获取数量
            since: 起始时间戳 (毫秒)
            
        Returns:
            DataFrame with columns: [timestamp, open, high, low, close, volume]
        """
        symbol = symbol or self.config['exchange'].get('symbol', 'BTC/USDT')
        timeframe = timeframe or self.config['exchange'].get('interval', '1h')
        
        # 获取数据
        ohlcv = self.exchange.fetch_ohlcv(
            symbol, 
            timeframe, 
            limit=limit,
            since=since
        )
        
        # 转换为DataFrame
        df = pd.DataFrame(
            ohlcv, 
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
        )
        
        # 转换时间戳
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('datetime', inplace=True)
        
        return df
    
    def fetch_ohlcv_range(
        self,
        symbol: str = None,
        timeframe: str = None,
        start_date: str = None,
        end_date: str = None
    ) -> pd.DataFrame:
        """获取指定日期范围的K线数据
        
        Args:
            symbol: 交易对
            timeframe: 时间周期
            start_date: 开始日期 'YYYY-MM-DD'
            end_date: 结束日期 'YYYY-MM-DD'
            
        Returns:
            DataFrame
        """
        symbol = symbol or self.config['exchange'].get('symbol', 'BTC/USDT')
        timeframe = timeframe or self.config['exchange'].get('interval', '1h')
        
        # 时间周期对应的毫秒数
        timeframe_ms = {
            '1m': 60*1000,
            '5m': 5*60*1000,
            '15m': 15*60*1000,
            '1h': 60*60*1000,
            '4h': 4*60*60*1000,
            '1d': 24*60*60*1000,
        }
        
        # 计算需要的K线数量
        if start_date and end_date:
            start = int(datetime.strptime(start_date, '%Y-%m-%d').timestamp() * 1000)
            end = int(datetime.strptime(end_date, '%Y-%m-%d').timestamp() * 1000)
            duration = end - start
            interval_ms = timeframe_ms.get(timeframe, 60*60*1000)
            limit = int(duration / interval_ms) + 1
        else:
            start = None
            limit = 1000  # 默认获取数量
        
        # 分批获取数据 (如果数量太大)
        all_ohlcv = []
        current_start = start
        
        while True:
            batch = self.exchange.fetch_ohlcv(
                symbol,
                timeframe,
                limit=min(limit, 1000),
                since=current_start
            )
            
            if not batch:
                break
            
            all_ohlcv.extend(batch)
            
            # 检查是否获取完毕
            if len(batch) < 1000 or (end and batch[-1][0] >= end):
                break
            
            # 继续获取下一批
            current_start = batch[-1][0] + 1
            limit -= 1000
            
            if limit <= 0:
                break
        
        # 转换为DataFrame
        df = pd.DataFrame(
            all_ohlcv,
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
        )
        
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('datetime', inplace=True)
        
        # 筛选日期范围
        if start_date:
            start_dt = pd.to_datetime(start_date)
            df = df[df.index >= start_dt]
        
        if end_date:
            end_dt = pd.to_datetime(end_date) + pd.Timedelta(days=1)
            df = df[df.index < end_dt]
        
        return df
    
    def get_current_price(self, symbol: str = None) -> float:
        """获取当前价格"""
        symbol = symbol or self.config['exchange'].get('symbol', 'BTC/USDT')
        ticker = self.exchange.fetch_ticker(symbol)
        return ticker['last']
    
    def get_balance(self) -> dict:
        """获取账户余额"""
        if self.config['exchange'].get('default_type') == 'future':
            return self.exchange.fetch_balance({'type': 'future'})
        return self.exchange.fetch_balance()
    
    def get_order_book(self, symbol: str = None, limit: int = 20) -> dict:
        """获取订单簿"""
        symbol = symbol or self.config['exchange'].get('symbol', 'BTC/USDT')
        return self.exchange.fetch_order_book(symbol, limit)


# 测试
if __name__ == '__main__':
    loader = DataLoader()
    
    # 测试获取数据
    print("获取BTC/USDT 1小时K线...")
    df = loader.fetch_ohlcv('BTC/USDT', '1h', limit=100)
    print(f"获取到 {len(df)} 根K线")
    print(df.tail())
