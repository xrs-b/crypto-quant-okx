"""
信号验证与记录模块 - 增强版
"""
import json
from typing import Dict, List
from datetime import datetime, timedelta
from core.config import Config
from core.exchange import Exchange


class SignalValidator:
    """信号验证器 - 过滤不符合条件的信号"""

    FILTER_META = {
        'NO_DIRECTION': {'group': 'signal', 'action_hint': '先观察方向分数与触发策略，当前未形成可执行方向'},
        'EXISTING_SAME_SIDE_POSITION': {'group': 'position', 'action_hint': '已有同向持仓，优先等平仓或切换币种'},
        'COOLDOWN_ACTIVE': {'group': 'risk', 'action_hint': '冷却期未结束，等剩余时间归零后再观察'},
        'LOW_BALANCE': {'group': 'risk', 'action_hint': '先补足可用余额，或者下调仓位比例'},
        'MAX_EXPOSURE': {'group': 'risk', 'action_hint': '总风险占用已高，建议降低 position_size 或等待仓位释放'},
        'MAX_SYMBOL_EXPOSURE': {'group': 'risk', 'action_hint': '单币种占用过高，避免继续集中在同一币种'},
        'LOW_VOLATILITY': {'group': 'market', 'action_hint': '市场太平静，当前更适合继续观望，不建议强行入场'},
        'HIGH_VOLATILITY': {'group': 'market', 'action_hint': '市场过于剧烈，先等待波动回落再评估'},
        'COUNTER_TREND': {'group': 'market', 'action_hint': '当前方向逆大趋势，除非策略明确允许，否则继续观望'},
        'INSUFFICIENT_STRATEGY_COUNT': {'group': 'signal', 'action_hint': '触发策略太少，可继续观察是否补齐确认条件'},
        'WEAK_SIGNAL_STRENGTH': {'group': 'signal', 'action_hint': '信号强度不足，建议继续等确认而非急于开仓'},
        # Regime Layer v1 - 保守接入
        'REGIME_RISK_ANOMALY': {'group': 'regime', 'action_hint': '检测到风险异常，市场可能剧烈波动，建议回避'},
        'REGIME_HIGH_VOL': {'group': 'regime', 'action_hint': '当前波动率较高，需降低仓位或观望'},
        'REGIME_LOW_VOL': {'group': 'regime', 'action_hint': '市场波动过低，趋势不明确，建议继续观察'},
    }

    def __init__(self, config: Config, exchange: Exchange):
        self.config = config
        self.exchange = exchange
        self.trading_config = config.get('trading', {})
        self.strategies_config = config.get('strategies', {})

    def _cfg(self, symbol: str, key: str, default=None):
        if hasattr(self.config, 'get_symbol_value'):
            return self.config.get_symbol_value(symbol, key, default)
        return self.config.get(key, default)

    def _cfg_section(self, symbol: str, key: str) -> Dict:
        if hasattr(self.config, 'get_symbol_section'):
            return self.config.get_symbol_section(symbol, key)
        return self.config.get(key, {}) or {}

    def _failure(self, code: str, reason: str, details: Dict, detail_key: str = None) -> tuple:
        meta = self.FILTER_META.get(code, {})
        if detail_key and detail_key in details and isinstance(details[detail_key], dict):
            details[detail_key]['code'] = code
            details[detail_key]['group'] = meta.get('group', 'other')
            details[detail_key]['action_hint'] = meta.get('action_hint', '')
        details['filter_meta'] = {
            'code': code,
            'group': meta.get('group', 'other'),
            'action_hint': meta.get('action_hint', ''),
        }
        return False, reason, details

    def validate(self, signal, current_positions: Dict = None,
                 tracking_data: Dict = None) -> tuple:
        """验证信号，返回 (passed, reason, details)"""
        current_positions = current_positions or {}
        tracking_data = tracking_data or {}
        details = {}

        # 0. 先过滤非方向性信号
        if signal.signal_type not in ['buy', 'sell']:
            details['direction_check'] = {'passed': False, 'reason': '无可执行方向'}
            return self._failure('NO_DIRECTION', '无可执行方向', details, 'direction_check')

        side = 'long' if signal.signal_type == 'buy' else 'short'

        # 1. 已有同方向持仓
        existing = current_positions.get(signal.symbol)
        if existing and existing.get('side') == side:
            details['position_check'] = {
                'passed': False,
                'reason': f"已有相同方向持仓: {existing.get('side')}",
                'existing_position': existing
            }
            return self._failure('EXISTING_SAME_SIDE_POSITION', f"已有相同方向持仓: {existing.get('side')}", details, 'position_check')
        details['position_check'] = {'passed': True, 'reason': '无冲突持仓'}

        # 2. 冷却时间
        cooldown = self._cfg(signal.symbol, 'trading.cooldown_minutes', 15)
        if tracking_data.get(signal.symbol):
            last_trade = tracking_data[signal.symbol].get('last_trade_time')
            if last_trade:
                last_time = datetime.fromisoformat(last_trade)
                diff_minutes = (datetime.now() - last_time).total_seconds() / 60
                if diff_minutes < cooldown:
                    details['cooldown_check'] = {
                        'passed': False,
                        'reason': f"冷却期内({diff_minutes:.1f}分钟<{cooldown}分钟)",
                        'remaining_minutes': round(cooldown - diff_minutes, 1)
                    }
                    return self._failure('COOLDOWN_ACTIVE', f"冷却期内({diff_minutes:.1f}分钟)", details, 'cooldown_check')
        details['cooldown_check'] = {'passed': True, 'reason': '冷却时间已过'}

        # 3. 资金与风险占比（用比例，不再错误地用绝对市值对 0.3 比较）
        position_ratio = float(self._cfg(signal.symbol, 'trading.position_size', 0.1))
        max_exposure = float(self._cfg(signal.symbol, 'trading.max_exposure', 0.3))
        max_per_symbol = float(self._cfg(signal.symbol, 'trading.max_position_per_symbol', 0.15))

        available_usdt = None
        current_exposure_ratio = 0.0
        symbol_exposure_ratio = 0.0

        if self.exchange:
            balance_info = self.exchange.fetch_balance()
            free = balance_info.get('free', {})
            total = balance_info.get('total', {})
            available_usdt = float(free.get('USDT', 0) or 0)
            total_usdt = float(total.get('USDT', available_usdt) or available_usdt or 1)

            for _, pos in current_positions.items():
                entry = float(pos.get('entry_price', 0) or 0)
                qty = float(pos.get('coin_quantity', pos.get('quantity', 0)) or 0)
                lev = max(1, int(pos.get('leverage', 1) or 1))
                margin_used = (entry * qty) / lev if entry and qty else 0
                ratio = margin_used / total_usdt if total_usdt > 0 else 0
                current_exposure_ratio += ratio
                if pos.get('symbol') == signal.symbol:
                    symbol_exposure_ratio += ratio

            if available_usdt < 100:
                details['balance_check'] = {
                    'passed': False,
                    'reason': f"余额不足({available_usdt:.2f} USDT)",
                    'available': available_usdt
                }
                return self._failure('LOW_BALANCE', '余额不足', details, 'balance_check')

            details['balance_check'] = {
                'passed': True,
                'reason': '余额充足',
                'available': round(available_usdt, 2)
            }
        else:
            details['balance_check'] = {'passed': True, 'reason': '跳过(无exchange)'}

        new_total_exposure = current_exposure_ratio + position_ratio
        # 获取杠杆信息用于日志
        configured_leverage = int(self.trading_config.get('leverage', 10))
        effective_leverage = configured_leverage
        if self.exchange and hasattr(self.exchange, 'get_actual_leverage'):
            try:
                effective_leverage = self.exchange.get_actual_leverage(signal.symbol)
            except Exception:
                pass
        
        if new_total_exposure > max_exposure:
            details['exposure_check'] = {
                'passed': False,
                'reason': f"超过最大持仓比例({new_total_exposure:.2f}>{max_exposure:.2f})",
                'current_exposure': round(current_exposure_ratio, 4),
                'new_position_ratio': position_ratio,
                'max_exposure': max_exposure,
                'planned_leverage': configured_leverage,
                'effective_leverage': effective_leverage
            }
            return self._failure('MAX_EXPOSURE', '超过最大持仓比例', details, 'exposure_check')
        details['exposure_check'] = {
            'passed': True,
            'reason': '总风险占用正常',
            'current_exposure': round(current_exposure_ratio, 4),
            'after_open': round(new_total_exposure, 4),
            'planned_leverage': configured_leverage,
            'effective_leverage': effective_leverage
        }

        new_symbol_exposure = symbol_exposure_ratio + position_ratio
        if new_symbol_exposure > max_per_symbol:
            details['symbol_exposure_check'] = {
                'passed': False,
                'reason': f"单币种持仓超过限制({new_symbol_exposure:.2f}>{max_per_symbol:.2f})",
            }
            return self._failure('MAX_SYMBOL_EXPOSURE', '单币种持仓超过限制', details, 'symbol_exposure_check')
        details['symbol_exposure_check'] = {
            'passed': True,
            'reason': '单币种风险正常',
            'after_open': round(new_symbol_exposure, 4)
        }

        # 4. 市场环境过滤（趋势 / 波动率）
        market_filters = self._cfg_section(signal.symbol, 'market_filters')
        context = getattr(signal, 'market_context', {}) or {}
        trend = context.get('trend', 'sideways')

        if market_filters.get('block_low_volatility', True) and context.get('volatility_too_low'):
            details['market_volatility_check'] = {
                'passed': False,
                'reason': f"波动率过低({context.get('volatility')})",
                'context': context
            }
            return self._failure('LOW_VOLATILITY', '波动率过低', details, 'market_volatility_check')

        if market_filters.get('block_high_volatility', True) and context.get('volatility_too_high'):
            details['market_volatility_check'] = {
                'passed': False,
                'reason': f"波动率过高({context.get('volatility')})",
                'context': context
            }
            return self._failure('HIGH_VOLATILITY', '波动率过高', details, 'market_volatility_check')

        if market_filters.get('block_counter_trend', True):
            if signal.signal_type == 'buy' and trend == 'bearish':
                details['trend_alignment_check'] = {
                    'passed': False,
                    'reason': '信号逆大趋势(当前偏空)',
                    'context': context
                }
                return self._failure('COUNTER_TREND', '信号逆大趋势', details, 'trend_alignment_check')
            if signal.signal_type == 'sell' and trend == 'bullish':
                details['trend_alignment_check'] = {
                    'passed': False,
                    'reason': '信号逆大趋势(当前偏多)',
                    'context': context
                }
                return self._failure('COUNTER_TREND', '信号逆大趋势', details, 'trend_alignment_check')

        details['market_context_check'] = {
            'passed': True,
            'trend': trend,
            'volatility': context.get('volatility'),
            'atr_ratio': context.get('atr_ratio'),
            'regime': context.get('regime'),
            'regime_confidence': context.get('regime_confidence')
        }

        # Regime Layer v1: 保守接入 - 基于 regime 状态过滤
        # 回退逻辑：如果 regime 不明确或数据不足，跳过 regime 过滤
        regime = context.get('regime', 'unknown')
        regime_confidence = context.get('regime_confidence', 0.0)
        
        # 配置: 是否启用 regime 过滤
        regime_cfg = self._cfg_section(signal.symbol, 'regime_filters') or {}
        enable_regime_filter = regime_cfg.get('enabled', True)
        
        if enable_regime_filter and regime and regime != 'unknown' and regime_confidence >= 0.5:
            # RISK_ANOMALY: 高置信风险异常，直接拦截
            if regime == 'risk_anomaly':
                details['regime_check'] = {
                    'passed': False,
                    'reason': f"风险异常检测({context.get('regime_details', '')})",
                    'regime': regime,
                    'confidence': regime_confidence
                }
                return self._failure('REGIME_RISK_ANOMALY', f"风险异常: {context.get('regime_details', '')}", details, 'regime_check')
            
            # HIGH_VOL: 高波动，降低仓位或拦截（配置决定）
            if regime == 'high_vol':
                block_high_vol = regime_cfg.get('block_high_vol', False)
                if block_high_vol:
                    details['regime_check'] = {
                        'passed': False,
                        'reason': f"高波动市场({context.get('regime_details', '')})",
                        'regime': regime,
                        'confidence': regime_confidence
                    }
                    return self._failure('REGIME_HIGH_VOL', f"高波动: {context.get('regime_details', '')}", details, 'regime_check')
                # 否则记录但放行
                details['regime_check'] = {
                    'passed': True,
                    'reason': f"高波动市场但放行({context.get('regime_details', '')})",
                    'regime': regime,
                    'confidence': regime_confidence,
                    'warning': '建议降低仓位'
                }
            
            # LOW_VOL: 低波动盘整，可以拦截（趋势不明确）
            if regime == 'low_vol':
                block_low_vol = regime_cfg.get('block_low_vol', True)  # 默认拦截
                if block_low_vol:
                    details['regime_check'] = {
                        'passed': False,
                        'reason': f"低波动盘整({context.get('regime_details', '')})",
                        'regime': regime,
                        'confidence': regime_confidence
                    }
                    return self._failure('REGIME_LOW_VOL', f"低波动: {context.get('regime_details', '')}", details, 'regime_check')
                details['regime_check'] = {
                    'passed': True,
                    'reason': f"低波动盘整但放行({context.get('regime_details', '')})",
                    'regime': regime,
                    'confidence': regime_confidence
                }
            
            # TREND / RANGE: 暂不拦截，只记录
            if regime in ['trend', 'range']:
                details['regime_check'] = {
                    'passed': True,
                    'reason': f"市场状态正常({context.get('regime_details', '')})",
                    'regime': regime,
                    'confidence': regime_confidence
                }
        else:
            # 回退旧逻辑: regime 不明确或置信度低
            details['regime_check'] = {
                'passed': True,
                'reason': ' regime状态不明或置信度低，跳过过滤',
                'regime': regime,
                'confidence': regime_confidence,
                'fallback': True
            }

        # 5. 最低强度与策略数（详细化）
        composite_cfg = self._cfg_section(signal.symbol, 'strategies.composite')
        min_strategy_count = composite_cfg.get('min_strategy_count', 1)
        min_strength = composite_cfg.get('min_strength', 20)
        if len(signal.strategies_triggered) < min_strategy_count:
            details['strategy_check'] = {
                'passed': False,
                'reason': f"触发策略数不足({len(signal.strategies_triggered)}<{min_strategy_count})"
            }
            return self._failure('INSUFFICIENT_STRATEGY_COUNT', '触发策略数不足', details, 'strategy_check')
        if signal.strength < min_strength:
            details['strength_check'] = {
                'passed': False,
                'reason': f"信号强度不足({signal.strength}<{min_strength})"
            }
            return self._failure('WEAK_SIGNAL_STRENGTH', '信号强度不足', details, 'strength_check')

        details['strategy_check'] = {
            'passed': True,
            'reason': '策略确认通过',
            'strategies': signal.strategies_triggered
        }
        details['strength_check'] = {
            'passed': True,
            'reason': '信号强度达标',
            'strength': signal.strength
        }

        return True, None, details

    def get_filter_summary(self, details: dict) -> str:
        for _, value in details.items():
            if isinstance(value, dict) and not value.get('passed', True):
                return value.get('reason', 'Unknown')
        return None


