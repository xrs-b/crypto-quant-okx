# PUBLIC REPO STRUCTURE

这是建议给公开仓库采用的目录结构。目标不是“最花哨”，而是：

- 新朋友一眼知道入口在哪
- 私密内容与可公开内容边界清晰
- 文档、脚本、配置样例、源码职责分层明确
- 后续继续维护时不容易把运行态脏文件带进仓库

---

## 推荐结构

```text
crypto-quant-okx/
├── README.md
├── LICENSE
├── .gitignore
├── .env.example
├── requirements.txt
├── requirements-public.txt          # 可选：若你想单独给公开版依赖清单
│
├── analytics/                       # 回测、分析、优化相关模块
├── bot/                             # 主入口、守护、CLI 参数入口
├── core/                            # 核心基础能力：配置、交易所、通知、数据库、运行控制
├── dashboard/                       # Dashboard API / 页面逻辑
├── ml/                              # 模型训练、预测、特征相关
├── signals/                         # 信号检测/验证/记录
├── trading/                         # 执行、风控、仓位管理
│
├── config/
│   ├── config.yaml.example
│   ├── config.local.yaml.example
│   └── presets/
│       ├── btc-focused.yaml
│       ├── btc-grid-candidate.yaml
│       ├── safe-mode.yaml
│       └── xrp-candidate.yaml
│
├── docs/
│   ├── DEPLOYMENT.md
│   ├── PUBLIC-RELEASE-STEPS.md
│   ├── PUBLIC-REPO-STRUCTURE.md
│   ├── current-context-summary.md        # 若保留，请确保无私密运行态信息
│   ├── current-context-ultra-short.md    # 同上
│   ├── layering-acceptance-checklist.md
│   └── layering-config-notes.md
│
├── scripts/
│   ├── start.sh
│   ├── start.public.sh
│   ├── keep_dashboard_alive.sh
│   ├── layering_state_report.py
│   ├── candidate-review.cron.example
│   ├── okx-trading.service               # 仅保留模板化版本
│   └── com.oink.crypto-quant-okx.dashboard-keepalive.plist  # 仅保留模板化版本
│
├── tests/
│   └── test_all.py
│
├── data/                            # 保留目录说明，不提交运行态产物
│   └── .gitkeep                     # 可选
└── logs/                            # 保留目录说明，不提交日志
    └── .gitkeep                     # 可选
```

---

## 各层职责建议

### 1. 根目录只放“入口型文件”

根目录适合放：

- `README.md`
- `LICENSE`
- `.gitignore`
- `.env.example`
- 依赖文件

不建议把太多一次性说明、临时脚本、私人笔记直接堆在根目录，否则公开后会显得杂乱。

---

### 2. `config/` 只放样例与可公开 preset

建议：

- `config.yaml.example`：完整、可运行、无秘密
- `config.local.yaml.example`：私密字段模板
- `presets/`：仅保留确认无私密信息、确实对外有价值的预设

不建议公开：

- `config.yaml`
- `config.local.yaml`
- `config/backups/`

如果本地项目要继续保留这些文件，也应确保它们被 `.gitignore` 排除。

---

### 3. `docs/` 放“稳定文档”，不要放私人碎片

公开仓库里的文档建议只保留：

- 部署文档
- 架构说明
- 发布说明
- 验收清单
- 配置说明

如果 `current-context-summary.md` / `current-context-ultra-short.md` 里包含明显的作者运行状态、实盘观察、私人决策记录，建议：

- 要么删掉
- 要么改写成对外可读的设计摘要

---

### 4. `scripts/` 只保留“模板化脚本”

适合保留：

- 通用启动脚本
- 通用保活脚本
- 可读的状态报告脚本
- `.service` / `.plist` / `.cron.example` 模板

要求：

- 不写死作者机器路径
- 支持 `PROJECT_DIR`
- 默认端口、默认入口、默认日志位置都与 README 一致
- 明确告诉读者“这是模板，不是作者机器可直接复用配置”

---

### 5. `data/` 与 `logs/` 建议保留目录但不保留内容

保留目录的好处：

- 新用户少踩“目录不存在”的坑
- 脚本默认路径更直观

建议做法：

- 只保留 `.gitkeep`
- 不提交数据库、运行状态、日志

---

## 公开仓库建议排除项

以下内容应排除在公开仓库之外：

```text
.venv/
logs/*
data/*.db
data/runtime_state.json
*.pid
config/config.yaml
config/config.local.yaml
config/backups/
```

如果仓库里还存在这些内容，建议优先处理 `.gitignore`，再清理 Git 历史和工作区。

---

## 对朋友最友好的入口顺序

建议公开仓库里，朋友第一次接触时的顺序是：

1. 看 `README.md`
2. 复制 `config/config.yaml.example`
3. 复制 `config/config.local.yaml.example`
4. 安装依赖
5. 执行 `--exchange-diagnose`
6. 启动 Dashboard（默认 `5555`）
7. 再考虑 testnet smoke / daemon / relay

也就是说，目录结构要围绕这个学习路径服务，而不是围绕作者自己的维护习惯服务。

---

## 一个实用原则

如果某个文件满足下面任一条件，就不适合放进 public repo：

- 只有作者自己看得懂
- 依赖作者机器路径
- 包含真实凭证或运行态数据
- 没有 README 上下文就无法理解用途
- 留着只会增加朋友部署困惑

---

## 当前仓库落地建议

结合当前项目，建议优先保持：

- 业务代码目录不大改
- 文档补齐到 `docs/`
- 脚本统一围绕 `PROJECT_DIR` + 默认端口 `5555`
- 配置样例和 presets 统一默认端口
- 把“作者私有运行残留”继续往仓库外挪

这样风险最小，也最适合在不大改核心逻辑的前提下完成公开版收尾。
