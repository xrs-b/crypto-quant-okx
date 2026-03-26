"""
收集SOL/HYPE历史数据用于机器学习
"""
from pathlib import Path
import time

import ccxt
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ML_DIR = PROJECT_ROOT / 'ml'

# 加载配置
with open(PROJECT_ROOT / 'config/config.yaml', encoding='utf-8') as f:
    config = yaml.safe_load(f)

api_config = config.get('api', {})
exchange_config = config.get('exchange', {})
mode = exchange_config.get('mode', 'testnet')

ex = ccxt.okx({
    'apiKey': api_config.get('key', ''),
    'secret': api_config.get('secret', ''),
    'password': api_config.get('passphrase', ''),
    'enableRateLimit': True,
    'testnet': (mode == 'testnet'),
    'options': {'defaultType': 'swap'}
})

SYMBOLS = ['SOL/USDT', 'HYPE/USDT']
TIMEFRAMES = ['1h', '4h', '1d']


def fetch_ohlcv_safe(symbol, timeframe, limit=500):
    """安全获取K线数据"""
    for _ in range(3):
        try:
            data = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
            return data
        except Exception as e:
            print(f"Error fetching {symbol} {timeframe}: {e}")
            time.sleep(2)
    return []


def collect_all_data():
    """收集所有数据"""
    all_data = {}

    for symbol in SYMBOLS:
        print(f"\n=== 收集 {symbol} ===")
        symbol_data = {}

        for tf in TIMEFRAMES:
            print(f"  {tf}...", end=" ")
            data = fetch_ohlcv_safe(symbol, tf, 500)
            if data:
                df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                symbol_data[tf] = df
                print(f"OK ({len(df)} 条)")
            else:
                print("失败")
                symbol_data[tf] = None

        all_data[symbol] = symbol_data

    return all_data


if __name__ == '__main__':
    data = collect_all_data()

    # 保存数据
    for symbol, symbol_data in data.items():
        for tf, df in symbol_data.items():
            if df is not None:
                filename = ML_DIR / f"{symbol.replace('/', '_')}_{tf}.csv"
                df.to_csv(filename, index=False)
                print(f"保存: {filename}")

    print("\n✅ 数据收集完成!")
