"""
goose.py
========
The engine. Two jobs, kept deliberately free of Flask and psycopg2 so both are
testable with plain python and no database:

  1. DETECTION -- did a starter lay a goose egg (settled fact, after the game)
  2. RISK      -- how exposed is this starter, and this lineup (before the game)

The league rule, locked 2026-09-06
----------------------------------
A goose is a starting-lineup player who scores **<= 0**, not exactly 0. A
negative score (fumble lost, a QB's interceptions) is still a goose. An EMPTY
starting slot is also a goose -- neglect is not an excuse.

Why this is tiers now, and not a percentage
-------------------------------------------
v0.4 showed a per-player goose PERCENTAGE and a team CHUG PRICE. Both were
honest numbers and both were useless to look at, for the same reason: the
model's strongest signal is availability. A player who is out, on bye or
projected near zero is a near-certain goose; every healthy starter is a 1-4%
shot. So the board was a wall of 2%s occasionally interrupted by a 90%, and
what it was really displaying was the injury report. The commissioner's read
was exactly right -- "it's tied to injury risk, which doesn't make sense."

The rework separates the two things that were tangled together:

  AVAILABILITY is now a LABEL, not a hidden multiplier. Out, bye, doubtful and
  empty slots say so in as many words and drop straight to the worst tier.
  Nobody needs a probability to understand "he isn't playing."

  QUALITY is now a TIER, measured the way the commissioner asked for it: where
  does this player's projection fall against the average starter projection at
  his position this week? A 9-point projection is a fine week for a tight end
  and a disaster for a quarterback, so the raw number was never comparable
  across a lineup. The ratio is.

Where the cut points come from
------------------------------
Measured, not guessed -- see analysis/fit_tiers.py, which regenerates this
table. 35,144 starter-slots across 22 leagues, 2021-2025, weeks 3-17. The
"projection" there is the player's average points in that season's prior weeks,
the closest thing that dataset has to a forward projection.

    tier          ratio to position avg    share    P(goose)   Dynasty Dons
    SAFE          >= 1.25                  22.6%      1.1%        0.9%
    SOLID         0.90 - 1.25              38.9%      1.5%        1.8%
    SHAKY         0.60 - 0.90              24.9%      3.1%        3.3%
    GOOSE BAIT    0.35 - 0.60               9.1%      5.8%        5.9%
    COOKED        < 0.35                    4.4%     11.3%       10.8%

Monotonic at every position, and it holds in Dynasty Dons on its own 3,925
slots. That is a 10x spread between the top and bottom tier, against the ~2x
of useful spread the old percentage had once availability was stripped out.

One thing the tiers deliberately do NOT do
------------------------------------------
They do not equalise positions. A SAFE tight end still gooses ~2.6% where a
SAFE quarterback gooses ~0%, because tight ends simply goose more. The tier
answers "is this player weak FOR HIS POSITION"; the team risk rating below is
what accounts for the mix.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Sequence

# --------------------------------------------------------------------------
# Detection -- unchanged, and still the only thing that settles a week
# --------------------------------------------------------------------------

EMPTY_SLOT_IDS = (None, "", "0", 0)


def is_empty_slot(player_id) -> bool:
    return player_id in EMPTY_SLOT_IDS


def is_goose(points, player_id=None) -> bool:
    """
    The settled-fact test. `points` of None means Sleeper has no score for that
    slot, which for a real player mid-week means "hasn't played yet" -- callers
    must only run this on a FINAL week. An empty slot is always a goose.
    """
    if is_empty_slot(player_id):
        return True
    if points is None:
        return True
    try:
        return float(points) <= 0.0
    except (TypeError, ValueError):
        return True


def find_gooses(starters: Sequence, starters_points: Sequence, slots: Sequence = ()) -> list[dict]:
    """
    Walk one roster's final starting lineup and return a record per goose.

    `starters` and `starters_points` are Sleeper's matchup arrays -- same
    length, aligned by index. `slots` is the league's roster_positions with BN
    stripped, used only for labelling.
    """
    out = []
    for i, pid in enumerate(starters or []):
        pts = starters_points[i] if i < len(starters_points or []) else None
        if not is_goose(pts, pid):
            continue
        out.append({
            "slot_index": i,
            "slot": slots[i] if i < len(slots) else f"S{i + 1}",
            "player_id": None if is_empty_slot(pid) else str(pid),
            "points": None if is_empty_slot(pid) else pts,
            "empty_slot": is_empty_slot(pid),
        })
    return out


# --------------------------------------------------------------------------
# Player risk tiers
# --------------------------------------------------------------------------

SAFE, SOLID, SHAKY, BAIT, COOKED = "SAFE", "SOLID", "SHAKY", "GOOSE BAIT", "COOKED"

# Best to worst. Index into this is the tier's severity everywhere else.
TIERS = (SAFE, SOLID, SHAKY, BAIT, COOKED)
TIER_INDEX = {name: i for i, name in enumerate(TIERS)}

# ratio-to-position-average cut points, worst first. See the module docstring.
RATIO_CUTS = ((0.35, COOKED), (0.60, BAIT), (0.90, SHAKY), (1.25, SOLID))

# How much each tier contributes to a lineup's risk rating. COOKED jumps to 5
# rather than continuing 1-2-3-4 on purpose: one player who is not playing is
# worse news for a lineup than two merely weak ones, and the team bands below
# were fitted with these weights.
TIER_WEIGHT = {SAFE: 0, SOLID: 1, SHAKY: 2, BAIT: 3, COOKED: 5}

# Measured P(goose) per tier, Dynasty Dons 2024-2025. Shown in tooltips and
# used where something downstream still wants a number (Goose Watch sorts on
# it). Not displayed as the headline -- that was the old mistake.
TIER_RATE = {SAFE: 0.010, SOLID: 0.018, SHAKY: 0.033, BAIT: 0.059, COOKED: 0.108}

TIER_BLURB = {
    SAFE:   "well above the position bar",
    SOLID:  "around the position bar",
    SHAKY:  "under the position bar",
    BAIT:   "well under the position bar",
    COOKED: "barely projected at all",
}

# Statuses that mean "will not play".
OUT_HARD = {"Out", "IR", "PUP", "Sus", "NA", "DNR", "COV"}

# Sleeper projects ~0 for a player it expects not to dress. Below this, the
# projection is not a weak forecast, it is an absence.
ZERO_PROJECTION_THRESHOLD = 1.0

# Used only when a position has too few starters league-wide to average -- a
# broken feed, or a week where nobody started a tight end. Dynasty Dons starter
# means, 2024-2025 (PPR base; the league's bonuses push live projections
# higher, which is fine, this is a floor to divide by, not a target).
FALLBACK_POSITION_AVG = {"QB": 16.4, "RB": 12.8, "WR": 11.5, "TE": 9.6}
MIN_POSITION_SAMPLE = 4
DEFAULT_POSITION = "WR"


def position_averages(starters: Iterable[Mapping]) -> dict[str, float]:
    """
    The week's bar, per position: the mean projection of every STARTER at that
    position across the whole league.

    `starters` is every started slot in the league this week, each a mapping
    with "position" and "projection". Empty slots and players with no
    projection are excluded -- they are the thing being measured against the
    bar, and letting a pile of zeros into the average would drag the bar down
    and make a bad week look like a normal one.

    A position with fewer than MIN_POSITION_SAMPLE real starters falls back to
    the measured league average rather than trusting a mean of two.
    """
    buckets: dict[str, list[float]] = {}
    for row in starters:
        pos = (row.get("position") or "").upper()
        proj = row.get("projection")
        if not pos or proj is None:
            continue
        try:
            proj = float(proj)
        except (TypeError, ValueError):
            continue
        if proj <= ZERO_PROJECTION_THRESHOLD:
            continue
        buckets.setdefault(pos, []).append(proj)

    out: dict[str, float] = {}
    for pos, values in buckets.items():
        if len(values) >= MIN_POSITION_SAMPLE:
            out[pos] = sum(values) / len(values)
        else:
            out[pos] = FALLBACK_POSITION_AVG.get(pos, FALLBACK_POSITION_AVG[DEFAULT_POSITION])
    return out


def tier_for_ratio(ratio: float) -> str:
    for edge, name in RATIO_CUTS:
        if ratio < edge:
            return name
    return SAFE


def player_risk(
    *,
    player_id=None,
    position: str | None = None,
    projection: float | None = None,
    position_avg: float | None = None,
    injury_status: str | None = None,
    on_bye: bool = False,
) -> dict:
    """
    One starter's risk, as a tier plus the reason for it.

    Returns {"tier", "ratio", "reason", "weight", "rate", "blurb"}. `reason` is
    None for an ordinary healthy player and a short all-caps label otherwise --
    that label is the whole point of the rework, because "OUT" tells an owner
    more in two letters than "90%" did in three.

    Order matters, and it is order of certainty. Availability first: nothing a
    projection says can rescue a player who is not on the field. Then the
    projection ratio. Questionable is last because it is the only genuinely
    uncertain signal here, and it nudges rather than decides.
    """
    if is_empty_slot(player_id):
        return _risk(COOKED, None, "EMPTY SLOT")
    if on_bye:
        return _risk(COOKED, None, "ON BYE")

    status = (injury_status or "").strip()
    if status in OUT_HARD:
        return _risk(COOKED, None, status.upper())
    if status == "Doubtful":
        return _risk(COOKED, None, "DOUBTFUL")

    if projection is None:
        # No projection at all -- a week-1 rookie, or someone the feed misses.
        # NOT treated as bad: a starter with no history is usually someone
        # drafted on purpose, and those slots goose at well under the rate the
        # bottom tier would charge them. Park them mid-table and say why.
        return _risk(SHAKY, None, "NO PROJECTION")

    try:
        proj = float(projection)
    except (TypeError, ValueError):
        return _risk(SHAKY, None, "NO PROJECTION")

    if proj <= ZERO_PROJECTION_THRESHOLD:
        return _risk(COOKED, 0.0, "PROJECTED ZERO")

    bar = position_avg
    if not bar or float(bar) <= 0:
        bar = FALLBACK_POSITION_AVG.get(
            (position or "").upper(), FALLBACK_POSITION_AVG[DEFAULT_POSITION])
    ratio = proj / float(bar)
    tier = tier_for_ratio(ratio)

    if status == "Questionable":
        tier = worsen(tier, 1)
        return _risk(tier, ratio, "QUESTIONABLE")
    return _risk(tier, ratio, None)


def worsen(tier: str, steps: int = 1) -> str:
    """Move a tier `steps` toward COOKED, stopping there."""
    i = min(TIER_INDEX.get(tier, 1) + steps, len(TIERS) - 1)
    return TIERS[i]


def _risk(tier: str, ratio: float | None, reason: str | None) -> dict:
    return {
        "tier": tier,
        "ratio": None if ratio is None else round(float(ratio), 3),
        "reason": reason,
        "weight": TIER_WEIGHT[tier],
        "rate": TIER_RATE[tier],
        "blurb": reason.title() if reason else TIER_BLURB[tier],
    }


# --------------------------------------------------------------------------
# Team risk rating
# --------------------------------------------------------------------------

# Mean tier weight per STARTING SLOT, so the rating does not move just because
# a league starts eleven players instead of nine. Bands fitted on the same 35k
# slots, and named so the MEDIAN lineup does not read as an emergency -- the
# average lineup scores about 1.38, which is EXPOSED, which really is a one in
# three week. Roughly 17% / 24% / 31% / 28% of team-weeks land in each band.
#
#   CLEAN       < 0.90     P(at least one goose)  11%
#   STEADY     0.90-1.20                          16%
#   EXPOSED    1.20-1.60                          30%
#   GOOSE BAIT  >= 1.60                           43%
TEAM_CUTS = ((0.90, "CLEAN"), (1.20, "STEADY"), (1.60, "EXPOSED"))
TEAM_TIERS = ("CLEAN", "STEADY", "EXPOSED", "GOOSE BAIT")
TEAM_INDEX = {name: i for i, name in enumerate(TEAM_TIERS)}
TEAM_RATE = {"CLEAN": 0.11, "STEADY": 0.16, "EXPOSED": 0.30, "GOOSE BAIT": 0.43}


def team_risk(tiers: Sequence[str]) -> dict:
    """
    A lineup's rating from its tier composition.

    Returns {"tier", "score", "at_risk", "worst", "rate", "counts"}, where
    `at_risk` counts slots at GOOSE BAIT or worse -- the number an owner
    actually wants, because it is how many players they could still do
    something about.
    """
    tiers = [t for t in tiers if t in TIER_INDEX]
    if not tiers:
        return {"tier": None, "score": None, "at_risk": 0, "worst": None,
                "rate": None, "counts": {}}

    score = sum(TIER_WEIGHT[t] for t in tiers) / len(tiers)
    band = next((name for edge, name in TEAM_CUTS if score < edge), "GOOSE BAIT")
    worst = max(tiers, key=lambda t: TIER_INDEX[t])
    return {
        "tier": band,
        "score": round(score, 2),
        "at_risk": sum(1 for t in tiers if TIER_INDEX[t] >= TIER_INDEX[BAIT]),
        "worst": worst,
        "rate": TEAM_RATE[band],
        "counts": {t: tiers.count(t) for t in TIERS if tiers.count(t)},
    }


def expected_gooses(tiers: Iterable[str]) -> float:
    """
    Expected COUNT of gooses across the slots handed in -- the launch sanity
    check. Summed over all twelve lineups it should land near 3.7 for a normal
    week. If it says 11, something upstream is broken; do not ship it.
    """
    return round(sum(TIER_RATE.get(t, 0.0) for t in tiers), 2)
