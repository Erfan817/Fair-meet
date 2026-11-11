"""阶段 D：AI 结构化 I/O 契约。

设计原则（任务书 §5、§6 阶段 D、§7）：
- AI 只能输出**受约束的结构化内容**：要么是文本回复，要么是「修改提案（proposals）」。
- 任何写入都必须以 **ProposedOperation** 表达，经 tools 层校验 + 授权 +（如改已确认项）人工确认后执行。
- AI 不得直接写数据库、不得绕过权限；它没有 DB 句柄，只有 tools。
- 输出经 Pydantic 严格校验；未通过校验的内容一律不落库（orchestrator 负责拒绝）。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

from fairmeet.domain import ItemStatus, ItemType, NeedKind, ProvenanceKind


# ---------------------------------------------------------------------------
# 支持的操作类型（白名单）
# ---------------------------------------------------------------------------
class OperationType(str, Enum):
    ADD_NEED = "add_need"
    ADD_INSPIRATION = "add_inspiration"
    ADD_PLACE = "add_place"
    ADD_DAY = "add_day"
    ADD_ITEM = "add_item"
    UPDATE_ITEM = "update_item"
    SET_ITEM_STATUS = "set_item_status"


class OpAddNeed(BaseModel):
    op: Literal["add_need"] = "add_need"
    kind: NeedKind
    category: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=500)


class OpAddInspiration(BaseModel):
    op: Literal["add_inspiration"] = "add_inspiration"
    title: str = Field(min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, max_length=2000)
    source_url: Optional[str] = Field(default=None, max_length=500)
    place_name: Optional[str] = Field(default=None, max_length=120)


class OpAddPlace(BaseModel):
    op: Literal["add_place"] = "add_place"
    name: str = Field(min_length=1, max_length=120)
    category: Optional[str] = Field(default=None, max_length=40)


class OpAddDay(BaseModel):
    op: Literal["add_day"] = "add_day"
    day_index: int = Field(ge=1)
    date: Optional[str] = None
    title: Optional[str] = Field(default=None, max_length=120)


class OpAddItem(BaseModel):
    op: Literal["add_item"] = "add_item"
    day_id: str
    item_type: ItemType = ItemType.ACTIVITY
    title: str = Field(min_length=1, max_length=200)
    place_id: Optional[str] = None
    start_time: Optional[str] = Field(default=None, max_length=10)
    end_time: Optional[str] = Field(default=None, max_length=10)
    stay_minutes: Optional[int] = Field(default=None, ge=0)
    cost: Optional[float] = Field(default=None, ge=0)
    cost_provenance_kind: ProvenanceKind = ProvenanceKind.ESTIMATED
    note: Optional[str] = Field(default=None, max_length=2000)


class OpUpdateItem(BaseModel):
    op: Literal["update_item"] = "update_item"
    item_id: str
    title: Optional[str] = Field(default=None, max_length=200)
    start_time: Optional[str] = Field(default=None, max_length=10)
    end_time: Optional[str] = Field(default=None, max_length=10)
    stay_minutes: Optional[int] = Field(default=None, ge=0)
    cost: Optional[float] = Field(default=None, ge=0)
    note: Optional[str] = Field(default=None, max_length=2000)


class OpSetItemStatus(BaseModel):
    op: Literal["set_item_status"] = "set_item_status"
    item_id: str
    status: ItemStatus


ProposedOperation = Union[
    OpAddNeed,
    OpAddInspiration,
    OpAddPlace,
    OpAddDay,
    OpAddItem,
    OpUpdateItem,
    OpSetItemStatus,
]


class ProposedOperationEnvelope(BaseModel):
    """带判别字段的包装，便于从 JSON 还原具体操作（Pydantic discriminated union）。"""

    operation: ProposedOperation = Field(discriminator="op")


# ---------------------------------------------------------------------------
# AI 输出契约
# ---------------------------------------------------------------------------
class SourceRef(BaseModel):
    """来源/核实标示：AI 引用的信息来源及其可信状态。"""

    label: str
    kind: ProvenanceKind = ProvenanceKind.UNKNOWN
    detail: Optional[str] = None


class AIResponse(BaseModel):
    """AI 的规范化输出：文本回复 + 可选的可执行提案 + 来源标示。

    - `proposals` 中每个操作都要经 tools 校验；**本模型本身不执行任何写入**。
    - `requires_confirmation` 由 proposal 构建阶段根据是否触碰已确认项决定。
    """

    reply: str = Field(min_length=1)
    clarification_questions: list[str] = Field(default_factory=list)
    operations: list[ProposedOperationEnvelope] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 修改提案与差异（工具体系产出，供确认流程使用）
# ---------------------------------------------------------------------------
class ChangeKind(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    STATUS = "status"


class DiffEntry(BaseModel):
    """一处变更的前后差异。"""

    field: str
    before: Optional[str] = None
    after: Optional[str] = None


class OperationImpact(BaseModel):
    """单个操作的执行计划与影响。"""

    op: ProposedOperationEnvelope
    change_kind: ChangeKind
    target_ref: Optional[str] = None  # 如 item_id
    affects_confirmed: bool = False  # 是否触碰已确认项
    diffs: list[DiffEntry] = Field(default_factory=list)
    impact_notes: list[str] = Field(default_factory=list)  # 对路线/预算/后续影响
    blocked_reason: Optional[str] = None  # 校验/授权失败原因（此时不可执行）


class ChangeProposal(BaseModel):
    """一份待确认的修改提案（可能含多个操作）。

    - status: pending → confirmed / rejected
    - 只要含 affects_confirmed 的操作，就必须 requires_confirmation=True。
    """

    id: str
    trip_id: str
    member_id: str
    summary: str
    impacts: list[OperationImpact]
    requires_confirmation: bool
    status: Literal["pending", "confirmed", "rejected"] = "pending"
    created_at: str
