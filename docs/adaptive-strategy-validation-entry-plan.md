# Adaptive Strategy Validation Entry Plan

> 目标：**唔等真实自然开单，都可以持续验证 adaptive strategy / layering / rollout / governance 主线。**
>
> 这份文档不是泛泛而谈的“测试建议”，而是给主力开发者直接接手落地的**验证入口方案**：先解决“长期要等自然单才知有无问题”这个卡点，再继续推进 adaptive strategy。

---

## 1. 为什么现在要先做“验证入口”

当前 adaptive strategy 主线已经推进到：

- signal / decision / validator / risk 的 guarded 生效
- execution / layering 的 guarded live
- governance / workflow / approval / rollout executor skeleton

但项目推进仍被一个老问题卡住：

> 很多功能虽然代码已经到位，**真正验收却长期依赖“等市场自然出现一笔会开仓的机会”**。

这会导致几个后果：

1. **节奏被市场支配**：无单就无验证，开发主线被迫停摆。
2. **验证口径混乱**：有时只能看日志猜，有时只能等真实资金路径。
3. **问题定位太晚**：execution / layering / rollout 的 bug 往往要到最后一层才暴露。
4. **高风险功能迟迟不敢推进**：因为一推就要碰真钱 / 真交易入口。

所以现在优先级要明确切换为：

> **先建设验证入口，再继续加 adaptive 功能。**

核心原则只有一句：

> **先让系统“可以被稳定触发、稳定观测、稳定对比”，再谈真实市场下的长期表现。**

---

## 2. 本方案要解决什么问题

这份方案要解决的是以下验证缺口：

### 2.1 当前缺口

- adaptive signal / decision 调整是否真有按预期生效
- validator / risk conservative enforcement 是否只会收紧，不会误放宽
- execution / layering guarded live 是否真影响了 entry plan / layer plan
- workflow / governance / rollout executor skeleton 是否能走完状态流转
- approval / replay / audit / rollback 语义是否稳定

### 2.2 明确不解决的事

这份方案**不承诺**直接解决：

- 真正的长期 alpha 是否提升
- 不同市场阶段下长期收益曲线是否更优
- 真实交易所微观成交行为完全一致
- 所有网络、滑点、撮合、限频问题的最终真实性

换句话说，这份文档解决的是：

> **“功能能否被主动验证、被分层验收、被持续回归”**，
>
> 不是“策略最终一定赚钱”。

---

## 3. 验证设计原则

后续所有验证入口，都建议遵循以下原则：

### 3.1 分层验证，唔好一上来就验证全链真钱

先验证：

1. signal
2. decision
3. validator
4. risk
5. execution
6. layering
7. governance
8. workflow / rollout

任何一层未稳定，都唔好急住靠真实下单“顺便一起验”。

### 3.2 同一份输入，尽量支持多模式重放

同一条 signal / market snapshot / replay case，最好可以跑出：

- baseline
- observe-only
- guarded_execute
- workflow/governance dry-run
- execution shadow
- testnet controlled execute

咁样先有可比性。

### 3.3 验证入口要 fail-closed

任何验证入口都唔可以默认流去真实交易：

- 默认 dry-run / shadow
- 默认要显式开关
- 默认带 tag / audit / reason
- 默认可回放、可追踪、可跳过真钱

### 3.4 验证数据与真实链路尽量复用同一套结构

不要做一套“测试专用假逻辑”同生产逻辑完全分离。最好复用：

- signal payload schema
- policy snapshot
- validation snapshot
- risk snapshot
- execution / layering snapshot
- approval / workflow state schema

这样验证结果才对后续实现真正有指导价值。

---

## 4. 四类验证入口总览

本方案建议建立四条主验证入口，彼此互补，而唔系二选一。

