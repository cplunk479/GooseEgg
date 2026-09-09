"""
demo.py
=======
Demo mode: a fake but realistic mid-Sunday week 1, for showing the app off and
for testing screens that otherwise only exist for six hours a week in
September.

The rule that shapes this whole module
--------------------------------------
NOTHING FAKE TOUCHES A REAL ROW. Demo mode works by replacing what Sleeper
says, not by writing invented history into the database. The screens, the
engine and the tier model all run completely unmodified against a synthetic
feed -- which is the point, because a demo built out of hand-written HTML would
prove nothing about whether the real thing works.

Two pieces, and they are different in kind:

  THE FEED       A snapshot of matchups, projections and game states, stored
                 as JSON in app_meta and installed into sleeper.py while demo
                 mode is on. Read-only. Switching demo off drops it.

  THE PROPS      A handful of curses, tokens, blessings, marks and chugs, which DO
                 have to be real rows because that is where the app keeps them.
                 Every one is stamped is_demo = TRUE and deleted on the way out.
                 That flag is set in exactly one place, here, and is the only
                 thing standing between a demo and someone's real season.

Building the snapshot calls Sleeper for real, so build() must run while demo
mode is OFF. Real lineups, real players, real projections, real matchups;
only the clock and the points are invented. That is what makes it look right.
"""
from __future__ import annotations

import hashlib
import json
import random
import time

import settings as settingsmod
import sleeper

FLAG = "demo_mode"
PAYLOAD = "demo_payload"

# Where the imaginary Sunday sits: late afternoon, early games over, the
# afternoon slate in the fourth quarter, the late slate and the night games
# still to come. Chosen because that is the moment Goose Watch is actually
# useful and the moment with all four row states on screen at once.
GAME_SCRIPT = [
    ("final", None, "FINAL"),
    ("final", None, "FINAL"),
    ("final", None, "FINAL"),
    ("final", None, "FINAL"),
    ("live", 4, "Q4 3:18"),
    ("live", 4, "Q4 7:41"),
    ("live", 4, "Q4 11:02"),
    ("live", 4, "Q4 1:55"),
    ("live", 3, "Q3 5:29"),
    ("live", 3, "Q3 12:14"),
    ("live", 2, "HALFTIME"),
]
# Everything past the script -- the late slate, SNF, MNF -- has not kicked off.

# How far through a game each state is, for scaling a projection into a
# plausible running score.
PROGRESS = {"final": 1.0, 4: 0.86, 3: 0.62, 2: 0.45}


def _rng(*parts) -> random.Random:
    """
    Deterministic per-player randomness. Seeded from the player id so the same
    demo renders identically on every page load and on every worker -- a demo
    where the scores jump each refresh looks broken, not live.
    """
    seed = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12]
    return random.Random(int(seed, 16))


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def active(db) -> bool:
    return settingsmod.get_bool(db, FLAG, False)


# The parsed snapshot, kept per process. install() runs on EVERY request, and
# the payload is a few hundred KB of JSON -- re-parsing it on every page load
# would make demo mode measurably slower than the real thing, which is a silly
# way for a demo to look bad. Keyed on the raw string so a rebuild is picked up
# immediately without a restart.
_parsed: tuple[str, dict] | None = None


