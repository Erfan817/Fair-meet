# 阶段 D 交接记录 — 2026-11-11

> 依据：任务书 [docs/deepseek-harness-implementation-brief.md](../deepseek-harness-implementation-brief.md) + [已确认决策记录](07-confirmed-decisions.md)。
> 阶段 D 目标：结构化 I/O 契约 + 受控业务工具 + 校验（先行），再做对话协调；数据源可用性台账与降级；来源/推断/核实状态/操作影响标示。**LLM 一律 mock，断网可跑**。

---

## 1. 改动摘要

### 新增（业务代码，`fairmeet/ai/` 包）
- `ai/contracts.py` — 结构化 I/O 契约：`OperationType` 白名单；7 种 `ProposedOperation`（add_need/add_inspiration/add_place/add_day/add_item/update_item/set_item_status）+ 判别包装 `ProposedOperationEnvelope`；`AIResponse`（reply/澄清/operations/sources）；`SourceRef`；`DiffEntry/OperationImpact/ChangeProposal`。
- `ai/tools.py` — **受控业务工具层** `Toolbox`：`plan()`（dry-run 计算变更类型/是否触碰已确认项/差异/影响，不写库，授权失败写 `blocked_reason`）与 `execute()`（真正写入，仍经 services 授权）。**AI 无 DB 句柄，只有 tools**。
- `ai/proposal.py` — **修改提案** `ProposalService`：`build`（生成 pending 提案，**不执行**；有可写操作或碰已确认项则 `requires_confirmation=True`）、`confirm`（仅发起者、仅 pending，才执行）、`reject`。持久化到 `change_proposals` 表。
- `ai/explain.py` — `Explainer.explain_conflicts`：**基于真实 CheckService/需求数据**组织解释，不编造；无数据时明确说明。
- `ai/orchestrator.py` — 对话编排 `Orchestrator` + `LLMClient`(Protocol) + `FakeLLM`：确定性「解释冲突」快捷路径（不调 LLM）；LLM 结构化输出经 `AIResponse` 校验，含 operations 时生成**待确认提案**不自动执行；**非法 JSON 或 schema 不符 → `mode='invalid'` 且不落提案**。
- `ai/sources.py` — `SourceRegistry`：数据源可用性台账（`source_availability` 表）+ `probe_geo/probe_route` 降级探测（不抛）。
- `fairmeet/templates/assistant.html` — 协调助手页（对话、待确认提案的差异/影响展示、确认/拒绝、来源）。

### 修改
- `fairmeet/db.py` — 追加 `change_proposals`、`source_availability` 两表（阶段 B/C 表不受影响）。
- `fairmeet/app.py` — `create_app(db, geo, llm)` 注入 LLM（默认 FakeLLM）；装配 Explainer/ProposalService/Orchestrator/SourceRegistry；新增阶段 D 端点与 `/trip/{id}/assistant` 页面。

### 保持原样（未触碰）
- `fairmeet/center.py`、`config.py`、`geocode.py` 及阶段 B/C 既有实现/模板均未改；既有未提交改动原样保护。

## 2. 设计决定

| 决定 | 取舍与理由 | 任务书一致性 |
|------|-----------|--------------|
| 结构化契约先行（Pydantic/JSON Schema） | 所有 AI 输出先校验，未通过不落库 | 任务书 §6 阶段 D「先结构化 I/O + 受控工具 + 校验」 |
| AI 只能经 Toolbox → services（带授权） | 复用 `require_member`，无绕过权限/直写库路径；tools 无 DB 句柄 | 任务书 §5「AI 不得绕过权限或直接写数据库」 |
| 提案两段式 plan/execute + 显式 confirm | 改已确认项展示 diffs/影响并等待确认，**不静默修改** | 任务书 §3.8、§9.7 |
| 只要含**任何可写操作**即 requires_confirmation | 写操作一律走确认，避免静默落库（比最低要求更严格） | 任务书 §5「操作前确认」 |
| 冲突解释走确定性 CheckService 结果 | 不编造精确数字/事实；未知/估算显式标注 | 任务书 §5、§8 |
| LLM 用 Protocol + FakeLLM，非法输出安全拒绝 | 断网可测；不依赖真实模型/网络 | 07 决策（阶段 D 再定提供方） |
| 来源台账 + 探测降级 | 外部不可用时记录并降级，不假装实时 | 任务书 §3.7、§10 |

