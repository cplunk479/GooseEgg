"""
goose.py
========
The engine. Two jobs, kept deliberately free of Flask and psycopg2 so both are
testable with plain python and no database:

  1. DETECTION  -- did a starter lay a goose egg (settled fact, after the game)
  2. PREDICTION -- how likely is one, and how likely is the owner to chug
                   (a forecast, before the game)

The league rule, locked 2026-09-06
----------------------------------
A goose is a starting-lineup player who scores **<= 0**, not exactly 0. A
negative score (fumble lost, a QB's interceptions) is still a goose. An EMPTY
starting slot is also a goose -- neglect is not an excuse.

Why prediction is an availability model, not a performance model
---------------------------------------------------------------
Dynasty Dons scoring is generous: full PPR, 7-point TDs, 0.1/yard rushing and
receiving, plus 0.5 per rush/rec first down and a 0.5 TE premium. A single
catch is worth 1.0 and the first down it earns another 0.5. So a 0.00 almost
never means "bad game" -- it means the player did not play, or touched the ball
zero times. That is why the model reads bye weeks, inactives and injury
designations FIRST, and only falls through to a statistical base rate for
healthy players who are expected to play.

Where the base rates come from
------------------------------
Measured, not guessed. 40,217 starter-slots across every league in
Scripts/files/fantasy.db (2021-2025, weeks 1-17, QB/RB/WR/TE), banded by the
player's average points in that season's PRIOR weeks -- the closest thing in
that dataset to a projection. Rates are P(points <= 0), matching the league
rule above.

    pos  band      n       P(goose)
    QB   0-4       158      5.7%
    QB   4-8       155      3.9%
    QB   8-12      338      4.4%   <- clamped to 3.9, see _monotonic() below
    QB   12+      4619      1.1%
    RB   0-4       495     10.3%
    RB   4-8      1352      4.4%
    RB   8-12     2506      1.7%
    RB   12+      6055      0.8%
    WR   0-4       499     21.0%
    WR   4-8      1907      8.5%
    WR   8-12     4316      5.0%
    WR   12+      8002      2.3%
    TE   0-4       235     13.6%
    TE   4-8      1162      8.3%
    TE   8-12     2076      4.9%
    TE   12+      1422      2.9%

Sanity check for launch: run these over a real Dynasty Dons week and the
league-wide expected goose count should land near 3.6 (the measured rate is
2.7% of starter-slots, 27% of team-weeks). If week 1 predicts 11, something is
wrong -- do not ship it.

Replace this table with the app's own stored projections once a season of them
exists; prior-week average is a stand-in for a projection, not a projection.
"""
from __future__ import annotations

from typing import Iterable, Sequence

# --------------------------------------------------------------------------
# Detection
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
# Prediction
# --------------------------------------------------------------------------

_BANDS = (4.0, 8.0, 12.0, float("inf"))

_RAW_BASE_RATES = {
    "QB": (0.057, 0.039, 0.044, 0.011),
    "RB": (0.103, 0.044, 0.017, 0.008),
    "WR": (0.210, 0.085, 0.050, 0.023),
    "TE": (0.136, 0.083, 0.049, 0.029),
}


def _monotonic(rates: tuple) -> tuple:
    """
    A higher projection must never carry MORE goose risk. QB 8-12 measured
    4.4% against 3.9% for 4-8, which is a 338-sample wobble rather than a real
    effect, and it would show up in the app as a better quarterback pricing
    worse. Clamp each band to at most the band below it.
    """
    out = []
    ceiling = 1.0
    for r in rates:
        r = min(r, ceiling)
        out.append(r)
        ceiling = r
    return tuple(out)


BASE_RATES = {pos: _monotonic(rates) for pos, rates in _RAW_BASE_RATES.items()}
DEFAULT_POSITION = "WR"  # the most common starter here, and the most goose-prone

# What to use when there is NO projection at all -- a week-1 rookie, a player
# the feed doesn't cover. The population rate for the position, measured over
# the same 40k starter-slots.
#
# The obvious choice was the lowest band (0-4), on the reasoning that an
# unknown player is a bad player. That is wrong and the calibration test caught
# it: a starter with no history is usually a rookie somebody drafted on
# purpose, and those slots goose at 1.9%, not the 21% the bottom WR band would
# have charged them. 269 such slots were predicting 56 gooses against 5 real
# ones, which was most of the model's original over-prediction.
UNKNOWN_PROJECTION_RATES = {"QB": 0.015, "RB": 0.018, "WR": 0.043, "TE": 0.053}

# Dynasty Dons gooses LESS than the cross-league population the base rates were
# measured on, and for a real reason: this league has the most generous scoring
# of the set (full PPR, 7-point TDs, 0.5 per rush/rec first down, TE premium),
# so it takes less production to escape zero. Several of the leagues in that
# sample are half-PPR with no bonuses, where a quiet game lands on zero far
# more easily.
#
# Rather than refit the whole table on the ~3.7k Dynasty Dons slots -- too thin
# to band four ways by position -- keep the cross-league SHAPE and scale it by
# one factor fitted to this league's own measured rate. Fitted 2026-09-06
# against 2024 + 2025 weeks 1-14: raw model 5.11 gooses/week against 3.89
# actual, so 3.89/5.11 = 0.762.
#
# Refit this whenever the league changes scoring, and re-run tests/test_goose.py.
LEAGUE_CALIBRATION = 0.762

