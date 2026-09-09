"""
app.py
======
Flask front end for the GOOSE EGG Challenge.

Thin on purpose: every state change lives in week_engine.py and every rule
lives in settings.py. Routes authenticate, read, and render.

Tabs: Watch, Curse, My Geese, Standings, Admin. Watch leads because on a
Sunday it is the only screen anybody opens. The Curse tab's route is still
`board` -- the screen was renamed, not rebuilt.
"""
from __future__ import annotations

import os
import time

import bcrypt
from dotenv import load_dotenv
from flask import (
    Flask, abort, flash, redirect, render_template, request, session, url_for,
)

import db as dbmod
import demo as demomod
import filters as filtersmod
import goose
import players_sync
import settings as settingsmod
import sleeper
import themes as themesmod
import watch as watch_mod
import week_engine as engine

load_dotenv()

LEAGUE_ID = os.environ.get("GOOSE_LEAGUE_ID", "")
SEASON = int(os.environ.get("GOOSE_SEASON", "2026"))
POLL_SECRET = os.environ.get("POLL_SECRET", "")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def get_db():
    """
    Open the request's connection and, on the way, point sleeper.py at the demo
    snapshot if demo mode is on.

    Installing it HERE rather than in a before_request hook keeps the two in
    lockstep: the seam is only ever switched by code that also has a database
    to read the flag from, so there is no window where a worker serves demo
    scores because a previous request left the seam installed.
    """
    if not hasattr(request, "_goose_db"):
        request._goose_db = dbmod.open_wrapped()
        try:
            demomod.install(request._goose_db)
        except Exception:
            # A broken snapshot must never take the app down -- and must not
            # leave an aborted transaction behind for the route to trip over.
            request._goose_db.rollback()
            sleeper.set_demo(None)
    return request._goose_db


@app.teardown_request
def _close_db(exc):
    db = getattr(request, "_goose_db", None)
    if db is not None:
        if exc:
            db.rollback()
        db.close()


# Registered from filters.py so app.py and tests/test_templates.py can never
# hold two different definitions of the same filter -- which is exactly how a
# renamed filter once shipped green and broke a page.
for _name, _fn in filtersmod.FILTERS.items():
    app.add_template_filter(_fn, _name)


def current_owner(db):
    rid = session.get("roster_id")
    if rid is None:
        return None
    return db.execute(
        "SELECT * FROM owners WHERE league_id = %s AND season = %s AND roster_id = %s",
        (LEAGUE_ID, SEASON, rid),
    ).fetchone()


def require_login(db):
    owner = current_owner(db)
    if owner is None:
        return None
    return owner


def require_admin(db):
    owner = require_login(db)
    if owner is None or not owner["is_admin"]:
        return None
    return owner


def active_week(db) -> int:
    return settingsmod.get_int(db, "active_week", 1) or 1


def owners_map(db) -> dict:
    rows = db.execute(
        "SELECT * FROM owners WHERE league_id = %s AND season = %s ORDER BY roster_id",
        (LEAGUE_ID, SEASON),
    ).fetchall()
    return {r["roster_id"]: r for r in rows}


def label(owner_row) -> str:
    if owner_row is None:
        return "Unknown"
    return owner_row["team_name"] or owner_row["owner_name"] or f"Roster {owner_row['roster_id']}"


def owner_theme(owner_row) -> str:
    """The owner's stored theme, or the default if unset/unknown. Bracket
    access (not .get) so this works on both RealDictCursor rows in
    production and sqlite3.Row in tests -- neither is a plain dict."""
    if owner_row is None:
        return themesmod.DEFAULT_THEME
    try:
        raw = owner_row["theme"]
    except (KeyError, IndexError):
        raw = None
    return themesmod.resolve(raw)


def _app_version() -> str:
    try:
        with open(os.path.join(os.path.dirname(__file__), "VERSION")) as fh:
            return fh.read().strip()
    except OSError:
        return "?"


@app.context_processor
def inject_globals():
    """
    The page chrome: who is logged in, the admin badge count, the version.

    Every lookup here is wrapped, because this runs during template rendering
    on EVERY page. An exception raised here cannot be handled by the route --
    the route has already returned -- so it becomes a 500 on a page that had
    otherwise rendered fine. Chrome is not worth a page: a badge that silently
    reads zero is a far better failure than a screen nobody can open.
    """
    db = getattr(request, "_goose_db", None)
    try:
        owner = current_owner(db) if db is not None else None
    except Exception:
        owner = None
    pending = 0
    if db is not None and owner is not None and owner["is_admin"]:
        try:
            pending = db.execute(
                "SELECT COUNT(*) AS n FROM chugs WHERE season = %s AND status = 'owed'",
                (SEASON,),
            ).fetchone()["n"]
        except Exception:
            pending = 0
    return {
        "me": owner,
        "season": SEASON,
        "pending_chugs": pending,
        "demo_mode": bool(sleeper.demo_payload()),
        "tier_order": list(goose.TIERS),
        "app_version": _app_version(),
        "theme": owner_theme(owner),
        "themes": themesmod.THEMES,
        "theme_choices": themesmod.theme_choices(),
    }


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    db = get_db()
    owners = db.execute(
        "SELECT roster_id, owner_name, team_name FROM owners "
        "WHERE league_id = %s AND season = %s ORDER BY COALESCE(team_name, owner_name)",
        (LEAGUE_ID, SEASON),
    ).fetchall()

    if request.method == "POST":
        rid = request.form.get("roster_id", type=int)
        pin = (request.form.get("pin") or "").strip()
        row = db.execute(
            "SELECT * FROM owners WHERE league_id = %s AND season = %s AND roster_id = %s",
            (LEAGUE_ID, SEASON, rid),
        ).fetchone()
        if row is None or not row["pin_hash"]:
            flash("No PIN set for that owner yet — ask an admin.", "error")
            return redirect(url_for("login"))
        if not bcrypt.checkpw(pin.encode(), row["pin_hash"].encode()):
            flash("Wrong PIN.", "error")
            return redirect(url_for("login"))
        session["roster_id"] = rid
        return redirect(url_for("board"))

    return render_template("login.html", owners=owners)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# board
