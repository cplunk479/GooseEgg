"""
tests/test_demo.py
==================
End-to-end wiring for demo mode and the tier pipeline.

    python tests/test_demo.py

WHY THIS EXISTS
---------------
Every other test in this repo stubs at the module boundary: FakeSleeper hands
week_engine a list of dicts. That proves the engine's arithmetic and proves
nothing about the seam demo mode installs, which is the one piece of v0.5 that
could silently serve invented scores to a real league.

So this one stubs LOWER -- at sleeper._get_json, the single function that
touches the network -- with payloads shaped like the real Sleeper responses
(the field names are the ones recorded in sleeper.py's own comments, read off
a live /scores/nfl/regular/2025/1). Everything above that runs for real:
demo.build, the seam, position averages, tiers, lock_week's snapshot,
watch.build's classification and sorting, and a full Flask render of every
screen.

What it still does not prove: that Sleeper's real responses match the shapes
below. Nothing local can prove that. The recorded field names are the closest
thing to it, and the first live demo build is the test that closes the gap.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# db.py imports psycopg2 and app.py imports bcrypt; neither is needed here and
# neither is installed everywhere these tests should run. Same trick as
# test_week_engine.py -- stub the names, touch no database.
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
    sys.modules.update({"psycopg2": _pg, "psycopg2.extensions": _ext,
                        "psycopg2.extras": _extras})
if "bcrypt" not in sys.modules:
    _bc = types.ModuleType("bcrypt")
    _bc.checkpw = lambda *a, **k: False
    _bc.hashpw = lambda p, s: p
    _bc.gensalt = lambda *a, **k: b""
    sys.modules["bcrypt"] = _bc

from test_week_engine import FakeDB, to_sqlite  # noqa: E402

import db_init  # noqa: E402
import demo as demomod  # noqa: E402
import goose  # noqa: E402
import settings as settingsmod  # noqa: E402
import sleeper  # noqa: E402
import watch as watchmod  # noqa: E402
import week_engine as engine  # noqa: E402

LEAGUE, SEASON, WEEK = "TESTLEAGUE", 2026, 1
SLOTS = ["QB", "RB", "WR", "WR", "WR", "TE", "FLEX", "FLEX", "FLEX", "FLEX", "SUPER_FLEX"]
TEAMS = sorted(sleeper.NFL_TEAMS)
ROSTERS = list(range(1, 13))

failures: list[str] = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


# --------------------------------------------------------------------------
# A stub Sleeper, at the network boundary
# --------------------------------------------------------------------------
#
# 132 starters (12 rosters x 11 slots), spread across all 32 NFL teams so the
# bye-week and game-state paths are genuinely exercised, with a projection
# ramp per position wide enough to land players in every tier.

POSITION_OF_SLOT = {
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE",
    "FLEX": "WR", "SUPER_FLEX": "QB",
}
PROJ_BY_POSITION = {"QB": 18.0, "RB": 13.0, "WR": 12.0, "TE": 9.0}


def player_id(rid: int, slot_index: int) -> str:
    return f"{rid:02d}{slot_index:02d}"


def build_players() -> dict:
    """player_id -> row, with a projection spread that fills every tier."""
    out = {}
    for rid in ROSTERS:
        for i, slot in enumerate(SLOTS):
            pos = POSITION_OF_SLOT[slot]
            pid = player_id(rid, i)
            # A repeating multiplier ladder: some stars, some dead weight.
            ladder = (1.55, 1.20, 1.00, 0.95, 0.80, 0.70, 0.50, 0.30, 1.35, 1.10, 0.90)
            factor = ladder[(rid + i) % len(ladder)]
            out[pid] = {
                "full_name": f"Player {pid}",
                "position": pos,
                "team": TEAMS[(rid * 11 + i) % len(TEAMS)],
                "injury_status": None,
                "projection": round(PROJ_BY_POSITION[pos] * factor, 2),
            }
    return out


# Two teams sit out, so bye handling is on the path rather than theoretical.
BYE_TEAMS = {TEAMS[0], TEAMS[1]}
PLAYING = [t for t in TEAMS if t not in BYE_TEAMS]

PLAYERS = build_players()

# One starter is ruled OUT -- and he has to be on a team that PLAYS, or the bye
# check upstream of him answers first and this stops testing what it says it
# tests. That is not a bug in the model (a bye does beat an injury) but it is a
# trap in a fixture, and this one fell into it.
OUT_PID = next(pid for pid, p in sorted(PLAYERS.items())
               if p["team"] not in BYE_TEAMS and p["position"] == "QB")
PLAYERS[OUT_PID]["injury_status"] = "Out"
OUT_SLOT = (int(OUT_PID[:2]), int(OUT_PID[2:]))

BASE_KICKOFF = 1_788_000_000  # a Sunday, in seconds


def scores_payload() -> list[dict]:
    """The /scores/nfl/regular/<season>/<week> shape, per sleeper.py's notes."""
    games = []
    for i in range(0, len(PLAYING) - 1, 2):
        home, away = PLAYING[i], PLAYING[i + 1]
        games.append({
            "status": "pre_game",
            "start_time": (BASE_KICKOFF + i * 1800) * 1000,   # epoch MILLIseconds
            "metadata": {
                "home_team": home, "away_team": away,
                "is_over": False, "is_in_progress": False,
                "quarter": None, "quarter_num": None, "time_remaining": None,
                "home_score": 0, "away_score": 0,
            },
        })
    return games


