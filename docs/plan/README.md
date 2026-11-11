# FairMeet 阶段 A 计划包（Reviewable Plan）

> 依据：[任务书](../deepseek-harness-implementation-brief.md)
> 性质：**阶段 A 交付物 —— 只读盘点 + 可审阅的计划**。按任务书第 11 节，大规模编码前须先提交本计划包供确认。
> 原则：主线是「完整出行规划」；餐厅/约饭/会面点/路线计算只是旅程内可复用子能力，**不重定义产品主线**；保护所有既有未提交改动。

## 阅读顺序

| # | 文档 | 内容 |
|---|------|------|
| 01 | [盘点与差异报告](01-inventory-and-gap.md) | 当前代码/文档/未提交改动/依赖/密钥现状，与任务书目标的逐条差异 |
| 02 | [目标架构与技术选型](02-architecture.md) | 分层架构、模块划分、技术选型理由、ADR、横切关注点、非目标 |
| 03 | [旅程数据模型草案](03-data-model.md) | 实体、关系、状态机、来源与不确定性表示、SQLite 映射、ER 图 |
| 04 | [端到端用户流程](04-user-flows.md) | 3.1–3.8 主链路、降级路径、旧会面点能力转化、验收剧本、权限隐私 |
| 05 | [分阶段计划与验收](05-phases-and-acceptance.md) | 阶段 A–E、依赖图、每阶段 DoD、依赖清单、交接模板 |
| 06 | [风险与待验证事项](06-risks-and-open-questions.md) | 风险登记表、open questions、AI 安全合规、隐私最小化 |
| 07 | [已确认决策记录](07-confirmed-decisions.md) | 用户确认的场景/城市/范围/前端/依赖/协作/配额/AI/部署/节奏等口径 |

## 一句话结论

方向正确但需重整：现有仓库只是一份「会面点推荐」的阶段 0 内核（球面质心 + 同步地理编码骨架，4 个测试全绿），**尚未建立完整出行规划的应用入口、持久化、前端、POI/评分闭环与 AI 协调层**。本计划包给出把主线切换到「完整出行规划」的目标架构、旅程数据模型、端到端流程，以及 A–E 分阶段实施与验收路径，等待确认后逐阶段实现。

## 确认本计划时需要回答的问题

见 [06 · 待验证事项](06-risks-and-open-questions.md#待验证事项open-questions)。主要包括：首轮场景与目标城市、是否只做国内、前端技术偏好、是否接受新增依赖（fastapi/pydantic/uvicorn/jinja2）、地图配额情况等。

## 下一步

1. ~~审阅本计划包六份文档。~~ 已审阅并确认（见 [07 · 已确认决策记录](07-confirmed-decisions.md)）。
2. ~~回答关键问题。~~ 已确认：**成都 3–5 人 / 2–3 天国内游**为首轮场景；首轮仅境内；Jinja2 + 原生 JS 无 SPA；接受 `fastapi/pydantic/uvicorn/jinja2`，优先 Python 3.14；SQLite 起步；阶段 B 不依赖高德、不接 LLM、不预上 WebSocket。
3. ~~进入阶段 B~~ 已完成，见 [阶段 B 交接记录](phase-b-handover.md)。
4. ~~进入阶段 C~~ 已完成，见 [阶段 C 交接记录](phase-c-handover.md)。
5. ~~进入阶段 D~~ 已完成，见 [阶段 D 交接记录](phase-d-handover.md)。
6. ~~进入阶段 E~~ 已完成，见 [阶段 E 交接记录](phase-e-handover.md)。A–E 主链路已闭环。
7. ~~端到端整体验收走查~~ 已完成，见 [端到端验收报告](end-to-end-acceptance.md)（§9 十条：10 PASS / 0 FAIL，含限定说明）。

## 状态

- 阶段 A：✅ 计划已提交并经用户确认（2026-11-11）
- 阶段 B：✅ 旅程协作基础完成（见 [交接](phase-b-handover.md)）
- 阶段 C：✅ 可编辑计划与地图完成（见 [交接](phase-c-handover.md)）
- 阶段 D：✅ AI 协调与来源增强完成（见 [交接](phase-d-handover.md)）
- 阶段 E：✅ 出发前与途中体验完成（见 [交接](phase-e-handover.md)）
- 端到端验收：✅ §9 十条全部达成（真实 uvicorn+HTTP 走查），见 [验收报告](end-to-end-acceptance.md)
- 收尾：[导出/离线/预订评估](phase-e-export-offline-booking-eval.md)

> 说明：均为离线可复现验收（Geo 用 Fake、AI 用 FakeLLM）；真实高德/真实 LLM 接入为遗留待验证项。既有未提交改动原样保护。