# --------------------------------------------------------------------------

def snapshot_or_preview(db, week: int, wk_row) -> tuple[dict, bool]:
    """
    The week's numbers, and whether they are frozen.

    Returns (by_roster, is_preview). A locked or settled week reads the
    SNAPSHOT out of the database -- that is the version curses are graded
    against and it must never be recomputed for display. Any earlier week is
    computed live from Sleeper so the board is not blank all week, and comes
    back flagged so every screen can say out loud that the numbers still move.

    A preview failure is not fatal. Sleeper being down before kickoff should
    cost you the preview, not the page.
    """
    if wk_row and wk_row["status"] in ("locked", "final"):
        rows = db.execute(
            "SELECT * FROM team_weeks WHERE season = %s AND week = %s", (SEASON, week)
        ).fetchall()
        if rows:
            return {r["roster_id"]: dict(r) for r in rows}, False

    try:
        projected = engine.project_week(db, LEAGUE_ID, SEASON, week)
    except Exception:
        # Same reasoning as the Watch route: project_week touches players_cache,
        # so this may be a database error, and leaving the transaction aborted
        # would take down the render that follows. A preview is a nicety; the
        # board without one is still a board.
        db.rollback()
        return {}, True

    out = {}
    for rid, bucket in projected["rosters"].items():
        risk = bucket["risk"]
        out[rid] = {
            "roster_id": rid,
            "proj_total": bucket["proj_total"],
            "risk_tier": risk["tier"],
            "risk_score": risk["score"],
            "at_risk": risk["at_risk"],
            "chug_odds": risk["rate"],
            "actual_total": None,
            "goose_count": 0,
            "slots": bucket["slots"],
        }
    return out, True


def most_cursed(db, owners: dict) -> dict | None:
    """
    Season leader in curses ABSORBED -- the banner. Counts every curse aimed at
    an owner whether it landed, was survived or was blocked, because being
    picked on is the thing the banner is about, not the outcome.

    Returns None below two curses: crowning somebody "most cursed" off a single
    curse in week 1 is noise, not a story.
    """
    rows = db.execute(
        "SELECT target_roster_id AS rid, COUNT(*) AS n, "
        "COUNT(*) FILTER (WHERE status = 'landed') AS landed "
        "FROM curses WHERE season = %s GROUP BY target_roster_id "
        "ORDER BY n DESC, landed DESC LIMIT 1",
        (SEASON,),
    ).fetchall()
    if not rows or rows[0]["n"] < 2:
        return None
    top = rows[0]
    return {
        "roster_id": top["rid"],
        "team": label(owners.get(top["rid"])),
        "avatar": (owners.get(top["rid"]) or {}).get("avatar"),
        "count": top["n"],
        "landed": top["landed"] or 0,
    }


def _owner_avatar(owner_row):
    """Bracket access, not .get -- owner rows are RealDictRow in production and
    sqlite3.Row in tests, and only one of those two has .get()."""
    if owner_row is None:
        return None
    try:
        return owner_row["avatar"]
    except (KeyError, IndexError):
        return None


def crown_key(row) -> tuple:
    """
    The one definition of "drunkest": most chugs, then most gooses, then most
    curses landed. Standings sorts its whole table with this and crown_leader
    takes the top of the same order, so the crown on the Curse tab can never
    disagree with the crown on Standings.
    """
    return (-row["chugs"], -row["geese"], -row["curses_landed"])


def crown_leader(db, owners: dict) -> dict | None:
    """
    Who holds the Goose Crown, in three grouped queries rather than the seven
    per owner the Standings table runs -- this is a banner, not a table.

    Returns None until somebody has actually chugged. An empty crown is worse
    than no crown: in week 1 it names a leader who has done nothing, and the
    tie-break would hand it to whoever sorts first.
    """
    def tally(sql, key):
        return {r[key]: r["n"] for r in db.execute(sql, (SEASON,)).fetchall()}

    chugs = tally("SELECT roster_id, COUNT(*) AS n FROM chugs "
                  "WHERE season = %s GROUP BY roster_id", "roster_id")
    geese = tally("SELECT roster_id, COUNT(*) AS n FROM gooses "
                  "WHERE season = %s GROUP BY roster_id", "roster_id")
    landed = tally("SELECT caster_roster_id, COUNT(*) AS n FROM curses "
                   "WHERE season = %s AND status = 'landed' "
                   "GROUP BY caster_roster_id", "caster_roster_id")

    rows = [{
        "roster_id": rid,
        "team": label(owner),
        "avatar": _owner_avatar(owner),
        "chugs": chugs.get(rid, 0),
        "geese": geese.get(rid, 0),
        "curses_landed": landed.get(rid, 0),
    } for rid, owner in owners.items()]
    if not rows:
        return None

    rows.sort(key=crown_key)
    top = dict(rows[0])
    if top["chugs"] < 1:
        return None
    # A crown two owners are level on is a tie, and saying so is more honest
    # than picking one of them and hoping nobody checks the table.
    top["tied"] = sum(1 for r in rows if crown_key(r) == crown_key(top)) - 1
    return top


