"""
Forward Readiness Checker - 前向数据就绪检查器

自动判断 forward data 是否达到可开始校准 Entry Decision 阈值/权重的门槛。

输出状态:
- OBSERVE: 样本未够，继续观察
- WEAK_READY: 勉强可分析，但分布不足/偏态  
- READY: 可开始校准

评估维度:
- 总 forward signal 数量
- allow/watch/block 各类样本数 (从 filter_details 提取)
- 已有 outcome 可评估样本数
- regime 覆盖度
- symbol 覆盖度
- 最近窗口的分布偏态

设计原则:
- MVP 先用现有数据结构推导
- 给出清晰的 readiness rationale
- 兼容现有 API，不做无关重构
"""
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any
from enum import Enum
import json
from datetime import datetime, timedelta
import pandas as pd


class ReadinessStatus(Enum):
    """就绪状态枚举"""
    OBSERVE = "OBSERVE"       # 样本未够，继续观察
    WEAK_READY = "WEAK_READY" # 勉强可分析，但分布不足/偏态
    READY = "READY"           # 可开始校准


@dataclass
class ReadinessMetrics:
    """就绪指标详情"""
    # 信号计数
    total_signals: int = 0
    buy_signals: int = 0
    sell_signals: int = 0
    hold_signals: int = 0
    
    # 过滤决策分布 (从 filter_details 提取)
    allow_count: int = 0
    watch_count: int = 0
    block_count: int = 0
    
    # 可评估样本
    evaluable_signals: int = 0  # 有足够历史数据可评估outcome的信号
    
    # Symbol 覆盖
    symbols: List[str] = field(default_factory=list)
    symbol_counts: Dict[str, int] = field(default_factory=dict)
    dominant_symbol_pct: float = 0.0
    
    # 时间分布
    latest_signal_at: Optional[str] = None
    oldest_signal_at: Optional[str] = None
    days_of_data: int = 0
    
    # 最近窗口 (最近7天) 统计
    recent_signals_7d: int = 0
    recent_buy_sell_ratio: float = 0.0
    
    # 分布偏态指标
    direction_skew: float = 0.0  # 0=均衡, 1=完全倾向一方


@dataclass
class ReadinessResult:
    """就绪检查结果"""
    status: str = "OBSERVE"
    readiness_pct: float = 0.0  # 0-100
    score: int = 0               # 0-100 综合评分
    
    metrics: ReadinessMetrics = field(default_factory=ReadinessMetrics)
    
    reasons: List[str] = field(default_factory=list)    # 有利因素
    blockers: List[str] = field(default_factory=list)    # 不利因素/阻碍
    next_steps: List[str] = field(default_factory=list)  # 下一步建议
    
    def to_dict(self) -> Dict:
        return {
            'status': self.status,
            'readiness_pct': round(self.readiness_pct, 1),
            'score': self.score,
            'metrics': asdict(self.metrics),
            'reasons': self.reasons,
            'blockers': self.blockers,
            'next_steps': self.next_steps
        }


