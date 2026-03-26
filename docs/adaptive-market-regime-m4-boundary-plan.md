# Adaptive Market Regime M4 边界方案（Execution / Layering Guardrails）

> 配套主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
>
> 配套 backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)
>
> 配套 M3 边界：[`docs/adaptive-market-regime-m3-boundary-plan.md`](./adaptive-market-regime-m3-boundary-plan.md)
>
> 目的：在真正进入 M4 前，先把 adaptive regime 在 **execution / layering** 层的实施边界、禁区、灰度方式、回滚方式与推荐推进顺序讲清楚，避免一上来把当前已稳定的 execution state machine、layer plan 与持仓管理链路搅乱。

---

## 1. 一句话结论

M4 可以做，但只能做成 **execution profile 的保守化收口**，唔可以做成“adaptive regime 终于可以任意改执行”。

最稳妥的结论是：

- **先做 execution profile hints，再做 guarded enforcement**；
- **先碰 entry 侧 execution profile，不先碰 exit 侧 profile**；
- **只能让 execution 变得更保守、更慢、更小、更难追加，不准更激进**；
- **layering profile 必须晚于 execution profile enforcement**；
- **partial TP / trailing profile 化属于 M4 最后段增强项，不应作为 M4 起手式。**

如果要用一句最短的话定义 M4：

> M4 不是“让 adaptive regime 更会赚”，而是“在不破坏 execution 主骨架的前提下，让 executor 只做更保守的执行参数收紧”。

---

## 2. 为什么 M4 比 M3 更敏感

M3 影响的是 `validator / risk budget`，它们仍然属于“进入执行前的守门”。
但 M4 开始碰的是：

- `trading/executor.py` 真正消费的 execution profile
- `trading.layering` 的 layer plan 语义
- 同方向计划仓的节奏、层序、追加条件
- 持仓后续的 `partial TP / trailing` 行为

这比 M3 敏感得多，原因有 6 个。

### 2.1 M4 开始改的不是“是否开”，而是“怎么开、怎么加、怎么管”

M3 出错，常见现象是：

- allow 变少
- validator block 变多
- risk budget 收紧

M4 出错，常见现象会变成：

- layer1 能开但 layer2/3 行为怪异
- 同样 allow 的单，执行尺寸和层序不稳定
- intent / layer plan / trade snapshot 看起来不一致
- 持仓管理解释变得很复杂：到底是 regime 改了 profile，还是 baseline 自己触发了 exit

也就是说，M4 一旦出错，不是“多挡几张单”，而是会让**执行状态机可解释性下降**。

### 2.2 当前项目的 execution 链已经不是薄壳，已存在实盘纪律

当前项目 execution 链里，已经有这些在线骨架：

- layering 三层计划仓（默认 `[0.06, 0.06, 0.04]` / `0.16`）
- direction lock
- open intents
- signal idempotency
- min add interval
- max layers per signal
- reconcile / stale close / self-heal
- partial TP / trailing

这些不是可随便替换的参数展示层，而是互相咬合的运行态纪律。

所以 M4 的核心风险，不是“参数挑得够不够聪明”，而是：

> **一旦 execution profile override 越过边界，就可能把 layering / direction lock / intents / reconcile / self-heal 的解释链打散。**

### 2.3 当前项目仍在等真实开仓继续验收 layering 主链路

根据当前项目上下文：

- 最近并非没有候选信号，而是大量信号被审批 / 验证拦住；
- 当前重点仍是等待真实开仓，对 layering / intents / locks / layer plan / reset 做实战验收；
- partial TP 兼容方案先保留，不在当前阶段大改；
- UI 继续降优先级，优先级是数据正确性、执行链稳定性、通知可读性。

这代表 M4 不应该抢跑去“重新设计执行策略”。

如果 M4 一上来就改 layer ratios、加仓节奏、exit profile，很容易把当前最重要的实盘验收对象污染掉，之后根本讲不清是：

- 基线 layering 有问题；
- 还是 adaptive execution profile 有问题；
- 还是 partial TP / trailing 与 layering 发生了新冲突。

### 2.4 M4 比 M3 更容易制造“隐性回归”

M3 的 block / shrink 通常还算容易看出；
M4 的问题则更容易藏在行为细节里：

- 计划层比例看似合理，但 layer plan reset 异常；
- layer ratios 收紧后，剩余预算与 pending intents 组合出奇怪边界；
- add 条件被 profile 化后，同一 symbol 在不同 regime 下的节奏不一致；
- partial TP / trailing 的 override 与 baseline cache 状态互相覆盖。