@app.route("/")
def board():
    db = get_db()
    me = require_login(db)
    if me is None:
        return redirect(url_for("login"))

    week = request.args.get("week", type=int) or active_week(db)
    wk = engine.ensure_week(db, SEASON, week)
    db.commit()
    owners = owners_map(db)
    tw, preview = snapshot_or_preview(db, week, wk)

    # Sealed curses: while the week is still open nobody sees who is targeting
    # whom, except for their own. Fails CLOSED -- if the setting can't be read
    # we seal, because leaking a target can't be taken back.
    sealed = settingsmod.get_bool(db, "curses_sealed_until_lock", True) and wk["status"] == "open"
    curses = db.execute(
        "SELECT * FROM curses WHERE season = %s AND week = %s ORDER BY id", (SEASON, week)
    ).fetchall()
    blessings = db.execute(
        "SELECT * FROM blessings WHERE season = %s AND status = 'active' AND expires_after >= %s",
        (SEASON, week),
    ).fetchall()
    blessed = {b["roster_id"] for b in blessings}

    # Goosifer's marks. Same shape as blessings, and read with the same
    # `earned_week < week` rule the engine uses -- a mark armed by last
    # night's settle is exactly what this board is here to advertise.
    wraths = db.execute(
        "SELECT * FROM wraths WHERE season = %s AND status = 'active' "
        "AND earned_week < %s AND (expires_after IS NULL OR expires_after >= %s)",
        (SEASON, week, week),
    ).fetchall()
    wrathed = {w["roster_id"] for w in wraths}
    wrath_mult = max(1, settingsmod.get_int(db, "wrath_multiplier", 2) or 1)

    curse_by_target: dict = {}
    for c in curses:
        visible = (not sealed) or c["caster_roster_id"] == me["roster_id"]
        curse_by_target.setdefault(c["target_roster_id"], []).append({
            "id": c["id"],
            "status": c["status"],
            "caster": label(owners.get(c["caster_roster_id"])) if visible else None,
            "mine": c["caster_roster_id"] == me["roster_id"],
            "sealed": not visible,
            "threshold": c["threshold_proj"],
        })

    rows = []
    for rid, owner in owners.items():
        t = tw.get(rid) or {}
        mine = [c for c in curse_by_target.get(rid, []) if c["status"] == "cast"]
        rows.append({
            "roster_id": rid,
            "team": label(owner),
            "owner_name": owner["owner_name"],
            "avatar": owner["avatar"],
            "proj_total": t.get("proj_total"),
            "actual_total": t.get("actual_total"),
            "goose_count": t.get("goose_count") or 0,
            "risk_tier": t.get("risk_tier"),
            "risk_score": t.get("risk_score"),
            "risk_rate": goose.TEAM_RATE.get(t.get("risk_tier")),
            "at_risk": t.get("at_risk") or 0,
            "curses": curse_by_target.get(rid, []),
            "cast_on_them": len(mine),
            "blessed": rid in blessed,
            "wrathed": rid in wrathed,
            "is_me": rid == me["roster_id"],
        })

    # Worst lineup first. Teams with no numbers at all sink rather than float:
    # "we don't know yet" is not the top of a risk board.
    rows.sort(key=lambda r: (
        r["risk_tier"] is None,
        not r["wrathed"],
        -goose.TEAM_INDEX.get(r["risk_tier"], -1),
        -(r["risk_score"] or 0),
    ))

    my_tokens = engine.unspent_tokens(db, SEASON, me["roster_id"])
    my_row = next((r for r in rows if r["is_me"]), None)

    # Wrapped like every other read on this page: a banner is never worth a
    # 500, and this one runs three queries the older schemas may not answer.
    try:
        crown = crown_leader(db, owners)
    except Exception:
        db.rollback()
        crown = None

    weeks = [r["week"] for r in db.execute(
        "SELECT week FROM weeks WHERE season = %s ORDER BY week", (SEASON,)
    ).fetchall()] or [week]

    return render_template(
        "board.html", week=week, wk=wk, rows=rows, my_row=my_row,
        my_tokens=len(my_tokens), my_blessed=me["roster_id"] in blessed,
        my_wrathed=me["roster_id"] in wrathed, wrath_mult=wrath_mult,
        weeks=weeks, sealed=sealed, preview=preview,
        most_cursed=most_cursed(db, owners), crown=crown,
        can_cast=wk["status"] == "open" and len(my_tokens) > 0,
        targets=[r for r in rows if not r["is_me"]],
    )