def matchups_payload() -> list[dict]:
    out = []
    for rid in ROSTERS:
        starters = [player_id(rid, i) for i in range(len(SLOTS))]
        if rid == 7:
            starters[5] = "0"          # one owner leaves a slot empty
        out.append({
            "roster_id": rid, "matchup_id": (rid + 1) // 2,
            "starters": starters,
            "starters_points": [0.0] * len(SLOTS),
            "points": 0.0,
        })
    return out


def projections_payload() -> dict:
    # Sleeper returns raw STATS, which the app multiplies by the league's own
    # scoring settings. One stat with weight 1.0 keeps the arithmetic legible
    # without shortcutting player_points, which stays under test.
    return {pid: {"stats": {"pts_proxy": p["projection"]}} for pid, p in PLAYERS.items()}


def fake_get_json(url: str, timeout: int = 20):
    if "/scores/nfl/" in url:
        return scores_payload()
    if "/projections/nfl/" in url:
        return projections_payload()
    if "/matchups/" in url:
        return matchups_payload()
    if url.endswith(f"/league/{LEAGUE}"):
        return {
            "league_id": LEAGUE, "name": "Test Dons", "season": str(SEASON),
            "roster_positions": SLOTS + ["BN"] * 9,
            "scoring_settings": {"pts_proxy": 1.0},
        }
    raise AssertionError(f"stub has no route for {url}")


def build_db(db=None, migrations=None) -> FakeDB:
    """
    Schema, settings, owners and players.

    `db` lets another suite hand in its own connection wrapper (test_resilience
    passes one that models Postgres transaction semantics), and `migrations`
    lets it build a PREVIOUS schema by passing an empty list -- which is how the
    stale-database regression is reproduced without a real Postgres.
    """
    db = db if db is not None else FakeDB()
    migrations = db_init.MIGRATIONS if migrations is None else migrations
    for stmt in db_init.TABLES:
        db.execute(to_sqlite(stmt))
    for stmt in migrations:
        try:
            db.execute(to_sqlite(stmt.replace(" IF NOT EXISTS", "")))
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc):
                raise
    for stmt in db_init.INDEXES:
        db.execute(stmt)
    for key, value in settingsmod.DEFAULTS.items():
        db.execute("INSERT INTO app_meta (key, value) VALUES (%s, %s)", (key, value))
    db.execute("INSERT INTO app_meta (key, value) VALUES ('active_week', '1')")
    for rid in ROSTERS:
        db.execute(
            "INSERT INTO owners (league_id, season, roster_id, owner_name, team_name, "
            "is_admin, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (LEAGUE, SEASON, rid, f"owner{rid}", f"Team {rid}", rid == 1, 0),
        )
    for pid, p in PLAYERS.items():
        db.execute(
            "INSERT INTO players_cache (player_id, full_name, position, team, injury_status) "
            "VALUES (%s, %s, %s, %s, %s)",
            (pid, p["full_name"], p["position"], p["team"], p["injury_status"]),
        )
    db.commit()
    return db


