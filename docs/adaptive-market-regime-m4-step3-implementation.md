# Adaptive Market Regime M4 Step 3 实施稿（Guarded Layering Profile）

> 配套主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
>
> 配套 backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)
>
> 配套 M4 边界方案：[`docs/adaptive-market-regime-m4-boundary-plan.md`](./adaptive-market-regime-m4-boundary-plan.md)
>
> 前置步骤：[`docs/adaptive-market-regime-m4-step1-implementation.md`](./adaptive-market-regime-m4-step1-implementation.md)、[`docs/adaptive-market-regime-m4-step2-implementation.md`](./adaptive-market-regime-m4-step2-implementation.md)
>
> 相关执行链文档：[`docs/layering-config-notes.md`](./layering-config-notes.md)、[`docs/layering-acceptance-checklist.md`](./layering-acceptance-checklist.md)

---

## 1. 这份文档要解决什么

这份文档只处理 **M4 Step 3：guarded layering profile**。

它不是再重复 M4 Step 1/2 做过的 execution hints / execution guardrails，而是把 **layering profile 哪些可以开始进入最小 live、生效边界在哪里、先写哪些测试、先改哪些文件、失败怎么退** 拆成可以直接开工的任务。

一句话定义 Step 3：

> **在不改 execution state machine 语义的前提下，让 adaptive regime 只对 layering profile 做“小范围、可回退、只更保守”的收紧；其中 `layer_ratios` 默认继续最谨慎推进，优先让 guardrail 类字段先 live，plan-shape 类字段后 live。**

---

## 2. Step 3 与 Step 2 的边界判词

## 2.1 Step 2 已经做了什么

M4 Step 2 已经允许以下 execution guardrails 小范围真生效：

- `layer_max_total_ratio`
- `max_layers_per_signal`
- `min_add_interval_seconds`
- `profit_only_add`
- `allow_same_bar_multiple_adds`
- `leverage_cap`

这些仍然更接近 **execution 外围纪律收紧**，特点是：

- 对 live entry / add gating 有影响
- 但不直接重写 layer plan 的形状
- 回滚时只要关掉 enforcement 开关，baseline layer plan 语义就能回到原样

## 2.2 Step 3 新增的最小目标

Step 3 的最小目标必须缩到以下 3 件事：

1. **把 guarded layering profile 与 Step 2 execution guardrails 明确分层**
   - Step 2 管外围纪律
   - Step 3 才开始碰 layer plan shape / layering profile 本身

2. **先让少量 layering 字段进入可控 live，仍以 conservative-only 为硬边界**
   - 先 live guardrail-like layering 字段
   - `layer_ratios` 继续后置到 Step 3 后半段 / 第二批灰度

3. **保持当前执行主骨架不变**
   - 不改 direction lock
   - 不改 intents 生命周期
   - 不改 reconcile / self-heal
   - 不改 partial TP / trailing
   - 不改 intent / direction lock 语义

## 2.3 Step 3 明确不做的事

Step 3 明确 **不做**：

- 不碰 partial TP / trailing enforcement 或语义
- 不碰 reconcile / stale close / self-heal 逻辑
- 不碰 open intent 生命周期、状态流转、清理语义
- 不碰 direction lock scope / 创建 / 释放语义
- 不把 layering 改造成更激进的 plan-shape
- 不允许任何字段因为 regime 变得比 baseline 更松
- 不做动态 intent 拆分、补单语义重写、skip layer 语义重写

这句要钉死：

> **Step 3 是 layering profile input shaping，不是 execution state machine rewrite。**

---

## 3. Step 3 最小 live 范围

## 3.1 hints 与 live 的分层

Step 3 仍要坚持 **hints 先于 live，guardrail 先于 plan-shape**。

建议把字段分成三层：

### A. 已可 live 的 Step 2/3 交界 guardrails

这些字段在 Step 3 继续允许 live，因为它们本质仍属于纪律收紧：

