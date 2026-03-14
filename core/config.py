"""
配置管理模块
"""
import os
import yaml
from typing import Any, Dict, List, Optional
from pathlib import Path


class Config:
    """配置管理类"""
    
    def __init__(self, config_path: str = None):
        if config_path is None:
            # 默认配置文件路径
            project_root = Path(__file__).parent.parent
            config_path = project_root / "config" / "config.yaml"
        
        self.config_path = config_path
        self._config: Dict = {}
        self._load()
    
    def _load(self):
        """加载配置文件，支持 config.local.yaml 本地覆盖"""
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f) or {}
        else:
            template_path = str(self.config_path) + '.example'
            if os.path.exists(template_path):
                with open(template_path, 'r', encoding='utf-8') as f:
                    self._config = yaml.safe_load(f) or {}
            else:
                self._config = self._get_default()

        local_candidates = [
            Path(self.config_path).with_name('config.local.yaml'),
            Path.home() / '.crypto-quant-okx.local.yaml',
        ]
        for local_path in local_candidates:
            if os.path.exists(local_path):
                with open(local_path, 'r', encoding='utf-8') as f:
                    local_config = yaml.safe_load(f) or {}
                self._config = self._deep_merge(self._config, local_config)
    
    def _deep_merge(self, base: Dict, override: Dict) -> Dict:
        """深度合并配置"""
        result = dict(base)
        for key, value in (override or {}).items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _get_default(self) -> Dict:
        """获取默认配置"""
        return {
            'exchange': {'name': 'okx', 'mode': 'testnet'},
            'symbols': {
                'watch_list': ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'HYPE/USDT'],
                'quote_currency': 'USDT'
            },
            'trading': {
                'position_size': 0.1,
                'max_exposure': 0.3,
                'leverage': 10,
                'stop_loss': 0.02,
                'take_profit': 0.04
            },
            'strategies': {
                'rsi': {'enabled': True, 'period': 14, 'oversold': 35, 'overbought': 65},
                'macd': {'enabled': True, 'fast_period': 12, 'slow_period': 26, 'signal_period': 9},
                'ma_cross': {'enabled': True, 'fast_period': 5, 'slow_period': 20},
                'bollinger': {'enabled': True, 'period': 20, 'std_multiplier': 2}
            }
        }
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项 - 支持点号访问"""
        keys = key.split('.')
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value
    
    def set(self, key: str, value: Any):
        """设置配置项"""
        keys = key.split('.')
        config = self._config
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        config[keys[-1]] = value
    
    def save(self):
        """保存配置"""
        with open(self.config_path, 'w', encoding='utf-8') as f:
            yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)
    
    @property
    def all(self) -> Dict:
        """获取全部配置"""
        return self._config
    
    def reload(self):
        """重新加载配置"""
        self._load()
    
    # =========================================================================
    # 便捷访问方法
    # =========================================================================
    
    @property
    def symbols(self) -> List[str]:
        """获取监听的币种列表"""
        return self.get('symbols.watch_list', [])
    
    @property
    def exchange_mode(self) -> str:
        """获取交易模式"""
        return self.get('exchange.mode', 'testnet')
    
    @property
    def leverage(self) -> int:
        """获取杠杆倍数"""
        return self.get('trading.leverage', 10)
    
    @property
    def position_size(self) -> float:
        """获取单笔交易金额"""
        return self.get('trading.position_size', 0.1)
    
    @property
    def stop_loss(self) -> float:
        """获取止损比例"""
        return self.get('trading.stop_loss', 0.02)
    
    @property
    def take_profit(self) -> float:
        """获取止盈比例"""
        return self.get('trading.take_profit', 0.04)
    
    @property
    def strategies_config(self) -> Dict:
        """获取策略配置"""
        return self.get('strategies', {})
    
    @property
    def ml_enabled(self) -> bool:
        """获取ML是否启用"""
        return self.get('ml.enabled', False)
    
    @property
    def db_path(self) -> str:
        """获取数据库路径"""
        return self.get('database.path', 'data/trading.db')
    
    @property
    def dashboard_config(self) -> Dict:
        """获取仪表盘配置"""
        return self.get('dashboard', {})


# 全局配置实例
config = Config()
