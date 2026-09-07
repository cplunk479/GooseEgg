"""
tests/test_resilience.py
========================
The failures that only show up on Postgres, reproduced without one.

    python tests/test_resilience.py

WHY THIS FILE EXISTS
--------------------
v0.5.0 deployed green and Goose Watch returned a 500. Every local suite passed,
and structurally could not have caught it, for one reason:

    SQLite has no aborted-transaction state. Postgres does.

On Postgres, a statement that errors inside a transaction poisons the whole
connection -- every later statement returns "current transaction is aborted,
commands ignored until end of transaction block" until someone rolls back.
SQLite just carries on. So a route that CATCHES a database error and renders a
polite "something went wrong" page works perfectly in the test suite and 500s
in production, because the template render then runs `inject_globals`, which
queries the same dead connection, and by then the route has already returned
and can no longer catch anything.

That is exactly what happened: the v0.5.0 schema migration had not been run on
the live database, Goose Watch's SELECT hit a column that did not exist, the
route handled it correctly, and the page died anyway while rendering the
handled version of itself.

So `AbortingDB` below models Postgres's transaction semantics on top of SQLite.
It is not a database test. It is a test of what the app does when the database
says no, and it fails without the `db.rollback()` calls in app.py.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_demo as td  # noqa: E402  (also installs the psycopg2/bcrypt stubs)

import db_init  # noqa: E402
import week_engine as engine  # noqa: E402

failures: list[str] = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


class AbortedTransaction(Exception):
    """psycopg2.errors.InFailedSqlTransaction, near enough."""


class AbortingDB(td.FakeDB):
    """
    A FakeDB with Postgres transaction semantics.

    Once any statement raises, every subsequent statement raises
    AbortedTransaction until rollback() or commit() clears the flag -- which is
    what a real connection does and what SQLite does not. Everything else
    behaves exactly as before.
    """

    def __init__(self, path):
        if os.path.exists(path):
            os.remove(path)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = OFF")
        self.aborted = False
        self.rollbacks = 0

    def execute(self, sql, params=()):
        if self.aborted:
            raise AbortedTransaction(
                "current transaction is aborted, commands ignored until "
                "end of transaction block")
        try:
            return td.FakeDB.execute(self, sql, params)
        except Exception:
            self.aborted = True
            raise

    def commit(self):
        self.aborted = False
        return td.FakeDB.commit(self)

    def rollback(self):
        self.aborted = False
        self.rollbacks += 1
        return td.FakeDB.rollback(self)

    def close(self):
        """The app closes per request; this suite keeps one connection."""


def build_app(db):
    os.environ.update(GOOSE_LEAGUE_ID=td.LEAGUE, GOOSE_SEASON=str(td.SEASON),
                      FLASK_SECRET_KEY="test")
    import app as appmod
    appmod.LEAGUE_ID, appmod.SEASON = td.LEAGUE, td.SEASON
    appmod.dbmod = types.SimpleNamespace(open_wrapped=lambda: db)
    appmod.app.config["TESTING"] = False        # 500s must render, not re-raise
    client = appmod.app.test_client()
    with client.session_transaction() as sess:
        sess["roster_id"] = 1
    return client


def build_stale_db(path) -> AbortingDB:
    """A database on the PREVIOUS schema -- TABLES applied, MIGRATIONS not."""
    return td.build_db(AbortingDB(path), migrations=[])


def build_current_db(path) -> AbortingDB:
    """A database with everything this version expects."""
    return td.build_db(AbortingDB(path))


def test_a_stale_schema_degrades_instead_of_500ing():
    """
    The exact production failure: v0.5 code, v0.4 database.

    Every screen must still answer. Goose Watch is allowed to say it cannot load
    the live board -- that is a handled failure and there is a banner for it --
    but it is not allowed to return a 500, and neither is anything else.
    """
    print("v0.5 code against a v0.4 database (the live 500)")
    td.reset_sleeper()
    db = build_stale_db("/tmp/goose_stale.db")
    client = build_app(db)

    for label, path in [("Board", "/"), ("Goose Watch", "/watch"), ("My Geese", "/me"),
                        ("Standings", "/standings"), ("Admin", "/admin?tab=chugs")]:
        db.aborted = False
        resp = client.get(path)
        check(f"{label} answers instead of 500ing", resp.status_code == 200,
              f"HTTP {resp.status_code}")

    db.aborted = False
    body = client.get("/watch").get_data(as_text=True)
    check("Goose Watch says what went wrong", "FEED PROBLEM" in body, body[:200])
    check("...and still renders its chrome", "GOOSE EGG" in body)
    check("the route rolled the dead transaction back", db.rollbacks > 0,
          "no rollback -- inject_globals will die on the next query")

    db.aborted = False
    check("the connection is usable afterwards",
          db.execute("SELECT COUNT(*) AS n FROM owners").fetchone()["n"] == 12)


def test_page_chrome_never_takes_a_page_down():
    """
    inject_globals runs during template rendering, after the route has returned.
    Anything it raises is past the last place that could catch it, so a 500 is
    the only possible outcome. It has to swallow.
    """
    print("\npage chrome cannot 500 a page that had otherwise rendered")
    td.reset_sleeper()
    db = build_stale_db("/tmp/goose_chrome.db")
    client = build_app(db)

    # Fail ONLY the context processor's own query. Matching on the exact SQL
    # matters: a broader "any query touching chugs" stub also breaks the
    # Standings route's own counts, and then a 500 proves nothing about the
    # chrome -- the route really did fail. This isolates the one statement that
    # runs after the route has returned and can no longer be caught.
    BADGE_SQL = "FROM chugs WHERE season = %s AND status = 'owed'"
    original = db.execute

    def only_the_badge_fails(sql, params=()):
        if BADGE_SQL in sql:
            raise AbortedTransaction("boom")
        return original(sql, params)

    db.execute = only_the_badge_fails
    resp = client.get("/standings")
    body = resp.get_data(as_text=True)
    db.execute = original
    check("the page still renders with a broken badge count", resp.status_code == 200,
          f"HTTP {resp.status_code}")
    check("and the page content itself is intact", "STANDINGS" in body.upper())


def test_migrations_stay_additive():
    """
    db_init.MIGRATIONS runs unattended on every deploy now. A DROP or a type
    change in that list would silently destroy a live season's data on the next
    push, so the shape of the list is worth asserting.
    """
    print("\nthe migration list is additive only")
    check("there are migrations to run", len(db_init.MIGRATIONS) > 0)
    for stmt in db_init.MIGRATIONS:
        upper = stmt.upper()
        check(f"additive: {stmt[:52]}...",
              upper.startswith("ALTER TABLE") and "ADD COLUMN IF NOT EXISTS" in upper
              and " DROP " not in upper and " TYPE " not in upper,
              stmt)


def test_a_current_schema_is_healthy():
    """The control: with the migrations applied, nothing degrades."""
    print("\nthe same code against a CURRENT database")
    td.reset_sleeper()
    db = build_current_db("/tmp/goose_current.db")
    engine.open_week(db, td.LEAGUE, td.SEASON, td.WEEK, 1)
    engine.lock_week(db, td.LEAGUE, td.SEASON, td.WEEK)
    db.commit()
    client = build_app(db)

    resp = client.get("/watch")
    body = resp.get_data(as_text=True)
    check("Goose Watch renders", resp.status_code == 200, f"HTTP {resp.status_code}")
    check("and reports no problem", "FEED PROBLEM" not in body)
    check("and nothing had to be rolled back", db.rollbacks == 0, str(db.rollbacks))


def main() -> int:
    # Every test here drives real Flask routes, because the bug this file
    # exists for lives in the seam between a route and its template render.
    # Skip rather than fail where Flask is not installed -- run_tests.sh is
    # meant to be runnable from a checkout without a full environment.
    try:
        import flask  # noqa: F401
    except ImportError:
        print("resilience checks")
        print("  skip  flask is not installed here")
        return 0

    test_a_stale_schema_degrades_instead_of_500ing()
    test_page_chrome_never_takes_a_page_down()
    test_migrations_stay_additive()
    test_a_current_schema_is_healthy()
    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