这类问题通常要等真实交易样本跑出来才暴露，所以 M4 必须比 M3 更强调 **灰度、追踪、回滚**。

### 2.5 M4 一旦放宽，很容易变成“偷偷加杠杆 / 提高 aggressiveness”

在 M3 里，“只更保守”主要体现在 validator 与 risk caps；
在 M4 里，更危险的是执行侧的“放宽”往往披着优化外衣，例如：

- 更大 layer1 ratio，看起来像“更快抓住趋势”；
- 更短 min add interval，看起来像“更敏捷地加仓”；
- 更高 max layers per signal，看起来像“更完整执行 layer plan”；
- 更宽 trailing / 更晚 partial TP，看起来像“给趋势单更大空间”。

这些都可能在没有足够证据时，直接把 execution aggressiveness 推高。

所以 M4 必须把 **only more conservative** 写死成硬边界，而不是口头原则。

### 2.6 M4 直接连接 execution observability，若没 single source of truth 会非常危险

M4 之后，同一笔单至少要能回答：

- baseline execution profile 是什么
- effective execution profile 是什么
- 哪些 override 生效 / 忽略
- 为什么命中该 regime
- 该笔 intent / order / trade 实际按哪套 profile 执行

如果这条链断了，就会出现最糟糕的情况：

> 单是开了，但没人能准确解释它究竟按 baseline 执行，还是按 adaptive profile 执行。

---

## 3. 当前项目语境下，M4 的职责是什么

结合当前代码与项目状态，M4 应只承担三件事：

### 3.1 让 executor 拥有 `baseline vs effective execution profile` 的单一事实来源

也就是先在 execution 层明确：

- baseline profile 是当前 `trading.layering` + 执行默认值
- effective profile 是在 regime policy 约束后、真正给 executor 用的那套参数
- 任何 execution-level adaptive 行为都必须先过 `conservative_only` 检查

### 3.2 让 entry 侧 execution profile 先做“更保守”的轻量收紧

M4 起手式只应该聚焦 **entry / layering 前半段**，例如：

- layer1 / layer2 / layer3 比例变小
- layer_max_total_ratio 变小
- leverage_cap 变小
- min_add_interval_seconds 变长
- max_layers_per_signal 变少
- profit_only_add 从 `False -> True`

### 3.3 让 observability 明确解释 execution profile 的来源与命中情况

也就是：

- 每笔 intent / order / trade 记录 baseline / effective / applied / ignored
- rollout 命中情况可追踪
- 能按 symbol / regime / policy_version 复盘

注意：M4 **不负责** 重写 execution state machine，也不负责在这个阶段追求更高收益曲线。

---

## 4. M4 与 M3 / M5 的边界

| 阶段 | 可以做 | 不可以做 |
|---|---|---|
| M3 | validator / risk 的保守 guardrails | 不改 execution profile；不改 layering 计划参数 |
| **M4** | execution profile hints、guarded execution profile enforcement、谨慎的 layering profile 收紧 | 不改 intent 生命周期；不改 lock 语义；不改 reconcile / self-heal 语义；不先改 partial TP / trailing 主逻辑 |
| M5 | 离线校准、版本比较、策略建议 | 不应反向驱动线上绕过 M4 边界 |

### 4.1 M3 的边界

M3 到此为止，应已经证明：

- adaptive regime 会少犯错
- validator / risk guardrails 有解释性
- rollout 与回滚可控

但 **M3 还没有资格直接动 execution profile**。

### 4.2 M4 的边界

M4 的本质是：

- 在 executor 内生成 effective execution profile
- 允许小范围保守 override 真正影响 entry execution
- 但不触碰 execution 主骨架的状态机语义

### 4.3 M5 的边界

M5 只应该负责：

- 用离线数据评估 M4 是否值得扩大
- 产出 policy version 比较
- 总结哪些 execution override 有价值

不应让 M5 变成“因为报表看起来不错，所以线上直接放宽执行”。

---

## 5. 当前项目里，“只更保守”在 M4 的明确含义

M4 最大的坑，就是大家会误把“自适应执行”理解为“执行更灵活”。

在本项目里，**M4 的 only more conservative 必须具体写成以下约束**。

### 5.1 对 layering profile 而言，“更保守”只允许这些方向

