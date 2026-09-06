"""
tests/test_goose.py
===================
Runs with plain python -- no pytest, no database, no network:

    python tests/test_goose.py

Two halves. The first is ordinary unit testing of detection and of the tier
arithmetic. The second is a CALIBRATION test against real Dynasty Dons history
out of the Obsidian vault's fantasy.db: it assigns the shipped tiers to 2024
and 2025 lineups and checks that the gooses in each tier actually come out in
the order the tiers claim.

A tier model fails differently from a probability model, so the calibration
asks a different question than it did in v0.4. Not "is the number right" but
"do the tiers SEPARATE" -- does COOKED really goose several times more often
than SAFE, at every position. A five-step ramp where the steps are all the
same height is worse than useless: it looks like information and is not.

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


# --------------------------------------------------------------------- tiers

def test_tiers() -> None:
    print("\nrisk tiers -- availability is a label, quality is a ratio")

    bar = 12.0
    check("well above the bar is SAFE",
          goose.player_risk(player_id="1", position="WR", projection=18.0, position_avg=bar)["tier"] == goose.SAFE)
    check("around the bar is SOLID",
          goose.player_risk(player_id="1", position="WR", projection=12.0, position_avg=bar)["tier"] == goose.SOLID)
    check("well under the bar is GOOSE BAIT",
          goose.player_risk(player_id="1", position="WR", projection=6.0, position_avg=bar)["tier"] == goose.BAIT)
    check("barely projected is COOKED",
          goose.player_risk(player_id="1", position="WR", projection=3.0, position_avg=bar)["tier"] == goose.COOKED)

    # The whole point of the rework: these say WHY, they do not hide it in a number.
    empty = goose.player_risk(player_id=None)
    check("an empty slot is COOKED and says so", empty["tier"] == goose.COOKED and empty["reason"] == "EMPTY SLOT")
    bye = goose.player_risk(player_id="1", position="RB", projection=17.0, position_avg=bar, on_bye=True)
    check("a bye beats a great projection", bye["tier"] == goose.COOKED and bye["reason"] == "ON BYE")
    out = goose.player_risk(player_id="1", position="RB", projection=19.0, position_avg=bar, injury_status="Out")
    check("OUT beats a great projection", out["tier"] == goose.COOKED and out["reason"] == "OUT")
    zero = goose.player_risk(player_id="1", position="WR", projection=0.4, position_avg=bar)
    check("a zero projection reads as 'not playing'",
          zero["tier"] == goose.COOKED and zero["reason"] == "PROJECTED ZERO")

    q_healthy = goose.player_risk(player_id="1", position="WR", projection=12.0, position_avg=bar)
    q_doubt = goose.player_risk(player_id="1", position="WR", projection=12.0, position_avg=bar,
                                injury_status="Questionable")
    check("questionable moves a player exactly one tier worse",
          goose.TIER_INDEX[q_doubt["tier"]] == goose.TIER_INDEX[q_healthy["tier"]] + 1)
    check("questionable says so", q_doubt["reason"] == "QUESTIONABLE")

    # A missing projection must NOT be read as a bad one. Getting this wrong in
    # v0.4 was most of the model's over-prediction: a starter with no history is
    # usually a rookie somebody drafted on purpose.
    unknown = goose.player_risk(player_id="1", position="WR", projection=None, position_avg=bar)
    check("no projection parks mid-table rather than condemning",
          unknown["tier"] == goose.SHAKY and unknown["reason"] == "NO PROJECTION")

    check("an unknown position falls back rather than crashing",
          goose.player_risk(player_id="1", position="LB", projection=6.0, position_avg=None)["tier"] in goose.TIERS)

    # Monotonic by construction: a better projection may never carry MORE risk.
    tiers = [goose.player_risk(player_id="1", position="WR", projection=p, position_avg=bar)["tier"]
             for p in (2, 4, 6, 8, 10, 12, 14, 16, 20)]
    ranks = [goose.TIER_INDEX[t] for t in tiers]
    check("tier never worsens as the projection rises", ranks == sorted(ranks, reverse=True), str(tiers))
    weights = [goose.TIER_WEIGHT[t] for t in goose.TIERS]
    check("tier weights rise with severity", weights == sorted(weights), str(weights))
    rates = [goose.TIER_RATE[t] for t in goose.TIERS]
    check("tier rates rise with severity", rates == sorted(rates), str(rates))


def test_position_averages() -> None:
    print("\nposition averages -- the week's own bar, not a number from a table")
    starters = ([{"position": "WR", "projection": p} for p in (10, 12, 14, 8, 16)]
                + [{"position": "QB", "projection": p} for p in (20, 24)])
    avg = goose.position_averages(starters)
    check("averages the real starters", abs(avg["WR"] - 12.0) < 0.01, str(avg))
    check("too few starters falls back to the measured league mean",
          abs(avg["QB"] - goose.FALLBACK_POSITION_AVG["QB"]) < 0.01, str(avg))

    # Empty slots and non-playing starters must not drag the bar down -- they
    # are the thing being measured against it.
    polluted = starters + [{"position": "WR", "projection": 0.0} for _ in range(5)]
    check("zeros are excluded from the bar",
          abs(goose.position_averages(polluted)["WR"] - 12.0) < 0.01)
    check("an empty starter list does not crash", goose.position_averages([]) == {})


def test_team_risk() -> None:
    print("\nteam risk -- a rating from the mix, normalised for lineup size")
    clean = goose.team_risk([goose.SAFE] * 11)
    check("an all-SAFE lineup is CLEAN", clean["tier"] == "CLEAN", str(clean))
    check("nothing at risk in a clean lineup", clean["at_risk"] == 0)

    bad = goose.team_risk([goose.COOKED, goose.COOKED, goose.BAIT] + [goose.SHAKY] * 8)
    check("a lineup full of holes is GOOSE BAIT", bad["tier"] == "GOOSE BAIT", str(bad))
    check("at_risk counts GOOSE BAIT and worse", bad["at_risk"] == 3, str(bad["at_risk"]))
    check("worst tier is surfaced", bad["worst"] == goose.COOKED)

    # Normalised, so a nine-man lineup and an eleven-man lineup with the same
    # mix rate the same. Before this it did not, and a superflex league looked
    # permanently more dangerous than a standard one.
    nine = goose.team_risk([goose.SHAKY] * 9)
    eleven = goose.team_risk([goose.SHAKY] * 11)
    check("lineup size does not move the rating", nine["tier"] == eleven["tier"])
    check("empty lineup returns no rating rather than a fake one",
          goose.team_risk([])["tier"] is None)

    ratings = [goose.team_risk([t] * 10)["tier"] for t in goose.TIERS]
    ranks = [goose.TEAM_INDEX[r] for r in ratings]
    check("team rating never improves as tiers worsen", ranks == sorted(ranks), str(ratings))


# -------------------------------------------------------------- calibration

def _load_history():
    if not os.path.exists(HISTORY_DB):
        return None
    conn = sqlite3.connect(HISTORY_DB)
    conn.row_factory = sqlite3.Row
    return conn


def test_calibration() -> None:
    print("\ncalibration -- the shipped tiers against real Dynasty Dons weeks")
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
        WHERE rs.slot != 'BN' AND rs.week BETWEEN 3 AND 17
          AND rs.league_id IN (%s)
        """ % ",".join("?" * len(leagues)),
        leagues,
    ))
    if not rows:
        print("  skip  Dynasty Dons seasons not present in the history db")
        return

    # Prior-week average stands in for a projection -- it is what the cut
    # points were fitted on. The real app feeds live Sleeper projections.
    # Weeks 1-2 are excluded above because two games of history is not an
    # average, it is noise.
    history = defaultdict(dict)
    for r in conn.execute("SELECT player_id, season, week, pts_ppr FROM player_stats WHERE week BETWEEN 1 AND 17"):
        history[(r[0], r[1])][r[2]] = r[3] or 0.0

    def projection_for(pid, season, week):
        weeks = history.get((pid, season), {})
        prior = [v for w, v in weeks.items() if w < week]
        return sum(prior) / len(prior) if prior else None

    # The bar has to be computed the way the app computes it: per league-week,
    # per position, over that week's actual starters.
    by_week = defaultdict(list)
    for r in rows:
        by_week[(r["league_id"], r["season"], r["week"])].append(r)

    per_tier = defaultdict(lambda: [0, 0])          # tier -> [slots, gooses]
    per_pos_tier = defaultdict(lambda: [0, 0])      # (pos, tier) -> [slots, gooses]
    team_band = defaultdict(lambda: [0, 0])         # band -> [team-weeks, weeks with a goose]
    total_gooses = 0

    for key, week_rows in by_week.items():
        starters = [{"position": r["pos"], "projection": projection_for(
            r["player_id"], r["season"], r["week"])} for r in week_rows]
        avg = goose.position_averages(starters)

        by_roster = defaultdict(list)
        for r in week_rows:
            risk = goose.player_risk(
                player_id=r["player_id"], position=r["pos"],
                projection=projection_for(r["player_id"], r["season"], r["week"]),
                position_avg=avg.get((r["pos"] or "").upper()),
            )
            goosed = goose.is_goose(r["pts"], r["player_id"])
            total_gooses += int(goosed)
            per_tier[risk["tier"]][0] += 1
            per_tier[risk["tier"]][1] += int(goosed)
            per_pos_tier[((r["pos"] or "?"), risk["tier"])][0] += 1
            per_pos_tier[((r["pos"] or "?"), risk["tier"])][1] += int(goosed)
            by_roster[r["roster_id"]].append((risk["tier"], goosed))

        for rid, slots in by_roster.items():
            if len(slots) < 8:
                continue
            band = goose.team_risk([t for t, _ in slots])["tier"]
            team_band[band][0] += 1
            team_band[band][1] += int(any(g for _, g in slots))

    print(f"  {len(by_week)} league-weeks, {len(rows)} starter-slots, {total_gooses} gooses")
    print("  tier          slots   P(goose)")
    observed = []
    for tier in goose.TIERS:
        n, g = per_tier[tier]
        rate = (g / n) if n else 0.0
        observed.append(rate)
        print(f"  {tier:<12} {n:>6}   {rate:>7.2%}")

    check("every tier has a usable sample", all(per_tier[t][0] >= 100 for t in goose.TIERS),
          str({t: per_tier[t][0] for t in goose.TIERS}))
    check("goose rate rises monotonically from SAFE to COOKED",
          observed == sorted(observed), str([round(r, 4) for r in observed]))
    check("COOKED gooses at least 4x as often as SAFE",
          observed[-1] >= observed[0] * 4,
          f"SAFE {observed[0]:.2%} vs COOKED {observed[-1]:.2%}")

    # The shipped TIER_RATE constants are what the UI quotes back to people.
    # If reality has drifted away from them, the copy on the screen is lying.
    for tier in goose.TIERS:
        n, g = per_tier[tier]
        if n < 100:
            continue
        rate = g / n
        check(f"{tier} matches its published rate within 3 points",
              abs(rate - goose.TIER_RATE[tier]) < 0.03,
              f"observed {rate:.2%} vs published {goose.TIER_RATE[tier]:.2%}")

    # A ramp that only works in aggregate would be hiding a position where it
    # is flat or backwards -- which is exactly the bug the old raw-projection
    # bands had.
    for pos in ("QB", "RB", "WR", "TE"):
        rates = []
        for tier in goose.TIERS:
            n, g = per_pos_tier[(pos, tier)]
            if n >= 40:
                rates.append(g / n)
        if len(rates) >= 4:
            check(f"{pos} tiers separate in the right direction",
                  rates[-1] >= rates[0],
                  f"SAFE-end {rates[0]:.2%} vs COOKED-end {rates[-1]:.2%}")

    print("  team rating   team-weeks   P(>=1 goose)")
    team_rates = []
    for band in goose.TEAM_TIERS:
        n, g = team_band[band]
        rate = (g / n) if n else 0.0
        team_rates.append(rate if n >= 30 else None)
        print(f"  {band:<12} {n:>10}   {rate:>10.1%}")
    seen = [r for r in team_rates if r is not None]
    check("team ratings separate in the right direction",
          seen == sorted(seen), str([None if r is None else round(r, 3) for r in seen]))

    per_team_week = total_gooses / max(1, sum(n for n, _ in team_band.values()))
    league_week = per_team_week * 12
    print(f"  {league_week:.1f} gooses per league-week across twelve teams")
    check("the league-wide rate is still in the plausible 2-6 band",
          2.0 <= league_week <= 6.0, f"{league_week:.2f}")
    conn.close()


def main() -> int:
    test_detection()
    test_tiers()
    test_position_averages()
    test_team_risk()
    test_calibration()
    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
