# Layering 配置整理笔记

> 目标：帮后续手调参数时，先知道每个开关会影响边界，而唔系只睇字面。

## 当前默认分层

- `layer_ratios: [0.06, 0.06, 0.04]`
- `layer_max_total_ratio: 0.16`
- 含义：第一层 6%，第二层 6%，第三层 4%，累计最多 16%

这组默认值与当前验收文档一致，适合先观察实盘行为，不建议在未完成验收前频繁改动。

---

## 关键参数说明

### 1) `layer_ratios`
每层目标保证金占比。

示例：

```yaml
trading:
  layering:
    layer_ratios: [0.06, 0.06, 0.04]
```

解释：
- layer 1 → 6%
- layer 2 → 6%
- layer 3 → 4%

如果你想更保守，可以改成：

```yaml
layer_ratios: [0.05, 0.04, 0.03]
layer_max_total_ratio: 0.12
```

---

### 2) `layer_max_total_ratio`
单一 symbol / side 在本轮 layering 中允许的累计目标上限。

经验：
- 应该 **大于等于** `layer_ratios` 总和
- 如果你故意设得更大，代表给风险预算二次裁剪留空间
- 如果设得过小，会令后续层明明配置存在，但实际上永远开不出

---

### 3) `min_add_interval_seconds`
两层之间的最小等待时间。

- `300` = 5 分钟
- 适合避免同一波信号抖动时太急补仓
- 若你做短周期仿真，可以临时降到 `60` 方便观察，但实盘不建议太低

---

### 4) `max_layers_per_signal`
单个 `root_signal_id` 最多允许成交几层。

- `1`：同一 signal 只允许打一层，剩余层要等后续新 signal / 新条件
- `3`：理论上同一 signal 链路可逐步走完整个 3 层

当前默认设为 `1`，偏保守，适合先控并发与幂等问题。

---

### 5) `direction_lock_scope`
方向锁范围。

可选：
- `symbol`：同一币种共用锁，更保守
- `symbol_side`：同币多/空分开，更宽松

当前项目默认偏向：

```yaml
direction_lock_scope: symbol
```

原因：
- 当前重点是避免并发开仓 / 对账恢复时状态撕裂
- `symbol` 粒度更容易降低重复 intent 与交叉恢复复杂度

---

### 6) `allow_same_bar_multiple_adds`
是否允许同一 bar 内连开多层。

建议：
- `false`：默认更稳，便于验收
- `true`：只适合你已确认信号链路、幂等、风控观测都稳定之后再放开

---

## 建议调参顺序

优先调：
1. `min_add_interval_seconds`
2. `max_layers_per_signal`
3. `layer_ratios`
4. `layer_max_total_ratio`

尽量不要一轮同时改太多项，否则之后很难判断究竟是哪一个开关导致行为变化。

---

## 最小验收建议

每次改 layering 后，至少确认：

1. 首仓是否按 layer 1 占比执行
2. 同一 signal 是否被幂等拦截
3. stale intent / stale lock 是否会在后续对账周期自动收口
4. 平仓后 layer plan 是否 reset 回 `idle`

配合文档：
- `docs/layering-acceptance-checklist.md`

---

## 这轮新增的自愈关注点

现在对账 / orphan cleanup 会额外关注：

- stale intent 但实际已有持仓 / open trade → 自动回收 intent
- stale lock 且已无 active intent → 自动释放方向锁
- layer plan 仍显示 active/pending，但真实上已经无仓无单 → 自动 reset 为 `idle`

所以后续观察日志 / API 时，除了看 `removed_*`，也要留意：

- `healed_intents`
- `healed_locks`
- `plan_resets`

这样可以分清：
- 是“单纯清掉垃圾状态”
- 还是“真实执行后遗留，需要自愈收口”
