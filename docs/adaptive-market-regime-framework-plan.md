# 市场状态自适应框架重构方案（渐进式接入版）

> 目标不是“换一套新策略”，而是在**现有 crypto-quant-okx 系统之上**，加一层可观测、可回测、可灰度、可回滚的 **Market Regime Adaptive Framework**，让现有信号、审批、执行、风控、分仓与通知链条，能按市场状态做轻量自适应。

---

## 1. 背景与目标

当前系统已经具备较完整的交易主链路：

- `signals/`：`detector.py` 负责多策略信号检测，已带 `market_context` 与 `regime_info`
- `signals/entry_decider.py`：已有开单决策层，对信号强度、趋势、波动、regime 做评分
- `signals/validator.py`：已有验证层，会结合风险预算、波动过滤、regime filter 做放行/拦截
- `trading/executor.py`：已有执行时风控、direction lock、layering、open intent、observability
- `analytics/backtest.py`：已有基础回测与 signal quality 分析
- `ml/engine.py`：已有 ML 预测与特征框架
- `docs/`：已有 layering、发布、验收等工程化文档

这说明项目并不是“从 0 到 1”，而是已经有了：

1. **轻量 regime 能力雏形**（`core/regime.py`）
2. **信号→审批→验证→执行** 的分层链路
3. **风险预算 + layering + 对账/自愈 + observability** 的工程骨架

因此本方案的重点是：

- 不推翻现有策略与交易链路
- 不直接把 regime 做成“新的下单策略”
- 而是把 regime 提升为一层**横切控制平面**，统一影响：
  - 信号加权
  - 进场评分
  - 验证阈值
  - 执行参数
  - 风险预算
  - 回测分桶
  - 可观测性与复盘

### 目标

1. 让系统能明确区分不同市场状态，并对现有链路做**有限、可解释的自适应**
2. 所有自适应动作都应可通过配置启停，不搞写死魔法行为
3. 保留当前 `layering / risk budget / notification / dashboard / reconcile` 主链路
4. 支持长期迭代：先观测，再半自动建议，再小范围放开，最后才考虑更强自动化

---

## 2. 明确不做什么

为避免方案失控，先明确边界。

### 本方案**不做**

1. **不替换现有策略集**
   - 继续沿用 `RSI / MACD / MA_Cross / Bollinger / Volume / Pattern / ML`
2. **不把 regime 当成独立开仓策略**
   - regime 只决定“当前市场更适合谁、更不适合谁、阈值该多严”
3. **不一次性重写 validator / executor / backtest**
   - 只做渐进式接入点扩展
4. **不推翻当前分仓、方向锁、风险预算、自愈与通知链**
   - 这些是现有系统稳定性的基础
5. **不在本阶段直接上复杂在线学习 / 自动再训练 / 自更新参数**
   - 本阶段先做配置驱动 + 数据闭环 + 离线校准
6. **不承诺 regime 一定提高收益**
   - 首要目标是减少错配、提升可解释性与回滚能力，不是 PPT 式“收益倍增”

---

## 3. 当前现状评估（基于现有代码）

## 3.1 已有能力

### A. Regime 雏形已存在，但还偏“点状接入”

`core/regime.py` 当前定义了：

- `trend`
- `range`
- `high_vol`
- `low_vol`
- `risk_anomaly`
- `unknown`

并基于 EMA gap、volatility、ATR ratio、price spike、volume spike 做轻量分类。

### B. SignalDetector 已开始使用 regime

`signals/detector.py` 已经：

- 在 `Signal` 中带 `regime_info`
- 调用 `detect_regime(...)`
- 将 regime 同步进 `signal.market_context`
- 在 `_apply_regime_weighting()` 中用趋势/震荡对策略强弱做乘数调整

但这里仍偏“局部 heuristic”，尚未升级为可统一配置、可审计、可分阶段启停的框架。

### C. EntryDecider 已具备 regime-aware 评分基础

`signals/entry_decider.py` 已把开单决策拆成多个分数：

