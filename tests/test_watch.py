"""
tests/test_watch.py
===================
Goose Watch classification.

    python tests/test_watch.py

The test that matters most is the bye-week one. A player with no game in the
feed scores nothing, and if "no game" were read as "his game is over" the board
would announce on Thursday that half the league is drinking. `pending` and
`final` have to stay apart.

Reuses the SQLite shim from test_week_engine so the schema still comes from
db_init.TABLES and cannot drift.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_week_engine import FakeDB, to_sqlite  # noqa: E402

import db_init  # noqa: E402
import goose  # noqa: E402
import sqlite3  # noqa: E402
import sleeper  # noqa: E402
import watch  # noqa: E402

LEAGUE, SEASON, WEEK = "TEST", 2026, 3
SLOTS = ["QB", "RB", "WR", "WR", "WR", "TE", "FLEX", "FLEX", "FLEX", "FLEX", "SUPER_FLEX"]

failures: list[str] = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


class FakeSleeper:
    FINAL, LIVE, PRE = sleeper.FINAL, sleeper.LIVE, sleeper.PRE

    def __init__(self, starters, points, games):
        self._starters, self._points, self._games = starters, points, games

    def starting_slots(self, league_id):
        return SLOTS

    def matchups(self, league_id, week):
        return [
            {"roster_id": rid, "starters": s, "starters_points": self._points[rid]}
            for rid, s in self._starters.items()
        ]

    def game_state_by_team(self, season, week):
        return self._games


def build(starters, points, games, snapshot_tiers=None):
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
        "INSERT INTO owners (league_id, season, roster_id, owner_name, team_name, is_admin, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)", (LEAGUE, SEASON, 1, "conner", "Melange", 1, 0),
    )
    for pid, (name, pos, team) in PLAYERS.items():
        db.execute(
            "INSERT INTO players_cache (player_id, full_name, position, team) VALUES (%s, %s, %s, %s)",
            (pid, name, pos, team),
        )
    for idx, tier in (snapshot_tiers or {}).items():
        db.execute(
            "INSERT INTO lineup_slots (season, week, roster_id, slot_index, slot, player_id, "
            "goose_prob, tier) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (SEASON, WEEK, 1, idx, SLOTS[idx], starters[1][idx],
             goose.TIER_RATE[tier], tier),
        )
    db.commit()
    watch.sleeper = FakeSleeper(starters, points, games)
    return db


PLAYERS = {
    "pFINAL": ("Final Guy", "WR", "TB"),
    "pLIVE": ("Live Guy", "RB", "KC"),          # live, but only Q3 -- not "danger" yet
    "pQ4": ("Fourth Quarter Guy", "RB", "MIN"),  # live and in the 4th -- this is danger
    "pPRE": ("Pregame Guy", "TE", "SF"),
    "pBYE": ("Bye Guy", "WR", "DET"),
    "pSCORED": ("Scored Guy", "WR", "TB"),
    "pRISKY": ("Risky Guy", "WR", "KC"),
}
# seconds_left mirrors what sleeper.game_state_by_team computes: 0 once a game
# is over, None before kickoff, and (4 - quarter) * 15 min + the clock while
# it is live. Kept explicit here so the sort is tested against stated numbers
# rather than against the parser that produced them.
GAMES = {
    "TB": {"state": sleeper.FINAL, "clock": "FINAL", "quarter": None, "opponent": "ATL",
           "seconds_left": 0},
    "KC": {"state": sleeper.LIVE, "clock": "Q3 8:42", "quarter": 3, "opponent": "DEN",
           "seconds_left": 1422},
    "MIN": {"state": sleeper.LIVE, "clock": "Q4 2:10", "quarter": 4, "opponent": "GB",
            "seconds_left": 130},
    "SF": {"state": sleeper.PRE, "clock": "not started", "quarter": None, "opponent": "SEA",
           "seconds_left": None},
    # DET deliberately absent -- on bye
}


def test_classification():
    print("classification: final / live-early / live-Q4 / pregame / bye / empty")
    starters = {1: ["pFINAL", "pLIVE", "pQ4", "pPRE", "pBYE", "0",
                    "pSCORED", "pRISKY", "pFINAL", "pLIVE", "pPRE"]}
    points = {1: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 12.5, 8.0, -2.0, 0.0, 0.0]}
    db = build(starters, points, GAMES)
    d = watch.build(db, LEAGUE, SEASON, WEEK)

    by_state = {}
    for r in d["rows"]:
        by_state.setdefault(r["state"], []).append(r["player_id"])

    check("a zero in a FINAL game is goosed", "pFINAL" in by_state.get("goosed", []))
    check("a negative in a FINAL game is goosed",
          by_state.get("goosed", []).count("pFINAL") == 2, str(by_state.get("goosed")))
    check("a zero live but still in Q3 is pending, not danger yet",
          "pLIVE" in by_state.get("pending", []) and "pLIVE" not in by_state.get("danger", []),
          str(by_state))
    check("a zero live in the 4th quarter is danger", "pQ4" in by_state.get("danger", []))
    check("a zero before kickoff is pending", "pPRE" in by_state.get("pending", []))
    check("A BYE-WEEK ZERO IS PENDING, NOT GOOSED",
          "pBYE" in by_state.get("pending", []) and "pBYE" not in by_state.get("goosed", []),
          str(by_state))
    check("an empty slot is goosed whatever the clock", None in by_state.get("goosed", []))
    check("points on the board is safe", "pSCORED" in by_state.get("safe", []))
    check("the goosed count matches the list", d["total_goosed"] == len(d["goosed"]))
    check("a starter's photo is keyed off the real player_id",
          any(r["player_id"] == "pQ4" and r["photo"] == "https://sleepercdn.com/content/nfl/players/thumb/pQ4.jpg"
              for r in d["rows"]))
    check("an empty slot has no photo",
          all(r["photo"] is None for r in d["rows"] if r["player_id"] is None))
    db.close()


def test_cleared_only_flags_the_feared():
    print("\ncleared only lists players the model actually worried about")
    starters = {1: ["pSCORED", "pRISKY"] + ["pSCORED"] * 9}
    points = {1: [10.0, 6.0] + [10.0] * 9}
    db = build(starters, points, GAMES,
               snapshot_tiers={0: goose.SAFE, 1: goose.BAIT})
    d = watch.build(db, LEAGUE, SEASON, WEEK)
    ids = [r["player_id"] for r in d["cleared"]]
    check("a GOOSE BAIT starter who scored shows as cleared", "pRISKY" in ids, str(ids))
    check("a SAFE starter who scored is not noise", ids.count("pSCORED") == 0, str(ids))
    db.close()


def test_sorts_by_time_left():
    """
    The ordering rule: closest to settled first, not-yet-kicked-off last.

    The trap this guards is that a game with no kickoff has `seconds_left` of
    None, and a naive ascending sort treats None as smaller than everything --
    which would put the 8pm game above the one with two minutes left.
    """
    print("\nrows sort by game time remaining, with 'yet to play' last")
    starters = {1: ["pQ4", "pLIVE", "pPRE", "pBYE"] + ["pSCORED"] * 7}
    points = {1: [0.0, 0.0, 0.0, 0.0] + [9.0] * 7}
    db = build(starters, points, GAMES)
    d = watch.build(db, LEAGUE, SEASON, WEEK)

    order = [r["player_id"] for r in d["pending"]]
    check("the Q3 game (12 min left) outranks a game that has not kicked off",
          order.index("pLIVE") < order.index("pPRE"), str(order))
    check("a bye-week player sorts to the very bottom",
          order.index("pBYE") > order.index("pLIVE"), str(order))
    check("seconds_left is carried onto every row",
          all("seconds_left" in r for r in d["rows"]))
    check("a finished game reports zero seconds left",
          all(r["seconds_left"] == 0 for r in d["rows"] if r["nfl_team"] == "TB"))
    check("a game with no kickoff reports None, not zero",
          all(r["seconds_left"] is None for r in d["rows"] if r["nfl_team"] == "SF"),
          str([r["seconds_left"] for r in d["rows"] if r["nfl_team"] == "SF"]))
    db.close()


def test_drinkers_and_totals():
    print("\nper-owner rollup")
    starters = {
        1: ["pFINAL", "pFINAL", "pQ4"] + ["pSCORED"] * 8,
        2: ["pSCORED"] * 11,
    }
    points = {1: [0.0, 0.0, 0.0] + [9.0] * 8, 2: [9.0] * 11}
    db = build(starters, points, GAMES)
    db.execute(
        "INSERT INTO owners (league_id, season, roster_id, owner_name, team_name, is_admin, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)", (LEAGUE, SEASON, 2, "other", "Team Two", 0, 0),
    )
    db.commit()
    d = watch.build(db, LEAGUE, SEASON, WEEK)
    check("only the owner with a final zero is drinking", len(d["drinkers"]) == 1, str(d["drinkers"]))
    check("they are down for two", d["drinkers"][0]["goosed"] == 2, str(d["drinkers"][0]))
    check("their 4th-quarter zero counts as danger, not a chug", d["total_danger"] == 1, str(d["total_danger"]))
    check("the clean owner is not listed as drinking",
          all(x["owner"] != "Team Two" for x in d["drinkers"]))
    db.close()


def test_feed_failure_is_visible():
    print("\nan unreadable feed must not look like a clean week")
    starters = {1: ["pFINAL"] * 11}
    points = {1: [0.0] * 11}
    db = build(starters, points, {})          # no games at all
    d = watch.build(db, LEAGUE, SEASON, WEEK)
    check("feed_ok is False so the page can say why", d["feed_ok"] is False)
    check("nothing is declared goosed off a dead feed", d["total_goosed"] == 0, str(d["total_goosed"]))
    check("those starters sit in pending instead", len(d["pending"]) == 11, str(len(d["pending"])))
    db.close()


def test_clock_formatting():
    print("\nthe clock string")
    check("final", sleeper._clock({"is_over": True, "quarter": "F"}) == "FINAL")
    check("final by quarter alone", sleeper._clock({"quarter": "F"}) == "FINAL")
    check("halftime", sleeper._clock(
        {"is_in_progress": True, "quarter_num": 2, "time_remaining": "00:00"}) == "HALFTIME")
    check("mid-quarter", sleeper._clock(
        {"is_in_progress": True, "quarter_num": 3, "time_remaining": "08:42"}) == "Q3 8:42",
        sleeper._clock({"is_in_progress": True, "quarter_num": 3, "time_remaining": "08:42"}))
    check("not started", sleeper._clock({}) == "not started")
    check("an empty metadata dict does not raise", isinstance(sleeper._clock({}), str))


def test_game_state_parsing():
    print("\nparsing the real /scores shape (fields read off a live response)")
    payload = [{
        "status": "complete",
        "start_time": 1757264400000,
        "metadata": {"home_team": "ATL", "away_team": "TB", "is_over": True,
                     "is_in_progress": False, "quarter": "F", "quarter_num": 4,
                     "time_remaining": "00:00", "home_score": 20, "away_score": 23},
    }, {
        "status": "in_game",
        "start_time": 1757275000000,
        "metadata": {"home_team": "KC", "away_team": "DEN", "is_over": False,
                     "is_in_progress": True, "quarter": "3", "quarter_num": 3,
                     "time_remaining": "08:42", "home_score": 14, "away_score": 10},
    }]
    original = sleeper.week_games
    sleeper.week_games = lambda season, week: payload
    try:
        states = sleeper.game_state_by_team(2025, 1)
        check("both teams from a game are indexed", {"ATL", "TB", "KC", "DEN"} <= set(states))
        check("a complete game reads final", states["ATL"]["state"] == sleeper.FINAL)
        check("an in-progress game reads live", states["KC"]["state"] == sleeper.LIVE)
        check("the live clock is human", states["KC"]["clock"] == "Q3 8:42", states["KC"]["clock"])
        check("the live game exposes its raw quarter number", states["KC"]["quarter"] == 3, str(states["KC"]["quarter"]))
        check("a final game's quarter is not read as 'currently playing'",
              states["ATL"]["quarter"] is None, str(states["ATL"]["quarter"]))
        check("opponents are paired", states["TB"]["opponent"] == "ATL")
        check("scores follow the right side",
              states["TB"]["score"] == 23 and states["ATL"]["score"] == 20)
        check("start_time is converted from milliseconds",
              states["ATL"]["start_time"] == 1757264400, str(states["ATL"]["start_time"]))
        check("a team on bye is simply absent", "DET" not in states)
    finally:
        sleeper.week_games = original


def main() -> int:
    test_classification()
    test_cleared_only_flags_the_feared()
    test_sorts_by_time_left()
    test_drinkers_and_totals()
    test_feed_failure_is_visible()
    test_clock_formatting()
    test_game_state_parsing()
    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
