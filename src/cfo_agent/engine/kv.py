"""Tiny key-value store on the same dual backend as the ledger (Postgres when
DATABASE_URL is set, a local SQLite file otherwise). Client-agnostic — adapters
may use it without knowing which client is running.

Holds operational state that must survive container restarts on Railway:
  - "qbo_refresh_token"      QBO refresh tokens ROTATE on every use; a rotated
                             token persisted only to a local .env is lost on an
                             ephemeral filesystem, killing auth (~100 days max).
  - "close_month:<client>"   the active close month, switchable by an admin DM
                             ("start the 2026-07 close") instead of a manual
                             CLOSE_MONTH env bump on two Railway services.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..config import RUNS_LOCAL
from . import db

_DDL = "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, updated_at TEXT)"


def _conn():
    c = db.connect(RUNS_LOCAL / "kv.sqlite3")
    c.execute(_DDL)
    c.commit()
    return c


def get(key: str):
    c = _conn()
    try:
        r = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return r["v"] if r else None
    finally:
        c.close()


def set(key: str, value: str):
    c = _conn()
    try:
        c.execute(
            "INSERT INTO kv (k, v, updated_at) VALUES (?,?,?) "
            "ON CONFLICT (k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (key, value,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        c.commit()
    finally:
        c.close()


def active_month(client: str, fallback: str = None):
    """The close month currently being worked: the DB value (set by an admin DM
    or directly) wins; fall back to the caller's CLOSE_MONTH env / --month arg."""
    return get(f"close_month:{client}") or fallback
