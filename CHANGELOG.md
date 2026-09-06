# Changelog

## [0.3.0] — 2026-09-06

Goose Watch — the live Sunday board.

### Added
- `watch.py` — classifies every starter in the league into one of four states
  and rolls them up per owner. `sleeper.game_state_by_team()` and
  `sleeper._clock()` turn the `/scores` feed into per-team game state.
- `/watch` route and `watch.html`: a "drinking tonight" strip of avatars with
  counts, then Goosed (final), Still on zero (live), Yet to play, and Cleared.
- The Watch tab, restoring the five-tab bar from the design canvas.
- `tests/test_watch.py`.

### The distinction the whole screen rests on
Four states, not two:

    goosed   game FINAL, finished on <= 0. Settled.
    danger   game LIVE, on <= 0 right now. Still escapable.
    pending  not kicked off. A zero here means nothing yet.
    safe     points on the board.

**A team absent from the scores feed is `pending`, never `final`.** Byes are
absences, and reading absence as "his game is over" would announce on Thursday
that half the league is drinking. There is a test named in capitals about this.

`watch.py` never writes. It settles nothing, raises no chug, resolves no curse
— the week is still graded from final scores by `settle_week`. If the two ever
disagree, `settle_week` is right.

### Verified against the real endpoint, not assumed
Field names (`metadata.is_over`, `is_in_progress`, `quarter`, `quarter_num`,
`time_remaining`, `home_team`/`away_team`, and `start_time` in **milliseconds**)
were read off an actual `GET /scores/nfl/regular/2025/1` response before any of
this was written. That is the FAAB app's `/schedule` lesson applied in advance
rather than after a day of debugging.

### Fixed during the build
- The clock stripped a leading zero by testing for a `"00:"` prefix, which
  never matched — the leading zero is on the minutes digit, so `08:42` rendered
  as `Q3 08:42`. Caught by the test, not by reading it.

### Still outstanding
- **Not yet run against a real Postgres.** Unchanged and still the top item.
- The Discord recap poster — waiting on the webhook URL, by decision.
- Hosting decision still deferred.

## [0.2.0] — 2026-09-06

The app itself: week lifecycle, curses, blessings, chugs, and every screen.

### Added
- `week_engine.py` — the whole state machine. `open_week` (admin gate + optional
  stipend), `lock_week` (snapshots every lineup and freezes each curse against
  its target's projection), `settle_week` (gooses, then curses, then the chug
  cap, then blessing expiry — in that order, so a curse chug is capped like a
  goose chug), plus `cast_curse`, `cancel_curse`, `confirm_chug`,
  `unconfirm_chug`.
- `app.py` — PIN login, Board, My Geese, Standings, Admin (chugs / week /
  curses / rules), and `/poll` for the external pinger. Every poll job is
  separately guarded so one failure cannot stop the others.
- `players_sync.py` — throttled player-directory cache. `injury_status` is the
  single strongest input to the model, so a stale cache prices an OUT player
  like a healthy one.
- `set_pins.py` — PINs and admin grants. **This script is the only thing that
  makes someone an admin.**
- Templates in the canvas palette: warm gold for Goosiah and safety, ember for
  Goothulu, on `#0E0B07`. Geometry and type lifted from the FAAB app unchanged.
- `tests/test_week_engine.py` — drives open → curse → lock → settle → confirm
  and checks gooses, blessing blocking, blessing expiry, the chug cap rollover,
  empty slots, re-settle idempotency and the open-week gate.
- `tests/test_templates.py` — compiles and renders every template with the
  app's real filters.
- `run_tests.sh`.

### Design decisions worth remembering
- **A curse token is minted when a chug is CONFIRMED, not when it is owed.**
  You earn the curse by drinking, not by being in debt.
- **The chug cap rolls, it does not forgive.** Overflow moves to next week and
  keeps its `rolled_from_week` origin.
- **Blessings are state, never an action.** They fire automatically and expire;
  there is no "spend blessing" button anywhere.
- **Sealed curses fail closed.** If the setting can't be read, targets stay
  hidden — a leaked target cannot be un-leaked.

### Fixed during the build
- Two statements were Postgres-only in ways that would have made them
  untestable: an alias on an `UPDATE` target, and a bare `OFFSET` with no
  `LIMIT`. Both rewritten to run identically on either engine, so the engine
  tests execute the SQL that actually ships rather than a paraphrase of it.

### Known simplifications
- `standings()` issues about seven queries per owner. Fine for twelve people;
  fold into one aggregate if it ever feels slow.
- `lock_week` loads the whole player cache into memory. It runs once a week.

### Still outstanding
- **Not yet run against a real Postgres.** The engine tests use SQLite with a
  schema derived mechanically from `db_init.TABLES`, which cannot drift — but
  SQLite is the more forgiving engine and this is not a substitute. Run
  `db_init.py` and one real week against a throwaway Postgres before deploying.
  This is the same class of gap that let the FAAB app ship a 404ing URL.
- Goose Watch (the live Sunday tracker) is designed but not built.
- The Discord recap poster is blocked on a webhook URL.
- Hosting decision still deferred.

## [0.1.0] — 2026-09-06

Scaffold. Data layer, Sleeper client and goose engine, with tests.

### Added
- `goose.py` — goose detection (`<= 0`, empty slots included) and the
  probability model. Base rates measured from 40,217 starter-slots in the
  vault's `fantasy.db`, banded by position and projection.
- `sleeper.py` — league, users, rosters, matchups, projections computed against
  the league's real scoring settings, kickoff/lock clock and bye-week detection
  off the `/scores` feed, and the player directory with `injury_status`.
- `db.py`, `db_init.py`, `settings.py` — Postgres helper, idempotent schema
  (10 tables) and the commissioner's eight tunable rules.
- `tests/test_goose.py` — unit tests plus a calibration backtest against
  Dynasty Dons 2024 and 2025.

### Fixed during the build
- **Unknown players were massively over-charged.** A starter with no prior
  history was being given the worst projection band (21% for a WR), on the
  reasoning that an unknown player is a bad one. Real rate for those slots is
  1.9% — they are mostly rookies somebody drafted on purpose. 269 such slots
  were predicting 56 gooses against 5 real ones. Now uses the position's
  population rate. Found by the calibration backtest, not by reading the code.
- **The model over-predicted by 72%** even after that (6.69 against 3.89 per
  week). Root cause was importing cross-league base rates unscaled: Dynasty
  Dons has the most generous scoring in the sample, so zeroes are genuinely
  rarer here. Added a single fitted `LEAGUE_CALIBRATION` factor (0.762) rather
  than refitting the whole table on too-thin data. Now predicts 3.93 against
  3.89 actual; mean team chug odds 28.2% against a real 28.9% team-week rate.
- **The zero-projection backstop was firing on backward averages.** It only
  makes sense for a forward projection — Sleeper projecting 0.0 means it thinks
  the player will not play, whereas an average of 0.0 just means he has been
  quiet. Added `projection_is_forecast`, which backtests set to False.
- QB's 8–12 base rate measured *above* its 4–8 rate on a 338-sample wobble,
  which would have priced a better quarterback worse. Clamped monotonic.

### Not built yet
Flask app, templates, admin screens, the weekly lock/settle jobs, the Discord
recap poster (blocked on a webhook URL).
