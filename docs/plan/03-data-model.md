# FairMeet 数据建模文档（03）

> 版本：v0.1（计划阶段草案，待用户确认）
> 定位：本文件描述 **一趟完整旅程（Trip）** 的领域数据模型，是 `deepseek-harness-implementation-brief.md` 第 3、4 节与第 2 节产品边界的落地表达。
> 范围：只做数据建模，不涉及具体实现代码、API 路由或前端。
> 原则：产品主线是完整出行规划；餐厅/约饭/会面点是旅程内可复用子能力；硬约束、偏好、可协商项必须显式区分；来源与不确定性必须在**模型层**强制表达，禁止 LLM 凭空生成精确数字；已确认计划不得被静默修改。

---

## 0. 建模总原则

1. **以 Trip 为聚合根**：所有协作数据（成员、需求、灵感、地点、方案、行程、决策、提案、版本）都归属于某个 Trip，独立授权、独立生命周期。
2. **来源与核实状态是一等公民**：凡是「外部世界的数字或事实」（坐标、路线、时长、价格、营业时间、评分），都不得只存裸值，必须携带 `DataProvenance`（来源 + 核实状态 + 获取时间）。这是应对「LLM 编造精确数字」的结构性防线。
3. **状态机显式化**：计划项、旅程、提案、灵感解析都有明确状态与合法迁移；状态是数据，不是隐含逻辑。
4. **修改受控**：对已确认（confirmed）对象的修改必须先落为 `ChangeProposal`，经确认后才产生新 `Revision` 并更新对象。审计可回溯。
5. **软删除 / 版本化优于物理覆盖**：重要结构（行程项、方案）通过 Revision 与 ChangeLog 保留历史，可理解、可恢复。
6. **领域模型与外部格式解耦**：外部平台（小红书、大众点评、高德/Google Maps）的字段只进入 `DataProvenance`、外部映射表或原始载荷字段，绝不渗入核心行程字段。

---

## 1. 实体清单

下面按「旅程主干 → 需求与灵感 → 地点 → 方案与行程 → 校验与协作 → 版本与审计」顺序列出。每个实体给出：用途、关键字段、关系。

### 1.1 Trip（旅程）

- **用途**：聚合根。代表「一趟旅程」，承载基本信息、状态与发起人。所有协作数据的容器。
- **关键字段**：
  - `trip_id`（PK）
  - `title`（旅程标题，如「五一成都-都江堰 4 日」）
  - `destination_text`（用户/成员描述的目的地原文，可能模糊）
  - `destination_place_id`（FK → Place，可选；一旦目的地被规范化则引用统一地点）
  - `date_start` / `date_end`（日期范围，`DATE`；可空 = 尚未确定，属待澄清项）
  - `date_flexibility`（枚举：`fixed` / `flexible_by_days` / `tbd`）
  - `budget_amount` / `budget_currency` / `budget_scope`（枚举：`per_person_total` / `per_person_per_day` / `group_total`）
  - `status`（枚举，见 §2.2：`draft` / `collecting` / `planning` / `reviewing` / `confirmed` / `in_trip` / `completed` / `archived` / `cancelled`）
  - `originator_member_id`（FK → Member，发起人）
  - `created_at` / `updated_at`
  - `current_revision_id`（FK → Revision，指向当前生效版本，可选）
- **关系**：1 Trip → N Member / MemberRequirement / InspirationItem / PlanOption / ItineraryDay / ChangeProposal / Revision / CollaborationEvent；N:1 Place（目的地）。

### 1.2 Member（成员）

- **用途**：旅程的参与者，承载角色、加入状态与权限。发起人本身也是一名 Member（role=owner）。
- **关键字段**：
  - `member_id`（PK）
  - `trip_id`（FK → Trip）
  - `display_name`
  - `role`（枚举：`owner` 发起人 / `editor` 可编辑计划 / `contributor` 可提交需求与灵感但不可改计划 / `viewer` 只读）
  - `join_status`（枚举：`invited` / `joined` / `declined` / `removed`；对应邀请链接的生命周期）
  - `permission_scope`（JSON，可细粒度开关，如 `can_edit_itinerary` / `can_confirm` / `can_delete_others_comment`；默认由 role 推导，允许个别覆盖）
  - `contact_ref`（非敏感标识，如昵称/脱敏邮箱/手机号 hash；最小化收集）
  - `invite_token_ref`（指向邀请凭据的引用，不存明文 token）
  - `joined_at` / `last_active_at`
  - `consent_flags`（JSON：位置/健康饮食/行动需求等敏感数据的可见范围同意标记）
- **关系**：N:1 Trip；1 Member → 0..1 MemberRequirement（通常一名成员一份需求集合，见下）；是 Comment/Vote/Confirmation/ChangeProposal 的 author。

> 隐私说明：Member 只存最小标识；地理位置、健康/饮食、行动需求等敏感信息放在 MemberRequirement，并受 `consent_flags` 与可见范围控制（brief §8）。

### 1.3 MemberRequirement（成员需求）

- **用途**：记录一名成员对本次旅程的诉求；三类（硬约束/偏好/可协商）必须显式区分。这是冲突校验的输入源。
- **关键字段**：
  - `requirement_id`（PK）
  - `trip_id`（FK → Trip）
  - `member_id`（FK → Member）
  - `requirement_kind`（枚举：`hard` 硬约束 / `preference` 偏好 / `negotiable` 可协商）
  - `category`（枚举，见下，决定语义解析）
    - `origin` 出发地
    - `available_dates` 可参加日期
    - `budget` 预算
    - `interest` 兴趣（主题/活动类型）
    - `dietary` 饮食（忌口/素食/清真等）
    - `mobility` 行动/无障碍需求
    - `pace` 节奏偏好（强度）
    - `other` 其他自由文本
  - `value_text`（人类可读原文，如「清真」「不能走超过 1 km」「最多人均 3000」）
  - `value_structured`（JSON，按 category 的结构化表达；见 §3.1）
  - `is_mandatory`（冗余布尔，由 `requirement_kind=hard` 推导，便于查询；避免多处歧义时以 `requirement_kind` 为准）
  - `response_status`（枚举：`not_answered` / `answered` / `outdated` / `declined_to_share`；用于显示「未回应项」）
  - `source`（枚举：`member_self` / `ai_extracted` / `owner_imported`；AI 抽取的需 member 或 owner 复核）
  - `created_at` / `updated_at`