| 验证方式 | 优先级 | 主要用途 | 能验证到什么 | 不能验证什么 | 风险 |
|---|---|---:|---|---|---|
| 影子单 / 仿真注入 | P0 | 日常开发、快速回归 | signal→decision→validator→risk→execution/layering/governance 语义 | 真实交易所成交、真实网络、真实账户状态耦合 | 若注入语义太假，会产生“假通过” |
| 历史回放 / 重放验证 | P0 | 批量验证、回归、A/B 对比 | 同一历史样本在不同 mode / policy 下的差异 | 实时并发、真实交易 API、实时状态竞争 | 历史数据不全会影响结论 |
| 受控 testnet 开单入口 | P1 | execution / order / reconcile / state 近实战验证 | 下单请求、订单状态、对账、自愈、部分状态流转 | 实盘深度、真实滑点、真实资金约束完全一致 | 若开关失控会过度下 testnet 单 |
| workflow / rollout 受控入口 | P1 | approval / governance / rollout executor 验证 | state transition、audit、queue、playbook、safe apply | 不验证真实策略收益，也不应直接改 live 参数 | 若边界不清可能把治理流误变成自动执行 |

建议理解方式：

- **影子单 / 仿真注入**：最快、最常用、最应该先落地
- **历史回放 / 重放**：最适合做 regression / compare / evidence
- **受控 testnet**：验证最接近真实 execution，但要控频控范围
- **workflow / rollout 入口**：专门解决 governance / approval / rollout 唔使等真实市场事件

---

## 5. 方案一：影子单 / 仿真注入（首选 P0）

这是最应该先做的验证入口。

### 5.1 定义

影子单 / 仿真注入，不是真发单，而是：

- 主动构造一条候选 signal / intent / execution case
- 走系统原本的 signal → decision → validator → risk → execution/layering/workflow 逻辑
- 但在真实下单前截停
- 记录完整快照、计划、原因、分支与结果

建议默认叫法：

- `shadow_signal`
- `shadow_intent`
- `simulation_injection`
- `validation_entry`

### 5.2 适用范围

最适合验证：

- adaptive signal strength / reasons / tags
- EntryDecider allow/watch/block
- validator conservative enforcement
- risk budget tightening
- execution effective profile
- layering plan shape / guardrails
- governance_ready / workflow_ready / approval_ready
- rollout executor dry-run / queue-only / safe metadata apply plan

### 5.3 最推荐的注入粒度

建议支持 3 个层级，不要只做一种：

#### A. signal 级注入
输入一条 synthetic signal context：

- symbol
- timeframe / timestamp
- side
- market snapshot
- feature summary
- regime snapshot（可选直接指定）
- signal reasons / scores（可半自动构造）

用途：

- 快速验证 decision / validator / risk 是否按预期
- 验证 adaptive overrides 是否只收紧

#### B. execution candidate 级注入
输入更完整的准执行上下文：

- approved signal / decision result
- validator summary
- risk context
- account exposure snapshot
- layering baseline config
- adaptive policy snapshot

用途：

- 直接验证 execution / layering 的 effective profile
- 适合做 `compute_entry_plan()` / plan shape / guardrail 检查

#### C. workflow / governance item 级注入
输入一个 governance bucket / action item：

- recommendation item
- action_playbook item
- approval candidate
- queue progression / stage model

用途：

- 不等真实 calibration / rollout 自然长出来，都可以验证 workflow / approval / state transition

### 5.4 能验证到什么

影子单 / 仿真注入能高质量验证：

- 各层 schema 是否稳定
- adaptive snapshot 是否贯通
- baseline / effective / enforced / ignored 是否解释清楚
- layering / plan shape 是否按 conservative-only 生效
- governance / workflow queue 语义是否合理
- replay / audit / summary 是否可消费

### 5.5 不能验证什么

它**不能**证明：

- OKX testnet / real 下单一定成功
- 订单回报状态一定如预期
- network / ccxt / exchange throttle 问题都解决
- 实际成交价、滑点、部分成交、真实仓位同步完全一致

### 5.6 主要风险

最大风险系：

> 注入数据太“干净”，导致系统在影子环境通过，但一去真实链路就出事。

所以注入时要刻意覆盖：

- 边界值
- 缺字段
- 冲突信号
- 高 transition risk
- low confidence
- rollout miss
- blocked_by 非空
- requires_manual=true
- direction lock / idempotency 冲突场景

### 5.7 落地建议

建议新增一个统一入口，例如：

