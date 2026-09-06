"""
week_engine.py
==============
The week's life cycle. Everything that changes state lives here so the Flask
layer stays thin and this stays testable.

    upcoming -> open -> locked -> final
      admin    admin   kickoff   week over

open_week(week)
    Admin action. Until a week is open, nobody can cast a curse. Deliberate:
    projections post before lineups are set, and the FAAB app needed the same
    guardrail after people bet into matchups that were not real yet.
    Grants the weekly stipend, if the commissioner has turned one on.

lock_week(week)
    Fires at the first Sunday kickoff. Snapshots every starting lineup and
    every projection into lineup_slots / team_weeks, and stamps each open
    curse with its target's frozen projection.

    THE SNAPSHOT IS THE WHOLE GAME. If the number a curse is graded against can
    move after the curse was cast, the curse was never real. This is the same
    lesson the FAAB app learned with bet lines, and the reason threshold_proj
    is a column rather than a live lookup.

settle_week(week)
    Fires once the last game is over. Detects gooses, raises chugs, resolves
    every curse, expires stale blessings.

    Ordering inside settle matters and is not arbitrary:
      1. gooses      -- facts first
      2. curses      -- blessings block, then the frozen threshold decides
      3. chugs       -- raised from both, then capped and rolled
      4. blessings   -- expire what went unused
    Curses are resolved before chugs are capped so that a curse chug is subject
    to the same weekly cap as a goose chug.

Tokens are NOT minted here. The rule is one token per chug you actually
*perform*, so minting happens when an admin confirms the chug (confirm_chug
below), not when the chug is raised. You earn the curse by drinking, not by
owing.
"""
from __future__ import annotations

import time

import goose
import settings as settingsmod
import sleeper


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def now() -> int:
    return int(time.time())


def get_week(db, season: int, week: int):
    return db.execute(
        "SELECT * FROM weeks WHERE season = %s AND week = %s", (season, week)
    ).fetchone()


def ensure_week(db, season: int, week: int):
    db.execute(
        "INSERT INTO weeks (season, week, status) VALUES (%s, %s, 'upcoming') "
        "ON CONFLICT (season, week) DO NOTHING",
        (season, week),
    )
    return get_week(db, season, week)


def roster_ids(db, league_id: str, season: int) -> list[int]:
    rows = db.execute(
        "SELECT roster_id FROM owners WHERE league_id = %s AND season = %s ORDER BY roster_id",
        (league_id, season),
    ).fetchall()
    return [r["roster_id"] for r in rows]


def unspent_tokens(db, season: int, roster_id: int) -> list:
    return db.execute(
        "SELECT * FROM curse_tokens WHERE season = %s AND roster_id = %s "
        "AND spent_on IS NULL ORDER BY id",
        (season, roster_id),
    ).fetchall()


def active_blessing(db, season: int, roster_id: int, week: int):
    """A blessing protects the week AFTER it was earned, and only that week."""
    return db.execute(
        "SELECT * FROM blessings WHERE season = %s AND roster_id = %s "
        "AND status = 'active' AND expires_after >= %s ORDER BY id LIMIT 1",
        (season, roster_id, week),
    ).fetchone()


def mint_token(db, season: int, roster_id: int, week: int, source: str, note: str = None) -> int:
    cur = db.execute(
        "INSERT INTO curse_tokens (season, roster_id, earned_week, source, note, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (season, roster_id, week, source, note, now()),
    )
    return cur.fetchone()["id"]


# --------------------------------------------------------------------------
# open
# --------------------------------------------------------------------------

def open_week(db, league_id: str, season: int, week: int, admin_roster_id: int = None) -> dict:
    ensure_week(db, season, week)
    row = get_week(db, season, week)
    if row["status"] not in ("upcoming", "open"):
        return {"ok": False, "reason": f"week {week} is already {row['status']}"}

    db.execute(
        "UPDATE weeks SET status = 'open', opened_by = %s, opened_at = %s, "
        "lock_epoch = COALESCE(lock_epoch, %s), end_epoch = COALESCE(end_epoch, %s) "
        "WHERE season = %s AND week = %s",
        (
            admin_roster_id, now(),
            sleeper.week_lock_epoch(season, week),
            sleeper.week_end_epoch(season, week),
            season, week,
        ),
    )

    stipend = settingsmod.get_int(db, "weekly_stipend", 0) or 0
    granted = 0
    if stipend > 0:
        for rid in roster_ids(db, league_id, season):
            already = db.execute(
                "SELECT COUNT(*) AS n FROM curse_tokens WHERE season = %s AND roster_id = %s "
                "AND earned_week = %s AND source = 'stipend'",
                (season, rid, week),
            ).fetchone()["n"]
            for _ in range(max(0, stipend - already)):
                mint_token(db, season, rid, week, "stipend", f"weekly stipend, week {week}")
                granted += 1
    db.commit()
    return {"ok": True, "week": week, "stipend_tokens": granted}