- signal strength
- regime alignment
- volatility fitness
- trend alignment
- execution risk
- ml confidence

这非常适合作为 regime framework 的**审批层接入点**。

### D. Validator / Executor 已具备硬边界能力

`signals/validator.py` 和 `trading/executor.py` 已经具备：

- 风险预算
- 冷却
- 总暴露 / 单币种暴露
- market filter
- regime filter
- execution-time guard
- layering / idempotency / direction lock
- observability context

这意味着 regime framework 不需要“亲自下单”，只需要决定：

- 什么时候更严
- 什么时候放宽
- 放宽/收紧哪些参数
- 调整结果如何落进已有执行框架

### E. Analytics / ML 有基础，但未形成 regime 闭环

目前 `analytics/backtest.py` 和 `ml/engine.py` 已能提供：

- 基础历史回测
- signal quality 统计
- ML 预测

但还缺：

- 按 regime 分桶回测
- regime 切片下的胜率/收益/回撤
- 不同 regime 下各策略权重建议
- regime 漂移检测

---

## 4. 总体架构

建议把“市场状态自适应”做成一个**横切框架层**，而不是塞进某一个模块里乱长逻辑。

```text
Market Data
   ↓
Regime Engine（状态识别 / 置信度 / 特征快照）
   ↓
Regime Policy（不同状态下的策略偏好、阈值、风险限制）
   ↓
┌─────────────────────────────────────────────┐
│ Signal Layer                                │
│ signals/detector.py                         │
│ - 生成原始策略理由                          │
│ - 根据 regime policy 调整 strength/weight   │
└─────────────────────────────────────────────┘
   ↓
┌─────────────────────────────────────────────┐
│ Decision Layer                              │
│ signals/entry_decider.py                    │
│ - 按 regime 解释信号是否值得开单            │
│ - 输出 allow/watch/block + breakdown        │
└─────────────────────────────────────────────┘
   ↓
┌─────────────────────────────────────────────┐
│ Approval / Validation Layer                 │
│ signals/validator.py + RiskManager          │
│ - 根据 regime 收紧/放宽风控阈值             │
│ - 决定是否进入执行层                        │
└─────────────────────────────────────────────┘
   ↓
┌─────────────────────────────────────────────┐
│ Execution Layer                             │
│ trading/executor.py                         │
│ - layering、direction lock、intent、执行时风控 │
│ - regime 只影响参数，不改执行骨架           │
└─────────────────────────────────────────────┘
   ↓
Analytics / Dashboard / Notifications / Backtest
```

### 核心原则

1. **Regime Engine 负责识别，不负责决策**
2. **Regime Policy 负责映射规则，不直接改交易所行为**
3. **Decision / Validator / Executor 继续各司其职**
4. **所有适配优先通过配置与 policy 注入，不要散落硬编码**

---

## 5. 市场状态（Regime）定义

当前 `core/regime.py` 的枚举可作为 v1 起点，但建议从“状态名”进一步升级到“状态语义 + 可行动约束”。

## 5.1 建议保留的 v1 regime 集合

| Regime | 含义 | 对现有系统的主要影响 |
|---|---|---|
| `trend_up` | 趋势上行 | 偏多信号更宽松，逆势空头更保守 |
| `trend_down` | 趋势下行 | 偏空信号更宽松，逆势多头更保守 |
| `range` | 区间震荡 | 均值回归类策略可保留，追涨杀跌更保守 |
| `low_vol` | 波动不足 | 降低出手频率，避免把噪音当趋势 |
| `high_vol` | 高波动趋势/高振幅 | 降低仓位、提高确认阈值 |
| `risk_anomaly` | 异常波动/异常成交量/跳变 | 默认只观测或强拦截 |
| `unknown` | 数据不足/状态不稳 | 回退旧逻辑或保守模式 |

### 为什么建议把 `trend` 拆成 `trend_up / trend_down`

当前代码里的 `trend` + `ema_direction` 可表达方向，但在配置和回测统计上不够直观。

拆开后好处：

