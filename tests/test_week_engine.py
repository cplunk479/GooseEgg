"""
tests/test_week_engine.py
=========================
Drives a whole season week through the REAL week_engine functions:
open -> cast curses -> lock -> settle -> confirm chugs, and checks that
gooses, curses, blessings, tokens and the weekly chug cap all land correctly.

    python tests/test_week_engine.py

WHAT THIS DOES NOT PROVE
------------------------
It runs against SQLite, not Postgres. The schema is derived mechanically from
db_init.TABLES at runtime (SERIAL -> AUTOINCREMENT, and so on) so it cannot
drift from what actually ships, and every SQL statement executed here is the
one in week_engine.py -- but SQLite is a more forgiving engine and this is not
a substitute for running against a real database.

The FAAB app's own lesson applies directly: a mocked test proves the logic and
never the environment. Before deploying, run db_init.py and one real week
against a throwaway Postgres. That step is still outstanding.

Sleeper is stubbed. No network is touched.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# db_init imports db.py, which imports psycopg2 at module load. psycopg2 is a
# compiled driver and is not installed on every machine that should be able to
# run these tests -- so stub the three names db.py touches. Nothing here talks
# to Postgres; the point is to reach db_init.TABLES, which is the schema this
# test builds from so it can never drift from what ships.
import types  # noqa: E402
if "psycopg2" not in sys.modules:
    _pg = types.ModuleType("psycopg2")
    _ext = types.ModuleType("psycopg2.extensions")
    _ext.DECIMAL = types.SimpleNamespace(values=[])
    _ext.new_type = lambda *a, **k: None
    _ext.register_type = lambda *a, **k: None
    _extras = types.ModuleType("psycopg2.extras")
    _extras.RealDictCursor = object
    _pg.extensions, _pg.extras = _ext, _extras
    _pg.connect = lambda *a, **k: None
    sys.modules.update({
        "psycopg2": _pg, "psycopg2.extensions": _ext, "psycopg2.extras": _extras,
    })

import db_init  # noqa: E402
import goose  # noqa: E402
import week_engine as engine  # noqa: E402

LEAGUE = "TEST_LEAGUE"
SEASON = 2026
SLOTS = ["QB", "RB", "WR", "WR", "WR", "TE", "FLEX", "FLEX", "FLEX", "FLEX", "SUPER_FLEX"]

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


# ---------------------------------------------------------------- the shim

def to_sqlite(sql: str) -> str:
    sql = sql.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    sql = re.sub(r"NUMERIC\(\d+,\s*\d+\)", "REAL", sql)
    sql = sql.replace("BIGINT", "INTEGER").replace("BOOLEAN", "INTEGER")
    sql = sql.replace("DEFAULT FALSE", "DEFAULT 0").replace("DEFAULT TRUE", "DEFAULT 1")
    return sql


class Cur:
    def __init__(self, cur):
        self._c = cur
        self.rowcount = cur.rowcount

    def fetchone(self):
        r = self._c.fetchone()
        return dict(r) if r is not None else None

    def fetchall(self):
        return [dict(r) for r in self._c.fetchall()]


class FakeDB:
    """Same surface as db.DBWrapper, translating %s placeholders to ?."""

    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = OFF")

    def execute(self, sql, params=()):
        return Cur(self._conn.execute(sql.replace("%s", "?"), tuple(params)))

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


class FakeSleeper:
    """Deterministic stand-in. `points` is set per test."""

    def __init__(self):
        self.starters = {}
        self.points = {}
        self.totals = {}
        # Every starter projects this many points. Settable so a test can move
        # Sleeper's projections mid-week, which is the whole point of freezing.
        self.projection_points = 0.0

    def starting_slots(self, league_id):
        return SLOTS

    def scoring_settings(self, league_id):
        return {"rec": 1.0}

    def projections(self, season, week):
        return {}

    def bye_teams(self, season, week):
        return set()

    def player_points(self, stats, scoring):
        return self.projection_points

    def week_lock_epoch(self, season, week):
        return 1_700_000_000

    def week_end_epoch(self, season, week):
        return 1_700_200_000

    def matchups(self, league_id, week):
        out = []
        for rid, starters in self.starters.items():
            out.append({
                "roster_id": rid,
                "starters": starters,
                "starters_points": self.points.get(rid, []),
                "points": self.totals.get(rid),
            })
        return out


def build_db(fake_sleeper) -> FakeDB:
    db = FakeDB()
    for stmt in db_init.TABLES:
        db.execute(to_sqlite(stmt))
    # The migration list has to run here too, or this test builds a v0.4 schema
    # and every v0.5 statement fails on a column that ships in production.
    # SQLite has no ADD COLUMN IF NOT EXISTS, so the guard is a try/except on
    # the duplicate -- which is what the Postgres clause does anyway.
    for stmt in db_init.MIGRATIONS:
        try:
            db.execute(to_sqlite(stmt.replace(" IF NOT EXISTS", "")))
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc):
                raise
    for stmt in db_init.INDEXES:
        db.execute(stmt)
    for key, value in __import__("settings").DEFAULTS.items():
        db.execute("INSERT INTO app_meta (key, value) VALUES (%s, %s)", (key, value))
    for rid in range(1, 5):
        db.execute(
            "INSERT INTO owners (league_id, season, roster_id, owner_name, team_name, is_admin, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (LEAGUE, SEASON, rid, f"owner{rid}", f"Team {rid}", rid == 1, 0),
        )
    db.commit()
    engine.sleeper = fake_sleeper
    return db


def lineup(*points):
    """11 starters; pass the points, '-' means an empty slot."""
    starters, pts = [], []
    for i, p in enumerate(points):
        if p == "-":
            starters.append("0")
            pts.append(0.0)
        else:
            starters.append(f"p{i}")
            pts.append(p)
    return starters, pts


# ------------------------------------------------------------------- tests

def test_full_week() -> None:
    print("a full week: open -> curse -> lock -> settle")
    fs = FakeSleeper()
    db = build_db(fs)

    for rid in range(1, 5):
        s, p = lineup(*([12.0] * 11))
        fs.starters[rid] = s
        fs.points[rid] = p
        fs.totals[rid] = sum(p)

    r = engine.open_week(db, LEAGUE, SEASON, 1, admin_roster_id=1)
    check("week opens", r["ok"])
    check("no stipend by default", r["stipend_tokens"] == 0)

    # roster 1 has a token to spend
    engine.mint_token(db, SEASON, 1, 1, "admin", "test")
    db.commit()
    r = engine.cast_curse(db, SEASON, 1, caster=1, target=2)
    check("curse casts", r["ok"], r.get("reason", ""))
    check("cannot curse yourself", not engine.cast_curse(db, SEASON, 1, 1, 1)["ok"])
    check("cannot cast without a token", not engine.cast_curse(db, SEASON, 1, 3, 4)["ok"])

    # roster 2 will fall short of its own projection; roster 3 lays two gooses
    s, p = lineup(0.0, -2.0, *([9.0] * 9))
    fs.starters[3], fs.points[3] = s, p
    fs.totals[3] = sum(p)

    r = engine.lock_week(db, LEAGUE, SEASON, 1)
    check("week locks", r["ok"], r.get("reason", ""))
    check("all four lineups snapshotted", r["lineups"] == 4, str(r))
    check("the curse was frozen against a number", r["curses_frozen"] == 1, str(r))

    frozen = db.execute("SELECT threshold_proj FROM curses WHERE id = 1").fetchone()["threshold_proj"]
    check("threshold is stored, not looked up live", frozen is not None)

    # roster 2 underperforms its snapshot -> the curse lands
    fs.totals[2] = (frozen or 0) - 10

    r = engine.settle_week(db, LEAGUE, SEASON, 1)
    check("week settles", r["ok"], r.get("reason", ""))
    check("two gooses found on roster 3", r["gooses"] == 2, str(r["gooses"]))
    check("the curse landed", r["curses_landed"] == 1, str(r))

    owed3 = db.execute(
        "SELECT COUNT(*) AS n FROM chugs WHERE roster_id = 3 AND status = 'owed'"
    ).fetchone()["n"]
    check("roster 3 owes two chugs", owed3 == 2, str(owed3))
    owed2 = db.execute(
        "SELECT COUNT(*) AS n FROM chugs WHERE roster_id = 2 AND reason = 'curse'"
    ).fetchone()["n"]
    check("the cursed owner owes a chug", owed2 == 1, str(owed2))

    minted = db.execute(
        "SELECT COUNT(*) AS n FROM curse_tokens WHERE roster_id = 1 AND source = 'curse_landed'"
    ).fetchone()["n"]
    check("landing a curse mints a token for the caster", minted == 1, str(minted))

    # owing is not drinking: no token until an admin confirms
    before = len(engine.unspent_tokens(db, SEASON, 3))
    chug = db.execute("SELECT id FROM chugs WHERE roster_id = 3 LIMIT 1").fetchone()["id"]
    engine.confirm_chug(db, SEASON, chug, admin_roster_id=1)
    after = len(engine.unspent_tokens(db, SEASON, 3))
    check("confirming a chug mints exactly one token", after == before + 1, f"{before} -> {after}")

    engine.unconfirm_chug(db, SEASON, chug)
    check("undoing a confirmation claws the token back",
          len(engine.unspent_tokens(db, SEASON, 3)) == before)
    db.close()


def test_blessing_blocks() -> None:
    print("\nsurviving a curse earns a blessing, and it blocks exactly once")
    fs = FakeSleeper()
    db = build_db(fs)
    for rid in range(1, 5):
        s, p = lineup(*([10.0] * 11))
        fs.starters[rid], fs.points[rid] = s, p
        fs.totals[rid] = sum(p)

    engine.open_week(db, LEAGUE, SEASON, 1, 1)
    engine.mint_token(db, SEASON, 1, 1, "admin", "t")
    db.commit()
    engine.cast_curse(db, SEASON, 1, 1, 2)
    engine.lock_week(db, LEAGUE, SEASON, 1)

    frozen = db.execute("SELECT threshold_proj FROM curses WHERE id = 1").fetchone()["threshold_proj"]
    fs.totals[2] = (frozen or 0) + 25          # beats it
    r = engine.settle_week(db, LEAGUE, SEASON, 1)
    check("beating the number survives the curse", r["curses_survived"] == 1, str(r))

    b = db.execute("SELECT * FROM blessings WHERE roster_id = 2").fetchone()
    check("a blessing is created", b is not None)
    check("it covers next week only", b and b["earned_week"] == 1 and b["expires_after"] == 2)

    # week 2: cursed again, the blessing should fire by itself
    engine.open_week(db, LEAGUE, SEASON, 2, 1)
    engine.mint_token(db, SEASON, 1, 2, "admin", "t")
    db.commit()
    engine.cast_curse(db, SEASON, 2, 1, 2)
    engine.lock_week(db, LEAGUE, SEASON, 2)
    frozen2 = db.execute("SELECT threshold_proj FROM curses WHERE week = 2").fetchone()["threshold_proj"]
    fs.totals[2] = (frozen2 or 0) - 40         # would have lost badly
    r = engine.settle_week(db, LEAGUE, SEASON, 2)
    check("the blessing blocks the curse automatically", r["curses_blocked"] == 1, str(r))
    check("a blocked curse raises no chug",
          db.execute("SELECT COUNT(*) AS n FROM chugs WHERE roster_id = 2 AND reason = 'curse'"
                     ).fetchone()["n"] == 0)
    check("the blessing is spent, not still sitting there",
          db.execute("SELECT status FROM blessings WHERE id = %s", (b["id"],)
                     ).fetchone()["status"] == "consumed")

    # week 3: no blessing left, so the same shortfall must land
    engine.open_week(db, LEAGUE, SEASON, 3, 1)
    engine.mint_token(db, SEASON, 1, 3, "admin", "t")
    db.commit()
    engine.cast_curse(db, SEASON, 3, 1, 2)
    engine.lock_week(db, LEAGUE, SEASON, 3)
    f3 = db.execute("SELECT threshold_proj FROM curses WHERE week = 3").fetchone()["threshold_proj"]
    fs.totals[2] = (f3 or 0) - 40
    r = engine.settle_week(db, LEAGUE, SEASON, 3)
    check("with no blessing left the curse lands", r["curses_landed"] == 1, str(r))
    db.close()


def test_blessing_expires() -> None:
    print("\nblessings do not bank")
    fs = FakeSleeper()
    db = build_db(fs)
    for rid in range(1, 5):
        s, p = lineup(*([10.0] * 11))
        fs.starters[rid], fs.points[rid] = s, p
        fs.totals[rid] = sum(p)
    db.execute(
        "INSERT INTO blessings (season, roster_id, earned_week, expires_after, status, created_at) "
        "VALUES (%s, %s, %s, %s, 'active', 0)", (SEASON, 2, 1, 2),
    )
    db.commit()
    for wk in (2, 3):
        engine.open_week(db, LEAGUE, SEASON, wk, 1)
        engine.lock_week(db, LEAGUE, SEASON, wk)
        engine.settle_week(db, LEAGUE, SEASON, wk)
    status = db.execute("SELECT status FROM blessings WHERE roster_id = 2").fetchone()["status"]
    check("an unused blessing expires rather than carrying on", status == "expired", status)
    db.close()


def test_chug_cap_rolls() -> None:
    print("\nthe weekly chug cap rolls the overflow forward instead of forgiving it")
    fs = FakeSleeper()
    db = build_db(fs)
    for rid in range(1, 5):
        s, p = lineup(*([10.0] * 11))
        fs.starters[rid], fs.points[rid] = s, p
        fs.totals[rid] = sum(p)
    # roster 4 lays five gooses against a cap of three
    s, p = lineup(0.0, 0.0, 0.0, 0.0, 0.0, *([10.0] * 6))
    fs.starters[4], fs.points[4] = s, p
    fs.totals[4] = sum(p)

    engine.open_week(db, LEAGUE, SEASON, 1, 1)
    engine.lock_week(db, LEAGUE, SEASON, 1)
    r = engine.settle_week(db, LEAGUE, SEASON, 1)
    check("all five gooses are recorded", r["gooses"] == 5, str(r["gooses"]))
    check("two chugs rolled forward", r["chugs_rolled"] == 2, str(r["chugs_rolled"]))

    this_week = db.execute(
        "SELECT COUNT(*) AS n FROM chugs WHERE roster_id = 4 AND week = 1"
    ).fetchone()["n"]
    next_week = db.execute(
        "SELECT COUNT(*) AS n FROM chugs WHERE roster_id = 4 AND week = 2"
    ).fetchone()["n"]
    check("three land this week", this_week == 3, str(this_week))
    check("two land next week", next_week == 2, str(next_week))
    check("nothing was forgiven", this_week + next_week == 5)
    check("the rollover keeps its origin",
          db.execute("SELECT rolled_from_week FROM chugs WHERE roster_id = 4 AND week = 2 LIMIT 1"
                     ).fetchone()["rolled_from_week"] == 1)
    db.close()


def test_empty_slot_gooses() -> None:
    print("\nan empty starting slot is a goose")
    fs = FakeSleeper()
    db = build_db(fs)
    for rid in range(1, 5):
        s, p = lineup(*([10.0] * 11))
        fs.starters[rid], fs.points[rid] = s, p
        fs.totals[rid] = sum(p)
    s, p = lineup("-", *([10.0] * 10))
    fs.starters[2], fs.points[2] = s, p
    fs.totals[2] = sum(p)

    engine.open_week(db, LEAGUE, SEASON, 1, 1)
    engine.lock_week(db, LEAGUE, SEASON, 1)
    r = engine.settle_week(db, LEAGUE, SEASON, 1)
    check("the empty slot gooses", r["gooses"] == 1, str(r["gooses"]))
    g = db.execute("SELECT * FROM gooses WHERE roster_id = 2").fetchone()
    check("it is flagged as an empty slot, not a bad player", bool(g["empty_slot"]))
    slot = db.execute(
        "SELECT tier, risk_reason FROM lineup_slots "
        "WHERE roster_id = 2 AND slot_index = 0").fetchone()
    check("an empty slot is COOKED, and the snapshot says why",
          slot["tier"] == goose.COOKED and slot["risk_reason"] == "EMPTY SLOT",
          str(dict(slot)))
    db.close()


def test_settle_is_idempotent() -> None:
    print("\nsettling twice does not double-charge anyone")
    fs = FakeSleeper()
    db = build_db(fs)
    for rid in range(1, 5):
        s, p = lineup(*([10.0] * 11))
        fs.starters[rid], fs.points[rid] = s, p
        fs.totals[rid] = sum(p)
    s, p = lineup(0.0, *([10.0] * 10))
    fs.starters[3], fs.points[3] = s, p
    fs.totals[3] = sum(p)

    engine.open_week(db, LEAGUE, SEASON, 1, 1)
    engine.lock_week(db, LEAGUE, SEASON, 1)
    engine.settle_week(db, LEAGUE, SEASON, 1)
    first = db.execute("SELECT COUNT(*) AS n FROM chugs").fetchone()["n"]
    engine.settle_week(db, LEAGUE, SEASON, 1, force=True)
    second = db.execute("SELECT COUNT(*) AS n FROM chugs").fetchone()["n"]
    check("a re-settle raises no second chug", first == second, f"{first} -> {second}")
    db.close()


def test_curse_needs_open_week() -> None:
    print("\ncurses cannot be cast into a locked week")
    fs = FakeSleeper()
    db = build_db(fs)
    for rid in range(1, 5):
        s, p = lineup(*([10.0] * 11))
        fs.starters[rid], fs.points[rid] = s, p
        fs.totals[rid] = sum(p)
    engine.mint_token(db, SEASON, 1, 1, "admin", "t")
    db.commit()
    check("no curse before the week is opened", not engine.cast_curse(db, SEASON, 1, 1, 2)["ok"])
    engine.open_week(db, LEAGUE, SEASON, 1, 1)
    check("a curse casts once open", engine.cast_curse(db, SEASON, 1, 1, 2)["ok"])
    engine.lock_week(db, LEAGUE, SEASON, 1)
    engine.mint_token(db, SEASON, 1, 1, "admin", "t")
    db.commit()
    check("no curse after lock", not engine.cast_curse(db, SEASON, 1, 1, 3)["ok"])
    db.close()


def test_unlock_holds_the_frozen_threshold() -> None:
    """
    The integrity property behind the admin unlock, and the one worth a test:
    reopening a week must not hand anybody a fresher projection.

    Two curses -- one cast before the lock, one cast during the unlocked window
    -- must be graded against the SAME number, even after the underlying
    projection has moved and the week is locked a second time.
    """
    print("\nunlocking a week does not move the bar")
    fs = FakeSleeper()
    db = build_db(fs)
    for rid in range(1, 5):
        st, pts = lineup(*([10.0] * 11))
        fs.starters[rid], fs.points[rid] = st, pts
        fs.totals[rid] = sum(pts)

    fs.projection_points = 9.0            # every starter projects 9.0
    engine.open_week(db, LEAGUE, SEASON, 1, 1)
    engine.mint_token(db, SEASON, 1, 1, "admin")
    engine.mint_token(db, SEASON, 2, 1, "admin")
    db.commit()

    early = engine.cast_curse(db, SEASON, 1, 1, 4)
    check("the early curse casts", early["ok"], str(early))
    check("it carries no threshold before the lock", early.get("threshold_proj") is None)

    engine.lock_week(db, LEAGUE, SEASON, 1)
    frozen = db.execute(
        "SELECT threshold_proj FROM curses WHERE id = %s", (early["curse_id"],)
    ).fetchone()["threshold_proj"]
    check("locking freezes the early curse", frozen is not None, str(frozen))

    r = engine.unlock_week(db, SEASON, 1, 1)
    check("unlock succeeds on a locked week", r["ok"], str(r))
    check("it reports the thresholds it is holding", r["thresholds_held"] == 1, str(r))
    check("the week is open again",
          engine.get_week(db, SEASON, 1)["status"] == "open")
    check("the early threshold survived the unlock",
          db.execute("SELECT threshold_proj FROM curses WHERE id = %s",
                     (early["curse_id"],)).fetchone()["threshold_proj"] == frozen)

    # Sleeper's projections move, as they do all week. A curse cast now, on a
    # different target (one curse per target is the default rule), must still
    # be graded against that target's KICKOFF projection.
    frozen_3 = db.execute(
        "SELECT proj_total FROM team_weeks WHERE season = %s AND week = 1 AND roster_id = 3",
        (SEASON,)).fetchone()["proj_total"]
    fs.projection_points = 4.0
    late = engine.cast_curse(db, SEASON, 1, 2, 3)
    check("a curse casts during the unlocked window", late["ok"], str(late))
    check("the late curse inherits the KICKOFF number, not the new one",
          late.get("threshold_proj") == frozen_3,
          f"late {late.get('threshold_proj')} vs frozen {frozen_3}")
    check("and that number is the pre-move projection", frozen_3 == frozen, str(frozen_3))

    engine.lock_week(db, LEAGUE, SEASON, 1, force=True)
    after = {r["id"]: r["threshold_proj"] for r in db.execute(
        "SELECT id, threshold_proj FROM curses WHERE season = %s AND week = 1", (SEASON,)
    ).fetchall()}
    check("re-locking moves nobody's threshold",
          all(v == frozen for v in after.values()), str(after))
    check("both curses are graded against the same bar", len(set(after.values())) == 1, str(after))

    check("unlock refuses on a settled week",
          not engine.unlock_week(db, SEASON, 2, 1)["ok"] and
          not (engine.settle_week(db, LEAGUE, SEASON, 1) and
               engine.unlock_week(db, SEASON, 1, 1))["ok"])
    db.close()


def test_reset_week_unwinds_everything() -> None:
    """
    A reset has to leave nothing behind. The failure this guards against is a
    half-unwind -- a token still sitting in someone's balance minted by a chug
    that no longer exists -- which would quietly inflate the curse economy
    every time the commissioner re-tested a week.
    """
    print("\nresetting a week leaves nothing behind")
    fs = FakeSleeper()
    db = build_db(fs)
    for rid in range(1, 5):
        st, pts = lineup(0.0, 0.0, *([10.0] * 9))
        fs.starters[rid], fs.points[rid] = st, pts
        fs.totals[rid] = sum(pts)

    engine.open_week(db, LEAGUE, SEASON, 1, 1)
    engine.mint_token(db, SEASON, 1, 1, "admin")
    db.commit()
    engine.cast_curse(db, SEASON, 1, 1, 3)
    engine.lock_week(db, LEAGUE, SEASON, 1)
    engine.settle_week(db, LEAGUE, SEASON, 1)

    owed = db.execute("SELECT id FROM chugs WHERE season = %s", (SEASON,)).fetchall()
    check("the week produced chugs to unwind", len(owed) > 0, str(len(owed)))
    engine.confirm_chug(db, SEASON, owed[0]["id"], 1)
    minted = db.execute(
        "SELECT COUNT(*) AS n FROM curse_tokens WHERE source = 'chug'").fetchone()["n"]
    check("confirming minted a token", minted == 1, str(minted))

    r = engine.reset_week(db, SEASON, 1)
    check("reset reports what it removed", r["ok"] and r["gooses"] > 0, str(r))
    for table in ("gooses", "chugs", "lineup_slots", "team_weeks"):
        n = db.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE season = %s",
                       (SEASON,)).fetchone()["n"]
        check(f"{table} is empty for the week", n == 0, str(n))
    check("the unspent minted token is gone",
          db.execute("SELECT COUNT(*) AS n FROM curse_tokens WHERE source = 'chug'"
                     ).fetchone()["n"] == 0)
    curse = db.execute("SELECT * FROM curses WHERE season = %s", (SEASON,)).fetchone()
    check("the curse is rewound to cast, not deleted",
          curse and curse["status"] == "cast" and curse["threshold_proj"] is None,
          str(dict(curse) if curse else None))
    check("its token stays spent -- the economy spans weeks",
          curse["token_id"] is not None and db.execute(
              "SELECT spent_on FROM curse_tokens WHERE id = %s", (curse["token_id"],)
          ).fetchone()["spent_on"] is not None)
    check("the week is back to upcoming",
          engine.get_week(db, SEASON, 1)["status"] == "upcoming")
    check("owners survive a reset",
          db.execute("SELECT COUNT(*) AS n FROM owners").fetchone()["n"] == 4)
    db.close()


def _curse_and_settle(db, fs, week, caster, target, miss: bool):
    """Open, curse, lock, set the target's score either side of the bar, settle."""
    engine.open_week(db, LEAGUE, SEASON, week, 1)
    engine.mint_token(db, SEASON, caster, week, "admin", "t")
    db.commit()
    engine.cast_curse(db, SEASON, week, caster, target)
    engine.lock_week(db, LEAGUE, SEASON, week)
    frozen = db.execute(
        "SELECT threshold_proj FROM curses WHERE week = %s AND target_roster_id = %s "
        "ORDER BY id DESC LIMIT 1", (week, target),
    ).fetchone()["threshold_proj"]
    fs.totals[target] = (frozen or 0) + (-40 if miss else 25)
    return engine.settle_week(db, LEAGUE, SEASON, week)


