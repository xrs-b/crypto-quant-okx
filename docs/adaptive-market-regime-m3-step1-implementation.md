# Adaptive Market Regime M3 Step 1 实施稿（Validator Effective Snapshot）

> 配套主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
>
> 配套 backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)
>
> 配套 M3 边界方案：[`docs/adaptive-market-regime-m3-boundary-plan.md`](./adaptive-market-regime-m3-boundary-plan.md)

---

## 1. 这份文档要解决什么

这份文档只处理 **M3 Step 1**：

> **先把 validator 的 effective snapshot / hints / observability 做完整，先让系统清楚记录“如果 M3 validator guardrails 生效，会怎样”，而不是立即大范围改变 validator 通过/拦截结果。**

它的作用是把 M3 Step 1 拆成一组 **可以直接开工、可以分批提交、可以灰度、可以快速回滚** 的实施任务，避免开发时把 Step 1 写着写着越界写成 Step 2。

---

## 2. Step 1 的边界判词

## 2.1 最小目标

M3 Step 1 的最小目标非常明确：

1. **validator 产出 effective validation snapshot**
2. **记录 baseline vs effective 的差异**
3. **输出 applied / ignored / hints / notes / reason code 草案**
4. **把这些字段写入 validation details / signal observability / 后续报表入口**
5. **默认不直接改变 validator pass / block 结果**

换句话说：

> Step 1 先做“看得见、讲得清、能复盘”的 validator guardrail 阶段，不抢先做真正 conservative enforcement。

## 2.2 允许的最小生效边界

如果开发过程中认定某个点需要少量真生效，**只允许在非常小的边界内发生**，并且必须同时满足：

- 有明确配置开关；
- 默认关闭；
- 只对白名单 symbol 生效；
- 只允许“更保守”，不允许放宽；
- 文档和代码都要明确写成 Step 1.5 / guarded pilot，而不是伪装成 Step 1 默认行为。

当前建议：

- **Step 1 默认仍应是 snapshot + hints-only**；
- `risk_anomaly` 统一 reason code 可以先做，但默认仍建议先走 observability，不直接一刀切改变 validator 结果；
- 真正的 validator conservative enforcement 应归入 **M3 Step 2**。

---

## 3. 和 M3 Step 2 的边界

## 3.1 Step 1 做什么

Step 1 只做以下内容：

- 生成 `effective_validation_snapshot`
- 生成 `validation_hints`
- 生成 `adaptive_validation_observability`
- 做 conservative merge / ignore 逻辑
- 统一 reason code / hint code / ignored reason 的字段结构
- 在 `observe_only / decision_only / guarded_execute` 下都能稳定输出同一套观测结构
- 确保当前 validator 真正使用的阈值和结果仍以 baseline 为主

## 3.2 Step 2 才做什么

下面这些都属于 **M3 Step 2 validator conservative enforcement**，Step 1 不越界：

- 真正用 `effective min_strength` 取代 baseline `min_strength`
- 真正用 `effective min_strategy_count` 取代 baseline `min_strategy_count`
- 真正把 `block_counter_trend / block_high_vol / block_low_vol` 的 regime override 纳入通过/拦截判定
- 真正把 `transition_risk / stability_score / risk_anomaly` 变成 validator hard block / downgrade gate
- 真正对白名单 symbol 开启 validator conservative enforcement

## 3.3 明确禁区

Step 1 明确不碰：

- `core/risk_budget.py` 的实际生效预算计算结果
- `trading/executor.py` 的 execution profile / layer ratios
- direction lock / intents / reconcile / self-heal 语义
- partial TP / trailing / exit profile
- 把 Step 1 做成“大幅减少交易数”的隐性 validator 改造

---

## 4. Step 1 交付定义

完成 Step 1 后，系统至少应能做到：

1. 对每次 validator 调用，输出一份结构化 `base_validation_snapshot`
2. 对同一次 validator 调用，输出一份结构化 `effective_validation_snapshot`
3. 清楚标记这次 effective snapshot 是：
   - `mode=observe_only` 的 display-only hint
   - 还是 `mode=guarded_execute` 下的 candidate effective snapshot
