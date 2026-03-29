# Adaptive Market Regime Backlog

## 2026-03-28 已完成：testnet bridge execution evidence-gated readiness / alert
- 已新增统一 `m5_testnet_bridge_evidence_gate_v1`，把 `testnet_bridge_execution_evidence` 进一步收口成可直接消费的生产门禁语义：
  - 最近 testnet execute 是否成功（`recent_execute_succeeded`）
  - cleanup / reconcile 是否健康（`cleanup_reconcile_healthy`）
  - 是否仍有 residual / pending exposure（`exposure_clear`、`blocking_issues`）
  - 当前是否可继续放权（`can_enable_low_intervention`）
- 该 gate 已直接接入：
  - `workflow_alert_digest`：当最近 testnet execute 缺失、cleanup/reconcile 未闭合、或残留 exposure 未清时，直接升成更明确 alert；
  - `unified_workbench_overview`：rollout line / summary 会固定暴露 `testnet_recent_execute_success`、`testnet_bridge_gate_blocked` 与 gate summary；
  - `production_rollout_readiness`：会直接把 testnet evidence gate 当成 low-intervention readiness blocker / runbook 来源，而唔再只停留喺 evidence 原始字段。
- 安全边界保持不变：只消费 testnet bridge 证据、只影响后端 gate / alert / workbench 输出，**唔会扩到真实盘执行权限**。
- 已补 helper + API 相关测试，覆盖 blocked / ready 两种 evidence gate 输出。

## 2026-03-28 已完成：production rollout readiness / operator-facing gate
- 已新增统一后端生产门禁入口 `m5_production_rollout_readiness_v1`，把 `unified_workbench_overview + workflow_alert_digest + runtime_orchestration_summary + control_plane_readiness + validation_gate + auto-promotion review queues` 固定收口成一份更适合生产前/低干预前巡检的 readiness gate。
- 调用方而家可以直接见到：
  - 是否已经 `production_ready`
  - 是否可 `can_enable_low_intervention_runtime`
  - 当前 `blocking_issues`
  - 建议先做咩 `runbook_actions`
- 已落点到：
  - `analytics/helper.build_production_rollout_readiness()`
  - `dashboard/api:/api/backtest/production-rollout-readiness`
  - `dashboard/api:/api/backtest/calibration-report?view=production_rollout_readiness`
- 已补 helper + API + calibration-report view 测试覆盖，确保生产门禁输出与相关 summary 稳定存在。

## 2026-03-29 已完成：runtime daemon / CLI adaptive rollout orchestration integration
- 已把既有 `execute_adaptive_rollout_orchestration(...)` first-class 主调度入口正式接入 `bot/run.py` 运行时：
  - daemon 每轮会在 `approval_hygiene` 之后尝试执行一轮 safe orchestration pass；
  - 新增 `runtime.adaptive_rollout_orchestration` 配置（`enabled/use_cache/notify_on_activity/max_items/actor`）；
  - 新增 CLI：`python bot/run.py --adaptive-rollout-orchestration`，方便人工/agent 单次触发；
  - 运行结果会持久化进 `runtime_state.adaptive_rollout_orchestration`，保留 `gate_status / gate_blocked / auto_approval_executed_count / controlled_rollout_executed_count / review_queue_queued_count / recovery_* / testnet_bridge_status` 与 runtime summary `next_step / stuck_points / follow_ups`。
- daily `health_summary` 亦已接入 orchestration 状态，调用方可以直接睇到：
  - 运行时有没有开启 adaptive rollout orchestration；
  - 上一次 gate 系 ready 定 blocked；
  - 最近有冇 auto-approval / controlled rollout / review queue / recovery activity；
  - 下一步 runtime 预计会做乜。
- 安全边界保持不变：仍完全复用既有 `production_rollout_readiness` gate、rollout executor allowlist、review-only / metadata-only / queue-only 约束，**冇新增真实盘危险执行权限**。

## 2026-03-28 已完成：runtime orchestration summary / low-intervention entrypoint
- 已新增统一后端运行期入口 `m5_runtime_orchestration_summary_v1`，专门把 `adaptive_rollout_orchestration + workflow operator digest + workbench governance + unified workbench overview + recovery/review queues` 收口成一份更直接可巡检的 runtime summary。
- 2026-03-29 runtime cadence closure：daemon 侧 `runtime.adaptive_rollout_orchestration` 现补 `min_interval_seconds` 节流门闸；当上次 orchestration 仍在 cooldown 窗口内，会稳定返回/持久化 `cooldown_active + remaining_seconds + next_eligible_run_at`，避免每轮重复重放 safe orchestration pass、重复噪音通知与 follow-up 队列抖动，同时 `force` 单次执行仍可显式绕过节流。
- 2026-03-29 recovery same-cycle closure：`execute_adaptive_rollout_orchestration(...)` 现会把 `recovery_execution` 当成真正会改变同轮 control-plane 状态的 lane；只要 recovery 产生 `scheduled_retry / retry_reentered_executor / rollback_queued / manual_recovery_annotated` 任一状态迁移，就会追加一次 `post_recovery_queue` rollout executor rerun，并把 `recovery_*` 原因写入统一 `rerun_reasons`。目标系补齐 recovery → executor 的同轮闭环，避免 recovery 只喺本轮尾段落账、要等下一轮 runtime/agent 再消费。
- 2026-03-29 recovery rerun observability closure：runtime / operator-facing summary 而家会额外暴露 `rerun_observability`，集中说明 rerun 是否由 recovery 触发、primary reason、reason groups、rerun pass labels / count、以及 `recovery_retry_scheduled / retry_reentered_executor / rollback_queued / manual_annotation` 结果计数；`bot/run.py` 持久化 runtime_state 同 daily health summary 亦会直接显示 recovery rerun happened / why / how many / result，方便值班时一眼分清“有冇 recovery-induced same-cycle replay”。
- 2026-03-29 review/recovery follow-up budget+fairness fence：`execute_auto_promotion_review_queue_layer(...)` 同 `execute_recovery_queue_layer(...)` 现统一补上单轮 mutation 节流与基础公平性栅栏。新增 `max_mutations_per_round / max_mutations_per_queue_per_round / fairness_queue_order`（默认仍保持无限总 budget + 每队列单轮 1 个公平位，避免破坏旧行为），按 queue kind / recovery bucket 做 round-robin 选取；未获本轮名额的 item 会显式落 `skipped`，原因区分 `fairness_queue_cap_reached` / `pass_budget_exhausted`，并在 execution summary + runtime follow_ups 直接带出 budget/fairness 解释字段，避免 review/recovery follow-up lane 喺同一轮尾段持续放大。
- 2026-03-29 upstream budget fairness closure：`execute_controlled_auto_approval_layer(...)` 同 `execute_controlled_rollout_layer(...)` 现进一步补上 aging-aware selection policy。默认 `selection_policy=oldest_pending_first`，当 `max_executed_per_pass` 命中时，会优先消费等待最久的 pending / ready candidate（rollout 另外优先照顾更早 `review_due_at` 的 follow-up candidate），并把 `selection_policy / ordered_item_ids / selection_rank / selection_reference_at` 回写到 execution result。目的系补齐上游 budget fence 之后仍存在的隐性 starvation：避免 payload 固定顺序令头部 item 长期霸住 auto-approval / rollout 名额，更贴近生产级低干预编排。
- 调用方而家可以一眼见到：
  - 最近自动推进了什么（`recent_progress`）
  - 当前卡在哪里（`stuck_points`）
  - 下一步系统会怎么做（`next_step` / `next_actions`）
  - 是否仍有 review / rollback follow-up（`follow_ups`）
- 结构保持稳定、可序列化，主字段固定为：
  - `headline`
  - `summary`
  - `recent_progress`
  - `stuck_points`
  - `next_step`
  - `follow_ups`
  - `transition_journal`
  - `related_summary`
- 已落点到：
  - `analytics/helper.build_runtime_orchestration_summary()`
  - `dashboard/api:/api/backtest/runtime-orchestration-summary`
  - `dashboard/api:/api/backtest/calibration-report?view=runtime_orchestration_summary`
- 已补 helper + API 测试覆盖 runtime entrypoint / calibration-report 视图。

## 2026-03-28 已完成：control-plane manifest 消费层接入
- 已新增统一后端消费摘要 `m5_control_plane_readiness_summary_v1`，把 rollout control-plane manifest、validation gate readiness、replay-safe、upgrade-window、rollback-window 收敛成一个稳定、可序列化输出。
- 继续把 persisted contract drift 收口成稳定 `m5_control_plane_contract_drift_summary_v1`，并回挂到 `workflow_operator_digest / workbench_governance_view / workflow_alert_digest / unified_workbench_overview`；调用方而家可直接见到：边啲 item 因契约漂移被冻结、主导 drift type（`registry/version/generation/missing_snapshot`）、以及 `requires_manual_review`。
- 已落点到：
  - `analytics/helper.build_workflow_operator_digest()`
  - `analytics/helper.build_dashboard_summary_cards()`
  - `analytics/helper.build_unified_workbench_overview()`
  - `dashboard/api:/api/forward/readiness`
  - `dashboard/api:/api/backtest/workflow-operator-digest`
  - `dashboard/api:/api/backtest/unified-workbench-overview`
  - `dashboard/api:/api/backtest/rollout-control-plane`
- 调用方现可直接判断：
  - 当前 control-plane compatibility 是否允许继续自动推进
  - 当前版本组合是否 `replay_safe`
  - upgrade / rollback contract window
  - validation freeze / regression 同 readiness 的阻断关系
- 已补 helper + API 测试覆盖 direct summary / related_summary 输出。

## 2026-03-28 已完成：post-promotion / rollback review queue 语义补强
- 已把 controlled auto-promotion 执行后的 follow-up 明确拆成两条队列：
  - `post_promotion_review_queue`：自动推进后应该继续观察什么、何时复核；
  - `rollback_review_queue`：一旦 review overdue / validation regression / rollback trigger 命中，就明确升级为 rollback review。
- review queue item 现统一带出：
  - `review_due_at` / `review_window_hours`
  - `observation_targets`
  - `recommended_action`
  - `rollback_triggered`
- 已落点到：
  - analytics/helper：summary + operator/workbench attention 统一消费
  - database：auto-promotion activity summary 可直接返回 review queue 视图
  - dashboard/api：新增 `/api/backtest/auto-promotion-review-queues`
- 已补测试覆盖：post-promotion review queue、rollback review queue、database summary、dashboard API。
> **主线第一入口 / 总纲**：[`docs/adaptive-strategy-mainline-roadmap.md`](./adaptive-strategy-mainline-roadmap.md)
>
> 配套主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
>
> M3 边界方案：[`docs/adaptive-market-regime-m3-boundary-plan.md`](./adaptive-market-regime-m3-boundary-plan.md)
>
> M4 边界方案：[`docs/adaptive-market-regime-m4-boundary-plan.md`](./adaptive-market-regime-m4-boundary-plan.md)
>
> 这份 backlog 不是重复方案，而是把方案拆成**可执行、可追踪、可灰度、可回滚**的开发清单，供后续主力开发按阶段推进。

---

## 1. 使用说明

### 1.1 目标

把 adaptive regime 框架拆成可以逐项落地的 backlog，并且明确：

- 哪些任务只做观测、不影响实盘行为
- 哪些任务开始影响 `decision`
- 哪些任务开始影响 `execution`
- 每项任务的输入 / 输出 / 配置 / 可观测性 / 验收 / 风险 / 回滚点

### 1.2 阶段定义

本 backlog 采用 **M0 / M1 / M2 / M3 / M4 / M5** 作为主里程碑，辅以 **P0 / P1 / P2** 表示优先级：

- **P0**：必须先做，否则后续阶段容易返工或不可控
- **P1**：强烈建议本阶段完成，否则上线价值有限
- **P2**：增强项，可后置

### 1.3 生效边界

| 阶段 | 生效范围 | 说明 |
|---|---|---|
| M0 | 只观察不生效 | 统一字段、配置、文档、观测位，禁止改变交易行为 |
| M1 | 只观察不生效 | 生成 policy snapshot 并贯穿链路，但不改变 decision / validation / execution |
| M2 | 开始影响 decision | 仅影响 EntryDecider 的评分与 allow/watch/block |
| M3 | 开始影响 validation / risk | 仅允许更保守，不改变执行骨架 |
| M4 | 开始影响 execution | 允许 layer ratios / execution profile 轻度自适应 |
| M5 | 离线校准闭环 | 不直接自动上线，只输出建议与版本比较 |

### 1.4 总体约束

1. 所有 adaptive 行为都必须可通过配置关闭。
2. `disabled / observe_only / decision_only / guarded_execute / full` 需要保留清晰边界。
3. 在 M0/M1/M2 阶段，**不得破坏当前 layering / direction lock / open intent / reconcile / self-heal 主链路**。
4. 回测、日志、dashboard、通知的口径必须尽量与实盘一致，避免“实盘一套、分析一套”。

---

## 2. backlog 总览

| ID | 任务 | 阶段 | 优先级 | 是否生效 | 主要影响 |
|---|---|---|---|---|---|
| AR-M0-01 | 统一 regime taxonomy 与 snapshot schema | M0 | P0 | 否 | 观测基础 |
| AR-M0-02 | 设计 `adaptive_regime` 配置结构与模式开关 | M0 | P0 | 否 | 配置基础 |
| AR-M0-03 | 定义 policy snapshot / effective config schema | M0 | P0 | 否 | 链路统一口径 |
| AR-M0-04 | 在主链路预埋 observability 字段 | M0 | P0 | 否 | 日志 / DB / API |
| AR-M0-05 | README / docs / rollout playbook 建立入口 | M0 | P1 | 否 | 开发追踪 |
| AR-M1-01 | 新增 RegimePolicy resolver 骨架 | M1 | P0 | 否 | policy 统一入口 |
| AR-M1-02 | detector 接入 policy snapshot（只记录） | M1 | P0 | 否 | signal 观测 |
| AR-M1-03 | entry_decider / validator / executor 接入 policy snapshot（只记录） | M1 | P0 | 否 | 全链路观测 |
| AR-M1-04 | analytics/backtest 支持 regime tag 与 policy version | M1 | P1 | 否 | 数据闭环 |
| AR-M1-05 | dashboard/API 增加 regime 基础视图 | M1 | P1 | 否 | 观察面 |
| AR-M2-01 | decision 阈值支持 regime override | M2 | P0 | 是 | decision |
| AR-M2-02 | transition risk / stability gate 接入审批层 | M2 | P0 | 是 | decision |
| AR-M2-03 | decision breakdown 增补 regime 解释字段 | M2 | P1 | 是 | 决策可解释性 |
| AR-M2-04 | baseline vs decision_only 对比脚本 / 报表 | M2 | P1 | 否 | 上线前验证 |
| AR-M3-01 | validator 支持 effective validation snapshot | M3 | P0 | 是 | validation |
| AR-M3-02 | risk budget 支持 conservative overrides | M3 | P0 | 是 | risk |
| AR-M3-03 | `risk_anomaly` / 高 transition risk hard block | M3 | P1 | 是 | validation / risk |
| AR-M3-04 | 灰度 symbol rollout 与回滚机制 | M3 | P1 | 是 | 上线治理 |
| AR-M4-01 | execution profile 支持 regime layer ratio override | M4 | P0 | 是 | execution |
| AR-M4-02 | executor 落地 effective execution profile 追踪 | M4 | P0 | 是 | execution observability |
| AR-M4-03 | guarded layering profile（Step 3） | M4 | P0 | 是 | layering / execution |
| AR-M4-04 | trailing / partial TP regime profile 化（可选） | M4 | P2 | 是 | execution |
| AR-M5-01 | strategy × regime 离线分析报告 | M5 | P1 | 否 | 校准闭环 |
| AR-M5-02 | policy version 比较与建议生成 | M5 | P1 | 否 | 版本治理 |
| AR-M5-03 | regime detector / policy calibration playbook | M5 | P2 | 否 | 运维与迭代 |
| AR-M5-04 | approval decision persistence / replay state layer | M5 | P0 | 否（仅持久化与恢复） | workflow / approval 闭环 |
| AR-M5-08 | rollout executor skeleton / dispatch-plan-apply-result layer | M5 | P0 | 默认否（仅 skeleton / dry-run / very-safe controlled apply） | rollout execution orchestration |
| AR-M5-09 | workflow operator digest / low-intervention governance summary API | M5 | P0 | 否（仅聚合已有 workflow/approval/executor 状态） | dashboard / API / low-touch consumption |
| AR-M5-10 | workflow attention view / manual approval + blocked follow-up API | M5 | P0 | 否（仅聚合已有 workflow/approval/executor 状态） | dashboard / API / agent / low-touch巡检消费 |
| AR-M5-11 | approval / rollout workbench governance aggregate view | M5 | P0 | 否（仅聚合已有 workflow/approval/executor 状态） | dashboard / API / agent / 人工巡检工作台消费 |
| AR-M5-12 | unified transition journal / state-change audit trail | M5 | P0 | 否（仅记录与聚合状态迁移） | database / analytics / dashboard / agent / 人工巡检 |

