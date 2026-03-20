"""
MFE/MAE 止盈止损建议应用模块

将 MFE/MAE 分析结果应用于实际交易的止盈止损参数。
提供带安全回退的推荐值。
"""
from typing import Dict, Optional, Any
from datetime import datetime, timedelta
from analytics.mfe_mae import MFEAnalyzer
from core.logger import trade_logger


class RecommendationProvider:
    """MFE/MAE 建议提供者 - 带缓存和回退"""
    
    # 最小样本量要求
    MIN_SAMPLE_SIZE = 5
    
    # 建议缓存时间（避免每周期都重新计算）
    CACHE_TTL_MINUTES = 60
    
    def __init__(self, db=None, config=None):
        """
        初始化建议提供者
        
        Args:
            db: 数据库实例
            config: 配置实例（用于获取默认 SL/TP）
        """
        self.db = db
        self.config = config
        self.analyzer = MFEAnalyzer(db) if db else None
        
        # 缓存
        self._cache = {
            'global': {
                'recommendations': None,
                'cached_at': None,
            },
            'by_symbol': {}  # symbol -> {'recommendations': ..., 'cached_at': ...}
        }
        
        # 默认值（从配置或硬编码）
        self._default_stop_loss = 0.02  # 2%
        self._default_take_profit = 0.04  # 4%
        self._default_trailing_stop = 0.015  # 1.5%
        
        if config:
            self._default_stop_loss = config.get('trading.stop_loss', 0.02)
            self._default_take_profit = config.get('trading.take_profit', 0.04)
            self._default_trailing_stop = config.get('trading.trailing_stop', 0.015)
    
    def _is_cache_valid(self, cached_at: Optional[datetime]) -> bool:
        """检查缓存是否有效"""
        if cached_at is None:
            return False
        age = (datetime.now() - cached_at).total_seconds() / 60
        return age < self.CACHE_TTL_MINUTES
    
    def _get_analysis_report(self, force_refresh: bool = False) -> Dict:
        """
        获取 MFE/MAE 分析报告（带缓存）
        
        Returns:
            Dict: 分析报告，包含 recommendations
        """
        cache = self._cache['global']
        
        if not force_refresh and self._is_cache_valid(cache.get('cached_at')):
            return cache.get('recommendations', {})
        
        # 重新计算
        if self.analyzer:
            report = self.analyzer.generate_analysis_report()
        else:
            report = {'status': 'no_db', 'message': '无数据库实例'}
        
        # 更新缓存
        cache['recommendations'] = report
        cache['cached_at'] = datetime.now()
        
        return report
    
    def get_recommendations_for_symbol(self, symbol: str, force_refresh: bool = False) -> Dict:
        """
        获取指定币种的建议
        
        Args:
            symbol: 币种，如 'BTC/USDT'
            force_refresh: 强制刷新缓存
            
        Returns:
            Dict: {
                'stop_loss': float,
                'take_profit': float, 
                'trailing_stop': float,
                'trailing_activation': float,  # 追踪止损激活阈值
                'source': 'mfe_mae' | 'default',
                'sample_size': int,
                'is_fallback': bool,
                'details': {...}
            }
        """
        # 先尝试获取全局分析
        report = self._get_analysis_report(force_refresh)
        
        # 检查样本量
        analyzable_count = report.get('summary', {}).get('analyzable_count', 0)
        
        if analyzable_count < self.MIN_SAMPLE_SIZE:
            trade_logger.info(f"MFE/MAE 样本不足 ({analyzable_count}/{self.MIN_SAMPLE_SIZE})，使用默认参数")
            return self._fallback_recommendations(source='default', sample_size=analyzable_count)
        
        # 尝试获取币种特定建议
        by_symbol = report.get('by_symbol', {})
        symbol_data = by_symbol.get(symbol, {})
        
        if symbol_data and symbol_data.get('trade_count', 0) >= 3:
            # 币种有足够样本
            sl_rec = symbol_data.get('sl_recommendation', {})
            tp_rec = symbol_data.get('tp_recommendation', {})
            
            recommended_sl = sl_rec.get('recommended_sl_pct')
            recommended_tp = tp_rec.get('recommended_tp_pct')
            
            if recommended_sl is not None and recommended_tp is not None:
                return {
                    'stop_loss': recommended_sl / 100,  # 转换为小数
                    'take_profit': recommended_tp / 100,
                    'trailing_stop': self._default_trailing_stop,  # 暂时用默认值
                    'trailing_activation': None,
                    'source': 'mfe_mae_symbol',
                    'sample_size': symbol_data.get('trade_count', 0),
                    'is_fallback': False,
                    'details': {
                        'sl_recommendation': sl_rec,
                        'tp_recommendation': tp_rec,
                        'avg_mfe_pct': symbol_data.get('avg_mfe_pct'),
                        'avg_mae_pct': symbol_data.get('avg_mae_pct'),
                    }
                }
        
        # 使用全局建议
        sl_rec = report.get('stop_loss', {})
        tp_rec = report.get('take_profit', {})
        ts_rec = report.get('trailing_stop', {})
        
        recommended_sl = sl_rec.get('recommended_sl_pct')
        recommended_tp = tp_rec.get('recommended_tp_pct')
        ts_activation = ts_rec.get('activation_threshold_pct')
        ts_distance = ts_rec.get('trailing_distance_pct')
        
        if recommended_sl is not None and recommended_tp is not None:
            return {
                'stop_loss': recommended_sl / 100,  # 转换为小数
                'take_profit': recommended_tp / 100,
                'trailing_stop': ts_distance / 100 if ts_distance else self._default_trailing_stop,
                'trailing_activation': ts_activation / 100 if ts_activation else None,
                'source': 'mfe_mae_global',
                'sample_size': analyzable_count,
                'is_fallback': False,
                'details': {
                    'sl_recommendation': sl_rec,
                    'tp_recommendation': tp_rec,
                    'ts_suggestion': ts_rec,
                }
            }
        
        # 回退
        trade_logger.warning(f"MFE/MAE 建议计算失败，使用默认参数")
        return self._fallback_recommendations(source='default', sample_size=analyzable_count)
    
    def _fallback_recommendations(self, source: str, sample_size: int = 0) -> Dict:
        """返回默认建议"""
        return {
            'stop_loss': self._default_stop_loss,
            'take_profit': self._default_take_profit,
            'trailing_stop': self._default_trailing_stop,
            'trailing_activation': None,
            'source': source,
            'sample_size': sample_size,
            'is_fallback': True,
            'details': {
                'reason': '样本不足或计算失败' if sample_size < self.MIN_SAMPLE_SIZE else '计算返回空值'
            }
        }
    
    def get_stop_loss(self, symbol: str = None) -> float:
        """
        获取止损比例
        
        Args:
            symbol: 可选的币种特定查询
            
        Returns:
            float: 止损比例（小数，如 0.02 表示 2%）
        """
        if symbol:
            rec = self.get_recommendations_for_symbol(symbol)
            return rec.get('stop_loss', self._default_stop_loss)
        
        # 无币种参数时返回全局推荐或默认值
        report = self._get_analysis_report()
        analyzable = report.get('summary', {}).get('analyzable_count', 0)
        
        if analyzable >= self.MIN_SAMPLE_SIZE:
            sl_pct = report.get('stop_loss', {}).get('recommended_sl_pct')
            if sl_pct is not None:
                return sl_pct / 100
        
        return self._default_stop_loss
    
    def get_take_profit(self, symbol: str = None) -> float:
        """
        获取止盈比例
        
        Args:
            symbol: 可选的币种特定查询
            
        Returns:
            float: 止盈比例（小数，如 0.04 表示 4%）
        """
        if symbol:
            rec = self.get_recommendations_for_symbol(symbol)
            return rec.get('take_profit', self._default_take_profit)
        
        report = self._get_analysis_report()
        analyzable = report.get('summary', {}).get('analyzable_count', 0)
        
        if analyzable >= self.MIN_SAMPLE_SIZE:
            tp_pct = report.get('take_profit', {}).get('recommended_tp_pct')
            if tp_pct is not None:
                return tp_pct / 100
        
        return self._default_take_profit
    
    def get_trailing_stop(self, symbol: str = None) -> Dict:
        """
        获取追踪止损参数
        
        Args:
            symbol: 可选的币种特定查询
            
        Returns:
            Dict: {
                'distance': float,  # 追踪距离
                'activation': float,  # 激活阈值（可选）
            }
        """
        rec = self.get_recommendations_for_symbol(symbol) if symbol else {}
        
        return {
            'distance': rec.get('trailing_stop', self._default_trailing_stop),
            'activation': rec.get('trailing_activation'),
        }
    
    def get_all_recommendations(self, force_refresh: bool = False) -> Dict:
        """
        获取完整建议报告
        
        Args:
            force_refresh: 强制刷新缓存
            
        Returns:
            Dict: 完整的 MFE/MAE 分析和建议
        """
        report = self._get_analysis_report(force_refresh)
        
        # 添加当前默认值的对比
        report['_meta'] = {
            'defaults': {
                'stop_loss': self._default_stop_loss,
                'take_profit': self._default_take_profit,
                'trailing_stop': self._default_trailing_stop,
            },
            'min_sample_size': self.MIN_SAMPLE_SIZE,
            'cache_ttl_minutes': self.CACHE_TTL_MINUTES,
            'generated_at': datetime.now().isoformat(),
        }
        
        return report


# 便捷函数
def get_recommendation_provider(db=None, config=None) -> RecommendationProvider:
    """获取建议提供者实例"""
    return RecommendationProvider(db, config)
