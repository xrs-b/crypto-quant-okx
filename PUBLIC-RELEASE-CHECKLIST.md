# PUBLIC RELEASE CHECKLIST

面向把 `crypto-quant-okx` 整理成 **朋友下载后可直接部署运行** 的公开版本。

> 目标不是“勉强能公开”，而是做到：  
> **别人 clone / download 后，照 README 就能装依赖、配配置、做 testnet 验证、正常启动。**

---

## P0 · 不做先别公开

### 1. 轮换并作废所有疑似已暴露凭证
- [ ] 轮换 Discord bot token
- [ ] 轮换 webhook / channel 凭证（如适用）
- [ ] 检查是否还有真实 API Key / Secret / Passphrase 留在仓库或历史里
- [ ] 用 `gitleaks` / 手工 grep 再扫一次

重点检查：
- [ ] `config/presets/btc-focused.yaml`
- [ ] `config/presets/safe-mode.yaml`
- [ ] `config/config.local.yaml`
- [ ] `config/backups/`
- [ ] 历史 commit

### 2. 清理不该公开的运行态文件
- [ ] 确认以下内容不进入公开版仓库
  - [ ] `logs/`
  - [ ] `data/trading.db`
  - [ ] `data/runtime_state.json`
  - [ ] `bot.pid`
  - [ ] `dashboard.pid`
  - [ ] `relay.pid`
  - [ ] `.venv/`
  - [ ] `config/backups/`
- [ ] 补全 `.gitignore`
- [ ] 检查 Git 历史里有没有提交过以上内容

### 3. 所有示例配置必须可直接复制使用

公开首版建议只保留两个 preset：`btc-focused`（标准 testnet 起点）与 `safe-mode`（更保守的降级示例）。

- [ ] `config/config.yaml.example` 保持完整、可运行、无真实凭证
- [ ] `config/config.local.yaml.example` 只保留模板值
- [ ] 任何 preset 不得包含真实 token
- [ ] 示例配置字段必须和当前代码读取逻辑一致

---

## P1 · 让朋友真的能部署

### 4. 去掉本机绝对路径依赖
当前已发现路径硬编码痕迹，公开前建议全部改为“项目根目录相对路径”或环境变量：

- [ ] `bot/run.py`
- [ ] `core/runtime_control.py`
- [ ] `scripts/start.sh`
- [ ] `scripts/keep_dashboard_alive.sh`
- [ ] `scripts/review_candidates.py`
- [ ] `scripts/daily_summary.py`
- [ ] `scripts/candidate-review.cron.example`
- [ ] `scripts/com.oink.crypto-quant-okx.dashboard-keepalive.plist`

建议标准：
- 从 `Path(__file__).resolve()` 推导项目根目录
- 或支持 `PROJECT_DIR` 环境变量覆盖

### 5. 统一 Dashboard 端口与启动方式
- [x] 默认端口统一为 `5555`
- [x] README、配置样例、脚本、Dashboard 文案全部统一到 `5555`
- [x] 确保 `python3 bot/run.py --dashboard --port 5555` 为公开版默认启动方式

### 6. 补齐最小可运行路径
朋友第一次部署，至少要能完成以下动作：

- [ ] 安装依赖成功
- [ ] 复制配置成功
- [ ] `--exchange-diagnose` 成功
- [ ] `--notify-test` 成功
- [ ] `--exchange-smoke` 成功
- [ ] `python3 bot/run.py` 能启动
- [ ] Dashboard 能打开

如果任何一步要依赖“你本地机器特定路径 / 特定文件 / 特定历史数据”，都要继续补。

---

## P2 · 提升公开仓库可用性

### 7. 补公开版部署文件
建议至少补这些：

- [ ] `README.md`
- [ ] `PUBLIC-RELEASE-CHECKLIST.md`
- [ ] `LICENSE`
- [ ] `docs/FINAL-RELEASE-REVIEW.md`
- [ ] `docs/FIRST-PUBLIC-RELEASE-PLAN.md`
- [ ] `.env.example`（如决定采用环境变量）
- [ ] `scripts/start.sh` 的公开模板版
- [ ] `scripts/okx-trading.service` 的模板版

### 8. Dashboard / Web 安全项
- [ ] 如继续保留 legacy `dashboard/app.py`，将其中开发态默认 `SECRET_KEY` 改成只允许环境变量注入或明确标注仅限本地开发
- [ ] 如果允许远程访问 Dashboard，补充最小安全说明
- [ ] 说明是否建议只内网访问

### 9. 依赖与复现稳定性
- [ ] 评估是否补 `requirements-lock.txt` 或固定版本范围
- [ ] 检查 sklearn 模型文件与当前依赖是否匹配
- [ ] 如果模型必须重新训练，要在 README 里写清楚

### 10. 测试与验收
- [ ] 跑一次已有测试
- [ ] 做一次从零环境安装验证
- [ ] 做一次 testnet 开平仓 smoke 验收
- [ ] 做一次通知链路验收
- [ ] 做一次 Dashboard 验收

---

## P3 · 仓库发布策略

### 推荐方案：新建 public repo
- [ ] 保留当前仓库为私有运行仓库
- [ ] 新建独立 public repo
- [ ] 只挑选适合公开的代码、配置样例、文档、测试
- [ ] 不直接把现有私有仓库改公开

### 为什么不建议直接公开当前仓库
- [ ] 当前仓库带有明显私人运行态痕迹
- [ ] 本地绝对路径较多
- [ ] 运行日志 / 状态文件 / 备份配置风险较高
- [ ] Git 历史可能含敏感信息

---

## 发布前最终验收清单

### 对外安装体验
- [ ] 一个全新用户按照 README 可以走通
- [ ] README 没有依赖你私下补充说明
- [ ] 不需要知道你的本地目录结构
- [ ] 不需要手动修脚本才能启动主流程

### 对外安全体验
- [ ] 不包含真实 token / secret / webhook
- [ ] 不包含你的本地路径与私人运维信息
- [ ] 不包含本地数据库、日志、备份、运行时状态

### 对外定位
- [ ] README 明确说明这是学习 / 研究 / 技术交流项目
- [ ] README 明确要求先 testnet
- [ ] README 明确不承诺盈利
- [ ] README 明确适合人群与门槛

---

## 建议的下一步执行顺序

1. [ ] 先处理敏感信息与历史排查
2. [ ] 再修绝对路径与脚本模板
3. [ ] 再做从零环境部署验证
4. [ ] 最后才建公开仓库并发布

---

## 如果你想把它做成真正“朋友一键上手”

建议下一轮继续补：

- [ ] `bootstrap.sh`：自动创建 `.venv`、安装依赖、复制配置模板
- [ ] `.env.example`：统一环境变量配置方式
- [ ] `scripts/start.public.sh`：不依赖你本机路径的公开版启动脚本
- [ ] `scripts/doctor.py`：一键检查 Python、依赖、配置、目录、API 连接
- [ ] `docs/DEPLOYMENT.md`：单独写完整部署说明

做到这一步，公开版先算真正“像个可分享项目”，唔系只系“把私人仓库掀开盖”。
