# crypto-quant-okx

一个面向 **OKX U 本位合约** 的量化交易系统，支持 **信号检测、进场审批、风险控制、分层开仓（layering）、持仓对账、通知推送、Dashboard 观察、回测与参数分析**。

> 推荐公开仓库名：**`crypto-quant-okx`**
>
> GitHub short description：**An OKX-focused quant trading system for research, testnet deployment, and operator-supervised automation.**

许可证：**MIT**。选择 MIT 的原因很简单：对朋友分享和二次改造阻力最低、保留署名要求、同时配合 MIT 的免责条款，比较符合这个“学习 / 研究 / 自行承担交易风险”的公开版定位。

> 这个项目更适合有一定 Python / 量化 / 交易所 API 使用经验的开发者或朋友交流学习。  
> **不建议零基础用户直接上实盘。**

## Public mainline policy

- **公共 GitHub 仓库是唯一长期代码主线**
- **私人环境只保留运行态与本地覆盖**：例如 `.env`、`config/config.local.yaml`、`logs/`、`data/trading.db`、`data/runtime_state.json`
- 不在公共仓库长期保留真实运行态、真实凭证与私人机器痕迹

相关文档：

- [`docs/PUBLIC-MAINLINE-WORKFLOW.md`](docs/PUBLIC-MAINLINE-WORKFLOW.md)
- [`docs/GITHUB-PUBLIC-REPO-COPY.md`](docs/GITHUB-PUBLIC-REPO-COPY.md)
- [`docs/adaptive-market-regime-framework-plan.md`](docs/adaptive-market-regime-framework-plan.md)
- [`docs/adaptive-market-regime-backlog.md`](docs/adaptive-market-regime-backlog.md)
- [`docs/adaptive-strategy-validation-entry-plan.md`](docs/adaptive-strategy-validation-entry-plan.md)
- [`docs/adaptive-market-regime-m3-boundary-plan.md`](docs/adaptive-market-regime-m3-boundary-plan.md)
- [`docs/adaptive-market-regime-m4-boundary-plan.md`](docs/adaptive-market-regime-m4-boundary-plan.md)
- [`docs/adaptive-market-regime-m4-step1-implementation.md`](docs/adaptive-market-regime-m4-step1-implementation.md)
- [`docs/adaptive-market-regime-m4-step2-implementation.md`](docs/adaptive-market-regime-m4-step2-implementation.md)
- [`docs/adaptive-market-regime-m4-step3-implementation.md`](docs/adaptive-market-regime-m4-step3-implementation.md)
- [`docs/adaptive-market-regime-m4-step4-implementation.md`](docs/adaptive-market-regime-m4-step4-implementation.md)
- [`docs/adaptive-market-regime-m4-step4-batch2-implementation.md`](docs/adaptive-market-regime-m4-step4-batch2-implementation.md)

M4 Step 3 status: guarded layering profile 第一批已落地，当前 live 只放行 conservative guardrail-like layering 字段；`layer_ratios` 继续 hints-only，除非显式开启 `layering_plan_shape_enforcement_enabled`。

M4 Step 4 planning status: 已新增 plan-shape / `layer_ratios` guarded live 实施拆分；原则是先补齐 baseline/effective/live shape audit，再通过独立开关小范围放开 `layer_ratios`，并保持 `layer_count` 仅作为 derived 审计字段。

M4 Step 4 batch2 status: 已补第二批可直接开工实施稿，明确区分 hints / guardrails live / plan shape live，并把 `layer_ratios`、`layer_count` 与 `layer_max_total_ratio`、`max_layers_per_signal`、`min_add_interval_seconds`、`profit_only_add` 的约束关系、灰度、失败信号和回滚粒度写清楚。
- [`docs/adaptive-market-regime-m3-step1-implementation.md`](docs/adaptive-market-regime-m3-step1-implementation.md)
- [`docs/adaptive-market-regime-m0-implementation.md`](docs/adaptive-market-regime-m0-implementation.md)
- [`PUBLIC-REPO-MANIFEST.md`](PUBLIC-REPO-MANIFEST.md)

