"""
配置管理模块
"""
import os
import re
import yaml
from typing import Any, Dict, List, Optional
from pathlib import Path


_MISSING = object()


DEFAULT_LAYERING_CONFIG = {
    'layer_count': 3,
    'layer_ratios': [0.06, 0.06, 0.04],
    'layer_max_total_ratio': 0.16,
    'min_add_interval_seconds': 0,
    'profit_only_add': False,
    'disallow_skip_layers': True,
    'direction_lock_enabled': True,
    'direction_lock_scope': 'symbol_side',
    'direction_lock_release_on_flat': True,
    'signal_idempotency_enabled': True,
    'signal_idempotency_ttl_seconds': 3600,
    'max_layers_per_signal': 3,
    'allow_same_bar_multiple_adds': False,
}


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

        self._config = self._resolve_env_placeholders(self._config)
        self._normalize_legacy_layering_config()
        self._validate()

    def _resolve_env_placeholders(self, value: Any) -> Any:
        """递归解析 ${VAR} / ${VAR:-default} 环境变量占位符"""
        if isinstance(value, dict):
            return {k: self._resolve_env_placeholders(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_env_placeholders(item) for item in value]
        if not isinstance(value, str):
            return value

        pattern = re.compile(r'^\$\{([A-Z0-9_]+)(?::-(.*))?\}$')
        matched = pattern.match(value.strip())
        if not matched:
            return value

        env_key = matched.group(1)
        default_value = matched.group(2)
        env_value = os.getenv(env_key)
        if env_value not in (None, ''):
            return env_value
        if default_value is not None:
            return default_value
        return ''
    

    def _normalize_legacy_layering_config(self):
        trading = self._config.setdefault('trading', {})
        layering = trading.get('layering') or {}
        if not isinstance(layering, dict):
            raise ValueError('trading.layering 必须是对象')
        normalized = dict(DEFAULT_LAYERING_CONFIG)
        normalized.update(layering)
        if not layering.get('layer_count'):
            if isinstance(normalized.get('layer_ratios'), list) and normalized.get('layer_ratios'):
                normalized['layer_count'] = len(normalized['layer_ratios'])
        trading['layering'] = normalized

    def _validate(self):
        self._validate_layering_config()

    def _validate_layering_config(self):
        trading = self.get('trading', {}) or {}
        layering = trading.get('layering') or {}
        if not isinstance(layering, dict):
            raise ValueError('trading.layering 必须是对象')

        layer_count = int(layering.get('layer_count', DEFAULT_LAYERING_CONFIG['layer_count']) or 0)
        layer_ratios = layering.get('layer_ratios', DEFAULT_LAYERING_CONFIG['layer_ratios'])
        if not isinstance(layer_ratios, list) or not layer_ratios:
            raise ValueError('trading.layering.layer_ratios 必须是非空数组')
        try:
            layer_ratios = [float(x) for x in layer_ratios]
        except Exception as exc:
            raise ValueError('trading.layering.layer_ratios 必须全部为数字') from exc
        if any(x <= 0 for x in layer_ratios):
            raise ValueError('trading.layering.layer_ratios 必须全部 > 0')
        if layer_count <= 0:
            raise ValueError('trading.layering.layer_count 必须 > 0')
        if len(layer_ratios) != layer_count:
            raise ValueError('trading.layering.layer_count 必须与 layer_ratios 长度一致')
        max_total_ratio = float(layering.get('layer_max_total_ratio', DEFAULT_LAYERING_CONFIG['layer_max_total_ratio']) or 0)
        if max_total_ratio <= 0:
            raise ValueError('trading.layering.layer_max_total_ratio 必须 > 0')
        if sum(layer_ratios) - max_total_ratio > 1e-9:
            raise ValueError('trading.layering.layer_max_total_ratio 不能小于 layer_ratios 总和')
        min_add_interval = int(layering.get('min_add_interval_seconds', DEFAULT_LAYERING_CONFIG['min_add_interval_seconds']) or 0)
        if min_add_interval < 0:
            raise ValueError('trading.layering.min_add_interval_seconds 不能 < 0')
        ttl_seconds = int(layering.get('signal_idempotency_ttl_seconds', DEFAULT_LAYERING_CONFIG['signal_idempotency_ttl_seconds']) or 0)
        if ttl_seconds < 0:
            raise ValueError('trading.layering.signal_idempotency_ttl_seconds 不能 < 0')
        max_layers_per_signal = int(layering.get('max_layers_per_signal', layer_count) or 0)
        if max_layers_per_signal <= 0:
            raise ValueError('trading.layering.max_layers_per_signal 必须 > 0')
        if max_layers_per_signal > layer_count:
            raise ValueError('trading.layering.max_layers_per_signal 不能大于 layer_count')
        scope = str(layering.get('direction_lock_scope', DEFAULT_LAYERING_CONFIG['direction_lock_scope']) or '').strip()
        if scope not in {'symbol_side', 'symbol'}:
            raise ValueError('trading.layering.direction_lock_scope 仅支持 symbol_side / symbol')
        layering['layer_count'] = layer_count
        layering['layer_ratios'] = layer_ratios
        layering['layer_max_total_ratio'] = max_total_ratio
        layering['min_add_interval_seconds'] = min_add_interval
        layering['signal_idempotency_ttl_seconds'] = ttl_seconds
        layering['max_layers_per_signal'] = max_layers_per_signal
        layering['direction_lock_scope'] = scope

    def get_layering_config(self, symbol: str = None) -> Dict[str, Any]:
        trading = self.get_symbol_section(symbol, 'trading') if symbol else (self.get('trading', {}) or {})
        layering = dict(DEFAULT_LAYERING_CONFIG)
        layering.update((trading.get('layering') or {}))
        layering['layer_count'] = int(layering.get('layer_count') or len(layering.get('layer_ratios') or DEFAULT_LAYERING_CONFIG['layer_ratios']))
        layering['layer_ratios'] = [float(x) for x in (layering.get('layer_ratios') or DEFAULT_LAYERING_CONFIG['layer_ratios'])]
        layering['layer_max_total_ratio'] = float(layering.get('layer_max_total_ratio') or sum(layering['layer_ratios']) or DEFAULT_LAYERING_CONFIG['layer_max_total_ratio'])
        layering['min_add_interval_seconds'] = int(layering.get('min_add_interval_seconds') or 0)
        layering['signal_idempotency_ttl_seconds'] = int(layering.get('signal_idempotency_ttl_seconds') or 0)
        layering['max_layers_per_signal'] = int(layering.get('max_layers_per_signal') or layering['layer_count'])
        layering['direction_lock_scope'] = str(layering.get('direction_lock_scope') or 'symbol_side')
        for key in ('profit_only_add', 'disallow_skip_layers', 'direction_lock_enabled', 'direction_lock_release_on_flat', 'signal_idempotency_enabled', 'allow_same_bar_multiple_adds'):
            layering[key] = bool(layering.get(key, DEFAULT_LAYERING_CONFIG[key]))
        return layering

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
                'position_size': 0.08,
                'max_exposure': 0.3,
                'max_position_per_symbol': 0.12,
                'total_margin_cap_ratio': 0.30,
                'total_margin_soft_cap_ratio': 0.25,
                'symbol_margin_cap_ratio': 0.12,
                'base_entry_margin_ratio': 0.08,
                'min_entry_margin_ratio': 0.04,
                'max_entry_margin_ratio': 0.10,
                'add_position_enabled': False,
                'quality_scaling_enabled': False,
                'high_quality_multiplier': 1.15,
                'low_quality_multiplier': 0.75,
                'leverage': 10,
                'stop_loss': 0.02,
                'take_profit': 0.04,
                'partial_tp_enabled': False,
                'partial_tp_threshold': 0.015,
                'partial_tp_ratio': 0.5,
                # 第二止盈层（多级退出）- 默认可禁用
                'partial_tp2_enabled': False,
                'partial_tp2_threshold': 0.03,
                'partial_tp2_ratio': 0.3,
                'layering': dict(DEFAULT_LAYERING_CONFIG),
            },
            'strategies': {
                'rsi': {'enabled': True, 'period': 14, 'oversold': 35, 'overbought': 65},
                'macd': {'enabled': True, 'fast_period': 12, 'slow_period': 26, 'signal_period': 9},
                'ma_cross': {'enabled': True, 'fast_period': 5, 'slow_period': 20},
                'bollinger': {'enabled': True, 'period': 20, 'std_multiplier': 2}
            }
        }
    
    def _get_nested_value(self, data: Dict, key: str, default: Any = None) -> Any:
        keys = key.split('.') if key else []
        value = data
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k, _MISSING)
            else:
                return default
            if value is _MISSING:
                return default
        return default if value is None else value

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项 - 支持点号访问"""
        return self._get_nested_value(self._config, key, default)

    def get_symbol_overrides(self, symbol: str) -> Dict:
        """获取指定币种的局部覆盖配置"""
        overrides = self.get('symbol_overrides', {}) or {}
        return overrides.get(symbol, {}) or {}

    def get_symbol_value(self, symbol: str, key: str, default: Any = None) -> Any:
        """获取币种覆盖后的配置值；无覆盖则回退全局配置"""
        override_value = self._get_nested_value(self.get_symbol_overrides(symbol), key, _MISSING)
        if override_value is not _MISSING:
            return override_value
        return self.get(key, default)

    def get_symbol_section(self, symbol: str, key: str) -> Dict:
        """获取币种覆盖后的 section 配置（深度合并）"""
        base = self.get(key, {}) or {}
        override = self._get_nested_value(self.get_symbol_overrides(symbol), key, {}) or {}
        if isinstance(base, dict) and isinstance(override, dict):
            return self._deep_merge(base, override)
        return override or base
    
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
        return self.get('trading.leverage', 3)

    @property
    def position_mode(self) -> str:
        """获取持仓模式"""
        return self.get('exchange.position_mode', 'oneway')
    
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
