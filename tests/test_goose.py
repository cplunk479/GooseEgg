"""
tests/test_goose.py
===================
Runs with plain python -- no pytest, no database, no network:

    python tests/test_goose.py

Two halves. The first is ordinary unit testing of the detection and odds
arithmetic. The second is a CALIBRATION test against real Dynasty Dons
history out of the Obsidian vault's fantasy.db: it runs the shipped model
over 2024 and 2025 lineups and checks that the gooses it PREDICTS match the
gooses that actually happened.

That second half is the one that matters. The FAAB app shipped a URL that
404'd for a day because a mocked test proved the parsing and never the real
thing; the lesson written into faab-platform-maintenance.md was to check
against reality at least once. A goose model that reads fine and predicts
eleven gooses a week is wrong, and only real data says so.

fantasy.db is not part of this repo. Point GOOSE_HISTORY_DB at it, or leave it
and the calibration half skips cleanly.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import goose  # noqa: E402

# tests/ -> goose-platform/ -> Dev/ -> the vault root, which holds Scripts/.
_VAULT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_DB = os.path.join(_VAULT_ROOT, "Scripts", "files", "fantasy.db")
HISTORY_DB = os.environ.get("GOOSE_HISTORY_DB", DEFAULT_DB)

DYNASTY_DONS = [("1090814852546793472", 2024), ("1194744649990090752", 2025)]

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


# ---------------------------------------------------------------- detection

def test_detection() -> None:
    print("detection -- the locked rule is <= 0, and an empty slot gooses")
    check("exactly zero is a goose", goose.is_goose(0.0, "1234"))
    check("negative is a goose", goose.is_goose(-2.0, "1234"))
    check("0.5 is not a goose", not goose.is_goose(0.5, "1234"))
    check("empty slot '0' is a goose", goose.is_goose(12.4, "0"))
    check("empty slot None is a goose", goose.is_goose(None, None))
    check("missing score is a goose", goose.is_goose(None, "1234"))
    check("garbage score is a goose", goose.is_goose("n/a", "1234"))

    starters = ["100", "0", "300", "400"]
    points = [0.0, 0.0, -2.0, 18.6]
    slots = ["QB", "RB", "WR", "TE"]
    found = goose.find_gooses(starters, points, slots)
    check("finds exactly three gooses", len(found) == 3, f"got {len(found)}")
    check("flags the empty slot", any(g["empty_slot"] for g in found))
    check("keeps the healthy starter out", all(g["slot"] != "TE" for g in found))
    check("labels slots", [g["slot"] for g in found] == ["QB", "RB", "WR"])

    short = goose.find_gooses(["100", "200"], [4.5], ["QB", "RB"])
    check("a missing points entry gooses rather than crashing", len(short) == 1)


# --------------------------------------------------------------- prediction

def test_probability() -> None:
    print("\nprobability -- availability dominates, and the table is monotonic")
    check("empty slot is certain", goose.player_goose_probability(player_id="0") == 1.0)
    check(
        "bye week is near certain",
        goose.player_goose_probability(player_id="1", position="WR", projection=14.0, on_bye=True) > 0.9,
    )
    check(
        "an OUT designation beats a great projection",
        goose.player_goose_probability(player_id="1", position="RB", projection=19.0, injury_status="Out") > 0.8,
    )
    check(
        "questionable raises risk but is not a sentence",
        0.1 < goose.player_goose_probability(player_id="1", position="WR", projection=9.0, injury_status="Questionable") < 0.4,
    )
    check(
        "a zero projection is read as 'not playing'",
        goose.player_goose_probability(player_id="1", position="WR", projection=0.4) > 0.5,
    )
    check(
        "a healthy WR1 is low risk",
        goose.player_goose_probability(player_id="1", position="WR", projection=17.0) < 0.05,
    )

    for pos, rates in goose.BASE_RATES.items():
        check(f"{pos} base rates never rise with projection", list(rates) == sorted(rates, reverse=True), str(rates))

    check(
        "an unknown position falls back rather than crashing",
        0 < goose.player_goose_probability(player_id="1", position="LB", projection=6.0) < 1,
    )


def test_odds() -> None:
    print("\nodds -- team chug odds and the sportsbook price")
    check("no risk is still bounded", goose.team_chug_odds([0, 0, 0]) <= 0.01)
    check("a certainty is bounded below 1", goose.team_chug_odds([1.0]) <= 0.995)
    combined = goose.team_chug_odds([0.1, 0.1])
    check("two 10% starters give 19%", abs(combined - 0.19) < 0.001, f"got {combined}")
    check("odds never fall as risk is added", goose.team_chug_odds([0.1, 0.1]) > goose.team_chug_odds([0.1]))

    check("26% prices at +285", goose.american_price(0.26) == "+285", goose.american_price(0.26))
    check("49% prices at +105", goose.american_price(0.49) == "+105", goose.american_price(0.49))
    check("a favourite prices negative", goose.american_price(0.75).startswith("-"))
    for p in (0.05, 0.12, 0.26, 0.35, 0.49, 0.62, 0.88):
        back = goose.implied_probability(goose.american_price(p))
        check(f"round-trips {int(p * 100)}%", abs(back - p) < 0.01, f"got {back:.3f}")


# -------------------------------------------------------------- calibration

def _load_history():
    if not os.path.exists(HISTORY_DB):
        return None
    conn = sqlite3.connect(HISTORY_DB)
    conn.row_factory = sqlite3.Row
    return conn


def test_calibration() -> None:
    print("\ncalibration -- the shipped model against real Dynasty Dons weeks")
    conn = _load_history()
    if conn is None:
        print(f"  skip  no history db at {HISTORY_DB}")
        return

    leagues = [lid for lid, _ in DYNASTY_DONS]
    rows = list(conn.execute(
        """
        SELECT rs.league_id, rs.season, rs.week, rs.roster_id, rs.player_id,
               p.position AS pos, ps.pts_ppr AS pts
        FROM roster_slots rs
        LEFT JOIN player_stats ps
               ON ps.player_id = rs.player_id AND ps.season = rs.season AND ps.week = rs.week
        LEFT JOIN players p ON p.player_id = rs.player_id
        WHERE rs.slot != 'BN' AND rs.week BETWEEN 1 AND 14
          AND rs.league_id IN (%s)
        """ % ",".join("?" * len(leagues)),
        leagues,
    ))
    if not rows:
        print("  skip  Dynasty Dons seasons not present in the history db")
        return

    # Prior-week average stands in for a projection -- it is what the base
    # rates in goose.py were banded on. The real app feeds live projections.
    history = defaultdict(dict)
    for r in conn.execute("SELECT player_id, season, week, pts_ppr FROM player_stats WHERE week BETWEEN 1 AND 17"):
        history[(r[0], r[1])][r[2]] = r[3] or 0.0

    def projection_for(pid, season, week):
        weeks = history.get((pid, season), {})
        prior = [v for w, v in weeks.items() if w < week]
        return sum(prior) / len(prior) if prior else None

    predicted_by_week = defaultdict(float)
    actual_by_week = defaultdict(int)
    team_probs = defaultdict(list)

    for r in rows:
        proj = projection_for(r["player_id"], r["season"], r["week"])
        p = goose.player_goose_probability(
            player_id=r["player_id"], position=r["pos"], projection=proj,
            # prior-week average, not a forecast -- see the flag's docstring
            projection_is_forecast=False,
        )
        key = (r["season"], r["week"])
        predicted_by_week[key] += p
        team_probs[(r["season"], r["week"], r["roster_id"])].append(p)
        if goose.is_goose(r["pts"], r["player_id"]):
            actual_by_week[key] += 1

    n_weeks = len(actual_by_week)
    predicted = sum(predicted_by_week.values()) / n_weeks
    actual = sum(actual_by_week.values()) / n_weeks
    print(f"  {n_weeks} league-weeks, {len(rows)} starter-slots")
    print(f"  predicted {predicted:.2f} gooses/week   actual {actual:.2f} gooses/week")

    check(
        "predicted goose rate is within 0.5/week of reality",
        abs(predicted - actual) < 0.5,
        f"predicted {predicted:.2f} vs actual {actual:.2f}",
    )
    check(
        "predicted rate is in the plausible 2-6 per week band",
        2.0 <= predicted <= 6.0,
        f"{predicted:.2f}",
    )

    odds = [goose.team_chug_odds(v) for v in team_probs.values()]
    mean_odds = sum(odds) / len(odds)
    goosed_team_weeks = defaultdict(bool)
    for r in rows:
        if goose.is_goose(r["pts"], r["player_id"]):
            goosed_team_weeks[(r["season"], r["week"], r["roster_id"])] = True
    actual_team_rate = sum(1 for k in team_probs if goosed_team_weeks.get(k)) / len(team_probs)
    print(f"  mean team chug odds {mean_odds:.1%}   actual team-week goose rate {actual_team_rate:.1%}")
    check(
        "mean team chug odds are within 4 points of the real team-week rate",
        abs(mean_odds - actual_team_rate) < 0.04,
        f"{mean_odds:.1%} vs {actual_team_rate:.1%}",
    )
    conn.close()


def main() -> int:
    test_detection()
    test_probability()
    test_odds()
    test_calibration()
    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
