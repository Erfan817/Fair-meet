# FairMeet 学习计划与进度

> 边做边学的完整路线。每完成一个任务就更新此文件（这也是 git 历史的一部分）。
>
> **2026-09-21**：已并入外部评审的增量（配置清单、失败语义、模块边界、数据模型、调用链、边界用例、commit 序列、产出指标）。评审原文与仓库状态核对见 `docs/review-2026-09-21.md`。
> **验收标准仍以 `docs/phase0-spec.md` 第 6 节为唯一权威**，本文件只细化「怎么做」。

## 总体路线

```
Phase 0 最小内核(2周) → Phase 1 等时圈公平算法(3周) → Phase 2 对话Agent(3周) → Phase 3 工程质量(2周) → Phase 4 加分项
```

| 阶段 | 内容 | 核心技能收获 | 状态 |
|------|------|-------------|------|
| Phase 0 | 完整推荐管线：地理编码→球面质心→POI→评分→地图 | Python 工程化、pytest、异步基础、FastAPI | 🚧 进行中 |
| Phase 1 | 通勤时间矩阵 + 方差最小化替代地理中点（目标函数：公平成本 = 通勤时间均值 + λ × 方差，λ 可扫描出公平-效率曲线）；策略模式；Redis 缓存；三级降级链 | 算法建模、缓存设计、并发限流 | ⬜ |
| Phase 2 | 会话状态机 + 增量重排 + function calling + 成本护栏（手写循环 vs LangGraph，见「待决策清单」） | LLM 应用、Agent 设计、成本控制 | ⬜ |
| Phase 3 | Docker compose、GitHub Actions CI、压测与 P95 优化 | DevOps、性能工程 | ⬜ |
| Phase 4 | 抽算法为 PyPI 包发布 / 写算法对比技术博客（二选一） | 开源发布、技术写作 | ⬜ |

## Phase 0 细化日程

| 天 | 任务 | 状态 | 提交 |
|----|------|------|------|
| Day 1~2 | 环境搭建 + 调通高德地理编码 | ✅ | `792033c` |
| Day 3~4 | 球面质心纯函数 + 4 个单元测试 | ✅ | `2577d68` |
| Day 5 | `config.py` 扩展 + `geocode.py` 重构 + respx mock 测试（断网可跑） | 👈 进行中（空文件已建） | `feat: add typed application settings` |
| Day 6 | async/await：多地址并发查询 + Semaphore 限流 + 超时/缓存/部分降级 | ⬜ | `feat: implement async geocoding client` ＋ `test: add mocked geocoding tests` |
| Day 7 | POI 周边搜索 + 多因子评分排序（权重可配置）+ 搜索与评分拆分 | ⬜ | `feat: add poi search and configurable scoring` |
| Day 8 | FastAPI 骨架：`POST /api/v1/recommend` + Pydantic 数据模型 | ⬜ | `feat: expose recommendation api` |
| Day 9 | 地图结果页（高德 JS API）+ 极简输入页 | ⬜ | `feat: add minimal map result page` ＋ `test: add end-to-end recommendation tests` |
| Day 10 | 错误处理收尾 + center 边界补测 + README 快速开始 + 验收自查 | ⬜ | `docs: complete local quickstart` |

### Day 5~10 任务细化（2026-09-21 评审补充）

> 并入的是「怎么做」的细节，不改变规格书的验收口径。

**Day 5 · 配置模块的管辖范围**

`config.py` 集中管理，全部来自环境变量，不写死在代码里（`.env.example` 已有模板）：

| 配置项 | 说明 |
|--------|------|
| `AMAP_WEB_KEY` | 服务端 REST：地理编码、周边搜索 |
| `AMAP_JS_KEY`、`AMAP_JS_SECURITY_CODE` | 前端地图展示（Day 8~9 才用） |
| 地理编码接口地址 | 便于替换实现、便于 mock |
| POI 搜索接口地址 | 同上 |
| 请求超时时间 | ⭐ 原计划未显式列出 |
| 最大并发数 | ⭐ 对应 `asyncio.Semaphore` |
| 搜索半径 | 默认 1500m（FR-4） |
| Top N | 默认 5、上限 10（FR-5） |
| 各评分因子权重 | 改配置即可调权重，不动代码（FR-5） |

**Day 6 · 先把失败语义定下来，再写测试**

- 「API 失败」与「地名认不出来」必须分开：后者是**正常业务分支**，进 `dropped_locations` 继续算（FR-2），不是 400
- 期望响应形态：`{"valid_locations": [...], "dropped_locations": [...]}`
- 网络异常 / 超时 / 非 200 统一收敛成同一类异常，上层只认一种
- 同一地址在一次运行内不重复请求（进程内缓存）
- ⚠️ 当前 `geocode.py` 把这两种情况混用了同一个异常，**先定语义再补 mock 测试**，否则测试会锁死错误行为

**Day 7 · 模块边界**

```text
fairmeet/
├── center.py      # 公平中心（已独立，换实现不动其他文件）
├── geocode.py     # 地址 → 经纬度
├── poi.py         # 中心 → 周边候选场所
├── scoring.py     # 候选场所 → 排序 + 分数明细
├── models.py      # 数据模型
└── config.py      # 配置
```

搜索与评分必须拆开：`poi.py` 只负责取回候选，`scoring.py` 只负责打分，互不依赖对方内部结构。

