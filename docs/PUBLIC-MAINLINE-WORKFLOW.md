# PUBLIC MAINLINE WORKFLOW

面向 `crypto-quant-okx` 的长期维护约定：**公共仓库是唯一代码主线，私人环境只保留运行态。**

---

## 1. 核心原则

### 公共仓库 = 唯一长期代码主线

所有准备长期保留、复用、分享、review、发布的内容，都应该优先进入公共仓库：

- 源代码
- 测试
- 文档
- 配置样例
- 模板脚本
- 发布说明
- 面向公开用户的安装与运维说明

### 私人环境 = 运行态与本地覆盖

私人环境不是另一条长期代码主线，而是公共主线代码的**本地运行副本 / 私有部署工作区**。它只负责承载：

- `.env`
- `config/config.yaml`
- `config/config.local.yaml`
- `logs/`
- `data/trading.db`
- `data/runtime_state.json`
- `data/config_audit.json`
- `*.pid`
- 本地模型产物、中间数据、缓存
- 与某台机器强绑定的 launchd / systemd / cron 实际部署文件

换句话说：

> **代码往公共仓库收敛，运行态往私人环境隔离。**

---

## 2. 推荐目录边界

### 应长期留在公共仓库的内容

- `analytics/`
- `bot/`
- `core/`
- `dashboard/`
- `signals/`
- `strategies/`
- `trading/`
- `tests/`
- `README.md`
- `docs/`
- `LICENSE`
- `.env.example`
- `config/*.example`
- 已脱敏、可复用的模板脚本

### 不应长期留在公共仓库的内容

- 真实凭证
- 本地数据库
- 日志
- runtime state
- PID 文件
- 私有配置
- 本机绝对路径
- 临时调试输出
- 仅对作者本人当天运行有意义的状态文件

详细保留/排除规则见：[`../PUBLIC-REPO-MANIFEST.md`](../PUBLIC-REPO-MANIFEST.md)

---

## 3. 日常工作约定

### 新功能 / 修复

如果某项修改对未来还有复用价值，应默认先整理成可公开、可 review、可提交的形式，然后进入公共仓库。

包括但不限于：

- 策略逻辑调整
- 风控修复
- 交易所适配修复
- Dashboard 改进
- 文档补充
- 测试补充
- 脚本模板优化

### 私人环境允许存在的差异

私人环境可以有本地差异，但这些差异应尽量限制在：

- secret / token / passphrase
- 本机路径
- 本机端口与服务管理
- 本地 watch list 微调
- 本地日志级别
- 本地运行数据

如果这些差异开始演变成“长期维护的一套平行代码”，说明边界已经跑偏，应该尽快回收整理到公共主线，或明确删除。

---

## 4. 发布与同步规则

### 公开发布前

发布前要确认：

1. 文档已经说明 public mainline 策略
2. `.gitignore` 仍正确拦截本地运行态
3. 没有把 `.env`、本地 config、logs、db、runtime state 一起提交
4. README 与部署文档仍然能指导新用户从零启动

### 私人部署更新时

推荐顺序：

1. 先从公共仓库同步代码
2. 再在私人环境保留本地配置和运行态
3. 必要时做本地迁移、回滚或增量验收

不推荐的做法：

- 在私人环境长期直接改代码但不回流公共仓库
- 让私人环境逐渐变成“另一套真正运行的主线”
- 把运行态文件当成项目资产长期堆在公共仓库

---

## 5. 提交判断准则

遇到一项变更时，可以用下面的判断：

### 应提交到公共仓库

- 任何别人 clone 后也需要的代码
- 任何 README / docs 需要说明的行为变化
- 配置结构变化
- 模板脚本改进
- 默认安全策略改进
- 测试与发布说明

### 不应提交到公共仓库

- 真实 API 凭证
- 真实通知 token / webhook
- 本地数据库
- 本地运行日志
- 本地 runtime state
- 私人机器上的临时 patch 文件
- 一次性排障中间产物

---

## 6. 对外表达建议

对外可以统一这样描述：

> `crypto-quant-okx` 的公共 GitHub 仓库是唯一代码主线；私人环境只保留 secrets、local config、logs、database 和 runtime state，不在公共仓库长期保留运行态。

这句话适合放在：

- README
- release notes
- public repo manifest
- 首次发布说明

---

## 7. 一句话版本

> **Public repo for code, private environment for runtime.**
