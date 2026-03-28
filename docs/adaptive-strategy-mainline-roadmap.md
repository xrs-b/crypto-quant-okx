# Adaptive Strategy Mainline Roadmap

> 当前建议的 **第一入口 / first entry**。
>
> 如果你只打算先读一份文档，请先读这里；需要补细节时，再跳去 framework / backlog / phase summary / 各阶段 implementation 文档。

## 0. 主线优先阅读顺序

1. **本文**：`docs/adaptive-strategy-mainline-roadmap.md`
2. 阶段总结：`docs/adaptive-market-regime-phase-summary-2026-03-27.md`
3. 执行清单：`docs/adaptive-market-regime-backlog.md`
4. 总体设计：`docs/adaptive-market-regime-framework-plan.md`
5. 专项实施稿：M0 / M3 / M4 / validation-entry 等 implementation / boundary plan 文档

---

## 1. 总体目标

把当前 `crypto-quant-okx` 从“带若干 adaptive 开关的交易系统”，持续推进成一套：

- **可感知市场状态**：能识别 regime / stability / transition risk
- **可自适应决策**：能按 regime 对 signal / decision / validation / risk / execution 做受控收紧
- **可治理**：能基于 calibration、policy diff、rollout gate、workflow state 做版本化推进
- **可低干预运行**：默认安全关闭、灰度放开、强审计、强回滚，让人工更多做巡检与例外处理，而不是天天手动盯盘拍板

### 生产级目标的准确定义

这里说的“生产级低干预自适应市场策略系统”，不是“完全无人值守随便开自动下单”，而是：

1. adaptive 行为有清晰版本与边界；
2. 从 regime → policy → decision → validation → execution → governance → rollout 有统一状态口径；
3. 默认 fail-closed；
4. 关键动作可 explain / replay / audit / rollback；
5. 人工主要做例外审批、抽检、回滚和策略升级确认。

---

## 2. 当前阶段判断

## 2.1 一句话判断

**当前已完成从 M0 到 M5 的“骨架搭建 + 受控生效 + 治理雏形”，项目已进入 `M5.x 治理闭环深化 / 向低干预 rollout 过渡` 阶段。**

## 2.2 已经完成到哪

已明确完成：

- **M0**：统一 regime / policy snapshot / adaptive 配置骨架
- **M1**：全链 observe-only 透传与可观测性
- **M2**：decision-aware，自适应开始真实影响 EntryDecider
- **M3**：validator + risk conservative enforcement
- **M4**：execution + layering guarded live
- **M5（第一段）**：calibration、policy diff、rollout gate、workflow-ready、approval persistence、executor skeleton、transition journal 等治理闭环雏形

### 当前更精确的位置

如果把当前 M5 再细分，项目大致位于：