@app.route("/curse", methods=["POST"])
def cast_curse():
    db = get_db()
    me = require_login(db)
    if me is None:
        return redirect(url_for("login"))
    week = request.form.get("week", type=int) or active_week(db)
    target = request.form.get("target", type=int)
    result = engine.cast_curse(db, SEASON, week, me["roster_id"], target)
    flash("Curse cast — sealed until kickoff." if result["ok"] else result["reason"],
          "success" if result["ok"] else "error")
    return redirect(url_for("board", week=week))


@app.route("/curse/<int:curse_id>/cancel", methods=["POST"])
def cancel_curse(curse_id):
    db = get_db()
    me = require_login(db)
    if me is None:
        return redirect(url_for("login"))
    result = engine.cancel_curse(db, SEASON, curse_id, me["roster_id"])
    flash("Curse withdrawn, token returned." if result["ok"] else result["reason"],
          "success" if result["ok"] else "error")
    return redirect(url_for("board"))


# --------------------------------------------------------------------------
# my geese
# --------------------------------------------------------------------------

@app.route("/me")
def my_geese():
    db = get_db()
    me = require_login(db)
    if me is None:
        return redirect(url_for("login"))
    rid = me["roster_id"]
    week = request.args.get("week", type=int) or active_week(db)
    wk = engine.ensure_week(db, SEASON, week)
    db.commit()

    lineup, preview, team_risk = [], False, None
    if wk["status"] in ("locked", "final"):
        lineup = [dict(r) for r in db.execute(
            "SELECT l.*, p.full_name, p.position, p.team, p.injury_status "
            "FROM lineup_slots l LEFT JOIN players_cache p ON p.player_id = l.player_id "
            "WHERE l.season = %s AND l.week = %s AND l.roster_id = %s ORDER BY l.slot_index",
            (SEASON, week, rid),
        ).fetchall()]
        for row in lineup:
            row["photo"] = (
                f"https://sleepercdn.com/content/nfl/players/thumb/{row['player_id']}.jpg"
                if row.get("player_id") else None
            )
            row["projection"] = row.get("proj_pts")
            row["name"] = row.get("full_name")
            row["nfl_team"] = row.get("team")
            row["ratio"] = row.get("proj_ratio")
            row["reason"] = row.get("risk_reason")
        team_risk = goose.team_risk([r.get("tier") for r in lineup])

    if not lineup:
        # Nothing frozen yet -- show the live version so an owner can look at
        # his week before kickoff instead of an empty page. Labelled as moving.
        preview = True
        try:
            bucket = engine.project_week(db, LEAGUE_ID, SEASON, week)["rosters"].get(rid)
        except Exception:
            db.rollback()
            bucket = None
        if bucket:
            lineup = bucket["slots"]
            team_risk = bucket["risk"]

    gooses = db.execute(
        "SELECT g.*, p.full_name, p.position, p.team FROM gooses g "
        "LEFT JOIN players_cache p ON p.player_id = g.player_id "
        "WHERE g.season = %s AND g.roster_id = %s ORDER BY g.week DESC, g.slot_index",
        (SEASON, rid),
    ).fetchall()

    chugs = db.execute(
        "SELECT * FROM chugs WHERE season = %s AND roster_id = %s ORDER BY status DESC, week DESC, id",
        (SEASON, rid),
    ).fetchall()

    owners = owners_map(db)
    curses = db.execute(
        "SELECT * FROM curses WHERE season = %s AND (caster_roster_id = %s OR target_roster_id = %s) "
        "ORDER BY week DESC, id DESC",
        (SEASON, rid, rid),
    ).fetchall()

    tw = db.execute(
        "SELECT * FROM team_weeks WHERE season = %s AND week = %s AND roster_id = %s",
        (SEASON, week, rid),
    ).fetchone()

    my_curse = db.execute(
        "SELECT * FROM curses WHERE season = %s AND week = %s AND target_roster_id = %s "
        "AND status = 'cast' LIMIT 1",
        (SEASON, week, rid),
    ).fetchone()

    proj_total = (tw or {}).get("proj_total")
    if proj_total is None and lineup:
        proj_total = round(sum(float(r.get("projection") or 0) for r in lineup), 2)

    return render_template(
        "my_geese.html", week=week, wk=wk, lineup=lineup, gooses=gooses, chugs=chugs,
        curses=curses, owners=owners, team_week=tw, my_curse=my_curse,
        preview=preview, team_risk=team_risk, proj_total=proj_total,
        tokens=len(engine.unspent_tokens(db, SEASON, rid)),
        blessing=engine.active_blessing(db, SEASON, rid, week),
        wrath=engine.active_wrath(db, SEASON, rid, week),
        wrath_mult=max(1, settingsmod.get_int(db, "wrath_multiplier", 2) or 1),
        label=label,
    )


@app.route("/me/theme", methods=["POST"])
def set_theme():
    db = get_db()
    me = require_login(db)
    if me is None:
        return redirect(url_for("login"))
    # The picker lives in the ribbon on every screen, so a theme change has to
    # come back to the screen it was made from. Only a local path is accepted:
    # "next" arrives from a form field, and a form field is user input.
    nxt = request.form.get("next") or ""
    back = nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("board")

    theme = request.form.get("theme", "")
    if theme not in themesmod.THEMES:
        flash("Unknown theme.", "error")
        return redirect(back)
    db.execute(
        "UPDATE owners SET theme = %s WHERE league_id = %s AND season = %s AND roster_id = %s",
        (theme, LEAGUE_ID, SEASON, me["roster_id"]),
    )
    db.commit()
    flash(f"Theme set to {themesmod.THEMES[theme]['label']}.", "success")
    return redirect(back)