只允许：

- `layer_ratios` 单层比例 **不大于 baseline**
- `layer_ratios` 总和 **不大于 baseline 总和**
- `layer_max_total_ratio` **不大于 baseline**
- `max_layers_per_signal` **不大于 baseline**
- `min_add_interval_seconds` **不小于 baseline**
- `profit_only_add` 允许 `False -> True`，不允许 `True -> False`
- `allow_same_bar_multiple_adds` 允许 `True -> False`，不允许反向
- `disallow_skip_layers` 允许保持更严格，不允许为了 regime 变松

### 5.2 对 execution profile 而言，“更保守”只允许这些方向

只允许：

- `leverage_cap` **降低**，不允许提高
- 首层 entry aggressiveness **降低**，不允许提高
- 追加触发难度 **提高**，不允许降低
- 同方向计划总暴露 **降低**，不允许提高
- 下单节奏 **放慢**，不允许加快

换句话说，M4 可以让 executor “更难出手、更慢追加、更小尺寸”，但不能让 executor 因 regime 变得更激进。

### 5.3 与 direction lock / intents / reconcile / self-heal 的关系

这里要特别写清楚：

- **direction lock**：M4 只能消费锁带来的既有约束，不能改锁语义
- **intents**：M4 只能给 intent 补充 effective profile observability，不能改 intent 生命周期
- **reconcile**：M4 只能让 trade / open_trade 记录更可解释，不能改 reconcile 判断逻辑
- **self-heal**：M4 不能因为 regime 新增特殊自愈路径

也就是说，M4 的 profile override 必须是 **execution 参数层面的 pure input shaping**，不能升级成状态机改写。

### 5.4 与 partial TP / trailing 的关系

当前项目已明确：

- partial TP 先保留兼容方案，不立即大改；
- trailing / partial TP 与 layering 存在潜在冲突；
- 当前优先继续观察并验收分仓加仓主链路。

因此在 M4 里：

- **把 partial TP / trailing profile 化，不等于“只更保守”自动成立**；
- 这类改动即使数值看似收紧，也会改变持仓后的真实行为；
- 它们必须晚于 entry 侧 execution profile 收紧，并且默认 shadow / hints-only。

### 5.5 一个很重要的边界判词

在本项目里：

> **M4 的“只更保守”优先定义为 entry-side execution profile 的保守收紧，而不是 exit-side 行为重写。**

这句一定要钉死，不然最容易偷跑到 trailing / partial TP。

---

## 6. M4 哪些 execution / profile 项可以先碰

## 6.1 第一优先：execution profile hints（只观察，不立即真生效）

先生成并记录：

- `baseline_execution_profile`
- `effective_execution_profile_hint`
- `applied_execution_overrides`
- `ignored_execution_overrides`
- `rollout_match`
- `effective_state`（hint-only / enforced / bypassed）

### 建议先纳入 hints 的字段

1. `layer_ratios`
2. `layer_max_total_ratio`
3. `max_layers_per_signal`
4. `min_add_interval_seconds`
5. `profit_only_add`
6. `allow_same_bar_multiple_adds`
7. `leverage_cap`（若已有统一执行入口）

这一步的目标不是改变执行，而是先验证：

- effective profile 结构够不够稳定
- 可解释性是否足够
- conservative merge 是否能稳定工作

## 6.2 第二优先：guarded execution profile enforcement（先不碰 layering 深水区）

在 hints 稳定后，优先让以下项目小范围真生效：

1. `leverage_cap`
2. `layer_max_total_ratio`
3. `max_layers_per_signal`
4. `min_add_interval_seconds`
5. `profit_only_add`

为什么先这些？

- 它们对 state machine 的侵入相对小；
- 更像“执行纪律收紧”；
- 比直接改 `layer_ratios` 更容易解释与回滚。

## 6.3 第三优先：guarded layering profile enforcement

只有在前两步稳定后，才考虑小范围生效：

1. `layer_ratios` 更保守重映射
2. `layer_count` / `max_layers_per_signal` 的联动收紧（如确有必要）
3. `layer plan` 上限的 regime 保守化

注意：

- `layer_ratios` 是 M4 中最容易影响当前 layering 验收口径的项；
- 应视作 **M4 后半段**，不是起手式。

## 6.4 最后才考虑：trailing / partial TP profile hints

即使要碰，也建议顺序是：

