# Adaptive Market Regime Backlog

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

---

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
  - 2026-03-27 implementation update（第一批）：execution/layering 路径现已真正消费 guarded live layering guardrails；`layer_max_total_ratio`、`max_layers_per_signal`、`min_add_interval_seconds`、`profit_only_add`、`allow_same_bar_multiple_adds` 仅在 `layering_profile_enforcement_enabled=true` + rollout 命中时 live，且保持 conservative-only；`layer_ratios` 继续 hints-only，`layer_count` 继续只作 derived/audit。

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
