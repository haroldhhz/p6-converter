"""
Activity & Audit logging for LAP Platform.

Stores activity events (every IMS Generator and Risk Manager run) in a local
SQLite database. The DB file lives next to the app so it persists across
uploads even when the Azure Functions runtime spins down.

Tables
──────
activity_events    — one row per workflow run (or per sub-action)
admin_views        — append-only audit of admin detail views (self-audit)

Retention policy
────────────────
Snapshots (full JSON blobs) are NOT stored in SQLite — only pointer URLs.
Default: 90-day metadata retention (configurable via RETENTION_DAYS env).
"""

import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

DB_PATH = os.getenv("ACTIVITY_DB_PATH", str(Path(__file__).parent / "activity.db"))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "90"))
SNAPSHOT_RETENTION_DAYS = int(os.getenv("SNAPSHOT_RETENTION_DAYS", "30"))


# ─── Schema ───────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS activity_events (
    event_id        TEXT PRIMARY KEY,
    user_email      TEXT NOT NULL,
    user_display_name TEXT,
    function_name   TEXT NOT NULL,   -- ims_generator | schedule_risk_manager
    triggered_at    REAL NOT NULL,    -- Unix timestamp UTC
    completed_at    REAL,             -- Unix timestamp UTC (null = still running)
    status          TEXT NOT NULL,    -- success | partial | failed
    project_code    TEXT,
    tranche         TEXT,
    input_files     TEXT,             -- JSON array
    result_summary  TEXT,             -- JSON object (row counts, totals)
    result_artifact_url TEXT,         -- URL to blob storage snapshot
    duration_ms     INTEGER,
    error_message   TEXT,
    created_at      REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_activity_user_triggered
    ON activity_events(user_email, triggered_at DESC);

CREATE INDEX IF NOT EXISTS idx_activity_function_triggered
    ON activity_events(function_name, triggered_at DESC);

CREATE INDEX IF NOT EXISTS idx_activity_project
    ON activity_events(project_code, triggered_at DESC);


CREATE TABLE IF NOT EXISTS admin_views (
    view_id         TEXT PRIMARY KEY,
    admin_email     TEXT NOT NULL,
    admin_display_name TEXT,
    target_event_id TEXT NOT NULL,
    target_function TEXT NOT NULL,
    target_project  TEXT,
    viewed_at       REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (target_event_id) REFERENCES activity_events(event_id)
);
"""


# ─── Connection helper ─────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level="DEFERRED")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db():
    conn = _get_conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        yield conn
    finally:
        conn.close()


# ─── Dataclass ────────────────────────────────────────────────────────────────

@dataclass
class ActivityEvent:
    event_id: str
    user_email: str
    function_name: str
    status: str
    triggered_at: float
    user_display_name: Optional[str] = None
    completed_at: Optional[float] = None
    project_code: Optional[str] = None
    tranche: Optional[str] = None
    input_files: list[dict] = field(default_factory=list)
    result_summary: Optional[dict] = None
    result_artifact_url: Optional[str] = None
    duration_ms: Optional[int] = None
    error_message: Optional[str] = None
    created_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ActivityEvent":
        d = dict(row)
        if d.get("input_files") and isinstance(d["input_files"], str):
            d["input_files"] = json.loads(d["input_files"])
        if d.get("result_summary") and isinstance(d["result_summary"], str):
            d["result_summary"] = json.loads(d["result_summary"])
        return cls(**d)


# ─── Event capture ───────────────────────────────────────────────────────────

def capture_event(
    user_email: str,
    function_name: str,
    project_code: Optional[str] = None,
    tranche: Optional[str] = None,
    input_files: Optional[list[dict]] = None,
    user_display_name: Optional[str] = None,
) -> str:
    """
    Insert a new activity event at the moment the user triggers a run.
    Returns the event_id for later use in complete_event().
    """
    event_id = str(uuid.uuid4())
    now = time.time()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO activity_events
               (event_id, user_email, user_display_name, function_name,
                triggered_at, status, project_code, tranche, input_files, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                user_email,
                user_display_name or "",
                function_name,
                now,
                "running",
                project_code or "",
                tranche or "",
                json.dumps(input_files or []),
                now,
            ),
        )
        conn.commit()
    logger.info("Event captured: %s | %s | %s", event_id, function_name, project_code)
    return event_id


def complete_event(
    event_id: str,
    status: str,  # success | partial | failed
    result_summary: Optional[dict] = None,
    result_artifact_url: Optional[str] = None,
    duration_ms: Optional[int] = None,
    error_message: Optional[str] = None,
) -> None:
    """Update an event when processing finishes (success or failure)."""
    now = time.time()
    with get_db() as conn:
        conn.execute(
            """UPDATE activity_events
               SET completed_at = ?, status = ?, result_summary = ?,
                   result_artifact_url = ?, duration_ms = ?, error_message = ?
               WHERE event_id = ?""",
            (
                now,
                status,
                json.dumps(result_summary) if result_summary else None,
                result_artifact_url or None,
                duration_ms,
                error_message or None,
                event_id,
            ),
        )
        conn.commit()
    logger.info("Event completed: %s | %s | %dms", event_id, status, duration_ms or 0)


def update_event_status(event_id: str, status: str) -> None:
    """Patch the status of an existing event (e.g. partial → failed)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE activity_events SET status = ? WHERE event_id = ?",
            (status, event_id),
        )
        conn.commit()


