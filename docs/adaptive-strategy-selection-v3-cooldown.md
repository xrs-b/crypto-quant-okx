# Adaptive Strategy Selection v3 - per-strategy cooldown / recovery window

## What changed

- `adaptive_strategy_selection_v2` 升级为 `adaptive_strategy_selection_v3`
- 在 regime budget / slots 之后，新增 **per-strategy cooldown / recovery window**
- 冷却依据直接来自最近 close outcome：
  - 最近负收益 / stop-loss 触发后，策略先进入 cooldown
  - cooldown 结束后，若恢复样本不足或恢复表现未达阈值，继续留在 recovery window
- 原则保持 **只收紧不放宽**：被 cooldown / recovery 命中的策略权重直接归零，不会反向放大
- 仍保持 **testnet-only**：这里只影响候选排序和执行前许可链路的收紧，不放宽任何实盘边界

## Mainline integration points

### `bot/run.py`

- `_strategy_selection_config()`
  - 新增 strategy cooldown / recovery 配置读取
- `_evaluate_strategy_cooldown()`
  - 对单策略按 `symbol_regime -> symbol -> regime -> global` 评估 cooldown scope
  - 输出结构化 cooldown contract
- `_build_strategy_selection_contract()`
  - 输出：
    - `strategy_cooldowns`
    - `cooldown_summary`
    - `selection_reason_codes`
  - cooldown / recovery 命中时，把该策略 weight 归零
- `_build_candidate_contract()`
  - 把 cooldown summary 带入 candidate ranking contract

### `signals/detector.py`

- `apply_strategy_selection()`
  - 把 strategy cooldown metadata 写回每个 reason
  - 同步 `strategy_cooldown_summary` 到 `signal.market_context`

### `core/reason_codes.py`

- 新增结构化 reason code：
  - `SKIP_STRATEGY_COOLDOWN_ACTIVE`
  - `SKIP_STRATEGY_RECOVERY_WINDOW_ACTIVE`

## Contract highlights

### strategy selection contract

- `schema_version: adaptive_strategy_selection_v3`
- `strategy_cooldowns.{strategy}`
  - `cooldown_active`
  - `recovery_window_active`
  - `reason_code`
  - `scope` / `scope_key`
  - `cooldown_until` / `remaining_minutes`
  - `recovery_trade_count` / `recovery_win_rate` / `recovery_avg_return_pct`
- `cooldown_summary`
  - `active_count`
  - `recovery_window_count`
  - `blocked_strategies`
  - `reason_code_counts`

## Default posture

默认保持保守：

- `strategy_cooldown_hours = 6`
- `strategy_recovery_window_trades = 2`
- `strategy_recovery_min_win_rate = 50%`
- `strategy_recovery_min_avg_return_pct = 0`

这些都只会让策略更难重新进入，不会放宽现有门槛。