# Statuses that mean "will not play". Sleeper uses these on the player record.
OUT_STATUSES = {"Out", "IR", "PUP", "Sus", "NA", "DNR", "Doubtful", "COV"}
OUT_HARD = {"Out", "IR", "PUP", "Sus", "NA", "DNR", "COV"}

P_EMPTY_SLOT = 1.00
P_BYE = 0.98
P_OUT = 0.90
P_DOUBTFUL = 0.65
P_ZERO_PROJECTION = 0.55   # Sleeper projects ~0 for players it expects not to play.
                           # The one number here that is NOT measured -- the
                           # backtest has no forward projections to fit it
                           # against. It is a backstop for when injury_status
                           # is missing; revisit after a season of stored
                           # projections makes it checkable.
QUESTIONABLE_MULTIPLIER = 2.5
QUESTIONABLE_FLOOR = 0.15
ZERO_PROJECTION_THRESHOLD = 1.0


def base_rate(position: str | None, projection: float | None) -> float:
    """The uncalibrated cross-league rate. See LEAGUE_CALIBRATION for the scaling."""
    pos = (position or "").upper()
    if projection is None:
        return UNKNOWN_PROJECTION_RATES.get(pos, UNKNOWN_PROJECTION_RATES[DEFAULT_POSITION])
    rates = BASE_RATES.get(pos, BASE_RATES[DEFAULT_POSITION])
    proj = float(projection)
    for i, edge in enumerate(_BANDS):
        if proj < edge:
            return rates[i]
    return rates[-1]


def player_goose_probability(
    *,
    player_id=None,
    position: str | None = None,
    projection: float | None = None,
    injury_status: str | None = None,
    on_bye: bool = False,
    projection_is_forecast: bool = True,
) -> float:
    """
    P(this starter scores <= 0), as a float in [0, 1].

    Checked in order of how decisive each signal is. Availability dominates:
    a player who is not playing is a near-certain goose regardless of how good
    he is, and a healthy WR1 is a near-certain non-goose regardless of matchup.

    `projection_is_forecast` must be False when `projection` is a backward
    average rather than a real forward projection -- backtests do this. The
    zero-projection backstop below only makes sense for a forecast: Sleeper
    projecting 0.0 means it believes the player will not play, whereas an
    average of 0.0 just means he has been quiet.
    """
    if is_empty_slot(player_id):
        return P_EMPTY_SLOT
    if on_bye:
        return P_BYE

    status = (injury_status or "").strip()
    if status in OUT_HARD:
        return P_OUT
    if status == "Doubtful":
        return P_DOUBTFUL

    if (
        projection_is_forecast
        and projection is not None
        and float(projection) <= ZERO_PROJECTION_THRESHOLD
    ):
        return P_ZERO_PROJECTION

    p = base_rate(position, projection) * LEAGUE_CALIBRATION
    if status == "Questionable":
        p = max(p * QUESTIONABLE_MULTIPLIER, QUESTIONABLE_FLOOR)
    return min(p, 0.99)


def team_chug_odds(probabilities: Iterable[float]) -> float:
    """
    P(at least one goose) = 1 - product(1 - p).

    Independence is assumed and is not strictly true -- two starters can share
    a game that gets weather-cancelled, and a single owner's neglect correlates
    across his whole lineup. Both push the real number slightly higher than
    this. Good enough for a beer game; do not quote it as a real book price.
    """
    survive = 1.0
    for p in probabilities:
        p = min(max(float(p), 0.0), 1.0)
        survive *= (1.0 - p)
    return min(max(1.0 - survive, 0.005), 0.995)


def american_price(probability: float, round_to: int = 5) -> str:
    """
    Probability -> a sportsbook price, because '+285' reads better on the board
    than '26%'. No vig is applied: this is a display of the model's own number,
    not a line anyone is betting into.
    """
    p = min(max(float(probability), 0.005), 0.995)
    if p < 0.5:
        raw = (1.0 - p) / p * 100.0
        value = int(round(raw / round_to) * round_to)
        return f"+{value}"
    raw = p / (1.0 - p) * 100.0
    value = int(round(raw / round_to) * round_to)
    return f"-{max(value, 100)}"


def implied_probability(price: str) -> float:
    """Inverse of american_price, for tests and for tooltips."""
    price = str(price).strip()
    value = float(price.lstrip("+"))
    if value < 0:
        value = abs(value)
        return value / (value + 100.0)
    return 100.0 / (value + 100.0)


def expected_gooses(probabilities: Iterable[float]) -> float:
    """
    Sum of per-player probabilities -- the expected COUNT, not the chance of at
    least one. This is the launch sanity check: summed across all 12 lineups it
    should land near 3.6 for a normal week.
    """
    return round(sum(min(max(float(p), 0.0), 1.0) for p in probabilities), 2)
