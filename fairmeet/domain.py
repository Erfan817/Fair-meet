"""FairMeet 领域模型（阶段 B）。

产品主线是「完整出行规划」，本文件先落地协作基础所需实体：
Trip（旅程）、Member（成员）、Need（成员需求）、Inspiration（灵感）、Place（地点库）。

设计要点（对应 docs/plan/03-data-model.md 与 07-confirmed-decisions.md）：
- 需求区分 硬约束 / 偏好 / 可协商（NeedKind），硬约束冲突必须显式报告，不得静默放宽。
- 灵感保留来源链接与解析状态；阶段 B 只支持「手动添加 / 粘贴链接 + 描述」，
  解析状态用枚举显式表达，解析失败可手动修正。
- 地点统一进地点库，坐标统一 GCJ-02（高德体系）；阶段 B 不依赖高德，坐标可为空。
- 所有时间戳为 UTC ISO8601 字符串，便于 SQLite 存储与跨端一致。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


def utcnow() -> str:
    """当前 UTC 时间，ISO8601（秒级）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class NeedKind(str, Enum):
    """需求的约束强度。"""

    HARD = "hard"  # 硬约束：冲突时必须显式报告并请成员处理
    PREFERENCE = "preference"  # 偏好：尽量满足
    NEGOTIABLE = "negotiable"  # 可协商：可作为权衡项


class MemberStatus(str, Enum):
    INVITED = "invited"  # 已邀请未确认
    JOINED = "joined"  # 已加入
    DECLINED = "declined"  # 拒绝


class InspirationStatus(str, Enum):
    """灵感解析/核实状态（阶段 B：手动或粘贴链接，通常待确认）。"""

    IDENTIFIED = "identified"  # 已识别
    PENDING = "pending"  # 待确认
    VERIFIED = "verified"  # 已核实
    PARSE_FAILED = "parse_failed"  # 解析失败（可手动修正）
    EXCLUDED = "excluded"  # 已排除


class TripStatus(str, Enum):
    DRAFT = "draft"  # 草案
    PLANNING = "planning"  # 规划中
    CONFIRMED = "confirmed"  # 已确认


# ---------------------------------------------------------------------------
# 领域对象（读取形态）
# ---------------------------------------------------------------------------


class Trip(BaseModel):
    id: str
    title: str
    destination: str = "成都"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    budget: Optional[float] = None
    status: TripStatus = TripStatus.DRAFT
    invite_token: str  # 不可猜的邀请令牌
    created_by: Optional[str] = None  # 发起人 member id
    created_at: str = Field(default_factory=utcnow)


class Member(BaseModel):
    id: str
    trip_id: str
    display_name: str
    status: MemberStatus = MemberStatus.JOINED
    is_owner: bool = False
    created_at: str = Field(default_factory=utcnow)


class Need(BaseModel):
    id: str
    member_id: str
    trip_id: str
    kind: NeedKind
    category: str  # 例如 origin_date/budget/interest/dietary/accessibility
    value: str
    source: Optional[str] = None  # manual / agent:<name> 等，便于区分来源
    created_at: str = Field(default_factory=utcnow)


class Place(BaseModel):
    id: str
    trip_id: str
    name: str
    category: Optional[str] = None
    lng: Optional[float] = None  # GCJ-02
    lat: Optional[float] = None
    source: Optional[str] = None  # 来源（手动/粘贴链接等）
    created_at: str = Field(default_factory=utcnow)


class Inspiration(BaseModel):
    id: str
    trip_id: str
    member_id: Optional[str] = None
    title: str
    note: Optional[str] = None
    source_url: Optional[str] = None
    source: Optional[str] = None  # 来源标识（manual / agent:<name> 等）
    place_id: Optional[str] = None
    status: InspirationStatus = InspirationStatus.PENDING
    created_at: str = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# 输入模型（创建请求）
# ---------------------------------------------------------------------------


class TripCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    destination: str = "成都"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    budget: Optional[float] = Field(default=None, ge=0)
    owner_name: str = Field(default="发起人", min_length=1, max_length=60)


class MemberJoin(BaseModel):
    display_name: str = Field(min_length=1, max_length=60)


class NeedCreate(BaseModel):
    kind: NeedKind
    category: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=500)
    source: Optional[str] = Field(default=None, max_length=200)


class PlaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: Optional[str] = Field(default=None, max_length=40)
    lng: Optional[float] = None
    lat: Optional[float] = None
    source: Optional[str] = Field(default=None, max_length=200)


class InspirationCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, max_length=2000)
    source_url: Optional[str] = Field(default=None, max_length=500)
    place_name: Optional[str] = Field(default=None, max_length=120)
    place_id: Optional[str] = Field(default=None, max_length=80)
    source: Optional[str] = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def exclusive_place(self):
        if self.place_id and self.place_name:
            raise ValueError("place_id 与 place_name 不能同时提供")
        return self


# ===========================================================================
# 阶段 C：可编辑逐日行程 / 交通段 / 地图适配 / 来源与不确定性
# ===========================================================================


class ItemStatus(str, Enum):
    """行程条目状态（对应任务书 §3.8，确认修改前不得静默改写）。"""

    DRAFT = "draft"  # 草案
    DISCUSSING = "discussing"  # 待讨论
    PENDING = "pending"  # 待确认
    CONFIRMED = "confirmed"  # 已确认
    DONE = "done"  # 已完成
    CANCELLED = "cancelled"  # 已取消/替换


class ItemType(str, Enum):
    """行程条目类型：餐饮/会面点也是行程条目，不另起独立流程。"""

    ACTIVITY = "activity"
    MEAL = "meal"
    LODGING = "lodging"
    MEETING_POINT = "meeting_point"
    TRANSIT = "transit"
    FREE_TIME = "free_time"
    NOTE = "note"


class ProvenanceKind(str, Enum):
    """数据来源与不确定性（任务书 §3.7/§5：不得把推断伪装成已核实事实）。"""

    VERIFIED = "verified"  # 已核实（人工/权威来源确认）
    EXTERNAL = "external"  # 外部数据（如地图/POI 服务返回）
    ESTIMATED = "estimated"  # 估算（规则/模型推算，非精确）
    UNKNOWN = "unknown"  # 未知


class Provenance(BaseModel):
    """内嵌值对象：说明一个数值/结论的来源与获取时间。"""

    kind: ProvenanceKind = ProvenanceKind.UNKNOWN
    source: Optional[str] = None  # 来源标识，如 amap/rule/manual
    fetched_at: Optional[str] = None  # 获取时间（ISO8601）


class TransportMode(str, Enum):
    WALK = "walk"
    TRANSIT = "transit"
    DRIVE = "drive"
    TAXI = "taxi"
    OTHER = "other"


class ItineraryDay(BaseModel):
    id: str
    trip_id: str
    day_index: int  # 1-based 第几天
    date: Optional[str] = None  # YYYY-MM-DD，可空（日期未定）
    title: Optional[str] = None
    created_at: str = Field(default_factory=utcnow)


class ItineraryItem(BaseModel):
    id: str
    trip_id: str
    day_id: str
    item_type: ItemType = ItemType.ACTIVITY
    title: str
    place_id: Optional[str] = None
    start_time: Optional[str] = None  # HH:MM
    end_time: Optional[str] = None
    stay_minutes: Optional[int] = None
    cost: Optional[float] = None  # 费用估算（元）
    cost_provenance: Provenance = Field(default_factory=Provenance)
    status: ItemStatus = ItemStatus.DRAFT
    note: Optional[str] = None
    sort_order: int = 0
    created_at: str = Field(default_factory=utcnow)


class TransportSegment(BaseModel):
    id: str
    trip_id: str
    day_id: str
    from_item_id: Optional[str] = None
    to_item_id: Optional[str] = None
    mode: TransportMode = TransportMode.TRANSIT
    duration_minutes: Optional[int] = None  # 时长（分钟）
    duration_provenance: Provenance = Field(default_factory=Provenance)
    created_at: str = Field(default_factory=utcnow)


# ---- 阶段 C 输入模型 ----


class DayCreate(BaseModel):
    day_index: int = Field(ge=1)
    date: Optional[str] = None
    title: Optional[str] = Field(default=None, max_length=120)


