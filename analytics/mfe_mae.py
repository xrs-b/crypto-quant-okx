"""
MFE (Maximum Favorable Excursion) / MAE (Maximum Adverse Excursion) 分析模块

MFE: 持仓期间达到的最大盈利（相对于入场价）
MAE: 持仓期间遭受的最大亏损（相对于入场价）

用于止盈止损优化诊断。
"""
from typing import Dict, List, Any, Optional
from datetime import datetime
import math


class MFEAnalyzer:
    """MFE/MAE 分析器"""
    
    def __init__(self, db=None):
        """
        初始化分析器
        
        Args:
            db: 数据库实例，如果为 None 则返回示例数据
        """
        self.db = db
        self._min_sample_size = 10  # 最小样本量
    
    def calculate_mfe_mae_from_positions(self, positions: List[Dict]) -> List[Dict]:
        """
        从持仓数据计算 MFE/MAE
        
        使用 peak_price 和 trough_price 来估算
        - long: peak = 最大盈利, trough = 最大亏损
        - short: trough = 最大盈利, peak = 最大亏损
        """
        results = []
        for pos in positions:
            side = pos.get('side', '').lower()
            entry = float(pos.get('entry_price', 0) or 0)
            peak = float(pos.get('peak_price') or 0)
            trough = float(pos.get('trough_price') or 0)
            coin_qty = float(pos.get('coin_quantity') or pos.get('quantity', 0) or 0)
            
            if entry <= 0 or coin_qty <= 0:
                continue
            
            if side == 'long':
                # 多头：peak 是最高价（潜在最大盈利），trough 是最低价（潜在最大亏损）
                mfe_pct = ((peak - entry) / entry * 100) if peak else 0
                mae_pct = ((entry - trough) / entry * 100) if trough else 0
            elif side == 'short':
                # 空头：trough 是最低价（潜在最大盈利），peak 是最高价（潜在最大亏损）
                mfe_pct = ((entry - trough) / entry * 100) if trough else 0
                mae_pct = ((peak - entry) / entry * 100) if peak else 0
            else:
                continue
            
            results.append({
                'symbol': pos.get('symbol'),
                'side': side,
                'entry_price': entry,
                'mfe_pct': round(mfe_pct, 2),
                'mae_pct': round(mae_pct, 2),
                'peak_price': peak,
                'trough_price': trough,
                'coin_quantity': coin_qty,
            })
        
        return results
    
    def calculate_mfe_mae_from_trades(self, trades: List[Dict]) -> List[Dict]:
        """
        从交易记录计算 MFE/MAE
        
        由于 closed trades 可能没有完整的持仓期间数据，
        这里使用 entry 和 exit 价格的简单分析
        """
        results = []
        for trade in trades:
            if trade.get('status') != 'closed':
                continue
            
            side = trade.get('side', '').lower()
            entry = float(trade.get('entry_price', 0) or 0)
            exit_price = float(trade.get('exit_price') or 0)
            coin_qty = float(trade.get('coin_quantity') or trade.get('quantity', 0) or 0)
            
            if entry <= 0 or coin_qty <= 0:
                continue
            
            # 如果没有 exit_price，跳过或标记为未知
            if exit_price <= 0:
                results.append({
                    'trade_id': trade.get('id'),
                    'symbol': trade.get('symbol'),
                    'side': side,
                    'entry_price': entry,
                    'exit_price': None,
                    'realized_pnl_pct': None,
                    'mfe_pct': None,
                    'mae_pct': None,
                    'note': 'exit_price 缺失，无法计算',
                })
                continue
            
            if side == 'long':
                realized_pct = ((exit_price - entry) / entry * 100)
            elif side == 'short':
                realized_pct = ((entry - exit_price) / entry * 100)
            else:
                continue
            
            results.append({
                'trade_id': trade.get('id'),
                'symbol': trade.get('symbol'),
                'side': side,
                'entry_price': entry,
                'exit_price': exit_price,
                'realized_pnl_pct': round(realized_pct, 2),
                'mfe_pct': None,  # 需要持仓期间数据
                'mae_pct': None,  # 需要持仓期间数据
                'note': '仅计算了实现盈亏，需要持仓数据计算 MFE/MAE',
            })
        
        return results
    
    def get_stop_loss_recommendation(self, mae_pcts: List[float]) -> Dict:
        """
        基于 MAE 分布推荐止损设置
        
        Args:
            mae_pcts: MAE 百分比列表
        """
        if not mae_pcts or len(mae_pcts) < 3:
            return {
                'recommended_sl_pct': None,
                'reason': '样本不足，无法推荐',
                'sample_size': len(mae_pcts) if mae_pcts else 0,
                'min_required': 3,
            }
        
        # 计算分位数
        sorted_mae = sorted(mae_pcts)
        n = len(sorted_mae)
        
        # 推荐止损设置在 75% 分位数（能保护 75% 的交易不被止损）
        p75_idx = int(n * 0.75)
        p75_mae = sorted_mae[min(p75_idx, n-1)]
        
        # 推荐在 90% 分位数（更保守）
        p90_idx = int(n * 0.90)
        p90_mae = sorted_mae[min(p90_idx, n-1)]
        
        return {
            'recommended_sl_pct': round(p75_mae, 2),
            'conservative_sl_pct': round(p90_mae, 2),
            'reason': '基于 MAE 分布的 75%/90% 分位数',
            'sample_size': n,
            'mae_distribution': {
                'min': round(min(mae_pcts), 2),
                'max': round(max(mae_pcts), 2),
                'avg': round(sum(mae_pcts) / n, 2),
                'median': round(sorted_mae[n//2], 2),
            }
        }
    
    def get_take_profit_recommendation(self, mfe_pcts: List[float]) -> Dict:
        """
        基于 MFE 分布推荐止盈设置
        
        Args:
            mfe_pcts: MFE 百分比列表
        """
        if not mfe_pcts or len(mfe_pcts) < 3:
            return {
                'recommended_tp_pct': None,
                'reason': '样本不足，无法推荐',
                'sample_size': len(mfe_pcts) if mfe_pcts else 0,
                'min_required': 3,
            }
        
        sorted_mfe = sorted(mfe_pcts)
        n = len(sorted_mfe)
        
        # 推荐止盈设置在 50% 分位数（实现 50% 的潜在盈利）
        p50_idx = int(n * 0.50)
        p50_mfe = sorted_mfe[min(p50_idx, n-1)]
        
        # 激进：75% 分位数
        p75_idx = int(n * 0.75)
        p75_mfe = sorted_mfe[min(p75_idx, n-1)]
        
        return {
            'recommended_tp_pct': round(p50_mfe, 2),
            'aggressive_tp_pct': round(p75_mfe, 2),
            'reason': '基于 MFE 分布的 50%/75% 分位数',
            'sample_size': n,
            'mfe_distribution': {
                'min': round(min(mfe_pcts), 2),
                'max': round(max(mfe_pcts), 2),
                'avg': round(sum(mfe_pcts) / n, 2),
                'median': round(sorted_mfe[n//2], 2),
            }
        }
    
    def get_trailing_stop_suggestion(self, mfe_pcts: List[float], mae_pcts: List[float]) -> Dict:
        """
        推荐追踪止损设置
        
        基于 MFE 峰值来动态调整止损
        """
        if not mfe_pcts or not mae_pcts:
            return {
                'activation_threshold_pct': None,
                'trailing_distance_pct': None,
                'reason': '样本不足',
            }
        
        # 激活阈值：建议使用平均 MFE 的 50%
        avg_mfe = sum(mfe_pcts) / len(mfe_pcts)
        activation = avg_mfe * 0.5
        
        # 追踪距离：建议使用平均 MAE
        avg_mae = sum(mae_pcts) / len(mae_pcts)
        
        return {
            'activation_threshold_pct': round(activation, 2),
            'trailing_distance_pct': round(avg_mae, 2),
            'reason': f'激活阈值=50%平均MFE({avg_mfe:.1f}%), 追踪距离=平均MAE({avg_mae:.1f}%)',
            'sample_size': min(len(mfe_pcts), len(mae_pcts)),
        }
    
    def analyze_by_symbol(self, trades: List[Dict], positions: List[Dict]) -> Dict[str, Dict]:
        """
        按币种聚合分析
        """
        symbol_data = {}
        
        # 处理 positions（计算 MFE/MAE）
        for pos in positions:
            symbol = pos.get('symbol')
            if symbol not in symbol_data:
                symbol_data[symbol] = {
                    'trade_count': 0,
                    'mfe_pcts': [],
                    'mae_pcts': [],
                    'realized_pnls': [],
                }
            
            side = pos.get('side', '').lower()
            entry = float(pos.get('entry_price', 0) or 0)
            peak = float(pos.get('peak_price') or 0)
            trough = float(pos.get('trough_price') or 0)
            
            if entry > 0:
                if side == 'long':
                    mfe = ((peak - entry) / entry * 100) if peak else 0
                    mae = ((entry - trough) / entry * 100) if trough else 0
                elif side == 'short':
                    mfe = ((entry - trough) / entry * 100) if trough else 0
                    mae = ((peak - entry) / entry * 100) if peak else 0
                else:
                    mfe, mae = 0, 0
                
                symbol_data[symbol]['mfe_pcts'].append(mfe)
                symbol_data[symbol]['mae_pcts'].append(mae)
        
        # 处理 trades（计算实现盈亏）
        for trade in trades:
            if trade.get('status') != 'closed':
                continue
            
            symbol = trade.get('symbol')
            if symbol not in symbol_data:
                symbol_data[symbol] = {
                    'trade_count': 0,
                    'mfe_pcts': [],
                    'mae_pcts': [],
                    'realized_pnls': [],
                }
            
            pnl_pct = trade.get('pnl_percent')
            if pnl_pct is not None:
                symbol_data[symbol]['realized_pnls'].append(float(pnl_pct))
            
            symbol_data[symbol]['trade_count'] += 1
        
        # 汇总结果
        results = {}
        for symbol, data in symbol_data.items():
            mfe_pcts = data['mfe_pcts']
            mae_pcts = data['mae_pcts']
            realized = data['realized_pnls']
            
            results[symbol] = {
                'trade_count': data['trade_count'],
                'avg_mfe_pct': round(sum(mfe_pcts) / len(mfe_pcts), 2) if mfe_pcts else None,
                'avg_mae_pct': round(sum(mae_pcts) / len(mae_pcts), 2) if mae_pcts else None,
                'avg_realized_pnl_pct': round(sum(realized) / len(realized), 2) if realized else None,
                'best_mfe_pct': round(max(mfe_pcts), 2) if mfe_pcts else None,
                'worst_mae_pct': round(max(mae_pcts), 2) if mae_pcts else None,
                'sl_recommendation': self.get_stop_loss_recommendation(mae_pcts) if mae_pcts else {},
                'tp_recommendation': self.get_take_profit_recommendation(mfe_pcts) if mfe_pcts else {},
            }
        
        return results
    
    def generate_analysis_report(self) -> Dict:
        """
        生成完整的 MFE/MAE 分析报告
        """
        if self.db is None:
            # 返回示例数据
            return self._get_sample_report()
        
        # 获取数据
        trades = self.db.get_trades(status='closed', limit=500)
        positions = self.db.get_positions()
        
        closed_count = len(trades)
        position_count = len(positions)
        
        # 样本量检查
        total_analyzable = position_count + sum(1 for t in trades if t.get('exit_price'))
        
        if total_analyzable < self._min_sample_size:
            return {
                'status': 'insufficient_data',
                'message': f'样本量不足: 仅 {total_analyzable} 笔可分析交易 (需要 {self._min_sample_size}+)',
                'closed_trades': closed_count,
                'open_positions': position_count,
                'analyzable_count': total_analyzable,
                'sample_report': self._get_sample_report(),
                'recommendation': '建议积累更多交易数据后再进行 MFE/MAE 分析',
            }
        
        # 计算 MFE/MAE
        position_mfe_mae = self.calculate_mfe_mae_from_positions(positions)
        trade_mfe_mae = self.calculate_mfe_mae_from_trades(trades)
        
        # 收集所有 MAE/MFE 百分比
        all_mae_pcts = [r['mae_pct'] for r in position_mfe_mae if r.get('mae_pct') is not None]
        all_mfe_pcts = [r['mfe_pct'] for r in position_mfe_mae if r.get('mfe_pct') is not None]
        
        # 止盈止损建议
        sl_recommendation = self.get_stop_loss_recommendation(all_mae_pcts)
        tp_recommendation = self.get_take_profit_recommendation(all_mfe_pcts)
        ts_suggestion = self.get_trailing_stop_suggestion(all_mfe_pcts, all_mae_pcts)
        
        # 按币种分析
        by_symbol = self.analyze_by_symbol(trades, positions)
        
        return {
            'status': 'success',
            'summary': {
                'closed_trades': closed_count,
                'open_positions': position_count,
                'analyzable_count': total_analyzable,
            },
            'overall': {
                'avg_mfe_pct': round(sum(all_mfe_pcts) / len(all_mfe_pcts), 2) if all_mfe_pcts else None,
                'avg_mae_pct': round(sum(all_mae_pcts) / len(all_mae_pcts), 2) if all_mae_pcts else None,
                'max_mfe_pct': round(max(all_mfe_pcts), 2) if all_mfe_pcts else None,
                'max_mae_pct': round(max(all_mae_pcts), 2) if all_mae_pcts else None,
            },
            'stop_loss': sl_recommendation,
            'take_profit': tp_recommendation,
            'trailing_stop': ts_suggestion,
            'by_symbol': by_symbol,
            'trade_details': position_mfe_mae + trade_mfe_mae,
        }
    
    def _get_sample_report(self) -> Dict:
        """返回示例报告，用于演示或样本不足时"""
        return {
            'status': 'sample',
            'message': '示例数据 - 用于演示功能',
            'summary': {
                'closed_trades': 0,
                'open_positions': 0,
                'analyzable_count': 0,
            },
            'overall': {
                'avg_mfe_pct': 5.2,
                'avg_mae_pct': -2.8,
                'max_mfe_pct': 12.5,
                'max_mae_pct': -8.3,
            },
            'stop_loss': {
                'recommended_sl_pct': 3.5,
                'conservative_sl_pct': 5.0,
                'reason': '基于 MAE 分布的 75%/90% 分位数（示例）',
                'sample_size': 0,
                'mae_distribution': {
                    'min': -8.3,
                    'max': -1.2,
                    'avg': -2.8,
                    'median': -2.5,
                }
            },
            'take_profit': {
                'recommended_tp_pct': 4.0,
                'aggressive_tp_pct': 8.0,
                'reason': '基于 MFE 分布的 50%/75% 分位数（示例）',
                'sample_size': 0,
                'mfe_distribution': {
                    'min': 1.5,
                    'max': 12.5,
                    'avg': 5.2,
                    'median': 4.8,
                }
            },
            'trailing_stop': {
                'activation_threshold_pct': 2.6,
                'trailing_distance_pct': 2.8,
                'reason': '激活阈值=50%平均MFE, 追踪距离=平均MAE（示例）',
                'sample_size': 0,
            },
            'by_symbol': {
                'BTC/USDT': {
                    'trade_count': 0,
                    'avg_mfe_pct': 6.0,
                    'avg_mae_pct': -2.5,
                    'avg_realized_pnl_pct': 1.8,
                    'best_mfe_pct': 12.5,
                    'worst_mae_pct': -8.3,
                    'sl_recommendation': {'recommended_sl_pct': 3.0},
                    'tp_recommendation': {'recommended_tp_pct': 5.0},
                }
            },
            'trade_details': [],
            'note': '当积累足够交易数据后，此示例将被真实数据替换'
        }


# 便捷函数
def get_mfe_mae_analysis(db=None) -> Dict:
    """获取 MFE/MAE 分析报告"""
    analyzer = MFEAnalyzer(db)
    return analyzer.generate_analysis_report()