---

## ⚠️ 风险提示

**合约交易风险极高，可能亏损全部本金。**

在使用本项目之前，请先接受以下事实：

- 自动交易系统不保证盈利
- 交易所 API、网络、配置错误都可能导致异常行为
- 杠杆会同时放大收益和亏损
- 任何实盘操作都应先在 **testnet / 模拟环境** 做完整验收

**强烈建议先用 testnet 跑通以下链路：**

> 公开部署完整步骤见：[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)

1. 读取余额
2. 读取合约信息
3. 通知测试
4. 生成 smoke plan
5. 执行最小 testnet 开平仓验收
6. 观察日志、Dashboard、数据库是否一致

---

## 项目简介

`crypto-quant-okx` 不是单纯的“自动下单脚本”，而是一套偏工程化的交易系统，核心目标是：

- 自动检测候选交易信号
- 用验证层与进场审批层过滤低质量机会
- 用风险预算与仓位限制控制暴露
- 支持分层开仓（layering）
- 通过本地数据库、日志、Dashboard 和通知系统提升可观察性
- 新增 workflow attention API（manual approval / blocked follow-up），便于 dashboard、agent、人工低干预巡检直接拉取待处理项
- 新增 merged timeline 后端视图，可直接查看同一 item 的 approval DB timeline + workflow/executor timeline 合并结果，方便 dashboard、agent、人工巡检统一追踪
- 为回测、信号质量分析、参数优化预留接口

当前项目围绕 **OKX 合约** 场景设计，默认支持：

- `testnet`：模拟盘
- `real`：真实盘

---

## 适合谁 / 不适合谁

### 适合
- 想自己跑 OKX 合约 testnet / real 的开发者
- 会改 YAML 配置、能看日志、能接受策略需要持续观察的人
- 想保留“可解释、可监督、可回滚”运行方式的人
- 想通过 OpenClaw 作为运维入口管理量化系统的人

### 不太适合
- 希望“下载即赚钱”的用户
- 不理解杠杆 / 风控 / API 权限风险的人
- 不愿意先做 testnet 验证的人

---

## 功能特性

### 1) 信号与进场审批
- 多指标信号检测
- 信号验证与过滤
- Entry Decision 进场审批层
- 支持按币种局部覆盖参数（`symbol_overrides`）

### 2) 风险控制
- 总暴露上限控制
- 单币种仓位上限
- 连续亏损熔断
- 冷却时间控制
- 固定止损 / 止盈 / 追踪止损

### 3) 分层开仓（Layering）
- 多层计划仓位
- 同方向锁（direction lock）
- 信号幂等控制（signal idempotency）
- 同周期重复加仓限制
- 平仓后 layer state 自动 reset

### 4) 对账与自愈
- 交易所持仓、本地 positions、本地 open trades 三方对账
- 自动补建缺失 open trade
- 自动清理 orphan intents / locks
- 自动同步 layer plan state

### 5) 可观察性
- Flask Dashboard
- Discord / Telegram / Email 通知能力
- 运行状态记录
- 信号过滤原因可追踪
- 分仓验收辅助脚本

### 6) 研究与分析
- 历史数据收集
- ML 模型训练
- 回测
- 信号质量分析
- 参数优化与候选币审查

---

## 系统要求

### 基础要求
- Python **3.9+**
- macOS 或 Linux
- OKX API（建议先用 testnet）
- 建议使用项目虚拟环境 `.venv`

### 主要依赖
见 `requirements.txt`：

- `ccxt`
- `pandas`
- `pandas-ta`
- `pyyaml`
- `requests`
- `flask`
- `flask-cors`
- `scikit-learn`
- `joblib`

---

## 快速开始

### 1. 克隆项目

```bash
git clone <your-repo-url>
cd crypto-quant-okx
```

### 2. 创建虚拟环境并安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 准备配置文件

