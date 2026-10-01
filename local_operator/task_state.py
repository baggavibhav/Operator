from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .config import APP_DIR, TASK_DB_PATH


class TaskStatus(str, Enum):
    NEW = "new"
    WAITING_FOR_INPUT = "waiting_for_input"
    READY = "ready"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


@dataclass
class TaskState:
    id: str
    goal: str
    status: TaskStatus = TaskStatus.NEW
    context: dict[str, Any] = field(default_factory=dict)
    pending_field: str | None = None
    pending_question: str | None = None
    plan: dict[str, Any] | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    current_step: int = 0
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    revision: int = 0

    @classmethod
    def create(cls, goal: str) -> "TaskState":
        return cls(id=str(uuid.uuid4()), goal=goal.strip())

    def snapshot(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


_ACTIVE_STATUSES = {
    TaskStatus.NEW,
    TaskStatus.WAITING_FOR_INPUT,
    TaskStatus.READY,
    TaskStatus.PLANNING,
    TaskStatus.EXECUTING,
    TaskStatus.VERIFYING,
}


class TaskStore:
    """Durable SQLite checkpoints with short-lived database handles."""

    def __init__(self, db_path: Path = TASK_DB_PATH):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if row is None:
                conn.execute("INSERT INTO metadata(key, value) VALUES ('schema_version', '1')")
            elif row[0] != "1":
                raise RuntimeError(f"Unsupported task database schema version: {row[0]}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    pending_field TEXT,
                    pending_question TEXT,
                    plan_json TEXT,
                    results_json TEXT NOT NULL,
                    current_step INTEGER NOT NULL,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    revision INTEGER NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def create(self, goal: str) -> TaskState:
        task = TaskState.create(goal)
        self.save(task)
        return task

    def save(self, task: TaskState) -> None:
        task.updated_at = time.time()
        task.revision += 1
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                    id, goal, status, context_json, pending_field, pending_question,
                    plan_json, results_json, current_step, error, created_at, updated_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    goal=excluded.goal,
                    status=excluded.status,
                    context_json=excluded.context_json,
                    pending_field=excluded.pending_field,
                    pending_question=excluded.pending_question,
                    plan_json=excluded.plan_json,
                    results_json=excluded.results_json,
                    current_step=excluded.current_step,
                    error=excluded.error,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    revision=excluded.revision
                """,
                (
                    task.id,
                    task.goal,
                    task.status.value,
                    json.dumps(task.context, default=str),
                    task.pending_field,
                    task.pending_question,
                    json.dumps(task.plan, default=str) if task.plan is not None else None,
                    json.dumps(task.results, default=str),
                    int(task.current_step),
                    task.error,
                    float(task.created_at),
                    float(task.updated_at),
                    int(task.revision),
                ),
            )

    def get(self, task_id: str) -> TaskState | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, goal, status, context_json, pending_field, pending_question,
                       plan_json, results_json, current_step, error, created_at, updated_at, revision
                FROM tasks WHERE id=?
                """,
                (task_id,),
            ).fetchone()
        return self._from_row(row) if row else None

    def latest_active(self) -> TaskState | None:
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        values = tuple(status.value for status in _ACTIVE_STATUSES)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT id, goal, status, context_json, pending_field, pending_question,
                       plan_json, results_json, current_step, error, created_at, updated_at, revision
                FROM tasks
                WHERE status IN ({placeholders})
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                values,
            ).fetchone()
        return self._from_row(row) if row else None

    def latest_waiting(self) -> TaskState | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, goal, status, context_json, pending_field, pending_question,
                       plan_json, results_json, current_step, error, created_at, updated_at, revision
                FROM tasks
                WHERE status=?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (TaskStatus.WAITING_FOR_INPUT.value,),
            ).fetchone()
        return self._from_row(row) if row else None

    def cancel_active(self, reason: str = "Cancelled by user.") -> TaskState | None:
        task = self.latest_active()
        if task is None:
            return None
        task.status = TaskStatus.CANCELLED
        task.pending_field = None
        task.pending_question = None
        task.error = reason
        task.context["cancelled_by_user"] = True
        self.save(task)
        return task

    def mark_inflight_interrupted(self) -> int:
        inflight = (
            TaskStatus.PLANNING.value,
            TaskStatus.EXECUTING.value,
            TaskStatus.VERIFYING.value,
        )
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE tasks
                SET status=?, error=COALESCE(error, ?), updated_at=?, revision=revision+1
                WHERE status IN (?, ?, ?)
                """,
                (
                    TaskStatus.INTERRUPTED.value,
                    "Previous run stopped before the task completed; automatic write resumption is disabled.",
                    now,
                    *inflight,
                ),
            )
            return int(cur.rowcount)

    def close(self) -> None:
        # Connections are scoped to each transaction; retained for API symmetry.
        return None

    def __enter__(self) -> "TaskStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def recent(self, limit: int = 20) -> list[TaskState]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, goal, status, context_json, pending_field, pending_question,
                       plan_json, results_json, current_step, error, created_at, updated_at, revision
                FROM tasks ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> TaskState:
        return TaskState(
            id=row[0],
            goal=row[1],
            status=TaskStatus(row[2]),
            context=json.loads(row[3] or "{}"),
            pending_field=row[4],
            pending_question=row[5],
            plan=json.loads(row[6]) if row[6] else None,
            results=json.loads(row[7] or "[]"),
            current_step=int(row[8] or 0),
            error=row[9],
            created_at=float(row[10]),
            updated_at=float(row[11]),
            revision=int(row[12]),
        )
