"""
lineups.py
==========
The starting lineup that opens under a team on the Curse board.

Sibling to `../faab-platform/lineups.py`, deliberately the same shape so the
two codebases stay readable side by side -- but this one is smaller, because
the Goose app already computes most of what that module had to invent.

What this adds over what the board already had
----------------------------------------------
The board knew a team's projected TOTAL and its risk tier. It could not show
you the eleven players that total is made of, and -- the part that actually
mattered on a Sunday -- it could not show you what any of them had SCORED.
`team_weeks.actual_total` is written by settle_week, once, after the last game
is over. So from kickoff until Sunday night the Curse board was showing a
column of nulls while Goose Watch, three feet away in the tab bar, had live
scores for the same players. This module closes that gap.

Frozen versus live, and why they are not the same question
----------------------------------------------------------
    PROJECTIONS  frozen the moment the week locks, and never recomputed. That
                 number is the bar a curse is graded against; if it can move
                 after the curse was cast, the curse was never real. Before
                 lock there is nothing to freeze, so the projection is live
                 and every screen showing it says so.

    ACTUALS      always live, always refetched, at every stage. There is no
                 such thing as a stale actual worth showing -- a score that
                 has moved is simply a newer fact about the same game.

That split is the whole design. `build()` takes the week row and reads the
projections from the snapshot or from Sleeper accordingly, but it reads the
actuals from Sleeper every single time either way.

How fresh "live" actually is
----------------------------
Sleeper's matchups response is cached in sleeper.py for `_TTL_MATCHUPS`
seconds (120 at the time of writing), so a page refresh gets scores at most
two minutes old, and hammering refresh does not hammer Sleeper. The game-state
feed behind the clock has its own TTL. If you want the board to be fresher
than that, lower the TTL -- do not add a second uncached fetch here.

This module reads. It never writes, never settles, never raises a chug and
never touches a curse. If it and settle_week ever disagree about whether
something is a goose, settle_week is right: it grades off final scores once
every game is genuinely over, and this is a view of a week in progress.
"""
from __future__ import annotations

import goose
import sleeper
import week_engine as engine


def _rows_from_snapshot(db, season: int, week: int) -> dict[int, list[dict]]:
    """The frozen lineup, as lock_week wrote it. Projections here never move."""
    out: dict[int, list[dict]] = {}
    rows = db.execute(
        "SELECT l.*, p.full_name, p.position, p.team, p.injury_status "
        "FROM lineup_slots l LEFT JOIN players_cache p ON p.player_id = l.player_id "
        "WHERE l.season = %s AND l.week = %s ORDER BY l.roster_id, l.slot_index",
        (season, week),
    ).fetchall()
    for r in rows:
        pid = r["player_id"]
        out.setdefault(r["roster_id"], []).append({
            "slot_index": r["slot_index"],
            "slot": r["slot"],
            "player_id": pid,
            "name": r["full_name"] or ("Empty slot" if pid is None else f"Player {pid}"),
            "position": r["position"],
            "nfl_team": (r["team"] or "").upper() or None,
            "injury_status": r["injury_status"],
            "projection": r["proj_pts"],
            "tier": r["tier"],
            "ratio": r["proj_ratio"],
            "reason": r["risk_reason"],
            "photo": (f"https://sleepercdn.com/content/nfl/players/thumb/{pid}.jpg"
                      if pid else None),
        })
    return out


def _rows_from_preview(db, league_id: str, season: int, week: int) -> dict[int, list[dict]]:
    """The live lineup, before there is anything frozen to read."""
    projected = engine.project_week(db, league_id, season, week)
    return {rid: list(b["slots"]) for rid, b in projected["rosters"].items()}


def build(db, league_id: str, season: int, week: int, wk_row) -> dict[int, dict]:
    """
    One panel per roster: `{roster_id: {starters, proj_total, actual_total, ...}}`.

    Never raises for a missing player, a missing game or a team on bye -- a
    lineup panel is a nicety and must not be able to take down the board.
    The caller is still expected to wrap it, because Sleeper being unreachable
    should cost you the panels and nothing else.
    """
    frozen = bool(wk_row and wk_row["status"] in ("locked", "final"))

    by_roster = _rows_from_snapshot(db, season, week) if frozen else {}
    if not by_roster:
        # A locked week with no snapshot is not supposed to happen, but a
        # blank board is a worse answer than a live one that says it moves.
        by_roster = _rows_from_preview(db, league_id, season, week)
        frozen = False

    # Actuals and the clock, live, every time. `starters_points` is the
    # league's OWN scoring applied by Sleeper, so it is the authoritative
    # actual rather than anything worth re-deriving here.
    points_by_roster: dict[int, list] = {}
    for m in sleeper.matchups(league_id, week):
        points_by_roster[m.get("roster_id")] = m.get("starters_points") or []
    games = sleeper.game_state_by_team(season, week)

    out: dict[int, dict] = {}
    for rid, slots in by_roster.items():
        pts = points_by_roster.get(rid, [])
        starters, proj_total, actual_total = [], 0.0, 0.0
        any_started = yet_to_play = live_geese = 0
        all_final = True

        for row in sorted(slots, key=lambda r: r["slot_index"]):
            pid = row.get("player_id")
            proj = float(row.get("projection") or 0.0)
            proj_total += proj

            game = games.get(row.get("nfl_team") or "") or {}
            state = game.get("state")
            empty = pid is None

            # An empty slot has no game and cannot be saved by one: it is a
            # goose the moment the lineup locks, so it counts as settled
            # rather than sitting in "yet to play" forever.
            if empty:
                started, final = True, True
            else:
                started = state in (sleeper.LIVE, sleeper.FINAL)
                final = state == sleeper.FINAL

            raw = pts[row["slot_index"]] if row["slot_index"] < len(pts) else None
            try:
                actual = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                actual = None

            # Before kickoff there is deliberately no number: Sleeper already
            # carries a 0.0 for an unplayed starter and showing it reads as
            # "played, scored nothing" rather than "hasn't played".
            live_actual = (actual if started else None)
            if empty:
                live_actual = 0.0

            if started:
                any_started += 1
                actual_total += (live_actual or 0.0)
            else:
                yet_to_play += 1
            if not final:
                all_final = False
            # Only a FINISHED game makes a goose. A zero in the first quarter
            # is Sunday happening -- the same distinction Goose Watch draws,
            # and getting it wrong here would put a panic count on the board
            # at 1:05pm every week.
            if final and goose.is_goose(live_actual, pid):
                live_geese += 1

            starters.append({
                **row,
                "proj": proj,
                "live_actual": live_actual,
                "delta": (None if live_actual is None else round(live_actual - proj, 2)),
                "state": state,
                "started": started,
                "final": final,
                "empty": empty,
                "is_goose": bool(final and goose.is_goose(live_actual, pid)),
                "clock": game.get("clock"),
                "opponent": game.get("opponent"),
                "venue": ("vs" if game.get("home") else "@") if game else None,
                "seconds_left": game.get("seconds_left"),
            })

        out[rid] = {
            "roster_id": rid,
            "starters": starters,
            "frozen": frozen,
            "proj_total": round(proj_total, 2),
            "actual_total": round(actual_total, 2) if any_started else None,
            "any_started": bool(any_started),
            "all_final": bool(any_started) and all_final,
            "yet_to_play": yet_to_play,
            "live_geese": live_geese,
        }
    return out
