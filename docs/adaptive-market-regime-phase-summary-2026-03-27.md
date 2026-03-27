# Adaptive Market Regime 阶段总结（2026-03-27）

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
1. 继续完善 M5 governance / orchestration 闭环（现已补 joint governance / conflict resolution 首版，可继续往自动执行/审批 playbook 推）
2. 提高 report/API/dashboard 对 calibration delivery 的消费能力（已新增可消费的 joint priority queue / next actions；最新一轮再将 governance_ready 主入口上提到 report-ready 顶层与 API summary，默认消费路径更直接）
3. 再决定是否继续做更深层的 adaptive rollout automation

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


- 2026-03-27 dual-layer approval update：审批持久化从单层 latest state 扩展成 `approval_events`（immutable event log）+ `approval_state`（latest snapshot）双层模型；新增稳定事件字段 `item_id / event_type / decision / actor / reason / created_at / source / details`，并补齐 timeline 查询与基于 event log 的 snapshot recovery，仍保持 no-op，不触发真实自动 rollout / execution。

- 2026-03-27 controlled auto-approval execution layer：在既有自动审批判断层之上，新增默认关闭的受控执行层；只会把 low-risk / 无 blocker / 无需人工审批 / judgement=auto_approve / 非终态 的审批项推进到真实 `approved + ready`，并写入完整 `reason / actor / source / replay_source / event log` 审计痕迹；仍保持 no-op，不触发真实策略执行。