4. 记录：
   - `applied_overrides`
   - `ignored_overrides`
   - `validation_hints`
   - `would_block_reasons`
   - `would_tighten_fields`
5. 让主链路可观察到：
   - baseline validator 结果
   - 若按 effective snapshot 执行，会不会变成 block
   - 为什么会变
6. 默认行为下，validator 实际 pass / block 结果不变

---

## 5. 建议实施顺序（直接可开工）

## Task 1：补 validator snapshot resolver 骨架

### 目标
在 validator 进入真实校验前，先把 **baseline config → conservative merge → effective snapshot** 这条链路收口。

### 建议新增/调整文件
- `core/regime_policy.py`
- `signals/validator.py`
- `tests/test_regime.py`

### 要做的事
1. 在 `core/regime_policy.py` 增加 validator 专用 resolver / helper：
   - 建议函数名方向：
     - `build_validation_baseline_snapshot(...)`
     - `build_validation_effective_snapshot(...)`
     - `merge_validation_overrides_conservatively(...)`
2. baseline snapshot 先只覆盖当前 validator 已有真实消费字段：
   - `min_strength`
   - `min_strategy_count`
   - `block_counter_trend`
   - `block_high_volatility`
   - `block_low_volatility`
   - `regime_filter_enabled`
3. effective snapshot 只允许保守收紧：
   - 数值门槛只允许变严格
   - 布尔 block 只允许 `False -> True`
   - 任何放宽输入都写进 `ignored_overrides`
4. snapshot 需要带上来源信息：
   - `policy_version`
   - `policy_mode`
   - `regime_name`
   - `regime_confidence`
   - `stability_score`
   - `transition_risk`
   - `policy_source`

### 输出定义
产出 validator 级结构：

```yaml
adaptive_validation_snapshot:
  baseline:
    min_strength: 20
    min_strategy_count: 1
    block_counter_trend: true
    block_high_volatility: true
    block_low_volatility: true
    regime_filter_enabled: true
  effective:
    min_strength: 24
    min_strategy_count: 2
    block_counter_trend: true
    block_high_volatility: true
    block_low_volatility: true
    regime_filter_enabled: true
  effective_state: hints_only
  policy_mode: guarded_execute
  policy_version: adaptive_policy_v1_m3_step1
  regime_name: high_vol
  regime_confidence: 0.74
  stability_score: 0.58
  transition_risk: 0.41
  applied_overrides:
    min_strength:
      baseline: 20
      effective: 24
      source: validation_overrides.min_strength
  ignored_overrides:
    block_low_volatility:
      requested: false
      ignored_reason: non_conservative_override
```

---

## Task 2：在 validator 内先接 snapshot 与 hints，不改最终放行结果

### 目标
让 `signals/validator.py` 能输出 Step 1 所需的 observability，但默认不改变 pass / block。

### 建议新增/调整文件
- `signals/validator.py`
- `tests/test_all.py`
- `tests/test_regime.py`

### 要做的事
1. 在 `validate()` 开头、真正检查前，生成：
   - `adaptive_validation_snapshot`
   - `adaptive_validation_hints`
   - `adaptive_validation_observability`
2. 当前真实校验仍使用 baseline：
   - baseline `market_filters`
   - baseline `strategies.composite.min_strategy_count`
   - baseline `strategies.composite.min_strength`
   - baseline `regime_filters`
3. 新增 “would-have” 观测字段，但不接管结果：
   - `would_fail_min_strength`
   - `would_fail_min_strategy_count`
   - `would_block_counter_trend`
   - `would_block_high_volatility`
   - `would_block_low_volatility`
   - `would_block_risk_anomaly`
   - `would_block_transition_risk`
4. 如果 baseline 已经 block，也照样输出 effective hints，保证复盘口径一致。
5. 统一提示字段，让后续 dashboard / analytics 可以直接消费。

### 建议字段草案

