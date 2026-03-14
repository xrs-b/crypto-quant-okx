"""
机器学习模块 - 增强版
"""
import os
import pickle
import json
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timedelta
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
        """确保模型目录存在"""
        Path(self.model_path).mkdir(parents=True, exist_ok=True)
    
    def predict(self, symbol: str, ohlcv_data: List) -> Optional[Tuple[int, float]]:
        """
        预测信号
        
        Args:
            symbol: 交易对 (e.g., 'BTC/USDT')
            ohlcv_data: K线数据
            
        Returns:
            (prediction, probability): 预测方向(1=涨,0=跌)和概率
        """
        if not self.enabled:
            return None
        
        # 获取模型
        model = self._load_model(symbol)
        if model is None:
            return None
        
        try:
            # 准备特征
            features = self._prepare_features(ohlcv_data)
            if features is None:
                return None
            
            # 预测
            prediction = model.predict(features)[0]
            prob = model.predict_proba(features)[0]
            
            # 返回预测和上涨概率
            return int(prediction), float(prob[1])
            
        except Exception as e:
            print(f"ML预测错误: {e}")
            return None
    
    def _load_model(self, symbol: str) -> Optional[Any]:
        """加载模型"""
        # 映射交易对
        symbol_map = {
            'BTC/USDT': 'BTC_USDT',
            'ETH/USDT': 'ETH_USDT',
            'SOL/USDT': 'SOL_USDT',
            'XRP/USDT': 'XRP_USDT',
            'HYPE/USDT': 'HYPE_USDT'
        }
        
        model_name = symbol_map.get(symbol, symbol.replace('/', '_'))
        model_file = f"{self.model_path}/{model_name}_model.pkl"
        
        if not os.path.exists(model_file):
            return None
        
        try:
            with open(model_file, 'rb') as f:
                return pickle.load(f)
        except Exception as e:
            print(f"加载模型失败: {e}")
            return None
    
    def _prepare_features(self, ohlcv_data: List) -> Optional[pd.DataFrame]:
        """准备特征"""
        try:
            df = pd.DataFrame(ohlcv_data)
            
            # 重命名列
            df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
            
            # 计算技术指标特征
            features = pd.DataFrame()
            
            # 基础特征
            features['close'] = df['close']
            features['volume'] = df['volume']
            
            # RSI
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rs = avg_gain / (avg_loss + 1e-10)
            features['RSI'] = 100 - (100 / (1 + rs))
            
            # MACD
            ema12 = df['close'].ewm(span=12).mean()
            ema26 = df['close'].ewm(span=26).mean()
            features['MACD'] = ema12 - ema26
            features['MACD_signal'] = features['MACD'].ewm(span=9).mean()
            features['MACD_hist'] = features['MACD'] - features['MACD_signal']
            
            # 布林带
            bb_mid = df['close'].rolling(20).mean()
            bb_std = df['close'].rolling(20).std()
            features['BB_upper'] = bb_mid + 2 * bb_std
            features['BB_lower'] = bb_mid - 2 * bb_std
            features['BB_position'] = (df['close'] - features['BB_lower']) / (features['BB_upper'] - features['BB_lower'] + 1e-10)
            
            # 均线
            features['MA5'] = df['close'].rolling(5).mean()
            features['MA20'] = df['close'].rolling(20).mean()
            features['MA60'] = df['close'].rolling(60).mean()
            features['MA5_ratio'] = features['MA5'] / (features['MA20'] + 1e-10)
            
            # 成交量特征
            features['volume_ma20'] = df['volume'].rolling(20).mean()
            features['volume_ratio'] = df['volume'] / (features['volume_ma20'] + 1e-10)
            
            # 收益率特征
            features['returns'] = df['close'].pct_change()
            features['returns_5d'] = df['close'].pct_change(5)
            features['volatility'] = features['returns'].rolling(20).std()
            
            # 动量
            features['momentum'] = df['close'] / df['close'].shift(10) - 1
            
            # 取最新一行
            features = features.tail(1)
            
            # 填充NaN
            features = features.fillna(0)
            
            # 选择特征列
            feature_columns = self.ml_config.get('features', [
                'RSI', 'MACD', 'MACD_signal', 'MACD_hist',
                'BB_upper', 'BB_lower', 'BB_position',
                'MA5_ratio', 'volume_ratio', 'returns', 'volatility', 'momentum'
            ])
            
            return features[feature_columns]
            
        except Exception as e:
            print(f"特征准备错误: {e}")
            return None
    
    def get_feature_importance(self, symbol: str) -> Optional[Dict]:
        """获取特征重要性"""
        model = self._load_model(symbol)
        if model is None or not hasattr(model, 'feature_importances_'):
            return None
        
        feature_names = self.ml_config.get('features', [
            'RSI', 'MACD', 'MACD_signal', 'MACD_hist',
            'BB_upper', 'BB_lower', 'BB_position',
            'MA5_ratio', 'volume_ratio', 'returns', 'volatility', 'momentum'
        ])
        
        importances = model.feature_importances_
        
        return {
            name: float(imp) 
            for name, imp in zip(feature_names, importances)
        }