---

> 验证入口专项方案：[`docs/adaptive-strategy-validation-entry-plan.md`](./adaptive-strategy-validation-entry-plan.md)
>
> 说明：当前 adaptive strategy / layering / rollout 的进一步推进，已明确不应再长期依赖“等自然开单再验”；后续涉及影子单、历史回放、受控 testnet、workflow/governance 受控验证入口时，统一先参考上述方案。

## 2.1 近期补充进展（2026-03-27）

### AR-M5-04｜approval decision persistence / replay state layer

- 2026-03-28：validation replay / workflow runner 已进一步接入 rollout transition policy observability，fixture-driven case 可以稳定输出并断言 `transition_policy snapshot / materialized_rule / next_transition / dispatch_route / target_stage`，减少后续 safe action / transition policy 验收对自然开单的依赖。

- **阶段 / 优先级**：M5 / P0
- **生效范围**：仅持久化、恢复、审计；**不触发真实自动执行**
- **目标**：把 dashboard / workflow-ready 输出里的 approval queue 从即时计算结果，推进到 `item_id` 级别的持久化状态台账，支持 `pending -> approved/rejected/deferred` 闭环，以及后续 replay / 恢复 / 审计。
- **涉及模块**：`core/database.py`、`dashboard/api.py`、`analytics/helper.py`、`analytics/governance.py`、`tests/`
- **输出**：新增 `approval_state` 持久化结构，核心字段包含 `item_id / approval_type / target / decision / state / workflow_state / updated_at / reason / actor / replay_source / details`。
- **状态合并语义**：
  - workflow-ready / pending API 重放时，只会刷新观测字段与 `last_seen_at`；
  - 若已有 `approved/rejected/deferred/expired` 终态，后续 replay 不会把它冲回 `pending`；
  - dashboard replay 会把已持久化状态重新叠加回 workflow-ready 视图，供恢复/审计使用。
- **安全边界**：仅落地“审批状态账本”和“回放/恢复视图”；即使审批通过，也不会新增任何危险自动执行链路。


### AR-M5-11｜approval / rollout workbench governance aggregate view

- **阶段 / 优先级**：M5 / P0
- **生效范围**：仅聚合已有 `workflow_state / approval_state / workflow-consumer-view / workflow-operator-digest / workflow-attention-view / rollout_executor`，**不触发真实自动执行**
- **目标**：补一层更适合“工作台”直接消费的集中聚合输出，让 dashboard / agent / 人工巡检一眼看到：
  1. 哪些 item 可自动批（`auto_batch`）；
  2. 哪些仍 blocked / manual approval；
  3. 哪些 queued / ready；
  4. rollout stage 当前推进到哪；
  5. 最近系统自动调整了什么（auto approval / controlled rollout / rollout executor）。
- **输出结构**：稳定 JSON，包含 `headline / summary / lanes / rollout / recent_adjustments / upstreams`，序列化稳定，适合后端直出给 dashboard / agent / 人工巡检。
- **API**：新增 `GET /api/backtest/workbench-governance-view`，并支持 `calibration-report?view=workbench_governance_view`；继续补 `GET /api/backtest/workbench-governance-items`（lane/action/risk/stage/bucket/filter 入口）与 `GET /api/backtest/workbench-governance-detail`（detail / why / next-step 入口）。
- **细化消费能力**：workbench item catalog 统一标准化 `lane_id / action_type / risk_level / approval_state / workflow_state / current_rollout_stage / target_rollout_stage / bucket_tags / why / next_step`，让调用方可以直接回答“某类 item 具体有哪些、为什么在这里、下一步是什么”。
- **兼容性**：不替换现有 consumer / attention / digest / summary-cards 视图；只是往更集中、更适合 approval+rollout 工作台消费的方向补一层聚合入口，并把 filter/detail 复用同一份序列化稳定的 item catalog。
- **测试**：覆盖 helper 聚合 payload、独立 API、calibration-report view，并补 filter/detail API 用例，确保 lane / rollout / recent adjustment / why / next-step 结构稳定存在。
- **2026-03-27 workbench 明细层补强**：`workbench-governance-detail` 继续向下补 `queue / approval / rollout` 三段 drill-down，统一输出 `queue_name / route / handler / transition_rule / next_transition / blocking_points / rollback_hints / why_summary`，让调用方可以直接回答“当前在哪条 queue/handler/route、为什么进这条路、下一步 transition 是什么、阻塞点/回滚提示是什么”，并保持 JSON 结构稳定、可序列化、适合 dashboard / agent / 人工巡检直接消费。
- **2026-03-27 merged timeline 增量**：在既有 `approval timeline`（DB immutable event log）与 `workbench detail executor/action timeline` 之上，新增统一 `merged_timeline`：
  - 以后调用方可直接看到同一 item 的 `approval DB events + workflow/executor events` 合并时间线，不需要自己 join 两套结构；
  - 输出统一包含 `schema_version / locator / summary / events / sources / raw`，其中 `summary` 会固定给出 `approval_event_count / executor_event_count / event_count / event_types / phases / timestamp_range`；
  - `build_workbench_governance_detail_view(...)` 已把 `merged_timeline` 作为 drill-down 的一部分输出，并在 `summary.merged_timeline` 回挂摘要；
  - dashboard/API 新增独立后端入口 `GET /api/backtest/workbench-governance-merged-timeline`，优先服务 dashboard / agent / 人工巡检，不做前端绑定。
- **2026-03-27 executor/action timeline detail 增量**：在上述 detail drill-down 之上，继续补 `timeline` 视图，统一产出 `summary + events + raw` 三层：
  - `summary`：直接给出 `current_status / workflow_state / approval_state / current_stage / target_stage / dispatch_route / handler_key / executor_class / decision_path / key_timestamps / audit_event_types / result_summary / blocking_points`；
  - `events`：按 `workflow -> approval -> executor_plan -> dispatch -> result` 固定 phase 串出动作执行轨迹，回答“做过哪些 action、每一步状态、走了哪条审批/调度/执行路径”；
  - `raw`：保留 `workflow_item / approval_item / executor_item` 原始快照，方便 dashboard / agent / 人工巡检做二次展开而不需要重新拼装。
  - 该视图优先服务后端/helper/API 消费层，不做前端要求；结构保持稳定、可序列化、适合后续接入 approval timeline / rollout executor timeline 汇总。
- **2026-03-27 timeline summary aggregation 增量**：继续在上述 `detail + merged_timeline` 基础上补 item / bucket / action-type 级 timeline 摘要聚合：
  - helper 新增统一 `build_workbench_timeline_summary_aggregation(...)`，复用现有 `workbench item catalog + detail + merged timeline`，直接输出 `items + groups.by_bucket + groups.by_action_type + groups.by_lane`；
  - 每个 item 会固定回挂 `timeline` 与 `merged_timeline` 的稳定摘要；每个 group 会给出 `item_count / filters / timeline_summary / merged_timeline_summary`，方便 dashboard / agent / 人工巡检快速看某 bucket 或某类 item 的整体 timeline 状态，而不用逐个点 detail；
  - dashboard/API 新增 `GET /api/backtest/workbench-governance-timeline-summary`，支持沿用 lane/action/risk/stage/bucket/owner/q 过滤条件，并补 `max_groups / max_items_per_group` 控制输出体积；
  - 输出保持稳定、可序列化，不做前端绑定，优先服务后端消费与低干预巡检。
- **2026-03-28 rollout gate consumption 补强**：把既有 `auto_advance_gate / rollback_gate` 从 rollout executor plan 继续推到低干预消费层：
  - `workflow_operator_digest` summary/attention 现直接给出 `gate_consumption`、`rollback_candidates`，调用方可以直接看哪些 item 可 auto-advance、哪些已成 rollback candidate；
  - `workbench_governance_view / workbench item catalog / timeline summary aggregation / unified_workbench_overview` 统一回挂 `auto_advance_gate / rollback_gate` 与聚合后的 blocker / trigger 统计，调用方无须再钻 `state_machine/details`;
  - workbench bucket/filter 继续沿用稳定 JSON，只增量补 `auto_advance_allowed / rollback_candidate` bucket tag 与 gate summary，优先服务 dashboard/API/agent 消费，不做前端。
- **2026-03-28 gate-driven lane / queue / route 收口**：继续把 `stage_loop + gate + operator_action_policy` 从“展示字段”推进成稳定的 lane routing 语义：
  - helper 新增统一 `lane_routing` 归属逻辑，按 `auto_advance / review_pending / rollback_prepare / blocked / queued / ready` 语义统一判定 `lane_id / lane_reason / queue_name / dispatch_route / route_family / next_transition`；
  - `workflow_consumer_view` 现会直接给每个 workflow item 回挂 `stage_loop / operator_action_policy / lane_routing / lane_id / queue_name / dispatch_route`，令 dashboard/API/workbench 后续复用同一套落位结果，而唔系各层各自猜 lane；
  - `workflow_operator_digest / workbench item catalog / workbench_governance_view / rollout_stage_progression` 已切到同一判定口径，manual approval / rollback candidate / auto batch 的车道归属更一致，低干预工作台更易解释“点解会喺呢条 lane”；
  - `core/database.py` 的统一 state machine details 亦开始持久化 `lane_routing` 摘要，令 approval persistence / replay / audit 层都能保留相同 lane/route 语义；
  - 安全边界维持不变：本次只收口 queue routing / lane semantics / metadata，不新增危险真实交易执行。
- **2026-03-27 provenance / timestamp 口径统一补强**：为 `approval timeline / executor action timeline / merged timeline / timeline summary aggregation` 补统一事件元数据，继续保持后向兼容：
  - 单条 event 统一补 `normalized_event_type / provenance / timestamp_info`，优先回答“这个事件究竟来自 approval DB、executor、workflow replay，定系 synthetic summary”；
  - `provenance` 固定表达 `origin / source / family / phase / producer / replay_source / synthetic`，避免调用方再靠 `source` 字符串猜来源；
  - `timestamp_info` 固定表达 `value / source / phase / field / fallback_fields / present`，把“时间取自 created_at、scheduled_review，定系纯 synthetic 顺序”讲清楚；
  - summary / aggregation 层同步补 `provenance_origins / provenance_sources / normalized_event_types / timestamp_sources / timestamp_phases`，方便上层直接做筛选、审计同聚合，而唔需要重新逐条扫 event。

### AR-M5-12｜unified transition journal / state-change audit trail

- **阶段 / 优先级**：M5 / P0
- **生效范围**：仅记录 approval / rollout / recovery 的状态迁移与审计语义，**不触发真实交易执行**
- **目标**：让系统不止保留 latest status / timeline summary，而系显式输出“最近发生了哪些状态迁移”：
  1. 统一记录 `from -> to`；
  2. 固定透出 `trigger / reason / actor / source / timestamp / changed_fields`；
  3. 优先落在 `database -> analytics helper -> dashboard api`；
  4. 让 dashboard / agent / 人工巡检可以直接消费 recent transitions，而唔使自己再逐条 diff event log。
- **输出**：
  - approval event details 回挂 `transition_journal`；
  - database 暴露 `get_recent_transition_journal(...) / get_transition_journal_summary(...)`；
  - analytics helper 暴露 `build_transition_journal_overview(...)`；
  - dashboard/API 暴露 `GET /api/approvals/transition-journal`，并把 transition journal 回挂到 `audit-overview`。
- **安全边界**：只做状态迁移账本与聚合，不改变真实执行边界。
- **2026-03-28 consumption-layer follow-up**：继续把 transition journal 从独立审计入口推到主消费层：
  - helper 新增稳定 `transition_journal` consumer payload（`schema_version = m5_transition_journal_consumer_v1`），统一输出 `headline / summary / recent_transitions / latest / overview`，方便 dashboard / agent / workbench 直接复用；
  - `workflow_operator_digest / workbench_governance_view / unified_workbench_overview` 现直接回挂最近状态迁移摘要，并在 summary 里补 `transition_count / latest_transition_at`，调用方无需再额外单独请求 `transition-journal` API；
  - `/api/backtest/workflow-operator-digest`、`/api/backtest/workbench-governance-view`、`/api/backtest/unified-workbench-overview` 以及 `calibration-report?view=operator_digest|workbench_governance_view|unified_workbench_overview` 已统一接入同一份 transition journal 摘要，保持低干预巡检入口一致。

### AR-M5-10｜workflow attention view / manual approval + blocked follow-up API

- **阶段 / 优先级**：M5 / P0
- **生效范围**：仅聚合已有 `workflow_state / approval_state / workflow-consumer-view`，**不触发真实自动执行**
- **目标**：补一个比 `workflow-consumer-view` / `workflow-operator-digest` 更聚焦的消费入口，让外部可直接拉到：
  1. `manual_approval`：仍需人工审批的项；
  2. `blocked_follow_up`：被 blocker / deferred / blocked 卡住、需要后续跟进的项。
- **输出结构**：稳定 JSON，包含 `headline / summary / filters / items / by_bucket / execution`，方便 dashboard、agent、人工低干预巡检直接消费，而不是再做前端二次拼装。
- **API**：新增 `GET /api/backtest/workflow-attention-view`，支持 `max_items` 限流。
- **兼容性**：不替换旧 `workflow-state`、`workflow-consumer-view`、`workflow-operator-digest`；只是在其之上追加更窄、更实用的 attention 视图。
- **测试**：覆盖 helper 聚合逻辑与 API 返回结构，确保 manual / blocked bucket、summary、filters 稳定存在。

### AR-M5-06｜controlled rollout state-apply execution layer

- **阶段 / 优先级**：M5 / P0
- **生效范围**：仅对白名单低风险治理动作落地 `state/workflow_state`；**不做真实交易执行、不直接改交易参数、不直接下单**
- **目标**：在 approval/workflow persistence 之上，补一个比 auto-approval 更保守的真实落地层：
  1. 默认关闭；
  2. 只允许 allowlist action type（默认 `joint_observe`）进入 `state_apply`；
  3. 支持少量更丰富但仍 very-safe 的动作类型：
     - `joint_observe`：把 `state/workflow_state` 安全推进到 `ready`；
     - `joint_queue_promote_safe`：只记录 safe queue promotion 审计与 ready 状态，不触发真实执行；
     - `joint_stage_prepare`：只记录 rollout stage prepare / stage transition 元数据，不触发真实执行；
     - `joint_review_schedule`：只写 review scheduling 元数据与 timeline event，状态保持 `pending`；
     - `joint_metadata_annotate`：只写 metadata / annotation / tags，状态保持 `pending`；
  4. 保留完整 `actor/source/reason/replay_source/details/event_type` 审计痕迹；
  5. 保持 terminal state 不可被后续 replay/state-apply 覆盖。
