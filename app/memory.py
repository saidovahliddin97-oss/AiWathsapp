"""Persistence layer: relatives, short-term history, long-term facts,
processed-event registry (idempotency) and the outbox of unsent replies.

SQLite is used for the MVP - a single file, no extra service. All access goes
through :class:`Store`, so swapping in Postgres later only touches this module.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

from app.models import AgeGroup, Fact, Mode, Relative, StoredMessage

SOURCE_PRIORITY = {"user": 3, "config": 2, "conversation": 1}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS relatives (
    id TEXT PRIMARY KEY,
    phone TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    relation TEXT NOT NULL,
    age_group TEXT NOT NULL,
    address TEXT,
    mode TEXT NOT NULL DEFAULT 'FULL_CHAT',
    notes TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    wa_message_id TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_rel ON messages(relative_id, id);
CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_id TEXT,
    key TEXT,
    fact TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_id TEXT NOT NULL,
    text TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_id TEXT NOT NULL,
    phone TEXT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL
);
"""

# Events whose processing failed for a transient reason may be re-claimed
# when WhatsApp (or an operator) re-delivers them.
RETRYABLE_STATUS = "retryable"
MAX_EVENT_ATTEMPTS = 3


def normalize_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _exec(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    # ------------------------------------------------------------------ relatives
    def upsert_relative(self, rel: Relative) -> None:
        self._exec(
            """INSERT INTO relatives (id, phone, name, relation, age_group, address, mode, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET phone=excluded.phone, name=excluded.name,
                 relation=excluded.relation, age_group=excluded.age_group,
                 address=excluded.address, mode=excluded.mode, notes=excluded.notes""",
            (
                rel.id,
                normalize_phone(rel.phone),
                rel.name,
                rel.relation,
                rel.age_group.value,
                rel.address,
                rel.mode.value,
                json.dumps(rel.notes, ensure_ascii=False),
            ),
        )

    @staticmethod
    def _row_to_relative(row: sqlite3.Row) -> Relative:
        return Relative(
            id=row["id"],
            phone=row["phone"],
            name=row["name"],
            relation=row["relation"],
            age_group=AgeGroup(row["age_group"]),
            address=row["address"],
            mode=Mode(row["mode"]),
            notes=json.loads(row["notes"] or "[]"),
        )

    def find_relative_by_phone(self, phone: str) -> Relative | None:
        row = self._exec("SELECT * FROM relatives WHERE phone = ?", (normalize_phone(phone),)).fetchone()
        return self._row_to_relative(row) if row else None

    def get_relative(self, relative_id: str) -> Relative | None:
        row = self._exec("SELECT * FROM relatives WHERE id = ?", (relative_id,)).fetchone()
        return self._row_to_relative(row) if row else None

    def list_relatives(self) -> list[Relative]:
        return [self._row_to_relative(r) for r in self._exec("SELECT * FROM relatives ORDER BY id")]

    # ------------------------------------------------------------ short-term memory
    def add_message(
        self, relative_id: str, sender: str, text: str, kind: str = "text", wa_message_id: str | None = None
    ) -> None:
        self._exec(
            "INSERT INTO messages (relative_id, sender, text, kind, wa_message_id, created_at) VALUES (?,?,?,?,?,?)",
            (relative_id, sender, text, kind, wa_message_id, time.time()),
        )

    def recent_messages(self, relative_id: str, limit: int = 24, max_chars: int = 6000) -> list[StoredMessage]:
        """Most recent messages (oldest first), trimmed to a character budget so the
        prompt stays small even when individual messages are long."""
        rows = self._exec(
            "SELECT sender, text, kind FROM messages WHERE relative_id = ? ORDER BY id DESC LIMIT ?",
            (relative_id, limit),
        ).fetchall()
        picked: list[StoredMessage] = []
        total = 0
        for row in rows:
            total += len(row["text"])
            if picked and total > max_chars:
                break
            picked.append(StoredMessage(sender=row["sender"], text=row["text"], kind=row["kind"]))
        return list(reversed(picked))

    def recent_greetings(self, relative_id: str, limit: int = 5) -> list[str]:
        rows = self._exec(
            "SELECT text FROM messages WHERE relative_id = ? AND sender='assistant' AND kind='greeting' "
            "ORDER BY id DESC LIMIT ?",
            (relative_id, limit),
        ).fetchall()
        return [r["text"] for r in rows]

    def last_assistant_message_at(self, relative_id: str) -> float | None:
        row = self._exec(
            "SELECT MAX(created_at) AS t FROM messages WHERE relative_id = ? AND sender='assistant'",
            (relative_id,),
        ).fetchone()
        return row["t"] if row and row["t"] is not None else None

    # ------------------------------------------------------------------ idempotency
    def claim_event(self, event_id: str) -> bool:
        """Atomically claim an event for processing. Returns False for duplicates."""
        with self._lock:
            now = time.time()
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO processed_events (event_id, status, attempts, updated_at) VALUES (?, 'processing', 1, ?)",
                (event_id, now),
            )
            if cur.rowcount == 1:
                return True
            cur = self._conn.execute(
                "UPDATE processed_events SET status='processing', attempts=attempts+1, updated_at=? "
                "WHERE event_id=? AND status=? AND attempts < ?",
                (now, event_id, RETRYABLE_STATUS, MAX_EVENT_ATTEMPTS),
            )
            return cur.rowcount == 1

    def finish_event(self, event_id: str, status: str) -> None:
        self._exec(
            "UPDATE processed_events SET status=?, updated_at=? WHERE event_id=?", (status, time.time(), event_id)
        )

    def event_status(self, event_id: str) -> str | None:
        row = self._exec("SELECT status FROM processed_events WHERE event_id=?", (event_id,)).fetchone()
        return row["status"] if row else None

    # ------------------------------------------------------------ long-term memory
    def add_fact(self, fact: Fact, key: str | None = None) -> bool:
        """Store a durable fact. When ``key`` is given the fact competes with other
        active facts of the same key and scope: the more reliable source wins
        (user > config > conversation), then higher confidence, then the newer one.
        Returns True if the new fact became active."""
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM facts WHERE active=1 AND fact=? AND relative_id IS ?", (fact.fact, fact.relative_id)
            ).fetchone():
                return True
            active = True
            if key:
                rows = self._conn.execute(
                    "SELECT id, source, confidence FROM facts WHERE active=1 AND key=? AND relative_id IS ?",
                    (key, fact.relative_id),
                ).fetchall()
                new_rank = (SOURCE_PRIORITY.get(fact.source, 0), fact.confidence)
                for row in rows:
                    old_rank = (SOURCE_PRIORITY.get(row["source"], 0), row["confidence"])
                    if old_rank > new_rank:
                        active = False
                    else:
                        self._conn.execute("UPDATE facts SET active=0 WHERE id=?", (row["id"],))
            self._conn.execute(
                "INSERT INTO facts (relative_id, key, fact, source, confidence, active, created_at) VALUES (?,?,?,?,?,?,?)",
                (fact.relative_id, key, fact.fact, fact.source, fact.confidence, int(active), time.time()),
            )
            return active

    def get_facts(self, relative_id: str | None = None, min_confidence: float = 0.7) -> list[Fact]:
        """Active global facts plus facts scoped to ``relative_id``."""
        rows = self._exec(
            "SELECT relative_id, fact, source, confidence FROM facts WHERE active=1 AND confidence >= ? "
            "AND (relative_id IS NULL OR relative_id = ?) ORDER BY id",
            (min_confidence, relative_id),
        ).fetchall()
        return [
            Fact(fact=r["fact"], source=r["source"], confidence=r["confidence"], relative_id=r["relative_id"])
            for r in rows
        ]

    def deactivate_fact(self, fact_text: str) -> None:
        self._exec("UPDATE facts SET active=0 WHERE fact=?", (fact_text,))

    # Candidate facts: detected automatically, used only after approval.
    def add_fact_candidate(self, relative_id: str, text: str, reason: str) -> int:
        cur = self._exec(
            "INSERT INTO fact_candidates (relative_id, text, reason, created_at) VALUES (?,?,?,?)",
            (relative_id, text, reason, time.time()),
        )
        return int(cur.lastrowid)

    def list_fact_candidates(self, status: str = "pending") -> list[dict]:
        return [dict(r) for r in self._exec("SELECT * FROM fact_candidates WHERE status=? ORDER BY id", (status,))]

    def resolve_fact_candidate(self, candidate_id: int, approve: bool, fact_text: str | None = None) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM fact_candidates WHERE id=? AND status='pending'", (candidate_id,)
            ).fetchone()
            if not row:
                return False
            self._conn.execute(
                "UPDATE fact_candidates SET status=? WHERE id=?", ("approved" if approve else "rejected", candidate_id)
            )
        if approve:
            self.add_fact(
                Fact(
                    fact=fact_text or f"Родственник сообщил: {row['text']}",
                    source="conversation",
                    confidence=0.9,
                    relative_id=row["relative_id"],
                )
            )
        return True

    # ------------------------------------------------------------------- outbox
    def enqueue_outbox(self, relative_id: str, phone: str, text: str, kind: str, error: str) -> int:
        cur = self._exec(
            "INSERT INTO outbox (relative_id, phone, text, kind, attempts, last_error, created_at) VALUES (?,?,?,?,1,?,?)",
            (relative_id, phone, text, kind, error, time.time()),
        )
        return int(cur.lastrowid)

    def pending_outbox(self, max_attempts: int) -> list[dict]:
        return [
            dict(r)
            for r in self._exec(
                "SELECT * FROM outbox WHERE status='pending' AND attempts < ? ORDER BY id", (max_attempts,)
            )
        ]

    def mark_outbox(self, outbox_id: int, sent: bool, error: str | None = None) -> None:
        if sent:
            self._exec("UPDATE outbox SET status='sent', last_error=NULL WHERE id=?", (outbox_id,))
        else:
            self._exec("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?", (error, outbox_id))


