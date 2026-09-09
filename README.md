# GOOSE EGG Challenge — Dynasty Dons

A weekly drinking game with a sportsbook front end. Any player in your starting
lineup who scores **0 or less** is a *goose*, and the owner chugs. Completed
chugs mint **Goothulu's Curse** tokens, which you spend on another owner: miss
your locked projection while cursed and you chug too, and **Goosifer's Wrath**
marks you for next week; beat it and you earn **Goosiah's Blessing**, one week
of immunity, plus a curse token of your own.

Carrying the mark is the expensive part. A curse that lands on a marked owner
costs them **two** chugs instead of one, and those chugs mint **nothing** — the
one way to drink in this app and come away with no ammunition. Every screen
shows who is marked, which is the point: they are the best target on the board
and everybody can see it.

Sister app to `../faab-platform` (Bring Dat Wood), deliberately a separate
service and repo — that one is live mid-season for a different league and
nothing here may be able to break it.

- League: Dynasty Dons, Sleeper `1328860655079424000`, 12 teams
- Lineup: QB, RB, WR×3, TE, FLEX×4, SUPER_FLEX — 11 starters, no K/DEF/IDP
- Season: weeks 1–17, **playoffs included** (eliminated teams keep chugging)
- Design brief and locked rules: `../goose-egg-platform-design.md`

## Status

Live on Render. Login, Goose Watch, Curse, My Geese, Standings and Admin are
built; the week lifecycle (open → lock → unlock → settle) works and is tested
end to end.

**The Discord recap is still to come, and nothing has been run against live
Sleeper from this build environment** — see the outstanding list in
`CHANGELOG.md`.

## How risk is scored

Not a percentage. A **tier**, from where a player's projection falls against
the average starter projection at his position that week:

| Tier | Ratio to position average | P(goose), measured |
| :--- | :--- | :--- |
| `SAFE` | ≥ 1.25 | 1.1% |
| `SOLID` | 0.90 – 1.25 | 1.5% |
| `SHAKY` | 0.60 – 0.90 | 3.1% |
| `GOOSE BAIT` | 0.35 – 0.60 | 5.8% |
| `COOKED` | < 0.35 | 11.3% |

Availability is not folded into that. A player who is out, on bye, doubtful or
in an empty slot goes straight to `COOKED` **with the reason printed** — "OUT"
tells an owner more in three letters than "90%" did in three digits.

A lineup is rated from its mix — `CLEAN` / `STEADY` / `EXPOSED` /
`GOOSE BAIT` — at a measured 11% / 16% / 30% / 43% chance of at least one
goose. The average lineup is `EXPOSED`.

Cut points are measured, not chosen. `python analysis/fit_tiers.py` regenerates
them from 35,144 starter-slots in `Scripts/files/fantasy.db`. If the bands stop
separating, move them **in both that file and `goose.py`**.

| File | What it does |
| :--- | :--- |
| `goose.py` | Goose detection and the risk tiers. No Flask, no database — pure logic. |
| `sleeper.py` | Every Sleeper call: league, rosters, matchups, projections, kickoff clock, bye weeks, player directory. |
| `db.py` | Postgres connection helper, same shape as the FAAB app's. |
| `db_init.py` | Schema, settings seed, owner seed. Safe to re-run. |
| `settings.py` | The commissioner's tunable rules. Every economy value is a row, not a constant. |
| `watch.py` | Goose Watch: classifies every starter live into goosed / danger / pending / safe. Reads only, never writes. |
| `week_engine.py` | The week lifecycle: open, lock, settle, cast, confirm. All state changes live here. |
| `app.py` | Flask routes and PIN auth. Thin — it reads and renders. |
| `filters.py` | The Jinja filters, shared by `app.py` and the template tests so they cannot drift. |
| `demo.py` | Demo mode: a synthetic mid-Sunday feed, plus the only rows demo mode writes and takes back. |
| `analysis/fit_tiers.py` | Where the tier cut points come from. Re-run it when scoring changes. |
| `themes.py` | The five owner-selectable colour themes, base tokens and tier ramp, as data. `base.html` generates its CSS from this. |
| `players_sync.py` | Throttled cache of Sleeper's player directory, including `injury_status`. |
| `set_pins.py` | Sets PINs and grants admin. The only thing that makes someone an admin. |
| `tests/` | `test_goose.py` (tiers + calibration backtest), `test_week_engine.py` (full week, unlock, reset), `test_watch.py`, `test_templates.py`, `test_demo.py` (end-to-end over a stubbed Sleeper), `test_resilience.py` (what the app does when the database says no). Run `./run_tests.sh`. |

## Lineups on the board

Tapping a team on the Curse board opens its starting lineup: photo, slot, NFL
matchup and clock, risk tier, projection, and what they have actually scored.
`lineups.py` + `templates/_lineup.html`, ported from the FAAB app's board
expanders.

Actuals are read live from Sleeper on every render (matchups are cached 120s,
so a refresh gets scores at most two minutes old). Projections are the
opposite: once a week locks they come from the frozen `lineup_slots` snapshot
and never move, because that is the bar a curse is graded against. Before lock
there is nothing to freeze, so they are live and every screen says so.

