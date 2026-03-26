# FINAL RELEASE REVIEW

面向 `crypto-quant-okx` 首次公开发布前的最终人工复核。

> 目标：不是“能 push 就算完”，而是确认这份 public repo 对朋友安全、清晰、可复现，且不会把作者私有运行痕迹一起带出去。

---

## 1. Secrets / 历史 / 边界复核

### 工作区 secrets 扫描
- [ ] `grep -RInE --exclude-dir=.git --exclude-dir=.venv '(token|secret|passphrase|webhook|api[_-]?key)' .`
- [ ] 人工复核命中项，确认只有模板值、环境变量占位符、文档说明
- [ ] `config/presets/`、`.env.example`、`config/*.example` 没有真实凭证
- [ ] 没有把截图、导出文件、测试记录中的敏感内容带进仓库

### Git 历史与发布边界
- [ ] 抽查最近提交，确认没有把 `.env`、`config/config*.yaml`、运行日志、数据库提交进历史
- [ ] 如历史曾出现真实凭证，先轮换，再决定是否额外清理历史
- [ ] 明确 public repo 只承担“学习 / 研究 / testnet-first 分享”，不承担作者私有运行环境复刻

---

## 2. 仓库内容复核

### 应保留
- [ ] 源码目录、测试、部署文档、示例配置、精简 preset、模板脚本齐全
- [ ] `LICENSE` 已存在且 README 有说明
- [ ] `README.md`、`docs/DEPLOYMENT.md`、`PUBLIC-REPO-MANIFEST.md`、`PUBLIC-RELEASE-CHECKLIST.md` 彼此一致

### 不应保留
- [ ] `.env`
- [ ] `config/config.yaml`
- [ ] `config/config.local.yaml`
- [ ] `config/backups/`
- [ ] `logs/`
- [ ] `data/trading.db`
- [ ] `data/runtime_state.json`
- [ ] `*.pid`
- [ ] `.venv/`
- [ ] 作者私有运维笔记、临时调试草稿、带绝对路径的私用脚本

### 忽略文件
- [ ] `.gitignore` 已覆盖运行态文件、数据库、私有配置、虚拟环境、缓存文件
- [ ] 新增/调整的公开文档不会误导用户把私有文件提交进仓库

---

## 3. 示例配置与 presets 复核

### 示例配置
- [ ] `config/config.yaml.example` 字段与当前代码读取逻辑一致
- [ ] `config/config.local.yaml.example` 只包含模板值
- [ ] `.env.example` 与 README / DEPLOYMENT 用法一致

### Public presets 策略
- [ ] 仅保留最容易解释、最不容易误配的 preset
- [ ] 当前公开版默认仅保留：
  - [ ] `config/presets/btc-focused.yaml`
  - [ ] `config/presets/safe-mode.yaml`
- [ ] 不保留更激进或更容易让朋友误以为“现成 alpha”的候选 preset
- [ ] README / 文档已同步说明为什么只保留少量 preset

---

## 4. 从零部署复核

找一台“没有作者上下文”的环境，按 README / DEPLOYMENT 走一遍：

- [ ] `git clone ...`
- [ ] `python3 -m venv .venv`
- [ ] `pip install -r requirements.txt`
- [ ] `cp .env.example .env`
- [ ] `cp config/config.yaml.example config/config.yaml`
- [ ] `cp config/config.local.yaml.example config/config.local.yaml`
- [ ] 填入最小 testnet 配置
- [ ] `PROJECT_DIR="$PWD" .venv/bin/python3 bot/run.py --exchange-diagnose`
- [ ] `PROJECT_DIR="$PWD" .venv/bin/python3 bot/run.py --notify-test`（如启用通知）
- [ ] `PROJECT_DIR="$PWD" .venv/bin/python3 bot/run.py --exchange-smoke --symbol BTC/USDT --side long`
- [ ] `PROJECT_DIR="$PWD" scripts/start.public.sh dashboard`

成功标准：
- [ ] 不需要知道作者机器路径
- [ ] 不需要修改脚本硬编码
- [ ] 文档命令与实际入口一致
- [ ] Dashboard 默认端口、脚本默认值、README 文案一致

---

## 5. Testnet 验收复核

在明确为 `exchange.mode: testnet` 的前提下：

- [ ] 先做 `--exchange-smoke` 预演
- [ ] 再做一次最小 `--exchange-smoke --execute`
- [ ] 核对交易所 testnet 页面、终端输出、本地 db、runtime_state、Dashboard 是否一致
- [ ] 确认通知链路不会误发到真实生产频道
- [ ] 确认不会因为 preset 或示例配置默认值过激而扩大风险

---

## 6. README / 文案 / 许可证复核

- [ ] README 明确写出高风险免责声明
- [ ] README 明确要求先 testnet，再考虑 real
- [ ] README 明确适合人群 / 不适合人群
- [ ] README 明确说明当前采用 `MIT License`
- [ ] README 明确说明为何只公开少量 preset
- [ ] 文档中不出现“下载即赚钱”“默认可直接实盘”等误导表述

---

## 7. 发布动作前最后 5 分钟

- [ ] `git status` 只剩这次准备公开的代码/文档变更
- [ ] 再看一遍 `git diff --stat`
- [ ] 再做一次关键字扫描：`token|secret|passphrase|webhook|/Users/|/home/`
- [ ] 确认 release tag、README clone 地址、仓库名、LICENSE 都已准备好
- [ ] 确认首版只发布自己愿意长期维护和解释的内容

如果其中任一项不确定，宁可延后发布，也不要带着模糊边界硬上。