- `layer_max_total_ratio`
- `max_layers_per_signal`
- `min_add_interval_seconds`
- `profit_only_add`
- `allow_same_bar_multiple_adds`

### B. Step 3 第一批可进入最小 live 的 layering profile

严格来说，这批字段虽然与 layer plan 有关，但仍较容易解释和回滚：

- `max_layers_per_signal`（若 Step 2 已 live，则 Step 3 继续纳入 layering profile 审计）
- `layer_max_total_ratio`（若 Step 2 已 live，则 Step 3 继续纳入 layering baseline/effective 对照）
- `min_add_interval_seconds`
- `profit_only_add`

这批可以视作 **Step 3 最小 live 包**，因为它们会改变 layering 行为，但仍未重写 plan shape。

### C. Step 3 第二批才允许进入 live 的 plan-shape 字段

- `layer_ratios`

`layer_ratios` 必须被视作 Step 3 最敏感、最后开的字段，因为它直接改变：

- 首层/后续层分配
- layer plan shape
- planned total vs filled sequence 的解释口径
- 当前 layering 主链路验收样本的可比性

所以结论很简单：

> **Step 3 最小 live 范围默认不含 `layer_ratios` 真生效；`layer_ratios` 先 hints-only，再进入第二批 rollout。**

---

## 4. Step 3 字段推进顺序（重点）

这一节直接回答：

- 哪些字段先 live
- 哪些继续 hints-only
- 推进顺序怎样最稳

## 4.1 第一梯队：先 live

### 1) `layer_max_total_ratio`

**定位**：同方向计划总暴露收紧

**建议**：继续 live，且作为 Step 3 layering 审计的核心字段之一。

**原因**：

- 已在 Step 2 范围内较容易解释
- 对 layer plan 的影响是“总量收紧”，不是“结构重排”
- 回滚简单

### 2) `max_layers_per_signal`

**定位**：限制同一 signal 的最大可追加层数

**建议**：继续 live。

**原因**：

- 更像“最多加几层”的纪律约束
- 不直接改每层比例
- 可以和 `layer_ratios` 分开审计

### 3) `min_add_interval_seconds`

**定位**：限制加仓节奏

**建议**：继续 live。

**原因**：

- 对 layering 是节奏约束，不是结构重写
- 回滚和解释都容易

### 4) `profit_only_add`

**定位**：只允许盈利条件下追加

**建议**：继续 live。

**原因**：

- 这是非常典型的 conservative-only 条件
- 对执行骨架侵入小
- 能直接减少高风险补仓

## 4.2 第二梯队：可 live，但要跟 Step 3 observability 一起补强

### 5) `allow_same_bar_multiple_adds`

**定位**：是否允许同 bar 多次追加

**建议**：保持可 live，但在 Step 3 中不作为主角；继续视作附属 guardrail。

**原因**：

- 它不是核心 layering profile 目标
- 但与 `min_add_interval_seconds`、`profit_only_add` 一起能补足节奏纪律

## 4.3 第三梯队：先 hints-only，第二批 rollout 再 live

### 6) `layer_ratios`

**定位**：layer plan shape 本体

**建议**：

- Step 3 第一批：**hints-only**
- Step 3 第二批：在 rollout symbol 小范围真生效

**原因**：

- 最容易污染当前 layering 验收口径
- 最容易让人分不清：是 execution guardrail 变了，还是 layer plan shape 变了
- 对 baseline vs effective plan 的解释要求最高

### `layer_ratios` 的 live 条件建议

只有同时满足以下条件才进入 live：

1. Step 2 guardrails 已稳定
2. Step 3 第一批 live 已稳定
3. 有真实样本能验证 baseline vs effective layer plan
4. rollout symbol 范围足够小
5. 已补齐 `baseline_layer_plan / effective_layer_plan / live_layer_plan` 的审计字段

---

## 5. 字段草案与保守 merge 规则

