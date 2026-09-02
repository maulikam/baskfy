# DECISIONS-SW — judgement calls of the swing run

Same convention as `docs/DECISIONS-MERGE.md` and `docs/smallcase/DECISIONS-SC.md`: numbered by
module; context, the choice, the rejected alternatives and why, the reversal path. Decisions
made without Maulik's review are tagged **⚠ UNREVIEWED** until he clears them.

Six decisions were pre-taken in the pack so the run does not stall on them:

## PACK.1 — The parabolic short is detected, never traded · (not reversible this run)

NSE cash equities cannot be shorted for delivery; MIS squares off at 15:20 and F&O covers ~180
names, and both products sit behind `INTRADAY_ENABLED` / `OPTIONS_ENABLED`, which default off
(non-negotiable 5). `TRADEABLE_SETUPS` excludes `PARABOLIC_SHORT`; SW10 proves no plan line can
carry it. Rejected: an F&O short leg (a new product, a new risk model, and he tells beginners to
skip the setup anyway). Reversal: a written decision to enable `OPTIONS_ENABLED`, then a new
module — not an edit to this run.

## PACK.2 — Two entry modes, EOD trigger and live ORH, both in this run · ⚠ UNREVIEWED

Maulik chose both (2 Sep 2026). The EOD mode (a `BUY_ON_TRIGGER` line at the pivot, sent as a
LIMIT buy the next morning on confirm, stop at the prior day's low) needs no open-hour process
and is the fallback whenever the monitor is off; the live mode (5-minute ORH break, stop at the
range low) is the method as taught. They share the plan, the sizing and the stops. Rejected:
EOD only (loses the ORH's selectivity), live only (a missed morning = no trades). Reversal:
disable the monitor flag; the EOD mode stands alone.

## PACK.3 — The swing GTT band is 0.5–10%, not the desk's 8–12% · ⚠ UNREVIEWED

The desk's `StopBand(0.08, 0.12)` encodes vol-scaled stops for a weekly book. A swing stop at the
low of the day is typically 2–6% away and is *supposed* to be tight. `packages/execution.gtt`
already injects the band (it is "journalled, not refused"), so the desk's `/swing/execute`
passes `StopBand(min_pct=0.005, max_pct=0.10)`; the sizing refuses anything wider than 10%
before a GTT is ever built. Rejected: widening the desk's default (would silently change the
weekly book's findings). Reversal: one constant in the desk's swing route.

## PACK.4 — The swing sleeve has its own exposure ladder, separate from R1–R4 · ⚠ UNREVIEWED

The desk's R1–R4 overlay decides the weekly momentum book's equity share from index/breadth
signals. His progressive exposure is a different thing — it reads the trader's own results. Both
exist; neither imports the other (`test_regime_names_do_not_collide.py` is the precedent, and
SW10 adds the swing edge). The sleeve is a `MY_STRATEGY` capital portfolio in the M34/
PORTFOLIO_REDESIGN sense, with `sleeve_capital_inr` as its cash. Rejected: feeding the swing
book through R1–R4 (would put swing entries under a weekly cap they have nothing to do with).
Reversal: a `regime_cap` input on `build_entries`, as `sleeves.py` already accepts.

## PACK.5 — Only the risk knobs are settings; the pattern thresholds are code · ⚠ UNREVIEWED

`sw_config` carries sleeve capital, risk per trade, max position %, max positions, the
opening-range window, the stop mode and the three liquidity floors. Base geometry, EP gap,
ladder tiers etc. stay `baskfy_core.swing.config` defaults. Reason: a threshold that can be
changed in a form gets changed after a bad week, which is the failure mode the method exists
to prevent; a code change leaves a diff and a DECISIONS entry. Rejected: a full "strategy
editor". Reversal: promote a field to `sw_config` with a migration and a ceiling.

## PACK.6 — The ladder reads simulated trades until execution is enabled · ⚠ UNREVIEWED

Before the real-money flag flips there are no real closes, and a ladder that reads nothing
never moves — which would leave the paper period unable to exercise SW8. So `exposure_tier`
reads simulated closes while `BASKFY_SWING_EXECUTION_ENABLED=false`, and real closes only once
it is true (the paper rung is reset to 0 at the flip; the journal page keeps both cards).
Rejected: mixing them (a paper streak would size real money). Reversal: a query filter.

---

(Module entries follow, newest at the bottom.)

## SW0.1 — The pack is committed with two documentation corrections in it · ⚠ UNREVIEWED

**Context.** SW0 is "read the pack and record where the run stands", and it is the commit that
puts `docs/swing/` into the repository. Reading the pack against the code found two places where
the document was incomplete rather than wrong, and both were fixed in the same reading:

* `04` §6.1 described the default stop as "low of the day" without naming the `stop_mode` value
  `LOW_OF_DAY`. The engine has the value; the numerical contract did not mention it, so SW1's
  docs-parity test — which asserts that every string the engine can write is named in `04` —
  failed on it. The document now names both modes.
* `03` §2 and §3 gave `sw_setup_daily` and `sw_market_daily` primary keys with no `user_id`,
  which contradicts `02` Track C §6. See SW2.1; the fix is a note in `03` pointing at it.
* `03` §1 said the audit was `updated_at`/`updated_by`, "as `settings_audit` does for the desk".
  See SW2.2; `03` gains §1b.

**The choice.** Commit the pack *as adopted* — corrections included — rather than committing it
verbatim and then correcting it in SW1 and SW2. The rejected alternative (three commits to reach
a document that is right) buys a tidier history and costs a reader two commits in which the
specification says something the run already knew to be incomplete.

**Reversal.** `git show` the SW0 commit; the three edits are the only non-additive hunks in it.

## SW0.2 — The dev database cannot feed a detector, and SW0 says so rather than fixing it

**Context.** The detectors need 125 sessions of bars (`flag.lookback_bars + base_max_bars`). The
local dev database holds **ten**, for 180 instruments, and no `factor_daily` rows at all. Several
acceptance criteria in SW3–SW5 are phrased "on the dev stack".

**The choice.** Record it loudly in STATUS as a fact about the machine, and let each module
decide what to do about it when it gets there, rather than starting the run with a backfill.
A backfill is an evening of Kite calls (D5, `docs/07` §4b), it is not what SW0 is for, and the
modules that need bars can build their own fixtures — which they need anyway, because a test
that depends on whatever the developer's database happens to contain is not a test.

**Rejected.** Running `make backfill` first: hours of network before a line of the run's own code
exists, and it would still not make an acceptance criterion reproducible on another machine.

**Reversal.** None needed; it is an observation. If a real-data check becomes necessary, the
backfill command is in `RUN-AND-TEST.md` §2.


## SW1.1 — The pack's tests assert shapes; SW1 adds a second layer that asserts numbers · ⚠ UNREVIEWED

**Context.** SW1's Goal is "the pack's pre-built `baskfy_core.swing` is the run's foundation,
proven in the repo's own harness, not the author's", and one of its criteria is to add the
package to `make mutants` "at the same threshold as `factors`". The first run answered
**41.0%** overall and **19.1%** for `setups.py`, against `factors`' 77.5%.

Reading all 271 survivors, essentially every one was in one of two families:

* **a comparison boundary** — `>=` mutated to `>`, `<=` to `<`. `04` is written in `≥` and `≤`,
  and the 103 tests the pack shipped never place a fixture *on* a threshold, so nothing could
  tell the two readings apart;
* **a term of a scoring formula** — the 30/25/20/15/10 weights of §2.6, the "full marks at twice
  the threshold" divisors, the `1 − x` inversions. The tests asserted `0 < score <= 100`, which
  every possible formula satisfies.

Neither is a defect in the pack's tests. "A textbook flag is detected; a base with no pole is
not" is exactly what a detector test should say. They are simply blind to arithmetic, and the
arithmetic is what decides what gets bought and at what size.

**The choice.** Add two test modules rather than edit the seven that exist —
`test_swing_contract_detectors.py` (§1–§4), `test_swing_contract_book.py` (§5–§10) and
`test_swing_contract_edges.py` (the measurements, the defaults and the guards) — and leave every
shipped test untouched. Each new test asserts `04` directly, by one of two
techniques:

1. **Put the threshold on the measurement.** Detect the fixture once with the shipped
   configuration, read the value the engine measured, re-run with the threshold set to *exactly*
   that value, and require the row to still be found; one step further and it must not be. That
   is a boundary test without a fixture that has to land on a boundary by luck.
2. **Recompute the formula from the document**, in the test, from the row's own inputs — not
   against a stored golden. A golden would be regenerated the first time a fixture moved, and a
   regenerated golden asserts nothing.

**Rejected.** (a) *Recording 41% and moving on.* It is the honest number, but "the foundation is
proven" would then be a claim the evidence does not support, and every later module builds on
this arithmetic. (b) *Rewriting the pack's tests to be stricter.* They are good tests of a
different thing; replacing them would lose the shape coverage to gain the numeric coverage.
(c) *Lowering the bar because `setups.py` is "only" a screener.* It chooses what a person buys.

**Also changed, and why.** `tools/mutation.py` gained a per-target test selection. It previously
ran one list against every mutant; a swing mutant scored against the factor suites survives
every time, because those suites never import the package — a 41% that was, in part, measuring
nothing. `generate()` now records a target's path *inside* the package (`swing/setups.py`) rather
than its basename, which the old code would have written to `baskfy_core/setups.py`.

**The result.** 41.0% → 67.8% (the first two modules) → **83.4%** (with a third,
`test_swing_contract_edges.py`, that recomputes the detectors' *measurements* from the raw
fixture bars and pins the defaults, guards and `frozen=True`). That is above `factors`' 77.5%,
which is the comparison the criterion asks for. 27 of the 76 remaining survivors are
`slots=True` on a dataclass decorator — the equivalent mutant `reconciliation/MUTANTS.md`
already justifies for `factors`.

**Reversal.** Delete the three modules and the `SWING_*` entries in `tools/mutation.py`; nothing
else imports them.

## SW2.1 — `sw_setup_daily` and `sw_market_daily` are keyed by `user_id` first · ⚠ UNREVIEWED

**Context.** `03` §2 gives `sw_setup_daily` the primary key `(date, instrument_id, setup)` and
§3 gives `sw_market_daily` the key `(date)`. `02` Track C §6 says "Every `sw_` row carries
`user_id`, as P4.1 requires, so the day D3 is answered nothing needs a migration", and SW2's own
acceptance criterion repeats it. The two documents disagree about two tables.

**The choice.** `user_id` leads the primary key of both. Under the charter's precedence order the
law of the run (`02`) outranks the data model's sketch (`03`), and the substance agrees with it:
both tables are computed **through the liquidity floors in `sw_config`**, which are per-user
settings, so a row of either was already a statement about one user's universe rather than about
the market. A detector row computed at a ₹5 cr turnover floor and one computed at ₹50 cr are
different rows about the same day.

**Rejected.** (a) Keeping `03`'s keys and adding `user_id` as an ordinary column — the uniqueness
would then be wrong the moment a second user existed, which is precisely the migration Track C §6
exists to avoid. (b) Treating both tables as global and dropping `user_id` — it makes the
liquidity floors a lie.

**Reversal.** A migration that drops `user_id` from the two keys; no code reads the key shape
directly. `03` §2 and §3 now carry a note pointing here.

## SW2.2 — The settings audit is a table, not two columns · ⚠ UNREVIEWED

**Context.** `03` §1 gives `sw_config` `updated_at` and `updated_by` and calls them "audit, as
`settings_audit` does for the desk". SW2's criterion says "`settings_audit` on write". The desk's
own module says why it has one: "You cannot reconstruct why a trade was sized the way it was
without knowing what the parameters were at the time." Two columns cannot answer that — they say
who touched the row last, not what `risk_per_trade_pct` **was** on the morning of a trade.

**The choice.** A twelfth table, `sw_config_audit`, shaped exactly like `desk.settings_audit`
(`key`, `old_value`, `new_value`, `changed_at`, `changed_by`, `note`), one row per changed field,
written in the same transaction as the change. `03` gains §1b.

**Rejected.** (a) Writing into `desk.settings_audit` — it lives in the desk's schema in the same
Postgres, and the screener's API writing into the desk's evidence table crosses a boundary M19
drew on purpose. (b) A JSONB before/after blob on `sw_config` — it makes "show me every change to
`risk_per_trade_pct`" a JSON path expression instead of an equality, which is the query this
exists for. (c) Living with the two columns — it satisfies the letter of `03` and none of its
stated purpose.

**Reversal.** Drop the table and the two functions in `baskfy_api.swing_settings` that write it;
`sw_config` keeps `updated_at`/`updated_by` either way.

## SW2.3 — A setting above its ceiling is a new problem type, answered 422 · ⚠ UNREVIEWED

**Context.** SW2's criterion: "a `risk_per_trade_pct` above the ceiling is a **422** with the
ceiling named". This API answers **400** `invalid-screen-definition` for every schema violation
and reserves 422 for `no-trading-day` (`app.py`'s own docstring: "a client that cannot tell 'your
JSON is wrong' from 'that date is outside the range we hold'").

**The choice.** A new `ProblemType.SETTING_ABOVE_CEILING` at 422, carrying `field`, `requested`,
`ceiling` and `env_var`. A ceiling refusal is genuinely not a schema violation: the payload is
well formed and the number is one a person can legitimately want. It is the M4.1 boundary saying
no, and the caller needs the ceiling back so the form can render "max 1.0% — set by the server"
rather than "invalid". The precedent for adding a type `docs/07` does not list is
`INTERNAL_ERROR`, which is documented on the enum itself.

**Rejected.** (a) 400 `invalid-screen-definition` — the type name would be a lie on a settings
route, and the caller would get a field path where it needs a limit. (b) Reusing
`NO_TRADING_DAY` because it already owns 422 — worse than either.

**Note.** `app._TYPE_FOR_STATUS` still maps 422 to `no-trading-day`. That map only translates
`HTTPException`s the *framework* raises before a route runs, and nothing there can raise a
ceiling refusal, so it is left alone rather than made ambiguous.

**Reversal.** Delete the enum member, its status/title entries and the factory; the route then
raises whatever it is told to instead.


## SW3.1 — The sector strip is computed by the swing job, not read from `market_health_daily` · ⚠ UNREVIEWED

**Context.** `04` §2.6 gives a candidate `+5` for "a sector in the top-3 breadth strip", and `05`
§2 says the strip is "the top-5 sectors by `pct_above_20dma` from `market_health_daily`". But
`market_health_daily` is written by `compute_market_health` for exactly the twelve
`MARKET_HEALTH_SLUGS` — the *size* universes (NIFTY 50, MIDCAP 150, …). **No sector index has a
breadth row, and none ever will under that step**, so the strip as specified has nothing to read.

**The choice.** `baskfy.swing.detect` computes sector breadth itself, over the liquid universe it
already has in memory: for each sector index the candidate's instrument belongs to *on that date*,
the share of that index's liquid members closing above their own 20-day average. The strip goes
into `sw_market_daily.detail.sectors` and SW4's page reads it from there.

Two reasons beyond "the other option is empty". The number is over **this book's** universe — the
names the detectors could actually have picked, not every constituent including the illiquid ones
a swing trader cannot get out of. And it costs nothing: the frame, the 20-day averages and the
liquidity predicate are all already computed for the detectors.

**Which indices are sectors** is `SECTOR_INDEX_SLUGS` in `baskfy_core.universes` — an explicit
list of NSE's fifteen sectoral indices, not a pattern, because `nifty-midcap-150` and `nifty-bank`
are both "nifty-<word>" and only one of them is a sector. A name in two sector indices (a bank is
in both `nifty-bank` and `nifty-financial-services`) takes the **narrower** one, measured by that
day's membership count.

**Rejected.** (a) Extending `compute_market_health` to cover sector indices — it would put a
screener step's twelve rows up to ~27 for a number only the swing book reads, and its breadth is
over index membership rather than over the liquid universe. (b) Dropping the `+5` — it is one of
the two adjustments `04` §2.6 names, and "young stock in a hot theme" is his stated preference.

**Reversal.** Delete `sector_breadth` and the `detail.sectors` payload; the `+5` then never fires
and every candidate scores as if its sector were cold. `05` §2 should be edited to match whichever
way this settles.

## SW3.2 — `upper_circuit` is converted into the adjusted space on the way in · ⚠ UNREVIEWED

**Context.** `ohlcv_daily.open/high/low/close` are adjusted **in place** (`raw x adj_factor`;
`decile-blueprint/docs/DECISIONS.md` §21.10 notes there is no raw counterpart for O/H/L).
`upper_circuit` has no adjusted twin either, and it is the exchange's own band — a raw price. The
lock test in `04` §3.5 is `high >= upper_circuit`, which compares the two directly.

On any day with `adj_factor = 1` they agree. On the morning after a 1:2 split they do not: the
stored high is half the exchange print and the band is not, so an unconverted comparison would
find **no** name locked when the factor is below 1, and every name locked when it is above.

**The choice.** `load_swing_bars` multiplies the band by the row's own `adj_factor` on the way in,
so the comparison happens entirely in the adjusted space. Levels then come back out of the
detector adjusted and are divided by the same factor before storage, which is `03` §9's rule.

**Rejected.** (a) Storing `upper_circuit_raw` alongside an adjusted copy — a schema change to
`ohlcv_daily`, which the swing run has no business making. (b) Comparing in the raw space by
dividing `high` — the detector's frame is adjusted throughout and a single raw column in it is
the kind of mixed-units bug this whole convention exists to prevent.

**Reversal.** One expression in `load_swing_bars`.

## SW3.3 — SW3's "on the dev stack" criterion is met against written bars, not found ones · ⚠ UNREVIEWED

**Context.** SW3's last acceptance criterion is "a `sw_setup_daily` row for a real date exists on
the dev stack". The dev database on this machine holds **ten sessions** of bars for 180
instruments and no `factor_daily` rows at all (SW0, DECISIONS-SW SW0.2); the flag detector needs
125 sessions. No detector run against it can produce a row, whatever the code does.

**The choice.** Meet the criterion's *Goal* — "the job really writes a row, against a real
database, with every stored number checked" — by writing the bars the assertion needs.
`test_swing_detect.py` inserts a 140-session textbook flag into the test database and asserts the
stored row end to end: the status, the levels, the rounding, the adjustment factor, the score
adjustments, the funnel counts and the market row.

That is a **better** test than the criterion's literal wording, not a weaker one: it is
reproducible on any machine, it fails for one reason, and it checks the numbers rather than
checking that a row exists.

**Rejected.** (a) Running `make backfill` first — an evening of Kite calls (D5) before the run's
own code has been exercised once, and the result would still not be reproducible elsewhere.
(b) Marking SW3 blocked — the module's work is done and the blocker is a machine's data.

**What is still not proven.** Nothing has run the detectors over a real NSE day. The first time
that happens is Maulik's first `make swing DATE=…` against a backfilled database, and it is in
`SW-FINAL-REPORT.md`'s first-morning steps.


## SW4.1 — `PATCH /swing/config` is the one mutation on the hub, and it is whitelisted by path · ⚠ UNREVIEWED

**Context.** `02` Track A says the web hub is "read-only ... every mutation on it is a 405 except
watchlist edits, notes and the catalyst field (they change no money)". `05` §2 also puts a
settings form on `/me`. Track C §4 says the app "gets no route under `/swing` that can reach the
gateway". A settings write is a mutation, so the two sentences have to be reconciled explicitly
rather than by reading.

**The choice.** One `PATCH`, on `/swing/config`, and the read-only tests **whitelist it by path**
rather than by pattern — because `/swing/execute` matches any pattern that admits
`/swing/setups`, and `/swing/execute` is precisely the route Track C §4 exists to keep out of
this application. Three properties make the write defensible, and each is asserted:

1. it writes seven numbers into `sw_config` and touches nothing else;
2. `SwingConfigPatch` uses `extra="forbid"` and has **no field** for `exposure_level` or
   `first_live_sessions_left`, so a caller who asks to climb the ladder is *told* the field does
   not exist rather than answered `200` and left believing they had;
3. every accepted value is checked against a ceiling that is not reachable from any form.

**Rejected.** (a) Putting the settings write on a different prefix to keep `/swing` literally
GET-only — it would hide the surface from the very test that is supposed to watch it. (b) Making
the hub fully read-only and moving settings to the desk console — the ceilings are already
server-side, and a person cannot set their own risk from a page they do not open.

**Reversal.** Delete the route and the entry in `DELIBERATE_MUTATING_ROUTES` / `ALLOWED_PATHS`;
both tests then enforce a GET-only surface.

## SW4.2 — The pages say "allocation", not "book" or "sleeve" · ⚠ UNREVIEWED

**Context.** `docs/swing` calls the money a **sleeve** and the positions a **book**, and so does
this schema (`sleeve_capital_inr`). The web app's own vocabulary guard
(`apps/web/src/lib/__tests__/no-jargon.test.ts`, PORTFOLIO_REDESIGN §8) bans both words in
user-visible strings: "book" → "portfolio group", "sleeve" → "allocation".

**The choice.** The pages say **allocation**. The schema, the docs and the Python keep "sleeve"
and "book", which are the trader's own words and the right ones in a specification.

The guard is a product decision about what a *reader* is shown, made after research that found
those words did not land, and widening it for one hub would make the vocabulary a matter of which
page you are on. The charter's tie-break is "names that keep their meaning" — and "allocation" is
the same thing under a name the rest of the product already uses.

**Rejected.** Adding "sleeve" and "book" to the scanner's ALLOWED list. That is the "never widen
the pattern to silence a hit" failure the namespace rule warns about in `CLAUDE.md`.

**Reversal.** Two strings per page.

## SW4.3 — SW4 ships without a Playwright check and without a p95 measurement · ⚠ UNREVIEWED

**Context.** SW4's criteria include "a Playwright check renders the page with one flag and one
locked EP and shows the lock icon" and "`GET /swing/setups` p95 < 300 ms on the dev stack with
2,500 instruments".

**The choice.** Both are deferred, and STATUS says so loudly rather than the module claiming them.

The browser check needs a seeded database with a detected flag *and* a locked EP, which needs the
125 sessions of bars this machine does not have (SW0.2, SW3.3). The e2e suite builds and starts
the app against the dev database; a check that cannot produce its fixture would either be skipped
(a test that never runs) or seeded by the test itself, which is a second seeding path for the
browser suite alone.

The p95 budget cannot be measured against 180 instruments and ten sessions. Measuring it there
would produce a number that flatters the query by two orders of magnitude, which is worse than no
number: `benchmarks/AS-MEASURED.md` is the record of what was actually measured.

**What was done instead.** The surface is covered by `test_api_swing.py` (the contract, against a
real database) and by the two read-only tests. The query is a single indexed read on
`(user_id, date)` with one join to `instrument`; the index exists (`ix_sw_setup_daily_date_setup_score`).

**Rejected.** Writing a Playwright check that skips when the data is absent — a test that has
never run is not evidence, and it reads on the status page as coverage.

**Reversal / what closes it.** SW11 owns the budget table, and both items are on its list; the
browser check wants either a backfilled dev database or a fixture seeder of its own.


## SW5.1 — `WatchConfig` is a new group in `baskfy_core.swing.config`, and `04` gains §9.5 · ⚠ UNREVIEWED

**Context.** SW5's criterion names two numbers the pack had not written down: "detector rows with
`SETTING_UP` and **score ≥ 60**" and, from `03` §4, "flags expire after **10 sessions** without a
trigger". The kickoff is explicit that "every threshold is a field of `baskfy_core.swing.config`,
never a literal in a detector, task, router or page", and `04`'s own preamble says every number in
it is such a field.

**The choice.** A `WatchConfig` group with `auto_watch_min_score` [60] and `flag_valid_bars` [10],
and a new `04` §9.5 that states the whole rule — including that a `MANUAL` row never expires and
that expiry is a state change rather than a delete. SW1's docs-parity test then holds both to the
document, which is how it was caught: a literal `60` in the task would have passed every test in
the run.

`ep.valid_bars` [3] already existed and is reused rather than duplicated.

**Rejected.** (a) Constants in `swing_watch.py` — they are thresholds of the method, and the run's
own rule is that those live in one place. (b) `sw_config` fields — PACK.5 keeps pattern thresholds
in code so that changing one leaves a diff and a decision, and "which flags are worth watching" is
a pattern threshold.

**Reversal.** Delete the group and inline the two numbers; `04` §9.5 then has to go with them, and
the docs-parity test makes that impossible to forget.

## SW5.2 — Every swing response model is prefixed `Swing` · (not really a judgement call, but it cost a build)

`PlanOut` in `routers/swing.py` collided with `PlanOut` in `baskfy_api.schemas` — the billing
plan. OpenAPI names a schema by its Python class name, so the generator renamed **both** to
`baskfy_api__routers__swing__PlanOut` and `baskfy_api__schemas__PlanOut`, and every existing
`PlanOut` reference in `packages/api-client/src/client.ts` stopped resolving. The web build caught
it; nothing else would have.

Every response model in the router now carries the prefix. Recorded because the failure mode is
invisible in Python — both classes are valid, both routes work, and the break lands in a different
package.

## SW5.3 — The web read-only test bans the GTT *verbs*, not the word · ⚠ UNREVIEWED

**Context.** SW4's web-side read-only test forbade the substring `gtt` anywhere under
`lib/swing` or the pages. SW5's positions page has to show whether a position has a resting stop —
`sw_position.gtt_id` — because an unprotected position is the one state `04` §6 forbids and the
page leads with it.

**The choice.** Narrow the ban to what *does* something: `place_gtt`, `delete_gtt`, `/gtt`,
`/execute`, `place_order`, `OrderGateway`, `kiteconnect`. And add a second test asserting the id
is only ever **read** — no assignment, no request body containing it — so the narrowing cannot
quietly become a licence to manage triggers from the web app.

This is a narrowing of an over-broad pattern, not the "widen the pattern to silence a hit" failure
`CLAUDE.md` warns about: displaying that a stop exists is the opposite of arming one, and the
alternative was a page that could not warn about the thing it most needs to warn about.

**Rejected.** Renaming the field on the wire (`stop_armed_id`) to dodge the substring — the schema
word would then differ from the database word for the benefit of a regex.

**Reversal.** Restore the single-word ban and drop `gtt_id` from `SwingPositionOut`; the page then
cannot tell a protected position from an unprotected one.
