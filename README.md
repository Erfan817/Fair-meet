# FairMeet

> 让一群人的旅行，从“各自收藏”变成“共同完成”。

**FairMeet** 是一个 AI 协助的多人共享出行规划工具。它把同行者的需求、约束、灵感和现实预算放进同一个旅程空间，逐步整理成一份**有来源、可讨论、可执行、能随变化调整**的共享计划。

当前项目处于 **阶段 B：旅程协作基础**。首轮验收场景是 **3–5 人、2–3 天、包含住宿与市内交通的国内游**，演示目的地为成都。

> 产品主线始终是“完整出行规划”。餐厅推荐、地点发现、路线计算和多人约饭，都会作为旅程中的可复用能力存在，而不是独立的产品定位。

[阶段 A 计划包](docs/plan/README.md) · [已确认决策](docs/plan/07-confirmed-decisions.md) · [快速开始](#快速开始)

## 为什么做 FairMeet

多人旅行最难的部分，通常不是找到一个景点，而是把不同人的“想去”“不能去”“可以商量”和“还没想好”放在一起，持续推进到可以出发的方案。

FairMeet 围绕这段协作过程设计：

- **先收集，再协调**：先让每个人表达需求与灵感，再进入共同决策。
- **区分约束与偏好**：把“必须满足”和“最好满足”分开，减少无效争论。
- **保留来源与上下文**：链接、地点、备注和状态一起保存，方便回看和讨论。
- **允许局部调整**：未来面对预算、时间或成员变化时，只重排受影响的部分。

## 当前可以做什么

阶段 B 已打通一条可运行的协作主流程：

| 能力 | 说明 |
| --- | --- |
| 创建旅程 | 记录标题、目的地、日期和预算，生成独立旅程空间 |
| 邀请同行者 | 通过不可猜的邀请链接加入，手机浏览器即可打开 |
| 录入成员需求 | 支持硬约束、偏好、可协商三类需求 |
| 管理灵感箱 | 手动添加地点、粘贴攻略链接、补充说明和来源 |
| 维护地点库 | 灵感中的地点可进入旅程地点库，后续补齐坐标和更多信息 |
| JSON API | 提供旅程、成员、需求、地点和灵感的基础接口 |
| 移动端页面 | 使用 Jinja2 服务端渲染，优先保证邀请协作场景可用 |

当前阶段明确**不包含**：高德在线调用、LLM 协调、WebSocket 实时同步、逐日行程编辑、地图与路线规划。这些能力会在后续阶段逐步加入；没有外部 Key 也可以完整演示当前协作流程。

## 技术栈

- **运行时**：Python 3.11+（开发环境首选 Python 3.14）
- **Web**：FastAPI · Uvicorn · Jinja2
- **数据**：Pydantic v2 · SQLite
- **测试**：pytest · pytest-asyncio · respx
- **质量**：Ruff

## 快速开始

### 1. 创建虚拟环境并安装依赖

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

如果本机没有 Python 3.14，将命令中的版本替换为已安装的 Python 3.11 或更高版本即可。

### 2. 启动开发服务器

```powershell
.\.venv\Scripts\python.exe -m uvicorn fairmeet.app:app --reload
```

打开 <http://127.0.0.1:8000/>，即可创建第一趟旅程。

### 3. 运行测试与代码检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

测试默认不访问真实网络，断网环境也可以运行。

### 可选配置

复制 `.env.example` 为 `.env`，再按需填写高德服务配置。阶段 B 不依赖这些 Key；它们为后续地理编码、地图展示和路线能力预留：

```dotenv
AMAP_WEB_KEY=
AMAP_JS_KEY=
AMAP_JS_SECURITY_CODE=
```

数据库默认写入项目根目录的 `fairmeet.db`。可以通过 `FAIRMEET_DB_PATH` 指定数据库位置，例如：

```powershell
$env:FAIRMEET_DB_PATH = ":memory:"
```

## API 入口

启动服务后，可以访问：

- OpenAPI 文档：<http://127.0.0.1:8000/docs>
- 健康检查：`GET /api/health`
- 旅程：`POST /api/trips`、`GET /api/trips/{trip_id}`
- 成员：`POST /api/trips/{trip_id}/members`、`GET /api/trips/{trip_id}/members`
- 需求：`POST /api/trips/{trip_id}/needs`、`GET /api/trips/{trip_id}/needs`
- 地点：`POST /api/trips/{trip_id}/places`、`GET /api/trips/{trip_id}/places`
- 灵感：`POST /api/trips/{trip_id}/inspirations`、`GET /api/trips/{trip_id}/inspirations`

也可以直接使用页面入口：

- `/`：创建旅程
- `/join/{token}`：通过邀请链接加入
- `/trip/{trip_id}?member={member_id}`：查看并协作旅程空间

## 项目结构

```text
fairmeet/
├── app.py          # FastAPI 应用、页面路由与 JSON API
├── config.py       # 环境变量与配置
├── db.py           # SQLite 持久化
├── domain.py       # Pydantic 数据模型与领域枚举
├── services.py     # 旅程、成员、需求、灵感等业务服务
├── geocode.py      # 地理编码能力骨架
├── templates/      # Jinja2 页面模板
└── static/         # 页面静态资源

tests/              # API、配置、地理编码与服务层测试
docs/               # 计划包、学习记录与开发文档
```

## 路线图

| 阶段 | 目标 | 状态 |
| --- | --- | --- |
| A | 产品与工程基线：架构、数据模型、流程、风险和验收标准 | 已完成 |
| B | 旅程协作基础：入口、持久化、旅程/成员、需求和灵感箱 | 进行中 |
| C | 可编辑计划与地图：逐日行程、时间线、地图、约束与预算校验 | 待开始 |
| D | AI 协调与来源增强：结构化输入输出、受控工具和对话协调 | 待开始 |
| E | 出发前与途中体验：确认、变更历史、通知、摘要和局部重排 | 待开始 |

## 文档导航

- [阶段 A 计划包](docs/plan/README.md)：架构、数据模型、用户流程、分阶段计划与风险
- [目标架构与技术选型](docs/plan/02-architecture.md)
- [旅程数据模型](docs/plan/03-data-model.md)
- [端到端用户流程](docs/plan/04-user-flows.md)
- [分阶段计划与验收](docs/plan/05-phases-and-acceptance.md)
- [风险与待验证事项](docs/plan/06-risks-and-open-questions.md)
- [已确认决策记录](docs/plan/07-confirmed-decisions.md)
- [学习计划与进度](docs/study-plan.md) · [开发日志](docs/devlog.md) · [评审记录](docs/review-2026-09-21.md)
- [历史需求规格说明书](docs/phase0-spec.md)：早期“会面点推荐”阶段 0 规格，仅作参考

## 当前边界与安全提示

阶段 B 的邀请链接用于本机演示和早期协作验证，并不等同于生产级身份认证。正式公网部署前，还需要补充账户体系、权限模型、邀请链接失效策略、敏感信息保护和审计能力。

## License

MIT（LICENSE 文件待补充）
