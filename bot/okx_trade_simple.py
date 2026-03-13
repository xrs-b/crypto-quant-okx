#!/usr/bin/env python3
"""OKX合约量化交易机器人 - 简化版"""
import os, yaml, subprocess
from datetime import datetime
import ccxt
import pandas as pd
import joblib
import json

# 动态获取项目根目录
import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 从config.yaml加载配置
with open(f"{PROJECT_ROOT}/config/config.yaml") as f:
    config = yaml.safe_load(f)

# 加载所有参数
t = config.get('trading', {})
s = config.get('strategy', {})
TRADING_PAIRS = t.get('symbols', ['SOL-USDT-SWAP'])
RSI_PERIOD = s.get('rsi_period', 14)
RSI_OVERSOLD = s.get('rsi_oversold', 35)
RSI_OVERBOUGHT = s.get('rsi_overbought', 65)
POSITION_SIZE = t.get('position_size', 0.1)
MAX_EXPOSURE = t.get('max_exposure', 0.3)
LEVERAGE = t.get('leverage', 3)
STOP_LOSS_PCT = t.get('stop_loss', 0.02)
TAKE_PROFIT_PCT = t.get('take_profit', 0.04)
TRAILING_STOP_PCT = t.get('trailing_stop', 0.02)
MACD_FAST = s.get('macd_fast', 12)
MACD_SLOW = s.get('macd_slow', 26)
MACD_SIGNAL = s.get('macd_signal', 9)

POSITION_FILE = '/tmp/okx_trailing.json'

def load_config():
    with open(os.path.join(PROJECT_ROOT, 'config/config.yaml')) as f:
        return yaml.safe_load(f)

def send_discord(msg):
    try:
        cfg = load_config()
        ch = cfg.get('discord', {}).get('channel_id', '')
        subprocess.run(f'/opt/homebrew/bin/openclaw message send --channel discord --target "{ch}" --message "{msg}"', shell=True, capture_output=True, timeout=30)
    except:
        pass

def get_exchange():
    c = load_config()
    a = c.get('api', {})
    return ccxt.okx({
        'apiKey': a.get('key', ''),
        'secret': a.get('secret', ''),
        'password': a.get('passphrase', ''),
        'enableRateLimit': True,
        'timeout': 30000,
        'testnet': (config.get('mode', 'testnet') == 'testnet'),
    })

def add_features(df):
    close = df[4]
    delta = close.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(RSI_PERIOD).mean()
    avg_loss = loss.rolling(RSI_PERIOD).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))
    ema12 = close.ewm(span=MACD_FAST).mean()
    ema26 = close.ewm(span=MACD_SLOW).mean()
    df['MACD'] = ema12 - ema26
    df['MACD_signal'] = df['MACD'].ewm(span=MACD_SIGNAL).mean()
    return df

def get_traditional_signal(df):
    rsi = df['RSI'].iloc[-1]
    macd = df['MACD'].iloc[-1]
    macd_s = df['MACD_signal'].iloc[-1]
    if rsi < RSI_OVERSOLD or (macd > macd_s and rsi < 50):
        return 1
    elif rsi > RSI_OVERBOUGHT or (macd < macd_s and rsi > 50):
        return -1
    return 0

def get_positions(ex):
    pos = ex.fetch_positions()
    open_pos = {}
    for p in pos:
        c = float(p.get('contracts', 0) or 0)
        if c > 0:
            open_pos[p['symbol']] = {'contracts': c, 'side': p.get('side', 'long'), 'entry': float(p.get('entryPrice', 0) or 0)}
    return open_pos

def format_price(p):
    return str(round(p, 2)) + " USDT"

def main():
    print(f"OKX {datetime.now()}")
    ex = get_exchange()
    bal = ex.fetch_balance({'type': 'future'})
    usdt = bal['free'].get('USDT', 0)
    print(f"余额: {format_price(usdt)}")
    pos = get_positions(ex)
    print(f"持仓: {list(pos.keys()) if pos else '无'}")
    
    for pair in TRADING_PAIRS:
        print(f"\n=== {pair} ===")
        try:
            ohlcv = ex.fetch_ohlcv(pair, '1h', limit=50)
            df = pd.DataFrame(ohlcv)
            df = add_features(df)
            tk = ex.fetch_ticker(pair)
            price = float(tk['last'])
            sig = get_traditional_signal(df)
            rsi = df['RSI'].iloc[-1]
            print(f"价格: {format_price(price)} RSI:{rsi:.1f} 信号:{'买' if sig==1 else '卖' if sig==-1 else '观'}")
        except Exception as e:
            print(f"错误: {e}")

if __name__ == '__main__':
    main()
