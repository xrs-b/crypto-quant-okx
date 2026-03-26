# Adaptive Market Regime M0 实施清单（可直接开工版）

> 配套主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
>
> 配套 backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)

---

## 0. 当前实现状态（2026-03-26 更新）

### M0 Step 2 已完成（observe-only 接入）

已把统一 `regime_snapshot` / `adaptive_policy_snapshot` 以 **observe-only** 方式接入：

- `signals/detector.py`
  - 输出统一 `signal.regime_snapshot`
  - 兼容保留 `signal.regime_info`
  - 同步挂到 `signal.market_context`
  - 生成 `signal.adaptive_policy_snapshot`
- `signals/entry_decider.py`
  - 在 `EntryDecisionResult` 中透传 regime / policy snapshot
  - 仅供观测，不改变现有打分和 allow/watch/block 规则
- `signals/validator.py`
  - 在 `details/filter_details` 中写入 observe-only snapshot
  - 不改变现有过滤条件
- `trading/executor.py`
  - 在 risk/execution observability 中透传 snapshot
  - 不改变 execute / deny 结果
- `analytics/backtest.py`
  - 为交易结果预留 `regime_tags` / `policy_tags` / `observe_only_tags`
  - 不破坏原有回测输出字段

### 本步边界

- **没有启用 adaptive policy 生效逻辑**
- **没有改动真实交易 allow/block/execute 判定**
- 所有新字段都只用于透传、埋点、后续观测与回测标签

## 1. 这份文档解决什么问题

主计划与 backlog 已经把方向讲清楚，但 M0 仍然偏“设计项”。

这份文档把 **M0 再拆成主力开发者第一天就能开写代码** 的实施清单，重点回答：

1. **先改哪些真实文件**，为什么从这些文件开始
2. **每个任务的最小实现边界**，避免一上来做成 M1/M2
3. **先补哪些测试**，确保 M0 只做 schema / config / observability，不改交易行为
4. **推荐 commit / PR 顺序**，把风险拆小
5. **哪些地方只埋点、不生效**
6. **schema / config / logging / snapshot 字段草案**

M0 的硬边界只有一句话：

> **统一 adaptive regime 的数据结构、配置入口、观测位与文档入口，但不改变 detector / decider / validator / executor 的实际放行与执行结果。**

---

## 2. 当前代码基础（基于现有仓库真实路径）

当前项目并不是从 0 开始，M0 应该建立在这些现有模块之上：

- `core/regime.py`
  - 已有 `Regime` / `RegimeResult` / `RegimeDetector`
  - 当前输出结构较轻，只到 `regime / confidence / indicators / details`
- `signals/detector.py`
  - `Signal` 已有 `market_context`、`regime_info`
  - 已在 `analyze()` 中调用 `detect_regime()`
  - 已有 `_apply_regime_weighting()`，但这是 **已生效的 heuristic**，不是统一 policy snapshot
- `signals/entry_decider.py`
  - 已消费 `signal.regime_info` 与 `signal.market_context`
  - 很适合作为后续 M2 decision-only 接入点
- `signals/validator.py`
  - 已有 `regime_filters`、`market_filters`、`filter_details`
  - 已有统一失败码体系 `FILTER_META`
- `trading/executor.py`
  - 已有 `plan_context`、`observability`、`entry_plan`、`layer plan`、`direction lock`、`open intent`
  - 是后续 execution snapshot 最适合承载的地方
- `core/risk_budget.py`
  - 已有 `risk_budget` 统一入口和 `compute_entry_plan()`
  - 后续 M3/M4 的 conservative overrides 很适合挂这里
- `analytics/backtest.py`
  - 已串起 detector + validator 基础回测
  - 当前还没有 regime / policy version 维度输出
- `core/database.py`
  - `signals.filter_details`、`trades.plan_context` 已是现成 snapshot 容器
  - 不必在 M0 一上来就做重 schema migration
- `tests/test_regime.py` / `tests/test_all.py`
  - 已有 regime、detector、validator、executor、dashboard 的回归测试骨架

结论：

**M0 最应该做的是“把这些散落在现有代码里的 regime 相关口径收起来”，而不是加更多交易逻辑。**

---

## 3. M0 第一批建议先改的文件

按建议优先顺序：

