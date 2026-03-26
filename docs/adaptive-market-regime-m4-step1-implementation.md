# Adaptive Market Regime M4 Step 1 实施稿（Execution Profile Hints）

> 配套主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
>
> 配套 backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)
>
> 配套 M4 边界方案：[`docs/adaptive-market-regime-m4-boundary-plan.md`](./adaptive-market-regime-m4-boundary-plan.md)
>
> 相关执行链文档：[`docs/layering-config-notes.md`](./layering-config-notes.md)、[`docs/layering-acceptance-checklist.md`](./layering-acceptance-checklist.md)

---

## 1. 这份文档要解决什么

这份文档只处理 **M4 Step 1**：

> **先把 execution profile hints / effective snapshot / observability 做完整，先让系统稳定回答“如果 M4 execution adaptation 生效，会怎样”，但暂时不直接改变 execution profile 的真实生效结果。**

它的作用不是再讲一遍 M4 能不能做，而是把 **Step 1 拆成可直接开工、可分批提交、可灰度、可回滚** 的实施任务，避免开发时一边写 hints，一边偷偷越界写成 enforcement。

---

## 2. Step 1 的边界判词

## 2.1 最小目标

M4 Step 1 的最小目标必须钉死成以下 3 件事：

1. **execution profile hints**
   - 生成 execution 层的 `baseline vs effective` 候选快照
   - 但默认只作为 hint / candidate，不改实际下单参数

2. **effective snapshot**
   - 给 executor / intent / trade / observability 提供统一的 execution snapshot 结构
   - 明确记录 `applied / ignored / rollout_match / effective_state`

3. **observability**
   - 让后续可以回答：
     - baseline execution profile 是什么
     - effective hint 是什么
     - 哪些 override 会收紧
     - 哪些 override 被忽略
     - 这次属于 hints-only、bypassed、还是未来可 enforcement 的 candidate

换句话说：

> Step 1 先做“看得见、讲得清、能复盘”的 execution guardrail 基础层，**唔直接改变 execution profile 真生效**。

## 2.2 当前阶段明确不做的事

Step 1 明确 **不做**：

- 不把 `effective_execution_profile` 真写回 entry sizing / layer plan / real order params
- 不改变 `layer_ratios` 的真实执行结果
- 不改变 `layer_max_total_ratio` 的真实执行结果
- 不改变 `max_layers_per_signal` / `min_add_interval_seconds` / `profit_only_add` 的真实执行结果
- 不改变 `direction lock / intents / reconcile / self-heal` 的语义
- 不碰 partial TP / trailing 的真实 enforcement
- 不把 Step 1 伪装成“先小范围真生效一点点”但文档不写清楚

## 2.3 Step 1 与后续步骤的边界

### Step 1 做什么

Step 1 只做：

- `baseline_execution_profile` 统一收口
- `effective_execution_profile_hint` 统一收口
- conservative-only merge / ignore 逻辑
- execution hints / observability / audit fields
- rollout 命中判断与 `effective_state` 标记
- 单元测试与文档，把执行边界锁死

### Step 2 才做什么

Step 2 才进入 **guarded execution profile enforcement**：

- 让 `leverage_cap`
- `layer_max_total_ratio`
- `max_layers_per_signal`
- `min_add_interval_seconds`
- `profit_only_add`

在 rollout symbol + enforcement 开关齐全前提下，小范围真正生效。

### Step 3 / 后续再做什么

M4 后续阶段才考虑：

- `layer_ratios` 的 guarded layering enforcement
- `layer_count / plan shape` 的更深层保守化
- trailing / partial TP hints
- trailing / partial TP enforcement（最后段，默认继续后置）

---

## 0. 实施状态（2026-03-26）

- `core/regime_policy.py` 已补齐 `build_execution_baseline_snapshot()`、`merge_execution_overrides_conservatively()`、`build_execution_effective_snapshot()`。
- `trading/executor.py` 已在 observability / plan_context 链路接入统一 `adaptive_execution_snapshot` 与 `adaptive_execution_hints`。
- 当前阶段保持 **hints-only**：真实 `entry_plan / layer_plan / order sizing` 继续走 baseline，不提前进入 execution enforcement。
- 已补测试覆盖：conservative-only merge、ignored reasons、rollout mismatch、JSON serializable、以及 hints-only 不改变 live execution inputs。

