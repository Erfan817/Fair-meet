# 阶段 C 交接记录 — 2026-11-11

> 依据：任务书 [docs/deepseek-harness-implementation-brief.md](../deepseek-harness-implementation-brief.md) + [已确认决策记录](07-confirmed-decisions.md)。
> 阶段 C 目标：可编辑逐日行程 + 地图/时间线 + 约束/预算校验 + 地理适配层；餐饮/会面点作为行程条目；数据区分来源；断网可测。

---

## 1. 改动摘要

### 新增（业务代码）
- `fairmeet/adapters/__init__.py` — 地理适配层**端口**：`GeoAdapter`（Protocol，含 geocode/search_poi/route）+ 结果模型 `GeocodeResult/PoiResult/RouteResult` + `GeoUnavailable`。
- `fairmeet/adapters/fake.py` — `FakeGeoAdapter`（离线确定性；`unavailable=True` 模拟外部不可用，用于断言降级）。
- `fairmeet/adapters/amap.py` — `AmapGeoAdapter`（高德 REST 真实实现，超时/非200/status!=1/无结果→`GeoUnavailable`，Key 由注入，不回显）。
- `fairmeet/itinerary.py` — `ItineraryService`：天/条目/交通段 CRUD、条目**状态机**（草案→待讨论→待确认→已确认→已完成/已取消，非法流转拒绝）、`update_item` 局部更新、`add_transport(compute=True)` 经适配层算时长并**降级**为估算/未知。
- `fairmeet/checks.py` — `CheckService`：`budget()`（累计+按 provenance 分类+超预算判定+估算提示）、`check()`（超预算 warning、**硬约束冲突显式报告**、时间重叠 warning、交通未知 info；`ok=False` 当有 hard）。
- `fairmeet/templates/plan.html` — 行程编辑页（逐日时间线、检查结果、预算、改状态、加天/加条目）。
- `fairmeet/templates/map.html` — 时间线与**离线地图示意**（SVG 按坐标缩放散点+连线；无坐标点标注「无坐标」；调用 `/api/trips/{id}/map-data`）。
- `tests/test_adapters.py`（11）· `tests/test_itinerary.py`（13）· `tests/test_checks.py`（7）— 阶段 C 测试，全部离线。

### 修改
- `fairmeet/domain.py` — 追加阶段 C 模型/枚举：`ItemStatus`、`ItemType`（含 meal/meeting_point）、`ProvenanceKind`+`Provenance`、`TransportMode`、`ItineraryDay/ItineraryItem/TransportSegment`、输入模型、`ConflictItem/CheckResult/BudgetSummary`。阶段 B 内容保持不变。
- `fairmeet/db.py` — 追加 `itinerary_days`、`itinerary_items`、`transport_segments` 三表与索引（`CREATE TABLE IF NOT EXISTS`，阶段 B 表不受影响）。
- `fairmeet/app.py` — `create_app(db, geo)` 注入适配器；装配 `ItineraryService`/`CheckService`；新增阶段 C JSON 端点与 `plan`/`map` 页面路由。

### 保持原样（未触碰）
- `fairmeet/center.py`、`fairmeet/config.py`、`fairmeet/geocode.py` 与阶段 A/B 时一致（center/config/geocode 仍为既有未提交改动，内容未变；仅在 ruff 配置里对它们保留 per-file-ignore）。
- 阶段 B 的 services/domain 既有字段、templates（base/index/join/trip）、static 均未改动。

## 2. 设计决定

| 决定 | 取舍与理由 | 任务书一致性 |
|------|-----------|--------------|
| 适配层用 Protocol 端口 + Fake/Amap 两实现 | 领域只依赖端口，外部格式不渗入核心；测试用 Fake，断网可跑；真实高德按需切换 | 任务书 §7「外部格式不得渗入核心行程模型」 |
| 默认注入 FakeGeoAdapter | 阶段 C 演示/测试**不依赖高德**，符合 07「阶段 B 不需配额，C 前核实」；真实高德需核实 Key/配额后再开 | 07 决策 |
| 交通时长/费用均带 `Provenance` | 区分 verified/external/estimated/unknown，禁止把估算当精确数 | 任务书 §3.7/§5 |
| 硬约束冲突显式报告（severity=hard + ok=False） | 绝不自动放宽或静默忽略；未覆盖的硬约束至少 info 提示 | 任务书 §2.5/§3.4 |
| 状态机限制合法流转 | 草案/待讨论/待确认/已确认/已完成/已取消；非法流转拒绝 | 任务书 §3.8 |
| 局部更新只改指定列，状态独立端点 | **局部调整不无故覆盖其他已确认项**（有回归测试） | 任务书 §9.8 |
| 地图用离线 SVG 示意 | 真实高德 JS 需安全密钥/配额核实，先做可离线演示的坐标可视化并显式降级 | 任务书 §3.7 降级；07 决策 |
| 餐饮/会面点作为 `ItemType` 的行程条目 | 复用地点库+行程，不另起独立流程 | 任务书 §4 末段、§2.2 |
| 复用 `center.py` 作旅程内会面点能力 | 保留子能力不重定义主线；阶段 C 已在 ItineraryService 预留会面点条目类型 | 任务书 §6 阶段 C |