def install(db) -> bool:
    """Point sleeper.py at the stored snapshot. Called once per request."""
    global _parsed
    if not active(db):
        sleeper.set_demo(None)
        return False
    raw = settingsmod.get_raw(db, PAYLOAD)
    if not raw:
        sleeper.set_demo(None)
        return False
    if _parsed is not None and _parsed[0] == raw:
        sleeper.set_demo(_parsed[1])
        return True
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        _parsed = None
        sleeper.set_demo(None)
        return False
    _parsed = (raw, payload)
    sleeper.set_demo(payload)
    return True


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def build(db, league_id: str, season: int, week: int) -> dict:
    """
    Pull the real week from Sleeper and bend it into a mid-Sunday snapshot.

    Must run with demo mode OFF, or it would build a demo out of the last demo.
    """
    global _parsed
    _parsed = None
    sleeper.set_demo(None)
    sleeper.clear_cache()

    games = sorted(
        (g for g in sleeper.week_games(season, week) if g.get("start_time")),
        key=lambda g: g["start_time"],
    )
    matchups = sleeper.matchups(league_id, week)
    scoring = sleeper.scoring_settings(league_id)
    projections = sleeper.projections(season, week)
    players = {
        r["player_id"]: r for r in db.execute(
            "SELECT player_id, position, team FROM players_cache").fetchall()
    }

    state_by_team, fake_games = _fake_games(games)
    starters_total = sum(len(m.get("starters") or []) for m in matchups)
    goose_ids = _pick_gooses(matchups, state_by_team, players)

    fake_matchups = []
    for m in matchups:
        starters = [str(p) if p not in (None, "", "0", 0) else None
                    for p in (m.get("starters") or [])]
        points = []
        for pid in starters:
            points.append(_fake_points(
                pid, players.get(pid) or {}, projections.get(pid, {}), scoring,
                state_by_team, goose_ids,
            ))
        fake_matchups.append({
            **{k: v for k, v in m.items() if k not in ("starters_points", "points", "players_points")},
            "starters": m.get("starters"),
            "starters_points": points,
            "points": round(sum(points), 2),
        })

    payload = {
        "built_at": int(time.time()),
        "league_id": league_id,
        "season": season,
        "week": week,
        "games": fake_games,
        "matchups": fake_matchups,
        "projections": projections,
        "gooses": sorted(goose_ids),
        "starters": starters_total,
    }
    settingsmod.set_raw(db, PAYLOAD, json.dumps(payload))
    return payload


def _fake_games(games: list[dict]) -> tuple[dict, list[dict]]:
    """Rewrite each real game's clock and score to sit somewhere in GAME_SCRIPT."""
    state_by_team: dict = {}
    out: list[dict] = []
    for i, g in enumerate(games):
        meta = dict(g.get("metadata") or {})
        home = str(meta.get("home_team") or "").upper()
        away = str(meta.get("away_team") or "").upper()
        script = GAME_SCRIPT[i] if i < len(GAME_SCRIPT) else ("pre", None, "not started")
        state, quarter, clock = script
        rng = _rng("game", home, away)

        if state == "pre":
            meta.update(is_over=False, is_in_progress=False, quarter=None,
                        quarter_num=None, time_remaining=None,
                        home_score=0, away_score=0)
            status = "pre_game"
        else:
            share = PROGRESS["final" if state == "final" else quarter]
            meta.update(
                is_over=(state == "final"),
                is_in_progress=(state == "live"),
                quarter="F" if state == "final" else str(quarter),
                quarter_num=4 if state == "final" else quarter,
                time_remaining="00:00" if state == "final" else clock.split(" ")[-1],
                home_score=int(round(rng.uniform(17, 31) * share)),
                away_score=int(round(rng.uniform(13, 28) * share)),
            )
            status = "complete" if state == "final" else "in_game"

        # The QUARTER goes in alongside the state. Goose Watch only calls a
        # live zero "danger" from the fourth quarter on, so a demo that plants
        # its zeros in live-but-Q2 games shows an empty danger section -- which
        # is one of the four states the demo exists to put on screen.
        for team in (home, away):
            if team:
                state_by_team[team] = (state, quarter)
        out.append({**g, "status": status, "metadata": meta})
    return state_by_team, out


def _pick_gooses(matchups, state_by_team, players) -> set:
    """
    Hand-place the zeros so the demo actually shows what it is there to show.

    Left to chance, a realistic week 1 produces three or four gooses scattered
    anywhere -- including all of them in games that have not kicked off, which
    renders as a completely uneventful screen. So: one settled goose in a
    finished game, one still-on-zero in a fourth quarter, one earlier in the
    day, and one in a game yet to start. That is every row state on Goose Watch
    at once, which is the whole reason to look at it.
    """
    by_state: dict = {"final": [], 4: [], "early": [], "pre": []}
    for m in matchups:
        for pid in (m.get("starters") or []):
            pid = str(pid) if pid not in (None, "", "0", 0) else None
            if not pid:
                continue
            team = ((players.get(pid) or {}).get("team") or "").upper()
            state, quarter = state_by_team.get(team) or (None, None)
            if state == "final":
                by_state["final"].append(pid)
            elif state == "live" and (quarter or 0) >= 4:
                by_state[4].append(pid)
            elif state == "live":
                by_state["early"].append(pid)
            elif state == "pre":
                by_state["pre"].append(pid)

    picked = set()
    for key, count in (("final", 2), (4, 2), ("early", 1), ("pre", 1)):
        pool = sorted(by_state.get(key) or [])
        if not pool:
            continue
        rng = _rng("goose", key, len(pool))
        picked.update(rng.sample(pool, min(count, len(pool))))
    return picked