```yaml
adaptive_validation_hints:
  enabled: true
  effective_state: hints_only
  would_change_result: true
  baseline_result: pass
  hinted_result: block
  hint_codes:
    - WOULD_RAISE_MIN_STRENGTH
    - WOULD_BLOCK_HIGH_VOL
  would_block_reasons:
    - code: REGIME_HIGH_VOL_HINT
      message: adaptive effective snapshot would block high-vol entry
  notes:
    - validator still uses baseline thresholds in step1
```

---

## Task 3：统一 conservative-only 防呆与配置开关

### 目标
先把 Step 1 所需的配置边界卡死，避免后续误把 Step 2 偷偷做进去。

### 建议新增/调整文件
- `core/config.py`
- `core/regime_policy.py`
- `config/config.yaml.example`
- `docs/adaptive-market-regime-m3-boundary-plan.md`

### 配置草案

```yaml
adaptive_regime:
  enabled: true
  mode: observe_only
  guarded_execute:
    validator_snapshot_enabled: true
    validator_hints_enabled: true
    validator_enforcement_enabled: false
    risk_hints_enabled: false
    enforce_conservative_only: true
    rollout_symbols: []
  defaults:
    policy_version: adaptive_policy_v1_m3_step1
  regimes:
    high_vol:
      validation_overrides:
        min_strength: 24
        min_strategy_count: 2
        block_counter_trend: true
    risk_anomaly:
      validation_overrides:
        force_reason_code: REGIME_RISK_ANOMALY
```

### 规则要求
- `validator_snapshot_enabled=true`：允许输出 snapshot
- `validator_hints_enabled=true`：允许输出 hints
- `validator_enforcement_enabled=false`：Step 1 默认不真生效
- `enforce_conservative_only=true`：禁止任何放宽型 override
- `rollout_symbols`：先为 Step 2 预埋，但 Step 1 只记录，不作为真实拦截依据

---

## Task 4：补测试，先保边界再保功能

### 目标
先把 Step 1 的边界写成测试，防止后面开发滑向 Step 2。

### 建议第一批测试

#### A. `tests/test_regime.py`
1. `test_validation_snapshot_keeps_baseline_when_no_override`
2. `test_validation_snapshot_applies_only_conservative_numeric_tightening`
3. `test_validation_snapshot_rejects_non_conservative_boolean_relaxation`
4. `test_validation_snapshot_records_ignored_overrides_reason`
5. `test_validation_snapshot_includes_regime_and_policy_metadata`

#### B. `tests/test_all.py`
1. `test_validator_step1_emits_hints_without_changing_pass_result`
2. `test_validator_step1_emits_would_block_reason_for_high_vol`
3. `test_validator_step1_preserves_existing_block_reason_when_baseline_fails`
4. `test_validator_step1_observability_payload_is_json_serializable`

### 测试顺序建议
1. 先写 snapshot helper 单测
2. 再写 validator hints 行为测试
3. 最后补配置防呆 / conservative-only 测试

---

## Task 5：补 observability 入口与文档入口

### 目标
让 Step 1 不只是“代码里有字段”，而是项目文档与后续实施入口都找得到。

### 建议新增/调整文件
- `README.md`
- `docs/adaptive-market-regime-backlog.md`
- `docs/adaptive-market-regime-framework-plan.md`
- `docs/adaptive-market-regime-m3-boundary-plan.md`

### 要做的事
- 在 README 的 adaptive regime 文档入口加入 Step 1 实施稿
- 在 backlog 的 `AR-M3-01` 下补一段 Step 1 执行说明 / status
- 在 framework plan 的 M3 段落补 “Step 1 / Step 2” 明确分层
- 在 boundary plan 的“相关文档”里补 Step 1 链接

---

## 6. 先改哪些文件（建议第一批）

按最稳妥顺序，建议第一批文件如下：

1. `core/regime_policy.py`
   - 先收口 validation snapshot / conservative merge helper
2. `tests/test_regime.py`
   - 先锁 Step 1 边界