## 5.1 Step 3 关心的字段清单

```yaml
layering_profile:
  layer_ratios: [0.06, 0.06, 0.04]
  layer_max_total_ratio: 0.16
  max_layers_per_signal: 1
  min_add_interval_seconds: 300
  profit_only_add: false
  allow_same_bar_multiple_adds: false
```

## 5.2 conservative-only 规则

Step 3 必须继续沿用并强化以下规则：

- `layer_ratios`：每层不大于 baseline；总和不大于 baseline 总和
- `layer_max_total_ratio`：只允许下降
- `max_layers_per_signal`：只允许下降
- `min_add_interval_seconds`：只允许上升
- `profit_only_add`：只允许 `False -> True`
- `allow_same_bar_multiple_adds`：只允许 `True -> False`

## 5.3 `layer_ratios` 的额外防呆

`layer_ratios` 进入 Step 3 后，建议再加 4 个额外保护：

1. **长度保护**
   - 不允许长度大于 baseline layer count
   - 默认不扩层

2. **总量保护**
   - sum(effective ratios) 不得大于 baseline sum

3. **单层非负且有序保护**
   - 不能出现负值
   - 不要生成语义异常的层序（例如 layer2 > baseline layer1 且总量又被截断得很怪）

4. **live gating**
   - 未开启 `layering_profile_enforcement_enabled` 时，`layer_ratios` 只能 hints-only

---

## 6. 输出结构草案

## 6.1 Step 3 snapshot 草案

```yaml
adaptive_layering_snapshot:
  baseline:
    layer_ratios: [0.06, 0.06, 0.04]
    layer_max_total_ratio: 0.16
    max_layers_per_signal: 1
    min_add_interval_seconds: 300
    profit_only_add: false
    allow_same_bar_multiple_adds: false
  effective:
    layer_ratios: [0.05, 0.04, 0.03]
    layer_max_total_ratio: 0.12
    max_layers_per_signal: 1
    min_add_interval_seconds: 600
    profit_only_add: true
    allow_same_bar_multiple_adds: false
  live:
    layer_ratios: [0.06, 0.06, 0.04]
    layer_max_total_ratio: 0.12
    max_layers_per_signal: 1
    min_add_interval_seconds: 600
    profit_only_add: true
    allow_same_bar_multiple_adds: false
  effective_state: guarded_layering_partial
  rollout_match: true
  layering_profile_really_enforced: true
  plan_shape_really_enforced: false
  applied_overrides: {}
  ignored_overrides: {}
  enforced_fields:
    - layer_max_total_ratio
    - min_add_interval_seconds
    - profit_only_add
  hinted_only_fields:
    - layer_ratios
```

## 6.2 当 `layer_ratios` 进入第二批 live 时

建议新增：

```yaml
adaptive_layer_plan_audit:
  baseline_layer_plan:
    layer_count: 3
    ratios: [0.06, 0.06, 0.04]
    total_ratio: 0.16
  effective_layer_plan:
    layer_count: 3
    ratios: [0.05, 0.04, 0.03]
    total_ratio: 0.12
  live_layer_plan:
    layer_count: 3
    ratios: [0.05, 0.04, 0.03]
    total_ratio: 0.12
  plan_shape_really_enforced: true
```

这样线上才讲得清：

- baseline plan 长什么样
- effective candidate 长什么样
- live 真正采用了哪套 shape

---

## 7. 配置开关草案

## 7.1 推荐配置结构

```yaml
adaptive_regime:
  enabled: true
  mode: guarded_execute
  guarded_execute:
    execution_profile_hints_enabled: true
    execution_profile_enforcement_enabled: true
    layering_profile_hints_enabled: true
    layering_profile_enforcement_enabled: false
    layering_plan_shape_enforcement_enabled: false
    exit_profile_hints_enabled: false
    exit_profile_enforcement_enabled: false
    conservative_only: true
    rollout_symbols: ["BTC/USDT"]
  defaults:
    policy_version: adaptive_policy_v1_m4_step3
  regimes:
    high_vol:
      execution_overrides:
        layer_max_total_ratio: 0.12
        max_layers_per_signal: 1
        min_add_interval_seconds: 600
        profit_only_add: true
        layer_ratios: [0.05, 0.04, 0.03]
```