- **关系**：N:1 Trip、N:1 Member；被 ConstraintCheck / Conflict 引用（哪个需求触发了哪条冲突）。

> 一名成员可有多条 MemberRequirement（不同 category 各一条），以 `(member_id, category, requirement_kind)` 组合区分为典型粒度。

### 1.4 InspirationItem（灵感箱条目）

- **用途**：承载成员丢进灵感箱的原始素材（链接/文字/地点），以及解析、抽取、人工修正的结果。是方案生成与地点库的原料。
- **关键字段**：
  - `inspiration_id`（PK）
  - `trip_id`（FK → Trip）
  - `added_by_member_id`（FK → Member）
  - `raw_input_type`（枚举：`url` / `text` / `place_query` / `manual`）
  - `raw_content`（原始链接或文本原样保存，不丢失来源）
  - `source_url`（规范化后的外部链接；为空表示纯文本/手动）
  - `source_platform`（枚举/自由值：`xiaohongshu` / `dianping` / `weibo` / `web` / `manual` / `unknown`）
  - `parse_status`（枚举：`identified` 已识别 / `pending_confirm` 待确认 / `verified` 已核实 / `parse_failed` 解析失败 / `excluded` 已排除）
  - `extracted_candidates`（JSON 数组：抽取出的候选地点/活动/时间建议/来源，见 §3.3）
  - `confirmed_place_ids`（JSON / 关联表：人工确认后指向 Place 的引用集合）
  - `manual_correction`（JSON：人工修正记录，含修正人、时间、原值/新值）
  - `dedup_group_key`（去重分组键：规范化后的 URL 或核心地点名哈希）
  - `provenance`（内嵌 `DataProvenance`：原始来源 + 抓取/获取时间 + 核实状态）
  - `created_at` / `updated_at`
- **关系**：N:1 Trip、N:1 Member；0..N → Place（通过抽取/确认匹配）；同一 `dedup_group_key` 视为重复项。

> brief §2.7：不假设任何社交链接都能稳定解析。`parse_failed` 是合法终态之一，且必须允许「手动补充或修正」，绝不可阻塞规划。手动修正生成 `manual_correction` 并把 `parse_status` 推进为 `pending_confirm` 或 `verified`。

### 1.5 Place（统一地点）与 PlaceBranch（分店）

- **用途**：地点库。`Place` 表示「统一地点」（去重合并后的规范实体）；`PlaceBranch` 表示同一地点的具体分店（连锁店/多门店场景，如「星巴克」的某一家店）。餐厅、会面点、景点、住宿都纳入同一地点库复用。
- **Place 关键字段**：
  - `place_id`（PK）
  - `canonical_name`（规范名）
  - `category`（枚举：`restaurant` / `cafe` / `attraction` / `lodging` / `transit_hub` / `shopping` / `meeting_point` / `other`）
  - `region_hint`（城市/区域提示，用于跨分店消歧）
  - `external_refs`（JSON 数组：外部来源映射，见 §1.5.1）
  - `merged_into_place_id`（FK → Place，可空；去重合并时指向保留的主实体，软合并）
  - `dedup_key`（去重键：规范名 + 城市 + 类别 的哈希，用于合并判定）
  - `default_provenance`（内嵌 DataProvenance）
  - `created_at` / `updated_at`
- **PlaceBranch 关键字段**：
  - `branch_id`（PK）
  - `place_id`（FK → Place）
  - `branch_name`（分店名，如「春熙路店」）
  - `address_text`
  - `lat` / `lng`（`DOUBLE`；CRS 见 §4.4，统一 GCJ-02）
  - `coord_crs`（枚举，默认 `GCJ-02`）
  - `phone` / `hours_text`（原始文本；营业时间结构化可选）
  - `external_refs`（该分店对应的外部 POI 映射）
  - `provenance`（内嵌 DataProvenance）
  - `is_primary`（是否该 Place 的主分店/默认展示分店）
- **关系**：Trip.destination_place_id → Place；Place 1:N PlaceBranch；PlaceBranch 被 ItineraryItem、TransportSegment 端点引用；InspirationItem 抽取候选匹配到 Place/PlaceBranch。

#### 1.5.1 外部来源映射（external_refs 结构）

`external_refs` 是 JSON 数组，每项形如：

```json
{
  "provider": "amap|google|dianping|manual",
  "external_id": "B0FFH...",
  "external_type": "poi|geocode|review",
  "raw_name": "原始名称",
  "raw_address": "原始地址",
  "fetched_at": "2026-11-11T10:00:00Z"
}
```

- 这保证**外部格式不渗入核心字段**：核心只存 `canonical_name/address/lat/lng`，外部 id 与原始名称留在映射里。
- 同一 Place 可映射多个 provider，便于交叉核实与降级。

> **去重合并**：两个 Place 若 `dedup_key` 相同或外部映射指向同一 POI，则合并——把被合并方 `merged_into_place_id` 指向保留方，迁移其 PlaceBranch、引用与灵感关联，**不物理删除**（保证历史引用可解析）。详见 §5.3。

### 1.6 PlanOption（方案）

