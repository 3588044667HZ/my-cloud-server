"""状态持久化：SQLite 存储更新与动作状态，支持断电恢复。

表结构（与 client-executor.md 状态机一致）：
- updates:   update_id, status, started_at, finished_at
- actions:   update_id, action_id, type, status, attempt, last_error,
             rollback_status, started_at, finished_at

动作状态流转：PENDING → DOWNLOADING → ... → COMPLETED，失败 → ROLLING_BACK → ROLLED_BACK。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .exceptions import StateError

logger = logging.getLogger(__name__)

# 动作状态常量
PENDING = "PENDING"
DOWNLOADING = "DOWNLOADING"
DOWNLOADED = "DOWNLOADED"
VERIFYING = "VERIFYING"
VERIFIED = "VERIFIED"
EXECUTING = "EXECUTING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
ROLLING_BACK = "ROLLING_BACK"
ROLLED_BACK = "ROLLED_BACK"
SKIPPED = "SKIPPED"

# 更新状态常量
UPDATE_RUNNING = "RUNNING"
UPDATE_COMPLETED = "COMPLETED"
UPDATE_FAILED = "FAILED"

_ISO = "%Y-%m-%dT%H:%M:%S.%fZ"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_ISO)


class StateStore:
    """SQLite 状态存储（线程安全：内部持锁）。"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        try:
            with self._lock:
                self._init_schema()
        except sqlite3.Error as e:
            self._conn.close()
            raise StateError(f"初始化状态库失败: {e}") from e

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS updates (
                update_id   TEXT PRIMARY KEY,
                status      TEXT NOT NULL,
                started_at  TEXT,
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS actions (
                update_id        TEXT NOT NULL,
                action_id        TEXT NOT NULL,
                type             TEXT NOT NULL,
                status           TEXT NOT NULL,
                attempt          INTEGER NOT NULL DEFAULT 0,
                last_error       TEXT,
                rollback_status  TEXT,
                started_at       TEXT,
                finished_at      TEXT,
                PRIMARY KEY (update_id, action_id)
            );
            """
        )
        self._conn.commit()

    @property
    def db_path(self) -> str:
        return self._db_path

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error as e:
                logger.warning("关闭状态库失败: %s", e)

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        try:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur
        except sqlite3.Error as e:
            raise StateError(f"状态库操作失败: {e}") from e

    # ---- 更新级 ----

    def record_update_start(self, update_id: str) -> None:
        with self._lock:
            self._execute(
                """INSERT INTO updates(update_id, status, started_at)
                   VALUES(?, ?, ?)
                   ON CONFLICT(update_id) DO UPDATE SET
                       status=excluded.status, started_at=excluded.started_at,
                       finished_at=NULL""",
                (update_id, UPDATE_RUNNING, _now()),
            )

    def record_update_finish(self, update_id: str, status: str) -> None:
        with self._lock:
            self._execute(
                "UPDATE updates SET status=?, finished_at=? WHERE update_id=?",
                (status, _now(), update_id),
            )

    def get_update_status(self, update_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._execute(
                "SELECT update_id, status, started_at, finished_at FROM updates WHERE update_id=?",
                (update_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ("update_id", "status", "started_at", "finished_at")
        return dict(zip(keys, row))

    def get_incomplete_update(self) -> Optional[str]:
        """返回最近一个未完成（非 COMPLETED / FAILED）的 update_id。"""
        with self._lock:
            cur = self._execute(
                "SELECT update_id FROM updates WHERE status NOT IN (?, ?) ORDER BY started_at DESC",
                (UPDATE_COMPLETED, UPDATE_FAILED),
            )
            row = cur.fetchone()
        return row[0] if row else None

    # ---- 动作级 ----

    def record_action_start(self, update_id: str, action_id: str, action_type: str) -> None:
        with self._lock:
            self._execute(
                """INSERT INTO actions(update_id, action_id, type, status, attempt,
                                       started_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(update_id, action_id) DO UPDATE SET
                       status=excluded.status,
                       attempt=attempt + 1,
                       last_error=NULL,
                       started_at=excluded.started_at,
                       finished_at=NULL,
                       rollback_status=NULL""",
                (update_id, action_id, action_type, EXECUTING, 1, _now()),
            )

    def record_action_success(self, update_id: str, action_id: str) -> None:
        with self._lock:
            self._execute(
                "UPDATE actions SET status=?, finished_at=? WHERE update_id=? AND action_id=?",
                (COMPLETED, _now(), update_id, action_id),
            )

    def record_action_failure(
        self, update_id: str, action_id: str, error: str
    ) -> None:
        with self._lock:
            self._execute(
                "UPDATE actions SET status=?, last_error=?, finished_at=? WHERE update_id=? AND action_id=?",
                (FAILED, error, _now(), update_id, action_id),
            )

    def record_action_status(self, update_id: str, action_id: str, status: str) -> None:
        with self._lock:
            self._execute(
                "UPDATE actions SET status=? WHERE update_id=? AND action_id=?",
                (status, update_id, action_id),
            )

    def record_rollback_status(
        self, update_id: str, action_id: str, status: str, error: Optional[str] = None
    ) -> None:
        with self._lock:
            self._execute(
                "UPDATE actions SET rollback_status=?, last_error=? WHERE update_id=? AND action_id=?",
                (status, error, update_id, action_id),
            )

    def get_action_status(self, update_id: str, action_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._execute(
                """SELECT update_id, action_id, type, status, attempt, last_error,
                          rollback_status, started_at, finished_at
                   FROM actions WHERE update_id=? AND action_id=?""",
                (update_id, action_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ("update_id", "action_id", "type", "status", "attempt",
                "last_error", "rollback_status", "started_at", "finished_at")
        return dict(zip(keys, row))

    def get_incomplete_actions(self, update_id: str) -> List[Dict[str, Any]]:
        """返回未完成（status != COMPLETED 且 != SKIPPED）的动作。"""
        with self._lock:
            cur = self._execute(
                "SELECT action_id, status FROM actions WHERE update_id=? AND status NOT IN (?, ?)",
                (update_id, COMPLETED, SKIPPED),
            )
            rows = cur.fetchall()
        return [{"action_id": r[0], "status": r[1]} for r in rows]

    def get_all_actions(self, update_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._execute(
                """SELECT update_id, action_id, type, status, attempt, last_error,
                          rollback_status, started_at, finished_at
                   FROM actions WHERE update_id=? ORDER BY started_at""",
                (update_id,),
            )
            rows = cur.fetchall()
        keys = ("update_id", "action_id", "type", "status", "attempt",
                "last_error", "rollback_status", "started_at", "finished_at")
        return [dict(zip(keys, r)) for r in rows]


__all__ = [
    "StateStore",
    "PENDING", "DOWNLOADING", "DOWNLOADED", "VERIFYING", "VERIFIED",
    "EXECUTING", "COMPLETED", "FAILED", "ROLLING_BACK", "ROLLED_BACK", "SKIPPED",
    "UPDATE_RUNNING", "UPDATE_COMPLETED", "UPDATE_FAILED",
]
