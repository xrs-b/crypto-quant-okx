# DEPLOYMENT

这份文档面向“朋友下载后能自己部署”的公开版场景。

## 1. 准备环境

- macOS 或 Linux
- Python 3.9+
- Git
- OKX testnet API（强烈建议先用模拟盘）

可选但推荐：
- `python-dotenv` / direnv / shell profile，用来加载 `.env`
- `tmux` / `screen` / systemd / launchd，用于守护进程

## 2. 克隆项目

```bash
git clone <your-repo-url>
cd crypto-quant-okx
```

## 3. 创建虚拟环境并安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 4. 准备配置文件

```bash
cp .env.example .env
cp config/config.yaml.example config/config.yaml
cp config/config.local.yaml.example config/config.local.yaml
```

建议分层：

- `.env`：环境变量与 secret
- `config/config.yaml`：公开参数、策略参数、风控参数
- `config/config.local.yaml`：本机私密覆盖（如你不想把 secret 放进 `.env`）

## 5. 填入最小必要配置

### 方案 A：推荐，用 `.env`

至少填这些：

```dotenv
PROJECT_DIR=/absolute/path/to/crypto-quant-okx
OKX_API_KEY=your_okx_api_key
OKX_API_SECRET=your_okx_api_secret
OKX_API_PASSPHRASE=your_okx_api_passphrase
DASHBOARD_SECRET_KEY=replace_with_a_long_random_secret
DISCORD_BOT_TOKEN=
DISCORD_CHANNEL_ID=
```

然后在当前 shell 载入：

```bash
set -a
source .env
set +a
```

### 方案 B：把 secret 放进 `config/config.local.yaml`

如果你不想依赖环境变量，也可以只改 `config/config.local.yaml`：

```yaml
api:
  key: your_real_or_testnet_key
  secret: your_real_or_testnet_secret
  passphrase: your_real_or_testnet_passphrase
```

Dashboard 建议仍走环境变量：

```bash
export DASHBOARD_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

## 6. 确认交易模式为 testnet

检查 `config/config.yaml`：

```yaml
exchange:
  mode: testnet
```

在第一次部署时，不要急着切 `real`。

## 7. 基础只读验证

### 7.1 配置与导入检查

```bash
.venv/bin/python3 -m py_compile bot/run.py core/*.py dashboard/*.py scripts/*.py
```

### 7.2 交易所只读诊断

```bash
PROJECT_DIR="$PWD" .venv/bin/python3 bot/run.py --exchange-diagnose
```

你应该重点确认：
- 能否正常读取余额
- 目标币种是不是 U 本位永续
- `position_mode` 是否与你 OKX 账户一致

### 7.3 通知链路测试（可选）

```bash
PROJECT_DIR="$PWD" .venv/bin/python3 bot/run.py --notify-test
```

## 8. 生成最小 testnet 验收计划

```bash
PROJECT_DIR="$PWD" .venv/bin/python3 bot/run.py --exchange-smoke --symbol BTC/USDT --side long
```

建议确认输出里有：
- `exchange_mode: testnet`
- `execute_ready: True`
- 开仓/平仓参数预览正常

## 9. 执行最小 testnet 开平仓验收

> 这一步会真的在 testnet 下单。

```bash
PROJECT_DIR="$PWD" .venv/bin/python3 bot/run.py --exchange-smoke --symbol BTC/USDT --side long --execute
```

验收后请核对：
- 终端输出显示开仓、平仓都成功
- `data/trading.db` 有记录
- `data/runtime_state.json` 状态合理
- Dashboard 能看到对应变化（若已启动）
- 交易所 testnet 页面也能看到对应成交/持仓变化

## 10. 启动方式

### 方式 A：前台直接跑

```bash
PROJECT_DIR="$PWD" .venv/bin/python3 bot/run.py
```

### 方式 B：守护模式

```bash
PROJECT_DIR="$PWD" .venv/bin/python3 bot/run.py --daemon
```

### 方式 C：用通用脚本

```bash
chmod +x scripts/start.sh scripts/start.public.sh scripts/keep_dashboard_alive.sh
PROJECT_DIR="$PWD" scripts/start.public.sh start
PROJECT_DIR="$PWD" scripts/start.public.sh dashboard
PROJECT_DIR="$PWD" scripts/start.public.sh status
```

## 11. 启动 Dashboard

```bash
export DASHBOARD_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
PROJECT_DIR="$PWD" .venv/bin/flask --app dashboard.api:app run --host 0.0.0.0 --port 5555
```

或：

```bash
PROJECT_DIR="$PWD" scripts/start.public.sh dashboard
```

默认访问：

- `http://127.0.0.1:5555`
- 局域网部署时可换成你的主机 IP

## 12. 定时任务 / 保活

### cron 示例

参考：`scripts/candidate-review.cron.example`

核心原则：
- 不要写死作者机器路径
- 用 `PROJECT_DIR=/你的路径`
- 优先用项目虚拟环境 `.venv/bin/python3`
- 把仓库里的 `*.service` / `*.plist` / `*.cron.example` 当作模板，而不是可直接照搬的作者本机配置

更多公开版保留/排除规则见：`PUBLIC-REPO-MANIFEST.md`

### Dashboard 保活

```bash
PROJECT_DIR="$PWD" scripts/keep_dashboard_alive.sh
```

## 13. 切到真实盘前的清单

只有以下都通过，先考虑 `real`：

- [ ] `--exchange-diagnose` 正常
- [ ] `--notify-test` 正常（如果你启用了通知）
- [ ] `--exchange-smoke --execute` 在 testnet 成功
- [ ] Dashboard 可访问
- [ ] logs / db / runtime_state 能对上
- [ ] 你确认 API 权限、IP 白名单、持仓模式都正确
- [ ] 你理解杠杆和自动交易风险

## 14. 常见坑

### Dashboard SECRET_KEY 忘了配置

如果你看到日志提示：

- `DASHBOARD_SECRET_KEY 未设置`

说明当前仍在用仅适合本地开发的默认值。公开部署前一定要换掉。

### 脚本报找不到项目路径

优先这样运行：

```bash
PROJECT_DIR="$PWD" scripts/start.public.sh start
```

### 用了系统 Python，结果缺包

请改用：

```bash
.venv/bin/python3 ...
```

## 15. 推荐分享给朋友的最小步骤

如果你要把项目发给朋友，最短可复制版本可以是：

1. `git clone ...`
2. `python3 -m venv .venv`
3. `source .venv/bin/activate`
4. `pip install -r requirements.txt`
5. `cp .env.example .env`
6. `cp config/config.yaml.example config/config.yaml`
7. 填好 testnet API
8. `set -a && source .env && set +a`
9. `PROJECT_DIR="$PWD" .venv/bin/python3 bot/run.py --exchange-diagnose`
10. `PROJECT_DIR="$PWD" .venv/bin/python3 bot/run.py --exchange-smoke --symbol BTC/USDT --side long`
11. `PROJECT_DIR="$PWD" scripts/start.public.sh dashboard`