- **涉及模块**：`analytics/helper.py`、`analytics/__init__.py`、`dashboard/api.py`、`config/config.yaml.example`、`tests/`
- **配置**：`governance.controlled_rollout_execution.enabled/mode/auto_promote_ready_candidates/allowed_action_types/actor/source/reason_prefix/target_state/target_workflow_state/default_review_after_hours`
- **安全边界**：
  - 只对 low-risk + auto-approval-policy 判定可自动处理 + 无 blocker + 无人工审批要求的项生效；
  - 只做 `controlled_rollout_state_apply` immutable event；
  - `decision` 保持原值（通常仍是 `pending`），避免把 state-apply 冒充人工批准；
  - 若 item 已在终态（approved/rejected/deferred/expired），则必须跳过。

### AR-M5-07｜rollout stage orchestration / queue progression / scheduled review semantics

- **阶段 / 优先级**：M5 / P0
- **生效范围**：默认仅生成 orchestration / workflow / persistence-ready 结构；受控 state-apply 仅允许写 very-safe stage / review / queue 元数据，**不做真实交易执行**
- **目标**：在已有 `governance_ready / workflow_ready / approval_state / controlled rollout action types` 之上，把“自动 rollout”从松散 action list 推进到更明确的 stage orchestration 语义：
  1. 为每个 bucket / playbook item 生成 `stage_model`（current/target/readiness/transition_model）；
  2. 为 orchestration queue 增加 `queue_progression`（queue depth / ready actions / pending blockers / promote action）；
  3. 为 execution window 增加 `scheduled_review` 语义（trade-count checkpoint / remaining trades / review trigger）；
  4. 在 delivery / workflow / approval persistence 链路透传 `rollout_stage / target_rollout_stage / stage_model / queue_progression / scheduled_review`；
  5. 为 orchestration summary 增加 stage/readiness/queue 聚合，方便 dashboard / agent 后续做低干预巡检与自动推进。
- **涉及模块**：`analytics/backtest.py`、`analytics/helper.py`、`dashboard/api.py`、`tests/`
- **新增语义**：
  - `stage_model`：表达 observe / candidate / guarded_prepare / rollback_prepare / review_pending 等阶段，以及 advance / on_block / on_review_due / on_rollback transition；
  - `queue_progression`：表达 bucket 是否 blocked/ready/idle、可推进 action id、queue depth、推荐 safe promote action；
  - `execution_window.scheduled_review`：表达按样本数或事件驱动的 review checkpoint，而不是模糊的“稍后复核”；
  - `orchestration_summary`：表达全局 stage counts / readiness counts / blocked queue count / scheduled review count。
- **安全边界**：
  - 所有新增字段都必须可序列化、可审计、可回放；
  - 默认只增强 orchestrator 语义，不新增真实 rollout 执行；
  - 即使受控 state-apply 开启，也只允许 `joint_stage_prepare / joint_review_schedule / joint_queue_promote_safe` 这类 very-safe 元数据/状态动作；
  - 保持 terminal state preserve 语义，不允许后续 replay 覆盖终态。

### AR-M5-08｜rollout executor skeleton / dispatch-plan-apply-result layer

- **阶段 / 优先级**：M5 / P0
- **生效范围**：默认关闭；支持 `disabled / dry_run / controlled`，其中 controlled 当前只对白名单 very-safe action 做状态/元数据 apply；**不做真实交易执行、不自动改 live trading 参数**
- **目标**：在已有 `workflow_state / approval_state / auto_approval / controlled rollout / orchestration` 基础上，补一层真正可扩展的 rollout executor skeleton，先统一：
  1. `supported_action_map`：明确哪些 action 当前可执行、哪些只 queue/plan；
  2. `dispatch -> plan -> apply -> result` 执行骨架；
  3. `status / summary / audit` 执行台账；
  4. dry-run 与 controlled apply 分离；
  5. 后续 safe action types 能直接挂到 executor dispatcher。
- **当前 apply 白名单**：
  - `joint_observe`
  - `joint_queue_promote_safe`
  - `joint_stage_prepare`
  - `joint_review_schedule`
  - `joint_metadata_annotate`
- **当前 queue/plan only**：
  - `joint_expand_guarded`
  - `joint_freeze`
  - `joint_deweight`
  - `prefer_strategy_best_policy`
  - `rollout_freeze`
- **涉及模块**：`analytics/helper.py`、`analytics/__init__.py`、`dashboard/api.py`、`config/config.yaml.example`、`tests/`
- **安全边界**：
  - 默认 `enabled=false`；
  - `dry_run` 不落库，只产出执行计划与审计包；
  - `controlled` 也只允许 allowlist + low-risk + auto-approval-eligible + no blocker + no manual required 的 safe action；
  - 所有 apply 结果都保留 `execution_layer / execution_mode / rollback_capable / real_trade_execution=false / dangerous_live_parameter_change=false` 审计字段；
  - terminal state 继续不可被 skeleton apply 覆盖。
- **验收重点**：
  - disabled/dry-run/controlled 三种模式行为清晰；
  - 可执行 action 会生成 plan+audit 并按 controlled 模式安全落库；
  - 敏感 action 即使 allowlist 命中，也只会 queue，不会自动 apply；
  - 测试覆盖 supported action map、dispatch/result envelope、审计字段、dry-run no-op 语义。
- **2026-03-27 skeleton+1 增量**：
  - `supported_action_map` 补充 `handlers` 视图，统一 `handler_key=dispatch_mode::executor_class`；
  - `dispatch / apply / result` envelope 补充 `status/code` 标准字段；
  - safe apply 增加 `idempotency_key` 与 `already_applied -> idempotent_skip` 语义；
  - queue-only action 补充 `queue_plan`（queue_name / queue_priority / next_action / blocked_reason）；
  - `summary` 新增 `dry_run_count / error_count / by_disposition / by_status`，方便后续 dashboard/executor capability 扩展；
  - 继续保持 `real_trade_execution=false`、`dangerous_live_parameter_change=false`，不触发真实交易或 live 参数修改。
- **2026-03-27 skeleton+2 / queue-transition + approval-hook 增量**：
  - queue-only path 新增 `approval_hook`，显式串起 `approval_state / workflow_state / auto_approval_decision / requires_manual / blocked_by`；
  - `queue_plan` 新增 `queue_transition` 与 `queue_progression.status`，把 queue disposition 细化成 `ready_to_queue / blocked_by_approval / deferred`；
  - queue-only dispatcher 不再一律返回 `queued`，而会按审批 gate 返回 `queued / blocked_by_approval / deferred`，方便后续 queue executor / dashboard / audit 直接消费；
  - 仍保持安全边界：只做 queue semantics 与审批钩子，不触发真实交易执行，不做 live 参数 apply。
- **2026-03-27 skeleton+3 / staged-dispatcher + transition-rules 增量**：
  - executor plan / dispatch / result / persisted details 统一补充稳定字段：`transition_rule / dispatch_route / next_transition / retryable / rollback_hint`；
  - 新增 staged dispatcher rule resolver，按 `stage_model / queue_progression / approval_required / requires_manual / auto_approval_decision / blockers / terminal state` 决定 route；
  - queue-only path 细分为 `manual_review_queue / deferred_review_queue / stage_promotion_queue / operator_followup_queue` 等 dispatch route，表达更接近真实 rollout workflow 的队列语义；
  - safe-apply path 明确 `stage_metadata_apply / queue_metadata_apply / review_metadata_apply / safe_state_apply` 等 apply route，但依旧只写状态/元数据，不触发真实下单；
  - 新增 deferred retry / rollback hint 语义，方便后续 dashboard / agent / queue executor 做自动重试、人工回滚与巡检解释。

- **2026-03-27 skeleton+4 / safe-action-registry + richer-stage-handlers 增量**：
  - 在 `analytics/helper.py` 把 very-safe apply / queue-only 动作正式收敛为 `SAFE_ROLLOUT_STAGE_HANDLER_REGISTRY` + `action_registry`，不再只靠 scattered if/else 推断；
  - `supported_action_map` 继续保留，同时新增 `stage_handlers / fallback_handler`，executor 结果里新增 `action_registry`，方便 dashboard / agent / audit 稳定读取；
  - `joint_stage_prepare / joint_queue_promote_safe / joint_review_schedule / joint_metadata_annotate` 各自补 richer handler payload（`stage_handler / queue_handler / review_handler / metadata_handler`），并统一保留 `safe_handler_key / route / disposition / stage_family / observability / serialization_ready`；
  - unsupported action 明确走 fallback handler：`unsupported::unsupported_action` + `unsupported_hold`，避免未来扩 action type 时 silently 混进默认 apply；
- **2026-03-28 skeleton+5 / execution-timeline + recovery-policy 增量**：
  - `state_machine` 继续下沉一层，新增稳定的 `execution_timeline` 与 `recovery_policy` 语义，不再只有 latest `execution_status`；
  - `execution_timeline` 固定表达 `latest_status / previous_status / statuses / attempt_count / retry_count / recovered / recovered_from_status / recovery_stage / transition_rule / next_transition / rollback_hint`；
  - `recovery_policy` 固定表达 `policy / recommended_action / owner / retryable / rollback_candidate / rollback_hint / blocked_by`，方便 dashboard / API / agent 直接回答“呢个 item 依家系重试、恢复完成、定要人工介入”；
  - `core/database.py` 的 approval snapshot/timeline summary 同步透传上述结构，`/api/approvals/state-machine` summary 亦补 `recovered_count / recovery_policy_counts`；
  - 保持安全边界：仍然只做状态、审计、消费层输出增强，不新增真实交易执行。
  - 继续保持安全边界：所有 handler 都只落状态/元数据/审计，不触发真实交易执行，不做危险 live parameter apply。
- **2026-03-27 skeleton+5 / queue executor 真消费增量**：
  - queue-only path 不再只停留在 result 展示；executor 现会真正消费 `queue_plan / approval_hook / dispatch_route`，把 queue disposition 持久化进 approval/workflow 状态链；
  - `ready_to_queue / blocked_by_approval / deferred` 会分别写入稳定 workflow 状态（例如 `queued / blocked_by_approval / deferred`）与 immutable timeline event；
  - 持久化 details 统一保留 `queue_plan / approval_hook / queue_transition / queue_progression / dispatch_route / next_transition / retryable / rollback_hint / queue_plan_consumed`，方便 dashboard/api/audit/回滚解释直接复用；
  - 继续保持 fail-closed：只推进治理/队列/审批状态，不做真实交易执行，不改 live 参数。

### AR-M5-09｜workflow operator digest / low-intervention governance summary API

- **阶段 / 优先级**：M5 / P0
- **生效范围**：只读聚合；**不触发真实执行、不改审批结论、不改 live 参数**
- **目标**：在已有 `governance_ready / workflow_ready / workflow_state / approval_state / workflow-consumer-view / calibration-report` 之上，再补一层真正给人/agent/后端入口直接消费的低干预摘要：
  1. 一眼看到 `manual approval / blocked / ready / queued / deferred` 的当前状态；
  2. 把 `workflow_state + approval_state + rollout_stage_progression + rollout_executor` 汇总成稳定 digest；
  3. 输出 `headline / summary / attention / next_actions / execution / stage_progression`，减少上层自己拼字段；
  4. 通过独立 API 与 `calibration-report?view=operator_digest` 双入口暴露，方便 dashboard/backend/agent 共用。
- **涉及模块**：`analytics/helper.py`、`analytics/__init__.py`、`dashboard/api.py`、`tests/`
- **输出契约**：`schema_version = m5_workflow_operator_digest_v1`
- **安全边界**：
  - 只复用已有 workflow/approval/executor 状态；
  - 不新增自动执行；
  - 不覆盖 terminal state；
  - 不把 digest 冒充审批状态源，它只是消费层摘要。
- **验收重点**：
  - helper 能识别人工审批、blocked、ready、queued、auto-advance candidate；
  - `/api/backtest/workflow-operator-digest` 返回稳定摘要；
  - `/api/backtest/calibration-report?view=operator_digest` 可直接复用同一摘要层；
  - 测试覆盖 helper + API 双入口。

### AR-M5-10｜dashboard backend summary cards / low-intervention dashboard digest API

- **阶段 / 优先级**：M5 / P0
- **生效范围**：只读聚合；**不触发真实执行、不改审批结论、不改 live 参数**
- **目标**：在已有 `workflow-consumer-view / workflow-attention-view / workflow-operator-digest / calibration-report` 之上，再补一层更适合 dashboard summary cards 的后端聚合入口，让低干预治理入口更收口：
  1. 一眼看到 `manual / blocked / ready / queued / deferred / auto-advance` 等核心计数；
  2. 直接给出 `key alerts / next actions / executor-bridge status / rollout stage progression`；
  3. 保持结构稳定、可序列化、可被 dashboard/backend/agent/人工巡检共同消费；
  4. 通过独立 API 与 `calibration-report?view=dashboard_summary_cards` 双入口暴露。
- **涉及模块**：`analytics/helper.py`、`analytics/__init__.py`、`dashboard/api.py`、`tests/`
- **输出契约**：`schema_version = m5_dashboard_summary_cards_v1`
- **输出骨架**：
  - `headline / summary`：聚合 workflow + approval + executor + bridge 的主状态；
  - `cards[] / card_index{}`：稳定卡片结构，当前包含 `workflow_overview / key_alerts / next_actions / execution_status / stage_progression`；
  - `key_alerts / next_actions / attention / execution / stage_progression`：保留给 agent/backend 直接消费的顶层捷径；
  - `workflow_consumer_view / workflow_attention_view / workflow_operator_digest`：回挂底层来源，方便排查与扩展。
- **验收重点**：
  - `/api/backtest/dashboard-summary-cards` 返回稳定 summary card payload；
  - `/api/backtest/calibration-report?view=dashboard_summary_cards` 复用同一聚合层；
  - helper 能覆盖 counts / key alerts / next actions / blocked/manual/ready/executor/bridge 状态；
  - 测试覆盖 helper + 独立 API + calibration-report 复用入口。

### VEP-01 / VEP-04｜Shadow Validation Entry Pack（step 2 已落地）

- **阶段 / 优先级**：Validation Entry Pack / P0
- **生效范围**：仅 shadow / dry-run / replay；**不做真实下单、不做危险 live 参数修改**
- **本次已落地内容**：
  - `validation/shadow_runner.py` 已扩成统一 case loader + single run + batch replay；
  - case schema 现支持 `shadow_signal / shadow_execution / shadow_workflow`；
  - workflow case 可用 `input.symbol_results` 直接生成 `workflow_ready`，覆盖 governance / workflow / approval queue 核心链路；
  - workflow runner 会复用现有 `Database.sync_approval_items()` / timeline / state snapshot，在临时 SQLite 上做 approval replay；
  - CLI 现支持：
    - `python bot/run.py --validation-entry run --case <file>`
    - `python bot/run.py --validation-replay --case <file|dir> [more paths...]`
  - replay summary 会聚合 `case_count / pass_count / fail_count / case_types / failed_cases`；
  - 输出 envelope 继续固定包含：`case_id / case_type / mode / status / baseline / adaptive / diff / assertions / artifacts / audit`；
  - `audit.real_trade_execution=false`、`audit.exchange_mode=shadow`、`dangerous_live_parameter_change=false` 明确安全边界；
  - fixture basket 已扩到 execution + workflow：
    - `tests/fixtures/validation/execution/high-vol-tighten-long-001.yaml`
    - `tests/fixtures/validation/workflow/governance-approval-replay-001.yaml`
  - 测试已覆盖 case schema / workflow runner / replay summary / CLI 落盘。
