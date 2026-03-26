"""
机器学习模型 - 预测涨跌
使用Random Forest + 技术指标
"""
from pathlib import Path
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
import joblib

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ML_DIR = PROJECT_ROOT / 'ml'


# 添加技术指标
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

    # 均线
    df['MA5'] = close.rolling(5).mean()
    df['MA20'] = close.rolling(20).mean()

    # 波动率
    df['VOLATILITY'] = std / df['BB_mid']

    return df


# 创建标签 (未来涨跌)
def create_labels(df, forward=4):
    df = df.copy()
    df['future_return'] = df['close'].shift(-forward) / df['close'] - 1

    # 标签: 1=涨, 0=跌
    df['label'] = (df['future_return'] > 0.01).astype(int)

    return df


# 训练模型
def train_model(symbol):
    print(f"\n=== 训练 {symbol} ===")

    # 读取数据
    df = pd.read_csv(ML_DIR / f'{symbol}_1h.csv')
    print(f"原始数据: {len(df)} 条")

    # 添加指标
    df = add_indicators(df)

    # 创建标签
    df = create_labels(df)

    # 删除无效数据
    df = df.dropna()
    print(f"有效数据: {len(df)} 条")

    # 特征
    features = ['RSI', 'MACD', 'MACD_signal', 'BB_upper', 'BB_lower', 'MA5', 'MA20', 'VOLATILITY']
    X = df[features]
    y = df['label']

    # 分割数据
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, shuffle=False)

    # 训练
    model = RandomForestClassifier(n_estimators=100, max_depth=10, random_state=42)
    model.fit(X_train, y_train)

    # 评估
    train_acc = model.score(X_train, y_train)
    test_acc = model.score(X_test, y_test)

    print(f"训练准确率: {train_acc:.2%}")
    print(f"测试准确率: {test_acc:.2%}")

    # 特征重要性
    importance = pd.DataFrame({
        'feature': features,
        'importance': model.feature_importances_
    }).sort_values('importance', ascending=False)
    print(f"\n特征重要性:")
    print(importance)

    # 保存模型
    model_path = ML_DIR / f'{symbol}_model.pkl'
    joblib.dump(model, model_path)
    print(f"\n模型保存: {model_path}")

    return model


if __name__ == '__main__':
    # 训练SOL和HYPE模型
    train_model('SOL_USDT')
    train_model('HYPE_USDT')
    print("\n✅ 训练完成!")
