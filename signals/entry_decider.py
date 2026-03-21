"""
Entry Decision Layer (MVP) - 开单决策层
评估"这个信号值不值得开单"，输出更精细的决策结果

输出:
- decision: allow / watch / block
- score: 0-100 总分
- breakdown: 各维度评分详情
- reason_summary: 中文解释

设计原则:
- 复用现有 signal/regime/market_context 结果
- 不破坏现有 API 向后兼容
- 决策结果写入 filter_details 供观测
"""
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, List
from enum import Enum


class EntryDecision(Enum):
    """开单决策枚举"""
    ALLOW = "allow"   # 允许开单
    WATCH = "watch"  # 观望（不建议开，但记录）
    BLOCK = "block"   # 拦截（不建议开单）


@dataclass
class DecisionBreakdown:
    """决策详情分解"""
    # 各维度评分 (0-100)
    signal_strength_score: int = 0          # 信号强度评分
    regime_alignment_score: int = 0           # 市场状态适配度
    volatility_fitness_score: int = 0        # 波动环境适合度
    trend_alignment_score: int = 0            # 趋势顺势度
    execution_risk_score: int = 0             # 执行风险度
    ml_confidence_score: int = 0              # ML 置信度 (如无ML则为50)
    
    # 各维度原因
    signal_strength_reason: str = ""
    regime_alignment_reason: str = ""
    volatility_fitness_reason: str = ""
    trend_alignment_reason: str = ""
    execution_risk_reason: str = ""
    ml_confidence_reason: str = ""


@dataclass
class EntryDecisionResult:
    """开单决策结果"""
    decision: str = "watch"           # allow/watch/block
    score: int = 50                   # 0-100 总分
    breakdown: DecisionBreakdown = field(default_factory=DecisionBreakdown)
    reason_summary: str = ""           # 中文解释
    watch_reasons: List[str] = field(default_factory=list)  # 需要观望的原因
    
    def to_dict(self) -> Dict:
        return {
            'decision': self.decision,
            'score': self.score,
            'breakdown': asdict(self.breakdown),
            'reason_summary': self.reason_summary,
            'watch_reasons': self.watch_reasons
        }