- **当前未覆盖 / 剩余缺口**：更深 execution guard basket、workflow safe-apply allowlist，以及 testnet bridge 在真实交易所上的更完整 reconcile/cleanup 自动化。
- **最新补齐（2026-03-27 / batch 2~3）**：
  1. `shadow_workflow` 的 **testnet bridge** 已从 plan-only skeleton 推到 **controlled execute 第一层**：默认 `allow_execute=false`，显式开启后仅允许 `minimal_smoke`，并固定输出 `plan_only / controlled_execute / skipped / blocked / error` 状态口径；
  2. controlled execute 继续维持硬边界：若 `exchange.mode != testnet`、plan 不 ready、或仍有 pending approvals（默认要求为 0），则直接 `blocked`，不会误落到 real mode；
  3. 审计字段已补齐 `status / blocking_reasons / rollback_expected / cleanup_required / execute_profile / real_trade_execution`，用于后续 reconcile / cleanup / automation 接续；
  4. controlled execute 第二层已补 **最小 reconcile / cleanup trail**：bridge result 现在会稳定输出 `open_status / close_status / cleanup_needed / residual_position_detected / reconcile_summary / failure_compensation_hint`，并在 `cleanup_required=true` 且未确认收口时强制回到 `error`；
  5. shadow bridge stub / fixture 现可驱动 `open/close confirmation`、`pending-approval blocked`、`cleanup needed`、`residual position detected` 等场景，测试已覆盖成功、blocked、cleanup needed、real-mode blocked 主路径；
  6. workflow payload 已新增 **consumer view**，把 `workflow_state / approval_state / queues / rollout_executor / rollout_stage_progression` 聚成统一 API 消费对象，减少 dashboard/API 侧自行拼装；
  7. rollout executor 已补 **stage progression summary**，把 `rollout_stage -> target_rollout_stage -> next_transition / dispatch_route / retryable / rollback_hint` 统一暴露，作为更接近自动 rollout 的下一层状态机摘要；
  8. 再补 **testnet bridge summary / report export**：单 case 现固定附带 `testnet_bridge_summary`，batch replay summary 会聚合 `status_counts / blocking_reason_counts / cleanup_needed_count / controlled_execute_success_count / case_ids_requiring_cleanup`，CLI `--validation-output` 同时支持 `.md` 导出人类可读 bridge 报告，方便阶段性验收、审计和回放归档。
- **阶段判断**：
  - 以“默认关闭、真实盘硬拦截、可审计、可回滚、可批量 replay、可导出阶段报告”的标准看，**testnet bridge 已可视为阶段性完成**；
  - 后续重点不再是补 bridge skeleton，而是接真实 testnet API 查询/持仓快照、继续扩 regression basket，以及 safe-apply / queue dispatcher 的自动化闭环。
- **下一步建议**：
  1. 把 direction lock / intent / layer gap / queue progression 这些高价值 regression case 补齐；
  2. 继续扩 safe-apply / stage handler allowlist，并把 stage progression 真正喂给 queue dispatcher；
  3. 若后续接真实 testnet API，再把当前最小 trail 接到真实订单查询 / 持仓快照，而不是只停留在 smoke bridge 的结构化语义层。

### AR-M5-05｜approval audit / stale cleanup / decision diff layer

- **阶段 / 优先级**：M5 / P0
- **生效范围**：仅审计与状态卫生；**不触发真实自动执行**
- **目标**：补齐 approval/workflow 向低干预巡检、自动恢复、后续自动 approval 靠拢时最缺的三块：
  1. 哪些 pending/ready 已 stale；
  2. 最近 decision / state / workflow_state 有什么变化；
  3. 单个 item 的 timeline 概览 / 摘要如何。
- **涉及模块**：`core/database.py`、`dashboard/api.py`、`analytics/helper.py`、`tests/`
- **新增能力**：
  - `get_stale_approval_states`：返回超时未刷新、仍处于 `pending/ready/replayed` 的审批项；
  - `cleanup_stale_approval_states`：支持 dry-run 预览与真实清理，把 stale pending 安全标记为 `expired`，并写入 `stale_cleanup` immutable event；
  - `get_recent_approval_decision_diff`：从 immutable timeline 中提炼最近 decision/state/workflow_state diff；
  - `get_approval_timeline_summary`：按 item 生成当前状态、decision path、event counts、timeline preview 等摘要；
  - dashboard/API 增加 `stale` / `cleanup` / `decision-diff` / `timeline-summary` / `audit-overview` 入口。
- **状态语义**：
  - stale cleanup 只处理非终态的 `pending/ready/replayed`；
  - cleanup 默认 dry-run，显式 POST 才会落库；
  - cleanup 只把 stale 项标记为 `expired`，不会触发 preset apply、rollout、交易执行；
  - timeline rebuild 继续保留终态锁定语义，避免 stale cleanup 覆盖人工终态判断。
- **验收重点**：
  - 能回答“哪些 pending 已 stale”；
  - 能回答“最近 decision 有什么变化”；
  - 能回答“某 item 的 timeline 概览/摘要如何”；
  - 测试覆盖 cleanup / diff / timeline summary 语义。
- **2026-03-27 runtime hygiene integration**：已把 approval stale cleanup 从“只在 API/手工调用时可见”推进到运行态卫生层：
  - `bot/run.py` 新增 `build_approval_hygiene_summary(...)` / `maybe_run_approval_hygiene(...)`，守护周期会固定产出 stale + decision diff 摘要；
  - 当 `runtime.approval_hygiene.auto_cleanup_enabled=true` 时，daemon 会在每轮结束后自动把 stale `pending/ready/replayed` 安全标记为 `expired`，并保留既有 immutable `stale_cleanup` 审计事件；
  - 每日 `health_summary` 已直接暴露 approval hygiene 状态（mode / stale count / diff count / last run / last expired count），让低干预巡检唔使再额外钻 approvals API；
  - CLI 新增 `python3 bot/run.py --approval-hygiene`，方便人工/agent 手工触发 audit 或 dry-run 验证。

## 3. M0：基础定义与观测位（只观察不生效）

> M0 直接开工清单：[`docs/adaptive-market-regime-m0-implementation.md`](./adaptive-market-regime-m0-implementation.md)

### AR-M0-01｜统一 regime taxonomy 与 snapshot schema

- **阶段 / 优先级**：M0 / P0
- **生效范围**：只观察不生效
- **目标**：把当前 `core/regime.py` 的状态命名、输出字段、版本信息统一下来，避免后续 detector / decider / validator / analytics 各自理解一套。
- **涉及模块**：`core/regime.py`、`signals/detector.py`、`analytics/`、`docs/`
- **输入**：现有 `regime_info`、market context、已有趋势/波动特征
- **输出**：统一 `regime snapshot` 结构，例如 `name / family / direction / confidence / stability_score / transition_risk / features / detector_version / detected_at`
- **配置变更**：暂不要求生效配置；可先定义默认 schema 与 detector version 常量
- **可观测性要求**：日志 / signal metadata / backtest record 中都能看到同一套字段名
- **测试 / 验收**：
  - 关闭 adaptive regime 时，signal 仍可生成且不报 schema 错误
  - 至少 1 个 symbol 的 signal / log / API 输出中能看到新 schema
  - `trend` 是否拆为 `trend_up / trend_down` 必须在文档和代码口径保持一致
- **依赖关系**：无
- **风险 / 回滚点**：
  - 风险：字段改名后老 dashboard / 分析脚本读取失败
  - 回滚：保留旧字段兼容映射 1 个阶段；新增字段不替换旧字段读取入口

### AR-M0-02｜设计 `adaptive_regime` 配置结构与模式开关

- **阶段 / 优先级**：M0 / P0
- **生效范围**：只观察不生效
- **目标**：定义统一配置树和模式开关，避免后续 override 散落到 `strategies.*`、`market_filters.*`、`trading.*` 多处失控。
- **涉及模块**：`config/`、配置加载器、`core/config`（若存在）、`docs/`
- **输入**：当前全局配置、`symbol_overrides` 机制、现有策略/风控配置结构
- **输出**：`adaptive_regime.enabled / mode / detector / defaults / regimes / symbol_overrides` 结构说明与样例
- **配置变更**：新增配置段，但默认必须等价于关闭或 observe-only
- **可观测性要求**：运行时能输出当前 `adaptive_regime.mode`、policy source、symbol override 是否命中
- **测试 / 验收**：
  - 缺省不填该配置时，系统行为与当前主线一致
  - `enabled=false`、`mode=observe_only`、`mode=decision_only` 可被正确解析
  - 配置优先级有文档和代码测试覆盖
- **依赖关系**：AR-M0-01
- **风险 / 回滚点**：
  - 风险：配置优先级混乱导致某 symbol 行为不可预测
  - 回滚：所有 regime override 读取失败时必须回退旧配置，不允许读半套配置继续跑

### AR-M0-03｜定义 policy snapshot / effective config schema

- **阶段 / 优先级**：M0 / P0
- **生效范围**：只观察不生效
- **目标**：统一 detector / decider / validator / executor 消费的 policy 快照结构，避免各层再各发明一套 if/else。
- **涉及模块**：建议新增 `core/regime_policy.py` 或同级模块、`signals/`、`trading/`
- **输入**：regime snapshot、symbol、全局配置、symbol override、signal context
- **输出**：统一 `policy snapshot`，包括 `signal_weight_overrides / decision_overrides / validation_overrides / risk_overrides / execution_overrides / mode / policy_version`
- **配置变更**：无额外强依赖，但 schema 需匹配 AR-M0-02
- **可观测性要求**：每次生成 policy snapshot 时应可输出裁剪后的摘要（避免日志过爆）
- **测试 / 验收**：
  - 相同输入得到稳定输出
  - 无 regime / 低 confidence / 配置缺失时能回退到 neutral policy
  - policy snapshot 可以被序列化进 signal / decision / validation / execution 记录
- **依赖关系**：AR-M0-01, AR-M0-02
- **风险 / 回滚点**：
  - 风险：policy schema 过大，后续变更频繁击穿上下游
  - 回滚：v1 先限制字段数量，只暴露当前阶段真的消费的字段

### AR-M0-04｜在主链路预埋 observability 字段

- **阶段 / 优先级**：M0 / P0
- **生效范围**：只观察不生效
- **目标**：先把链路打通，让后续阶段即使尚未生效，也能看到“如果生效会发生什么”。
- **涉及模块**：`signals/detector.py`、`signals/entry_decider.py`、`signals/validator.py`、`trading/executor.py`、数据库模型 / 序列化层、dashboard API
- **输入**：regime snapshot、policy snapshot、effective config snapshot
- **输出**：signal / decision / validation / execution 记录里的观察字段
- **配置变更**：无；默认只写观测字段
- **可观测性要求**：最少包含：
  - `regime_name / confidence / stability_score / transition_risk`
  - `policy_mode / policy_version`
  - `effective_decision_overrides`
  - `effective_validation_overrides`
  - `effective_execution_overrides`
- **测试 / 验收**：
  - observe-only 下，allow/block/execution 结果与关闭 adaptive regime 一致
  - 至少一条完整链路中 4 层都有 regime/policy 字段
  - 新字段缺失时不能导致主链路异常
- **依赖关系**：AR-M0-03
- **风险 / 回滚点**：
  - 风险：序列化字段膨胀导致日志难读 / DB 记录过大
  - 回滚：对完整 snapshot 存摘要 + hash，详细内容仅写 debug log 或附表

### AR-M0-05｜README / docs / rollout playbook 建立入口

- **阶段 / 优先级**：M0 / P1
- **生效范围**：只观察不生效
- **目标**：让后续主力开发知道主计划、backlog、灰度上线步骤分别看哪里，避免靠聊天记录接力。
- **涉及模块**：`README.md`、`docs/adaptive-market-regime-framework-plan.md`、新增 rollout/checklist 文档（可后续补）
- **输入**：主计划文档、当前项目文档结构
- **输出**：README 文档入口、方案文档互链、开发追踪说明
- **配置变更**：无
- **可观测性要求**：无
- **测试 / 验收**：README 可直接跳转到主计划与 backlog
- **依赖关系**：无
- **风险 / 回滚点**：几乎无；回滚就是移除入口链接

---

## 4. M1：Observe Only 全链路贯通（只观察不生效）

### AR-M1-01｜新增 RegimePolicy resolver 骨架

- **阶段 / 优先级**：M1 / P0
- **生效范围**：只观察不生效
- **目标**：让 regime → policy 的映射收口到统一 resolver，后续所有生效逻辑都从这里取值。
- **涉及模块**：`core/regime_policy.py`（建议）、配置读取层、`tests/`
- **输入**：regime snapshot、symbol、全局配置、symbol override、运行模式
- **输出**：policy snapshot（neutral / observe-only）
- **配置变更**：启用 `adaptive_regime` 配置读取，但 observe-only 下不改变主链路结果
- **可观测性要求**：记录 policy source、matched regime、matched symbol override、policy version
- **测试 / 验收**：
  - resolve 相同输入结果稳定
  - neutral policy 时不注入任何生效 override
  - 输出能被后续 4 层安全消费
- **依赖关系**：AR-M0-01 ~ AR-M0-03
- **风险 / 回滚点**：resolver 若异常必须 fail-safe 回到 neutral policy

### AR-M1-02｜detector 接入 policy snapshot（只记录）

- **阶段 / 优先级**：M1 / P0
- **生效范围**：只观察不生效
- **目标**：让 signal 层能看到“原始 strength”“建议 multiplier”“如果生效后的 adjusted strength”，但暂不修改最终 signal 结果。
- **涉及模块**：`signals/detector.py`
- **输入**：regime snapshot、policy snapshot、原始 signal reasons
- **输出**：`base_strength / suggested_regime_multiplier / hypothetical_adjusted_strength / policy_version`
- **配置变更**：无实际生效开关
- **可观测性要求**：reason metadata 或 signal observability 中能看到 base vs hypothetical adjusted 对比
- **测试 / 验收**：
  - detector 输出不因 policy 缺失报错
  - observe-only 下最终 signal strength 与旧逻辑完全一致
  - 可导出“若开启 adaptive，会怎样”的观测结果
- **依赖关系**：AR-M1-01
- **风险 / 回滚点**：避免误把 hypothetical 值写回正式 strength 字段

### AR-M1-03｜entry_decider / validator / executor 接入 policy snapshot（只记录）

- **阶段 / 优先级**：M1 / P0
- **生效范围**：只观察不生效
- **目标**：把同一份 policy snapshot 带入决策层、验证层、执行层，形成一致的全链路观测记录。
- **涉及模块**：`signals/entry_decider.py`、`signals/validator.py`、`trading/executor.py`
- **输入**：signal、regime snapshot、policy snapshot
- **输出**：每层的 `effective_*_snapshot`（当前为 hypothetical）
- **配置变更**：无实际生效开关
- **可观测性要求**：能明确区分：
  - 当前真实生效值
  - observe-only 下 hypothetical override
- **测试 / 验收**：
  - `decision`, `validation`, `execution` 记录都含 `policy_mode=observe_only`
  - decision 结果、validator 结果、executor 参数与 baseline 保持一致
- **依赖关系**：AR-M1-01
- **风险 / 回滚点**：命名必须清楚，避免分析时把 hypothetical 当成真实执行值

### AR-M1-04｜analytics/backtest 支持 regime tag 与 policy version

- **阶段 / 优先级**：M1 / P1
- **生效范围**：只观察不生效
- **目标**：让后续判断不是靠感觉，而是能按 regime 分桶看 signal quality、allow rate、execution rate、收益差异。
- **涉及模块**：`analytics/backtest.py`、信号质量分析脚本、数据导出脚本
- **输入**：signal / trade records 中的 regime snapshot、policy version、mode
- **输出**：按 regime / policy version 分桶的统计字段
- **配置变更**：无
- **可观测性要求**：报表至少展示 `trade_count / allow_rate / execution_rate / win_rate / avg_return`
- **测试 / 验收**：
  - baseline 与 observe-only 都可输出一致口径报表
  - 历史缺失 regime 字段的数据可被安全跳过或标记 unknown
