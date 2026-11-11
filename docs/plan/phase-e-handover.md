# 阶段 E 交接记录 — 2026-11-11

> 依据：任务书 [docs/deepseek-harness-implementation-brief.md](../deepseek-harness-implementation-brief.md) + [已确认决策记录](07-confirmed-decisions.md)。
> 阶段 E 目标（A–E 最后一阶段）：计划确认、变更历史/版本、通知（普通请求/刷新）、出发前摘要、移动端途中查看；**局部重排保留未受影响已确认项并展示差异**；据反馈再评估导出/离线/预订（仅评估）。

---

## 1. 改动摘要

### 新增（业务代码）
- `fairmeet/history.py` — `HistoryService`：`record()`（不可变 ChangeLogEntry：谁/何时/什么/前后差异）、`list_entries()`、`updates_since()`（**更新通知 feed，普通请求/刷新，无 WebSocket**）、`snapshot()`/`list_revisions()`（行程版本快照）。
- `fairmeet/replan.py` — `ReplanService.replan_day()`：**局部重排**——只重排指定天的指定条目顺序（`sort_order`），不改其他天/不改状态/不改金额；产出 `ReplanResult`（changed diffs / affected / **preserved_item_ids** 含跨天未受影响项 / budget_delta / impact_notes）；记录一条 `ChangeType.REPLAN` 到变更历史。
- `fairmeet/templates/summary.html` — 出发前摘要（成员、预算与来源分类、检查结果、逐日条目）。
- `fairmeet/templates/ontrip.html` — 移动端途中查看（逐日时间线，已确认项提示「更改会展示差异并等待确认」）。
- `fairmeet/templates/history.html` — 变更历史 + 版本快照。
- `tests/conftest.py`（共享夹具）· `tests/test_history.py`（4）· `tests/test_replan.py`（6，含核心回归）· `tests/test_summary_onboarding.py`（7）。
- `docs/plan/phase-e-export-offline-booking-eval.md` — 导出/离线/外部预订**评估**（不实现）。

### 修改
- `fairmeet/domain.py` — 追加阶段 E 模型：`ChangeType`、`FieldDiff`、`ChangeLogEntry`、`Revision`、`ReplanRequest`、`ReplanResult`。
- `fairmeet/db.py` — 追加 `change_log`、`revisions` 两表与索引（阶段 B/C/D 表不受影响）。
- `fairmeet/app.py` — 装配 `HistoryService`/`ReplanService`；新增阶段 E 端点（history/updates/revisions/replan/summary）与 summary/ontrip/history 页面；`ui_add_item`/`ui_set_item_status`/提案 confirm 挂钩变更历史；replan 错误映射修正（Forbidden→403）。

### 保持原样（未触碰）
- `fairmeet/center.py`、`config.py`、`geocode.py` 及阶段 B/C/D 既有实现/模板均未改；既有未提交改动原样保护。

## 2. 设计决定

| 决定 | 取舍与理由 | 任务书一致性 |
|------|-----------|--------------|
| 变更历史为不可变 append-only 日志 | 审计/理解/追溯；不覆盖历史 | 任务书 §7「保存重要计划变更历史/版本」 |
| 版本快照存完整行程 JSON | 支持理解与恢复；结构直观 | 任务书 §3.8/§7 |
| 通知用 `updates_since` 增量拉取（请求/刷新） | 不引入 WebSocket，符合「先验证流程再上实时」 | 任务书 §10 |
| 局部重排只改指定天/条目的 sort_order | **不动其他天与其他已确认项**；preserved 显式列出未受影响项 | 任务书 §3.8、§9.8 |
| 重排也记录 changed diffs + 快照 | 差异可见、可追溯 | 任务书 §9.7、§9.8 |
| 导出/离线/预订只评估不实现 | 交易/离线不在核心范围，先评估 | 任务书 §2.8、§6 阶段 E |

## 3. 运行方式

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn fairmeet.app:app --reload
# 旅程空间 → 出发前摘要 /trip/{id}/summary、途中查看 /trip/{id}/ontrip、变更历史 /trip/{id}/history
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check fairmeet tests
```

## 4. 测试真实结果

- `pytest -q` → **117 passed**（阶段 B/C/D 既有 102 + 阶段 E 新增 15：history 4 + replan 6 + summary_onboarding 7；`test_config.py`/`test_geocode.py` 仍为空文件）。
- **断网**：内存库 + FakeGeoAdapter + FakeLLM，无真实网络/模型，离线通过。
- `ruff check fairmeet tests` → **All checks passed**。
- **真实启动验证**：`uvicorn` 启动；真实 HTTP 验证 summary 页 200、ontrip 页 200、replan（changed 2）、history 页 200、history entries 1、revisions 1。
- **核心回归已覆盖**：`test_replan_preserves_other_day_confirmed` 断言 day2 已确认条目 status/cost/sort_order 完全不变，day1 已确认项 status/cost 不变，preserved 含跨天未受影响项。
- **未运行/受限**：无真实 LLM；导出/离线/预订未实现（按评估结论）。

## 5. 已知限制

- `updates_since` 以秒级 `created_at` 比较，同一秒内的多条更新可能被边界漏掉（feed 属通知用途，容忍；如需精确可后续引入单调游标/rowid）。
- 版本快照只做「保存与查看」，**未提供一键回滚**（恢复涉及确认与影响展示，留作后续）。
- 局部重排目前是确定性「重排顺序」；按时间/预算自动优化属增强项。
- 导出/离线/预订按评估不实现。

## 6. 未实现项（本阶段明确不做）

- 一键回滚/版本恢复（可后续加，需确认与差异展示）。
- 导出文件（.ics/PDF）、离线缓存、外部预订/支付（评估见 phase-e-export-offline-booking-eval.md）。
- 真实 LLM 提供方接入（阶段 D 遗留，提供方待定）。
- WebSocket 实时协作（无实测需求依据，不引入）。

## 7. 下阶段/收尾建议

- **A–E 主链路已闭环**，可做一次**端到端整体验收走查**（任务书 §9 十条完成标准），用一趟成都多人多日旅程从创建→邀请→需求→灵感→行程/地图→AI 提案确认→检查/预算→局部重排差异→摘要/途中→变更历史，逐条对照并记录结果。
- 若要上真实外部能力：先核实高德配额/Key 类型、再评估真实 LLM 提供方与成本护栏。
- 若用户反馈需要回滚：在顶部加「恢复到某快照」，走确认+差异展示流程（复用阶段 D 的 diff/impact 机制）。
- 导出/离线/预订：按触发条件单独立项（见评估文档）。