1. 先 shadow record / hints-only
2. 样本足够后再讨论是否 enforcement
3. 默认只允许更保守，且必须能与 baseline 行为做 A/B 对比

---

## 7. M4 哪些绝对不要先碰

下面这些，建议明确列入 **M4 起步禁区**。

## 7.1 不要先改 direction lock 语义

不要因为 regime 去改：

- `direction_lock_scope`
- lock 创建 / 释放时机
- 某些 regime 下跳过 lock

原因：这不是 execution profile，这是执行纪律根语义。

## 7.2 不要先改 intents 生命周期

不要因为 adaptive regime 去改：

- `open_intents` 创建时机
- pending / submitted / filled / failed 的状态切换
- intent 清理逻辑

M4 只能给 intent 附加 effective profile 记录，不能重写 intent 状态机。

## 7.3 不要先改 reconcile / self-heal 逻辑

包括但不限于：

- `reconcile_trade_close`
- stale trade close
- stale lock cleanup
- orphan intent cleanup
- layer state reset / sync

这些属于稳定性兜底，不应在 M4 里掺 regime 特判。

## 7.4 不要先改 partial TP / trailing enforcement

即使 backlog 里 M4 允许谈这块，也不建议作为 M4 首批生效项。

原因很直接：

- 它们与 layering / 持仓缓存 / trade close reason 有耦合；
- 当前项目已经明确这块敏感，先保留兼容方案；
- 一旦同时改 entry 与 exit，很难定位线上行为异常来源。

## 7.5 不要先做任何“更激进”的 execution override

明确禁止：

- 提高 `layer_ratios`
- 提高 `layer_max_total_ratio`
- 缩短 `min_add_interval_seconds`
- 提高 `max_layers_per_signal`
- 将 `profit_only_add` 从 `True -> False`
- 开启更多同 bar 加仓机会
- 因 regime 给趋势单更大首层尺寸

这些都不是 M4 起步该做的事。

---

## 8. 推荐推进顺序

## 8.1 总顺序

推荐顺序：

1. **Execution profile hints**
2. **Guarded execution profile enforcement**
3. **Guarded layering profile enforcement**
4. **Trailing / partial TP hints**
5. **若样本充分，再讨论 trailing / partial TP enforcement**

如果要更白话一点：

> 先让 executor 会“解释自己要怎么做”，再让它“真的这么做”；先收紧执行纪律，再收紧 layer plan；最后才碰 exit profile。

## 8.2 为什么先 execution profile hints

因为 M4 的第一要务不是改参数，而是建立 **single source of truth**：

- baseline 是什么
- effective 是什么
- 为什么变
- 哪些没变

如果连这条链都未稳定，就直接生效，后面只会越来越难排查。

## 8.3 为什么 enforcement 先于 layering profile

因为以下项比较像“外围护栏”：

- `leverage_cap`
- `layer_max_total_ratio`
- `max_layers_per_signal`
- `min_add_interval_seconds`
- `profit_only_add`

而 `layer_ratios` 会直接改 layer plan 结构，是更深的 execution 侵入。

## 8.4 为什么 exit profile 要最后做

因为当前项目的阶段重点，仍然是：

- 先验证 layering / intent / lock / reset 主链路
- partial TP 兼容方案先保留，不提前大改

所以把 trailing / partial TP 摆最后，不是保守过头，而是工程上更清醒。

---

## 9. 最小生效包建议

## 9.1 推荐的最小生效包（首选）

> **M4-Minimal Package = execution profile hints + guarded execution profile enforcement（不含 layer_ratios / partial TP / trailing）**

包含：

1. executor 输出 `baseline_execution_profile`
2. executor 输出 `effective_execution_profile_hint`
3. 增加 `applied / ignored / rollout_match / effective_state`
4. 小范围 symbol 真生效以下保守项：
   - `leverage_cap`
   - `layer_max_total_ratio`
   - `max_layers_per_signal`
   - `min_add_interval_seconds`
   - `profit_only_add`
5. trade / open_trade / intent / notification 里至少有一处能追到 effective execution profile 摘要

### 为什么这是首选

- 不直接污染 layer ratios 验收口径；
- 不碰 exit profile；
- 对当前 layering / intents / reconcile / self-heal 主链影响最小；
- 一旦出问题，可快速只回退 enforcement，不丢 observability。

## 9.2 第二阶段最小扩展包

在首选包稳定后，再加：

