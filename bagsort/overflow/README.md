# 溢出转送子系统（overflow diversion）

早高峰本站排队接不下时，系统自动把溢出行李转到备用终端（backup terminal），
峰值过后再收回来。本子系统解决五件事：

1. **什么时候转、转多少、什么时候收** —— 可插拔策略（反应式 / 预测式 / 混合式）；
2. **额度账、外发记录、终端归属三者必须吻合** —— 每次回收前强制对账；
3. **转送与回收前后快照落盘** —— 哈希链归档，篡改即断链；
4. **外发中断可回滚** —— 回滚是一个原子 journal 事务，两终端账目不被破坏；
5. **外发中的行李不能在本站再次入账** —— check-in 守卫；**溢出历史可按时间复算**。

## 数据模型（全部由 journal 命令折叠，重启可重放）

| 视图 | 内容 |
| --- | --- |
| 额度账 | `overflow_capacity` + 按物理位置重算的 `occupancy/free` |
| 归属 | `OverflowBag(bag_id, home, backup, status, batch_id)`，`home` 终身不变 |
| 外发记录 | `OverflowBatch`：offer 列表、accept 子集、状态机 |
| 决策 | `OverflowDecision`：每分钟策略判断（动作、数量、依据、预测值） |

行李状态机：

```
at_home ──prepare──▶ in_flight ──accept──▶ held ──recall──▶ returned ──depart──▶ released
   ▲                    │                     │
   └──── rollback ──────┴─────────────────────┘
```

- `released` 是终态保留行：行李已离开系统，但归属行不删，保证关闭的外发批次永远对得上账。
- 只在 `at_home / returned` 才能从本站出港；`held` 必须先回收。

外发批次状态机：`prepared → accepted/partial → partial_recall → recalled`，
异常路径为 `→ rolled_back`。

## 触发策略：响应速度 vs 误转率

| 策略 | 依据 | 响应速度 | 误转风险 |
| --- | --- | --- | --- |
| `reactive` 反应式 | 当前实际排队长度 | 慢（队列已经超限才动） | **零**（不存在幻影到达） |
| `predictive` 预测式 | 到达预测上沿（EWMA + 分位带 + 上游 advisory） | 快（提前转） | **高**（预测错就白转） |
| `hybrid` 混合式（默认） | 预测只允许限量预转，实际队列确认后才放量 | 较快 | **有上限** |

### 混合式对误转风险的兜底

1. 预测单独触发时，最多只走一个**小的封顶批次**（`first_tranche`，默认 4 件）；
2. 且必须同时满足“队列已到确认线（默认 85% 容量）+ 预测上沿超过容量”；
3. 实际队列一旦真的超限，按真实超限量转，不受封顶限制；
4. 预转后若洪峰没来（队列仍健康），**backstop 立即回收**这批预转行李，
   暴露窗口最短；
5. 所有回收受家端容量约束，整批回收（LIFO，后转先收），放不下的等下一拍。

确定性仿真（`bagsort overflow`，96 分钟曲线，含一次假预警 + 一次真预警）给出的对比：

| 策略 | 溢出分钟 | 峰值排队 | 误转率 |
| --- | --- | --- | --- |
| reactive | 36 | 148 | 0% |
| hybrid | 34 | 140 | 6.25%（仅假预警窗口的两批 4 件，且立即回收） |
| predictive | 23 | 136 | 41.7% |

## 对账（reconcile）

回收动作在执行前强制 `reconcile`，不平账直接拒绝。校验独立于账本自计数，
从折叠状态重算：

- 每个终端占用 ≤ 容量（家端在“已溢出未转出”的瞬间允许短暂超容量——那是触发条件本身；
  备用端是硬物理边界，行李只能落在有空位的终端）；
- 每个活跃批次的成员、accepted 子集与归属行一一对应；
- accepted 但仍 in_flight、未 accepted 却 held、活跃批次的行李已回家等都是错误；
- rolled_back 批次的成员必须全部 at_home/released；recalled 批次必须 returned/released；
- 守恒：外发记录里每个包都恰好有一行归属。

## 外发中断回滚

`interrupt(batch_id)` / `transfer(..., fail_remaining=True)`：

- 未到达的包（in_flight）直接回 at_home；
- 已到达备用端的包（held）也随回滚回来，中断不允许把归属 custody 劈成两半；
- 全部在**一个 journal session** 内提交，异常即整体回滚，两端占用同步恢复。

## 快照归档

每次 transfer / interrupt / recall / tick 动作前后各写一份
`NNNNNN-{operation}-{before|after}.json`，包含水位线、时间戳和 overflow 状态切片。
after 文件带前一文件的 sha256，全局成链；改/删任一文件都会让
`GET /api/overflow/snapshots` 校验失败。

## 历史按时间复算

`overflow.history.recompute(entries, needed_window)` 重放 journal 命令重建分钟级时间线，
并对每次转送做**反事实判定**：若该批没转，之后 N 分钟内（默认 6）队列峰值是否会超容量——
会则 `needed=true`，否则计为误转。支持 `filter_window(history, start, end)` 按时间窗复算。

## HTTP 接口

见 `/api/catalog` 中 `overflow` 段：状态、配置、队列、held、批次、决策、
transfer / interrupt / recall / tick、reconcile、snapshots、history。
