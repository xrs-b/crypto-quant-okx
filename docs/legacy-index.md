# Legacy 清单 / 索引

这份文档统一说明本仓库里的 **legacy / 兼容保留入口**，避免信息散落在 README、文件头和零散说明里。

## 当前正式入口

当前主线应统一使用以下入口：

- **交易主入口**：`python3 bot/run.py`
- **Dashboard 正式入口**：`dashboard.api:app`
- **一体化启动脚本**：`scripts/start.sh` / `scripts/start.public.sh`
- **ML 正式触发方式**：`python3 bot/run.py --collect` / `python3 bot/run.py --train`

如果是部署或日常运维，请优先看：

- [`README.md`](../README.md)
- [`docs/DEPLOYMENT.md`](DEPLOYMENT.md)

## Legacy 文件 / 入口清单

以下内容目前仍保留在仓库中，但**不属于当前主线正式运行入口**：

| 路径 | 类型 | 当前状态 | 保留原因 |
| --- | --- | --- | --- |
| `bot/main.py` | 历史交易入口 | legacy | 保留旧主程序实现，便于排查历史逻辑、比对迁移前行为 |
| `dashboard/app.py` | 历史 Dashboard 入口 | legacy | 保留旧页面入口与兼容参考，便于本地排查和旧代码对照 |
| `ml/collect_data.py` | 历史直跑脚本 | legacy | 保留研究/排障参考；主线已统一收口到 `bot/run.py --collect` |
| `ml/train_model.py` | 历史直跑脚本 | legacy | 保留研究/排障参考；主线已统一收口到 `bot/run.py --train` |
| `ml/simple_model.py` | 历史实验脚本 | legacy | 保留本地研究样例与模型对照，不作为部署入口 |
| `~/.crypto-quant-okx.local.yaml` | 历史配置兼容入口 | compat-only | 兼容旧个人环境；默认不自动加载，须显式环境变量启用 |

## 为什么这些 legacy 内容还不删除

保留原则很简单：

1. **兼容旧环境**：避免直接删掉后让历史脚本、旧部署习惯或本地排查流程瞬间失效。
2. **方便回溯**：某些问题需要对照旧入口、旧实现或旧配置来源来定位。
3. **降低迁移风险**：当前阶段优先确保机器人持续稳定运行，不做会影响执行行为的激进清理。

## 使用边界

- 可以把这些 legacy 文件当作**历史参考 / 排障素材 / 迁移痕迹**。
- **不要**把它们当成新的部署命令、systemd/launchd 入口或自动化运维入口。
- 新增文档、脚本或操作说明时，尽量直接指向本页和正式入口，避免重复维护多份 legacy 说明。

## 维护约定

后续如果要继续收敛 legacy：

- 先更新本页；
- 再从 README / 文件头只保留简短指针；
- 最后再评估是否可以真正删除对应 legacy 文件。
