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
      5. wraths      -- expire what went uncashed
    Curses are resolved before chugs are capped so that a curse chug is subject
    to the same weekly cap as a goose chug -- including a doubled one, which is
    two real rows and therefore eats two of the cap.

Goosifer's Wrath
----------------
A curse that LANDS marks its target. The mark is read the following week and
only the following week (or until they survive one, if the commissioner has
turned persistence on): a curse that lands on a marked owner costs them
wrath_multiplier chugs instead of one, and those chugs mint no tokens at all.

Two rules keep it honest and both live in resolve_curses:
  - a mark is read with `earned_week < week`, so the miss that arms a mark can
    never also be doubled by it. You get the week in between to fix your team.
  - it never stacks past the multiplier. Landing on a marked owner consumes
    the mark and arms exactly one fresh one, so two bad weeks costs double and
    five bad weeks still costs double.

Tokens are NOT minted here, with one exception. The rule is one token per chug
you actually *perform*, so minting happens when an admin confirms the chug
(confirm_chug below), not when the chug is raised. You earn the curse by
drinking, not by owing.

The exception is surviving a curse. There is no chug to confirm -- the owner
did the hard thing and beat the bar with Goothulu on them -- so that token is
minted at settle, alongside the blessing.
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


def active_wrath(db, season: int, roster_id: int, week: int):
    """
    The mark covering THIS week, if there is one.

    `earned_week < week` is load-bearing: settle arms a mark in the same pass
    that resolves curses, and without it the very curse that armed the mark
    would be doubled by it. The mark is a warning about next week, not a
    surcharge on this one.

    A NULL expires_after means the commissioner turned on wrath_persists when
    this mark was written: it does not age out, it waits to be survived.
    """
    return db.execute(
        "SELECT * FROM wraths WHERE season = %s AND roster_id = %s AND status = 'active' "
        "AND earned_week < %s AND (expires_after IS NULL OR expires_after >= %s) "
        "ORDER BY id LIMIT 1",
        (season, roster_id, week, week),
    ).fetchone()


def arm_wrath(db, season: int, roster_id: int, week: int, curse_id=None,
              by_admin: bool = False) -> int | None:
    """
    Mark an owner for next week. Returns the new row id, or None if they are
    already marked from this same week -- two curses landing on one owner in
    one week is still one mark, because the mark is about the NEXT week and
    there is only one of those.
    """
    already = db.execute(
        "SELECT id FROM wraths WHERE season = %s AND roster_id = %s AND status = 'active' "
        "AND earned_week = %s LIMIT 1",
        (season, roster_id, week),
    ).fetchone()
    if already:
        return None
    persists = settingsmod.get_bool(db, "wrath_persists", False)
    cur = db.execute(
        "INSERT INTO wraths (season, roster_id, earned_week, expires_after, status, "
        "caused_by, created_by_admin, created_at) "
        "VALUES (%s, %s, %s, %s, 'active', %s, %s, %s) RETURNING id",
        (season, roster_id, week, None if persists else week + 1,
         curse_id, by_admin, now()),
    )
    return cur.fetchone()["id"]


def lift_wraths(db, season: int, roster_id: int, curse_id=None) -> int:
    """
    Burn the mark off. Surviving a curse does this -- you proved the point, so
    you are not still carrying the last miss. Clears persistent marks too,
    which is the only way one of those ever ends.
    """
    cur = db.execute(
        "UPDATE wraths SET status = 'lifted', resolved_by = %s, resolved_at = %s "
        "WHERE season = %s AND roster_id = %s AND status = 'active'",
        (curse_id, now(), season, roster_id),
    )
    return cur.rowcount


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
# projection -- shared by the live preview and by the lock snapshot
# --------------------------------------------------------------------------