## 7.2 开关含义

- `layering_profile_hints_enabled`
  - 允许输出 Step 3 layering snapshot / audit
- `layering_profile_enforcement_enabled`
  - 允许 Step 3 第一批 layering 字段 live（不含 plan shape）
- `layering_plan_shape_enforcement_enabled`
  - 允许 `layer_ratios` 真正进入 live layer plan
  - 默认应关闭

## 7.3 默认推荐

Step 3 默认推荐配置：

- `execution_profile_enforcement_enabled=true`
- `layering_profile_hints_enabled=true`
- `layering_profile_enforcement_enabled=true`
- `layering_plan_shape_enforcement_enabled=false`

也即：

> **先让 Step 3 的 layering 审计完整上线，并让 guardrail-like layering 字段 live；但 `layer_ratios` 仍先 hints-only。**

---

## 8. 建议先改哪些文件

按最稳妥顺序，建议第一批文件如下：

1. `core/regime_policy.py`
   - 收口 Step 3 layering snapshot / plan-shape gating / conservative merge
2. `tests/test_regime.py`
   - 先锁 Step 3 字段边界与 ignored reason
3. `trading/executor.py`
   - 接 live layering profile / live plan-shape gating / audit 输出
4. `tests/test_all.py`
   - 补 executor 行为测试
5. `config/config.yaml.example`
   - 预埋 Step 3 开关与样例
6. `docs/adaptive-market-regime-m4-step3-implementation.md`
   - 本文
7. `docs/adaptive-market-regime-backlog.md`
8. `docs/adaptive-market-regime-framework-plan.md`
9. `docs/adaptive-market-regime-m4-boundary-plan.md`
10. `README.md`

如果只做第一刀，建议顺序是：

- 先 `core/regime_policy.py`
- 再 `tests/test_regime.py`
- 再 `trading/executor.py`
- 再 `tests/test_all.py`

---

## 9. 先写哪些测试

## 9.1 `tests/test_regime.py`

建议先写以下 Step 3 边界测试：

1. `test_layering_snapshot_keeps_layer_ratios_hints_only_when_plan_shape_disabled`
2. `test_layering_snapshot_enforces_guardrail_fields_before_layer_ratios`
3. `test_layering_snapshot_rejects_non_conservative_layer_ratios`
4. `test_layering_snapshot_rejects_expanding_layer_count`
5. `test_layering_snapshot_records_plan_shape_disabled_reason`
6. `test_layering_snapshot_marks_live_fields_and_hinted_fields_separately`

## 9.2 `tests/test_all.py`

建议再写以下 executor 行为测试：

1. `test_executor_step3_uses_live_layering_guardrails_without_mutating_layer_ratios`
2. `test_executor_step3_keeps_baseline_plan_shape_when_shape_enforcement_disabled`
3. `test_executor_step3_uses_effective_layer_ratios_only_when_shape_enforcement_enabled`
4. `test_executor_step3_emits_baseline_effective_live_layer_plan_audit`
5. `test_executor_step3_rollout_miss_falls_back_to_baseline_layering`

## 9.3 测试顺序建议

1. 先写 snapshot helper / merge helper 单测
2. 再写 live gating 单测
3. 最后写 executor 行为级测试

先把测试写死，能避免 Step 3 做着做着越界碰到 state machine。

---

## 10. 分批实施任务拆分

## Task 1：补 Step 3 layering snapshot helper

### 目标

在 `core/regime_policy.py` 补齐 Step 3 专用 layering snapshot：

