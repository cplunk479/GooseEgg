"""
db_init.py
==========
Create (or additively migrate) the schema, seed the commissioner settings, and
optionally seed owners from Sleeper.

Safe to re-run. Every statement is CREATE ... IF NOT EXISTS or an idempotent
upsert, and settings are only written when the key is missing -- so a re-run
never stomps a rule the commissioner has tuned. That property matters: in the
FAAB app a forgotten db_init re-run after a deploy made a shipped feature look
like it had never shipped, and the fix was to make re-running harmless and
routine.

Usage
-----
    export DATABASE_URL=postgresql://...
    python db_init.py                 # schema + settings
    python db_init.py --seed-owners   # also pull the 12 owners from Sleeper
    python db_init.py --reset         # DROP everything first (destructive)
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

import db as dbmod
import settings as settingsmod

load_dotenv()

LEAGUE_ID = os.environ.get("GOOSE_LEAGUE_ID", "")
SEASON = int(os.environ.get("GOOSE_SEASON", "2026"))

TABLES = [
    # ---------------------------------------------------------------- meta
    """
    CREATE TABLE IF NOT EXISTS app_meta (
        key         TEXT PRIMARY KEY,
        value       TEXT
    )
    """,

    # -------------------------------------------------------------- owners
    # One row per roster per season. PIN auth mirrors the FAAB app (bcrypt).
    # is_admin is what actually grants Admin -- announcing someone as an admin
    # in the group chat grants nothing, which is exactly how Blayne, Nick and
    # Duncan ended up locked out of the FAAB admin screens.
    """
    CREATE TABLE IF NOT EXISTS owners (
        league_id   TEXT NOT NULL,
        season      INTEGER NOT NULL,
        roster_id   INTEGER NOT NULL,
        user_id     TEXT,
        owner_name  TEXT,
        team_name   TEXT,
        avatar      TEXT,
        pin_hash    TEXT,
        is_admin    BOOLEAN NOT NULL DEFAULT FALSE,
        created_at  BIGINT,
        PRIMARY KEY (league_id, season, roster_id)
    )
    """,

    # ------------------------------------------------------- player cache
    """
    CREATE TABLE IF NOT EXISTS players_cache (
        player_id      TEXT PRIMARY KEY,
        full_name      TEXT,
        position       TEXT,
        team           TEXT,
        injury_status  TEXT,
        updated_at     BIGINT
    )
    """,

    # --------------------------------------------------------------- weeks
    # status: 'upcoming' -> 'open' -> 'locked' -> 'final'
    # A week is CLOSED until an admin opens it, same guardrail the FAAB app
    # needed: projections post before lineups are set.
    """
    CREATE TABLE IF NOT EXISTS weeks (
        season       INTEGER NOT NULL,
        week         INTEGER NOT NULL,
        status       TEXT NOT NULL DEFAULT 'upcoming',
        lock_epoch   BIGINT,
        end_epoch    BIGINT,
        opened_by    INTEGER,
        opened_at    BIGINT,
        locked_at    BIGINT,
        settled_at   BIGINT,
        PRIMARY KEY (season, week)
    )
    """,

    # -------------------------------------------------- lineup snapshots
    # Written at lock and never rewritten. proj_pts is the frozen number a
    # curse is graded against -- the single most important column in the
    # schema. The FAAB app learned this the hard way with bet lines: if the
    # threshold can move after the decision, the decision was never real.
    """
    CREATE TABLE IF NOT EXISTS lineup_slots (
        season       INTEGER NOT NULL,
        week         INTEGER NOT NULL,
        roster_id    INTEGER NOT NULL,
        slot_index   INTEGER NOT NULL,
        slot         TEXT,
        player_id    TEXT,
        proj_pts     NUMERIC(8,2),
        actual_pts   NUMERIC(8,2),
        goose_prob   NUMERIC(6,4),
        is_goose     BOOLEAN,
        PRIMARY KEY (season, week, roster_id, slot_index)
    )
    """,

    # team-level snapshot, one row per roster per week
    """
    CREATE TABLE IF NOT EXISTS team_weeks (
        season        INTEGER NOT NULL,
        week          INTEGER NOT NULL,
        roster_id     INTEGER NOT NULL,
        proj_total    NUMERIC(8,2),
        actual_total  NUMERIC(8,2),
        chug_odds     NUMERIC(6,4),
        goose_count   INTEGER NOT NULL DEFAULT 0,
        locked_at     BIGINT,
        PRIMARY KEY (season, week, roster_id)
    )
    """,

    # -------------------------------------------------------------- gooses
    """
    CREATE TABLE IF NOT EXISTS gooses (
        id          SERIAL PRIMARY KEY,
        season      INTEGER NOT NULL,
        week        INTEGER NOT NULL,
        roster_id   INTEGER NOT NULL,
        player_id   TEXT,
        slot        TEXT,
        slot_index  INTEGER,
        points      NUMERIC(8,2),
        empty_slot  BOOLEAN NOT NULL DEFAULT FALSE,
        created_at  BIGINT,
        UNIQUE (season, week, roster_id, slot_index)
    )
    """,

    # --------------------------------------------------------------- chugs
    # reason: 'goose' | 'curse'
    # status: 'owed' | 'paid' | 'waived'
    # rolled_from_week supports the max_chugs_per_week cap: the overflow does
    # not vanish, it moves to the next week and keeps aging.
    """
    CREATE TABLE IF NOT EXISTS chugs (
        id                SERIAL PRIMARY KEY,
        season            INTEGER NOT NULL,
        week              INTEGER NOT NULL,
        roster_id         INTEGER NOT NULL,
        reason            TEXT NOT NULL,
        ref_id            INTEGER,
        status            TEXT NOT NULL DEFAULT 'owed',
        safe_pour         BOOLEAN NOT NULL DEFAULT FALSE,
        rolled_from_week  INTEGER,
        confirmed_by      INTEGER,
        confirmed_at      BIGINT,
        created_at        BIGINT
    )
    """,

    # -------------------------------------------------------- curse tokens
    # source: 'chug' | 'stipend' | 'curse_landed' | 'admin'
    """
    CREATE TABLE IF NOT EXISTS curse_tokens (
        id           SERIAL PRIMARY KEY,
        season       INTEGER NOT NULL,
        roster_id    INTEGER NOT NULL,
        earned_week  INTEGER,
        source       TEXT NOT NULL DEFAULT 'chug',
        spent_on     INTEGER,
        note         TEXT,
        created_at   BIGINT
    )
    """,

    # -------------------------------------------------------------- curses
    # status: 'cast' -> 'blocked' | 'landed' | 'survived'
    #   blocked  = target held a Goosiah blessing, auto-consumed
    #   landed   = target missed the frozen projection -> chug
    #   survived = target beat it -> target earns a blessing
    # threshold_proj is copied in at lock, never read live.
    """
    CREATE TABLE IF NOT EXISTS curses (
        id                SERIAL PRIMARY KEY,
        season            INTEGER NOT NULL,
        week              INTEGER NOT NULL,
        caster_roster_id  INTEGER NOT NULL,
        target_roster_id  INTEGER NOT NULL,
        token_id          INTEGER,
        status            TEXT NOT NULL DEFAULT 'cast',
        threshold_proj    NUMERIC(8,2),
        actual_total      NUMERIC(8,2),
        blocked_by        INTEGER,
        created_by_admin  BOOLEAN NOT NULL DEFAULT FALSE,
        created_at        BIGINT,
        resolved_at       BIGINT
    )
    """,

    # ----------------------------------------------------------- blessings
    # status: 'active' | 'consumed' | 'expired'
    # Blessings do NOT bank: earned in week N, live only through week N+1, and
    # they fire automatically. There is no "spend" action anywhere in the app.
    """
    CREATE TABLE IF NOT EXISTS blessings (
        id                SERIAL PRIMARY KEY,
        season            INTEGER NOT NULL,
        roster_id         INTEGER NOT NULL,
        earned_week       INTEGER NOT NULL,
        expires_after     INTEGER NOT NULL,
        status            TEXT NOT NULL DEFAULT 'active',
        consumed_by       INTEGER,
        created_by_admin  BOOLEAN NOT NULL DEFAULT FALSE,
        created_at        BIGINT,
        resolved_at       BIGINT
    )
    """,
]

# Additive column migrations, run after the CREATE TABLEs on every init. This
# is how v0.5's tier columns reach a database that was created by v0.4 --
# ADD COLUMN IF NOT EXISTS is a no-op on a fresh database and the only safe way
# to change a live one without a migration framework. Never put a DROP or a
# type change in this list: it runs unattended on every deploy.
MIGRATIONS = [
    # v0.5 -- risk tiers replace the goose percentage
    "ALTER TABLE lineup_slots ADD COLUMN IF NOT EXISTS tier TEXT",
    "ALTER TABLE lineup_slots ADD COLUMN IF NOT EXISTS proj_ratio NUMERIC(6,3)",
    "ALTER TABLE lineup_slots ADD COLUMN IF NOT EXISTS risk_reason TEXT",
    "ALTER TABLE team_weeks  ADD COLUMN IF NOT EXISTS risk_tier TEXT",
    "ALTER TABLE team_weeks  ADD COLUMN IF NOT EXISTS risk_score NUMERIC(6,2)",
    "ALTER TABLE team_weeks  ADD COLUMN IF NOT EXISTS at_risk INTEGER",
    # v0.5 -- a week can now be unlocked, so record who did it and when
    "ALTER TABLE weeks ADD COLUMN IF NOT EXISTS unlocked_at BIGINT",
    "ALTER TABLE weeks ADD COLUMN IF NOT EXISTS unlocked_by INTEGER",
    # v0.5 -- demo mode. Every row demo mode creates carries this flag, which
    # is the ONLY thing that lets leaving demo mode delete exactly what it
    # made and nothing a real week produced. Never set it by hand.
    "ALTER TABLE curses       ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE curse_tokens ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE blessings    ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE chugs        ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE owners       ADD COLUMN IF NOT EXISTS theme TEXT NOT NULL DEFAULT 'goothulu'",
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_gooses_owner ON gooses (season, roster_id)",
    "CREATE INDEX IF NOT EXISTS idx_chugs_owner ON chugs (season, roster_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_chugs_week ON chugs (season, week)",
    "CREATE INDEX IF NOT EXISTS idx_tokens_owner ON curse_tokens (season, roster_id, spent_on)",
    "CREATE INDEX IF NOT EXISTS idx_curses_week ON curses (season, week)",
    "CREATE INDEX IF NOT EXISTS idx_curses_target ON curses (season, target_roster_id)",
    "CREATE INDEX IF NOT EXISTS idx_blessings_owner ON blessings (season, roster_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_lineup_week ON lineup_slots (season, week)",
]

DROP_ORDER = [
    "blessings", "curses", "curse_tokens", "chugs", "gooses",
    "team_weeks", "lineup_slots", "weeks", "players_cache", "owners", "app_meta",
]


def create_schema(db, reset: bool = False) -> None:
    if reset:
        for table in DROP_ORDER:
            db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        print("  dropped existing tables")
    for stmt in TABLES:
        db.execute(stmt)
    for stmt in MIGRATIONS:
        db.execute(stmt)
    for stmt in INDEXES:
        db.execute(stmt)
    db.commit()
    print(f"  {len(TABLES)} tables, {len(MIGRATIONS)} migrations, {len(INDEXES)} indexes")


def seed_settings(db) -> int:
    """
    Write each default ONLY if the key is missing. A re-run must never reset a
    rule the commissioner has changed mid-season.
    """
    written = 0
    for key, value in settingsmod.DEFAULTS.items():
        row = db.execute("SELECT 1 FROM app_meta WHERE key = %s", (key,)).fetchone()
        if row is None:
            settingsmod.set_raw(db, key, value)
            written += 1
    if db.execute("SELECT 1 FROM app_meta WHERE key = 'active_week'").fetchone() is None:
        settingsmod.set_raw(db, "active_week", "1")
        written += 1
    db.commit()
    return written


def seed_owners(db) -> int:
    """Pull the league's twelve owners from Sleeper. PINs are set separately."""
    import time
    import sleeper

    if not LEAGUE_ID:
        sys.exit("ERROR: GOOSE_LEAGUE_ID not set -- cannot seed owners.")

    users = {u["user_id"]: u for u in sleeper.users(LEAGUE_ID)}
    now = int(time.time())
    count = 0
    for r in sleeper.rosters(LEAGUE_ID):
        user = users.get(r.get("owner_id")) or {}
        meta = user.get("metadata") or {}
        db.execute(
            """
            INSERT INTO owners
                (league_id, season, roster_id, user_id, owner_name, team_name, avatar, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (league_id, season, roster_id) DO UPDATE SET
                user_id    = EXCLUDED.user_id,
                owner_name = EXCLUDED.owner_name,
                team_name  = EXCLUDED.team_name,
                avatar     = EXCLUDED.avatar
            """,
            (
                LEAGUE_ID, SEASON, r["roster_id"], r.get("owner_id"),
                user.get("display_name"), meta.get("team_name"),
                user.get("avatar"), now,
            ),
        )
        count += 1
    db.commit()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the Goose Egg database.")
    parser.add_argument("--reset", action="store_true", help="DROP every table first (destructive)")
    parser.add_argument("--seed-owners", action="store_true", help="Pull owners from Sleeper")
    args = parser.parse_args()

    db = dbmod.open_wrapped()
    try:
        print(f"Goose Egg init -- league {LEAGUE_ID or '(unset)'}, season {SEASON}")
        if args.reset:
            confirm = input("This DROPS every table. Type 'yes' to continue: ")
            if confirm.strip().lower() != "yes":
                sys.exit("aborted")
        print("schema:")
        create_schema(db, reset=args.reset)
        written = seed_settings(db)
        print(f"settings: {written} default(s) written, existing values left alone")
        if args.seed_owners:
            print(f"owners:   {seed_owners(db)} seeded from Sleeper")
        print("\nNext: set PINs and grant admin to Conner and Blayne (set_pins.py).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