- **M5.1 已完成**：governance-ready / workflow-ready / approval-ready 输出稳定
- **M5.2 已完成**：approval persistence / replay、workbench / digest / attention / timeline / transition journal 等消费层主入口已经成形
- **M5.3 进行中 / 待继续**：从“可治理、可汇总、可安全 state-apply”进一步推进到“**safe rollout action executor registry + richer stage handlers + 更明确的自动推进边界**”
- **2026-03-28 implementation update**：rollout executor 现已把 `auto_advance_gate / rollback_gate` 作为稳定结构接入 `action_registry + executor plan/audit/details`，统一输出 `readiness_score / blockers / manual_required / review_window_open / rollback triggers / idempotency_rule`，令 safe handler 不止知道“可做什么”，仲知道“几时可以自动推、几时应该停或准备回滚”。
- **2026-03-28 transition-policy update**：safe rollout action registry 再向前收口一步：各 action spec 现内建 `transition_policy`（含 `transition_rule / dispatch_route / next_transition / retryable / rollback_hint / default_target_stage`），并统一暴露到 `action_registry / supported_action_map / executor plan / audit / stage_progression`。换言之，executor 对“下一步怎么推”不再只靠散落 if/else，而係有一层可复用、可审计、可版本化的 transition policy 底座。
- **2026-03-28 implementation update (stage handlers)**：新增 richer rollout stage handler 语义，覆盖 `observe / candidate / guarded_prepare / controlled_apply / review_pending / rollback_prepare`；executor plan / stage progression / persisted details 现在会统一暴露 `owner / auto_progression / waiting_on / why_stopped / next_transition / rollback_stage`，令系统可以更明确回答“当前卡在哪里、谁负责推进、下一步系继续推进、等 review，定准备 rollback”。
- **2026-03-28 implementation update (stage-loop consumption)**：`stage_loop` 已继续上推到 low-intervention 消费层；operator digest、workbench governance、timeline aggregation、unified workbench overview 都会稳定回挂 `stage_loop` 摘要与主导路径，让 dashboard/API/agent 直接判断 auto-advance / review-pending / rollback-prepare 三条路径分布，而唔使再回到底层 executor `plan/result/details` 自己拼。
- **2026-03-28 implementation update (stage advisory consumption)**：rollout `stage_handler.advisory` 已进一步收口成统一 `rollout_advisory` 消费结构，并正式挂到 `workflow_operator_digest / workbench_governance_view / unified_workbench_overview`。低干预调用方而家可直接拿到 `recommended_action / urgency / ready_for_live_promotion`，同时有稳定 `auto_promotion_candidates` / `auto_promotion_candidate_queue` 队列可供后续自动推进与运营摘要复用。
- **2026-03-28 implementation update (gate-driven lane routing)**：`lane / queue / route` 归属已开始收口到统一 `lane_routing` 语义层；workflow consumer / operator digest / workbench catalog / workbench view / rollout stage progression 会共用同一套判定，稳定输出 `lane_id / lane_reason / queue_name / dispatch_route / route_family / next_transition`，令 `auto_batch / manual_approval / blocked / queued / rollback_candidate / ready` 的落位更一致、更可解释，且继续维持 queue-only / metadata-only 安全边界。
- **2026-03-28 implementation update (control-plane manifest)**：为继续向生产级低干预收口，rollout 主线现新增统一 `control_plane_manifest`，显式声明 `action registry version / stage handler registry version / transition policy version / gate policy version / stage loop / lane routing / operator action policy` 等 contract，并输出 compatibility / replay-safe / upgrade-window / rollback-window 摘要。manifest 已挂到 rollout executor、unified workbench overview，并通过独立 API 暴露，方便升级前后快速确认“当前控制平面版本是否同代、可否安全 replay / 回滚 / 继续自动推进”。
- **2026-03-28 implementation update (post-promotion review closure)**：`execute_auto_promotion_review_queue_layer(...)` 现由“只会把 follow-up review 入队”推进到“可安全完成 due review / 分流 rollback review”。当 post-promotion review 已到期、且未触发 rollback regression 时，系统会以 metadata-only 方式把 item 标记为 `post_promotion_review_completed` 并回到 `workflow_state=ready`；若命中 rollback review queue，则继续升级到 `rollback_prepare`。新执行摘要统一暴露 `completed_count / rollback_escalated_count`，dry-run 亦会保留 `dry_run_complete` 语义，方便低干预 runtime 一眼分清“已完成观察窗口”同“要准备回滚”嘅 item。
- **2026-03-28 implementation update (runtime orchestration entrypoint)**：为低干预运行期再收口一层，后端现新增 `runtime_orchestration_summary` 入口，直接聚合 `adaptive_rollout_orchestration / recent adjustments / stuck points / next action / review queue / rollback follow-up`。调用方无须再手动拼 operator digest、workbench、unified overview，已经可以直接回答“最近自动推进了什么、卡在哪、下一步会点做、是否仍需 review/rollback 跟进”。
- **2026-03-28 implementation update (testnet execution evidence gate)**：testnet bridge 证据层现再向前收口成统一 `testnet_bridge_evidence_gate`，直接回答“最近 execute 有无成功、cleanup/reconcile 是否健康、是否仍有 residual/pending exposure、可否继续 low-intervention 放权”。该 gate 已挂入 `workflow_alert_digest / unified_workbench_overview / production_rollout_readiness`，令生产门禁同 workbench alert 可以直接基于最近 testnet 执行证据判级，而唔使调用方自行解释原始 evidence 字段。