class ForwardReadinessChecker:
    """
    前向数据就绪检查器
    
    用于判断是否有足够的数据来校准 Entry Decision 的阈值和权重。
    """
    
    # MVP 阈值配置
    THRESHOLDS = {
        # 绝对数量要求
        'min_total_signals': 100,
        'min_buy_sell_signals': 20,  # buy + sell 至少需要这么多
        
        # 分布要求
        'min_symbols': 3,
        'max_dominant_symbol_pct': 0.80,  # 单一symbol不超80%
        'min_evaluable_signals': 30,
        
        # 评分权重
        'weight_total': 20,
        'weight_distribution': 25,
        'weight_symbols': 20,
        'weight_evaluable': 20,
        'weight_recency': 15,
    }
    
    # 状态阈值
    READY_THRESHOLD = 70      # >=70 为 READY
    WEAK_READY_THRESHOLD = 40  # 40-69 为 WEAK_READY
    OBSERVE_THRESHOLD = 0      # <40 为 OBSERVE
    
    def __init__(self, db=None):
        """初始化
        
        Args:
            db: Database 实例，传入则以数据库计算
        """
        self.db = db
    
    def check(self, signals: List[Dict] = None) -> ReadinessResult:
        """
        执行就绪检查
        
        Args:
            signals: 信号列表，传入则直接使用，否则从db获取
            
        Returns:
            ReadinessResult: 就绪检查结果
        """
        # 获取信号数据
        if signals is None:
            if self.db is None:
                raise ValueError("需要传入 signals 或提供 db 实例")
            signals = self.db.get_signals(limit=10000)
        
        if not signals:
            return self._empty_result()
        
        # 计算指标
        metrics = self._calculate_metrics(signals)
        
        # 计算评分
        score, readiness_pct = self._calculate_score(metrics)
        
        # 确定状态
        status = self._determine_status(score)
        
        # 生成原因和建议
        reasons, blockers, next_steps = self._generate_reasoning(metrics, score)
        
        return ReadinessResult(
            status=status.value,
            readiness_pct=readiness_pct,
            score=score,
            metrics=metrics,
            reasons=reasons,
            blockers=blockers,
            next_steps=next_steps
        )
    
    def _empty_result(self) -> ReadinessResult:
        """空结果"""
        return ReadinessResult(
            status=ReadinessStatus.OBSERVE.value,
            readiness_pct=0,
            score=0,
            metrics=ReadinessMetrics(),
            blockers=["暂无信号数据"],
            next_steps=["等待系统生成更多信号"]
        )
    
    def _calculate_metrics(self, signals: List[Dict]) -> ReadinessMetrics:
        """计算各项指标"""
        metrics = ReadinessMetrics()
        
        if not signals:
            return metrics
        
        df = pd.DataFrame(signals)
        
        # 1. 基本计数
        metrics.total_signals = len(signals)
        metrics.buy_signals = int(df[df['signal_type'] == 'buy'].shape[0])
        metrics.sell_signals = int(df[df['signal_type'] == 'sell'].shape[0])
        metrics.hold_signals = int(df[df['signal_type'] == 'hold'].shape[0])
        
        # 2. 从 filter_details 提取决策分布
        allow_count = 0
        watch_count = 0
        block_count = 0
        
        for sig in signals:
            fd = sig.get('filter_details') or sig.get('filter_detail') or {}
            if isinstance(fd, str):
                try:
                    fd = json.loads(fd) if fd else {}
                except:
                    fd = {}
            
            # 提取 decision
            decision = fd.get('decision', '').lower()
            if decision == 'allow':
                allow_count += 1
            elif decision == 'watch':
                watch_count += 1
            elif decision == 'block':
                block_count += 1
            else:
                # fallback: 基于 filtered 字段和 filter_reason 推断
                if sig.get('filtered') == 0:
                    allow_count += 1  # 未被过滤 = allow
                else:
                    # 有过滤则根据 reason 判断
                    reason = sig.get('filter_reason', '').lower()
                    if '过低' in reason or '高' in reason or '风险' in reason:
                        block_count += 1
                    else:
                        watch_count += 1
        
        metrics.allow_count = allow_count
        metrics.watch_count = watch_count
        metrics.block_count = block_count
        
        # 3. Symbol 覆盖
        if 'symbol' in df.columns:
            symbol_counts = df['symbol'].value_counts().to_dict()
            metrics.symbol_counts = symbol_counts
            metrics.symbols = list(symbol_counts.keys())
            
            if symbol_counts:
                max_count = max(symbol_counts.values())
                metrics.dominant_symbol_pct = max_count / len(signals)
        
        # 4. 时间分布
        if 'created_at' in df.columns:
            dates = pd.to_datetime(df['created_at'], errors='coerce')
            valid_dates = dates.dropna()
            
            if len(valid_dates) > 0:
                metrics.latest_signal_at = valid_dates.max().isoformat()
                metrics.oldest_signal_at = valid_dates.min().isoformat()
                
                time_diff = valid_dates.max() - valid_dates.min()
                metrics.days_of_data = max(1, time_diff.days)
                
                # 最近7天
                recent_cutoff = valid_dates.max() - timedelta(days=7)
                recent_df = df[dates >= recent_cutoff]
                metrics.recent_signals_7d = len(recent_df)
                
                # 最近买卖比
                recent_buy = int(recent_df[recent_df['signal_type'] == 'buy'].shape[0])
                recent_sell = int(recent_df[recent_df['signal_type'] == 'sell'].shape[0])
                if recent_buy + recent_sell > 0:
                    metrics.recent_buy_sell_ratio = recent_buy / (recent_buy + recent_sell)
        
        # 5. 可评估信号 (MVP: 假设所有非hold且有时序数据的都算可评估)
        # 实际需要检查信号是否在窗口内有足够历史数据
        metrics.evaluable_signals = metrics.buy_signals + metrics.sell_signals
        
        # 6. 方向偏态 (buy vs sell 均衡度)
        total_directional = metrics.buy_signals + metrics.sell_signals
        if total_directional > 0:
            buy_ratio = metrics.buy_signals / total_directional
            # 0 = 完全均衡, 1 = 完全偏向一方
            metrics.direction_skew = abs(0.5 - buy_ratio) * 2
        
        return metrics
    
    def _calculate_score(self, metrics: ReadinessMetrics) -> tuple:
        """计算就绪评分 (0-100)"""
        t = self.THRESHOLDS
        score = 0
        
        # 1. 总信号量得分 (20%)
        if metrics.total_signals >= t['min_total_signals']:
            score += t['weight_total']
        else:
            score += t['weight_total'] * (metrics.total_signals / t['min_total_signals'])
        
        # 2. 分布得分 (25%)
        # 检查 allow/watch/block 是否有合理分布
        total_decisions = metrics.allow_count + metrics.watch_count + metrics.block_count
        if total_decisions > 0:
            # 分布均衡度 (三者都有且不太偏)
            min_count = min(metrics.allow_count, metrics.watch_count, metrics.block_count)
            distribution_score = min_count / (total_decisions / 3) if total_decisions > 0 else 0
            distribution_score = min(1.0, distribution_score)
            
            # 同时要求有一定量的 allow 信号用于校准
            if metrics.allow_count >= 10:
                score += t['weight_distribution'] * distribution_score
            else:
                score += t['weight_distribution'] * distribution_score * 0.5
        else:
            # 没有决策数据时，基于原始信号分布给分
            directional = metrics.buy_signals + metrics.sell_signals
            if directional >= t['min_buy_sell_signals']:
                score += t['weight_distribution'] * 0.5
        
        # 3. Symbol 覆盖得分 (20%)
        if len(metrics.symbols) >= t['min_symbols']:
            score += t['weight_symbols']
        else:
            score += t['weight_symbols'] * (len(metrics.symbols) / t['min_symbols'])
        
        # 惩罚 dominant symbol 过高
        if metrics.dominant_symbol_pct > t['max_dominant_symbol_pct']:
            penalty = (metrics.dominant_symbol_pct - t['max_dominant_symbol_pct']) * 50
            score = max(0, score - penalty)
        
        # 4. 可评估样本得分 (20%)
        if metrics.evaluable_signals >= t['min_evaluable_signals']:
            score += t['weight_evaluable']
        else:
            score += t['weight_evaluable'] * (metrics.evaluable_signals / t['min_evaluable_signals'])
        
        # 5. 最近数据得分 (15%)
        if metrics.recent_signals_7d >= 10:
            score += t['weight_recency']
        elif metrics.recent_signals_7d >= 5:
            score += t['weight_recency'] * 0.6
        elif metrics.recent_signals_7d > 0:
            score += t['weight_recency'] * 0.3
        
        readiness_pct = round(score, 1)
        return int(score), readiness_pct
    
    def _determine_status(self, score: int) -> ReadinessStatus:
        """根据评分确定状态"""
        if score >= self.READY_THRESHOLD:
            return ReadinessStatus.READY
        elif score >= self.WEAK_READY_THRESHOLD:
            return ReadinessStatus.WEAK_READY
        else:
            return ReadinessStatus.OBSERVE
    
    def _generate_reasoning(self, metrics: ReadinessMetrics, score: int) -> tuple:
        """生成原因、阻碍和建议"""
        reasons = []
        blockers = []
        next_steps = []
        
        t = self.THRESHOLDS
        
        # 有利因素
        if metrics.total_signals >= t['min_total_signals']:
            reasons.append(f"信号总量充足: {metrics.total_signals}")
        
        if metrics.evaluable_signals >= t['min_evaluable_signals']:
            reasons.append(f"可评估样本足够: {metrics.evaluable_signals}")
        
        if len(metrics.symbols) >= t['min_symbols']:
            reasons.append(f"Symbol覆盖良好: {len(metrics.symbols)}个")
        
        if metrics.recent_signals_7d >= 10:
            reasons.append(f"近期数据活跃: 最近7天{metrics.recent_signals_7d}条")
        
        # 阻碍因素
        if metrics.total_signals < t['min_total_signals']:
            blockers.append(f"信号总量不足: {metrics.total_signals}/{t['min_total_signals']}")
        
        if metrics.allow_count < 10:
            blockers.append(f"allow样本不足: {metrics.allow_count} (需要>=10进行校准)")
        
        if metrics.dominant_symbol_pct > t['max_dominant_symbol_pct']:
            dominant = max(metrics.symbol_counts.items(), key=lambda x: x[1])
            blockers.append(f"Symbol分布偏态: {dominant[0]}占{int(metrics.dominant_symbol_pct*100)}%")
        
        if metrics.direction_skew > 0.7:
            blockers.append(f"方向偏态严重: buy/sell不均衡")
        
        # 建议
        if score < self.WEAK_READY_THRESHOLD:
            next_steps.append("继续观察，等待更多信号数据积累")
            next_steps.append("关注多个symbol的信号分布")
        elif score < self.READY_THRESHOLD:
            next_steps.append("数据勉强可用，建议小范围试点校准")
            next_steps.append("增加allow样本数量后再全面校准")
        else:
            next_steps.append("数据就绪，可以开始阈值/权重校准")
            next_steps.append("建议先用历史数据做回测验证")
        
        return reasons, blockers, next_steps


# 便捷函数
def check_forward_readiness(db=None, signals: List[Dict] = None) -> Dict:
    """
    快速检查前向数据就绪状态
    
    Args:
        db: Database 实例
        signals: 信号列表 (可选)
        
    Returns:
        Dict: 就绪检查结果 (JSON 友好)
    """
    checker = ForwardReadinessChecker(db=db)
    result = checker.check(signals=signals)
    return result.to_dict()