- **用途**：AI 或成员产出的可比较方案。一个 Trip 可有多套方案用于比较；方案之间在「目的地安排/住宿区域/每日主题/密度/预算/交通方式」上有差异。
- **关键字段**：
  - `option_id`（PK）
  - `trip_id`（FK → Trip）
  - `label`（方案名，如「紧凑城市线」「松弛近郊线」）
  - `created_by`（枚举：`ai` / `member`；AI 生成的需标注）
  - `author_member_id`（FK → Member，可空，AI 方案为空）
  - `destination_arrangement`（JSON：目的地/区域安排）
  - `lodging_area`（住宿区域描述或 PlaceBranch 引用）
  - `daily_theme`（JSON：逐日主题）
  - `density`（枚举：`relaxed` / `balanced` / `packed`，或数值）
  - `budget_estimate`（金额 + 币种 + 内嵌 DataProvenance；估算必须标 estimated）
  - `transport_mode_mix`（JSON：交通方式构成，如「地铁为主 + 一次包车」）
  - `rationale`（依据说明，文本）
  - `unverified_notes`（JSON 数组：未核实信息清单，UI 必显示）
  - `status`（枚举：`draft` / `under_discussion` / `adopted` / `discarded`）
  - `itinerary_day_ids`（关联：此方案对应的行程日集合；见 §1.7）
  - `created_at` / `updated_at`
- **关系**：N:1 Trip；1 PlanOption → N ItineraryDay（方案展开为逐日安排）；引用 Place/PlaceBranch；可被 Vote/ChangeProposal 引用。

> 方案比较必须展示「依据、预算估算、交通影响、主要取舍、未核实信息」（brief §3.5），因此上述字段是必填语义，不是可选装饰。

### 1.7 ItineraryDay 与 ItineraryItem（逐日行程）

- **ItineraryDay**：
  - `day_id`（PK）
  - `trip_id`（FK → Trip）
  - `option_id`（FK → PlanOption，可空；若行程直接编辑不挂方案则空）
  - `day_date`（`DATE`；可空 = 未定日）
  - `day_index`（第几天，从 1 起，用于无绝对日期时的相对顺序）
  - `theme`
  - `notes`
  - `sort_order`
- **ItineraryItem**：
  - `item_id`（PK）
  - `day_id`（FK → ItineraryDay）
  - `trip_id`（FK → Trip，冗余便于校验与授权）
  - `item_type`（枚举：`activity` 活动 / `meal` 餐饮 / `lodging` 住宿 / `meeting_point` 会面点 / `transit` 交通 / `free_time` / `note`）
  - `title`
  - `place_id` / `branch_id`（FK，可空；会面点/餐饮同样作为行程项，走地点库）
  - `start_time` / `end_time`（本地时间，可空 = 时间待定）
  - `time_slot_hint`（枚举：`morning` / `lunch` / `afternoon` / `dinner` / `evening` / `flexible`）
  - `planned_duration_min`（预计停留分钟，可空）
  - `cost_estimate`（金额 + 币种 + 内嵌 DataProvenance；见 §4）
  - `source_ref`（指向 InspirationItem / 手动 / AI 提案的溯源引用）
  - `status`（枚举，见 §2.1：`draft` / `discussing` / `pending_confirm` / `confirmed` / `done` / `cancelled` / `replaced`）
  - `replaces_item_id`（FK → ItineraryItem，可空；替换场景指向被替换项）
  - `alternative_place_ids`（JSON：收藏的备选地点，用于讨论「替换」）
  - `sort_order`
  - `created_at` / `updated_at`
- **关系**：N:1 ItineraryDay（N:1 Trip）；N:1 Place/PlaceBranch；1 item 可关联 1..N TransportSegment（到下一项的交通段）。

### 1.8 TransportSegment（交通段）

- **用途**：表达行程项之间/地点之间的交通。方式、时长、来源都必须显式，绝不臆造精确时间。
- **关键字段**：
  - `segment_id`（PK）
  - `trip_id`（FK → Trip）
  - `from_branch_id` / `to_branch_id`（FK → PlaceBranch；端点也可是住宿/会面点）
  - `from_item_id` / `to_item_id`（FK → ItineraryItem，可空；行程内相邻项交通）
  - `mode`（枚举：`walk` / `transit` / `drive` / `taxi` / `bike` / `mixed` / `unknown`）
  - `distance_m`（可空）
  - `duration_min`（可空）
  - `source`（枚举：**`map_service` 地图服务提供 / `estimated` 规则估算 / `unknown` 未知**）
  - `provenance`（内嵌 DataProvenance：provider=amap/google、fetched_at、核实状态）
  - `burden_note`（成员负担说明，如「步行 1.2km，对行动不便成员有压力」）
  - `degraded_reason`（当 source=unknown 时的降级原因，如「地图服务超时」）
- **关系**：N:1 Trip；端点 N:1 PlaceBranch；可被 ConstraintCheck 引用。

> brief §3.7 / §4.7：路线与数值必须来自地图/规则服务或明确标为估算；数据不可用时明确降级。`source` 与 `provenance` 双保险，`unknown` 是合法且必要的状态。

### 1.9 ConstraintCheck 与 Conflict（校验与冲突）

- **ConstraintCheck**（一次校验运行/单条规则结果）：
  - `check_id`（PK）
  - `trip_id`（FK → Trip）
  - `target_type` / `target_id`（校验对象：Trip / PlanOption / ItineraryDay / ItineraryItem / TransportSegment）
  - `rule_code`（枚举：`date_conflict` / `budget_exceeded` / `too_many_places` / `mobility_mismatch` / `opening_hours` / `tight_transfer` / `detour` / `dietary_unresolved` …）
  - `severity`（枚举：`hard_violation` 硬约束违反 / `warning` 警告 / `info` 提示）
  - `result`（枚举：`pass` / `fail` / `warn` / `unknown_data`）
  - `message`
  - `related_requirement_ids`（JSON：触发该结果的 member requirement 引用）
  - `related_item_ids`（JSON）
  - `data_status`（枚举：`verified` / `external` / `estimated` / `unknown`；校验所用数据是否完整）
  - `created_at`
