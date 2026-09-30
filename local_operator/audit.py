from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import AUDIT_DB_PATH, APP_DIR


class AuditLog:
    def __init__(self, db_path: Path = AUDIT_DB_PATH):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(
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
        self.conn.commit()

    def start_run(self, request: str, model: str, plan: dict[str, Any] | None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO runs(started_at, request, model, plan_json, status) VALUES (?, ?, ?, ?, ?)",
                (time.time(), request, model, json.dumps(plan) if plan else None, "running"),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, status=?, error=? WHERE id=?",
                (time.time(), status, error, run_id),
            )
            self.conn.commit()

    def action(self, run_id: int, step_index: int, tool: str, risk: str, args: dict[str, Any],
               approved: bool | None, result: dict[str, Any] | None, status: str,
               duration_ms: float, error: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO actions(run_id, step_index, tool, risk, args_json, approved,
                   result_json, status, duration_ms, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, step_index, tool, risk, json.dumps(args, default=str),
                    None if approved is None else int(approved),
                    json.dumps(result, default=str) if result is not None else None,
                    status, duration_ms, error,
                ),
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, started_at, finished_at, request, model, status, error FROM runs ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [
            {"id": r[0], "started_at": r[1], "finished_at": r[2], "request": r[3],
             "model": r[4], "status": r[5], "error": r[6]}
            for r in rows
        ]
