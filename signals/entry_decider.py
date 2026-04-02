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
from typing import Dict, Optional, List, Any
import copy

from core.config import DEFAULT_ADAPTIVE_REGIME_CONFIG
from core.regime_policy import build_observe_only_payload
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
    mtf_breakout_score: int = 0              # MTF breakout 证据分（observe-only，不参与总分）

    # 各维度原因
    signal_strength_reason: str = ""
    regime_alignment_reason: str = ""
    volatility_fitness_reason: str = ""
    trend_alignment_reason: str = ""
    execution_risk_reason: str = ""
    ml_confidence_reason: str = ""
    mtf_breakout_reason: str = ""

    # 观测辅助信息（兼容旧 API，新增字段只会出现在 breakdown 中）
    signal_conflict_score: int = 0            # 多空冲突程度，越高代表越冲突
    mean_reversion_bias: bool = False         # 是否主要依赖 RSI / Bollinger / Pattern 等抄底摸顶逻辑
    observe_only_phase: str = ""
    observe_only_state: str = ""
    observe_only_summary: str = ""
    observe_only_tags: List[str] = field(default_factory=list)
    adaptive_policy_mode: str = "observe_only"
    adaptive_policy_state: str = "neutral"
    adaptive_policy_is_effective: bool = False
    adaptive_effective_thresholds: Dict[str, Any] = field(default_factory=dict)
    adaptive_effective_overrides: Dict[str, Any] = field(default_factory=dict)
    adaptive_applied_overrides: List[str] = field(default_factory=list)
    adaptive_ignored_overrides: List[Dict[str, Any]] = field(default_factory=list)
    adaptive_triggered_rules: List[Dict[str, Any]] = field(default_factory=list)
    adaptive_decision_notes: List[str] = field(default_factory=list)
    adaptive_decision_tags: List[str] = field(default_factory=list)
    adaptive_decision_audit: Dict[str, Any] = field(default_factory=dict)
    mtf_breakout_observe_only: bool = True


