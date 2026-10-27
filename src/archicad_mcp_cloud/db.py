"""SQLite storage: users and their subscription, login codes, signed-in devices and AI usage.

Secrets are stored as hashes: device tokens with SHA-256, login codes with an
HMAC keyed by SECRET_KEY.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    created REAL NOT NULL,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    status TEXT NOT NULL DEFAULT 'none',
    period_start REAL,
    period_end REAL,
    cancel_at_period_end INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS login_codes (
    email TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL,
    expires REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    sent REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT,
    created REAL NOT NULL,
    last_used REAL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    ts REAL NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cost_micros INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_user_ts ON usage(user_id, ts);
CREATE TABLE IF NOT EXISTS stripe_events (id TEXT PRIMARY KEY, received REAL NOT NULL);
"""

CODE_LIFETIME = 10 * 60
CODE_ATTEMPTS = 5
CODE_RESEND_INTERVAL = 30


@dataclass
class User:
    id: int
    email: str
    created: float
    stripe_customer_id: str | None
    stripe_subscription_id: str | None
    status: str
    period_start: float | None
    period_end: float | None
    cancel_at_period_end: int


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Database:
    def __init__(self, path: str, secret_key: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._secret = (secret_key or "development-only-secret").encode()
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    # --- users --------------------------------------------------------------

    def user_by_email(self, email: str) -> User | None:
        rows = self._query("SELECT * FROM users WHERE email = ?", (email,))
        return User(**dict(rows[0])) if rows else None

    def user_by_id(self, user_id: int) -> User | None:
        rows = self._query("SELECT * FROM users WHERE id = ?", (user_id,))
        return User(**dict(rows[0])) if rows else None

    def user_by_customer(self, customer_id: str) -> User | None:
        rows = self._query("SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,))
        return User(**dict(rows[0])) if rows else None

    def get_or_create_user(self, email: str) -> User:
        self._execute("INSERT OR IGNORE INTO users (email, created) VALUES (?, ?)", (email, time.time()))
        return self.user_by_email(email)

    def set_customer(self, user_id: int, customer_id: str) -> None:
        self._execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", (customer_id, user_id))

    def set_subscription(self, user_id: int, subscription_id: str | None, status: str,
                         period_start: float | None, period_end: float | None, cancel_at_period_end: bool) -> None:
        self._execute(
            "UPDATE users SET stripe_subscription_id = ?, status = ?, period_start = ?, period_end = ?, "
            "cancel_at_period_end = ? WHERE id = ?",
            (subscription_id, status, period_start, period_end, int(cancel_at_period_end), user_id),
        )

    # --- login codes --------------------------------------------------------

    def _code_hash(self, email: str, code: str) -> str:
        return hmac.new(self._secret, f"{email}:{code}".encode(), hashlib.sha256).hexdigest()

    def create_login_code(self, email: str) -> str | None:
        """A new 6-digit code, or None when one was sent moments ago."""
        rows = self._query("SELECT sent FROM login_codes WHERE email = ?", (email,))
        now = time.time()
        if rows and now - rows[0]["sent"] < CODE_RESEND_INTERVAL:
            return None
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._execute(
            "INSERT OR REPLACE INTO login_codes (email, code_hash, expires, attempts, sent) VALUES (?, ?, ?, 0, ?)",
            (email, self._code_hash(email, code), now + CODE_LIFETIME, now),
        )
        return code

    def check_login_code(self, email: str, code: str) -> bool:
        rows = self._query("SELECT * FROM login_codes WHERE email = ?", (email,))
        if not rows:
            return False
        row = rows[0]
        if row["expires"] < time.time() or row["attempts"] >= CODE_ATTEMPTS:
            self._execute("DELETE FROM login_codes WHERE email = ?", (email,))
            return False
        if not hmac.compare_digest(row["code_hash"], self._code_hash(email, code.strip())):
            self._execute("UPDATE login_codes SET attempts = attempts + 1 WHERE email = ?", (email,))
            return False
        self._execute("DELETE FROM login_codes WHERE email = ?", (email,))
        return True

    # --- devices ------------------------------------------------------------

    def create_device(self, user_id: int, name: str) -> str:
        token = "amc_" + secrets.token_urlsafe(32)
        self._execute(
            "INSERT INTO devices (token_hash, user_id, name, created) VALUES (?, ?, ?, ?)",
            (_token_hash(token), user_id, name[:100], time.time()),
        )
        return token

    def user_for_token(self, token: str) -> User | None:
        if not token:
            return None
        digest = _token_hash(token)
        rows = self._query("SELECT user_id FROM devices WHERE token_hash = ?", (digest,))
        if not rows:
            return None
        self._execute("UPDATE devices SET last_used = ? WHERE token_hash = ?", (time.time(), digest))
        return self.user_by_id(rows[0]["user_id"])

    def delete_device(self, token: str) -> None:
        self._execute("DELETE FROM devices WHERE token_hash = ?", (_token_hash(token),))

    # --- usage --------------------------------------------------------------

    def record_usage(self, user_id: int, model: str, usage: dict[str, Any], cost: int) -> None:
        self._execute(
            "INSERT INTO usage (user_id, ts, model, input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, cost_micros) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, time.time(), model, usage.get("input_tokens") or 0, usage.get("output_tokens") or 0,
             usage.get("cache_read_input_tokens") or 0, usage.get("cache_creation_input_tokens") or 0, cost),
        )

    def used_micros(self, user_id: int, since: float) -> int:
        rows = self._query("SELECT COALESCE(SUM(cost_micros), 0) AS total FROM usage WHERE user_id = ? AND ts >= ?",
                           (user_id, since))
        return int(rows[0]["total"])

    # --- Stripe webhook idempotency ----------------------------------------

    def first_time_event(self, event_id: str) -> bool:
        cursor = self._execute("INSERT OR IGNORE INTO stripe_events (id, received) VALUES (?, ?)", (event_id, time.time()))
        return cursor.rowcount == 1