换句话说：

> 现在已经不是缺“adaptive 想法”，而是缺把现有 adaptive 能力稳稳串成一条 **可自动推进但仍低风险** 的生产级闭环主线。

---

## 3. 主线阶段地图

下面采用 **Phase A ~ Phase F** 表达长期主线，同时标注它与既有 **M0 ~ M5** 的对应关系，方便后续继续追踪。

| Phase | 对应里程碑 | 当前状态 | 核心目标 | 建议优先级 |
|---|---|---:|---|---|
| Phase A | M0-M1 | 已完成 | 建立统一 schema / policy snapshot / observability | P0 |
| Phase B | M2 | 已完成 | 自适应开始影响 decision，但只保守收紧 | P0 |
| Phase C | M3-M4 | 已完成 | 自适应开始影响 validation / risk / execution / layering | P0 |
| Phase D | M5.1-M5.2 | 大部分完成 | 建立 governance / workflow / approval / workbench / timeline 闭环 | P0 |
| Phase E | M5.3 | **当前主线** | 建立 safe rollout executor registry、stage handler、自动推进语义 | **P0** |
| Phase F | M5.4+ / 生产收口 | 未完成 | 面向低干预生产运行的验证、门禁、回滚、运营化 | P0 |

---

## 4. 各阶段主线任务

## Phase A｜统一骨架与观测面（M0-M1，已完成）

### 目标
先统一 adaptive strategy 的数据结构、配置结构、透传链路和观察面，避免后续每层各说各话。

### 已完成要点
- regime taxonomy / snapshot 基本统一
- policy snapshot / effective config 结构建立
- adaptive 配置骨架建立
- observe-only 贯穿 signal / decision / validation / execution / backtest
- dashboard / report / summary 已能读到不少 adaptive 信息

### 完成标志
- 新增 adaptive 字段时，主链路已有固定挂载点
- 不需要再靠临时字段在多个模块重复拼装含义

### 风险点
- schema 演化过快导致旧报告兼容性下降
- 观测字段太散，后续检索成本上升

### 依赖关系
- 无；这是全主线地基

### 推荐步骤
1. 保持 schema 变更优先向后兼容
2. 新增字段尽量先进 snapshot / effective / summary 固定结构
3. 少走临时旁路输出

---

## Phase B｜Decision-aware adaptive（M2，已完成）

### 目标
让 adaptive 首次真实影响开单决策，但仍坚持“只收紧，不放宽”。

### 已完成要点
- regime / policy 已开始影响 EntryDecider
- allow/watch/block 与 breakdown 更可解释
- 决策层已形成保守收紧语义

### 完成标志
- adaptive 对 decision 的影响可观测、可比较、可关闭
- baseline 与 decision-only 语义区分清楚

### 风险点
- 解释性足够但 calibration 仍不足时，容易“看似合理、实则过严/过松”
- 决策层与后续 validation / risk 生效边界若不一致，会造成理解偏差

### 依赖关系
- 依赖 Phase A 的 snapshot / policy 统一口径

### 推荐步骤
1. 保持 decision 层不做激进放宽
2. 所有策略化影响优先沉淀到 breakdown / effective snapshot
3. 对照回测/历史样本持续校正 threshold tightening 的质量

---

## Phase C｜Validation / Risk / Execution guarded live（M3-M4，已完成）

### 目标
让 adaptive 从“看得见”走到“有限度真实生效”，但仍受 guardrail、开关、rollout 边界约束。

### 已完成要点
- validator conservative enforcement 已落地
- risk hints / effective snapshot / conservative enforcement 已落地
- execution guardrails 已可真实生效
- layering guardrails 已落地
- `layer_ratios` / plan shape 已在严格 gated 条件下 guarded live

### 完成标志
- adaptive 已真实影响 validation / risk / execution
- 影响范围仍限定为保守收紧、严格门控、可回滚
- 未破坏既有 intent / reconcile / state machine 主语义

### 风险点
- execution 层轻微收紧都可能放大实盘体感影响
- layering 形状变更如果缺少明确 audit，很难快速定位问题