```bash
python3 bot/run.py --validation-entry shadow-signal --input path/to/case.json
python3 bot/run.py --validation-entry shadow-execution --input path/to/case.json
python3 bot/run.py --validation-entry shadow-workflow --input path/to/case.json
```

最少输出：

- input summary
- baseline result
- adaptive result
- diff summary
- blocked reasons / ignored overrides
- execution/layering snapshot
- workflow/governance snapshot
- audit id / replay id

---

## 6. 方案二：受控 testnet 开单入口（P1，近实战）

这个入口不是用来替代影子验证，而是用来补足“接近真实执行”的缺口。

### 6.1 定义

显式提供一个**受控、最小、可审计**的 testnet execute 入口，不等自然信号，而系手动指定：

- symbol
- side
- validation case / preset
- 是否允许真的发 testnet order

### 6.2 适用范围

最适合验证：

- ccxt / exchange adapter 请求链路
- order create / cancel / fill / reconcile
- open trade / position / intent state 对账
- execution observability 是否与真实链路一致
- 自愈逻辑 / reset 逻辑 / layering state 是否能跟订单状态互动

### 6.3 推荐边界

受控 testnet 入口一定要限制成“最小交易验收包”：

- 单 symbol
- 单 side
- 固定最小 size / margin
- 固定 testnet mode
- 显式 `--execute`
- 默认 dry-run
- 默认只允许 smoke profile
- 默认限制每日次数 / 冷却时间

### 6.4 能验证到什么

- 下单 API 真的通
- 订单状态、trade/open_trade、position 对账基本通
- reconcile / self-heal / layer state reset 真实有反应
- execution logging / DB / dashboard 口径更接近真实

### 6.5 不能验证什么

- 实盘流动性
- 实盘滑点
- 真正资金心理压力下的人工运维决策
- 所有交易所实盘限制差异

### 6.6 风险

- 以为 testnet 通过就等于 real 通过
- 过度依赖 testnet，而忽略 schema / state / replay 层回归
- 若入口设计太宽，可能不断打 testnet 噪音单，影响排障

### 6.7 落地建议

建议把现有 smoke 能力扩成“有案例上下文”的 controlled validation execute，而不是另开一套完全独立脚本。

例如：

```bash
python3 bot/run.py --validation-entry testnet-execution \
  --case cases/layering-minimal-long.json \
  --execute
```

建议强制输出：

- validation case id
- testnet only assertion
- order ids
- intent / open_trade / position diff
- reconcile summary
- rollback / cleanup recommendation

---

## 7. 方案三：历史回放 / 重放验证（首选 P0）

这是最适合持续回归和做 A/B 对比的入口。

### 7.1 定义

从历史 market data / signal records / decision records / trade contexts 中，抽出可重放样本，然后在当前代码上重跑：

- baseline
- observe-only
- guarded_execute
- execution shadow
- workflow/governance dry-run

### 7.2 适用范围

适合验证：

- decision 阈值变化前后差异
- validator / risk tighten 是否符合预期
- execution profile / layering profile 在历史 case 上是否会变
- rollout / governance item 的分布、blocked reason、queue semantics
- 版本升级后有无回归

### 7.3 推荐样本篮子

不要只回放“好看的案例”，建议固定至少 5 类：

1. **应允许的强信号样本**
2. **应阻断的弱/冲突样本**
3. **高 transition risk 边界样本**
4. **layering / repeated add / direction lock 敏感样本**
5. **governance / workflow / approval 边界样本**

最好再加：

6. 历史曾出过 bug 的回归样本
7. rollout miss / symbol override miss 样本
8. partial data / malformed snapshot 样本

### 7.4 能验证到什么

- 同一历史 case 在不同版本 / mode 下差异清晰可比
- 可以形成 regression suite
- 可以直接支持主力开发者改完代码后回归
- 可以为“是否推进下一阶段”提供证据，而唔系靠感觉

### 7.5 不能验证什么

- 实时多线程 / 定时器 / 并发竞争
- 真实 API / 订单状态时序
- 实时账户状态变化带来的非确定性

### 7.6 风险

