# 02 · 目标架构与技术选型

> 依据：[任务书](../deepseek-harness-implementation-brief.md) 第 2、5、7、8 节；[阶段 A 计划包说明](README.md)。
> 性质：可审阅的目标架构方案，供用户确认。**本文件不引入代码改动**，只描述结构、职责与选型理由。
> 核心原则：**优先模块化单体，不早拆微服务；领域模型与外部提供方解耦；无实测依据不引入 Redis/微服务/复杂 Agent 框架。**

---

## 1. 分层架构图（ASCII）

自上而下的依赖方向：上层依赖下层，下层不认识上层；**领域层不依赖任何外部格式**，外部能力经端口（Protocol/ABC）反转注入。

```
                          ┌───────────────────────────────────────────────┐
                          │              表现层 / 接口层                    │
                          │  fairmeet/api   (JSON/HTTP 接口, Pydantic 校验) │
                          │  fairmeet/web   (移动端 HTML: Jinja2 + HTMX/JS) │
                          │  ── 邀请链接 / 旅程空间 / 行程时间线 / 地图页 ──  │
                          └───────────────────────┬───────────────────────┘
                                                  │ 调用（DTO / 命令 / 查询）
                                                  ▼
                          ┌───────────────────────────────────────────────┐
                          │           应用 / 业务服务层                     │
                          │  fairmeet/services                              │
                          │  旅程服务 成员与需求 灵感箱 地点库 方案比较       │
                          │  行程编辑器 路线编排 协作决策 行程检查与预算      │
                          │  通知与变更 摘要导出                            │
                          │  ── 硬约束校验 / 状态机流转 / 审计 / 事务边界 ──  │
                          └───────┬───────────────────────────────┬────────┘
                                  │ 依赖（纯 Python 对象）          │ 经端口调用
                                  ▼                               ▼
          ┌───────────────────────────────────┐   ┌──────────────────────────────────────┐
          │         领域模型层                 │   │        外部适配器层（端口+实现）        │
          │  fairmeet/domain                   │   │  fairmeet/adapters                     │
          │  实体/值对象/聚合/状态机约束        │   │  ┌────────────────────────────────┐   │
          │  Journey / Member / Requirement    │   │  │ ports.py  定义 Protocol/ABC 端口 │   │
          │  Inspiration / Place / PlanItem    │   │  │  MapPort POIPort RoutePort      │   │
          │  Proposal / CheckReport / AuditLog │   │  │  GeocodePort ContentPort AIPort │   │
          │  ── 无 fastapi / httpx / amap 痕迹 ──│   │  └───────────────┬────────────────┘   │
          └───────────────────────────────────┘   │       实现：                          │
                                                   │  amap.py        (默认, 高德)          │
                                                   │  fake_mock.py   (离线 mock, 测试/降级) │
                                                   │  content/       (链接/攻略解析, 可降级)│
                                                   │  ai_provider.py (LLM 结构化输出)       │
                                                   └───────────────┬──────────────────────┘
                                                                   │ HTTPS
                                                                   ▼
                                                   ┌────────────────────────────┐
                                                   │   外部服务（高德/内容源/LLM）│
                                                   └────────────────────────────┘

  持久化：fairmeet/persistence（SQLite 内嵌，仓储实现 domain 仓储端口）
  AI 协调：fairmeet/ai ── 只经 services 的受控业务工具，产生「操作提案(Proposal)」
```

### 1.1 AI 层边界（硬约束，对应任务书第 5 节）

```
  用户自然语言
       │
       ▼
 ┌──────────────┐   结构化输入/输出 (JSON Schema 校验)   ┌──────────────────────────┐
 │ fairmeet/ai  │ ───────────────────────────────────▶ │ fairmeet/services        │
 │ 协调助手      │                                       │ 受控业务工具(白名单)      │
 │ (LLM 提供方)  │ ◀─── 只读查询结果 / 工具返回 ──────── │ 校验权限/硬约束/生成提案  │
 └──────┬───────┘                                       └───────────┬──────────────┘
        │ 生成 Proposal（不改状态）                                  │ 用户确认后
        ▼                                                           ▼
   提案草稿 ──▶ 展示差异与影响 ──▶ 成员确认 ─────────────▶ 服务层执行状态变更 + 审计
                                                               │
                                                               ▼
                                                         持久化 (SQLite)

  ✗ AI 不得直接读写数据库
  ✗ AI 不得绕过权限或静默修改已确认计划
  ✓ AI 只能：只读查询 + 提交经校验的「业务操作提案」，由服务层落地
```

