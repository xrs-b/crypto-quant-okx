# Adaptive Market Regime 阶段总结（2026-03-27）

> **主线第一入口 / 总纲**：[`docs/adaptive-strategy-mainline-roadmap.md`](./adaptive-strategy-mainline-roadmap.md)
>
> 2026-03-27 晚补记：在 testnet bridge 阶段性完成后，消费层继续补了一层 `workflow operator digest` 摘要 API（`/api/backtest/workflow-operator-digest` 与 `calibration-report?view=operator_digest`），把 `workflow_state / approval_state / rollout_stage_progression / rollout_executor` 汇成低干预巡检入口，方便 dashboard / agent / 人工治理直接消费。
>
> 同轮继续补了 `approval / rollout workbench governance view`（`/api/backtest/workbench-governance-view` 与 `calibration-report?view=workbench_governance_view`），把 `workflow-consumer-view / attention-view / operator-digest / rollout executor` 再往上聚成一个更适合工作台的一眼式入口：直接回答 auto-batch、blocked、queued/ready、rollout frontier，以及最近系统自动调整了什么。

## 一句话结论
当前项目已从“近几个月数据驱动的局部适配策略”，推进到一套 **带 regime 感知、带 rollout/gate、带治理闭环雏形** 的自适应市场策略框架。

---

## 当前完成度总览

### M0：统一骨架与 schema
已完成：
- 统一 `regime snapshot` schema
- 统一 `adaptive policy snapshot` schema
- `adaptive_regime` 配置骨架与读取 helper
- 全链 observe-only 透传骨架

### M1：observe-only 可观测性
已完成：
- signal / decision / validation / execution / backtest 全链 observe-only 透传
- richer summary / tags / notes / phase / state
- dashboard / execution-state / backtest summary 可读性增强

### M2：decision-aware
已完成：
- adaptive regime 首次真实影响 `EntryDecider`
- 仅限 `decision_only / guarded_execute / full` 模式下生效
- 只收紧、不放宽 baseline
- 支持：阈值收紧、条件式 downgrade、ignored/applied/effective/audit 输出

### M3：validator + risk conservative enforcement
已完成：
- validator hints / effective snapshot / observability
- validator conservative enforcement（灰度、开关、rollout 控制）
- risk hints / effective snapshot
- risk conservative enforcement（预算/entry sizing guardrails 真生效）

### M4：execution + layering guarded live
已完成：
- execution profile hints / effective snapshot
- execution guardrails conservative enforcement
- layering guardrails 第一批 live
- `layer_ratios` / plan shape 在严格 gated 条件下 guarded live
- `layer_count` 固定为 derived/audit 字段，不允许独立 override

### M5：governance / calibration 闭环起点
已完成：
- `regime × policy` calibration report
- `policy A/B diff`
- `rollout gate`
- structured `recommendations`
- delivery payload
- report-ready API：`/api/backtest/calibration-report`
- `governance_ready` 直出入口（含 `priority_queue` / `next_actions` / `blocking_items` / `bucket_index`）
- `action_playbook` / `approval_ready` 准备层（含 `preconditions` / `approval_required` / `rollback_plan` / `risk_level` / `owner_hint` / `execution_window`）
- `workflow_ready` 工作流直出层（含 `actions` / `approval_queue` / `queues` / `filters` / `by_bucket`，供 dashboard/agent/人工治理直接消费）
- orchestration semantics（action queue / next actions / blocking chain / rollback candidates）
- controlled rollout state-apply execution layer（默认关闭；除 `joint_observe` 外，已扩到 `joint_queue_promote_safe` / `joint_stage_prepare` / `joint_review_schedule` / `joint_metadata_annotate` 等 very-safe 动作；全部只写持久化 state / workflow / metadata / audit，不触发真实交易执行）
- rollout stage orchestration 首版（新增 `stage_model` / `queue_progression` / `scheduled_review` / `orchestration_summary`，并把 stage/queue/review semantics 透传到 delivery、workflow-ready、approval persistence，继续保持默认安全关闭）
- rollout executor skeleton 首版（新增 `supported_action_map`、`dispatch -> plan -> apply -> result` envelope、`disabled/dry_run/controlled` 模式、executor audit/status 摘要；当前只对白名单 very-safe action 做 controlled apply，敏感 action 仍只 queue/plan）
- rollout executor skeleton+1（补齐统一 `handler_key`、标准化 `dispatch/apply/result.status+code`、safe apply `idempotency_key` / `idempotent_skip` 语义、queue-only `queue_plan`、`summary.by_disposition/by_status`；仍严格保持 `real_trade_execution=false` / `dangerous_live_parameter_change=false`）
- recovery orchestration / retry queue policy 首版（把 `execution_timeline + recovery_policy` 继续推进到 `recovery_orchestration`：明确 item 应进 `retry_queue / rollback_candidate / manual_recovery / recovered_monitoring`，补 `retry_stage / retry_schedule / should_retry_at / manual fallback / rollback_route`，并新增 `/api/backtest/workflow-recovery-view` 方便 dashboard / agent / operator 直接回答“边个重试、边个回滚、边个转人工、几时再试”）
- unified transition journal / state-change audit trail 首版（approval event details 回挂 `transition_journal`，数据库可直接拉最近 `from -> to / trigger / reason / actor / source / timestamp / changed_fields`，dashboard/API 提供独立 recent transition 入口，方便 workbench / agent / 人工巡检直接看“最近发生了哪些状态迁移”）
- 2026-03-28 消费层续推：transition journal 已进一步接到 `workflow_operator_digest / workbench_governance_view / unified_workbench_overview`，并同步透过 calibration-report 入口输出稳定 `transition_journal` 摘要；低干预巡检时可直接在 workbench/operator digest 看到最近状态迁移，唔使再额外单独请求 transition-journal API。
- 2026-03-28 validation freshness gate：validation replay/summary 现可声明 `freshness_policy + generated_at/evaluated_at`；当验证证据过旧时，会把 gate 标成 `stale`、冻结 auto-approval / controlled rollout / rollout executor 的自动推进，但**不会**误判成 regression rollback，避免系统拿旧 replay 结果继续自动推进。

