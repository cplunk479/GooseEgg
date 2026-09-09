"""
tests/test_templates.py
=======================
Compiles and renders every template with the app's real custom filters, so a
typo in a filter name or a broken Jinja block fails here rather than on
someone's phone at 3pm on a Sunday.

    python tests/test_templates.py

Renders against representative fake context -- it proves the templates are
valid and reachable, not that the numbers on them are right.
"""
from __future__ import annotations

import os
import sys

from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import filters as filtersmod  # noqa: E402
import goose  # noqa: E402
import themes as themesmod  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures: list[str] = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


def build_env():
    from jinja2 import StrictUndefined
    env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "templates")),
                      undefined=StrictUndefined)
    # The app's real filters, not a copy of them. See filters.py.
    env.filters.update(filtersmod.FILTERS)
    env.globals["url_for"] = lambda ep, **kw: f"/{ep}"
    env.globals["get_flashed_messages"] = lambda **kw: []
    env.globals["request"] = type(
        "R", (), {"endpoint": "board", "full_path": "/?"})()
    return env


ME = {"roster_id": 1, "owner_name": "Conner", "team_name": "The Melange Moguls", "is_admin": True}
OWNERS = {i: {"roster_id": i, "owner_name": f"o{i}", "team_name": f"Team {i}", "avatar": None}
          for i in range(1, 5)}
WK = {"week": 1, "status": "open", "lock_epoch": 1, "end_epoch": 2,
      "opened_at": 1, "locked_at": None, "settled_at": None}

ROW = {"roster_id": 1, "team": "The Melange Moguls", "owner_name": "Conner", "avatar": None,
       "proj_total": 153.4, "actual_total": None, "goose_count": 0,
       "risk_tier": "EXPOSED", "risk_score": 1.36, "risk_rate": 0.30, "at_risk": 2,
       "curses": [{"id": 1, "status": "cast", "caster": "Team 2", "mine": False,
                   "sealed": True, "threshold": 150.0}],
       "cast_on_them": 1, "blessed": False, "wrathed": False, "is_me": True}

# A second row with nothing known about it, because "no projection yet" is a
# real state all week and the board has to render it without a tier.
BLANK_ROW = {**ROW, "roster_id": 2, "team": "Team 2", "is_me": False, "proj_total": None,
             "risk_tier": None, "risk_score": None, "risk_rate": None, "at_risk": 0,
             "curses": [], "cast_on_them": 0, "blessed": True, "wrathed": False}

# A third row carrying Goosifer's Wrath. It has to be its own row rather than a
# flag on BLANK_ROW: the marked branch and the blessed branch are mutually
# exclusive in the template, so one row can only ever compile one of them.
WRATHED_ROW = {**BLANK_ROW, "roster_id": 3, "team": "Team 3", "blessed": False,
               "wrathed": True}

MOST_CURSED = {"roster_id": 3, "team": "Team 3", "avatar": None, "count": 4, "landed": 2}
CROWN = {"roster_id": 3, "team": "Team 3", "avatar": None, "chugs": 9, "geese": 6,
         "curses_landed": 2, "tied": 0}

