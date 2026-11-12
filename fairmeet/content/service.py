"""SQLite 只读研究；进程共用 2 worker、8 槽，同库/每旅程另有原子限额。

部署边界：本地 SQLite；无跨主机租约。不自动重放中断请求。资料 7 天过期，
启动/create/get 惰性清理摘录；保留最小任务墓碑用于幂等、限频与审计。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from fairmeet.content.models import (
    PROVIDER_MESSAGES,
    ContentHit,
    ContentProviderError,
    ResearchError,
    ResearchRequest,
)
from fairmeet.content.providers import (
    AmapContentProvider,
    BraveContentProvider,
    FakeContentProvider,
    PublicWebContentProvider,
)
from fairmeet.content.safety import infer_topic, safe_text, safe_url
from fairmeet.domain import MemberStatus
from fairmeet.services import Forbidden, NotFound

ACTIVE = ("queued", "running")
MAX_ACTIVE_RUNS = 8
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fairmeet-research")
_SLOTS = threading.BoundedSemaphore(MAX_ACTIVE_RUNS)
_PROCESS_TOKEN = uuid.uuid4().hex

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS research_runs (
        id TEXT PRIMARY KEY,
        trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
        member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
        request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
        status TEXT NOT NULL, payload TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        worker_pid INTEGER, worker_token TEXT,
        UNIQUE(trip_id,member_id,request_key)
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_research_trip_active
        ON research_runs(trip_id) WHERE status IN ('queued','running')""",
    "CREATE INDEX IF NOT EXISTS idx_research_rate ON research_runs(trip_id,member_id,created_at)",
    """CREATE TABLE IF NOT EXISTS research_preferences (
        trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
        member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
        enabled INTEGER NOT NULL DEFAULT 0, consent_version TEXT NOT NULL DEFAULT 'v1',
        updated_at TEXT NOT NULL, PRIMARY KEY(trip_id,member_id)
    )""",
    """CREATE TABLE IF NOT EXISTS research_daily_usage (
        source TEXT NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(source,day)
    )""",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # Windows 的 os.kill(pid,0) 不具有 POSIX 探测语义，不能使用。
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # 无此 PID；权限错误则保守认为仍在运行。
        try:
            code = wintypes.DWORD()
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class ResearchService:
    def __init__(self, db, trips, itinerary, provider=None) -> None:
        self.db, self.trips, self.itinerary = db, trips, itinerary
        self._providers = {
            "fake": FakeContentProvider(), "brave": BraveContentProvider(), "public_web": PublicWebContentProvider(),
            "amap_poi": AmapContentProvider(getattr(itinerary, "geo", None)),
        }
        if provider is not None:
            source = getattr(provider, "source", "fake")
            if source not in self._providers:
                raise ValueError("不支持的研究来源")
            self._providers[source] = provider
        self._lock = threading.RLock()
        self._closed = False
        self._tasks: dict[str, Future] = {}
        with self.db.cursor() as cur:
            for statement in _SCHEMA:
                cur.execute(statement)
            for row in cur.execute("SELECT * FROM research_runs WHERE status IN ('queued','running')").fetchall():
                # 不打断同一进程其他实例或另一个存活本地进程的任务。
                if (row["worker_pid"] == os.getpid() and row["worker_token"] != _PROCESS_TOKEN) or not _pid_alive(
                    row["worker_pid"],
                ):
                    run = self._decode(row)
                    run.update(
                        status="interrupted", stage="任务已中断",
                        reply="进程中断；未自动重放检索，请明确重新发起。",
                    )
                    self._write(cur, run)
            self._cleanup(cur)

    def _require(self, trip_id: str, member_id: str):
        member = self.trips.require_member(trip_id, member_id)
        if member.status != MemberStatus.JOINED:
            raise Forbidden("只有已加入旅程的成员可使用研究资料")
        return member

    @staticmethod
    def _decode(row) -> dict:
        run = json.loads(row["payload"])
        for key in ("id", "trip_id", "member_id", "status", "input_fingerprint", "created_at", "expires_at"):
            run[key] = row[key]
        return run

    @staticmethod
    def _write(cur, run: dict) -> None:
        cur.execute("UPDATE research_runs SET status=?,payload=? WHERE id=?", (run["status"], _json(run), run["id"]))

    def _cleanup(self, cur) -> None:
        for row in cur.execute("SELECT * FROM research_runs WHERE expires_at<=?", (_stamp(),)).fetchall():
            run = self._decode(row)
            if run["status"] in ACTIVE:
                run["status"] = "interrupted"
            run.update(
                stage="资料已过期", reply="研究资料已过期并清理，请重新发起。",
                documents=[], candidates=[], errors=[],
            )
            self._write(cur, run)

    def consent_enabled(self, trip_id: str, member_id: str) -> bool:
        with self.db.cursor() as cur:
            self._require(trip_id, member_id)
            return self._consent(cur, trip_id, member_id)

    @staticmethod
    def _consent(cur, trip_id: str, member_id: str) -> bool:
        row = cur.execute(
            "SELECT enabled,consent_version FROM research_preferences WHERE trip_id=? AND member_id=?",
            (trip_id, member_id),
        ).fetchone()
        return bool(row and row["enabled"] and row["consent_version"] == "v1")

    def sources(self, trip_id: str, member_id: str) -> list[dict]:
        enabled = self.consent_enabled(trip_id, member_id)
        descriptions = {
            "fake": ("离线虚构演示", "虚构资料，不联网、不是真实地点；无外部费用。"),
            "amap_poi": ("高德地点搜索（非点评/攻略）", "真实地图POI记录，可用于地点发现；不含用户点评、攻略、"
                         "营业/票价核实。使用已配置高德Key及每日配额，需成员联网同意。"),
            "brave": ("Brave 网页搜索", "仅搜索摘要，不支持正文/评论；须配置密钥、验收开关及每日配额，"
                      "默认关闭。未经真实 API 验收，费用按 Brave 套餐计算。"),
            "public_web": ("DuckDuckGo 公开网页搜索", "真实公开搜索，仅取得搜索页摘要或链接，不读取结果正文/评论。"
                           "免密钥、不调用付费 API；默认每日最多 20 次，可由服务端配置；"
                           "遇验证或限流即停止，不绕过或重试。仍需成员授权及本次联网同意。"),
        }
        return [{
            "id": source, "source": source, "name": descriptions[source][0],
            "available": bool(provider.available()), "search": True, "read": False,
            "is_simulated": bool(provider.is_simulated), "consent_enabled": enabled,
            "requires_network_consent": source != "fake",
            "detail": descriptions[source][1] + (
                " 本实例已停止该来源：" + PROVIDER_MESSAGES[provider.blocked_code] + " 重启前不会再次请求。"
                if getattr(provider, "blocked_code", None) else ""
            ),
            "blocked_code": getattr(provider, "blocked_code", None),
            "daily_limit": getattr(provider, "daily_limit", 0),
        } for source, provider in self._providers.items()]

    def set_consent(self, trip_id: str, member_id: str, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise ResearchError("联网选择必须为布尔值", "invalid_consent")
        with self.db.cursor() as cur:
            self._require(trip_id, member_id)
            cur.execute(
                "INSERT INTO research_preferences(trip_id,member_id,enabled,consent_version,updated_at) "
                "VALUES(?,?,?,'v1',?) "
                "ON CONFLICT(trip_id,member_id) DO UPDATE SET enabled=excluded.enabled,"
                "consent_version=excluded.consent_version,updated_at=excluded.updated_at",
                (trip_id, member_id, int(enabled), _stamp()),
            )
            if not enabled:
                for row in cur.execute(
                    "SELECT * FROM research_runs WHERE trip_id=? AND member_id=? AND status IN ('queued','running')",
                    (trip_id, member_id),
                ).fetchall():
                    self._cancel_row(cur, row)
        return {"trip_id": trip_id, "member_id": member_id, "enabled": enabled, "consent_version": "v1"}

    def fingerprint(self, trip_id: str) -> str:
        # 在同一事务中采集稳定快照；不读取研究、灵感、凭据、姓名、邀请 token。
        with self.db.cursor() as cur:
            trip = cur.execute(
                "SELECT destination,start_date,end_date,budget FROM trips WHERE id=?", (trip_id,),
            ).fetchone()
            if trip is None:
                raise NotFound("旅程不存在")
            snapshot = {"trip": dict(trip)}
            columns = {
                "members": "id,status",
                "needs": "id,member_id,kind,category,value",
                "places": "id,name,category,lng,lat,source",
                "itinerary_days": "id,day_index,date,title",
                "itinerary_items": "*",
                "transport_segments": "*",
            }
            for table, fields in columns.items():
                rows = cur.execute(f"SELECT {fields} FROM {table} WHERE trip_id=? ORDER BY id", (trip_id,)).fetchall()
                snapshot[table] = [dict(row) for row in rows]
            return _hash(snapshot)

    def create(self, trip_id: str, member_id: str, request: ResearchRequest) -> dict:
        if not isinstance(request, ResearchRequest):
            raise ResearchError("请使用 ResearchRequest", "invalid_request")
        body_hash = _hash(request.model_dump())
        with self._lock:
            if self._closed:
                raise ResearchError("研究服务已关闭", "closed")
            with self.db.cursor() as cur:
                self._require(trip_id, member_id)
                self._cleanup(cur)
                row = cur.execute(
                    "SELECT * FROM research_runs WHERE trip_id=? AND member_id=? AND request_key=?",
                    (trip_id, member_id, request.idempotency_key),
                ).fetchone()
                if row:
                    if row["request_hash"] != body_hash:
                        raise ResearchError("此幂等键已用于不同请求，请使用新键", "idempotency_conflict")
                    self._check_expiry(row)
                    return self._decode(row)
                destination = self.trips.get_trip(trip_id).destination.strip()
                if not destination:
                    raise ResearchError("请先填写旅程目的地，再查找资料", "missing_destination")
                if not request.enabled_sources:
                    raise ResearchError("请至少选择一个资料来源", "missing_source")
                if len(request.enabled_sources) != 1:
                    raise ResearchError("首版一次只能选择一个来源，不能混合虚构与真实资料", "mixed_sources")
                if request.consent_version != "v1":
                    raise ResearchError("联网授权版本不受支持，请重新确认", "invalid_consent_version")
                for source in request.enabled_sources:
                    provider = self._providers[source]
                    if source != "fake":
                        if not request.network_consent or not self._consent(cur, trip_id, member_id):
                            raise ResearchError("需本成员开启旅程联网授权，并明确同意本次联网", "consent_required")
                    if not provider.available():
                        code = getattr(provider, "blocked_code", None) or "source_disabled"
                        raise ResearchError(PROVIDER_MESSAGES[code], code)
                cutoff = _stamp(_now() - timedelta(minutes=1))
                count = cur.execute(
                    "SELECT COUNT(*) FROM research_runs WHERE trip_id=? AND member_id=? AND created_at>=?",
                    (trip_id, member_id, cutoff),
                ).fetchone()[0]
                if count >= 5:
                    raise ResearchError("每位成员每趟旅程每分钟最多发起 5 次研究", "rate_limited")
                if cur.execute(
                    "SELECT 1 FROM research_runs WHERE trip_id=? AND status IN ('queued','running')", (trip_id,),
                ).fetchone():
                    raise ResearchError("本旅程已有活动研究，请等待或取消后再发起", "trip_busy")
                if cur.execute(
                    "SELECT COUNT(*) FROM research_runs WHERE status IN ('queued','running')",
                ).fetchone()[0] >= MAX_ACTIVE_RUNS:
                    raise ResearchError("研究队列已满，请稍后重试", "queue_full")
                if not _SLOTS.acquire(blocking=False):
                    raise ResearchError("研究队列已满，请稍后重试", "queue_full")
                try:
                    created = _now()
                    run = {
                        "id": uuid.uuid4().hex, "trip_id": trip_id, "member_id": member_id,
                        "status": "queued", "stage": "准备检索", "topic": infer_topic(request.message),
                        "intent": request.intent, "enabled_sources": list(request.enabled_sources),
                        "is_simulated": all(self._providers[s].is_simulated for s in request.enabled_sources),
                        "reply": "", "candidates": [], "documents": [], "errors": [],
                        "usage": {"searches": 0, "reads": 0, "max_searches": len(request.enabled_sources),
                                  "max_reads": 0, "source_searches": {}},
                        "input_fingerprint": self.fingerprint(trip_id),
                        "created_at": _stamp(created), "expires_at": _stamp(created + timedelta(days=7)),
                    }
                    cur.execute(
                        "INSERT INTO research_runs(id,trip_id,member_id,request_key,request_hash,status,payload,"
                        "input_fingerprint,created_at,expires_at,worker_pid,worker_token) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run["id"], trip_id, member_id, request.idempotency_key, body_hash, run["status"], _json(run),
                         run["input_fingerprint"], run["created_at"], run["expires_at"], os.getpid(), _PROCESS_TOKEN),
                    )
                except Exception:
                    _SLOTS.release()
                    raise
            try:
                # 仅传最小外发信息；不把 message、needs 或完整 request 留在 worker 中。
                future = _EXECUTOR.submit(
                    self._work, run["id"], destination, request.scope, tuple(request.enabled_sources),
                )
                self._tasks[run["id"]] = future
                future.add_done_callback(lambda done, rid=run["id"]: self._done(rid))
            except Exception:
                _SLOTS.release()
                with self.db.cursor() as cur:
                    run.update(status="failed", stage="任务启动失败", reply="任务未启动，请使用新键重试。")
                    self._write(cur, run)
                raise ResearchError("研究任务未能启动", "worker_unavailable") from None
            return run

    @staticmethod
    def _check_expiry(row) -> None:
        if row["expires_at"] <= _stamp():
            raise ResearchError("研究资料已过期，请重新发起", "expired")

    def get(self, trip_id: str, member_id: str, run_id: str) -> dict:
        with self.db.cursor() as cur:
            self._require(trip_id, member_id)
            self._cleanup(cur)
            row = self._owned(cur, trip_id, member_id, run_id)
            expired = row["expires_at"] <= _stamp()
            run = self._decode(row)
        # 清理事务先提交，不因业务异常回滚摘录清理。
        if expired:
            raise ResearchError("研究资料已过期，请重新发起", "expired")
        return run

    @staticmethod
    def _owned(cur, trip_id: str, member_id: str, run_id: str):
        row = cur.execute("SELECT * FROM research_runs WHERE id=? AND trip_id=?", (run_id, trip_id)).fetchone()
        if row is None:
            raise NotFound("研究记录不存在")
        if row["member_id"] != member_id:
            raise Forbidden("只能访问本人发起的研究资料")
        return row

    @staticmethod
    def _cancel_row(cur, row) -> dict:
        run = ResearchService._decode(row)
        if run["status"] in ACTIVE:
            run.update(status="cancelled", stage="已取消", reply="研究已取消；已发出的外部请求与费用不保证撤回。")
            ResearchService._write(cur, run)
        return run

    def cancel(self, trip_id: str, member_id: str, run_id: str) -> dict:
        with self.db.cursor() as cur:
            self._require(trip_id, member_id)
            row = self._owned(cur, trip_id, member_id, run_id)
            self._check_expiry(row)
            return self._cancel_row(cur, row)

    def _done(self, run_id: str) -> None:
        _SLOTS.release()
        with self._lock:
            self._tasks.pop(run_id, None)

    def _guard(self, cur, row, *, network: bool = False) -> bool:
        if row is None or row["status"] not in ACTIVE:
            return False
        try:
            self._require(row["trip_id"], row["member_id"])
            permitted = not network or self._consent(cur, row["trip_id"], row["member_id"])
        except Forbidden:
            permitted = False
        if _now() - datetime.fromisoformat(row["created_at"]) >= timedelta(seconds=120):
            run = self._decode(row)
            run.update(status="partial" if run["candidates"] else "failed", stage="研究超时",
                       reply="研究达到 120 秒协作截止，已停止新请求；保留已取得资料，未生成 AI 行程。")
            run["errors"].append({"source": "research", "code": "timeout", "message": PROVIDER_MESSAGES["timeout"]})
            self._write(cur, run)
            return False
        if self._closed or not permitted or row["expires_at"] <= _stamp():
            self._cancel_row(cur, row)
            return False
        return True

    def _stage(self, run_id: str, stage: str) -> bool:
        with self.db.cursor() as cur:
            row = cur.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
            if not self._guard(cur, row):
                return False
            run = self._decode(row)
            run.update(status="running", stage=stage)
            self._write(cur, run)
            return True

    def _reserve_search(self, run_id: str, source: str) -> bool:
        with self.db.cursor() as cur:
            row = cur.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
            if not self._guard(cur, row, network=source != "fake"):
                return False
            provider = self._providers[source]
            if not provider.available():
                raise ContentProviderError(getattr(provider, "blocked_code", None) or "source_disabled")
            if source != "fake":
                day = _now().date().isoformat()
                limit = max(0, int(getattr(provider, "daily_limit", 0)))
                cur.execute("INSERT OR IGNORE INTO research_daily_usage(source,day,used) VALUES(?,?,0)", (source, day))
                cur.execute(
                    "UPDATE research_daily_usage SET used=used+1 WHERE source=? AND day=? AND used<?",
                    (source, day, limit),
                )
                if cur.rowcount != 1:
                    raise ContentProviderError("daily_limit")
            run = self._decode(row)
            run["usage"]["searches"] += 1
            counts = run["usage"]["source_searches"]
            counts[source] = counts.get(source, 0) + 1
            run["stage"] = "查找资料"
            self._write(cur, run)
            return True

    def _error(self, run_id: str, source: str, code: str) -> None:
        if code not in PROVIDER_MESSAGES:
            code = "provider_failure"
        with self.db.cursor() as cur:
            row = cur.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
            if not self._guard(cur, row):
                return
            run = self._decode(row)
            run["errors"].append({"source": source, "code": code, "message": PROVIDER_MESSAGES[code]})
            self._write(cur, run)

    def _save_hits(self, run_id: str, source: str, hits: list[ContentHit]) -> None:
        if not isinstance(hits, list) or any(not isinstance(hit, ContentHit) for hit in hits[:5]):
            raise ContentProviderError("invalid_evidence")
        simulated = bool(self._providers[source].is_simulated)
        secrets = (os.environ.get("BRAVE_SEARCH_API_KEY", ""),)
        with self.db.cursor() as cur:
            row = cur.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
            if not self._guard(cur, row, network=source != "fake"):
                return
            run = self._decode(row)
            seen = {doc["url"] for doc in run["documents"] if doc["url"]}
            for hit in hits[:5]:
                if len(run["candidates"]) >= 5:
                    break
                title = safe_text(hit.title, 200, secrets)
                url = safe_url(hit.url, secrets)
                if not title or url and url in seen:
                    continue
                if simulated:
                    title = "【虚构资料】" + title
                    # Fake 不能冒用真实平台链接，即使是可注入的测试来源。
                    if url and not url.startswith("https://example.com/"):
                        url = None
                content_scope = "snippet" if simulated else hit.content_scope
                if content_scope not in {"body", "snippet", "link", "structured"}:
                    raise ContentProviderError("invalid_evidence")
                doc_id = uuid.uuid4().hex
                run["documents"].append({
                    "id": doc_id, "title": title, "url": url,
                    "platform": "虚构演示资料" if simulated else safe_text(hit.platform, 100, secrets),
                    "published_at": None, "fetched_at": _stamp(),
                    "content_scope": content_scope, "is_simulated": simulated,
                    "excerpt": "" if content_scope == "link" else safe_text(hit.excerpt, 1000, secrets),
                    "kind": "unknown" if simulated else "external",
                })
                run["candidates"].append({
                    "id": uuid.uuid4().hex, "name": title,
                    "category": "meal" if (
                        simulated and hit.category == "meal" or not simulated and run.get("topic") == "dining"
                    ) else "activity",
                    "area": safe_text(hit.area, 200, secrets) if simulated else "待人工确认",
                    "reason": "离线虚构样例，仅展示资料到候选的流程。" if simulated else
                        ("高德返回的地点记录，不含用户点评；需进一步确认主体、分店与适用条件。"
                         if source == "amap_poi" else
                         "网页搜索提供的资料候选，不等于已识别商家或地点；请人工核对主体与分店。"),
                    "risks": ["虚构资料不能用于真实出行" if simulated else {
                        "link": "仅取得搜索链接，未取得摘要、正文或评论",
                        "snippet": "仅取得搜索摘要，未读取正文或评论",
                        "body": "来源正文未经人工核验，未读取评论",
                        "structured": "仅取得地图地点记录，不含用户点评、营业、票价或预约核实",
                    }[content_scope],
                              "位置、营业、预约、价格及成员硬约束均未核实"],
                    "evidence_ids": [doc_id], "match_status": "simulated" if simulated else "unresolved",
                    "place_id": None, "lat": None, "lng": None, "cost": None,
                    "status": "pending", "source_url": url,
                })
                if url:
                    seen.add(url)
            # evidence_ids 只由此处刚创建的 document ID 构成，不接收 Provider 自报引用。
            self._write(cur, run)

    def _work(self, run_id: str, destination: str, scope: str, sources: tuple[str, ...]) -> None:
        try:
            if not self._stage(run_id, "准备检索"):
                return
            for source in sources:
                try:
                    if not self._reserve_search(run_id, source):
                        return
                    search = self._providers[source].search
                    parameters = inspect.signature(search).parameters
                    # 兼容旧两参离线注入 Provider；内建端口支持本地枚举主题。
                    with self.db.cursor() as cur:
                        row = cur.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
                        if not self._guard(cur, row, network=source != "fake"):
                            return
                        topic = self._decode(row).get("topic", "general")
                    if "topic" in parameters:
                        hits = search(destination, scope, topic=topic)
                    else:
                        hits = search(destination, scope)
                    self._save_hits(run_id, source, hits)
                except ContentProviderError as exc:
                    self._error(run_id, source, exc.code)
                except Exception:
                    self._error(run_id, source, "provider_failure")
            if not self._stage(run_id, "整理资料候选"):
                return
            with self.db.cursor() as cur:
                row = cur.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
                if not self._guard(cur, row):
                    return
                run = self._decode(row)
                errors, candidates = run["errors"], run["candidates"]
                run["status"] = "partial" if errors and candidates else "failed" if errors else "succeeded"
                run["stage"] = "等待人工采纳" if candidates else "检索结束"
                if run["is_simulated"]:
                    run["reply"] = "离线虚构演示资料，不是真实攻略或商家，未生成 AI 行程。" if candidates else (
                        "此目的地没有对应离线样例；仅提供成都虚构示例，不会把成都样例迁移到其他城市。未生成 AI 行程。"
                    )
                elif sources == ("amap_poi",):
                    run["reply"] = (
                        "已取得真实高德地点记录，不含点评或攻略；营业、预约、费用仍未知，尚未生成行程。"
                        if candidates else "没有取得同城地点记录，未生成行程。"
                    )
                else:
                    run["reply"] = (
                        "仅整理网页搜索资料（摘要或链接），未读取正文/评论，未确认商家、位置或可执行安排，"
                        "未生成 AI 行程。"
                        if candidates else "未取得可用资料候选；无结果或来源失败不代表不存在相关地点。未生成 AI 行程。"
                    )
                    if any(doc["is_simulated"] for doc in run["documents"]):
                        run["reply"] += "其中标明虚构的资料仅作离线演示。"
                self._write(cur, run)
        except Exception:
            # 最后防线也只保存固定消息，不记录 exception/repr/响应正文。
            self._error(run_id, "research", "provider_failure")
            with self.db.cursor() as cur:
                row = cur.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
                if row and row["status"] in ACTIVE:
                    run = self._decode(row)
                    run.update(status="failed", stage="研究失败", reply="研究未完成，未修改行程。")
                    self._write(cur, run)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = list(self._tasks.items())
        # 不关闭进程共用 executor，避免影响其他 service。等最多当前来源自身的 timeout。
        with self.db.cursor() as cur:
            for run_id, _ in tasks:
                row = cur.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
                if row:
                    self._cancel_row(cur, row)
        for _, future in tasks:
            future.cancel()
        for _, future in tasks:
            if not future.cancelled():
                future.result()
