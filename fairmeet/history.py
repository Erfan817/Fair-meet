"""阶段 E：变更历史 / 修订 / 更新通知 feed。

- `record()` 追加一条不可变 `ChangeLogEntry`（谁/何时/什么/前后差异）。
- `snapshot()` 保存一次行程快照为 `Revision`（版本），支持理解与恢复（只读理解；恢复见后续）。
- `list_entries()` / `updates_since()`：变更历史与「更新通知 feed」。
  **通知用普通请求/刷新实现**（按 since 拉取增量），不引入 WebSocket（任务书 §10）。
"""

from __future__ import annotations

import json
import uuid

from fairmeet.db import Database
from fairmeet.domain import (
    ChangeLogEntry,
    ChangeType,
    FieldDiff,
    Revision,
)


class HistoryService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        trip_id: str,
        change_type: ChangeType,
        summary: str,
        member_id: str | None = None,
        target_ref: str | None = None,
        diffs: list[FieldDiff] | None = None,
    ) -> ChangeLogEntry:
        entry = ChangeLogEntry(
            id=uuid.uuid4().hex,
            trip_id=trip_id,
            member_id=member_id,
            change_type=change_type,
            target_ref=target_ref,
            summary=summary,
            diffs=diffs or [],
        )
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO change_log(id,trip_id,member_id,change_type,target_ref,summary,diffs,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    entry.id,
                    entry.trip_id,
                    entry.member_id,
                    entry.change_type.value,
                    entry.target_ref,
                    entry.summary,
                    json.dumps([d.model_dump() for d in entry.diffs], ensure_ascii=False),
                    entry.created_at,
                ),
            )
        return entry

    def _row_to_entry(self, r) -> ChangeLogEntry:
        return ChangeLogEntry(
            id=r["id"],
            trip_id=r["trip_id"],
            member_id=r["member_id"],
            change_type=ChangeType(r["change_type"]),
            target_ref=r["target_ref"],
            summary=r["summary"],
            diffs=[FieldDiff(**d) for d in json.loads(r["diffs"])],
            created_at=r["created_at"],
        )

    def list_entries(self, trip_id: str, limit: int = 200) -> list[ChangeLogEntry]:
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT * FROM change_log WHERE trip_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (trip_id, limit),
            )
            return [self._row_to_entry(r) for r in cur.fetchall()]

    def updates_since(self, trip_id: str, since: str | None = None) -> list[ChangeLogEntry]:
        """更新通知 feed：拉取 since（ISO8601）之后的新变更。since 为空返回全部（有限条）。"""
        with self.db.cursor() as cur:
            if since:
                cur.execute(
                    "SELECT * FROM change_log WHERE trip_id=? AND created_at>? ORDER BY created_at ASC, rowid ASC",
                    (trip_id, since),
                )
            else:
                cur.execute(
                    "SELECT * FROM change_log WHERE trip_id=? ORDER BY created_at DESC, rowid DESC LIMIT 50",
                    (trip_id,),
                )
            return [self._row_to_entry(r) for r in cur.fetchall()]

    def snapshot(
        self,
        trip_id: str,
        label: str,
        snapshot: dict,
        member_id: str | None = None,
        change_log_id: str | None = None,
    ) -> Revision:
        rev = Revision(
            id=uuid.uuid4().hex,
            trip_id=trip_id,
            member_id=member_id,
            label=label,
            change_log_id=change_log_id,
            snapshot=snapshot,
        )
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO revisions(id,trip_id,member_id,label,change_log_id,snapshot,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    rev.id,
                    rev.trip_id,
                    rev.member_id,
                    rev.label,
                    rev.change_log_id,
                    json.dumps(rev.snapshot, ensure_ascii=False),
                    rev.created_at,
                ),
            )
        return rev

    def list_revisions(self, trip_id: str, limit: int = 100) -> list[Revision]:
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT * FROM revisions WHERE trip_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (trip_id, limit),
            )
            return [
                Revision(
                    id=r["id"],
                    trip_id=r["trip_id"],
                    member_id=r["member_id"],
                    label=r["label"],
                    change_log_id=r["change_log_id"],
                    snapshot=json.loads(r["snapshot"]),
                    created_at=r["created_at"],
                )
                for r in cur.fetchall()
            ]
