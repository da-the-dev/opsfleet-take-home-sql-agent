"""Per-user preference profile (docs/ARCHITECTURE.md §4.4, user level).

A small, auditable key-value profile per user ("Manager A prefers tables").
Updated only through an explicit tool call when the user expresses a
preference — never silently inferred — so "what do you know about me?" has a
truthful, inspectable answer.
"""

import json
import sqlite3
from pathlib import Path

from . import config


class PreferenceStore:
    def __init__(self, path: Path = config.REPORTS_DB) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS preferences (user_id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        self._conn.commit()

    def get(self, user_id: str) -> dict[str, str]:
        row = self._conn.execute(
            "SELECT data FROM preferences WHERE user_id = ?", (user_id,)
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def set(self, user_id: str, key: str, value: str) -> dict[str, str]:
        prefs = self.get(user_id)
        if value:
            prefs[key] = value
        else:
            prefs.pop(key, None)  # empty value clears the preference
        self._conn.execute(
            "INSERT INTO preferences (user_id, data) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data",
            (user_id, json.dumps(prefs)),
        )
        self._conn.commit()
        return prefs
