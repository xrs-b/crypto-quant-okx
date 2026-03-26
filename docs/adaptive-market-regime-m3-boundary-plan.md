# Adaptive Market Regime M3 边界方案（Validation / Risk Guardrails）

> 配套主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
>
> 配套 backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)
>
> Step 1 实施稿：[`docs/adaptive-market-regime-m3-step1-implementation.md`](./adaptive-market-regime-m3-step1-implementation.md)
>
> 目的：在真正进入 M3 开发前，先把 adaptive regime 在 **validator / risk** 层的实施边界、风险、回滚策略、灰度方式与推荐推进顺序讲清楚，避免一上来把现有执行主链路搅乱。

---

## 1. 一句话结论

M3 可以做，但只能做成 **“保守型 guardrails”**：

- **先碰 validator，再碰 risk budget**；
- **先做 observe-only / hint，再做 conservative enforcement**；
- **只能收紧，不准放宽**；
- **绝对不要在 M3 提前改 execution 骨架**，包括 layering 编排、direction lock 语义、intent 生命周期、reconcile / self-heal 逻辑、partial TP / trailing 参数。

如果要用一句最短的话定义 M3：

> M3 不是“让 adaptive regime 更聪明地下单”，而是“让系统在不确定、高波动、异常状态下更少犯错”。

---

## 2. 为什么 M3 敏感

M0/M1 主要是 schema、observe-only、snapshot；M2 主要影响 `EntryDecider` 的 allow/watch/block；但 **M3 开始碰 validation / risk**，已经不是“建议层”，而是会影响真实开仓概率与真实仓位预算。

之所以敏感，原因有 5 个：

### 2.1 M3 已经开始改“真实生效边界”

一旦 validator 阈值或 risk budget 真生效，影响的不是展示，不是评分，而是：

- 某些信号直接过不了 `signals/validator.py`
- 某些单即使 decision allow，也会在预算阶段被卡掉
- 某些 layering 计划层会因为剩余预算不足而永远打不出

也就是说，M3 会改变实盘入场密度、开仓尺寸、同币种占用、整体暴露。

### 2.2 项目当前执行链已经较复杂，M3 若越界容易打穿主链路

当前项目已有并在线承载的骨架包括：

- `trading.layering` 三层计划仓（默认 `[0.06, 0.06, 0.04]` / 上限 `0.16`）
- `direction lock`
- `open intents`
- `reconcile_trade_close` / stale close / 自愈
- `risk_budget` 总暴露、单币种暴露、单笔入场预算

这些东西本来就彼此耦合：

- risk budget 会影响能否按 layer plan 打出计划层；
- intents / locks 会影响执行中的“在途暴露”；
- reconcile / self-heal 要求执行状态机保持可解释；
- layering 默认是当前最重要的实盘验收对象之一。

所以 M3 最大风险不是“阈值调得不够漂亮”，而是 **把 validator / risk 改得像半个 executor**，最终出现“到底是 regime 卡住、预算卡住、intent 卡住、还是 layer plan 没打出来”都说不清。

### 2.3 当前系统最近 48h 的主要现状，本身就偏保守

根据项目近两日记录：

- 最近并非无信号，而是大量候选信号被拦截；
- 常见原因是 `波动率过低`、`EntryDecision=block` 等；
- executed 近 48h 为 0；
- 当前重点仍是等待真实信号，对现有 layering / intents / locks / layer plan / reset 做实战验收。

这意味着 M3 不能做成“再堆一层激进新逻辑”。

如果 M3 一上来过于重手，最容易出现：

- 本来就少的有效开仓进一步被清空；
- 大家误以为 adaptive regime 没价值，实际上只是“多叠一层 filter”；
- layering 的真实验收窗口被继续推迟，因为根本开不出来。

### 2.4 M3 若放宽风险，会直接与既有风控承诺冲突

当前 framework / backlog 已经反复定调：M3 是 **Guarded Validation & Risk**，只允许 **more conservative**。

原因很现实：