class SignalRecorder:
    """信号记录器 - 增强版"""

    def __init__(self, database):
        self.db = database

    def record(self, signal, filter_result: tuple = None) -> int:
        passed, reason, details = filter_result or (True, None, {})
        filter_meta = details.get('filter_meta', {}) if isinstance(details, dict) else {}
        signal_id = self.db.record_signal(
            symbol=signal.symbol,
            signal_type=signal.signal_type,
            price=signal.price,
            strength=signal.strength,
            reasons=signal.reasons,
            strategies_triggered=signal.strategies_triggered
        )
        # Always save filter_details (includes entry_decision) for observability
        # Merge signal's filter_details with validation details
        signal_filter_details = getattr(signal, 'filter_details', None) or {}
        # Merge validation details into signal's filter_details
        if details:
            signal_filter_details.update(details)
        
        self.db.update_signal(
            signal_id,
            filtered=1 if not passed else 0,
            filter_reason=reason,
            filter_code=filter_meta.get('code') if not passed else None,
            filter_group=filter_meta.get('group') if not passed else None,
            action_hint=filter_meta.get('action_hint') if not passed else None,
            filter_details=json.dumps(signal_filter_details, ensure_ascii=False)
        )
        for reason_data in signal.reasons:
            self.db.record_strategy_analysis(
                signal_id=signal_id,
                strategy_name=reason_data.get('strategy', 'Unknown'),
                triggered=reason_data.get('triggered', True),
                strength=int(reason_data.get('strength', 0) or 0),
                confidence=float(reason_data.get('confidence', 0) or 0),
                action=reason_data.get('action'),
                details=reason_data.get('detail')
            )
        return signal_id

    def mark_executed(self, signal_id: int, trade_id: int = None):
        self.db.update_signal(signal_id, executed=1, trade_id=trade_id)

    def mark_filtered(self, signal_id: int, reason: str, details: dict = None):
        self.db.update_signal(signal_id, filtered=1, filter_reason=reason)

    def get_signal_history(self, symbol: str = None, limit: int = 100) -> List[Dict]:
        return self.db.get_signals(symbol=symbol, limit=limit)

    def get_signal_stats(self, days: int = 30) -> Dict:
        signals = self.db.get_signals(limit=1000)
        cutoff = datetime.now() - timedelta(days=days)
        signals = [s for s in signals if datetime.fromisoformat(s['created_at']) > cutoff]
        total = len(signals)
        executed = sum(1 for s in signals if s.get('executed'))
        filtered = sum(1 for s in signals if s.get('filtered'))
        strategy_counts = {}
        for s in signals:
            for strat in s.get('strategies_triggered', []):
                strategy_counts[strat] = strategy_counts.get(strat, 0) + 1
        return {
            'total_signals': total,
            'executed_signals': executed,
            'filtered_signals': filtered,
            'execution_rate': round(executed / total * 100, 2) if total > 0 else 0,
            'strategy_counts': strategy_counts
        }