## 3. Step 1 完成后的交付定义

完成 Step 1 后，系统至少应能做到：

1. 对每次 execution plan / executor 入口，产出一份结构化 `baseline_execution_profile`
2. 对同一次执行评估，产出一份结构化 `effective_execution_profile_hint`
3. 清楚标记本次状态：
   - `hints_only`
   - `bypassed`
   - `disabled`
   - `rollout_not_matched`
4. 记录：
   - `applied_overrides`
   - `ignored_overrides`
   - `would_tighten_fields`
   - `hint_codes`
   - `notes`
5. 让主链路可观察到：
   - baseline execution profile
   - hinted execution profile
   - 本次如果 enforcement，会收紧哪些执行参数
6. 默认配置下，**真实下单 / 分仓 / 加仓行为保持不变**

---

## 4. 建议实施顺序（直接可开工）

## Task 1：补 execution profile resolver 骨架

### 目标

在 executor 真正消费 execution 参数前，先把 **baseline config → conservative merge → effective hint snapshot** 这条链路收口。

### 建议新增/调整文件

- `core/regime_policy.py`
- `trading/executor.py`
- `tests/test_regime.py`

### 要做的事

1. 在 `core/regime_policy.py` 增加 execution 专用 helper，建议函数名方向：
   - `build_execution_baseline_snapshot(...)`
   - `build_execution_effective_snapshot(...)`
   - `merge_execution_overrides_conservatively(...)`
2. baseline snapshot 先只覆盖当前 execution / layering 已真实消费、且适合 M4 起手式的字段：
   - `layer_ratios`
   - `layer_max_total_ratio`
   - `max_layers_per_signal`
   - `min_add_interval_seconds`
   - `profit_only_add`
   - `allow_same_bar_multiple_adds`
   - `leverage_cap`
3. effective snapshot 只允许保守收紧：
   - `layer_ratios` 单层不大于 baseline
   - `layer_ratios` 总和不大于 baseline 总和
   - `layer_max_total_ratio` 只允许下降
   - `max_layers_per_signal` 只允许下降
   - `min_add_interval_seconds` 只允许上升
   - `profit_only_add` 只允许 `False -> True`
   - `allow_same_bar_multiple_adds` 只允许 `True -> False`
   - `leverage_cap` 只允许下降
4. snapshot 需带上来源信息：
   - `policy_mode`
   - `policy_version`
   - `policy_source`
   - `regime_name`
   - `regime_confidence`
   - `stability_score`
   - `transition_risk`
   - `rollout_match`

### 输出结构草案

```yaml
adaptive_execution_snapshot:
  baseline:
    layer_ratios: [0.06, 0.06, 0.04]
    layer_max_total_ratio: 0.16
    max_layers_per_signal: 1
    min_add_interval_seconds: 300
    profit_only_add: false
    allow_same_bar_multiple_adds: false
    leverage_cap: 3
  effective:
    layer_ratios: [0.05, 0.05, 0.03]
    layer_max_total_ratio: 0.13
    max_layers_per_signal: 1
    min_add_interval_seconds: 600
    profit_only_add: true
    allow_same_bar_multiple_adds: false
    leverage_cap: 2
  effective_state: hints_only
  policy_mode: guarded_execute
  policy_version: adaptive_policy_v1_m4_step1
  regime_name: high_vol
  regime_confidence: 0.74
  stability_score: 0.58
  transition_risk: 0.41
  rollout_match: true
  applied_overrides:
    layer_max_total_ratio:
      baseline: 0.16
      effective: 0.13
      source: execution_overrides.layer_max_total_ratio
    min_add_interval_seconds:
      baseline: 300
      effective: 600
      source: execution_overrides.min_add_interval_seconds
  ignored_overrides:
    leverage_cap:
      requested: 5
      ignored_reason: non_conservative_override
```

---

