# Adaptive Market Regime M4 Step 4 实施稿（Layering Plan Shape / `layer_ratios` Guarded Live）

> 配套主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
>
> 配套 backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)
>
> 配套 M4 边界方案：[`docs/adaptive-market-regime-m4-boundary-plan.md`](./adaptive-market-regime-m4-boundary-plan.md)
>
> 前置步骤：[`docs/adaptive-market-regime-m4-step1-implementation.md`](./adaptive-market-regime-m4-step1-implementation.md)、[`docs/adaptive-market-regime-m4-step2-implementation.md`](./adaptive-market-regime-m4-step2-implementation.md)、[`docs/adaptive-market-regime-m4-step3-implementation.md`](./adaptive-market-regime-m4-step3-implementation.md)
>
> 第二批可直接开工拆分：[`docs/adaptive-market-regime-m4-step4-batch2-implementation.md`](./adaptive-market-regime-m4-step4-batch2-implementation.md)
>
> 相关执行链文档：[`docs/layering-config-notes.md`](./layering-config-notes.md)、[`docs/layering-acceptance-checklist.md`](./layering-acceptance-checklist.md)

---

## 1. 这份文档要解决什么

这份文档只处理 **M4 Step 4：layering plan shape / `layer_ratios` guarded live**。

它不是重复 Step 1/2 的 execution hints / guardrails，也不是重复 Step 3 的 guarded layering profile 第一批 live，而是把 **`layer_ratios` / plan shape 何时才可以进入 live、边界如何钉死、先写哪些测试、先改哪些文件、如何灰度、如何回滚** 拆成可直接开工的任务。

一句话定义 Step 4：

> **在 Step 1/2/3 已证明 execution hints、guardrails live、layering guardrails live 都稳定之后，才允许在严格 guarded 条件下，小范围让 `layer_ratios` / plan shape 逐步进入 live；但仍然只做 conservative-only input shaping，不改 execution state machine 语义。**

---

## 2. Step 4 与前面步骤的边界判词

## 2.1 Step 1 / Step 2 / Step 3 分别已经做了什么

### Step 1：hints-only

Step 1 已经完成：

- `baseline_execution_profile`
- `effective_execution_profile_hint`
- conservative-only merge
- `applied / ignored / rollout_match / effective_state / hint_codes`
- executor / observability / plan_context 的 hints 链路

但 **不改 live execution / live layer plan**。

### Step 2：guardrails live

Step 2 已经允许外围 execution guardrails 小范围真生效：

- `layer_max_total_ratio`
- `max_layers_per_signal`
- `min_add_interval_seconds`
- `profit_only_add`
- `allow_same_bar_multiple_adds`
- `leverage_cap`

这一步仍然偏向 **纪律收紧**，不是 plan shape live。

### Step 3：layering guardrails live，plan shape 继续后置

Step 3 已经把 layering baseline / effective / live profile 审计补齐，并让以下字段可以在 guarded 条件下 live：

- `layer_max_total_ratio`
- `max_layers_per_signal`
- `min_add_interval_seconds`
- `profit_only_add`
- `allow_same_bar_multiple_adds`

但结论仍然很清楚：

> **`layer_ratios` 默认继续 hints-only；只有单独开启 `layering_plan_shape_enforcement_enabled` 时，才允许进入下一步。**

## 2.2 Step 4 的最小目标

Step 4 的最小目标必须收窄成以下 4 件事：

1. **明确把 hints、guardrails live、plan shape live 切成三层**
   - hints：可观察、不可执行
   - guardrails live：可收紧节奏 / 总量 / 次数，但不改 plan shape
   - plan shape live：才允许 `layer_ratios` 真正进入 live layer plan

2. **在严格 gated 下让 `layer_ratios` 逐步进入 live layer plan**
   - 默认仍关闭
   - 只在 rollout 命中 + 配置开启 + conservative-only 校验通过时生效

3. **必要时让 `layer_count` 只以“衍生审计字段”形式进入 Step 4**
   - Step 4 可以记录 / 审计 `effective_layer_count`
   - 但不允许把它做成独立扩层能力
   - `layer_count` 只能作为 `layer_ratios` 长度 / 截断结果的派生事实，不是单独可自由 override 的激进入口

