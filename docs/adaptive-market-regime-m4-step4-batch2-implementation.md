# Adaptive Market Regime M4 Step 4 第二批实施稿（`layer_ratios` / Plan Shape Guarded Live）

> 配套主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
>
> 配套 backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)
>
> 配套 M4 边界方案：[`docs/adaptive-market-regime-m4-boundary-plan.md`](./adaptive-market-regime-m4-boundary-plan.md)
>
> 前置步骤：[`docs/adaptive-market-regime-m4-step3-implementation.md`](./adaptive-market-regime-m4-step3-implementation.md)、[`docs/adaptive-market-regime-m4-step4-implementation.md`](./adaptive-market-regime-m4-step4-implementation.md)

---

## 1. 这份文档的定位

这份文档只服务 **M4 Step 4 第二批**：把 `layer_ratios` / plan shape 从“已经规划好”继续拆成 **可以直接开工** 的最小实施任务。

一句话定义：

> **第二批不是把 adaptive layering 全量放开，而是在 Step 3 第一批 guardrails live 已经站稳的前提下，评估让 `layer_ratios` / live plan shape 在严格 guarded 条件下小范围进入真实执行。**

这里一定要继续分清三层：

1. **hints**：只记录，不进 live plan
2. **guardrails live**：只收紧总量 / 节奏 / 次数 / 条件，不改 per-layer shape
3. **plan shape live**：才允许 `layer_ratios` 真正改变 live layer plan

第二批只新增第 3 层的受控落地能力；**不重做 Step 2/3，也不扩张 M4 边界。**

---

## 2. 第二批最小目标

第二批最小目标只收敛为 5 件事：

1. **补齐 plan shape 审计单一事实来源**
   - baseline / effective / live `layer_ratios`
   - baseline / effective / live `layer_count`（derived only）
   - `plan_shape_really_enforced`
   - `plan_shape_enforced_fields / ignored_fields`
   - `live_layer_shape_source`

2. **在严格 guarded 条件下，允许 `layer_ratios` 小范围进入 live plan**
   - 必须独立开关
   - 必须独立 rollout
   - 必须 conservative-only
   - 必须保留 Step 3 guardrails live

3. **明确 `layer_count` 只是派生结果，不是独立 override 入口**
   - `layer_count = len(layer_ratios)`
   - 只允许因保守截断而减少有效层数
   - 不允许通过 `layer_count` 单独扩层或重写 layer 序列

4. **保持执行主骨架完全不变**
   - 不改 partial TP / trailing
   - 不改 reconcile / self-heal
   - 不改 intent lifecycle
   - 不改 direction lock 语义
   - 不改 layer reset / skip-layer 主语义

5. **让回滚粒度停留在 plan-shape 层，不拖累 Step 3**
   - Step 4 第二批异常时，优先只关 `layering_plan_shape_enforcement_enabled`
   - 不应要求同时回滚 Step 3 guardrails live

---

## 3. 字段边界：哪些可进入第二批 live，哪些仍不应碰

## 3.1 第二批允许进入 live 的字段

### A. `layer_ratios`

这是第二批唯一新增进入 **plan shape live** 的核心字段。

允许进入 live，但必须同时满足：

- `layering_profile_enforcement_enabled=true`
- `layering_plan_shape_enforcement_enabled=true`
- `mode in {guarded_execute, full}`
- symbol 命中 plan-shape rollout
- Step 3 guardrails live 已开启并生效
- conservative-only 校验通过

### B. `layer_max_total_ratio`

继续保持 **guardrails live**。

它不是第二批新开放字段，但它必须继续作为 `layer_ratios` live 的上层约束：

- live `layer_ratios` 总和不得大于 live `layer_max_total_ratio`
- 若 `layer_ratios` 收紧但 total cap 没同步可解释，仍视为实现不完整

### C. `max_layers_per_signal`

继续保持 **guardrails live**。

它不是 plan shape 主字段，但决定 live plan 最多允许消费多少层。第二批里它必须继续先于 `layer_ratios` 站稳。

### D. `min_add_interval_seconds`

继续保持 **guardrails live**。

它负责“节奏更慢”，不是“shape 更改”，所以仍维持 Step 3 语义，不在第二批重写。

### E. `profit_only_add`

继续保持 **guardrails live**。

