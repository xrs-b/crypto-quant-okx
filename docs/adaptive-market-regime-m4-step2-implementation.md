# Adaptive Market Regime · M4 Step 2 Implementation

## 目标

M4 Step 2 开始让 adaptive regime 在 **execution profile guardrails** 层小范围真正生效，但仍坚持：

- **默认安全关闭**
- **只允许更保守，不允许放宽 baseline**
- **只碰外围 guardrails，不改 partial TP / trailing / reconcile / intent / direction lock 语义**
- **`layer_ratios` 继续默认 hints-only，不提前做 deeper layering aggression 改写**

## 本步真实生效范围

仅在以下条件同时满足时，execution guardrails 才会真的进入 live execution/profile selection：

1. `adaptive_regime.mode in {guarded_execute, full}`
2. `adaptive_regime.guarded_execute.execution_profile_enforcement_enabled = true`
3. `rollout_symbols` 命中（空列表视为全量）

在上述前提下，当前只允许这些 guardrail 字段真正 enforced：

- `layer_max_total_ratio`
- `max_layers_per_signal`
- `min_add_interval_seconds`
- `profit_only_add`
- `allow_same_bar_multiple_adds`
- `leverage_cap`（主要透过 risk/executor 既有入口继续消费）

## 当前明确不做

- 不改 `partial TP / trailing / reconcile / intent / direction lock` 语义
- 不把 `layer_ratios` 默认写进 live layer plan
- 不做更激进的 layering aggressiveness 改写
- 不做 exit profile enforcement

## 执行口径

`core.regime_policy.build_execution_effective_snapshot()` 现在会同时输出：

- `baseline`
- `effective`（candidate）
- `enforced_profile`
- `enforced_fields`
- `execution_profile_really_enforced`
- `field_decisions[]`

每个 field decision 都会明确：

- baseline
- effective
- live
- applied / ignored / enforced
- decision
- reason

所以线上可以分清：

- 只是 candidate / hinted
- 真正 enforced
- 因 rollout / 开关 / non-conservative / layering disabled 被忽略

## Executor 接入方式

`trading/executor.py` 新增 live execution profile 收口：

- runtime layering guards 读取 `enforced_profile`
- layer plan 的 `max_total_ratio` 开始读取 `enforced_profile`
- `layer_ratios` 仍保持 baseline，除非未来显式开启 `layering_profile_enforcement_enabled`

因此本步开始，adaptive regime 会真正影响：

- 加仓间隔检查
- 单 signal 最大层数
- profit-only add
- same-bar multiple adds
- layer max total ratio 的 live profile

## 验收重点

新增测试覆盖：

1. 默认不生效
2. 开关开启后 guardrails 生效
3. 只收紧不放宽
4. rollout miss 不进入 live enforcement
5. 输出中可观测 baseline/effective/enforced/field decisions
6. live profile 确实使用 enforced guardrails，而 `layer_ratios` 继续保持 baseline

## 回滚

把 `execution_profile_enforcement_enabled` 关回 `false` 即可恢复 hints-only；如需更严格保守，仍可保留 hints 观测而不影响 live execution。