# --------------------------------------------------------------------------
# goose watch
# --------------------------------------------------------------------------

@app.route("/watch")
def goose_watch():
    """
    The live Sunday board. Hits Sleeper on every load rather than reading the
    database, because the whole value is that it is current.

    If Sleeper cannot be reached the page says so in as many words. It must
    never render an empty, cheerful "no gooses" board off a failed fetch --
    that is the fail-safe-that-renders-nothing bug the FAAB app shipped, where
    a missing migration looked exactly like a feature that had never existed.
    """
    db = get_db()
    me = require_login(db)
    if me is None:
        return redirect(url_for("login"))

    week = request.args.get("week", type=int) or active_week(db)
    data, problem = None, None
    try:
        data = watch_mod.build(db, LEAGUE_ID, SEASON, week)
        if not data["feed_ok"]:
            problem = ("Sleeper's game feed came back empty, so nothing here knows "
                       "which games have finished. Scores below may be stale.")
    except Exception as exc:
        # ROLL BACK BEFORE RENDERING. watch_mod.build reads the database as well
        # as Sleeper, so the exception this catches may have come from Postgres
        # -- and a failed statement puts the whole connection into "current
        # transaction is aborted, commands ignored until end of transaction
        # block". Every later query on it then fails too, including the pending
        # -chug count in inject_globals, which runs while this very template
        # renders. That turned a handled error into a 500 on the live deploy
        # (v0.5.0 shipped its schema migration without running it, so this
        # screen's SELECT hit a column that did not exist yet).
        #
        # SQLite has no aborted-transaction state at all, which is why the whole
        # local suite renders this page happily against a v0.4 schema and proves
        # nothing. Rolling back here is what makes the fail-soft actually soft.
        db.rollback()
        problem = (f"Could not load the live board ({type(exc).__name__}). This screen "
                   f"reads live scores on every load, so there is nothing to show until "
                   f"that is back. Nothing is lost — the week is graded from final "
                   f"scores when it settles.")

    # Marked owners, for the badge. Wrapped: Goose Watch is the one screen that
    # must render on a Sunday whatever the database is doing, and a badge is
    # never worth a 500.
    try:
        wrathed = {r["roster_id"] for r in db.execute(
            "SELECT roster_id FROM wraths WHERE season = %s AND status = 'active' "
            "AND earned_week < %s AND (expires_after IS NULL OR expires_after >= %s)",
            (SEASON, week, week),
        ).fetchall()}
    except Exception:
        db.rollback()
        wrathed = set()

    return render_template("watch.html", week=week, data=data, problem=problem,
                           me_roster=me["roster_id"], wrathed=wrathed)



# --------------------------------------------------------------------------
# standings
# --------------------------------------------------------------------------

@app.route("/standings")
def standings():
    db = get_db()
    me = require_login(db)
    if me is None:
        return redirect(url_for("login"))

    owners = owners_map(db)
    rows = []
    for rid, owner in owners.items():
        chugs_paid = db.execute(
            "SELECT COUNT(*) AS n FROM chugs WHERE season = %s AND roster_id = %s AND status = 'paid'",
            (SEASON, rid),
        ).fetchone()["n"]
        chugs_owed = db.execute(
            "SELECT COUNT(*) AS n FROM chugs WHERE season = %s AND roster_id = %s AND status = 'owed'",
            (SEASON, rid),
        ).fetchone()["n"]
        from_curses = db.execute(
            "SELECT COUNT(*) AS n FROM chugs WHERE season = %s AND roster_id = %s AND reason = 'curse'",
            (SEASON, rid),
        ).fetchone()["n"]
        geese = db.execute(
            "SELECT COUNT(*) AS n FROM gooses WHERE season = %s AND roster_id = %s", (SEASON, rid)
        ).fetchone()["n"]
        cast = db.execute(
            "SELECT status, COUNT(*) AS n FROM curses WHERE season = %s AND caster_roster_id = %s "
            "GROUP BY status", (SEASON, rid),
        ).fetchall()
        cast_by = {r["status"]: r["n"] for r in cast}
        blessings = db.execute(
            "SELECT COUNT(*) AS n FROM blessings WHERE season = %s AND roster_id = %s", (SEASON, rid)
        ).fetchone()["n"]
        wraths = db.execute(
            "SELECT COUNT(*) AS n FROM wraths WHERE season = %s AND roster_id = %s "
            "AND status = 'consumed'", (SEASON, rid),
        ).fetchone()["n"]

        rows.append({
            "roster_id": rid,
            "team": label(owner),
            "avatar": owner["avatar"],
            "chugs": chugs_paid + chugs_owed,   # the crown counts every chug earned
            "paid": chugs_paid,
            "owed": chugs_owed,
            "geese": geese,
            "from_curses": from_curses,
            "curses_landed": cast_by.get("landed", 0),
            "curses_failed": cast_by.get("survived", 0) + cast_by.get("blocked", 0),
            "blessings": blessings,
            "wraths_cashed": wraths,
            "wrathed": engine.active_wrath(db, SEASON, rid, active_week(db)) is not None,
            "tokens": len(engine.unspent_tokens(db, SEASON, rid)),
            "is_me": rid == me["roster_id"],
        })

    # The crown goes to the DRUNKEST owner. Ties break on gooses, then on
    # curses landed -- doing it the hard way beats being handed it.
    rows.sort(key=crown_key)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    if rows:
        rows[0]["tied"] = sum(1 for r in rows if crown_key(r) == crown_key(rows[0])) - 1

    assassin = max(rows, key=lambda r: r["curses_landed"]) if rows else None
    teflon = min(rows, key=lambda r: r["geese"]) if rows else None
    return render_template("standings.html", rows=rows, assassin=assassin, teflon=teflon,
                           most_cursed=most_cursed(db, owners))