- `baseline`
- `effective`
- `live`
- `enforced_fields`
- `hinted_only_fields`
- `plan_shape_really_enforced`
- `ignored_overrides`

### 本任务重点

- 把 `layer_ratios` 与其他 guardrail 字段分开处理
- live profile 与 effective profile 要明确区分
- 未开启 shape enforcement 时，`layer_ratios` 只能留在 hints

## Task 2：在 executor 接 live layering profile，但先不放开 `layer_ratios`

### 目标

让 executor 真正消费 Step 3 第一批 live layering 字段：

- `layer_max_total_ratio`
- `max_layers_per_signal`
- `min_add_interval_seconds`
- `profit_only_add`
- `allow_same_bar_multiple_adds`

但 `layer_ratios` 继续保持 baseline live。

### 本任务重点

- live layer plan 仍以 baseline ratios 建立
- 先让总量/节奏/条件收紧生效
- 补 audit，让线上能看见 baseline/effective/live 差异

## Task 3：补 `layer_ratios` 的第二批 rollout 能力

### 目标

在不默认开启的前提下，为第二批 rollout 做好 `layer_ratios` 真生效能力。

### 本任务重点

- 配置必须独立开关
- rollout symbol 必须命中
- plan-shape audit 字段必须齐全
- 默认保持关闭

## Task 4：补 observability / intent / trade 摘要入口

### 目标

让至少一条稳定链路能追到 Step 3 layering 生效摘要。

### 最少字段

- `policy_mode`
- `policy_version`
- `regime_name`
- `effective_state`
- `enforced_fields`
- `hinted_only_fields`
- `plan_shape_really_enforced`
- `rollout_match`

## Task 5：补配置样例与文档入口

### 目标

把 Step 3 开关和入口写进：

- `config/config.yaml.example`
- `README.md`
- backlog / framework / boundary 文档

---

## 11. 灰度方式

## 阶段 A：Step 3 hints + partial live（推荐默认）

- `execution_profile_enforcement_enabled=true`
- `layering_profile_hints_enabled=true`
- `layering_profile_enforcement_enabled=true`
- `layering_plan_shape_enforcement_enabled=false`
- `rollout_symbols=["BTC/USDT"]`

效果：

- `layer_max_total_ratio / max_layers_per_signal / min_add_interval_seconds / profit_only_add / allow_same_bar_multiple_adds` 可 live
- `layer_ratios` 继续 hints-only

## 阶段 B：小范围放开 `layer_ratios`

- 保持阶段 A 全部条件
- 再打开 `layering_plan_shape_enforcement_enabled=true`
- rollout symbol 仍保持极小范围

效果：

- `layer_ratios` 小范围进入 live layer plan
- 必须同步观察 baseline/effective/live plan 差异

## 阶段 C：扩大 symbol 范围

只有在以下条件满足才扩大：

- Step 3 第一批 live 稳定
- `layer_ratios` rollout 无异常
- intent / lock / reconcile / self-heal 指标无异常抬升

---

## 12. 验收标准

## 12.1 功能验收

- Step 3 默认能输出 `adaptive_layering_snapshot`
- 未开启 shape enforcement 时，`layer_ratios` 不进入 live plan
- 开启 shape enforcement 时，`layer_ratios` 才进入 live plan
- rollout miss 时，live 退回 baseline

## 12.2 边界验收

- 不改 partial TP / trailing
- 不改 reconcile / self-heal
- 不改 intent 生命周期
- 不改 direction lock 语义
- 不把 Step 3 变成 deeper execution rewrite

## 12.3 可解释性验收

每条 Step 3 影响样本，至少能回答：

- baseline layering profile 是什么
- effective layering candidate 是什么
- live layering profile 是什么
- 哪些字段真生效
- 哪些字段只是 hints-only
- `layer_ratios` 是否已进入 live plan

## 12.4 稳定性验收

必须证明：

