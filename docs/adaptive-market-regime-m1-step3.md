# Adaptive Market Regime M1 Step 3

## 目标

在 **不改变真实交易行为** 的前提下，继续统一 adaptive regime observe-only 的展示口径，并补强 summary 视角，方便 dashboard / execution-state / backtest report 用同一套字段读数。

## 本次统一口径

### 统一 canonical 字段

新增统一对象：`observe_only`

```json
{
  "enabled": true,
  "phase": "observe_only",
  "state": "transition_risk+neutral",
  "summary": "...",
  "banner": "Adaptive regime / policy currently run in observe-only mode; outputs are display-only and do not alter execution logic.",
  "tags": ["observe_only", "adaptive_regime", "adaptive_policy"],
  "top_tags": ["observe_only", "adaptive_regime"],
  "tag_count": 3,
  "regime": {
    "name": "trend",
    "family": "trend",
    "direction": "up",
    "confidence": 0.81
  },
  "policy": {
    "mode": "observe_only",
    "version": "adaptive_policy_v1_m1",
    "source": "adaptive_regime.defaults",
    "state": "neutral"
  },
  "notes": [],
  "snapshots": {
    "regime_snapshot": {},
    "adaptive_policy_snapshot": {}
  }
}
```

### 兼容字段

为避免旧调用点即刻失效，以下字段仍保留为镜像：

- `observe_only_summary`
- `observe_only_phase`
- `observe_only_state`
- `observe_only_tags`

但后续新接入优先使用 `observe_only` 对象，不再继续扩散同义字段。

## 新增 summary 视角

### execution-state / dashboard API

新增：

- `observe_only_summary.banner`
- `observe_only_summary.top_tags`
- `observe_only_summary.top_regimes`
- `observe_only_summary.top_policies`
- `observe_only_summary.recent`
- `summary.recent_decisions`

### backtest summary

新增：

- `summary.observe_only_summary_view`
- symbol 级 `observe_only_summary_view`

用于快速看到最近 observe-only 标签、主 regime / policy 分布，而不是只看长字段列表。

## 保证

- 真实下单、风控、执行链行为不变
- 仅补强可观察性与展示层后端输出
- 旧字段仍保留，方便平滑迁移