- **依赖关系**：AR-M0-04, AR-M1-03
- **风险 / 回滚点**：历史数据不完整时要允许 fallback，不要因为旧数据缺字段导致报表全坏

### AR-M1-05｜dashboard/API 增加 regime 基础视图

- **阶段 / 优先级**：M1 / P1
- **生效范围**：只观察不生效
- **目标**：至少先把当前 regime、confidence、policy mode、最近分布暴露出来，先以 API / 简单卡片为主。
- **涉及模块**：`dashboard/`、API 序列化层、前端模板
- **输入**：最新 regime snapshot、signal / decision 记录聚合
- **输出**：Regime snapshot 卡片、distribution 面板（基础版）
- **配置变更**：无
- **可观测性要求**：支持查看最近 24h 的 regime 占比、切换次数、allow/watch/block 分布
- **测试 / 验收**：
  - dashboard 不因 regime 数据缺失报错
  - 页面可在无交易情况下展示 regime 观察信息
- **依赖关系**：AR-M0-04, AR-M1-04
- **风险 / 回滚点**：UI 不应绑死完整字段；先做 API/简版卡片，避免一开始就重 UI

---

## 5. M2：Decision Only（开始影响 decision）

### AR-M2-01｜decision 阈值支持 regime override

- **阶段 / 优先级**：M2 / P0
- **生效范围**：开始影响 `decision`，不影响 execution
- **目标**：让 `allow_score_min`、冲突阈值等审批规则可以按 regime 覆盖。
- **涉及模块**：`signals/entry_decider.py`、`core/regime_policy.py`、配置层
- **输入**：signal breakdown、regime snapshot、policy decision overrides
- **输出**：最终 allow / watch / block 结果与详细 breakdown
- **配置变更**：`adaptive_regime.mode=decision_only`；增加 `decision_overrides` 配置项
- **可观测性要求**：输出 baseline score、effective thresholds、被 override 的原因
- **测试 / 验收**：
  - `decision_only` 下能稳定改变 allow/watch/block
  - executor 参数、risk budget、layer ratios 不发生变化
  - 可对比 baseline 与 decision_only 的差异样本
- **依赖关系**：M1 全链路观测完成
- **风险 / 回滚点**：
  - 风险：审批层过度收紧导致交易数断崖式下降
  - 回滚：切回 `observe_only`，保留数据继续对比
- **进展（2026-03-26 / M2 Step 1）**：
  - 已在 `signals/entry_decider.py` 接入 decision-aware adaptive override，当前仅消费 `decision_overrides`
  - 仅在 `adaptive_regime.mode in {decision_only, guarded_execute, full}` 且 `enabled=true` 时生效；`observe_only` 继续只观察不改决策
  - 当前实现只允许更保守方向生效：提高 `allow_score_min`、降低 `block_score_max` / `max_conflict_ratio_allow`，以及可选 `downgrade_allow_to_watch` / `downgrade_watch_to_block`
  - decision 输出已补充 `adaptive_effective_thresholds / adaptive_effective_overrides / adaptive_applied_overrides`，方便灰度观察与回滚
- **进展（2026-03-26 / M2 Step 2）**：
  - 在不触碰 validator / risk / execution 生效逻辑前提下，继续扩展 `signals/entry_decider.py` 的 decision-aware 能力
  - 新增更细粒度的保守审批门槛：可按维度收紧 `signal_strength / regime_alignment / volatility_fitness / trend_alignment / execution_risk / ml_confidence / signal_conflict_score` 的 allow 边界
  - 新增条件式 `conditional_overrides`：支持在命中指定评分/冲突条件时直接触发 `watch` / `block`，并附带结构化 `reason / tag / note`
  - observability 从“仅 applied keys”升级为 `adaptive_effective_* + adaptive_applied_overrides + adaptive_ignored_overrides + adaptive_triggered_rules + adaptive_decision_notes + adaptive_decision_tags`，方便灰度期明确看到 effective / applied / ignored 及原因
  - 继续保持“只收紧不放宽”原则：所有数值 override 仍经 conservative merge，任何会放宽 baseline 的输入都会被忽略并写入 ignored reason

### AR-M2-02｜transition risk / stability gate 接入审批层

- **阶段 / 优先级**：M2 / P0
- **生效范围**：开始影响 `decision`
- **目标**：在 regime 切换边界区增加保守惩罚，减少状态抖动造成的追单。
- **涉及模块**：`core/regime.py`、`signals/entry_decider.py`
- **输入**：`stability_score`、`transition_risk`、切换后冷却 bar 数
- **输出**：score penalty、allow→watch 降级、必要时 block
- **配置变更**：`detector.min_stability_score`、`cooloff_bars_after_switch`、transition penalty 参数
- **可观测性要求**：decision breakdown 中记录 penalty 分数与触发规则
- **测试 / 验收**：
  - 边界样本能看到决策惩罚生效
  - 稳定 regime 下不应无故触发 penalty
- **依赖关系**：AR-M0-01, AR-M2-01
- **风险 / 回滚点**：若状态指标不稳定，先仅记录 penalty 建议，不立即 hard gate

### AR-M2-03｜decision breakdown 增补 regime 解释字段

- **阶段 / 优先级**：M2 / P1
- **生效范围**：影响 decision 可解释性
- **目标**：把“这次为什么因为 regime 放行 / 变保守”写清楚，否则 decision_only 阶段很难复盘。
- **涉及模块**：`signals/entry_decider.py`、通知 / dashboard decision 展示
- **输入**：decision 结果、regime snapshot、policy snapshot、penalty/bonus 明细
- **输出**：结构化 breakdown 字段
- **配置变更**：无
- **可观测性要求**：至少包含 `regime_name / confidence / stability_score / transition_risk / policy_mode / bonuses / penalties / effective_thresholds`
- **测试 / 验收**：抽样查看高分 block 与低分 allow，解释字段完整可读
- **依赖关系**：AR-M2-01, AR-M2-02
- **风险 / 回滚点**：无明显风险，主要是避免输出过长刷爆通知
- **进度备注（2026-03-26 / M2 Step 3）**：已把 EntryDecider 内 adaptive decision 元数据收口成较稳定的 decision-audit 结构，统一 `effective / applied / ignored / triggered` 四类输出，同时保留旧 breakdown 字段兼容；仍严格遵守“只收紧不放宽”，且未触碰 validator / risk / execution 生效逻辑。

### AR-M2-04｜baseline vs decision_only 对比脚本 / 报表

- **阶段 / 优先级**：M2 / P1
- **生效范围**：不直接生效，用于验证
- **目标**：为进入 M3 提供证据，确认 adaptive regime 是“少犯错”而不是“单纯少交易”。
- **涉及模块**：`analytics/`、`scripts/`、`docs/`
- **输入**：baseline、observe-only、decision-only 样本与回测结果
- **输出**：对比报表：trade_count_delta / allow_rate_delta / win_rate_delta / bad_trade_reduction / missed_good_trade_ratio
- **配置变更**：无
- **可观测性要求**：报表中必须标明 policy version、样本区间、symbol 范围
- **测试 / 验收**：能产出至少一版 decision_only 对比结果，作为是否进入 M3 的 gate
- **依赖关系**：AR-M1-04, AR-M2-01
- **风险 / 回滚点**：无；如报表口径不稳定，先冻结 M3

---

## 6. M3：Validation / Risk 保守生效（开始影响 validation / risk）

> M3 边界、禁区、最小生效包、灰度/回滚策略详见：[`docs/adaptive-market-regime-m3-boundary-plan.md`](./adaptive-market-regime-m3-boundary-plan.md)
>
> M3 Step 1 可直接开工的实施拆分详见：[`docs/adaptive-market-regime-m3-step1-implementation.md`](./adaptive-market-regime-m3-step1-implementation.md)

### AR-M3-01｜validator 支持 effective validation snapshot

- **阶段 / 优先级**：M3 / P0
- **生效范围**：开始影响 `validation`
- **目标**：把 regime policy 产出的验证层约束变成运行时 effective thresholds，但只允许保守收紧。
- **涉及模块**：`signals/validator.py`
- **输入**：policy validation overrides、基础 validator 配置、signal context
- **输出**：effective validation config snapshot 与最终 validate 结果
- **配置变更**：新增 `validation_overrides` 配置项，如 `min_strength`、`min_strategy_count`、`block_counter_trend`
- **可观测性要求**：每次 validate 要能看到基础值 vs effective 值
- **测试 / 验收**：
  - `decision_only` 与 `guarded_execute` 的 validator 行为可明确区分
  - 不允许 override 把阈值调得比 baseline 更激进
- **依赖关系**：AR-M2 阶段完成
- **风险 / 回滚点**：若误支持放宽阈值，立即回退到 decision_only
- **实施拆分（2026-03-26）**：
  - Step 1 先做 `effective_validation_snapshot + hints + observability`，默认保持 `validator_enforcement_enabled=false`，不直接改变 pass / block 结果
  - Step 2 才进入 `validator conservative enforcement`，并要求 rollout symbol、conservative-only、防呆与回滚开关齐全
  - 详细任务拆分见：[`docs/adaptive-market-regime-m3-step1-implementation.md`](./adaptive-market-regime-m3-step1-implementation.md)
- **Status（2026-03-26 / M3 Step 2）**：done
- **Notes**：`signals/validator.py` 已从 Step 1 的 hints-only 进入 Step 2 guarded enforcement：仅在 `validator_enforcement_enabled=true` + rollout symbol 命中 + `mode in {guarded_execute, full}` 时，validator 才会小范围真正使用 adaptive effective gate；当前只接入 validator 层 conservative enforcement（`min_strength`、`min_strategy_count`、`block_counter_trend`、`block_high_volatility`、`block_low_volatility`、`risk_anomaly` hard block、`transition_risk` hard block），并新增 `validator_enforcement_categories` 以支持部分场景灰度；仍严格保持“只更保守，不放宽 baseline”，且未修改 risk / execution 生效逻辑。详细说明见：[`docs/adaptive-market-regime-m3-step2-implementation.md`](./adaptive-market-regime-m3-step2-implementation.md)

### AR-M3-02｜risk budget 支持 conservative overrides

- **阶段 / 优先级**：M3 / P0
- **生效范围**：开始影响 `risk`
- **目标**：在高波动 / 风险异常 / 低稳定状态下自动收紧总暴露、单币种暴露、入场保证金比例等。
- **涉及模块**：`core/risk_budget*`、`trading/executor.py`、可能的 risk manager 入口
- **输入**：policy risk overrides、当前账户暴露、symbol 暴露、regime snapshot
- **输出**：effective risk cap、allow/block / reduced-size 建议
- **配置变更**：新增 `risk_overrides.total_margin_cap_ratio / symbol_margin_cap_ratio / base_entry_margin_ratio / leverage_cap`
- **可观测性要求**：执行前日志必须打印 effective risk caps 与命中原因
- **测试 / 验收**：
  - 高风险 regime 下，总暴露和单币种暴露确实更保守
  - 平稳 regime 下不应无故缩仓
  - 不影响现有 direction lock / intent 幂等逻辑
- **依赖关系**：AR-M3-01
- **风险 / 回滚点**：收得过紧可能导致“理论 allow，实际永远不开仓”；回滚到 M2 或仅保留 anomaly hard block
- **Status（2026-03-26 / M3 Step 4）**：done
- **Notes**：
  - 已在 `core/regime_policy.py` / `trading/executor.py` / `RiskManager.can_open_position()` 落地 risk conservative enforcement：仅在 `guarded_execute.risk_enforcement_enabled=true`、`mode in {guarded_execute, full}`、且 rollout symbol 命中时，risk budget / entry sizing 才真正使用 adaptive effective caps。
  - 当前真实生效范围严格限制在 M3 risk budget / sizing guardrails：`total_margin_cap_ratio`、`total_margin_soft_cap_ratio`、`symbol_margin_cap_ratio`、`base_entry_margin_ratio`、`max_entry_margin_ratio`、`leverage_cap`。
  - 继续保持“只收紧不放宽”：任何会放宽 baseline 的 risk override 都会进入 `ignored_overrides`，不会进入 `enforced_budget`。
  - `adaptive_risk_snapshot` / `adaptive_risk_hints` 已补齐 `baseline / effective / enforced_budget / applied / ignored / enforced_fields / field_decisions / rollout_match / effective_state`，且 entry plan 已明确区分“看见的 effective candidate”和“真正 enforced 的 budget”。
  - 仍然**不碰 execution profile / layering 参数**，未提前进入 M4。

### AR-M3-03｜`risk_anomaly` / 高 transition risk hard block

- **阶段 / 优先级**：M3 / P1
- **生效范围**：开始影响 `validation / risk`
- **目标**：对最危险的状态给出统一 hard block 语义，减少各模块重复判断。
- **涉及模块**：`core/regime_policy.py`、`signals/validator.py`、通知 / observability
- **输入**：`risk_anomaly`、低 confidence、极高 transition risk
- **输出**：统一 hard block reason code
- **配置变更**：`force_mode: block_new_entry` 或等价配置
- **可观测性要求**：block reason 要可聚合统计，不能只是一句自由文本
- **测试 / 验收**：
  - anomaly 样本一定走统一 block reason
  - block 原因在 dashboard / 报表可汇总
- **依赖关系**：AR-M3-01, AR-M3-02
- **风险 / 回滚点**：若 detector 假阳性太多，会过度停机；回滚为 soft tighten only

### AR-M3-04｜灰度 symbol rollout 与回滚机制

- **阶段 / 优先级**：M3 / P1
- **生效范围**：开始影响 validation / risk
- **目标**：先按 symbol 或小范围 market subset 灰度，不要一口气全市场打开 guarded_execute。
- **涉及模块**：配置层、上线文档、运维脚本 / checklist
- **输入**：symbol override、样本门槛、回滚阈值
- **输出**：灰度清单、回滚 playbook、阶段性启用策略
- **配置变更**：支持 `symbol_overrides.adaptive_regime` 或白名单模式
- **可观测性要求**：能知道哪些 symbol 在 M2、哪些已进入 M3
- **测试 / 验收**：
  - 至少支持单 symbol 开启 guarded_execute
  - 回滚为 observe_only / decision_only 不需要改代码
- **依赖关系**：AR-M3-01 ~ AR-M3-03
- **风险 / 回滚点**：如果灰度粒度不够细，会让问题难定位；回滚方式必须文档化

---

## 7. M4：Execution Parameter Adaptation（开始影响 execution）

> M4 执行边界、禁区、最小生效包、灰度/回滚策略详见：[`docs/adaptive-market-regime-m4-boundary-plan.md`](./adaptive-market-regime-m4-boundary-plan.md)

### AR-M4-01｜execution profile 支持 regime layer ratio override

- **Status（2026-03-26 / M4 Step 1）**：done
- **Notes**：
  - 已在 `core/regime_policy.py` / `trading/executor.py` 落地 `baseline_execution_profile` 与 `effective_execution_profile_hint`，当前严格保持 hints-only，不改 live entry plan / layer plan / 下单输入。
  - 已支持 execution conservative-only merge，当前覆盖 `layer_ratios`、`layer_max_total_ratio`、`max_layers_per_signal`、`min_add_interval_seconds`、`profit_only_add`、`allow_same_bar_multiple_adds`、`leverage_cap`。
  - observability 已统一输出 `baseline / effective hint / applied / ignored / rollout_match / effective_state / hint_codes / ignored reasons`，并通过 executor observability / intent plan_context 进入摘要链路。
  - 默认仍由 baseline execution profile 驱动真实执行；`execution_profile_enforcement_enabled=false`、`layering_profile_enforcement_enabled=false` 时不会提前进入 M4 Step 2。