CASES = {
    "login.html": {"owners": [{"roster_id": 1, "owner_name": "a", "team_name": "T"}], "me": None},
    "error.html": {"code": 404, "message": "nope", "me": ME},
    "board.html": {
        "me": ME, "week": 1, "wk": WK, "rows": [ROW, BLANK_ROW, WRATHED_ROW], "my_row": ROW,
        "my_tokens": 2, "my_blessed": False, "my_wrathed": False, "wrath_mult": 2,
        "weeks": [1], "sealed": True,
        "preview": True, "can_cast": True, "most_cursed": MOST_CURSED, "crown": CROWN,
        "targets": [BLANK_ROW],
    },
    "board.html::locked": {
        "_template": "board.html",
        "me": ME, "week": 1, "wk": {**WK, "status": "locked"}, "rows": [ROW, BLANK_ROW, WRATHED_ROW],
        "my_row": ROW, "my_tokens": 0, "my_blessed": True, "my_wrathed": True,
        "wrath_mult": 2, "weeks": [1], "sealed": False,
        "preview": False, "can_cast": False, "most_cursed": None, "crown": None,
        "targets": [BLANK_ROW],
    },
    "my_geese.html": {
        "me": ME, "week": 1, "wk": WK, "owners": OWNERS,
        "label": lambda o: (o or {}).get("team_name", "?"),
        "preview": True, "proj_total": 153.4,
        "wrath": {"id": 1, "earned_week": 1, "expires_after": 2, "status": "active"},
        "wrath_mult": 2,
        "team_risk": {"tier": "EXPOSED", "score": 1.36, "rate": 0.30, "at_risk": 2,
                      "worst": "COOKED", "counts": {}},
        # One live-preview row (the shape project_week returns) and one frozen
        # snapshot row (the shape lineup_slots returns). The template has to
        # render both, and they do not use the same key names.
        "lineup": [{"slot": "FLEX", "player_id": "p1", "name": "A Player", "position": "WR",
                    "nfl_team": "TB", "projection": 6.8, "tier": "GOOSE BAIT",
                    "ratio": 0.55, "reason": None, "photo": "/x.jpg",
                    "actual_pts": None, "is_goose": None},
                   {"slot": "QB", "player_id": None, "name": None, "position": None,
                    "nfl_team": None, "projection": 0.0, "tier": "COOKED", "ratio": None,
                    "reason": "EMPTY SLOT", "photo": None,
                    "actual_pts": 0.0, "is_goose": True}],
        "gooses": [{"week": 1, "slot": "FLEX", "player_id": "p1", "full_name": "A Player",
                    "position": "WR", "team": "TB", "points": 0.0, "empty_slot": False}],
        "chugs": [{"id": 1, "week": 1, "reason": "goose", "status": "owed",
                   "safe_pour": False, "rolled_from_week": None}],
        "curses": [{"id": 1, "week": 1, "caster_roster_id": 1, "target_roster_id": 2,
                    "status": "landed", "threshold_proj": 150.0, "actual_total": 140.0}],
        "team_week": {"proj_total": 153.4, "actual_total": None,
                      "risk_tier": "EXPOSED", "risk_score": 1.36, "at_risk": 2},
        "my_curse": {"threshold_proj": 153.4}, "tokens": 2,
        "blessing": {"expires_after": 2},
    },
    "watch.html": {
        "me": ME, "week": 3, "problem": None,
        "me_roster": 1, "wrathed": {1},
        "data": {
            "week": 3, "total_goosed": 2, "total_danger": 1,
            "games_live": 5, "games_final": 3, "games_total": 13, "feed_ok": True,
            "drinkers": [{"roster_id": 1, "owner": "Team 1", "avatar": None, "goosed": 2}],
            "goosed": [{"name": "A Player", "roster_id": 1, "owner": "Team 1", "slot": "FLEX", "position": "WR",
                        "nfl_team": "TB", "points": 0.0, "clock": "FINAL", "empty_slot": False, "tier": "COOKED", "risk_reason": "OUT",
                        "seconds_left": 0, "goose_prob": 0.108, "player_id": "p1", "opponent": "ATL",
                        "photo": "https://sleepercdn.com/content/nfl/players/thumb/p1.jpg"},
                       {"name": "Empty slot", "roster_id": 1, "owner": "Team 1", "slot": "TE", "position": None,
                        "nfl_team": None, "points": None, "clock": "empty slot",
                        "empty_slot": True, "tier": "COOKED", "risk_reason": "EMPTY SLOT",
                        "seconds_left": None, "goose_prob": 0.108, "player_id": None,
                        "opponent": None, "photo": None}],
            "danger": [{"name": "B Player", "roster_id": 2, "owner": "Team 2", "slot": "FLEX", "position": "RB",
                        "nfl_team": "KC", "points": 0.0, "clock": "Q4 2:10", "empty_slot": False,
                        "tier": "SOLID", "risk_reason": None, "seconds_left": 130,
                        "goose_prob": 0.018, "player_id": "p2", "opponent": "DEN",
                        "photo": "https://sleepercdn.com/content/nfl/players/thumb/p2.jpg"}],
            "pending": [{"name": "C Player", "roster_id": 3, "owner": "Team 3", "slot": "WR", "position": "WR",
                         "nfl_team": "SF", "points": 0.0, "clock": "not started",
                         "empty_slot": False, "tier": "SHAKY", "risk_reason": None,
                         "seconds_left": None, "goose_prob": 0.033, "player_id": "p3",
                         "opponent": "SEA",
                         "photo": "https://sleepercdn.com/content/nfl/players/thumb/p3.jpg"}],
            "cleared": [{"name": "D Player", "roster_id": 4, "owner": "Team 4", "slot": "FLEX", "position": "WR",
                         "nfl_team": "TB", "points": 6.4, "clock": "FINAL", "empty_slot": False, "tier": "GOOSE BAIT", "risk_reason": None,
                         "seconds_left": 0, "goose_prob": 0.059, "player_id": "p4",
                         "opponent": "ATL",
                         "photo": "https://sleepercdn.com/content/nfl/players/thumb/p4.jpg"}],
            "rows": [], "per_owner": [],
        },
    },
    "watch.html::problem": {
        "me": ME, "week": 3, "me_roster": 1, "wrathed": set(), "data": None,
        "problem": "Could not reach Sleeper (Timeout).",
    },
    "standings.html": {
        "most_cursed": MOST_CURSED,
        "me": ME,
        "rows": [{"rank": 1, "roster_id": 1, "team": "Team 1", "avatar": None, "chugs": 9,
                  "paid": 7, "owed": 2, "geese": 6, "from_curses": 3, "curses_landed": 2,
                  "curses_failed": 3, "blessings": 0, "wraths_cashed": 2, "wrathed": True, "tokens": 1, "is_me": True, "tied": 1},
                 {"rank": 2, "roster_id": 2, "team": "Team 2", "avatar": None, "chugs": 9,
                  "paid": 9, "owed": 0, "geese": 5, "from_curses": 1, "curses_landed": 1,
                  "curses_failed": 0, "blessings": 1, "wraths_cashed": 0, "wrathed": False, "tokens": 0, "is_me": False},
                 {"rank": 3, "roster_id": 3, "team": "Team 3", "avatar": None, "chugs": 4,
                  "paid": 4, "owed": 0, "geese": 4, "from_curses": 0, "curses_landed": 0,
                  "curses_failed": 2, "blessings": 0, "wraths_cashed": 0, "wrathed": False, "tokens": 2, "is_me": False},
                 {"rank": 4, "roster_id": 4, "team": "Team 4", "avatar": None, "chugs": 0,
                  "paid": 0, "owed": 0, "geese": 0, "from_curses": 0, "curses_landed": 0,
                  "curses_failed": 0, "blessings": 0, "wraths_cashed": 1, "wrathed": False, "tokens": 0, "is_me": False}],
        "assassin": {"team": "Team 3", "curses_landed": 4},
        "teflon": {"team": "Team 4", "geese": 1},
    },
}