- M2 之前没有足够证据证明 regime 已稳定可靠到可以放大仓位；
- detector / policy 当前仍处于 `regime_v1_m0`、`adaptive_policy_v1_m1` 这一代；
- 当前 observe-only 与 decision-only 更多是建立一致口径，不是证明“某 regime 下可以更激进”。

所以 M3 如果做放宽，例如：

- 提高 `base_entry_margin_ratio`
- 提高 `total_margin_cap_ratio`
- 提高 `symbol_margin_cap_ratio`
- 放松 `min_strength` / `min_strategy_count`

本质上就已经越过 M3 边界，跑去做 M4/M5 甚至“未经证实的 alpha 放大”。

### 2.5 M3 是最容易制造“隐性回归”的阶段

observe-only 出错通常只是字段错；M2 出错通常是 allow/watch/block 变化；
但 M3 出错，会出现这种难排查问题：

- decision allow，但 validate 失败；
- validate 通过，但 entry ratio 被压到低于 min；
- 预算看似未超，但考虑 pending intents 后实际被卡；
- layer1 可以开，layer2/3 永远开不出来；
- 不同 symbol 的 rollout 模式不一致，导致线上行为碎片化。

所以 M3 一定要先写边界文档，再开发。

---

## 3. 当前项目语境下，M3 的职责是什么

结合当前代码与工程状态，M3 应只承担两件事：

### 3.1 让 validator 在危险 regime 下更严格

也就是在 `signals/validator.py` 现有骨架内，针对：

- `min_strength`
- `min_strategy_count`
- `block_counter_trend`
- `block_high_volatility`
- `block_low_volatility`
- `risk_anomaly` / `transition_risk` / `stability_score` 相关 gate

做 **effective threshold tightening**。

### 3.2 让 risk budget 在危险 regime 下更保守

也就是在 `core/risk_budget.py` 现有预算模型内，针对：

- `total_margin_cap_ratio`
- `total_margin_soft_cap_ratio`
- `symbol_margin_cap_ratio`
- `base_entry_margin_ratio`
- `max_entry_margin_ratio`
- `leverage_cap`（如项目已有统一入口可消费）

做 **conservative cap override**。

注意，M3 不负责：

- 重新定义建仓方向；
- 发明新的下单流程；
- 动态重排 layer plan；
- 修改 intent / lock / reconcile 的状态机。

---

## 4. M3 与 M2 / M4 的边界在哪里

| 阶段 | 可以做 | 不可以做 |
|---|---|---|
| M2 | 改 `EntryDecider` 的评分、阈值、allow/watch/block | 不改 validator 真阈值；不改 risk budget；不改 execution profile |
| **M3** | 改 validator 生效阈值；改 risk budget 的保守 caps；加入 hard block / soft tighten | **不改 execution 主干，不改 layering 编排，不改 partial TP / trailing，不改 intents / locks / reconcile 语义** |
| M4 | 才允许碰 execution profile，例如 `layer_ratios`、`layer_max_total_ratio`、执行参数 profile 化 | 不应绕过 M3/M2 的守门逻辑去直接“放大收益” |

### 4.1 M2 的边界

M2 本质是：

- 这单值不值得考虑开；
- allow/watch/block 如何因 regime 变保守；
- 仍然不改真实预算与执行参数。

所以 M2 哪怕输出 `allow`，M3 仍然有权因为 validator / risk 更保守而拦住。

### 4.2 M3 的边界

M3 本质是：

- allow 之后，再决定“是否值得进入执行层”；
- 即使值得，也只能给更保守的预算；
- 不重新定义 executor 如何打层、如何持久化状态。

### 4.3 M4 的边界

只有到 M4，才允许谈：

- `layer_ratios` 随 regime 动态改变；
- `layer_max_total_ratio` 随 regime profile；
- partial TP / trailing / execution profile 的轻量适配。

所以 **layering 参数 adaptation 不属于 M3**。这条边界必须写死。

---

## 5. 什么叫“只更保守”

M3 的核心纪律，就是 **only more conservative**。这里必须给出可落地定义，不然实现时很容易偷跑。

