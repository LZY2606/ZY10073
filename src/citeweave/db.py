"""SQLite persistence: connections, transactions, recovery and helpers."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schema import SCHEMA


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DB:
    """Process-wide SQLite handle with a single writer lock.

    The workbench is single-machine and single-process; the writer lock
    serialises mutations and SQLite WAL keeps reads available.  Crash
    durability relies on SQLite's journal plus the application-level
    ``import_stage`` marker cleaned up on open.
    """

    def __init__(self, path: str | Path, recover_on_open: bool = False):
        self.path = str(path)
        self._recover_on_open = recover_on_open
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=FULL")
        self._migrate()
        if recover_on_open:
            self.recover()

    # ---------- plumbing ----------
    def _migrate(self) -> None:
        with self._lock, self.conn:
            self.conn.executescript(SCHEMA)

    def recover(self) -> int:
        """Drop staged imports left behind by a killed process.

        Returns the number of abandoned stages (also surfaced through the
        ``/api/recovery`` endpoint so the UI can explain what happened).
        """
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT id FROM versions WHERE import_stage='staged'").fetchall()
            for row in rows:
                self.conn.execute(
                    "UPDATE versions SET import_stage='abandoned' WHERE id=?",
                    (row["id"],))
            self.meta_set("last_recovered_count", str(len(rows)))
        return len(rows)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def tx(self) -> sqlite3.Connection:
        """Begin an explicit transaction (BEGIN IMMEDIATE)."""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def get(self, table: str, row_id: int) -> sqlite3.Row:
        row = self.one(f"SELECT * FROM {table} WHERE id=?", (row_id,))
        if row is None:
            from .models import NotFound
            raise NotFound(f"{table} {row_id} not found")
        return row

    # ---------- meta ----------
    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))

    # ---------- idempotency ----------
    def cached_request(self, request_key: str):
        row = self.one(
            "SELECT result_status, result_body FROM idempotent_requests "
            "WHERE request_key=?", (request_key,))
        if row is None:
            return None
        return row["result_status"], json.loads(row["result_body"])

    def remember_request(self, request_key: str, status: int,
                         body: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO idempotent_requests"
            "(request_key,created_at,result_status,result_body) "
            "VALUES(?,?,?,?)",
            (request_key, utcnow(), status, json.dumps(body, ensure_ascii=False)))