### 第一批（必须先动）

1. `core/regime.py`
2. `core/config.py`
3. `signals/detector.py`
4. `signals/entry_decider.py`
5. `signals/validator.py`
6. `trading/executor.py`
7. `analytics/backtest.py`
8. `tests/test_regime.py`
9. `tests/test_all.py`
10. `docs/adaptive-market-regime-backlog.md`
11. `README.md`

### 为什么先改这批

#### 1) `core/regime.py` 先改
因为 M0 最核心是统一 `regime snapshot` schema。如果这里不先定，下面所有模块都只会继续传旧结构。

#### 2) `core/config.py` 第二个改
因为 M0 不是只写文档，而是要让后续 M1/M2 有固定配置入口。配置树必须先落位，后面的 snapshot 才有 `mode / version / defaults / symbol override` 来源。

#### 3) `signals/detector.py` 紧跟着改
它是 regime 产生后的第一跳，而且 `Signal` 数据结构就在这里。先把 `regime_snapshot / policy_snapshot` 的挂载位定下来，后面 decider / validator / executor 才能统一消费。

#### 4) `signals/entry_decider.py`、`signals/validator.py`、`trading/executor.py` 只做埋点
这三层都已经有现成 metadata/filter_details/plan_context/observability 容器，M0 只需把 snapshot 带进去，先不要改 decision 和 execution 行为。

#### 5) `analytics/backtest.py` 在 M0 就先埋 regime tag
因为后续所有“observe-only 值不值得开”的判断，都要靠历史输出字段。M0 不必做完整报表，但要先把字段传出来。

#### 6) 测试与文档最后补齐
测试先锁住“不改变交易行为”，文档入口再给后续开发者接棒。

---

## 4. 推荐实施顺序（commit / PR 顺序）

如果拆成小 PR，我建议 4 个：

### PR1：schema + config 骨架
**目标**：只建立统一数据结构，不碰业务行为。

涉及文件：

- `core/regime.py`
- `core/config.py`
- `tests/test_regime.py`
- `tests/test_all.py`

建议 commit：

1. `docs: define adaptive regime m0 implementation plan`
2. `feat: add adaptive regime snapshot and config scaffolding`
3. `test: cover adaptive regime config parsing and snapshot schema`

### PR2：signal / decision / validation observe-only 埋点
**目标**：把 snapshot 带过 detector / decider / validator，但不生效。

涉及文件：

- `signals/detector.py`
- `signals/entry_decider.py`
- `signals/validator.py`
- `tests/test_all.py`

建议 commit：

1. `feat: attach adaptive regime snapshots to signal and decision flow`
2. `test: ensure observe-only regime fields do not change decision baseline`

### PR3：executor / backtest snapshot 埋点
**目标**：把 execution 与回测链路也接通同一份 snapshot。

涉及文件：

- `trading/executor.py`
- `analytics/backtest.py`
- `tests/test_all.py`

建议 commit：

1. `feat: add adaptive regime observability to executor and backtest`
2. `test: expose regime snapshots in execution and backtest outputs`

### PR4：README / backlog / docs 入口整理
**目标**：确保后续接手的人一眼知道去哪看。

涉及文件：

- `README.md`
- `docs/adaptive-market-regime-backlog.md`
- `docs/adaptive-market-regime-m0-implementation.md`

建议 commit：

1. `docs: link adaptive regime implementation entrypoints`

如果不拆 PR，只本地一口气做，也建议按上面顺序拆 commit。

---

## 5. M0 任务清单（第一天即可开工）

下面按“任务卡”方式写，适合主力开发者持续追踪。

### M0-T1｜统一 regime snapshot schema（先落 `core/regime.py`）

**主要文件**

- `core/regime.py`
- `tests/test_regime.py`
- `tests/test_all.py`

**为什么先做**

当前 `RegimeResult.to_dict()` 只有：

- `regime`
- `confidence`
- `indicators`
- `details`

这不足以支撑后续 M1 observe-only 的统一口径。

**最小实现边界**

只做下面几件事：

1. 保留现有 `Regime` 检测逻辑，不强行重写分类逻辑
2. 扩展 `RegimeResult` 输出结构，新增兼容字段
3. 允许旧消费方继续读 `regime / confidence / indicators / details`
4. 新增更完整的 snapshot 字段，但默认值可以是保守值