```bash
cp .env.example .env
cp config/config.yaml.example config/config.yaml
cp config/config.local.yaml.example config/config.local.yaml
```

### 4. 编辑配置

建议分层管理：

- `config/config.yaml`：公开参数、策略参数、风控参数、watch list
- `config/presets/*.yaml`：只放可公开的策略/开关，不放 Discord / Telegram secret
- `config/config.local.yaml`：默认且唯一推荐的本地私密配置来源，只放 API Key / webhook / bot token / chat id / channel id

> 当前默认加载顺序：`config/config.yaml` → `config/config.local.yaml` → 环境变量占位符解析。
>
> `~/.crypto-quant-okx.local.yaml` 已降级为**兼容后门**，默认**不会自动加载**；只有显式设置以下任一环境变量才会启用：
>
> - `CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL=1`
> - `CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG=/absolute/path/to/legacy-home-local.yaml`

如果你喜欢走环境变量，也可以直接用：

```yaml
api:
  key: ${OKX_API_KEY}
  secret: ${OKX_API_SECRET}
  passphrase: ${OKX_API_PASSPHRASE}

notification:
  discord:
    bot_token: ${DISCORD_BOT_TOKEN:-}
    channel_id: ${DISCORD_CHANNEL_ID:-}

# 注意：preset 不要写 bot_token / webhook / channel_id / chat_id
# 这些 secret 统一放 config.local.yaml 或 .env
```

建议同时准备 `.env`：

```dotenv
PROJECT_DIR=/absolute/path/to/crypto-quant-okx
DASHBOARD_SECRET_KEY=replace_with_a_long_random_secret
OKX_API_KEY=your_okx_api_key
OKX_API_SECRET=your_okx_api_secret
OKX_API_PASSPHRASE=your_okx_api_passphrase
```

### 5. 先跑只读检查

```bash
python3 bot/run.py --exchange-diagnose
python3 bot/run.py --notify-test
```

### 6. 生成最小 testnet 验收计划

```bash
python3 bot/run.py --exchange-smoke --symbol BTC/USDT --side long
```

### 7. 显式执行 testnet 最小开平仓验收（会下 testnet 单）

```bash
python3 bot/run.py --exchange-smoke --symbol BTC/USDT --side long --execute
```

### 8. 运行交易主程序

```bash
python3 bot/run.py
```

### 9. 启动 Dashboard

```bash
python3 bot/run.py --dashboard --port 5555
```

或：

```bash
./.venv/bin/flask --app dashboard.api:app run --host 0.0.0.0 --port 5555
```

---

## OpenClaw 接入 / 安装思路

如果你想把这套系统接到 OpenClaw，建议把它当成一个“本地可控服务”来接，而不是一上来做高风险自动化。

### 推荐接法

1. **先确保本地项目能独立跑通**
   - `bot/run.py --exchange-diagnose`
   - `bot/run.py --notify-test`
   - `bot/run.py --daemon`
   - Dashboard 可以正常打开

2. **把 OpenClaw 当成运维入口**
   - 查看日志
   - 触发只读诊断
   - 触发 testnet smoke
   - 查看分仓状态报告
   - 读取当前配置与运行状态

3. **适合通过 OpenClaw 调用的命令**

```bash
python3 bot/run.py --exchange-diagnose
python3 bot/run.py --notify-test
python3 bot/run.py --reconcile-positions
python3 scripts/layering_state_report.py
```

4. **不建议默认直接开放实盘高风险操作**
   - 实盘开关、API 密钥、真实资金操作建议保留人工确认
   - 最好先在 testnet 验证完整链路

---

## 传统本地部署

### 方式 A：前台运行

```bash
python3 bot/run.py
```

### 方式 B：守护模式

```bash
python3 bot/run.py --daemon
```

或：

```bash
PROJECT_DIR="$PWD" scripts/start.public.sh start
```

### 方式 C：启动 Dashboard

```bash
python3 bot/run.py --dashboard --port 5555
```

或：

```bash
PROJECT_DIR="$PWD" scripts/start.public.sh dashboard
```

