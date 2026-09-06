"""
filters.py
==========
The Jinja filters, in one place.

They live here rather than as decorated functions inside app.py for one
reason: tests/test_templates.py needs the SAME filters the app registers, and
when it had its own hand-written copies they drifted -- a filter was renamed
in app.py, the test kept passing against its own stale definition, and the
template only broke in a browser. One dict, imported by both, cannot drift.
"""
from __future__ import annotations


def tierclass(tier) -> str:
    """'GOOSE BAIT' -> 'goose-bait', so a tier can drive a CSS class directly."""
    return (tier or "unknown").lower().replace(" ", "-")


def one_in(rate) -> str:
    """
    A rate people can picture, sitting next to a tier they can read.

    "1 in N" only for rates where N is close to a whole number. 43% rounds to
    "1 in 2", which reads as a coin flip and overstates it by seven points --
    so anything at or above a third falls back to a plain percentage, which is
    exact and no harder to say.
    """
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return ""
    if rate <= 0:
        return ""
    if rate >= 0.34:
        return f"{rate * 100:.0f}% of weeks"
    return f"1 in {round(1 / rate)} weeks"


def pct(probability) -> str:
    return "--" if probability is None else f"{float(probability) * 100:.0f}%"


def pts(value) -> str:
    return "--" if value is None else f"{float(value):.1f}"


FILTERS = {
    "tierclass": tierclass,
    "oneIn": one_in,
    "pct": pct,
    "pts": pts,
}