**建议字段草案**

```python
{
  "regime": "trend",              # 兼容旧字段
  "name": "trend",                # 新字段，先与 regime 同值
  "family": "trend",              # trend / range / vol / risk / unknown
  "direction": "up",              # up / down / neutral / unknown
  "confidence": 0.78,
  "stability_score": 0.50,         # M0 可先给 heuristic/default
  "transition_risk": 0.50,         # M0 可先给 heuristic/default
  "indicators": {...},
  "features": {...},               # M0 可直接镜像 indicators，后续再收敛
  "details": "趋势上涨(gap=2.10%)",
  "detected_at": "2026-03-26T16:00:00",
  "detector_version": "regime_v1_m0"
}
```

**字段策略建议**

- `name`：M0 先与 `regime` 相同，避免破坏旧逻辑
- `family`：
  - `trend -> trend`
  - `range -> range`
  - `high_vol / low_vol -> vol`
  - `risk_anomaly -> risk`
  - `unknown -> unknown`
- `direction`：
  - `trend` / `high_vol` 可根据 `ema_direction` 映射 `up/down`
  - `range` / `low_vol` → `neutral`
  - `risk_anomaly` / `unknown` → `unknown`
- `stability_score`、`transition_risk`：
  - M0 不要求复杂模型
  - 可先从 `abs(ema_gap)`、`volatility` 做简单 heuristic
  - 重点是 **字段先存在**，不是一上来算得多准

**先写哪些测试**

- `tests/test_regime.py`
  - `result.to_dict()` 包含新增字段
  - 旧字段仍存在
  - `unknown` 情况也能完整输出 snapshot
- `tests/test_all.py`
  - detector 产出的 `signal.regime_info` 包含 `name / family / direction / detector_version`

**只埋点不生效**

- 不拆 `trend` 为 `trend_up / trend_down` 的实际行为分支
- 不改变当前 detector / validator 里的判断条件
- 只是把方向作为 snapshot 字段先输出

---

### M0-T2｜加 `adaptive_regime` 配置骨架（落 `core/config.py`）

**主要文件**

- `core/config.py`
- `config/config.yaml.example`
- `config/config.local.yaml.example`
- `tests/test_all.py`

**为什么现在做**

后续所有阶段都要有统一开关；M0 不把配置入口立住，M1 会继续把开关散落进 `regime_filters`、`market_filters`、`entry_decider`。

**最小实现边界**

1. 只新增配置读取和默认值
2. 不改变任何现有运行行为
3. 默认等价于 observe-only / disabled
4. 支持 symbol override 读取，但 M0 不真的消费 override 做行为调整

**建议配置草案**

```yaml
adaptive_regime:
  enabled: false
  mode: observe_only   # disabled / observe_only / decision_only / guarded_execute / full
  detector:
    version: regime_v1_m0
    min_confidence: 0.5
    min_stability_score: 0.0
    cooloff_bars_after_switch: 0
  defaults:
    policy_version: adaptive_policy_v1_m0
  regimes: {}
```

**建议在 `Config` 增加的便捷方法**

- `get_adaptive_regime_config(symbol: str = None) -> Dict[str, Any]`
- `get_adaptive_regime_mode(symbol: str = None) -> str`
- `is_adaptive_regime_enabled(symbol: str = None) -> bool`

**实现建议**

- 参考现有 `get_layering_config()` 风格
- 使用 `get_symbol_section()` 做 symbol override merge
- 保证：配置缺失时返回安全默认值，而不是 `None`

**先写哪些测试**

- 缺省配置时：
  - `enabled == False`
  - `mode == observe_only` 或项目约定默认值
- symbol override 能覆盖 `adaptive_regime.mode`
- 非法 mode 时直接抛错或回退默认值（建议抛错，避免静默）

**只埋点不生效**

- `mode=decision_only`、`guarded_execute` 先只被解析，不产生行为改变

---

### M0-T3｜定义 policy snapshot 中立骨架（建议新建 `core/regime_policy.py`）

**主要文件**

- `core/regime_policy.py`（新）
- `tests/test_all.py`

**为什么 M0 就值得建骨架**