for tab in ("chugs", "week", "curses", "rules", "demo", "reset"):
    CASES[f"admin.html::{tab}"] = {
        "me": ME, "tab": tab, "owners": OWNERS,
        "label": lambda o: (o or {}).get("team_name", "?"),
        "chugs": [{"id": 1, "roster_id": 1, "week": 1, "reason": "goose", "status": "owed",
                   "safe_pour": False, "rolled_from_week": None},
                  {"id": 2, "roster_id": 2, "week": 1, "reason": "curse", "status": "paid",
                   "safe_pour": True, "rolled_from_week": None}],
        "weeks": [WK], "curses": [{"id": 1, "week": 1, "caster_roster_id": 1,
                                   "target_roster_id": 2, "status": "cast",
                                   "threshold_proj": 150.0, "created_by_admin": False}],
        "blessings": [{"id": 1, "roster_id": 2, "earned_week": 1, "expires_after": 2,
                       "status": "active"}],
        # One active mark, one spent, and one persistent (NULL expiry) -- the
        # three shapes the row can take, and the NULL is its own branch.
        "wraths": [{"id": 1, "roster_id": 3, "earned_week": 1, "expires_after": 2,
                    "status": "active", "created_by_admin": False},
                   # earned_week 0 is the demo's week-1 mark: it has to render as
                   # "preseason" rather than "week 0", and that branch only
                   # compiles if a case actually carries it.
                   {"id": 2, "roster_id": 4, "earned_week": 0, "expires_after": None,
                    "status": "active", "created_by_admin": True},
                   {"id": 3, "roster_id": 2, "earned_week": 1, "expires_after": 2,
                    "status": "consumed", "created_by_admin": False}],
        "active": 1, "wk": WK,
        "rules": [{"key": "points_per_chug", "kind": "int", "label": "Tokens per chug",
                   "value": 1, "default": "1"},
                  {"key": "curse_stacking", "kind": "bool", "label": "Stacking",
                   "value": False, "default": "0"}],
        "now": 3, "lock_epoch": 1, "end_epoch": 2,
        "demo_on": False, "demo": None,
    }

# The demo tab renders differently depending on whether the seam is installed,
# and the ON branch is the one that reads fields off the payload -- so it needs
# its own case or half that tab is never compiled.
CASES["admin.html::demo-on"] = {
    **CASES["admin.html::demo"],
    "demo_on": True,
    "demo": {"week": 1, "starters": 132, "gooses": ["p1", "p2", "p3", "p4", "p5"]},
}

# Preseason: every owner on zero chugs. The crown is vacant and no row wears
# podium flair -- a branch that only exists before week 1 settles, and the one
# most likely to rot unseen.
CASES["standings.html::vacant"] = {
    **CASES["standings.html"],
    "most_cursed": None,
    "rows": [{**r, "chugs": 0, "paid": 0, "owed": 0, "geese": 0, "curses_landed": 0,
              "tied": 0}
             for r in CASES["standings.html"]["rows"]],
}

# Every template renders with a strict Undefined, so a key the app forgets to
# pass fails here instead of on a phone. This is the setting that caught the
# my_geese preview/snapshot key mismatch.
STRICT = True


def main() -> int:
    env = build_env()
    print("templates compile and render")
    for name, ctx in CASES.items():
        tpl_name = name.split("::")[0]
        try:
            ctx = {k: v for k, v in ctx.items() if k != "_template"}
            html = env.get_template(tpl_name).render(
                season=2026, pending_chugs=1, app_version="0.1.0",
                demo_mode=False, tier_order=list(goose.TIERS),
                theme=themesmod.DEFAULT_THEME, themes=themesmod.THEMES, **ctx
            )
            check(name, len(html) > 200, f"only {len(html)} chars")
        except Exception as exc:
            check(name, False, f"{type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