- **Conflict**（需要成员处理的冲突，从 check 中提炼的「待办」）：
  - `conflict_id`（PK）
  - `trip_id`（FK → Trip）
  - `check_id`（FK → ConstraintCheck，来源）
  - `conflict_type`（如 `hard_constraint_clash` / `budget` / `schedule`）
  - `description`
  - `involved_requirement_ids`（JSON）
  - `involved_member_ids`（JSON：受影响成员）
  - `resolution_status`（枚举：`open` / `acknowledged` / `resolved` / `waived_by_owner`）
  - `resolution_note` / `resolved_by_member_id` / `resolved_at`
- **关系**：N:1 Trip；Conflict 派生自 ConstraintCheck；引用 MemberRequirement（哪个硬约束被触发）与 ItineraryItem/TransportSegment。

> brief §2.5 / §3.4：硬约束冲突必须显式报告并请成员处理，**不得由模型擅自忽略或放宽**。`resolution_status=waived_by_owner` 只允许 owner 显式豁免并留痕。

### 1.10 CollaborationEvent / Comment / Vote / Confirmation（协作决策）

- **CollaborationEvent**（协作时间线的统一事件流）：
  - `event_id`（PK）
  - `trip_id`（FK → Trip）
  - `actor_member_id`（FK → Member）
  - `event_type`（枚举：`comment` / `vote` / `confirmation` / `proposal_created` / `item_status_changed` / `member_joined` / `requirement_updated` / `revision_created` …）
  - `target_type` / `target_id`
  - `payload`（JSON：事件细节）
  - `created_at`
- **Comment**：
  - `comment_id`（PK）/ `trip_id`（FK）/ `author_member_id`（FK）
  - `target_type` / `target_id`（挂在哪个方案/行程项/地点上）
  - `body`
  - `parent_comment_id`（可空，支持回复）
  - `visibility`（枚举：`all_members` / `owner_only`）
  - `created_at` / `edited_at`
- **Vote**：
  - `vote_id`（PK）/ `trip_id`（FK）/ `member_id`（FK）
  - `target_type` / `target_id`（通常是 PlanOption 或备选 Place/ItineraryItem）
  - `choice`（枚举：`approve` / `reject` / `abstain`，或方案排序打分 `rank`）
  - `weight`（默认 1；少数场景 owner 权重，谨慎使用）
  - `created_at`
  - 约束：同 `(member_id, target_type, target_id)` 唯一（一人一票；改票即更新）
- **Confirmation**（对某对象的确认/签署）：
  - `confirmation_id`（PK）/ `trip_id`（FK）/ `member_id`（FK）
  - `target_type` / `target_id`（如某个 ItineraryItem、某个 ChangeProposal、整份计划）
  - `decision`（枚举：`approve` / `reject`）
  - `note`
  - `created_at`
- **关系**：均 N:1 Trip、N:1 Member；均以 `(target_type, target_id)` 多态引用任意对象；同时回写 CollaborationEvent 供时间线。

### 1.11 ChangeProposal（变更提案）

- **用途**：AI 或成员对**已确认**对象提出的修改，必须先成提案、展示差异与影响范围、经确认后才落地。这是「不得静默修改已确认计划」的结构保障（brief §2.4、§3.8）。
- **关键字段**：
  - `proposal_id`（PK）
  - `trip_id`（FK → Trip）
  - `created_by`（枚举：`ai` / `member`；AI 提案必须走确认）
  - `author_member_id`（FK → Member，可空）
  - `target_type` / `target_id`（要修改的对象，如一个 ItineraryItem 或 PlanOption）
  - `target_revision_id`（FK → Revision，提案基于哪个版本；若目标已被改则提案失效/需 rebase）
  - `change_summary`（文本：改了什么）
  - `diff`（JSON：字段级差异，原值/新值）
  - `impact_scope`（JSON：受影响范围——相关 TransportSegment 变化、预算增量、后续时段受影响项、受影响成员/需求）
  - `status`（枚举：`proposed` / `under_review` / `approved` / `rejected` / `applied` / `superseded` / `stale`）
  - `required_confirmations`（JSON / 关联 Confirmation：需要哪些成员/角色确认）
  - `applied_revision_id`（FK → Revision，批准并应用后产生的新版本）
  - `created_at` / `resolved_at`
- **关系**：N:1 Trip；引用目标对象与 Revision；1 proposal → N Confirmation；应用后产生新 Revision 并更新 ChangeLog。

### 1.12 Revision 与 ChangeLog（版本与变更历史）

- **Revision**（某对象或整份计划的快照版本）：
  - `revision_id`（PK）
  - `trip_id`（FK → Trip）
  - `scope_type`（枚举：`trip` 整份 / `plan_option` / `itinerary_day` / `itinerary_item`）
  - `scope_id`（对应对象 id）
  - `revision_no`（同一 scope 下递增）
  - `snapshot`（JSON：该范围当时的内容快照；用于恢复与差异对比）
  - `parent_revision_id`（FK → Revision，可空，版本树）
  - `created_by`（`ai` / `member` / `system`）
  - `proposal_id`（FK → ChangeProposal，可空；此版本由哪个提案产生）
  - `created_at`
- **ChangeLog**（细粒度变更流水，可读审计）：
  - `changelog_id`（PK）/ `trip_id`（FK）
  - `revision_id`（FK → Revision，可空）
  - `actor_type`（`ai` / `member` / `system`）+ `actor_member_id`（可空）
  - `entity_type` / `entity_id`（被改对象）
  - `field_path`（如 `ItineraryItem.start_time`）
  - `old_value` / `new_value`（JSON）
  - `reason`（变更原因 / 关联 proposal 摘要）
  - `created_at`
- **关系**：N:1 Trip；Revision 被 Trip.current_revision_id、ChangeProposal.applied_revision_id 引用；ChangeLog 汇总为时间线并可关联 CollaborationEvent。

> brief §7 / §8：保存重要计划变更历史/版本，支持理解与恢复。Revision 负责「快照恢复」，ChangeLog 负责「可读审计」，二者互补。

### 1.13 DataProvenance（来源与核实状态，内嵌复用结构）