虽然真正生效要到 M1/M2，但如果 M0 不先定 neutral policy 的结构，后面每层都会自己 invent 一套 snapshot 名字。

**最小实现边界**

只做一个 **neutral / observe-only resolver**：

输入：

- `regime_snapshot`
- `adaptive_regime config`
- `symbol`

输出固定结构：

```python
{
  "enabled": False,
  "mode": "observe_only",
  "policy_version": "adaptive_policy_v1_m0",
  "policy_source": "adaptive_regime.defaults",
  "regime_name": "trend",
  "signal_weight_overrides": {},
  "decision_overrides": {},
  "validation_overrides": {},
  "risk_overrides": {},
  "execution_overrides": {},
  "is_effective": False,
  "notes": ["m0-observe-only"]
}
```

**重点**

- M0 不做真实 override 计算
- 但要让所有调用方能拿到稳定结构
- 后续 M1 再慢慢把这个 resolver 变成真正入口

**先写哪些测试**

- 无配置时返回 neutral snapshot
- observe-only 模式下 `is_effective == False`
- 输出可 JSON 序列化，可直接写进 `filter_details` / `plan_context`

**只埋点不生效**

- 所有 override 字段默认空 dict
- `is_effective` 必须是 `False`

---

### M0-T4｜扩展 `Signal` 结构与 detector 埋点（落 `signals/detector.py`）

**主要文件**

- `signals/detector.py`
- `tests/test_all.py`

**为什么这一步关键**

`Signal` 是后续所有链路的载体。M0 要先把 snapshot 从“局部上下文”提升成正式字段。

**最小实现边界**

在 `Signal` dataclass 中新增：

- `regime_snapshot: Dict = field(default_factory=dict)`
- `adaptive_policy_snapshot: Dict = field(default_factory=dict)`
- `observability_snapshot: Dict = field(default_factory=dict)`（可选）

并在 `analyze()` 中：

1. `regime_result.to_dict()` 同时写到
   - `signal.regime_info`（兼容旧字段）
   - `signal.regime_snapshot`（新字段）
2. 生成 neutral `adaptive_policy_snapshot`
3. 在 `market_context` 中只补轻量索引字段，不塞整份大对象

**建议最小落位**

```python
signal.regime_info = regime_snapshot         # 兼容旧字段
signal.regime_snapshot = regime_snapshot     # 新字段
signal.adaptive_policy_snapshot = neutral_policy_snapshot
signal.market_context.update({
  'regime': regime_snapshot['name'],
  'regime_confidence': regime_snapshot['confidence'],
  'regime_family': regime_snapshot['family'],
  'policy_mode': neutral_policy_snapshot['mode'],
  'policy_version': neutral_policy_snapshot['policy_version'],
})
```

**关于 `_apply_regime_weighting()` 的处理建议**

M0 **不要动它的当前行为**，因为它已经是现网行为一部分。

但要在 `reason.metadata` 中先补足影子字段，方便未来收口：

```python
metadata:
  regime_multiplier: 1.12
  regime_weighting_source: "legacy_market_context"
  adaptive_policy_applied: false
  adaptive_policy_mode: "observe_only"
```

即：

- 先承认当前 still 是 legacy weighting
- 不要在 M0 假装已经切到 policy 驱动

**先写哪些测试**

- `signal.regime_snapshot` 存在
- `signal.adaptive_policy_snapshot` 存在
- 现有 `signal.regime_info` 与 `market_context['regime']` 仍兼容
- 在相同输入下，`signal.signal_type` 与 `signal.strength` 不因为新增字段而改变

**只埋点不生效**

- 不把 `_apply_regime_weighting()` 改成读 `adaptive_policy_snapshot`
- 不修改 reason strength 计算路径

---

### M0-T5｜决策层只记录 snapshot，不改 allow/watch/block（落 `signals/entry_decider.py`）

**主要文件**

- `signals/entry_decider.py`
- `tests/test_all.py`

**最小实现边界**

只做两件事：

1. `EntryDecisionResult.to_dict()` / `DecisionBreakdown` 输出里带上 regime / policy 观测字段
2. 在 `decide()` 内把“当前 baseline 阈值”和“adaptive hypothetical 阈值”区分开

**建议新增字段草案**

在 `DecisionBreakdown` 增加：

