# FIRST PUBLIC RELEASE PLAN

面向 `crypto-quant-okx` 首次公开版发布（建议 `v0.1.0`）。

---

## 1. 建议仓库名

优先建议：

- `crypto-quant-okx`

可选备选：

- `okx-quant-trading-kit`
- `okx-quant-bot-starter`

选择理由：
- 现有目录名与 README 已围绕 `crypto-quant-okx` 建立，迁移成本最低
- 名称直观表达“crypto + quant + OKX”定位
- 不会误导成“傻瓜式自动印钞 bot”

---

## 2. 建议首版定位（v0.1.0）

`v0.1.0` 应定位为：

> 一个可供朋友 clone、按文档完成 testnet 部署、理解系统结构、继续自行改造的 **首个可公开分享版本**。

不是：
- 一键实盘工具
- 盈利承诺产品
- 已充分抽象和长期稳定的通用框架

---

## 3. v0.1.0 建议包含范围

### 应包含
- 核心交易系统代码
- Flask Dashboard
- 示例配置：`.env.example`、`config/config.yaml.example`、`config/config.local.yaml.example`
- 精简后的 public presets：
  - `btc-focused.yaml`
  - `safe-mode.yaml`
- 公开部署文档、发布清单、仓库边界说明
- 通用启动/保活/模板化运维脚本
- 基础测试
- `LICENSE`

### 不应包含
- 真实凭证、真实 webhook、真实 API 配置
- 本地数据库、runtime state、日志、PID
- `config/config.yaml` / `config/config.local.yaml`
- `config/backups/`
- 作者机器专属路径与临时脚本
- 更偏内部实验/候选筛选的 preset（例如带明显 candidate / symbol bias 的预设）
- 任何会让朋友误以为“直接切 real 就安全”的默认配置

---

## 4. 为什么首版只保留两个 preset

建议首版只保留：
- `btc-focused`
- `safe-mode`

原因：
1. **选择更少，误配更少**：朋友第一次接触时，两个 preset 已足够表达“标准版 / 更保守版”。
2. **语义更清晰**：`candidate` 类命名容易让人误会成作者已经验证过的推荐 alpha。
3. **维护成本更低**：首版公开后，作者要解释每个 preset 的适用场景；过多 preset 会放大文档与支持负担。
4. **更符合 testnet-first 分享**：先提供最稳妥的公共起点，而不是把候选实验一起公开。

---

## 5. 建议发布步骤

1. 完成最终 secrets / history / ignore 复核
2. 跑基础语法检查与必要测试
3. 核对 README、DEPLOYMENT、MANIFEST、CHECKLIST、LICENSE 一致
4. 确认公开版只保留 `btc-focused` + `safe-mode`
5. 提交一个“public release prep”收尾 commit
6. 推到新的 public repo
7. 打 tag：`v0.1.0`
8. 编写 GitHub Release Notes
9. 用公开仓库地址重新走一次 README 最短部署链路

---

## 6. 建议 release notes 骨架

标题：

`v0.1.0 - first public release`

建议正文结构：

### What’s included
- Core OKX quant trading workflow
- Signal / validation / risk-control pipeline
- Layering support
- Dashboard
- Public deployment docs
- Example configs and two beginner-friendly presets

### What changed for the public release
- Added open-source license
- Simplified public presets
- Added final review and release planning docs
- Cleaned up public-repo guidance

### Recommended first steps
1. Clone the repo
2. Set up `.venv`
3. Copy `.env.example` and `config/*.example`
4. Fill in **testnet** credentials only
5. Run `--exchange-diagnose`
6. Run `--exchange-smoke`
7. Start Dashboard on port `5555`

### Important warning
- This project is for learning / research / technical sharing
- Testnet first
- No profit guarantee
- Real trading remains the user’s own responsibility

---

## 7. 发布公告骨架

可用于 GitHub / Discord / Telegram / 朋友圈技术说明的短文案：

> 我把自己这套面向 OKX U 本位合约的量化交易项目整理成了一个可公开分享的版本：`crypto-quant-okx`。
> 
> 这次首版重点不是“神奇策略”，而是把一套相对工程化的交易系统整理到朋友可以自己 clone、自己配 testnet、自己跑通 Dashboard 和基础验收的程度。
> 
> 首版包含：信号检测、进场审批、风控、layering、对账、通知、Dashboard、示例配置、部署文档，以及两个更稳妥的 public presets。
> 
> 也顺手把公开边界讲清楚：默认先走 testnet，不承诺盈利，不包含任何真实运行态或私密配置。
> 
> 如果你本身会 Python / YAML / API 配置，想拿来研究、学习、继续改造，欢迎看看。实盘请一定自己评估风险。

---

## 8. 首版之后建议的下一步

`v0.1.x` 可继续补：
- bootstrap / doctor 脚本
- 更清晰的 preset 生成与说明机制
- 更细的测试覆盖
- public-only 示例数据与截图
- 更完整的 Dashboard 安全说明

先把首版做成“稳妥、诚实、能装起来”的分享仓库，比一开始就塞满功能更重要。