1. `layer_ratios` regime conservative mapping
2. `layer_count` / `max_layers_per_signal` 的协同保守化（若必要）
3. layer plan observability 增强（baseline vs effective plan）

但仍然：

- 不改 direction lock 语义；
- 不改 intent 生命周期；
- 不改 reconcile / self-heal；
- 默认不改 partial TP / trailing enforcement。

## 9.3 不建议的“假最小包”

### 方案 A：直接先改 `layer_ratios`

坏处：

- 这会直接污染当前 layering 主链路验收；
- 问题定位会落到 layer plan 深水区。

### 方案 B：entry 与 exit profile 一起上

坏处：

- 一旦行为异常，根本分不清是开仓 profile 还是持仓管理 profile 导致。

### 方案 C：只做 observability，不做任何可控 enforcement

坏处：

- 会继续拖慢 M4 的真实价值验证；
- 没法证明 execution-side conservative adaptation 是否有效。

---

## 10. 配置开关设计建议

建议保留现有顶层模式：

```yaml
adaptive_regime:
  enabled: true
  mode: guarded_execute
```

但 M4 需要比 M3 更细的 execution 子开关，避免一开 `guarded_execute` 就把 execution / layering / exit profile 全开。

## 10.1 推荐配置结构

```yaml
adaptive_regime:
  enabled: true
  mode: guarded_execute
  guarded_execute:
    execution_profile_hints_enabled: true
    execution_profile_enforcement_enabled: false
    layering_profile_enforcement_enabled: false
    exit_profile_hints_enabled: false
    exit_profile_enforcement_enabled: false
    conservative_only: true
    rollout_symbols: ["BTC/USDT"]
  defaults:
    policy_version: adaptive_policy_v1_m4_boundary
  regimes:
    high_vol:
      execution_overrides:
        layer_max_total_ratio: 0.12
        max_layers_per_signal: 2
        min_add_interval_seconds: 600
        profit_only_add: true
    transition_risk_high:
      execution_overrides:
        leverage_cap: 2
        layer_max_total_ratio: 0.10
```

## 10.2 子开关含义

- `execution_profile_hints_enabled`
  - 只生成 effective execution hint，不真正改执行
- `execution_profile_enforcement_enabled`
  - 允许真生效外围 execution profile 收紧
- `layering_profile_enforcement_enabled`
  - 允许 layer ratios / deeper layering profile 小范围真生效
- `exit_profile_hints_enabled`
  - 只记录 trailing / partial TP 的 hypothetical profile
- `exit_profile_enforcement_enabled`
  - 真生效 trailing / partial TP override；默认应关闭
- `conservative_only`
  - 拒绝所有放宽型 override
- `rollout_symbols`
  - 灰度 symbol 白名单

## 10.3 配置防呆建议

必须做显式保护：

- `conservative_only=true` 时，任何放宽型 execution override 直接忽略并记入 `ignored_overrides`
- `layering_profile_enforcement_enabled=false` 时，`layer_ratios` 只能落 hint
- `exit_profile_enforcement_enabled=false` 时，trailing / partial TP override 只能 shadow record
- symbol 不在 rollout list 时，自动退回 hint-only 或 bypass

---

## 11. 灰度方式建议

## 11.1 按 symbol 灰度，不按全市场一刀切

建议顺序：

1. 单 symbol（BTC/USDT）
2. 1~2 个高流动性 symbol
3. 再考虑扩大到 watch list

原因：

- 高流动性 symbol 更容易积累 execution 样本；
- 出问题时更容易定位；
- 能更快观察同一 profile 在不同 regime 下的行为一致性。

## 11.2 先 hints，再 enforcement，再 deeper layering

推荐灰度阶段：

### 阶段 A
- `execution_profile_hints_enabled=true`
- `execution_profile_enforcement_enabled=false`
- `layering_profile_enforcement_enabled=false`
- `exit_profile_hints_enabled=false`

### 阶段 B
- 开 `execution_profile_enforcement_enabled=true`
- 只启用外围保守项：`leverage_cap / layer_max_total_ratio / max_layers_per_signal / min_add_interval_seconds / profit_only_add`

### 阶段 C
- 继续保留前述 enforcement
- 小范围开启 `layering_profile_enforcement_enabled=true`
- 只让 `layer_ratios` 更保守，不做更复杂动态编排

### 阶段 D
- 仅当 entry 侧稳定后，才开 `exit_profile_hints_enabled=true`
- 继续观察，不急于 enforcement