### 5.1 在 validator 层，“更保守”指什么

只允许以下方向：

- `min_strength` **提高**，不允许降低；
- `min_strategy_count` **提高**，不允许降低；
- `block_counter_trend` 从 `False -> True` 可以，`True -> False` 不行；
- `block_high_volatility` 从 `False -> True` 可以，反向不行；
- `block_low_volatility` 从 `False -> True` 可以，反向不行；
- `risk_anomaly` / 高 `transition_risk` 增加 hard block 可以，取消 hard block 不行；
- `allow` 降级成 `watch` / `block` 可以，反向不行。

### 5.2 在 risk 层，“更保守”指什么

只允许以下方向：

- `total_margin_cap_ratio` **降低**，不允许提高；
- `total_margin_soft_cap_ratio` **降低**，不允许提高；
- `symbol_margin_cap_ratio` **降低**，不允许提高；
- `base_entry_margin_ratio` **降低**，不允许提高；
- `max_entry_margin_ratio` **降低**，不允许提高；
- `leverage_cap` **降低**，不允许提高；
- 预算不足时直接 block 或 shrink size 可以，放大 size 不行。

### 5.3 在 execution 语义上，“更保守”不等于“随便改 layer plan”

这里最容易误解。

例如：

- 把 `[0.06, 0.06, 0.04]` 改成 `[0.04, 0.03, 0.02]` 看起来更保守；
- 但这已经是在改 execution profile / layering plan；
- 它会连带影响 intent 计划层、plan reset、后续加仓节奏、验收口径。

所以在本项目里：

> **“更保守”首先是 validator 阈值和 risk budget 的保守化，不包括在 M3 重写 layer plan。**

如果要收紧分层计划，请等到 M4，在 execution adaptation 阶段明确做。

---

## 6. M3 哪些层可以先碰

## 6.1 优先可碰：validator 的 effective thresholds

当前 `signals/validator.py` 已有天然承接点：

- `market_filters`
- `regime_filters`
- `strategies.composite.min_strategy_count`
- `strategies.composite.min_strength`
- 风险预算前置校验

因此第一批建议只做：

### 可先试的字段 / 阈值

1. `strategies.composite.min_strength`
   - 例：`high_vol` / `transition_risk_high` 下从 20 提高到 24 或 26
2. `strategies.composite.min_strategy_count`
   - 例：从 1 提到 2，避免单策略在高噪音状态下误触发
3. `market_filters.block_counter_trend`
   - 仅允许更严格地拦逆势单
4. `regime_filters.block_high_vol`
   - 从“只警告”升级为“可配置硬拦截”
5. `risk_anomaly` hard block
   - 统一 reason code，不让各处自己发明文本
6. `transition_risk` / `stability_score` gate
   - 只在高风险边界期加 conservative block / downgrade

### 推荐落地方式

- 先生成 `effective_validation_snapshot`
- 先在 `observe_only` / `hints` 模式记录 baseline vs effective
- 样本稳定后，再在 `guarded_execute` 对小范围 symbol 生效

## 6.2 次优先可碰：risk budget 的 conservative caps

当前 `core/risk_budget.py` 已有统一入口：

- `get_risk_budget_config()`
- `compute_entry_plan()`
- `summarize_margin_usage()`

因此第二批可做：

### 可先试的字段 / 阈值

1. `total_margin_cap_ratio`
   - 高波动 / risk_anomaly 下收紧总暴露
2. `total_margin_soft_cap_ratio`
   - 让系统更早进入保守 entry ratio
3. `symbol_margin_cap_ratio`
   - 避免高风险状态继续向单币种集中
4. `base_entry_margin_ratio`
   - 高风险时降低单笔目标保证金
5. `max_entry_margin_ratio`
   - 防止 quality scaling 或其它逻辑把单笔 entry 拉高
6. `leverage_cap`
   - 如 executor / exchange 入口支持消费，则只允许下压

### 推荐落地方式