它负责“追加条件更严格”，继续作为 plan shape live 的前置安全带。

### F. `layer_count`

允许进入第二批，但只能作为 **derived audit field**：

- `baseline_layer_count = len(baseline.layer_ratios)`
- `effective_layer_count = len(effective.layer_ratios)`
- `live_layer_count = len(live.layer_ratios)`

它**不是独立 live 字段**，也不应暴露为 `execution_overrides.layer_count` 的自由入口。

## 3.2 第二批仍然不应碰的字段 / 主题

- partial TP 全部语义
- trailing 全部语义
- reconcile / stale close / self-heal
- intent lifecycle / pending intent 状态机
- direction lock scope / acquire / release 语义
- layer reset 主逻辑
- skip-layer / disallow-skip 语义改写
- 任何会让 baseline execution 更激进的 override

---

## 4. 重点关系：6 个字段怎样一起约束

这一段是第二批最重要的边界，避免以后写着写着又混淆“shape”与“guardrail”。

## 4.1 `layer_ratios`

含义：**每一层的 live 配比 shape**。

它直接决定：

- 首层大小
- 后续层级权重
- 有效层数（派生）
- live layer plan 的形状

因此它必须最后开、单独开、单独回滚。

## 4.2 `layer_count`

含义：**`layer_ratios` 长度的派生事实**，不是策略独立入口。

规则：

- 不允许单独 override 扩层
- 只允许跟随 `layer_ratios` 保守截断后减少
- 不允许“先改 `layer_count`，再凑 `layer_ratios`”

## 4.3 `layer_max_total_ratio`

含义：**总分配天花板**。

关系：

- live `sum(layer_ratios)` 必须 `<= live layer_max_total_ratio`
- `layer_ratios` 即使 conservative，也不能绕过 total cap 审计
- 若 total cap 更紧，shape 也必须可解释地落在 cap 内

## 4.4 `max_layers_per_signal`

含义：**同一信号允许消费的最大层数上限**。

关系：

- `live_layer_count <= baseline_layer_count`
- `live_layer_count` 还必须与 `max_layers_per_signal` 一致可解释
- 若 live `layer_ratios` 仍是 3 层，但 `max_layers_per_signal=1`，那也要明确：shape 可见 ≠ 一次信号可全部消费

## 4.5 `min_add_interval_seconds`

含义：**相邻 add 的节奏下限**。

关系：

- 它不改 shape
- 它只决定同一 shape 在时间轴上能否更慢地被消费
- 所以第二批不能把它和 `layer_ratios` 混成一个开关

## 4.6 `profit_only_add`

含义：**追加条件 guardrail**。

关系：

- 它不决定每层比例
- 它只决定 live shape 里后续层有没有机会被触发
- 所以 `profit_only_add` 是 Step 3 guardrail，唔系 Step 4 shape 本体

## 4.7 一句话总规则

> **`layer_ratios` 决定 shape，`layer_count` 只是 shape 的派生事实，`layer_max_total_ratio / max_layers_per_signal / min_add_interval_seconds / profit_only_add` 决定的是 live shape 能否、能多大、能多快、能在什么条件下被消费。**

---

## 5. 第二批字段草案 / 配置开关草案

```yaml
adaptive_regime:
  mode: guarded_execute
  execution:
    layering_profile_hints_enabled: true
    layering_profile_enforcement_enabled: true
    layering_plan_shape_enforcement_enabled: false
    layering_plan_shape_rollout_symbols: []
    layering_plan_shape_rollout_fraction: 0.0
    layering_plan_shape_shadow_compare_enabled: true
    layering_plan_shape_require_guardrails_live: true
    layering_plan_shape_require_step3_stable: true
    layering_plan_shape_fail_closed: true
    layering_plan_shape_force_baseline_on_invalid: true
```

第二批字段建议：

- 保留：`layering_profile_hints_enabled`
- 保留：`layering_profile_enforcement_enabled`
- 保留：`layering_plan_shape_enforcement_enabled`
- 保留：`layering_plan_shape_rollout_symbols`
- 保留：`layering_plan_shape_rollout_fraction`
- 保留：`layering_plan_shape_shadow_compare_enabled`
- 建议新增：`layering_plan_shape_fail_closed`
- 建议新增：`layering_plan_shape_force_baseline_on_invalid`

