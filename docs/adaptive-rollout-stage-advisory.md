# Adaptive rollout stage advisory

## Why

Validation freshness gate 已经能阻止 stale / regressed validation 继续 auto-advance，但系统之前对“下一步应推进到哪个 rollout stage”仍偏静态：
- 有 gate，但缺少统一的 stage advisory；
- workbench / timeline 能看历史，但未直接给出 stage-level 推荐动作；
- executor 能 apply/queue，但未把实时 gate + risk + approval 信号收敛成一个明确的推进建议。

这层补上后，rollout executor 会为每个 item 生成 `stage_handler.advisory`，用于支撑低干预运营：
- 何时继续 observe / collect_more_signal
- 何时 hold_until_blockers_clear
- 何时 freeze_auto_advance
- 何时 move_to_review_pending
- 何时 prepare_rollback_review

## What it adds

`analytics/helper.py`
- 新增 `_recommend_rollout_stage_advisory(...)`
- 把 validation freshness / regression、auto advance gate、risk、approval eligibility、review overdue、execution status 收敛成统一 advisory
- advisory 透传到：
  - `rollout_executor.items[].plan.stage_handler.advisory`
  - `rollout_executor.items[].audit.stage_handler.advisory`
  - `rollout_stage_progression.items[].stage_progression.advisory`
- `build_rollout_stage_progression()` 新增 summary 聚合：
  - `by_advisory_action`
  - `by_advisory_stage`
  - `by_advisory_urgency`

## Safety boundary

仍然保持原有安全边界：
- 只生成 stage-level recommendation / metadata
- 不新增任何真实交易自动执行
- validation stale / regression 仍然优先阻断推进
- queue-only / manual-gated action 仍不会被 advisory 绕过

## Intended operator value

这层主要提升三件事：
1. 更快判断市场/验证状态是否足以继续 rollout
2. 更明确告诉系统下一步应推进、冻结还是回滚准备
3. 让 unified overview / workbench / timeline 后续更容易直接显示“推荐动作”而唔使只显示原始状态