- 第一阶段不要改 `compute_entry_plan` 核心公式；
- 只在进入公式前对 risk budget 配置做 conservative merge；
- `pending_intents`、当前持仓、symbol margin 汇总逻辑保持原样；
- 输出 `base_risk_budget` vs `effective_risk_budget` 对比字段。

---

## 7. M3 哪些绝对不要先碰

下面这些，**M3 明确列入禁区**。

## 7.1 不要先碰 execution / layering profile

包括但不限于：

- `trading.layering.layer_ratios`
- `trading.layering.layer_max_total_ratio`
- `min_add_interval_seconds`
- `max_layers_per_signal`
- `profit_only_add`
- `disallow_skip_layers`
- `allow_same_bar_multiple_adds`

原因：这些都属于 execution profile，不属于 M3 的 validator / risk 保守 guardrails。

## 7.2 不要先碰 direction lock 语义

不要在 M3 里：

- 改 `direction_lock_scope`
- 改锁释放规则
- 因 regime 去“特判”锁是否可跳过

原因：direction lock 是执行纪律，不是 regime policy playground。

## 7.3 不要先碰 intents 生命周期

不要在 M3 里：

- 改 `open_intents` 的 pending/submitted 语义
- 因 regime 改 intent 创建 / 删除时机
- 因 regime 新增 intent side effects

M3 只应该消费 pending intents 做预算保守，不应该改 intent 状态机本身。

## 7.4 不要先碰 reconcile / self-heal 逻辑

包括：

- `reconcile_trade_close`
- stale trade close
- stale lock cleanup
- orphan intent cleanup
- layer state 回写 / reset

这些是存量稳定性兜底，M3 不应掺进去。

## 7.5 不要先碰 partial TP / trailing / exit profile

这部分与 layering 已被确认存在潜在冲突；当前项目也明确：

- partial TP 先保留兼容方案，不在当前阶段投入大改；
- 优先观察和验收分仓加仓主链路。

所以 M3 不要越权碰 exit adaptation。

## 7.6 不要先碰“激进放宽”

明确禁止：

- 某些 regime 下提高总暴露；
- 某些 regime 下提高 symbol cap；
- 某些 regime 下提高 base entry size；
- 某些 regime 下放松 validator 门槛；
- 用 regime 去绕过已有 counter-trend / volatility filter。

这些都不是 M3。

---

## 8. 推荐推进顺序

## 8.1 总顺序

推荐顺序：

1. **Validator observe-only hints**
2. **Validator conservative enforcement**
3. **Risk observe-only hints**
4. **Risk conservative enforcement**
5. **最后才考虑 M4 execution adaptation**

也就是说，不建议“一次把 validator + risk 真生效全开”。

## 8.2 为什么先 validator 再 risk

因为 validator 比 risk 更好解释，也更少连带副作用。

### 先做 validator 的好处

- 拦截理由清楚；
- 可以直接映射到现有 `filter_code / filter_group / action_hint`；
- 不会直接影响 layer plan 预算分配；
- 对 intents / reconcile / executor 的侵入最小。

### risk 后做的原因

risk budget 虽然也在 M3 范围内，但它一旦生效，副作用更多：

- 同样的 `allow` 结果，可能因 entry ratio 缩小而表现不同；
- 可能导致 layer1 可开、layer2/3 不可开；
- 可能与待执行 intent 的预算占用一起触发“看似诡异”的 block。

所以应先证明 validator guardrails 是有价值且稳定的，再让 risk 生效。

---

## 9. 最小生效包建议

## 9.1 推荐的最小生效包（首选）

> **M3-Minimal Package = Validator first, Risk hints only**

包含：

1. `signals/validator.py` 支持 `effective_validation_snapshot`
2. `risk_anomaly` 统一 hard block reason code
3. `transition_risk` / `stability_score` 只做 validator 级 conservative gate
4. risk budget 先只输出 `effective_risk_budget_hint`，不真正改变 `compute_entry_plan()` 输入
5. dashboard / signal record / filter_details 补足 baseline vs effective 对比

### 为什么这是首选