**要点：**
- `domain` 层只含纯 Python 实体与规则，**不 import** fastapi/httpx/amap/pydantic 等外部格式。
- `services` 依赖 `domain` 与 `adapters/ports`（依赖倒置）：业务逻辑面向端口编程，运行期注入具体适配器。
- `adapters` 隔离高德/内容源/LLM 的私有格式（GCJ-02 坐标、原始 JSON 字段），统一转为领域 DTO。
- 现状对照：现有 `fairmeet/center.py`（球面质心纯函数）迁移为 `domain/geo.py` 的领域算法；`fairmeet/geocode.py`（同步高德调用）重构为 `adapters/amap.py` 实现 `GeocodePort`，改为可注入且可 mock。

---

## 2. 模块划分与目录建议

对应任务书第 4 节 12 个功能模块，给出包结构。**依赖方向自上而下，`domain` 零外部依赖。**

### 2.1 目录结构建议

```
fairmeet/
├── __init__.py
├── config.py                 # 配置与密钥（仅从 env/.env 读，脱敏）
├── app.py                    # 组合根：装配 adapters → services → api/web（工厂函数）
├── domain/                   # 领域模型层（纯 Python，无外部格式渗入）
│   ├── models.py             # Journey, Member, Requirement, Inspiration, Place, PlanItem, Proposal ...
│   ├── enums.py              # 状态机枚举：草案/待讨论/待确认/已确认/已完成/已取消; 硬约束/偏好/可协商
│   ├── geo.py                # 球面质心（迁移自 center.py）、距离等纯几何
│   ├── constraints.py        # 硬约束校验规则（日期/预算/无障碍…），确定性、可单测
│   └── provenance.py         # 来源、核实状态、时间戳、不确定性值对象
├── services/                 # 应用/业务服务层
│   ├── journey_service.py    # 模块1 旅程空间
│   ├── member_service.py     # 模块2 成员与需求
│   ├── inspiration_service.py# 模块3 灵感箱与来源
│   ├── place_service.py      # 模块4 地点库（规范化/去重/合并）
│   ├── option_service.py     # 模块5 方案比较
│   ├── itinerary_service.py  # 模块6 行程编辑器（局部调整）
│   ├── routing_service.py    # 模块7 地图与路线编排（经 RoutePort）
│   ├── collab_service.py     # 模块8 协作决策（评论/投票/确认/变更）
│   ├── check_service.py      # 模块10 行程检查与预算
│   ├── notify_service.py     # 模块11 通知与变更（先同步实现）
│   └── export_service.py     # 模块12 行程摘要与导出
│       # 模块9 AI 协调助手主体在 fairmeet/ai，其可调用的业务能力由上面服务暴露为受控工具
├── adapters/                 # 外部适配器层
│   ├── ports.py              # MapPort/POIPort/RoutePort/GeocodePort/ContentPort/AIPort（Protocol）
│   ├── amap.py               # 高德实现（默认）：geocode/POI/route，GCJ-02
│   ├── fake.py               # 离线 mock 实现（测试与降级）
│   ├── content/              # 链接/攻略解析（可解析失败→手动）
│   └── ai_provider.py        # LLM 提供方（结构化输出），可 mock
├── persistence/              # 持久化（SQLite 内嵌）
│   ├── db.py                 # 连接/事务/迁移入口
│   ├── schema.sql
│   └── repositories.py       # 仓储实现（domain 仓储端口的具体落地）
├── ai/                       # AI 协调层（module 9）
│   ├── orchestrator.py       # 澄清/总结/提案编排
│   ├── tools.py              # 受控业务工具白名单（只读查询 + 提案，不含直接写库）
│   └── schemas.py            # 结构化输入输出 Schema（提案/抽取结果的校验）
├── api/                      # 表现层：JSON/HTTP
│   ├── routes_*.py           # journeys/members/inspirations/plan/check/...
│   └── deps.py               # 依赖注入、权限检查、错误映射
└── web/                      # 表现层：移动端 HTML
    ├── routes_web.py         # 页面路由（邀请链接落地、旅程空间、时间线、地图）
    ├── templates/            # Jinja2 模板
    └── static/               # 原生 JS/HTMX + 少量 CSS
```

