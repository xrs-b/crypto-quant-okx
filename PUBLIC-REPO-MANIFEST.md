# PUBLIC REPO MANIFEST

面向准备把本项目作为 **public repo** 分享时的保留/排除约定。

> 维护策略：**公共仓库是唯一长期代码主线；私人环境只保留 config / secret / logs / db / runtime state 等运行态内容。**

## 公开仓库应保留

这些内容适合留在公开仓库，方便别人 clone 后理解、安装、测试：

> preset 策略已刻意收窄：公开首版只保留 `btc-focused` 与 `safe-mode`，避免候选型 preset 造成误用或误解。

- 核心代码：`bot/` `core/` `signals/` `strategies/` `trading/` `analytics/` `dashboard/`
- 开源许可：`LICENSE`（MIT）
- 可公开样例配置：
  - `.env.example`
  - `config/config.yaml.example`
  - `config/config.local.yaml.example`
  - `config/presets/btc-focused.yaml`（仅公开策略/通知开关，不含 secret）
  - `config/presets/safe-mode.yaml`（仅公开策略/通知开关，不含 secret）
- 部署与使用文档：
  - `README.md`
  - `docs/DEPLOYMENT.md`
  - `docs/PUBLIC-MAINLINE-WORKFLOW.md`
  - `docs/GITHUB-PUBLIC-REPO-COPY.md`
  - `PUBLIC-RELEASE-CHECKLIST.md`
  - 本文件
- 测试：`tests/`
- 通用模板脚本：
  - `scripts/start.sh`
  - `scripts/start.public.sh`
  - `scripts/keep_dashboard_alive.sh`
  - `scripts/okx-trading.service`
  - `scripts/com.oink.crypto-quant-okx.dashboard-keepalive.plist`
  - `scripts/candidate-review.cron.example`

## 不应进入公开仓库

以下内容默认视为 **本地运行态、私人运维痕迹或内部工作材料**：

- 私密配置与凭证：
  - `.env`
  - `config/config.yaml`
  - `config/config.local.yaml`
  - `config/backups/`
  - 所有 Discord / Telegram / SMTP / webhook / channel/chat id 等真实通知凭证
- 运行态文件：
  - `logs/`
  - `*.pid`
  - `data/runtime_state.json`
  - `data/config_audit.json`
  - `data/trading.db`
  - `dashboard/data/`
- 本地环境目录：
  - `.venv/`
  - `__pycache__/`
- 本地训练产物 / 中间数据：
  - `ml/data/`
  - `ml/models/`
- 内部上下文 / 工作流文档：
  - `docs/current-context-*`
  - `docs/plans/`
  - 仅供作者自己续接思路的临时计划、审计、重构草稿

## 保留但按“模板”理解的文件

以下文件可以公开，但**不要直接照抄作者机器设置**：

- `scripts/okx-trading.service`
- `scripts/com.oink.crypto-quant-okx.dashboard-keepalive.plist`
- `scripts/candidate-review.cron.example`

使用时应自行替换：

- 用户名
- 仓库路径
- Python / venv 路径
- 端口、日志路径、调度频率

## 发布前最少检查

1. `git status` 应只包含你准备公开的代码/文档改动
2. 再扫一遍疑似 secret / token / 私人路径
3. 确认 README 与 DEPLOYMENT 仍能指导新用户完成 testnet 部署
4. 确认没有把运行日志、数据库、PID、私有配置一起提交

## 推荐工作流

- **公共 GitHub 仓库**：唯一长期代码主线，只保留核心代码、示例配置、脱敏文档、测试与模板脚本
- **私人运行环境**：只保留你的真实运行态、日志、数据库、本地配置、`.env`、`config.local` 与机器专属部署文件

如果二者长期混在同一个工作目录，发布前请至少再做一次完整人工复核，并遵守：[`docs/PUBLIC-MAINLINE-WORKFLOW.md`](docs/PUBLIC-MAINLINE-WORKFLOW.md)