- 生效面最小；
- 与当前 layering 实盘验收冲突最小；
- 能先回答“adaptive regime 在 M3 有无 guardrail 价值”；
- 回滚最容易，只需切回 observe-only / decision-only。

## 9.2 第二阶段最小扩展包

在上面的基础上，确认样本后再加：

1. `total_margin_cap_ratio` conservative override
2. `symbol_margin_cap_ratio` conservative override
3. `base_entry_margin_ratio` conservative override
4. `max_entry_margin_ratio` conservative override

但仍然：

- 不改 layer ratios；
- 不改 layer_max_total_ratio；
- 不改 executor 状态机；
- 不改 partial TP / trailing。

## 9.3 不建议的“假最小包”

以下看似省事，其实不建议：

### 方案 A：直接先上 risk，不上 validator

坏处：

- 现象会变成“allow 了但开不出来 / 开得异常小”，可解释性差；
- 线上同事更难区分是预算问题还是决策问题。

### 方案 B：直接改 layer ratios，让 high_vol 更小仓

坏处：

- 这已经越界到 M4；
- 还会污染当前 layering 主链路验收。

### 方案 C：validator 和 risk 一起全量真生效

坏处：

- 一旦交易数骤降，很难判断是哪个层面收得太重；
- 回滚也更模糊。

---

## 10. 配置开关设计建议

建议保留现有顶层模式：

```yaml
adaptive_regime:
  enabled: true
  mode: observe_only  # disabled / observe_only / decision_only / guarded_execute / full
```

但对于 M3，再增加 **更细的子开关**，避免 `guarded_execute` 一打开就所有东西全生效。

## 10.1 推荐配置结构

```yaml
adaptive_regime:
  enabled: true
  mode: guarded_execute
  detector:
    min_confidence: 0.55
    min_stability_score: 0.50
    cooloff_bars_after_switch: 2
  guarded_execute:
    validator_enabled: true
    risk_enabled: false
    enforce_conservative_only: true
    dry_run_risk_hints: true
    rollout_symbols: ["BTC/USDT"]
  defaults:
    policy_version: adaptive_policy_v1_m3_boundary
  regimes:
    high_vol:
      validation_overrides:
        min_strength: 24
        min_strategy_count: 2
        block_counter_trend: true
      risk_overrides:
        total_margin_cap_ratio: 0.24
        symbol_margin_cap_ratio: 0.10
        base_entry_margin_ratio: 0.06
    risk_anomaly:
      force_mode: block_new_entry
```

## 10.2 子开关含义

- `validator_enabled`
  - 允许只开 validator 生效
- `risk_enabled`
  - 允许 risk 继续只看 hints
- `enforce_conservative_only`
  - 防止配置误写成放宽
- `dry_run_risk_hints`
  - risk 先只生成 hint，不改 entry plan
- `rollout_symbols`
  - 灰度 symbol 白名单

## 10.3 配置防呆建议

需要在 config / resolver 层做显式保护：

- 若 `enforce_conservative_only=true`，任何放宽型 override 直接忽略并记入 `ignored_overrides`
- 若 `risk_enabled=false`，`risk_overrides` 仅落 observability，不影响 `compute_entry_plan`
- 若 symbol 不在 rollout list，自动退回 `decision_only` 或 `observe_only` 行为

---

## 11. 灰度方式建议

## 11.1 按 symbol 灰度，不按全市场一刀切

建议顺序：

1. 单 symbol（如 BTC/USDT）
2. 1~2 个高流动性 symbol
3. 全 watch list

原因：

- 高流动性标的更容易观察 high_vol / low_vol / transition 边界；
- 数据量更快积累；
- 出问题时更容易定位。

## 11.2 先 validator 真生效，risk 仍 dry-run

推荐灰度阶段：

### 阶段 A
- `mode=guarded_execute`
- `validator_enabled=true`
- `risk_enabled=false`
- `dry_run_risk_hints=true`

### 阶段 B
- `validator_enabled=true`
- `risk_enabled=true`
- risk 只启用最小 caps 收紧

