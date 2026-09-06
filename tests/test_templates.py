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

import goose  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures: list[str] = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


def build_env():
    env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "templates")))
    env.filters["odds"] = lambda p: "--" if p is None else goose.american_price(float(p))
    env.filters["pct"] = lambda p: "--" if p is None else f"{float(p) * 100:.0f}%"
    env.filters["pts"] = lambda v: "--" if v is None else f"{float(v):.1f}"
    env.globals["url_for"] = lambda ep, **kw: f"/{ep}"
    env.globals["get_flashed_messages"] = lambda **kw: []
    env.globals["request"] = type("R", (), {"endpoint": "board"})()
    return env


ME = {"roster_id": 1, "owner_name": "Conner", "team_name": "The Melange Moguls", "is_admin": True}
OWNERS = {i: {"roster_id": i, "owner_name": f"o{i}", "team_name": f"Team {i}", "avatar": None}
          for i in range(1, 5)}
WK = {"week": 1, "status": "open", "lock_epoch": 1, "end_epoch": 2,
      "opened_at": 1, "locked_at": None, "settled_at": None}

ROW = {"roster_id": 1, "team": "The Melange Moguls", "owner_name": "Conner", "avatar": None,
       "chug_odds": 0.26, "proj_total": 153.4, "actual_total": None, "goose_count": 0,
       "curses": [{"id": 1, "status": "cast", "caster": "Team 2", "mine": False, "sealed": True}],
       "blessed": False, "is_me": True}

CASES = {
    "login.html": {"owners": [{"roster_id": 1, "owner_name": "a", "team_name": "T"}], "me": None},
    "error.html": {"code": 404, "message": "nope", "me": ME},
    "board.html": {
        "me": ME, "week": 1, "wk": WK, "rows": [ROW], "my_row": ROW, "my_tokens": 2,
        "my_blessed": False, "weeks": [1], "sealed": True, "targets": [ROW],
    },
    "my_geese.html": {
        "me": ME, "week": 1, "owners": OWNERS, "label": lambda o: (o or {}).get("team_name", "?"),
        "lineup": [{"slot": "FLEX", "player_id": "p1", "full_name": "A Player", "position": "WR",
                    "team": "TB", "injury_status": "Questionable", "proj_pts": 6.8,
                    "actual_pts": None, "goose_prob": 0.12, "is_goose": None}],
        "gooses": [{"week": 1, "slot": "FLEX", "player_id": "p1", "full_name": "A Player",
                    "position": "WR", "team": "TB", "points": 0.0, "empty_slot": False}],
        "chugs": [{"id": 1, "week": 1, "reason": "goose", "status": "owed",
                   "safe_pour": False, "rolled_from_week": None}],
        "curses": [{"id": 1, "week": 1, "caster_roster_id": 1, "target_roster_id": 2,
                    "status": "landed", "threshold_proj": 150.0, "actual_total": 140.0}],
        "team_week": {"proj_total": 153.4, "actual_total": None, "chug_odds": 0.26},
        "my_curse": {"threshold_proj": 153.4}, "tokens": 2,
        "blessing": {"expires_after": 2},
    },
    "watch.html": {
        "me": ME, "week": 3, "problem": None,
        "me_roster": 1,
        "data": {
            "week": 3, "total_goosed": 2, "total_danger": 1,
            "games_live": 5, "games_final": 3, "games_total": 13, "feed_ok": True,
            "drinkers": [{"roster_id": 1, "owner": "Team 1", "avatar": None, "goosed": 2}],
            "goosed": [{"name": "A Player", "owner": "Team 1", "slot": "FLEX", "position": "WR",
                        "nfl_team": "TB", "points": 0.0, "clock": "FINAL", "empty_slot": False,
                        "goose_prob": 0.12, "player_id": "p1", "opponent": "ATL",
                        "photo": "https://sleepercdn.com/content/nfl/players/thumb/p1.jpg"},
                       {"name": "Empty slot", "owner": "Team 1", "slot": "TE", "position": None,
                        "nfl_team": None, "points": None, "clock": "empty slot",
                        "empty_slot": True, "goose_prob": 1.0, "player_id": None,
                        "opponent": None, "photo": None}],
            "danger": [{"name": "B Player", "owner": "Team 2", "slot": "FLEX", "position": "RB",
                        "nfl_team": "KC", "points": 0.0, "clock": "Q4 2:10", "empty_slot": False,
                        "goose_prob": 0.09, "player_id": "p2", "opponent": "DEN",
                        "photo": "https://sleepercdn.com/content/nfl/players/thumb/p2.jpg"}],
            "pending": [{"name": "C Player", "owner": "Team 3", "slot": "WR", "position": "WR",
                         "nfl_team": "SF", "points": 0.0, "clock": "not started",
                         "empty_slot": False, "goose_prob": 0.04, "player_id": "p3",
                         "opponent": "SEA",
                         "photo": "https://sleepercdn.com/content/nfl/players/thumb/p3.jpg"}],
            "cleared": [{"name": "D Player", "owner": "Team 4", "slot": "FLEX", "position": "WR",
                         "nfl_team": "TB", "points": 6.4, "clock": "FINAL", "empty_slot": False,
                         "goose_prob": 0.31, "player_id": "p4", "opponent": "ATL",
                         "photo": "https://sleepercdn.com/content/nfl/players/thumb/p4.jpg"}],
            "rows": [], "per_owner": [],
        },
    },
    "watch.html::problem": {
        "me": ME, "week": 3, "me_roster": 1, "data": None,
        "problem": "Could not reach Sleeper (Timeout).",
    },
    "standings.html": {
        "me": ME,
        "rows": [{"rank": 1, "roster_id": 1, "team": "Team 1", "avatar": None, "chugs": 9,
                  "paid": 7, "owed": 2, "geese": 6, "from_curses": 3, "curses_landed": 2,
                  "curses_failed": 3, "blessings": 0, "tokens": 1, "is_me": True}],
        "assassin": {"team": "Team 3", "curses_landed": 4},
        "teflon": {"team": "Team 4", "geese": 1},
    },
}

for tab in ("chugs", "week", "curses", "rules"):
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
        "active": 1, "wk": WK,
        "rules": [{"key": "points_per_chug", "kind": "int", "label": "Tokens per chug",
                   "value": 1, "default": "1"},
                  {"key": "curse_stacking", "kind": "bool", "label": "Stacking",
                   "value": False, "default": "0"}],
        "now": 3, "lock_epoch": 1, "end_epoch": 2,
    }


def main() -> int:
    env = build_env()
    print("templates compile and render")
    for name, ctx in CASES.items():
        tpl_name = name.split("::")[0]
        try:
            html = env.get_template(tpl_name).render(
                season=2026, pending_chugs=1, app_version="0.1.0", **ctx
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