- **用途**：**强制**在任何「外部数字或事实」上携带来源、核实状态与获取时间。建议作为 JSON 内嵌结构（embedded value object），复用于 PlaceBranch 坐标、TransportSegment 时长、费用估算、营业时间、InspirationItem 原始来源、PlanOption 预算等。
- **字段**：
  - `source_type`（枚举：`map_service` / `external_site` / `user_input` / `rule_estimate` / `llm_inference` / `manual`）
  - `provider`（如 `amap` / `google` / `dianping` / `xhs` / `system_rule`）
  - `source_url`（可空）
  - `fetched_at`（获取时间，ISO-8601；估算则为计算时间）
  - `verified_status`（枚举，**四态强制**，见 §4）：
    - `verified` 已核实（有权威来源且当前有效）
    - `external` 外部数据（来自外部服务，未独立核实，可能过期）
    - `estimated` 估算（规则/AI 计算，明确非精确）
    - `unknown` 未知（数据缺失或服务不可用）
  - `confidence`（`0..1`，可选，用于估算/外部数据）
  - `expires_at`（可空；对时效性数据如营业时间、价格设定过期点）
  - `note`（可空，如「地图服务超时，降级为直线估算」）
- **复用点**：`Place`/`PlaceBranch`、`ItineraryItem.cost_estimate`、`TransportSegment`、`PlanOption.budget_estimate`、`InspirationItem`、营业时间字段。

> **禁止裸值原则**：模型层凡承载外部世界数值的字段，若没有 `DataProvenance`，应视为建模缺陷（可在 DB 层用非空约束 + 应用层校验强制）。这样即便 LLM 试图输出「步行 7 分钟」而无来源，也无法落库为 verified。

---

## 2. 状态机

### 2.1 计划项状态（ItineraryItem.status）

状态集合：`draft`（草案）/ `discussing`（待讨论）/ `pending_confirm`（待确认）/ `confirmed`（已确认）/ `done`（已完成）/ `cancelled`（已取消）/ `replaced`（已替换）。

合法迁移：

| 从 \ 到 | draft | discussing | pending_confirm | confirmed | done | cancelled | replaced |
|---|---|---|---|---|---|---|---|
| **draft** | – | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ |
| **discussing** | ✅ | – | ✅ | ✅* | ❌ | ✅ | ✅ |
| **pending_confirm** | ✅ | ✅ | – | ✅ | ❌ | ✅ | ✅ |
| **confirmed** | ❌ | ✅** | ❌ | – | ✅ | ✅† | ✅† |
| **done** | ❌ | ❌ | ❌ | ❌ | – | ❌ | ❌ |
| **cancelled** | ✅（重开） | ❌ | ❌ | ❌ | ❌ | – | ❌ |
| **replaced** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | – |

> 说明：
> - `*`：`discussing → confirmed` 允许，但需经过一次 Confirmation 记录（成员确认）。
> - `**`：`confirmed → discussing` 即「重新打开讨论」，只允许通过 ChangeProposal 触发（见 §3.2）。
> - `†`：`confirmed → cancelled/replaced` **必须**由已批准的 ChangeProposal 驱动，且 `replaced` 时设置 `replaces_item_id` 关系。
> - `done` 是终态（不可回流）；`cancelled` 可由 owner 重开为 `draft`。
> - 任何「涉及 confirmed 项的修改」都不允许直接 set status，必须走提案流程。

### 2.2 旅程状态（Trip.status）

集合：`draft`（草案，刚创建）/ `collecting`（收集成员与需求/灵感）/ `planning`（形成方案与行程）/ `reviewing`（检查与确认中）/ `confirmed`（计划已整体确认）/ `in_trip`（旅途中）/ `completed`（已完成）/ `archived`（归档）/ `cancelled`（取消）。

合法迁移（简化）：

```
draft → collecting → planning → reviewing → confirmed → in_trip → completed → archived
   │        │            │           │            │
   └────────┴────────────┴───────────┴────────────┴────→ cancelled（任意非终态可取消）
                reviewing ⇄ planning（回到调整）
                in_trip → reviewing（途中大改：回到检查）
```

- 允许 `reviewing → planning`（发现冲突需重排）与 `confirmed → reviewing`（出发前临时再改）。
- `in_trip` 时允许对某天做局部重排，但**尽可能保留未受影响的 confirmed 项**（§3.3 为约束）。
- `completed` 之后仅可 `archived`，不可修改计划内容（只读）。

### 2.3 ChangeProposal 状态（补充）

`proposed → under_review → (approved → applied) | rejected`；被新提案覆盖则 `superseded`；若目标 revision 已变化则 `stale`（需重新生成 diff）。

### 2.4 InspirationItem 解析状态（补充）

`identified`（已识别）→ `pending_confirm`（待确认）→ `verified`（已核实）/ `excluded`（已排除）；解析失败进入 `parse_failed`（可由人工修正后进入 `pending_confirm`）。终态：`verified` / `excluded`。

---

## 3. 关键约束

### 3.1 硬约束 vs 偏好 vs 可协商的表示

三类通过 `MemberRequirement.requirement_kind` 区分，并在 `value_structured` 中按 `category` 结构化，供规则引擎确定性校验：

- **硬约束（hard）**：不可违反。校验失败 → `ConstraintCheck.severity=hard_violation` + 生成 `Conflict`（`resolution_status=open`），必须显式请成员处理。系统/AI **不得自动放宽或忽略**（brief §2.5）。
  - 例：`category=mobility, kind=hard, value_structured={"max_walk_m":800}`；`category=dietary, kind=hard, {"halal":true}`；`category=available_dates, kind=hard, {"must_include":["2026-05-01","2026-05-04"]}`。
- **偏好（preference）**：尽量满足，未满足仅提示 `warning`/`info`，不阻塞。可被方案取舍解释覆盖。
  - 例：`category=interest, kind=preference, {"tags":["美食","博物馆"]}`。