## 11.3 灰度样本门槛建议

进入下一步前，至少要满足：

- 能抽到真实 intent / trade 样本验证 effective execution profile 可追溯；
- 能按 regime 看 execution override 命中分布；
- 没有出现 layering 状态解释混乱；
- 没有出现 intent / lock / reconcile / self-heal 的异常抬升。

---

## 12. 回滚方式

M4 一定要做成 **分层回滚**，不可以只有“整包开 / 整包关”。

## 12.1 回滚层级

1. `exit_profile_enforcement_enabled=true -> false`
2. `layering_profile_enforcement_enabled=true -> false`
3. `execution_profile_enforcement_enabled=true -> false`
4. `execution_profile_hints_enabled=true -> false`（通常不建议立刻关）
5. `mode=guarded_execute -> decision_only / observe_only`
6. `enabled=false`

## 12.2 回滚原则

### 先回 deeper enforcement，再回 hints

因为 hints 主要保留复盘证据；
真正高风险的是深层 execution enforcement。

### 先回 layering / exit，再回外围 execution profile

因为：

- `layer_ratios`
- `partial TP / trailing`

对行为影响最深，也最难解释。

### 回滚优先走配置，不依赖热修代码

M4 若要上线，就必须保证：

- 只改配置即可关闭 execution enforcement；
- 只改配置即可停掉 deeper layering / exit profile；
- observability 仍然保留，方便复盘。

---

## 13. 验收标准

M4 验收不要只看“代码能跑”，而要看是否守住边界。

## 13.1 架构验收

- effective execution profile 有单一事实来源；
- adaptive execution 逻辑集中，不散落大量 regime if/else；
- `direction_lock / intents / reconcile / self-heal` 语义未被 M4 改写；
- M4 未偷跑大改 partial TP / trailing 主逻辑。

## 13.2 行为验收

- hints-only 下，执行结果与当前主线一致；
- execution enforcement 只会更保守，不会放宽 baseline；
- layering profile enforcement 未破坏 layer plan reset / 同步 / 观察链；
- rollout symbol 外的行为不受影响。

## 13.3 可解释性验收

每条被 M4 影响的 execution 样本，至少能看到：

- `regime_name`
- `policy_version`
- `baseline_execution_profile`
- `effective_execution_profile`
- `applied_overrides`
- `ignored_overrides`
- `effective_state`
- `rollout_match`
- `root_signal_id / intent_id / trade_id` 的关联链

## 13.4 稳定性验收

必须证明：

- `direction lock` 没有异常释放 / 残留；
- `open intents` 没有异常积压；
- `reconcile issue` 没有明显抬升；
- `self-heal` 频率没有因为 M4 异常上升；
- `layer plan` 观察面没有变得不可解释。

---

## 14. 失败信号

以下信号一旦出现，应视为 M4 灰度失败或至少需要暂停扩量。

### 14.1 execution profile 命中了，但 order / trade 无法回溯其来源

说明 observability 不合格，必须先补证据链。

### 14.2 layer1 / layer2 / layer3 行为明显异常，但无法区分是 risk 还是 execution 造成

说明 M3 / M4 边界被打穿，需暂停并回退 execution enforcement。

### 14.3 intent / lock / reconcile / self-heal 异常量抬升

这属于越界红线，说明 M4 已对状态机造成副作用。

### 14.4 大量 `ignored_overrides`、命中率极低、或 rollout 外误生效

说明配置设计与 resolver 落地口径有问题，继续放量无意义。

### 14.5 当前真实开仓本来就少，M4 上线后几乎看不到有效样本

说明 M4 切入时机未成熟，应该先继续积累 M3 / baseline execution 样本，而不是硬推 deeper execution adaptation。

### 14.6 partial TP / trailing 解释复杂度突然飙升

说明 exit profile 已过早进入真实行为层，应立即关闭 exit enforcement。

---

## 15. 监控点建议

## 15.1 Execution 监控

- `effective execution profile hit rate by regime`
- `execution enforcement hit rate by symbol`
- `layer_max_total_ratio` 命中分布
- `max_layers_per_signal` 收紧命中分布
- `min_add_interval_seconds` 收紧命中分布

## 15.2 Layering 监控

- `filled_layers / planned_layers` 分布
- `layer plan reset` 次数与异常样本
- `same-bar add denied` 频率
- `profit_only_add denied` 频率
- `min_add_interval denied` 频率

