"""
app.py
======
Flask front end for the GOOSE EGG Challenge.

Thin on purpose: every state change lives in week_engine.py and every rule
lives in settings.py. Routes authenticate, read, and render.

Tabs mirror the design canvas: Board, Watch, My Geese, Standings, Admin.
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
import goose
import players_sync
import settings as settingsmod
import sleeper
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
    if not hasattr(request, "_goose_db"):
        request._goose_db = dbmod.open_wrapped()
    return request._goose_db


@app.teardown_request
def _close_db(exc):
    db = getattr(request, "_goose_db", None)
    if db is not None:
        if exc:
            db.rollback()
        db.close()


@app.template_filter("odds")
def odds_filter(probability):
    """0.26 -> '+285'. Empty rather than a fake price when we have no number."""
    if probability is None:
        return "--"
    return goose.american_price(float(probability))


@app.template_filter("pct")
def pct_filter(probability):
    if probability is None:
        return "--"
    return f"{float(probability) * 100:.0f}%"


@app.template_filter("pts")
def pts_filter(value):
    return "--" if value is None else f"{float(value):.1f}"


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


@app.context_processor
def inject_globals():
    db = getattr(request, "_goose_db", None)
    owner = current_owner(db) if db is not None else None
    pending = 0
    if db is not None and owner is not None and owner["is_admin"]:
        pending = db.execute(
            "SELECT COUNT(*) AS n FROM chugs WHERE season = %s AND status = 'owed'", (SEASON,)
        ).fetchone()["n"]
    return {
        "me": owner,
        "season": SEASON,
        "pending_chugs": pending,
        "app_version": open(os.path.join(os.path.dirname(__file__), "VERSION")).read().strip(),
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

    tw = {
        r["roster_id"]: r for r in db.execute(
            "SELECT * FROM team_weeks WHERE season = %s AND week = %s", (SEASON, week)
        ).fetchall()
    }

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

    curse_by_target: dict = {}
    for c in curses:
        visible = (not sealed) or c["caster_roster_id"] == me["roster_id"]
        curse_by_target.setdefault(c["target_roster_id"], []).append({
            "id": c["id"],
            "status": c["status"],
            "caster": label(owners.get(c["caster_roster_id"])) if visible else None,
            "mine": c["caster_roster_id"] == me["roster_id"],
            "sealed": not visible,
        })

    rows = []
    for rid, owner in owners.items():
        t = tw.get(rid)
        rows.append({
            "roster_id": rid,
            "team": label(owner),
            "owner_name": owner["owner_name"],
            "avatar": owner["avatar"],
            "chug_odds": (t or {}).get("chug_odds"),
            "proj_total": (t or {}).get("proj_total"),
            "actual_total": (t or {}).get("actual_total"),
            "goose_count": (t or {}).get("goose_count") or 0,
            "curses": curse_by_target.get(rid, []),
            "blessed": rid in blessed,
            "is_me": rid == me["roster_id"],
        })
    rows.sort(key=lambda r: (r["chug_odds"] is None, -(r["chug_odds"] or 0)))

    my_tokens = engine.unspent_tokens(db, SEASON, me["roster_id"])
    my_row = next((r for r in rows if r["is_me"]), None)

    weeks = [r["week"] for r in db.execute(
        "SELECT week FROM weeks WHERE season = %s ORDER BY week", (SEASON,)
    ).fetchall()] or [week]

    return render_template(
        "board.html", week=week, wk=wk, rows=rows, my_row=my_row,
        my_tokens=len(my_tokens), my_blessed=me["roster_id"] in blessed,
        weeks=weeks, sealed=sealed,
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

    lineup = db.execute(
        "SELECT l.*, p.full_name, p.position, p.team, p.injury_status "
        "FROM lineup_slots l LEFT JOIN players_cache p ON p.player_id = l.player_id "
        "WHERE l.season = %s AND l.week = %s AND l.roster_id = %s ORDER BY l.goose_prob DESC",
        (SEASON, week, rid),
    ).fetchall()

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

    return render_template(
        "my_geese.html", week=week, lineup=lineup, gooses=gooses, chugs=chugs,
        curses=curses, owners=owners, team_week=tw, my_curse=my_curse,
        tokens=len(engine.unspent_tokens(db, SEASON, rid)),
        blessing=engine.active_blessing(db, SEASON, rid, week),
        label=label,
    )


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
        problem = (f"Could not reach Sleeper ({type(exc).__name__}). This screen reads "
                   f"live scores on every load, so there is nothing to show until it "
                   f"is back. Nothing is lost — the week is graded from final scores "
                   f"when it settles.")

    return render_template("watch.html", week=week, data=data, problem=problem,
                           me_roster=me["roster_id"])



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
            "tokens": len(engine.unspent_tokens(db, SEASON, rid)),
            "is_me": rid == me["roster_id"],
        })

    # The crown goes to the DRUNKEST owner. Ties break on gooses, then on
    # curses landed -- doing it the hard way beats being handed it.
    rows.sort(key=lambda r: (-r["chugs"], -r["geese"], -r["curses_landed"]))
    for i, r in enumerate(rows):
        r["rank"] = i + 1

    assassin = max(rows, key=lambda r: r["curses_landed"]) if rows else None
    teflon = min(rows, key=lambda r: r["geese"]) if rows else None
    return render_template("standings.html", rows=rows, assassin=assassin, teflon=teflon)


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

    wk = engine.ensure_week(db, SEASON, week)
    db.commit()
    lock_epoch = wk["lock_epoch"] or sleeper.week_lock_epoch(SEASON, week)
    end_epoch = wk["end_epoch"] or sleeper.week_end_epoch(SEASON, week)

    return render_template(
        "admin.html", tab=tab, owners=owners, label=label, chugs=chugs,
        weeks=weeks, curses=curses, blessings=blessings, active=week, wk=wk,
        rules=settingsmod.all_tunables(db), now=int(time.time()),
        lock_epoch=lock_epoch, end_epoch=end_epoch,
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
        done.append(f"players:failed({type(exc).__name__})")

    week = active_week(db)
    wk = engine.ensure_week(db, SEASON, week)
    db.commit()

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