The live goose count on a team row only counts FINISHED games. A zero in the
first quarter is Sunday happening, not a goose — the same line Goose Watch
draws.

## The Goose Crown

The drunkest owner wears it. `crown_key()` in `app.py` is the single
definition of that order (chugs, then gooses, then curses landed):
Standings sorts its whole table with it and `crown_leader()` takes the top
of the same order for the Curse tab's banner, so the two screens cannot
disagree. It stays vacant until somebody has actually chugged, and says so
when two owners are level. Second and third place wear the same honour at
lower intensity -- row wash, rank colour and mark all step down together.

## Themes

Five owner-selectable colour themes — Goothulu (the original), Goosiah (the
one light theme), Goosifer, Maverick and Duck Blind. Each owner picks their
own from the swatch in the top ribbon, on any screen (`POST /me/theme`,
which returns to the screen it was set from); it changes only
what that owner sees, stored in `owners.theme`. `base.html` builds one
`:root[data-theme="..."]` CSS block per theme straight from `themes.py`, so
the colour data and the stylesheet can never drift apart. A row missing the
column (an unmigrated database) falls back to Goothulu rather than 500ing —
see `test_resilience.py`.

## Admin

Five tabs, at `/admin`:

- **Chugs** — confirm a chug. Confirming is what mints a curse token; owing one
  earns nothing.
- **Week** — set active, open, lock, **unlock**, settle. Unlock reopens a locked
  week for casting *without* moving any frozen projection: a curse cast late is
  graded against the kickoff number like everyone else's.
- **Curses** — manual grants, voids, tokens, blessings.
- **Rules** — every value in the curse economy, tunable without a redeploy.
- **Demo** — a simulated mid-Sunday week for showing the app off. Every screen
  gets a loud banner and `/poll` stops locking and settling while it is on.
- **Reset** — clear one week or the whole season, behind a typed confirmation.
  For testing before the season starts. There is no undo.

## Deploying

`render.yaml`'s build command runs `pip install -r requirements.txt && python
db_init.py`, so schema migrations apply on every deploy. **If you deploy any
other way, run `python db_init.py` yourself** — it is additive and idempotent,
and skipping it is how v0.5.0 500'd Goose Watch on a column that did not exist
yet.

## Setup

```bash
cp .env.example .env          # fill in DATABASE_URL and GOOSE_LEAGUE_ID
pip install -r requirements.txt
python db_init.py --seed-owners
python set_pins.py --roster <n> --pin <pin> --admin   # Conner and Blayne
./run_tests.sh
flask --app app run --port 5001
```

Weekly rhythm: an admin opens the week, owners cast curses, the app locks at
the first Sunday kickoff and settles once the last game is done. `/poll` does
the locking, settling and week roll on a schedule; the Week tab does all three
by hand.

## The model, briefly

A goose here almost never means a bad game. Scoring is generous enough that one
catch is worth 1.0 and the first down it earns another 0.5, so a zero means the
player **did not play**. The model reads availability first — empty slot, bye,
injury designation — and only falls through to a statistical base rate for
healthy players.

Base rates are measured, not guessed: 40,217 starter-slots from the vault's
`fantasy.db`, banded by position and projection, then scaled by a single
league calibration factor because Dynasty Dons' generous scoring makes zeroes
rarer here than in the wider sample.

Reality check, from the same data: **2.7% of starter-slots goose, about 3.6 per
week league-wide, and only 27% of team-weeks have any goose at all.** The
average owner chugs roughly once every 3.7 weeks. If the model ever predicts
double-digit gooses for a week, it is broken — `tests/test_goose.py` is what
proves it isn't.

## Rules that are settings, not code

`points_per_chug`, `weekly_stipend`, `curse_stacking`, `max_curses_per_target`,
`max_chugs_per_week`, `landed_curse_mints_point`, `survived_curse_mints_token`,
`curses_sealed_until_lock`, `safe_pour_allowed`, `wrath_enabled`,
`wrath_multiplier`, `wrath_persists`. All live in `app_meta` and are editable
from Admin. The league will want to tune these in week 4; nobody should be
redeploying to do it.

`wrath_persists` is the one worth a sentence. Off (the default) the mark lives
one week exactly like a blessing and re-arms on each new miss. On, it stays
until the owner survives a curse — which, with `max_curses_per_target` at 1,
can leave a marked owner nobody bothers to target stuck wearing it. Rows keep
whatever rule they were written under; flipping the setting does not rewrite a
mark somebody is already carrying.

## Inherited lessons

Carried over from `../faab-platform-maintenance.md` — each cost a debugging
session over there:

1. The Sleeper `league_id` changes every season, chained via `previous_league_id`.
2. Snapshot anything a decision is graded against, at the moment of the decision.
3. Kickoff times come from `/scores/...`, not `/schedule/...` — that path 404s.
4. Never build a fail-safe that renders nothing; say *why* a section is empty.
5. Verify against a real database and real data, not by inspection.
6. Compute points from the league's real `scoring_settings`, never stock PPR.
#   G o o s e E g g 
 
 #   G o o s e E g g 
 
 