# --------------------------------------------------------------------------
# lock
# --------------------------------------------------------------------------

def lock_week(db, league_id: str, season: int, week: int, force: bool = False) -> dict:
    row = ensure_week(db, season, week)
    if row["status"] == "locked" and not force:
        return {"ok": False, "reason": f"week {week} is already locked"}
    if row["status"] == "final":
        return {"ok": False, "reason": f"week {week} is already settled"}

    slots = sleeper.starting_slots(league_id)
    scoring = sleeper.scoring_settings(league_id)
    matchups = sleeper.matchups(league_id, week)
    projections = sleeper.projections(season, week)
    byes = sleeper.bye_teams(season, week)

    players = {
        r["player_id"]: r for r in db.execute(
            "SELECT player_id, position, team, injury_status FROM players_cache"
        ).fetchall()
    }

    stamped = 0
    for m in matchups:
        rid = m.get("roster_id")
        starters = m.get("starters") or []
        probs, proj_total = [], 0.0

        for i, pid in enumerate(starters):
            pid = None if goose.is_empty_slot(pid) else str(pid)
            meta = players.get(pid) or {}
            proj = (
                sleeper.player_points(projections.get(pid, {}), scoring)
                if pid else 0.0
            )
            p = goose.player_goose_probability(
                player_id=pid,
                position=meta.get("position"),
                projection=proj if pid else None,
                injury_status=meta.get("injury_status"),
                on_bye=bool(meta.get("team") and meta["team"].upper() in byes),
            )
            probs.append(p)
            proj_total += proj

            db.execute(
                """
                INSERT INTO lineup_slots
                    (season, week, roster_id, slot_index, slot, player_id, proj_pts, goose_prob)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (season, week, roster_id, slot_index) DO UPDATE SET
                    slot = EXCLUDED.slot, player_id = EXCLUDED.player_id,
                    proj_pts = EXCLUDED.proj_pts, goose_prob = EXCLUDED.goose_prob
                """,
                (season, week, rid, i, slots[i] if i < len(slots) else f"S{i+1}",
                 pid, round(proj, 2), round(p, 4)),
            )

        db.execute(
            """
            INSERT INTO team_weeks (season, week, roster_id, proj_total, chug_odds, locked_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (season, week, roster_id) DO UPDATE SET
                proj_total = EXCLUDED.proj_total,
                chug_odds  = EXCLUDED.chug_odds,
                locked_at  = EXCLUDED.locked_at
            """,
            (season, week, rid, round(proj_total, 2),
             round(goose.team_chug_odds(probs), 4), now()),
        )
        stamped += 1

    # Freeze each open curse against its target's projection. After this line
    # the number cannot move, whatever Sleeper does to its projections later.
    # No alias on the UPDATE target: Postgres allows `UPDATE curses c ...`,
    # SQLite does not, and the engine tests run this exact statement.
    cur = db.execute(
        """
        UPDATE curses SET threshold_proj = t.proj_total
        FROM team_weeks t
        WHERE curses.season = t.season AND curses.week = t.week
          AND curses.target_roster_id = t.roster_id
          AND curses.season = %s AND curses.week = %s AND curses.status = 'cast'
        """,
        (season, week),
    )
    frozen = cur.rowcount

    db.execute(
        "UPDATE weeks SET status = 'locked', locked_at = %s WHERE season = %s AND week = %s",
        (now(), season, week),
    )
    db.commit()
    return {"ok": True, "week": week, "lineups": stamped, "curses_frozen": frozen}


# --------------------------------------------------------------------------
# settle
# --------------------------------------------------------------------------