# --------------------------------------------------------------------------
# admin
# --------------------------------------------------------------------------

@app.route("/admin")
def admin():
    db = get_db()
    me = require_admin(db)
    if me is None:
        abort(403)

    tab = request.args.get("tab", "chugs")
    owners = owners_map(db)
    week = active_week(db)

    chugs = db.execute(
        "SELECT * FROM chugs WHERE season = %s ORDER BY status DESC, week, roster_id, id", (SEASON,)
    ).fetchall()
    weeks = db.execute("SELECT * FROM weeks WHERE season = %s ORDER BY week", (SEASON,)).fetchall()
    curses = db.execute(
        "SELECT * FROM curses WHERE season = %s ORDER BY week DESC, id DESC LIMIT 60", (SEASON,)
    ).fetchall()
    blessings = db.execute(
        "SELECT * FROM blessings WHERE season = %s ORDER BY earned_week DESC, id DESC LIMIT 40", (SEASON,)
    ).fetchall()
    wraths = db.execute(
        "SELECT * FROM wraths WHERE season = %s ORDER BY earned_week DESC, id DESC LIMIT 40", (SEASON,)
    ).fetchall()

    wk = engine.ensure_week(db, SEASON, week)
    db.commit()
    lock_epoch = wk["lock_epoch"] or sleeper.week_lock_epoch(SEASON, week)
    end_epoch = wk["end_epoch"] or sleeper.week_end_epoch(SEASON, week)

    payload = sleeper.demo_payload()
    return render_template(
        "admin.html", tab=tab, owners=owners, label=label, chugs=chugs,
        weeks=weeks, curses=curses, blessings=blessings, wraths=wraths,
        active=week, wk=wk,
        rules=settingsmod.all_tunables(db), now=int(time.time()),
        lock_epoch=lock_epoch, end_epoch=end_epoch,
        demo_on=bool(payload), demo=payload,
    )


@app.route("/admin/chug/<int:chug_id>/<action>", methods=["POST"])
def admin_chug(chug_id, action):
    db = get_db()
    me = require_admin(db)
    if me is None:
        abort(403)
    if action == "confirm":
        r = engine.confirm_chug(db, SEASON, chug_id, me["roster_id"])
    elif action == "safe-pour":
        r = engine.confirm_chug(db, SEASON, chug_id, me["roster_id"], safe_pour=True)
    elif action == "undo":
        r = engine.unconfirm_chug(db, SEASON, chug_id)
    else:
        r = {"ok": False, "reason": "Unknown action."}
    flash("Done." if r["ok"] else r["reason"], "success" if r["ok"] else "error")
    return redirect(url_for("admin", tab="chugs"))


@app.route("/admin/rules", methods=["POST"])
def admin_rules():
    db = get_db()
    me = require_admin(db)
    if me is None:
        abort(403)
    for key, kind, _ in settingsmod.TUNABLE:
        if kind == "bool":
            settingsmod.set_bool(db, key, request.form.get(key) == "on")
        else:
            value = request.form.get(key, type=int)
            if value is not None:
                settingsmod.set_raw(db, key, max(0, value))
    db.commit()
    flash("Rules updated. They take effect immediately.", "success")
    return redirect(url_for("admin", tab="rules"))


@app.route("/admin/week", methods=["POST"])
def admin_week():
    db = get_db()
    me = require_admin(db)
    if me is None:
        abort(403)
    action = request.form.get("action")
    week = request.form.get("week", type=int) or active_week(db)

    if action == "set-active":
        settingsmod.set_raw(db, "active_week", week)
        db.commit()
        r = {"ok": True}
        flash(f"Active week set to {week}.", "success")
    elif action == "open":
        r = engine.open_week(db, LEAGUE_ID, SEASON, week, me["roster_id"])
        flash(f"Week {week} open for curses." if r["ok"] else r["reason"],
              "success" if r["ok"] else "error")
    elif action == "lock":
        r = engine.lock_week(db, LEAGUE_ID, SEASON, week, force=True)
        flash(f"Locked. {r.get('lineups', 0)} lineups snapshotted, "
              f"{r.get('curses_frozen', 0)} curses frozen." if r["ok"] else r["reason"],
              "success" if r["ok"] else "error")
    elif action == "unlock":
        r = engine.unlock_week(db, SEASON, week, me["roster_id"])
        flash(f"Week {week} reopened for curses. {r.get('thresholds_held', 0)} frozen "
              f"projection(s) held — late curses are graded against the kickoff number."
              if r["ok"] else r["reason"], "success" if r["ok"] else "error")
    elif action == "settle":
        r = engine.settle_week(db, LEAGUE_ID, SEASON, week, force=True)
        flash(f"Settled. {r.get('gooses', 0)} gooses, {r.get('curses_landed', 0)} curses landed."
              if r["ok"] else r["reason"], "success" if r["ok"] else "error")
    else:
        flash("Unknown action.", "error")
    return redirect(url_for("admin", tab="week"))