## 15.3 状态机健康监控

- `active_intents` 数量趋势
- `direction_locks` 数量趋势
- `self-heal` 触发次数
- `reconcile issue` 次数
- `stale lock / orphan intent` 清理次数

## 15.4 业务结果监控

- allow / validate_pass / executed 漏斗
- executed by regime / symbol
- layer1 执行率、layer2/3 追加率
- 被 execution profile 收紧后放弃的样本比例
- 是否真正“少犯错”，而不是纯粹“更少交易”

---

## 16. 推荐实施方案（最终建议）

## 16.1 推荐顺序

### Step 1：先补 M4 边界与入口
- 新增本文档
- 在 README / framework / backlog 补入口链接

### Step 2：先做 execution profile hints
- 只补 `baseline vs effective` snapshot
- 不立刻改变 executor 行为
- 先证明 effective profile 可解释、可追踪

### Step 3：只开 guarded execution profile enforcement
- 先只对白名单 symbol 生效
- 先只动外围保守项：`leverage_cap / layer_max_total_ratio / max_layers_per_signal / min_add_interval_seconds / profit_only_add`

### Step 4：确认稳定后，再考虑 guarded layering profile
- 才开始谈 `layer_ratios` 更保守映射
- 仍然不改 direction lock / intents / reconcile / self-heal

### Step 5：entry 侧稳定后，再考虑 exit profile hints
- 先 shadow trailing / partial TP
- 不急于 enforcement

## 16.2 最小生效包（最终推荐）

> **先 execution profile hints，再 guarded execution profile enforcement；先不碰 layer_ratios，先不碰 partial TP / trailing。**

更具体地说：

- 首个真生效包：
  - `baseline_execution_profile`
  - `effective_execution_profile_hint`
  - `applied / ignored / rollout_match / effective_state`
  - 小范围 symbol 生效：
    - `leverage_cap`
    - `layer_max_total_ratio`
    - `max_layers_per_signal`
    - `min_add_interval_seconds`
    - `profit_only_add`

- 第二个真生效包：
  - `layer_ratios` regime conservative mapping
  - layer plan baseline/effective 对照
  - 继续保持 exit profile hints-only

---

## 17. 对当前项目的明确边界判词

结合当前项目现状，我的建议很明确：

1. **M4 可以做，但必须是 execution profile guardrails，不是 execution state machine 改写。**
2. **M4 比 M3 更敏感，因为它开始直接影响“怎么开、怎么加、怎么管”，而不只是“让不让开”。**
3. **M4 的“只更保守”必须定义成 entry-side execution profile 收紧，不包括提前大改 partial TP / trailing。**
4. **如果只能选一个最稳起手式，就选：execution profile hints + guarded execution profile enforcement（不含 layer_ratios / exit profile）。**
5. **一旦 M4 干扰当前 layering 主链路验收，应优先回退 layering / exit enforcement，再视情况回退外围 execution enforcement。**

---

## 18. 相关文档

- 主计划：[`docs/adaptive-market-regime-framework-plan.md`](./adaptive-market-regime-framework-plan.md)
- Backlog：[`docs/adaptive-market-regime-backlog.md`](./adaptive-market-regime-backlog.md)
- M4 Step 1 实施稿：[`docs/adaptive-market-regime-m4-step1-implementation.md`](./adaptive-market-regime-m4-step1-implementation.md)
- M4 Step 2 实施稿：[`docs/adaptive-market-regime-m4-step2-implementation.md`](./adaptive-market-regime-m4-step2-implementation.md)
- M4 Step 3 实施稿：[`docs/adaptive-market-regime-m4-step3-implementation.md`](./adaptive-market-regime-m4-step3-implementation.md)
- M3 边界：[`docs/adaptive-market-regime-m3-boundary-plan.md`](./adaptive-market-regime-m3-boundary-plan.md)
- M0 实施稿：[`docs/adaptive-market-regime-m0-implementation.md`](./adaptive-market-regime-m0-implementation.md)
- Layering 配置：[`docs/layering-config-notes.md`](./layering-config-notes.md)
- Layering 验收清单：[`docs/layering-acceptance-checklist.md`](./layering-acceptance-checklist.md)

> 这份文档的作用，不是替代主计划，而是把 **“M4 到底能碰什么、不能碰什么、先后顺序怎样、失败了怎么退”** 讲清楚，等真正开做 execution adaptation 时，大家唔使边写边猜。