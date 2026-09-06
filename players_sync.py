"""
players_sync.py
===============
Cache of Sleeper's player directory. The goose model leans on `injury_status`
harder than on anything else, so this is not cosmetic -- a stale cache means
an OUT player prices like a healthy one.

/players/nfl is a multi-MB dump and Sleeper asks for at most about one pull a
day, so this is throttled rather than run on every poll tick. It never raises:
a failed sync leaves the last good cache in place.
"""
from __future__ import annotations

import time

import sleeper

SYNC_INTERVAL_SECONDS = 20 * 3600


def last_synced_at(db):
    row = db.execute("SELECT value FROM app_meta WHERE key = 'players_synced_at'").fetchone()
    return int(row["value"]) if row and row["value"] else None


def sync(db, force: bool = False) -> int:
    last = last_synced_at(db)
    now = int(time.time())
    if not force and last is not None and (now - last) < SYNC_INTERVAL_SECONDS:
        return 0
    try:
        players = sleeper.all_players()
    except Exception:
        return 0
    if not players:
        return 0

    for pid, p in players.items():
        db.execute(
            """
            INSERT INTO players_cache (player_id, full_name, position, team, injury_status, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (player_id) DO UPDATE SET
                full_name     = EXCLUDED.full_name,
                position      = EXCLUDED.position,
                team          = EXCLUDED.team,
                injury_status = EXCLUDED.injury_status,
                updated_at    = EXCLUDED.updated_at
            """,
            (pid, p["full_name"], p["position"], p["team"], p["injury_status"], now),
        )
    db.execute(
        "INSERT INTO app_meta (key, value) VALUES ('players_synced_at', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (str(now),),
    )
    db.commit()
    return len(players)