- **阶段 / 优先级**：M4 / P0
- **生效范围**：开始影响 `execution`
- **目标**：在不改执行骨架的前提下，根据 regime 调整 layer ratios / max total ratio 等执行参数。
- **涉及模块**：`trading/executor.py`、layer plan state、配置层
- **输入**：policy execution overrides、当前 layer plan、regime snapshot
- **输出**：effective execution profile
- **配置变更**：新增 `execution_overrides.layer_ratios / layer_max_total_ratio / leverage_cap`
- **可观测性要求**：每笔 intent / order 记录 effective layer profile、policy version、root signal id
- **测试 / 验收**：
  - range / high_vol 下 layer ratios 可变得更保守
  - 不允许破坏已有 plan state reset、direction lock、open intents 逻辑
  - 同一笔单的 effective execution profile 可被完整追溯
- **依赖关系**：M3 稳定后再做
- **风险 / 回滚点**：执行参数覆盖最容易把 layering 搞乱；任何异常优先回退到 M3
- **实施拆分（2026-03-26 / M4 Step 1）**：
  - Step 1 先做 `execution profile hints + effective snapshot + observability`，默认保持 `execution_profile_enforcement_enabled=false`，不直接改变 live execution profile
  - Step 2 才进入 `guarded execution profile enforcement`，并要求 rollout symbol、conservative-only、防呆与回滚开关齐全
  - `layer_ratios` 真生效、deep layering enforcement、partial TP / trailing hints/enforcement 继续后置，不混入 Step 1
  - 详细任务拆分见：[`docs/adaptive-market-regime-m4-step1-implementation.md`](./adaptive-market-regime-m4-step1-implementation.md)

### AR-M4-02｜executor 落地 effective execution profile 追踪

- **阶段 / 优先级**：M4 / P0
- **生效范围**：从 execution observability 进入受控 execution guardrail enforcement
- **目标**：让执行层不仅“用了什么参数”，还要能解释“为什么这套参数此时生效”，并在 rollout / conservative-only 前提下让外围 guardrails 小范围真生效。
- **涉及模块**：`trading/executor.py`、通知、trade/open_trade 记录、dashboard execution 面板
- **输入**：effective execution profile、policy snapshot、account exposure
- **输出**：结构化 execution observability 记录
- **配置变更**：无额外要求
- **可观测性要求**：至少包含 `policy_mode / policy_version / effective_layer_ratios / effective_cap / override_reason`
- **测试 / 验收**：随机抽取执行样本，可从 trade 记录追到 regime 与 policy
- **依赖关系**：AR-M4-01
- **风险 / 回滚点**：若 rollout 样本不足或 observability 解释不清，应立即关闭 `execution_profile_enforcement_enabled` 回退到 hints-only
- **Notes**：M4 Step 2 已落地最小可控 enforcement：仅当 `execution_profile_enforcement_enabled=true` + rollout symbol 命中 + `mode in {guarded_execute, full}` 时，executor 才真正采用 `enforced_profile`。当前真实生效字段仅限 guardrails：`layer_max_total_ratio`、`max_layers_per_signal`、`min_add_interval_seconds`、`profit_only_add`、`allow_same_bar_multiple_adds`、`leverage_cap`；`layer_ratios` 继续默认 hints-only，除非显式开启 `layering_profile_enforcement_enabled`。同时 observability 已补齐 `baseline / effective / enforced_profile / enforced_fields / execution_profile_really_enforced / field_decisions`。详细说明见：[`docs/adaptive-market-regime-m4-step2-implementation.md`](./adaptive-market-regime-m4-step2-implementation.md)

### AR-M4-03｜guarded layering profile（Step 3）

- **阶段 / 优先级**：M4 / P0
- **生效范围**：开始影响 `layering profile`，但仍坚持 conservative-only 与分层开关
- **目标**：在 Step 2 execution guardrails 已稳定前提下，把 layering baseline / effective / live profile 审计补齐，并先让 guardrail-like layering 字段进入最小 live；`layer_ratios` 默认继续 hints-only，后置到第二批 rollout
- **涉及模块**：`core/regime_policy.py`、`trading/executor.py`、`tests/`、配置层、execution observability / dashboard 摘要链路
- **输入**：policy execution/layering overrides、baseline layering config、当前 layer plan、regime snapshot、rollout gating
- **输出**：`adaptive_layering_snapshot`、`baseline/effective/live layer plan audit`、`enforced_fields / hinted_only_fields / plan_shape_really_enforced`
- **配置变更**：建议新增/明确 `layering_profile_hints_enabled`、`layering_profile_enforcement_enabled`、`layering_plan_shape_enforcement_enabled`
- **可观测性要求**：至少能回答 baseline / effective / live layering profile 是什么，哪些字段真生效，`layer_ratios` 是否仍只是 hints-only
- **测试 / 验收**：
  - `layer_ratios` 在 `layering_plan_shape_enforcement_enabled=false` 时不得进入 live layer plan
  - `layer_max_total_ratio / max_layers_per_signal / min_add_interval_seconds / profit_only_add` 可继续作为最小 live layering 包
  - rollout miss 必须回退 baseline
  - 不允许破坏 layer plan reset / direction lock / open intents / reconcile / self-heal
- **依赖关系**：AR-M4-01, AR-M4-02
- **风险 / 回滚点**：`layer_ratios` 最容易污染当前 layering 主链路验收，任何异常优先关闭 `layering_plan_shape_enforcement_enabled` 再视情况关闭 `layering_profile_enforcement_enabled`
- **实施拆分（2026-03-26 / M4 Step 3）**：
  - Step 3 文档已明确：先上 layering audit 与最小 live guardrails，再后置 `layer_ratios` 真生效
  - 第一批 live 字段：`layer_max_total_ratio`、`max_layers_per_signal`、`min_add_interval_seconds`、`profit_only_add`（`allow_same_bar_multiple_adds` 作为附属节奏 guardrail）
  - `layer_ratios` 默认继续 hints-only，等第二批 rollout 才允许进入 live layer plan
  - 详细实施拆分见：[`docs/adaptive-market-regime-m4-step3-implementation.md`](./adaptive-market-regime-m4-step3-implementation.md)
  - 2026-03-26 update：第一批 guarded layering live 已接入 execution/layering 路径；`layer_max_total_ratio`、`max_layers_per_signal`、`min_add_interval_seconds`、`profit_only_add`、`allow_same_bar_multiple_adds` 可在 `layering_profile_enforcement_enabled=true` + rollout 命中时真生效，`layer_ratios` 仍只做 hints / audit。
  - 2026-03-27 planning update：已补 M4 Step 4 实施拆分文档，明确 `layer_ratios` / plan shape guarded live 需独立 `layering_plan_shape_enforcement_enabled`、独立 rollout、独立回滚；`layer_count` 仅作为 derived audit 字段，不做独立扩层 override。详见：[`docs/adaptive-market-regime-m4-step4-implementation.md`](./adaptive-market-regime-m4-step4-implementation.md)
  - 2026-03-27 batch2 planning update：已新增第二批可直接开工拆分文档，进一步把 hints / guardrails live / plan shape live 三层边界、`layer_ratios` 与 `layer_count` 的 derived-only 关系、以及 `layer_max_total_ratio` / `max_layers_per_signal` / `min_add_interval_seconds` / `profit_only_add` 的协同约束与回滚方式写清楚。详见：[`docs/adaptive-market-regime-m4-step4-batch2-implementation.md`](./adaptive-market-regime-m4-step4-batch2-implementation.md)
  - 2026-03-27 implementation update（第一批）：execution/layering 路径现已真正消费 guarded live layering guardrails；`layer_max_total_ratio`、`max_layers_per_signal`、`min_add_interval_seconds`、`profit_only_add`、`allow_same_bar_multiple_adds` 仅在 `layering_profile_enforcement_enabled=true` + rollout 命中时 live，且保持 conservative-only；`layer_ratios` 继续 hints-only，`layer_count` 继续只作 derived/audit。
  - 2026-03-27 implementation update（第二批前半段 / done）：已补 `merge_layer_ratios_conservatively(...)`、`derive_layer_count_from_ratios(...)`、`build_layering_plan_shape_snapshot(...)`，并让 executor 仅在 `layering_profile_enforcement_enabled=true` + `layering_plan_shape_enforcement_enabled=true` + plan-shape rollout 命中 + guardrails 已 live + conservative-only + total-cap 校验通过时消费 live `layer_ratios`。默认仍 fail-closed 回 baseline；`layer_count` 明确禁止独立 override，仅由 `layer_ratios` 派生。observability 已补 baseline/effective/live/enforced/applied/ignored、field decisions、plan-shape validation、live source、是否真正 enforced。

### AR-M4-04｜trailing / partial TP regime profile 化（可选增强）

- **阶段 / 优先级**：M4 / P2
- **生效范围**：开始影响 execution
- **目标**：把 trailing / partial TP 按 regime 轻度 profile 化，但这是增强项，不应早于 layer ratio 适配。
- **涉及模块**：止盈止损逻辑、持仓管理、通知
- **输入**：policy execution profile、持仓状态、波动水平
- **输出**：effective trailing / TP 参数
- **配置变更**：新增 trailing / partial TP regime profile 段
- **可观测性要求**：必须能看出某笔平仓是“策略原始参数”还是“regime override 参数”触发
- **测试 / 验收**：
  - 不与现有 partial TP / layering 兼容方案冲突
  - 样本不足时只允许 shadow record，不立即全量生效
- **依赖关系**：AR-M4-01, AR-M4-02
- **风险 / 回滚点**：该项与当前项目的 layering / partial TP 关系最敏感，建议最后做；一旦出现解释复杂或行为冲突，立即关闭

---

## 8. M5：离线校准与版本治理

### AR-M5-01｜strategy × regime 离线分析报告

- **Status（2026-03-27 / M5 Step 2）**：partial done
- **Notes**：
  - 已在 `analytics/backtest.py` 落地首版 `regime × policy` calibration report，先把全链路 adaptive 样本按 `regime_tag / policy_tag` 收口成统一离线分析出口。
  - 当前回测聚合结果已补 `all_trades`、symbol 级 `regime_policy_calibration`，以及聚合级 `calibration_report`，可直接看到 `by_regime / by_policy_version / by_regime_policy` 的 trade_count、win_rate、avg_return_pct、total_return_pct。
  - 本次继续把 `strategy × regime` 校准入口真正落地：trade 级结果开始保留 `strategy_tags / dominant_strategy / strategy_reasons`，并在 symbol / aggregate calibration report 中新增 `by_strategy / by_regime_strategy / by_policy_strategy / regime_strategy_fit / strategy_policy_fit`，让 adaptive strategy 调整不再只看 policy bucket，而能直接看各 regime 下边个 strategy 真有 fit。
  - delivery / report-ready payload 亦同步暴露 strategy fit 表格与 `top_strategy` headline，方便后续 dashboard / report / agent 直接消费，而唔需要再自己由原始 trade reasons 横向拼装。
  - 现进一步把治理建议从 `regime × policy_version` 扩展到 `regime × strategy`：基于现有 `by_regime_strategy + regime_strategy_fit` 输出 `strategy_recommendations / strategy_governance`，可直接回答某 strategy 在某 regime 下应扩张、降权、冻结、观察、补样、repricing/review 的治理动作。
  - strategy governance 保持与既有 rollout gate / recommendation 风格一致，统一给出 `type / governance_mode / priority / confidence / blocking / actions / summary_line / orchestration`，并在聚合级 delivery 中补 `strategy_priority_queue / strategy_next_actions / strategy_blocking_items`，方便后续 report / orchestration 直接消费。
  - 本轮再补 `joint_governance` 联合治理层：基于既有 `recommendations + strategy_governance + strategy_policy_fit + rollout_gates + policy_ab_diffs` 对 `regime × policy_version × strategy` 输出稳定结构，明确 `conflict_resolution / combined_actions / final_governance_decision`，回答当 policy 想 expand、但 strategy 想 freeze/deweight（反之亦然）时，应该由谁优先、点样 freeze / 降权 / 继续观察 / guarded expand。
  - 联合治理明确加上 `conflict_category / priority_resolution / blocking_precedence / fallback_decision`：默认以已有 gate/diff/recommendation 结果为证据，不额外拍脑袋；当 policy 层出现 rollback/tighten blocker，会优先压住 strategy 扩张；当 policy 可 expand 但 strategy fit 明显 underperform，则先冻结或降权到 strategy 粒度；若 strategy 自己有更优 policy fit，亦会把它作为 guarded rollout 的额外 guardrail。
  - `delivery` / `orchestration_ready` 同步补 `joint_priority_queue / joint_next_actions`，让 dashboard/report/agent 后续可以直接消费联合治理队列，而唔需要再自己横向 join policy queue 同 strategy queue。
- **阶段 / 优先级**：M5 / P1
- **生效范围**：不直接生效
- **目标**：形成 `strategy × regime` 的二维统计，为后续 policy 调整提供数据依据。
- **涉及模块**：`analytics/`、`ml/`（如适合）、报表脚本
- **输入**：历史 signal / trade / regime / policy version 数据
- **输出**：按 regime 的 win rate / avg return / bad trade reduction / strategy fit 报表
- **配置变更**：无
- **可观测性要求**：报表需标明时间窗、样本量、symbol 范围
- **测试 / 验收**：可稳定导出至少一版离线分析结果
- **依赖关系**：M1~M4 的观测字段完整
- **风险 / 回滚点**：无；关键是不要过度拟合小样本

### AR-M5-02｜policy version 比较与建议生成