- 配置更清晰
- 回测分桶更自然
- Dashboard 更好读
- 任务拆分更容易

### v1.5 可增加但不急着上生产的维度

- `trend_weak_up / trend_strong_up`
- `trend_weak_down / trend_strong_down`
- `range_tight / range_wide`
- `event_risk`（大事件窗口）
- `liquidity_thin`（低流动性）

这些先不要在第一阶段落地，避免状态数膨胀导致配置与回测一起爆炸。

## 5.2 Regime 输出结构建议

建议统一输出为：

```yaml
regime:
  name: trend_up
  family: trend
  direction: up
  confidence: 0.78
  stability_score: 0.66
  transition_risk: 0.22
  features:
    ema_gap: 0.021
    volatility: 0.014
    atr_ratio: 0.012
    volume_ratio: 1.34
  detected_at: 2026-03-26T15:00:00
  detector_version: regime_v2
```

### 新增两个关键字段

1. `stability_score`
   - 当前状态是否稳定，避免刚切换状态就激进调参
2. `transition_risk`
   - 状态边界模糊时，Decision/Validator 可以自动更保守

---

## 6. 关键重构：从“点状 regime 调整”升级到“统一 policy 驱动”

### 问题

目前 regime 逻辑散落在：

- `signals/detector.py` 的 `_apply_regime_weighting`
- `signals/entry_decider.py` 的 `_eval_regime_alignment`
- `signals/validator.py` 的 regime filter

问题不是这些逻辑不能用，而是：

- 规则分散，后续维护易打架
- 很难回测“某条 regime 规则改了后影响了什么”
- 很难在不同层保持一致口径

### 建议

增加一个统一模块，例如：

- `core/regime_policy.py`
- 或 `signals/regime_policy.py`

职责：

1. 输入：`regime_result + symbol + config + signal_context`
2. 输出：统一 policy snapshot，例如：

```yaml
policy:
  signal_weight_overrides:
    RSI: 1.08
    Bollinger: 1.12
    MACD: 0.92
  decision_overrides:
    allow_score_min: 72
    high_conflict_watch_score_min: 66
  validation_overrides:
    block_counter_trend: true
    min_strategy_count: 2
    min_strength: 24
  execution_overrides:
    layer_ratios: [0.04, 0.04, 0.03]
    layer_max_total_ratio: 0.11
    leverage_cap: 5
  risk_overrides:
    total_margin_cap_ratio: 0.22
    symbol_margin_cap_ratio: 0.10
  mode: conservative
```

这会成为整个框架的核心：

- Detector 只读 `signal_weight_overrides`
- EntryDecider 只读 `decision_overrides`
- Validator 只读 `validation_overrides`
- RiskManager / Executor 只读 `risk_overrides` / `execution_overrides`

这样就不会各处自己发明一套 regime 逻辑。

---

## 7. 信号层如何逐步接入

## 7.1 当前接入点

`signals/detector.py` 已有非常适合的落点：

- `signal.market_context`
- `signal.regime_info`
- `_apply_regime_weighting`

## 7.2 建议改造方向

### Phase A：从硬编码乘数改为 policy 驱动

当前：

- bullish/bearish/sideways → 手工决定 trend_following / mean_reversion 的 multiplier

建议：

- 由 `RegimePolicy` 提供每个策略的 multiplier
- `detector.py` 只做读取和应用

### Phase B：记录“原始分数 + 调整后分数”

建议在 `reason.metadata` 中保留：

- `base_strength`
- `regime_multiplier`
- `adjusted_strength`
- `regime_name`
- `policy_version`

目的：方便回测和复盘，不然日后你只会见到一个被改过的 strength，却不知道为什么变了。

### Phase C：支持按 regime 的最小触发门槛

例如：

- `high_vol` 下 Volume/MACD 需要更高确认度
- `low_vol` 下 MA cross 基本不值得积极入场
- `range` 下 Bollinger/RSI 可保留，但要结合冲突度

注意：这里不是禁用策略，而是“提高进入最终 signal.reasons 的门槛”。