## Task 2：在 executor 内接 execution snapshot 与 hints，但不改真实执行

### 目标

让 `trading/executor.py` 在 Step 1 里稳定输出 execution hints / observability，但默认不改变 entry plan、layer plan 与下单参数。

### 建议新增/调整文件

- `trading/executor.py`
- `tests/test_all.py`
- `tests/test_regime.py`

### 要做的事

1. 在 executor 构建 entry plan / layering plan 前，先生成：
   - `adaptive_execution_snapshot`
   - `adaptive_execution_hints`
   - `adaptive_execution_observability`
2. 当前真实执行仍使用 baseline：
   - baseline `trading.layering.*`
   - baseline `risk / entry sizing` 已存在真实生效结果
   - Step 1 只记录 hypothetical effective hint
3. 新增 would-have / would-tighten 观测字段，但不接管真实结果：
   - `would_reduce_layer_ratios`
   - `would_reduce_layer_max_total_ratio`
   - `would_reduce_max_layers_per_signal`
   - `would_increase_min_add_interval_seconds`
   - `would_enable_profit_only_add`
   - `would_disable_same_bar_multiple_adds`
   - `would_reduce_leverage_cap`
4. 如果 baseline 本来已 block / skip add / deny add，也照样输出 hint，保证复盘口径一致。
5. 明确记录：
   - executor 当前仍使用 baseline
   - Step 1 hint 未接管真实 execution profile

### 建议字段草案

```yaml
adaptive_execution_hints:
  enabled: true
  effective_state: hints_only
  baseline_execution_source: trading.layering
  hinted_execution_source: adaptive_regime.execution_overrides
  would_change_execution_profile: true
  would_tighten_fields:
    - layer_ratios
    - layer_max_total_ratio
    - min_add_interval_seconds
    - profit_only_add
  hint_codes:
    - WOULD_REDUCE_LAYER_TOTAL
    - WOULD_SLOW_ADD_INTERVAL
    - WOULD_ENABLE_PROFIT_ONLY_ADD
  notes:
    - step1 keeps baseline execution profile live
```

---

## Task 3：先把 effective snapshot 接到 observability 入口

### 目标

让 Step 1 不只是“executor 里算过”，而是后续 intent / trade / logs / dashboard / analytics 都追得到。

### 建议新增/调整文件

- `trading/executor.py`
- `core/models.py` / 对应 trade serialization 模块（按项目实际结构）
- `dashboard/` API 序列化层（如已有 execution observability 输出）
- `tests/test_all.py`

### 要做的事

1. 至少选择一条稳定链路挂出 snapshot 摘要：
   - intent metadata
   - open trade metadata
   - execution log context
   - dashboard execution state API
2. 当前 Step 1 建议优先挂：
   - execution log context
   - execution state / dashboard API
   - intent metadata 摘要
3. 详细 snapshot 可先存摘要 + `policy_version + regime_name + effective_state + applied/ignored keys`，避免记录体积过大。
4. 保留单一事实来源：
   - executor 内部有完整 snapshot
   - 外部记录用摘要，不要在多处再发明不同字段名

### 最少应暴露的摘要字段

- `policy_mode`
- `policy_version`
- `regime_name`
- `effective_state`
- `rollout_match`
- `applied_override_keys`
- `ignored_override_keys`
- `would_tighten_fields`

---

## Task 4：统一 conservative-only 防呆与配置开关

### 目标

先把 Step 1 的配置边界卡死，避免后续误把 execution enforcement / layering enforcement 偷偷做进去。

### 建议新增/调整文件

- `core/config.py`
- `core/regime_policy.py`
- `config/config.yaml.example`
- `docs/adaptive-market-regime-m4-boundary-plan.md`

### 配置草案