4. **继续保持 execution 主骨架不变**
   - 不改 partial TP / trailing
   - 不改 reconcile / self-heal
   - 不改 intent lifecycle
   - 不改 direction lock 语义
   - 不改 skip-layer / intent 拆分 / layer reset 语义

## 2.3 Step 4 明确不做的事

Step 4 明确 **不做**：

- 不碰 partial TP / trailing hints 或 enforcement
- 不碰 reconcile / stale close / self-heal 逻辑
- 不碰 open intent 生命周期、状态流转、释放语义
- 不碰 direction lock scope / 创建 / 释放 / 冲突处理语义
- 不把 `layer_count` 做成独立的激进 profile 开关
- 不允许新增比 baseline 更多的 layer
- 不允许更大的首层、更大的总分配、更快的加仓节奏、更宽松的 add 条件
- 不做动态补单语义重写
- 不做 intent / layer plan reset 机制改写

这句要继续钉死：

> **Step 4 是 plan-shape guarded live，不是 execution state machine rewrite。**

---

## 3. Step 4 的边界：hints vs guardrails live vs plan shape live

## 3.1 三层定义

### A. hints

特点：

- 只出现在 baseline/effective snapshot、audit、dashboard、trade/intent observability
- 不进入 live layer plan
- 不改变 executor 实际消费的 layer shape

Step 4 中仍可处于 hints-only 的字段：

- `layer_ratios`（当 `layering_plan_shape_enforcement_enabled=false`）
- `layer_count`（仅作为审计派生字段）

### B. guardrails live

特点：

- 已允许影响真实执行
- 但本质是节奏、次数、总量、条件收紧
- 不直接重写 per-layer shape

属于这一层的字段：

- `layer_max_total_ratio`
- `max_layers_per_signal`
- `min_add_interval_seconds`
- `profit_only_add`
- `allow_same_bar_multiple_adds`

### C. plan shape live

特点：

- 真正改变 live layer plan 的层序 / 每层比例
- 解释成本最高
- 必须最后开、单独开、可独立回滚

Step 4 只允许 **一个核心主题** 进入这一层：

- `layer_ratios`

而 `layer_count` 的定位必须写死：

- **不是独立 override 主字段**
- 只作为 `effective/live_layer_ratios` 长度产生的派生事实
- 若 baseline 为 3 层，Step 4 只允许通过保守截断把 live shape 收紧到更少有效层；不允许反向扩成更多层

## 3.2 Step 4 哪些字段能进，哪些仍不该碰

### 可以进入 Step 4 的字段

#### 1) `layer_ratios`

- **Step 4 核心字段**
- 允许从 hints-only 进入 guarded live
- 但必须满足 conservative-only、长度保护、总和保护、逐层不放大、灰度开关、rollout gating

#### 2) `layer_count`

- 只能作为派生审计字段进入 Step 4
- 可用于表达：当前 effective/live plan 实际有几层会被消费
- 不建议新增独立配置项去直接 override `layer_count`

#### 3) `layer_max_total_ratio`
- 继续 live
- 继续作为 plan shape live 的上层 guardrail

#### 4) `max_layers_per_signal`
- 继续 live
- 继续作为可追加层数上限 guardrail

#### 5) `min_add_interval_seconds`
- 继续 live
- 继续作为节奏 guardrail

#### 6) `profit_only_add`
- 继续 live
- 继续作为追加条件 guardrail

### 仍然不该碰的字段 / 主题

- partial TP 相关字段
- trailing 相关字段
- reconcile / stale close / self-heal
- intent lifecycle / pending intent state machine
- direction lock 语义
- skip-layer 语义
- layer reset 主逻辑
- 任何会让 baseline execution 变得更激进的字段

---

## 4. Step 4 字段推进顺序（重点）

这一节直接回答：

- 哪些字段已经是前置 guardrail
- Step 4 先后顺序怎样排
- `layer_ratios` 何时才真 live

## 4.1 推进顺序总表