---

## 当前系统能力（按层）

### 1. 市场识别层
- 已有统一 regime / policy snapshot
- 已能在多层输出统一 observe-only / effective 结构

### 2. 决策层
- `EntryDecider` 已能按 regime/policy 做保守收紧
- 可观测性较完整，便于解释“为什么 allow/watch/block”

### 3. 验证层
- validator 已支持 guarded conservative enforcement
- 仍严格限制为“只更保守”

### 4. 风险预算层
- 风险预算已支持 hints + conservative enforcement
- 已可真实影响 entry sizing 的 budget/cap/leverage 侧

### 5. 执行与分仓层
- execution guardrails 已可真实生效
- layering 外围 guardrails 已可真实生效
- `layer_ratios` 已可在严格 gated 条件下 guarded live
- 未越界改写 intent / reconcile / state machine 主语义

### 6. 治理闭环层
- backtest 已能产出 regime/policy calibration 报告
- 已能比较 policy version 在不同 regime 下的表现
- 已能给出 rollout / tighten / rollback / repricing / sample collection 建议
- 已开始具备 orchestration-ready 结构

---

## 当前坚持的设计原则
- 不是“换新策略”，而是 **在现有系统上加一层市场状态自适应框架**
- 所有 live 生效都遵循：
  - 默认安全关闭
  - rollout 控制
  - 只更保守，不放宽 baseline
  - fail-closed
  - 可观测、可回滚
- 不轻易改写：
  - reconcile / self-heal
  - intent lifecycle
  - direction lock 语义
  - partial TP / trailing enforcement

---

## 当前最重要的收获
1. adaptive regime 已不再只是观察层，而是对：
   - decision
   - validator
   - risk
   - execution
   - layering
   都有受控真实影响。
2. 系统已开始从“自适应执行框架”走向“自适应策略治理闭环”。
3. 后续不再只是继续加规则，而是要重点做：
   - calibration 精炼
   - rollout 治理
   - policy 版本比较
   - 可视化 / 报告消费

---

## 当前建议的下一阶段方向
优先级建议：
1. 先建设验证入口方案，解除 adaptive strategy / layering / rollout 长期依赖自然开单验证的停滞点：[`docs/adaptive-strategy-validation-entry-plan.md`](./adaptive-strategy-validation-entry-plan.md)
2. 继续完善 M5 governance / orchestration 闭环（现已补 joint governance / conflict resolution 首版，可继续往自动执行/审批 playbook 推）
3. 提高 report/API/dashboard 对 calibration delivery 的消费能力（已新增可消费的 joint priority queue / next actions；最新一轮再将 governance_ready 主入口上提到 report-ready 顶层与 API summary，默认消费路径更直接）
4. 再决定是否继续做更深层的 adaptive rollout automation

不建议当前阶段立刻做：
- 更激进的 execution override
- 放宽型 adaptive policy
- exit/profile 大改
- state machine 级重写

---

## 当前可对外表述
这个项目当前已经不是“简单做几个 adaptive 开关”，而是：

> 一套能按市场状态识别、策略版本表现、rollout gate 与 calibration recommendation 持续收紧/扩张/回滚的自适应市场策略框架雏形。

但它仍然是：
- 渐进式演进
- 强灰度控制
- 强可观测性
- 强回滚边界

而不是“一次性大爆改”。

- 2026-03-28 auto-promotion closure follow-up：controlled auto-promotion 现已补上执行后摘要层；`auto_promotion_execution` 会随 persisted approval/workflow state 回灌，operator digest / workbench governance / unified workbench overview 会直接显示最近 promotion、stage transition、reason code 同 rollback review candidate。数据库/API 亦新增 auto-promotion activity summary（含 `/api/backtest/auto-promotion-summary` 与 state-machine summary 聚合），令低干预巡检可以直接答到“最近自动推进过乜、由 guarded_prepare 去咗边、边啲要准备 rollback review”。
- 2026-03-28 review queue semantics follow-up：在 auto-promotion summary / database / dashboard API 上进一步补齐 `post_promotion_review_queue` 与 `rollback_review_queue`。系统而家不只知道“刚自动推进过”，仲会明确记录自动推进后下一步要观察嘅 observation targets（例如 post_apply_samples / validation_gate_health / transition_journal_drift）、何时 review_due_at，以及一旦 review_overdue / regression / rollback trigger 出现时应该直接转去 `prepare_rollback_review`。对应 API 新增 `/api/backtest/auto-promotion-review-queues`，方便 dashboard/workbench/agent 直接消费。
- 2026-03-28 testnet bridge execution evidence lane：受控 testnet bridge 现已补 execution evidence 回灌层；`execution receipt / reconcile_summary / cleanup_result` 会收口成统一 `testnet_bridge_execution_evidence`，并同步进入 `workflow_state.summary / approval_state.summary / workflow_consumer_view / workflow_operator_digest / dashboard_summary_cards / unified_workbench_overview / runtime_orchestration_summary / adaptive_rollout_orchestration.summary`。上层而家可直接判读本轮有冇真 testnet 执行、reconcile/cleanup 是否完成、是否仍有 residual / pending exposure / follow-up required，同时继续保持 testnet-only minimal smoke 安全边界。