### 阶段 C
- 扩大 symbol 范围
- 仍然不碰 execution overrides

## 11.3 灰度样本门槛建议

在进入下一步前，建议至少满足：

- 某个灰度 symbol 在目标 regime 下积累到可复盘样本；
- 能看到 validator block 的 reason code 分布；
- 能确认不是“单纯把所有信号都拦光”。

这里不强行写死样本数，但必须满足“能分 regime / reason code / symbol 做复盘”。

---

## 12. 回滚方式

M3 必须做成 **多级回滚**，而不是只有“开/关”两个档。

## 12.1 回滚层级

1. `risk_enabled=true -> false`
2. `validator_enabled=true -> false`
3. `mode=guarded_execute -> decision_only`
4. `mode=decision_only -> observe_only`
5. `enabled=false`

## 12.2 回滚原则

### 先回 risk，再回 validator

因为 risk 更容易制造隐性副作用。

### 回滚只改配置，不改代码

M3 若需要上线后回滚，应该能靠配置做到：

- 禁用 risk 生效
- 禁用 validator 生效
- 退回 decision-only

不应依赖线上热修代码来恢复交易主链路。

### 回滚后保留 observability

即使回到 `observe_only`，也应继续保留：

- regime snapshot
- policy snapshot
- effective hint
- ignored / applied override 记录

否则失去复盘证据。

---

## 13. 验收标准

M3 验收不要只看“有没有写完代码”，要看有没有守住边界。

## 13.1 架构验收

- validator / risk 的 adaptive 逻辑集中在 policy / effective snapshot 层，不散落大段 if/else；
- `direction_lock / intents / reconcile / self-heal / layer plan` 主骨架未被 regime 逻辑入侵；
- 不存在 M3 偷跑 execution profile override。

## 13.2 行为验收

- `observe_only` 下行为与当前主线一致；
- `guarded_execute + validator_only` 能改变 validator 结果，但不改 risk budget / layer plan；
- `guarded_execute + risk_enabled` 只会更保守，不会放宽任何 cap；
- symbol 不在 rollout 范围时，行为回退到非 M3 生效路径。

## 13.3 可解释性验收

每条被 M3 影响的 signal / validation 至少能看见：

- `regime_name`
- `regime_confidence`
- `stability_score`
- `transition_risk`
- `base_validation_config`
- `effective_validation_config`
- `base_risk_budget`
- `effective_risk_budget`
- `applied_overrides`
- `ignored_overrides`
- `block_reason_code`

## 13.4 风险验收

必须证明：

- 没出现 direction lock 异常释放 / 异常残留；
- 没出现 open intents 数异常积累；
- 没出现 reconcile / stale self-heal 异常增多；
- layering 状态机没有因 M3 被污染；
- 被 M3 拦下的单，理由可聚合、可复盘、可解释。

---

## 14. 失败信号

以下信号一旦出现，应视为 M3 灰度失败或至少需要暂停扩量。

### 14.1 交易数断崖式下滑，但 reason code 不集中

说明不是 guardrail 在有效工作，而是实现口径混乱、到处都在挡。

### 14.2 decision allow 很多，但 validator / risk deny 明显异常升高

说明 M2 与 M3 口径脱节，可能存在重复过滤或阈值打架。

### 14.3 layer1 能开、layer2/3 长期无法触发，且不是行情原因

说明 risk budget 的收紧可能已实质干扰 layering 验收，应暂停并回退 risk。

### 14.4 orphan intents / stale locks / reconcile issue 明显上升

说明 M3 已经对执行状态机造成副作用，这属于越界红线。

### 14.5 大量 `ignored_overrides` 或配置命中率极低

说明配置设计与实际 regime / symbol / mode 并未对齐，继续扩量没有意义。

### 14.6 线上问题无法从 observability 还原

若出现“开不出来，但不知道是 validator、risk、intent 还是 lock”的情况，说明 M3 observability 不合格。

---

## 15. 监控点建议

M3 上线后，建议盯以下监控点。

## 15.1 Validator 监控

