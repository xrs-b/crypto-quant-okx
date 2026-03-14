"""
机器学习模块 - 增强版
"""
import os
import pickle
import json
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path


class MLEngine:
    """机器学习引擎"""

    def __init__(self, config: Dict):
        self.config = config
        self.ml_config = config.get('ml', {})
        self.model_path = self.ml_config.get('model_path', 'ml/models')
        self.enabled = self.ml_config.get('enabled', True)
        self._ensure_model_dir()

    def _ensure_model_dir(self):
        Path(self.model_path).mkdir(parents=True, exist_ok=True)

    def predict(self, symbol: str, ohlcv_data: List) -> Optional[Tuple[int, float]]:
        if not self.enabled:
            return None
        model = self._load_model(symbol)
        if model is None:
            return None
        try:
            features = self._prepare_features(ohlcv_data)
            if features is None:
                return None
            prediction = model.predict(features)[0]
            prob = model.predict_proba(features)[0]
            return int(prediction), float(prob[1])
        except Exception as e:
            print(f"ML预测错误: {e}")
            return None

    def _symbol_to_name(self, symbol: str) -> str:
        symbol_map = {
            'BTC/USDT': 'BTC_USDT',
            'ETH/USDT': 'ETH_USDT',
            'SOL/USDT': 'SOL_USDT',
            'XRP/USDT': 'XRP_USDT',
            'HYPE/USDT': 'HYPE_USDT'
        }
        return symbol_map.get(symbol, symbol.replace('/', '_').replace(':', '_'))

    def _load_model(self, symbol: str) -> Optional[Any]:
        model_name = self._symbol_to_name(symbol)
        model_file = f"{self.model_path}/{model_name}_model.pkl"
        if not os.path.exists(model_file):
            return None
        try:
            with open(model_file, 'rb') as f:
                return pickle.load(f)
        except Exception as e:
            print(f"加载模型失败: {e}")
            return None

    def _calc_feature_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        features = pd.DataFrame(index=df.index)
        features['close'] = df['close']
        features['volume'] = df['volume']

        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(14).mean()
        avg_loss = loss.rolling(14).mean()
        rs = avg_gain / (avg_loss + 1e-10)
        features['RSI'] = 100 - (100 / (1 + rs))

        ema12 = df['close'].ewm(span=12).mean()
        ema26 = df['close'].ewm(span=26).mean()
        features['MACD'] = ema12 - ema26
        features['MACD_signal'] = features['MACD'].ewm(span=9).mean()
        features['MACD_hist'] = features['MACD'] - features['MACD_signal']

        bb_mid = df['close'].rolling(20).mean()
        bb_std = df['close'].rolling(20).std()
        features['BB_upper'] = bb_mid + 2 * bb_std
        features['BB_lower'] = bb_mid - 2 * bb_std
        features['BB_position'] = (df['close'] - features['BB_lower']) / (features['BB_upper'] - features['BB_lower'] + 1e-10)

        features['MA5'] = df['close'].rolling(5).mean()
        features['MA20'] = df['close'].rolling(20).mean()
        features['MA60'] = df['close'].rolling(60).mean()
        features['MA5_ratio'] = features['MA5'] / (features['MA20'] + 1e-10)
        features['trend_gap'] = (features['MA20'] - features['MA60']) / (features['MA60'] + 1e-10)

        features['volume_ma20'] = df['volume'].rolling(20).mean()
        features['volume_ratio'] = df['volume'] / (features['volume_ma20'] + 1e-10)

        features['returns'] = df['close'].pct_change()
        features['returns_3d'] = df['close'].pct_change(3)
        features['returns_5d'] = df['close'].pct_change(5)
        features['volatility'] = features['returns'].rolling(20).std()
        features['momentum'] = df['close'] / df['close'].shift(10) - 1

        prev_close = df['close'].shift(1)
        tr = pd.concat([
            (df['high'] - df['low']),
            (df['high'] - prev_close).abs(),
            (df['low'] - prev_close).abs(),
        ], axis=1).max(axis=1)
        features['ATR'] = tr.rolling(14).mean()
        features['atr_ratio'] = features['ATR'] / (df['close'] + 1e-10)
        return features

    def _prepare_features(self, ohlcv_data: List) -> Optional[pd.DataFrame]:
        try:
            df = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            features = self._calc_feature_frame(df)
            features = features.tail(1).fillna(0)
            feature_columns = self.ml_config.get('features', [
                'RSI', 'MACD', 'MACD_signal', 'MACD_hist',
                'BB_upper', 'BB_lower', 'BB_position',
                'MA5_ratio', 'trend_gap', 'volume_ratio', 'returns', 'returns_3d', 'returns_5d',
                'volatility', 'momentum', 'atr_ratio'
            ])
            return features[feature_columns]
        except Exception as e:
            print(f"特征准备错误: {e}")
            return None

    def get_feature_importance(self, symbol: str) -> Optional[Dict]:
        model = self._load_model(symbol)
        if model is None or not hasattr(model, 'feature_importances_'):
            return None
        feature_names = self.ml_config.get('features', [
            'RSI', 'MACD', 'MACD_signal', 'MACD_hist',
            'BB_upper', 'BB_lower', 'BB_position',
            'MA5_ratio', 'trend_gap', 'volume_ratio', 'returns', 'returns_3d', 'returns_5d',
            'volatility', 'momentum', 'atr_ratio'
        ])
        importances = model.feature_importances_
        return {name: float(imp) for name, imp in zip(feature_names, importances)}

    def get_model_metrics(self, symbol: str) -> Optional[Dict]:
        metrics_file = f"{self.model_path}/{self._symbol_to_name(symbol)}_metrics.json"
        if not os.path.exists(metrics_file):
            return None
        with open(metrics_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def get_all_model_metrics(self, symbols: Optional[List[str]] = None) -> List[Dict]:
        symbols = symbols or ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'HYPE/USDT']
        data = []
        for symbol in symbols:
            metrics = self.get_model_metrics(symbol)
            if metrics:
                data.append(metrics)
        return data