```python
regime_name: str = ""
regime_confidence: float = 0.0
regime_stability_score: float = 0.0
regime_transition_risk: float = 0.0
adaptive_policy_mode: str = ""
adaptive_policy_version: str = ""
effective_decision_overrides: Dict = field(default_factory=dict)
hypothetical_decision_overrides: Dict = field(default_factory=dict)
```

**实现原则**

- `effective_decision_overrides`：M0 一律 `{}`
- `hypothetical_decision_overrides`：可先等于 neutral snapshot 或空 dict
- 不允许 `allow_score_min` 等阈值真的变化

**先写哪些测试**

- 同一条测试信号，在新增 snapshot 后，`decision` 结果不变
- `breakdown` 中能看到 `regime_name / adaptive_policy_mode`

**只埋点不生效**

- 不修改 `_make_decision()` 的阈值来源
- 不接入 `transition_risk` 惩罚

---

### M0-T6｜验证层把 regime/policy snapshot 写入 `filter_details`（落 `signals/validator.py`）

**主要文件**

- `signals/validator.py`
- `tests/test_all.py`

**为什么先做这个**

validator 现在已经是最关键的过滤观测点，且 `SignalRecorder.record()` 会把 `filter_details` 写入 DB。M0 在这里埋点，收益最高。

**最小实现边界**

在 `validate()` 的 `details` 里统一追加：

```python
{
  "adaptive_regime": {
    "regime_snapshot": {...},
    "policy_snapshot": {...},
    "effective_validation_snapshot": {
      "mode": "observe_only",
      "applied": False,
      "overrides": {}
    }
  }
}
```

并在 `details['market_context_check']` 或新增 `details['adaptive_regime_check']` 里标明：

- `mode`
- `policy_version`
- `applied: False`

**注意**

当前 validator 已有真实 `regime_filters` 行为，这是现有主线逻辑；M0 不能把它误包装成 adaptive 已生效。

建议明确区分：

- `legacy_regime_filter`: 当前已存在的 validator 逻辑
- `adaptive_regime_snapshot`: 新框架 observe-only 埋点

**先写哪些测试**

- `validate()` 返回结果不变
- `details` 新增 `adaptive_regime` 区块
- risk_anomaly / low_vol 等现有 case 仍按旧逻辑通过/拦截

**只埋点不生效**

- 不用 `policy_snapshot.validation_overrides` 改任何 `min_strength`、`block_counter_trend`

---

### M0-T7｜执行层只记录 effective execution snapshot 草案（落 `trading/executor.py`）

**主要文件**

- `trading/executor.py`
- `tests/test_all.py`

**为什么 M0 就该做**

executor 已有 `plan_context` / `observability` / `entry_plan`。后续如果不提前约定 snapshot 名字，M3/M4 很容易把字段越写越乱。

**最小实现边界**

在以下位置补充 snapshot：

- `_prepare_open_execution()` 返回的 `plan_context`
- `build_observability_context(..., extra=...)`
- `record_trade(..., plan_context=...)`

建议字段：

```python
plan_context["adaptive_regime"] = {
  "regime_snapshot": {...},
  "policy_snapshot": {...},
  "effective_execution_snapshot": {
    "mode": "observe_only",
    "applied": False,
    "layer_ratios": current_layer_ratios,
    "layer_max_total_ratio": current_layer_max_total_ratio,
    "risk_budget_source": "baseline"
  }
}
```

**这里的关键点**

- `layer_ratios` 记录的是 **当前 baseline 实际值**
- 不是 hypothetical override
- 因为 M0 的目标是先把“当时到底用了哪套执行参数”写清楚

如果需要，也可以补一个假想字段：

- `hypothetical_execution_overrides: {}`

但别在 M0 引入太多噪音。

**先写哪些测试**

- `RiskManager.can_open_position()` 返回细节里含 `adaptive_regime` 区块
- `get_execution_state_snapshot()` 或 execution-state API 仍能工作
- 不影响 `layering` / `direction lock` / `open intent` 测试

**只埋点不生效**

- 不允许按 regime 动态改 `layer_ratios`
- 不允许按 regime 改 `risk_budget`
- 不允许动 `partial TP / trailing stop`

---

