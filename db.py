"""
db.py
=====
Shared Postgres connection helper. Same shape as faab-platform/db.py on
purpose -- every call site in this app uses the sqlite-style
`db.execute(sql, params).fetchone()` chain, so the two codebases stay
readable side by side.

NUMERIC columns are cast to float at the driver layer so templates can format
them directly instead of tripping over Decimal.
"""
from __future__ import annotations

import os

import psycopg2
import psycopg2.extensions
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

_DEC2FLOAT = psycopg2.extensions.new_type(
    psycopg2.extensions.DECIMAL.values,
    "DEC2FLOAT",
    lambda v, c: float(v) if v is not None else None,
)
psycopg2.extensions.register_type(_DEC2FLOAT)


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL not set. Example: "
            "postgresql://goose:goose@localhost:5432/goose"
        )
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def connect():
    return psycopg2.connect(
        get_database_url(),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


class DBWrapper:
    """sqlite3.Connection-shaped wrapper: .execute(...).fetchone()/.fetchall()."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params: tuple | list = ()):
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    @property
    def raw(self):
        return self._conn


def open_wrapped() -> DBWrapper:
    return DBWrapper(connect())