### 依赖关系
- 依赖 Phase B 已有 decision 语义与 Phase A observability 基座

### 推荐步骤
1. 所有 execution 生效项保持“灰度 + 审计 + 明确回滚位”
2. 继续避免过早碰 exit/profile 大改
3. 将 live 字段与 hints 字段持续分清楚

---

## Phase D｜Governance / workflow / approval 闭环（M5.1-M5.2，大部分已完成）

### 目标
把 adaptive system 从“会调参数/会给建议”推进到“**会形成可消费、可追踪、可审计的治理闭环**”。

### 已完成要点
- calibration report / policy A-B diff / rollout gate 已具备
- governance_ready / action_playbook / approval_ready / workflow_ready 已具备
- approval persistence / replay 已具备
- workbench governance view / attention view / operator digest 已具备
- merged timeline / transition journal / timeline summary 等审计消费层已具备
- controlled rollout state-apply / executor skeleton 已有 very-safe 基础能力

### 完成标志
- dashboard / agent / 人工巡检已有比较稳定的低干预消费入口
- item 级状态不再只靠即时计算，而有持久化与回放能力
- “为什么卡住、下一步做什么、最近发生了什么”基本能从系统直接读到

### 风险点
- 入口变多，若没有统一第一入口，后续检索成本仍会上升
- 视图丰富但执行层不够强，会出现“看得懂但推不动”

### 依赖关系
- 依赖 Phase C 已有真实生效层，否则治理只是空中楼阁

### 推荐步骤
1. 把本文档作为统一总纲入口
2. 后续新增治理视图时优先挂回主线文档，而不是继续平铺新入口
3. 持续明确：哪些是 summary、哪些是 detail、哪些是 executable

---

## Phase E｜Safe rollout automation 主线（M5.3，当前主线 / 下一步重点）

### 目标
把现有 workflow / approval / executor skeleton，推进成一套 **可低风险自动推进的 safe rollout control plane**。

### 当前判断
这是**最值得继续深挖**的一段，也是离“低干预生产级”最近但最容易踩边界的一段。

### 下一步推荐步骤（按优先级）

#### E1. 建立 safe rollout action executor registry
**优先级：P0**

目标：把“哪些 action 可执行、谁来执行、在什么模式执行、成功/失败/回滚如何表达”彻底标准化。

建议输出：
- 统一 action registry / handler registry
- 明确 action → handler → dispatch mode → audit fields 映射
- 区分 `plan-only / dry-run / controlled-apply / forbidden`
- 每类 action 固定 `preconditions / blockers / rollback_hint / idempotency rule`

- **2026-03-28 implementation update (validation-gate consumer/persistence)**：validation gate 进一步接通 persisted approval/workflow state、dashboard summary card、unified workbench overview 同 `/api/approvals/state-machine`；freeze / regression / rollback trigger 而家会随 replay/state merge 稳定回灌，调用方可以直接睇到 gate 当前 ready/frozen 状态、最近缺口/失败 capability、主导 freeze reason，以及 `validation_gate_regressed` 等 rollback 触发线索，同时继续维持 metadata/review-only 安全边界，不触发危险真实交易执行。
- **2026-03-28 implementation update (control-plane contract drift guard)**：executor / queue consumer 现会把 `control_plane_contract` 快照同 approval details 一齐持久化，固定记录 action/handler/dispatch/transition contract 与 manifest generation/version；`build_control_plane_readiness_summary(...)` 会同步扫描 persisted contract，同当前 manifest 做 version / registry drift 检查。即使未来 registry / handler 升级，只要旧快照同现行 control plane 脱节，系统都会先转入 review-required/blocking，而唔会继续静默 auto-promotion。
- **2026-03-28 implementation update (contract drift low-intervention consumption)**：persisted drift 现进一步收口成稳定 `m5_control_plane_contract_drift_summary_v1`，并正式挂到 `workflow_operator_digest / workbench_governance_view / workflow_alert_digest / unified_workbench_overview`。低干预调用方而家可以直接见到 `frozen_item_count / dominant_drift_type / requires_manual_review`，以及 item 级 `frozen_by_contract_drift / drift_types / issues`，唔使再手动钻 readiness 明细先知边个因 contract drift 被 freeze。
完成标志：
- 新动作接入不再靠散落 if/else
- workbench / digest / timeline 能稳定引用同一 registry 元数据
- safe action 与敏感 action 的边界可程序化判断

