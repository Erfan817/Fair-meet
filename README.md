<p align="center">
  <img src="docs/assets/fairmeet-banner.svg" alt="FairMeet — Different plans. One shared journey." width="100%" />
</p>

<h1 align="center">FairMeet</h1>

<p align="center">
  <strong>把每个人的想法，排进同一趟旅程。</strong><br />
  多人协作 · 逐日行程 · AI 提案 · 途中调整
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-155E83?style=flat-square" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/Web-FastAPI-155E83?style=flat-square" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Storage-SQLite-155E83?style=flat-square" alt="SQLite" />
  <img src="https://img.shields.io/badge/Status-Early%20development-E4A63A?style=flat-square" alt="早期开发中" />
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#核心能力">核心能力</a> ·
  <a href="#配置外部服务">服务配置</a> ·
  <a href="#开发与测试">开发文档</a> ·
  <a href="https://github.com/Erfan817/Fair-meet/issues">反馈问题</a>
</p>

---

## 旅行不是一个人的待办清单

有人想去博物馆，有人想吃火锅；有人预算有限，有人周日必须返程。攻略散落在聊天记录和收藏夹里，最后往往只剩一个人在排计划。

**FairMeet 是一个 AI 协助的多人共享出行规划应用**：让同行者一起收集灵感、表达需求、讨论安排，把零散想法整理成可编辑的逐日行程。出发前核对预算与冲突，途中有变化时，调整受影响的安排，而不是从头再来。

> **项目状态**：A–E 阶段的原型主流程已实现，当前仍是早期开发版本，适合本地体验与功能验证，**不是可直接公网部署的生产服务**。首轮验证场景为成都 3–5 人、2–3 天的国内游。

## 为什么不是又一个 AI 行程助手

个人 agent 会替一个人查资料、排行程、改计划。FairMeet 不做同一件事，而是做它结构上做不到的部分：**几个人在同一份账本上提案、核对、确认**。

- **一个人说了不算的约束**：谁的硬约束被违反，由确定性规则判断，模型不能放宽。
- **改了什么看得见**：AI 只能提交待确认提案，确认前不修改行程；确认后保留版本与变更历史。
- **信息有出处**：外部数据、估算与未知分开标注，某个助手说得再笃定也不能把它变成事实。

因此方向不是和用户的助手竞争，而是让它接入：成员自己的 agent 可以通过接口提交需求、灵感与提案，**确认权仍属于人**。产品形态也不是一个入口级应用，而是一个跟着群聊走的「旅程房间」——人用免登录链接、agent 用受委托工具，读写同一份不可私改的账本。完整判断、形态设计、接口草案与 F–H 路线见 [个人 Agent 时代的定位与路线图](docs/plan/agent-era-positioning.md)。

## 核心能力

| | 能力 | 你可以做什么 |
| :---: | --- | --- |
| 🤝 | **一起规划** | 创建共享旅程，通过邀请链接加入；分别记录硬约束、个人偏好和可协商需求。 |
| 📌 | **灵感有出处** | 收集地点、攻略链接和备注，保留来源与状态；带地点名的灵感可进入地点库。 |
| 🗓️ | **行程能编辑** | 按天安排活动、餐饮、住宿、会面点与交通，管理条目状态、费用和时间。 |
| 🧭 | **信息有依据** | 查看时间线与坐标示意图；区分外部数据、估算与未知信息，不把估算当作事实。 |
| 🧮 | **预算与冲突可见** | 查看费用汇总、时间重叠、预算超支及规则覆盖范围内的硬约束问题。 |
| 💬 | **AI 提案，人来决定** | 用自然语言提出调整意图；模型输出经结构化校验，变更以待确认提案呈现。 |
| 🎒 | **从出发前到途中** | 查看出发前摘要、途中页面、变更历史与版本快照；局部重排指定天的条目。 |

餐厅推荐、多人约饭、地点发现和路线计算，都是完整旅程内的能力。FairMeet 的主线是**共同完成一趟旅行的规划**，而不是只计算一个会面点。

### 一趟旅程如何推进