3. `signals/validator.py`
   - 再接 snapshot / hints / observability
4. `tests/test_all.py`
   - 补 validator 行为级测试
5. `docs/adaptive-market-regime-m3-step1-implementation.md`
   - 保持文档与实现同步
6. `docs/adaptive-market-regime-backlog.md`
7. `docs/adaptive-market-regime-framework-plan.md`
8. `docs/adaptive-market-regime-m3-boundary-plan.md`
9. `README.md`

如果只做最小第一刀，可以先做前 4 个文件，再补文档入口。

---

## 7. 字段草案（Step 1 版本）

## 7.1 validator details 内建议新增字段

```yaml
adaptive_validation_snapshot:
  baseline: {}
  effective: {}
  effective_state: hints_only
  policy_mode: observe_only
  policy_version: adaptive_policy_v1_m3_step1
  regime_name: high_vol
  regime_confidence: 0.74
  stability_score: 0.58
  transition_risk: 0.41
  applied_overrides: {}
  ignored_overrides: {}

adaptive_validation_hints:
  enabled: true
  baseline_result: pass
  hinted_result: block
  would_change_result: true
  hint_codes: []
  would_block_reasons: []
  would_tighten_fields: []
  notes: []

adaptive_validation_observability:
  phase: m3_step1
  state: hints_only
  rollout_match: false
  enforcement_enabled: false
  conservative_only: true
```

## 7.2 hint code 草案

建议先用稳定、可聚合的 code，而唔好一开始散落自由文本：

- `WOULD_RAISE_MIN_STRENGTH`
- `WOULD_RAISE_MIN_STRATEGY_COUNT`
- `WOULD_BLOCK_COUNTER_TREND`
- `WOULD_BLOCK_HIGH_VOL`
- `WOULD_BLOCK_LOW_VOL`
- `WOULD_BLOCK_RISK_ANOMALY`
- `WOULD_BLOCK_TRANSITION_RISK`
- `IGNORED_NON_CONSERVATIVE_OVERRIDE`
- `ROLL_OUT_SYMBOL_NOT_MATCHED`

## 7.3 ignored reason 草案

- `non_conservative_override`
- `unsupported_validation_field`
- `mode_not_effective`
- `validator_enforcement_disabled`
- `rollout_symbol_not_matched`
- `low_regime_confidence`

---

## 8. 灰度方式（Step 1）

Step 1 的灰度重点不是“真拦截几多单”，而是：

> **先验证 observability 口径够不够清楚，hint 是否稳定，would-change 结果是否可信。**

### 推荐灰度顺序

### 阶段 A：全量 hints-only
- `mode=observe_only` 或 `decision_only`
- `validator_snapshot_enabled=true`
- `validator_hints_enabled=true`
- `validator_enforcement_enabled=false`

目标：先看字段稳定性、日志可读性、reason code 聚合度。

### 阶段 B：guarded_execute 但仍 hints-only
- `mode=guarded_execute`
- `validator_enforcement_enabled=false`
- `rollout_symbols=["BTC/USDT"]`

目标：验证 Step 2 将来会生效在哪些 symbol、命中率如何，但仍不改变 validator 结果。

### 阶段 C：移交 Step 2
- 只有在 Step 1 的 hints 数据足够清楚后，才进入 validator conservative enforcement。

---

## 9. 验收标准

Step 1 验收不看“拦了几多单”，而看是否把边界和可解释性立住。

### 9.1 功能验收
- validator 每次执行都能稳定输出 `adaptive_validation_snapshot`
- 当存在 validation override 时，能看到 `baseline vs effective`
- 当 override 非保守时，会进入 `ignored_overrides`
- 默认配置下，不改变当前 validator 的 pass / block 结果

### 9.2 可解释性验收
- 能明确看见 `regime_name / confidence / stability_score / transition_risk`
- 能明确看见 `policy_mode / policy_version / policy_source`
- 能明确看见 `would_change_result` 与 `would_block_reasons`
- hint code 可聚合，不依赖自由文本复盘