def _base_db():
    fs = FakeSleeper()
    db = build_db(fs)
    for rid in range(1, 5):
        s, p = lineup(*([10.0] * 11))
        fs.starters[rid], fs.points[rid] = s, p
        fs.totals[rid] = sum(p)
    return fs, db


def test_wrath_arms_and_doubles() -> None:
    print("\nGoosifer's Wrath: a miss marks you, the next miss costs double")
    fs, db = _base_db()

    r = _curse_and_settle(db, fs, 1, caster=1, target=2, miss=True)
    check("the curse lands", r["curses_landed"] == 1, str(r))
    check("landing arms a mark", r["wraths_armed"] == 1, str(r))
    check("nothing is cashed on the week that armed it", r["wraths_cashed"] == 0, str(r))
    check("week 1 costs exactly one chug",
          db.execute("SELECT COUNT(*) AS n FROM chugs WHERE roster_id = 2 AND reason = 'curse'"
                     ).fetchone()["n"] == 1)
    check("that chug still mints",
          bool(db.execute("SELECT mints_tokens FROM chugs WHERE roster_id = 2 LIMIT 1"
                          ).fetchone()["mints_tokens"]))

    w = db.execute("SELECT * FROM wraths WHERE roster_id = 2").fetchone()
    check("the mark covers next week only", w["earned_week"] == 1 and w["expires_after"] == 2)
    check("it does not read on the week it was armed",
          engine.active_wrath(db, SEASON, 2, 1) is None)
    check("it does read on the following week",
          engine.active_wrath(db, SEASON, 2, 2) is not None)

    # week 2: cursed again, misses again -> doubled and worthless
    r = _curse_and_settle(db, fs, 2, caster=1, target=2, miss=True)
    check("the mark is cashed", r["wraths_cashed"] == 1, str(r))
    check("a fresh mark is armed by the new miss", r["wraths_armed"] == 1, str(r))

    wk2 = db.execute(
        "SELECT * FROM chugs WHERE roster_id = 2 AND week = 2 AND reason = 'curse'"
    ).fetchall()
    check("week 2 costs two chugs", len(wk2) == 2, str(len(wk2)))
    check("neither of them mints", not any(c["mints_tokens"] for c in wk2))
    check("both point at the mark that doubled them",
          all(c["wrath_id"] == w["id"] for c in wk2), str([c["wrath_id"] for c in wk2]))

    before = len(engine.unspent_tokens(db, SEASON, 2))
    for c in wk2:
        out = engine.confirm_chug(db, SEASON, c["id"], admin_roster_id=1)
        check("confirming a wrath chug mints nothing", out["tokens_minted"] == 0, str(out))
    check("drinking twice under the wrath earns nothing at all",
          len(engine.unspent_tokens(db, SEASON, 2)) == before)

    check("the old mark is spent, not still active",
          db.execute("SELECT status FROM wraths WHERE id = %s", (w["id"],)
                     ).fetchone()["status"] == "consumed")
    check("it never stacks past double",
          db.execute("SELECT COUNT(*) AS n FROM wraths WHERE roster_id = 2 AND status = 'active'"
                     ).fetchone()["n"] == 1)
    db.close()


