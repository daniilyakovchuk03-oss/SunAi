import json
import sqlite3
import threading
import time
from typing import Any, Optional

from .config import settings

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS dialogs (
    chat_id           TEXT PRIMARY KEY,
    channel_id        TEXT,
    phone             TEXT,
    state             TEXT NOT NULL DEFAULT 'sleeping',
    amo_lead_id       INTEGER,
    window_opened_at  REAL,
    last_inbound_at   REAL,
    last_outbound_at  REAL,
    ai_message_count  INTEGER NOT NULL DEFAULT 0,
    followup_count    INTEGER NOT NULL DEFAULT 0,
    handed_off_at     REAL,
    meta              TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    direction   TEXT NOT NULL,          -- inbound | outbound
    author      TEXT NOT NULL,          -- client | ai | manager | system
    text        TEXT NOT NULL,
    crm_user_id TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    payload      TEXT NOT NULL,
    run_after    REAL NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | done | failed
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_pending ON jobs(status, run_after);

CREATE TABLE IF NOT EXISTS decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    reason     TEXT NOT NULL,
    checks     TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_chat ON decisions(chat_id, id);
"""


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def q(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        cur = db().execute(sql, params)
        rows = cur.fetchall()
        db().commit()
        return rows


def one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    rows = q(sql, params)
    return rows[0] if rows else None


def run(sql: str, params: tuple = ()) -> int:
    with _lock:
        cur = db().execute(sql, params)
        db().commit()
        return cur.lastrowid


# ---------- диалоги ----------

def get_dialog(chat_id: str) -> Optional[sqlite3.Row]:
    return one("SELECT * FROM dialogs WHERE chat_id = ?", (chat_id,))


def upsert_dialog(chat_id: str, **fields: Any) -> sqlite3.Row:
    existing = get_dialog(chat_id)
    if existing is None:
        run(
            "INSERT INTO dialogs (chat_id, channel_id, phone, state) VALUES (?, ?, ?, 'sleeping')",
            (chat_id, fields.get("channel_id"), fields.get("phone")),
        )
    if fields:
        allowed = {
            "channel_id", "phone", "state", "amo_lead_id", "window_opened_at",
            "last_inbound_at", "last_outbound_at", "ai_message_count",
            "followup_count", "handed_off_at", "meta",
        }
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                vals.append(json.dumps(v, ensure_ascii=False) if k == "meta" else v)
        if sets:
            vals.append(chat_id)
            run(f"UPDATE dialogs SET {', '.join(sets)} WHERE chat_id = ?", tuple(vals))
    return get_dialog(chat_id)


def bump(chat_id: str, field: str, by: int = 1) -> None:
    run(f"UPDATE dialogs SET {field} = {field} + ? WHERE chat_id = ?", (by, chat_id))


# ---------- сообщения ----------

def add_message(chat_id: str, direction: str, author: str, text: str,
                crm_user_id: str | None = None) -> int:
    return run(
        "INSERT INTO messages (chat_id, direction, author, text, crm_user_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, direction, author, text, crm_user_id, time.time()),
    )


def history(chat_id: str, limit: int = 40) -> list[dict]:
    rows = q(
        "SELECT * FROM (SELECT * FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?)"
        " ORDER BY id ASC",
        (chat_id, limit),
    )
    return [dict(r) for r in rows]


# ---------- решения ----------

def add_decision(chat_id: str, verdict: str, reason: str, checks: list[dict]) -> None:
    run(
        "INSERT INTO decisions (chat_id, verdict, reason, checks, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, verdict, reason, json.dumps(checks, ensure_ascii=False), time.time()),
    )


def decisions(chat_id: str, limit: int = 20) -> list[dict]:
    rows = q(
        "SELECT * FROM (SELECT * FROM decisions WHERE chat_id = ? ORDER BY id DESC LIMIT ?)"
        " ORDER BY id ASC",
        (chat_id, limit),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["checks"] = json.loads(d["checks"])
        out.append(d)
    return out


# ---------- очередь ----------

def enqueue(payload: dict, delay: float = 0.0) -> int:
    return run(
        "INSERT INTO jobs (payload, run_after, created_at) VALUES (?, ?, ?)",
        (json.dumps(payload, ensure_ascii=False), time.time() + delay, time.time()),
    )


def claim_jobs(limit: int = 10) -> list[dict]:
    rows = q(
        "SELECT * FROM jobs WHERE status = 'pending' AND run_after <= ? ORDER BY id LIMIT ?",
        (time.time(), limit),
    )
    jobs = []
    for r in rows:
        run("UPDATE jobs SET status = 'running', attempts = attempts + 1 WHERE id = ?", (r["id"],))
        jobs.append({"id": r["id"], "payload": json.loads(r["payload"]), "attempts": r["attempts"]})
    return jobs


def finish_job(job_id: int, ok: bool, retry_in: float = 30.0, max_attempts: int = 5) -> None:
    if ok:
        run("UPDATE jobs SET status = 'done' WHERE id = ?", (job_id,))
        return
    row = one("SELECT attempts FROM jobs WHERE id = ?", (job_id,))
    if row and row["attempts"] >= max_attempts:
        run("UPDATE jobs SET status = 'failed' WHERE id = ?", (job_id,))
    else:
        run("UPDATE jobs SET status = 'pending', run_after = ? WHERE id = ?",
            (time.time() + retry_in, job_id))


def reset_all() -> None:
    for t in ("dialogs", "messages", "jobs", "decisions"):
        run(f"DELETE FROM {t}")
