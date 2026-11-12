# Agent 接入说明（F–G 阶段）

> 状态：**F 与 G 阶段已实现（2026-11-12）**，属于原型能力，不是生产级授权体系。
> 设计依据：[个人 Agent 时代的定位与路线图](agent-era-positioning.md) §5.3、§7 F。
> 代码：`fairmeet/agent.py`、`fairmeet/agent_routes.py`、`fairmeet/mcp/`。

## 1. 能力边界

| 能做什么 | 不能做什么 |
| --- | --- |
| 读取成员可见范围内的旅程上下文 | 读取邀请令牌（`invite_token` 不进入上下文） |
| 读取确定性校验结果与预算汇总 | 读取其他成员硬约束的具体内容（只给数量） |
| 以成员身份提交偏好 / 可协商需求 | 声明硬约束（返回 `hard_constraint_requires_member`） |
| 提交带来源的灵感（进入待确认） | 直接改行程、静默修改已确认安排 |
| 提交待确认修改提案（带 `expected_revision`） | 确认 / 拒绝提案（返回 `confirmation_requires_member`） |
| 查看提案及其差异与影响 | 仅凭旧版本提交提案（返回 `revision_conflict`） |
| 仅在成员预先授权的范围内自动生效 | 越界自动确认（一律退回人工） |

所有 agent 写入与拒绝尝试都会写入 `agent_audit`，成员可查询。

## 2. 创建委托

由成员本人调用（当前原型用 `member_id` 标识调用人，与其余成员端接口一致）：

```bash
curl -X POST "http://127.0.0.1:8000/api/trips/<trip_id>/agent-delegations?member_id=<member_id>" \
  -H "Content-Type: application/json" \
  -d '{"agent_name":"my-agent","scopes":["trip:read","checks:read","need:submit","inspiration:submit"],"expires_in_hours":168}'
```

响应中的 `token`（`fm_agent_...`）**只返回一次**；服务端只保存 SHA-256 哈希。丢失后只能撤销并重新创建。

- `expires_in_hours`：1–720，默认 168（7 天）。
- `scopes` 省略时授予全部六项。
- `auto_confirm`（默认 `false`）与 `auto_confirm_max_cost`：开启自动确认时必须给出成本上限，否则返回 `invalid_auto_confirm_policy`。规则见下文「自动确认」。

查看与撤销：

```bash
curl "http://127.0.0.1:8000/api/trips/<trip_id>/agent-delegations?member_id=<member_id>"
```

```bash
curl -X DELETE "http://127.0.0.1:8000/api/trips/<trip_id>/agent-delegations/<delegation_id>?member_id=<member_id>"
```

成员只能撤销自己的委托；旅程发起人可以撤销该旅程内的任意委托。

## 3. 作用域

| 作用域 | 允许的操作 |
| --- | --- |
| `trip:read` | `GET /api/agent/trip` |
| `checks:read` | `GET /api/agent/checks` |
| `need:submit` | `POST /api/agent/needs` |
| `inspiration:submit` | `POST /api/agent/inspirations` |
| `proposal:submit` | `POST /api/agent/proposals` |
| `proposal:read` | `GET /api/agent/proposals[/{id}]` |

缺少作用域返回 `insufficient_scope`，`details.granted` 列出已有作用域。

## 4. Agent 接口

所有请求携带：

```text
Authorization: Bearer fm_agent_...
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/agent/delegation` | 当前委托信息（不含令牌） |
| GET | `/api/agent/trip` | 旅程上下文：旅程、成员、需求、灵感、地点、逐日行程、校验、预算、提案摘要 |
| GET | `/api/agent/checks` | 校验结果与预算汇总 |
| POST | `/api/agent/needs` | 提交需求：`{"kind":"preference","category":"...","value":"..."}` |
| POST | `/api/agent/inspirations` | 提交灵感：`title` 必填，且必须带 `source_url` / `source` / `place_id` / `place_name` 之一 |
| POST | `/api/agent/proposals` | 提交修改提案：`summary`、`expected_revision`、`operations`（白名单操作） |
| GET | `/api/agent/proposals` | 列出提案，可用 `?status=pending` 过滤 |
| GET | `/api/agent/proposals/{id}` | 查看单个提案的差异与影响 |

提交灵感后状态为 `pending`（待确认），不会直接写入行程。

### 提案与并发控制（G 阶段）

`GET /api/agent/trip` 返回的 `revision` 是行程状态指纹。提案必须带上读取上下文时的 `expected_revision`：

```json
{
  "summary": "第二天加入熊猫基地",
  "expected_revision": "22f4da0c11bafd55",
  "operations": [
    {"op": "add_item", "day_id": "<day_id>", "title": "熊猫基地", "cost": 100}
  ]
}
```

- 行程已变化时返回 `409 revision_conflict`，`details` 同时给出 `expected` 与 `current`；重新读取上下文后重提即可。
- 操作沿用既有白名单：`add_need`、`add_inspiration`、`add_place`、`add_day`、`add_item`、`update_item`、`set_item_status`。
- 结构非法返回 `invalid_operation`（带操作序号）；空提案返回 `empty_proposal`。
- agent 提案不能声明硬约束（`add_need` + `hard`）或提交无来源灵感。
- 提案记录 `origin`（`agent:<name>`）与 `delegation_id`，成员在页面上能看出「哪个 agent 代表谁提交」。
- 提案默认停在 `pending`，必须由成员本人在 `/api/trips/{trip_id}/proposals/{id}/confirm` 确认。

### 自动确认（默认关闭）