class ModelTrainer:
    """模型训练器"""

    def __init__(self, config: Dict):
        self.config = config
        self.ml_config = config.get('ml', {})
        self.model_path = self.ml_config.get('model_path', 'ml/models')
        Path(self.model_path).mkdir(parents=True, exist_ok=True)

    def _symbol_to_name(self, symbol: str) -> str:
        symbol_map = {
            'BTC/USDT': 'BTC_USDT',
            'ETH/USDT': 'ETH_USDT',
            'SOL/USDT': 'SOL_USDT',
            'XRP/USDT': 'XRP_USDT',
            'HYPE/USDT': 'HYPE_USDT'
        }
        return symbol_map.get(symbol, symbol.replace('/', '_').replace(':', '_'))

    def train(self, symbol: str, historical_data: pd.DataFrame) -> bool:
        try:
            df = self._prepare_training_data(historical_data).dropna()
            if len(df) < 120:
                print(f"数据量不足: {len(df)}")
                return False

            feature_columns = self.ml_config.get('features', [
                'RSI', 'MACD', 'MACD_signal', 'MACD_hist',
                'BB_upper', 'BB_lower', 'BB_position',
                'MA5_ratio', 'trend_gap', 'volume_ratio', 'returns', 'returns_3d', 'returns_5d',
                'volatility', 'momentum', 'atr_ratio'
            ])
            X = df[feature_columns]
            y = df['target']

            split_idx = int(len(X) * 0.8)
            X_train, X_test = X[:split_idx], X[split_idx:]
            y_train, y_test = y[:split_idx], y[split_idx:]

            from sklearn.ensemble import RandomForestClassifier
            from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score

            model = RandomForestClassifier(
                n_estimators=200,
                max_depth=8,
                min_samples_split=20,
                min_samples_leaf=8,
                class_weight='balanced_subsample',
                random_state=42,
                n_jobs=-1
            )
            model.fit(X_train, y_train)

            y_train_pred = model.predict(X_train)
            y_test_pred = model.predict(X_test)
            train_score = accuracy_score(y_train, y_train_pred)
            test_score = accuracy_score(y_test, y_test_pred)
            precision = precision_score(y_test, y_test_pred, zero_division=0)
            recall = recall_score(y_test, y_test_pred, zero_division=0)
            f1 = f1_score(y_test, y_test_pred, zero_division=0)

            print(f"{symbol} 训练完成:")
            print(f"  训练集准确率: {train_score:.2%}")
            print(f"  测试集准确率: {test_score:.2%}")
            print(f"  F1: {f1:.2%}")

            model_name = self._symbol_to_name(symbol)
            model_file = f"{self.model_path}/{model_name}_model.pkl"
            with open(model_file, 'wb') as f:
                pickle.dump(model, f)

            importances = sorted([
                {'feature': name, 'importance': round(float(imp), 6)}
                for name, imp in zip(feature_columns, model.feature_importances_)
            ], key=lambda x: x['importance'], reverse=True)

            training_cfg = self.ml_config.get('training', {})
            metrics = {
                'symbol': symbol,
                'model_file': model_file,
                'train_accuracy': round(float(train_score), 6),
                'test_accuracy': round(float(test_score), 6),
                'precision': round(float(precision), 6),
                'recall': round(float(recall), 6),
                'f1': round(float(f1), 6),
                'train_samples': int(len(X_train)),
                'test_samples': int(len(X_test)),
                'label_horizon': int(training_cfg.get('label_horizon', 3)),
                'min_return_threshold': float(training_cfg.get('min_return_threshold', 0.002)),
                'features': feature_columns,
                'top_features': importances[:8],
            }
            metrics_file = f"{self.model_path}/{model_name}_metrics.json"
            with open(metrics_file, 'w', encoding='utf-8') as f:
                json.dump(metrics, f, ensure_ascii=False, indent=2)

            print(f"  模型已保存: {model_file}")
            print(f"  指标已保存: {metrics_file}")
            return True
        except ImportError:
            print("需要安装scikit-learn: pip install scikit-learn")
            return False
        except Exception as e:
            print(f"训练错误: {e}")
            return False

    def _prepare_training_data(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        if 'timestamp' not in df.columns:
            df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume'][:len(df.columns)]
        horizon = int(self.ml_config.get('training', {}).get('label_horizon', 3))
        min_ret = float(self.ml_config.get('training', {}).get('min_return_threshold', 0.002))

        engine = MLEngine(self.config)
        features = engine._calc_feature_frame(df).copy()
        close_series = df['close'].astype(float)
        features['future_return'] = close_series.shift(-horizon) / close_series - 1
        features['target'] = (features['future_return'] > min_ret).astype(int)

        # 去掉过于中性的样本，减少纯噪音标签
        features = features[(features['future_return'] >= min_ret) | (features['future_return'] <= -min_ret)].copy()
        return features

    def auto_train_all(self, data_dir: str = 'ml/data') -> Dict[str, bool]:
        results = {}
        symbols = {
            'BTC/USDT': 'BTC_USDT',
            'ETH/USDT': 'ETH_USDT',
            'SOL/USDT': 'SOL_USDT',
            'XRP/USDT': 'XRP_USDT',
            'HYPE/USDT': 'HYPE_USDT'
        }
        for symbol, filename in symbols.items():
            csv_file = f"{data_dir}/{filename}_1h.csv"
            if not os.path.exists(csv_file):
                print(f"数据文件不存在: {csv_file}")
                results[symbol] = False
                continue
            try:
                df = pd.read_csv(csv_file)
                results[symbol] = self.train(symbol, df)
            except Exception as e:
                print(f"训练{symbol}失败: {e}")
                results[symbol] = False
        return results


class DataCollector:
    """数据收集器"""

    def __init__(self, exchange, config: Dict):
        self.exchange = exchange
        self.config = config
        self.data_dir = 'ml/data'
        self._ensure_dir()

    def _ensure_dir(self):
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)

    def collect_data(self, symbol: str, timeframe: str = '1h', limit: int = 1000) -> bool:
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            if not ohlcv:
                return False
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
            symbol_map = {
                'BTC/USDT': 'BTC_USDT',
                'ETH/USDT': 'ETH_USDT',
                'SOL/USDT': 'SOL_USDT',
                'XRP/USDT': 'XRP_USDT',
                'HYPE/USDT': 'HYPE_USDT'
            }
            filename = symbol_map.get(symbol, symbol.replace('/', '_'))
            filepath = f"{self.data_dir}/{filename}_{timeframe}.csv"
            df.to_csv(filepath, index=False)
            print(f"数据已保存: {filepath}")
            return True
        except Exception as e:
            print(f"数据收集错误: {e}")
            return False

    def collect_all(self, symbols: List[str], timeframe: str = '1h') -> Dict[str, bool]:
        results = {}
        for symbol in symbols:
            results[symbol] = self.collect_data(symbol, timeframe)
        return results