---

## 8. 审批层如何逐步接入

审批层主要是 `signals/entry_decider.py`。

这是最适合承接 regime framework 的地方，因为它天然负责“值不值得开”。

## 8.1 建议新增能力

### A. 决策阈值改为可按 regime 覆盖

例如：

- `allow_score_min`
- `block_score_max`
- `max_conflict_ratio_allow`
- `falling_knife_block_max_score`

这些阈值现在主要是全局常量/配置，建议支持：

```yaml
entry_decider:
  regime_overrides:
    range:
      allow_score_min: 72
      max_conflict_ratio_allow: 0.30
    trend_up:
      allow_score_min: 66
    risk_anomaly:
      allow_score_min: 90
      force_decision: block
```

### B. 增加“状态切换期惩罚”

如果 `transition_risk` 高：

- 决策总分直接扣分
- 或强制 allow → watch

这样可以避免 regime 在临界区来回闪烁时，系统反复追单。

### C. 决策输出写清“regime 为什么影响这次单”

建议在 `breakdown` 里增加：

- `regime_name`
- `regime_confidence`
- `regime_stability_score`
- `regime_transition_risk`
- `policy_mode`
- `regime_penalties`
- `regime_bonuses`

这样之后 dashboard 和回测才能复盘。

---

## 9. 验证层如何逐步接入

验证层主要是 `signals/validator.py`，外加 `RiskManager.can_open_position()`。

## 9.1 原则

Validator 不要再自己判断“市场好不好”，而是消费 policy 给出的约束。

### 建议拆分为两类规则

#### 1) Hard block（硬拦截）

适合：

- `risk_anomaly`
- `unknown + confidence too low`
- `transition_risk` 极高
- `high_vol + 当前暴露已高`

#### 2) Soft tighten（软收紧）

适合：

- `min_strength` 提高
- `min_strategy_count` 提高
- `block_counter_trend` 强化
- 冷却时间拉长

## 9.2 现有 validator 可利用的接入点

当前 `SignalValidator.validate()` 已有：

- `market_filters`
- `regime_filters`
- `strategies.composite.min_strategy_count`
- `strategies.composite.min_strength`

建议别新造一套平行逻辑，而是让 regime policy 在运行时生成“有效阈值快照”，再把快照写进 `details`。

例如：

```yaml
validation_effective_config:
  min_strength: 26
  min_strategy_count: 2
  block_counter_trend: true
  block_high_volatility: true
  policy_source: regime_policy_v1
```

这样：

- 验证逻辑仍走现有骨架
- 但使用的是 regime 调整后的配置

---

## 10. 执行层如何逐步接入

执行层重点不是“让 regime 控制下单方向”，而是让 regime 影响**执行强度与风险边界**。

## 10.1 现有执行层保留不动的部分

以下能力建议保持原样，仅允许参数被 policy 覆盖：

- `direction lock`
- `open intents`
- `execution-time guard`
- `layer plan`
- `layer_no / root_signal_id / observability`
- 对账、自愈、stale close

## 10.2 regime 对执行层的建议影响点

### A. 分仓参数动态收紧

例如：

- `trend_up/down`：维持当前 `[0.06, 0.06, 0.04]`
- `range`：改为 `[0.05, 0.04, 0.03]`
- `high_vol`：改为 `[0.04, 0.03, 0.02]`
- `risk_anomaly`：禁止新开仓

### B. 风险预算动态收紧

现有 `core.risk_budget` 已很适合承接这一层。

建议允许 policy 覆盖：

- `base_entry_margin_ratio`
- `total_margin_cap_ratio`
- `symbol_margin_cap_ratio`
- `soft_cap`
- `leverage_cap`

### C. 追踪止损 / 部分止盈保守度

在后续阶段可以考虑：

- `high_vol`：更早触发 trailing activation
- `range`：更积极部分止盈
- `trend_up/down`：给趋势单更宽 trailing distance

但这个属于 M2/M3 以后的增强项，M1 不建议先动，以免执行结果解释太复杂。

