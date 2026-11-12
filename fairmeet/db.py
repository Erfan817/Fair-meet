"""FairMeet SQLite 持久化层（阶段 B）。

使用标准库 sqlite3（零额外依赖），提供最小的连接管理与建表。

- 通过环境变量 FAIRMEET_DB_PATH 指定数据库文件；默认 ./fairmeet.db。
- 测试可传 ":memory:" 或临时文件，互不干扰。
- 表结构对齐 docs/plan/03-data-model.md 的阶段 B 子集（Trip/Member/Need/Place/Inspiration）。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    destination TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    budget REAL,
    status TEXT NOT NULL,
    invite_token TEXT NOT NULL UNIQUE,
    created_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL,
    is_owner INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS needs (
    id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    category TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS places (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    category TEXT,
    lng REAL,
    lat REAL,
    source TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inspirations (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    member_id TEXT,
    title TEXT NOT NULL,
    note TEXT,
    source_url TEXT,
    place_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_members_trip ON members(trip_id);
CREATE INDEX IF NOT EXISTS idx_needs_trip ON needs(trip_id);
CREATE INDEX IF NOT EXISTS idx_places_trip ON places(trip_id);
CREATE INDEX IF NOT EXISTS idx_inspirations_trip ON inspirations(trip_id);

-- ===== 阶段 C：逐日行程 / 交通段 =====
CREATE TABLE IF NOT EXISTS itinerary_days (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    day_index INTEGER NOT NULL,
    date TEXT,
    title TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(trip_id, day_index)
);

CREATE TABLE IF NOT EXISTS itinerary_items (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    day_id TEXT NOT NULL REFERENCES itinerary_days(id) ON DELETE CASCADE,
    item_type TEXT NOT NULL,
    title TEXT NOT NULL,
    place_id TEXT,
    start_time TEXT,
    end_time TEXT,
    stay_minutes INTEGER,
    cost REAL,
    cost_provenance_kind TEXT NOT NULL DEFAULT 'unknown',
    cost_provenance_source TEXT,
    cost_provenance_fetched_at TEXT,
    status TEXT NOT NULL,
    note TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transport_segments (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    day_id TEXT NOT NULL REFERENCES itinerary_days(id) ON DELETE CASCADE,
    from_item_id TEXT,
    to_item_id TEXT,
    mode TEXT NOT NULL,
    duration_minutes INTEGER,
    duration_provenance_kind TEXT NOT NULL DEFAULT 'unknown',
    duration_provenance_source TEXT,
    duration_provenance_fetched_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_days_trip ON itinerary_days(trip_id);
CREATE INDEX IF NOT EXISTS idx_items_day ON itinerary_items(day_id);
CREATE INDEX IF NOT EXISTS idx_items_trip ON itinerary_items(trip_id);
CREATE INDEX IF NOT EXISTS idx_segments_day ON transport_segments(day_id);

-- ===== 阶段 D：AI 修改提案 / 数据源可用性台账 =====
CREATE TABLE IF NOT EXISTS change_proposals (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload TEXT NOT NULL,        -- ChangeProposal JSON（含 impacts）
    requires_confirmation INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_availability (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL UNIQUE,
    available INTEGER NOT NULL,
    detail TEXT,
    checked_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proposals_trip ON change_proposals(trip_id);

-- ===== 阶段 E：变更历史 / 修订快照 =====
CREATE TABLE IF NOT EXISTS change_log (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    member_id TEXT,
    change_type TEXT NOT NULL,
    target_ref TEXT,
    summary TEXT NOT NULL,
    diffs TEXT NOT NULL,          -- FieldDiff 列表 JSON
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    member_id TEXT,
    label TEXT NOT NULL,
    change_log_id TEXT,
    snapshot TEXT NOT NULL,       -- 行程快照 JSON
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_changelog_trip ON change_log(trip_id, created_at);
CREATE INDEX IF NOT EXISTS idx_revisions_trip ON revisions(trip_id, created_at);
"""


def default_db_path() -> str:
    return os.getenv("FAIRMEET_DB_PATH", "fairmeet.db")


class Database:
    """轻量 SQLite 封装：负责连接、建表与事务边界。"""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or default_db_path()
        self._lock = threading.RLock()
        self._depth = 0
        # check_same_thread=False：便于 FastAPI 在测试与同步路由中复用连接管理。
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        # A single connection is shared by HTTP handlers and research workers.
        # Serialize access and use savepoints for nested service transactions.
        with self._lock:
            nested = self._depth > 0
            savepoint = f"fm_{self._depth}"
            cur = self._conn.cursor()
            if nested:
                cur.execute(f"SAVEPOINT {savepoint}")
            else:
                cur.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield cur
                if nested:
                    cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self._conn.commit()
            except Exception:
                if nested:
                    cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self._conn.rollback()
                raise
            finally:
                self._depth -= 1
                cur.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
