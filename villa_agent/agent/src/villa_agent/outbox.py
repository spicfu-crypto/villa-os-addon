"""Durable outgoing queue in SQLite.

Every snapshot and event is written here first and deleted only after the MQTT broker
acknowledged it (QoS 1). If the broker or the internet is down, messages wait here
(max QUEUE_MAX_AGE_DAYS / QUEUE_MAX_MB, oldest dropped first) and are sent in order.

It also stores:
- seq: a monotonic counter that survives restarts,
- agent_instance: a random id created with the queue file. If the file is ever lost,
  seq starts again at 1 with a NEW instance id, so the cloud never mistakes new
  messages for duplicates.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueuedMessage:
    id: int
    topic: str
    payload: bytes
    created_at: float


class Outbox:
    def __init__(self, path: str, *, max_age_s: float, max_bytes: int, clock: Any = time.time):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._lock = threading.Lock()
        self._max_age_s = max_age_s
        self._max_bytes = max_bytes
        self._clock = clock
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            # FULL: a power cut must not undo a queued leak event or roll back seq.
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " topic TEXT NOT NULL, payload BLOB NOT NULL, created_at REAL NOT NULL)"
            )
            self._db.execute("INSERT OR IGNORE INTO meta VALUES ('seq', '0')")
            self._db.execute("INSERT OR IGNORE INTO meta VALUES ('hb_seq', '0')")
            self._db.execute("INSERT OR IGNORE INTO meta VALUES ('dropped', '0')")
            self._db.execute(
                "INSERT OR IGNORE INTO meta VALUES ('instance', ?)", (uuid.uuid4().hex,)
            )
            self.instance_id: str = self._db.execute(
                "SELECT value FROM meta WHERE key='instance'"
            ).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _incr(self, key: str, by: int = 1) -> int:
        row = self._db.execute(
            "UPDATE meta SET value = CAST(value AS INTEGER) + ? WHERE key=? RETURNING value",
            (by, key),
        ).fetchone()
        return int(row[0])

    def next_seq(self) -> int:
        """Sequence for queued messages (snapshots, events)."""
        with self._lock:
            return self._incr("seq")

    def next_heartbeat_seq(self) -> int:
        """Heartbeats are live-only, so they get their own counter (no gaps in `seq`)."""
        with self._lock:
            return self._incr("hb_seq")

    @property
    def dropped(self) -> int:
        """Messages dropped because of the age/size limit, since the queue file was created."""
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key='dropped'").fetchone()
            return int(row[0])

    def put(self, topic: str, message: dict[str, Any], *, replace_topic: bool = False) -> None:
        """Queue a message. replace_topic=True drops older unsent messages on the same topic
        (used for snapshots: after a long outage only the latest snapshot matters)."""
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
        with self._lock:
            if replace_topic:
                self._db.execute("DELETE FROM outbox WHERE topic = ?", (topic,))
            self._db.execute(
                "INSERT INTO outbox (topic, payload, created_at) VALUES (?, ?, ?)",
                (topic, payload, self._clock()),
            )
            self._prune_locked()

    def _prune_locked(self) -> None:
        cutoff = self._clock() - self._max_age_s
        cur = self._db.execute("DELETE FROM outbox WHERE created_at < ?", (cutoff,))
        dropped = cur.rowcount
        total = self._db.execute("SELECT COALESCE(SUM(LENGTH(payload)), 0) FROM outbox").fetchone()[
            0
        ]
        while total > self._max_bytes:
            row = self._db.execute(
                "SELECT id, LENGTH(payload) FROM outbox ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                break
            self._db.execute("DELETE FROM outbox WHERE id = ?", (row[0],))
            total -= row[1]
            dropped += 1
        if dropped:
            self._incr("dropped", dropped)
            log.warning("queue limit reached, dropped oldest messages", extra={"dropped": dropped})

    def peek(self, limit: int = 100) -> list[QueuedMessage]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, topic, payload, created_at FROM outbox ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [QueuedMessage(*r) for r in rows]

    def ack(self, message_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM outbox WHERE id = ?", (message_id,))

    def __len__(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