### M0-T8｜回测先输出 regime / policy tag，不做复杂分桶（落 `analytics/backtest.py`）

**主要文件**

- `analytics/backtest.py`
- `tests/test_all.py`

**为什么 M0 就做最低限度**

后续要比较 baseline vs observe-only，没有字段就没法开始留样本。

**最小实现边界**

在回测 trade record 或 symbol result 中，增加：

- `entry_regime_name`
- `entry_regime_family`
- `entry_regime_confidence`
- `policy_mode`
- `policy_version`

以及 signal quality 输出中，至少加：

- `regime_name`
- `policy_mode`

**实现建议**

先在 `_run_symbol()` 里开仓时把当前 signal 的 snapshot 带进 `BacktestPosition` 或临时变量；平仓时写到 trade dict。

**先写哪些测试**

- 回测 summary 仍可运行
- recent_trades 中出现 `entry_regime_name` 等字段
- 无 regime 字段时 fallback 到 `unknown`，不能炸

**只埋点不生效**

- 不做按 regime 的 performance panel
- 不做 policy A/B 回测器

---

### M0-T9｜README / backlog 增加清晰入口

**主要文件**

- `README.md`
- `docs/adaptive-market-regime-backlog.md`
- `docs/adaptive-market-regime-framework-plan.md`

**最小实现边界**

1. README 在相关文档列表里加入 M0 实施文档
2. backlog 的 M0 区域加一句链接，告诉开发者“开工请直接看实施清单”
3. framework plan 的相关文档区也可追加链接

**建议文案**

- README：`docs/adaptive-market-regime-m0-implementation.md`
- backlog：`M0 直接开工清单见 docs/adaptive-market-regime-m0-implementation.md`

---

## 6. M0 期间哪些地方只埋点、不生效

这是最重要的边界，建议开发时直接当 checklist。

### 只埋点，不改变行为

- `core/regime.py`
  - 可以扩 schema
  - **不能**因为 M0 去大改 regime 分类逻辑，导致测试行为漂移
- `signals/detector.py`
  - 可以挂 `regime_snapshot` / `adaptive_policy_snapshot`
  - **不能**把 `_apply_regime_weighting()` 改为 policy 驱动
- `signals/entry_decider.py`
  - 可以记录 hypothetical thresholds
  - **不能**改 `allow_score_min` / `watch/block` 结果
- `signals/validator.py`
  - 可以写 `adaptive_regime` snapshot 到 `details`
  - **不能**用 adaptive overrides 改过滤结果
- `trading/executor.py`
  - 可以记录 baseline execution profile
  - **不能**改 layer ratios / risk budget / leverage cap
- `analytics/backtest.py`
  - 可以打 tag
  - **不能**让 adaptive regime 影响回测开平仓逻辑

### M0 明确不要做

- 不拆真实执行状态到新数据库表
- 不改 `signals` / `trades` SQL schema 做重 migration
- 不在 dashboard 前端做重 UI
- 不新增自动通知“regime 切换”
- 不开始做 `decision_only`
- 不把 `risk_anomaly` hard block 改成新 adaptive 逻辑来源

---

## 7. schema / config / logging / snapshot 字段草案

下面给一份尽量贴近现有项目的命名草案，方便直接开工时统一口径。

### 7.1 regime snapshot（建议统一名：`regime_snapshot`）

```json
{
  "regime": "trend",
  "name": "trend",
  "family": "trend",
  "direction": "up",
  "confidence": 0.74,
  "stability_score": 0.58,
  "transition_risk": 0.27,
  "indicators": {
    "ema_gap": 0.0211,
    "volatility": 0.0138,
    "atr_ratio": 0.0112,
    "price_change_1d": 0.0083,
    "price_change_5d": 0.0314,
    "volume_ratio": 1.32,
    "trend_strength": 0.21,
    "ema_direction": 1
  },
  "features": {
    "ema_gap": 0.0211,
    "volatility": 0.0138,
    "atr_ratio": 0.0112,
    "volume_ratio": 1.32
  },
  "details": "趋势上涨(gap=2.11%)",
  "detected_at": "2026-03-26T16:10:00",
  "detector_version": "regime_v1_m0"
}
```

### 7.2 policy snapshot（建议统一名：`adaptive_policy_snapshot`）