def project_week(db, league_id: str, season: int, week: int) -> dict:
    """
    Compute every lineup's projection and risk tier for a week. READ ONLY --
    it touches Sleeper and players_cache and writes nothing.

    This exists so that one piece of arithmetic serves two jobs that used to be
    welded together inside lock_week. lock_week now calls this and PERSISTS the
    answer; the Board and My Geese call it and DISPLAY the answer before a week
    locks. Before v0.5 there was no second caller, which is why the board was
    blank until Sunday kickoff -- there was nothing wrong with the numbers, they
    simply did not exist until the moment they were frozen.

    Anything rendered from this is a moving number and must be labelled as one.
    The frozen snapshot in lineup_slots / team_weeks is the only version a
    curse is ever graded against.
    """
    slots = sleeper.starting_slots(league_id)
    scoring = sleeper.scoring_settings(league_id)
    matchups = sleeper.matchups(league_id, week)
    projections = sleeper.projections(season, week)
    byes = sleeper.bye_teams(season, week)

    players = {
        r["player_id"]: r for r in db.execute(
            "SELECT player_id, full_name, position, team, injury_status FROM players_cache"
        ).fetchall()
    }

    # Pass one: every started slot in the league, so the position averages are
    # the LEAGUE'S OWN bar for the week rather than a number from a table.
    # This has to happen before any tier is assigned -- the bar is the divisor.
    raw: list[dict] = []
    for m in matchups:
        rid = m.get("roster_id")
        for i, pid in enumerate(m.get("starters") or []):
            pid = None if goose.is_empty_slot(pid) else str(pid)
            meta = players.get(pid) or {}
            proj = (
                sleeper.player_points(projections.get(pid, {}), scoring)
                if pid else None
            )
            team = (meta.get("team") or "").upper()
            raw.append({
                "roster_id": rid,
                "slot_index": i,
                "slot": slots[i] if i < len(slots) else f"S{i + 1}",
                "player_id": pid,
                "name": meta.get("full_name") or ("Empty slot" if pid is None else f"Player {pid}"),
                "position": meta.get("position"),
                "nfl_team": team or None,
                "injury_status": meta.get("injury_status"),
                "projection": proj,
                "on_bye": bool(team and team in byes),
                # Same undocumented Sleeper CDN path the app itself uses.
                # Missing players 404 and the template hides the broken image.
                "photo": f"https://sleepercdn.com/content/nfl/players/thumb/{pid}.jpg" if pid else None,
            })

    position_avg = goose.position_averages(raw)

    # Pass two: tier every slot against that bar.
    rosters: dict = {}
    for row in raw:
        risk = goose.player_risk(
            player_id=row["player_id"],
            position=row["position"],
            projection=row["projection"],
            position_avg=position_avg.get((row["position"] or "").upper()),
            injury_status=row["injury_status"],
            on_bye=row["on_bye"],
        )
        row.update(risk)
        bucket = rosters.setdefault(row["roster_id"], {"slots": [], "proj_total": 0.0})
        bucket["slots"].append(row)
        bucket["proj_total"] += float(row["projection"] or 0.0)

    for rid, bucket in rosters.items():
        bucket["slots"].sort(key=lambda r: r["slot_index"])
        bucket["roster_id"] = rid
        bucket["proj_total"] = round(bucket["proj_total"], 2)
        bucket["risk"] = goose.team_risk([r["tier"] for r in bucket["slots"]])

    return {
        "week": week,
        "position_avg": {k: round(v, 2) for k, v in position_avg.items()},
        "rosters": rosters,
        "expected_gooses": goose.expected_gooses(
            r["tier"] for b in rosters.values() for r in b["slots"]),
    }


# --------------------------------------------------------------------------
# lock
# --------------------------------------------------------------------------

