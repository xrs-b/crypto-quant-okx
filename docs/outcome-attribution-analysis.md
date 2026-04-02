# Outcome Attribution Analysis CLI

用于做一版非 UI 的样本归因分析，重点看：

- `instant_stopout`
- `pre_arm_exit`
- stale / drift（基于 `signal_age_seconds_at_entry`、`stale_signal_ttl_seconds`、`entry_drift_pct_from_signal`、`entry_drift_tolerance_bps`）
- `close_reason_code`
- 按 `symbol / dominant_strategy / regime` 聚合

## 用法

```bash
python3 scripts/analyze_outcome_attribution.py
```

新增一个更聚焦“自动问题摘要”的非 UI 脚本，默认看 `XRP/USDT` + `SOL/USDT`，并同时输出：

- 最近 24 小时视角
- 最近 50 笔视角

```bash
python3 scripts/outcome_issue_summary.py
```

再往前推进一层，现在也支持“自动参数调优建议（只给建议，不自动改配置）”CLI，同样默认聚焦 `XRP/USDT` + `SOL/USDT`：

```bash
python3 scripts/parameter_tuning_advice.py
```

以及“配置 patch 草案生成器”（依然只输出建议文本 / YAML patch，不自动 apply）：

```bash
python3 scripts/parameter_tuning_patch.py
```

常用参数：

```bash
# 最近 48 小时
python3 scripts/analyze_outcome_attribution.py --hours 48

# 最近 100 笔，聚焦 XRP / SOL
python3 scripts/analyze_outcome_attribution.py --limit 100 --focus-symbol XRP/USDT --focus-symbol SOL/USDT

# 只看 XRP / SOL，并输出 JSON
python3 scripts/analyze_outcome_attribution.py \
  --symbol XRP/USDT \
  --symbol SOL/USDT \
  --hours 72 \
  --json

# 自动问题摘要：仅输出最近 72 小时视角
python3 scripts/outcome_issue_summary.py --view hours --hours 72

# 自动问题摘要：只输出最近 80 笔视角
python3 scripts/outcome_issue_summary.py --view trades --limit 80

# 自动问题摘要：改成看 BTC / ETH
python3 scripts/outcome_issue_summary.py \
  --symbol BTC/USDT \
  --symbol ETH/USDT

# 自动参数调优建议：最近 72 小时，仅输出建议
python3 scripts/parameter_tuning_advice.py --view hours --hours 72

# 自动参数调优建议：最近 80 笔，并输出 JSON
python3 scripts/parameter_tuning_advice.py --view trades --limit 80 --json

# 配置 patch 草案：只看 XRP，并输出人类可读建议
python3 scripts/parameter_tuning_patch.py --view hours --hours 72 --symbol XRP/USDT

# 配置 patch 草案：输出 YAML patch 草案
python3 scripts/parameter_tuning_patch.py --view trades --limit 80 --format yaml
```

## 输出内容

- 全局样本规模、structured outcome 覆盖率
- `instant_stopout_count/share`
- `pre_arm_exit_count/share`
- stale signal breach / drift breach 计数与占比
- `signal_age_seconds_at_entry` 分布（mean / median / p90 / min / max）
- `entry_drift_pct_from_signal` 分布（mean / median / p90 / min / max）
- `close_reason_code` 分布
- 按 `symbol / dominant_strategy / regime` 的聚合摘要
- 对焦 symbol（默认 `XRP/USDT`、`SOL/USDT`）的额外摘要与最近 flag 样本
- 自动问题摘要脚本额外输出字段覆盖率：
  - `signal_age_seconds_at_entry`
  - `entry_drift_pct_from_signal`
  - `exit_guard_state`

## 说明

- 仅统计已平仓样本。
- stale breach 判定：`signal_age_seconds_at_entry > stale_signal_ttl_seconds`
- drift breach 判定：`abs(entry_drift_pct_from_signal) > entry_drift_tolerance_bps / 100`
- strategy 聚合按 `dominant_strategy` 做单桶归类，避免一笔多策略交易重复计数。
