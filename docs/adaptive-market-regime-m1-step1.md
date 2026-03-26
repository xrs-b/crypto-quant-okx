# Adaptive Market Regime M1 Step 1

本阶段继续保持 **observe-only**，重点不是改变交易判定，而是让同一份 `regime_snapshot` / `adaptive_policy_snapshot` 在各层输出更可读、更容易做后续分桶分析。

## 本次增强

- `core/regime_policy.py`
  - 升级 policy version 到 `adaptive_policy_v1_m1`
  - 新增统一 observe-only helper：
    - `build_regime_observe_only_view()`
    - `build_policy_observe_only_view()`
    - `build_observe_only_bundle()`
  - 统一生成：
    - `summary`
    - `phase`
    - `state`
    - `tags`
    - `notes`
- `signals/entry_decider.py`
  - 在 `breakdown` 暴露 observe-only phase/state/summary/tags
  - summary 文案带出 observe-only 标签态
- `signals/validator.py`
  - `details` 增加 `adaptive_regime_observe_only` 聚合块
- `trading/executor.py` / `RiskManager`
  - 继续复用同一份 snapshot payload，日志与 details 可直接带出 summary/tags
- `analytics/backtest.py`
  - trade 输出增加 `observe_only_summary / phase / state`
  - 保留原 `regime_tags / policy_tags`，并补 `observe_only_tags`

## 保持不变

- 不修改 allow / watch / block 判定逻辑
- 不修改 validator 真实过滤逻辑
- 不修改 executor 真正下单逻辑
- 仍然只是 richer observability