## 3. 运行方式

```powershell
# 依赖同阶段 B/C（阶段 D 无新增第三方依赖）
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn fairmeet.app:app --reload
# 旅程空间 → 「AI 协调助手」（/trip/{id}/assistant?member={member_id}）
# 真实 LLM：当前默认 FakeLLM；接入真实提供方时注入 create_app(llm=...)（提供方/额度阶段 D 再定）

.\.venv\Scripts\python.exe -m pytest -q         # 断网可跑
.\.venv\Scripts\python.exe -m ruff check fairmeet tests
```

## 4. 测试真实结果

- `pytest -q` → **102 passed，0 xfail**。构成：阶段 B/C 既有 54（api 9 + services 10 + center 4 + adapters 11 + itinerary 13 + checks 7）+ 阶段 D 新增 **48**（contracts 13 + tools 10 + proposal 10 + orchestrator 9 + sources 6）；`test_config.py`/`test_geocode.py` 仍为空文件（0 测试，如实标注）。
- **断网**：全部用内存库 + FakeGeoAdapter + FakeLLM；**无真实 LLM、无真实网络**，离线通过。
- `ruff check fairmeet tests` → **All checks passed**。
- **真实启动验证**：`uvicorn` 启动；真实 HTTP 验证 `GET /ai/explain`（确定性）、`POST /ai/chat`（默认 FakeLLM 无提案）、`GET /proposals`、`POST /sources/probe`（geo available）、`GET /sources`（台账 2 条）、`/assistant` 页面 200、**未授权成员 chat → 403**。
- **发现并修复 1 处实现缺陷**：orchestrator 原先仅捕获 `ValidationError`，LLM 返回**非法 JSON** 时会抛 `JSONDecodeError` 而非安全回退。已修复为同时捕获 `json.JSONDecodeError`，统一 `mode='invalid'` 不落提案；对应测试由 xfail 转正式通过（复核后 102 passed，无 xfail）。
- **未运行/受限**：未接入真实 LLM 提供方（阶段 D 确认提供方/额度后再接）；未做真实模型质量评测。

## 5. 已知限制

- LLM 默认 FakeLLM；真实对话需注入 `LLMClient` 实现（提供方/额度未定）。
- 提案执行目前逐条执行、单失败不回滚已执行项（记录在 `skipped`，可追溯）；未做事务级原子性。
- 「影响范围」目前覆盖：是否碰已确认项、字段级差异、预算增减、状态变更；更细的路线受影响项传导留阶段 E 的差异展示增强。
- 冲突解释覆盖硬约束/时间重叠/交通未知/预算；更细规则（营业时间/绕行/密度）后续增强。

## 6. 未实现项（本阶段明确不做）

- 真实 LLM 提供方接入与成本护栏（提供方待定）。
- 变更历史/版本、通知、出发前摘要、局部重排差异展示 —— 阶段 E。
- 未验证抓取的内容解析仍不作生产依赖。

## 7. 下阶段建议

- 阶段 E：确认流程、变更历史/版本（复用 ChangeProposal + Revision/ChangeLog）、通知（先请求/刷新）、出发前摘要、移动端途中查看；**局部重排保留未受影响已确认项并展示差异**（可复用阶段 D 的 diffs/impact 与阶段 C 的局部更新机制做回归）。
- 若用户提供 LLM 提供方与额度：接入真实 `LLMClient`，保留 FakeLLM 作为离线测试门禁；增加成本护栏与超时/降级。