**Day 8 · 调用链与数据模型**

```text
请求参数校验 → 批量地理编码 → 过滤无效地点 → 计算公平中心
→ 搜索周边 POI → 评分排序 → 返回推荐结果
```

每一层定义命名模型，**不在接口函数里堆字典**（这也是 Phase 2 工具 I/O 稳定的前提）：

`RecommendRequest` / `GeocodedLocation` / `Venue` / `ScoreDetail` / `RecommendResponse`

**Day 10 · `center.py` 待补边界用例**

> 风险点：输入点接近**对跖点**时，三维向量之和可能接近零，中心点在数学上不稳定。（规格 FR-3 只写了跨 180° 经线与近极点，未覆盖这一条。）

- [ ] 跨 180° 经线测试
- [ ] 高纬度测试
- [ ] 重合点测试
- [ ] 近对跖点测试
- [ ] 向量模长过小时的错误处理策略（抛错还是退化？理由写进 devlog）

---

## Phase 0 验收标准（来自规格第 6 节）

- [ ] 3 个真实地点请求返回 Top 5 场所 + 可打开的地图页
- [ ] 输入含无效地名时正常返回并列出被丢弃位置
- [ ] pytest 全绿且断网可跑
- [ ] 新环境按 README 5 分钟跑起来
- [ ] 中心计算独立模块，可整体替换
- [ ] devlog 记录完整
- [ ] 仓库无 API key 泄露

## 提交信息规范（Conventional Commits 简化版）

| 前缀 | 用途 | 示例 |
|------|------|------|
| `feat:` | 新功能 | `feat: geocode module with mock tests` |
| `fix:` | 修 bug | `fix: handle empty geocodes list` |
| `refactor:` | 不改功能只改结构 | `refactor: move import to top` |
| `docs:` | 文档 | `docs: update devlog` |
| `test:` | 只加测试 | `test: boundary cases for center` |
| `chore:` | 杂务（配置、gitignore） | `chore: add .vscode to gitignore` |

## 进度更新规则

1. 每完成一天任务：更新本文件的日程表（状态 + 提交号）
2. 每次踩坑/学到东西：devlog 记一条
3. 每次提交前：`py -m pytest` 必须全绿

## 进阶概念学习清单（材料：docs/reference/geocode_advanced.py）

> 约定：每学会一个概念并**用自己的话**在下面写出解释，才可以把对应写法吸收进项目代码。
> 目标节奏：每周吃透 1 个，Phase 1 结束前全部完成。
> ⚠️ 2026-09-21 评审里直接写出的异步 Geocoder 用法（`AsyncClient` / `gather` / `Semaphore` 具体写法）与 `geocode_advanced.py` **同级**：都是参考材料，不是可以照抄的答案。按规格第 0 节规则 2 —— 先自己写，卡住超过 2 小时再看，看完在 devlog 记「我的思路 vs 它的思路」。

- [ ] 1. `def geocode(address: str, *, key: str)` 里 `*` 的作用 —— 我的解释：
- [ ] 2. `TypedDict` 是什么，和普通 dict 的区别 —— 我的解释：
- [ ] 3. `raise GeocodeError(...) from exc` 中 `from exc` 的作用 —— 我的解释：
- [ ] 4. `data.get("geocodes") or []` 处理了哪两种情况 —— 我的解释：
- [ ] 5. `cast()` 的作用；pyright 是什么；`# pyright: ignore` 注释的用途 —— 我的解释：

## 待决策清单

> 这些是「评审建议 vs 原计划」的分歧点，**不是对错问题**，留到对应阶段开头再定。

| 事项 | 选项 | 当前倾向 |
|------|------|----------|
| CI 的位置 | 规格书放在 Phase 3 ／ 评审建议提前到 Phase 0 收尾 | 待 Day 10 决定。提前的收益：「断网可跑」变成自动门禁；代价：Day 10 变重 |
| Phase 2 实现方式 | 手写最小 function calling 循环 ／ 直接上 LangGraph | 倾向先手写（Phase 2 的核心技能是 Agent 设计），Phase 4 再用 LangGraph 重构并写对比 |
| 是否需要持久化 | 原计划全程内存 ／ 评审提到 SQLite、PostgreSQL | 属 Phase 2/3 议题；**Phase 0 不碰数据库**（规格第 5 节） |

## 项目产出与量化指标

> 学习的终点是作品。以下指标在对应阶段完成后**回填实测值**，不要提前编造。

```text
- 支持 2～10 个参与者位置
- 地理编码请求并发数上限为 N
- 单次推荐端到端响应时间低于 5 秒（规格第 4 节性能要求）
- 支持无效地址部分降级（dropped_locations）
- 推荐结果返回可解释分数明细（score_detail）
- 测试覆盖率达到 XX%
- 在 XX 条测试样例上完成公平算法对比（Phase 1 的 λ 扫描曲线）
```

简历描述（Phase 2 完成后启用）：

```text
设计并实现多人公平会面点推荐系统，基于高德地图 API 完成地址解析、球面中心计算、
周边 POI 检索和多因子可解释排序；使用 FastAPI 暴露推荐服务，通过 asyncio + Semaphore
实现批量地理编码并发控制，并使用 respx 对第三方 HTTP 请求进行隔离测试。
```