### 第 0 阶段：保持现状，先确认 Step 3 稳定

继续 live：

1. `layer_max_total_ratio`
2. `max_layers_per_signal`
3. `min_add_interval_seconds`
4. `profit_only_add`
5. `allow_same_bar_multiple_adds`

此时：

- `layer_ratios` 仍 hints-only
- `layer_count` 仍只做 audit / derived

### 第 1 阶段：补 plan shape audit 与防呆，再不改 live

先补这些能力：

1. baseline / effective / live `layer_ratios`
2. baseline / effective / live `layer_count`（derived）
3. `plan_shape_really_enforced`
4. `plan_shape_enforced_fields`
5. `plan_shape_ignored_fields`
6. `live_layer_shape_source`
7. `shape_guardrail_decisions`

在这一步，`layer_ratios` 仍然可以是 hints-only，先把“看得清、讲得清”补完整。

### 第 2 阶段：小范围放开 `layer_ratios` live

前提全部满足后，才放开：

1. `layer_ratios`

同时继续保留前置 guardrails：

2. `layer_max_total_ratio`
3. `max_layers_per_signal`
4. `min_add_interval_seconds`
5. `profit_only_add`

`layer_count` 在这一阶段仍不是单独配置目标，而是 `layer_ratios` 长度 / 截断后自然得到的派生结果。

## 4.2 为什么这个顺序最稳

### 1) `layer_max_total_ratio` 必须继续先于 `layer_ratios`

因为它定义总暴露天花板。

先有总量 guardrail，再有 plan shape live，才不会出现：

- shape 变了，但总暴露解释不清
- `layer_ratios` 看起来收紧，但累计后仍与 live total cap 不一致

### 2) `max_layers_per_signal` 必须先站稳

因为它限制“最多还能加几层”，属于层数纪律，而不是每层大小分配。

先稳住它，再让 `layer_ratios` live，才能把“层数收紧”与“层内配比收紧”分开解释。

### 3) `min_add_interval_seconds` 必须先站稳

因为它定义节奏边界。

如果同时放开 shape 与节奏，出问题时很难判断到底是：

- 形状导致
- 节奏导致
- 两者耦合导致

### 4) `profit_only_add` 必须先站稳

因为它定义追加条件。

先把盈利条件收紧稳定住，再放开 shape，能减少“shape 变了后补仓行为更复杂”的解释难度。

### 5) `layer_ratios` 一定要最后开

因为它最直接改 live layer plan 本体：

- 首层大小
- 后续层级权重
- live layer count（derived）
- plan shape audit 口径
- baseline/live 对比样本可比性

### 6) `layer_count` 只能跟着 `layer_ratios` 的保守收紧派生出来

不能倒过来先做“独立 layer_count override”，否则很容易把 Step 4 演化成：

- 先改层数
- 再改层比
- 最后没人能解释 live shape 到底怎么生成

---

## 5. 字段草案与 conservative-only 规则

## 5.1 Step 4 关心的字段草案

```yaml
adaptive_regime:
  execution:
    layering_profile_hints_enabled: true
    layering_profile_enforcement_enabled: true
    layering_plan_shape_enforcement_enabled: false
    layering_plan_shape_rollout_symbols: []
    layering_plan_shape_rollout_fraction: 0.0
    layering_plan_shape_shadow_compare_enabled: true
    layering_plan_shape_require_guardrails_live: true
    layering_plan_shape_require_step3_stable: true

execution_overrides:
  layer_ratios: [0.05, 0.04, 0.03]
  layer_max_total_ratio: 0.12
  max_layers_per_signal: 1
  min_add_interval_seconds: 600
  profit_only_add: true
```

> `layer_count` 不建议作为 Step 4 独立 override 字段暴露；如确实要在 snapshot 中出现，应为 derived 字段，例如 `baseline_layer_count / effective_layer_count / live_layer_count`。

## 5.2 `layer_ratios` 的 conservative-only 规则

`layer_ratios` 进入 Step 4 时，必须继续满足：

