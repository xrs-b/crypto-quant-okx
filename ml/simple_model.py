"""
简单的量化信号模型 - 不需要ML库
基于技术指标规则
"""
import pandas as pd
import os

def add_indicators(df):
    df = df.copy()
    close = df['close']
    
    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # MACD
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    df['MACD'] = ema12 - ema26
    df['MACD_signal'] = df['MACD'].ewm(span=9).mean()
    
    # 布林带
    df['BB_mid'] = close.rolling(20).mean()
    std = close.rolling(20).std()
    df['BB_upper'] = df['BB_mid'] + 2 * std
    df['BB_lower'] = df['BB_mid'] - 2 * std
    
    return df

def predict_signal(df):
    """基于规则预测信号"""
    latest = df.iloc[-1]
    
    rsi = latest['RSI']
    macd = latest['MACD']
    macd_s = latest['MACD_signal']
    close = latest['close']
    bb_upper = latest['BB_upper']
    bb_lower = latest['BB_lower']
    
    # 分数系统
    score = 0
    
    # RSI评分
    if rsi < 30:
        score += 2  # 超卖
    elif rsi < 40:
        score += 1
    elif rsi > 70:
        score -= 2  # 超买
    elif rsi > 60:
        score -= 1
    
    # MACD评分
    if macd > macd_s:
        score += 1  # 金叉
    else:
        score -= 1  # 死叉
    
    # 布林带评分
    if close < bb_lower:
        score += 1  # 接近下轨
    elif close > bb_upper:
        score -= 1  # 接近上轨
    
    # 信号
    if score >= 2:
        return 1, "买入", score
    elif score <= -2:
        return -1, "卖出", score
    else:
        return 0, "观望", score

def analyze(symbol):
    print(f"\n=== {symbol} 分析 ===")
    
    # 读取数据
    df = pd.read_csv(f'/Volumes/MacHD/Projects/crypto-quant-okx/ml/{symbol}_1h.csv')
    
    # 添加指标
    df = add_indicators(df)
    
    # 预测
    signal, text, score = predict_signal(df)
    
    latest = df.iloc[-1]
    
    print(f"RSI: {latest['RSI']:.1f}")
    print(f"MACD: {latest['MACD']:.2f}")
    print(f"布林带: {latest['close']:.2f} (上:{latest['BB_upper']:.2f} 下:{latest['BB_lower']:.2f})")
    print(f"信号分数: {score}")
    print(f"信号: {text}")
    
    return signal

if __name__ == '__main__':
    analyze('SOL_USDT')
    analyze('HYPE_USDT')
    print("\n✅ 分析完成!")