主要风险：
- 动作分类不稳会导致后续 handler 泛滥
- registry 若只列名字不列约束，等于没有真正降低风险

依赖：
- 依赖当前 approval persistence、executor skeleton、workbench item catalog

#### E2. 补 richer stage handlers / transition handlers
**优先级：P0**

目标：把现有 `stage_model / queue_progression / scheduled_review` 从“描述语义”推进成“可驱动状态迁移的 handler 语义”。

建议输出：
- `observe -> candidate -> guarded_prepare -> controlled_apply -> review_pending -> rollback_prepare` 等阶段 handler
- 每个阶段明确 `enter / stay / exit / rollback / review_due` 条件
- queue promotion 与 scheduled review 有独立 handler，而不是揉成单一 apply

- **2026-03-28 implementation update (validation-gate consumer/persistence)**：validation gate 进一步接通 persisted approval/workflow state、dashboard summary card、unified workbench overview 同 `/api/approvals/state-machine`；freeze / regression / rollback trigger 而家会随 replay/state merge 稳定回灌，调用方可以直接睇到 gate 当前 ready/frozen 状态、最近缺口/失败 capability、主导 freeze reason，以及 `validation_gate_regressed` 等 rollback 触发线索，同时继续维持 metadata/review-only 安全边界，不触发危险真实交易执行。
完成标志：
- 系统能回答“当前为何停在此阶段、谁能推动、自动推进条件是否满足”
- 同一 item 的 next step 可由 stage handler 稳定推导

主要风险：
- 阶段语义与 approval 终态冲突
- transition 太复杂会让调试成本暴涨

依赖：
- 依赖 E1 registry 标准化；否则 handler 仍会散落

#### E3. 明确 auto-advance gate 与 rollback gate
**优先级：P0**

目标：将“何时允许系统自动前进一步、何时必须停下、何时回滚”写成稳定门禁。

建议输出：
- readiness score / blockers / risk level / manual-required / cooldown / review window 等统一 gate
- auto-advance 只能命中 very-safe + low-risk + no blocker + approval-eligible 项
- rollback gate 明确触发因子：异常 transition、review 失败、样本不足、风险升高

- **2026-03-28 implementation update (validation-gate consumer/persistence)**：validation gate 进一步接通 persisted approval/workflow state、dashboard summary card、unified workbench overview 同 `/api/approvals/state-machine`；freeze / regression / rollback trigger 而家会随 replay/state merge 稳定回灌，调用方可以直接睇到 gate 当前 ready/frozen 状态、最近缺口/失败 capability、主导 freeze reason，以及 `validation_gate_regressed` 等 rollback 触发线索，同时继续维持 metadata/review-only 安全边界，不触发危险真实交易执行。
完成标志：
- 自动推进不是靠零散 heuristics，而是统一 gate 决定
- agent / dashboard / operator 对“为什么自动推/为什么没推”有统一解释

主要风险：
- gate 太松会越界，太紧则自动化名存实亡

依赖：
- 依赖 E1/E2 以及现有 transition journal / timeline audit

#### E4. validation entry / shadow / replay 验证链路常态化
**优先级：P1**

目标：解除“过度依赖自然开单验证”的瓶颈，让 rollout handler / stage handler 能在更低风险环境持续验收。