def reset_sleeper():
    sleeper.set_demo(None)
    sleeper.clear_cache()
    sleeper._get_json = fake_get_json


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def test_projection_pipeline():
    print("project_week: real projections in, tiers out")
    reset_sleeper()
    db = build_db()
    p = engine.project_week(db, LEAGUE, SEASON, WEEK)

    check("every roster is projected", len(p["rosters"]) == 12, str(len(p["rosters"])))
    check("every slot is tiered",
          all(len(b["slots"]) == 11 for b in p["rosters"].values()))
    avg = p["position_avg"]
    check("the bar is computed per position", set(avg) >= {"QB", "RB", "WR", "TE"}, str(avg))
    check("the QB bar sits near the QB projections",
          14 < avg["QB"] < 24, str(avg))

    tiers = [s["tier"] for b in p["rosters"].values() for s in b["slots"]]
    used = set(tiers)
    check("the spread reaches more than one tier", len(used) >= 4, str(sorted(used)))
    check("every tier name is one goose.py knows", used <= set(goose.TIERS), str(used))

    # The availability overrides, on the path rather than in isolation.
    flat = {(s["roster_id"], s["slot_index"]): s
            for b in p["rosters"].values() for s in b["slots"]}
    out_row = flat[OUT_SLOT]
    check("the OUT player is COOKED and says OUT",
          out_row["tier"] == goose.COOKED and out_row["reason"] == "OUT", str(out_row))
    check("...and OUT beat a perfectly good projection",
          (out_row["projection"] or 0) > 10, str(out_row["projection"]))
    check("the empty slot is COOKED and says so",
          flat[(7, 5)]["tier"] == goose.COOKED and flat[(7, 5)]["reason"] == "EMPTY SLOT",
          str(flat[(7, 5)]))
    byes = [s for s in flat.values() if s["reason"] == "ON BYE"]
    check("bye-week starters are caught", len(byes) > 0, str(len(byes)))

    check("every roster gets a team rating",
          all(b["risk"]["tier"] in goose.TEAM_TIERS for b in p["rosters"].values()))
    check("the league-wide expectation is plausible, not eleven a week",
          1.0 <= p["expected_gooses"] <= 12.0, str(p["expected_gooses"]))
    db.close()


def test_lock_snapshots_tiers():
    print("\nlock_week: the snapshot carries the tier, not just the number")
    reset_sleeper()
    db = build_db()
    engine.open_week(db, LEAGUE, SEASON, WEEK, 1)
    r = engine.lock_week(db, LEAGUE, SEASON, WEEK)
    check("the lock reports every lineup", r["ok"] and r["lineups"] == 12, str(r))

    rows = db.execute("SELECT * FROM lineup_slots WHERE season = %s AND week = %s",
                      (SEASON, WEEK)).fetchall()
    check("132 slots are frozen", len(rows) == 132, str(len(rows)))
    check("every frozen slot has a tier", all(r_["tier"] in goose.TIERS for r_ in rows))
    check("a ratio is stored wherever one was computed",
          any(r_["proj_ratio"] is not None for r_ in rows))
    check("the reason is stored for the overrides",
          any(r_["risk_reason"] == "EMPTY SLOT" for r_ in rows))

    tw = db.execute("SELECT * FROM team_weeks WHERE season = %s AND week = %s",
                    (SEASON, WEEK)).fetchall()
    check("every team gets a frozen rating",
          len(tw) == 12 and all(t["risk_tier"] in goose.TEAM_TIERS for t in tw))
    check("at_risk is counted", all(t["at_risk"] is not None for t in tw))
    check("projections are non-zero", all((t["proj_total"] or 0) > 50 for t in tw),
          str([t["proj_total"] for t in tw][:3]))
    db.close()