---

## 11. 配置设计

建议新增统一配置段，避免 regime 配置散落各处。

## 11.1 顶层结构建议

```yaml
adaptive_regime:
  enabled: true
  mode: observe_only   # observe_only / decision_only / guarded_execute / full
  detector:
    version: v2
    min_confidence: 0.55
    min_stability_score: 0.50
    cooloff_bars_after_switch: 2
  regimes:
    trend_up:
      signal_weight_overrides:
        MACD: 1.12
        MA_Cross: 1.10
        RSI: 0.92
      decision_overrides:
        allow_score_min: 66
      risk_overrides:
        total_margin_cap_ratio: 0.30
        symbol_margin_cap_ratio: 0.14
      execution_overrides:
        layer_ratios: [0.06, 0.06, 0.04]
    range:
      signal_weight_overrides:
        RSI: 1.08
        Bollinger: 1.10
        MACD: 0.90
      decision_overrides:
        allow_score_min: 72
        max_conflict_ratio_allow: 0.30
      risk_overrides:
        total_margin_cap_ratio: 0.24
        symbol_margin_cap_ratio: 0.10
      execution_overrides:
        layer_ratios: [0.05, 0.04, 0.03]
    risk_anomaly:
      force_mode: block_new_entry
```

## 11.2 运行模式建议

### `observe_only`

- 识别 regime
- 生成 policy snapshot
- 写入日志 / DB / dashboard
- **不改变实际决策**

用于先建立数据基线。

### `decision_only`

- 影响 EntryDecider
- 不改变执行层参数
- 适合先验证“审批质量是否改善”

### `guarded_execute`

- 可影响 validator / risk budget / layer ratios
- 但只允许保守方向调整
- 禁止放大总暴露

### `full`

- 所有链路按 policy 生效
- 仅在前面阶段数据充分后考虑

## 11.3 配置优先级建议

```text
symbol_override.adaptive_regime
> adaptive_regime.regimes[regime_name]
> adaptive_regime.defaults
> 现有 trading / strategies / market_filters 默认值
```

这样就能兼容当前项目已有的 `symbol_overrides` 体系。

---

## 12. 可观测性设计

这部分非常关键。没有 observability，regime 只会变成“又多一层玄学”。

## 12.1 信号级观测

建议每条 signal 至少记录：

- `regime_name`
- `regime_confidence`
- `stability_score`
- `transition_risk`
- `policy_version`
- `policy_mode`
- `base_signal_strength`
- `adjusted_signal_strength`
- `strategy_weight_changes`
- `decision_threshold_overrides`
- `validation_overrides`
- `execution_overrides`

现有 `Signal.filter_details` / `observability` 非常适合继续承载。

## 12.2 Dashboard 建议新增视图

### A. Regime Snapshot 卡片

显示：

- 当前 symbol 的 regime
- confidence / stability / transition risk
- 当前 policy mode
- 当前 effective layer ratios / risk caps

### B. Regime Distribution 面板

最近 24h / 7d：

- 各 regime 占比
- regime 切换频率
- regime 下的 allow/watch/block 分布

### C. Regime Performance 面板

按 regime 展示：

- signal 数量
- allow rate
- execution rate
- win rate
- avg return
- max drawdown
- avg hold time

### D. Regime Transition 日志

记录：

- `range -> trend_up`
- `trend_up -> high_vol`
- `high_vol -> risk_anomaly`

并标出切换时系统做了什么策略收紧。

## 12.3 通知建议

仅建议发送**状态变化通知**，不要每个周期都刷屏：

- regime 切换
- 进入 `risk_anomaly`
- `adaptive_regime.mode` 从 observe 升级到 guarded_execute
- 由于 regime 收紧而 block 的高分信号

---

## 13. 回测与验证方案

这是整个方案能否长期推进的关键。

## 13.1 回测目标

不是只看总收益，而是回答：

1. regime 能否减少明显错配交易？
2. 在哪些状态下，哪些策略更有效？
3. regime policy 改动后，收益/回撤/交易数如何变化？
4. regime 是否只是“减少交易”，还是“减少坏交易”？