成员可在创建委托时预先授权一类非常窄的自动生效范围。**任一条件不满足，提案都会退回人工确认**：

1. 委托开启 `auto_confirm` 且设置了 `auto_confirm_max_cost`；
2. 所有操作可执行且**只做追加**（`add_day` / `add_item` / `add_need` / `add_inspiration` / `add_place`），不修改已有条目；
3. 追加成本合计不超过上限，且不突破旅程预算；
4. 旅程当前不存在任何硬约束（因此不可能引入硬约束冲突）；
5. 从提交到执行之间行程没有再变化。

不满足时的 `auto_confirm_skipped_reason` 取值：`auto_confirm_disabled`、`max_cost_not_set`、`blocked_operation`、`operation_not_append_only`、`cost_over_limit`、`budget_over`、`hard_constraint_present`、`revision_changed`、`confirm_failed`。

自动生效会写入变更历史（`proposal_confirm`）与审计（`proposal.auto_confirmed`），成员仍可追溯。

### 上下文中的隐私处理

- 本人的需求完整可见；他人的偏好与可协商项可见。
- 他人的硬约束：只返回 `other_hard_constraint_count`，具体类别与值不出现。
- 校验结果中涉及他人硬约束的冲突，`message` 被改写为「存在冲突，需由成员本人提供」，`ref` 置空。

## 5. 错误码

错误响应统一为：

```json
{"error": {"code": "source_required", "message": "...", "details": {}}}
```

| code | HTTP | 含义 |
| --- | --- | --- |
| `missing_token` | 401 | 缺少 Bearer 令牌 |
| `invalid_token` | 401 | 令牌不存在 |
| `token_revoked` | 401 | 令牌已撤销 |
| `token_expired` | 401 | 令牌过期 |
| `member_inactive` | 403 | 委托成员已不在旅程 |
| `insufficient_scope` | 403 | 缺少作用域 |
| `invalid_scope` | 422 | 创建委托时作用域非法 |
| `hard_constraint_requires_member` | 403 | agent 不能声明硬约束 |
| `source_required` | 422 | 灵感缺少来源 |
| `confirmation_requires_member` | 403 | agent 不能确认提案 |
| `revision_conflict` | 409 | 行程已变化，需重新读取上下文 |
| `invalid_operation` | 422 | 操作结构非法或类型未知 |
| `empty_proposal` | 422 | 提案没有任何操作 |
| `invalid_auto_confirm_policy` | 422 | 开启自动确认但未设置成本上限 |
| `not_found` / `forbidden` / `bad_request` | 404 / 403 / 400 | 通用业务错误 |

## 6. MCP 接入

MCP 适配层是薄包装：权限、校验与审计仍在服务端，适配层不做本地规则。

```json
{
  "mcpServers": {
    "fairmeet": {
      "command": "python",
      "args": ["-m", "fairmeet.mcp"],
      "env": {
        "FAIRMEET_BASE_URL": "http://127.0.0.1:8000",
        "FAIRMEET_AGENT_TOKEN": "fm_agent_..."
      }
    }
  }
}
```

（在仓库虚拟环境中运行时，`command` 指向 `.venv/Scripts/python.exe` 或 `.venv/bin/python`。）

提供的工具：

| 工具 | 说明 |
| --- | --- |
| `fairmeet_get_trip_context` | 读取旅程上下文 |
| `fairmeet_get_checks` | 读取校验与预算 |
| `fairmeet_submit_need` | 提交偏好 / 可协商需求（schema 已排除 `hard`） |
| `fairmeet_submit_inspiration` | 提交带来源的灵感 |
| `fairmeet_submit_proposal` | 提交待确认提案（需 `expected_revision`） |
| `fairmeet_list_proposals` | 列出提案（可按状态过滤） |

协议实现为 JSON-RPC over stdio 的最小子集（`initialize`、`ping`、`tools/list`、`tools/call`、通知），不依赖官方 SDK。

## 7. 审计

成员可查询：

```bash
curl "http://127.0.0.1:8000/api/trips/<trip_id>/agent-audit?member_id=<member_id>"
```

每条记录包含：委托 id、成员、agent 名称、动作、结果（`applied` / `denied`）、目标对象、结构化细节与时间。常见动作：

- `need.submit`、`inspiration.submit`
- `proposal.submit`（含操作数、revision、是否需确认、被阻塞数）
- `proposal.auto_confirmed` / `proposal.auto_confirm_skipped`（含策略与跳过原因）
- `proposal.confirm_denied`（agent 尝试确认时记录）

## 8. 已知限制与下一步

- 成员端仍是 `member_id` 原型身份，不是生产级认证；委托令牌解决的是「agent 代表谁」，不是「人如何登录」。
- 没有图形界面创建委托，当前通过 API 完成；网页入口属于后续工作。
- 没有限流、配额与委托审批流；公开部署前必须补齐。
- 自动确认刻意保守：只接受追加类操作、要求成本上限、且旅程不能存在任何硬约束；「仅限本人条目」目前用「只追加、不改已有条目」近似（行程条目暂无成员归属）。
- H 阶段将加入：群聊卡片入口、提案交换格式、事件订阅与跨旅程偏好。

## 9. 验收

```bash
./.venv/Scripts/python.exe -m pytest tests/test_agent_api.py tests/test_agent_proposals.py tests/test_mcp.py -q
```

覆盖：委托创建/列表/撤销、令牌哈希存储、过期与撤销、作用域、上下文脱敏、硬约束拒绝、来源必填、确认拒绝、审计、提案创建与 `revision_conflict`、自动确认的允许与各越界分支、MCP 工具调用与真实服务接线。