1. 每层比例 **不大于 baseline 对应层**
2. 总和 **不大于 baseline 总和**
3. 长度 **不大于 baseline layer count**
4. 不允许通过补零 / 插层方式扩层
5. 若长度缩短，则只允许对尾层做保守截断
6. live 总和 **不大于 `layer_max_total_ratio` live 值**
7. live 可消费层数 **不大于 `max_layers_per_signal` live 值 + baseline 首层语义允许范围**

## 5.3 `layer_count` 的 Step 4 规则

`layer_count` 若进入 snapshot / audit，只允许作为下列派生事实：

- `baseline_layer_count = len(baseline_layer_ratios)`
- `effective_layer_count = len(effective_layer_ratios)`
- `live_layer_count = len(live_layer_ratios)`

并要求：

- `effective_layer_count <= baseline_layer_count`
- `live_layer_count <= baseline_layer_count`
- `live_layer_count` 不得因为 regime 大于 baseline
- `live_layer_count` 与 `max_layers_per_signal` 的关系必须可解释

## 5.4 其他字段继续沿用的 merge 规则

- `layer_max_total_ratio`：只允许下降
- `max_layers_per_signal`：只允许下降
- `min_add_interval_seconds`：只允许上升
- `profit_only_add`：只允许 `False -> True`
- `allow_same_bar_multiple_adds`：只允许 `True -> False`

---

## 6. 建议 observability / snapshot 结构

```yaml
adaptive_layering_snapshot:
  baseline:
    layer_ratios: [0.06, 0.06, 0.04]
    layer_count: 3
    layer_max_total_ratio: 0.16
    max_layers_per_signal: 1
    min_add_interval_seconds: 300
    profit_only_add: false
  effective:
    layer_ratios: [0.05, 0.04, 0.03]
    layer_count: 3
    layer_max_total_ratio: 0.12
    max_layers_per_signal: 1
    min_add_interval_seconds: 600
    profit_only_add: true
  live:
    layer_ratios: [0.05, 0.04, 0.03]
    layer_count: 3
    layer_max_total_ratio: 0.12
    max_layers_per_signal: 1
    min_add_interval_seconds: 600
    profit_only_add: true
  hinted_only_fields: []
  enforced_fields:
    - layer_ratios
    - layer_max_total_ratio
    - max_layers_per_signal
    - min_add_interval_seconds
    - profit_only_add
  plan_shape_enforced_fields:
    - layer_ratios
  plan_shape_really_enforced: true
  live_layer_shape_source: adaptive_effective_profile
  field_decisions:
    layer_ratios:
      baseline: [0.06, 0.06, 0.04]
      effective: [0.05, 0.04, 0.03]
      live: [0.05, 0.04, 0.03]
      decision: enforced
      source: execution_overrides.layer_ratios
    layer_count:
      baseline: 3
      effective: 3
      live: 3
      decision: derived_from_layer_ratios
```

当 `layering_plan_shape_enforcement_enabled=false` 时，应表现为：

```yaml
adaptive_layering_snapshot:
  effective:
    layer_ratios: [0.05, 0.04, 0.03]
  live:
    layer_ratios: [0.06, 0.06, 0.04]
  hinted_only_fields:
    - layer_ratios
  plan_shape_really_enforced: false
```

---

## 7. 直接可开工的实施任务拆分

## Task 1：先补 plan shape snapshot / audit，不改 live 语义

### 目标

把 `layer_ratios` / `layer_count` / `plan_shape_really_enforced` 的审计链补齐，但默认仍不改 live layer plan。

### 先改哪些文件

- `core/regime_policy.py`
- `trading/executor.py`
- `tests/test_regime.py`
- `tests/test_all.py`

### 要做的事

1. 在 `core/regime_policy.py` 补 plan-shape 级 helper：
   - `build_layering_plan_shape_snapshot(...)`
   - `merge_layer_ratios_conservatively(...)`
   - `derive_layer_count_from_ratios(...)`
2. 在 snapshot 中明确：
   - baseline/effective/live `layer_ratios`
   - baseline/effective/live `layer_count`
   - `plan_shape_really_enforced`
   - `plan_shape_enforced_fields`
   - `plan_shape_ignored_fields`