@dataclass
class EntryDecisionResult:
    """开单决策结果"""
    decision: str = "watch"           # allow/watch/block
    score: int = 50                   # 0-100 总分
    breakdown: DecisionBreakdown = field(default_factory=DecisionBreakdown)
    reason_summary: str = ""           # 中文解释
    watch_reasons: List[str] = field(default_factory=list)  # 需要观望的原因
    regime_snapshot: Dict = field(default_factory=dict)
    adaptive_policy_snapshot: Dict = field(default_factory=dict)
    observe_only: bool = True

    def to_dict(self) -> Dict:
        return {
            'decision': self.decision,
            'score': self.score,
            'breakdown': asdict(self.breakdown),
            'reason_summary': self.reason_summary,
            'watch_reasons': self.watch_reasons,
            'regime_snapshot': self.regime_snapshot,
            'adaptive_policy_snapshot': self.adaptive_policy_snapshot,
            'observe_only': self.observe_only
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
        'max_conflict_ratio_allow': 0.35,

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

        # decision
        'allow_score_min': 68,
        'block_score_max': 35,
        'single_strategy_block_max_strength': 24,
        'single_strategy_block_max_score': 64,
        'high_conflict_watch_score_min': 68,
        'falling_knife_rsi_threshold': 30,
        'falling_knife_block_max_score': 72,
        'sideways_mean_reversion_watch_max_score': 72,
        'sideways_ml_only_watch_max_score': 60,
    }

    CONSERVATIVE_THRESHOLD_RULES = {
        'allow_score_min': 'max',
        'block_score_max': 'min',
        'max_conflict_ratio_allow': 'min',
        'high_conflict_watch_score_min': 'max',
        'falling_knife_block_max_score': 'min',
        'sideways_mean_reversion_watch_max_score': 'min',
        'sideways_ml_only_watch_max_score': 'min',
        'min_signal_strength_for_allow': 'max',
        'min_regime_alignment_for_allow': 'max',
        'min_volatility_fitness_for_allow': 'max',
        'min_trend_alignment_for_allow': 'max',
        'min_execution_risk_for_allow': 'max',
        'min_ml_confidence_for_allow': 'max',
        'max_signal_conflict_score_for_allow': 'min',
    }

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.thresholds = {**self.DEFAULT_THRESHOLDS, **self._config_get('entry_decider', {})}

    def _config_get(self, key: str, default=None):
        if hasattr(self.config, 'get') and not isinstance(self.config, dict):
            try:
                return self.config.get(key, default)
            except TypeError:
                pass
        if not isinstance(self.config, dict):
            return default
        value = self.config
        for part in (key or '').split('.'):
            if not part:
                continue
            if not isinstance(value, dict):
                return default
            value = value.get(part)
            if value is None:
                return default
        return value

    def _deep_merge(self, base: Dict, override: Dict) -> Dict:
        result = copy.deepcopy(base or {})
        for key, value in (override or {}).items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    def get_symbol_overrides(self, symbol: str) -> Dict:
        overrides = self._config_get('symbol_overrides', {}) or {}
        if not isinstance(overrides, dict):
            return {}
        return overrides.get(symbol, {}) or {}

    def get_adaptive_regime_config(self, symbol: str = None) -> Dict[str, Any]:
        base = self._config_get('adaptive_regime', {}) or {}
        merged = self._deep_merge(DEFAULT_ADAPTIVE_REGIME_CONFIG, base)
        if symbol:
            symbol_adaptive = (self.get_symbol_overrides(symbol) or {}).get('adaptive_regime') or {}
            if isinstance(symbol_adaptive, dict):
                merged = self._deep_merge(merged, symbol_adaptive)
        return merged

    def _cfg(self, key: str, default=None, effective_thresholds: Optional[Dict[str, Any]] = None):
        """获取配置值"""
        if effective_thresholds and key in effective_thresholds:
            return effective_thresholds[key]
        return self.thresholds.get(key, default)

    def decide(self, signal, current_positions: Dict = None,
               tracking_data: Dict = None, ml_prediction: tuple = None) -> EntryDecisionResult:
        """
        评估信号是否值得开单
        """
        current_positions = current_positions or {}
        tracking_data = tracking_data or {}

        result = EntryDecisionResult()
        breakdown = DecisionBreakdown()
        observe_only_payload = build_observe_only_payload(
            self,
            getattr(signal, 'symbol', None),
            signal=signal,
        )
        result.regime_snapshot = observe_only_payload['regime_snapshot']
        result.adaptive_policy_snapshot = observe_only_payload['adaptive_policy_snapshot']
        setattr(signal, 'regime_snapshot', result.regime_snapshot)
        setattr(signal, 'adaptive_policy_snapshot', result.adaptive_policy_snapshot)
        breakdown.observe_only_phase = observe_only_payload.get('observe_only_phase', '')
        breakdown.observe_only_state = observe_only_payload.get('observe_only_state', '')
        breakdown.observe_only_summary = observe_only_payload.get('observe_only_summary', '')
        breakdown.observe_only_tags = list(observe_only_payload.get('observe_only_tags') or [])

        effective_thresholds, adaptive_meta = self._build_effective_thresholds(result.adaptive_policy_snapshot)
        breakdown.adaptive_policy_mode = adaptive_meta['mode']
        breakdown.adaptive_policy_state = adaptive_meta['state']
        breakdown.adaptive_policy_is_effective = adaptive_meta['is_effective']
        breakdown.adaptive_effective_thresholds = effective_thresholds
        breakdown.adaptive_effective_overrides = adaptive_meta['effective_overrides']
        breakdown.adaptive_applied_overrides = adaptive_meta['applied_overrides']
        breakdown.adaptive_ignored_overrides = adaptive_meta['ignored_overrides']
        breakdown.adaptive_decision_notes = adaptive_meta['notes']
        breakdown.adaptive_decision_tags = adaptive_meta['tags']
        self._sync_adaptive_breakdown(breakdown, self._build_adaptive_decision_audit(effective_thresholds, adaptive_meta))
        result.observe_only = not adaptive_meta['is_effective']

        # 1. Signal Strength Score
        signal_strength_score, signal_strength_reason = self._eval_signal_strength(signal, effective_thresholds)
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

        mtf_score, mtf_reason, mtf_observe_only = self._eval_mtf_breakout(signal)
        breakdown.mtf_breakout_score = mtf_score
        breakdown.mtf_breakout_reason = mtf_reason
        breakdown.mtf_breakout_observe_only = mtf_observe_only

        breakdown.signal_conflict_score = getattr(signal, '_entry_signal_conflict_score', 0)
        breakdown.mean_reversion_bias = getattr(signal, '_entry_mean_reversion_bias', False)

        result.breakdown = breakdown

        total_score = self._calculate_total_score(breakdown)
        result.score = total_score

        result.decision, result.watch_reasons = self._make_decision(
            breakdown, total_score, signal, effective_thresholds, adaptive_meta
        )

        result.reason_summary = self._generate_summary(result.decision, breakdown, signal)
        return result

    def _append_ignored_override(self, ignored_overrides: List[Dict[str, Any]], key: str, reason: str, value: Any = None, *, stage: str = 'decision_normalization'):
        ignored_overrides.append({
            'key': str(key or '?'),
            'value': value,
            'reason': str(reason or 'ignored'),
            'stage': stage,
            'status': 'ignored',
        })

    def _append_applied_override(self, applied_overrides: List[str], key: str):
        key = str(key or '?')
        if key not in applied_overrides:
            applied_overrides.append(key)

    def _append_unique_text(self, items: List[str], value: Any):
        if isinstance(value, str) and value and value not in items:
            items.append(value)

    def _normalize_triggered_rule(self, key: str, action: str, metric: str, actual: Any, operator: str, expected: Any, reason: str) -> Dict[str, Any]:
        return {
            'key': str(key or '?'),
            'status': 'triggered',
            'stage': 'conditional_rule',
            'action': str(action or 'watch'),
            'metric': str(metric or '?'),
            'actual': actual,
            'operator': str(operator or '=='),
            'value': expected,
            'reason': str(reason or ''),
        }

    def _build_adaptive_decision_audit(self, effective_thresholds: Dict[str, Any], adaptive_meta: Dict[str, Any]) -> Dict[str, Any]:
        adaptive_meta = adaptive_meta or {}
        return {
            'effective': {
                'mode': adaptive_meta.get('mode', 'observe_only'),
                'state': adaptive_meta.get('state', 'neutral'),
                'is_effective': bool(adaptive_meta.get('is_effective', False)),
                'thresholds': dict(effective_thresholds or {}),
                'overrides': copy.deepcopy(adaptive_meta.get('effective_overrides') or {}),
                'notes': list(adaptive_meta.get('notes') or []),
                'tags': list(adaptive_meta.get('tags') or []),
            },
            'applied': list(adaptive_meta.get('applied_overrides') or []),
            'ignored': copy.deepcopy(adaptive_meta.get('ignored_overrides') or []),
            'triggered': copy.deepcopy(adaptive_meta.get('triggered_rules') or []),
        }

    def _sync_adaptive_breakdown(self, breakdown: DecisionBreakdown, audit: Dict[str, Any]):
        audit = audit or {}
        effective = dict(audit.get('effective') or {})
        breakdown.adaptive_policy_mode = effective.get('mode', breakdown.adaptive_policy_mode)
        breakdown.adaptive_policy_state = effective.get('state', breakdown.adaptive_policy_state)
        breakdown.adaptive_policy_is_effective = bool(effective.get('is_effective', breakdown.adaptive_policy_is_effective))
        breakdown.adaptive_effective_thresholds = dict(effective.get('thresholds') or {})
        breakdown.adaptive_effective_overrides = copy.deepcopy(effective.get('overrides') or {})
        breakdown.adaptive_decision_notes = list(effective.get('notes') or [])
        breakdown.adaptive_decision_tags = list(effective.get('tags') or [])
        breakdown.adaptive_applied_overrides = list(audit.get('applied') or [])
        breakdown.adaptive_ignored_overrides = copy.deepcopy(audit.get('ignored') or [])
        breakdown.adaptive_triggered_rules = copy.deepcopy(audit.get('triggered') or [])
        breakdown.adaptive_decision_audit = {
            'effective': {
                'mode': breakdown.adaptive_policy_mode,
                'state': breakdown.adaptive_policy_state,
                'is_effective': breakdown.adaptive_policy_is_effective,
                'thresholds': dict(breakdown.adaptive_effective_thresholds or {}),
                'overrides': copy.deepcopy(breakdown.adaptive_effective_overrides or {}),
                'notes': list(breakdown.adaptive_decision_notes or []),
                'tags': list(breakdown.adaptive_decision_tags or []),
            },
            'applied': list(breakdown.adaptive_applied_overrides or []),
            'ignored': copy.deepcopy(breakdown.adaptive_ignored_overrides or []),
            'triggered': copy.deepcopy(breakdown.adaptive_triggered_rules or []),
        }

    def _build_effective_thresholds(self, policy_snapshot: Dict[str, Any]) -> tuple:
        effective_thresholds = dict(self.thresholds)
        policy_snapshot = policy_snapshot or {}
        effective_overrides = dict(policy_snapshot.get('effective_overrides') or {})
        decision_overrides = dict(policy_snapshot.get('decision_overrides') or {})
        applied_overrides: List[str] = []
        ignored_overrides: List[Dict[str, Any]] = []
        notes: List[str] = []
        tags: List[str] = []
        triggered_rules: List[Dict[str, Any]] = []
        is_effective = bool(policy_snapshot.get('is_effective', False))

        if not is_effective:
            return effective_thresholds, {
                'mode': policy_snapshot.get('mode', 'observe_only'),
                'state': policy_snapshot.get('state', 'neutral'),
                'is_effective': False,
                'effective_overrides': effective_overrides,
                'applied_overrides': [],
                'ignored_overrides': [],
                'triggered_rules': [],
                'notes': notes,
                'tags': tags,
            }

        for key, rule in self.CONSERVATIVE_THRESHOLD_RULES.items():
            if key not in decision_overrides:
                continue
            override_value = decision_overrides.get(key)
            base_value = effective_thresholds.get(key)
            if not isinstance(override_value, (int, float)):
                self._append_ignored_override(ignored_overrides, key, 'override is not numeric', override_value, stage='threshold_rule')
                continue
            if base_value is None:
                effective_thresholds[key] = override_value
                self._append_applied_override(applied_overrides, key)
                notes.append(f'{key}: adopt conservative threshold from adaptive override')
                continue
            if not isinstance(base_value, (int, float)):
                self._append_ignored_override(ignored_overrides, key, 'baseline is not numeric', override_value, stage='threshold_rule')
                continue
            final_value = max(base_value, override_value) if rule == 'max' else min(base_value, override_value)
            if final_value != base_value:
                effective_thresholds[key] = final_value
                self._append_applied_override(applied_overrides, key)
            else:
                self._append_ignored_override(ignored_overrides, key, 'override ignored because it would loosen baseline', override_value, stage='threshold_rule')
                notes.append(f'{key}: override ignored because it would loosen baseline')

        for tendency_key in ('downgrade_allow_to_watch', 'downgrade_watch_to_block'):
            if tendency_key not in decision_overrides:
                continue
            override_value = decision_overrides.get(tendency_key)
            if isinstance(override_value, bool):
                effective_thresholds[tendency_key] = override_value
                if override_value:
                    self._append_applied_override(applied_overrides, tendency_key)
                else:
                    self._append_ignored_override(ignored_overrides, tendency_key, 'override disabled explicitly', override_value, stage='decision_downgrade')
            else:
                self._append_ignored_override(ignored_overrides, tendency_key, 'override is not boolean', override_value, stage='decision_downgrade')

        note_tags = decision_overrides.get('decision_tags') or []
        if isinstance(note_tags, list):
            for tag in note_tags:
                self._append_unique_text(tags, tag)
            if tags:
                self._append_applied_override(applied_overrides, 'decision_tags')
        elif 'decision_tags' in decision_overrides:
            self._append_ignored_override(ignored_overrides, 'decision_tags', 'override is not a list', decision_overrides.get('decision_tags'), stage='decision_metadata')

        decision_notes = decision_overrides.get('decision_notes') or []
        if isinstance(decision_notes, list):
            for note in decision_notes:
                self._append_unique_text(notes, note)
            if decision_notes:
                self._append_applied_override(applied_overrides, 'decision_notes')
        elif 'decision_notes' in decision_overrides:
            self._append_ignored_override(ignored_overrides, 'decision_notes', 'override is not a list', decision_overrides.get('decision_notes'), stage='decision_metadata')

        effective_overrides.setdefault('decision', {})
        for key in applied_overrides:
            if key in effective_thresholds:
                effective_overrides['decision'][key] = effective_thresholds[key]
        if tags:
            effective_overrides['decision']['decision_tags'] = list(tags)
        if notes:
            effective_overrides['decision']['decision_notes'] = list(notes)

        return effective_thresholds, {
            'mode': policy_snapshot.get('mode', 'observe_only'),
            'state': policy_snapshot.get('state', 'neutral'),
            'is_effective': bool(policy_snapshot.get('is_effective', False)),
            'effective_overrides': effective_overrides,
            'applied_overrides': applied_overrides,
            'ignored_overrides': ignored_overrides,
            'triggered_rules': triggered_rules,
            'notes': notes,
            'tags': tags,
        }

    def _eval_signal_strength(self, signal, effective_thresholds: Optional[Dict[str, Any]] = None) -> tuple:
        strength = getattr(signal, 'strength', 0) or 0
        strategies = getattr(signal, 'strategies_triggered', []) or []
        reasons = getattr(signal, 'reasons', []) or []
        direction_score = getattr(signal, 'direction_score', {}) or {}
        net = float(direction_score.get('net', 0) or 0)
        buy_score = float(direction_score.get('buy', 0) or 0)
        sell_score = float(direction_score.get('sell', 0) or 0)
        dominant = max(buy_score, sell_score, 0.0)
        opposing = min(buy_score, sell_score) if buy_score and sell_score else 0.0
        conflict_ratio = (opposing / dominant) if dominant > 0 else 0.0

        mean_reversion_set = {'RSI', 'Bollinger', 'Pattern'}
        trend_following_set = {'MACD', 'MA_Cross', 'Volume'}
        mean_reversion_count = sum(1 for r in reasons if r.get('strategy') in mean_reversion_set)
        trend_following_count = sum(1 for r in reasons if r.get('strategy') in trend_following_set)
        ml_count = sum(1 for r in reasons if r.get('strategy') == 'ML')
        mean_reversion_bias = mean_reversion_count >= 2 and trend_following_count == 0

        score = 0
        strength_component = min(60, int(strength * 0.6))
        score += strength_component
        strategy_count = len(strategies)
        if strategy_count >= 3:
            score += 20
        elif strategy_count == 2:
            score += 15
        elif strategy_count == 1:
            score += 10
        net_component = min(20, max(0, int(net / 2)))
        score += net_component

        if conflict_ratio >= self._cfg('max_conflict_ratio_allow', 0.35, effective_thresholds):
            score -= min(18, int(conflict_ratio * 30))
        if mean_reversion_bias and ml_count > 0 and strength < 65:
            score -= 8

        score = max(0, min(100, score))
        extras = []
        if conflict_ratio >= self._cfg('max_conflict_ratio_allow', 0.35, effective_thresholds):
            extras.append(f"存在方向冲突({conflict_ratio:.0%})")
        if mean_reversion_bias:
            extras.append("偏抄底/摸顶型")
        extra_text = f"，{'；'.join(extras)}" if extras else ""
        if score >= 50:
            reason = f"信号强度{strength}，触发{strategy_count}个策略，净强度{net:.1f}{extra_text}"
        else:
            reason = f"信号强度偏弱({strength})，仅触发{strategy_count}个策略{extra_text}"

        setattr(signal, '_entry_signal_conflict_score', int(round(conflict_ratio * 100)))
        setattr(signal, '_entry_mean_reversion_bias', mean_reversion_bias)
        return score, reason

    def _eval_regime_alignment(self, signal) -> tuple:
        regime_info = getattr(signal, 'regime_info', {}) or {}
        regime = regime_info.get('regime', 'unknown')
        confidence = regime_info.get('regime_confidence', 0) or regime_info.get('confidence', 0)
        signal_type = getattr(signal, 'signal_type', 'hold')
        score = 50
        if regime == 'risk_anomaly' and confidence >= 0.5:
            score = 10
            reason = "检测到风险异常，市场可能剧烈波动"
        elif regime == 'high_vol' and confidence >= 0.5:
            if signal_type in ['buy', 'sell']:
                score = 45
                reason = f"当前高波动市场({confidence:.0%}置信度)，需谨慎"
            else:
                score = 50
                reason = "高波动市场，无明确方向"
        elif regime == 'low_vol' and confidence >= 0.5:
            score = 60
            reason = "低波动盘整市场，趋势不明确"
        elif regime == 'trend' and confidence >= 0.5:
            score = 75
            reason = f"趋势市场({regime})，置信度{confidence:.0%}"
        elif regime == 'range' and confidence >= 0.5:
            score = 60
            reason = "震荡市场，区间波动为主"
        else:
            score = 50
            reason = f"市场状态不明确(regime={regime})，置信度{confidence:.0%}"
        return score, reason

    def _eval_volatility_fitness(self, signal) -> tuple:
        market_context = getattr(signal, 'market_context', {}) or {}
        volatility = market_context.get('volatility', 0) or 0
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
            if volatility > 0.01:
                score = 75
                reason = f"波动适中({volatility:.4f})，存在交易机会"
            else:
                score = 60
                reason = f"波动偏低({volatility:.4f})，但尚可接受"
        return score, reason

    def _eval_trend_alignment(self, signal) -> tuple:
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
        elif (signal_type == 'buy' and trend == 'bullish') or (signal_type == 'sell' and trend == 'bearish'):
            score = 85
            reason = f"顺势交易({signal_type} + {trend})，胜率更高"
        elif (signal_type == 'buy' and trend == 'bearish') or (signal_type == 'sell' and trend == 'bullish'):
            score = 30
            reason = f"逆势交易({signal_type} + {trend})，风险较大"
        else:
            score = 50
            reason = f"趋势状态: {trend}"
        return score, reason

    def _eval_execution_risk(self, signal, current_positions: Dict, tracking_data: Dict) -> tuple:
        from datetime import datetime
        symbol = getattr(signal, 'symbol', '')
        signal_type = getattr(signal, 'signal_type', 'hold')
        score = 70
        watch_reasons = []
        side = 'long' if signal_type == 'buy' else 'short'
        existing = current_positions.get(symbol) if current_positions else {}
        if existing and existing.get('side') == side:
            score -= 40
            watch_reasons.append("已有同向持仓")
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
        if current_positions and len(current_positions) > 2:
            score -= 10
            watch_reasons.append(f"已持仓{len(current_positions)}个币种")
        score = max(0, min(100, score))
        return score, "，".join(watch_reasons) if watch_reasons else "执行条件良好"

    def _eval_ml_confidence(self, ml_prediction, signal) -> tuple:
        score = 50
        reason = "无ML模型数据"
        if ml_prediction is None:
            reasons = getattr(signal, 'reasons', []) or []
            ml_reasons = [r for r in reasons if r.get('strategy') == 'ML']
            if ml_reasons:
                ml_reason = ml_reasons[0]
                prob = ml_reason.get('value', 0) or 0
                action = ml_reason.get('action')
                signal_type = getattr(signal, 'signal_type', 'hold')
                if (action == 'buy' and signal_type == 'buy') or (action == 'sell' and signal_type == 'sell'):
                    score = int(prob * 100)
                    reason = f"ML预测{action}概率{prob:.0%}，与信号方向一致"
                else:
                    score = max(10, int((1 - prob) * 50))
                    reason = "ML预测方向与信号不一致"
            return score, reason
        pred, prob = ml_prediction
        min_conf = self._cfg('ml_min_confidence_allow', 0.6)
        signal_type = getattr(signal, 'signal_type', 'hold')
        if pred == 1 and prob >= min_conf:
            if signal_type == 'buy':
                score = int(prob * 100)
                reason = f"ML预测上涨概率{prob:.0%}，与做多信号一致"
            else:
                score = 30
                reason = f"ML预测上涨但信号是{signal_type}，方向冲突"
        elif pred == 0 and prob >= min_conf:
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

    def _eval_mtf_breakout(self, signal) -> tuple:
        market_context = getattr(signal, 'market_context', {}) or {}
        payload = dict(market_context.get('mtf_breakout') or {})
        score = int(payload.get('score', market_context.get('mtf_breakout_score', 0)) or 0)
        reason = str(payload.get('reason') or market_context.get('mtf_breakout_reason') or 'MTF breakout 无额外证据')
        observe_only = bool(payload.get('observe_only', market_context.get('mtf_breakout_observe_only', True)))
        return score, reason, observe_only

    def _calculate_total_score(self, breakdown: DecisionBreakdown) -> int:
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

    def _metric_map(self, breakdown: DecisionBreakdown, total_score: int) -> Dict[str, Any]:
        return {
            'total_score': total_score,
            'signal_strength_score': breakdown.signal_strength_score,
            'regime_alignment_score': breakdown.regime_alignment_score,
            'volatility_fitness_score': breakdown.volatility_fitness_score,
            'trend_alignment_score': breakdown.trend_alignment_score,
            'execution_risk_score': breakdown.execution_risk_score,
            'ml_confidence_score': breakdown.ml_confidence_score,
            'mtf_breakout_score': breakdown.mtf_breakout_score,
            'signal_conflict_score': breakdown.signal_conflict_score,
            'mean_reversion_bias': breakdown.mean_reversion_bias,
        }

    def _compare_rule(self, actual: Any, operator: str, expected: Any) -> bool:
        if operator == '>=':
            return actual >= expected
        if operator == '>':
            return actual > expected
        if operator == '<=':
            return actual <= expected
        if operator == '<':
            return actual < expected
        if operator == '==':
            return actual == expected
        if operator == '!=':
            return actual != expected
        return False

    def _evaluate_conditional_overrides(self, breakdown: DecisionBreakdown, total_score: int, effective_thresholds: Dict[str, Any], adaptive_meta: Dict[str, Any]) -> Dict[str, Any]:
        decision_overrides = dict(((adaptive_meta or {}).get('effective_overrides') or {}).get('decision') or {})
        raw_overrides = dict(((adaptive_meta or {}).get('effective_overrides') or {}).get('decision') or {})
        policy_snapshot_effective = raw_overrides
        # fallback to raw policy payload when effective_overrides does not carry conditional rules
        conditional_rules = []
        for key in ('conditional_overrides', 'force_watch_if', 'force_block_if'):
            if key in policy_snapshot_effective:
                pass
        metrics = self._metric_map(breakdown, total_score)
        triggered_rules = list(adaptive_meta.get('triggered_rules') or [])
        notes = list(adaptive_meta.get('notes') or [])
        tags = list(adaptive_meta.get('tags') or [])
        ignored_overrides = list(adaptive_meta.get('ignored_overrides') or [])
        applied_overrides = list(adaptive_meta.get('applied_overrides') or [])
        return {
            'metrics': metrics,
            'triggered_rules': triggered_rules,
            'notes': notes,
            'tags': tags,
            'ignored_overrides': ignored_overrides,
            'applied_overrides': applied_overrides,
        }

    def _apply_conditional_rules(self, breakdown: DecisionBreakdown, total_score: int, signal, policy_snapshot: Dict[str, Any], adaptive_meta: Dict[str, Any]) -> Dict[str, Any]:
        decision_overrides = dict(policy_snapshot.get('decision_overrides') or {})
        conditional_rules = decision_overrides.get('conditional_overrides') or []
        triggered_rules = list(adaptive_meta.get('triggered_rules') or [])
        notes = list(adaptive_meta.get('notes') or [])
        tags = list(adaptive_meta.get('tags') or [])
        ignored_overrides = list(adaptive_meta.get('ignored_overrides') or [])
        applied_overrides = list(adaptive_meta.get('applied_overrides') or [])
        watch_reasons: List[str] = []
        force_watch = False
        force_block = False
        metrics = self._metric_map(breakdown, total_score)
        metrics.update({
            'signal_type': getattr(signal, 'signal_type', 'hold'),
            'trend': (getattr(signal, 'market_context', {}) or {}).get('trend', 'sideways'),
        })

        if conditional_rules and not isinstance(conditional_rules, list):
            self._append_ignored_override(ignored_overrides, 'conditional_overrides', 'override is not a list', conditional_rules, stage='conditional_rule')
            conditional_rules = []

        for idx, rule in enumerate(conditional_rules):
            key = f'conditional_overrides[{idx}]'
            if not isinstance(rule, dict):
                self._append_ignored_override(ignored_overrides, key, 'rule is not a dict', rule, stage='conditional_rule')
                continue
            metric = rule.get('metric')
            operator = rule.get('operator', '<=')
            expected = rule.get('value')
            action = str(rule.get('action') or 'watch').lower()
            actual = metrics.get(metric)
            if metric not in metrics:
                self._append_ignored_override(ignored_overrides, key, 'metric not supported', metric, stage='conditional_rule')
                continue
            if operator not in {'>=', '>', '<=', '<', '==', '!='}:
                self._append_ignored_override(ignored_overrides, key, 'operator not supported', operator, stage='conditional_rule')
                continue
            if action not in {'watch', 'block'}:
                self._append_ignored_override(ignored_overrides, key, 'action must be watch or block', action, stage='conditional_rule')
                continue
            try:
                matched = self._compare_rule(actual, operator, expected)
            except Exception:
                self._append_ignored_override(ignored_overrides, key, 'rule comparison failed', rule, stage='conditional_rule')
                continue
            if not matched:
                self._append_ignored_override(ignored_overrides, key, 'condition not met', {'metric': metric, 'actual': actual, 'operator': operator, 'value': expected}, stage='conditional_rule')
                continue
            reason = str(rule.get('reason') or f'adaptive rule matched: {metric} {operator} {expected}')
            note = rule.get('note')
            tag = rule.get('tag')
            triggered_rules.append(self._normalize_triggered_rule(
                key=key,
                action=action,
                metric=metric,
                actual=actual,
                operator=operator,
                expected=expected,
                reason=reason,
            ))
            self._append_applied_override(applied_overrides, key)
            watch_reasons.append(reason)
            self._append_unique_text(notes, note)
            self._append_unique_text(tags, tag)
            if action == 'block':
                force_block = True
            else:
                force_watch = True

        return {
            'watch_reasons': watch_reasons,
            'force_watch': force_watch,
            'force_block': force_block,
            'triggered_rules': triggered_rules,
            'notes': notes,
            'tags': tags,
            'ignored_overrides': ignored_overrides,
            'applied_overrides': applied_overrides,
        }

    def _make_decision(self, breakdown: DecisionBreakdown, total_score: int,
                       signal, effective_thresholds: Optional[Dict[str, Any]] = None,
                       adaptive_meta: Optional[Dict[str, Any]] = None) -> tuple:
        watch_reasons = []
        market_context = getattr(signal, 'market_context', {}) or {}
        trend = market_context.get('trend', 'sideways')
        signal_type = getattr(signal, 'signal_type', 'hold')
        reasons = getattr(signal, 'reasons', []) or []
        adaptive_meta = adaptive_meta or {}

        if breakdown.signal_strength_score < 30:
            watch_reasons.append("信号强度不足")
        if breakdown.trend_alignment_score < 40:
            watch_reasons.append("逆趋势交易")
        if breakdown.volatility_fitness_score < 30:
            watch_reasons.append("波动环境不适合")
        if breakdown.execution_risk_score < 40:
            watch_reasons.append("执行风险较高")
        if breakdown.regime_alignment_score < 30:
            watch_reasons.append("市场状态不明确")
        if breakdown.signal_conflict_score >= int(self._cfg('max_conflict_ratio_allow', 0.35, effective_thresholds) * 100):
            watch_reasons.append("信号链路存在多空冲突")

        mean_reversion_strategies = {'RSI', 'Bollinger', 'Pattern'}
        trend_following_strategies = {'MACD', 'MA_Cross', 'Volume'}
        mean_reversion_only = breakdown.mean_reversion_bias and not any(
            r.get('strategy') in trend_following_strategies for r in reasons
        )
        ml_only_with_bollinger = all(r.get('strategy') in {'ML', 'Bollinger'} for r in reasons) and len(reasons) <= 2
        strategy_count = len(getattr(signal, 'strategies_triggered', []) or [])

        buy_rsi_values = [float(r.get('value') or 0) for r in reasons if r.get('strategy') == 'RSI' and r.get('action') == 'buy']
        sell_rsi_values = [float(r.get('value') or 100) for r in reasons if r.get('strategy') == 'RSI' and r.get('action') == 'sell']
        has_bollinger = any(r.get('strategy') == 'Bollinger' for r in reasons)
        has_opposing_volume = any(r.get('strategy') == 'Volume' and r.get('action') != signal_type for r in reasons)
        high_conflict = breakdown.signal_conflict_score >= int(self._cfg('max_conflict_ratio_allow', 0.35, effective_thresholds) * 100)
        falling_knife_buy = (
            signal_type == 'buy' and trend in ['sideways', 'bearish'] and has_bollinger and
            any(val and val <= float(self._cfg('falling_knife_rsi_threshold', 30, effective_thresholds)) for val in buy_rsi_values)
        )
        falling_knife_sell = (
            signal_type == 'sell' and trend in ['sideways', 'bullish'] and has_bollinger and
            any(val and val >= (100 - float(self._cfg('falling_knife_rsi_threshold', 30, effective_thresholds))) for val in sell_rsi_values)
        )

        conservative_allow_guards = [
            ('min_signal_strength_for_allow', breakdown.signal_strength_score, '信号强度未达到 adaptive 保守阈值'),
            ('min_regime_alignment_for_allow', breakdown.regime_alignment_score, 'regime 适配度未达到 adaptive 保守阈值'),
            ('min_volatility_fitness_for_allow', breakdown.volatility_fitness_score, '波动环境未达到 adaptive 保守阈值'),
            ('min_trend_alignment_for_allow', breakdown.trend_alignment_score, '趋势顺势度未达到 adaptive 保守阈值'),
            ('min_execution_risk_for_allow', breakdown.execution_risk_score, '执行风险评分未达到 adaptive 保守阈值'),
            ('min_ml_confidence_for_allow', breakdown.ml_confidence_score, 'ML 置信度未达到 adaptive 保守阈值'),
        ]
        for key, actual, reason in conservative_allow_guards:
            threshold = effective_thresholds.get(key)
            if isinstance(threshold, (int, float)) and actual < threshold:
                watch_reasons.append(f'{reason}({actual}<{threshold})')

        max_conflict_score_for_allow = effective_thresholds.get('max_signal_conflict_score_for_allow')
        if isinstance(max_conflict_score_for_allow, (int, float)) and breakdown.signal_conflict_score > max_conflict_score_for_allow:
            watch_reasons.append(f'信号冲突分超出 adaptive 保守阈值({breakdown.signal_conflict_score}>{max_conflict_score_for_allow})')

        conditional_result = self._apply_conditional_rules(
            breakdown,
            total_score,
            signal,
            getattr(signal, 'adaptive_policy_snapshot', {}) or {},
            adaptive_meta,
        )
        for reason in conditional_result['watch_reasons']:
            if reason not in watch_reasons:
                watch_reasons.append(reason)
        self._sync_adaptive_breakdown(breakdown, {
            'effective': {
                'mode': breakdown.adaptive_policy_mode,
                'state': breakdown.adaptive_policy_state,
                'is_effective': breakdown.adaptive_policy_is_effective,
                'thresholds': breakdown.adaptive_effective_thresholds,
                'overrides': breakdown.adaptive_effective_overrides,
                'notes': conditional_result['notes'],
                'tags': conditional_result['tags'],
            },
            'applied': conditional_result['applied_overrides'],
            'ignored': conditional_result['ignored_overrides'],
            'triggered': conditional_result['triggered_rules'],
        })

        if strategy_count <= 1 and getattr(signal, 'strength', 0) <= self._cfg('single_strategy_block_max_strength', 24, effective_thresholds) and total_score <= self._cfg('single_strategy_block_max_score', 64, effective_thresholds):
            watch_reasons.append("单策略弱信号，缺少交叉确认")
            return EntryDecision.BLOCK.value, watch_reasons

        if signal_type in ['buy', 'sell'] and trend == 'sideways' and high_conflict:
            watch_reasons.append("横盘中多空信号打架")
            if has_opposing_volume or total_score < self._cfg('high_conflict_watch_score_min', 68, effective_thresholds):
                if has_opposing_volume:
                    watch_reasons.append("量能方向与入场方向相反")
                return EntryDecision.BLOCK.value, watch_reasons
            return EntryDecision.WATCH.value, watch_reasons

        if falling_knife_buy or falling_knife_sell:
            watch_reasons.append("疑似接飞刀/摸顶，等待止跌或顺势确认")
            if has_opposing_volume or total_score <= self._cfg('falling_knife_block_max_score', 72, effective_thresholds):
                return EntryDecision.BLOCK.value, watch_reasons
            return EntryDecision.WATCH.value, watch_reasons

        if signal_type in ['buy', 'sell'] and trend == 'sideways' and mean_reversion_only:
            if total_score <= self._cfg('sideways_mean_reversion_watch_max_score', 72, effective_thresholds):
                watch_reasons.append("横盘中的抄底/摸顶信号，确认度不足")
                return EntryDecision.WATCH.value, watch_reasons

        if signal_type in ['buy', 'sell'] and trend == 'sideways' and ml_only_with_bollinger and total_score <= self._cfg('sideways_ml_only_watch_max_score', 60, effective_thresholds):
            watch_reasons.append("仅 ML + Bollinger 确认，缺少趋势/量能确认")
            return EntryDecision.WATCH.value, watch_reasons

        block_score_max = self._cfg('block_score_max', 35, effective_thresholds)
        allow_score_min = self._cfg('allow_score_min', 68, effective_thresholds)

        if conditional_result['force_block']:
            if breakdown.adaptive_applied_overrides:
                watch_reasons.append('adaptive overrides 生效: ' + ', '.join(breakdown.adaptive_applied_overrides))
            return EntryDecision.BLOCK.value, watch_reasons

        if total_score <= block_score_max or (
            breakdown.trend_alignment_score < 35 and
            breakdown.volatility_fitness_score < 30 and
            len(watch_reasons) >= 2
        ):
            return EntryDecision.BLOCK.value, watch_reasons

        decision = EntryDecision.WATCH.value if total_score < allow_score_min or len(watch_reasons) >= 1 else EntryDecision.ALLOW.value

        if decision == EntryDecision.ALLOW.value and conditional_result['force_watch']:
            decision = EntryDecision.WATCH.value
        if decision == EntryDecision.ALLOW.value and effective_thresholds.get('downgrade_allow_to_watch'):
            watch_reasons.append('adaptive regime 保守降级：allow→watch')
            decision = EntryDecision.WATCH.value
        elif decision == EntryDecision.WATCH.value and not watch_reasons and effective_thresholds.get('downgrade_watch_to_block'):
            watch_reasons.append('adaptive regime 保守降级：watch→block')
            decision = EntryDecision.BLOCK.value

        if decision != EntryDecision.ALLOW.value and breakdown.adaptive_applied_overrides:
            watch_reasons.append('adaptive overrides 生效: ' + ', '.join(breakdown.adaptive_applied_overrides))

        return decision, watch_reasons

    def _generate_summary(self, decision: str, breakdown: DecisionBreakdown, signal) -> str:
        strength = getattr(signal, 'strength', 0)
        total_hint = (
            breakdown.signal_strength_score * 0.25 +
            breakdown.regime_alignment_score * 0.15 +
            breakdown.volatility_fitness_score * 0.15 +
            breakdown.trend_alignment_score * 0.20 +
            breakdown.execution_risk_score * 0.15 +
            breakdown.ml_confidence_score * 0.10
        )
        total_hint = int(round(total_hint))
        adaptive_hint = ''
        if breakdown.adaptive_applied_overrides:
            adaptive_hint = f" Adaptive={','.join(breakdown.adaptive_applied_overrides)}。"
        ignored_hint = ''
        if breakdown.adaptive_ignored_overrides:
            ignored_hint = f" Ignored={','.join([item.get('key', '?') for item in breakdown.adaptive_ignored_overrides[:3]])}。"
        tag_hint = ''
        if breakdown.adaptive_decision_tags:
            tag_hint = f" Tags={','.join(breakdown.adaptive_decision_tags[:3])}。"
        if decision == EntryDecision.BLOCK.value:
            return (f"信号被拦截：总分{total_hint}分，"
                    f"{breakdown.trend_alignment_reason}，{breakdown.volatility_fitness_reason}。"
                    f"建议等待条件改善后再评估。{adaptive_hint}{ignored_hint}{tag_hint}")
        elif decision == EntryDecision.WATCH.value:
            return (f"信号建议观望：总分{total_hint}分，"
                    f"信号强度{strength}，趋势{alignment_to_chinese(breakdown.trend_alignment_score)}，"
                    f"波动{alignment_to_chinese(breakdown.volatility_fitness_score)}。"
                    f"观察标签:{breakdown.observe_only_phase}/{breakdown.observe_only_state}。{adaptive_hint}{ignored_hint}{tag_hint}"
                    f"建议继续观察确认。")
        else:
            return (f"信号允许开单：总分{total_hint}分，"
                    f"{breakdown.trend_alignment_reason}，{breakdown.volatility_fitness_reason}。"
                    f"当前条件适合入场。{adaptive_hint}{ignored_hint}{tag_hint}")


def alignment_to_chinese(score: int) -> str:
    if score >= 70:
        return "有利"
    elif score >= 50:
        return "一般"
    else:
        return "不利"


__all__ = ['EntryDecider', 'EntryDecision', 'EntryDecisionResult', 'DecisionBreakdown']
