"""
settings.py
===========
Typed accessors over the `app_meta` key/value table.

The whole point of this module: EVERY rule of the curse economy is a row here,
not a constant in the code. The league will want to tune these in week 4 and
nobody should be redeploying to do it. See
Dev/goose-egg-platform-design.md section 0 for the decision.

Nothing here raises. A missing or garbage row falls back to the default rather
than taking down /poll or a page load.

Commissioner-tunable rules
--------------------------
points_per_chug           int   Curse tokens minted per completed chug. v1 = 1.
weekly_stipend            int   Free tokens every owner gets each week. v1 = 0.
                                Turn this up if the curse economy stalls --
                                at the measured ~3.6 gooses/week only about a
                                quarter of the league mints anything, so most
                                owners would otherwise never cast a curse.
curse_stacking            bool  Allow spending 2+ tokens on one target.
max_curses_per_target     int   How many curses one owner can absorb in a week.
max_chugs_per_week        int   Cap per owner; the remainder rolls forward as
                                debt instead of piling into one sitting.
landed_curse_mints_point  bool  Does a landed curse mint a token for the caster.
survived_curse_mints_token bool Does surviving a curse mint a token for the target.
                                On by default: beating the bar under a curse is
                                the hardest thing an owner does all week, and
                                the blessing alone expires unused most weeks.
curses_sealed_until_lock  bool  Hide curse targets from the league until kickoff.
safe_pour_allowed         bool  Allow a non-alcoholic equivalent to settle a chug.

Goosifer's Wrath
----------------
wrath_enabled             bool  Arm the mark at all. Off returns the game to
                                v0.7 behaviour exactly.
wrath_multiplier          int   Chugs a landed curse costs a marked owner. 2 =
                                double. Those chugs mint NO tokens, whatever
                                points_per_chug says -- the punishment is that
                                the beer buys you nothing.
wrath_persists            bool  OFF (default): the mark lives one week, exactly
                                like a blessing, and re-arms on each new miss.
                                ON: it stays until the owner survives a curse.
                                Rows already written keep the rule they were
                                written under; flipping this does not rewrite
                                a mark somebody is already carrying.

Season shape
------------
active_week               int   The week the app is currently "on".
season_start_week         int   1
season_end_week           int   17 -- playoffs INCLUDED. Eliminated teams stay
                                in consolation matchups and keep chugging.

Automation
----------
auto_advance              bool  Roll the week after the last NFL game.
auto_lock                 bool  Lock lineups + snapshot projections at the
                                first Sunday kickoff.
auto_settle               bool  Grade gooses and curses once the week is over.
"""
from __future__ import annotations

DEFAULTS = {
    # curse economy -- v1 ships deliberately plain, see the module docstring
    "points_per_chug": "1",
    "weekly_stipend": "0",
    "curse_stacking": "0",
    "max_curses_per_target": "1",
    "max_chugs_per_week": "3",
    "landed_curse_mints_point": "1",
    "survived_curse_mints_token": "1",
    # Goosifer's Wrath
    "wrath_enabled": "1",
    "wrath_multiplier": "2",
    "wrath_persists": "0",
    "curses_sealed_until_lock": "1",
    "safe_pour_allowed": "1",
    # season shape
    "season_start_week": "1",
    "season_end_week": "17",
    # automation
    "auto_advance": "1",
    "auto_lock": "1",
    "auto_settle": "1",
}

# Keys the Admin -> Rules screen renders, in display order, with the editor
# hint and a one-line explanation for the commissioner.
TUNABLE = [
    ("points_per_chug", "int", "Curse tokens earned per completed chug"),
    ("weekly_stipend", "int", "Free curse tokens per owner per week"),
    ("curse_stacking", "bool", "Allow stacking tokens for a double curse"),
    ("max_curses_per_target", "int", "Curses one owner can absorb in a week"),
    ("max_chugs_per_week", "int", "Chug cap per owner; the rest rolls forward"),
    ("landed_curse_mints_point", "bool", "A landed curse mints a token for the caster"),
    ("survived_curse_mints_token", "bool", "Surviving a curse mints a token for the target"),
    ("wrath_enabled", "bool", "Goosifer's Wrath: a landed curse marks the target"),
    ("wrath_multiplier", "int", "Chugs a landed curse costs a marked owner (they mint nothing)"),
    ("wrath_persists", "bool", "The mark stays until they survive a curse, not just one week"),
    ("curses_sealed_until_lock", "bool", "Hide curse targets until Sunday kickoff"),
    ("safe_pour_allowed", "bool", "Allow a non-alcoholic pour to settle a chug"),
]


def get_raw(db, key: str, default=None):
    try:
        row = db.execute("SELECT value FROM app_meta WHERE key = %s", (key,)).fetchone()
    except Exception:
        return DEFAULTS.get(key, default)
    if row is None or row["value"] is None:
        return DEFAULTS.get(key, default)
    return row["value"]


def set_raw(db, key: str, value) -> None:
    db.execute(
        "INSERT INTO app_meta (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, str(value)),
    )


def get_int(db, key: str, default=None):
    raw = get_raw(db, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        try:
            return int(DEFAULTS.get(key, default))
        except (TypeError, ValueError):
            return default


def get_bool(db, key: str, default: bool = True) -> bool:
    raw = get_raw(db, key)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def set_bool(db, key: str, value: bool) -> None:
    set_raw(db, key, "1" if value else "0")


def all_tunables(db) -> list[dict]:
    """Everything the Admin -> Rules screen needs, in one call."""
    out = []
    for key, kind, label in TUNABLE:
        out.append({
            "key": key,
            "kind": kind,
            "label": label,
            "value": get_bool(db, key) if kind == "bool" else get_int(db, key, 0),
            "default": DEFAULTS.get(key),
        })
    return out
