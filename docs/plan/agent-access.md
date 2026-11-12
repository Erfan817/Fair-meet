# Agent 接入说明（F 阶段）

> 状态：**F 阶段已实现（2026-11-12）**，属于原型能力，不是生产级授权体系。
> 设计依据：[个人 Agent 时代的定位与路线图](agent-era-positioning.md) §5.3、§7 F。
> 代码：`fairmeet/agent.py`、`fairmeet/agent_routes.py`、`fairmeet/mcp/`。

## 1. 能力边界

| 能做什么 | 不能做什么 |
| --- | --- |
| 读取成员可见范围内的旅程上下文 | 读取邀请令牌（`invite_token` 不进入上下文） |
| 读取确定性校验结果与预算汇总 | 读取其他成员硬约束的具体内容（只给数量） |
| 以成员身份提交偏好 / 可协商需求 | 声明硬约束（返回 `hard_constraint_requires_member`） |
| 提交带来源的灵感（进入待确认） | 直接改行程、静默修改已确认安排 |
| — | 确认 / 拒绝提案（返回 `confirmation_requires_member`） |

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
- `scopes` 省略时授予全部四项。

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

提交灵感后状态为 `pending`（待确认），不会直接写入行程。

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

协议实现为 JSON-RPC over stdio 的最小子集（`initialize`、`ping`、`tools/list`、`tools/call`、通知），不依赖官方 SDK。

## 7. 审计

成员可查询：

```bash
curl "http://127.0.0.1:8000/api/trips/<trip_id>/agent-audit?member_id=<member_id>"
```

每条记录包含：委托 id、成员、agent 名称、动作、结果（`applied` / `denied`）、目标对象、结构化细节与时间。常见动作：

- `need.submit`、`inspiration.submit`
- `proposal.confirm_denied`（agent 尝试确认时记录）

## 8. 已知限制与下一步

- 成员端仍是 `member_id` 原型身份，不是生产级认证；委托令牌解决的是「agent 代表谁」，不是「人如何登录」。
- 没有图形界面创建委托，当前通过 API 完成；网页入口属于后续工作。
- 没有限流、配额与委托审批流；公开部署前必须补齐。
- G 阶段将加入：agent 创建/补充提案、`expected_revision` 并发控制、确认人校验与自动确认的范围约束。

## 9. 验收

```bash
./.venv/Scripts/python.exe -m pytest tests/test_agent_api.py tests/test_mcp.py -q
```

覆盖：委托创建/列表/撤销、令牌哈希存储、过期与撤销、作用域、上下文脱敏、硬约束拒绝、来源必填、确认拒绝、审计、MCP 工具调用与真实服务接线。