1. **创建旅程**：填写目的地、日期与预算，把邀请链接发给同行者。
2. **表达需求**：分别说清“必须满足”“比较喜欢”和“可以商量”的事。
3. **收集灵感**：把地点、攻略链接与理由放进同一个旅程空间。
4. **安排与核对**：编辑逐日行程，查看交通来源、预算和冲突提示。
5. **讨论后确认**：查看 AI 提案及其影响，再决定是否执行。
6. **带着计划出发**：使用摘要和途中页面，有变化时进行局部调整。

## 快速开始

**准备条件**：Python 3.11+、Git；开发环境首选 Python 3.14。首次安装依赖需要联网，**不配置高德或 DeepSeek Key 也可以体验本地演示模式**。

### 1. 获取代码

```bash
git clone https://github.com/Erfan817/Fair-meet.git
```

```bash
cd Fair-meet
```

### 2. 创建环境并启动

<details open>
<summary><strong>Windows（Git Bash）</strong></summary>

```bash
py -3.14 -m venv .venv
```

```bash
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
```

```bash
./.venv/Scripts/python.exe -m uvicorn fairmeet.app:app --reload
```

若未安装 Python 3.14，将 `-3.14` 换成已安装的 Python 3.11 或更高版本。

</details>

<details>
<summary><strong>macOS / Linux</strong></summary>

