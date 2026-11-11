# 真实外部服务接入说明（高德 / DeepSeek）

> 更新：2026-11-11。本文记录真实外部服务的接入方式、实测结果与降级策略。
> 原则：密钥仅从 `.env`/环境变量读取，**不入库、不回显**；测试一律 mock 网络、断网可跑。

---

## 1. 高德（真实地理编码 / POI / 路线）—— 已实测可用

### 配置
`.env` 中已有 `AMAP_WEB_KEY`（服务端 REST 用）。适配器选择由 `FAIRMEET_GEO` 控制：
- `auto`（默认）：有 `AMAP_WEB_KEY` 就用真实高德，否则回退 Fake；
- `fake`：强制离线 Fake；
- `amap`：强制真实高德（无 key 时回退 Fake）。

### 实测（真实联网，key 未回显）
- **地理编码**：`成都宽窄巷子` → (104.05518, 30.66321)，`provenance=external`。
- **POI 周边搜索**：`春熙路 咖啡` 半径 1500m → 20 条（瑞幸/星巴克等，含分类、距离）。
- **路线（步行）**：宽窄巷子→春熙路 → 2879m / 38.4 分钟。
- **路线（驾车）**：同起终点 → 4347m / 30.1 分钟。
- **完整 app（auto 模式）**：`_build_geo_adapter()` 选中 `AmapGeoAdapter`；交通段计算返回真实数据（39 分钟，`provenance=external`，`source=amap`）。

### 降级
网络异常/超时/HTTP≠200/`status!=1`/无结果 → `GeoUnavailable`，上层降级为「估算/未知」，不崩溃（见 `checks` 的 `transport_unknown` 提示）。

### 测试
- 真实适配器的离线覆盖：`tests/test_amap_adapter.py`（respx mock geocode/POI/route 的正常与各类失败）。
- 真实调用**不进自动化测试**（避免配额消耗）；测试门禁始终离线。

---

## 2. DeepSeek（真实 LLM）—— 代码就绪，待填 Key 实测

### 配置
在 `.env` 增加（示例见 `.env.example`）：
```
DEEPSEEK_API_KEY=sk-...        # 必填，填入后自动启用真实 DeepSeek
DEEPSEEK_BASE_URL=https://api.deepseek.com   # 可选
DEEPSEEK_MODEL=deepseek-chat   # 可选
```
装配逻辑：`create_app()` 未显式注入 llm 时，`build_llm_from_env()` 检测到 `DEEPSEEK_API_KEY` 即用 `DeepSeekLLM`，否则保持 `FakeLLM`（离线）。

### 实现
`fairmeet/ai/deepseek.py` 的 `DeepSeekLLM` 实现 orchestrator 的 `LLMClient` 端口：调用 OpenAI 兼容 `/chat/completions`（`response_format=json_object`），返回 JSON 字符串交给 orchestrator 做 `AIResponse` 校验；非法输出→`mode='invalid'` 不落提案；网络/HTTP 错误抛 `DeepSeekError`。

### 测试
`tests/test_deepseek_client.py`（respx mock）：正常解析、401/结构异常/网络错误抛错、key 不出现在 repr。**不含真实 key、不联网。**

### 待办
- 当前本机 **`DEEPSEEK_API_KEY` 未配置**（端点可达但返回 401）。填入 key 后：
  1. 启动 app，`/trip/{id}/assistant` 发消息应走真实 DeepSeek；
  2. 建议实测：对话生成提案 → 校验 → 确认执行；观察成本/超时，按需加护栏（后续）。

### 实测（2026-11-11，真实 DeepSeek key，经临时环境变量注入、不落盘）
- **最小调用**：返回合法 JSON（`{"reply":"pong"}`）✓
- **端到端（真实 DeepSeek + 真实高德，完整 app 路径）**：
  - 「帮我加午餐」→ `mode=chat`，生成 1 个 pending 提案，`requires_confirmation=True`，影响说明含「+120 元（来源:estimated）」；**确认前 0 条 → 确认后 1 条**（川菜午餐/meal/120/estimated）✓
  - 「把已确认条目费用改成 250」→ 提案 `affects_confirmed=True`，diff `cost:100→250`；**确认前成本仍 100，确认后 250，状态保持 confirmed** ✓（§9.7 核心要求，真实模型跑通）
- **过程中发现并修复**：初版 system prompt 未给出操作 schema，DeepSeek 自造了 `op:add_activity` + 非标字段 → 被校验安全拒绝（`mode=invalid`，无写入，证明「AI 不得绕过校验」生效）。已在 `_SYSTEM_PROMPT` 中补充**白名单操作与字段、真实 id 约束、估算标注规则**，并把旅程 days/items 注入上下文；修复后真实模型稳定产出合法提案。
- **安全提醒**：测试用的 key 曾出现在对话中，建议在 DeepSeek 控制台**轮换/作废**该 key；本仓库未写入任何文件。

---

## 3. 安全与合规
- `.env` 已被 `.gitignore` 忽略、未被 git 跟踪；测试不读取/回显本机 key。
- 真实调用仅在用户本地运行时发生；CI/自动化测试保持离线 mock。
- 未接入任何支付/预订交易能力（任务书 §2.8）。
