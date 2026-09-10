"""
tests/test_sleeper_cache.py
============================
The response cache in sleeper.py, specifically week_games() -- the one feed
that answers both "when does this team kick off" (genuinely static) and "what
is the score right now" (the whole reason Goose Watch exists).

    python tests/test_sleeper_cache.py

This is the regression test for a real bug: _SCORES_TTL was 3600 (one hour),
under the reasoning "a published schedule does not move." True for the
kickoff half of the payload, false for the live half -- and the two halves
are the SAME cached response, so the live half was going stale for up to an
hour at a time. Seahawks/Patriots could be well into the second quarter and
Goose Watch would still be showing "not started," because the cached fetch
from an hour-old page load hadn't expired yet.

Every other live-moving feed in this file (_TTL_MATCHUPS) is 120 seconds.
_SCORES_TTL has to match that shape, not the schedule's.

No network: _get_json is monkeypatched to a counter so the test proves the
CACHE behaves correctly without needing a real HTTP call or a live game to
test against.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sleeper  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


class FakeClock:
    """A controllable time.time() so the test can jump forward without sleeping."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def reset():
    sleeper._scores_cache.clear()
    sleeper._demo_for = lambda week: None  # never route through demo mode here


def test_ttl_matches_what_actually_moves() -> None:
    print("_SCORES_TTL has to move on the LIVE half of the feed, not the schedule half")
    # 3600 is the exact value that shipped the bug: a game that kicked off
    # anywhere in that hour-old cache window would read as "not started" for
    # the rest of the hour. Anything longer than _TTL_MATCHUPS (also live data,
    # also 120s) reintroduces the same class of staleness.
    check("_SCORES_TTL is short enough to track a live game",
          sleeper._SCORES_TTL <= sleeper._TTL_MATCHUPS,
          f"_SCORES_TTL={sleeper._SCORES_TTL}, _TTL_MATCHUPS={sleeper._TTL_MATCHUPS} "
          f"-- game state is exactly as time-sensitive as matchups and must not "
          f"be cached any longer")
    check("and it did not regress back toward an hour",
          sleeper._SCORES_TTL <= 300,
          f"_SCORES_TTL={sleeper._SCORES_TTL} -- Seahawks/Patriots could kick off "
          f"and Goose Watch would still show 'not started' for most of a quarter")


def test_a_hit_within_ttl_does_not_refetch() -> None:
    print("\na second call inside the TTL window reuses the cached response")
    reset()
    clock = FakeClock()
    calls = []

    def fake_get_json(url, timeout=sleeper.HTTP_TIMEOUT):
        calls.append(clock.now)
        return [{"metadata": {"home_team": "SEA", "away_team": "NE"}}]

    orig_time, orig_get = sleeper.time.time, sleeper._get_json
    sleeper.time.time, sleeper._get_json = clock, fake_get_json
    try:
        sleeper.week_games(2026, 2)
        clock.advance(sleeper._SCORES_TTL - 1)
        sleeper.week_games(2026, 2)
        check("only one real fetch happened", len(calls) == 1, str(calls))
    finally:
        sleeper.time.time, sleeper._get_json = orig_time, orig_get


def test_a_stale_entry_refetches() -> None:
    print("\nonce the TTL has actually elapsed, the next call refetches -- "
          "this is the half of the bug that mattered: a kickoff that happened "
          "DURING the stale window must be visible soon after, not an hour later")
    reset()
    clock = FakeClock()
    calls = []

    def fake_get_json(url, timeout=sleeper.HTTP_TIMEOUT):
        calls.append(clock.now)
        # Simulate the game turning live between the two calls -- exactly the
        # Seahawks/Patriots scenario: kickoff happens inside the cache window.
        in_progress = len(calls) > 1
        return [{"metadata": {"home_team": "SEA", "away_team": "NE",
                               "is_in_progress": in_progress, "quarter_num": 1 if in_progress else None}}]

    orig_time, orig_get = sleeper.time.time, sleeper._get_json
    sleeper.time.time, sleeper._get_json = clock, fake_get_json
    try:
        first = sleeper.week_games(2026, 2)
        check("first fetch sees the pre-kickoff state",
              not (first[0]["metadata"].get("is_in_progress")))

        clock.advance(sleeper._SCORES_TTL + 1)
        second = sleeper.week_games(2026, 2)
        check("a refetch happens once the TTL elapses", len(calls) == 2, str(calls))
        check("and the live state is now visible",
              second[0]["metadata"].get("is_in_progress") is True,
              "kickoff happened but the cache never refreshed to show it")
    finally:
        sleeper.time.time, sleeper._get_json = orig_time, orig_get


def test_game_state_by_team_reflects_a_refresh() -> None:
    print("\ngame_state_by_team() -- what Goose Watch actually reads -- "
          "picks up a kickoff once the cache has had a chance to refresh")
    reset()
    clock = FakeClock()
    state = {"live": False}

    def fake_get_json(url, timeout=sleeper.HTTP_TIMEOUT):
        live = state["live"]
        return [{
            "metadata": {
                "home_team": "SEA", "away_team": "NE",
                "is_in_progress": live, "is_over": False,
                "quarter_num": 2 if live else None,
                "time_remaining": "07:30" if live else None,
            },
            "start_time": int(clock.now * 1000),
        }]

    orig_time, orig_get = sleeper.time.time, sleeper._get_json
    sleeper.time.time, sleeper._get_json = clock, fake_get_json
    try:
        before = sleeper.game_state_by_team(2026, 2)
        check("before kickoff, Watch sees it as not live",
              before.get("SEA", {}).get("state") != sleeper.LIVE)

        state["live"] = True
        clock.advance(sleeper._SCORES_TTL + 1)
        after = sleeper.game_state_by_team(2026, 2)
        check("SEAHAWKS/PATRIOTS GOING LIVE IS VISIBLE WITHIN ONE TTL WINDOW",
              after.get("SEA", {}).get("state") == sleeper.LIVE,
              f"got {after.get('SEA', {}).get('state')!r} -- this is the exact "
              f"symptom that was reported: a live game not sorting to the top "
              f"of Goose Watch because the cache never refreshed")
        check("seconds_left comes through once live",
              after.get("SEA", {}).get("seconds_left") is not None)
    finally:
        sleeper.time.time, sleeper._get_json = orig_time, orig_get


def main() -> int:
    test_ttl_matches_what_actually_moves()
    test_a_hit_within_ttl_does_not_refetch()
    test_a_stale_entry_refetches()
    test_game_state_by_team_reflects_a_refresh()
    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