3. 未开启 `layering_plan_shape_enforcement_enabled` 时：
   - `effective.layer_ratios` 可以存在
   - `live.layer_ratios` 必须继续 baseline
4. 把 `layer_count` 明确做成 derived field，不给独立写 live override 的入口

### 先写哪些测试

1. `test_layering_shape_snapshot_keeps_layer_ratios_hints_only_when_shape_enforcement_disabled`
2. `test_layering_shape_snapshot_derives_layer_count_from_ratios`
3. `test_layering_shape_snapshot_rejects_expanding_layer_count`
4. `test_layering_shape_snapshot_rejects_non_conservative_layer_ratios`
5. `test_layering_shape_snapshot_keeps_live_shape_baseline_on_rollout_miss`

## Task 2：让 executor 能消费 live `layer_ratios`，但只在独立开关 + rollout 下启用

### 目标

让 `trading/executor.py` 在严格 gating 下可以真正消费 live `layer_ratios`，但不改 execution 主骨架语义。

### 先改哪些文件

- `trading/executor.py`
- `core/regime_policy.py`
- `tests/test_all.py`

### 要做的事

1. 在 executor 生成 layer plan 前接入 `adaptive_layering_snapshot.live.layer_ratios`
2. 仅当以下条件全部满足时才允许 live：
   - `layering_profile_enforcement_enabled=true`
   - `layering_plan_shape_enforcement_enabled=true`
   - rollout 命中
   - conservative-only merge 通过
   - `layering_plan_shape_require_guardrails_live=true`
3. 若任一条件不满足：
   - live `layer_ratios` 回退 baseline
   - observability 标注为 hints-only / ignored / rollout_miss / guardrail_not_live
4. executor 只消费 live shape 输入，不改：
   - direction lock
   - intent lifecycle
   - reconcile / self-heal
   - layer reset 主语义

### 先写哪些测试

1. `test_executor_step4_uses_live_layer_ratios_only_when_shape_enforcement_enabled`
2. `test_executor_step4_falls_back_to_baseline_layer_ratios_on_rollout_miss`
3. `test_executor_step4_preserves_guardrail_fields_when_shape_live_enabled`
4. `test_executor_step4_does_not_mutate_intent_or_lock_semantics`

## Task 3：补灰度 / shadow compare / 回滚防线

### 目标

让 Step 4 可以小范围上线，并且一旦异常能独立回滚，不拖累 Step 2/3。

### 先改哪些文件

- `core/regime_policy.py`
- `trading/executor.py`
- `tests/test_regime.py`
- `tests/test_all.py`
- 如项目已有配置样例文件，再补对应配置说明

### 要做的事

1. 增加 plan-shape 专属 rollout / state 字段
2. 在 observability 中补：
   - `shadow_live_layer_ratios`
   - `shadow_live_layer_count`
   - `shape_guardrail_decisions`
   - `shape_live_rollout_match`
3. 出现异常时优先只关闭：
   - `layering_plan_shape_enforcement_enabled`
4. 若异常仍持续，再关闭：
   - `layering_profile_enforcement_enabled`
5. 保持 Step 2 execution guardrails 可单独存活，不要求跟 Step 4 一起回滚

### 先写哪些测试

1. `test_layering_shape_rollout_can_be_disabled_without_disabling_guardrail_live`
2. `test_layering_shape_shadow_compare_emits_baseline_vs_live_diff`
3. `test_layering_shape_rollback_restores_baseline_live_plan`

---

## 实施状态更新（2026-03-27 / 第一批）

- 已完成 **第一批 guarded layering live**：`layer_max_total_ratio`、`max_layers_per_signal`、`min_add_interval_seconds`、`profit_only_add`、`allow_same_bar_multiple_adds` 已在 execution/layering 路径接入 live guardrails。
- 生效前提仍然严格：`execution_profile_enforcement_enabled=true`、`layering_profile_enforcement_enabled=true`、policy mode ∈ `{guarded_execute, full}`、rollout 命中、且 override 通过 conservative-only 校验。
- 默认仍安全关闭；rollout miss / 开关关闭时继续回退 baseline live。
- `layer_ratios` 仍然 **hints-only**，未在本批进入 live layer plan；`layer_count` 仍只作 derived / audit。
- observability 已明确输出 baseline / effective / live / enforced / applied / ignored / field decisions / 是否真正 enforced。