- **Status（2026-03-27 / M5 Step 4）**：done
- **Notes**：
  - `analytics/backtest.py` 的 `calibration_report` 已补齐 `policy_ab_diffs` 与 `rollout_gates`，不再只给分桶 summary，而是开始输出可直接指导 rollout / tighten / rollback 的结构化判断。
  - `policy_ab_diffs` 现会以样本最多的 policy 作为 baseline，输出 candidate vs baseline 的总体 delta，以及同 regime 下的 `delta_trade_count / delta_win_rate / delta_avg_return_pct / sample_ready`，方便做 policy A/B compare。
  - `rollout_gates` 现按 `regime × policy_version` 输出明确 `decision=expand|hold|tighten|rollback`、`reason`、`message` 与关键指标；`summary.rollout_gate_summary` 则提供整体 gate tally，方便后续报告或 dashboard 直接消费。
  - 新增自动 calibration 建议层：每个 `regime × policy_version` 都会产出结构化 `recommendation object`，除 `type / category / priority / confidence / reason / suggested_action / blocking_issue / gate_decision / evidence` 外，现进一步补 `governance_mode / actions / rollout_plan / thresholds / guardrails / next_review_after_trade_count / summary_line`，让 dashboard / report / rollout playbook 可直接消费治理动作而非再做二次猜测。
  - 在此基础上又补一层统一 `delivery` payload：把 `by_regime_policy + rollout_gates + recommendations + baseline comparison` 收口成稳定的 `views.items`，并给出 `render_ready.sections` 与 `orchestration_ready.queue/queues/action_catalog`，让后续 dashboard/report/人工治理或自动 rollout orchestration 不必再自己横向 join 四份输出。
  - `summary.delivery_ready` 会暴露 schema version、bucket 数、blocking 数与 priority queue size，方便聚合层快速判断这批 calibration 结果是否已经具备“可渲染 / 可编排”消费条件。
  - 现进一步补上后端消费入口：`analytics.backtest.export_calibration_payload()` / `build_calibration_report_ready_payload()` 负责把聚合级 `calibration_report` 整成稳定的 `report_ready` 结构；`dashboard/api.py` 新增 `/api/backtest/calibration-report?view=report_ready|delivery|governance_ready|full`，默认直接返回可给 dashboard/report/agent 消费的 `summary + delivery_ready + views.items + render_ready + orchestration_ready`，外部调用方无需再自己钻 `run_all()['calibration_report']['delivery']`。
  - 本轮再把 strategy + policy 联合治理单独收口为 `governance_ready` 直出层：统一暴露 `items / priority_queue / next_actions / blocking_items / bucket_index`，并在 `delivery` 与 `report_ready` 内同步挂载，令 dashboard / report / agent / 人工治理消费端都唔使再自己拼 `joint_governance + conflict_resolution + joint_priority_queue + joint_next_actions`。
  - 继续前推消费层整合：`build_calibration_report_ready_payload()` 现将 `joint_governance / priority_queue / next_actions / blocking_items / bucket_index` 直接上提到 report-ready 顶层，并在 `tables.governance_ready` 保留完整对象；`summary.governance_ready` 同步暴露 item / queue / blocking 计数与 schema，令 dashboard/report/consumer 默认优先读联合治理 ready 结构，而唔使再钻入底层 `delivery.views.tables` 或原始 `joint_governance/conflict_resolution` 明细。
  - 建议对象继续优先从既有 `rollout_gates + policy_ab_diffs` 推导，而不是拍脑袋：样本不足升级为更可执行的 `collect_more_samples` + `rollout_freeze`；负收益类拆成 `tighten_thresholds`、`rollout_freeze`、`repricing_review`；正向桶统一落到带 guardrail 的 `expand_guarded`；正收益但稳定性不足则归到 `repricing_review` / instability review 流程。
  - M5 orchestration 再往前推一步：每个 `views.items[*]` 现已附带 `orchestration.action_queue / next_actions / blocking_chain / review_checkpoints / rollback_candidate`，把原本只有 queue 的结果升级成带动作顺序、依赖关系、复核检查点、快速回退候选的执行语义。这样下游无论系 dashboard、报告生成，定系 agent/人工治理，都可以更直接回答“下一步先 freeze 还是 collect samples、repricing review 要唔要等 rollback 后、expand 之前仲有咩 blocker 未解”。
  - 聚合级 `delivery.orchestration_ready` 亦同步补 `prioritized_queue / next_actions / blocking_chain / review_checkpoints / rollback_candidates`，并把 `summary.delivery_ready` 扩展到 next action / blocking chain / rollback candidate 计数，方便外层快速判断呢批 calibration 输出是否已经接近可执行闭环，而唔止系静态展示。
  - 再向前推进一层可执行准备：`joint_governance` 现补 `action_playbook`，把 `combined_actions` 展开成稳定、可序列化的逐项动作卡片，统一附带 `risk_level / owner_hint / execution_window / preconditions / rollback_plan / approval_required / approval_roles`，方便 dashboard / agent / 人工审批流直接回答“下一步具体做咩、边个批、失败点样退”。
  - `build_joint_governance_ready_payload()` / `delivery.orchestration_ready` 同步新增 `approval_ready`、`joint_action_playbook`、`joint_approval_queue`，令治理输出唔止系 queue/summary，而系接近审批工作台可直接消费的 prepared action set；仍保持 no-op，不触发真实自动执行。
  - 今轮再补更明确的 workflow 入口：`build_governance_workflow_ready_payload()` 与 `/api/backtest/calibration-report?view=workflow_ready` 直接暴露 `actions / approval_queue / queues / filters / by_bucket / summary`，dashboard / report / agent / 人工治理可唔经手 `governance_ready` 原始结构就直接消费准备好的审批工作流数据。
  - 2026-03-27 implementation update（workflow state layer / done）：在 `workflow_ready` 之上再补一层稳定、可序列化的 `workflow_state + approval_state`，把 `item state / approval state / transition / execution readiness / rollback readiness / by_bucket indexes` 统一收口；旧 `actions / approval_queue` 继续保留兼容，方便 dashboard / agent / 人工低干预逐步迁移，而唔需要一次过重写所有消费端。
  - 同步在 `dashboard/api.py` 新增 `/api/backtest/workflow-state` 自然入口，默认直接返回 workflow state layer，方便后续审批工作台、auto-approval agent、rollout orchestrator 直接对接。
  - 2026-03-27 implementation update（controlled auto-approval execution layer / done）：在既有 `auto_approval_policy + approval_state + workflow_state + approval_events` 之上新增受控自动批执行层，默认由 `governance.auto_approval_execution.enabled=false` + `mode=disabled` 安全关闭；只有显式切到 `mode=controlled` 时，且审批项同时满足 `auto_approval_decision=auto_approve`、`auto_approval_eligible=true`、`risk_level=low`、`blocked_by=[]`、`requires_manual=false`、`approval_required=false`、当前非终态，才会真正写入 `approval_state=approved` 与 `workflow_state=ready`。执行层只做审批状态流转，不触发真实 preset apply / strategy execute；并把 `reason / actor / source / replay_source / rule_hits / execution_layer` 全量写进 snapshot + immutable event log，供 dashboard/api/audit/replay 直接消费。
  - 2026-03-28 implementation update（recovery orchestration / retry queue policy / done）：在 `state_machine.execution_timeline + recovery_policy` 之上再补稳定 `recovery_orchestration`，显式输出 `queue_bucket / target_route / retry_stage / retry_schedule / should_retry_at / rollback_route / manual_recovery.fallback_action`，把 recovery 从“描述层”推进到“可编排层”。同时 `workflow_consumer_view.summary` / `workflow_state.summary.state_machine` 新增 recovery queue 统计，`dashboard/api.py` 新增 `/api/backtest/workflow-recovery-view`，令 dashboard / agent / operator 可直接回答：哪些 item 入 retry queue、哪些做 rollback candidate、哪些转 manual recovery、何时应再试；仍严格保持 no-op / no real trade execution。
- 2026-03-27 rollout executor test sync：`joint_expand_guarded` 这类 queue-only 敏感动作，executor 的 `dispatch.reason` 已从早期泛化值 `queue_only` 收口为 spec 级 `blocked_reason`（例如 `live_rollout_parameter_change_not_supported`），旧失败 `test_rollout_executor_skeleton_dispatches_safe_actions_and_queues_sensitive_actions` 属于测试期望过时，已同步到当前语义，避免后续 stage handler / queue plan 继续沿用旧口径踩坑。
- 2026-03-27 implementation update（auto-approval policy / done）：在既有 `approval_state / workflow_state / governance_ready / workflow_ready` 之上补一层明确 `auto_approval_policy`，统一给出稳定字段 `auto_approval_decision / reason / confidence / requires_manual / auto_approval_eligible / blocked_by / rule_hits`；规则优先复用现有 `risk_level / approval_required / action_type / governance_mode / final decision / blocking issues / preconditions`，把 low-risk + no-blocker item 标为可自动批准候选，把 high-risk / expand/deweight/freeze 类动作收口到 manual review，把 rollback/critical 风险项收口到 freeze，把 observe/review 或仍有 blocker 的项收口到 defer；仍保持 no-op，只推进到“可自动判断”，不做真实自动执行。
  - `summary.recommendation_summary` 现除 critical/high/medium/low tally 外，也补 `by_type / by_governance_mode / blocked / aligned_with_rollout_gate / top_actions / top_priority_items`，方便后续 dashboard/report 直接做治理面板与 rollout 队列。
- **阶段 / 优先级**：M5 / P1
- **生效范围**：不直接生效
- **目标**：让每次 policy 更新都有版本号、差异说明与基于数据的建议，而不是拍脑袋改 multiplier/threshold。
- **涉及模块**：`analytics/`、`docs/`、配置版本管理
- **输入**：policy version A/B、回测 / 实盘统计
- **输出**：版本比较报告、建议变更项、回退建议
- **配置变更**：policy version 命名规范
- **可观测性要求**：每次报表都必须带 policy version
- **测试 / 验收**：能比较至少两版 policy，并给出变更影响摘要
- **依赖关系**：AR-M5-01
- **风险 / 回滚点**：若版本标记不统一，先冻结版本比较功能

### AR-M5-03｜regime detector / policy calibration playbook

- **阶段 / 优先级**：M5 / P2
- **生效范围**：不直接生效
- **目标**：总结如何校准 detector 阈值、如何调整 policy、何时允许升级模式、何时必须回滚。
- **涉及模块**：`docs/`
- **输入**：前面阶段的报表、复盘经验、灰度记录
- **输出**：运维/研究 playbook
- **配置变更**：无
- **可观测性要求**：无
- **测试 / 验收**：文档可指导后续开发者进行版本更新和回滚决策
- **依赖关系**：AR-M5-01, AR-M5-02
- **风险 / 回滚点**：无明显风险

---

## 9. 建议先做的 M0 范围（推荐最小起步包）

### 必做范围（建议一次做完）

1. **AR-M0-01**：统一 regime snapshot schema
2. **AR-M0-02**：定义 `adaptive_regime` 配置结构与模式开关
3. **AR-M0-03**：定义 policy snapshot / effective config schema
4. **AR-M0-04**：在 signal / decision / validation / execution 预埋观察字段
5. **AR-M0-05**：补 README / docs 入口与开发追踪说明

### 为什么先做这 5 项

因为这 5 项完成后：

- 后续开发不再靠聊天记忆“regime 到底长什么样”
- M1 能直接开始做 observe-only，而不会先返工字段设计
- M2/M3/M4 的每一层都知道自己该消费什么 snapshot
- 未来回测、dashboard、通知不会各说各话

### M0 完成定义

满足以下条件即可视为 M0 完成：

- adaptive regime 的 schema、配置、模式边界都已文档化
- 代码里已有明确预埋点，哪怕还未正式启用真实 override
- 关闭 adaptive regime 时行为不变
- 主力开发以后进入项目，只看 README + plan + backlog 就知道下一步做什么

---

## 10. 上线顺序建议

1. **先做 M0**：统一文档、schema、配置、可观测位
2. **再做 M1**：全链路 observe-only 跑基线
3. **再做 M2**：只影响 decision，先验证审批是否更聪明
4. **再做 M3**：只做保守收紧，不做激进放大
5. **最后做 M4**：再让 execution 参数自适应
6. **M5 持续循环**：所有调整基于离线/实盘数据复盘

---

## 11. 开发跟踪建议

建议后续主力开发按以下方式追踪：

- 每完成一个 backlog 项，就在本文件对应项下追加完成日期 / PR / commit
- 每次模式升级（如 observe_only → decision_only）都要追加“升级依据 / 样本区间 / 回滚标准”
- 如果某项最终决定不做，直接在对应项下标注 `dropped` 与原因，不要静默遗忘

推荐追加格式：

```markdown
- Status: done / in_progress / blocked / dropped
- Owner: <name>
- PR/Commit: <sha or link>
- Notes: <关键决策 / 风险 / 回滚备注>
```

---

## 12. 相关文档

- 主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
- 本 backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)
- 分仓配置说明：[`docs/layering-config-notes.md`](./layering-config-notes.md)
- 分仓验收清单：[`docs/layering-acceptance-checklist.md`](./layering-acceptance-checklist.md)

> 一句话总结：**先把口径统一、先把观察打通、先从 decision 开始，再逐步碰 risk 与 execution。** 唔好一上来就把 adaptive regime 直接塞进 executor 主干，咁样最易出事。

## Status（2026-03-26 / M3 Step 4）: done

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


- 2026-03-27 dual-layer approval update：审批持久化从单层 latest state 扩展成 `approval_events`（immutable event log）+ `approval_state`（latest snapshot）双层模型；新增稳定事件字段 `item_id / event_type / decision / actor / reason / created_at / source / details`，并补齐 timeline 查询与基于 event log 的 snapshot recovery，仍保持 no-op，不触发真实自动 rollout / execution。
- 2026-03-27 implementation update（workflow / approval state machine closure / done）：在 `database + analytics/helper + dashboard api` 之间补了一层统一状态机语义，显式收口 `approval_state / workflow_state / queue_status / dispatch_route / next_transition / executor_result / rollout_stage`。`approval_state.details.state_machine` 现稳定产出 `schema_version / phase / terminal / retryable / rollback_candidate / lifecycle_path / executor_result`，并由 `merge_persisted_approval_state(...)` 回挂到 approval/workflow items；workflow summary 亦新增 `queued_count / retry_pending_count / execution_failed_count / state_machine` 聚合，方便同一 item 从建议→审批→排队→执行准备→结果/重试/回滚用统一语义持续跟踪，而唔使上层自己再拼状态。
- 2026-03-28 implementation update（operator action policy / done）：在既有 runtime approval hygiene loop 之上，继续把 `operator_action_policy` 接到 unified state machine / workflow summary / operator digest / workbench / runtime health summary：
  - `state_machine` 新增稳定 `operator_action_policy` 结构（`action / route / priority / owner / follow_up / reason_codes`），可直接表达 `retry / escalate / review_schedule / freeze_followup / observe_only_followup`；
  - `workflow_state.summary.state_machine` 新增 `operator_action_counts / operator_route_counts / follow_up_counts`，令消费层唔止知 blocked/stale，而系知应该点分流；
  - `workflow_operator_digest` 的 `next_actions` 已改为按 action policy 分组，workbench catalog/item 亦新增 `operator_action / operator_route / operator_follow_up` 过滤与明细字段；
  - `runtime approval hygiene` / `health_summary` 亦补 `operator_action_summary`，让 stale / decision diff 能直接带出 review / escalate / observe-only routing 建议；
  - 2026-03-28 consumption-layer follow-up：`workbench-governance-detail` 现直接暴露 `operator_action_policy` 摘要与 `drilldown.operator_action`，同时 `workbench-governance-view / items / timeline-summary / detail` API 均支持按 `operator_action / operator_route / follow_up` 显式过滤，方便 agent / dashboard 低干预按 routing lane 消费；
  - 2026-03-28 aggregation follow-up：`workbench_timeline_summary_aggregation` 已升级到 `v2`，除 `by_bucket / by_action_type / by_lane` 外，再新增 `by_operator_action / by_operator_route / by_follow_up`；并且所有 group/lane/bucket payload 统一补 `operator_action_policy_summary`（`action_counts / route_counts / follow_up_counts / dominant_* / policy_combinations / reason_codes`），调用方可以直接看某 bucket/lane/action group 嘅 operator policy 汇总，唔使逐 item 再分析；
  - 全部仍限定在 metadata / review / queue routing 层，不触发危险真实执行。
- 2026-03-27 implementation update（dashboard state-machine summary API / done）：新增 `GET /api/approvals/state-machine`，直接返回 approval/rollout item 的统一状态机摘要与 `phase_counts / workflow_state_counts / rollback_candidate_count / retryable_count / terminal_count`，方便 dashboard / agent / 低干预巡检直接回答“当前处于 proposal / approval / queue / execution / terminal 哪一段、下一步去边、仲可唔可以 retry/rollback”。

