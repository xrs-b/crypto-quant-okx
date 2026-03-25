# 分仓策略验收清单

> 目的：真实开仓后，按项核对分仓逻辑是否符合预期；尽量先看 dashboard 总览里的 execution watch / exposure 摘要，再配合 signals、trading.log、DB 数据复盘。

## 0. 验收前准备

- 确认 `trading.layering.*` 已启用，并沿用当前项目默认分仓结构（首仓 6%、第二层 6%、第三层 4%）。
- Dashboard 可访问：重点查看
  - `/api/system/execution-state`
  - 总览页 `overviewExecutionSummary / overviewExecutionExposure / overviewExecutionWatch`
  - 信号页最近 50 条信号的 observability 字段
- 运行日志可查看：`logs/trading.log`
- DB 可查看：`signals.filter_details`、`open_intents`、`direction_locks`、`layer_plan_states`

---

## 1. 首仓只开 6% 的验证点

### 预期
- 首次开仓走 `layer_no=1`
- `layer_ratio=0.06`
- 风控计算中的 `projected_total_exposure` / `projected_symbol_exposure` 与首仓 6% 规划一致

### 观察点
- Dashboard execution watch 中：对应 symbol 的 `layer plan` 应显示 `current=0 -> pending/fill layer 1`
- `signals.filter_details.observability.layer_no = 1`
- `signals.filter_details.observability.projected_*_exposure` 有值
- `open_intents.layer_no = 1`
- 成交后 `trades.layer_no = 1`

### 通过标准
- 第一笔不是按 10%/8% 默认仓位直接开满，而是明确按 layer 1 的 6% 落地

---

## 2. 同一 signal 不重复开仓

### 预期
- 同一个 `signal_id` 重复进入执行链路时，被 idempotency 拦截

### 观察点
- trading.log / filter_details 出现：
  - `signal_id`
  - `root_signal_id`
  - `deny_reason=signal_idempotency`
- Dashboard execution watch 中最近决策卡片显示 `deny=signal_idempotency`
- 不应新增第二条同 signal 的 open intent / open trade

### 通过标准
- 同一 signal 只对应 0 或 1 次实际成交

---

## 3. 同一周期不连开多层

### 预期
- 若 `allow_same_bar_multiple_adds=false`，同一 bar / candle 内不允许连续开多层

### 观察点
- 第二次尝试被拦截时：
  - `deny_reason=allow_same_bar_multiple_adds`
  - `signal_bar_marker` 出现在 filter_details
- `layer_plan_states.plan_data.signal_bar_markers` 有记录
- 当前 layer plan 的 `filled_layers / pending_layers` 不应在同一周期内连续跳增

### 通过标准
- 同一 bar 最多只新增一层

---

## 4. 加仓最小间隔验证

### 预期
- 若配置了 `min_add_interval_seconds`，在时间窗口内禁止继续加仓

### 观察点
- 被拦截时：
  - `deny_reason=min_add_interval_seconds`
  - `remaining_seconds` 出现在 filter_details / 风控详情
- Dashboard execution watch 最近决策可见拦截原因

### 通过标准
- 未达到最小间隔前不会新增下一层；时间到后才允许进入下一层

---

## 5. 平仓后 layer state reset

### 预期
- 全部平仓后：
  - open intent 清空
  - direction lock 释放
  - layer plan 回到 `idle`
  - `root_signal_id` 清空
  - `filled_layers / pending_layers` reset

### 观察点
- `/api/system/execution-state` 中该 symbol 不再显示 active plan / lock / intent
- `layer_plan_states.status = idle`
- `layer_plan_states.current_layer = 0`
- `plan_data.filled_layers = []`
- `plan_data.pending_layers = []`

### 通过标准
- 平仓后下一次新 signal 从 layer 1 重新开始

---

## 6. intent / lock / layer_plan 状态观察点

### intent
- 开仓前创建 `open_intents`
- 提交后状态从 `pending -> submitted -> filled/failed`
- 失败时 intent 会回收，不应长期悬挂

### direction lock
- 开仓前获取锁
- 成功/失败收尾后释放锁
- 若出现孤儿锁，可通过 orphan cleanup 自动清理

### layer plan
- `pending_layers`：预留但未成交
- `filled_layers`：已成交层
- `current_layer`：当前已完成最大层
- `root_signal_id`：本轮分仓主链路根 signal

### 通过标准
- 三者状态转换前后一致，不出现“锁还在、intent 已没、plan 没同步”这种撕裂状态

---

## 7. 建议的实盘/仿真验收顺序

1. 空仓状态下触发首仓，确认只开 6%
2. 立即重复投喂同 signal，确认幂等拦截
3. 在同一 bar 内再次触发加仓，确认同周期拦截
4. 等待最小间隔后再次触发，确认允许下一层
5. 手动或规则平仓，确认 layer state / intent / lock 全部 reset

---

## 8. 本轮改造新增的关键观察字段

至少应在日志或 `filter_details.observability` 看到：

- `signal_id`
- `root_signal_id`
- `layer_no`
- `deny_reason`
- `current_symbol_exposure`
- `projected_symbol_exposure`
- `current_total_exposure`
- `projected_total_exposure`

如缺任一项，先补观测再做下一轮策略放量。