- 历史样本不完整，回放只剩半条链
- 样本偏差太大，只覆盖作者熟悉场景
- 回放结果被误用成真实收益证明

### 7.7 落地建议

建议在项目内建立：

```text
tests/fixtures/validation/
  signal/
  execution/
  workflow/
  regression/
```

再加一个批量入口，例如：

```bash
python3 bot/run.py --validation-replay tests/fixtures/validation/regression
```

每个 case 输出：

- expected assertions
- actual result
- diff summary
- pass / fail
- report json

---

## 8. 方案四：workflow / rollout 受控验证入口（P1）

用户而家要推进的已经唔止系 signal / entry，本身仲包括：

- governance
- approval persistence
- workflow state
- rollout stage orchestration
- rollout executor skeleton

这些东西如果继续等“真实策略自然生成 recommendation，再自然演化到 approval / rollout”，会继续卡死。

所以必须有独立入口。

### 8.1 定义

直接注入或重放一条 workflow/governance item，验证：

- action_playbook
- approval queue
- auto approval policy
- controlled safe apply
- queue progression
- stage transition
- replay / stale / audit / timeline

### 8.2 适用范围

特别适合验证：

- `pending -> approved / deferred / expired`
- `workflow_state -> ready / blocked / review_pending`
- queue route / dispatch route
- safe metadata apply
- audit event / immutable log
- timeline rebuild / snapshot recovery

### 8.3 能验证到什么

- governance / workflow 主语义是否闭环
- rollout executor skeleton 的 dispatch-plan-apply-result 是否稳定
- approval / replay / stale cleanup 是否可靠
- 未来自动 approval / 自动 rollout 的前置地基是否真可用

### 8.4 不能验证什么

- 某个 governance recommendation 最终是否提高收益
- 真正 live 参数 apply 的市场后果
- 实盘自动 rollout 的全部风险

### 8.5 风险

- 若入口无清晰隔离，可能误把 workflow controlled apply 做成 live parameter change
- 若 audit 字段不稳定，后续 replay / dashboard / agent 消费会一直返工

### 8.6 落地建议

建议提供：

```bash
python3 bot/run.py --validation-entry workflow-dry-run --input path/to/workflow-item.json
python3 bot/run.py --validation-entry workflow-safe-apply --input path/to/workflow-item.json
```

并强制要求：

- 默认 `dry_run`
- 只有 allowlist action type 可 safe apply
- 明确输出 `real_trade_execution=false`
- 明确输出 `dangerous_live_parameter_change=false`

---

## 9. 验证分层：每一层要点样验

以下是建议主力开发按层做的验证矩阵。

| 层 | 主要验证入口 | 必验点 | 最佳方式 |
|---|---|---|---|
| signal | shadow / replay | schema、reasons、strength、regime tags | shadow signal + replay |
| decision | shadow / replay | allow/watch/block、threshold diff、notes/tags | replay A/B |
| validator | shadow / replay | conservative-only、hard block reason、effective snapshot | replay + assertions |
| risk | shadow / replay | exposure cap、entry sizing tighten、ignored/applied | shadow execution |
| execution | shadow + testnet | plan、intent、order request envelope、observability | shadow first, testnet second |
| layering | shadow + testnet + replay | layer ratios、plan shape、guardrails、reset | shadow/replay first |
| governance | workflow injection + replay | recommendations、priority queue、blocking | workflow dry-run |
| workflow | workflow injection | approval state、timeline、stale、dispatch route | workflow dry-run/safe-apply |

### 9.1 signal 层
重点：

- input schema 稳唔稳定
- regime / policy tags 有没有贯通
- signal reasons / strength / hypothetical adjusted value 对唔对

### 9.2 decision 层
重点：

- allow/watch/block 是否符合预期
- adaptive overrides 是否只会收紧
- ignored / applied / triggered / notes 是否可解释

### 9.3 validator 层
重点：

- hard block 是否统一 reason code
- conservative-only merge 有无失守
- rollout miss 是否回 baseline

### 9.4 risk 层
重点：

- effective budget 是否较 baseline 更保守
- sizing 是否被正确收紧
- observe-only / hints-only / enforced 是否区分清楚