### 2.2 12 个功能模块 → 归属

| # | 功能模块 | 归属 | 说明 |
|---|----------|------|------|
| 1 | 旅程空间 | `services/journey_service` + `domain` | 基本信息、状态、共享权限 |
| 2 | 成员与需求 | `services/member_service` | 加入、硬约束/偏好/可协商、回应状态 |
| 3 | 灵感箱与来源 | `services/inspiration_service` + `adapters/content` | 导入/解析/去重/人工修正 |
| 4 | 地点库 | `services/place_service` + `adapters`(Geocode/POI) | 统一地点与分店、坐标、合并 |
| 5 | 方案比较 | `services/option_service` | 候选方案差异 |
| 6 | 行程编辑器 | `services/itinerary_service` | 逐日行程、局部调整 |
| 7 | 地图与路线 | `services/routing_service` + `adapters`(Route) | 数据不可用明确降级 |
| 8 | 协作决策 | `services/collab_service` | 评论/投票/确认/变更记录 |
| 9 | AI 协调助手 | `ai/` + `services` 工具 | 受控提案，不直连库 |
| 10 | 行程检查与预算 | `services/check_service` + `domain/constraints` | 确定性校验、费用汇总 |
| 11 | 通知与变更 | `services/notify_service` | 先同步，按需再异步 |
| 12 | 行程摘要与导出 | `services/export_service` | 出发前摘要、分享视图 |

### 2.3 依赖方向（依赖倒置）

```
api/web  ──▶ services ──▶ domain
                 │
                 └──▶ adapters/ports (Protocol) ◀── adapters/amap | fake | content | ai_provider
                 │
                 └──▶ persistence/repositories (实现 domain 仓储端口)

domain  ⟂ 不依赖任何上层、不 import 任何外部库
services 面向端口编程，运行期由 app.py 注入具体实现
```

- **领域不依赖外部格式**：高德 GCJ-02、原始 JSON、LLM 返回结构只在 `adapters` 内被转换成领域 DTO；核心行程模型永远持有规范化后的值对象（含来源/核实状态）。
- **换实现不改业务**：替换地图提供方或 AI 提供方只需新增 `adapters` 实现并改组合根，`services`/`domain` 不动；这直接满足任务书「换实现不需要改动其他文件」的要求，也避免早期锁死单一供应商。

---

## 3. 技术选型与理由

### 3.1 后端 Web 框架：沿用 FastAPI + Pydantic v2（建议）

**结论：采用 FastAPI + Pydantic v2；需新增依赖 `fastapi`、`pydantic`、`uvicorn`。当前环境未安装，属新增依赖，须在计划中明示。**

| 维度 | 说明 |
|------|------|
| 为什么沿用 | 任务书与既有 `docs/phase0-spec.md` 已按 FastAPI 设计（FR-7 HTTP API、依赖注入）；Pydantic v2 天然承担 DTO 校验、请求/响应契约、结构化输出 Schema，与「AI 结构化输入输出 + 校验」正好共用一套模型。生态成熟、单机即可跑、易离线测试。 |
| 风险 | ①当前未安装，`python -m pip install fastapi pydantic uvicorn` 会拉入 starlette/anyio 等传递依赖；②Pydantic v2 与 v1 API 差异大，需锁定 `pydantic>=2`；③Python 3.14.5 较新，需确认依赖有对应 wheel（如遇 wheel 缺失，退回 3.12 虚拟环境运行属备选）。 |
| 缓解 | 依赖锁定在 `pyproject`/`requirements`；API 与业务分离，框架只出现在 `api/`，即便将来换框架也不影响 `domain`/`services`。 |
| 备选 | Flask（更轻但无原生校验/异步，Pydantic 仍要另加）；标准库 http.server（不推荐，工程化能力弱）。 |

### 3.2 前端：服务端模板 Jinja2 + 原生 JS / HTMX（推荐，轻量优先）

**结论：移动优先选「Jinja2 服务端渲染 + 原生 JS + 可选 HTMX 渐进增强」，不引入独立 SPA 框架。**