- `layer plan reset` 没被破坏
- `direction lock` 无异常残留/异常释放
- `open intents` 无异常积压
- `reconcile issue` 无明显抬升
- `self-heal` 频率无明显异常上涨

---

## 13. 失败信号

以下信号一旦出现，应暂停 Step 3 扩量或立即回滚：

1. `layer_ratios` 明明未开启 shape enforcement，却已改变 live layer plan
2. 线上无法区分 baseline/effective/live layer plan
3. `layer plan reset`、intent、lock、reconcile、自愈异常抬升
4. rollout 外 symbol 误命中 live layering profile
5. `ignored_overrides` 大量出现且 reason 不稳定
6. partial TP / trailing 的解释复杂度突然上升

第 6 点尤其重要：

> **只要 Step 3 开始让人怀疑 partial TP / trailing 被顺手改了，就说明越界了。**

---

## 14. 回滚方式

回滚顺序建议：

1. `layering_plan_shape_enforcement_enabled=true -> false`
2. `layering_profile_enforcement_enabled=true -> false`
3. `execution_profile_enforcement_enabled=true -> false`
4. `mode=guarded_execute -> decision_only / observe_only`
5. `enabled=false`

回滚原则：

- **先回 plan-shape，再回 guardrails**
- 保留 hints / audit，方便复盘
- 优先走配置回滚，不依赖热修代码

---

## 15. 建议提交批次

### Commit A
- 文档：本实施稿 + README / backlog / framework / boundary 入口

### Commit B
- `core/regime_policy.py`
- `tests/test_regime.py`

### Commit C
- `trading/executor.py`
- `tests/test_all.py`

### Commit D
- `config/config.yaml.example`
- observability / intent / trade 摘要字段

如果想再稳一点，完全可以先只合入 Commit A，把 Step 3 边界钉死，再开代码实施。

---

## 16. 当前建议结论

对当前项目，M4 Step 3 的最稳起手式是：

> **先上线 Step 3 layering audit 与 guardrail-like live；`layer_max_total_ratio / max_layers_per_signal / min_add_interval_seconds / profit_only_add` 继续可 live；`layer_ratios` 先 hints-only，等第二批 rollout 再进入 live。**

换句话说：

- Step 2 负责 execution 外围 guardrails
- Step 3 开始处理 layering profile，但仍先避开最深的 plan-shape 改写
- `layer_ratios` 是 Step 3 最后开的开关，不是起手式

---

## 17. 建议第一批文件（可直接开工）

建议第一批真正动手的文件：

1. `core/regime_policy.py`
2. `tests/test_regime.py`
3. `trading/executor.py`
4. `tests/test_all.py`
5. `config/config.yaml.example`

文档与入口同步文件：

6. `docs/adaptive-market-regime-m4-step3-implementation.md`
7. `docs/adaptive-market-regime-backlog.md`
8. `docs/adaptive-market-regime-framework-plan.md`
9. `docs/adaptive-market-regime-m4-boundary-plan.md`
10. `README.md`

---

## 18. 相关文档

- 主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
- Backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)
- M4 边界方案：[`docs/adaptive-market-regime-m4-boundary-plan.md`](./adaptive-market-regime-m4-boundary-plan.md)
- M4 Step 1：[`docs/adaptive-market-regime-m4-step1-implementation.md`](./adaptive-market-regime-m4-step1-implementation.md)
- M4 Step 2：[`docs/adaptive-market-regime-m4-step2-implementation.md`](./adaptive-market-regime-m4-step2-implementation.md)
- Layering 配置：[`docs/layering-config-notes.md`](./layering-config-notes.md)
- Layering 验收清单：[`docs/layering-acceptance-checklist.md`](./layering-acceptance-checklist.md)

> 一句话收尾：**Step 3 先让 layering profile 审计与 guardrails 真正站稳，再最后小范围放开 `layer_ratios`；千万唔好一上来就改 plan shape。**