def lock_week(db, league_id: str, season: int, week: int, force: bool = False) -> dict:
    row = ensure_week(db, season, week)
    if row["status"] == "locked" and not force:
        return {"ok": False, "reason": f"week {week} is already locked"}
    # A settled week refuses even a forced lock. Flipping it back to `locked`
    # while its chugs, tokens and blessings all still exist would leave the
    # status lying about a week people have already acted on. Reset is the way
    # back from settled, and it says so.
    if row["status"] == "final":
        return {"ok": False, "reason":
                f"week {week} is settled -- use Reset week to unwind it first"}

    projected = project_week(db, league_id, season, week)

    stamped = 0
    for rid, bucket in projected["rosters"].items():
        for row_ in bucket["slots"]:
            db.execute(
                """
                INSERT INTO lineup_slots
                    (season, week, roster_id, slot_index, slot, player_id,
                     proj_pts, goose_prob, tier, proj_ratio, risk_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (season, week, roster_id, slot_index) DO UPDATE SET
                    slot = EXCLUDED.slot, player_id = EXCLUDED.player_id,
                    proj_pts = EXCLUDED.proj_pts, goose_prob = EXCLUDED.goose_prob,
                    tier = EXCLUDED.tier, proj_ratio = EXCLUDED.proj_ratio,
                    risk_reason = EXCLUDED.risk_reason
                """,
                (season, week, rid, row_["slot_index"], row_["slot"], row_["player_id"],
                 round(float(row_["projection"] or 0.0), 2), round(row_["rate"], 4),
                 row_["tier"], row_["ratio"], row_["reason"]),
            )

        risk = bucket["risk"]
        db.execute(
            """
            INSERT INTO team_weeks
                (season, week, roster_id, proj_total, chug_odds, risk_tier,
                 risk_score, at_risk, locked_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (season, week, roster_id) DO UPDATE SET
                proj_total = EXCLUDED.proj_total,
                chug_odds  = EXCLUDED.chug_odds,
                risk_tier  = EXCLUDED.risk_tier,
                risk_score = EXCLUDED.risk_score,
                at_risk    = EXCLUDED.at_risk,
                locked_at  = EXCLUDED.locked_at
            """,
            (season, week, rid, bucket["proj_total"], risk["rate"], risk["tier"],
             risk["score"], risk["at_risk"], now()),
        )
        stamped += 1

    # Freeze each open curse against its target's projection -- but ONLY those
    # that have no threshold yet.
    #
    # The `threshold_proj IS NULL` guard is what makes an admin unlock safe. A
    # week can now go locked -> open -> locked so people can still cast, and
    # without this guard the second lock would re-freeze every curse against a
    # projection that has since absorbed the inactives list. That would make
    # cursing late strictly better than cursing early, which is the whole
    # failure the frozen threshold exists to prevent. Cast early or cast late:
    # the number you are graded against is the one from first kickoff.
    #
    # No alias on the UPDATE target: Postgres allows `UPDATE curses c ...`,
    # SQLite does not, and the engine tests run this exact statement.
    cur = db.execute(
        """
        UPDATE curses SET threshold_proj = t.proj_total
        FROM team_weeks t
        WHERE curses.season = t.season AND curses.week = t.week
          AND curses.target_roster_id = t.roster_id
          AND curses.season = %s AND curses.week = %s AND curses.status = 'cast'
          AND curses.threshold_proj IS NULL
        """,
        (season, week),
    )
    frozen = cur.rowcount

    db.execute(
        "UPDATE weeks SET status = 'locked', locked_at = %s WHERE season = %s AND week = %s",
        (now(), season, week),
    )
    db.commit()
    return {"ok": True, "week": week, "lineups": stamped, "curses_frozen": frozen,
            "expected_gooses": projected["expected_gooses"]}


def unlock_week(db, season: int, week: int, admin_roster_id: int = None) -> dict:
    """
    Put a locked week back to `open` so curses can still be cast.

    What it does NOT do is give anything back its freedom to move. Every
    threshold_proj already stamped on a curse stays exactly where it is, the
    lineup snapshot stays in place, and a curse cast during the unlocked window
    is stamped immediately with that same frozen number (see cast_curse). The
    unlock buys TIME, not a fresh projection -- an owner who casts at 4pm is
    graded against the 1pm bar like everybody else.

    Refuses on a settled week. Undoing a settle means unwinding chugs, tokens
    and blessings that people have already acted on, which is reset_week's job
    and deliberately behind a typed confirmation.
    """
    row = get_week(db, season, week)
    if row is None:
        return {"ok": False, "reason": f"week {week} does not exist yet"}
    if row["status"] == "final":
        return {"ok": False, "reason":
                f"week {week} is settled -- use Reset week to unwind it"}
    if row["status"] != "locked":
        return {"ok": False, "reason": f"week {week} is {row['status']}, not locked"}

    db.execute(
        "UPDATE weeks SET status = 'open', unlocked_at = %s, unlocked_by = %s "
        "WHERE season = %s AND week = %s",
        (now(), admin_roster_id, season, week),
    )
    db.commit()
    held = db.execute(
        "SELECT COUNT(*) AS n FROM curses WHERE season = %s AND week = %s "
        "AND status = 'cast' AND threshold_proj IS NOT NULL",
        (season, week),
    ).fetchone()["n"]
    return {"ok": True, "week": week, "thresholds_held": held}