| 维度 | 服务端模板（推荐） | 独立 SPA（Vue/React） |
|------|-------------------|----------------------|
| 邀请链接可访问 | ✅ 直接 GET 落地页，无构建步骤，手机浏览器即开 | 需打包产物与路由，首屏更重 |
| 移动优先 | ✅ 响应式 CSS 足够 | 可行但工程量大 |
| 开发成本 | ✅ 低，无前端构建链 | ❌ 需 node/构建/状态管理 |
| 协作实时性 | 先用普通请求/局部刷新验证流程（任务书第 10 节认可） | 需额外框架与状态同步 |
| 过度工程风险 | 低 | 高（当前阶段无实测需求） |
| 建议用途 | 旅程空间、邀请落地、需求录入、行程时间线、地图页 | 暂不需要 |

- 地图展示仍用**高德 JS API**（前端引脚本即可），与 POI/路线后端适配层解耦。
- 实时协作**不预先上 WebSocket**：先用请求+刷新/HTMX 局部更新验证流程，确有需要再演进（对应任务书：先完整单旅程端到端）。
- 新增依赖：`jinja2`（当前未安装，须新增）。

### 3.3 持久化：先 SQLite（模块化单体内嵌）

**结论：采用 SQLite 内嵌持久化，经 `persistence` 仓储层访问；不引入 Redis、不引入独立数据库服务。**

- 理由：单旅程端到端阶段数据量小、并发低；SQLite 零运维、随进程内嵌、便于本地与全新环境「5 分钟跑起来」，事务能保证状态流转与审计一致。
- 抽象：仓储以端口形式定义，SQLite 只是实现；若后续确有并发/规模需求再评估替换（Postgres），业务层不改。**在无实测依据前不预先引入 Redis 缓存**（任务书明确禁止）。
- 驱动：Python 标准库 `sqlite3` 即可，零新增依赖。

### 3.4 外部适配器：可替换接口（Protocol/ABC），默认高德，支持 mock

**结论：在 `adapters/ports.py` 用 `typing.Protocol` 定义地理编码/POI/路由/地图/内容解析/AI 端口；`amap.py` 为默认实现，`fake.py` 为离线 mock。**

- 端口：`GeocodePort`、`POIPort`、`RoutePort`、`MapPort`、`ContentPort`、`AIPort`。
- 默认实现高德（现 `geocode.py` 重构为 `amap.py` 实现 `GeocodePort`，识别失败抛领域异常而非外部异常）。
- **mock 离线**：测试用 `fake.py`（配合已安装的 `respx` 拦截 httpx），关键测试断网可跑。
- **解析失败可降级手动**：`ContentPort` 解析失败时不阻塞规划，状态置「解析失败」，由成员手动补充/修正（任务书第 2 节第 7 条）。
- 坐标系：全程 GCJ-02（沿用高德返回坐标），转换只在适配器内处理，不渗入领域。

### 3.5 AI 协调层：结构化输入输出 + 受控业务工具，不引入重框架

**结论：`fairmeet/ai` 用「结构化输入输出 Schema 校验 + 白名单受控业务工具（function-calling 式）」实现，不引入 LangChain 等复杂 Agent 框架。**

- 流程：LLM 产出结构化 JSON（抽取结果/提案），经 Pydantic Schema 校验；涉及改动的动作只能生成 `Proposal`（操作提案），由 `services` 校验权限与硬约束、展示差异、经成员确认后才落地并写审计。
- 不直连库：`ai/tools.py` 只暴露只读查询与「提交提案」，不含直接写库路径。
- 无重框架理由：当前是单旅程、受控工具、少量编排，重框架增加不可控性且难测试（任务书：无测量依据不引入复杂 Agent 框架）。LLM 提供方经 `AIPort` 注入，测试可 mock。

---

## 4. 横切关注点