def _fake_points(pid, meta, proj_stats, scoring, state_by_team, goose_ids) -> float:
    """
    A plausible running score for one starter.

    Anchored to the player's REAL projection, scaled by how much of his game
    has been played, and spread with a wide multiplier because fantasy weeks
    are wildly overdispersed -- a tight 10%-either-side jitter would produce
    twelve lineups that all land within a point of projection, which is the
    one thing a fantasy Sunday never looks like.
    """
    if not pid:
        return 0.0
    team = (meta.get("team") or "").upper()
    state, quarter = state_by_team.get(team) or (None, None)
    if state is None or state == "pre":
        return 0.0                       # not kicked off; Sleeper reports 0
    if pid in goose_ids:
        return 0.0

    projected = sleeper.player_points(proj_stats, scoring)
    if projected <= 0:
        projected = 6.0
    rng = _rng("pts", pid)
    # Lognormal-ish: mostly near projection, a long right tail, a real floor.
    multiplier = max(0.05, min(2.6, rng.lognormvariate(-0.08, 0.55)))
    share = 1.0 if state == "final" else PROGRESS.get(quarter, 0.62)
    return round(projected * multiplier * share, 2)


# --------------------------------------------------------------------------
# props -- the only rows demo mode writes, and it takes all of them back
# --------------------------------------------------------------------------

