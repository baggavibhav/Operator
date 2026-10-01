from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import AUDIT_DB_PATH, APP_DIR


class AuditLog:
    """SQLite audit log with short-lived cross-platform connections."""

    def __init__(self, db_path: Path = AUDIT_DB_PATH):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    request TEXT,
                    model TEXT,
                    plan_json TEXT,
                    status TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    step_index INTEGER NOT NULL,
                    tool TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    args_json TEXT,
                    approved INTEGER,
                    result_json TEXT,
                    status TEXT,
                    duration_ms REAL,
                    error TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def start_run(self, request: str, model: str, plan: dict[str, Any] | None) -> int:
        with self._lock, self._connection() as conn:
            cur = conn.execute(
                "INSERT INTO runs(started_at, request, model, plan_json, status) VALUES (?, ?, ?, ?, ?)",
                (time.time(), request, model, json.dumps(plan) if plan else None, "running"),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, error=? WHERE id=?",
                (time.time(), status, error, run_id),
            )

    def action(self, run_id: int, step_index: int, tool: str, risk: str, args: dict[str, Any],
               approved: bool | None, result: dict[str, Any] | None, status: str,
               duration_ms: float, error: str | None = None) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO actions(run_id, step_index, tool, risk, args_json, approved,
                   result_json, status, duration_ms, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, step_index, tool, risk, json.dumps(args, default=str),
                    None if approved is None else int(approved),
                    json.dumps(result, default=str) if result is not None else None,
                    status, duration_ms, error,
                ),
            )

    def close(self) -> None:
        return None

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT id, started_at, finished_at, request, model, status, error FROM runs ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [
            {"id": r[0], "started_at": r[1], "finished_at": r[2], "request": r[3],
             "model": r[4], "status": r[5], "error": r[6]}
            for r in rows
        ]