class ItemCreate(BaseModel):
    item_type: ItemType = ItemType.ACTIVITY
    title: str = Field(min_length=1, max_length=200)
    place_id: Optional[str] = None
    start_time: Optional[str] = Field(default=None, max_length=10)
    end_time: Optional[str] = Field(default=None, max_length=10)
    stay_minutes: Optional[int] = Field(default=None, ge=0)
    cost: Optional[float] = Field(default=None, ge=0)
    cost_provenance_kind: ProvenanceKind = ProvenanceKind.UNKNOWN
    note: Optional[str] = Field(default=None, max_length=2000)


class ItemUpdate(BaseModel):
    """条目局部更新（部分字段）。状态流转走独立端点。"""

    title: Optional[str] = Field(default=None, max_length=200)
    start_time: Optional[str] = Field(default=None, max_length=10)
    end_time: Optional[str] = Field(default=None, max_length=10)
    stay_minutes: Optional[int] = Field(default=None, ge=0)
    cost: Optional[float] = Field(default=None, ge=0)
    cost_provenance_kind: Optional[ProvenanceKind] = None
    note: Optional[str] = Field(default=None, max_length=2000)
    sort_order: Optional[int] = None


class TransportCreate(BaseModel):
    from_item_id: Optional[str] = None
    to_item_id: Optional[str] = None
    mode: TransportMode = TransportMode.TRANSIT
    duration_minutes: Optional[int] = Field(default=None, ge=0)
    duration_provenance_kind: ProvenanceKind = ProvenanceKind.UNKNOWN


# ---- 阶段 C 检查/预算结果 ----


class ConflictItem(BaseModel):
    """一处冲突或风险提示。severity=hard 表示硬约束冲突，必须显式请成员处理。"""

    severity: str  # "hard" | "warning" | "info"
    code: str
    message: str
    ref: Optional[str] = None  # 关联的 need/item id


class CheckResult(BaseModel):
    ok: bool
    conflicts: list[ConflictItem] = Field(default_factory=list)


class BudgetSummary(BaseModel):
    trip_budget: Optional[float] = None
    estimated_total: float = 0.0
    by_provenance: dict[str, float] = Field(default_factory=dict)  # 按来源分类合计
    over_budget: bool = False
    note: str = ""


# ===========================================================================
# 阶段 E：变更历史 / 修订 / 通知
# ===========================================================================


class ChangeType(str, Enum):
    ITEM_CREATE = "item_create"
    ITEM_UPDATE = "item_update"
    ITEM_STATUS = "item_status"
    DAY_CREATE = "day_create"
    TRANSPORT_ADD = "transport_add"
    PROPOSAL_CONFIRM = "proposal_confirm"
    TRIP_STATUS = "trip_status"
    REPLAN = "replan"


class FieldDiff(BaseModel):
    field: str
    before: Optional[str] = None
    after: Optional[str] = None


class ChangeLogEntry(BaseModel):
    """一条不可变变更记录：谁、在哪个旅程、何时、做了什么、前后差异。

    用于变更历史/审计；同时也是「更新通知 feed」的数据源（按 since 拉取）。
    """

    id: str
    trip_id: str
    member_id: Optional[str] = None
    change_type: ChangeType
    target_ref: Optional[str] = None  # 关联条目/天/提案 id
    summary: str
    diffs: list[FieldDiff] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow)


class Revision(BaseModel):
    """某次变更后的行程快照（版本），支持理解与恢复。"""

    id: str
    trip_id: str
    member_id: Optional[str] = None
    label: str
    change_log_id: Optional[str] = None
    snapshot: dict  # 快速序列化的行程（days/items/transports）
    created_at: str = Field(default_factory=utcnow)


class ReplanRequest(BaseModel):
    """局部重排请求：仅针对某一天/某几条条目，不触碰其他安排。"""

    day_id: str
    item_ids: list[str] = Field(default_factory=list)  # 空表示当天所有条目
    reason: str = Field(default="局部调整", max_length=200)
    # 可选的新顺序：item_id 列表；未列出的保持相对顺序
    new_order: list[str] = Field(default_factory=list)


class ReplanResult(BaseModel):
    """局部重排结果：只包含被改动项，明确受影响范围与保留项。"""

    day_id: str
    changed: list[FieldDiff] = Field(default_factory=list)
    affected_item_ids: list[str] = Field(default_factory=list)
    preserved_item_ids: list[str] = Field(default_factory=list)  # 未受影响的（含已确认）
    budget_delta: float = 0.0
    impact_notes: list[str] = Field(default_factory=list)
    change_log_id: Optional[str] = None