## 13.2 回测应新增的维度

### A. Regime-aware Backtest

在 `analytics/backtest.py` 基础上增加：

- 每笔交易的开仓 regime / 平仓 regime
- 按 regime 分桶统计
- policy 前后对比

### B. Signal Quality by Regime

在 signal quality 里增加：

- `avg_quality_pct by regime`
- `positive_rate by regime`
- `strategy x regime` 二维统计

### C. Policy A/B 比较

至少支持：

- baseline：关闭 adaptive regime
- observe-only：只打标签不生效
- policy-v1：只影响 decision
- policy-v2：影响 decision + validator + risk

### D. 切换期损耗分析

检查 regime 切换前后 N 根 bar 内：

- 假信号密度
- allow→loss 的比例
- 被拦截后实际上变好的比例（避免过度保守）

## 13.3 验证指标建议

| 指标 | 说明 |
|---|---|
| trade_count_delta | 交易数变化 |
| allow_rate_delta | allow 比率变化 |
| execution_rate_delta | 实际执行率变化 |
| win_rate_delta | 胜率变化 |
| avg_return_delta | 单笔平均收益变化 |
| max_drawdown_delta | 最大回撤变化 |
| bad_trade_reduction | 坏交易减少比例 |
| missed_good_trade_ratio | 错过好交易比例 |
| regime_switch_noise | 状态切换噪音 |

---

## 14. 分阶段里程碑

## M0：文档与观测基线

### 目标

先统一方案、字段、配置和观测，不改交易行为。

### 工作项

- 统一 regime 命名与输出结构
- 设计 `adaptive_regime` 配置结构
- 在 signal / decision / validator / executor 里预留 policy snapshot 字段
- dashboard 增加基础 regime 展示位（可先只 API）
- 回测脚本支持记录 regime tag

### 产出

- 文档落地
- 字段定义明确
- 不影响实盘行为

### 验收

- 关闭 adaptive regime 时，现有交易行为无变化
- 已能在日志/DB 看到统一 regime snapshot

---

## M1：Observe Only（只观测不干预）

### 目标

让所有链路都能看到同一份 regime + policy snapshot，但不改变 allow/block/执行结果。

### 工作项

- 引入 `RegimePolicy` 生成器
- Detector / EntryDecider / Validator / Executor 均记录 effective policy
- analytics 支持按 regime 分桶
- dashboard 增加 regime distribution

### 验收

- 所有新字段稳定写入
- 系统行为与 adaptive regime 关闭时一致
- 能跑出“不同 regime 下信号质量差异”初步统计

---

## M2：Decision Only（先改审批，不动执行）

### 目标

只让 regime 影响 EntryDecider 的评分与 allow/watch/block。

### 工作项

- `allow_score_min` 等阈值支持 regime override
- 支持切换期惩罚 / stability gate
- 输出更详细的 decision breakdown

### 验收

- `observe_only` 与 `decision_only` 可一键切换
- 回测能比较 baseline vs decision_only
- allow/block 变化可解释、可追溯

---

## M3：Guarded Validation & Risk（影响验证和风险，不推翻执行骨架）

> M3 实施边界与回滚策略详见：[`docs/adaptive-market-regime-m3-boundary-plan.md`](./adaptive-market-regime-m3-boundary-plan.md)

### 目标

让 regime 影响 validator 与 risk budget，但只允许朝更保守方向调整。

### 工作项

- validator 支持 effective thresholds snapshot
- risk budget 支持 regime-based cap override
- high_vol / risk_anomaly 下限制 layer ratios / exposure

### 验收

- 执行层骨架不变
- `direction_lock / intents / layering / reconcile` 不受破坏
- 高风险状态下仓位明显收紧

---

## M4：Execution Parameter Adaptation（轻度执行参数自适应）

### 目标

在风险受控前提下，让 layer ratios / trailing / partial TP 轻量自适应。

### 工作项

