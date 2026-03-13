"""
配置管理模块
"""
import os
import yaml
from typing import Any, Dict, Optional
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
        """加载配置文件"""
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f) or {}
        else:
            # 尝试加载模板
            template_path = str(self.config_path) + '.example'
            if os.path.exists(template_path):
                with open(template_path, 'r', encoding='utf-8') as f:
                    self._config = yaml.safe_load(f) or {}
            else:
                self._config = self._get_default()
    
    def _get_default(self) -> Dict:
        """获取默认配置"""
        return {
            'exchange': {
                'name': 'okx',
                'mode': 'testnet',
                'default_type': 'swap'
            },
            'symbols': {
                'list': ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'HYPE/USDT'],
                'configs': {
                    'BTC': {'enabled': True, 'weight': 0.3},
                    'ETH': {'enabled': True, 'weight': 0.3},
                    'SOL': {'enabled': True, 'weight': 0.2},
                    'XRP': {'enabled': True, 'weight': 0.1},
                    'HYPE': {'enabled': True, 'weight': 0.1}
                }
            },
            'position': {
                'total_limit': 0.3,
                'single_limit': 0.1,
                'leverage': 10,
                'min_balance': 1000
            },
            'risk': {
                'stop_loss': 0.02,
                'take_profit': 0.04,
                'trailing_stop': 0.02,
                'max_daily_loss': 0.10
            },
            'strategies': {
                'rsi': {'enabled': True, 'period': 14, 'oversold': 35, 'overbought': 65, 'weight': 0.2},
                'macd': {'enabled': True, 'fast': 12, 'slow': 26, 'signal': 9, 'weight': 0.2},
                'ma_cross': {'enabled': True, 'fast_period': 5, 'slow_period': 20, 'weight': 0.2},
                'bollinger': {'enabled': True, 'period': 20, 'std': 2, 'weight': 0.2},
                'composite': {'enabled': True, 'min_strength': 70, 'require_multi': True, 'weight': 0.2}
            },
            'filters': {
                'min_price_change': 0.02,
                'trend_confirmation': True,
                'multi_timeframe': False,
                'volume_confirm': False
            },
            'ml': {
                'enabled': True,
                'min_confidence': 0.65
            },
            'notifications': {
                'discord': {
                    'enabled': True,
                    'channel_id': ''
                }
            },
            'dashboard': {
                'host': '0.0.0.0',
                'port': 8080,
                'username': 'admin',
                'password': 'admin'
            }
        }
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
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


# 全局配置实例
config = Config()
