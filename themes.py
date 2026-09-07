"""
themes.py
=========
The five owner-selectable colour themes.

Values are the validated output of the design canvas's colour solver
(Claude Design, Sept 2026) -- every tier ramp here already passed contrast,
lightness-monotonicity, badge-separation and colour-blindness-simulation
checks (see the design canvas artboards). Do not hand-edit a hex code
without re-running that solver; a colour that "looks right" can quietly fail
one of those checks (that's the whole reason the solver exists).

Each theme is a dict of CSS custom-property values, keyed to exactly the
property names base.html declares under `:root[data-theme="<key>"]`.
`tiers` holds the five risk-tier steps in TIERS order
(SAFE/SOLID/SHAKY/GOOSE BAIT/COOKED), each a {bg, fg} pair for a badge's
fill and label. Team ratings (CLEAN/STEADY/EXPOSED/GOOSE BAIT) reuse the
first four of these steps -- see base.html's .trisk-* rules.
"""
from __future__ import annotations

THEMES: dict[str, dict] = {
    "goothulu": {
        "label": "Goothulu",
        "blurb": "The original. Candlelit gold on a near-black warm ground.",
        "mode": "dark",
        "bg": "#0E0B07", "surf": "#1A1510", "surf2": "#251D15", "border": "#33281C",
        "text": "#F5EBD0", "text2": "#A08D74", "text3": "#6B5C48",
        "brand": "#E8B923", "halo": "#F2CE4B", "curse": "#D9821E", "blessing": "#E8B923", "ink": "#0E0B07",
        "tiers": {
            "safe":   {"bg": "#07241b", "fg": "#5fbc9c"},
            "solid":  {"bg": "#242f19", "fg": "#a0c081"},
            "shaky":  {"bg": "#423917", "fg": "#e1cb88"},
            "bait":   {"bg": "#a34803", "fg": "#fef1ea"},
            "cooked": {"bg": "#ec554e", "fg": "#1f0701"},
        },
    },
    "goosiah": {
        "label": "Goosiah",
        "blurb": "The holy one. Parchment, gold leaf, royal blue and purple -- the only light theme.",
        "mode": "light",
        "bg": "#F6F2E7", "surf": "#FFFFFF", "surf2": "#EDE6D6", "border": "#D8CDB4",
        "text": "#1B1733", "text2": "#54507A", "text3": "#837DA0",
        "brand": "#8A6400", "halo": "#C9A227", "curse": "#8B2033", "blessing": "#8A6400", "ink": "#F6F2E7",
        "tiers": {
            "safe":   {"bg": "#e5effe", "fg": "#044f7f"},
            "solid":  {"bg": "#c2e9d2", "fg": "#015233"},
            "shaky":  {"bg": "#e6cfa1", "fg": "#4c3c01"},
            "bait":   {"bg": "#fd925c", "fg": "#3e1901"},
            "cooked": {"bg": "#ee5a6b", "fg": "#260101"},
        },
    },
    "goosifer": {
        "label": "Goosifer",
        "blurb": "Fire and brimstone. Cold ash at the safe end, white-hot at the bad end.",
        "mode": "dark",
        "bg": "#0A0505", "surf": "#170B08", "surf2": "#24110C", "border": "#3E1913",
        "text": "#F8E6D8", "text2": "#B08876", "text3": "#775345",
        "brand": "#FF6B2B", "halo": "#FFA347", "curse": "#E8321A", "blessing": "#FFB347", "ink": "#170B08",
        "tiers": {
            "safe":   {"bg": "#283238", "fg": "#b1bdc5"},
            "solid":  {"bg": "#4c3e24", "fg": "#d8d1c6"},
            "shaky":  {"bg": "#6c482c", "fg": "#f8ece4"},
            "bait":   {"bg": "#d45731", "fg": "#1b0901"},
            "cooked": {"bg": "#fe8b88", "fg": "#4d050e"},
        },
    },
    "maverick": {
        "label": "Maverick",
        "blurb": "Night carrier deck. A tier ramp lifted straight off an instrument panel.",
        "mode": "dark",
        "bg": "#0A1620", "surf": "#11212D", "surf2": "#1B2C3A", "border": "#2C4353",
        "text": "#E9EEF3", "text2": "#94A8B8", "text3": "#657A89",
        "brand": "#E8A33D", "halo": "#FFC46B", "curse": "#E05437", "blessing": "#E8A33D", "ink": "#0A1620",
        "tiers": {
            "safe":   {"bg": "#012017", "fg": "#59b697"},
            "solid":  {"bg": "#1a2a12", "fg": "#95bb7f"},
            "shaky":  {"bg": "#39300b", "fg": "#d0ba79"},
            "bait":   {"bg": "#ac5001", "fg": "#fef1ea"},
            "cooked": {"bg": "#f15251", "fg": "#200601"},
        },
    },
    "duckblind": {
        "label": "Duck Blind",
        "blurb": "Olive drab, weathered wood and brass, blaze orange at the dangerous end.",
        "mode": "dark",
        "bg": "#11150E", "surf": "#191E13", "surf2": "#242B1A", "border": "#3A4229",
        "text": "#EAE6D4", "text2": "#9E9673", "text3": "#6C6549",
        "brand": "#C9A227", "halo": "#E3BF48", "curse": "#FF6A13", "blessing": "#C9A227", "ink": "#11150E",
        "tiers": {
            "safe":   {"bg": "#243522", "fg": "#95c890"},
            "solid":  {"bg": "#3e3f25", "fg": "#d4d690"},
            "shaky":  {"bg": "#594928", "fg": "#fee5b7"},
            "bait":   {"bg": "#d06126", "fg": "#190b01"},
            "cooked": {"bg": "#fe8671", "fg": "#420c01"},
        },
    },
}

DEFAULT_THEME = "goothulu"


def theme_choices() -> list[tuple[str, str, str]]:
    """(key, label, blurb) triples, in dict order, for the settings picker."""
    return [(k, t["label"], t["blurb"]) for k, t in THEMES.items()]


def resolve(theme_id) -> str:
    """A stored value that isn't a live theme id (unset, stale, hand-edited
    row) falls back to the default rather than rendering an undefined var."""
    return theme_id if theme_id in THEMES else DEFAULT_THEME