明确不要新增：

- `execution_overrides.layer_count`
- 任何独立“扩层”开关
- partial TP / trailing 的第二批配置

---

## 6. 直接可开工的实施任务拆分

## Task 1：先补 plan-shape snapshot / validation helper（不改 live 行为）

### 目标

先把第二批最关键的事实来源补齐：`layer_ratios` / `layer_count` / `shape_guardrail_decisions` / `plan_shape_really_enforced`。

### 建议第一批先改文件

1. `core/regime_policy.py`
2. `tests/test_regime.py`

### 要做的事

1. 增加 / 整理 helper：
   - `merge_layer_ratios_conservatively(...)`
   - `derive_layer_count_from_ratios(...)`
   - `build_layering_plan_shape_snapshot(...)`
2. 在 snapshot 中明确输出：
   - `baseline/effective/live.layer_ratios`
   - `baseline/effective/live.layer_count`
   - `plan_shape_really_enforced`
   - `plan_shape_enforced_fields`
   - `plan_shape_ignored_fields`
   - `live_layer_shape_source`
   - `shape_guardrail_decisions`
3. 严格 fail-closed：
   - rollout miss -> baseline live
   - invalid ratios -> baseline live
   - shape gating miss -> baseline live
4. 把 `layer_count` 明确限制为 derived-only

### 先写哪些测试

1. `test_layering_shape_snapshot_keeps_layer_ratios_hints_only_when_shape_disabled`
2. `test_layering_shape_snapshot_derives_layer_count_from_ratios`
3. `test_layering_shape_snapshot_rejects_non_conservative_layer_ratios`
4. `test_layering_shape_snapshot_rejects_expanding_layer_count`
5. `test_layering_shape_snapshot_respects_live_total_ratio_cap`
6. `test_layering_shape_snapshot_keeps_baseline_live_on_guardrail_not_live`

## Task 2：在 executor 接 plan shape live 输入，但只消费，不改状态机语义

### 目标

让 executor 可以在 gated 条件下消费 `live.layer_ratios`，但它只是输入形状切换，不是执行语义重写。

### 建议第二批改文件

1. `trading/executor.py`
2. `tests/test_all.py`

### 要做的事

1. 在 layer plan 生成入口接 `adaptive_layering_snapshot.live.layer_ratios`
2. 明确 gating：
   - `layering_profile_enforcement_enabled=true`
   - `layering_plan_shape_enforcement_enabled=true`
   - rollout hit
   - Step 3 guardrails live 已开启
   - conservative-only pass
3. 任一条件失败时：
   - 回退 baseline `layer_ratios`
   - 继续保留 Step 3 guardrails live
4. executor 只消费 live input，明确不改：
   - direction lock
   - intent lifecycle
   - reconcile / self-heal
   - layer reset / skip-layer 主语义

### 先写哪些测试

1. `test_executor_step4_batch2_uses_live_layer_ratios_only_when_shape_enforcement_enabled`
2. `test_executor_step4_batch2_falls_back_to_baseline_shape_on_rollout_miss`
3. `test_executor_step4_batch2_preserves_guardrails_when_shape_is_live`
4. `test_executor_step4_batch2_does_not_mutate_lock_or_intent_semantics`

## Task 3：补 shadow compare / rollout / rollback 防线

### 目标

让第二批可以小范围灰度，而不是“一开就全市场 live”。

### 建议第三批改文件

1. `core/regime_policy.py`
2. `trading/executor.py`
3. `tests/test_regime.py`
4. `tests/test_all.py`
5. `config/config.yaml.example`
6. `config/config.local.yaml.example`

### 要做的事

1. 增加 plan-shape 专属 observability：
   - `shape_live_rollout_match`
   - `shadow_live_layer_ratios`
   - `shadow_live_layer_count`
   - `shadow_shape_diff_summary`
2. 明确 rollback 粒度：
   - 先关 `layering_plan_shape_enforcement_enabled`
   - 若仍异常，再关 `layering_profile_enforcement_enabled`
   - Step 2 execution guardrails 不因 Step 4 shape 异常被被动一起回滚
3. 明确失败闭合：
   - invalid effective shape -> baseline live
   - 监控信号异常 -> 仅关闭 plan-shape live

### 先写哪些测试