# --------------------------------------------------------------------------
# settle
# --------------------------------------------------------------------------

def raise_chug(db, season: int, week: int, roster_id: int, reason: str, ref_id=None,
               mints_tokens: bool = True, wrath_id=None) -> int:
    cur = db.execute(
        "INSERT INTO chugs (season, week, roster_id, reason, ref_id, status, "
        "mints_tokens, wrath_id, created_at) "
        "VALUES (%s, %s, %s, %s, %s, 'owed', %s, %s, %s) RETURNING id",
        (season, week, roster_id, reason, ref_id, mints_tokens, wrath_id, now()),
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
    wraths_expired = expire_wraths(db, season, week)

    db.execute(
        "UPDATE weeks SET status = 'final', settled_at = %s WHERE season = %s AND week = %s",
        (now(), season, week),
    )
    db.commit()
    return {
        "ok": True, "week": week, "gooses": goose_count,
        "chugs_rolled": rolled, "blessings_expired": expired,
        "wraths_expired": wraths_expired, **resolved,
    }


def resolve_curses(db, season: int, week: int) -> dict:
    """
    A cast curse ends one of three ways:
      blocked  -- the target held a blessing. It fires by itself and is spent.
      landed   -- the target finished under the frozen projection. They chug,
                  the caster may mint a token for it, and they are marked with
                  Goosifer's Wrath for next week. If they were ALREADY marked,
                  the chug is multiplied and mints them nothing.
      survived -- the target beat it. They earn a blessing for next week, a
                  curse token to spend at their whim, and any mark they were
                  carrying is burned off.

    A blocked curse does neither: the blessing ate it, so nothing was proved
    and nothing was missed. A mark survives being blocked.

    A curse with no threshold (the week was never locked, so nothing was
    frozen) is left alone rather than graded against a number that does not
    exist -- failing safe means the curse simply does not resolve, and an
    admin can void it.
    """
    mints = settingsmod.get_bool(db, "landed_curse_mints_point", True)
    survivor_mints = settingsmod.get_bool(db, "survived_curse_mints_token", True)
    wrath_on = settingsmod.get_bool(db, "wrath_enabled", True)
    multiplier = max(1, settingsmod.get_int(db, "wrath_multiplier", 2) or 1)
    out = {
        "curses_blocked": 0, "curses_landed": 0, "curses_survived": 0,
        "curses_unresolved": 0, "wraths_armed": 0, "wraths_cashed": 0,
        "wraths_lifted": 0, "survivor_tokens": 0,
    }

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
            # Were they already marked? Read it BEFORE arming a new one, or
            # this week's miss would double the very chug that caused it.
            mark = active_wrath(db, season, target, week) if wrath_on else None
            n = multiplier if mark else 1
            for _ in range(n):
                raise_chug(db, season, week, target, "curse", c["id"],
                           mints_tokens=(mark is None),
                           wrath_id=(mark["id"] if mark else None))
            if mark:
                db.execute(
                    "UPDATE wraths SET status = 'consumed', resolved_by = %s, "
                    "resolved_at = %s WHERE id = %s",
                    (c["id"], now(), mark["id"]),
                )
                out["wraths_cashed"] += 1
            if mints:
                mint_token(db, season, c["caster_roster_id"], week, "curse_landed",
                           f"curse landed on roster {target}, week {week}")
            # And the miss marks them again for next week -- consumed or not,
            # a miss is a miss. arm_wrath refuses a second mark for the same
            # week by itself, so this never stacks past the multiplier.
            if wrath_on and arm_wrath(db, season, target, week, c["id"]):
                out["wraths_armed"] += 1
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
            # Beating the bar with Goothulu on you is the hardest thing an
            # owner does all week, and the blessing alone expires unused most
            # weeks. The token is the part they actually get to spend.
            if survivor_mints:
                mint_token(db, season, target, week, "curse_survived",
                           f"survived a curse, week {week}")
                out["survivor_tokens"] += 1
            out["wraths_lifted"] += lift_wraths(db, season, target, c["id"])
            out["curses_survived"] += 1
    return out


def expire_wraths(db, season: int, week: int) -> int:
    """
    A mark nobody cashed in is gone. Rows written with a NULL expires_after
    (wrath_persists was on) are skipped on purpose -- those end by being
    survived, not by the calendar.
    """
    cur = db.execute(
        "UPDATE wraths SET status = 'expired', resolved_at = %s "
        "WHERE season = %s AND status = 'active' AND expires_after IS NOT NULL "
        "AND expires_after < %s",
        (now(), season, week),
    )
    return cur.rowcount


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

    # If this week has ALREADY been locked once and an admin reopened it, the
    # target's projection is a settled number and this curse inherits it on the
    # spot. Leaving it NULL until the next lock would hand a late caster a
    # projection that has since absorbed the inactive list -- the exact edge the
    # frozen threshold exists to close. See unlock_week.
    frozen = None
    tw = db.execute(
        "SELECT proj_total, locked_at FROM team_weeks "
        "WHERE season = %s AND week = %s AND roster_id = %s",
        (season, week, target),
    ).fetchone()
    if tw and tw["locked_at"]:
        frozen = tw["proj_total"]

    token = tokens[0]
    cur = db.execute(
        "INSERT INTO curses (season, week, caster_roster_id, target_roster_id, token_id, "
        "threshold_proj, status, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'cast', %s) RETURNING id",
        (season, week, caster, target, token["id"], frozen, now()),
    )
    curse_id = cur.fetchone()["id"]
    db.execute("UPDATE curse_tokens SET spent_on = %s WHERE id = %s", (curse_id, token["id"]))
    db.commit()
    return {"ok": True, "curse_id": curse_id, "threshold_proj": frozen}


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

    Except under Goosifer's Wrath. A chug raised by a curse that landed on a
    marked owner carries mints_tokens = FALSE, and confirming it pays out
    nothing whatever points_per_chug says. That is the entire punishment: you
    drink twice and you come away with nothing to curse anybody back with.
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
    # A column added in v0.8: rows written before it exist read as None, and
    # None has to mean "mints", not "does not". Everything that came before
    # Goosifer earned its token.
    mints = c["mints_tokens"] is None or bool(c["mints_tokens"])
    per = settingsmod.get_int(db, "points_per_chug", 1) or 0
    minted = max(0, per) if mints else 0
    for _ in range(minted):
        mint_token(db, season, c["roster_id"], c["week"], "chug", f"chug #{chug_id}")
    db.commit()
    return {"ok": True, "tokens_minted": minted, "wrath": not mints}


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


# --------------------------------------------------------------------------
# admin resets -- destructive, and meant to be
# --------------------------------------------------------------------------
#
# Built for testing a real week end to end before the season starts, so these
# have to unwind EVERYTHING a settle created, in the reverse of the order
# settle_week created it. Getting that order wrong leaves orphans: a token
# minted by a chug that no longer exists, a curse pointing at a blessing row
# that has been deleted. The order below is the one that leaves nothing behind.
#
# Neither of these is reachable without a typed confirmation in the admin UI.
# There is no undo.

def reset_week(db, season: int, week: int) -> dict:
    """
    Return one week to `upcoming` as if it had never been played.

    Curses cast that week are NOT deleted -- they are rewound to 'cast' with
    their threshold cleared, and their tokens are still spent. That is the
    deliberate choice: the curse economy spans weeks, so silently handing back
    tokens for a week the commissioner is only re-testing would inflate
    everyone's balance. Void individual curses on the Curses tab if that is
    what you actually want.
    """
    counts: dict = {}

    # Tokens minted BY this week's events, but only ones nobody has spent yet.
    # A spent token bought a curse in a later week that other people played
    # around; clawing it back would rewrite their week too.
    counts["tokens"] = db.execute(
        "DELETE FROM curse_tokens WHERE season = %s AND earned_week = %s "
        "AND spent_on IS NULL AND source IN ('chug', 'curse_landed', 'curse_survived')",
        (season, week),
    ).rowcount
    counts["blessings"] = db.execute(
        "DELETE FROM blessings WHERE season = %s AND earned_week = %s", (season, week)
    ).rowcount
    counts["wraths"] = db.execute(
        "DELETE FROM wraths WHERE season = %s AND earned_week = %s", (season, week)
    ).rowcount
    # A mark this week's settle spent or burned off goes back to active, the
    # same way a consumed blessing does. Both are keyed off this week's curses,
    # which is why wraths carries resolved_by at all.
    db.execute(
        "UPDATE wraths SET status = 'active', resolved_by = NULL, resolved_at = NULL "
        "WHERE season = %s AND status IN ('consumed', 'lifted') "
        "AND resolved_by IN (SELECT id FROM curses WHERE season = %s AND week = %s)",
        (season, season, week),
    )
    # A blessing this week's settle CONSUMED goes back to active.
    db.execute(
        "UPDATE blessings SET status = 'active', consumed_by = NULL, resolved_at = NULL "
        "WHERE season = %s AND expires_after >= %s AND status = 'consumed' "
        "AND consumed_by IN (SELECT id FROM curses WHERE season = %s AND week = %s)",
        (season, week, season, week),
    )
    counts["curses_rewound"] = db.execute(
        "UPDATE curses SET status = 'cast', threshold_proj = NULL, actual_total = NULL, "
        "blocked_by = NULL, resolved_at = NULL WHERE season = %s AND week = %s",
        (season, week),
    ).rowcount
    # Chugs that ROLLED INTO this week came from an earlier one; send them home
    # rather than deleting someone else's week.
    db.execute(
        "UPDATE chugs SET week = rolled_from_week, rolled_from_week = NULL "
        "WHERE season = %s AND week = %s AND rolled_from_week IS NOT NULL",
        (season, week),
    )
    counts["chugs"] = db.execute(
        "DELETE FROM chugs WHERE season = %s AND week = %s", (season, week)
    ).rowcount
    counts["gooses"] = db.execute(
        "DELETE FROM gooses WHERE season = %s AND week = %s", (season, week)
    ).rowcount
    counts["lineups"] = db.execute(
        "DELETE FROM lineup_slots WHERE season = %s AND week = %s", (season, week)
    ).rowcount
    db.execute("DELETE FROM team_weeks WHERE season = %s AND week = %s", (season, week))
    db.execute(
        "UPDATE weeks SET status = 'upcoming', opened_at = NULL, opened_by = NULL, "
        "locked_at = NULL, settled_at = NULL, unlocked_at = NULL, unlocked_by = NULL "
        "WHERE season = %s AND week = %s",
        (season, week),
    )
    db.commit()
    return {"ok": True, "week": week, **counts}


def reset_season(db, season: int) -> dict:
    """
    Wipe every week of a season: gooses, chugs, curses, blessings, wraths and
    tokens all the way back to an empty board. Owners, PINs and commissioner rules
    survive -- resetting a test season should not cost anyone their login.
    """
    counts = {}
    for table in ("wraths", "blessings", "curses", "curse_tokens", "chugs", "gooses",
                  "team_weeks", "lineup_slots"):
        counts[table] = db.execute(
            f"DELETE FROM {table} WHERE season = %s", (season,)).rowcount
    db.execute("DELETE FROM weeks WHERE season = %s", (season,))
    db.commit()
    return {"ok": True, "season": season, **counts}