def test_wrath_expires_and_is_lifted() -> None:
    print("\nthe mark ages out on its own, and surviving burns it off early")
    fs, db = _base_db()
    _curse_and_settle(db, fs, 1, caster=1, target=2, miss=True)

    # Week 2 passes with nobody cursing roster 2 at all.
    engine.open_week(db, LEAGUE, SEASON, 2, 1)
    engine.lock_week(db, LEAGUE, SEASON, 2)
    engine.settle_week(db, LEAGUE, SEASON, 2)
    check("an uncashed mark survives its own week",
          db.execute("SELECT status FROM wraths WHERE roster_id = 2").fetchone()["status"] == "active")

    engine.open_week(db, LEAGUE, SEASON, 3, 1)
    engine.lock_week(db, LEAGUE, SEASON, 3)
    r = engine.settle_week(db, LEAGUE, SEASON, 3)
    check("and is gone the week after", r["wraths_expired"] == 1, str(r))
    check("nothing is left to cash", engine.active_wrath(db, SEASON, 2, 4) is None)

    # Now mark roster 3 and have them SURVIVE the next curse.
    _curse_and_settle(db, fs, 4, caster=1, target=3, miss=True)
    check("roster 3 is marked", engine.active_wrath(db, SEASON, 3, 5) is not None)
    r = _curse_and_settle(db, fs, 5, caster=1, target=3, miss=False)
    check("surviving lifts the mark", r["wraths_lifted"] == 1, str(r))
    check("nothing is carried into week 6", engine.active_wrath(db, SEASON, 3, 6) is None)
    check("surviving mints a token for the survivor", r["survivor_tokens"] == 1, str(r))
    check("the survivor can actually spend it",
          any(t["source"] == "curse_survived" for t in engine.unspent_tokens(db, SEASON, 3)))
    check("and still gets the blessing",
          db.execute("SELECT COUNT(*) AS n FROM blessings WHERE roster_id = 3 AND status = 'active'"
                     ).fetchone()["n"] == 1)
    db.close()