```yaml
adaptive_regime:
  enabled: true
  mode: observe_only
  guarded_execute:
    execution_profile_hints_enabled: true
    execution_profile_enforcement_enabled: false
    layering_profile_enforcement_enabled: false
    exit_profile_hints_enabled: false
    exit_profile_enforcement_enabled: false
    conservative_only: true
    rollout_symbols: []
  defaults:
    policy_version: adaptive_policy_v1_m4_step1
  regimes:
    high_vol:
      execution_overrides:
        layer_ratios: [0.05, 0.05, 0.03]
        layer_max_total_ratio: 0.13
        min_add_interval_seconds: 600
        profit_only_add: true
        leverage_cap: 2
    transition_risk_high:
      execution_overrides:
        layer_max_total_ratio: 0.10
        max_layers_per_signal: 1
        min_add_interval_seconds: 900
```

### 规则要求

- `execution_profile_hints_enabled=true`：允许生成 execution hints
- `execution_profile_enforcement_enabled=false`：Step 1 默认不真生效 execution profile
- `layering_profile_enforcement_enabled=false`：Step 1 明确不让 `layer_ratios` 进入真实 layer plan
- `exit_profile_hints_enabled=false`：Step 1 先不展开 trailing / partial TP hints
- `conservative_only=true`：拒绝所有放宽型 override
- `rollout_symbols`：Step 1 先只做 observability 命中判断，不作为真实 enforcement 条件

---

## Task 5：补测试，先保边界再保功能

### 目标

先把 Step 1 的边界写成测试，避免后续开发滑向 Step 2 / deeper layering enforcement。

### 建议第一批测试

#### A. `tests/test_regime.py`
1. `test_execution_snapshot_keeps_baseline_when_no_override`
2. `test_execution_snapshot_applies_only_conservative_numeric_tightening`
3. `test_execution_snapshot_rejects_non_conservative_layer_ratio_expansion`
4. `test_execution_snapshot_rejects_non_conservative_boolean_relaxation`
5. `test_execution_snapshot_records_ignored_override_reason`
6. `test_execution_snapshot_includes_regime_policy_rollout_metadata`

#### B. `tests/test_all.py`
1. `test_executor_step1_emits_hints_without_changing_entry_plan`
2. `test_executor_step1_emits_layering_hint_without_mutating_live_layer_plan`
3. `test_executor_step1_observability_payload_is_json_serializable`
4. `test_executor_step1_records_rollout_mismatch_as_hint_only`
5. `test_executor_step1_keeps_execution_profile_baseline_when_enforcement_disabled`

### 测试顺序建议

1. 先写 execution snapshot helper 单测
2. 再写 executor hints 行为测试
3. 最后补配置防呆 / conservative-only 测试

---

## Task 6：补文档入口与 backlog 状态

### 目标

让 Step 1 不只是“代码里有设计”，而是 README / framework / backlog / boundary 都找得到入口。

### 建议新增/调整文件

- `README.md`
- `docs/adaptive-market-regime-backlog.md`
- `docs/adaptive-market-regime-framework-plan.md`
- `docs/adaptive-market-regime-m4-boundary-plan.md`

### 要做的事

- 在 README 的 adaptive regime 文档入口加入 M4 Step 1 实施稿
- 在 backlog 的 M4 段落补 `AR-M4-01 / AR-M4-02` 的 Step 1 执行说明
- 在 framework plan 的 M4 段落明确 Step 1 / Step 2 / 后续 layering / exit 的边界
- 在 M4 boundary 文档“相关文档”里补 Step 1 链接

---

## 5. 先改哪些文件（建议第一批）

按最稳妥顺序，建议第一批文件如下：

1. `core/regime_policy.py`
   - 先收口 execution snapshot / conservative merge helper
2. `tests/test_regime.py`
   - 先锁 Step 1 边界
3. `trading/executor.py`
   - 再接 snapshot / hints / observability
4. `tests/test_all.py`
   - 补 executor 行为级测试
5. `config/config.yaml.example`
   - 预埋 M4 Step 1 配置开关与样例
6. `docs/adaptive-market-regime-m4-step1-implementation.md`
   - 保持文档与实现同步
7. `docs/adaptive-market-regime-backlog.md`
8. `docs/adaptive-market-regime-framework-plan.md`
9. `docs/adaptive-market-regime-m4-boundary-plan.md`
10. `README.md`

如果只做最小第一刀，可以先做前 4 个文件，再补配置样例与文档入口。

---