def seed_props(db, season: int, week: int, roster_ids: list[int]) -> dict:
    """
    Give the week enough curse traffic to be worth looking at: a few tokens
    held, two curses cast, one blessing shielding, one owner walking around
    under Goosifer's Wrath, one chug still owed.

    Deterministic from the roster list so the same league always demos the
    same way, and every row carries is_demo = TRUE.
    """
    if len(roster_ids) < 4:
        return {"ok": False, "reason": "need at least four rosters to seed a demo"}
    rng = _rng("props", season, week, *roster_ids)
    rosters = sorted(roster_ids)
    order = rosters[:]
    rng.shuffle(order)
    now = int(time.time())
    made = {"tokens": 0, "curses": 0, "blessings": 0, "wraths": 0, "chugs": 0}

    holders = order[:4]
    for i, rid in enumerate(holders):
        for _ in range(1 if i else 2):
            db.execute(
                "INSERT INTO curse_tokens (season, roster_id, earned_week, source, note, "
                "created_at, is_demo) VALUES (%s, %s, %s, 'chug', 'demo', %s, TRUE)",
                (season, rid, max(1, week - 1), now),
            )
            made["tokens"] += 1

    # Two curses, stamped with the target's frozen projection if the week has
    # been locked -- exactly what cast_curse would have done.
    for caster, target in ((order[0], order[-1]), (order[1], order[-2])):
        tw = db.execute(
            "SELECT proj_total, locked_at FROM team_weeks "
            "WHERE season = %s AND week = %s AND roster_id = %s",
            (season, week, target),
        ).fetchone()
        db.execute(
            "INSERT INTO curses (season, week, caster_roster_id, target_roster_id, "
            "threshold_proj, status, created_at, is_demo) "
            "VALUES (%s, %s, %s, %s, %s, 'cast', %s, TRUE)",
            (season, week, caster, target,
             (tw or {}).get("proj_total") if (tw or {}).get("locked_at") else None, now),
        )
        made["curses"] += 1

    db.execute(
        "INSERT INTO blessings (season, roster_id, earned_week, expires_after, status, "
        "created_at, is_demo) VALUES (%s, %s, %s, %s, 'active', %s, TRUE)",
        (season, order[2], max(1, week - 1), week, now),
    )
    made["blessings"] += 1

    # ---------------------------------------------------------------- wrath
    # The whole Goosifer lifecycle, laid out so all three states are on screen
    # at once and can be confirmed by looking rather than by reading SQL:
    #
    #   order[-1]  MARKED AND CURSED RIGHT NOW. It is already one of the two
    #              curse targets above, so this is the live drama: a marked
    #              owner with a curse in the air, worth double to whoever cast
    #              it. Sorts to the top of the Curse board.
    #   order[-2]  ALREADY PAID. A consumed mark from last week plus the two
    #              doubled chugs it produced, still owed -- so Admin -> Chugs,
    #              My Geese and the "wraths paid" count on Standings all have
    #              something real in them, and confirming either chug can be
    #              seen minting nothing.
    #   order[0]   SURVIVED one. Carries a curse_survived token, which is the
    #              other half of the change and invisible otherwise.
    #
    # earned_week has to be strictly BEFORE the demo week or active_wrath will
    # not find these at all -- that `earned_week < week` test is the rule that
    # stops a miss doubling the very curse that caused it, and the demo has to
    # respect it like anything else.
    #
    # Which means a week 1 demo needs a mark earned in "week 0". That is the one
    # honest bit of fiction here: there is no week 0, and the demo is replaying a
    # week that has not happened either. Admin renders anything below week 1 as
    # "preseason" rather than a nonsense number. The doubled chugs it produced
    # sit on the CURRENT week, where they read naturally as debt still owed.
    prev = week - 1
    mult = max(1, settingsmod.get_int(db, "wrath_multiplier", 2) or 1)

    db.execute(
        "INSERT INTO wraths (season, roster_id, earned_week, expires_after, status, "
        "created_at, is_demo) VALUES (%s, %s, %s, %s, 'active', %s, TRUE)",
        (season, order[-1], prev, week, now),
    )
    made["wraths"] += 1

    # The already-paid owner needs to be somebody else, which needs a fifth
    # roster. Below that the demo simply skips this prop rather than stacking
    # two contradictory states on one owner.
    payer = order[-2] if len(order) >= 5 and order[-2] != order[2] else None
    if payer is not None:
        cur = db.execute(
            "INSERT INTO wraths (season, roster_id, earned_week, expires_after, status, "
            "created_at, resolved_at, is_demo) "
            "VALUES (%s, %s, %s, %s, 'consumed', %s, %s, TRUE) RETURNING id",
            (season, payer, prev - 1, prev, now, now),
        )
        wid = cur.fetchone()["id"]
        made["wraths"] += 1
        for _ in range(mult):
            db.execute(
                "INSERT INTO chugs (season, week, roster_id, reason, status, "
                "mints_tokens, wrath_id, created_at, is_demo) "
                "VALUES (%s, %s, %s, 'curse', 'owed', FALSE, %s, %s, TRUE)",
                (season, week, payer, wid, now),
            )
            made["chugs"] += 1

    db.execute(
        "INSERT INTO curse_tokens (season, roster_id, earned_week, source, note, "
        "created_at, is_demo) VALUES (%s, %s, %s, 'curse_survived', 'demo', %s, TRUE)",
        (season, order[0], max(1, prev), now),
    )
    made["tokens"] += 1

    for rid in order[3:5]:
        db.execute(
            "INSERT INTO chugs (season, week, roster_id, reason, status, created_at, is_demo) "
            "VALUES (%s, %s, %s, 'goose', 'owed', %s, TRUE)",
            (season, max(1, week - 1), rid, now),
        )
        made["chugs"] += 1

    db.commit()
    return {"ok": True, **made}


def clear_props(db) -> dict:
    """
    Delete every demo row, in an order that leaves no orphans: curses before
    the tokens that paid for them, so nothing points at a row that is gone.
    """
    global _parsed
    _parsed = None
    removed = {}
    for table in ("curses", "blessings", "wraths", "chugs", "curse_tokens"):
        removed[table] = db.execute(f"DELETE FROM {table} WHERE is_demo").rowcount
    db.commit()
    return removed