def test_wrath_persists_setting() -> None:
    print("\nwrath_persists: the mark waits to be survived instead of ageing out")
    fs, db = _base_db()
    db.execute("UPDATE app_meta SET value = '1' WHERE key = 'wrath_persists'")
    db.commit()

    _curse_and_settle(db, fs, 1, caster=1, target=2, miss=True)
    w = db.execute("SELECT * FROM wraths WHERE roster_id = 2").fetchone()
    check("a persistent mark has no expiry at all", w["expires_after"] is None)

    for week in (2, 3, 4):
        engine.open_week(db, LEAGUE, SEASON, week, 1)
        engine.lock_week(db, LEAGUE, SEASON, week)
        engine.settle_week(db, LEAGUE, SEASON, week)
    check("three quiet weeks do not shift it",
          engine.active_wrath(db, SEASON, 2, 5) is not None)

    r = _curse_and_settle(db, fs, 5, caster=1, target=2, miss=False)
    check("only surviving ends it", r["wraths_lifted"] == 1, str(r))
    check("and then it is really gone", engine.active_wrath(db, SEASON, 2, 6) is None)
    db.close()


def test_wrath_can_be_switched_off() -> None:
    print("\nwrath_enabled off returns the game to v0.7 exactly")
    fs, db = _base_db()
    db.execute("UPDATE app_meta SET value = '0' WHERE key = 'wrath_enabled'")
    db.commit()

    r = _curse_and_settle(db, fs, 1, caster=1, target=2, miss=True)
    check("no mark is armed", r["wraths_armed"] == 0, str(r))
    check("no mark exists",
          db.execute("SELECT COUNT(*) AS n FROM wraths").fetchone()["n"] == 0)

    # Hand-plant one anyway; with the rule off it must not be read.
    db.execute(
        "INSERT INTO wraths (season, roster_id, earned_week, expires_after, status, created_at) "
        "VALUES (%s, %s, %s, %s, 'active', 0)", (SEASON, 2, 1, 2),
    )
    db.commit()
    r = _curse_and_settle(db, fs, 2, caster=1, target=2, miss=True)
    check("a planted mark is ignored while the rule is off", r["wraths_cashed"] == 0, str(r))
    check("the chug is single and still mints",
          db.execute("SELECT COUNT(*) AS n FROM chugs WHERE roster_id = 2 AND week = 2 "
                     "AND reason = 'curse' AND mints_tokens").fetchone()["n"] == 1)
    db.close()