def test_demo_build_and_seam():
    print("\ndemo mode: build the snapshot, install the seam, prove it is serving it")
    reset_sleeper()
    db = build_db()

    real_games = sleeper.week_games(SEASON, WEEK)
    check("the real feed is all pre-game before the demo",
          all(not (g["metadata"]["is_over"] or g["metadata"]["is_in_progress"])
              for g in real_games))

    payload = demomod.build(db, LEAGUE, SEASON, WEEK)
    check("the payload names its week", payload["week"] == WEEK)
    check("it counts the starters it covers", payload["starters"] == 132,
          str(payload["starters"]))
    check("it plants gooses deliberately", 3 <= len(payload["gooses"]) <= 6,
          str(len(payload["gooses"])))

    states = [("final" if g["metadata"]["is_over"] else
               "live" if g["metadata"]["is_in_progress"] else "pre")
              for g in payload["games"]]
    check("some games are final", states.count("final") >= 3, str(states.count("final")))
    check("some games are live", states.count("live") >= 3, str(states.count("live")))
    check("some games have not kicked off", states.count("pre") >= 1, str(states.count("pre")))

    # Nothing is installed until the flag goes up.
    check("building alone does not install the seam", sleeper.demo_payload() is None)

    settingsmod.set_bool(db, demomod.FLAG, True)
    db.commit()
    check("install picks up the stored snapshot", demomod.install(db) is True)
    check("the seam reports itself", sleeper.demo_payload() is not None)

    served = sleeper.week_games(SEASON, WEEK)
    check("week_games now serves the demo",
          any(g["metadata"]["is_over"] for g in served))
    check("matchups now serve demo points",
          any(any(p > 0 for p in m["starters_points"])
              for m in sleeper.matchups(LEAGUE, WEEK)))

    # The week guard: a snapshot built for week 1 must not answer for week 4.
    other = sleeper.week_games(SEASON, WEEK + 3)
    check("A SNAPSHOT FOR WEEK 1 DOES NOT ANSWER FOR WEEK 4",
          all(not g["metadata"]["is_over"] for g in other), "week 4 served week 1 scores")

    check("scores are deterministic across calls",
          sleeper.matchups(LEAGUE, WEEK)[0]["starters_points"]
          == sleeper.matchups(LEAGUE, WEEK)[0]["starters_points"])

    totals = [m["points"] for m in sleeper.matchups(LEAGUE, WEEK)]
    # A zero is legitimate mid-afternoon -- a lineup entirely in the late slate
    # really has scored nothing yet. What must not happen is a whole league of
    # them, or a number no fantasy team could post.
    check("no team posts an impossible score", all(0 <= t < 320 for t in totals),
          str([round(t) for t in totals]))
    check("most teams are on the board",
          sum(1 for t in totals if t > 0) >= len(totals) - 3,
          str([round(t) for t in totals]))
    check("the totals are not all the same number", len(set(totals)) > 8, str(len(set(totals))))
    db.close()


def test_demo_props_are_reversible():
    print("\ndemo props: real rows, flagged, and all of them come back")
    reset_sleeper()
    db = build_db()

    # A real curse and a real token, which must survive everything below.
    engine.mint_token(db, SEASON, 1, WEEK, "chug", "a real chug")
    db.execute(
        "INSERT INTO curses (season, week, caster_roster_id, target_roster_id, status, "
        "created_at) VALUES (%s, %s, %s, %s, 'cast', %s)", (SEASON, WEEK, 1, 2, 0))
    db.commit()

    made = demomod.seed_props(db, SEASON, WEEK, ROSTERS)
    check("props are seeded", made["ok"] and made["curses"] == 2, str(made))
    check("every seeded row is flagged as demo",
          db.execute("SELECT COUNT(*) AS n FROM curse_tokens WHERE is_demo"
                     ).fetchone()["n"] == made["tokens"])

    removed = demomod.clear_props(db)
    check("clearing removes what it made", sum(removed.values()) > 0, str(removed))
    check("THE REAL CURSE SURVIVES",
          db.execute("SELECT COUNT(*) AS n FROM curses").fetchone()["n"] == 1)
    check("THE REAL TOKEN SURVIVES",
          db.execute("SELECT COUNT(*) AS n FROM curse_tokens").fetchone()["n"] == 1)
    check("no demo row is left anywhere",
          all(db.execute(f"SELECT COUNT(*) AS n FROM {t} WHERE is_demo").fetchone()["n"] == 0
              for t in ("curses", "curse_tokens", "blessings", "chugs")))
    db.close()


