# Changelog

## [0.8.0] — 2026-09-09

Goosifer's Wrath, and a real reward for surviving.

Until now a curse was a coin flip with no memory: you missed, you drank one,
and Monday it was over. Two rules give the week after a miss some weight.

### Added
- **Goosifer's Wrath.** A curse that LANDS marks its target. The following
  week, a curse that lands on a marked owner costs them `wrath_multiplier`
  chugs (default 2) and mints them **no tokens at all** — the punishment is not
  the extra beer, it is drinking twice and coming away with nothing to curse
  anybody back with. New `wraths` table, shaped deliberately like `blessings`
  so the two mirror each other everywhere: earned in week N, read in week N+1,
  gone after.

  Two rules keep it honest, both in `resolve_curses`:
  - the mark is read with `earned_week < week`, so the miss that arms a mark
    can never also be doubled by it. You get the week in between to fix it.
  - it never stacks past the multiplier. Landing on a marked owner consumes the
    mark and arms exactly one fresh one, so two bad weeks costs double and five
    bad weeks still costs double.

  A blocked curse changes nothing — the blessing ate it, so nothing was proved
  and nothing was missed, and a mark survives being blocked.

- **Surviving a curse now pays.** The blessing alone expires unused most weeks,
  and beating the frozen bar with Goothulu on you is the hardest thing an owner
  does all week. Surviving now also mints a curse token (`survived_curse_mints_token`,
  on by default) and burns off any mark you were carrying. It is minted at
  settle rather than on a confirmed chug, because there is no chug to confirm —
  the one exception to "you earn tokens by drinking".

- **The mark is visible on every screen**, which is the entire point of it:
  a marked owner is the best target on the board and everybody should be able
  to see that at a glance on a phone.
  - **Curse** — marked rows sort to the top, wear the hottest row wash on the
    board with a flame left edge, carry a ribbon spelling out the multiplier,
    and get a pulsing sigil in the Marks column. The status tile swaps the
    blessing token for a wrath tile when you are the one marked (the two states
    are mutually exclusive by construction, so there was no reason to show two
    tiles one of which is always dark).
  - **My Geese** — a banner saying what it costs and when it lifts, plus a pill.
  - **Standings** — a badge on marked owners and a "n wraths paid" count.
  - **Goose Watch** — a badge beside the owner on every row, wrapped in a
    try/except: Watch must render on a Sunday whatever the database is doing.
  - **Admin → Curses** — the full mark list with lift buttons, and Mark / Lift
    controls. An admin mark arms from *next* week, the same offset a landed
    curse produces; marking somebody mid-week would grade a week they are
    already playing against a rule that was not in force at kickoff.

- `_macros.html` gained `sigil()` — a horned flame, drawn in SVG rather than
  shipped as artwork, so it recolours per theme (ember on Goothulu, blaze on
  Duck Blind, white-hot on Goosifer) and there is one definition of the mark
  rather than four that can drift. Deliberately not a goose: Goosiah and
  Goothulu are the two birds, and at 22px a third mascot competes with them.

### Changed
- `chugs` gained `mints_tokens` and `wrath_id`. The multiplier raises REAL chug
  rows rather than a count on one row, so the weekly cap, the Goose Crown and
  the admin confirm list all see them without a special case — a doubled curse
  eats two of `max_chugs_per_week` and rolls forward like anything else.
  `mints_tokens` defaults TRUE, so every chug already in the table keeps
  earning exactly what it earned before; `confirm_chug` treats NULL as TRUE for
  the same reason.
- `reset_week` and `reset_season` unwind marks: ones armed by the week are
  deleted, ones the week consumed or lifted go back to active — keyed off the
  week's curses through `wraths.resolved_by`, the same way a consumed blessing
  already was. `curse_survived` joined the token sources a reset claws back.
- Demo mode seeds one marked owner, so the badge has somewhere to appear.

### Settings
`wrath_enabled` (on), `wrath_multiplier` (2), `wrath_persists` (off) and
`survived_curse_mints_token` (on), all on the Admin → Rules screen.
`wrath_persists` on keeps the mark until the owner survives a curse instead of
ageing out after a week — worth knowing that with `max_curses_per_target` at 1,
a marked owner nobody targets can wear it indefinitely. Rows keep the rule they
were written under; flipping the setting does not rewrite a mark somebody is
already carrying.

