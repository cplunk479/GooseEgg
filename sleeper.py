"""
sleeper.py
==========
Everything this app knows how to ask Sleeper. No auth required.

Lessons carried over from faab-platform -- each of these cost a debugging
session over there, do not relearn them:

  1. The league_id CHANGES EVERY SEASON, chained via `previous_league_id`.
     Never hardcode one anywhere but the environment.
  2. Kickoff times come from `/scores/nfl/regular/<season>/<week>`, NOT
     `/schedule/...` -- the schedule path 404s for every season. The bug that
     found this passed every mocked test for a day because the mock proved the
     PARSING and never the URL.
  3. Point values must be computed from the league's real `scoring_settings`.
     Sleeper's precomputed pts_ppr/pts_std fields are a stock formula and are
     wrong here by 20%+ -- Dynasty Dons has 7-point TDs, first-down bonuses and
     a TE premium.
  4. `/players/nfl` is several MB. Sleeper asks for at most ~one pull a day.

The scores feed does double duty: it is the kickoff clock AND the bye-week
oracle. A team with no game in a week is on bye, and every player on it is a
near-certain goose if left in a lineup.
"""
from __future__ import annotations

import time
from typing import Any

import requests

BASE_V1 = "https://api.sleeper.app/v1"
BASE_V2 = "https://api.sleeper.com"
SCORES_URL = "https://api.sleeper.app/scores/nfl/regular/{season}/{week}"
PROJECTIONS_URL = (
    BASE_V2 + "/projections/nfl/{season}/{week}"
    "?season_type=regular"
    "&position[]=QB&position[]=RB&position[]=WR&position[]=TE"
)

HTTP_TIMEOUT = 20
PLAYERS_TIMEOUT = 60  # multi-MB payload
_SCORES_TTL = 3600    # a published schedule does not move