| 关注点 | 方案 |
|--------|------|
| **配置与密钥管理** | 仅从环境变量/`.env` 读取（沿用 `config.py` 的 `load_dotenv` 模式并加类型校验）；`.env` 不入库、`.env.example` 提供模板；启动时缺失关键密钥给出明确错误。 |
| **错误与降级** | 外部服务超时/限流/无结果 → 领域化异常 + 可理解错误信息 + 手动降级路径（灵感解析失败、路线不可用、地理编码失败均可手动补充）。API 层统一错误映射，不向用户泄漏内部栈。 |
| **数据来源 / 核实状态 / 时间戳** | `domain/provenance.py` 值对象：来源类型、来源链接、核实状态（已识别/待确认/已核实/解析失败/已排除）、获取时间、不确定性标记。规则/地图数值标「外部数据/估算/未知」，**不得由 LLM 凭空给精确数字**。 |
| **权限** | 成员仅能访问被授权的旅程；邀请链接带作用域令牌；权限检查在 `api/deps.py` 与 `services` 双层校验；退出/删除/隐私行为明确。 |
| **审计与变更历史** | 每次状态变更写 `AuditLog`（谁/何时/改了什么/影响范围/数据来源）；版本/历史支持理解与恢复；AI 提案与用户确认均记录。 |
| **日志脱敏** | 结构化日志，密钥、位置、健康/饮食等敏感字段脱敏或省略；禁止打印密钥与无关敏感信息（任务书第 8 节）。 |
| **可测试性** | API / 业务逻辑 / 外部适配器分层测试；适配器用 fake + respx mock，关键测试离线运行；领域规则（约束、几何、状态机）纯函数单测。 |

---

## 5. 非目标（明确不做）

- **不做订票 / 支付 / 订单履约**：可外链或预留接口，不擅自实现交易系统（任务书第 2 节第 8 条）。
- **早期不做微服务拆分**：坚持模块化单体。
- **不预先引入 Redis 缓存 / 消息队列 / 异步任务基础设施**：先完成单旅程端到端，按实测需求再演进。
- **不引入复杂 Agent 框架 / 重前端 SPA 框架**：以受控工具 + 服务端模板满足当前需求。
- **不把不稳定抓取当生产依赖**：社交平台链接解析先验证合法性/稳定性，失败降级手动。
- **不做实时协同 WebSocket**（首阶段）：先用请求/刷新验证流程。

---

## 6. 关键设计决策记录（ADR）

| 编号 | 决策 | 选项 | 选择 | 理由 | 风险 / 缓解 |
|------|------|------|------|------|-------------|
| ADR-1 | 部署形态 | 微服务 / 模块化单体 | **模块化单体** | 单旅程先跑通、降低运维与复杂度；未来可按边界拆分 | 单体易腐化 → 以清晰分层与端口解耦约束 |
| ADR-2 | 后端框架 | 沿用 FastAPI+Pydantic v2 / Flask / 标准库 | **FastAPI + Pydantic v2** | 与既有规格一致，校验与结构化输出共用 Pydantic，生态成熟 | 全新依赖；3.14 wheel 兼容 → 锁版本、必要时 3.12 环境 |
| ADR-3 | 前端形态 | 服务端模板+原生JS/HTMX / 独立 SPA | **Jinja2 + 原生 JS/HTMX** | 移动优先、邀请链接即开、无构建链，避免过度工程 | 交互复杂度上限 → 复杂交互按需渐进增强 |
| ADR-4 | 持久化 | SQLite / Postgres / Redis | **SQLite** | 内嵌零运维、本地易启动、事务保证审计一致 | 并发/规模受限 → 仓储抽象，按需替换 |
| ADR-5 | 外部能力接入 | 供应商 SDK 硬编码 / 端口抽象 | **Protocol 端口 + 高德默认 + mock** | 领域不依赖外部格式，可替换、可离线测试、可降级 | 抽象层维护成本 → 端口粒度适中，仅覆盖必要能力 |
| ADR-6 | AI 接入方式 | 重 Agent 框架直连 / 结构化输出+受控工具 | **结构化输出 + 受控业务工具** | 可控、可测、不直连库、符合安全边界 | 工具能力需逐步扩充 → 白名单 + Schema 校验 |
| ADR-7 | 实时协作 | WebSocket / 普通请求+刷新 | **先普通请求+刷新（HTMX 渐进）** | 无实测并发需求，先验证端到端主链路 | 实时性不足 → 有需求再引入 WS |
| ADR-8 | 缓存 | 预置 Redis / 进程内+SQLite | **不预置 Redis** | 任务书禁止无依据引入；现阶段无测量依据 | 热点性能 → 确有数据再评估 |

---

> 后续文档：[03 数据模型草案](03-data-model.md) · [04 端到端用户流程](04-user-flows.md) · [05 分阶段计划与验收](05-phases-and-acceptance.md) · [06 风险与待验证事项](06-risks-and-open-questions.md)