Turning `wrath_enabled` off returns the game to 0.7.0 exactly, and there is a
test that proves it — including that a hand-planted mark is ignored while the
rule is off.

### Tests
`test_week_engine.py` gained five scenarios: arming and doubling (with the
two-weeks-running case walked end to end, including confirming both doubled
chugs and checking nothing was minted), ageing out versus being lifted by
surviving, the persistence setting over four quiet weeks, the kill switch, and
a week reset that has to put a consumed mark back. `test_templates.py` gained a
third board row that is marked (the marked and blessed branches are mutually
exclusive, so one row can only ever compile one of them), a marked `::locked`
status tile, three admin mark shapes including the NULL-expiry one, and
`roster_id` on the Goose Watch fixtures — which the strict-Undefined render
caught immediately, exactly as designed.

Still SQLite, not Postgres. `db_init.py` is additive and safe to re-run, and
must be re-run on deploy or the `wraths` table will not exist — the 0.5.0
lesson, which shipped a migration without running it and made a live feature
look like it had never been built.

## [0.7.0] — 2026-09-07

Navigation, the crown, and where a theme is chosen.

### Changed
- **The theme picker moved to the top ribbon**, where it belongs: a theme
  applies to every screen, so choosing one from the bottom of a single screen
  was the wrong place. It is a `<details>` disclosure on the swatch in the
  header — no JS to open, closes on an outside tap, and each option previews
  its own theme rather than the active one. `POST /me/theme` now returns to
  the screen it was set from (a local path only — `next` arrives from a form
  field, and a form field is user input).
- **Board is now Curse**, tab and heading both, because casting is what owners
  open that screen to do; "board" described the layout, not the job. The route
  is still `board` — renaming it would have churned every `url_for` in the app
  to rename one label. The tab wears Goothulu's face instead of an icon.
- **Watch leads the tab bar.** On a Sunday it is the only screen anybody wants
  and it was sitting second.

### Fixed
- **An injury designation no longer replaces the risk tier** on My Geese or on
  Goose Watch. `OUT` in place of `COOKED` threw away the comparison the whole
  model exists to make, and made an unavailable player look like a different
  kind of problem from a badly projected one when they land in the same tier.
  The tier is the verdict; the reason now sits under it as the cause.

### Added
- **Goothulu's crown.** The Goose Crown banner on Standings is now the artwork
  wearing a drawn gold crown, with a pulsing halo and tentacle arcs bleeding
  off the edge behind the count — all CSS and SVG, so it recolours with the
  theme and costs no request.
- **A podium.** Second and third place wear the same honour at stepped-down
  intensity: the row wash, the rank colour and the mark (filled crown, outline
  crown, chevron) all step down together, so the top three read as a ranking
  rather than three separately highlighted rows. Everything derives from the
  active theme's own gold, so Duck Blind gets brass and Goosifer gets ember.
- **The crown on the Curse tab**, as a compact strip above the most-cursed
  banner — the crown is what everyone is chasing, so it belongs next to the
  button that moves it.
- The crown is **vacant until somebody has chugged** (previously the sort
  handed it to whoever came first at 0–0–0 in the preseason) and **says when
  it is tied**.
- `templates/_macros.html` for the shared crown, so the two screens that wear
  it cannot drift into two subtly different crowns.

### Tests
`test_templates.py` gained a four-row standings case (one row could never
compile the 2nd/3rd place flair), a `standings.html::vacant` case for the
preseason branch, and `crown` on both board cases — 16 cases, each rendered
under all five themes.

## [0.6.0] — 2026-09-07

Five owner-selectable colour themes, wired all the way in — not just a design
canvas. Goothulu (the original), Goosiah (the one light theme — parchment,
gold leaf, royal blue and purple), Goosifer (fire and brimstone — cold ash at
the safe end of the risk ramp, white-hot at the bad end), Maverick (a night
carrier deck, tier ramp lifted off an instrument panel), and Duck Blind (olive
drab and blaze orange).

### What changed
- `themes.py` — the five themes as data: base chrome tokens (bg/surf/border/
  text/brand/curse/blessing/ink) plus a five-step risk-tier ramp per theme,
  carried over unchanged from the design canvas's colour solver (already
  validated for contrast, lightness-monotonicity, badge separation, and
  simulated colour blindness — see the design canvas for the full case).