```json
{
  "enabled": false,
  "mode": "observe_only",
  "policy_version": "adaptive_policy_v1_m0",
  "policy_source": "adaptive_regime.defaults",
  "regime_name": "trend",
  "signal_weight_overrides": {},
  "decision_overrides": {},
  "validation_overrides": {},
  "risk_overrides": {},
  "execution_overrides": {},
  "is_effective": false,
  "notes": ["m0-observe-only"]
}
```

### 7.3 validator / decision / execution effective snapshot 命名建议

- `effective_decision_snapshot`
- `effective_validation_snapshot`
- `effective_execution_snapshot`

M0 建议统一结构：

```json
{
  "mode": "observe_only",
  "applied": false,
  "baseline_source": "current_runtime_config",
  "overrides": {}
}
```

### 7.4 logging / filter_details / plan_context 草案

#### `signals.filter_details`

```json
{
  "entry_decision": {...},
  "market_context_check": {...},
  "regime_check": {...},
  "adaptive_regime": {
    "regime_snapshot": {...},
    "policy_snapshot": {...},
    "effective_validation_snapshot": {...}
  }
}
```

#### `trades.plan_context`

```json
{
  "layer_no": 1,
  "layer_ratio": 0.06,
  "entry_plan": {...},
  "observability": {...},
  "adaptive_regime": {
    "regime_snapshot": {...},
    "policy_snapshot": {...},
    "effective_execution_snapshot": {
      "mode": "observe_only",
      "applied": false,
      "layer_ratios": [0.06, 0.06, 0.04],
      "layer_max_total_ratio": 0.16,
      "risk_budget_source": "baseline"
    }
  }
}
```

#### `DecisionBreakdown`

```python
regime_name
regime_confidence
regime_stability_score
regime_transition_risk
adaptive_policy_mode
adaptive_policy_version
effective_decision_overrides
```

---

## 8. 建议第一批测试清单

这里不追求大而全，只锁住 M0 的核心边界：**有新字段，但行为不变。**

### A. `tests/test_regime.py`

新增 / 调整：

- `test_regime_snapshot_contains_v1_m0_fields`
- `test_unknown_regime_snapshot_keeps_compat_fields`
- `test_direction_family_and_detector_version_present`

### B. `tests/test_all.py` - Config

新增：

- `test_adaptive_regime_config_defaults_are_safe`
- `test_symbol_override_can_override_adaptive_regime_mode`

### C. `tests/test_all.py` - Detector

新增：

- `test_signal_contains_regime_snapshot_and_policy_snapshot`
- `test_detector_strength_and_signal_type_unchanged_when_observe_only_snapshot_added`

### D. `tests/test_all.py` - EntryDecider

新增：

- `test_entry_decider_exposes_regime_policy_breakdown_without_changing_decision`

### E. `tests/test_all.py` - Validator

新增：

- `test_validator_details_include_adaptive_regime_snapshot`
- 保留现有 `risk_anomaly / low_vol / high_vol / trend` 测试，确保旧逻辑不漂移

### F. `tests/test_all.py` - Executor / RiskManager

新增：

- `test_can_open_position_returns_adaptive_regime_execution_snapshot`
- `test_execution_observability_keeps_baseline_layering_values`

### G. `tests/test_all.py` - Backtest

新增：

- `test_backtest_recent_trades_include_entry_regime_tags`

---

## 9. 第一日任务板（适合主力开发者直接追）

下面这份就是“开 IDE 就能做”的 task list：

### Day 1 / Block A：先把骨架立住

- [ ] 在 `core/regime.py` 扩展 `RegimeResult` 与 `to_dict()`，补 `name / family / direction / stability_score / transition_risk / detected_at / detector_version`
- [ ] 保留 `regime / confidence / indicators / details` 旧字段兼容
- [ ] 在 `core/config.py` 增加 `adaptive_regime` 默认值与读取 helper
- [ ] （可选但建议）新建 `core/regime_policy.py`，只返回 neutral observe-only snapshot
- [ ] 补 `tests/test_regime.py` 基础 schema 测试

### Day 1 / Block B：把 signal 链路打通

