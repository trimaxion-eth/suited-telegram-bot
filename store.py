"""SQLite state: who to DM, what they asked for, and what we already sent.

One file, stdlib only. The `sent` table is the important one: it makes pushes
idempotent across restarts, so redeploying the bot at 22:00 does not replay
"the Major starts in 15 minutes" to everyone who already got it. Rows are
pruned after a week, which is longer than any alert window here.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    added      INTEGER NOT NULL,
    alerts     INTEGER NOT NULL DEFAULT 1,
    digest     INTEGER NOT NULL DEFAULT 1,
    quiet_from INTEGER NOT NULL DEFAULT 2,   -- UTC hours; 2-9 is the dead zone
    quiet_to   INTEGER NOT NULL DEFAULT 9,
    last_push  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stakes (
    chat_id    INTEGER NOT NULL,
    stake      TEXT NOT NULL,               -- id prefix: nl200
    min_seated INTEGER NOT NULL DEFAULT 2,
    PRIMARY KEY (chat_id, stake)
);
CREATE TABLE IF NOT EXISTS sent (
    key TEXT PRIMARY KEY,
    at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- users -------------------------------------------------------------

    def touch_user(self, chat_id: int, username: str | None, first_name: str | None) -> bool:
        """Upsert on first contact. Returns True when the row is new."""
        row = self.db.execute("SELECT 1 FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
        now = int(time.time())
        if row:
            # Never blank a known handle with a null we merely did not receive.
            self.db.execute(
                "UPDATE users SET username = COALESCE(?, username), "
                "first_name = COALESCE(?, first_name) WHERE chat_id = ?",
                (username, first_name, chat_id),
            )
            self.db.commit()
            return False
        self.db.execute(
            "INSERT INTO users (chat_id, username, first_name, added) VALUES (?, ?, ?, ?)",
            (chat_id, username, first_name, now),
        )
        self.db.commit()
        return True

    def user(self, chat_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()

    def users(self, only_alerts: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM users"
        if only_alerts:
            sql += " WHERE alerts = 1"
        return list(self.db.execute(sql + " ORDER BY added"))

    def all_chats(self) -> list[int]:
        return [row["chat_id"] for row in self.db.execute("SELECT chat_id FROM users")]

    def set_flag(self, chat_id: int, field: str, value: int) -> None:
        if field not in ("alerts", "digest"):
            raise ValueError(field)
        self.db.execute(f"UPDATE users SET {field} = ? WHERE chat_id = ?", (value, chat_id))
        self.db.commit()

    def set_quiet(self, chat_id: int, start: int, end: int) -> None:
        self.db.execute(
            "UPDATE users SET quiet_from = ?, quiet_to = ? WHERE chat_id = ?",
            (start % 24, end % 24, chat_id),
        )
        self.db.commit()

    def mark_pushed(self, chat_id: int) -> None:
        self.db.execute(
            "UPDATE users SET last_push = ? WHERE chat_id = ?", (int(time.time()), chat_id)
        )
        self.db.commit()

    # -- per-stake table alerts -------------------------------------------

    def subscribe(self, chat_id: int, stake: str, min_seated: int = 2) -> None:
        self.db.execute(
            "INSERT INTO stakes (chat_id, stake, min_seated) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, stake) DO UPDATE SET min_seated = excluded.min_seated",
            (chat_id, stake, max(1, min_seated)),
        )
        self.db.commit()

    def unsubscribe(self, chat_id: int, stake: str) -> None:
        self.db.execute("DELETE FROM stakes WHERE chat_id = ? AND stake = ?", (chat_id, stake))
        self.db.commit()

    def subscriptions(self, chat_id: int) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM stakes WHERE chat_id = ? ORDER BY stake", (chat_id,)
            )
        )

    def subscribe_all(self, chat_id: int, min_seated: int = 2, stakes: list[str] | None = None):
        for stake in stakes or []:
            self.subscribe(chat_id, stake, min_seated)

    # -- idempotent sends --------------------------------------------------

    def was_sent(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM sent WHERE key = ?", (key,)).fetchone() is not None

    def mark_sent(self, key: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO sent (key, at) VALUES (?, ?)", (key, int(time.time()))
        )
        self.db.commit()

    def prune(self, older_than_seconds: int = 7 * 86400) -> None:
        self.db.execute("DELETE FROM sent WHERE at < ?", (int(time.time()) - older_than_seconds,))
        self.db.commit()

    # -- misc --------------------------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else default

    def set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, str(value)),
        )
        self.db.commit()