@app.route("/admin/curse", methods=["POST"])
def admin_curse():
    """
    Manual override, as the commissioner asked for. Used when someone forgets
    to cast, a curse needs voiding, or a blessing was earned off-app.
    """
    db = get_db()
    me = require_admin(db)
    if me is None:
        abort(403)
    action = request.form.get("action")
    week = request.form.get("week", type=int) or active_week(db)

    if action == "grant-curse":
        caster = request.form.get("caster", type=int)
        target = request.form.get("target", type=int)
        db.execute(
            "INSERT INTO curses (season, week, caster_roster_id, target_roster_id, status, "
            "created_by_admin, created_at) VALUES (%s, %s, %s, %s, 'cast', TRUE, %s)",
            (SEASON, week, caster, target, engine.now()),
        )
        db.commit()
        flash("Curse added by admin — no token was spent.", "success")
    elif action == "void-curse":
        cid = request.form.get("curse_id", type=int)
        row = db.execute("SELECT token_id FROM curses WHERE id = %s", (cid,)).fetchone()
        if row and row["token_id"]:
            db.execute("UPDATE curse_tokens SET spent_on = NULL WHERE id = %s", (row["token_id"],))
        db.execute("DELETE FROM curses WHERE id = %s AND season = %s", (cid, SEASON))
        db.commit()
        flash("Curse voided, token returned if there was one.", "success")
    elif action == "grant-token":
        rid = request.form.get("roster_id", type=int)
        engine.mint_token(db, SEASON, rid, week, "admin", "granted by admin")
        db.commit()
        flash("Token granted.", "success")
    elif action == "grant-blessing":
        rid = request.form.get("roster_id", type=int)
        db.execute(
            "INSERT INTO blessings (season, roster_id, earned_week, expires_after, status, "
            "created_by_admin, created_at) VALUES (%s, %s, %s, %s, 'active', TRUE, %s)",
            (SEASON, rid, week, week + 1, engine.now()),
        )
        db.commit()
        flash("Blessing granted for next week.", "success")
    elif action == "mark-wrath":
        rid = request.form.get("roster_id", type=int)
        # earned_week is THIS week, so the mark reads live from next week on --
        # the same offset a landed curse produces. Marking somebody and having
        # it bite them in the week they are already playing would grade a week
        # against a rule that was not in force when it started.
        if engine.arm_wrath(db, SEASON, rid, week, by_admin=True):
            db.commit()
            flash("Marked with Goosifer's Wrath from next week.", "success")
        else:
            flash("That owner is already marked for next week.", "error")
    elif action == "lift-wrath":
        rid = request.form.get("roster_id", type=int)
        n = engine.lift_wraths(db, SEASON, rid)
        db.commit()
        flash(f"{n} mark{'' if n == 1 else 's'} lifted." if n else "No active mark.",
              "success" if n else "error")
    elif action == "revoke-blessing":
        bid = request.form.get("blessing_id", type=int)
        db.execute(
            "UPDATE blessings SET status = 'expired', resolved_at = %s WHERE id = %s AND season = %s",
            (engine.now(), bid, SEASON),
        )
        db.commit()
        flash("Blessing revoked.", "success")
    else:
        flash("Unknown action.", "error")
    return redirect(url_for("admin", tab="curses"))


# --------------------------------------------------------------------------
# admin -- danger zone and demo mode
# --------------------------------------------------------------------------

def _typed_confirmation(expected: str) -> bool:
    """
    A reset is confirmed by TYPING it, not by clicking OK.

    A browser confirm() dialog would be one distracted click away from wiping a
    live season, and it also blocks automation dead. Making someone type
    "RESET WEEK 3" costs three seconds and cannot be done by accident.
    """
    return (request.form.get("confirm") or "").strip().upper() == expected.upper()


@app.route("/admin/reset", methods=["POST"])
def admin_reset():
    db = get_db()
    me = require_admin(db)
    if me is None:
        abort(403)
    action = request.form.get("action")

    if action == "reset-week":
        week = request.form.get("week", type=int) or active_week(db)
        if not _typed_confirmation(f"RESET WEEK {week}"):
            flash(f'Type "RESET WEEK {week}" exactly to confirm.', "error")
            return redirect(url_for("admin", tab="reset"))
        r = engine.reset_week(db, SEASON, week)
        flash(f"Week {week} cleared — {r['gooses']} goose(s), {r['chugs']} chug(s), "
              f"{r['lineups']} lineup slot(s) removed; {r['curses_rewound']} curse(s) "
              f"rewound to cast.", "success")

    elif action == "reset-season":
        if not _typed_confirmation(f"RESET SEASON {SEASON}"):
            flash(f'Type "RESET SEASON {SEASON}" exactly to confirm.', "error")
            return redirect(url_for("admin", tab="reset"))
        r = engine.reset_season(db, SEASON)
        flash(f"Season {SEASON} cleared back to an empty board. Owners, PINs and "
              f"rules were left alone.", "success")
    else:
        flash("Unknown action.", "error")
    return redirect(url_for("admin", tab="reset"))


