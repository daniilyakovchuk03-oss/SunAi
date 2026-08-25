"""Многопользовательский слой: аккаунты, пользователи, сессии, настройки.

Один аккаунт = одна компания со своим номером, правилами и промптами.
Сейчас он один, но структура рассчитана на несколько.
"""
import hashlib
import json
import os
import secrets
import time
from typing import Any, Optional

from . import storage as st

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    email      TEXT NOT NULL UNIQUE,
    pwd_hash   TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    role       TEXT NOT NULL DEFAULT 'admin',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    account_id  INTEGER PRIMARY KEY,
    rules_json  TEXT NOT NULL DEFAULT '{}',
    prompt_json TEXT NOT NULL DEFAULT '{}',
    updated_at  REAL NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
"""

PBKDF_ROUNDS = 240_000

DEFAULT_PROMPT = {
    "goal": "квалифицировать клиента и довести до записи на встречу с менеджером",
    "tone": "На «вы», коротко, без канцелярита и лишних восклицаний.",
    "forbidden": "Не обещать скидок, точных сроков и гарантий. Не выдумывать факты о продукте.",
    "knowledge": "",
    "greeting": "",
    "max_sentences": 3,
}


def init() -> None:
    with st._lock:
        st.db().executescript(SCHEMA)
        cols = [r["name"] for r in st.db().execute("PRAGMA table_info(dialogs)")]
        if "account_id" not in cols:
            st.db().execute("ALTER TABLE dialogs ADD COLUMN account_id INTEGER DEFAULT 1")
        st.db().commit()


# ---------- пароли ----------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF_ROUNDS)
    return f"pbkdf2${PBKDF_ROUNDS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds))
        return secrets.compare_digest(dk.hex(), digest)
    except Exception:
        return False


# ---------- аккаунты и пользователи ----------

def bootstrap(default_rules: dict) -> tuple[str, str] | None:
    """Первый запуск: создаёт аккаунт и администратора.

    Пароль берётся из ADMIN_PASSWORD, иначе генерируется и печатается один раз.
    """
    init()
    if st.one("SELECT id FROM accounts LIMIT 1"):
        return None

    now = time.time()
    account_id = st.run("INSERT INTO accounts (name, created_at) VALUES (?, ?)",
                        ("Основной аккаунт", now))
    email = os.getenv("ADMIN_EMAIL", "admin@local")
    password = os.getenv("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
    st.run(
        "INSERT INTO users (account_id, email, pwd_hash, name, role, created_at)"
        " VALUES (?, ?, ?, ?, 'owner', ?)",
        (account_id, email, hash_password(password), "Администратор", now),
    )
    st.run(
        "INSERT INTO settings (account_id, rules_json, prompt_json, updated_at)"
        " VALUES (?, ?, ?, ?)",
        (account_id, json.dumps(default_rules, ensure_ascii=False),
         json.dumps(DEFAULT_PROMPT, ensure_ascii=False), now),
    )
    return email, password


def user_by_email(email: str) -> Optional[dict]:
    row = st.one("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))
    return dict(row) if row else None


def create_session(user_id: int, days: int = 14) -> str:
    token = secrets.token_urlsafe(32)
    st.run("INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
           (token, user_id, time.time() + days * 86400))
    return token


def user_by_session(token: str) -> Optional[dict]:
    if not token:
        return None
    row = st.one(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token = ? AND s.expires_at > ?",
        (token, time.time()),
    )
    return dict(row) if row else None


def drop_session(token: str) -> None:
    st.run("DELETE FROM sessions WHERE token = ?", (token,))


def purge_sessions() -> None:
    st.run("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))


# ---------- настройки ----------

def _row(account_id: int) -> dict:
    row = st.one("SELECT * FROM settings WHERE account_id = ?", (account_id,))
    if row is None:
        st.run("INSERT INTO settings (account_id, updated_at) VALUES (?, ?)",
               (account_id, time.time()))
        row = st.one("SELECT * FROM settings WHERE account_id = ?", (account_id,))
    return dict(row)


def get_rules(account_id: int = 1) -> dict:
    return json.loads(_row(account_id)["rules_json"] or "{}")


def save_rules(account_id: int, rules: dict, actor: str = "") -> None:
    st.run("UPDATE settings SET rules_json = ?, updated_at = ?, updated_by = ?"
           " WHERE account_id = ?",
           (json.dumps(rules, ensure_ascii=False), time.time(), actor, account_id))
    log(account_id, actor, "rules.save", {"keys": sorted(rules.keys())})


def get_prompt(account_id: int = 1) -> dict:
    data = json.loads(_row(account_id)["prompt_json"] or "{}")
    return {**DEFAULT_PROMPT, **data}


def save_prompt(account_id: int, prompt: dict, actor: str = "") -> None:
    merged = {**get_prompt(account_id), **prompt}
    st.run("UPDATE settings SET prompt_json = ?, updated_at = ?, updated_by = ?"
           " WHERE account_id = ?",
           (json.dumps(merged, ensure_ascii=False), time.time(), actor, account_id))
    log(account_id, actor, "prompt.save", {})


def log(account_id: int, actor: str, action: str, payload: dict) -> None:
    st.run("INSERT INTO audit (account_id, actor, action, payload, created_at)"
           " VALUES (?, ?, ?, ?, ?)",
           (account_id, actor, action, json.dumps(payload, ensure_ascii=False), time.time()))


def audit(account_id: int = 1, limit: int = 30) -> list[dict]:
    rows = st.q("SELECT * FROM audit WHERE account_id = ? ORDER BY id DESC LIMIT ?",
                (account_id, limit))
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out
