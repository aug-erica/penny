"""Tiny DB shim: the ledger runs on SQLite locally (and in tests) and on
Postgres in the cloud, through one code path.

Set ``DATABASE_URL`` (Railway provides it) to use Postgres; otherwise a local
SQLite file is used. Queries throughout the ledger use ``?`` placeholders and
named row access (``row["col"]``) — both of which work on either backend via the
thin wrapper here. The only dialect-specific spots (autoincrement PK, upsert,
INSERT ... RETURNING) are handled explicitly by the ledger using ``is_pg``.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def database_url() -> str | None:
    return (os.environ.get("DATABASE_URL") or "").strip() or None


class Conn:
    """Wraps a sqlite3 or psycopg connection with a uniform, minimal surface."""

    def __init__(self, raw, is_pg: bool):
        self._raw = raw
        self.is_pg = is_pg

    def execute(self, sql: str, params=()):
        """Run a statement. `?` placeholders are translated for Postgres.
        Returns a cursor supporting fetchone()/fetchall()/iteration with rows
        that support named access (row["col"])."""
        if self.is_pg:
            cur = self._raw.cursor()
            cur.execute(sql.replace("?", "%s"), params)
            return cur
        return self._raw.execute(sql, params)

    def insert_returning_id(self, sql: str, params, id_col: str):
        """INSERT and return the new row's id — lastrowid on SQLite, RETURNING
        on Postgres."""
        if self.is_pg:
            cur = self._raw.cursor()
            cur.execute(sql.replace("?", "%s") + f" RETURNING {id_col}", params)
            row = cur.fetchone()
            return row[id_col] if isinstance(row, dict) else row[0]
        return self._raw.execute(sql, params).lastrowid

    def executemany(self, sql: str, seq):
        """Batched insert/update — one round trip instead of N. Used by the
        SQLite→Postgres importer so a cutover isn't thousands of slow round trips
        over the public DB proxy."""
        seq = list(seq)
        if not seq:
            return
        if self.is_pg:
            self._raw.cursor().executemany(sql.replace("?", "%s"), seq)
            return
        self._raw.executemany(sql, seq)

    def executescript(self, script: str):
        if self.is_pg:
            cur = self._raw.cursor()
            for stmt in pg_statements(script):
                cur.execute(stmt)
            return
        self._raw.executescript(script)

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()


def pg_statements(script: str) -> list:
    """Split a DDL script into statements for Postgres (which, unlike SQLite's
    executescript, gets them one at a time). `--` comment lines are stripped
    FIRST: a semicolon inside a comment would otherwise split mid-comment and
    execute the remainder as garbage SQL (this took Penny down on July 6)."""
    sql = "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("--"))
    return [s.strip() for s in sql.split(";") if s.strip()]


def connect(path) -> Conn:
    """Postgres if DATABASE_URL is set (path ignored), else a local SQLite file."""
    url = database_url()
    if url:
        import psycopg
        from psycopg.rows import dict_row
        return Conn(psycopg.connect(url, row_factory=dict_row), True)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    return Conn(raw, False)