- **可协商（negotiable）**：可被成员协商调整的软项，AI 可在方案中提出调整并请成员确认。
  - 例：`category=budget, kind=negotiable, {"target":2500,"stretch":3000}`。

表示要点：
1. `requirement_kind` 是唯一权威分类字段；`is_mandatory` 仅为查询冗余。
2. `value_structured` 用 JSON 存机器可判定值（数值、日期、布尔、枚举列表）；`value_text` 保留原文供显示与人工理解。
3. 冲突校验只对 `hard` 使用「阻断」语义；对 `preference`/`negotiable` 使用「建议/提示」语义。
4. 若结构化解析不出（如自由文本），保留 `value_text` 并允许 AI 提出解释、由成员确认后再结构化。

### 3.2 修改已确认项必须生成 ChangeProposal 并经确认

**规则**：任何对 `status=confirmed`（或 `done` 之外已生效）对象的修改，禁止直接写入。必须：

1. 创建 `ChangeProposal`（`created_by=ai|member`，记录 `target_type/target_id/target_revision_id`）。
2. 计算 `diff`（字段级原值/新值）与 `impact_scope`（受影响的 TransportSegment、预算变化、后续时段、受影响 member requirement）。
3. 展示给相关成员，收集 `Confirmation`（按对象是否需要多成员/owner 确认）。
4. `approved` 后 `apply`：产生新 `Revision`（`snapshot` 含新值）、写 `ChangeLog`、更新目标对象、`proposal.status=applied`。
5. 若确认前目标已被他人改动（revision 变化）→ `status=stale`，需 rebase 重新生成 diff，防止覆盖。

**AI 约束**：AI 只能通过创建 ChangeProposal 提出修改，不得直接改库（brief §5）。未确认的修改不得改变 confirmed 状态。

### 3.3 局部调整保留未受影响确认项

- 修改某天/某段时，`impact_scope` 必须精确列出「受影响项」；应用时只更新受影响项，其余 `confirmed` 项保持原状态与内容。
- 行程重排算法应以「最小改变」为目标：未受影响项不改 `revision`，从而让成员清楚看到「只有这几项被动了」。

### 3.4 其他关键完整性约束

- `Place.merged_into_place_id` 不得形成环（合并图必须为森林）。
- `TransportSegment.source` 与 `provenance.verified_status` 必须一致：`source=unknown ⇒ verified_status∈{unknown}`；`source=estimated ⇒ estimated`；`source=map_service ⇒ external|verified`。
- `cost_estimate` 无 `provenance` 时不得落库（NOT NULL/应用校验）。
- `Vote` 对同一 `(member_id,target_type,target_id)` 唯一。
- `Member.role=viewer` 的成员不得产生 ChangeProposal/Confirmation 生效（权限校验在业务层）。

---

## 4. 数据来源与不确定性表示

### 4.1 四态强制（DataProvenance.verified_status）

模型层对所有外部数字/事实**强制**四选一，不允许「裸值」：

| 状态 | 含义 | 典型来源 | UI 表现 |
|---|---|---|---|
| `verified` | 已核实，权威且当前有效 | 官方/已核实地图 POI、人工核实 | 正常展示 |
| `external` | 外部数据，未独立核实，可能过期 | 地图 API、点评平台 | 「外部数据 · 获取于 …」 |
| `estimated` | 估算，明确非精确 | 规则计算、直线距离/速度估算 | 「估算」并显示依据 |
| `unknown` | 未知/不可用 | 服务超时、数据缺失 | 「未知」+ 降级说明 |

### 4.2 如何在模型层强制区分

1. **结构性内嵌**：外部数值字段与 `DataProvenance` 成对出现（如 `TransportSegment.duration_min` + `provenance`；`cost_estimate` + `provenance`）。可设计为「值 + provenance」复合结构。
2. **非空约束**：DB 约定承载外部数值的列旁必有 provenance 列（JSON）且 NOT NULL；应用层写入校验拒绝缺 provenance 的写入。
3. **一致性触发器/域校验**：`estimated` 必须带 `confidence` 且 `provider=system_rule|llm`；`verified` 必须带 `source_url` 或 provider 权威标记；`unknown` 必须带 `note`/降级原因。
4. **推导链**：若一个值由另一些值推算（如费用 = 单价×人数），其 `provider=system_rule`、`verified_status=estimated`，并在 `note` 记录输入 provenance，形成可追溯推导链。
5. **禁止 LLM 造数**：任何由 `provider=llm_inference` 产生的事实性数值，其 `verified_status` 强制为 `estimated`（或 `unknown`），且**不允许**冒充 `verified`/`external`。业务层在解析 AI 输出时对 `source_type=llm_inference` 打标，落库前改写 status，无法通过校验将拒绝写入。

### 4.3 时效与过期

- 营业时间、价格、评分等设 `expires_at`；读取时若 `now > expires_at`，UI 自动降级显示为「可能过期」并要求重新核验。
- `fetched_at` 必填，便于「数据获取于 X 前」展示（brief §10：交通/营业/价格/内容会过期）。

### 4.4 坐标系统（GCJ-02）

- 所有存储坐标统一 **GCJ-02**（中国境内高德/腾讯生态），`PlaceBranch.coord_crs` 默认 `GCJ-02`。
- 引入外部坐标（如 WGS-84/Google）时，适配层负责坐标系转换后落库，并在 `provenance.note` 记录原始 CRS 与转换，避免叠加偏移误差。
- 坐标本身也是外部数据，须带 provenance（provider + fetched_at + verified_status）。

---

## 5. 存储映射建议（SQLite 关系表草案）

> 面向阶段 B/C 的模块化单体 + SQLite。JSON 列存半结构化/可变结构；核心判别字段抽为独立列以便索引与约束。规范化重点：Place 去重合并、来源映射、引用完整性。

### 5.1 表草案（表名 · 主键/外键 · 关键列）