> 说明：`center.py` 的「球面公平中心」作为会面点算法子能力保留；阶段 C 将其定位为**旅程内选择集合点**的工具，通过地点库+行程条目接入，未作为独立产品入口。

## 3. 运行方式

```powershell
# 环境（依赖在阶段 B 已锁定；阶段 C 无新增第三方依赖）
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# 启动（默认 FakeGeoAdapter，无需高德 Key）
.\.venv\Scripts\python.exe -m uvicorn fairmeet.app:app --reload
# 打开 http://127.0.0.1:8000/ → 创建旅程 → 旅程空间 → 「行程计划」/「地图/时间线」

# 测试（断网可跑）
.\.venv\Scripts\python.exe -m pytest -q
# 静态检查
.\.venv\Scripts\python.exe -m ruff check fairmeet tests
```

- 环境变量：`FAIRMEET_DB_PATH`（数据库路径）；高德 Key 仅 `AMAP_WEB_KEY` 等，阶段 C 默认 Fake，不需真实调用。真实高德调用需在核实配额/Key 类型后，通过 `create_app(geo=AmapGeoAdapter(...))` 或后续开关显式启用。

## 4. 测试真实结果

- `pytest -q` → **54 passed**（阶段 B 23 + 阶段 C 31：adapters 11 / itinerary 13 / checks 7）。
- **断网**：阶段 C 全部测试用内存库 + FakeGeoAdapter；Amap 用 respx mock httpx，**不访问真实网络**，离线通过。
- `ruff check fairmeet tests` → **All checks passed**。
- **真实启动验证**：`uvicorn fairmeet.app:app` 启动成功；真实 HTTP 走通「建旅程→加天→加带坐标地点→加条目→预算 API→行程页 200→地图页 200→map-data 返回天数」，budget 合计正确（100.0）。
- **未运行/受限**：**未做真实高德调用验证**（配额/Key 类型/费用未核实，按 07 决策留待正式接入前核实）；地图为离线示意，未接真实高德 JS 地图。

## 5. 已知限制

- 高德适配器仅实现真实 REST 调用与错误收敛；**未在真实账号下验证**连通性/配额/坐标系（GCJ-02）细节。
- 地图为离线 SVG 散点示意，非真实底图；真实底图/路线绘制待接入高德 JS（需安全密钥）。
- 地点坐标需手动输入或后续接地理编码补齐；灵感自动解析仍未接入。
- 硬约束校验覆盖预算/日期/若干类别；更细的营业时间、绕行、密度校验留待后续增强（当前未知项以 info 显式提示，不静默）。
- 无变更历史/差异确认（阶段 E）；AI 修改提案（阶段 D）。

## 6. 未实现项（本阶段明确不做）

- AI 对话/结构化提案/受控工具 —— 阶段 D。
- 确认流程、变更历史、通知、出发前摘要、局部重排的差异展示 —— 阶段 E（阶段 C 已保证局部更新不覆盖其他项的基础机制）。
- 真实高德 JS 地图底图与安全密钥接入 —— 待核实后。
- 灵感自动解析（小红书/大众点评）—— 待合法性与稳定性验证。

## 7. 下阶段建议

- 阶段 D 准入：先做**结构化 I/O 契约 + 受控业务工具**（AI 只能经校验的 operation 提案，复用 `TripService`/`ItineraryService` 的授权与业务入口），再上对话协调；LLM 测试用 mock/假响应，断网可跑。
- 若要在真实高德验证：先确认 Web 服务与 JS API 的配额、频控、Key 类型、费用上限及 GCJ-02 坐标一致性，再切换 `AmapGeoAdapter` 并补集成测试（respx mock 保持离线门禁）。
- 阶段 E 的局部重排差异，可直接复用阶段 C 的 `ItemUpdate` 局部更新 + 状态机，配合变更历史与受确认项保护。