def raise_chug(db, season: int, week: int, roster_id: int, reason: str, ref_id=None) -> int:
    cur = db.execute(
        "INSERT INTO chugs (season, week, roster_id, reason, ref_id, status, created_at) "
        "VALUES (%s, %s, %s, %s, %s, 'owed', %s) RETURNING id",
        (season, week, roster_id, reason, ref_id, now()),
    )
    return cur.fetchone()["id"]


def apply_weekly_cap(db, season: int, week: int) -> int:
    """
    Nobody owes more than max_chugs_per_week for a single week. The overflow is
    not forgiven -- it moves to the next week and keeps aging, so a bad week
    becomes a debt you carry rather than five beers in one sitting.
    """
    cap = settingsmod.get_int(db, "max_chugs_per_week", 3) or 3
    if cap <= 0:
        return 0
    rolled = 0
    rows = db.execute(
        "SELECT roster_id, COUNT(*) AS n FROM chugs "
        "WHERE season = %s AND week = %s AND status = 'owed' GROUP BY roster_id HAVING COUNT(*) > %s",
        (season, week, cap),
    ).fetchall()
    for r in rows:
        # The skip is done in Python rather than with OFFSET: Postgres accepts
        # a bare OFFSET but SQLite requires a LIMIT first, and the engine tests
        # run these statements verbatim. Not worth a dialect split for a list
        # that is never more than a lineup long.
        owed = db.execute(
            "SELECT id FROM chugs WHERE season = %s AND week = %s AND roster_id = %s "
            "AND status = 'owed' ORDER BY id",
            (season, week, r["roster_id"]),
        ).fetchall()
        for c in owed[cap:]:
            db.execute(
                "UPDATE chugs SET week = %s, rolled_from_week = COALESCE(rolled_from_week, %s) "
                "WHERE id = %s",
                (week + 1, week, c["id"]),
            )
            rolled += 1
    return rolled


def settle_week(db, league_id: str, season: int, week: int, force: bool = False) -> dict:
    row = ensure_week(db, season, week)
    if row["status"] == "final" and not force:
        return {"ok": False, "reason": f"week {week} is already settled"}
    if row["status"] == "upcoming":
        return {"ok": False, "reason": f"week {week} was never opened"}

    slots = sleeper.starting_slots(league_id)
    matchups = sleeper.matchups(league_id, week)

    goose_count = 0
    for m in matchups:
        rid = m.get("roster_id")
        starters = m.get("starters") or []
        points = m.get("starters_points") or []

        for i, pid in enumerate(starters):
            pts = points[i] if i < len(points) else None
            db.execute(
                "UPDATE lineup_slots SET actual_pts = %s, is_goose = %s "
                "WHERE season = %s AND week = %s AND roster_id = %s AND slot_index = %s",
                (pts, goose.is_goose(pts, pid), season, week, rid, i),
            )

        db.execute(
            "UPDATE team_weeks SET actual_total = %s WHERE season = %s AND week = %s AND roster_id = %s",
            (m.get("points"), season, week, rid),
        )

        for g in goose.find_gooses(starters, points, slots):
            cur = db.execute(
                """
                INSERT INTO gooses
                    (season, week, roster_id, player_id, slot, slot_index, points, empty_slot, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (season, week, roster_id, slot_index) DO NOTHING
                RETURNING id
                """,
                (season, week, rid, g["player_id"], g["slot"], g["slot_index"],
                 g["points"], g["empty_slot"], now()),
            )
            new = cur.fetchone()
            if new:
                raise_chug(db, season, week, rid, "goose", new["id"])
                goose_count += 1

        db.execute(
            "UPDATE team_weeks SET goose_count = (SELECT COUNT(*) FROM gooses "
            "WHERE season = %s AND week = %s AND roster_id = %s) "
            "WHERE season = %s AND week = %s AND roster_id = %s",
            (season, week, rid, season, week, rid),
        )

    resolved = resolve_curses(db, season, week)
    rolled = apply_weekly_cap(db, season, week)
    expired = expire_blessings(db, season, week)

    db.execute(
        "UPDATE weeks SET status = 'final', settled_at = %s WHERE season = %s AND week = %s",
        (now(), season, week),
    )
    db.commit()
    return {
        "ok": True, "week": week, "gooses": goose_count,
        "chugs_rolled": rolled, "blessings_expired": expired, **resolved,
    }