### 方式 D：通知 relay

```bash
python3 bot/run.py --relay-outbox --once
```

### 辅助脚本说明

项目中包含一些运维脚本：

- `scripts/start.sh`
- `scripts/start.public.sh`
- `scripts/keep_dashboard_alive.sh`
- `scripts/okx-trading.service`
- `scripts/com.oink.crypto-quant-okx.dashboard-keepalive.plist`
- `scripts/candidate-review.cron.example`

说明：
- `start.sh` / `start.public.sh` / `keep_dashboard_alive.sh` 可直接作为通用脚本参考
- `okx-trading.service`、`*.plist`、`*.cron.example` 属于 **模板文件**，公开仓库会保留，但你需要改成自己机器的用户、路径、端口与调度方式
- 公开仓库保留/排除规则见：[`PUBLIC-REPO-MANIFEST.md`](PUBLIC-REPO-MANIFEST.md)

这些脚本已经支持：

- 自动从脚本位置推导项目根目录
- 或通过 `PROJECT_DIR=/your/path` 覆盖
- 优先使用项目 `.venv`

朋友直接部署时，推荐这样调用：

```bash
PROJECT_DIR="$PWD" scripts/start.public.sh start
PROJECT_DIR="$PWD" scripts/start.public.sh dashboard
```

---

## 配置说明

### 配置文件分层

| 文件 | 用途 |
|---|---|
| `config/config.yaml.example` | 可公开提交的完整示例 |
| `config/config.local.yaml.example` | 私密配置模板 |
| `config/config.yaml` | 本地主配置 |
| `config/config.local.yaml` | 默认推荐的本地私密覆盖 |
| `~/.crypto-quant-okx.local.yaml` | 旧兼容入口；仅在显式环境变量开启时才加载 |

### 适合放在 `config.yaml` 的内容
- `exchange.mode`
- `exchange.position_mode`
- `symbols.watch_list`
- `symbols.candidate_watch_list`
- `trading.*`
- `strategies.*`
- `market_filters.*`
- `governance.*`

### 适合放在 `config.local.yaml` 的内容
- `api.key`
- `api.secret`
- `api.passphrase`
- `notification.discord.bot_token`
- `notification.discord.webhook_url`
- `notification.discord.channel_id`
- 任何 Email / Telegram 等真实凭证

### 旧的 home 覆盖文件策略

如果你历史上一直把 secret 放在 `~/.crypto-quant-okx.local.yaml`：

1. **现在推荐迁移到** `config/config.local.yaml`
2. 默认情况下，程序**不会再自动读取** home 文件，避免和项目内 `config/config.local.yaml` 双重覆盖造成混乱
3. 如你暂时未迁移，可显式启用兼容模式：

```bash
export CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL=1
# 或显式指定路径
export CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG="$HOME/.crypto-quant-okx.local.yaml"
```

建议只把这种兼容模式当作短期迁移手段，而不是长期主路径。

### Layering 默认示例
### Public presets（公开版只保留两个）

为了让朋友第一次上手时更不容易误配，公开仓库只保留两个 preset：

- `btc-focused.yaml`：默认示例，单一主观察币种，适合作为 README / testnet 演练起点
- `safe-mode.yaml`：更保守的降级示例，适合先验证链路、降低第一次误操作风险

这次**不再公开** `xrp-candidate`、`btc-grid-candidate` 一类候选 preset，原因是它们更像作者阶段性实验/候选方向，朋友拿到后容易误会成“现成推荐配置”。首版公开先把选择收窄，后续如真有长期维护价值，再单独补回。

更多发布边界与首版计划见：
- `docs/FINAL-RELEASE-REVIEW.md`
- `docs/FIRST-PUBLIC-RELEASE-PLAN.md`

来自 `config/config.yaml.example`：

```yaml
trading:
  layering:
    layer_count: 3
    layer_ratios: [0.06, 0.06, 0.04]
    layer_max_total_ratio: 0.16
    min_add_interval_seconds: 300
    signal_idempotency_enabled: true
    direction_lock_enabled: true
    direction_lock_scope: symbol
    allow_same_bar_multiple_adds: false
    max_layers_per_signal: 1
```