- `validation pass rate by regime`
- `validation block rate by reason_code`
- `high_vol / risk_anomaly / transition_risk` 的 block 占比
- `baseline threshold vs effective threshold` 差异分布

## 15.2 Risk 监控

- `effective_entry_margin_ratio` 分布
- `remaining_total_cap / remaining_symbol_cap` 命中情况
- `risk block rate by regime`
- `pending_intents included budget pressure` 频率

## 15.3 执行链健康监控（防越界）

- `active_intents` 数量趋势
- `direction_locks` 数量趋势
- self-heal 触发次数
- reconcile issue 次数
- layer plan reset 次数与异常样本

## 15.4 业务结果监控

- allow / validate_pass / executed 三段漏斗
- `executed by regime`
- `filtered by regime`
- `missed_good_trade_ratio`（若有回测/复盘支持）
- 是否只是“更少交易”，还是“更少坏交易”

---

## 16. 推荐实施方案（最终建议）

## 16.1 推荐顺序

### Step 1：先补 M3 边界与入口
- 新增本文档
- 在 backlog / framework 的 M3 段落加入口链接

### Step 2：做 validator effective snapshot（不立刻全生效）
- 详细拆分见：[`docs/adaptive-market-regime-m3-step1-implementation.md`](./adaptive-market-regime-m3-step1-implementation.md)
- 输出 `base vs effective validation snapshot`
- 保留 `applied / ignored / reason_code`

### Step 3：只开 validator conservative enforcement
- 先只对白名单 symbol 生效
- 只动 `min_strength / min_strategy_count / counter_trend / anomaly hard block / transition gate`

### Step 4：risk 先做 hints，再做最小 conservative enforcement
- 先只输出 `effective_risk_budget_hint`
- 稳定后才真生效 `total_margin_cap_ratio / symbol_margin_cap_ratio / base_entry_margin_ratio`

### Step 5：确认 M3 稳定后，再讨论 M4
- 届时才考虑 layer ratios / execution profile

## 16.2 最小生效包（最终推荐）

> **先 validator，后 risk；先 observe-only risk hints，后 conservative risk enforcement。**

更具体地说：

- 首个真生效包：
  - `risk_anomaly` hard block
  - `transition_risk / stability_score` validator gate
  - `min_strength` / `min_strategy_count` 按 regime 收紧
  - risk 只输出 hints

- 第二个真生效包：
  - `total_margin_cap_ratio` 收紧
  - `symbol_margin_cap_ratio` 收紧
  - `base_entry_margin_ratio` / `max_entry_margin_ratio` 收紧
  - 仍不碰 execution profile

---

## 17. 对当前项目的明确边界判词

结合当前项目现状，我的建议非常明确：

1. **M3 可以做，但必须是 validator / risk guardrails，不是 execution adaptation。**
2. **M3 的第一优先级不是“更高收益”，而是“减少高风险错配开仓”。**
3. **现阶段绝对不要把 adaptive regime 提前塞进 layering / direction lock / intents / reconcile 主链路。**
4. **如果只能选一个最稳的起手式，就选：validator 先真生效，risk 先 hints-only。**
5. **一旦 M3 干扰当前 layering 实盘验收，应优先回退 risk，再视情况回退 validator。**

---

## 18. 相关文档

- 主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
- Backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)
- M3 Step 1 实施稿：[`docs/adaptive-market-regime-m3-step1-implementation.md`](./adaptive-market-regime-m3-step1-implementation.md)
- M0 实施稿：[`docs/adaptive-market-regime-m0-implementation.md`](./adaptive-market-regime-m0-implementation.md)
- Layering 配置：[`docs/layering-config-notes.md`](./layering-config-notes.md)
- Layering 验收清单：[`docs/layering-acceptance-checklist.md`](./layering-acceptance-checklist.md)

> 这份文档的作用不是替代主计划，而是把 **“M3 到底能碰什么、不能碰什么、先后顺序怎样、失败了怎么退”** 讲清楚，等真正开做时，大家唔使边写边猜。