```bash
python3 -m venv .venv
```

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
```

```bash
.venv/bin/python -m uvicorn fairmeet.app:app --reload
```

请先确认 `python3` 的版本不低于 3.11。

</details>

### 3. 打开第一趟旅程

访问 **<http://127.0.0.1:8000/>**，创建旅程后即可录入需求、添加灵感、编辑行程。

- **交互式 API 文档**：<http://127.0.0.1:8000/docs>
- **健康检查**：<http://127.0.0.1:8000/api/health>
- **默认数据文件**：当前工作目录中的 `fairmeet.db`

> 邀请链接需要接收方能访问同一个服务。`127.0.0.1` 只供本机使用，不能直接作为手机或其他设备的访问地址；请勿为了体验邀请功能而将当前原型直接暴露到公网。

### Render 部署配置

仓库提供 [`render.yaml`](render.yaml)，可在 Render 的 **New → Blueprint** 中连接此仓库。配置使用 `requirements.txt` 安装运行依赖，并通过 Uvicorn 启动服务。

- 默认使用模拟地理适配器（`FAIRMEET_GEO=fake`）；若需真实高德，应配置 Key 并调整该开关。
- 可按需配置 `DEEPSEEK_API_KEY` 启用真实模型，不要将 Key 写入代码。
- SQLite 位于 `/tmp/fairmeet.db`，实例重启或重新部署后数据可能丢失。
- 此配置仅提供部署起点，不代表安全加固完成。当前缺少生产级认证，公开展示前应增加访问保护，并仅使用虚构数据。

## 配置外部服务

将 [`.env.example`](.env.example) 复制为 `.env`，按需配置。**请勿提交 `.env`、真实 Key 或个人旅程数据。**

| 配置项 | 默认值 / 行为 | 用途 |
| --- | --- | --- |
| `AMAP_WEB_KEY` | 未配置时使用离线地理适配器 | 高德服务端地理编码、POI 查询与路线数据 |
| `FAIRMEET_GEO` | `auto` | `auto` 按 Key 选择；`fake` 强制模拟；`amap` 请求启用高德，无 Key 时回退模拟 |
| `DEEPSEEK_API_KEY` | 未配置时使用 `FakeLLM` | 启用真实 DeepSeek 对话与提案生成 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 模型服务地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名称 |
| `FAIRMEET_DB_PATH` | `./fairmeet.db` | SQLite 文件路径，也支持 `:memory:` 临时数据库 |

**演示模式与真实服务的区别：**

- **不填 Key**：地理数据和模型响应使用模拟实现，适合离线走通交互；不代表真实地点、路线或模型规划质量。
- **填写 Key**：应用按配置调用真实服务，会产生网络请求，并可能消耗第三方配额或费用。旅程上下文或地点查询会发送到对应服务，请勿提交不必要的敏感信息。
- **地图展示**：当前是离线 SVG 坐标示意图，**不是可交互的高德地图底图**。`AMAP_JS_KEY` 和 `AMAP_JS_SECURITY_CODE` 为后续地图能力预留，当前不使用。

地理服务不可用时，相关数据可降级为估算或未知；不要把降级结果当作实时导航依据。接入细节见 [真实外部服务说明](docs/plan/real-integrations.md)。

## 规划前资料检索（首版）

在旅程的 **协调助手** 中输入需求，点击 **先查资料**，可查看候选、风险和来源取得范围，再保存为灵感。普通“发送”仍走原有聊天，不会偷偷联网。

- **公开真实搜索**：新增无需密钥的 DuckDuckGo 公开 HTML 搜索，必须由当前成员明确授权并点击才联网，默认每日最多 20 次。遇到验证码/限流会停止本实例后续请求，不绕过限制；本轮首次请求成功，后续实测遇验证页，不能承诺稳定可用。
- **地点搜索与草案**：新增高德真实地点搜索，明确标为地图记录而非点评/攻略；真实摘要或地点记录可生成 1–3 天的待确认活动草案。可选 DeepSeek 抽取可逐字核对的地点，时间由本地规则安排。模型与高德核对分别授权、默认关闭，确认前不修改行程。
- **不伪造事实**：营业、预约、票价未知时仍标待核实，不用估算时间冒充开放时间；存在未核实成员硬约束时阻止自动草案，保留人工规划。
- **离线与可选来源**：仍保留成都虚构资料演示；Brave 需密钥、验收开关及配额，尚未真实验收。均不读取任意网页正文或平台完整评论，不代表接通小红书、大众点评或美团。
- **隐私与费用**：搜索只发送目的地、明确填写的范围和固定主题词；模型只接收必要公开摘要和目的地，不发成员敏感需求。密钥只从环境配置读取，未知费用不当作免费。

具体配置、首版限制与验收记录见 [检索首版说明](docs/plan/research-mvp.md) 和 [分来源联合验收](docs/plan/live-joint-acceptance.md)，完整目标见 [功能设计](docs/plan/research-before-planning.md)。

## AI 如何参与规划

AI 的职责是协助解释、整理与提出变更，**不是直接替所有人决定行程**。

```text
表达调整意图 → 结构化输出校验 → 生成待确认提案 → 查看差异与影响 → 确认后执行
```

- 模型只能通过白名单工具提出受支持的操作，非法结构会被拒绝。
- 修改已确认安排时，提案标明影响并展示差异，不静默覆盖。
- 硬约束检查由确定性规则承担，模型不能自行放宽约束。
- 局部重排只作用于指定范围，保留范围外的已确认安排。

这些机制降低误改风险，但不代表模型输出、旅行建议或外部数据一定正确。出发前仍需核实营业时间、票务、交通和个人需求。

## 技术与工程结构

**FastAPI + Jinja2** 提供页面和 JSON API，**Pydantic v2** 校验数据，**SQLite** 保存旅程；前端采用服务端渲染与原生 JavaScript，不依赖单页应用构建链。高德与 DeepSeek 通过适配层接入，可替换为模拟实现。

```text
fairmeet/
├── app.py          # 页面路由、JSON API 与应用装配
├── domain.py       # 领域模型与状态定义
├── db.py           # SQLite 持久化
├── services.py     # 旅程、成员、需求与灵感
├── itinerary.py    # 逐日行程与交通段
├── checks.py       # 约束检查与预算汇总
├── history.py      # 变更记录与版本快照
├── replan.py       # 局部重排
├── adapters/       # 地理服务端口、模拟实现与高德接入
├── ai/             # 模型接入、工具、校验与提案流程
├── templates/      # Jinja2 页面
└── static/         # 样式与前端资源