- [ ] 在 `signals/detector.py` 的 `Signal` dataclass 增加 `regime_snapshot`、`adaptive_policy_snapshot`
- [ ] `analyze()` 中挂载 snapshot，但不改最终 signal 行为
- [ ] 在 reason metadata 中标记 `adaptive_policy_applied=false`
- [ ] 补 detector 行为不变测试

### Day 1 / Block C：把 decision / validation 打通

- [ ] `signals/entry_decider.py` 增加 regime / policy breakdown 字段
- [ ] `signals/validator.py` 在 `details` 增加 `adaptive_regime` 区块
- [ ] 验证现有 risk_anomaly / low_vol 等旧 case 行为不变

### Day 1 / Block D：把 execution / backtest 打通

- [ ] `trading/executor.py` 在 `plan_context` / `observability` 增加 `adaptive_regime` snapshot
- [ ] `analytics/backtest.py` 在 trade 结果中打 `entry_regime_name / policy_mode / policy_version`
- [ ] 补 execution/backtest snapshot 测试

### Day 1 / Block E：补文档入口

- [ ] 在 `README.md` 相关文档区加入本文件链接
- [ ] 在 `docs/adaptive-market-regime-backlog.md` 的 M0 区增加入口链接
- [ ] 若有余力，在 framework plan 的相关文档区也补入口

---

## 10. 主力开发时的实现注意事项

### 10.1 尽量复用现有存储容器，M0 不搞重迁移

现有项目已经有：

- `signals.filter_details`
- `trades.plan_context`
- `DecisionBreakdown`
- `market_context`
- `observability`

M0 阶段优先把 snapshot 放进这些现成容器里。

**不要一上来就加一堆 SQL 列或新表。**

原因：

- 当前 SQLite schema 已不小
- M0 重点是统一口径，不是做数据仓库
- 如果后面真要高频聚合，再考虑 M1/M2 做抽取或单独表

### 10.2 命名上要明确区分三类东西

建议代码里不要混成一锅：

1. `legacy regime behavior`
   - 当前 detector/validator 已生效逻辑
2. `regime snapshot`
   - 对市场状态的统一描述
3. `adaptive policy snapshot`
   - 新框架的 observe-only/neutral policy 容器

只要命名不清，后面分析时一定会误判“到底是旧逻辑还是新逻辑在生效”。

### 10.3 M0 不要试图一次解决 `trend_up / trend_down`

文档上长期建议拆成 `trend_up / trend_down`，但 M0 代码里没必要一步到位替换所有 `trend`。

更稳的方式是：

- `name` 先保持 `trend`
- `direction` 先输出 `up/down`
- 等 M1/M2 真开始消费 policy 时再考虑是否升级 taxonomy

这样改动面会小很多。

---

## 11. M0 完成标准（代码视角）

满足下面这些，即可认为 M0 真正完成：

### 文档完成

- [ ] 本文档已落地
- [ ] README / backlog 已有入口

### 代码完成

- [ ] `core/regime.py` 能输出统一 snapshot
- [ ] `core/config.py` 有 `adaptive_regime` 默认配置与 helper
- [ ] detector / decider / validator / executor / backtest 都能看到同一份 regime / policy snapshot
- [ ] 所有 adaptive snapshot 默认为 observe-only / not applied

### 行为验收

- [ ] 现有 `SignalDetector` 输出方向与强度无意外变化
- [ ] 现有 `EntryDecider` allow/watch/block 结果不变
- [ ] 现有 `SignalValidator` 放行/拦截结果不变
- [ ] 现有 `TradingExecutor` layer / risk / intent 行为不变

### 测试验收

- [ ] regime 测试覆盖新增字段
- [ ] config / detector / validator / executor / backtest 至少各有 1 条 observe-only 回归测试

---

## 12. 一句话建议

如果只讲最务实的 M0 路线，我建议是：

> **先在 `core/regime.py` + `core/config.py` 定 schema 与 mode，再把 neutral policy snapshot 通过 `signals/detector.py -> signals/entry_decider.py -> signals/validator.py -> trading/executor.py -> analytics/backtest.py` 全链路挂过去，全程只记录、不改行为。**

这样做的好处是：

- M1 observe-only 几乎可以直接起跑
- 不会碰坏当前已稳定的 layering / risk / validator 主链路
- 后面真要做 decision_only，也有统一的字段与入口，不会返工