### 切换到真实交易

```yaml
exchange:
  mode: real

api:
  key: your_real_okx_api_key
  secret: your_real_okx_api_secret
  passphrase: your_real_okx_api_passphrase
```

**强烈建议：先用 `testnet` 跑通，再切 `real`。**

---

## 常用运行命令

### 交易主流程

```bash
python3 bot/run.py
```

### 守护模式

```bash
python3 bot/run.py --daemon
```

### 训练模型

```bash
python3 bot/run.py --collect
python3 bot/run.py --train
```

### 回测 / 分析

```bash
python3 bot/run.py --backtest
python3 bot/run.py --signal-quality
python3 bot/run.py --optimize
```

### 只读诊断

```bash
python3 bot/run.py --exchange-diagnose
python3 bot/run.py --reconcile-positions
```

### 通知与 relay

```bash
python3 bot/run.py --notify-test
python3 bot/run.py --relay-outbox --once
python3 bot/run.py --approval-hygiene
```

---

## 日志与排障

### 常见目录
- `logs/`
- `data/trading.db`
- `data/runtime_state.json`

### 常见问题

#### 1. 缺少依赖
如果手工运行时报缺包，请确认你使用的是项目虚拟环境，而不是系统 Python。

#### 2. Dashboard 端口
公开版默认统一使用 `5555`。如果你需要改端口，请同时修改配置里的 `dashboard.port`，或在脚本启动时覆盖 `DASHBOARD_PORT`，避免文档、脚本、运行配置三边不一致。

#### 3. 模型版本 warning
如果看到 sklearn 相关 warning，通常说明本地依赖版本与历史模型文件版本不完全一致。可考虑重新训练模型。

#### 4. 对账异常
可先执行：

```bash
python3 bot/run.py --reconcile-positions
```

#### 5. 查看当前分仓状态

```bash
python3 scripts/layering_state_report.py
```

---

## 目录结构

```text
crypto-quant-okx/
├── analytics/           # 回测、优化、治理分析
├── bot/                 # 主入口与运行控制
├── config/              # 配置、样例、预设
├── core/                # 配置、交易所、数据库、通知等核心模块
├── dashboard/           # Flask Dashboard
├── data/                # 本地数据库与运行状态
├── docs/                # 使用说明、验收清单、上下文文档
├── logs/                # 运行日志
├── ml/                  # 数据收集、训练、模型文件
├── scripts/             # 启动、守护、巡检脚本
├── signals/             # 信号检测、验证、进场审批
├── strategies/          # 策略库
├── tests/               # 测试
├── trading/             # 执行器、风控、仓位管理
└── README.md
```

---

## 部署建议

如果你是准备分享给朋友，请按以下顺序做：

1. 以 **公共仓库作为唯一代码主线** 继续维护代码、文档、测试与模板脚本
2. 私人环境只承载真实交易运行态与本地覆盖，不再把它当成长期平行代码主线
3. 公共仓库只保留：
   - 核心代码
   - 示例配置
   - 脱敏后的脚本
   - 精简文档
   - 必要测试
4. 不要把以下内容放进公共仓库：
   - 真实 API Key / Secret / Passphrase
   - 真实 Discord / Telegram token
   - `.env`
   - `config/config.yaml`
   - `config/config.local.yaml`
   - 运行日志
   - 本地数据库
   - 运行时状态文件
   - 个人运维脚本中的绝对路径

详细工作约定见：[`docs/PUBLIC-MAINLINE-WORKFLOW.md`](docs/PUBLIC-MAINLINE-WORKFLOW.md)

---

## 免责声明

本项目仅供学习、研究与技术交流使用，**不构成任何投资建议，也不保证盈利**。  
使用者应自行评估代码、配置、交易风险与法律合规责任。  
因使用本项目造成的任何损失，项目作者与贡献者不承担责任。