# --------------------------------------------------------------------------- #
# Detection of potentially useful long-term facts.
#
# Heuristic MVP: look for durable life events in a relative's message
# (health, weddings, births, exams, travel, work, deaths ...). Matches become
# *candidates* only - they reach Claude's context after explicit approval, so
# nothing uncertain is ever presented as a confirmed fact.
# --------------------------------------------------------------------------- #
_CANDIDATE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("health", re.compile(r"бемор|касал|духтур|беморхона|больниц|болею|заболел|операци|дору", re.I)),
    ("wedding", re.compile(r"тӯй|туй\b|тўй|свадьб|никоҳ|хостгор", re.I)),
    ("birth", re.compile(r"таваллуд|зоид|фарзанд(дор)?\s+шуд|родил|день рождения|зодрӯз", re.I)),
    ("study", re.compile(r"имтиҳон|экзамен|донишгоҳ|университет|дохил\s+шуд|поступил|хатм", re.I)),
    ("work", re.compile(r"кор\s+(ёфт|пайдо)|кори\s+нав|работу|устроил|уволил", re.I)),
    ("travel", re.compile(r"сафар|мусофир|меоям|омада\s+истод|приед|билет|самолёт|самолет|ҳавопаймо", re.I)),
    ("loss", re.compile(r"вафот|фавт|умер|скончал|марҳум|таъзия", re.I)),
    ("home", re.compile(r"хона\s+(харид|сохт|гирифт)|кӯч|переех|ремонт", re.I)),
]


def detect_fact_candidates(text: str) -> list[str]:
    """Return categories of durable information found in ``text``."""
    if not text or len(text.strip()) < 6:
        return []
    return [name for name, pattern in _CANDIDATE_PATTERNS if pattern.search(text)]


def load_relatives_config(store: Store, path: str | Path) -> int:
    """Load relatives and user facts from a JSON config file (idempotent)."""
    p = Path(path)
    if not p.exists():
        return 0
    data = json.loads(p.read_text(encoding="utf-8"))
    count = 0
    for item in data.get("relatives", []):
        store.upsert_relative(Relative(**item))
        count += 1
    for item in data.get("known_facts", []):
        key = item.pop("key", None) if isinstance(item, dict) else None
        store.add_fact(Fact(**item), key=key)
    return count