@app.route("/admin/demo", methods=["POST"])
def admin_demo():
    """
    Demo mode on and off. See demo.py for what it does and does not touch.

    Building the snapshot has to happen with the seam OFF, so the order here is
    load-bearing: clear the seam, build against the real API, seed the props,
    and only then raise the flag.
    """
    db = get_db()
    me = require_admin(db)
    if me is None:
        abort(403)
    action = request.form.get("action")
    week = request.form.get("week", type=int) or active_week(db)

    if action == "demo-on":
        try:
            settingsmod.set_bool(db, demomod.FLAG, False)
            db.commit()
            payload = demomod.build(db, LEAGUE_ID, SEASON, week)
            rosters = engine.roster_ids(db, LEAGUE_ID, SEASON)
            demomod.clear_props(db)
            props = demomod.seed_props(db, SEASON, week, rosters)
            settingsmod.set_bool(db, demomod.FLAG, True)
            db.commit()
            demomod.install(db)
            flash(f"Demo mode on — week {week}, {payload['starters']} starters, "
                  f"{len(payload['gooses'])} planted gooses, "
                  f"{props.get('curses', 0)} curse(s) in play. Nothing real was touched.",
                  "success")
        except Exception as exc:
            db.rollback()
            settingsmod.set_bool(db, demomod.FLAG, False)
            db.commit()
            sleeper.set_demo(None)
            flash(f"Could not build the demo ({type(exc).__name__}). Demo mode left off.",
                  "error")

    elif action == "demo-off":
        removed = demomod.clear_props(db)
        settingsmod.set_bool(db, demomod.FLAG, False)
        settingsmod.set_raw(db, demomod.PAYLOAD, "")
        db.commit()
        sleeper.set_demo(None)
        sleeper.clear_cache()
        flash(f"Demo mode off — {sum(removed.values())} demo row(s) removed, "
              f"live Sleeper data restored.", "success")
    else:
        flash("Unknown action.", "error")
    return redirect(url_for("admin", tab="demo"))


# --------------------------------------------------------------------------
# automation
# --------------------------------------------------------------------------

@app.route("/poll")
def poll():
    """
    Hit on a schedule by an external pinger (UptimeRobot, same as the FAAB
    app). Every job is individually guarded and individually try/excepted --
    one failing job must never stop the others, and none of them may run twice.
    """
    if POLL_SECRET and request.args.get("secret") != POLL_SECRET:
        abort(403)
    db = get_db()
    done = []
    now_ts = int(time.time())

    try:
        n = players_sync.sync(db)
        if n:
            done.append(f"players:{n}")
    except Exception as exc:
        # Every job in this route is individually guarded, and the rollback is
        # part of the guard: without it a failed sync aborts the transaction and
        # takes the lock and settle jobs below down with it, which is the exact
        # opposite of individually guarded.
        db.rollback()
        done.append(f"players:failed({type(exc).__name__})")

    week = active_week(db)
    wk = engine.ensure_week(db, SEASON, week)
    db.commit()

    # Demo mode hands this app a Sunday where four games are already final. If
    # auto-settle ran against that it would raise REAL chugs, mint REAL tokens
    # and resolve REAL curses off invented scores -- the one way a demo that
    # writes nothing could still wreck a season. Everything that changes state
    # stops here while the seam is installed; the read-only screens carry on.
    if sleeper.demo_payload():
        done.append("demo:automation-paused")
        return {"ok": True, "week": week, "demo": True, "did": done}

    if settingsmod.get_bool(db, "auto_lock", True) and wk["status"] == "open":
        lock_at = wk["lock_epoch"] or sleeper.week_lock_epoch(SEASON, week)
        if lock_at and now_ts >= lock_at:
            try:
                r = engine.lock_week(db, LEAGUE_ID, SEASON, week)
                done.append(f"locked:{r.get('lineups', 0)}")
            except Exception as exc:
                db.rollback()
                done.append(f"lock:failed({type(exc).__name__})")

    if settingsmod.get_bool(db, "auto_settle", True) and wk["status"] == "locked":
        end_at = wk["end_epoch"] or sleeper.week_end_epoch(SEASON, week)
        if end_at and now_ts >= end_at:
            try:
                r = engine.settle_week(db, LEAGUE_ID, SEASON, week)
                done.append(f"settled:{r.get('gooses', 0)}")
            except Exception as exc:
                db.rollback()
                done.append(f"settle:failed({type(exc).__name__})")

    if settingsmod.get_bool(db, "auto_advance", True):
        wk = engine.ensure_week(db, SEASON, week)
        end_week = settingsmod.get_int(db, "season_end_week", 17) or 17
        if wk["status"] == "final" and week < end_week:
            settingsmod.set_raw(db, "active_week", week + 1)
            db.commit()
            done.append(f"advanced:{week + 1}")

    return {"ok": True, "week": week, "did": done}


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", code=403,
                           message="That screen is admin-only."), 403


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404,
                           message="No such page."), 404


if __name__ == "__main__":
    app.run(debug=True, port=5001)