- `owners.theme` — new additive column (migration in `db_init.py`), default
  `'goothulu'` so every existing owner keeps today's look until they pick
  something else.
- `base.html` — the old single hardcoded `:root` palette is now five
  `:root[data-theme="..."]` blocks, generated from `themes.py` via a Jinja
  loop so the CSS and the Python data can never drift apart. Every other
  hardcoded colour across `board.html`, `watch.html`, `standings.html`,
  `admin.html` and `my_geese.html` — curse/blessing gradients, danger
  banners, the team-rating dot — now derives from the active theme's tokens,
  most via `color-mix()` rather than a hand-picked constant per theme.
- `/me/theme` — new route. My Geese has a five-card picker at the bottom,
  each card previewing that theme's own colours (not the active theme's) so
  it doubles as a live sample.
- A theme choice is entirely per-owner and client-side-invisible to everyone
  else — there is no shared "league theme."

### Why `owners.theme` couldn't 500 the page
`inject_globals` resolves the active theme by bracket-indexing the owner row
and falling back to Goothulu on any `KeyError` — the same defensive pattern
`test_resilience.py` already exists to enforce, so a database that hasn't
run this migration yet (or any future one) degrades to the default theme
instead of a 500. Extended `test_resilience.py`'s stale-schema case to cover
it, and `test_templates.py` now renders every template under all five themes,
not just the one it used to assume.

## [0.5.1] — 2026-09-06

Hotfix. v0.5.0 deployed green and Goose Watch returned a 500.

### What happened
v0.5.0 added columns (`lineup_slots.tier` and friends) and `render.yaml`'s
build command was only `pip install -r requirements.txt` — so Render deployed
the new code against a database still on the v0.4 schema. Goose Watch is the
one screen that names those columns in a SELECT, so it was the one that broke.

The 500 itself came from a second, worse bug. The route CAUGHT the database
error and set its "feed problem" banner, exactly as designed — but left the
connection in Postgres's aborted-transaction state, where every later statement
returns `current transaction is aborted, commands ignored until end of
transaction block`. `inject_globals` then ran that state's next query while
rendering the handled version of the page, and by then the route had returned
and could no longer catch anything. A correctly handled error became a 500.

**Every local suite passed, and structurally could not have caught it: SQLite
has no aborted-transaction state.** This is the FAAB lesson again in a new
costume — a mocked test proves the logic and never the environment.

### Fixed
- `db.rollback()` in every handler that catches around a call which touches the
  database: the Goose Watch route, both `project_week` previews, the demo
  install in `get_db`, and `/poll`'s player sync (where the missing rollback
  also meant a failed sync silently took the lock and settle jobs down with
  it — the opposite of the "individually guarded" the docstring claimed).
- `inject_globals` now swallows its own failures. It runs during template
  rendering, after the route has returned, so anything it raises is past the
  last place that could catch it. A badge that reads zero beats a page nobody
  can open.

### Changed
- **`render.yaml` now runs `python db_init.py` on every deploy.** A migration
  that has to be remembered is a migration that will be forgotten. Safe because
  `db_init.py` is idempotent by construction, and now verified so against a
  real Postgres 16: `TABLES` + `MIGRATIONS` apply cleanly, re-run as a no-op,
  and never touch a commissioner setting that already exists. It runs in the
  BUILD so a failure fails the deploy loudly rather than crash-looping gunicorn.
- Goose Watch's failure copy no longer blames Sleeper specifically — the error
  it catches may equally be the database, and it said "Could not reach Sleeper"
  while the real problem was a missing column.

### Added
- `tests/test_resilience.py`. Models Postgres transaction semantics on top of
  SQLite (`AbortingDB`: once a statement raises, everything raises until a
  rollback) and replays the exact production failure — v0.5 code against a
  v0.4 database. With both fixes reverted it reproduces the live symptom
  precisely: Goose Watch 500, every other screen fine. It also asserts that
  `db_init.MIGRATIONS` stays additive, which matters much more now that the
  list runs unattended on every deploy.

### If you are deploying this by hand
Run `python db_init.py`. It is additive and safe to re-run.

## [0.5.0] — 2026-09-06

The chug-odds price is gone. Risk is a tier now, the board is visible before
kickoff, and there is a demo mode for showing the thing off in September.

