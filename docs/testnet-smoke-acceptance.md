# Testnet Smoke Acceptance

目标：为后续连续跑数提供一个最小、可重复、可验收的 testnet smoke 闭环。

## 最小执行方式

真实 testnet 最小开平仓：

```bash
python bot/run.py --exchange-smoke --execute --symbol BTC/USDT --side long
```

连续执行 3 轮并输出 acceptance 汇总：

```bash
python scripts/testnet_smoke_acceptance.py --runs 3 --interval-seconds 30 --symbol BTC/USDT --side long
```

只做预演：

```bash
python scripts/testnet_smoke_acceptance.py --runs 3 --preview-only
```

## 通过标准

- 每轮 CLI 退出码为 0
- `smoke_runs` 有新增记录
- `opened=true` 且 `closed=true`
- `cleanup_needed=false`
- `residual_position_detected=false`

## 建议巡检项

- 最近一次 `smoke_runs.details.reconcile_summary.open_order_confirmed=true`
- 最近一次 `smoke_runs.details.reconcile_summary.close_order_confirmed=true`
- 若出现 `manual_testnet_cleanup_required`，先清残仓，再继续连续跑数

## 与 live execution guard contract 的关系

runtime 主线会在真正 open_position 前消费 `live_execution_guard_v1`；
如果 final execution permit 已存在但 guard contract 缺失/不一致，executor 会直接 fail-closed 拒绝执行。
这保证 testnet 连续跑数时，执行前约束有清晰、可验证的统一入口。