### 9.3 边界验收
- Step 1 不直接改 `compute_entry_plan()` 输入
- Step 1 不改 execution profile
- Step 1 不碰 direction lock / intents / reconcile 语义
- Step 1 不把 validator enforcement 默认打开

### 9.4 工程验收
- 新字段可 JSON 序列化
- `observe_only` 下主链路兼容
- 旧 signal / log / dashboard 缺字段时不会报错

---

## 10. 失败信号

一旦出现以下信号，应视为 Step 1 已经越界或实现不合格：

1. **交易通过率发生明显变化**，但配置上并未打开 `validator_enforcement_enabled`
2. `adaptive_validation_snapshot` 存在，但 baseline/effective 看不出差异来源
3. `ignored_overrides` 大量出现，但没有稳定 reason code
4. hints 字段输出不稳定，同一种情况每次写法都不同
5. 线上无法回答“这单是 baseline block，还是 effective snapshot would block”
6. Step 1 改动波及 `risk_budget / executor / layer plan`

---

## 11. 回滚方式

Step 1 必须做到 **配置可回退、代码无需热修才可止血**。

### 配置回滚顺序
1. `validator_hints_enabled=true -> false`
2. `validator_snapshot_enabled=true -> false`
3. `mode=guarded_execute -> decision_only`
4. `mode=decision_only -> observe_only`
5. `adaptive_regime.enabled=false`

### 回滚原则
- 回滚后，validator 主逻辑应立即退回旧路径
- 若只关 hints，baseline validator 结果不受影响
- 即使回滚，也最好保留最小的 observe-only snapshot 入口，方便排查

---

## 12. 建议拆分成的提交批次

### Commit A（最小可审阅）
- 文档：本实施稿 + 入口互链

### Commit B（骨架）
- `core/regime_policy.py` validator snapshot helper
- `tests/test_regime.py` 边界测试

### Commit C（接入）
- `signals/validator.py` 接 snapshot + hints + observability
- `tests/test_all.py` 行为测试

如果希望更稳，也可以先只完成 Commit A，这样主开发者能按文档直接分工开做。

---

## 13. 当前建议结论

对当前项目，M3 Step 1 的最稳起手式是：

> **先做 validator effective snapshot / hints / observability，并明确保持 hints-only；把 conservative enforcement 严格留到 M3 Step 2。**

这样做的好处是：

- 不打扰当前 layering 实盘验收
- 不让 M3 一上来变成“多叠一层真过滤”
- 先把 baseline/effective 差异记录清楚
- 为 Step 2 提供真实样本，而不是靠拍脑袋调阈值

---

## 13.1 当前实现进度（2026-03-26）

本轮已完成 Step 1 的最小可交付实现：

- `core/regime_policy.py`
  - 新增 validator baseline/effective snapshot helper
  - 新增 conservative-only validation override merge
  - 接入 `validation_overrides` 解析，仍与 decision/risk/execution 生效边界分离
- `signals/validator.py`
  - 新增 `adaptive_validation_snapshot`
  - 新增 `adaptive_validation_hints`
  - 新增 `adaptive_validation_observability`
  - 默认保持 hints-only / observe-only，不改变 validator baseline pass/block 结果
- 测试
  - 覆盖 baseline vs effective、applied/ignored、observe-only 不改结果、rollout mismatch / non-conservative ignored reasons、JSON serializable

仍明确 **未进入 Step 2**：

- 未启用 validator enforcement
- 未改变 risk budget / execution 生效逻辑
- 未把 effective validator 阈值接管真实 pass/block 结果

## 14. 相关文档

- 主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
- Backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)
- M3 边界方案：[`docs/adaptive-market-regime-m3-boundary-plan.md`](./adaptive-market-regime-m3-boundary-plan.md)
- M0 实施稿：[`docs/adaptive-market-regime-m0-implementation.md`](./adaptive-market-regime-m0-implementation.md)

> 一句话收尾：**Step 1 先把 validator 的“有效快照”讲清楚，Step 2 才去决定要不要真拦截。**