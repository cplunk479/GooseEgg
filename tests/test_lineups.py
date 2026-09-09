"""
tests/test_lineups.py
=====================
The collapsible lineup panel on the Curse board.

    python tests/test_lineups.py

Two properties are worth a test file of their own, and they pull in opposite
directions:

  THE PROJECTION MUST NOT MOVE once the week is locked. That number is the bar
  a curse is graded against. The test below locks a week, then moves Sleeper's
  projections underneath it, and demands the panel still shows the frozen one.
  If that check ever goes green while showing the new number, curses are being
  graded against a bar that moved after they were cast.

  THE ACTUAL MUST MOVE on every single build. There is no such thing as a
  stale score worth showing. The test moves Sleeper's points and demands the
  panel follows without anything being re-locked or re-settled.

Plus the distinction Goose Watch exists to draw, which this panel has to draw
the same way: a zero in the second quarter is NOT a goose. Only a finished
game settles one. Getting that backwards would put a goose count on the board
at 1:05pm every Sunday.

Reuses the SQLite shim from test_week_engine, so the schema still comes from
db_init.TABLES and cannot drift from what ships.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_week_engine import FakeDB, to_sqlite  # noqa: E402

import db_init  # noqa: E402
import lineups as lineupsmod  # noqa: E402
import sleeper  # noqa: E402
import week_engine as engine  # noqa: E402

LEAGUE, SEASON, WEEK = "TEST", 2026, 3
SLOTS = ["QB", "RB", "WR", "WR", "WR", "TE", "FLEX", "FLEX", "FLEX", "FLEX", "SUPER_FLEX"]

# Four starters is enough to cover every state a row can be in, and the fifth
# slot is left empty on purpose -- an empty slot is an automatic goose and is
# the one row that has no game to wait for.
PLAYERS = {
    "pDONE": ("Done Guy", "WR", "TB"),     # game over, scored
    "pZERO": ("Zero Guy", "RB", "TB"),     # game over, scored nothing -> goose
    "pLIVE": ("Live Guy", "QB", "KC"),     # live in Q2 on zero -> NOT a goose
    "pPRE":  ("Pregame Guy", "TE", "SF"),  # has not kicked off
}
GAMES = {
    "TB": {"state": sleeper.FINAL, "clock": "FINAL", "quarter": None,
           "opponent": "ATL", "home": True, "seconds_left": 0},
    "KC": {"state": sleeper.LIVE, "clock": "Q2 7:41", "quarter": 2,
           "opponent": "DEN", "home": False, "seconds_left": 1661},
    "SF": {"state": sleeper.PRE, "clock": None, "quarter": None,
           "opponent": "SEA", "home": True, "seconds_left": None},
}

failures: list[str] = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


class FakeSleeper:
    """Deterministic Sleeper. `points` and `projection_points` are settable."""

    FINAL, LIVE, PRE = sleeper.FINAL, sleeper.LIVE, sleeper.PRE

    def __init__(self):
        self.starters = ["pDONE", "pZERO", "pLIVE", "pPRE", "0"]
        self.points = [14.2, 0.0, 0.0, 0.0, 0.0]
        self.projection_points = 10.0

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
        return [{"roster_id": 1, "starters": list(self.starters),
                 "starters_points": list(self.points), "points": sum(self.points)}]

    def game_state_by_team(self, season, week):
        return GAMES


def build_db(fs) -> FakeDB:
    db = FakeDB()
    for stmt in db_init.TABLES:
        db.execute(to_sqlite(stmt))
    for stmt in db_init.MIGRATIONS:
        try:
            db.execute(to_sqlite(stmt.replace(" IF NOT EXISTS", "")))
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc):
                raise
    db.execute(
        "INSERT INTO owners (league_id, season, roster_id, owner_name, team_name, "
        "is_admin, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (LEAGUE, SEASON, 1, "conner", "Melange", 1, 0),
    )
    for pid, (name, pos, team) in PLAYERS.items():
        db.execute(
            "INSERT INTO players_cache (player_id, full_name, position, team) "
            "VALUES (%s, %s, %s, %s)", (pid, name, pos, team),
        )
    db.commit()
    engine.sleeper = fs
    lineupsmod.sleeper = fs
    return db


def by_name(panel, name):
    return next(p for p in panel["starters"] if p["name"] == name)


def test_states() -> None:
    print("every state a starter can be in, on one lineup")
    fs = FakeSleeper()
    db = build_db(fs)
    engine.open_week(db, LEAGUE, SEASON, WEEK, 1)
    engine.lock_week(db, LEAGUE, SEASON, WEEK)
    wk = engine.get_week(db, SEASON, WEEK)

    lu = lineupsmod.build(db, LEAGUE, SEASON, WEEK, wk)[1]
    check("the panel is built off the frozen snapshot", lu["frozen"])
    check("every starter is present", len(lu["starters"]) == 5, str(len(lu["starters"])))

    done = by_name(lu, "Done Guy")
    check("a finished scorer shows its actual", done["live_actual"] == 14.2)
    check("and is marked final", done["final"] and done["started"])
    check("and beat its projection", done["delta"] == 4.2, str(done["delta"]))
    check("and is not a goose", not done["is_goose"])

    zero = by_name(lu, "Zero Guy")
    check("A FINAL ZERO IS A GOOSE", zero["is_goose"])

    live = by_name(lu, "Live Guy")
    check("a live Q2 zero shows its score", live["live_actual"] == 0.0)
    check("A LIVE ZERO IS NOT A GOOSE -- there is a half left to fix it",
          not live["is_goose"], "the board would panic at 1:05pm every Sunday")
    check("and its game is not final", not live["final"])
    check("the clock comes through", live["clock"] == "Q2 7:41")
    check("so does the matchup", live["opponent"] == "DEN" and live["venue"] == "@")

    pre = by_name(lu, "Pregame Guy")
    check("A PLAYER WHO HAS NOT KICKED OFF SHOWS NO NUMBER",
          pre["live_actual"] is None,
          "a 0.0 here reads as 'played, scored nothing'")
    check("and counts as yet to play", not pre["started"])

    empty = by_name(lu, "Empty slot")
    check("an empty slot is a settled goose", empty["is_goose"] and empty["final"])
    check("and never waits for a game", empty["started"])

    check("the panel counts one player yet to play", lu["yet_to_play"] == 1,
          str(lu["yet_to_play"]))
    check("two settled geese so far", lu["live_geese"] == 2, str(lu["live_geese"]))
    check("the week is not all final", not lu["all_final"])
    check("the actual total counts only what has played",
          lu["actual_total"] == 14.2, str(lu["actual_total"]))
    db.close()


def test_projection_is_frozen_but_the_actual_is_not() -> None:
    print("\nthe projection freezes at lock; the actual never does")
    fs = FakeSleeper()
    db = build_db(fs)
    engine.open_week(db, LEAGUE, SEASON, WEEK, 1)
    engine.lock_week(db, LEAGUE, SEASON, WEEK)
    wk = engine.get_week(db, SEASON, WEEK)

    first = lineupsmod.build(db, LEAGUE, SEASON, WEEK, wk)[1]
    frozen_total = first["proj_total"]
    check("the lineup projects for something", frozen_total > 0, str(frozen_total))

    # Sleeper revises everybody upward -- an inactive list landing, say.
    fs.projection_points = 25.0
    again = lineupsmod.build(db, LEAGUE, SEASON, WEEK, wk)[1]
    check("THE PROJECTION DOES NOT MOVE AFTER LOCK",
          again["proj_total"] == frozen_total,
          f"{frozen_total} -> {again['proj_total']}; a curse would be graded "
          f"against a bar that moved after it was cast")
    check("and neither does any single row",
          by_name(again, "Done Guy")["proj"] == by_name(first, "Done Guy")["proj"])

    # Now the scores move, which they must be allowed to do.
    fs.points = [14.2, 0.0, 9.9, 0.0, 0.0]
    live = lineupsmod.build(db, LEAGUE, SEASON, WEEK, wk)[1]
    check("THE ACTUAL DOES MOVE, with no re-lock and no settle",
          by_name(live, "Live Guy")["live_actual"] == 9.9,
          str(by_name(live, "Live Guy")["live_actual"]))
    check("and the total follows it", live["actual_total"] == 24.1,
          str(live["actual_total"]))
    check("and the live guy is no longer a goose candidate",
          live["live_geese"] == 2, str(live["live_geese"]))
    db.close()


def test_preview_before_lock() -> None:
    print("\nbefore lock there is nothing frozen, and the panel says so")
    fs = FakeSleeper()
    db = build_db(fs)
    engine.open_week(db, LEAGUE, SEASON, WEEK, 1)
    wk = engine.get_week(db, SEASON, WEEK)

    lu = lineupsmod.build(db, LEAGUE, SEASON, WEEK, wk)[1]
    check("the panel is a live preview", not lu["frozen"])
    check("it still lists the lineup", len(lu["starters"]) == 5)
    check("and it still shows live actuals",
          by_name(lu, "Done Guy")["live_actual"] == 14.2)

    fs.projection_points = 25.0
    moved = lineupsmod.build(db, LEAGUE, SEASON, WEEK, wk)[1]
    check("AN UNLOCKED PROJECTION IS ALLOWED TO MOVE",
          moved["proj_total"] > lu["proj_total"],
          f"{lu['proj_total']} -> {moved['proj_total']}")
    db.close()


def main() -> int:
    test_states()
    test_projection_is_frozen_but_the_actual_is_not()
    test_preview_before_lock()
    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