## 6. 字段草案（Step 1 版本）

## 6.1 execution snapshot 建议字段

```yaml
adaptive_execution_snapshot:
  baseline: {}
  effective: {}
  effective_state: hints_only
  policy_mode: observe_only
  policy_version: adaptive_policy_v1_m4_step1
  policy_source: adaptive_regime.regimes.high_vol.execution_overrides
  regime_name: high_vol
  regime_confidence: 0.74
  stability_score: 0.58
  transition_risk: 0.41
  rollout_match: false
  applied_overrides: {}
  ignored_overrides: {}
```

## 6.2 execution hints 建议字段

```yaml
adaptive_execution_hints:
  enabled: true
  baseline_result_profile: baseline_only
  hinted_result_profile: effective_candidate
  would_change_execution_profile: true
  would_tighten_fields: []
  hint_codes: []
  notes: []
```

## 6.3 execution observability 建议字段

```yaml
adaptive_execution_observability:
  phase: m4_step1
  state: hints_only
  enforcement_enabled: false
  layering_enforcement_enabled: false
  exit_enforcement_enabled: false
  conservative_only: true
  rollout_match: false
```

## 6.4 hint code 草案

建议先用稳定、可聚合的 code，而唔好一开始散落自由文本：

- `WOULD_REDUCE_LAYER_RATIO`
- `WOULD_REDUCE_LAYER_TOTAL`
- `WOULD_REDUCE_MAX_LAYERS_PER_SIGNAL`
- `WOULD_SLOW_ADD_INTERVAL`
- `WOULD_ENABLE_PROFIT_ONLY_ADD`
- `WOULD_DISABLE_SAME_BAR_ADDS`
- `WOULD_REDUCE_LEVERAGE_CAP`
- `IGNORED_NON_CONSERVATIVE_OVERRIDE`
- `ROLLOUT_SYMBOL_NOT_MATCHED`
- `EXECUTION_ENFORCEMENT_DISABLED`
- `LAYERING_ENFORCEMENT_DISABLED`

## 6.5 ignored reason 草案

- `non_conservative_override`
- `unsupported_execution_field`
- `mode_not_effective`
- `execution_enforcement_disabled`
- `layering_enforcement_disabled`
- `rollout_symbol_not_matched`
- `low_regime_confidence`

---

## 7. 灰度方式（Step 1）

Step 1 的灰度重点不是“真执行有冇变得更保守”，而是：

> **先验证 effective execution snapshot 够不够清楚，hint 是否稳定，would-tighten 结果是否可信。**

### 阶段 A：全量 hints-only

- `mode=observe_only` 或 `decision_only`
- `execution_profile_hints_enabled=true`
- `execution_profile_enforcement_enabled=false`
- `layering_profile_enforcement_enabled=false`

目标：先看字段稳定性、日志可读性、hint code 聚合度。

### 阶段 B：guarded_execute 但仍 hints-only

- `mode=guarded_execute`
- `execution_profile_enforcement_enabled=false`
- `layering_profile_enforcement_enabled=false`
- `rollout_symbols=["BTC/USDT"]`

目标：验证将来 Step 2 真生效时，会命中哪些 symbol、哪些 execution field，但仍不改变真实执行。

### 阶段 C：移交 Step 2

只有在 Step 1 hints 数据足够清楚后，才进入 guarded execution profile enforcement。

---

## 8. 验收标准

Step 1 验收不看“执行参数收紧了几多”，而看是否把边界与可解释性立住。

### 8.1 功能验收

- executor 每次进入执行评估都能稳定输出 `adaptive_execution_snapshot`
- 存在 execution override 时，能看到 `baseline vs effective`
- 非保守 override 会进入 `ignored_overrides`
- 默认配置下，不改变当前 entry plan / layer plan / 下单参数

### 8.2 可解释性验收

- 能明确看见 `regime_name / confidence / stability_score / transition_risk`
- 能明确看见 `policy_mode / policy_version / policy_source`
- 能明确看见 `would_tighten_fields / hint_codes / notes`
- 能明确回答“这次只是 hint，还是已经进入 enforcement”