def test_watch_over_demo():
    print("\nGoose Watch over the demo feed: four states, sorted by time left")
    reset_sleeper()
    db = build_db()
    engine.open_week(db, LEAGUE, SEASON, WEEK, 1)
    engine.lock_week(db, LEAGUE, SEASON, WEEK)
    demomod.build(db, LEAGUE, SEASON, WEEK)
    settingsmod.set_bool(db, demomod.FLAG, True)
    db.commit()
    demomod.install(db)

    watchmod.sleeper = sleeper
    d = watchmod.build(db, LEAGUE, SEASON, WEEK)

    check("the feed reads as healthy", d["feed_ok"] is True)
    check("some games are final", d["games_final"] > 0, str(d["games_final"]))
    check("some games are live", d["games_live"] > 0, str(d["games_live"]))
    check("there is something to look at", d["total_goosed"] > 0, str(d["total_goosed"]))
    check("somebody is drinking", len(d["drinkers"]) > 0)
    check("all 132 starters are on the board", len(d["rows"]) == 132, str(len(d["rows"])))

    # The ordering rule, on real assembled rows rather than a hand-built list.
    for group in ("danger", "pending", "cleared"):
        rows = d[group]
        started = [r["seconds_left"] for r in rows if r["seconds_left"] is not None]
        check(f"{group} is sorted by time left",
              started == sorted(started), str(started[:8]))
        first_none = next((i for i, r in enumerate(rows) if r["seconds_left"] is None), None)
        if first_none is not None:
            check(f"{group} puts 'yet to play' last",
                  all(r["seconds_left"] is None for r in rows[first_none:]),
                  f"a started game sits below an unstarted one in {group}")

    check("rows carry the frozen tier through to the screen",
          all(r["tier"] in goose.TIERS for r in d["rows"]))
    check("cleared only lists players the model feared",
          all(goose.TIER_INDEX[r["tier"]] >= goose.TIER_INDEX[goose.SHAKY]
              for r in d["cleared"]))
    db.close()


def test_every_screen_renders_over_the_demo():
    """
    The last mile: real Flask, real templates, real filters, real context, over
    the demo feed. A missing template variable dies here rather than on a phone.
    """
    print("\nevery screen renders through real Flask over the demo feed")
    try:
        import flask  # noqa: F401
    except ImportError:
        print("  skip  flask is not installed here")
        return

    reset_sleeper()
    db = build_db()
    engine.open_week(db, LEAGUE, SEASON, WEEK, 1)
    engine.lock_week(db, LEAGUE, SEASON, WEEK)
    demomod.build(db, LEAGUE, SEASON, WEEK)
    demomod.seed_props(db, SEASON, WEEK, ROSTERS)
    settingsmod.set_bool(db, demomod.FLAG, True)
    db.commit()

    os.environ["GOOSE_LEAGUE_ID"] = LEAGUE
    os.environ["GOOSE_SEASON"] = str(SEASON)
    os.environ["FLASK_SECRET_KEY"] = "test"
    import app as appmod
    appmod.LEAGUE_ID, appmod.SEASON = LEAGUE, SEASON
    appmod.dbmod = types.SimpleNamespace(open_wrapped=lambda: db)
    db.close = lambda: None                      # one connection for the run

    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()
    with client.session_transaction() as sess:
        sess["roster_id"] = 1

    pages = [
        ("Board", "/"),
        ("Board, week 1 explicitly", "/?week=1"),
        ("Goose Watch", "/watch"),
        ("My Geese", "/me"),
        ("Standings", "/standings"),
        ("Admin, chugs", "/admin?tab=chugs"),
        ("Admin, week", "/admin?tab=week"),
        ("Admin, curses", "/admin?tab=curses"),
        ("Admin, rules", "/admin?tab=rules"),
        ("Admin, demo", "/admin?tab=demo"),
        ("Admin, reset", "/admin?tab=reset"),
    ]
    for label, path in pages:
        resp = client.get(path)
        body = resp.get_data(as_text=True)
        check(f"{label} renders", resp.status_code == 200 and len(body) > 500,
              f"HTTP {resp.status_code}, {len(body)} chars")
        if resp.status_code == 200:
            check(f"{label} shows the demo banner", "DEMO MODE" in body,
                  "a page served fake scores without saying so")

    board = client.get("/").get_data(as_text=True)
    check("the board shows a tier rather than a price",
          any(t in board for t in goose.TEAM_TIERS) and "+2" not in board.split("<style>")[0])
    check("the token artwork is wired up", "img/goothulu.jpg" in board)
    check("the blessing artwork is wired up", "img/goosiah.jpg" in board)

    mine = client.get("/me").get_data(as_text=True)
    check("My Geese shows player photos", "sleepercdn.com/content/nfl/players" in mine)

    # And the safety net: automation must refuse to run while demo is on.
    poll = client.get("/poll").get_json()
    check("POLL REFUSES TO SETTLE A DEMO WEEK",
          poll.get("demo") is True and "demo:automation-paused" in poll.get("did", []),
          str(poll))


def main() -> int:
    test_projection_pipeline()
    test_lock_snapshots_tiers()
    test_demo_build_and_seam()
    test_demo_props_are_reversible()
    test_watch_over_demo()
    test_every_screen_renders_over_the_demo()
    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