def resolve_curses(db, season: int, week: int) -> dict:
    """
    A cast curse ends one of three ways:
      blocked  -- the target held a blessing. It fires by itself and is spent.
      landed   -- the target finished under the frozen projection. They chug,
                  and the caster may mint a token for it.
      survived -- the target beat it and earns a blessing for next week.

    A curse with no threshold (the week was never locked, so nothing was
    frozen) is left alone rather than graded against a number that does not
    exist -- failing safe means the curse simply does not resolve, and an
    admin can void it.
    """
    mints = settingsmod.get_bool(db, "landed_curse_mints_point", True)
    out = {"curses_blocked": 0, "curses_landed": 0, "curses_survived": 0, "curses_unresolved": 0}

    curses = db.execute(
        "SELECT * FROM curses WHERE season = %s AND week = %s AND status = 'cast' ORDER BY id",
        (season, week),
    ).fetchall()

    for c in curses:
        target = c["target_roster_id"]

        blessing = active_blessing(db, season, target, week)
        if blessing:
            db.execute(
                "UPDATE blessings SET status = 'consumed', consumed_by = %s, resolved_at = %s WHERE id = %s",
                (c["id"], now(), blessing["id"]),
            )
            db.execute(
                "UPDATE curses SET status = 'blocked', blocked_by = %s, resolved_at = %s WHERE id = %s",
                (blessing["id"], now(), c["id"]),
            )
            out["curses_blocked"] += 1
            continue

        if c["threshold_proj"] is None:
            out["curses_unresolved"] += 1
            continue

        tw = db.execute(
            "SELECT actual_total FROM team_weeks WHERE season = %s AND week = %s AND roster_id = %s",
            (season, week, target),
        ).fetchone()
        actual = (tw or {}).get("actual_total")
        if actual is None:
            out["curses_unresolved"] += 1
            continue

        if float(actual) < float(c["threshold_proj"]):
            db.execute(
                "UPDATE curses SET status = 'landed', actual_total = %s, resolved_at = %s WHERE id = %s",
                (actual, now(), c["id"]),
            )
            raise_chug(db, season, week, target, "curse", c["id"])
            if mints:
                mint_token(db, season, c["caster_roster_id"], week, "curse_landed",
                           f"curse landed on roster {target}, week {week}")
            out["curses_landed"] += 1
        else:
            db.execute(
                "UPDATE curses SET status = 'survived', actual_total = %s, resolved_at = %s WHERE id = %s",
                (actual, now(), c["id"]),
            )
            db.execute(
                "INSERT INTO blessings (season, roster_id, earned_week, expires_after, status, created_at) "
                "VALUES (%s, %s, %s, %s, 'active', %s)",
                (season, target, week, week + 1, now()),
            )
            out["curses_survived"] += 1
    return out


def expire_blessings(db, season: int, week: int) -> int:
    """Blessings do not bank. Anything whose window has closed is gone."""
    cur = db.execute(
        "UPDATE blessings SET status = 'expired', resolved_at = %s "
        "WHERE season = %s AND status = 'active' AND expires_after < %s",
        (now(), season, week),
    )
    return cur.rowcount


# --------------------------------------------------------------------------
# player actions
# --------------------------------------------------------------------------

def cast_curse(db, season: int, week: int, caster: int, target: int) -> dict:
    """Spend one token on another owner. Refuses rather than half-succeeding."""
    if caster == target:
        return {"ok": False, "reason": "You cannot curse yourself."}

    wk = get_week(db, season, week)
    if wk is None or wk["status"] != "open":
        return {"ok": False, "reason": f"Week {week} is not open for curses."}

    tokens = unspent_tokens(db, season, caster)
    if not tokens:
        return {"ok": False, "reason": "You have no curse tokens."}

    limit = settingsmod.get_int(db, "max_curses_per_target", 1) or 1
    existing = db.execute(
        "SELECT COUNT(*) AS n FROM curses WHERE season = %s AND week = %s "
        "AND target_roster_id = %s AND status = 'cast'",
        (season, week, target),
    ).fetchone()["n"]
    if existing >= limit:
        return {"ok": False, "reason": f"That owner already has {existing} curse(s) this week."}

    if not settingsmod.get_bool(db, "curse_stacking", False):
        mine = db.execute(
            "SELECT COUNT(*) AS n FROM curses WHERE season = %s AND week = %s "
            "AND caster_roster_id = %s AND target_roster_id = %s AND status = 'cast'",
            (season, week, caster, target),
        ).fetchone()["n"]
        if mine:
            return {"ok": False, "reason": "You have already cursed that owner this week."}

    token = tokens[0]
    cur = db.execute(
        "INSERT INTO curses (season, week, caster_roster_id, target_roster_id, token_id, status, created_at) "
        "VALUES (%s, %s, %s, %s, %s, 'cast', %s) RETURNING id",
        (season, week, caster, target, token["id"], now()),
    )
    curse_id = cur.fetchone()["id"]
    db.execute("UPDATE curse_tokens SET spent_on = %s WHERE id = %s", (curse_id, token["id"]))
    db.commit()
    return {"ok": True, "curse_id": curse_id}


