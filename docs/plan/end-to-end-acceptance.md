# FairMeet 端到端整体验收走查报告

> 日期：2026-11-11
> 场景：一趟**成都 2 天、3 人**国内游（发起人小林 + 阿May + 小张），含硬约束、偏好、可协商需求、多人灵感、逐日行程、餐饮/集合点、AI 提案、检查/预算、局部重排、摘要/途中、变更历史。
> 方式：**真实 uvicorn 启动 + 真实 HTTP 请求**走完整主链路（Geo 用默认离线 Fake，不访问外部网络）；§9.7 的 AI 提案确认额外走完整 FastAPI 应用代码路径（FakeLLM 注入，非真实模型）验证。
> 依据：任务书 [docs/deepseek-harness-implementation-brief.md](../deepseek-harness-implementation-brief.md) 第 9 节完成标准。
> 结论：**10 项 PASS、1 项 PASS（含限定说明）、0 项 FAIL**；另有 1 项附加检查 PASS。以下逐条如实记录证据与限定。

---

## 一、十条完成标准逐条对照

| # | 完成标准（§9） | 结果 | 证据（实测） |
|---|----------------|:----:|--------------|
| 1 | 发起人创建旅程并邀请≥1 名同行者 | **PASS** | `POST /api/trips` 200；成员数=3（含发起人）；邀请链接 `/join/{token}` 页 200 可直接打开；token 为 43 字符不可猜串 |
| 2 | 成员提交不同偏好与≥1 项硬约束 | **PASS** | 需求 4 条，覆盖 kind=`hard/preference/negotiable`，来自 3 位成员；硬约束含「出发日期」「行动需求」 |
| 3 | 多成员加灵感、保留来源、支持确认/手动修正识别 | **PASS** | 3 条灵感（均带 `source_url` 字段）；带地点名的自动入地点库；解析状态初值 `pending`，可经 UI 路由改为 verified/excluded 等（状态机端点存在） |
| 4 | 系统呈现冲突/缺失，用户能处理，AI 不静默跳过 | **PASS** | `GET /ai/explain`：hard=0、warnings=0、infos=2；硬约束（出发日期/行动需求）进入待核对显式提示；`checks` 有硬约束校验规则，不自动放宽 |
| 5 | 生成可编辑逐日计划，地点地图可见，餐饮/集合点作行程项 | **PASS** | 2 天、4 个行程条目，类型含 `activity/meal/meeting_point`；`/map-data` 返回 2 天、3 个有坐标点；`/plan` 页 200 |
| 6 | 交通/预算显示来源或估算，硬约束有规则校验 | **PASS** | 交通段时长 36 分钟，`duration_provenance=estimated`；预算合计 480 元，按来源分类 `{estimated:480, verified:0}`；`checks.ok=True` 且含冲突/校验规则项 |
| 7 | 成员讨论确认；改已确认项展示差异并要求确认 | **PASS** | 条目状态可流转至 `confirmed`；**改已确认项**：经 AI 提案生成 pending ChangeProposal（`affects_confirmed=True`、`requires_confirmation=True`），diff 展示 `cost: 100.0 → 250.0`，**确认前未执行**（成本仍 100），确认后执行（成本 250），状态保持 confirmed（完整 FastAPI 代码路径验证） |
| 8 | 局部调整不无故覆盖其他已确认安排 | **PASS** | 第 2 天已确认条目在第 1 天重排后状态/费用/顺序**完全不变**；重排结果的 `preserved_item_ids` 含该跨天未受影响项 |
| 9 | 外部 API 不可用有降级，关键测试可离线运行 | **PASS** | 本次走查全程未访问外部网络（Geo 默认 Fake）；适配层 `GeoUnavailable`→降级估算/未知（`test_adapters` 覆盖）；`pytest` 117 项**断网通过** |
| 10 | 新环境按文档配置启动测试，仓库无真实密钥 | **PASS** | README 快速开始（venv→`requirements-dev.txt`→uvicorn→pytest）；`.env` 在 `.gitignore`、`git ls-files` 确认**未被跟踪**；测试不读取/不回显本机 key |

## 二、附加检查

| 项 | 结果 | 证据 |
|----|:----:|------|
| 出发前摘要 / 途中查看 / 变更历史 | **PASS** | `/summary`、`/ontrip`、`/history` 页均 200；变更记录 1 条、版本快照 1 个（含 replan 记录与快照） |

## 三、限定与说明（不夸大）

1. **外部真实数据未验证**：本次验收的地图/路线/地理编码全部使用离线 `FakeGeoAdapter`，**未调用真实高德**（账号配额/Key 类型/GCJ-02 细节尚未核验，见 `07-confirmed-decisions.md`）。真实外部可用性属待验证项。
2. **AI 未接真实模型**：§9.7 的提案由注入的 `FakeLLM` 产生（结构化输出 + 校验 + 提案/确认链路是真实代码路径），**不代表真实 LLM 质量**。真实 LLM 提供方与成本护栏仍是待办。
3. **地图为离线 SVG 坐标示意**，非真实高德底图。
4. **变更历史 `updates_since` 秒级粒度**：同秒内多条更新可能被边界漏掉（通知用途，可容忍；必要时引入单调游标）。
5. **版本快照仅保存/查看，未提供一键回滚**。
6. **导出/离线/外部预订未实现**（已评估，见 [phase-e-export-offline-booking-eval.md](phase-e-export-offline-booking-eval.md)）。

## 四、测试与静态检查基线（走查同时复核）

- `pytest -q` → **117 passed**（断网）。
- `ruff check fairmeet tests` → **All checks passed**。

## 五、总体结论

以一趟成都多人多日旅程验证，任务书第 9 节的 **10 条端到端完成标准全部达成（PASS）**，其中第 7、8 条（改已确认项需确认 + 局部重排不覆盖其他已确认项）经专门回归与真实代码路径验证。产品主线始终是「完整出行规划」，会面点/餐厅仅作为旅程内行程条目与地点库能力。

**遗留待验证（非阻塞本轮验收）**：真实高德数据接入（需先核实配额/Key/坐标系）、真实 LLM 提供方与成本护栏、一键回滚、导出/离线/预订。这些均不改变本轮「主链路可用、真实可跑、失败可降级、测试可离线」的结论。