| 表 | PK | FK | 关键列 |
|---|---|---|---|
| `trip` | trip_id | destination_place_id→place；originator_member_id→member；current_revision_id→revision | title, destination_text, date_start, date_end, date_flexibility, budget_amount, budget_currency, budget_scope, status, created_at, updated_at |
| `member` | member_id | trip_id→trip | display_name, role, join_status, permission_scope(JSON), contact_ref, invite_token_ref, consent_flags(JSON), joined_at |
| `member_requirement` | requirement_id | trip_id→trip；member_id→member | requirement_kind, category, value_text, value_structured(JSON), response_status, source, created_at |
| `inspiration_item` | inspiration_id | trip_id→trip；added_by_member_id→member | raw_input_type, raw_content, source_url, source_platform, parse_status, extracted_candidates(JSON), confirmed_place_ids(JSON), manual_correction(JSON), dedup_group_key, provenance(JSON) |
| `place` | place_id | merged_into_place_id→place | canonical_name, category, region_hint, external_refs(JSON), dedup_key, default_provenance(JSON) |
| `place_branch` | branch_id | place_id→place | branch_name, address_text, lat, lng, coord_crs, phone, hours_text, external_refs(JSON), provenance(JSON), is_primary |
| `plan_option` | option_id | trip_id→trip | label, created_by, author_member_id→member, destination_arrangement(JSON), lodging_area, daily_theme(JSON), density, budget_estimate(JSON+provenance), transport_mode_mix(JSON), rationale, unverified_notes(JSON), status |
| `itinerary_day` | day_id | trip_id→trip；option_id→plan_option | day_date, day_index, theme, notes, sort_order |
| `itinerary_item` | item_id | day_id→itinerary_day；trip_id→trip；place_id→place；branch_id→place_branch；replaces_item_id→itinerary_item | item_type, title, start_time, end_time, time_slot_hint, planned_duration_min, cost_estimate(JSON+provenance), source_ref(JSON), status, alternative_place_ids(JSON), sort_order |
| `transport_segment` | segment_id | trip_id→trip；from_branch_id→place_branch；to_branch_id→place_branch；from_item_id→itinerary_item；to_item_id→itinerary_item | mode, distance_m, duration_min, source, provenance(JSON), burden_note, degraded_reason |
| `constraint_check` | check_id | trip_id→trip | target_type, target_id, rule_code, severity, result, message, related_requirement_ids(JSON), related_item_ids(JSON), data_status, created_at |
| `conflict` | conflict_id | trip_id→trip；check_id→constraint_check | conflict_type, description, involved_requirement_ids(JSON), involved_member_ids(JSON), resolution_status, resolution_note, resolved_by_member_id→member |
| `collaboration_event` | event_id | trip_id→trip；actor_member_id→member | event_type, target_type, target_id, payload(JSON), created_at |
| `comment` | comment_id | trip_id→trip；author_member_id→member；parent_comment_id→comment | target_type, target_id, body, visibility, created_at, edited_at |
| `vote` | vote_id | trip_id→trip；member_id→member | target_type, target_id, choice, weight, created_at；UNIQUE(member_id,target_type,target_id) |
| `confirmation` | confirmation_id | trip_id→trip；member_id→member | target_type, target_id, decision, note, created_at |
| `change_proposal` | proposal_id | trip_id→trip；author_member_id→member；target_revision_id→revision；applied_revision_id→revision | created_by, target_type, target_id, change_summary, diff(JSON), impact_scope(JSON), status, required_confirmations(JSON), created_at, resolved_at |
| `revision` | revision_id | trip_id→trip；parent_revision_id→revision；proposal_id→change_proposal | scope_type, scope_id, revision_no, snapshot(JSON), created_by, created_at |
| `change_log` | changelog_id | trip_id→trip；revision_id→revision | actor_type, actor_member_id, entity_type, entity_id, field_path, old_value(JSON), new_value(JSON), reason, created_at |

### 5.2 多态引用与索引

- `(target_type, target_id)` 多态引用广泛用于 comment/vote/confirmation/constraint_check/collaboration_event/change_proposal。SQLite 无原生多态外键，用应用层校验 + 复合索引 `(trip_id, target_type, target_id)`。
- 高频索引：`member(trip_id)`、`member_requirement(trip_id, member_id)`、`itinerary_item(day_id, sort_order)`、`transport_segment(trip_id)`、`vote(member_id, target_type, target_id)` 唯一、`place(dedup_key)`、`inspiration_item(trip_id, parse_status)`。
- 每张表加 `trip_id` 冗余便于授权查询与级联（成员仅能访问授权旅程，brief §8）。

### 5.3 何时该规范化 / Place 去重合并策略

- **应规范化（独立表）**：Place/PlaceBranch、Member、MemberRequirement、ItineraryItem、TransportSegment、PlanOption、ChangeProposal、Revision、ChangeLog——它们有稳定结构、被多对象引用、需要强一致。
- **可 JSON 内嵌（不单独建表）**：DataProvenance、diff、impact_scope、value_structured、extracted_candidates、permission_scope、consent_flags、external_refs、snapshot——结构多变、按对象整体读写、无需独立查询。
- **Place 去重合并流程**：
  1. 入库前计算 `dedup_key = hash(normalize(canonical_name)+region+category)`；同 key 命中候选。
  2. 结合 `external_refs` 中同 provider+external_id 判定为同一 POI → 直接复用不新建。
  3. 人工/规则确认合并时：保留主 Place，被合并方 `merged_into_place_id` 指向主 Place；迁移其 `place_branch`、更新所有引用（itinerary_item.branch_id、transport_segment 端点、inspiration 关联）到主 Place 的分支。
  4. **软合并**：不物理删 Place，保留指针便于解析历史引用；查询时沿 `merged_into_place_id` 解析到主实体。
  5. 合并需写 `change_log`，可回滚（恢复 `merged_into_place_id`）。
- **InspirationItem 去重**：`dedup_group_key`（规范化 URL 或核心名 hash）用于灵感箱内「同素材只显示一次」，与 Place 去重解耦。