### 9.5 execution 层
重点：

- entry plan 输入 / 输出是否稳定
- effective execution profile 是否有 audit
- shadow 与 testnet 的 payload 口径是否一致

### 9.6 layering 层
重点：

- `layer_ratios` 与 `layer_count` 语义是否没乱
- plan shape gate 是否独立
- direction lock / idempotency / reset 是否没被破坏

### 9.7 governance 层
重点：

- recommendation / joint governance 是否结构稳定
- blocking precedence 是否合理
- next_actions / queue 是否可消费

### 9.8 workflow 层
重点：

- approval persistence 与 immutable event 是否一致
- state transition 是否 fail-closed
- safe apply 是否严格 no-op to trading

---

## 10. 各验证方式的优先级与建议使用姿势

### P0：立即建设

#### 1) 影子单 / 仿真注入
因为它：

- 建设成本低于 testnet 自动验收
- 对 adaptive / layering / workflow 都适用
- 最适合日常开发回归

#### 2) 历史回放 / 重放验证
因为它：

- 最适合做版本对比
- 最适合固定 regression basket
- 最适合证明“改了之后有无变好 / 变坏”

### P1：紧接着建设

#### 3) workflow / rollout 受控入口
因为当前项目已经深入到 governance / approval / rollout executor skeleton，如果冇独立入口，这条线会继续靠自然事件拖慢。

#### 4) 受控 testnet 开单入口
因为 execution / reconcile / self-heal 最后仍要靠近真实链路验一次，但唔应该把它作为第一验证入口。

### P2：后续增强

- UI 可视化验证面板
- 案例管理器
- 自动 nightly replay regression
- 多 symbol / 多 regime validation coverage report

---

## 11. 最推荐的落地顺序

建议唔好并发乱开工，而系按以下顺序落地：

### Phase A：先做最小验证骨架（最高优先级）

目标：

- 可以主动给系统喂 case
- 可以输出 baseline vs adaptive diff
- 可以保存 report

最少要有：

1. `validation case schema`
2. `shadow signal / shadow execution / workflow dry-run` 三种入口
3. `report json` 输出
4. `tests/fixtures/validation/` 固定样本目录

### Phase B：补历史回放 / regression basket

目标：

- 不靠临场手工造 case
- 改完代码就能跑回归

最少要有：

1. 10~20 条高价值样本
2. baseline / adaptive diff 报告
3. pass/fail assertions
4. regression summary

### Phase C：补 workflow safe-apply 与 testnet controlled execute

目标：

- workflow 可以验证真实 state transition
- execution 可以做近真实交易所链路验收

最少要有：

1. workflow allowlist safe-apply
2. testnet execute smoke with case id
3. reconcile / cleanup summary
4. audit trail

### Phase D：再做自动化回归与报告消费

目标：

- 每次关键改动都有稳定验证证据
- dashboard / docs / PR summary 可直接贴结果

---

## 12. 最小实现包（建议主力开发者直接先做这个）

如果只拣一个最值得先做的最小包，我建议是：

## **MVP：Shadow Validation Entry Pack**

包含 4 样东西就够先开工：

### 12.1 一个统一 case schema

建议文件化 JSON/YAML：

```yaml
case_id: layering-guardrail-long-001
case_type: shadow_execution
symbol: BTC/USDT
side: long
mode: guarded_execute
input:
  market_snapshot: {}
  regime_snapshot: {}
  signal_snapshot: {}
  decision_snapshot: {}
  account_snapshot: {}
expect:
  decision: allow
  validator_pass: true
  risk_would_tighten: true
  execution_profile_really_enforced: true
  layer_ratios_live: false
```

### 12.2 一个 CLI 入口

例如：

```bash
python3 bot/run.py --validation-entry run --case tests/fixtures/validation/execution/layering-guardrail-long-001.yaml
```

### 12.3 一个统一 report 输出

至少返回：

- case metadata
- baseline summary
- adaptive summary
- diff summary
- assertions
- pass/fail
- audit refs

### 12.4 一组固定样本

建议先写 8~12 条，覆盖：