建议输出：
- 历史回放 / 影子单 / controlled testnet 场景下的 handler 验证脚本或 playbook
- action/stage 级 acceptance checklist
- 每次新增 safe action 都有最小验证入口
- **2026-03-28 implementation update**：validation replay summary 现已补 `coverage_matrix + readiness`，会稳定回答 approval replay / rollout executor dry-run / transition policy contract / testnet bridge（plan-only、controlled execute、blocked guard、cleanup recovery）是否都有 fixture 覆盖，及是否达到 `low_intervention_gate_ready`。后续新增 safe action / stage handler 时，可以直接用同一 summary 检查“主线验证矩阵有无缺口”，减少只看单 case pass 的错觉。
- **2026-03-28 implementation update (validation-gated rollout)**：validation replay readiness 已正式接入 rollout executor gate：当 `low_intervention_gate_ready=false` 时，very-safe apply 路径会统一冻结 `auto_advance_gate`，并在已进入 active/review path 的 stage item 上打开 `validation_gate_regressed` rollback trigger；同时 consumer/operator digest 会直出 `validation_gate` 摘要，令系统不止知道“动作本身安不安全”，仲知道“整条低干预验证矩阵是否仲够资格继续自动推进”。
- **2026-03-28 implementation update (validation-gated routing/policy)**：validation gate 现已进一步接通 `operator_action_policy / lane_routing / stage_loop / workbench/operator digest`。如果系 gate gap/freeze，会明确改派成 `review_schedule + validation_review_queue + review_validation_freeze`，并把 ready/queued item 压回 blocked/review lane；如果系 gate regression（failing required capability / failing cases），则会明确改派成 `freeze_followup + rollback_candidate_queue + rollback_candidate_review`，并把 stage/workbench 路径推去 `rollback_prepare / rollback_candidate`。manual approval lane 仍保留优先级，继续维持 metadata-only / queue-only 安全边界。

- **2026-03-28 implementation update (validation-gate consumer/persistence)**：validation gate 进一步接通 persisted approval/workflow state、dashboard summary card、unified workbench overview 同 `/api/approvals/state-machine`；freeze / regression / rollback trigger 而家会随 replay/state merge 稳定回灌，调用方可以直接睇到 gate 当前 ready/frozen 状态、最近缺口/失败 capability、主导 freeze reason，以及 `validation_gate_regressed` 等 rollback 触发线索，同时继续维持 metadata/review-only 安全边界，不触发危险真实交易执行。
完成标志：
- 新 handler/新 action 可以在非自然开单条件下被验证

主要风险：
- 验证入口口径与实盘口径分裂

依赖：
- 可并行推进，但要复用现有 workflow / executor 序列化结构

---

## Phase F｜生产级低干预运行收口（M5.4+，未完成）

### 目标
把 adaptive system 从“可治理、可安全推进”继续收口到“**可长期稳定低干预运行**”。

### 推荐步骤（按优先级）

#### F1. 生产门禁与运行策略固定化
- 固定 rollout policy versioning、promotion gate、freeze/rollback playbook
- 建立 operator-facing runbook
- **2026-03-28 implementation update (production rollout readiness gate)**：新增统一 `production_rollout_readiness` 入口，把 `unified_workbench_overview + workflow_alert_digest + runtime_orchestration_summary + control_plane_readiness + validation_gate + review queues` 固定成一层生产前/低干预前巡检门禁。调用方而家可以直接判断 `production_ready / can_enable_low_intervention_runtime / blocking_issues / runbook_actions`，唔使再自己横向拼 approval backlog、critical alerts、contract drift、validation freeze 同 rollback review backlog，向真正 operator-facing runbook 再收口一步。

#### F2. 观测与告警分层
- 把 operator digest / workbench / transition journal 的优先级与告警级别做分层
- 减少噪音，突出必须人工介入项
- **2026-03-28 implementation update (workflow alert digest)**：新增 `m5_workflow_alert_digest_v1`，把 validation gate、control-plane readiness、manual approval、blocked follow-up、rollback candidate、recent transition activity 收口成统一 severity-based alert contract（`critical/high/medium/info`）；并接入 `/api/backtest/workflow-alert-digest`、`calibration-report?view=workflow_alert_digest`、dashboard summary cards、unified workbench overview。目标系令系统直接分得清“边啲要即刻人工介入、边啲只需继续低干预观察”，为生产级低干预运行收口。

#### F3. 实盘前验证矩阵
- baseline / decision_only / guarded_execute / controlled rollout 各模式建立固定验收矩阵
- 把 testnet / shadow / replay / sample checkpoint 做成标准流程

#### F4. 策略/治理版本生命周期
- policy version、action registry version、stage handler version、schema version 联动管理
- 明确兼容窗口与回滚窗口