def _get_json(url: str, timeout: int = HTTP_TIMEOUT) -> Any:
    resp = requests.get(url, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"GET {url} -> HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


# --------------------------------------------------------------------------
# Response cache
# --------------------------------------------------------------------------
#
# Added in v0.5 because the Board and My Geese now render a LIVE projection
# preview before a week locks, and without this every page load would pull the
# league, the matchups and the full week's projections again. TTLs are set by
# how fast each thing actually moves, not by taste:
#
#   league shape   a whole season          rosters change, scoring does not
#   projections    15 minutes              Sleeper revises these through the week
#   matchups        2 minutes              starters change up to kickoff
#
# In-process only, so each Render web worker keeps its own copy. That is fine:
# the worst case is one owner seeing a two-minute-old lineup, and the numbers
# that MATTER are the ones lock_week freezes into the database, which never
# come from here.

_TTL_LEAGUE = 6 * 3600
_TTL_PROJECTIONS = 900
_TTL_MATCHUPS = 120

_cache: dict = {}


def _cached(key, ttl: int, producer):
    hit = _cache.get(key)
    if hit and (time.time() - hit[1]) < ttl:
        return hit[0]
    value = producer()
    _cache[key] = (value, time.time())
    return value


def clear_cache() -> None:
    """Drop every cached response. Called when demo mode is switched."""
    _cache.clear()
    _scores_cache.clear()


# --------------------------------------------------------------------------
# Demo mode seam
# --------------------------------------------------------------------------
#
# When a demo snapshot is installed, the three feeds that carry a week's
# RESULTS come from it instead of from Sleeper. Everything else -- the league's
# roster slots, its scoring settings, the player directory -- still comes from
# the real API, because those are the parts that make the demo look like this
# league rather than a mock-up.
#
# One seam, three functions, and it lives here rather than in the callers on
# purpose: every screen, the week engine and the tier model then run on demo
# data completely unmodified, which is the only way the demo proves anything
# about the real thing. See demo.py.
#
# The week guard matters. A snapshot is built for one week; asking for any
# other week falls through to the real API rather than serving week 1's scores
# under week 4's heading.

_demo: dict | None = None


def set_demo(payload: dict | None) -> None:
    global _demo
    _demo = payload or None


def demo_payload() -> dict | None:
    return _demo


def _demo_for(week: int | None = None) -> dict | None:
    if not _demo:
        return None
    if week is not None and int(_demo.get("week", -1)) != int(week):
        return None
    return _demo


# --------------------------------------------------------------------------
# League shape
# --------------------------------------------------------------------------

def league(league_id: str) -> dict:
    return _cached(("league", league_id), _TTL_LEAGUE,
                   lambda: _get_json(f"{BASE_V1}/league/{league_id}"))


def scoring_settings(league_id: str) -> dict:
    return league(league_id).get("scoring_settings") or {}


def starting_slots(league_id: str) -> list[str]:
    """roster_positions with the bench stripped -- Dynasty Dons 2026 is
    QB, RB, WR, WR, WR, TE, FLEX x4, SUPER_FLEX (11 starters, no K/DEF/IDP)."""
    positions = league(league_id).get("roster_positions") or []
    return [p for p in positions if p != "BN"]


def users(league_id: str) -> list[dict]:
    return _get_json(f"{BASE_V1}/league/{league_id}/users")


def rosters(league_id: str) -> list[dict]:
    return _get_json(f"{BASE_V1}/league/{league_id}/rosters")


def matchups(league_id: str, week: int) -> list[dict]:
    """
    Every roster's starters and starters_points for a week.

    Works in the fantasy PLAYOFFS too, which matters here: the challenge runs
    weeks 1-17 and eliminated teams stay in consolation matchups. Sleeper
    returns every roster in those weeks, so no bracket handling is needed --
    but confirm at build time whether this league plays week 18.
    """
    demo = _demo_for(week)
    if demo is not None:
        return demo.get("matchups") or []
    return _cached(("matchups", league_id, week), _TTL_MATCHUPS,
                   lambda: _get_json(f"{BASE_V1}/league/{league_id}/matchups/{week}"))


# --------------------------------------------------------------------------
# Projections and scoring
# --------------------------------------------------------------------------

def projections(season: int, week: int) -> dict:
    """player_id -> raw stat projection dict."""
    demo = _demo_for(week)
    if demo is not None:
        return demo.get("projections") or {}
    return _cached(
        ("projections", season, week), _TTL_PROJECTIONS,
        lambda: normalize_projections(
            _get_json(PROJECTIONS_URL.format(season=season, week=week))),
    )


def normalize_projections(raw: Any) -> dict:
    if isinstance(raw, dict):
        return {
            str(pid): (entry.get("stats", entry) if isinstance(entry, dict) else {})
            for pid, entry in raw.items()
        }
    if isinstance(raw, list):
        out: dict = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            pid = entry.get("player_id") or entry.get("id")
            if pid is None:
                continue
            out[str(pid)] = entry.get("stats", entry)
        return out
    raise RuntimeError(f"Unexpected projections payload: {type(raw).__name__}")


def player_points(stats: dict, scoring: dict) -> float:
    """Raw stat projection x this league's real scoring settings. See lesson 3."""
    total = 0.0
    for key, weight in (scoring or {}).items():
        val = (stats or {}).get(key)
        if not val:
            continue
        try:
            total += float(val) * float(weight)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


# --------------------------------------------------------------------------
# The schedule: kickoff clock and bye-week oracle
# --------------------------------------------------------------------------

_scores_cache: dict[tuple, tuple] = {}


def week_games(season: int, week: int) -> list[dict]:
    demo = _demo_for(week)
    if demo is not None:
        return demo.get("games") or []
    key = (season, week)
    cached = _scores_cache.get(key)
    if cached and (time.time() - cached[1]) < _SCORES_TTL:
        return cached[0]
    try:
        data = _get_json(SCORES_URL.format(season=season, week=week))
    except Exception:
        return []
    games = [g for g in (data or []) if isinstance(g, dict)]
    _scores_cache[key] = (games, time.time())
    return games


def _is_us_sunday(epoch_seconds: int) -> bool:
    """
    Sunday in US time, not UTC. Sunday Night Football is Monday in UTC and
    Monday Night Football is Tuesday, so shift back 6 hours before asking.
    """
    return time.gmtime(epoch_seconds - 6 * 3600).tm_wday == 6


def week_lock_epoch(season: int, week: int):
    """
    Kickoff of the week's FIRST SUNDAY game. Lineups freeze and the curse
    projection is snapshotted at this moment.

    Anchored to Sunday rather than to a game count on purpose: 2026 opens on a
    Wednesday and also has a Thursday game, so "the second game of the week"
    put the FAAB app's cutoff two and a half days early. Thanksgiving plus
    Black Friday and Saturday doubleheaders break the same way.

    Falls back to the week's earliest kickoff if no Sunday game is found, which
    fails SAFE -- closed too early rather than open during games. Returns None
    if the feed can't be read at all, and callers must treat None as
    "do not lock" rather than guessing.
    """
    starts = sorted(
        g["start_time"] // 1000 for g in week_games(season, week)
        if g.get("start_time")
    )
    if not starts:
        return None
    sundays = [s for s in starts if _is_us_sunday(s)]
    return sundays[0] if sundays else starts[0]


def week_end_epoch(season: int, week: int, buffer_seconds: int = 4 * 3600):
    """
    When every score for the week is final: the last kickoff plus a buffer.
    Settling before this would grade gooses with the late slate, SNF and MNF
    unplayed -- the exact coupled bug that bit the FAAB app's auto-settle.
    """
    starts = sorted(
        g["start_time"] // 1000 for g in week_games(season, week)
        if g.get("start_time")
    )
    if not starts:
        return None
    return starts[-1] + buffer_seconds


def teams_playing(season: int, week: int) -> set[str]:
    """Every NFL team abbreviation with a game this week."""
    out: set[str] = set()
    for g in week_games(season, week):
        meta = g.get("metadata") or {}
        for key in ("home_team", "away_team"):
            value = g.get(key) or meta.get(key)
            if value:
                out.add(str(value).upper())
    return out


def bye_teams(season: int, week: int, all_teams: set[str] | None = None) -> set[str]:
    """
    Teams on bye = every NFL team minus the ones with a game.

    Returns an EMPTY SET if the scores feed could not be read, rather than
    declaring all 32 teams on bye -- a failure here must never mark a whole
    league's lineups as certain gooses.
    """
    playing = teams_playing(season, week)
    if not playing:
        return set()
    return (all_teams or NFL_TEAMS) - playing


NFL_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
    "TEN", "WAS",
}