## 8. 配置开关建议

```yaml
adaptive_regime:
  execution:
    layering_profile_hints_enabled: true
    layering_profile_enforcement_enabled: true
    layering_plan_shape_enforcement_enabled: false
    layering_plan_shape_rollout_symbols: []
    layering_plan_shape_rollout_fraction: 0.0
    layering_plan_shape_shadow_compare_enabled: true
    layering_plan_shape_require_guardrails_live: true
    layering_plan_shape_require_step3_stable: true
```

建议解释：

- `layering_profile_hints_enabled`
  - 控制是否产出 layering hints / audit
- `layering_profile_enforcement_enabled`
  - 控制 guardrail-like layering 字段是否 live
- `layering_plan_shape_enforcement_enabled`
  - **Step 4 独立开关**，只控制 `layer_ratios` / plan shape live
- `layering_plan_shape_rollout_symbols`
  - plan-shape 专属灰度 symbol 白名单
- `layering_plan_shape_rollout_fraction`
  - plan-shape 专属灰度比例
- `layering_plan_shape_shadow_compare_enabled`
  - 先对比 baseline/live shape 差异，便于复盘
- `layering_plan_shape_require_guardrails_live`
  - 避免跳过 Step 3 直接开 Step 4
- `layering_plan_shape_require_step3_stable`
  - 用于防止还未稳定就提前开 shape live

---

## 9. 灰度阶段建议

## 阶段 A：shadow only

- `layering_plan_shape_enforcement_enabled=false`
- 只输出 `effective.layer_ratios` 与 shadow diff
- live 仍走 baseline

验收重点：

- baseline / effective / live 可解释
- `layer_count` 派生正确
- 没有出现 live shape 偷偷变化

## 阶段 B：极小范围 rollout

- 只对白名单 symbol 开启
- `layering_plan_shape_rollout_fraction` 极小
- 只允许 conservative-only `layer_ratios` live

验收重点：

- live layer plan 与 audit 一致
- Step 3 guardrails 仍然生效
- 无 intent / lock / reconcile 解释异常

## 阶段 C：扩大 rollout，但仍维持 guarded

- 扩至更多 symbol / 更高 fraction
- 继续保留独立回滚开关
- 继续要求 shadow compare

验收重点：

- `layer_ratios` live 没有制造重复加仓、层序异常、plan reset 异常
- baseline/live 样本可稳定复盘

---

## 10. 验收标准

Step 4 至少要满足以下验收：

1. 未开启 `layering_plan_shape_enforcement_enabled` 时，`layer_ratios` 不得进入 live layer plan
2. 开启后也必须同时满足 rollout 命中 + conservative-only 才能 live
3. `layer_count` 只能是 derived，不得成为独立扩层入口
4. `layer_ratios` live 后，`layer_max_total_ratio / max_layers_per_signal / min_add_interval_seconds / profit_only_add` 仍保持 guardrail 生效
5. 不破坏：
   - direction lock
   - intent lifecycle
   - reconcile / self-heal
   - layer plan reset 主语义
6. 任意一笔样本都能回答：
   - baseline shape 是什么
   - effective shape 是什么
   - live shape 是什么
   - 为什么 live / 为什么没 live
7. rollout miss 时必须回退 baseline
8. 关闭 `layering_plan_shape_enforcement_enabled` 后，能单独回退 Step 4，而不强迫关闭 Step 2/3 guardrails

---

## 11. 失败信号

出现以下任一现象，都应视为 Step 4 告警：

1. 未开启 shape enforcement，但 live `layer_ratios` 已变化
2. rollout miss 仍然消费了 adaptive `layer_ratios`
3. `effective/live_layer_count` 大于 baseline
4. `layer_ratios` 总和超过 baseline 或超过 live `layer_max_total_ratio`
5. 实际加仓节奏解释不清：到底是 `min_add_interval_seconds` 还是 shape 导致
6. layer plan reset / pending intent / direction lock 的解释链开始混乱
7. 真实交易样本出现 baseline/live 审计不一致
8. 关掉 Step 4 后，live plan 仍残留 adaptive shape