### 5.4 一致性保障建议

- 外键 `PRAGMA foreign_keys=ON`；关键 JSON 列在应用层 schema 校验。
- `provenance` 成对非空约束（应用层或触发器）确保「无来源不落数值」。
- 状态迁移在业务层用状态机校验（§2），DB 层可加 CHECK 约束限定枚举取值。
- 所有 confirmed 修改走 change_proposal → revision → change_log 链路，禁止旁路 UPDATE。

---

## 6. ER 关系图

### 6.1 Mermaid erDiagram

```mermaid
erDiagram
    TRIP ||--o{ MEMBER : has
    TRIP ||--o{ MEMBER_REQUIREMENT : has
    TRIP ||--o{ INSPIRATION_ITEM : collects
    TRIP ||--o{ PLAN_OPTION : offers
    TRIP ||--o{ ITINERARY_DAY : contains
    TRIP ||--o{ CHANGE_PROPOSAL : receives
    TRIP ||--o{ REVISION : versions
    TRIP ||--o{ COLLABORATION_EVENT : logs
    TRIP }o--|| PLACE : destination
    TRIP }o--o| REVISION : current_revision

    MEMBER ||--o{ MEMBER_REQUIREMENT : submits
    MEMBER ||--o{ COMMENT : writes
    MEMBER ||--o{ VOTE : casts
    MEMBER ||--o{ CONFIRMATION : gives
    MEMBER ||--o{ CHANGE_PROPOSAL : proposes

    PLACE ||--o{ PLACE_BRANCH : has
    PLACE }o--o| PLACE : merged_into

    INSPIRATION_ITEM }o--o{ PLACE : matches

    PLAN_OPTION ||--o{ ITINERARY_DAY : expands
    ITINERARY_DAY ||--o{ ITINERARY_ITEM : contains
    ITINERARY_ITEM }o--|| PLACE_BRANCH : at
    ITINERARY_ITEM }o--o| ITINERARY_ITEM : replaces
    ITINERARY_ITEM ||--o{ TRANSPORT_SEGMENT : connects

    TRANSPORT_SEGMENT }o--|| PLACE_BRANCH : from
    TRANSPORT_SEGMENT }o--|| PLACE_BRANCH : to

    CONSTRAINT_CHECK }o--o{ MEMBER_REQUIREMENT : evaluates
    CONFLICT ||--|| CONSTRAINT_CHECK : derives_from
    CONSTRAINT_CHECK }o--|| ITINERARY_ITEM : checks

    CHANGE_PROPOSAL ||--o{ CONFIRMATION : requires
    CHANGE_PROPOSAL }o--o| REVISION : applies_to
    REVISION ||--o{ CHANGE_LOG : records
```

### 6.2 关系要点（文字版）

- Trip 是聚合根：几乎一切 `N:1` 指向 Trip。
- Member 拥有 MemberRequirement；Member 也是 Comment/Vote/Confirmation/ChangeProposal 的作者。
- Place 1:N PlaceBranch；Place 自引用 `merged_into` 表达去重合并（森林，不成环）。
- PlanOption 1:N ItineraryDay 1:N ItineraryItem；ItineraryItem 通过 `replaces_item_id` 自引用表达替换，通过 TransportSegment 连接相邻地点。
- ConstraintCheck 评估 MemberRequirement 与 ItineraryItem；Conflict 派生自 ConstraintCheck。
- ChangeProposal → Confirmation（多方确认）→ Revision（快照）→ ChangeLog（字段级审计）。
- DataProvenance 不是独立表，内嵌于多处（图中不单列，避免误导为单表）。

---

## 7. 与产品边界 / 验收标准的对照

| brief 要求 | 本模型落点 |
|---|---|
| §2.4 不得静默修改已确认计划 | ChangeProposal + Confirmation + Revision/ChangeLog；§3.2 流程 |
| §2.5 硬约束冲突显式报告，不擅自忽略 | MemberRequirement.requirement_kind=hard + ConstraintCheck(hard_violation) + Conflict(open) |
| §2.6 保留来源、核实状态与不确定性 | DataProvenance 四态强制；§4 |
| §2.7 链接解析失败可手动修正、不阻塞 | InspirationItem.parse_status=parse_failed + manual_correction |
| §3.7 数值来自地图/规则或标估算，禁止 LLM 造数 | TransportSegment.source + provenance；§4.2 |
| §3.8 局部调整保留未受影响确认项 | ChangeProposal.impact_scope 精确受影响集；§3.3 |
| §4.4 地点库去重合并、坐标、外部映射 | Place/PlaceBranch + external_refs + merged_into；§5.3、§4.4 |
| §4.5 方案比较展示依据/未核实信息 | PlanOption.rationale/unverified_notes/budget_estimate.provenance |
| 餐厅/会面点为旅程内子能力 | 不建独立主线实体，作为 Place.category + ItineraryItem.item_type(meal/meeting_point) |

---

## 8. 待确认事项（需用户/Lead 决策）

1. **Vote 语义**：是否需要「方案排序打分」还是仅 approve/reject？影响 vote.choice 设计。
2. **多确认门限**：ChangeProposal 是否需要多成员阈值确认（如受影响成员全签），还是 owner 单签即可？影响 required_confirmations 实现。
3. **预算口径**：Trip.budget_scope 三选一是否够用，是否需分成员预算 vs 团体预算并存（当前 MemberRequirement.budget 可承载成员级）。
4. **日期不确定性**：是否允许「候选日期集」而非单一 date_start/date_end？当前以 flexibility + 需求 available_dates 近似，可评估是否升级为独立日期候选模型。
5. **Revision 粒度**：整份快照 vs 对象级快照的取舍（存储成本 vs 恢复便利）；当前设计 scope_type 可混合。

---

*（本文为计划阶段数据模型草案，随用户确认与阶段 B/C 实现反馈迭代。）*