# --------------------------------------------------------------------------
# Player directory
# --------------------------------------------------------------------------

def all_players() -> dict:
    """
    player_id -> name/position/team/injury_status.

    injury_status is the field the goose model leans on hardest -- it is the
    difference between a 2% starter and a 90% one.
    """
    raw = _get_json(f"{BASE_V1}/players/nfl", timeout=PLAYERS_TIMEOUT)
    out: dict = {}
    for pid, p in (raw or {}).items():
        if not isinstance(p, dict):
            continue
        full_name = (
            p.get("full_name")
            or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
            or None
        )
        out[str(pid)] = {
            "full_name": full_name,
            "position": p.get("position"),
            "team": p.get("team"),
            "injury_status": p.get("injury_status"),
        }
    return out


# --------------------------------------------------------------------------
# Live game state -- what Goose Watch runs on
# --------------------------------------------------------------------------
#
# Field names below were read off a real response
# (GET /scores/nfl/regular/2025/1) rather than assumed, because assuming is how
# the FAAB app shipped a URL that 404'd for a day. One game object carries:
#
#   status                     "complete"
#   start_time                 epoch MILLISECONDS
#   metadata.home_team         "ATL"
#   metadata.away_team         "TB"
#   metadata.is_over           true
#   metadata.is_in_progress    false
#   metadata.quarter           "F"       ("F" when final)
#   metadata.quarter_num       4
#   metadata.time_remaining    "00:00"
#   metadata.home_score        20
#   metadata.away_score        23
#
# Every one of these is read defensively: a mid-week feed can be missing any of
# them, and a KeyError here would take down the one screen people have open on
# Sunday afternoon.

