"""
watch.py
========
Goose Watch: the live Sunday board.

The one screen people will actually leave open. It answers, at a glance, "who
is drinking tonight" and "who is one bad quarter away from it".

Four states per starter, and the distinction between them is the whole point:

    goosed   the player's game is FINAL and he finished on <= 0. Settled.
    danger   his game is in the 4th quarter (or overtime) and he is on <= 0
             right now. Genuinely running out of snaps to fix it.
    pending  his game has not kicked off, or is live but still in Q1-Q3. A
             zero in the first three quarters is completely normal and not
             worth a panic banner -- there is a full quarter of offense left.
    safe     he has points on the board.

Conflating `pending` with `danger` would put every owner at maximum panic at
9am Sunday (or at 1:05pm the moment kickoff happens), and conflating it with
`goosed` would declare a bye-week player drunk on Thursday. So a team with no
game in the feed is `pending`, never `final` -- see the warning in
sleeper.game_state_by_team. The same reasoning is why a Q1 zero is `pending`
too: it hasn't earned "danger" yet.

This module reads. It never writes: nothing here settles a week, raises a chug
or resolves a curse. Sunday's screen is a view of a week that settle_week
grades later, off final scores, once every game is genuinely over. If this
module and settle_week ever disagree, settle_week is right.
"""
from __future__ import annotations

import goose
import sleeper

GOOSED, DANGER, PENDING, SAFE = "goosed", "danger", "pending", "safe"

# A starter is worth calling out as "escaped" only if the model actually
# feared him. Everyone else scoring points is just Sunday happening.
CLEARED_THRESHOLD = 0.10


# A live zero only counts as "danger" from the 4th quarter on -- see the
# module docstring. Anything before that is just Sunday happening.
DANGER_FROM_QUARTER = 4


def _classify(points, player_id, game_state: str | None, quarter: int | None) -> str:
    if goose.is_empty_slot(player_id):
        return GOOSED                      # nothing was started; no game can save it
    if points is None:
        return PENDING
    try:
        scored = float(points)
    except (TypeError, ValueError):
        return PENDING
    if scored > 0:
        return SAFE
    if game_state == sleeper.FINAL:
        return GOOSED
    if game_state == sleeper.LIVE and quarter is not None and quarter >= DANGER_FROM_QUARTER:
        return DANGER
    return PENDING


def build(db, league_id: str, season: int, week: int) -> dict:
    """
    Assemble the whole screen in one pass. Returns plain dicts so the template
    stays dumb and the tests stay readable.
    """
    slots = sleeper.starting_slots(league_id)
    matchups = sleeper.matchups(league_id, week)
    games = sleeper.game_state_by_team(season, week)

    players = {
        r["player_id"]: r for r in db.execute(
            "SELECT player_id, full_name, position, team FROM players_cache"
        ).fetchall()
    }
    owners = {
        r["roster_id"]: r for r in db.execute(
            "SELECT roster_id, owner_name, team_name, avatar FROM owners "
            "WHERE league_id = %s AND season = %s",
            (league_id, season),
        ).fetchall()
    }
    snapshot = {
        (r["roster_id"], r["slot_index"]): r for r in db.execute(
            "SELECT roster_id, slot_index, slot, goose_prob, proj_pts FROM lineup_slots "
            "WHERE season = %s AND week = %s",
            (season, week),
        ).fetchall()
    }

    rows: list[dict] = []
    per_owner: dict = {}

    for m in matchups:
        rid = m.get("roster_id")
        owner = owners.get(rid) or {}
        team_label = owner.get("team_name") or owner.get("owner_name") or f"Roster {rid}"
        starters = m.get("starters") or []
        points = m.get("starters_points") or []
        tally = {GOOSED: 0, DANGER: 0, PENDING: 0, SAFE: 0}

        for i, pid in enumerate(starters):
            pid = None if goose.is_empty_slot(pid) else str(pid)
            pts = points[i] if i < len(points) else None
            meta = players.get(pid) or {}
            nfl_team = (meta.get("team") or "").upper()
            game = games.get(nfl_team)
            state = _classify(pts, pid, (game or {}).get("state"), (game or {}).get("quarter"))
            tally[state] += 1

            snap = snapshot.get((rid, i)) or {}
            rows.append({
                "roster_id": rid,
                "owner": team_label,
                "avatar": owner.get("avatar"),
                "slot": snap.get("slot") or (slots[i] if i < len(slots) else f"S{i+1}"),
                "player_id": pid,
                "name": meta.get("full_name") or ("Empty slot" if pid is None else f"Player {pid}"),
                # Undocumented but long-standing Sleeper CDN convention -- same
                # /content/nfl/players/ path the Sleeper app itself uses for
                # roster headshots. Missing/practice-squad players 404; the
                # template hides a broken image rather than showing it.
                "photo": f"https://sleepercdn.com/content/nfl/players/thumb/{pid}.jpg" if pid else None,
                "position": meta.get("position"),
                "nfl_team": nfl_team or None,
                "opponent": (game or {}).get("opponent"),
                "points": pts,
                "state": state,
                "clock": (game or {}).get("clock") or ("no game" if pid else "empty slot"),
                "goose_prob": snap.get("goose_prob"),
                "empty_slot": pid is None,
            })

        per_owner[rid] = {
            "roster_id": rid,
            "owner": team_label,
            "avatar": owner.get("avatar"),
            "goosed": tally[GOOSED],
            "danger": tally[DANGER],
            "pending": tally[PENDING],
            "safe": tally[SAFE],
        }

    goosed = [r for r in rows if r["state"] == GOOSED]
    danger = [r for r in rows if r["state"] == DANGER]
    pending = [r for r in rows if r["state"] == PENDING]
    cleared = [
        r for r in rows
        if r["state"] == SAFE and (r["goose_prob"] or 0) >= CLEARED_THRESHOLD
    ]

    danger.sort(key=lambda r: -(r["goose_prob"] or 0))
    pending.sort(key=lambda r: -(r["goose_prob"] or 0))
    cleared.sort(key=lambda r: -(r["goose_prob"] or 0))
    goosed.sort(key=lambda r: (r["owner"], r["slot"]))

    drinkers = sorted(
        [o for o in per_owner.values() if o["goosed"]],
        key=lambda o: -o["goosed"],
    )

    states = [g["state"] for g in games.values()]
    return {
        "week": week,
        "rows": rows,
        "goosed": goosed,
        "danger": danger,
        "pending": pending,
        "cleared": cleared,
        "drinkers": drinkers,
        "per_owner": sorted(per_owner.values(), key=lambda o: (-o["goosed"], -o["danger"])),
        "total_goosed": len(goosed),
        "total_danger": len(danger),
        "games_live": states.count(sleeper.LIVE),
        "games_final": states.count(sleeper.FINAL),
        "games_total": len(games) // 2 if games else 0,
        "feed_ok": bool(games),
    }