---

## 12. 回滚方式

## 12.1 首选回滚顺序

1. 先关闭 `layering_plan_shape_enforcement_enabled`
2. 若异常仍在，再关闭 `layering_profile_enforcement_enabled`
3. 如再需要，回退到 `execution_profile_enforcement_enabled=false`

## 12.2 回滚原则

- Step 4 必须可 **独立回滚**
- 关闭 Step 4 时：
  - `layer_ratios` 回退 baseline live
  - Step 3 guardrail-like live 可继续保留
- 不要把 Step 4 回滚设计成“必须整体回退 M4”

---

## 13. 建议第一批文件

如果按“最小可交付、最低风险”开工，建议第一批只动：

1. `core/regime_policy.py`
2. `trading/executor.py`
3. `tests/test_regime.py`
4. `tests/test_all.py`
5. 配置样例 / 文档入口（如需要）

原因：

- `core/regime_policy.py` 负责 conservative merge、snapshot、derived fields
- `trading/executor.py` 负责受控消费 live shape
- `tests/test_regime.py` 先锁死 merge / gating / derived 规则
- `tests/test_all.py` 先锁死 executor 行为边界

---

## 14. 建议先写的测试名单

建议第一批先写以下测试，再开始 executor 接线：

### regime / snapshot 层

1. `test_merge_layer_ratios_conservatively_accepts_only_tightening_shape`
2. `test_merge_layer_ratios_conservatively_rejects_length_expansion`
3. `test_merge_layer_ratios_conservatively_rejects_sum_expansion`
4. `test_build_layering_shape_snapshot_derives_layer_count`
5. `test_build_layering_shape_snapshot_keeps_hints_only_when_shape_disabled`

### executor 层

6. `test_executor_uses_baseline_shape_when_shape_enforcement_disabled`
7. `test_executor_uses_live_layer_ratios_when_shape_enforcement_enabled_and_rollout_matched`
8. `test_executor_falls_back_to_baseline_shape_on_rollout_miss`
9. `test_executor_step4_preserves_guardrail_live_fields`
10. `test_executor_step4_does_not_change_intent_lock_reconcile_semantics`

---

## 15. 对当前项目最稳的 Step 4 结论

对当前项目，M4 Step 4 的最稳起手式是：

> **先把 `layer_ratios` / `layer_count` / plan-shape audit 补齐，再通过独立 `layering_plan_shape_enforcement_enabled` 小范围放开 `layer_ratios` live；同时继续把 `layer_max_total_ratio`、`max_layers_per_signal`、`min_add_interval_seconds`、`profit_only_add` 当作前置 guardrail，不碰 partial TP / trailing / reconcile / self-heal / intent lifecycle / direction lock 语义。**

重点不是“尽快把 plan shape 开起来”，而是：

- 把 hints、guardrails live、plan shape live 的边界切清楚
- 把 `layer_count` 限死为 derived field
- 把 Step 4 做成可灰度、可单独回滚、可独立审计

---

## 16. 相关文档

- M4 主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
- M4 backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)
- M4 边界方案：[`docs/adaptive-market-regime-m4-boundary-plan.md`](./adaptive-market-regime-m4-boundary-plan.md)
- M4 Step 1：[`docs/adaptive-market-regime-m4-step1-implementation.md`](./adaptive-market-regime-m4-step1-implementation.md)
- M4 Step 2：[`docs/adaptive-market-regime-m4-step2-implementation.md`](./adaptive-market-regime-m4-step2-implementation.md)
- M4 Step 3：[`docs/adaptive-market-regime-m4-step3-implementation.md`](./adaptive-market-regime-m4-step3-implementation.md)

> 一句话收尾：**Step 4 唔系“终于可以随便改 layering shape”，而系“在 guardrails 已经站稳后，先用最细粒度、最强审计、可独立回滚的方式，小范围放开 `layer_ratios` live”。**