- layer ratios 按 regime 动态覆盖
- trailing / partial TP 允许 regime profile 化
- 加强 dashboard 的 effective execution profile 展示

### 验收

- 参数变化可观测、可解释、可回滚
- 不出现“同一笔单到底按哪套规则执行”说不清的情况

---

## M5：Policy Calibration Loop（离线校准闭环）

### 目标

让 analytics / ML 为 regime policy 提供离线建议，但仍由人工确认上线。

### 工作项

- 输出 `strategy x regime` 统计报告
- 输出 regime-based 参数建议
- 支持 policy version 对比

### 验收

- 每次 policy 更新都有数据依据
- 支持版本化回退

---

## 15. 验收标准

## 15.1 架构验收

- Regime 识别、Policy、Decision、Validation、Execution 职责清晰
- 没有把大量 regime if/else 散落回各模块
- adaptive regime 可按模式启停

## 15.2 行为验收

- 关闭框架时，现有行为与当前主线一致
- observe_only 不改变交易结果
- decision_only 只改变审批结果，不改执行骨架
- guarded_execute 只允许更保守，不允许超配放大

## 15.3 数据验收

- 每个 signal 都能追溯 regime/policy/effective thresholds
- analytics 能出 regime 分桶统计
- dashboard 能展示当前 effective regime profile

## 15.4 风险验收

- 不破坏现有分仓、方向锁、对账自愈
- 不增加重复开仓、状态撕裂、孤儿 intent/lock 风险
- 可通过配置一键回退到非 adaptive 模式

---

## 16. 风险点与回滚方案

## 16.1 主要风险

### A. 状态切换抖动

问题：regime 在边界区频繁切换，导致阈值来回变。

应对：

- 引入 `stability_score`
- 引入 `cooloff_bars_after_switch`
- 切换期只允许更保守，不允许更激进

### B. 配置复杂度爆炸

问题：每个 regime、每个 symbol、每个层级都能配，最终没人敢改。

应对：

- v1 只保留少量 regime
- 先支持 defaults + 少量 override
- 只开放高价值参数，不全量参数化

### C. 回测与实盘口径不一致

问题：实盘用 effective policy，回测仍按旧逻辑，结果没法比。

应对：

- backtest 必须复用同一套 regime detector / policy resolver
- policy version 必须写入回测结果

### D. 执行层被“隐式改坏”

问题：regime 参数覆盖若直接侵入 executor，容易把 layering/risk chain 搞乱。

应对：

- M1/M2 先不动执行层参数
- M3 后只允许保守收紧
- 执行层保留 single source of truth 的 effective execution profile

## 16.2 回滚方案

### 配置回滚

```yaml
adaptive_regime:
  enabled: false
```

或：

```yaml
adaptive_regime:
  mode: observe_only
```

### 代码回滚原则

- 所有 adaptive 逻辑集中在新增模块与少量接入点
- 不直接替换 executor 主干流程
- 所有 override 都保留“无 policy 时回退旧配置”的路径

### 运行回滚顺序

1. `full` → `guarded_execute`
2. `guarded_execute` → `decision_only`
3. `decision_only` → `observe_only`
4. `observe_only` → `disabled`

---

## 17. 实施顺序建议

推荐顺序不是“从 detector 一路写到 executor”，而是：

1. **先统一数据结构**
   - regime snapshot / policy snapshot / effective config
2. **再做 observe-only**
   - 保证数据先跑起来
3. **再动 decision layer**
   - 最容易观察收益，也最少伤执行链
4. **再动 validator / risk budget**
   - 做保守收紧
5. **最后才碰 execution parameter adaptation**
   - layer ratios、trailing 等

一句话：

> 先让系统“看懂市场状态”，再让系统“因为看懂而少犯错”，最后才让系统“因为看懂而调参数”。

---

## 18. 任务清单（适合长期追踪）

## 18.1 文档 / 设计

- [ ] 统一 regime 命名（建议拆分 `trend_up / trend_down`）
- [ ] 定义 regime snapshot schema
- [ ] 定义 policy snapshot schema
- [ ] 明确 dashboard / analytics 所需字段

