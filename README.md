# OKX合约量化交易机器人

基于机器学习的OKX合约自动交易系统，支持做多/做空、止盈止损、追踪止损等功能。

## 功能特性

### 核心交易
- ✅ **ML模型预测** - Random Forest模型预测涨跌概率
- ✅ **做多/做空** - 支持多空双向交易
- ✅ **仓位控制** - 最大30%仓位，单笔10%
- ✅ **杠杆交易** - 3x杠杆

### 风险管理
- ✅ **固定止损** - 2%止损 (杠杆后)
- ✅ **固定止盈** - 4%止盈 (杠杆后)
- ✅ **分批止盈** - 50%仓位4%止盈，50%追踪止损
- ✅ **追踪止损** - 2%回调自动平仓，持久化存储

### 信号系统
- ✅ **RSI指标** - 14周期
- ✅ **MACD指标** - 12/26/9
- ✅ **信号强度** - 显示置信度
- ✅ **ML确认** - 需要ML高概率确认

### 其他
- ✅ **交易日志** - 记录所有交易
- ✅ **Discord推送** - 实时通知

## 技术栈

| 技术 | 说明 |
|------|------|
| Python 3.9+ | 编程语言 |
| ccxt | 交易所API封装 |
| scikit-learn | ML模型 |
| pandas | 数据处理 |

## 目录结构

```
crypto-quant-okx/
├── bot/
│   └── okx_trade.py      # 主交易机器人
├── ml/
│   ├── train_model.py    # 训练模型
│   ├── simple_model.py   # 简单模型
│   └── *_model.pkl       # 训练好的模型
├── strategies/
│   └── *.py              # 交易策略
├── data/
│   └── *.py              # 数据处理
└── config/
    └── config.yaml        # 配置文件
```

## 配置

config.yaml:
```yaml
api:
  key: "你的API Key"
  secret: "你的Secret"
  passphrase: "你的Passphrase"

discord:
  channel_id: "Discord频道ID"
```

## 运行

### 手动运行
```bash
cd /Volumes/MacHD/Projects/crypto-quant-okx
python3 bot/okx_trade.py
```

### Cron自动运行
```bash
# 每10分钟运行一次
*/10 * * * * python3 /Volumes/MacHD/Projects/crypto-quant-okx/bot/okx_trade.py
```

## 交易参数

| 参数 | 值 | 说明 |
|------|-----|------|
| RSI周期 | 14 | 相对强弱指数 |
| RSI超卖 | 35 | 买入信号 |
| RSI超买 | 65 | 卖出信号 |
| MACD | 12/26/9 | 指数移动平均 |
| 仓位上限 | 30% | 总仓位限制 |
| 单笔仓位 | 10% | 单笔交易比例 |
| 杠杆 | 3x | 合约杠杆 |
| 止损 | 2% | 杠杆后亏损 |
| 止盈 | 4% | 杠杆后盈利 |

## ML模型

- **训练数据**: 1小时K线
- **特征**: RSI, MACD, 布林带, 均线, 波动率
- **模型**: Random Forest
- **准确率**: ~70%

## 信号逻辑

```
买入信号:
- RSI < 35 且 ML概率 > 75%
- 或 RSI < 40 + ML概率 > 65%

卖出信号:
- RSI > 65 且 ML概率 < 25%
- 或 RSI > 60 + ML概率 < 35%
```

## 风险提示

⚠️ 本项目仅供学习交流，不构成投资建议！
- 合约交易风险高，可能亏损全部资金
- ML模型预测不保证准确
- 请在测试网充分验证后再用于实盘

## GitHub

https://github.com/xrs-b/crypto-quant-okx

## 更新日志

### 2026-03-13
- 集成ML模型预测
- 修复仓位控制 (30%上限)
- 修复止损/止盈按3x杠杆计算
- 改进ML信号确认逻辑
- 添加分批止盈 (50%@4%)
- 添加信号强度显示
- 添加交易日志
- 添加追踪止损持久化

---

*Created by 小圆*
