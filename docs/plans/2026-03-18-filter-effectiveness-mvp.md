# 2026-03-18 过滤规则有效性 MVP 方案

## 背景
上一轮已完成过滤原因标准化（`filter_code` / `filter_group` / `action_hint`）与按币种拆解，但仍停留在“解释为什么被过滤”。下一步要补上闭环：验证这些过滤到底挡得对不对。

## 目标
提供一版最小可用的**过滤规则后验评估**：
- 按 `filter_code` 汇总过滤后的结果
- 判断该过滤更像是：
  - `avoided_loss`：过滤后如果真入场，大概率会先吃到明显逆向波动
  - `missed_profit`：过滤后如果真入场，会有较明显顺向机会
  - `neutral`：后续走势不明显 / 无法下结论
  - `pending`：样本时间还不够，尚未覆盖观察窗口
  - `insufficient_data`：行情数据不足，无法判断
- 给 dashboard 一个可直接展示的 API

## MVP 边界
### 做
1. 新增 API：`/api/signals/filter-effectiveness`
2. 基于历史 filtered signals + 交易所 K 线做后验评估
3. 默认使用 24h 观察窗口，可通过 query 参数切换
4. 汇总到 `filter_code` 层级，并返回样本数 / 避险数 / 错杀数 / 中性数 / 待观察数 / 正确率
5. 在 dashboard 信号页新增一块「过滤规则有效性」表
6. 补测试，覆盖核心判断逻辑和 API 聚合

### 暂不做
- 不写新表，不做后台定时任务
- 不做每条信号永久缓存评估结果
- 不做复杂方向识别（例如 hold/no-direction 的方向后验模型）
- 不做自动调参，只先做可观测

## 判定规则（MVP）
仅对 `signal_type in {buy, sell}` 的 filtered signal 做方向性评估。

### buy 信号
- 顺向机会：窗口内 `max(high)` 相对入场价涨幅
- 逆向风险：窗口内 `min(low)` 相对入场价跌幅

### sell 信号
- 顺向机会：窗口内 `min(low)` 相对入场价跌幅
- 逆向风险：窗口内 `max(high)` 相对入场价涨幅

### 分类
给定 `min_move_pct`（默认 1.5%）：
- 若逆向风险 >= min_move_pct 且逆向风险 > 顺向机会 -> `avoided_loss`
- 若顺向机会 >= min_move_pct 且顺向机会 > 逆向风险 -> `missed_profit`
- 否则 -> `neutral`

### 特殊情况
- 信号时间距离现在不足观察窗口 -> `pending`
- 无法抓到足够 K 线 / 价格为空 -> `insufficient_data`
- `hold` / 无方向信号先归为 `insufficient_data`

## 实施步骤
1. 在 `dashboard/api.py` 中新增纯函数：
   - 评估单条 filtered signal
   - 聚合 filter effectiveness
2. 在 `core/exchange.py` 中给 `fetch_ohlcv` 增加 `since` 可选参数，便于按信号时间取窗口数据
3. 新增 `/api/signals/filter-effectiveness`
4. 在 dashboard 信号页增加表格
5. 添加单测：
   - 单条 buy/sell 分类
   - API 按 `filter_code` 聚合

## 验证
- `python -m py_compile` 相关文件
- `python -m unittest tests.test_all.TestDashboardApi ...`

## 成功标准
- 能看出每个 `filter_code` 究竟更常避险，定更常错失利润
- 代码不引入额外后台复杂度
- 保持 API/前端改动可回退、可验证