def cancel_curse(db, season: int, curse_id: int, caster: int) -> dict:
    """Take it back while the week is still open. The token comes back too."""
    c = db.execute(
        "SELECT * FROM curses WHERE id = %s AND season = %s", (curse_id, season)
    ).fetchone()
    if c is None or c["caster_roster_id"] != caster:
        return {"ok": False, "reason": "Not your curse."}
    if c["status"] != "cast":
        return {"ok": False, "reason": "That curse has already resolved."}
    wk = get_week(db, season, c["week"])
    if wk is None or wk["status"] != "open":
        return {"ok": False, "reason": "Too late — the week is locked."}

    db.execute("UPDATE curse_tokens SET spent_on = NULL WHERE id = %s", (c["token_id"],))
    db.execute("DELETE FROM curses WHERE id = %s", (curse_id,))
    db.commit()
    return {"ok": True}


def confirm_chug(db, season: int, chug_id: int, admin_roster_id: int, safe_pour: bool = False) -> dict:
    """
    Admin marks a chug performed. THIS is where a curse token is minted -- the
    rule is one token per chug you actually do, so owing a chug earns nothing.
    """
    c = db.execute("SELECT * FROM chugs WHERE id = %s AND season = %s", (chug_id, season)).fetchone()
    if c is None:
        return {"ok": False, "reason": "No such chug."}
    if c["status"] == "paid":
        return {"ok": False, "reason": "Already confirmed."}
    if safe_pour and not settingsmod.get_bool(db, "safe_pour_allowed", True):
        return {"ok": False, "reason": "Safe pour is switched off for this league."}

    db.execute(
        "UPDATE chugs SET status = 'paid', safe_pour = %s, confirmed_by = %s, confirmed_at = %s WHERE id = %s",
        (safe_pour, admin_roster_id, now(), chug_id),
    )
    per = settingsmod.get_int(db, "points_per_chug", 1) or 0
    for _ in range(max(0, per)):
        mint_token(db, season, c["roster_id"], c["week"], "chug", f"chug #{chug_id}")
    db.commit()
    return {"ok": True, "tokens_minted": max(0, per)}


def unconfirm_chug(db, season: int, chug_id: int) -> dict:
    """
    Undo a mistaken confirmation. Claws back an UNSPENT token that confirmation
    minted; a token already spent on a curse stays spent, because unwinding a
    cast curse retroactively would rewrite a week other people played.
    """
    c = db.execute("SELECT * FROM chugs WHERE id = %s AND season = %s", (chug_id, season)).fetchone()
    if c is None or c["status"] != "paid":
        return {"ok": False, "reason": "That chug is not confirmed."}
    cur = db.execute(
        "DELETE FROM curse_tokens WHERE id IN ("
        "  SELECT id FROM curse_tokens WHERE season = %s AND roster_id = %s "
        "  AND source = 'chug' AND note = %s AND spent_on IS NULL LIMIT %s)",
        (season, c["roster_id"], f"chug #{chug_id}",
         settingsmod.get_int(db, "points_per_chug", 1) or 0),
    )
    db.execute(
        "UPDATE chugs SET status = 'owed', confirmed_by = NULL, confirmed_at = NULL, "
        "safe_pour = FALSE WHERE id = %s",
        (chug_id,),
    )
    db.commit()
    return {"ok": True, "tokens_reclaimed": cur.rowcount}