- 2026-03-28 low-intervention group summary follow-up：继续把 workbench / operator digest 消费层往“唔使点开 detail 都睇到主流动作”推进一层；新增稳定 `low_intervention_summary` 结构，并挂到 `workflow_operator_digest.next_actions[*] / workflow_operator_digest.group_summaries / workbench_governance_view.lanes[*] / workbench_governance_view.group_summaries / workbench_timeline_summary_aggregation.groups[*]`。摘要现直接提供 `headline / dominant_action / dominant_route / dominant_follow_up / dominant_priority / priority_mix / status_overview(blocked/manual/ready/queued/deferred/auto_batch) / lane_mix / workflow_state_mix / approval_state_mix / risk_level_mix / bucket_mix`，调用方可直接按 `bucket / lane / operator_action / operator_route / follow_up` 看每组的主流治理方向与处理状态，减少逐 item / timeline drilldown 需要。
- 2026-03-28 execution-status / transition-engine follow-up：在 `approval_state.details.state_machine` 与 helper/API 消费层继续补齐更贴近真实执行器的动作状态语义；当前统一持久化并对外暴露 `execution_status=queued|dispatching|applied|skipped|blocked|deferred|error|recovered`、`transition_rule / next_transition / last_transition`，让 queue promotion、safe apply、blocked/deferred、error→recover 等路径都可审计、可回放、可恢复；`/api/approvals/state-machine` 亦同步输出 `execution_status_counts`，workbench item/timeline 聚合会带出 execution status 与 last transition。
- 2026-03-28 rollout gate closure update：继续沿主线把 `safe rollout executor registry + richer stage handlers` 推前一步；`analytics.helper.execute_rollout_executor(...)` 与 `action_registry` 现统一附带 `auto_advance_gate / rollback_gate / gate_policy`，固定输出 `readiness_score / blockers / manual_required / review_window_open / rollback_triggered / idempotency_rule`。低风险 allowlisted safe action 可明确标识 very-safe auto-advance 候选；执行失败、critical risk、review overdue、rollback pending 等情况会稳定暴露 rollback candidate 语义，方便后续 workbench / operator digest / auto-advance gate 继续复用。
- 2026-03-28 executor stage-loop closure：在上述 gate 语义之上，rollout executor 进一步固定输出 `stage_loop`（`loop_state / recommended_action / auto_advance_allowed / review_pending / rollback_candidate / waiting_on / why_stopped`），并把它贯穿到 `plan / result / stage_progression`。调用方可直接回答：哪些 item 仍属 `auto_advance`、哪些已进入 `review_pending`、哪些要走 `rollback_prepare`，而唔使再手动拼 `stage_handler + auto_advance_gate + rollback_gate`；同时保持 very-safe 边界、审计字段与序列化稳定。
- 2026-03-28 rollout transition-policy closure：继续沿 E1/E2 主线，把 action-specific transition semantics 从 executor 内部散落判断收口到 spec 级 `transition_policy`。目前 `joint_stage_prepare / joint_queue_promote_safe / joint_review_schedule / joint_metadata_annotate` 及 queue-only 敏感动作都统一声明 `transition_rule / dispatch_route / next_transition / retryable / rollback_hint`，并由 `action_registry / supported_action_map / executor plan / audit / stage_progression` 同步复用，方便后续把 stage handler、timeline、validation replay 与版本治理挂到同一套 transition contract 上。
- 2026-03-28 unified workbench overview follow-up：继续喺既有 `workflow_state / approval_state / workflow_operator_digest / workflow_recovery_view / workbench_governance_view / workbench_timeline_summary_aggregation` 之上，再补一层统一三线总览 `build_unified_workbench_overview(...)`，后端/API 直接输出 `approval / rollout / recovery` 三条主线的 `current_state / counts / headline / key_alerts / next_actions`，并新增 `GET /api/backtest/unified-workbench-overview` 以及 `calibration-report?view=unified_workbench_overview`，方便 dashboard / agent / 人工低干预巡检一眼睇清整体治理状态，而唔使自己再拼多份摘要。
- 2026-03-28 validation-gate consumer/persistence follow-up：继续把 validation gate 从 executor gate 接到更稳定嘅 persisted approval/workflow state 同消费层；`_persist_workflow_approval_payload(...)` 现会喺 rollout executor 落库后再做一次 persisted merge，把 `validation_gate / auto_advance_gate / rollback_gate / stage_loop` 回灌到 approval_state / workflow_state，令 freeze / regression / rollback 触发原因可以跨 replay 稳定保留。`dashboard_summary_cards` 新增独立 `validation_gate` card，`unified_workbench_overview.summary|lines.rollout` 亦会直接输出 `validation_gate + validation_gate_consumption`（gap_count / dominant freeze reason / dominant rollback trigger）；`/api/approvals/state-machine` 则补 `validation_status_counts / validation_freeze_reason_counts / rollback_trigger_counts` 同 item 级 `validation_gate / rollback_gate`，方便 dashboard / workbench / agent 直接答到“而家 gate 状态、最近缺口/回归、为何 freeze/rollback”。
- 2026-03-28 validation-gated routing follow-up：validation gate 而家唔止可见，已经直接影响 `operator_action_policy / lane_routing / stage_loop / workbench/operator digest`；当 gate 只系 capability gap/freeze 时，会把 ready/queued 候选改派到 `review_schedule -> validation_review_queue -> review_validation_freeze`，并把 lane 落到 blocked/review path；当 gate 出现 failing-required / failing-cases regression 时，则会把 active rollout item 升格成 `freeze_followup -> rollback_candidate_queue -> rollback_candidate_review`，同步把 `stage_loop` 推到 `rollback_prepare`、`lane` 推到 `rollback_candidate`。manual approval 仍优先保留 manual lane，继续维持 queue-only / metadata-only 安全边界，不触发危险真实交易执行。
- 2026-03-28 validation-gated execution follow-up：继续把上述 routing 真正接到执行门禁；`execute_controlled_auto_approval_layer(...)`、`execute_controlled_rollout_layer(...)` 同 `execute_rollout_executor(...)` 现统一附带 `execution_gate`，并在 very-safe 自动推进前硬性消费 validation gate。语义上会明确区分 `validation_gate_gap`（缺 capability／coverage，冻结自动推进）、`validation_gate_freeze`（整体未 ready 但未见回归）、`validation_gate_regression`（failing required capability / failing cases，除阻止 apply 外仲会保留 rollback candidate 语义）。各层返回值与持久化 details 会直接带 `effect / primary_reason / reason_codes / explain / validation_gate`，调用方可稳定回答点解 blocked / 点解仍可 allowed，而唔会误把 validation 只当成旁路提示。
- 2026-03-28 stage-loop consumer closure：在既有 `stage_loop` executor envelope 基础上，再向消费层补一层稳定摘要；`workflow_operator_digest.summary.stage_loop`、`workbench_governance_view.summary.stage_loop / lanes[*].stage_loop / group_summaries[*].stage_loop`、`workbench_timeline_summary_aggregation.summary.stage_loop / groups[*].stage_loop`、`unified_workbench_overview.lines.rollout.stage_loop / summary.stage_loop` 现统一给出 `loop_state_counts / path_counts / dominant_loop_state / dominant_path / waiting_on_counts`。调用方可直接睇到各 item / lane / line 当前主导系 `auto_advance`、`review_pending` 定 `rollback_prepare`，唔使再钻 plan/result/details。
- 2026-03-28 rollout stage advisory consumer follow-up：继续把 executor 侧 `stage_handler.advisory` 真正接到低干预消费入口；新增统一 `rollout_advisory` snapshot/summary，并挂到 `workflow_operator_digest.summary|stage_progression`、`workbench_governance_view.summary|rollout`、`unified_workbench_overview.summary|lines.rollout`。调用方而家可直接见到 `recommended_action / recommended_stage / urgency / ready_for_live_promotion`，同时稳定输出 `attention.auto_promotion_candidates` 与 `auto_promotion_candidate_queue`，唔使再自己从 executor plan/audit 手工拼 auto-promotion 候选。
- 2026-03-28 auto-promotion candidate view：继续把 advisory / validation_gate / auto_advance_gate / rollback_gate / operator_action_policy / lane_routing 收口成一份稳定、可序列化嘅 `auto_promotion_candidate_view`。新结构会逐 item 直接给出 `can_auto_promote / why_promotable / missing_requirements / risk_label / risk_score / manual_fallback_required / manual_fallback_reason_codes`，并通过 `GET /api/backtest/auto-promotion-candidates` 与 `calibration-report?view=auto_promotion_candidate_view` 暴露。调用方可直接睇到“边啲 item 可推进、点解可推进、仲差乜、风险几高、要唔要人工兜底”，作为下一层自动 rollout 入口，唔使再自己横向拼 digest/workbench/detail 多份数据。
- 2026-03-28 controlled auto-promotion execution：`execute_controlled_rollout_layer(...)` 现正式消费 `auto_promotion_candidate_view` 做真实但仍然 very-safe 嘅 stage/state 推进；只有在 `governance.controlled_rollout_execution.auto_promote_ready_candidates=true` 且 candidate 同时满足 ready / low-risk / no-blocker / no-manual-fallback / validation gate pass / allowlisted action 时，先会落真实 approval/workflow state。审计会额外保留 `before/after state+stage / reason_codes / actor/source / event_log / rollback_hint`，并继续明确 `real_trade_execution=false`、`dangerous_live_parameter_change=false`，终态与 review/rollback stage 一律保护不自动推进。
- 2026-03-28 unified workbench/timeline API compatibility fix：发现 `workbench-governance-timeline-summary` 路由曾误调 `build_unified_workbench_overview(...)`，并把 `lane_ids` 等过滤参数直接塞入 helper，触发 `unexpected keyword argument 'lane_ids'`。现已收口为两层修复：1) 路由改回正确调用 `build_workbench_timeline_summary_aggregation(...)`；2) `build_unified_workbench_overview(...)` 补兼容层，继续接受旧式 `lane_ids / action_types / ...` kwargs，并统一折叠进 `filters`，减少旧 caller 再踩 API 口径坑。

- 2026-03-28 auto-promotion closure follow-up：继续把 controlled auto-promotion 从“会执行”推进到“执行后可追踪、可审计、可回滚消费”。本轮新增统一 `auto_promotion_execution_summary`，把已执行 promotion 的 `before/after rollout_stage`、`reason_codes`、`risk_label`、`actor/source/created_at` 与 `rollback_review_candidates` 聚成稳定摘要；`merge_persisted_approval_state(...)` 会把 `auto_promotion_execution` 回灌到 approval/workflow item，令 operator digest / workbench governance / unified workbench overview 都能直接消费最近 promotion 与回滚复核候选；数据库同步补 `get_auto_promotion_activity()` / `get_auto_promotion_activity_summary()`，`/api/backtest/auto-promotion-summary` 与 `/api/approvals/state-machine` 亦会暴露 auto-promotion event/transition/rollback 摘要，方便低干预巡检追问“最近自动推进咗咩、由边个 stage 去边个、边啲需要回滚 review”。
- 2026-03-28 workbench/unified overview review-queue consumption：继续把 `post_promotion_review_queue / rollback_review_queue` 从独立 summary 接回 `workbench_governance_view` 同 `unified_workbench_overview`。后端新增稳定 `auto_promotion_review_queue_consumption`，统一输出 `dominant_queue_kind / dominant_action / review_due_count / observation_target_counts / rollback_trigger_counts`，并喺 `workbench_governance_view.summary|rollout`、`unified_workbench_overview.summary|lines.rollout` 补 `auto_promotion_review_queues` + `follow_up_review_queue`。调用方低干预巡检时而家可以直接一眼睇到自动推进后仲有边啲 follow-up / rollback review 要跟、优先做咩动作，唔使再额外拼独立 review queue API。
- 2026-03-28 review-queue detail/filter follow-up：继续把 auto-promotion review queue 从 summary 推进到 item-level 消费入口；`analytics/helper.py` 新增 `build_auto_promotion_review_queue_filter_view(...)` 同 `build_auto_promotion_review_queue_detail_view(...)`，稳定支持按 `queue_kind / due_status / observation_target / rollback_trigger / q` 过滤，并统一输出 `why_in_queue / queue_reason / next_step / review_due / due_status / due_in_hours`。`dashboard/api.py` 同步新增 `GET /api/backtest/auto-promotion-review-items` 与 `GET /api/backtest/auto-promotion-review-detail`，令 dashboard / agent / workflow caller 可以直接答到：边啲 item 喺 post-promotion review / rollback review 队列、点解喺度、下一步做咩、几时到期。
- 2026-03-28 review-queue execution follow-up：继续把 auto-promotion review queue 从“睇到”推进到“可受控入队执行”。`analytics/helper.py` 新增 `execute_auto_promotion_review_queue_layer(...)` 与独立 execution settings（`governance.auto_promotion_review_execution.*`），会在 metadata-only / review-only 边界内，把 `post_promotion_review_queue` 的 due review 推成 `review_pending`、把 `rollback_review_queue` 推成 `rollback_prepare`，并统一写回 `auto_promotion_review_execution.review_status / queue_kind / why_in_queue / next_step / event_log`。`dashboard/api.py` 同步新增 `GET /api/backtest/auto-promotion-review-execution`，方便 dashboard / agent 直接答到：边啲 follow-up review 已正式入队、入咗咩队、点解入、下一步系 post-review 定 rollback-review，而唔使再靠聊天记忆追 promotion 后续动作。
- 2026-03-28 recovery execution follow-up：继续把 `recovery_orchestration` 从“可见”推进到“可受控消费”。`analytics/helper.py` 新增 `execute_recovery_queue_layer(...)` 与 `governance.recovery_execution.*`，会在 metadata-only / queue-only 边界内，把 due retry item 标成 `queued + retry_queue`、把 rollback candidate 推成 `rollback_pending + rollback_candidate_queue`，并为 manual recovery 项补上稳定 execution/audit 注记；`dashboard/api.py` 同步新增 `GET /api/backtest/recovery-execution`，令 dashboard / agent / runtime 可以直接答到 recovery 队列有冇被正式接手执行，而唔止停留喺 summary / filter 视图。
- 2026-03-28 recovery retry→executor re-entry：继续沿 recovery 主线补 closure，due retry item 而家唔止会入 `retry_queue`，仲会在保留 `retry_scheduled` recovery 审计后，经 `retry_executor` 受控子路径重入 `execute_rollout_executor(...)` subset pass。系统会稳定输出并持久化 `retry_source / retry_attempt / reentered_executor / executor_reentry(dispatch_route|result_code|disposition)`，令 recovery 回流 executor 嘅来源、次数、dispatch 路径同结果状态变化都可解释、可回放、可测试；同时继续保持 `real_trade_execution=false` 同 `dangerous_live_parameter_change=false`。

- 2026-03-28 orchestration closure follow-up：继续把 auto-approval / rollout execution 从“各层可独立运行”推进到“同一轮可治理闭环”。新增 `execute_adaptive_rollout_orchestration(...)` 作为统一 orchestration 入口：会先对 `rollout_executor` 做 pre-approval dry-run 预演，再执行 `controlled_auto_approval`，若有新批准 item 则同轮重跑 executor 并接上 `controlled_rollout_execution` 与 `auto_promotion_review_execution`。同时把 `approved` 从“对 rollout 一律终态锁死”收口为“可继续 very-safe metadata rollout，但仍保留审批终态审计”，令低风险 item 可以在同一 replay 周期内完成 `plan -> auto-approve -> safe rollout -> follow-up review queue`，更贴近低干预生产闭环。
- 2026-03-28 control-plane manifest follow-up：为承接 roadmap 里「policy version / action registry version / stage handler version / schema version 联动管理」，现已新增统一 `build_rollout_control_plane_manifest(...)`，稳定输出 `versions / registries / contracts / compatibility`，并把 manifest 挂到 `rollout_executor.control_plane_manifest` 与 `unified_workbench_overview.control_plane_manifest`；`dashboard/api.py` 同步新增 `GET /api/backtest/rollout-control-plane`。重点唔系再加一个 summary，而系补生产化升级/回滚前必需嘅 contract 口径：明确当前 control plane 是否同代兼容、是否 replay-safe、升级窗口同回滚窗口系乜，避免后续自动推进继续靠聊天记忆判断版本边界。
- 2026-03-28 testnet bridge execution evidence lane：继续沿受控 testnet bridge 主线，把 execution receipt / reconcile / cleanup 结果正式回灌到消费层统一摘要。当前 `execute_testnet_bridge_layer(...)` 会稳定产出 `testnet_bridge_execution_evidence`，并同步挂到 `workflow_state.summary / approval_state.summary / workflow_consumer_view / workflow_operator_digest / dashboard_summary_cards / unified_workbench_overview / runtime_orchestration_summary / adaptive_rollout_orchestration.summary`；调用方可直接回答本轮有冇真 testnet 执行、open/close/reconcile 是否确认、cleanup 是否完成、是否仍有 residual / pending exposure / follow-up required，且仍严格限定 `exchange.mode=testnet`、`minimal_smoke` 与 no-real 执行边界。