### 完成标志
- 系统已经不只是“能跑”，而是“能稳定审计、稳定升级、稳定回滚”
- 人工介入主要集中在异常、升级、策略审查，而不是日常机械审批

### 风险点
- 生产收口若只做自动化，不做 runbook / rollback / alert hygiene，会导致事故恢复能力不足

### 依赖关系
- 强依赖 Phase E 基本成形，否则生产化只会把半成品自动化

---

## 5. 已完成 / 下一步 / 仍欠缺什么

## 5.1 已经完成到哪

可以简化理解为：

- **已完成**：
  1. adaptive strategy 基础骨架
  2. decision / validation / risk / execution 的保守生效
  3. calibration / governance / workflow / approval / timeline 的主消费层
- **已具备但仍偏 early-stage**：
  1. controlled rollout state-apply
  2. rollout executor skeleton
  3. stage orchestration 语义
- **未完成**：
  1. 完整 safe action registry
  2. richer stage handlers
  3. 统一 auto-advance / rollback gate
  4. 生产级验证矩阵与 runbook 收口

## 5.2 下一步是什么

### 唯一建议的主线下一步

> **优先继续推进：`safe rollout action executor registry + richer stage handlers + auto-advance/rollback gates`。**

这是当前最合理的下一跳，因为：

1. 前面 M0-M5 已经把“看见、判断、治理、审计”的基础打好了；
2. 真正限制低干预化的，不再是缺 summary，而是缺“怎么安全推动下一步”；
3. 这条线一旦清晰，后续 dashboard、agent、operator、testnet 验证都会更顺。

## 5.3 距离生产级低干预自适应市场策略系统，还差哪几块

目前还差的核心块，可按 1-6 理解：

1. **可执行动作注册中心**
   - 现在已有 skeleton，但未形成长期稳定、可扩展、可审计的 registry
2. **阶段处理器（stage handlers）**
   - 现在有 stage 语义，但还不够 handler 化、规则化
3. **统一自动推进/回滚门禁**
   - 需要把 readiness / blocker / risk / review / rollback 变成稳定 gate
4. **常态化验证入口**
   - 需要摆脱长期依赖自然开单验证
5. **生产 runbook / alert hygiene / operator handoff**
   - 需要把低干预运行中的人工职责边界做清楚
6. **版本治理与兼容策略**
   - 需要把 policy / registry / handler / schema 的版本关系固定下来

---

## 6. 建议优先级总表

| 优先级 | 事项 | 原因 |
|---|---|---|
| P0 | safe rollout action executor registry | 这是后续自动推进的统一底座 |
| P0 | richer stage handlers | 没有 handler，stage 只停留在描述层 |
| P0 | auto-advance / rollback gates | 决定系统能否安全低干预推进 |
| P1 | validation entry / shadow / replay 常态化 | 减少依赖自然开单的验证阻塞 |
| P1 | production runbook / operator playbook | 为低干预运行收口 |
| P2 | 更激进的 adaptive execution override | 当前不宜优先，风险高于收益 |
| P2 | 放宽型 policy | 当前主线仍应坚持保守收紧 |
| P2 | exit/profile 大改 | 暂非主线关键路径 |

---

## 7. 后续维护这份文档的规则

后续建议统一按以下方式更新本文，而不是继续分裂入口：

1. **阶段推进了**：直接更新“当前阶段判断”“已经完成到哪”“下一步是什么”
2. **新增一个实现稿/专项计划**：只在本文追加链接，不必重写主线
3. **阶段变更明显**：在本文新增 `M5.4 / Phase F` 等小节即可
4. **旧文档仍保留**：让它们负责细节，不再承担“第一入口”职责

---

## 8. 给未来主会话 / 子会话的超短接手说明

如果你是后来接手的会话，请先记住这三句：

1. **项目主线不是换策略，而是把现有系统推进成低干预、自适应、可治理的市场策略系统。**
2. **当前已走完 M0-M5 前半段，正处于 M5.3：把 workflow / approval / executor 雏形推进成 safe rollout automation control plane。**
3. **下一步最值得做的是：safe action registry、stage handlers、auto-advance/rollback gates。**