class EntryDecider:
    """
    开单决策器 (MVP)
    
    评估维度:
    1. signal_strength - 信号强度与策略一致性
    2. regime_alignment - 市场状态适配度
    3. volatility_fitness - 波动环境是否适合出手
    4. trend_alignment - 是否顺势或避免逆势
    5. execution_risk - 当前风险占用、冷却、持仓冲突等
    6. ml_confidence - ML 预测置信度 (如有)
    """
    
    # 评分阈值配置
    DEFAULT_THRESHOLDS = {
        # signal_strength
        'min_strength_allow': 25,
        'min_strategy_count_allow': 2,
        
        # regime
        'min_regime_confidence_allow': 0.5,
        
        # volatility
        'volatility_low_block': 0.003,
        'volatility_high_block': 0.05,
        
        # execution_risk
        'max_exposure_watch': 0.7,
        'max_exposure_block': 0.9,
        'cooldown_watch_minutes': 5,
        
        # ml_confidence
        'ml_min_confidence_allow': 0.6,
    }
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.thresholds = {**self.DEFAULT_THRESHOLDS, **self.config.get('entry_decider', {})}
    
    def _cfg(self, key: str, default=None):
        """获取配置值"""
        return self.thresholds.get(key, default)
    
    def decide(self, signal, current_positions: Dict = None,
               tracking_data: Dict = None, ml_prediction: tuple = None) -> EntryDecisionResult:
        """
        评估信号是否值得开单
        
        Args:
            signal: Signal 对象
            current_positions: 当前持仓 dict
            tracking_data: 追踪数据 (含冷却时间等)
            ml_prediction: ML 预测 tuple (pred, prob)
            
        Returns:
            EntryDecisionResult: 决策结果
        """
        current_positions = current_positions or {}
        tracking_data = tracking_data or {}
        
        result = EntryDecisionResult()
        breakdown = DecisionBreakdown()
        
        # 1. Signal Strength Score
        signal_strength_score, signal_strength_reason = self._eval_signal_strength(signal)
        breakdown.signal_strength_score = signal_strength_score
        breakdown.signal_strength_reason = signal_strength_reason
        
        # 2. Regime Alignment Score
        regime_score, regime_reason = self._eval_regime_alignment(signal)
        breakdown.regime_alignment_score = regime_score
        breakdown.regime_alignment_reason = regime_reason
        
        # 3. Volatility Fitness Score
        vol_score, vol_reason = self._eval_volatility_fitness(signal)
        breakdown.volatility_fitness_score = vol_score
        breakdown.volatility_fitness_reason = vol_reason
        
        # 4. Trend Alignment Score
        trend_score, trend_reason = self._eval_trend_alignment(signal)
        breakdown.trend_alignment_score = trend_score
        breakdown.trend_alignment_reason = trend_reason
        
        # 5. Execution Risk Score
        risk_score, risk_reason = self._eval_execution_risk(
            signal, current_positions, tracking_data
        )
        breakdown.execution_risk_score = risk_score
        breakdown.execution_risk_reason = risk_reason
        
        # 6. ML Confidence Score
        ml_score, ml_reason = self._eval_ml_confidence(ml_prediction, signal)
        breakdown.ml_confidence_score = ml_score
        breakdown.ml_confidence_reason = ml_reason
        
        result.breakdown = breakdown
        
        # 计算总分 (加权平均)
        total_score = self._calculate_total_score(breakdown)
        result.score = total_score
        
        # 生成决策
        result.decision, result.watch_reasons = self._make_decision(
            breakdown, total_score, signal
        )
        
        # 生成中文解释
        result.reason_summary = self._generate_summary(result.decision, breakdown, signal)
        
        return result
    
    def _eval_signal_strength(self, signal) -> tuple:
        """评估信号强度"""
        strength = getattr(signal, 'strength', 0) or 0
        strategies = getattr(signal, 'strategies_triggered', []) or []
        direction_score = getattr(signal, 'direction_score', {}) or {}
        net = direction_score.get('net', 0)
        
        # 评分逻辑
        score = 0
        
        # 基础强度评分 (0-60)
        strength_component = min(60, int(strength * 0.6))
        score += strength_component
        
        # 策略数量加分 (0-20)
        strategy_count = len(strategies)
        if strategy_count >= 3:
            score += 20
        elif strategy_count == 2:
            score += 15
        elif strategy_count == 1:
            score += 10
        
        # 净强度加分 (0-20)
        net_component = min(20, int(net / 2))
        score += net_component
        
        score = min(100, score)
        
        # 原因
        if score >= 50:
            reason = f"信号强度{strength}，触发{strategy_count}个策略，净强度{net}"
        else:
            reason = f"信号强度偏弱({strength})，仅触发{strategy_count}个策略"
        
        return score, reason
    
    def _eval_regime_alignment(self, signal) -> tuple:
        """评估市场状态适配度"""
        regime_info = getattr(signal, 'regime_info', {}) or {}
        market_context = getattr(signal, 'market_context', {}) or {}
        
        regime = regime_info.get('regime', 'unknown')
        confidence = regime_info.get('regime_confidence', 0) or regime_info.get('confidence', 0)
        signal_type = getattr(signal, 'signal_type', 'hold')
        
        score = 50  # 默认中立
        
        # 高置信风险异常，大幅扣分
        if regime == 'risk_anomaly' and confidence >= 0.5:
            score = 10
            reason = "检测到风险异常，市场可能剧烈波动"
        # 高波动但适配
        elif regime == 'high_vol' and confidence >= 0.5:
            if signal_type in ['buy', 'sell']:
                score = 45  # 高波动环境，降低分数
                reason = f"当前高波动市场({confidence:.0%}置信度)，需谨慎"
            else:
                score = 50
                reason = "高波动市场，无明确方向"
        # 低波动盘整
        elif regime == 'low_vol' and confidence >= 0.5:
            score = 60  # 低波动可以但得分不高
            reason = "低波动盘整市场，趋势不明确"
        # 趋势市场
        elif regime == 'trend' and confidence >= 0.5:
            score = 75
            reason = f"趋势市场({regime})，置信度{confidence:.0%}"
        # 震荡/盘整
        elif regime == 'range' and confidence >= 0.5:
            score = 60
            reason = "震荡市场，区间波动为主"
        # regime 不明确
        else:
            score = 50
            reason = f"市场状态不明确(regime={regime})，置信度{confidence:.0%}"
        
        return score, reason
    
    def _eval_volatility_fitness(self, signal) -> tuple:
        """评估波动环境适合度"""
        market_context = getattr(signal, 'market_context', {}) or {}
        
        volatility = market_context.get('volatility', 0) or 0
        atr_ratio = market_context.get('atr_ratio', 0) or 0
        too_low = market_context.get('volatility_too_low', False)
        too_high = market_context.get('volatility_too_high', False)
        
        score = 50
        
        if too_low:
            score = 25
            reason = f"波动率过低({volatility:.4f})，趋势不明确，不适合开单"
        elif too_high:
            score = 20
            reason = f"波动率过高({volatility:.4f})，风险过大，建议回避"
        else:
            # 正常波动范围
            if volatility > 0.01:
                score = 75
                reason = f"波动适中({volatility:.4f})，存在交易机会"
            else:
                score = 60
                reason = f"波动偏低({volatility:.4f})，但尚可接受"
        
        return score, reason
    
    def _eval_trend_alignment(self, signal) -> tuple:
        """评估趋势顺势度"""
        market_context = getattr(signal, 'market_context', {}) or {}
        
        trend = market_context.get('trend', 'sideways')
        signal_type = getattr(signal, 'signal_type', 'hold')
        
        score = 50
        
        if signal_type == 'hold':
            score = 50
            reason = "信号无明确方向"
        elif trend == 'sideways':
            score = 60
            reason = "横盘震荡市，建议区间操作"
        elif (signal_type == 'buy' and trend == 'bullish') or \
             (signal_type == 'sell' and trend == 'bearish'):
            score = 85
            reason = f"顺势交易({signal_type} + {trend})，胜率更高"
        elif (signal_type == 'buy' and trend == 'bearish') or \
             (signal_type == 'sell' and trend == 'bullish'):
            score = 30
            reason = f"逆势交易({signal_type} + {trend})，风险较大"
        else:
            score = 50
            reason = f"趋势状态: {trend}"
        
        return score, reason
    
    def _eval_execution_risk(self, signal, current_positions: Dict,
                              tracking_data: Dict) -> tuple:
        """评估执行风险"""
        from datetime import datetime
        
        symbol = getattr(signal, 'symbol', '')
        signal_type = getattr(signal, 'signal_type', 'hold')
        
        score = 70  # 默认较低风险
        
        watch_reasons = []
        
        # 1. 检查同向持仓
        side = 'long' if signal_type == 'buy' else 'short'
        existing = current_positions.get(symbol) if current_positions else {}
        if existing and existing.get('side') == side:
            score -= 40
            watch_reasons.append(f"已有同向持仓")
        
        # 2. 检查冷却时间
        cooldown = self._cfg('cooldown_watch_minutes', 5)
        if tracking_data.get(symbol):
            last_trade = tracking_data[symbol].get('last_trade_time')
            if last_trade:
                try:
                    last_time = datetime.fromisoformat(last_trade)
                    diff_minutes = (datetime.now() - last_time).total_seconds() / 60
                    if diff_minutes < cooldown:
                        score -= 25
                        watch_reasons.append(f"冷却期内({diff_minutes:.0f}分钟)")
                except Exception:
                    pass
        
        # 3. 总体持仓检查 (简化版)
        # 如果有其他持仓，给一定风险扣分
        if current_positions and len(current_positions) > 2:
            score -= 10
            watch_reasons.append(f"已持仓{len(current_positions)}个币种")
        
        score = max(0, min(100, score))
        
        if watch_reasons:
            reason = "，".join(watch_reasons)
        else:
            reason = "执行条件良好"
        
        return score, reason
    
    def _eval_ml_confidence(self, ml_prediction, signal) -> tuple:
        """评估 ML 置信度"""
        score = 50  # 默认中立（无ML时）
        reason = "无ML模型数据"
        
        if ml_prediction is None:
            # 检查 signal 中是否已有 ML 分析
            reasons = getattr(signal, 'reasons', []) or []
            ml_reasons = [r for r in reasons if r.get('strategy') == 'ML']
            if ml_reasons:
                ml_reason = ml_reasons[0]
                prob = ml_reason.get('value', 0) or 0
                action = ml_reason.get('action')
                signal_type = getattr(signal, 'signal_type', 'hold')
                
                # 如果 ML 方向与信号方向一致
                if (action == 'buy' and signal_type == 'buy') or \
                   (action == 'sell' and signal_type == 'sell'):
                    score = int(prob * 100)
                    reason = f"ML预测{action}概率{prob:.0%}，与信号方向一致"
                else:
                    score = max(10, int((1 - prob) * 50))
                    reason = f"ML预测方向与信号不一致"
            return score, reason
        
        # 直接传入的 ML 预测
        pred, prob = ml_prediction
        min_conf = self._cfg('ml_min_confidence_allow', 0.6)
        
        signal_type = getattr(signal, 'signal_type', 'hold')
        
        if pred == 1 and prob >= min_conf:  # ML 预测涨
            if signal_type == 'buy':
                score = int(prob * 100)
                reason = f"ML预测上涨概率{prob:.0%}，与做多信号一致"
            else:
                score = 30
                reason = f"ML预测上涨但信号是{signal_type}，方向冲突"
        elif pred == 0 and prob >= min_conf:  # ML 预测跌
            if signal_type == 'sell':
                score = int(prob * 100)
                reason = f"ML预测下跌概率{prob:.0%}，与做空信号一致"
            else:
                score = 30
                reason = f"ML预测下跌但信号是{signal_type}，方向冲突"
        else:
            score = 50
            reason = f"ML置信度不足({prob:.0%})，仅供参考"
        
        return score, reason
    
    def _calculate_total_score(self, breakdown: DecisionBreakdown) -> int:
        """计算总分 (加权平均)"""
        # 权重配置
        weights = {
            'signal_strength': 0.25,
            'regime_alignment': 0.15,
            'volatility_fitness': 0.15,
            'trend_alignment': 0.20,
            'execution_risk': 0.15,
            'ml_confidence': 0.10,
        }
        
        total = (
            breakdown.signal_strength_score * weights['signal_strength'] +
            breakdown.regime_alignment_score * weights['regime_alignment'] +
            breakdown.volatility_fitness_score * weights['volatility_fitness'] +
            breakdown.trend_alignment_score * weights['trend_alignment'] +
            breakdown.execution_risk_score * weights['execution_risk'] +
            breakdown.ml_confidence_score * weights['ml_confidence']
        )
        
        return int(round(total))
    
    def _make_decision(self, breakdown: DecisionBreakdown, total_score: int,
                       signal) -> tuple:
        """
        根据评分生成最终决策
        
        Returns:
            (decision, watch_reasons)
        """
        watch_reasons = []
        
        # 1. 信号强度不足
        if breakdown.signal_strength_score < 30:
            watch_reasons.append("信号强度不足")
        
        # 2. 逆趋势
        if breakdown.trend_alignment_score < 40:
            watch_reasons.append("逆趋势交易")
        
        # 3. 波动环境不适合
        if breakdown.volatility_fitness_score < 30:
            watch_reasons.append("波动环境不适合")
        
        # 4. 执行风险高
        if breakdown.execution_risk_score < 40:
            watch_reasons.append("执行风险较高")
        
        # 5. 市场状态不明确
        if breakdown.regime_alignment_score < 30:
            watch_reasons.append("市场状态不明确")
        
        # 决策分层
        # BLOCK: 总分 < 35 或 任意关键维度严重不达标
        if total_score < 35:
            return EntryDecision.BLOCK.value, watch_reasons
        
        # WATCH: 总分 < 60 或 有观望原因
        if total_score < 60 or len(watch_reasons) >= 2:
            return EntryDecision.WATCH.value, watch_reasons
        
        # ALLOW: 总分 >= 60 且 关键维度基本达标
        if total_score >= 60:
            return EntryDecision.ALLOW.value, watch_reasons
        
        # 默认观望
        return EntryDecision.WATCH.value, watch_reasons
    
    def _generate_summary(self, decision: str, breakdown: DecisionBreakdown,
                         signal) -> str:
        """生成中文解释"""
        signal_type = getattr(signal, 'signal_type', 'hold')
        strength = getattr(signal, 'strength', 0)
        
        if decision == EntryDecision.BLOCK.value:
            return (f"信号被拦截：总分{breakdown.signal_strength_score}分，"
                    f"{breakdown.trend_alignment_reason}，{breakdown.volatility_fitness_reason}。"
                    f"建议等待条件改善后再评估。")
        
        elif decision == EntryDecision.WATCH.value:
            return (f"信号建议观望：总分{breakdown.signal_strength_score}分，"
                    f"信号强度{strength}，趋势{alignment_to_chinese(breakdown.trend_alignment_score)}，"
                    f"波动{alignment_to_chinese(breakdown.volatility_fitness_score)}。"
                    f"建议继续观察确认。")
        
        else:  # ALLOW
            return (f"信号允许开单：总分{breakdown.signal_strength_score}分，"
                    f"{breakdown.trend_alignment_reason}，{breakdown.volatility_fitness_reason}。"
                    f"当前条件适合入场。")


def alignment_to_chinese(score: int) -> str:
    """评分转中文描述"""
    if score >= 70:
        return "有利"
    elif score >= 50:
        return "一般"
    else:
        return "不利"


# 导出
__all__ = ['EntryDecider', 'EntryDecision', 'EntryDecisionResult', 'DecisionBreakdown']