PRE, LIVE, FINAL = "pre", "live", "final"


def _clock(meta: dict) -> str:
    """A short human clock: FINAL, HALFTIME, 'Q3 8:42', or 'not started'."""
    quarter = str(meta.get("quarter") or "").strip()
    if meta.get("is_over") or quarter.upper() == "F":
        return "FINAL"
    if not meta.get("is_in_progress"):
        return "not started"
    num = meta.get("quarter_num")
    remaining = str(meta.get("time_remaining") or "").strip()
    if num == 2 and remaining in ("00:00", "0:00", ""):
        return "HALFTIME"
    # "08:42" -> "8:42". Checking for a "00:" prefix (the first attempt) never
    # matched, because the leading zero is on the minutes digit, not the pair.
    if len(remaining) == 5 and remaining[0] == "0":
        remaining = remaining[1:]
    label = f"Q{num}" if num else "LIVE"
    return f"{label} {remaining}".strip()


QUARTER_SECONDS = 15 * 60


def _seconds_left(meta: dict, state: str):
    """
    Game seconds remaining, for sorting Goose Watch by urgency.

    0 for a finished game, None before kickoff -- and None is NOT zero, which
    is the entire reason this returns None rather than a big number: a caller
    that sorts ascending must be able to push "hasn't started" to the bottom
    rather than the top. Overtime counts as 0 remaining; there is no way to
    know how much of it is left and a game in overtime is as urgent as it gets.
    """
    if state == FINAL:
        return 0
    if state != LIVE:
        return None
    num = meta.get("quarter_num")
    try:
        num = int(num)
    except (TypeError, ValueError):
        return None
    if num > 4:
        return 0
    remaining = str(meta.get("time_remaining") or "").strip()
    seconds = 0
    if ":" in remaining:
        mm, _, ss = remaining.partition(":")
        try:
            seconds = int(mm) * 60 + int(ss)
        except ValueError:
            seconds = 0
    return max(0, (4 - num) * QUARTER_SECONDS + seconds)


def game_state_by_team(season: int, week: int) -> dict:
    """
    TEAM -> {state, clock, opponent, score, opponent_score, start_time}.

    A team missing from this map has no game: either the feed is unreadable or
    the team is on bye. Callers must not treat "missing" as "final" -- doing so
    would declare a bye-week player's zero a settled goose on Thursday.
    """
    out: dict = {}
    for g in week_games(season, week):
        meta = g.get("metadata") or {}
        home = str(meta.get("home_team") or "").upper()
        away = str(meta.get("away_team") or "").upper()
        if not home or not away:
            continue

        if meta.get("is_over") or g.get("status") == "complete":
            state = FINAL
        elif meta.get("is_in_progress"):
            state = LIVE
        else:
            state = PRE

        clock = _clock(meta)
        # Raw quarter number, kept alongside the formatted clock string so a
        # caller can gate on "how far into the game" without re-parsing text.
        # None pre-kickoff; Sleeper has been seen to keep counting past 4 in
        # overtime rather than reset, so ">= 4" is the right test for "no time
        # left to fix a zero", not "== 4".
        quarter = meta.get("quarter_num") if meta.get("is_in_progress") else None
        start = g.get("start_time")
        start = int(start) // 1000 if start else None
        home_score = meta.get("home_score")
        away_score = meta.get("away_score")

        left = _seconds_left(meta, state)
        out[home] = {"state": state, "clock": clock, "quarter": quarter, "opponent": away,
                     "home": True, "score": home_score, "opponent_score": away_score,
                     "start_time": start, "seconds_left": left}
        out[away] = {"state": state, "clock": clock, "quarter": quarter, "opponent": home,
                     "home": False, "score": away_score, "opponent_score": home_score,
                     "start_time": start, "seconds_left": left}
    return out
