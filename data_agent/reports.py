"""Saved Reports library — the destructive-ops surface (docs/ARCHITECTURE.md §4.3).

SQLite stand-in for the production Postgres store. Two invariants enforced
here, in code, regardless of what the model asks for:

- every operation is scoped to the ``user_id`` provided by the session (the
  model cannot pass or alter it);
- deletion is *soft* (``deleted_at`` timestamp), so a confirmed-by-mistake
  delete is recoverable.

The confirmation gate itself lives in the graph (LangGraph ``interrupt()``),
not here: this module only exposes preview (``find``) and execute
(``soft_delete``) halves.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config


@dataclass
class Report:
    id: int
    title: str
    created_at: str
    body: str = ""


class ReportLibrary:
    def __init__(self, path: Path = config.REPORTS_DB) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            )
            """
        )
        self._conn.commit()

    def save(self, user_id: str, title: str, body: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "INSERT INTO reports (user_id, title, body, created_at) VALUES (?, ?, ?, ?)",
            (user_id, title, body, now),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_reports(self, user_id: str) -> list[Report]:
        rows = self._conn.execute(
            "SELECT id, title, created_at FROM reports "
            "WHERE user_id = ? AND deleted_at IS NULL ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [Report(id=r[0], title=r[1], created_at=r[2]) for r in rows]

    def get(self, user_id: str, report_id: int) -> Optional[Report]:
        row = self._conn.execute(
            "SELECT id, title, created_at, body FROM reports "
            "WHERE user_id = ? AND id = ? AND deleted_at IS NULL",
            (user_id, report_id),
        ).fetchone()
        return Report(id=row[0], title=row[1], created_at=row[2], body=row[3]) if row else None

    def find(
        self,
        user_id: str,
        mentioning: Optional[str] = None,
        created_on: Optional[str] = None,  # ISO date, e.g. "2026-07-15"
        report_ids: Optional[list[int]] = None,
    ) -> list[Report]:
        """Preview half of the delete flow: which reports would match."""
        query = "SELECT id, title, created_at FROM reports WHERE user_id = ? AND deleted_at IS NULL"
        params: list = [user_id]
        if mentioning:
            query += " AND (title LIKE ? OR body LIKE ?)"
            params += [f"%{mentioning}%", f"%{mentioning}%"]
        if created_on:
            query += " AND substr(created_at, 1, 10) = ?"
            params.append(created_on)
        if report_ids:
            query += f" AND id IN ({','.join('?' * len(report_ids))})"
            params += report_ids
        rows = self._conn.execute(query + " ORDER BY created_at DESC", params).fetchall()
        return [Report(id=r[0], title=r[1], created_at=r[2]) for r in rows]

    def soft_delete(self, user_id: str, report_ids: list[int]) -> int:
        """Execute half of the delete flow; only ever called after user confirmation."""
        if not report_ids:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "UPDATE reports SET deleted_at = ? "
            f"WHERE user_id = ? AND deleted_at IS NULL AND id IN ({','.join('?' * len(report_ids))})",
            [now, user_id, *report_ids],
        )
        self._conn.commit()
        return cur.rowcount