class ModelTrainer:
    """模型训练器"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.ml_config = config.get('ml', {})
        self.model_path = self.ml_config.get('model_path', 'ml/models')
    
    def train(self, symbol: str, historical_data: pd.DataFrame) -> bool:
        """
        训练模型
        
        Args:
            symbol: 交易对
            historical_data: 历史K线数据
            
        Returns:
            是否成功
        """
        try:
            # 准备特征
            df = self._prepare_training_data(historical_data)
            
            # 删除NaN
            df = df.dropna()
            
            if len(df) < 100:
                print(f"数据量不足: {len(df)}")
                return False
            
            # 特征列
            feature_columns = self.ml_config.get('features', [
                'RSI', 'MACD', 'MACD_signal', 'MACD_hist',
                'BB_upper', 'BB_lower', 'BB_position',
                'MA5_ratio', 'volume_ratio', 'returns', 'volatility', 'momentum'
            ])
            
            X = df[feature_columns]
            y = df['target']
            
            # 分割数据
            split_idx = int(len(X) * 0.8)
            X_train, X_test = X[:split_idx], X[split_idx:]
            y_train, y_test = y[:split_idx], y[split_idx:]
            
            # 训练模型
            from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
            
            # 使用随机森林
            model = RandomForestClassifier(
                n_estimators=100,
                max_depth=10,
                min_samples_split=10,
                random_state=42,
                n_jobs=-1
            )
            
            model.fit(X_train, y_train)
            
            # 评估
            train_score = model.score(X_train, y_train)
            test_score = model.score(X_test, y_test)
            
            print(f"{symbol} 训练完成:")
            print(f"  训练集准确率: {train_score:.2%}")
            print(f"  测试集准确率: {test_score:.2%}")
            
            # 保存模型
            symbol_map = {
                'BTC/USDT': 'BTC_USDT',
                'ETH/USDT': 'ETH_USDT',
                'SOL/USDT': 'SOL_USDT',
                'XRP/USDT': 'XRP_USDT',
                'HYPE/USDT': 'HYPE_USDT'
            }
            
            model_name = symbol_map.get(symbol, symbol.replace('/', '_'))
            model_file = f"{self.model_path}/{model_name}_model.pkl"
            
            with open(model_file, 'wb') as f:
                pickle.dump(model, f)
            
            print(f"  模型已保存: {model_file}")
            
            return True
            
        except ImportError:
            print("需要安装scikit-learn: pip install scikit-learn")
            return False
        except Exception as e:
            print(f"训练错误: {e}")
            return False
    
    def _prepare_training_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """准备训练数据"""
        df = df.copy()
        
        # 计算技术指标
        # RSI
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(14).mean()
        avg_loss = loss.rolling(14).mean()
        rs = avg_gain / (avg_loss + 1e-10)
        df['RSI'] = 100 - (100 / (1 + rs))
        
        # MACD
        ema12 = df['close'].ewm(span=12).mean()
        ema26 = df['close'].ewm(span=26).mean()
        df['MACD'] = ema12 - ema26
        df['MACD_signal'] = df['MACD'].ewm(span=9).mean()
        df['MACD_hist'] = df['MACD'] - df['MACD_signal']
        
        # 布林带
        bb_mid = df['close'].rolling(20).mean()
        bb_std = df['close'].rolling(20).std()
        df['BB_upper'] = bb_mid + 2 * bb_std
        df['BB_lower'] = bb_mid - 2 * bb_std
        df['BB_position'] = (df['close'] - df['BB_lower']) / (df['BB_upper'] - df['BB_lower'] + 1e-10)
        
        # 均线
        df['MA5'] = df['close'].rolling(5).mean()
        df['MA20'] = df['close'].rolling(20).mean()
        df['MA5_ratio'] = df['MA5'] / (df['MA20'] + 1e-10)
        
        # 成交量
        df['volume_ma20'] = df['volume'].rolling(20).mean()
        df['volume_ratio'] = df['volume'] / (df['volume_ma20'] + 1e-10)
        
        # 收益率
        df['returns'] = df['close'].pct_change()
        df['volatility'] = df['returns'].rolling(20).std()
        
        # 动量
        df['momentum'] = df['close'] / df['close'].shift(10) - 1
        
        # 创建目标变量: 未来1小时是否上涨
        df['future_close'] = df['close'].shift(-1)
        df['target'] = (df['future_close'] > df['close']).astype(int)
        
        return df
    
    def auto_train_all(self, data_dir: str = 'ml/data') -> Dict[str, bool]:
        """自动训练所有币种模型"""
        results = {}
        
        # 币种列表
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
                success = self.train(symbol, df)
                results[symbol] = success
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
        """确保目录存在"""
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
    
    def collect_data(self, symbol: str, timeframe: str = '1h', limit: int = 1000) -> bool:
        """
        收集历史数据
        
        Args:
            symbol: 交易对
            timeframe: 时间框架
            limit: 获取数量
            
        Returns:
            是否成功
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            
            if not ohlcv:
                return False
            
            df = pd.DataFrame(ohlcv)
            df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
            
            # 添加时间列
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            # 映射文件名
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
        """收集所有币种数据"""
        results = {}
        
        for symbol in symbols:
            results[symbol] = self.collect_data(symbol, timeframe)
        
        return results
