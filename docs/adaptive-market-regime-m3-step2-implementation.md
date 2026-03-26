# Adaptive Market Regime M3 Step 2 实施稿（Validator Conservative Enforcement）

> 延续 [`docs/adaptive-market-regime-m3-step1-implementation.md`](./adaptive-market-regime-m3-step1-implementation.md)

## 本步完成内容

M3 Step 2 让 validator 在 **guarded / rollout / conservative-only** 前提下，开始小范围真正使用 adaptive effective gate。

### 生效边界

- **只影响 validator**，不修改 risk / execution 生效逻辑
- **默认安全**：`validator_enforcement_enabled=false`
- **只允许更保守**：
  - 数值阈值只允许抬高（如 `min_strength` / `min_strategy_count`）
  - 布尔 block 只允许 `False -> True`
  - 任何放宽请求都进入 `ignored_overrides`
- **可灰度 / 可回滚**：
  - `rollout_symbols` 控制 symbol 白名单
  - `validator_enforcement_categories` 控制只开部分场景（`thresholds` / `market_guards` / `regime_guards`）

## 当前 enforcement 行为

当同时满足以下条件时，validator 会真正使用 adaptive effective gate：

1. `adaptive_regime.mode` 为 `guarded_execute` 或 `full`
2. `guarded_execute.validator_enforcement_enabled=true`
3. symbol 命中 `guarded_execute.rollout_symbols`（若配置了）

### 已真实接入的 conservative gates

- `thresholds`
  - `min_strength`
  - `min_strategy_count`
- `market_guards`
  - `block_counter_trend`
  - `block_high_volatility`
  - `block_low_volatility`
- `regime_guards`
  - `risk_anomaly` hard block
  - `transition_risk >= 0.8` hard block

## 可观测性

validator 输出现在会同时包含：

- `adaptive_validation_snapshot`
  - baseline
  - effective
  - applied_overrides
  - ignored_overrides
  - effective_state
  - enforcement_categories
- `adaptive_validation_hints`
  - would-block / would-tighten / hint codes
- `adaptive_validation_observability`
  - baseline_result
  - effective_result
  - enforced
  - block_reason
  - block_code
- `adaptive_validation_enforcement`
  - baseline / effective / applied / ignored / block_reasons / summary

## 回滚方式

任一配置即可立即回退：

- `validator_enforcement_enabled=false`
- `mode=observe_only` 或 `decision_only`
- 移除 rollout symbol
- 清空对应 `validator_enforcement_categories`

## 验收点

- 默认不开启时，仍为 hints-only
- 开启后仅对 rollout symbol 生效
- 只收紧，不放宽 baseline
- 输出可清楚解释：baseline / effective / applied / ignored / enforced / why block

## Status（2026-03-26 / M3 Step 3）: done

### 新增内容

- 在 **risk / budget / execution observability** 路径补上 `adaptive_risk_snapshot` 与 `adaptive_risk_hints`
- 当前阶段保持 **hints-only / observe-only**：
  - 不改 `compute_entry_plan()` 输入
  - 不改 execution 真正生效参数
  - 只输出 baseline vs effective 的保守风险视图
- 支持 conservative-only risk merge，当前已覆盖：
  - `total_margin_cap_ratio`
  - `total_margin_soft_cap_ratio`
  - `symbol_margin_cap_ratio`
  - `base_entry_margin_ratio`
  - `max_entry_margin_ratio`
  - `leverage_cap`
- 输出明确区分：
  - `baseline`
  - `effective`
  - `applied_overrides`
  - `ignored_overrides`
  - `would_tighten` / `would_tighten_fields`
  - `hint_codes`
  - `observe_only`
  - `effective_state=hints_only|disabled`

### 仍然刻意不做的事

- 不修改 execution 骨架
- 不真正把 adaptive risk view 写回 `compute_entry_plan()` / 下单输入
- 不提前进入 M4 execution adaptation