### Changed
- **Goose risk is a TIER, not a percentage.** The old model was honest and
  useless to look at: its strongest signal is availability, so the board was a
  wall of 2%s occasionally interrupted by a 90%, and what it was really
  displaying was the injury report. Conner's read — "it's tied to injury risk,
  which doesn't make sense" — was exactly right. The rework splits the two
  things that were tangled:
  - **Availability is a label.** `OUT`, `ON BYE`, `DOUBTFUL`, `EMPTY SLOT`,
    `PROJECTED ZERO` say so in as many words and drop straight to the worst
    tier. Nobody needs a probability to understand "he isn't playing."
  - **Quality is a ratio.** Where does this player's projection fall against
    the average starter projection at his position, this week, in this league?
    A 9-point projection is a fine week for a tight end and a disaster for a
    quarterback; the raw number was never comparable across a lineup.

  Five tiers — `SAFE` / `SOLID` / `SHAKY` / `GOOSE BAIT` / `COOKED` — with cut
  points **measured, not guessed**, in `analysis/fit_tiers.py` over 35,144
  starter-slots (22 leagues, 2021-2025). P(goose) runs 1.1% → 11.3% across the
  ramp, monotonic at every position, and it holds in Dynasty Dons on its own
  3,925 slots (0.9% → 10.8%). The old raw-projection bands gave roughly 2x of
  usable spread once availability was stripped out; this gives 10x.
- **Teams get a rating from the mix**, not a price: `CLEAN` / `STEADY` /
  `EXPOSED` / `GOOSE BAIT`, from the mean tier weight per starting slot
  (normalised, so a superflex lineup does not look permanently more dangerous
  than a standard one). Measured P(at least one goose): 11% / 16% / 30% / 43%.
  Band names were chosen so the MEDIAN lineup does not read as an emergency —
  the average team scores 1.38, which is `EXPOSED`, which really is a one in
  three week.
- **The chug-odds column is gone from the Board**, and the space it took is now
  a curse column: tap Goothulu in any row to spend a token on that owner. The
  old price was a number nobody could act on sitting where an action belonged.
- **Goose Watch sorts by game time remaining**, ascending, within each group —
  closest to settled at the top, anything that has not kicked off at the
  bottom. Sorting by risk put a 9pm kickoff above a player with two minutes
  left, which is backwards. `POS-TEAM` is now just the team; the slot column
  already says the position. "Cleared" moved above "Yet to play" so the
  not-yet-started rows really are last.

### Added
- **A live preview before the week locks.** The projection maths came out of
  `lock_week` into a shared `project_week()`; `lock_week` now persists what it
  returns and the Board and My Geese render it directly. Before this the board
  was blank until Sunday kickoff — not because anything was broken, but because
  the numbers did not exist until the moment they were frozen. Preview screens
  say so in a banner: these move, the frozen ones do not.
- **Admin unlock.** A locked week can go back to `open` so people can still
  cast. It does **not** hand anything a fresh number: every `threshold_proj`
  already stamped stays put, and a curse cast during the unlocked window
  inherits that same kickoff figure (`cast_curse` stamps it on the spot; the
  re-lock only freezes curses whose threshold is still NULL). Cast early or
  cast late, the bar is the same — otherwise cursing late would be strictly
  better, which is the whole failure the frozen threshold exists to prevent.
  Refuses on a settled week; that is Reset's job.
- **Admin reset.** `Reset week` unwinds a week as if it had never been played;
  `Reset season` clears the board entirely. Both behind a typed confirmation
  (`RESET WEEK 3`) rather than a browser dialog — one distracted click should
  not cost a season, and a `confirm()` also blocks automation dead. Curses are
  rewound to `cast` rather than deleted and their tokens stay spent: the
  economy spans weeks, so handing tokens back for a week you are only
  re-testing would quietly inflate everyone's balance. Owners, PINs and rules
  survive both.
- **Demo mode.** A fake but realistic mid-Sunday week 1 — early games final,
  the afternoon slate in the fourth quarter, the late slate still to come.
  Real players, real lineups, real projections; only the clock and the points
  are invented, and the gooses are hand-placed so every row state is on screen
  at once. It works by replacing what Sleeper says (one seam, three functions
  in `sleeper.py`), so every screen and the whole engine run over it
  completely unmodified — which is the only way a demo proves anything about
  the real thing. The one exception is a handful of curses, tokens and
  blessings, which have to be real rows to render; every one is flagged
  `is_demo` and deleted on the way out.
  **`/poll` refuses to lock, settle or advance while demo mode is on** — four
  of those games say FINAL, and auto-settle against them would raise real
  chugs and mint real tokens off invented scores.