1. `test_shape_rollout_can_be_disabled_without_disabling_step3_guardrails`
2. `test_shape_shadow_compare_emits_baseline_vs_live_diff`
3. `test_shape_rollback_restores_baseline_live_shape_without_touching_guardrails`

---

## 7. 建议实施顺序

### 阶段 A：只补审计，不让 `layer_ratios` live

验收通过前，不动 executor live 输入。

完成标准：

- baseline / effective / live `layer_ratios` 可解释
- `layer_count` 只以 derived 字段出现
- 所有 fail-closed 分支都回 baseline

### 阶段 B：单 symbol / 小比例 rollout 放开 shape live

建议只在：

- `BTC-USDT-SWAP` 或当前最稳定 symbol
- 很小 rollout fraction
- Step 3 guardrails 已稳定样本通过

完成标准：

- `layer_ratios` 可进入 live
- Step 3 guardrails 仍独立可解释
- live / baseline 差异能在 observability 看清楚

### 阶段 C：扩大 rollout，但不扩 M4 语义

只扩大范围，不新增语义。

仍然不做：

- partial TP
- trailing
- reconcile / self-heal
- intent lifecycle
- direction lock

---

## 8. 验收标准

第二批完成后，至少要满足：

1. `layer_ratios` 未开 shape enforcement 时，继续 hints-only
2. 开 shape enforcement 但 rollout miss 时，继续 baseline live
3. 开 shape enforcement 且 rollout hit 时，live `layer_ratios` 仍只允许 conservative-only
4. `layer_count` 永远只由 `layer_ratios` 派生，不存在独立扩层入口
5. `layer_ratios` live 后，仍受 `layer_max_total_ratio` / `max_layers_per_signal` / `min_add_interval_seconds` / `profit_only_add` 共同约束
6. Step 4 第二批不改变 partial TP / trailing / reconcile / self-heal / intent lifecycle / direction lock 语义
7. 出现异常时，可只关闭 `layering_plan_shape_enforcement_enabled` 完成回滚

---

## 9. 失败信号

以下任一出现，都应视为第二批 rollout 失败信号：

1. `layer_ratios` 未开启 shape live，却已改变真实 layer plan
2. `layer_count` 可被单独 override 或出现扩层
3. live `layer_ratios` 总和超过 live `layer_max_total_ratio`
4. `max_layers_per_signal` / `profit_only_add` / `min_add_interval_seconds` 的 guardrail 解释被 shape live 覆盖或混淆
5. rollout miss / invalid shape 时没有 fail-closed 到 baseline
6. 出现与 direction lock / open intents / reconcile / self-heal 相关的回归
7. trade / intent / plan observability 无法回答“这笔单按 baseline 还是 adaptive shape 执行”

---

## 10. 回滚方式

### 一级回滚（首选）

关闭：

```yaml
adaptive_regime:
  execution:
    layering_plan_shape_enforcement_enabled: false
```

效果：

- `layer_ratios` 立即回到 hints-only
- Step 3 guardrails live 保持不动
- 第二批影响被单独切断

### 二级回滚

若问题不止 shape 本体，再关闭：

```yaml
adaptive_regime:
  execution:
    layering_profile_enforcement_enabled: false
```

效果：

- Step 3 guardrails live 也一起关闭
- 回退到 Step 2 execution guardrails / hints-only 组合

### 三级回滚

如怀疑 M4 整体解释链异常，再回：

- `execution_profile_enforcement_enabled=false`
- 必要时 `mode=decision_only` 或 `observe_only`

---

## 11. 建议第一批文件（可直接开工）

最推荐的第一批文件顺序：

1. `core/regime_policy.py`
2. `tests/test_regime.py`
3. `trading/executor.py`
4. `tests/test_all.py`
5. `config/config.yaml.example`
6. `config/config.local.yaml.example`

原因：

- 先把事实来源和保守 merge 规则钉死
- 再把 executor 接线做成纯输入切换
- 最后再补 rollout / rollback / 配置说明

---

## 12. 一句话收尾

> **M4 Step 4 第二批的正确打法，不是急住放开 adaptive layering，而是先把 `layer_ratios` 从“可见的 hints”稳稳推进到“可回滚、可解释、只更保守的 plan shape live”；其余 guardrails 继续做 guardrails，唔好混线。**