1. strong allow case
2. weak block case
3. transition risk block case
4. validator hard block case
5. risk tighten case
6. layering guardrail live case
7. plan-shape rollout miss case
8. workflow approval defer case
9. workflow safe-apply eligible case
10. malformed / missing-field fallback case

---

## 13. 推荐的数据结构与输出约束

为了避免后面再返工，建议验证入口输出统一遵守以下口径：

### 13.1 输出统一 envelope

```json
{
  "case_id": "...",
  "case_type": "shadow_execution",
  "mode": "guarded_execute",
  "status": "pass",
  "baseline": {},
  "adaptive": {},
  "diff": {},
  "assertions": [],
  "artifacts": {},
  "audit": {}
}
```

### 13.2 artifacts 建议包含

- rendered policy snapshot
- effective validation snapshot
- effective risk snapshot
- effective execution/layering snapshot
- governance/workflow snapshot
- decision notes / validator reasons / blocked_by

### 13.3 audit 建议包含

- generated_at
- code_version / git sha
- policy_version
- replay_source / injection_source
- real_trade_execution=false/true
- exchange_mode=shadow/testnet

---

## 14. 实施风险与防呆要求

### 14.1 最大风险：测试入口偷偷变成真实入口

必须防呆：

- shadow 入口永不触发真实下单
- workflow safe-apply 永不触发 live parameter change
- testnet 入口强制校验 `exchange.mode=testnet`
- 所有 validation run 强制打 `validation_case_id`

### 14.2 第二大风险：验证结果不可比

必须防呆：

- baseline 与 adaptive 同 case 同输入对比
- 输出结构稳定
- assertion 结果机器可读

### 14.3 第三大风险：样本只覆盖顺风场景

必须防呆：

- regression basket 必须包含坏样本、边界样本、缺损样本
- 每修一个 bug，补一个回归 case

---

## 15. 建议的 backlog 拆分

建议把验证入口单独视为一条近期优先主线，而不是散落到别的 milestone 备注里。

### VEP-01｜validation case schema + case loader
- 定义 case schema
- 支持 YAML/JSON
- 支持 signal / execution / workflow 三类 case

### VEP-02｜shadow signal / execution runner
- 输入 case
- 跑 baseline + adaptive
- 输出 diff report

### VEP-03｜workflow dry-run / safe-apply runner
- 注入 governance/workflow item
- 跑 approval / state / queue / audit
- safe-apply 仅 allowlist

### VEP-04｜history replay / regression basket
- 固定 fixtures
- 支持批量跑
- 支持 pass/fail summary

### VEP-05｜controlled testnet execute bridge
- 在 case 基础上接 testnet smoke execute
- 输出 reconcile / cleanup / order trail

---

## 16. 结论：最推荐先做什么

如果而家只做一件事，最值得先做的是：

> **先落地 Shadow Validation Entry Pack：case schema + shadow runner + workflow dry-run + regression fixtures。**

原因很简单：

1. 它最直接解决“唔等自然开单”的卡点。
2. 它覆盖面最大：signal / decision / validator / risk / execution / layering / governance / workflow 都食到。
3. 它风险最低：默认不碰真钱，不碰真实订单。
4. 它对后续 testnet / rollout automation 都是地基，不会浪费。

受控 testnet 开单入口应作为第二步，用来补 execution / reconcile 的近实战验证；
workflow safe-apply 则应该同步建设，专门服务 governance / approval / rollout 主线。

---

## 17. 推荐的一句话执行顺序

> **先做 shadow + replay，后做 workflow safe-apply，再做 controlled testnet。**
>
> **先验证语义与状态，再验证订单与交易所。**

---

## 18. 后续接手建议

主力开发者接手时，建议直接按以下顺序开工：

1. 新增 `tests/fixtures/validation/` 与统一 case schema
2. 在 `bot/run.py` 增加 `--validation-entry` / `--validation-replay` 入口
3. 先接 `shadow_execution` 与 `workflow_dry_run`
4. 再补 `shadow_signal` 与 regression basket
5. 最后才把 case-based testnet execute bridge 接上

做到第 3 步，其实已经可以明显解除当前“等自然单”的停滞问题。
