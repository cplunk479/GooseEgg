"""
set_pins.py
===========
Set an owner's PIN and grant admin.

This script is the ONLY thing that makes someone an admin. Announcing it in
the group chat grants nothing -- that is exactly how Blayne, Nick and Duncan
ended up unable to open the FAAB admin screens after being named as its role
owners. Run this for Conner and Blayne before week 1.

Usage
-----
    python set_pins.py --list
    python set_pins.py --roster 3 --pin 4821
    python set_pins.py --roster 3 --admin
    python set_pins.py --roster 3 --pin 4821 --admin
    python set_pins.py --roster 3 --no-admin
"""
from __future__ import annotations

import argparse
import os
import sys

import bcrypt
from dotenv import load_dotenv

import db as dbmod

load_dotenv()

LEAGUE_ID = os.environ.get("GOOSE_LEAGUE_ID", "")
SEASON = int(os.environ.get("GOOSE_SEASON", "2026"))


def list_owners(db) -> None:
    rows = db.execute(
        "SELECT roster_id, owner_name, team_name, pin_hash IS NOT NULL AS has_pin, is_admin "
        "FROM owners WHERE league_id = %s AND season = %s ORDER BY roster_id",
        (LEAGUE_ID, SEASON),
    ).fetchall()
    if not rows:
        sys.exit("No owners. Run: python db_init.py --seed-owners")
    print(f"{'id':>3}  {'owner':<18} {'team':<30} {'pin':<5} admin")
    for r in rows:
        print(f"{r['roster_id']:>3}  {(r['owner_name'] or '')[:18]:<18} "
              f"{(r['team_name'] or '')[:30]:<30} {'set' if r['has_pin'] else '--':<5} "
              f"{'yes' if r['is_admin'] else ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Set PINs and grant admin.")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--roster", type=int)
    ap.add_argument("--pin", help="4+ digits")
    ap.add_argument("--admin", action="store_true")
    ap.add_argument("--no-admin", action="store_true")
    args = ap.parse_args()

    db = dbmod.open_wrapped()
    try:
        if args.list or args.roster is None:
            list_owners(db)
            return

        row = db.execute(
            "SELECT * FROM owners WHERE league_id = %s AND season = %s AND roster_id = %s",
            (LEAGUE_ID, SEASON, args.roster),
        ).fetchone()
        if row is None:
            sys.exit(f"No roster {args.roster} in {LEAGUE_ID} / {SEASON}.")

        if args.pin:
            pin = args.pin.strip()
            if len(pin) < 4 or not pin.isdigit():
                sys.exit("PIN must be at least 4 digits.")
            digest = bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()
            db.execute(
                "UPDATE owners SET pin_hash = %s WHERE league_id = %s AND season = %s AND roster_id = %s",
                (digest, LEAGUE_ID, SEASON, args.roster),
            )
            print(f"PIN set for roster {args.roster}.")

        if args.admin or args.no_admin:
            value = bool(args.admin and not args.no_admin)
            db.execute(
                "UPDATE owners SET is_admin = %s WHERE league_id = %s AND season = %s AND roster_id = %s",
                (value, LEAGUE_ID, SEASON, args.roster),
            )
            print(f"admin = {value} for roster {args.roster}.")

        db.commit()
        list_owners(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
