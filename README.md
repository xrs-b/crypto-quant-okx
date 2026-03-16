# OKX 合约量化交易机器人

[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

基于机器学习的 OKX 合约自动交易系统，支持做多/做空、智能止盈止损、追踪止损等功能。

## ⚠️ 风险提示

> **重要**：合约交易风险极高，可能亏损全部资金！
> - 请先用模拟盘 (testnet) 充分测试
> - 请设置合理的止损止盈比例
> - 不要投入超过承受能力的资金
> - 本项目仅供学习交流，不构成投资建议

## ✨ 功能特性

### 核心交易
- 🤖 **ML模型预测** - Random Forest 模型预测涨跌概率
- 📈 **多空双向** - 支持做多 (Long) 和做空 (Short)
- 💰 **仓位管理** - 最大30%仓位，单笔10%
- ⚡ **杠杆交易** - 支持1-50倍杠杆

### 风险管理
- 🛡️ **固定止损** - 按杠杆计算亏损比例
- 🎯 **固定止盈** - 按杠杆计算盈利比例
- 📊 **分批止盈** - 50%仓位4%止盈，50%追踪止损
- 🔄 **追踪止损** - 价格回调自动平仓，持久化存储

### 信号系统
- 📉 **RSI指标** - 14周期，相对强弱指数
- 📊 **MACD指标** - 12/26/9，指数平滑异同移动平均线
- 🎯 **信号强度** - 显示置信度百分比
- 🤖 **ML确认** - 需要 ML 高概率确认才交易

### 其他
- 📝 **交易日志** - 记录所有交易历史
- 🔔 **Discord推送** - 实时推送交易信号
- ⚙️ **配置驱动** - 所有参数从 config.yaml 读取

## 🏗️ 技术栈

| 技术 | 说明 |
|------|------|
| Python 3.9+ | 编程语言 |
| ccxt | 交易所 API 封装 |
| scikit-learn | ML 模型训练 |
| pandas | 数据处理 |
| PyYAML | 配置文件解析 |

## 📁 目录结构

```
crypto-quant-okx/
├── bot/
│   ├── main.py                # 主入口（旧版兼容）
│   └── run.py                 # 当前主运行入口 / CLI / dashboard
├── ml/
│   ├── train_model.py         # 训练模型
│   ├── simple_model.py        # 简单模型
│   ├── collect_data.py        # 收集历史数据
│   └── *_model.pkl           # 训练好的模型
├── strategies/
│   └── *.py                  # 交易策略
├── data/
│   └── *.py                  # 数据处理
├── config/
│   ├── config.yaml            # 配置文件 (本地)
│   └── config.yaml.example   # 配置模板
├── README.md                  # 项目说明
└── requirements.txt           # 依赖
```

## 🚀 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/xrs-b/crypto-quant-okx.git
cd crypto-quant-okx
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置

```bash
# 复制配置文件
cp config/config.yaml.example config/config.yaml

# 编辑配置文件
vim config/config.yaml
```

### 4. 运行

```bash
# 手动运行（当前主入口）
python3 bot/run.py

# 只读诊断交易所/合约参数（不会下单）
python3 bot/run.py --exchange-diagnose

# 生成最小 testnet 验收计划（默认只预演，不会下单）
python3 bot/run.py --exchange-smoke --symbol BTC/USDT --side long

# 显式执行 testnet 最小开平仓验收（会下 testnet 单）
python3 bot/run.py --exchange-smoke --symbol BTC/USDT --side long --execute

# 启动 dashboard
python3 bot/run.py --dashboard --port 8050
```

## ⚙️ 配置说明

### 完整配置项

```yaml
# =============================================================================
# 交易所配置
# =============================================================================
exchange: okx                     # 交易所名称

# 交易模式: testnet (模拟) / real (真实)
mode: testnet

# 持仓模式: oneway(单向) / hedge(双向)
# 必须与 OKX 账户设置一致，否则容易触发 posSide / 平仓方向错误
position_mode: oneway

# -----------------------------------------------------------------------------
# API密钥配置 (必须)
# -----------------------------------------------------------------------------
api:
  key: "你的API Key"              # 登录OKX → API管理 → 创建API
  secret: "你的Secret"
  passphrase: "你的Passphrase"

# -----------------------------------------------------------------------------
# 交易参数
# -----------------------------------------------------------------------------
trading:
  # 交易对列表
  symbols:
    - SOL/USDT
    - HYPE/USDT

  # 单笔仓位比例 (0.1 = 10%)
  position_size: 0.1

  # 最大仓位比例 (0.3 = 30%)
  max_exposure: 0.3

  # 杠杆倍数 (1-50)
  leverage: 3

  # 止损比例 (0.02 = 2%，按杠杆计算)
  stop_loss: 0.02

  # 止盈比例 (0.04 = 4%，按杠杆计算)
  take_profit: 0.04

  # 追踪止损比例 (0.02 = 2%)
  trailing_stop: 0.02

# -----------------------------------------------------------------------------
# 策略参数
# -----------------------------------------------------------------------------
strategy:
  rsi_period: 14                  # RSI周期
  rsi_oversold: 35               # RSI超卖阈值
  rsi_overbought: 65              # RSI超买阈值
  macd_fast: 12                   # MACD快线
  macd_slow: 26                   # MACD慢线
  macd_signal: 9                   # MACD信号线

# -----------------------------------------------------------------------------
# Discord通知
# -----------------------------------------------------------------------------
discord:
  enabled: true
  channel_id: "你的Channel ID"
```

### 切换到真实交易

```yaml
# config.yaml
mode: real  # 改为 real

# 更新API密钥为真实账户的密钥
api:
  key: "真实API Key"
  secret: "真实Secret"
  passphrase: "真实Passphrase"
```

## 📊 交易参数详解

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `position_size` | 0.1 | 单笔仓位占账户余额比例 |
| `max_exposure` | 0.3 | 总仓位上限 (30%) |
| `leverage` | 3 | 杠杆倍数 (OKX最高50x) |
| `stop_loss` | 0.02 | 止损比例 (杠杆后) |
| `take_profit` | 0.04 | 止盈比例 (杠杆后) |
| `trailing_stop` | 0.02 | 追踪止损回调比例 |

### 杠杆计算示例

- 账户: 10,000 USDT
- 单笔仓位: 10% = 1,000 USDT
- 杠杆: 3x
- 实际仓位: 3,000 USDT

| 条件 | 实际盈亏 |
|------|----------|
| 价格上涨 2% | +6% (180 USDT) |
| 价格下跌 2% | -6% (-180 USDT) |
| 止损触发 | -2% × 3 = -6% |

## 🤖 信号逻辑

### 买入条件 (满足其一)

```
1. RSI < 35 且 ML概率 > 75%
2. RSI < 40 且 ML概率 > 65%
3. 传统信号 + ML强烈确认
```

### 卖出条件 (满足其一)

```
1. RSI > 65 且 ML概率 < 25%
2. RSI > 60 且 ML概率 < 35%
3. 传统信号 + ML强烈确认
```

### 信号强度计算

```
信号强度 = RSI得分 + MACD得分 + ML得分

- RSI超卖/超买: +30
- RSI偏低/偏高: +15
- MACD金叉/死叉: +20
- ML概率 > 70%: +30
- ML概率 > 60%: +15
```

## 🔧 高级配置

### 自动运行 (Cron)

```bash
# 每5分钟运行一次（当前主入口）
*/5 * * * * cd /path/to/crypto-quant-okx && python3 bot/run.py
```

### ML模型训练

```bash
# 收集数据
python3 ml/collect_data.py

# 训练模型
python3 ml/train_model.py
```

## 📝 交易日志

日志文件位置: `/tmp/okx_trades.json`

```json
[
  {
    "time": "2026-03-13T10:00:00",
    "action": "OPEN_LONG",
    "symbol": "SOL/USDT",
    "price": 90.0,
    "amount": 1.0,
    "pnl": 0,
    "note": "开多"
  }
]
```

## 🔒 安全提示

1. **API密钥安全**
   - 务必保管好 API 密钥
   - 建议设置 IP 绑定
   - 定期更换密钥

2. **敏感信息**
   - `config/config.yaml` 已加入 `.gitignore`
   - 提交代码前确保不包含真实密钥

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 License

MIT License

## 👤 作者

- GitHub: [@xrs-b](https://github.com/xrs-b)

---

*如果对你有帮助，欢迎 Star ⭐*
