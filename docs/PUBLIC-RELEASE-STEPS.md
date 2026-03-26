# PUBLIC RELEASE STEPS

面向作者自己在把当前项目整理成公开仓库前，逐项执行的操作清单。

> 目标：把“自己正在跑的私有仓库”整理成“朋友 clone 后能按 README 部署的公开仓库”，同时尽量降低泄密、路径污染、运行态脏数据带入的风险。

---

## 1. 先确认公开版边界

建议把公开仓库定位为：

- 默认支持 `testnet`
- 默认保留完整代码、文档、脚本模板、配置样例
- 不包含你的真实凭证、日志、数据库、运行状态、机器路径
- 不承诺“作者私有环境一键复刻”，而是承诺“朋友可按文档独立部署”

**建议做法：新建 public repo，而不是直接把当前私有运行仓库改公开。**

原因：

- 私有仓库通常混有临时脚本、运行态文件、备份配置、机器路径
- Git 历史更容易残留旧 token / webhook / secret
- 公开版需要更干净的提交历史和更清晰的目录边界

---

## 2. 先轮换所有可疑凭证

在任何公开动作之前，先做：

1. 轮换 OKX API Key / Secret / Passphrase（如果曾在仓库、备份、聊天、截图中出现过）
2. 轮换 Discord bot token / webhook / channel 相关凭证
3. 轮换 Telegram / SMTP / 其他通知凭证
4. 检查交易所 API 权限是否最小化

建议重点检查：

- `config/config.local.yaml`
- `config/presets/btc-focused.yaml`
- `config/presets/safe-mode.yaml`
- `config/backups/`
- `.env`
- shell history / 临时导出文件 / 打包文件
- Git 历史 commit

---

## 3. 清理不应公开的运行态内容

公开版仓库里不要带这些内容：

- `logs/`
- `data/trading.db`
- `data/runtime_state.json`
- `.venv/`
- `bot.pid`
- `dashboard.pid`
- `relay.pid`
- `config/backups/`
- 任何作者机器专用 plist / service 实际部署文件（模板可保留）

发布前建议执行一次人工检查：

```bash
git status --short
find . -maxdepth 3 \( -name '*.pid' -o -name '*.db' -o -path './logs/*' -o -path './data/*' -o -path './config/backups/*' \)
```

---

## 4. 统一公开版默认值

当前公开版建议固定：

- Dashboard 默认端口：`5555`
- 启动命令默认写法：`python3 bot/run.py --dashboard --port 5555`
- 脚本默认环境变量：`DASHBOARD_PORT=5555`

原因：

- 脚本、README、`.env.example` 已经主要围绕 `5555`
- 朋友部署时更容易记忆，也更方便从文档直接照抄
- 避免 `8050 / 5555` 混用造成“脚本起在一个端口、配置写另一个端口、通知里再显示第三种文案”

如果你未来要改默认端口，必须同步改：

- `README.md`
- `docs/DEPLOYMENT.md`
- `config/config.yaml.example`
- `scripts/start.sh`
- `scripts/start.public.sh`
- `scripts/keep_dashboard_alive.sh`
- `bot/run.py`
- `dashboard/api.py`
- `core/notifier.py`

---

## 5. 从私有仓库导出公开版内容

建议流程：

1. 新建一个空的 public repo
2. 从私有仓库拣选这些内容过去：
   - 源代码目录
   - `README.md`
   - `docs/`
   - `scripts/` 中可模板化的脚本
   - `config/*.example`
   - `config/presets/btc-focused.yaml`（确认无真实凭证）
   - `config/presets/safe-mode.yaml`（确认无真实凭证）
   - `requirements*.txt` / `LICENSE` / `.gitignore` / `.env.example`
3. 不要直接拷贝：
   - 本地数据库
   - 日志
   - 运行状态
   - 备份配置
   - 机器专用 launchd / systemd 实际部署文件（除非它们已经模板化）

如果你决定直接在当前仓库收尾后再 push 到 public repo，至少要先确认：

```bash
git ls-files | grep -E '(^logs/|^data/|^config/backups/|\.pid$|\.venv/)'
```

结果应该为空，或只剩下明确允许公开的模板文件。

---

## 6. 做一轮“朋友视角”的部署验收

至少跑通以下链路：

```bash
cp config/config.yaml.example config/config.yaml
cp config/config.local.yaml.example config/config.local.yaml
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PROJECT_DIR="$PWD" .venv/bin/python3 bot/run.py --exchange-diagnose
PROJECT_DIR="$PWD" .venv/bin/python3 bot/run.py --exchange-smoke --symbol BTC/USDT --side long
PROJECT_DIR="$PWD" scripts/start.public.sh dashboard
```

最低验收标准：

- 能安装依赖
- 能读取示例配置启动
- `--exchange-diagnose` 正常
- `--exchange-smoke` 至少可预演
- Dashboard 能从 `5555` 打开
- 文档与实际命令一致

---

## 7. 发布前再做一次安全检查

建议在准备 push public 前执行：

```bash
grep -RInE --exclude-dir=.git --exclude-dir=.venv '(token|secret|passphrase|webhook|api[_-]?key)' .
grep -RInE --exclude-dir=.git --exclude-dir=.venv '/Users/|/home/|C:\\' .
```

检查重点：

- 是否还残留真实凭证
- 是否还有作者本机绝对路径
- 是否有不该公开的注释 / TODO / 调试命令

如果条件允许，再补一轮：

- `gitleaks detect`
- 人工看一遍最近几十个 commit

---

## 8. 发布建议顺序

推荐顺序：

1. 本地整理公开版目录
2. 本地完成基础验证
3. 新建 public repo
4. 首次 push 只包含“干净的公开版内容”
5. 在 public repo 打 tag / release
6. 再把 README 里的 clone / 安装 / testnet 验证链路重新按公开地址走一遍

---

## 9. 人手仍需决定的事项

这些通常不适合让代码自动替你决定：

- `LICENSE` 选 MIT / Apache-2.0 / GPL / 暂不放许可证
- 是否保留全部 presets，还是只保留最稳的一两个
- 是否保留通知相关功能的完整文档，还是先弱化
- 是否公开模型文件 / 历史训练产物 / 回测结果
- 是否保留作者自己的运维模板（service / plist / cron）
- public repo 是“可运行交易项目”还是“学习 / 参考项目”

---

## 10. 一句话版本

如果时间很赶，至少保证这四件事：

1. 所有密钥先轮换
2. 所有运行态文件别进公开仓库
3. 默认端口统一为 `5555`
4. 让朋友能按 README 从零跑起 Dashboard 和只读诊断