### 8.3 边界验收

- Step 1 不把 effective execution snapshot 真写入 live execution profile
- Step 1 不改变 direction lock / intents / reconcile / self-heal 语义
- Step 1 不提前进入 `layer_ratios` 真生效
- Step 1 不提前进入 trailing / partial TP hints/enforcement

### 8.4 工程验收

- 新字段可 JSON 序列化
- `observe_only` 下主链路兼容
- 旧 log / dashboard / API 缺字段时不会报错
- 文档、配置、测试对 Step 1 边界口径一致

---

## 9. 失败信号

一旦出现以下信号，应视为 Step 1 越界、或至少实现不合格：

1. **真实 entry plan / layer plan 行为发生变化**，但配置并未打开 `execution_profile_enforcement_enabled`
2. `adaptive_execution_snapshot` 存在，但 baseline/effective 差异无法解释来源
3. `ignored_overrides` 大量出现，但没有稳定 reason code
4. hint 输出不稳定，同一种情况每次字段命名或写法都不同
5. 线上无法回答“这笔 intent / trade 是 baseline 执行，还是只是 hinted execution candidate”
6. Step 1 改动波及 direction lock / intents / reconcile / self-heal / partial TP / trailing

---

## 10. 回滚方式

Step 1 必须做到 **配置可回退、代码无需热修就能止血**。

### 配置回滚顺序

1. `execution_profile_hints_enabled=true -> false`
2. `mode=guarded_execute -> decision_only`
3. `mode=decision_only -> observe_only`
4. `adaptive_regime.enabled=false`

### 回滚原则

- 回滚后，executor 主逻辑应立即退回旧路径
- 若只关 hints，baseline execution 行为不受影响
- 即使回滚，也建议保留最小 observe-only snapshot 入口，方便排查

---

## 11. 建议拆分成的提交批次

### Commit A（最小可审阅）
- 文档：本实施稿 + 入口互链

### Commit B（骨架）
- `core/regime_policy.py` execution snapshot helper
- `tests/test_regime.py` 边界测试

### Commit C（接入）
- `trading/executor.py` 接 snapshot + hints + observability
- `tests/test_all.py` 行为测试

### Commit D（配置与可观察性入口）
- `config/config.yaml.example`
- execution state / dashboard / intent metadata 摘要字段

如果想更稳，当前这轮也完全可以先只完成 Commit A，把任务边界先钉死，再开代码实施。

---

## 12. 当前建议结论

对当前项目，M4 Step 1 的最稳起手式是：

> **先做 execution profile hints / effective snapshot / observability，并明确保持 hints-only；把 execution profile enforcement 与 layering profile enforcement 严格留到后续步骤。**

这样做的好处是：

- 不打扰当前 layering 主链路真实验收
- 不让 M4 一上来变成“执行层已经偷偷变更”
- 先把 baseline / effective 差异记录清楚
- 为后续 Step 2 提供真实样本，而不是靠拍脑袋调 execution 参数

---

## 13. 建议第一批文件（可直接开工）

建议第一批真正动手的文件：

1. `core/regime_policy.py`
2. `tests/test_regime.py`
3. `trading/executor.py`
4. `tests/test_all.py`
5. `config/config.yaml.example`

文档与入口同步文件：

6. `docs/adaptive-market-regime-m4-step1-implementation.md`
7. `docs/adaptive-market-regime-backlog.md`
8. `docs/adaptive-market-regime-framework-plan.md`
9. `docs/adaptive-market-regime-m4-boundary-plan.md`
10. `README.md`

---

## 14. 相关文档

- 主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
- Backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)
- M4 边界方案：[`docs/adaptive-market-regime-m4-boundary-plan.md`](./adaptive-market-regime-m4-boundary-plan.md)
- Layering 配置：[`docs/layering-config-notes.md`](./layering-config-notes.md)
- Layering 验收清单：[`docs/layering-acceptance-checklist.md`](./layering-acceptance-checklist.md)

> 一句话收尾：**Step 1 先让 executor 讲清楚“如果要收紧，会怎样收紧”；Step 2 才去决定要不要真收紧。**