- **The real Goothulu and Goosiah artwork**, from the design package, replacing
  the placeholder SVG eggs: curse tokens on the Board, My Geese and Standings,
  in their held / sealed / landed / spent states, and a glowing shield for an
  active blessing (which stops animating under `prefers-reduced-motion`).
- **Player headshots in the My Geese lineup**, same Sleeper CDN path as Goose
  Watch.
- **A "Most cursed by Goothulu" banner** on the Board and Standings — season
  leader in curses absorbed, hidden below two so a single week-1 curse does not
  crown anybody.
- **`filters.py`.** The Jinja filters moved out of `app.py` so
  `tests/test_templates.py` imports the real ones instead of keeping its own
  copies. They had already drifted once.
- **`tests/test_demo.py`.** Stubs Sleeper at `_get_json` — the one function
  that touches the network — and runs everything above it for real: the demo
  build, the seam, tiers, `lock_week`'s snapshot, Goose Watch's sorting, and a
  full Flask render of every screen with the demo installed. `test_templates.py`
  now renders with `StrictUndefined`, which is what caught a real mismatch
  between the preview and snapshot row shapes on My Geese.

### Schema
Additive only, via `db_init.MIGRATIONS`, which runs on every init:
`lineup_slots.tier`, `.proj_ratio`, `.risk_reason`; `team_weeks.risk_tier`,
`.risk_score`, `.at_risk`; `weeks.unlocked_at`, `.unlocked_by`; and `is_demo`
on `curses`, `curse_tokens`, `blessings`, `chugs`. Run `python db_init.py`
after deploying — nothing drops, nothing changes type.

### Still outstanding
- The `/poll` pinger is still not set up on Render.
- Discord webhook still deferred, per Conner.
- Nothing here has run against live Sleeper: no egress to `api.sleeper.app`
  from either shell this was built in. The payload shapes in
  `tests/test_demo.py` come from the field names recorded in `sleeper.py`,
  which were read off a real response — but **the first live demo build is
  still the test that closes that gap.** Do it before showing anyone.

## [0.4.0] — 2026-09-06

Goose Watch tightened after the first real click-through on Render.

### Changed
- **"Danger" now means the 4th quarter, not just "live."** A live zero in
  Q1-Q3 is `pending`, same bucket as a player who hasn't kicked off. There is
  a full quarter-plus of offense left at that point; flagging it as danger
  just trains people to ignore the danger section. `sleeper.game_state_by_team`
  now also returns the raw `quarter` number per team so `watch._classify` can
  gate on it directly instead of re-parsing the clock string.
  `DANGER_FROM_QUARTER = 4` in `watch.py` if this ever needs tuning, and it
  also covers overtime (Sleeper keeps incrementing past 4 rather than
  resetting, so this is a `>=`, not `==`).
- Every player row on Goose Watch (Goosed, Danger, Pending, Cleared) now
  renders a small headshot next to the name, from Sleeper's CDN
  (`sleepercdn.com/content/nfl/players/thumb/{player_id}.jpg` — the same
  undocumented-but-stable path the Sleeper app itself uses). A player with no
  photo on file 404s quietly; the `onerror` handler just hides the broken
  image rather than showing a placeholder icon.

### Fixed
- Names showing as "Player 4046" instead of "Patrick Mahomes" on a fresh
  deploy was not a code bug — `players_cache` is only populated by
  `players_sync.sync()`, which only runs from the `/poll` route, which only
  runs when something pings it on a schedule. A brand-new deploy with no
  pinger set up yet has an empty cache. Hitting `/poll?secret=...` once by
  hand fills it immediately; an UptimeRobot-style pinger (same as the FAAB
  app) keeps it fresh going forward.

### Still outstanding
- The `/poll` pinger itself is not set up yet on the live Render deploy —
  without it, `players_cache` never refreshes, weeks never auto-lock/settle,
  and the Discord recap (once built) never fires. This is an operational step
  on Render, not a code change.
- Discord webhook still deferred, per Conner.

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