tests/              # API、业务服务、模型协议与适配器测试
docs/
├── assets/         # 品牌图标与 README 横幅
└── plan/           # 架构、决策、验收与阶段记录
```

### API 概览

完整请求参数与响应结构以运行后的 `/docs` 为准。下列路径均相对于 `/api/trips/{trip_id}`，创建旅程使用 `POST /api/trips`。

| 领域 | 相对路径示例 |
| --- | --- |
| 旅程与协作 | `/members`、`/needs`、`/inspirations`、`/places` |
| 行程与交通 | `/days`、`/items`、`/transports` |
| 校验与地图 | `/checks`、`/budget`、`/map-data` |
| AI 与确认 | `/ai/chat`、`/proposals` |
| 记录与调整 | `/history`、`/revisions`、`/replan`、`/summary` |

## 开发与测试

测试使用模拟网络响应，不需要真实第三方 Key，不应消耗外部服务配额。以下为 Windows Git Bash 命令；macOS / Linux 将 Python 路径替换为 `.venv/bin/python`。

```bash
./.venv/Scripts/python.exe -m pytest -q
```

```bash
./.venv/Scripts/python.exe -m ruff check fairmeet tests
```

提交改进时，请附上复现步骤、实际行为与预期行为；涉及提案确认、已确认条目修改、来源标注或局部重排的变更，请补充相应回归测试。截图与日志请先脱敏。

### 文档导航

| 想了解什么 | 从这里开始 |
| --- | --- |
| 产品范围与已确认取舍 | [决策记录](docs/plan/07-confirmed-decisions.md) |
| 与个人 agent 的关系和后续路线 | [定位与路线图](docs/plan/agent-era-positioning.md) |
| 系统如何组织 | [目标架构](docs/plan/02-architecture.md) · [数据模型](docs/plan/03-data-model.md) |
| 用户流程与验收标准 | [端到端流程](docs/plan/04-user-flows.md) · [阶段计划](docs/plan/05-phases-and-acceptance.md) |
| 外部服务接入 | [高德 / DeepSeek](docs/plan/real-integrations.md) |
| 走查证据与限定 | [端到端验收记录](docs/plan/end-to-end-acceptance.md) |
| 风险与待验证事项 | [风险清单](docs/plan/06-risks-and-open-questions.md) |
| 更多工程记录 | [计划包索引](docs/plan/README.md) · [开发日志](docs/devlog.md) · [学习计划](docs/study-plan.md) |

阶段记录反映各自记录时的状态，可能早于后续接入变更；当前服务配置请以 `.env.example` 与实现为准。

## 开发进度与边界

A–E 是原型工程的推进阶段，不代表生产就绪等级。

| 阶段 | 交付范围 | 当前状态 |
| --- | --- | --- |
| A · 定义基础 | 产品范围、架构、数据模型、流程与验收计划 | 已记录并确认 |
| B · 一起规划 | 旅程、成员、邀请、需求与灵感箱 | 已实现 |
| C · 做成行程 | 逐日编辑、交通、地图示意、规则检查与预算 | 已实现 |
| D · AI 协调 | 结构化输出、受控工具、提案确认与来源管理 | 已实现 |
| E · 带着出发 | 摘要、途中查看、变更历史、快照与局部重排 | 已实现 |

**尚未提供：**生产级账户与访问控制、面向外部 agent 的委托授权与 MCP 接口、高德 JS 地图底图、灵感链接自动解析、WebSocket 实时协作、版本一键回滚、行程导出与离线缓存、预订或支付交易。

### 安全与隐私

- 邀请链接和当前成员标识是原型协作机制，**不构成生产级身份认证或访问隔离**。
- 当前服务只建议用于本机演示或受信任环境；公网部署前须完善身份认证、授权、邀请撤销与过期、限流及隐私保护。
- 旅程可能包含日期、预算、偏好与行动需求。不要上传真实数据库、访问链接、密钥或可识别成员的信息。
- 更新 feed 依赖普通请求与刷新，不保证实时送达；版本快照支持保存与查看，暂不支持一键回滚。

## 许可证与品牌资源

项目现有文档标注计划采用 MIT，但仓库尚未提供正式 `LICENSE` 文件，许可证状态待补齐。请勿将此视为已完成的开源授权声明。

原创品牌资源：[图标 SVG](docs/assets/fairmeet-icon.svg) · [首页横幅 SVG](docs/assets/fairmeet-banner.svg)。图标以三条汇合的路线表达“不同起点，共同旅程”，使用海洋蓝、浅青与琥珀黄；SVG 可无损缩放，在 GitHub 浅色和深色主题下均保留独立底色。