# ─── Query ───────────────────────────────────────────────────────────────────

@dataclass
class EventFilter:
    user_email: Optional[str] = None
    function_name: Optional[str] = None
    date_from: Optional[str] = None       # ISO date string
    date_to: Optional[str] = None         # ISO date string
    status: Optional[str] = None
    project_code: Optional[str] = None
    page: int = 1
    limit: int = 50
    sort_col: str = "triggered_at"
    sort_dir: str = "desc"                # asc | desc

    def apply(self, conn: sqlite3.Connection):
        """Return (rows, total_count)."""
        conditions = []
        params = []

        if self.user_email:
            conditions.append("user_email LIKE ?")
            params.append(f"%{self.user_email}%")
        if self.function_name:
            conditions.append("function_name = ?")
            params.append(self.function_name)
        if self.status:
            conditions.append("status = ?")
            params.append(self.status)
        if self.project_code:
            conditions.append("project_code LIKE ?")
            params.append(f"%{self.project_code}%")
        if self.date_from:
            conditions.append("triggered_at >= ?")
            params.append(self._parse_date(self.date_from))
        if self.date_to:
            conditions.append("triggered_at <= ?")
            params.append(self._parse_date(self.date_to, end_of_day=True))

        where = " AND ".join(conditions) if conditions else "1=1"

        # Sanitise sort
        allowed_sort = {"triggered_at", "function_name", "status", "project_code", "user_email"}
        sort_col = self.sort_col if self.sort_col in allowed_sort else "triggered_at"
        sort_dir = "DESC" if self.sort_dir == "desc" else "ASC"

        # Total count
        total = conn.execute(
            f"SELECT COUNT(*) FROM activity_events WHERE {where}", params
        ).fetchone()[0]

        # Paginated rows
        offset = (self.page - 1) * self.limit
        rows = conn.execute(
            f"""SELECT * FROM activity_events
                WHERE {where}
                ORDER BY {sort_col} {sort_dir}
                LIMIT ? OFFSET ?""",
            [*params, self.limit, offset],
        ).fetchall()

        return rows, total

    @staticmethod
    def _parse_date(iso_date: str, end_of_day: bool = False) -> float:
        fmt = "%Y-%m-%d"
        dt = datetime.strptime(iso_date[:10], fmt)
        if end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59)
        else:
            dt = dt.replace(hour=0, minute=0, second=0)
        return dt.replace(tzinfo=timezone.utc).timestamp()


def list_events(filters: EventFilter) -> dict:
    """List activity events with pagination."""
    with get_db() as conn:
        rows, total = filters.apply(conn)
        events = [ActivityEvent.from_row(r).to_dict() for r in rows]
    return {
        "events": events,
        "total": total,
        "page": filters.page,
        "limit": filters.limit,
        "pages": (total + filters.limit - 1) // filters.limit if filters.limit else 1,
    }


def get_event(event_id: str) -> Optional[dict]:
    """Get a single activity event by ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM activity_events WHERE event_id = ?", (event_id,)
        ).fetchone()
    if not row:
        return None
    return ActivityEvent.from_row(row).to_dict()


# ─── Admin self-audit ─────────────────────────────────────────────────────────

def log_admin_view(
    admin_email: str,
    admin_display_name: Optional[str],
    target_event_id: str,
    target_function: str,
    target_project: Optional[str],
) -> None:
    """Append a record every time an admin opens a detail view."""
    view_id = str(uuid.uuid4())
    now = time.time()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO admin_views
               (view_id, admin_email, admin_display_name,
                target_event_id, target_function, target_project, viewed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (view_id, admin_email, admin_display_name or "", target_event_id,
             target_function, target_project or "", now),
        )
        conn.commit()


# ─── Maintenance ─────────────────────────────────────────────────────────────

def purge_old_events() -> int:
    """
    Delete events older than RETENTION_DAYS.
    Call this on startup or via a scheduled trigger.
    Returns the number of rows deleted.
    """
    cutoff = time.time() - (RETENTION_DAYS * 86400)
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM activity_events WHERE triggered_at < ?", (cutoff,)
        )
        conn.commit()
        deleted = cur.rowcount
    if deleted:
        logger.info("Purged %d activity events older than %d days", deleted, RETENTION_DAYS)
    return deleted


# ─── Bootstrap ───────────────────────────────────────────────────────────────

def init_db() -> None:
    """Call once at app startup."""
    with get_db() as conn:
        logger.info("Activity DB initialised at %s", DB_PATH)
    purge_old_events()