def test_reset_week_unwinds_wrath() -> None:
    print("\nresetting a week puts the mark back exactly as it was")
    fs, db = _base_db()
    _curse_and_settle(db, fs, 1, caster=1, target=2, miss=True)
    w1 = db.execute("SELECT * FROM wraths WHERE roster_id = 2").fetchone()
    _curse_and_settle(db, fs, 2, caster=1, target=2, miss=True)
    check("week 2 armed a second mark",
          db.execute("SELECT COUNT(*) AS n FROM wraths WHERE roster_id = 2").fetchone()["n"] == 2)

    r = engine.reset_week(db, SEASON, 2)
    check("the mark week 2 armed is deleted", r["wraths"] == 1, str(r))
    check("the mark week 2 CASHED is active again",
          db.execute("SELECT status FROM wraths WHERE id = %s", (w1["id"],)
                     ).fetchone()["status"] == "active")
    check("the doubled chugs are gone",
          db.execute("SELECT COUNT(*) AS n FROM chugs WHERE week = 2").fetchone()["n"] == 0)
    check("week 1's mark is untouched by a week 2 reset",
          engine.active_wrath(db, SEASON, 2, 2) is not None)
    db.close()


def main() -> int:
    test_full_week()
    test_blessing_blocks()
    test_blessing_expires()
    test_chug_cap_rolls()
    test_empty_slot_gooses()
    test_settle_is_idempotent()
    test_curse_needs_open_week()
    test_unlock_holds_the_frozen_threshold()
    test_reset_week_unwinds_everything()
    test_wrath_arms_and_doubles()
    test_wrath_expires_and_is_lifted()
    test_wrath_persists_setting()
    test_wrath_can_be_switched_off()
    test_reset_week_unwinds_wrath()
    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    print("\nNOTE: SQLite, not Postgres. Still to do before deploy -- run db_init.py")
    print("and one real week against a throwaway Postgres.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
