# 阶段 B 交接记录 — 2026-11-11

> 依据：任务书 [docs/deepseek-harness-implementation-brief.md](../deepseek-harness-implementation-brief.md) + [已确认决策记录](07-confirmed-decisions.md)。
> 阶段 B 目标：打通「创建旅程 → 邀请 → 需求录入 → 灵感收集」，**无外部解析、无高德、无 LLM** 也能走完。

---

## 1. 改动摘要

### 新增（业务代码）
- `fairmeet/domain.py` — 领域模型：`Trip`、`Member`、`Need`、`Place`、`Inspiration`；枚举 `NeedKind`（硬约束/偏好/可协商）、`MemberStatus`、`InspirationStatus`、`TripStatus`；以及创建请求模型（`TripCreate`/`MemberJoin`/`NeedCreate`/`PlaceCreate`/`InspirationCreate`）。
- `fairmeet/db.py` — SQLite 持久化（标准库 `sqlite3`，零额外依赖）：连接/建表/事务；`FAIRMEET_DB_PATH` 可配置，测试用 `:memory:`。
- `fairmeet/services.py` — 业务服务层 `TripService`：旅程/成员/需求/地点/灵感的创建与查询，**邀请令牌不可猜**（`secrets.token_urlsafe(32)`），**授权校验**（`require_member` 断言成员属于该旅程，否则 `Forbidden`）。
- `fairmeet/app.py` — FastAPI 应用入口；`create_app(db)` 工厂（测试注入内存库）+ 模块级 `app`（uvicorn 用）。JSON API + 移动端 Jinja2 页面路由。
- `fairmeet/templates/` — `base.html`、`index.html`（创建旅程）、`join.html`/`join_invalid.html`（邀请加入）、`trip.html`（旅程空间：成员/需求/灵感箱/地点库）。
- `fairmeet/static/app.css` — 移动端优先的极简样式。
- `tests/test_services.py` — 服务层测试（10 项）。
- `tests/test_api.py` — HTTP/页面测试（13 项）。
- `requirements.txt` / `requirements-dev.txt` — 分层、锁版本、可复现依赖清单。
- `pyproject.toml` — pytest / ruff 配置。

### 修改
- `README.md` — 产品定位从「会面点推荐」切换为「完整出行规划」主线；补充阶段 B 快速开始（本机演示）。
- `.gitignore` — 新增忽略 `*.db` 等本地库文件。

### 保持原样（未触碰）
- `fairmeet/center.py`、`fairmeet/config.py`、`fairmeet/geocode.py`、`tests/test_center.py`、`docs/study-plan.md`、`docs/phase0-spec.md` 等既有未提交改动**均未改动**（仅对 center/config/geocode 在 ruff 配置里加了 per-file-ignore，内容一字未改）。

## 2. 设计决定

| 决定 | 取舍与理由 | 与任务书边界一致性 |
|------|-----------|---------------------|
| 持久化用标准库 `sqlite3`（非 SQLModel/SQLAlchemy） | 零额外依赖、可立即跑、便于断网测试；仓储边界通过 `Database` 类收敛，后续可替换 | 模块化单体、早期不引 Redis/Postgres（任务书 §7） |
| 成员身份用「不可猜邀请链接 + member_id」 | 阶段 B 本地演示够用；服务端下发的 `member_id` 参与授权校验 | 明确「链接≠生产认证」，公网前另定账户（07） |
| 授权集中在 `TripService.require_member` | 所有旅程内写操作先校验成员归属，杜绝跨旅程越权；为阶段 D 受控工具复用同一入口 | AI 不得绕过权限直写库（任务书 §5） |
| 需求分 `hard/preference/negotiable` | 硬约束显式建模，后续阶段可规则校验、显式报冲突，不静默放宽 | 任务书 §2.5、§3.4 |
| 灵感统一入「待确认」，带来源链接与状态机 | 阶段 B 不做解析；手动/粘贴均有来源，状态可人工修正 | 任务书 §3.3、§2.7 |
| 前端 Jinja2 + 原生 JS，无 SPA | 移动优先、邀请链接直接开、无构建链 | 用户确认（07） |
| 阶段 B 不依赖高德 | 手动添加地点即可走通主流程；测试不联网 | 07 |

## 3. 运行方式

```powershell
# 环境（首选 Python 3.14；实测 3.14.5 全依赖可装）
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# 启动
.\.venv\Scripts\python.exe -m uvicorn fairmeet.app:app --reload
# 打开 http://127.0.0.1:8000/

# 测试
.\.venv\Scripts\python.exe -m pytest -q
# 静态检查
.\.venv\Scripts\python.exe -m ruff check fairmeet tests
```

- 环境变量：`FAIRMEET_DB_PATH`（数据库路径，默认 `fairmeet.db`）；高德 Key（`AMAP_WEB_KEY` 等）阶段 B **不需要**，仅 `.env` 占位，测试不读取、不回显。
- 新环境从零：装依赖 →（可选）配 `.env` → `uvicorn` 启动 → 浏览器打开首页创建旅程。

## 4. 测试真实结果

- `pytest -q` → **23 passed**（`tests/test_center.py` 4 + `test_services.py` 10 + `test_api.py` 13，均为阶段 B 新增/既有；`test_config.py`、`test_geocode.py` 仍为空文件，未产生测试，如实说明）。
- **断网/mock**：阶段 B 全部测试使用内存库与 TestClient，**不访问任何外部网络/高德**，离线可跑（已在本机离线状态执行通过）。
- `ruff check fairmeet tests` → **All checks passed**。
- **真实启动验证**：`uvicorn fairmeet.app:app` 启动成功；`GET /api/health` → 200；`GET /` → 200；真实 HTTP 走通「创建旅程→API 加入→加需求→加灵感→成员/需求/灵感计数一致→旅程页 200→邀请页 200」。
- **未运行/受限**：无真实高德调用（阶段 B 本就不接）；无公开网络部署验证（本机演示范围）。

## 5. 已知限制

- 邀请链接为阶段 B 本机演示的轻量身份，**不是生产级认证**；无 HTTPS、无链接撤销、无正式账户。
- 地点无坐标（阶段 B 不接高德）；灵感无自动解析，状态需人工维护。
- 无逐日行程/地图/路线；无实时推送（普通请求/刷新）。
- 需求暂只做记录与分类，**硬约束冲突检测**的规则校验在阶段 C/D 补齐。

## 6. 未实现项（本阶段明确不做）

- 逐日行程编辑、地图、路线、POI/评分 —— 阶段 C。
- AI 对话/结构化提案/受控工具 —— 阶段 D。
- 确认流程、变更历史、通知、摘要、局部重排 —— 阶段 E。
- 链接自动解析（小红书/大众点评）—— 待合法性与稳定性验证后再定。
- 高德真实调用 —— 阶段 C 前核实配额/Key 类型后再接。

## 7. 下阶段建议

- 阶段 C 准入：先核实高德 Web 服务/JS API 的**配额、频控、Key 类型、费用上限**；建立 `adapters/` 适配层（Protocol 接口 + 高德实现 + mock 实现），保证断网可测。
- 以成都多日游为样例，落地逐日行程条目与交通段、草案/确认状态、时间线与地图联动、约束与预算校验（硬约束冲突显式报告）。
- 复用阶段 B 已建的 `require_member` 授权与 `TripService` 业务入口，避免 AI（阶段 D）绕过校验。