## 18.2 核心模块

- [ ] 新增 `regime policy resolver` 模块
- [ ] `core/regime.py` 增加 stability / transition risk / version
- [ ] `Config` 支持 `adaptive_regime` 读取与 symbol override

## 18.3 Signal Layer

- [ ] `SignalDetector` 从 policy 读取策略 multiplier
- [ ] 保留 base_strength / adjusted_strength
- [ ] 将 policy version 写入 `reason.metadata`

## 18.4 Decision Layer

- [ ] EntryDecider 支持 regime override thresholds
- [ ] 增加 transition risk 惩罚
- [ ] 输出更详细 breakdown 字段

## 18.5 Validation / Risk

- [ ] Validator 支持 effective validation config snapshot
- [ ] Risk budget 支持 regime-based conservative overrides
- [ ] `risk_anomaly` 支持统一 hard block

## 18.6 Execution

- [ ] Executor 记录 effective execution profile
- [ ] layer ratios 支持 regime override（后置阶段）
- [ ] 保证 direction lock / intents / plan state 不被 override 破坏

## 18.7 Analytics

- [ ] backtest 输出按 regime 分桶结果
- [ ] signal quality 支持 regime slicing
- [ ] policy version 对比报告

## 18.8 Dashboard / Docs

- [ ] Dashboard 增加 regime snapshot
- [ ] Dashboard 增加 regime distribution / performance 面板
- [ ] README / docs 增加 adaptive regime 方案入口

## 18.9 上线治理

- [ ] 先在 `observe_only` 跑基线
- [ ] 达到样本阈值后进入 `decision_only`
- [ ] 小范围 symbol 灰度 `guarded_execute`
- [ ] 明确回滚 playbook

---

## 19. 建议的首批交付件

为了让主力开发者后续好追踪，建议第一轮就固定以下交付物：

1. 本文档（长期主计划）
2. 一个 `adaptive_regime` 配置样例
3. 一个 dashboard/API 字段清单
4. 一个 backtest 输出字段清单
5. 一个“灰度上线检查表”文档

---

## 20. 对当前项目最重要的结论

### 结论 1：这个项目已经不是“需不需要 regime”的阶段，而是“如何把已有 regime 雏形收敛成统一框架”的阶段

因为：

- detector、entry_decider、validator 已经都在碰 regime
- 如果现在不收口，后面只会越来越分散

### 结论 2：优先级最高的不是更复杂的状态识别，而是统一 policy + observability

当前最大风险不是 regime 不够聪明，而是：

- 改动不可追踪
- 决策依据分散
- 回测与实盘无法对齐

### 结论 3：最安全的路线是 Observe → Decision → Guarded Risk → Execution Adaptation

这样能最大程度保留当前：

- 分仓
- 风控
- 通知
- 对账/自愈
- Dashboard

这些已存在的稳定骨架。

---

## 21. 推荐下一步（立即执行顺序）

如果按开发顺序排，我建议下一步这样走：

1. **先做字段与配置统一**
   - 不改行为，只统一 regime snapshot / policy snapshot
2. **给 analytics/backtest 打上 regime tag**
   - 尽快形成数据闭环
3. **把 detector / entry_decider 的 regime 规则收敛到 policy resolver**
4. **先开 `observe_only` 跑一段时间**
5. **确认样本质量后，再启用 `decision_only`**

---

## 22. 相关文档

- `docs/adaptive-market-regime-backlog.md`
- `docs/adaptive-market-regime-m0-implementation.md`
- `docs/layering-config-notes.md`
- `docs/layering-acceptance-checklist.md`
- `README.md`

> 建议分工：
>
> - 本文继续作为 **主计划 / 架构说明**
> - `docs/adaptive-market-regime-backlog.md` 作为 **可执行 backlog / 阶段追踪清单**
>
> 后续设计变更、阶段完成记录、风险备注，优先写进这两份文档或配套 issue/checklist，而唔好散落喺聊天记录度。
