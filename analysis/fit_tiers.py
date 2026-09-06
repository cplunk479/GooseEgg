"""
analysis/fit_tiers.py
=====================
Where the tier cut points in goose.py come from. Re-run this whenever the
league changes scoring or another season lands in fantasy.db, and move the
cut points if the bands stop separating.

    python analysis/fit_tiers.py [path/to/fantasy.db]

The question it answers
-----------------------
"Is a starter's projection, RELATIVE TO the average starter projection at his
position that week, a good predictor of him laying a goose egg?"

Yes, strongly. Measured over 35,144 starter-slots (22 leagues, 2021-2025,
weeks 3-17, QB/RB/WR/TE), P(points <= 0) runs from 11.3% in the bottom band to
1.1% in the top -- a 10x spread, monotonic at every position.

Why weeks 3+ and not week 1
---------------------------
This dataset has no stored forward projections, so a player's average points
in that season's PRIOR weeks stands in for one. Weeks 1-2 have too little
history for that average to mean anything, so they are excluded from the FIT.
The app itself uses real Sleeper projections and applies the tiers from week 1.

Why the ratio and not the raw projection
----------------------------------------
A 9-point projection is a fine week for a TE and a disaster for a QB. Banding
on the raw number made a good quarterback and a bad tight end look alike, which
is exactly the complaint that started this rework. Dividing by the position's
own starter average puts every slot on one scale, and it self-corrects when
scoring settings change: if the league doubles TE scoring, TE projections and
the TE average both move and the tiers stay put.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import numpy as np
import pandas as pd

DEFAULT_DB = os.path.expanduser("~/Claude/Fantasy Football/Scripts/files/fantasy.db")
DYNASTY_DONS = ("1090814852546793472", "1194744649990090752")

# These must match goose.py. If you change them there, change them here.
CUTS = [(0.35, "COOKED"), (0.60, "GOOSE BAIT"), (0.90, "SHAKY"), (1.25, "SOLID")]
ORDER = ["COOKED", "GOOSE BAIT", "SHAKY", "SOLID", "SAFE"]
WEIGHT = {"COOKED": 5, "GOOSE BAIT": 3, "SHAKY": 2, "SOLID": 1, "SAFE": 0}
TEAM_CUTS = [(0.90, "CLEAN"), (1.20, "STEADY"), (1.60, "EXPOSED")]


def tier_for(ratio: float) -> str:
    return next((name for edge, name in CUTS if ratio < edge), "SAFE")


def load(db_path: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    slots = pd.read_sql(
        """
        SELECT rs.league_id, rs.season, rs.week, rs.roster_id, rs.player_id, p.position
        FROM roster_slots rs JOIN players p ON p.player_id = rs.player_id
        WHERE rs.slot NOT IN ('BN','IR','TAXI')
          AND p.position IN ('QB','RB','WR','TE')
          AND rs.week BETWEEN 1 AND 17
        """,
        conn,
    )
    stats = pd.read_sql("SELECT player_id, season, week, pts_ppr FROM player_stats", conn)

    stats["pts_ppr"] = stats["pts_ppr"].fillna(0.0)
    stats = stats.sort_values(["player_id", "season", "week"])
    prior_total = stats.groupby(["player_id", "season"])["pts_ppr"].cumsum() - stats["pts_ppr"]
    prior_n = stats.groupby(["player_id", "season"]).cumcount()
    stats["proxy"] = np.where(prior_n > 0, prior_total / prior_n.replace(0, np.nan), np.nan)

    df = slots.merge(
        stats[["player_id", "season", "week", "pts_ppr", "proxy"]],
        on=["player_id", "season", "week"], how="left",
    )
    # A starter with no stat row did not play. That is a goose, not missing data.
    df["pts_ppr"] = df["pts_ppr"].fillna(0.0)
    df["goose"] = (df["pts_ppr"] <= 0.0).astype(int)
    df = df[df["week"] >= 3].dropna(subset=["proxy"])

    avg = (df.groupby(["league_id", "season", "week", "position"])["proxy"]
             .mean().rename("pos_avg").reset_index())
    df = df.merge(avg, on=["league_id", "season", "week", "position"])
    df = df[df["pos_avg"] > 0].copy()
    df["ratio"] = df["proxy"] / df["pos_avg"]
    df["tier"] = df["ratio"].apply(tier_for)
    df["weight"] = df["tier"].map(WEIGHT)
    return df


def report(df: pd.DataFrame, title: str) -> None:
    print(f"\n{'=' * 62}\n{title}  --  {len(df):,} starter-slots, "
          f"{100 * df.goose.mean():.2f}% goose rate\n{'=' * 62}")
    t = df.groupby("tier", observed=True).agg(n=("goose", "size"), rate=("goose", "mean")).reindex(ORDER)
    t["share %"] = (100 * t["n"] / len(df)).round(1)
    t["P(goose) %"] = (100 * t["rate"]).round(2)
    print(t[["n", "share %", "P(goose) %"]].to_string())

    print("\nP(goose) % by position -- every column must fall top to bottom:")
    piv = df.pivot_table(index="tier", columns="position", values="goose", aggfunc="mean").reindex(ORDER) * 100
    print(piv.round(2).to_string())

    team = (df.groupby(["league_id", "season", "week", "roster_id"])
              .agg(score=("weight", "sum"), slots=("goose", "size"), gooses=("goose", "sum"))
              .reset_index())
    team = team[team["slots"] >= 8].copy()
    team["risk"] = team["score"] / team["slots"]
    team["band"] = team["risk"].apply(
        lambda r: next((n for e, n in TEAM_CUTS if r < e), "GOOSE BAIT"))
    team["any"] = (team["gooses"] > 0).astype(int)

    print("\nTeam risk rating (mean tier weight per slot, so lineup size does not matter):")
    tt = (team.groupby("band", observed=True)
              .agg(team_weeks=("any", "size"), p_any=("any", "mean"), mean_gooses=("gooses", "mean"))
              .reindex(["CLEAN", "STEADY", "EXPOSED", "GOOSE BAIT"]))
    tt["P(>=1 goose) %"] = (100 * tt["p_any"]).round(1)
    tt["mean gooses"] = tt["mean_gooses"].round(2)
    print(tt[["team_weeks", "P(>=1 goose) %", "mean gooses"]].to_string())
    print(f"\nBaseline: {team.gooses.mean():.2f} gooses per team-week "
          f"({12 * team.gooses.mean():.1f} across a 12-team league).")


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    if not os.path.exists(db_path):
        sys.exit(f"No database at {db_path}. Pass the path as an argument.")
    df = load(db_path)
    report(df, "ALL LEAGUES")
    report(df[df.league_id.isin(DYNASTY_DONS)], "DYNASTY DONS ONLY")
    print("\nThe Dynasty Dons table is the one that matters. If its tiers stop "
          "separating,\nmove the cut points in BOTH this file and goose.py.")


if __name__ == "__main__":
    main()
