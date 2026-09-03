# DECISIONS-SW — judgement calls of the swing run

Same convention as `docs/DECISIONS-MERGE.md` and `docs/smallcase/DECISIONS-SC.md`: numbered by
module; context, the choice, the rejected alternatives and why, the reversal path. Decisions
made without Maulik's review are tagged **⚠ UNREVIEWED** until he clears them.

Six decisions were pre-taken in the pack so the run does not stall on them (and three more at SW9.5, below them):

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

Three more were taken at SW9.5, when `07-primary-source-corrections.md` re-read the rules from
his own words:

## PACK.7 — The sleeve locks out new entries 15% below its peak, until it is back within 10% · ⚠ UNREVIEWED

**Context.** He publishes no portfolio-level "down X% and stop" rule; what he says is "I try to
contain them at 15-20%, which happen a few times per year" (`07`). The first draft had no
portfolio-level throttle at all: the ladder read results and the tape, and a run of small losses
across many names could carry on indefinitely at rung 0's two positions.

**Choice.** A drawdown breaker with hysteresis, in the ladder itself: `MarketConfig.max_drawdown_pct`
[15] locks, `resume_drawdown_pct` [10] releases; `market.drawdown_locked(drawdown_pct, was_locked)`
is the state machine and `exposure_tier` applies it before the gate and the results (rung 0,
`new_entries_allowed = false`, `drawdown_locked = true`); the plan answers every name
`DRAWDOWN_LOCKOUT`. The bottom of his range (15%) is the lock, because the breaker's job is to
contain the drawdown before it becomes the 20% he calls the top of it; the 10% release is the
distance a locked sleeve — which can only recover through the positions it already holds — must
climb back before it is allowed to add risk again, and the 5-point gap is what stops a sleeve
at 14.9% from trading and stopping every other evening. Exits are managed as always; only new
entries stop. The measurement is the sleeve's own EOD NAV against its own peak (SW9.5.1), never
the whole account. The backtest applies the same function to its own equity curve.

**Rejected.** A single threshold (flaps around it). 20% (the top of his range, and a fifth of the
sleeve gone before anything reacts). A per-day loss cap (nothing he describes; the desk's
`RISK_MAX_DAILY_LOSS_PCT` is the weekly book's and a different instrument). Reading the whole
account's NAV (Track C §5: the sleeve never sizes against the whole account, and it should not
be stopped by the weekly book's drawdown either).

**Reversal.** Two numbers in `MarketConfig`; `max_drawdown_pct = 100` disables it.

## PACK.8 — The swing GTT's limit rests 3% under its trigger · ⚠ UNREVIEWED

**Context.** "I always use market stops, never limit stops." A Zerodha GTT fires a LIMIT order,
and the gateway rests that limit at `GTT_LIMIT_FRACTION` (0.995) of the trigger — a half-percent
cushion sized for the weekly book's vol-scaled 8–12% stops. A swing stop is one ADR or tighter,
often 2–5%, on a name that moves 5%+ a day; a book falling fast can walk through half a percent
between the trigger and the limit, and the stop rests unfilled while the position keeps falling
— the one failure the method forbids.

**Choice.** `place_gtt_stop` gains `limit_fraction: float | None = None` (additive; `None` is the
constant, so every existing caller and test is byte-for-byte unchanged, and the weekly book's
GTT tests still pin 88.55 on an 89 trigger). The swing route passes
`C.SWING_GTT_LIMIT_FRACTION` — env `BASKFY_SWING_GTT_LIMIT_FRACTION`, default **0.97** — on every
GTT it arms (a buy's, a partial's re-arm, a raised stop's, a re-arm), so the limit rests 3%
under the trigger and fills on the way down the way a market stop would. 3% is a little over
half a typical swing ADR: wide enough that a 5%-ADR name gapping through its stop still meets
the limit, narrow enough that the worst fill a resting stop can take is bounded rather than
"wherever the market is". The gateway refuses a fraction outside (0, 1] as a caller bug (a
sell limit above its trigger cannot fill on the way down) before any layer runs; the dry-run
journal records the fraction so the paper sessions rehearse it.

**Rejected.** A market-order GTT leg (Kite's GTT API places LIMIT legs; there is no market leg
to ask for). Changing `GTT_LIMIT_FRACTION` itself (would change the weekly book's resting
stops — a live, order-placing system — from a swing-book decision). A cushion in ADRs per name
(the gateway holds no ADR; a per-name number would be a second implementation of the stop,
which is the thing the gateway exists not to be).

**Reversal.** One env variable; `1.0` rests the limit on the trigger, `0.995` is the weekly
book's.

## PACK.9 — The 1.0% risk ceiling is kept, though he risked up to 1.5% with a small account · ⚠ UNREVIEWED

**Context.** `07` quotes three ranges: "usually 0.25-1%. I rarely risk more than 1%", "most of
the time 0.3-0.5%", and — when the account was small — "0.5-1.5%". The ceiling
`BASKFY_SWING_RISK_PER_TRADE_PCT_MAX` is 1.0 and the default setting 0.5 (MD2).

**Choice.** Kept at 1.0. The 1.5% is what he did with an account he could afford to blow up
several times over (he did, and says so); this sleeve is one person's money at the end of a
merge that exists so that it is not blown up, and `02` §3 gates the first real sessions at half
risk. The ceiling is the top of the range he *recommends*, not the top of what he once did.
His 30% position ceiling and 15–20 position count, which are recommendations, were adopted in
the same pass.

**Rejected.** 1.5 (his small-account number; a ceiling a setting can reach is a number the
sleeve will one day trade at). 0.5 (would make the default the ceiling and MD2 moot).

**Reversal.** One env variable, and a settings write.

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

## SW6.1 — The pre-open pace counts minutes from 09:00, never zero · ⚠ UNREVIEWED

**Context.** `04` §7.3 pro-rates an average day's volume by `minutes_elapsed / 375` and the Beat
entry runs at 09:09, when the *session* has not opened (09:15). Zero minutes makes `live_gap`
answer "not a candidate" for every name — for the wrong reason — and the doc does not say what
the clock is measured from.

**The choice.** `minutes_elapsed = max(1, minutes since 09:00 IST)`: the pre-open order
collection window is when the matched quantity Kite reports as `volume` starts accumulating, and
that is the number the pace is a pace *of*. `PREOPEN_START` is a module constant in
`tasks/swing_premarket.py`, like `SESSION_MINUTES` in core — a fact about the exchange, not a
threshold (`02`: thresholds are config fields; the pre-open's start is not something to tune).

**What is not yet known.** Whether Kite's `volume` at 09:09 carries the pre-open matched quantity
or reads 0 until 09:15. The first flagged morning answers that (`STATUS.md` → NEEDS-MAULIK); if
it reads 0 the scan finds nothing and says so in its report, and the fix is a second Beat entry at
09:16 — one line in `celery_app.py`.

**Rejected.** Measuring from 09:15 with a floor of 1 — the volume then reads as 375× pace at 09:16
and every gapper qualifies. Measuring from midnight — the pace becomes meaningless.

**Reversal.** Change the constant; the tests name the arithmetic (nine minutes of 375) in their
docstring so the expected values move with it.

## SW6.2 — A live gap is watched with a trigger and no stop · ⚠ UNREVIEWED

**Context.** `06` SW6: the 09:09 scan writes "new `sw_watch` rows (setup EP, source `DETECTOR`,
catalyst empty)". A watch row carries `trigger` and `stop_ref`; for a live gap, the stop is "the
range low or the low of the day" (`04` §7.2) and at 09:09 there is no range and no day.

**The choice.** `trigger` = the indicative price (what the monitor's break is measured against
until the range replaces it), `stop_ref` = NULL. The morning plan then skips the name — `06`
SW5's `watch_items` drops rows without a stop, because a plan line has to size against one — and
the `SIGNAL` plan the monitor builds when the range breaks carries the verdict's own entry and
stop. The name is therefore on the desk page as *watched*, never as a line the plan invented a
stop for.

**Rejected.** `stop_ref = last × (1 − max_stop_distance_pct)` so the morning plan can size it —
a stop nobody's rule produced, sitting in a plan a person can confirm.

**Reversal.** Compute a provisional stop in `watch_live_gaps`; one line, and one fewer honest NULL.

## SW6.3 — "Desk notification" is the row and a log line · ⚠ UNREVIEWED

**Context.** `06` SW6: "`TRIGGERED` → `sw_signal` row + a one-line `sw_plan(source=SIGNAL)` +
desk notification." The desk has no notification channel of any kind — no Telegram, no push, no
mail from the desk process; its alerts are pages a person opens.

**The choice.** The notification is the `sw_signal` row the desk page (SW7) polls plus an INFO log
line from the monitor process. Building a channel is a product decision (which one, to whose
phone, paid or not) and belongs to Maulik — appended to `NEEDS-MAULIK.md` under Swing.

**Rejected.** Re-using the API's mailer from the desk — the desk process does not import
`baskfy_api`, and an email at 09:31 is not a notification anyone acts on at 09:31.

**Reversal.** `PgSignalStore.raise_signal` is the one call site; a channel is a second line in it.

## SW6.4 — The monitor is built with no gateway at all · (a stronger reading of the AC)

`06` SW6 says the strategy "implements `BaseStrategy`" and `BaseStrategy` takes a gateway. The
strategy keeps that signature — it is the desk's plugin shape, and the same runner could host it
— but `app.swing_monitor.main` hands it `gateway=None`. A process that holds no gateway cannot be
talked into using one, which is stronger than "holds one and never calls it". The test asserts
both: the source never names `self.gw` or a placing verb, and `main` passes `None`.

## SW7.1 — A stop is armed for the quantity that filled; a live order that has not filled arms nothing · ⚠ UNREVIEWED

**Context.** `06` SW7: "`BUY_ON_TRIGGER` → `OrderGateway.place(...)` then `place_gtt_stop(...)`
in the same request." The gateway's `place` returns the moment the broker *accepts* an order
(`PLACED`), not when it fills; a LIMIT buy at the trigger can rest unfilled for the session, fill
in part, or fill later. Non-negotiable 4 says every buy gets its stop the same session; the desk's
own EXCESS finding (`protection.py`) says a trigger for more shares than are held sells what you
do not own when it fires. The two pull in opposite directions the moment an order is not filled
in the call that placed it. C1 fixed the shape and this entry records why.

**The choice.**

* **Simulated (the whole of this run):** the gateway's dry-run branch is the fill. The line
  fills whole at the trigger, now; `sw_position` + `sw_fill(simulated=true)` are written and the
  GTT for that quantity is placed in the same call with `last_price = entry`. The paper record
  therefore shows what the rules would have done at the price the rules named.
* **Live (flag on, `DRY_RUN=false` — not this run):** a `PLACED` order is `SENT`. No position,
  no fill, no GTT; the line carries the order id as `journal_ref`, the session counts a confirm
  and no fill. The stop is armed for the *filled* quantity by whatever reconciles the fill —
  which nothing does yet, and STATUS says so. The same holds for a market sell: `SENT`, the book
  unchanged, the resting GTT still covering the full open quantity until the fill is known.
* **A GTT that cannot be armed after a simulated fill** (the only way is a `DUPLICATE` id from
  a store that reused a plan) leaves the position with `gtt_id = NULL` — the NAKED state `03`
  §7 defines as an alert — and the outcome's reason says "re-arm". Never a phantom stop, never
  a silent success.
* **A simulated trigger id** (`DRY-<client_id>`, the gateway's own shape) is never handed to
  `delete_gtt`, which would rightly refuse a non-integer id and journal a block for a trigger
  that does not exist at any exchange. The cancel is recorded as `DRY_RUN_GTT_DELETE` locally;
  a real integer id goes through the gateway's guarded cancel.
* **One GTT id per (plan, symbol, kind)** — `plan:symbol:GTT` for the buy (C1's literal),
  `plan:symbol:SELL:GTT` and `plan:symbol:RAISE:GTT` for the two exit lines — because SW5's
  evening plan legitimately holds a partial *and* a breakeven raise for one name, and a shared
  id made the second `DUPLICATE` after the first had already cancelled the old trigger.
* **The swing book journals to its own file**, `swing_orders_journal.jsonl`, in the desk
  journal's directory: the weekly execution report and the options page read
  `orders_journal.jsonl` as the record of the weekly book, and twenty sessions of paper swing
  fills in it would make that record answer the wrong question.

**Rejected.** Arming the GTT for the *requested* quantity on `PLACED` — the EXCESS case by
construction whenever a limit order part-fills. Polling `kc.orders()` inside `execute_line` until
the fill arrives — a network wait inside a confirm click, against a client this module may not
name, for a path the run never takes. Simulating a live-mode fill at the trigger — a fill that did
not happen, written as `simulated=false`.

**Reversal.** A fill reconciler (SW11's territory, or the first live session's) that reads the
order book for lines in `SENT`, writes the position and fill, and calls `rearm_gtt` for the filled
quantity — every piece it needs is already a public function here. `_cancel`'s simulated branch is
four lines.

## SW7.2 — The first live sessions halve the order, and the countdown is per session, in-process · ⚠ UNREVIEWED

**Context.** `02` §3.5: "the first real session runs at **half** the configured risk
(`sw_config.first_live_sessions` counts down from 5 with `risk_multiplier=0.5`)". SW5's STATUS
noted the field had no core function and that "SW7 multiplies `risk_per_trade_pct` before
calling `size_position`" — but the desk does not size; the plan line arrives sized by the evening
job, and re-sizing it at confirm would need the sleeve's equity, cash and turnover at 09:31.

**The choice.** Half the *risk* is half the *quantity* for a fixed stop distance, so the line's
quantity is halved at confirm — `first_live_quantity`: round down, never below one share —
**only for a real order** (`simulated=False`). A simulated line is never halved: a paper record
at half size would rehearse a smaller book than the rules describe, and the twenty paper sessions
are meant to be a rehearsal of exactly these rules. The counter comes down by one on the first
real order of a session, not per order: `_FIRST_LIVE_COUNTED` (a set of IST session dates,
in-process, the same shape as the desk's `PLANS` dict) stops a second live buy on the same
morning from counting the session twice. The line's stored `quantity` is left as planned; the
journal line and, later, the fill carry the size sent.

**Rejected.** Re-sizing with `size_position` at confirm — the desk has none of the inputs and
would be a second sizer disagreeing with the first. Decrementing per order — five orders on the
first morning would end the half-size period in one session. Persisting the "counted today" fact
through the store — C1 has no read for it; the store's `set_first_live_sessions_left` can be
made idempotent per day from `sw_config_audit` (`changed_by = '/swing/execute'` dated today) by
1.1.2 or later without changing this module.

**Reversal.** `first_live_quantity` is one function and the countdown is six lines in `_buy`;
a `risk_multiplier` column would replace the halving with a multiply and nothing else moves.


## SW7.3 — The desk page: a sqlite twin of the schema in its tests, a monitor state it cannot observe, and the other page-level calls · ⚠ UNREVIEWED

**Context.** `05` §3 specifies what the page shows; it does not say how the desk's test suite
gets a swing book (the `sw_` tables are the screener's Postgres and the desk tests run on
`tmp_path` sqlite), what "monitor state" means to a page that cannot see the monitor process,
whether a `BUY_ON_TRIGGER` line that is *waiting* carries a Confirm, or where "the last five
manage actions" are read from. Each is decided here.

**1. The tests build the `sw_` tables in sqlite from their own DDL.** `tests/test_swing_desk.py`
carries a `CREATE TABLE` per table, mirroring `0028_swing.py` column for column (NUMERIC,
BOOLEAN and the CHECKs in sqlite's spelling), and `PgSwingStore` runs the same SQL against it
with `schema=""`. Rejected: a fake connection that records statements (SW6's `FakeConn`) — it
cannot prove a round-trip, and G4 is a round-trip; a Postgres test database — the desk suite is
sqlite-only by charter (C0: "Desk leaves use sqlite (`tmp_path`)") and a suite that needs a
server up is a suite that is skipped. The cost is honest: sqlite's NUMERIC affinity returns a
`float`, exactly as the desk's Postgres adapter does (`analytics.pg._native`), so the store's
`_dec` re-quantises every known column to its storage scale on the way out — `100.80` comes
back as `100.80`, not `100.8` — and a dialect the two do not share (`INSERT … AS alias … ON
CONFLICT`, `RETURNING id`, `true`/`false` literals) was checked on both before it was used.
One test asserts the schema prefix on every table name the store issues, so the
`search_path=desk` fact (`app.swing_monitor`) cannot regress silently. Reversal: point the
fixture at a Postgres URL and drop the DDL list; the store does not change.

**2. Monitor state is derived, not observed — and there is a fifth state.** The desk cannot
see the monitor process; it has the flag, the clock, and the mark the monitor leaves in
`sw_session.monitor_ran` at close. So: flag off → **not enabled**; `monitor_ran` → **stopped
at 10:45**; a weekend → **idle**; before 09:15 → **idle**; inside the window → **running
since 09:15** (assumed from the flag — the page says "since", not "alive"); after the window
with no mark → **did not run**, which `05` §3 does not list and which is the truthful answer
(SW11's `SWING_MONITOR_DID_NOT_START` is the same fact as an alert). Rejected: a heartbeat
row written by the monitor each minute — a new column the schema does not have and a write per
minute for a display; a process check — the page and the monitor need not share a box.
Reversal: `monitor_state` is one function; a heartbeat would replace its middle branch.

**3. A waiting `BUY_ON_TRIGGER` line carries a Confirm.** `05` §3 names Confirm for triggers
and for the two exit kinds, but PACK.2 chose *both* entry modes, and the EOD mode is "a
`BUY_ON_TRIGGER` line at the pivot, sent as a LIMIT buy the next morning on confirm" — with the
monitor flag off, the waiting buys are the only entries there are. The page labels it so.
Rejected: no button on waiting buys (the EOD mode would be unreachable). Reversal: one branch
in the `confirm_form` macro.

**4. A waiting buy for a name the book already holds has no button**, nor does an exit for a
name it does not hold, a SELL beyond `quantity_open`, or a RAISE not above the resting stop —
the page does not offer what `execute_line` would refuse (it re-checks all four). The
morning plan is built at 09:10 and a SIGNAL fill can land at 09:31 for the same name; the
09:10 line does not know. Reversal: `_line_view`'s `why_not` chain.

**5. "The last five manage actions" are the last five exit-kind plan lines** (`SELL_AT_OPEN`,
`RAISE_GTT_STOP`), most recently touched first, with the state each reached. Rejected: a
`sw_manage_action` table — nothing writes one; `sw_session.manage_actions` — a counter, not a
list. Reversal: one query in `recent_manage_actions`.

**6. The swing gateway shares the weekly book's `RiskManager`.** `build_swing_gateway(kc,
risk)` takes the risk manager from the caller; the page hands it `app.main`'s, built by
`main.gateway()`, so the daily-loss cap, the kill switch and the order counter are one
account's. Rejected: a second `RiskManager` — the swing book could spend an order budget the
weekly book had exhausted, and a kill switch that halts one book and not the other is not a
kill switch. Reversal: construct one in `swing_gateway()`.

**7. The route supplies the broker's last price, for exits and re-arms only.** `execute_line`
refuses a SELL or a RAISE without a `last_price` rather than inventing one (1.1.1). The route
reads it through `Kite.ltp` — a read, not an order; the page module still names no placing
verb — and passes `None` when there is no session, so the outcome says why. A BUY gets no
lookup: its entry is its trigger. Rejected: a `last_price` form field — a typed price is a
price nobody quoted. Reversal: `last_price()` is one function.

**8. A guard's refusal is `BLOCKED` with the guard's words, not a 500.** `execute_line` marks
the line `REJECTED` and re-raises `UntouchableInstrumentError`; the route reports it the way
`/execute` reports one per order. Reversal: remove the `except` and the browser shows a 500.

**9. The 5-second refresh reloads only on change.** `/swing/data` carries a fingerprint of the
signals, the lines' states, the positions and the counters; the page polls it every 5 s inside
09:15–10:45 and reloads when it moves — so an inline outcome is not wiped by an idle poll.
Outside the window: no polling; a page opened before 09:15 reloads itself once at 09:15.


## SW8.1 — The ladder settles in the evening job, after `manage`, from a settlement record; it reads the paper book until the flag flips · ⚠ UNREVIEWED

**Context.** `06` SW8: "`swing-eod` feeds the last 5 closes into `exposure_tier`; the rung is
written to `sw_config` and `sw_market_daily` and shown on every page." Two jobs could do the
feeding — the detection job already computes a tier at 21:00 in `write_market_row`, and the
evening job at 21:05 builds the plan — and `04` §8.4 leaves three things open: which book the
closes come from, what "the current rung" is when the job that writes it is the job that reads it,
and where the write-back sits relative to `stops.manage`.

**The choice.**

1. **The evening job settles the ladder, between `manage` and `build_entries`** (C2's wording,
   kept). `manage` produces tomorrow's exit lines and closes nothing itself — the desk closes,
   at the open, through `/swing/execute` — so by 21:05 every close of the day is on the book. The
   plan is then built with the settled tier, which is the property the acceptance criterion
   actually needs: `sw_plan.exposure_level == sw_config.exposure_level`. The detection job's tier
   is left in place as a preview and overwritten on the same row; on an ordinary evening the two
   are identical.
2. **The closes come from the simulated book while `BASKFY_SWING_EXECUTION_ENABLED` is false,
   the real one after** — PACK.6 restated, now with a date bound (`closed_on <= session`, house
   rule 5) so a repair of a past evening cannot read tomorrow's results. The market row's
   `detail.closed_trades_read` says which.
3. **The rung the ladder climbs from is a settlement record, not `sw_config`.** The job records
   `{from, to, settled_by: "swing-eod"}` on the day's row; the next evening starts from
   yesterday's `to`; a re-run of the same evening starts from its own `from`; `sw_config` is the
   fallback for the first evening ever (and a repair with no records). Idempotency (house rule 7)
   forced this: with `sw_config` as the base, running the same evening twice climbed 0 → 1 → 2
   with nothing else changed — the first draft did exactly that and the test caught it.
4. **The write goes through `swing_settings.record_system_change`** — the function whose name
   says a job is doing it — so the audit row (`changed_by = "swing-eod"`, old → new, a note with
   the gate and the closes) is the same shape as a person's settings change, and an unchanged
   rung leaves none.

**Rejected.**

* *Writing back from the detection job* (`write_market_row`). It re-runs — the Beat entry at
  21:00 after the chain's own step, the Saturday five-session re-scan, `make swing DATE=` — and
  every re-run would climb again from the rung it had just written. It also reads closes without
  a date bound.
* *Reading the previous row's `exposure_level` column as the base.* The detection job rewrites
  that column on a re-detect from the rung the evening had just written, one rung too high; the
  record it cannot see is the safer fact. This leaves a known gap the evening job cannot close
  from its own file: **`swing-premarket` reads the column** for the morning plan and the Market
  page shows it, so a Saturday re-scan after a GREEN Friday can hand Monday morning a tier one
  rung above `sw_config` for one session. `write_market_row` should keep a row's tier and
  record when `detail.ladder.settled_by == "swing-eod"` and bound its closes by date — three
  lines, in a file this leaf did not own (C2); SW11 touches it and is the natural owner.
* *Adding a "the ladder moves only on a new close" rule.* `04` §8.4 as written climbs one rung
  on every GREEN evening while the last five closes are net positive, whether or not a trade
  closed since the last move; the core is tested that way (SW1). Not the worker's rule to add.
* *Counting only `mode = 'DRY_RUN'` sessions for the 20-session gate.* The EOD email counts every
  row; two counters that could disagree are worse than one that is slightly generous after a
  flag flip that makes the count moot anyway.

**Reversal.** Each choice is one function: `rung_in_force` (the base), `closed_r_multiples`
(the book and the date bound), `settle_ladder` (the two writes), and their call site in
`run_swing_eod` is eleven lines between `manage_open_positions` and `watch_items`. Moving the
settlement into the detection job means calling `settle_ladder` from `write_market_row` with the
row it just upserted and deleting the call in the evening — the records make either job
idempotent.

## SW8.2 — The ladder sentence says what the next close does, at every rung; the empty journal counts sessions instead · ⚠ UNREVIEWED

**Context.** `05` §2 asks the journal page for "the current loss streak and what it means for
the ladder". The wire carries two numbers and a list — `stats.current_loss_streak`, the
`ladder` card (`level`, `gate`, `new_entries_allowed`) and `ladder.last_r`, the closes the
ladder read — and `04` §8.4 is four rules in precedence order (RED → rung 0 and no entries; a
three-loss streak → one rung down; five closes net positive in GREEN → one rung up; AMBER
holds). A page that printed the four rules would be a rule book; a page that printed the
streak alone would leave the reader to work out which rule bites tonight.

**The choice.**

1. **One sentence per state, in `04` §8.4's precedence order, phrased as what the next close
   does** (`journal/copy.ts::ladderSentence`). Rungs are said as people say them — `level` 0 is
   "rung 1 of 4", the convention the Market and Setups pages already use.

   | state | the sentence says |
   |---|---|
   | gate `UNKNOWN` (no market row) | the gate has not been measured, no entry is allowed, and no close moves the ladder until the detectors have run |
   | gate RED | the ladder goes to rung 1 of 4 and allows no new entries whatever the next close says; it climbs again only once the gate turns |
   | streak ≥ 3, rung > 1 | the ladder falls a rung **each evening** the streak stands (rung k becomes k−1 at the next settlement); another losing close keeps it falling; a close that is not a loss ends the streak and the rung follows the last five closes again |
   | streak ≥ 3, rung 1 | already at the bottom; another loss keeps it there; a non-loss ends the streak; the rung can rise only after five closes net positive in GREEN |
   | streak 2 | "one more losing close makes 3 and steps the ladder down from rung k to k−1" (at rung 1: "would call for a step down, but the ladder is already at rung 1"); then **otherwise** — the climb clause below |
   | streak 0–1 | "N losing closes in a row would step the ladder down from rung k" (or the rung-1 variant); then **otherwise** — the climb clause |
   | climb clause | fewer than five closes: "cannot climb until 5 trades have closed net positive in a GREEN tape (n so far)"; GREEN and net positive: "climbs to rung k+1 at the next settlement, and again each evening that holds" (at rung 4: "at the top"); GREEN and net ≤ 0: "rung k holds until they are net positive"; AMBER: "holds rung k with entries allowed at its size" |

   "Each evening" and "at the next settlement" are deliberate: SW8.1 settles the ladder every
   evening from the same closes, so a standing streak steps down again tomorrow with no new
   trade, and a standing net-positive five climbs again — the page says the rule as the job
   runs it, not as a reader might assume it ("once per trade").
2. **The streak in the sentence is counted from `ladder.last_r`**, the closes the ladder
   actually read, not from either card's `current_loss_streak`. The two agree whenever the
   whole-record streak is under the lookback (five); when it is longer, the sentence says "5
   losses in a row" and the card says "7 in a row" — both true, about different windows, and
   the card the ladder reads is named under the sentence. The net R in the climb clause is the
   sum of `last_r` rounded to two places, so a float residue cannot turn a Decimal zero into a
   climb.
3. **Three `MarketConfig` numbers are mirrored in the page** — four rungs, a five-close
   lookback, a three-loss step-down — as named constants with the field they mirror, the way
   the market page mirrors §8.3's two breadth thresholds. The API ships the rung and the closes,
   not the rule; C2 fixes the wire and this leaf does not own the router.
4. **The empty journal counts sessions.** With no closed trade the top sentence is "No
   simulated trade has closed yet — N of 20 paper sessions logged, and the ladder is reading an
   empty record"; each card still draws its six bars, empty, with a caption saying so; the
   groupings say "No closed trades yet" / "No month has a closed trade yet"; the ladder sentence
   is the streak-0 form with "(none so far)". A record with nothing in it is a fact about how
   far the paper period has got, and the session count is the number that measures it.
5. **The top sentence reads the record the ladder reads** (`ladder.reads`), simulated until the
   flag flips. Two verdicts would be two sentences, and the point of `Answer` is one.
6. **The backtest card's `stats` and `params` are rendered generically** — nested records opened
   two levels as `group · key`, series as a count, `sleeve` in a key read as `allocation` and
   `book` as `record` (SW4.2) — because C3 gives the shape to leaf 1.3.2. The heading is `02`
   §3.3's, verbatim: "Backtest, EOD approximation".

**Rejected.**

* *Printing the four rules under the streak.* A rule book, and the reader still has to pick the
  rule; the sentence picks it.
* *One sentence for every state* ("Streak N; the ladder moves on the next close"). Wrong at
  RED (nothing moves it), wrong at the bottom (nothing lowers it), and silent about the climb.
* *Counting the streak from the card's statistics.* The card is the whole record; the ladder
  reads five closes; a sentence about the ladder should be computed from the ladder's input.
* *Asking 1.2.1 for a `rule` field on the wire.* Right in the long run; not this leaf's file,
  and the mirror is three constants with tests at every branch.
* *A combined "all closes" card with a simulated/real column.* `04` §10 says separately, and a
  column is a step from a sum.

**Reversal.** `ladderSentence` is one function with a test per branch; the three constants are
three lines; the `Verdict` component picks the card in one expression; `flatten`/`label` are
the generic renderer and go when SW9 draws its own card.

## SW9.1 — The backtest sizes every trade against a constant sleeve and re-settles the ladder at every close · ⚠ UNREVIEWED

**Context.** `04` §11: "size by §5 on a constant ₹10 lakh sleeve with the ladder in force". Two
phrases need a number behind them before an engine can run: what *constant* means once the sleeve
has made or lost money, and *when* the ladder is read inside a run that has no evening job.

**The choice.**

1. **Constant means constant.** `size_position` is called with `equity = params.sleeve_inr` on
   every session of the run, whatever the curve says; realised P&L is added to the equity curve
   and never re-risked. The backtest is therefore an **R-machine**: each trade risks the same
   ₹5,000 (0.5% of ₹10 lakh), so a 2019 trade and a 2024 trade weigh the same in the expectancy
   and in the by-year table, which is the number `02` §3.3 wants on the page before real money.
   Compounding is one multiplication the reader can do with the expectancy and the trade count;
   a compounding curve would instead make the by-year rows incomparable and let a good 2020
   flatter 2022. `cash_available` for §5's cash cap is the sleeve less the **cost basis** of what
   is open (`entry paid x quantity`), and the tier's exposure ceiling is tested against the same
   cost basis — Track C's "exposure ≤ 100% of the sleeve" measured on what was spent, not on what
   the market says today.
2. **The ladder is re-settled at every session's close**, exactly as SW8.1 settles it every
   evening: `exposure_tier(current_level=yesterday's rung, closed_r_multiples=every close so far,
   gate=today's gate)`, and the rung it returns is tomorrow's. The consequence SW8's evening job
   also has, stated here because a backtest makes it visible over years: in a GREEN tape with
   the last five closes net positive the rung climbs **one per session** until the top; on a
   three-loss streak it falls one per session until a non-loss close breaks the streak. `04`
   §8.4 says "never skips a rung"; it does not say "once per trade", and the live job does not
   read it that way either. The result carries a `ladder` trace — `(session, gate, rung)` — so
   the page can show it and a reviewer can decide whether a rung-per-session is the ladder he
   meant. **Additive to contract C3** (`BacktestResult.ladder`, a `ladder` key in `to_json()`);
   the runner leaf stores the JSON as-is.
3. **The gate inside the backtest is breadth-only.** `market_gate(breadth, None, ...)`: the bars
   frame carries no index series and C3 fixes the signature. `04` §8.2 says a missing index is
   ignored, so RED is breadth ≤ 2% up 25%-in-a-month, GREEN is ≥ 5%, and the index can never make
   a backtest day worse. The live gate can; the backtest is therefore *slightly* more permissive
   than the desk on days the NIFTY 500 sat below both MAs while breadth held. `high_1y` for the
   new-highs count is the factor engine's window (`HIGH_1Y_BARS`, 252) with the worker's own
   fallback (the bar's high) for the first year of a series.

**Rejected.** A compounding sleeve (rejected for the reasons in 1); reading the ladder once per
closed trade (a different rule from the one SW8 ships); an optional index argument on
`run_backtest` (a signature the sibling leaf has already coded against — a follow-up, not a
surprise).

**Reversal.** `equity=` in `_Run._enter` is the one line for compounding; `_tier_for` is the one
call for the ladder's cadence; an `index: pl.DataFrame | None = None` keyword on `run_backtest`
for the gate.

## SW9.2 — One entry session per candidate, the EP is bought the day after its gap, and the size is the desk's plan at the fill · ⚠ UNREVIEWED

**Context.** `04` §11 gives the entry in one sentence — "enter at the next session's open if it
is ≥ trigger, else at trigger if the next session's high ≥ trigger" — and the watchlist rules
(§9.5) say an EP stays enterable for three sessions and a flag for ten. §5 sizes at a price; §11
does not say which.

**The choice.**

1. **Exactly the next session, for both setups.** A `SETTING_UP` flag that does not trigger is
   re-detected tomorrow if it still qualifies (the detector runs every close), so §9.5's ten
   sessions are implied for flags. A `GAP_DAY` EP is detected once, on its gap day, so §11 gives
   it one session and this backtest gives it one. That is **stricter than the watchlist** (three
   sessions) and is recorded as a place where the backtest under-counts EP entries the desk would
   have taken on day two or three.
2. **The EP is entered the session after its gap** — that is what "next session" means when the
   detector runs at the gap day's close — and the position is created with
   `is_ep_gap_day=False`: `04` §6.4.2 ("EP on its gap day with `close < open`") describes a
   position bought *on* the gap day by the live monitor, and a red close on the following day is
   not that rule. A test asserts a red entry day does not sell the EP.
3. **The size is `plan.build_entries` at the fill price.** The evening plan sizes at the trigger;
   the backtest knows the fill (the open, or the trigger) at the moment it enters, so it hands
   `build_entries` a `WatchItem` whose `trigger` *is* the fill and whose `stop_ref` is the prior
   day's low, and takes the line's quantity and trail. Every refusal the plan can make — tier
   full, size refused (including §5.1's `STOP_TOO_WIDE` when the open gaps more than 10% above
   the prior low), exposure full, cash not spent twice — is the plan's own code and appears in the
   funnel under its own key. Risk per trade is then exactly 0.5% of the sleeve on the price paid,
   which is what §5 is for; sizing at the trigger and filling higher would risk more than the
   budget on every gap-up entry.
4. **"Already held" is judged at the close the plan was built**, not after the morning's exits: a
   name sold at the open for a close below its trail is not re-bought the same morning, because
   the evening plan would have skipped it `ALREADY_HELD`. Two candidates for one symbol on one
   evening (a flag and an EP on the same name, or two instruments that share a symbol) enter
   once, best score first, then symbol, setup and instrument — a total order, so two runs agree
   byte for byte.
5. **`BREAKOUT_TODAY` is not entered.** §11 names `SETTING_UP` and `GAP_DAY`; a breakout day's
   trigger is its own high, and buying the day after the pivot broke is a different trade from
   the one the method describes.

**Rejected.** Three sessions for an EP (§9.5 is the watchlist's rule, §11 is the backtest's, and
the document wins); sizing at the trigger (over-risks every gap-up); re-implementing the plan's
skip order inside the backtest (it would drift from the desk's).

**Reversal.** An EP validity window is a loop over `ep.valid_bars` sessions in `_enter`; the
sizing price is the one `trigger=` argument in the `WatchItem`.

## SW9.3 — A stop-out fills at the stop, or at the open when the day gapped through it — except on the entry day · ⚠ UNREVIEWED

**Context.** `04` §6.4.1: `bar.low ≤ stop` → `STOPPED_OUT` ("the GTT fired, or should have").
A GTT is a trigger, not a price: a stock that opens below the stop fills at the open.

**The choice.** The fill is `min(stop, open)` on any day after the entry day. On the entry day
itself the fill is the stop: the entry (at the open, or at the trigger inside the day) happened
before the low did, so the open is not a price the position could have been sold at. When the
entry was at the trigger and the same day's low is at or below the prior day's low, the engine
assumes the trigger came first and the stop second — the pessimistic reading (a −1R loss is
counted) rather than the flattering one (the trade never happened).

**Rejected.** Filling every stop at the stop price (overstates every gap-down; the desk's own
GTTs do not fill at the trigger on a gap); skipping same-day stop-outs on trigger entries (a
backtest that quietly drops its worst fills).

**Reversal.** `_apply_actions`, four lines.

## SW9.4 — The journal's prices carry the costs, and two close reasons of the backtest's own · ⚠ UNREVIEWED

**Context.** `04` §11: "costs 0.13% per side". §10's `ClosedTrade` has an `entry`, an
`exit_avg` and an `initial_stop`, and no cost field; the live journal never sees a cost.

**The choice.**

1. **`BacktestTrade.entry` is the price paid (fill × 1.0013) and every exit fill is the price
   received (fill × 0.9987)**, each snapped to the four-decimal grid; `initial_stop` stays the
   prior day's low. `r_multiple` and `pnl_inr` are then §10's formulas unchanged — no forked
   arithmetic — and a full stop-out reads a little worse than −1R, which is the truth of a round
   trip. The rules (`manage`) read the **raw** fill, as the live book's `entry_avg` is the raw
   fill: a breakeven stop sits at the exchange price, not at price-plus-cost. A card that shows
   `entry 152.20` for a fill at 152.00 is showing the cost; the page copy should say so.
2. **Two close reasons the stop rules cannot produce.** `NO_BAR`: a held name that prints no bar
   for `baskfy_core.backtest.MISSING_BAR_TOLERANCE_DAYS` (5) sessions is sold at its last close —
   the screener engine's own delisting rule, reused so "delisted" means one thing in both
   backtests; `instrument.delisted_on` is the runner's to honour when it loads the bars.
   `END_OF_RUN`: a position still open on the last session is closed at that session's close (cost
   applied) so the trade list and the equity curve agree; the funnel counts both separately.
   They are `BacktestCloseReason`, distinct from `ActionReason`, and not in `04` because they are
   properties of a simulation, not rules of the method.

**Rejected.** Charging costs as a separate rupee field (then R would ignore them or need a second
formula); dropping open positions from the trade list (the curve would disagree with the list).

**Reversal.** `_paid`/`_received` are the two functions; `_liquidate` is the one call site for
both reasons.

## SW9.5 — Detection windows are `bars_required` sessions of the calendar, and a single-bar name is left out · ⚠ UNREVIEWED

**Context.** The worker hands the detectors 200 sessions of bars and lets `_window` take each
instrument's last 125. Per session, the backtest slices the indicator frame (sorted by date) to
the last `SwingConfig.bars_required` (126) calendar sessions and hands *that* to
`detect_setups` — one contiguous slice, no per-instrument loop.

**The choice.** A name with missing bars inside the window is detected on fewer bars, not on
older ones (the worker, with 200 sessions in hand, would reach further back). The difference is
confined to names with gaps inside a 126-session window and is the more defensible reading —
a base is measured in sessions, not in bars that happened to print. `detect_flags` raises on an
instrument with exactly one bar in its window (its listing day: `slice(0, n − 1)` is empty and
`arg_max()` is null); the backtest leaves single-bar names out of the window, since one bar
cannot be a setup, and STATUS.md records the edge for `setups.py`'s owner. The speed
consequence is the one that matters: 300 names × 2,000 sessions run in ~25 s, and the detectors
are linear in the universe, so 2,500 names × 2,300 sessions is about ten times that — inside
`06` SW9's thirty minutes with room.

**Reversal.** `_slices` (the window length) and `_MIN_WINDOW_BARS`.

## SW9.6 — A run is an append-only row; the runner's bars are the detectors' bars plus the names that died inside the run · ⚠ UNREVIEWED

**Context.** `06` SW9 says the run is "stored in `sw_backtest_run` (id, params, started/finished,
stats JSONB)" and `04` §11 says "survivorship handled by `instrument.delisted_on`". Three things
needed a shape before the runner could be written: what a re-run does to the row, which bars the
runner reads, and which configuration a run is sized and filtered with.

**The choice.**

1. **A run is a fact, not a slot.** `sw_backtest_run` is append-only: a re-run inserts a second
   row and nothing edits a stored `stats`. The number that was on `/swing/journal` the week the
   flag was considered (`02` §3.3) must survive a detector recalibration that produces a
   different one, for the same reason `sw_setup_daily` is a snapshot and `sw_config_audit` is a
   history. `params` is written **on the way in** — a run that never finished still says what it
   was asked to do — and `stats` is `BacktestResult.to_json()` stored as-is (every price a string
   of its exact decimal, the `ladder` trace included), so the row is reproducible from `params`
   and the bars alone. A failure is a row too: `error` carries the exception's type, message and
   traceback, `finished_at` is set, and the exception is **re-raised** after the flush so Celery
   reports it. The Celery body commits twice — the started row before the run, the result or the
   error after — so a failed nine-year run is a durable row, not a rollback (house rule 3 read
   for a job: nothing is swallowed, and nothing is lost either). "Finished", for the journal,
   means `finished_at` set **and** `error` null.
2. **The bars are `load_swing_bars`'s frame, with one clause changed.** The runner reads the
   same adjusted frame the nightly job hands the detectors — the cash series, `upper_circuit`
   multiplied into the adjusted space, the user's liquidity floors — over
   `lookback_start(start, LOOKBACK_SESSIONS)..end`, so the run's first session is detected on
   the same 200 sessions of history the live job would have had that night (a test asserts the
   frame equals `load_swing_bars` over that window, column for column). The one difference is
   the survivorship clause: the nightly query keeps `delisted_on IS NULL` because a retired name
   cannot be a candidate tonight, and a backtest that read it would be a study of the survivors.
   The runner keeps every name delisted **on or after `start`** (or never); a name that stops
   printing bars is then sold by the engine's own `NO_BAR` rule (SW9.4). The reader that turns
   rows into the frame is mirrored in `tasks/swing_backtest.py` rather than imported, because
   the nightly reader is inline in `load_swing_bars` and that file is SW3's; a
   `delisted_since: date | None` keyword on it is the one-line merge, for SW12's pass.
3. **The configuration is what the detectors run with tonight, and the sleeve is the
   parameter.** `params_for` builds `BacktestParams` from `load_swing_config(user_id)` —
   `sw_config`'s three liquidity floors over `DEFAULT_SWING_CONFIG`, nothing else from the
   settings row — so the run's universe is the one the user actually trades. The sleeve is
   `params.sleeve_inr`, `04` §11's constant ₹10 lakh, and never `sw_config.sleeve_capital_inr`,
   which is ₹0 until Maulik sets it and would plan nothing; risk per trade is the pack's 0.5%
   (`SizingConfig`), not the settings row's, for the same reason — the backtest is the method's
   number, not this month's settings'. A test asserts a run trades at 492 shares with the
   settings row at ₹0.
4. **The task is `baskfy.swing.backtest`, on the compute queue, with no Beat entry.** The queue
   is set on the task itself (`queue=QUEUE_COMPUTE`) because no `baskfy.swing.*` route exists in
   `TASK_ROUTES` — the other swing tasks are routed by their Beat entries' `options`, and this
   one has none: a five-minute run over nine years is something a person asks for, from
   `tools/swing/backtest.py` or by name, not a schedule. Money arrives as strings and becomes
   `Decimal` (house rule 9); `start` defaults to 2017-01-01 (`04` §11: "2017→"), `end` to today
   in IST.

**Rejected.** Upserting one row per `(user, start, end)` (a re-run would erase the number the
gate was judged on); reading `load_swing_bars` unchanged (survivorship bias, and the caveat on
the card would be untrue); editing `tasks/swing.py` to add the keyword (SW3's file, another
leaf's tree during a parallel run); sizing from `sw_config.sleeve_capital_inr` (₹0 plans
nothing, and the run would change every time the settings did); a Beat entry (a run nobody asked
for, rewriting the card nightly).

**Reversal.** `latest_backtest`'s three `where` clauses decide what "finished" means; the
delisting clause is one `or_` in `swing_backtest._bar_query`; `params_for` is where a run's
config comes from; `queue=` on the decorator.

## SW9.7 — The card's `stats` is a card, not the row: the journal's own shape, numbers with their precision · ⚠ UNREVIEWED

**Context.** Contract C2 fixes the card as `{run_id, params, started_at, finished_at, stats,
caveats}` with `stats` an object whose shape this leaf owns, and the page (1.2.2) renders
`stats` generically two levels deep unless it recognises something. `02` §3.3 wants "its
R-distribution, win rate and expectancy" on the page. The stored row is `to_json()` whole —
the trade list, a 2,300-point equity curve, the ladder trace — which is a record, not a card.

**The choice.**

1. **`stats` on the wire is:** `04` §10's ten statistics at the **top level** (the same keys as
   the two journal cards' `stats`, so the page's tiles are one component fed three objects);
   `histogram` in the journal's six-bucket shape (`HISTOGRAM_BUCKETS`, `bucket_of` — computed
   from the stored trades' `r_multiple`, so the backtest's distribution and the paper book's are
   bucketed by one function and drawn by one component); `by_setup` and `by_year` as §10's
   statistics per group; `funnel` as the engine's counts; `equity` as `{sessions, start, end,
   low, high}` of the curve. The trade list, the curve and the ladder trace **stay on the row**:
   the card is what a page can read, the row is what a reviewer can audit, and `tools/swing/
   backtest.py --json` prints the row.
2. **Every stored decimal string is a `Decimal` on the card**, so the canonical encoder puts a
   number with its precision on the wire (`0.28`, not `"0.28"`, and not `0.28000000000000003`):
   the page's `signedR` and `toFixed` want numbers, and house rule 8 wants the stored precision.
   The same for `params.sleeve_inr` and `cost_pct_per_side`; `config` is passed as stored.
3. **The caveats come from `baskfy_core.swing.CAVEATS`, not from the row.** A run stored under
   an older wording still shows the sentences `04` §11 has today; the card cannot paraphrase a
   caveat away, and neither can a stale row.
4. **`JournalView.backtest` is a plain object** (`BacktestCard.as_json()`), not the dataclass,
   because the route hands it to `SwingBacktestCardOut` as-is and pydantic validates a mapping
   into a model where it would refuse a foreign dataclass; the route file is 1.2.1's and
   unchanged. The typed `BacktestCard` exists for the tests and the next reader.
5. **The page draws what it recognises and lists the rest.** With the ten headline numbers
   present: one sentence ("412 trades over 2300 sessions: +0.31R a trade, 42% winners, +127.75R
   in all"), the ten tiles, the six bars, by setup and by year closed as tables, the allocation's
   start and end, the funnel — and any key it does not know under "Other results", never
   dropped. Without them (an older run, a shape the page predates) it falls back to 1.2.2's
   generic listing, which a test keeps alive. Parameters are four tiles (first and last session,
   the constant allocation, the cost per side) and the whole `config` behind a disclosure, since
   the run is reproducible from it. A note under the caveats says entry and exit prices carry the
   cost per side (SW9.4) and that nothing compounds (SW9.1) — the two things a reader of the
   tiles would otherwise misread.

**Rejected.** Shipping `to_json()` as `stats` (a page of 4,600 curve points shown as "2300
entries", and every number a string); a second histogram bucketing for the backtest (two
answers to "what is a −1R"); leaving the card generic (the `02` §3.3 sentence would have been
a label/value grid three rows deep).

**Reversal.** `backtest_stats` / `backtest_params` in `swing_journal.py` are the whole shape;
`BacktestResults` / `BacktestParameters` in `page.tsx` are the drawing; the generic fallback is
`readStats` returning `null`.

## SW9.8 — The CLI runs the task body or the planted year, and the full-history time is an extrapolation on this machine · ⚠ UNREVIEWED

**Context.** `06` SW9: "`tools/swing/backtest.py` + a `baskfy.swing.backtest` task … the run
over the full history completes on the dev box in < 30 minutes." The dev database holds ten
sessions of bars for 180 instruments and is at migration `0026` (SW0.2, SW3.3, and the
"Not done" list on every STATUS section since); nothing on this machine can run 2017→.

**The choice.**

1. **Two modes, one script.** `--fixture` needs no database: it runs the planted year from
   `packages/core/tests/swing_backtest_fixtures.py` through the pure engine, prints the table
   and the planted trade beside the number the fixture expects, and exits 1 if they differ —
   the engine's smoke test from a shell, and the acceptance criterion "a fixture year with a
   known planted flag reproduces the planted trade's R to 2 dp" runnable without a worker. The
   default mode is the Celery task's body (`run_and_commit`) in-process against
   `BASKFY_DATABASE_URL`: load, run, store, print the stored statistics and **the elapsed
   time** — that elapsed time is the `< 30 min` measurement, and the first `--start 2017-01-01`
   run on a backfilled database is where it will be read. The fixture module is reached by
   putting the core suite's directory on `sys.path`, as `replay.py` puts the desk's on it; the
   worker and API tests reach the same module the same way, because the planted trade is the
   *specification* and a second copy could drift from it (the reason `test_swing_detect.py`
   gives for retyping its shape is pytest's import mechanics, not policy, and it does not apply
   to a module that owns its `sys.path` line).
2. **The full-history time is recorded as not measurable here, with the extrapolation.** From
   1.3.1's measured 300 instruments × 2,000 sessions in 24.1 s and the detectors' linearity in
   the universe, 2,500 × 2,300 extrapolates to four to five minutes — six times inside the
   thirty. The runner adds one query over `ohlcv_daily` (about 5.7 million rows for the cash
   series since mid-2016, a few tens of seconds through asyncpg) and one JSONB write. STATUS
   says so in those words; SW-FINAL-REPORT's first-morning steps are where the measured number
   goes.

**Rejected.** Running `make backfill` first (an evening of Kite calls before the module's own
code was exercised, and SW3.3 already settled this for the detectors); a `--fixture` that writes
the planted year into the database (it would need `BASKFY_DATABASE_URL`, which is the mode it
exists to avoid; the worker test does exactly that under the test database instead).

**Reversal.** The `sys.path` line in each of the three files; the STATUS sentence when the
number is measured.

## SW10.1 — The drill fakes the broker by making it explode, and fakes the clock by never reading it · ⚠ UNREVIEWED

**Context.** `06` SW10: "The DRY_RUN morning drill … premarket → replayed morning → confirm two
lines → EOD → next morning's plan, end to end, 0 orders reaching a broker." `02` §3.1 makes the
same drill the first condition of the real-money gate. A drill of the production code paths
needs a broker client to hand the gateway and a clock for four jobs that run at four times of
day; the doc says neither how the broker is stood in for nor how a script run on a Tuesday in
September rehearses a Wednesday in August.

**The choice.**

1. **The broker is `ExplodingKC`** — the fake SW7's suites already use, with a touch counter:
   `place_order`, `place_gtt`, `delete_gtt` and `instruments` raise `AssertionError` and count.
   It is handed to the real `build_swing_gateway`, so guards, risk, idempotency, the journal
   and the dry-run branch are the production ones; the fake is reached only if the flag failed
   to stop the order, in which case the gateway catches the raise, answers `REJECTED`, and the
   drill fails on the outcome *and* prints the count. Not a recording fake that answers
   `PLACED`: a fake that can answer makes "0 orders reached a broker" a claim about the fake's
   bookkeeping rather than about the flag. The proof is then three-fold and the drill checks
   all three — the fake was not touched, the swing journal is exactly `dry_run, gtt_dry_run`
   per confirmed line, and every position and fill row is `simulated=true` with a `DRY-…`
   trigger id.
2. **The clock is an argument, never a reading.** Every job takes its session date and its
   `now` (`run_swing_eod(now=…)`, `run_swing_premarket(now=…)`, `execute_line(now=…)`), and
   the monitor's strategy reads the ticks' own timestamps — the fixture is dated 19 Aug 2026,
   so the drill runs 18 Aug evening, 19 Aug morning and 20 Aug's plan on any day of the year,
   and a plan built at 09:31 is confirmed at 09:50 against its own 30-minute expiry. Nothing
   in the drill calls `datetime.now()`; the only clock in the run is the gateway's rate
   limiter, which is left in (two orders and four GTTs cost under a second).
3. **The monitor flag stays false, and the strategy is driven directly.** `BASKFY_SWING_MONITOR_ENABLED`
   is pinned `false` in the drill's environment before the desk is imported (`02` Track B: the
   flags default false and stay false), and `SwingBreakout` is constructed the way
   `tools/swing/replay.py` constructs it — no gateway, the CSV for candles, the synthesised
   four-ticks-a-candle tape — with **`PgSignalStore` over the desk's Postgres adapter** in
   place of the harness's list. That is the monitor process minus the flag check, the
   websocket and the Kite quote for circuit bands (the bands come from the fixture's
   watchlist). `record_monitor_ran` is called afterwards as the process would.
4. **The database is reset, and the drill refuses a database not named as disposable.** It
   runs `alembic upgrade head`, truncates the pipeline and `sw_` tables (the worker conftest's
   list, the `sw_` names found from `information_schema` so a new table needs no edit), seeds
   the reference rows and the calendar, then its own user, broker account, four instruments,
   40 sessions of bars and the two tables the detectors would have written. A database whose
   name does not end in `test`, `drill` or `_t<n>` is refused unless `--database-is-disposable`
   is passed — the safety rail against pointing it at the dev database by habit. `DRY_RUN=false`
   or the execution flag `true` in the environment is refused before anything is built, the
   Friday drill's rule.
5. **The sole user is the environment's id, and the drill owns that row.** `BASKFY_SOLE_USER_ID`
   (default 1) is the `app_user.id` the drill writes for — inserted with `OVERRIDING SYSTEM
   VALUE` when no row has that id, and **made the drill's, identity and all** (`ON CONFLICT (id)
   DO UPDATE`) when one does — so `C.SOLE_USER_ID` in the desk, the worker's `swing_user_id` and
   the store's `user_id` are one number, which is what Track C §6 says. Overwriting a row's
   identity is only acceptable because preflight already refused any database not named as
   disposable; the alternative — borrowing whatever sits on the id — put the drill's
   `broker_account` on a test suite's `@example.com` user and tripped that suite's own sweep
   (`DELETE FROM app_user WHERE email LIKE '%@example.com'`) on the next run. The drill's own
   email is `swing-drill@baskfy.invalid`, outside that sweep, so its rows — the record it
   printed — outlive it and the suites truncate around them.

**Rejected.** A `RecordingGateway` (it answers `PLACED`; see 1). Monkeypatching `datetime`
(the jobs take a clock; patching one would hide a job that started reading the wall clock).
Flipping the monitor flag inside the drill and running `swing_monitor.main` (needs a Kite
session for the ticker and the quote, and a flag flip is the one thing the drill exists to
show is unnecessary). Running against the dev database (it is at `0026` on purpose and holds
another session's data; a drill that resets it is a drill nobody runs twice).

**Reversal.** `ExplodingKC` and the journal directory are ten lines at the top of
`confirm_two_lines`; a recorded morning replaces the fixture by two paths; the reset is one
function (`reset_and_seed`) and the refusal one regex (`_disposable`).

## SW10.2 — A morning's SIGNAL plans are each sized against the 09:15 context, so two confirms can overshoot the rung — recorded, not fixed · ⚠ UNREVIEWED

**Context.** The drill's morning raised two triggers, `ALPHAFLAG` at 09:31 and `BETAEP` at
09:45, and both were confirmed. Each `SIGNAL` plan carried a line: 1,666 shares (₹1.68 lakh)
and 833 shares (₹1.75 lakh), each respecting rung 0's 25 % ceiling on a ₹10 lakh sleeve *on its
own*. Together they are ₹3.43 lakh, 34 % of the sleeve — over the rung's ceiling, and two
positions at a rung that allows two, so a third trigger would still have been skipped
`TIER_FULL`. The cause is SW6's `SignalContext`, "read once at start, not per tick": the
09:45 plan was built against a book that did not yet hold `ALPHAFLAG`.

**The choice.** The drill prints the book after the confirms against the rung's ceiling and
labels the overshoot a `WARNING` naming this entry; it does not fail on it, because it is not a
Track C breach — the cash was there (`by_cash` is a hard cap in `size_position`), nothing was
sold that was not owned, nothing was leveraged — and because the fix belongs to
`app/swing_monitor.py`, which SW10 does not own (SW11 edits it for telemetry) and SW10's
mandate is proofs, not changes. The finding is on STATUS's "What the drill found" and here.

**The fix, when taken (SW11 or SW12):** `PgSignalStore._plan_for` re-reads `load_context`
before building each plan — three queries per trigger, a few triggers a morning — so the
second plan sees the first confirm's position in `open_symbols` and its cost in
`open_exposure_inr`, and `build_entries` answers `TIER_FULL` / `EXPOSURE_FULL` / `ALREADY_HELD`
exactly as the evening would. One line in `_plan_for`, one test in `test_swing_monitor.py` (a
trigger after a confirmed one is sized against the book that includes it), and the drill's
`WARNING` line becomes the "book after confirms … (rung ceiling 25 %)" line. Note the same
staleness is present for the person: the desk page shows both lines with a Confirm each, and
`execute_line` re-checks only `ALREADY_HELD`, not the ceiling — a re-read in the store is the
smaller change and the one that keeps the desk's "no button where `execute_line` would refuse"
rule honest.

**Rejected.** Failing the drill (it would fail on a rule SW6 chose, with the change owned by
another leaf's file, and the AC is about orders and counters). Making the drill confirm only the
first trigger (the drill exists to find exactly this).

**Reversal.** Delete the `WARNING` branch in `confirm_two_lines` once the store re-reads.

## SW10.3 — What the source scans admit, and why each admission is named · ⚠ UNREVIEWED

**Context.** Three scans would have failed on code that is correct, and each needed a
decision rather than a wider regex.

1. **`baskfy_api` imports `baskfy_execution`** — `broker_ports` (the holding-row shape and
   `total_quantity` for the broker sync), `TenantIds` and `mint_client_id` from the package root
   (the plan store's client ids). Track C §4 is about the *order path*; these are pure shapes
   and ids. The test names an allow-list — those three modules and those three root names —
   and treats the gateway, `gtt`, the adapters, the brokers, the guards, `kiteconnect` and the
   desk's `app.` as offenders. Widening the allow-list is a diff on a named constant.
2. **`app/swing_monitor.py` names the Kite wrapper** — it *reads* circuit bands and minute
   candles through it. The runner is held to the placing-verb list only (`place`, `place_order`,
   `place_gtt`, `.place(`, `delete_gtt`, `OrderGateway`, `order`); the strategy is additionally
   held to `self.gw`, `kc.`, `kiteconnect`, `kite_client`.
3. **`UPDATE {SCHEMA}.sw_signal SET plan_line_id = ? WHERE id = ?`** in `PgSignalStore.raise_signal`
   carries no `user_id` predicate. The row was inserted with the store's `user_id` four lines
   earlier and is addressed by the id that INSERT returned; a `user_id` predicate would only
   re-check what the same connection just wrote. Whitelisted by function and exact statement,
   and a second test asserts the whitelisted statement is still issued, so a stale whitelist
   is a red test rather than a hole.

Also decided: the web scan is a small TypeScript lexer (strings, template literals, regex
literals, both comment forms) rather than a regex, because the vitest under `lib/swing/__tests__`
contains a regex literal with quotes in it and a string containing `//`, and a regex-based
stripper desynchronised on both. The lexer has its own test. Test files are counted and held to
the narrower rule a test can satisfy — no execution import, and with every string blanked no
`fetch(` and no server action — because their job is to *name* the banned words.

**Reversal.** Each admission is a named constant or a one-entry dict in the test file.

## SW10.4 — The confirm is the gate: a BUY is re-sized under the session's row lock, and refused with the skip's code · Maulik, 2 Sep 2026 (STANDING-ANSWERS A5)

**Context.** SW10.2 recorded the drill's finding: two SIGNAL lines, each inside rung 0's 25 %
on its own, confirmed together to 34 % of the sleeve, because each plan was sized against the
context the monitor read at 09:15. Maulik's decision (STANDING-ANSWERS A5, MD6): "Fix at
confirm in `/swing/execute` under a row lock on the day's `sw_session`: re-derive open exposure
at cost + today's CONFIRMED/SENT/FILLED lines + cash; re-size or refuse (`EXPOSURE_FULL` /
`TIER_FULL` / `SESSION_CAP`). Also re-read context per trigger so the page shows what will be
sent. Property test: no confirm sequence exceeds ceiling, count or cap; the drill shows 25 %,
not 34 %." Applied as stated; this entry records how.

**What was built.**

1. **The lock.** `PgSwingStore.lock_session_for_update(day)` opens a transaction on the desk's
   autocommit connection (`BEGIN`; the adapter passes it through), inserts the day's
   `sw_session` row if absent (`ON CONFLICT DO NOTHING`, so the monitor's and the evening's
   upserts keep theirs), then `SELECT … FOR UPDATE` on it; the sqlite twin, which has no row
   locks, takes `BEGIN IMMEDIATE` — the database's write lock, which serialises the same way
   for a one-file desk, and a test proves a second connection cannot write while it is held.
   `execute_line` runs the **whole** confirm inside it — the second read of the line's state
   (two requests that both passed `_validate` on a PROPOSED line: the second finds it FILLED
   under the lock and answers 409), the `CONFIRMED` mark, the context read, the re-size, the
   gateway call, the position, the fill, the GTT, the counters — so two browser tabs cannot
   confirm past the ceiling together. `COMMIT` on the way out; an exception rolls it back and
   `execute_line` then marks the line `REJECTED` and counts the click outside the transaction,
   which is the end state SW7 promised, reached through the rollback rather than around it.
   A SELL and a RAISE take the lock too: the book they change is the book the next BUY is
   sized against.
2. **The context.** One reader, `app.swing_monitor.load_context(conn, user_id, day, schema)`,
   for the monitor and the desk (`PgSwingStore.session_context` calls it over the store's
   schema; the sqlite twin grew `sw_market_daily` and `sw_setup_daily`, and the one
   Postgres-only `DISTINCT ON` became a portable "latest row per name" subquery). It returns
   the last close's gate and rung **with `drawdown_locked`**, the sleeve's capital, the open
   positions at cost, every `BUY_ON_TRIGGER` line of the day in `CONFIRMED`/`SENT`/`FILLED`
   (`ENTRY_TAKEN_STATES`) — the ones not yet a position are `PendingLine`s, held at the
   trigger for exposure, count and symbol — and `entries_today`, the count of those lines
   whatever became of them. Cash is capital less that exposure, the evening's own formula. The
   market row is the latest **strictly before** the day: the close the plan was built on, and
   the only row that can exist during a session (the detection job writes the day's row after
   the close; a "≤ day" read would only differ on a re-scanned Saturday). The context is read
   **before** the line is marked `CONFIRMED`, so a line is never counted against itself.
3. **The re-size.** `swing_execute.resize_buy` builds a `WatchItem` from the line (its own
   trigger and stop, the name's ADR/turnover/score from the context) and hands it to
   `swing_monitor.entries_now` — `build_entries` with the person's three sizing knobs
   (`sizing_config` from `SwingStore.config()`, the same three the worker and the monitor use)
   and `entries_already_today=context.entries_today`, the additive core argument of `04` §5.3.
   The quantity sent is `min(planned, allowed)` — never more than the page showed. A smaller
   quantity is written back to the row (`resize_line`: `quantity`, `risk_inr = (trigger − stop)
   × qty`, `position_value = trigger × qty`, the note saying what was done and why), and the
   position, the fill and the GTT all carry it. A line the rules cannot line is `BLOCKED` with
   the skip code leading the reason (`EXPOSURE_FULL: BETAEP — …`), the line `REJECTED` (never
   left `CONFIRMED`), nothing sent, nothing in the journal, and the click counted.
4. **The monitor re-reads per trigger.** `PgSignalStore._plan_for` calls `read_context()`
   before every plan, so the 09:45 trigger is sized against a book that holds the 09:31
   confirm; the `context=` argument the drill and `main` hand in is kept as the log's starting
   reading and is never what a plan is sized from (a test hands in a stale GREEN context over
   a RED database and gets `GATE_RED`). The monitor's `load_config` now also carries the
   person's sizing knobs (SW9.5's "the monitor still sizes with the pack's risk knobs" is
   closed), so the preview is sized with the numbers the confirm will use.
5. **The proofs.** `tests/test_swing_execute.py`: the drill's arithmetic (1,666 held; 833 →
   390 at 210; 24.98 %), `EXPOSURE_FULL` when the headroom cannot buy the minimum trade value,
   `TIER_FULL` by rung and by the person's cap, `SESSION_CAP` on the fourth entry counting
   `SENT` lines and not `REJECTED` ones, a `SENT` line as exposure, never more than the line,
   the line not counted against itself, the person's risk knob, `GATE_RED` / `DRAWDOWN_LOCKOUT`
   / no-ADR / too-wide / untradeable-setup refusals, the partial-then-full sequence
   (whole, shrunk, `TIER_FULL`), the lock taken for every kind and spanning the gateway call,
   the rollback on an exception, the 409 under the lock. `tests/test_swing_desk.py`: the lock
   commits, keeps an existing row, rolls back everything on an exception, is real across two
   connections; the context over the twin; `resize_line` at the schema's scale and scoped to
   the user; the route re-sizing 1,666 → 1,553 (rung 0, ₹93,390 held), `EXPOSURE_FULL` /
   `TIER_FULL` / `SESSION_CAP` as JSON with the row `REJECTED`. `tests/test_swing_track_c.py`:
   **500 seeded sequences** of 1–8 confirms on random sleeves (₹2–50 lakh), rungs, caps, books
   and lines through the real dry-run gateway — after every confirm the book is inside the
   ceiling, the count inside `min(rung, cap)`, at most three entries, never more than the line;
   after every refusal the book is exactly what it was — and the distribution is asserted to
   have exercised every code (a run: 338 sized, 416 re-sized, 1,075 `TIER_FULL`, 317
   `SESSION_CAP`, 25 `EXPOSURE_FULL`, 18 `SIZE_REFUSED`). `tests/test_swing_monitor.py`: the
   second plan of a morning sized against the first's confirm (390, not 833), the fourth
   trigger `SESSION_CAP`, the floor case with its arithmetic, the stale-context case. Core:
   `entries_already_today` as a hypothesis property and an exact case. The drill prints
   `EXPOSURE after confirms ₹249,817.30 = 25.0% of the sleeve (rung ceiling 25% =
   ₹250,000.00)` and exits 1 above the ceiling.

**Rejected.** Sizing at confirm with `size_position` alone (a second sizer disagreeing with the
plan's; the count, the cap and the gate would have been re-implemented — `build_entries` is
the rule and is reused whole). A lock taken only around the writes (the gateway call would sit
outside it and two tabs could both pass the re-size). Counting the session cap off
`sw_session.confirms` (a refused click bumps it; the cap is about entries taken, which are
lines in `ENTRY_TAKEN_STATES`). Leaving a refused line `PROPOSED` (the page would offer the
button again for a line the book refused; `REJECTED` with the reason in the outcome is what
SW7 already did for a gateway refusal).

**Reversal.** `lock_session_for_update` is one context manager and `execute_line`'s `with`;
`resize_buy` is one function called from one place in `_buy`; `entries_already_today` defaults
to 0 and `build_entries` is unchanged without it.

## SW10.5 — A live trigger that does not fit the ceiling whole is taken at the size that fits; the evening and the morning plans still skip it · ⚠ UNREVIEWED

**Context.** A5 says "re-size or refuse" and "the drill shows 25 %, not 34 %". `04` §9.1's
`build_entries` does not shrink a name to fit the exposure ceiling — it skips it
`EXPOSURE_FULL` — and refusing the drill's second confirm outright would have shown 16.8 %,
inside the ceiling but not what A5 describes. The two readings differ in what the sleeve holds
after the morning: one name at 16.8 % or two names at 25 %.

**The choice.** `app.swing_monitor.entries_now` — used by both the SIGNAL plan and the confirm
— calls `build_entries` once; if the **only** answer is `EXPOSURE_FULL`, it calls it once more
with the cash bounded by the ceiling's headroom (`equity × max_exposure_pct − open exposure`),
so `size_position`'s own cash cap produces the size that fits, and every other rule (the
count, the session cap, the stop width, the minimum trade value, the risk budget) still
applies to it. A headroom that cannot buy the minimum trade value is `EXPOSURE_FULL` with the
arithmetic in the detail ("book ₹2,48,000.00 of ₹2,50,000.00 leaves ₹2,000.00, and at that size
SIZE_REFUSED BELOW_MIN_TRADE_VALUE"), so a sliver never goes out. The evening and the morning
plans are **not** changed: a plan of many names built at 21:05 skips the one that does not fit,
because shrinking the third-best name of the night to a remainder is not "1, 2, 3 stocks per
day" — it is padding the book; the re-size is the rule for one name at the moment of its
trigger, where the alternative is passing on a setup the rules like for want of a few percent
of room. His words support both sides ("generally 5–25 %" — a re-sized 8 % position is inside
his range; and "fewer or no trades" in a tight book). Applied to the SIGNAL plan as well as
the confirm because A5's other clause — "the page shows what will be sent" — is only true if
the preview and the confirm are one function.

**Rejected.** Refusing at the ceiling (16.8 %, a setup passed for ₹82,000 of room). Shrinking in
the evening plan too (changes SW1's contract tests and `04` §9.1 for a case A5 did not name).
A separate "min position %" floor for the re-size (the minimum trade value already is one;
another threshold would be a literal, B13).

**Reversal.** Delete the second `build_entries` call in `entries_now` (eight lines) and the
confirm refuses `EXPOSURE_FULL` where it now shrinks; the drill's `resized != 1` check goes
with it.

## SW10.6 — What the sqlite twin, the census and the neighbouring tests had to learn · ⚠ UNREVIEWED

1. **The twin grew two tables and three columns.** `tests/test_swing_desk.py`'s DDL now carries
   `sw_market_daily` and `sw_setup_daily` (every column of `0028`/`0030`, so a statement written
   against the real table cannot pass by accident) and `sw_config`'s `0030` columns with the
   `0030` defaults (10 / 4.00). `Scenario.config()` writes last night's market row (rung 2, the
   rung the scenario's plans always said) and a detection row per name by default, because a
   plan line that exists was built from both and a book with neither is RED with no ADR to
   check; `market=False` / `detected=False` are for the tests that want the empty case.
2. **Three SW7 tests were re-pinned with the reason in the docstring**, none weakened:
   the gateway `DUPLICATE` case wipes the book between the two confirms (with the position on
   the book the gate answers `ALREADY_HELD` first, and the gateway's map is meant to be the
   *last* line); the band-warning case moves from a 12 % stop (now `STOP_TOO_WIDE` at the gate,
   `04` §6.1) to a 0.3 % one, the band's near edge; `MemoryStore.add_line` registers the name's
   ADR, as a detection row would have, so the SW7 fixtures need no edit. The re-read tests in
   `test_swing_monitor.py` replace the hand-built `SignalContext` with a `FakeConn` that answers
   `load_context`'s four reads from canned rows, because the store no longer sizes from a
   context it is handed.
3. **`journal_events` and the property's journal.** Every gateway in one test writes the same
   journal file (the path is the shim's); the property reads the tail per case. A stop under the
   band's 0.5 % floor journals `gtt_band_warning` beside the GTT — a dry event, filtered out of
   the "exactly `dry_run, gtt_dry_run` per entry" check, never out of the "nothing reached a
   broker" one.
4. **The census.** The two new statements (`lock_session_for_update`'s INSERT, `resize_line`'s
   UPDATE) name `user_id` and the Track C §6 scan finds them; the `SELECT … FOR UPDATE` ends its
   literal so the scan's `UPDATE\s+` cannot mistake it for a write.

## SW10.5.1 — A live gap is a `PENDING_RANGE` line that holds a session slot, not a seat in the rung · Maulik, 2 Sep 2026 (STANDING-ANSWERS A7) · ⚠ UNREVIEWED on three details

**Context.** A7 amends SW6.2: "keep no-stop; show it in the MORNING plan as a `PENDING_RANGE`
line: no qty, no stop, not confirmable, reserves one of the session's new-entry slots,
information-only preview at a 1-ADR stop … wider than 1 ADR → `STOP_TOO_WIDE`, slot released;
unreleased slots freed at 10:45; a `PENDING_RANGE` line can never reach `/swing/execute`."

**What was built.** `LineKind.PENDING_RANGE` and `EXECUTABLE_KINDS` in `plan.py`;
`WatchItem.stop_ref` may be `None`; `build_entries` emits the pending line after the same
refusals a buy faces (setup, lock-out, gate, held, session cap) with the preview in its note;
`watch_items` keeps a row with a trigger and no stop; `sw_plan_line.kind` gains the value
(`0031`); the monitor's `load_context` counts today's `PROPOSED` pending lines as `reserved`
and `entries_now` adds them (less the name's own) to `entries_already_today`; `PgSignalStore.
release_reservation` marks the name's pending line `SKIPPED` the moment it triggers (an
`UPDATE … WHERE state = 'PROPOSED'`, so once); `cutoff_open_orders` → `store.expire_pending`
frees what is left at 10:45 (`EXPIRED`); `_validate` and the route refuse a non-executable kind
with a 400 before anything is read or locked; the page renders the row with no button.

**The three readings, mine.** (1) **A session slot, not a tier seat.** The pending line counts
against `max_new_entries_per_session` for the names after it and is *not* refused by, nor
counted in, `min(rung, max_open_positions)`. It holds nothing; one of the plan's buys may
never trigger; the SIGNAL plan at range close answers `TIER_FULL` against the book as it is
then. The drill shows the consequence: at rung 0 with one buy lined and two `EXPOSURE_FULL`
skips the gap is still shown pending. (2) **A locked live gap is shown pending with the lock
noted** ("locked at the upper band: no fill until it unlocks") rather than skipped
`LOCKED_UPPER_CIRCUIT` as a priced flag is — MD10's own preview text names the case, and a
lock at 09:09 can open during the session; the slot is reserved either way. (3) **The gap is
ranked by a provisional score out of 70** — `live_gap_score`, the two terms of `04` §3 the
pre-open knows (gap, pace) — stored on `sw_watch.score`; a 72-score flag outranks any live gap,
which is his funnel's own order (a flag that set up over weeks over a gap nobody has seen
close). `sw_watch.adr_pct` carries the ADR the bars measured, because the gap has no
detection row and SW9.5.2 refuses a stop nobody can measure against one ADR — the monitor's
context and the plan fall back to it.

**Rejected.** Reserving a tier seat too (a reservation that refuses a priced flag for a name
with no stop). Computing the preview quantity into `quantity` (a confirmable-looking number
on a line that must not be confirmable). Writing a `sw_setup_daily` row for the gap at 09:09
(the detectors' snapshot table, with a status it did not measure).

**Reversal.** Move the `stop_ref is None` branch below the `TIER_FULL` check (four lines);
delete the locked-pending branch in `_pending_line`; drop `score`/`adr_pct` from the watch row
and read `live_gap_score` off the note.

## SW10.5.2 — The live buy is a marketable LIMIT that is polled, grown by postback and cancelled at 10:45; the desk pulls the order book rather than exposing a postback URL · Maulik, 2 Sep 2026 (STANDING-ANSWERS A8) · ⚠ UNREVIEWED on the transport and four details

**Context.** A8 amends SW7.1: a marketable LIMIT at `min(trigger × 1.005, range_high + 0.25 ×
ADR)`, never MARKET; poll ≤ 10 s at ≤ 2 req/s; COMPLETE → GTT + position in the same request;
partial → `SENT` with the filled quantity and a GTT for it; later fills via `on_order_update`
modify the GTT (never a second); cancel the remainder at 10:45; the 15:15 sweep is SW11's.

**What was built.** `plan.marketable_limit` (two `OpeningRangeConfig` fields, B13) and the
poll's two (`fill_poll_seconds`, `fill_poll_interval_seconds`); `execute_line(orders, clock,
sleep)` with an injectable `OrderSource`; one bookkeeping path `_apply_buy_fill` for a whole,
a partial and a dry-run fill (`simulated` decides the rows' flag); `on_order_update` — idempotent
on `filled_quantity ≤ quantity_entered`, the delta at the price that keeps the averages
consistent, `gw.modify_gtt_quantity` (new on the gateway, with its own guards, journal and
dry-run branch) for the new open quantity, an arm for a naked partial, never a second GTT;
`cutoff_open_orders` (reconcile → `gw.cancel_order`, also new on the gateway → line `FILLED` /
`EXPIRED`, GTT untouched → `expire_pending`); `eod_gtt_sweep` as the 15:15 hook; the context
counts a partially filled order's **resting remainder at the trigger** as exposure.

**The transport, mine.** The desk gets no HTTP postback URL. Kite's postback is an
unauthenticated POST from Kite's servers; the desk's `websec` refuses every state-changing
request without a recognised Origin, and opening that middleware for one path is a wider
security boundary than this run should draw on its own. Instead the same handler is fed by a
**pull**: `POST /swing/reconcile` (a websec-covered form) reads today's `SENT` buys' order
history through the Kite wrapper (a read) and applies each; the 10:45 sweep does the same
before cancelling. `on_order_update` takes Kite's postback dict shape unchanged, so wiring the
KiteTicker's `on_order_update` callback (a websocket, no HTTP) or a checksum-verified route is
a one-line call site when SW11 wants push. **Four details:** (1) a cancel with shares held
closes the line `FILLED` (the position stands for what filled; the note says the rest never
did), with none held `EXPIRED`; (2) a simulated (`DRY-…`) GTT is "modified" locally, as a
simulated delete already is — nothing exists at the exchange; (3) `cancel_order` is
order-shaped, so it lives on the gateway behind the tenant, untouchable and journal layers
(law 2), with the kill switch *not* refusing it (a cancelled buy reduces exposure); its
statuses are named constants like the GTT ones so the weekly execution report and the
`/execute` breaker — which read `place()`'s literals — are unaffected, byte for byte; (4) an
order source that answers nothing (no Kite session) leaves the line `SENT` and the confirm
says so, never a guessed fill.

**Rejected.** MARKET after a `TRIGGERED` signal (`04` §9.4's old wording; A8 says never).
Arming the GTT for the requested quantity on `PLACED` (EXCESS). Polling inside the dry-run
path (it completes immediately; the gates' words). A `sw_order` table (the line's
`journal_ref` + `position_id` + the fill rows are the record).

**Reversal.** `ORDER_OPEN_STATUSES` and the poll are one function; the reconcile route is
twenty lines; a push transport calls `on_order_update` with the same dict.

## SW10.5.3 — Half risk is applied to the risk budget at plan time, only when a confirm would be real, and the evening counts the sessions down · Maulik, 2 Sep 2026 (STANDING-ANSWERS A9) · ⚠ UNREVIEWED on "execution is enabled"

**Context.** A9 amends SW7.2: `risk_multiplier = 0.5` before `size_position` while
`first_live_sessions_left > 0` **and** execution is enabled; paper plans full size (record
this reading); SELL/RAISE untouched; the countdown in `sw_config`, decremented once when a
LIVE `sw_session` closes, never by a request; a restart changes nothing; header text; a tag.

**What was built.** `SizingConfig.risk_multiplier_first_live` [0.5] and `first_live_sessions`
[5]; `plan.first_live_multiplier` / `sizing_at`; `build_entries(risk_multiplier=1)`; the
evening (`count_first_live_session`, before the plan, through `record_system_change` — audited,
`changed_by="swing-eod"`, `sw_session.first_live_counted` the once-mark), the premarket and
the monitor's `entries_now` all size with it; the desk's `sizing_config(risk_multiplier=…)`
re-sizes with it at confirm; `sw_position.half_risk`; `first_live_quantity`,
`_FIRST_LIVE_COUNTED` and the desk's write to `first_live_sessions_left` are gone (the
Protocol keeps C1's method; a test asserts the module never calls it);
`SYSTEM_OWNED_FIELDS["first_live_sessions_left"]` is `swing-eod`. The header is a sentence the
worker's report and the desk's view both build ("first live sessions: N left · risk 0.250%").

**The reading recorded, mine.** "Execution is enabled" means *a confirm would place a real
order*: the swing flag on **and** `DRY_RUN` off at the desk (`swing_gates().dry_run` false);
in the worker, its own `swing_execution_enabled` setting. A flagged desk in `DRY_RUN` plans
full size — its confirms are simulated, and the paper record is meant to rehearse the rules
at the size the rules describe. The countdown moves when the evening closes a session with
the worker's flag on, whether or not an order went out that day: "never by a request" is the
rule, and a live session with no trigger is still a live session the operator sat through.

**Rejected.** Halving at send time (SW7.2 — a page that shows one size and sends another, and
every refusal seeing the wrong size). Decrementing from the desk at the first real order
(a request; a restart double-counts). A `sw_plan` header column (the sentence is derived from
two numbers both readers already have).

**Reversal.** `first_live_multiplier` is one function; the evening's call is eight lines.

## SW10.5.4 — The ladder reads real closes from day one; PACK.6's paper clause is void · Maulik, 2 Sep 2026 (STANDING-ANSWERS A10)

**Context.** A10: real closes from day one, rung starts at 0, one idempotent date-bound
settlement at 21:05, the 09:09 job settles only as a catch-up when no record exists.

**What was built.** `closed_r_multiples` / `load_closed_trades` / `sleeve_nav` /
`sleeve_drawdown` read `simulated = False`; `_book_switched` and the peak reset are gone
(there is no switch); `closed_trades_read` is always `"real"`; `swing_premarket.
catch_up_ladder` runs `settle_ladder` for the last close **only** when its row has no
`detail.ladder.settled_by` record — the same function, the same closes, the same date bound —
and a session the evening settled is never settled twice (three tests). The ladder tests were
re-pinned: the fixtures plant real closes; the paper-book case now asserts that five paper
wins move nothing with the flag off *and* on.

**Residual, not mine to fix.** `baskfy_api.swing_journal` still reports `ladder.reads =
"SIMULATED"` while the flag is false (PACK.6's wording on the journal card). The file is leaf
1.3.4's this session; the card's `reads` should become `"REAL"` unconditionally, and the
`sessions` counter's "paper sessions" label is MD8′'s to soften. Listed in STATUS.

**Reversal.** One keyword on each reader.

## SW10.5.5 — The funnel: the top 20 flags by score each evening, the top 5 plus every EP in focus, and MANUAL rows on a ten-session clock · Maulik, 2 Sep 2026 (STANDING-ANSWERS A14) · ⚠ UNREVIEWED on two details

**Context.** A14 amends SW5.1: top 20 SETTING_UP flags by score + every EP auto-watched; the
monitor watches all; daily focus = top 5 + every EP → push + top of page; DETECTOR flags expire
after 10 sessions or on trigger; MANUAL rows expire after 10 sessions unless re-confirmed.

**What was built.** `WatchConfig.auto_watch_top_n` [20], `focus_top_n` [5],
`manual_valid_bars` [10]; `auto_watch` takes tonight's candidates through `_top_flags` (every
EP + the top 20 flags by score, ties to the lower id) and writes `score` / `adr_pct` on the
row; `refresh_focus` recomputes `sw_watch.focus` from the whole `WATCHING` list (every EP +
the top 5 flags by score), called by the evening and by the premarket after the gap scan;
`add_manual` sets a ten-session expiry, `reconfirm` restarts it (`reconfirmed_on`),
`expire_stale` gives a pre-0031 MANUAL row its clock from `added_on` once and then retires it
like any other; `PATCH /swing/watch/{id}` accepts `reconfirm: true` (a DETECTOR row is refused
400); the read model carries `score`, `adr_pct`, `focus`, `reconfirmed_on`; the desk page sorts
focus triggers first. The web hub's "Still watching" control is `05`'s spec, not built here.

**Two details, mine.** (1) **"Top 20" is per evening, of tonight's candidates.** A row already
watching keeps its own ten-session clock; the list can therefore hold more than twenty flags
across evenings, and the focus five are chosen from all of them. The alternative — retiring
the twenty-first-best row because a better one arrived — is a state change MD16 did not ask
for and would make "what did I miss" unanswerable. (2) **Focus is recomputed, never
accumulated**: a better flag tonight pushes yesterday's fifth out of focus; the row stays.

**Reversal.** `_top_flags` and `refresh_focus` are twenty lines between them.

## SW10.5.6 — The drill's late partial fill is a SENT line the drill writes and a postback the handler applies · Maulik, 2 Sep 2026 (STANDING-ANSWERS B7) · ⚠ UNREVIEWED on the one written state

**Context.** B7: one flag break, one EP, one locked circuit, one late partial fill → two
confirms → 10:45 sweep → (15:15 if the stub exists) → EOD → next morning; the counters; 0
orders reaching a broker. The drill's own rule (SW10.1) is the real gateway over an exploding
broker client, and the gateway's dry-run branch fills every order whole — it cannot produce a
partially filled live order.

**The choice.** The drill keeps SW10's four signals and adds the live gap (`EPSILONGAP`, a
fifth fixture set `morning-live-gap.*` beside `morning-synthetic.*`, which stays as SW6 left
it) for A7; for A8 it takes `ALPHAFLAG`'s SIGNAL line through the live *shape*: the line is
marked `SENT` with an order id under the session lock, exactly as `execute_line` leaves an
accepted-but-unfilled marketable LIMIT (SW7.1), **and the drill prints that it wrote this state
because the dry-run branch cannot**; the 10:20 partial then arrives through
`on_order_update` — the real handler, the real gateway's dry-run GTT for exactly the filled
1,000, a repeated postback shown to write nothing — `BETAEP` is confirmed at 09:55 against a
book that counts the resting order (re-sized 833 → 389, 24.98 %, A5's story intact), the 10:45
sweep cancels the 666 remainder through the gateway's dry-run cancel with the GTT untouched,
and the 15:15 hook finds no naked position. The journal is exactly `dry_run, gtt_dry_run,
gtt_dry_run, order_cancel_dry_run`; the broker client is touched 0 times.

**Rejected.** A `RecordingGateway` in the drill (the drill's whole point is the real gateway).
A dry-run branch that answers `PLACED` when an order source is present (the gates' words are
"the dry-run path completes immediately"). Modifying the shared `morning-synthetic` fixture
(SW6's tests and STATUS pin four names).

**Reversal.** The written state is six lines in `confirm_two_lines`; a live-shaped dry-run
branch would let the drill run `execute_line` for both lines and delete them.

## SW9.5.1 — The sleeve's EOD NAV is computed from the book, and the peak is the evening's · ⚠ UNREVIEWED

**Context.** `07` says `03` §1's `sleeve_peak_inr` / `drawdown_pct` / `drawdown_locked` are
"written by `swing-eod` from the sleeve's EOD NAV — `portfolio_nav` is the source". Nothing in
`portfolio_nav_daily` describes this sleeve: that table values a user's portfolios from broker
holdings, and the swing book is a sub-set of one broker account that the weekly book also
trades in. There is no swing NAV to read.

**Choice.** `tasks/swing.py::sleeve_nav` computes it from the book itself, for the book the
ladder reads (PACK.6): `sleeve_capital_inr` + Σ `pnl_inr` of `CLOSED` positions closed on or
before the session + Σ `(mark − entry_avg) × quantity_open` of `OPEN`/`PARTIAL` positions (the
mark is the latest `ohlcv_daily.close` on or before the session, the entry when there is none)
+ Σ `(price − entry_avg) × quantity` over the `SELL` fills of those still-open positions (a
partial's realised half, which no column carries until the position closes). Bounded by date
like the closes the ladder reads (house rule 5). `sleeve_drawdown` raises the stored peak to
tonight's NAV if higher — a null peak (the first evening) *is* tonight's NAV, so the first
session is never locked; a peak of ₹0 or less divides nothing. Two consequences are decided
with it: (a) the peak and the drawdown are written to `sw_config` **without** audit rows (they
move most evenings; the day's market row is their history) while `drawdown_locked` **is**
audited (a decision, with the NAV and the peak in the note); (b) the night the ladder switches
books — the execution flag flipped since the previous settlement, read off
`detail.closed_trades_read` — the peak starts over at that night's NAV, because a paper peak is
not a level the real book has ever been at and a lock-out inherited from paper profits would
stop the real book on its first day. The detection job's `write_market_row` computes the same
measurement as a preview (as it does the rung) and writes nothing back; the evening's
`settle_ladder` is authoritative.

**Known edge.** Lowering `sleeve_capital_inr` reads as a drawdown of that size (the NAV falls,
the peak does not). Conservative — it stops entries, never adds risk — and the reset is the
same one the book switch uses. Not automated: a capital change is a person's decision, and
whether it was a withdrawal or a correction is not something the row says.

**Rejected.** Reading `portfolio_nav_daily` (not this sleeve). Storing a per-book peak (a second
column for a case a reset handles). Auditing every evening's peak (the audit is a history of
decisions, not a log of runs — SW8's own rule for the rung).

**Reversal.** `sleeve_nav` is one function; `reset_peak` is one keyword.

## SW9.5.2 — An unmeasured ADR is a refused stop, and the evening reads the latest detection row · ⚠ UNREVIEWED

**Context.** `widest_stop_pct(adr_pct)` is now what sizes a stop, and the evening's
`watch_items` took a watched name's ADR from **today's** `sw_setup_daily` row only — a flag
detected on Monday and not re-detected on Wednesday (a tightness or volume edge) carried
`adr_pct = 0` into the plan, which used to mean "trail the 20-day" and now means "no stop
admitted" (`STOP_TOO_WIDE`). A `MANUAL` row with no detection behind it has no ADR at all.

**Choice.** `watch_items` reads each name's **latest** detection row on or before the session
(today's when there is one) — the rule the monitor's `SignalContext.detected` already uses — so
a watched flag keeps its measured ADR across the sessions it is watched. A name the detectors
have never seen stays at 0 and is refused: the rule is "not wider than the ADR", and a stop
nobody can measure against the range is not shown to be inside it. The refusal names itself
(`SIZE_REFUSED / STOP_TOO_WIDE`) rather than being waved through at the 10% cap.

**Rejected.** Falling back to the 10% cap when the ADR is unknown (weakens the rule exactly for
the rows a person typed by hand). Computing the ADR from bars for every watch item (the right
long-term answer — the ADR is a property of the bars, not of the detector — and one more
query the evening does not need tonight; noted for SW11 with the manual-row form).

**Reversal.** One query in `watch_items`.

## SW9.5.3 — The worker hands the plan the trader's three sizing knobs · ⚠ UNREVIEWED

**Context.** `07`'s "the plan takes `min(rung, sizing.max_open_positions)`" is only true if the
plan is given the trader's number. `load_swing_config` applied only the three liquidity floors
from `sw_config`; `risk_per_trade_pct`, `max_position_pct` and `max_open_positions` — `03` §1's
settings, PACK.5's "risk knobs *are* settings" — never reached `SizingConfig`, so the evening,
the morning and the backtest runner sized every plan with the pack's defaults whatever the row
said. Harmless so far only because MD2 chose the default (0.5%) and no one had raised the cap.

**Choice.** `load_swing_config` applies the three (each already validated against its ceiling
where it was written) to `SizingConfig` beside the floors. The desk's monitor (`swing_monitor.
load_config`, SW6's file) still applies the floors only; MD6 has SW10 re-deriving a SIGNAL
line's size at confirm time from `sw_config`, which is where the desk's copy belongs.

**Rejected.** Leaving it (a setting the form accepts, audits and shows would size nothing).

**Reversal.** Three keywords in one `replace`.

## SW9.5.4 — Two defaults move in the migration, for rows nobody set · ⚠ UNREVIEWED

**Context.** `sw_config.max_open_positions` defaulted to 8 (the old top rung) and `adr_min_pct`
to 3.50; the engine's defaults are now 10 and 4.0. A row seeded at the old defaults and never
touched would keep planning against numbers the rules no longer mean.

**Choice.** `0030` moves the server defaults and updates rows sitting at **exactly** the old
default. `apply_patch` writes no audit row for an unchanged value, so a row at 8 or 3.50 is a
row nobody set — the seed's number — and a person who wants 3.5 back sets it, audited, in the
form. No dev or live database has a swing row today (STATUS SW0: the dev database is at 0026),
so in practice the UPDATE touches the test databases. The downgrade restores the defaults and
leaves the values.

**Rejected.** Leaving the rows (the evening would apply 3.5 while `04` says 4.0). Rewriting every
row (a person's 3.0 is theirs).

**Reversal.** The migration's downgrade.

## SW9.6.1 — The backtest reads the index in-frame, the runner picks the series once, and the run's row names it · Maulik, 2 Sep 2026 (STANDING-ANSWERS A12)

**Context.** MD14 / A12: "NIFTY 500 from `index_snapshot_daily` (NIFTY 50 fallback), 10/20
SMAs in-frame, no look-ahead." SW9.1's third choice had left the backtest breadth-only because
the bars frame carries no index and contract C3 fixed the signature; SW9.1 named the reversal —
"an `index: pl.DataFrame | None = None` keyword on `run_backtest`" — and this is it. Two things
still needed deciding: how the engine reads the series, and which series a run reads.

**The choice.**

1. **`run_backtest(..., index=(date, close) frame)`.** The engine sorts the closes, and for each
   session takes the last `index_ma_slow` closes dated **on or before** the session — the same
   reading `tasks/swing.py::load_index_reading` takes off the last twenty rows on or before the
   date — averages the last `index_ma_fast` and `index_ma_slow` of them, and hands
   `IndexReading` to `market_gate` exactly as the nightly job does. Fewer than `index_ma_slow`
   closes → `None`, the index is ignored, the gate is breadth's (`04` §8.2: "none → the index is
   ignored"). A close dated after the session is never in the window: the look-ahead test shifts
   the whole series one session later and asserts the gate changes on the boundary session only
   and on no earlier one. `None` for the whole argument keeps SW9's breadth-only gate, and the
   result's caveats gain :data:`INDEX_ABSENT_CAVEAT` so the page says so.
2. **The runner resolves one series for the whole window, at `params_for` time.** The nightly
   job picks per day (NIFTY 500 when it has twenty levels, NIFTY 50 otherwise); a run reads one
   series throughout, so `resolve_index_slug` picks the first of `nifty-500`, `nifty-50` with at
   least `index_ma_slow` levels between the lookback start and `end`, and `None` when neither
   has. The choice is made when the parameters are built rather than when the bars are loaded,
   so the row's `params` says which index the run read **from the moment the run is started** —
   a run that never finished still says what it was asked to read.
3. **`BacktestParams.index_slug`** is that label. The pure engine never reads it; it refuses a
   slug with no series (`index_slug` set, `index=None` → `ValueError`) so the stored parameters
   cannot claim an index the run did not have. The card shows it beside the other parameters.

**Rejected.** Reading the index per day from the database inside the runner (I/O per session,
and a per-day fallback would let one run read two benchmarks — the desk's rule for one night,
not a study's for nine years); putting the index closes into the bars frame as a pseudo-instrument
(the detectors would see it, and `test_the_frame_is_load_swing_bars_over_the_lookback_window`
is the runner's own claim that the bars are the detectors' bars); resolving the slug in
`finish_run` and rewriting `params` on the row (SW9.6's rule is that `params` is written on the
way in and never edited).

**Reversal.** `_IndexSeries.reading_on` is the reading; `resolve_index_slug` is the choice;
`index_slug` is one field with a default of `None`.

## SW9.6.2 — The backtest's drawdown is peak-to-trough as a percentage of the constant sleeve, and the lock-out is the live rule's · Maulik, 2 Sep 2026 (STANDING-ANSWERS A12) · ⚠ UNREVIEWED on the denominator

**Context.** A12: "Drawdown lock-out on the constant-sleeve equity curve (realised + open marked
at close), peak-to-trough as % of ₹10 lakh." `04` §8.5's live rule measures the sleeve against
its **peak** (`market.drawdown_pct`), because the live sleeve compounds. SW9.5 had the backtest
read the same formula off its own curve.

**The choice.** The backtest measures `(peak − equity) / sleeve × 100`, two decimals, against
the **constant sleeve** — A12's words — and hands that number to the same `drawdown_locked`
hysteresis and the same `exposure_tier` the evening uses, with `max_drawdown_pct` [15] and
`resume_drawdown_pct` [10] unchanged. The reason is SW9.1's: this sleeve never compounds, so
every trade risks 0.5 % of ₹10 lakh whatever the curve has made, and a lock-out "15 % below the
peak" on a curve that has doubled would be thirty *of the sleeve's* trades' risk at one point
of the run and sixty at another. Measured against the sleeve, 15 % is thirty trades' risk on
every session of the run — the number the rule means. On the fixture whose peak is the
untouched sleeve the two formulas agree to the paisa (the SW9.5 test's 0.56 / 0.54 / 0.23 %
are unchanged); they part only once the curve is above the sleeve, where the sleeve
denominator is the stricter one. `_sleeve_drawdown_pct` is the backtest's own three lines; the
live `market.drawdown_pct` is untouched. The result carries `DrawdownSummary` — the deepest
drawdown, the peak and trough it ran between, the trough's date, and how many sessions the
lock-out held — and the comparison carries each book's deepest drawdown per year.

**Rejected.** Keeping `market.drawdown_pct` (of the peak) for the backtest: it is the live
rule's literal formula, but it measures a compounding sleeve, and A12 named the denominator.
Reversible in one function if Maulik reads A12's "% of ₹10 lakh" as a description of the live
formula rather than a choice — hence the tag on the denominator only.

**Reversal.** `_sleeve_drawdown_pct` in `swing/backtest.py`; one substitution of
`drawdown_pct(peak=, equity=)`.

## SW9.6.3 — Three books over one detection pass: what "gate-off" means, what the columns are, and why the years are by entry · Maulik, 2 Sep 2026 (STANDING-ANSWERS A12) · ⚠ UNREVIEWED on the definitions

**Context.** A12: "Report gate-on vs gate-off per year and per setup, with breadth's and the
index rule's contributions labelled separately." "Gate-off" needed a precise meaning, the
comparison needed a shape, and it had to fit the speed budget (the speed test must stay under
60 s; SW9's 24 s was 97 % detection).

**The choice.**

1. **Three `GateMode`s, one detection pass.** `gate_off`: `market_gate` replaced by GREEN every
   session — **the ladder and the drawdown lock-out still apply**, because they are the trader's
   own results and A12 asks what the *tape's* gate buys, not what discipline buys.
   `breadth_only`: `market_gate(breadth, None)`, the gate SW9 shipped. `full`:
   `market_gate(breadth, index)`. Detection, breadth and the index reading happen once a session
   and every book takes its own gate, tier, fills and curve from them; the speed test measures
   **26.9 s** for three books against 27.5 s for one before the change — the per-trade loop is
   the noise SW9 said it was. `full` exists only when an index was supplied; the primary result
   (`trades`, `stats`, `by_year`, `equity_curve`, `funnel`, `ladder`, `drawdown`) is `full`'s
   when it exists and `breadth_only`'s otherwise, and `comparison.primary` says which.
2. **The columns.** Per book, over each scope: `entered`, `net_r`, `expectancy_r`,
   `win_rate_pct` and the curve's deepest drawdown inside the scope (`None` for a setup — the two
   setups share one curve). Scopes: the whole run, each year, each tradeable setup.
   **Breadth's contribution** = `breadth_only − gate_off`; **the index rule's** =
   `full − breadth_only` (absent without an index) — the same cell shape, as differences, so
   "what the gate buys" is read straight off the column: a negative `entered` is the entries the
   rule refused, its `net_r` is what those entries would have made or lost.
3. **The comparison's years are by the session *entered*;** `BacktestResult.by_year` stays by
   the close (`04` §11's statistics, SW9). The gate decides entries: a refusal in December
   belongs to December's tape, whatever January would have done with the position, and a
   per-year contribution keyed by the close would attribute a December refusal to the year the
   trade it prevented would have closed in. The page says so in its caption.
4. **"Gate-off never has fewer entries than gate-on"** is asserted on the fixtures, where it is
   a consequence of the definitions, and not claimed as a theorem: a book that took every entry
   can be fuller (`TIER_FULL`, `SESSION_CAP`) or locked out (its own losses) on a session where
   the gated book, spared, still enters. The test's docstring says so.

**Rejected.** Gate-off as "no gate, no ladder, no lock-out" (measures discipline and the tape
together; A12 asks for the tape's part); three separate `run_backtest` calls in the worker
(three detection passes, ~75 s in the speed test — over the budget — and the same numbers);
one table of contributions without the three columns (a reader should see the levels the
difference was taken between).

**Reversal.** `GateMode` is the list of books; `_Book._gate` is the three-line definition;
`_comparison` is the shape; `entry_date.year` → `exit_date.year` in `_comparison` moves the
comparison to close years.

## SW9.6.4 — Delisted names are sold on their last bar and counted DELISTED; the standing caveats gain B1's sentence; the card composes them · Maulik, 2 Sep 2026 (STANDING-ANSWERS B1–B5)

**Context.** B2: "Delisted names: sold at the last close, flagged `DELISTED` in the funnel."
SW9.4 had `NO_BAR` — the screener engine's five-session tolerance — for every name that stops
printing, delisted or merely suspended, and the runner already keeps names delisted on or after
`start`. B1: "where absent (pre-2020) assume no lock and **say so** on the page." B5: the
stored stats carry everything the page shows.

**The choice.**

1. **`run_backtest(..., delisted={instrument_id: delisted_on})`**, which the runner fills from
   `instrument.delisted_on` for every name in the frame. A held name with a known delisting is
   sold at its last close on the earlier of: the session it prints no bar (no tolerance — the
   name is known to be dead, not suspended), or its `delisted_on` itself when it printed a bar
   that day (whatever `manage` planned for tomorrow, there is no tomorrow). Close reason
   `DELISTED`, funnel `closed_delisted`. A name with **no** delisting known that stops printing
   is still `NO_BAR` after the tolerance — the two reasons now mean two different things, and
   `04` §11 names both. Using `delisted_on` on a session before that date is not look-ahead in
   any sense that matters: the price is the last close either way and no entry decision reads
   the map; only the exit date and the label move.
2. **`CAVEATS` is four sentences,** the fourth verbatim in `04` §11: "Where `upper_circuit` is
   absent no lock is assumed, so a name that was locked may have been entered here." No code
   changed for it — the detector's `_locked_expr` already treats a null band as no lock — and
   the test now shows all three cases (a band at the high, a band above it, no band).
3. **A result's caveats are its own** (`BacktestResult.caveats`): the standing four plus
   `INDEX_ABSENT_CAVEAT` when no index was supplied. The card keeps SW9.7's rule that the
   wording comes from the engine's constants, never the row — and reads one **fact** off the
   row, `comparison.index_supplied`, to decide whether the index-absent sentence applies. A row
   stored before SW9.6 has no `comparison`, had no index, and gets the sentence.
4. **The card carries** `max_drawdown_pct` at the top level, `drawdown`, and `comparison` with
   its numbers as `Decimal` and its modes restored to the engine's order (JSONB keeps none);
   the page draws the drawdown tiles and the two comparison tables and lists nothing new under
   "Other results". The Celery `summary` gains `index_slug`, `max_drawdown_pct` and the three
   books' `entered`/`net_r`.

**Rejected.** Treating every name's last bar in the frame as a forced exit without a map (a data
gap at the end of the frame would read as a delisting, and it *is* look-ahead: it reads the
absence of future bars); dropping `NO_BAR` (a suspension is not a delisting, and the screener
engine's rule is the one both backtests share); storing the caveats' wording on the row
(SW9.7's reason stands: a stale row must not paraphrase a caveat away).

**Reversal.** The `delisted` keyword with an empty default restores SW9.4 exactly; `CAVEATS[3]`
is one tuple element; `backtest_caveats` is the composition.

## SW13.1 — The desk ships as two compose services on the Baskfy box, the sleeve is set by the seed, and the gate proxies were fixed where they measured the wrong thing · ⚠ UNREVIEWED

**Context.** MD20: one system; the desk beside api/web/worker/beat, same Postgres, same token
store, every flag false until Maulik's hand. No AWS session in this leaf, so everything is
prepared and proven locally (`tools/deploy/smoke-local.sh`).

**Choices.** (1) `Dockerfile.desk` with the **repo root** as context and an allow-list
`.dockerignore`, since the desk imports three Baskfy packages and neither tree may leak `.env`,
`data/` or a token into a layer. (2) The token volume is **read-only** in the desk and
`KITE_API_SECRET` is blanked in compose: Baskfy is the only redeemer of the single-use
request_token (M74), the desk only reads. (3) The desk's swing flags come from
`.env.staging.compose` (compose defaults, `environment:` beats `env_file:`), api/worker's from
`.env.staging` — a live flip is two deliberate lines. (4) `baskfy_api.seed swing --capital --risk`
goes through `apply_patch` (ceilings, audit rows, quantised to the columns' scales so a re-run
audits nothing) — the deploy has no session token for `PATCH /swing/config`. (5) `compute.tf`
gained one ECR ARN despite "dns.tf only": without it the box cannot pull `baskfy-desk`, and the
Goal outranks the file list; reversible by deleting the line. (6) Gate G3's `tail -1` can never
show `Success` (terraform ends its output with a blank/ANSI-reset line, with or without colour),
so the gate's CHECK pipe was changed to `grep Success` — the artefact was right, the measurement
was not. (7) `desk.<host>` mirrors the `host` record; the retired desk box is untouched.

**Rejected.** A `DESK_DATABASE_URL` pointing at SQLite on a volume (D8 says the record migrates;
a second SQLite lineage is the fork problem again); mapping the api secret into the desk (two
redeemers, one token); a cron inside the desk container for the monitor (one process per
container, and a monitor that exists is a monitor that can be pointed at something — a separate
service can be stopped alone); staging the desk on 65.0.226.77 (MD18, superseded by MD20).

**Reversal.** Remove the two services and the `desk-data` volume from `compose.prod.yml`, the
`desk.` block from the Caddyfile, the `desk` record from `dns.tf` and the ARN from `compute.tf`;
`seed swing` without flags is unchanged.

## SW11B.1 — The catalyst feed links out, per symbol, fails soft, and amends Track C §7 · Maulik, 2 Sep 2026 (STANDING-ANSWERS A3; MD4) · ⚠ UNREVIEWED on four details

**Applied as decided (STANDING-ANSWERS A3):** NSE's free corporate-announcements and
event-calendar reads, through `NSEProvider` (cookie prime, the shared 1 req/s limiter,
archive-then-parse — one archived request per symbol per day), for the WATCHING names plus the
last session's EP rows; `sw_catalyst` (headline, stamp, URL, source, earnings date; unique on
the URL); `sw_watch.catalyst` auto-filled only while empty; `sw_watch.earnings_date` refreshed
every run; the API's two GETs and the three pages **link out** (`target=_blank rel=noopener`);
fail soft. **Track C §7 is amended under Maulik's name** in `02`: the one news source is the
exchange's own, through the existing provider, single-tenant own-use, links not text, nothing
redistributed.

**The four details decided here (⚠ UNREVIEWED; each cheap to reverse):**
1. *Headline* = NSE's subject line (`desc`) plus its one-line summary (`attchmntText`), capped
   at 160 characters (`nse.HEADLINE_MAX_CHARS`). Never the attachment. Rejected: the subject
   alone ("Updates") — unreadable on a watchlist.
2. *An announcement with no attachment URL is dropped* (counted in the note); a row with no
   stamp is kept and never "newest". Rejected: a synthetic URL to the filings page — two such
   rows would collide on the uniqueness key.
3. *The earnings flag is the nearest result meeting on or after the run*; it lives on one
   `NSE_EVENT_CALENDAR` row per name (linking to the exchange's calendar page) and is
   **deleted** when the calendar names none, so a stale date never shows. Rejected: keeping
   past dates as history — a flag is a reading, not a record.
4. *Per-symbol fetch and per-symbol fail-soft*: one refused name costs only itself; every name
   refused is a SUCCEEDED step with zero rows and the error in the note. `ProviderError` only
   is caught; anything else is a bug and raises. A 404 is absence (a renamed symbol), a 403 is
   loud. Reverse by catching more, or less, in `swing_catalyst.run_swing_catalyst`.

Read model: `catalyst_feed {headline, published_at, url, earnings_date}` on `SwingSetupOut` and
`SwingWatchOut` (null when the feed has nothing); `SwingWatchOut.earnings_date` beside it. No
new route (`test_swing_readonly` unchanged). Beat `swing-catalyst` 09:10 IST weekdays, compute
queue, a minute after the gap scan so the live-gap watch rows are on the list.

## SW11.1 — Five rules over five scrape-time gauges, evaluated by a subset evaluator, and four of them raised in-process too · Maulik, 2 Sep 2026 (STANDING-ANSWERS B8) · ⚠ UNREVIEWED on the mechanism

**Context.** `06` SW11 names three alerts; B8 adds two and says "spans optional and unable to
raise into the order path". The desk, the worker and the API are three processes; a counter in
one of them is not "is a position naked now".

**The choice.** The facts are rows, read by `baskfy_api/swing_health.py` (one indexed query
each, all users) and set on gauges by `refresh_swing_metrics` on every `/metrics` scrape — M20's
database-derived pattern, so the rules read the same numbers whichever process died. The
rules are written in a deliberately small PromQL grammar (a gauge comparison, `and on()`, an
IST window as UTC minutes-of-day, a weekday guard) so `test_swing_alerts.py` can evaluate each
from a synthetic series without `promtool` (not on the locked stack, house rule 1). The four
time-of-day rules are also raised in-process by `tasks/swing_ops.py` at their moment, through
`alerts.dispatch` — the `publish_late` pattern, so a box with no Prometheus still gets an email.
`SWING_POSITION_NAKED` is Prometheus-only: a condition held for ten minutes is a time-series
question. The rule names are upper-case `AlertName` members, verbatim from the swing docs.

**Rejected.** A worker Beat task setting gauges (prefork children each own a registry; the
exposition would show whichever child ran last). A push gateway (not on the stack). Wider
PromQL (`rate`, `absent`) that the test could not evaluate — a rule nobody has evaluated is
the failure B8 exists to prevent.

**Reversal.** The gauges are five `Gauge`s and one refresh; the evaluator is forty lines in one
test file; the worker checks are four functions on one query each.

## SW11.2 — The monitor process is the desk's clock; the strategy still holds no gateway · Maulik, 2 Sep 2026 (STANDING-ANSWERS A8) · ⚠ UNREVIEWED on the process boundary

**Context.** SW10.5 left the 10:45 cutoff and the 15:15 sweep as a route and a hook: both are
order-shaped (a cancel, a GTT), so a Beat entry cannot run them (law 2) and only the desk can.
SW6.4 built the monitor "with no gateway at all".

**The choice.** `app/swing_clock.py`: after `run_until_close` the same process runs the cutoff
at once and sleeps to `gtt_sweep_at` [15:15] for the sweep, building the swing gateway only
when each chore starts and dropping it after; the strategy object never sees one, and the
runner's source scan (no placing verb, `gateway=None`) is unchanged. `sw_session.monitor_ran`
is written on the **first tick the strategy handles** (pushed by the ticker or polled by the
fallback; `signals = 0`, rewritten at 10:45) — and at once when the watchlist is empty, since
a monitor with nothing to watch still ran and the clock still owes the day its sweep — because
`SWING_MONITOR_DID_NOT_START` reads it at 09:20: the old write at 10:45 would have fired the
rule on every healthy morning, and a write at launch would have silenced it for a process
whose token died before the first tick. Each chore is a line on `sw_session.notes`; no
column, no migration (0032 is SW11B's).

**Rejected.** A cron `POST /swing/cutoff` (needs the desk password and an Origin through
`websec`); a Beat task that re-arms (law 2); a `gtt_sweep_ran` column (a migration for a
marker the alert does not need — a naked position after 15:20 is the alert whether or not the
sweep ran).

**Reversal.** `run_after_close` is one call at the end of `swing_monitor.main`; the notes
marker is one method on the store.

## SW11.3 — The opening range is the ticks inside the window; the candle reconciles once, a minute later · Maulik, 2 Sep 2026 (STANDING-ANSWERS A4) · ⚠ UNREVIEWED on the tick at the window's edge

**The choice.** `SwingBreakout` keeps the tick high/low over `[open, open + window)` — half-open,
the same rule `opening_range` reads off candles — and closes the range at the first tick at or
after the window's end; `minute_candles` is asked **once**, at `range_reconcile_delay_minutes`
[1] after, and a differing candle range replaces the tick range for the verdicts that follow
(a signal already raised stands: it is a row and a line, and the confirm re-reads). A name that
triggered is never asked for. Both delays are `OpeningRangeConfig` fields (B13), named in `04`
§7.4. The fixture replay raises exactly its four signals and asks for candles once per name.
**The one semantic change:** a tick stamped exactly 09:20:00 is outside a 5-minute window (it
is the 09:20 candle's), where SW6's fallback counted it; one strategy test whose 09:20 tick sat
on that edge now ticks inside the window (`tests/test_swing_monitor.py`, the comment says so).

**Rejected.** Waiting for the candle before any verdict (Zerodha: the historical API "was
never built for polling during market hours"); polling candles on every tick until they exist
(SW6's loop, up to sixty asks a minute per name).

**Reversal.** `_tick_range` / `_reconcile` are two methods; set `range_reconcile_delay_minutes`
to 0 to reconcile at the first tick after the window.

## SW11.4 — The gap scan runs at 09:16 and reads `ohlc.open`; the pace stays the quote's volume · Maulik, 2 Sep 2026 (STANDING-ANSWERS A4; MD5) · ⚠ UNREVIEWED on the pace's source

**The choice.** `swing-premarket-gaps` moves to 09:16 (MD5, defensively) and `evaluate_gaps`
measures the gap from `QuoteRecord.open` when the quote carries a positive one, else the last
print. **The split A4 records:** the volume pace is the quote's `volume` over the minutes since
09:00 (SW6.1's clock; sixteen minutes of 375 at 09:16) — at 09:16 that is the pre-open match
plus a minute of trading, and the desk's ticker carries the same number as `volume_traded`
from then on, so the monitor does not re-read it: the watch row is the decision. The S2 probe
(`baskfy.swing.timing_probe`, Beat 09:04, `BASKFY_SWING_TIMING_PROBE`, self-disabling by a
`.done` file in `BASKFY_SWING_TIMING_PROBE_DIR`) is what settles whether 09:09 was ever usable;
its report says which line to move back. It holds one of the compute worker's two slots for
~18 minutes on the one morning it runs. The catalyst Beat (SW11B, 09:10, "a minute after the
gap scan") now precedes the scan — the driver's line to move.

**Reversal.** One Beat line; `gap_price` is one function.

## SW11.5 — The notifier tells and cannot act; a span cannot turn a 400 into a 500 · Maulik, 2 Sep 2026 (STANDING-ANSWERS A2, B8) · ⚠ UNREVIEWED on the SMTP knobs

**The choice.** `app/swing_notify.py` is a stdlib `smtplib` sender (`DESK_SMTP_*`,
`DESK_NOTIFY_FROM/TO` in `app/config.py`; the desk cannot import `baskfy_api.email`) and a
Telegram `sendMessage` sender constructed only when both `BASKFY_SWING_TELEGRAM_*` are set —
no `getUpdates`, no webhook, no command handler; the message says "this message tells; it
cannot act". Only `sw_watch.focus` names are pushed (A14); the notice carries the line or the
skip reason; a channel that raises is logged and the plan is still written. Telemetry: the
desk's M20 `span` closed its scope with a `yield None` inside `except`, which turned a body's
`HTTPException(409)` into `RuntimeError: generator didn't stop after throw()` once a tracer was
installed — latent, since no box has a tracer; rewritten to close the scope on the body's own
exception and re-raise it, with a test. Every metric/span call on the order path is wrapped at
the call site and tested with a sink that raises.

**Reversal.** `Notifier` is the one call site in `PgSignalStore.raise_signal`; the SMTP knobs
are seven lines of config.

## SW11.6 — Budgets measured honestly, and the journal reads REAL · Maulik, 2 Sep 2026 (STANDING-ANSWERS B9, A10)

`/swing/setups` p95 is measured with 2,500 detection rows on the day (the route's worst case,
183 ms; a real morning lines a few dozen); detect over 2,500 × 200 is the engine only (0.21 s),
labelled like `compute_factors`; tick→verdict and the confirm path are the desk suite's,
recorded through `benchmarks.budgets` off the path with `gate="none"` because `pytest -m
benchmark` here cannot run them. `GET /swing/journal` `reads` is `REAL` always (A10; the
`SIMULATED` literal stays in the wire type, nothing writes it). `write_market_row` keeps a row
`settled_by: swing-eod` and bounds closes by date (SW8.1's three lines, now written).

## SW12.1 — The swing goldens are flat, stable and self-describing; the mutation report is the harness's · Maulik, 2 Sep 2026 (STANDING-ANSWERS B11) · ⚠ UNREVIEWED on the layout

**Context.** B11 asks for goldens of the six pure functions in `go/testdata/golden/L1/swing/`,
byte-stable across two runs, and a re-run of the mutation harness with every survivor named.
`tools/parity/golden.py`'s `dump()` stamped every file with the wall clock, the interpreter
and the cwd, so no golden it wrote could be byte-stable; and its polars branch went through
`to_pandas()` (absent here — no pyarrow) or `to_dicts()`, which drops the dtypes and turns a
`Date` into a timestamp.

**The choice.** (1) The lane lives in `golden.py` itself (`python golden.py swing`), not a
`dump_L1.py`, because the six functions share one fixture set and one decode/recompute table
that the drift test must import too. (2) Files are **flat**: `L1/swing/<function>.case_NNN.json`
— docs/go-rewrite/03's `L<n>/<module>/<case>` with `swing` as the module — rather than one
directory per function; one directory the Go lane globs and a shell checksums. (3) A
`stable=True` document has no `dumped_at` / `python` / `cwd`; the meta says `stable: true`
instead. (4) A polars frame is encoded natively — `{"columns", "dtypes": polars names, "rows"}`
— with dates as ISO days and nulls as null; the pandas path is untouched. (5) `detect_setups`
goldens carry the **raw adjusted bars** the worker hands the detectors plus the whole
`SwingConfig`; the function under test is `detect_setups(with_swing_indicators(bars, config),
as_of, config)`, and the notes on every file say so, with the price convention (house rule 6).
(6) `build_entries`' tuple is stored as `{"lines", "skipped"}`. (7) The dumper round-trips each
case's inputs through JSON *before* computing, so the stored output is the output of exactly
the stored inputs. (8) `packages/core/tests/test_swing_goldens.py` re-renders every file from its
stored inputs through the live function and compares the text; drift fails there first.

**Rejected.** Nested `<function>/case_NNN.json` (what the leaf brief literally said — one level
deeper than 03's convention, and a `shasum dir/*` sees no files); a `dumped_at` kept and
stripped by the test (a golden that is not what the file says is not a golden); storing the
indicated frame instead of the bars (twice the size, and the Go lane needs the indicators
ported anyway).

**Reversal.** Move the files and change `swing_case_path`; the test globs whatever is there.
Regenerating is `uv run python ../tools/parity/golden.py swing` from `decile-blueprint` and one
commit that says why.

**The mutation re-run (same decision, second half).** Two things the harness itself needed:
(a) `SWING_TEST_SELECTION` gains `test_swing_pending_and_first_live.py` and
`test_swing_safety_properties.py` — written at SW10/SW10.5, after the list, so every mutant of
`first_live_multiplier`, `sizing_at`, `marketable_limit` and the `PENDING_RANGE` line had
"survived" against tests that were never run; (b) `04` §4 gains the parabolic **score** the
engine has computed since SW1 (`40 × clamp(ret_5/100) + 30 × clamp(dist/(8 × ADR)) + 30 ×
clamp(streak/6)`), because fourteen survivors sat in a formula the document did not state and
house rule 2 forbids a test that asserts the code — the sentence is the code's, written down,
⚠ UNREVIEWED as a spec addition. Every remaining survivor is justified **by name** in
`reconciliation/mutant-justifications.json` (rendered into `MUTANTS.md`): `slots=True` flips,
`maintain_order`, double guards, unreachable branches, and float-equality boundaries between
two measurements. The tick-snap observation the goldens surfaced is `QUESTIONS.md` Q-SW12-1,
not fixed (the goldens dump the code as it is).

## SW14.1 — The hub's five actions, a symbol on the wire, and two GET-only reads · Maulik, 2 Sep 2026 (STANDING-ANSWERS A14) · ⚠ UNREVIEWED on three details

**Context.** `05` §2 names four server actions (`watchAdd`, `watchDismiss`, `watchAnnotate`,
`settingsSave`); A14 added the "Still watching" control after that sentence was written. The
add-manual form's lookup is `GET /search`, whose instrument hit carries the **symbol** and not
the id `POST /swing/watch` wanted. The Setups header needs the drawdown and the index averages,
and the watchlist needs yesterday's `sw_signal` rows — neither was on the wire.

**Choices.** (1) **`watchReconfirm` is the fifth action** and `05` §2's sentence names it; the
read-only assertion is an exact set of five, over `(app)/swing` and `(app)/me/swing`, each a
`(prev, FormData) → {ok, error?}` that makes one call through `lib/swing/write.ts` (a closed path
type, no pattern) and never reads a token itself. (2) **`POST /swing/watch` accepts `symbol`** as
an alternative to `instrument_id` (exactly one, `extra="forbid"` kept), resolved server-side,
upper-cased; the action posts the symbol as typed, so a name that is not listed is a 404 the
form renders, and no second call translates one into the other. (3) **`GET /swing/signals`**
(`date`, `instrument_id`; the newest session when no date) and **`drawdown_pct` /
`drawdown_locked` on `GET /swing/market`'s row** — reads only; the API's swing surface is ten
paths, still four writes. (4) Two display numbers on the pages — `RESUME_DRAWDOWN_PCT` (10) in
`swing/copy.ts` beside the Market page's existing `04` §8.3 thresholds, and A14's 20/5 on the
watchlist header — are copy, not decisions; the decision (`drawdown_locked`, `focus`) is read
from the row. (5) Settings sit at **`/me/swing`**, reached from the user menu's Account group
and a fourth Me tab; the profile page itself is not edited.

**Unreviewed.** The symbol on the wire (a public-contract widening); the mini chart's cost —
one `bars` call per row, in parallel, capped at 24 rows by score; the trail distance on the
Positions page reads each open position's own bars (one call per position).

**Reversal.** Delete `symbol` from `SwingWatchIn` and the form's action resolves through a
second read; delete the signals route and the watchlist loses the line under the row; the
five-action set is one list in one test.

## Maulik's decisions, 2 Sep 2026 (taken in conversation; not ⚠ UNREVIEWED)

Recorded verbatim from the review session so the run and the report build on them.

| # | Decision | Where it lands |
|---|---|---|
| MD1 | **Sleeve capital ₹25,00,000.** `sw_config.sleeve_capital_inr` for the sole user; the seed keeps ₹0 (PACK) and the value is set through `PATCH /swing/config` in the first-morning steps | SW12 report, `RUN-AND-TEST.md` |
| MD2 | **Risk per trade 0.5 %** (the default; ceiling 1.0 % kept, PACK.9) | unchanged code |
| MD3 | **Notification: email through the existing mailer, one-way**, plus a **Telegram sender scaffolded dark** behind `BASKFY_SWING_TELEGRAM_BOT_TOKEN` / `BASKFY_SWING_TELEGRAM_CHAT_ID` — **never a confirm path**; a notification can only tell, never act | SW11 (`PgSignalStore.raise_signal` → notifier) |
| MD4 | **Catalyst feed: free NSE corporate announcements**, for watchlist and EP-candidate symbols only, through the existing NSE provider and its rate limiter; store headline + timestamp + filing URL; auto-fill `sw_watch.catalyst`; link out rather than reproduce the text; add an **earnings-date flag** from the results calendar; single-tenant own-use — recorded as a **Track C §7 amendment** (news is not redistributed; it is a link on one person's watchlist) | SW11B (new module before SW12) |
| MD5 | **S2 timing probe:** `tools/swing/kite_timing_probe.py` as a one-shot Beat task at 09:04 IST on weekdays, gated by `BASKFY_SWING_TIMING_PROBE=true`, self-disabling after one good run, writing `docs/swing/status/S2-kite-timing.md`; Maulik logs in to Kite before 09:00. Meanwhile the gap scan moves to **09:16** defensively and the opening range becomes **tick-based** (the candle source stays as the cross-check) | SW11 |
| MD6 | **SIGNAL sizing is fixed at confirm time, in SW10:** `POST /swing/execute` re-derives the account context under a row lock on the day's `sw_session` (open exposure at cost + every line already CONFIRMED/SENT/FILLED today + cash), re-sizes the line against the rung's exposure ceiling, the position count and the per-session entry cap, and refuses with `EXPOSURE_FULL` / `TIER_FULL` / `SESSION_CAP` instead of sending; a SIGNAL plan's size is a preview, the confirm path is the gate; the monitor also re-reads context per trigger so the page shows the size that will be sent; a property test over sequences of confirms; the drill must show 25 %, not 34 % | SW10 (after SW9.5) |
| MD7 | **SW9.5 first.** `07-primary-source-corrections.md` is executed as its own commit before SW10 is finished, because it changes numbers SW10's tests pin (stop ≤ 1 ADR, 3 new entries a session, `min(rung, cap)` positions, the 15 %/10 % drawdown lock-out, the 10-day-over-20-day index rule) | SW9.5 |
| MD8 | **Execution flag.** Maulik's answer, verbatim: *"Okay, potato would be required. We'll go live right away."* The run reads this as intent to go live as soon as possible. **It changes nothing in this run:** `BASKFY_SWING_EXECUTION_ENABLED` stays false (kickoff constraint; charter: a Track B flag flip is never autonomous), and `docs/swing/02` §3 still gates the flip on its checklist — twenty paper sessions, the backtest reviewed, the sleeve and risk written here (MD1/MD2 — done), first sessions at half risk. Only Maulik can amend §3 and only his hand flips the flag in the desk `.env`. The word "potato" is not understood and is asked about in the next review batch | NEEDS-MAULIK, SW12 report |
| MD9 | The ⚠ UNREVIEWED entries are reviewed **behavioural ones first, in conversation**; the rest stay tagged for asynchronous review | this file |
| MD8′ | **Clarified:** "potato" was a typo. Maulik's decision: **no paper period is required; the book goes live as soon as the run is done.** What this changes: `docs/swing/02` §3's twenty-session gate becomes *advisory* (the counter stays on the page and in the report as information, not as a lock), amended in §3 under his name in SW12. What it does **not** change: the flag stays false for the whole run and every SW10 proof keeps asserting it; no session of this run places an order; the flip is Maulik's hand in the desk `.env` after the run, and the first five live sessions still run at half risk (MD12). The run records, in the report, that the code has never seen a live Kite morning (S2) — the risk of going live without one is his to take and is stated plainly | 02 §3 (SW12), NEEDS-MAULIK, SW12 report |
| MD10 | **SW6.2 amended — `PENDING_RANGE` lines.** A live gap keeps no stop, but the MORNING plan shows it as a `PENDING_RANGE` line: no quantity, no stop, not confirmable, sorted by score with the others, **reserving one of the session's new-entry slots** so a lower-scored flag cannot crowd it out; an information-only preview ("≈ N shares if the stop lands 1 ADR below; locked-circuit: no fill") that is never sent. It becomes a real line only through the SIGNAL plan at range close with `stop = min(range low, LOD)`; a stop wider than 1 ADR skips it (`STOP_TOO_WIDE`) and frees the slot; a reserved slot that never triggers is freed at 10:45. Tests: a `PENDING_RANGE` line can never reach `/swing/execute`; the slot is freed by 10:45 | SW10.5 |
| MD11 | **SW7.1 amended — the fill gap closed.** The buy is a marketable LIMIT at `min(trigger × 1.005, range_high + 0.25 × ADR)`, never MARKET; the request polls the order for up to 10 s (orders endpoint, ≤ 2 req/s): COMPLETE → GTT for the filled quantity + `sw_position` in the same request; OPEN/partial → `SENT` with the order id and the quantity filled so far, a GTT for that quantity now if > 0. Later fills are reconciled by the desk's `on_order_update` postback handler: each fill **modifies** the GTT quantity (never a second GTT); protection is never withheld because the stop distance grew past 1 ADR (the rule bounds entries, not protection). At 10:45 any open remainder is cancelled; at 15:15 an EOD sweep asserts no filled quantity is without a GTT (`SWING_POSITION_NAKED`) and re-arms. Tests: partial-then-complete ends with one GTT covering exactly the filled quantity; a cancelled remainder leaves the GTT untouched; a DRY_RUN fill follows the same path with `simulated=true` | SW10.5 (desk), SW11 (sweep + alert) |
| MD12 | **SW7.2 amended — half risk at plan time, countdown persisted.** `risk_multiplier = 0.5` applies to `risk_per_trade_pct` before `size_position`, so the line shown is the line sent and every refusal sees the real size; SELL/RAISE quantities are never touched; `first_live_sessions_left` lives in `sw_config`, decremented once when a LIVE `sw_session` closes (never by a request; a restart changes nothing); the plan header says "first live sessions: N left · risk 0.25 %"; the journal tags those trades. Tests: the fifth session decrements to 0 and the sixth plans at full risk; a restart mid-countdown does not reset it; a SELL line's quantity is identical with and without the multiplier | SW10.5 |
| MD13 | **SW8.1 amended — the ladder reads real closes from day one** (STANDING-ANSWERS A10): no paper book exists, PACK.6's paper clause is void, the rung starts at 0; one idempotent, date-bound settlement at 21:05 after `manage` and fill reconciliation; the 09:09 job runs it only as a catch-up when no record exists for the previous session | SW10.5 |
| MD14 | **SW9.1 amended — constant ₹10 lakh sleeve + the index rule** (A12): NIFTY 500 from `index_snapshot_daily` (NIFTY 50 fallback), 10/20 SMAs in-frame with no look-ahead; the drawdown lock-out on the constant-sleeve curve; gate-on vs gate-off reported per year and per setup with breadth's and the index rule's contributions labelled separately | SW9.6 |
| MD15 | **SW9.2 / SW9.3 kept** (A13) | — |
| MD16 | **SW5.1 amended — the watch funnel** (A14): top 20 flags by score + every EP auto-watched; the monitor watches all; daily focus = top 5 + every EP → push + top of page; the rest below the fold, never pushed; MANUAL rows expire after 10 sessions unless re-confirmed | SW10.5 |
| MD17 | **Process** (STANDING-ANSWERS header + §C): read `STANDING-ANSWERS.md` before any question; apply it and cite "(STANDING-ANSWERS §n)"; otherwise the pack default, decide-record-continue; ask only for a Kite credential, a data-losing schema change, the weekly book / R1–R4, or a Track C boundary; everything else → `docs/swing/QUESTIONS.md` with the recommendation applied, ⚠ UNREVIEWED | this run |
| MD18 | **Deployment (Maulik, 2 Sep, 23:00).** No local run. When the code is complete: migrate, commit, deploy to AWS. Baskfy side (api/web/worker/beat) to staging.baskfy.com via the existing ECR + SSM path — after Maulik's `aws sso login` (asked only once the code is done). **Desk half to the desk box (65.0.226.77) now, before Friday's rebalance** — his call over the standing rail, taken with: a verified `python -m scripts.backup` + dated copy first, the desk suite green on the box before the restart, `DRY_RUN=true` and every swing flag false in the box's `.env`, and a rollback (`git checkout <previous sha>` + restart) written into the deploy leaf | SW13 (deploy) |
| MD19 | **Documentation minimised** from here: STATUS/DECISIONS entries a few lines each; the final report short; tokens go to code | this run |
| MD20 | **One system: Baskfy.** (Maulik, 2 Sep, 23:10.) The desk box at 65.0.226.77 is not a deploy target and will not rebalance; the merged desk — weekly book and swing — runs as a service on the Baskfy box beside api/web/worker/beat, on the same Postgres and the same Kite token store, `DRY_RUN=true` and every swing flag false until his hand flips them. Supersedes MD18's desk-box clause | SW13 (deploy) |

## SW15.1 — A provisional bar is a reading of `04` §1 for a day in progress; one in flight, one a minute; the desk writes the row and the sweep publishes it · Maulik, 3 Sep 2026 ("the scan anytime") · ⚠ UNREVIEWED on the three rules

**Context.** `04` §1–§4 define the universe and the detectors over daily bars; `06` SW3 runs
them at 21:00 on published closes. Maulik asked for the scan on demand, intraday, from Kite.

**Choices.** (1) **A provisional bar.** Between 09:15 and 15:30 IST on a trading day, today's
bar is built per liquid name from the live quote — `ohlc` for open/high/low, `last_price` for
the close, the session's volume so far, turnover = close × volume, the day's circuit band, the
last published bar's `adj_factor` — and the detectors run unchanged over it. Every row written
carries `provisional=true`, the page says "provisional — scanned 13:42 IST from live quotes",
and the nightly replaces the rows (same keys flip to false, the rest are deleted). A half-day's
volume and a base that can still widen are labelled, not hidden. The universe is `04` §1 **as
of the last close** (`liquid_universe`), so a name that became liquid today is not quoted —
cheap to reverse (quote the register instead) and the honest reading. Outside those hours the
scan is the last published session re-run, plain. (2) **One in flight per user (409) and one a
minute (429)**, answered from `sw_scan_run` rather than the API's Redis limiter, so the rules
hold with no cache and are testable against the database; a `RUNNING` row older than ten
minutes no longer blocks (a dead worker must not lock the button). Both numbers are settings
(`swing_scan_min_interval_seconds`, `swing_scan_stale_after_seconds`; the desk reads the same
env names). (3) **The desk writes the row, the worker sweeps.** The desk venv has no Celery
client and reaches Baskfy through Postgres alone, so its button inserts a `QUEUED` row and
`baskfy.swing.scan_sweep` (Beat, every minute, one indexed SELECT) publishes it — the same
fallback the API takes when its broker is down. Rejected: hand-rolling Celery's wire format
over `redis-py` (brittle), and a bearer from the desk to the API (the desk has none). (4) The
funnel is now written to `sw_market_daily.detail.funnel` on every run — `GET /swing/setups`
had been reading a key nothing wrote.

**Reversal.** `provisional` defaults false; delete the two columns and the run table
(0033's downgrade removes the provisional rows with the flag) and the nightly is exactly what
it was. The sweep is one Beat entry.

## SW16.1 — The writer refuses a 40 % day-on-day move; the repair runs NSE directly and ascending; one `run --rm` with bind-mounted files is not a deploy · 3 Sep 2026 (leaf 1.7.1) · ⚠ UNREVIEWED

**Context.** The 1/14-scale index rows were the fixture builder's random walk seeded into the
development database and copied to staging (STATUS SW16). Nothing in `store_snapshots` noticed
NIFTY 50 falling 96 % overnight.

**Choices.** (1) **The guard lives in the writer, not the parser.** The parser was innocent, and
a guard on the shared upsert catches every path — nightly, reference backfill, repair. The bound
is 40 % against the last stored level within 14 days: no NSE index has moved that in a session,
the fixture moved 96 %, and India VIX's worst day is inside it. A refused row is named in the step
notes and dropped; the file's other rows still land; a first-ever row is accepted. The guard
compares against what is *stored*, so it also refuses a correct value next to a wrong anchor —
which is what it did for `nifty-consumer-services`, whose stored history is another index's
(M31). That is the intended failure: loud, named, unwritten. (2) **The repair uses the NSE
provider directly**, not `build_provider_stack()`: the composite's fallback for
`index_snapshots` is the FixtureProvider, which is how these rows came to exist. Ascending, one
commit per day, so each repaired day anchors the next; the report prints the anchor before the
window so an operator sees a window that starts inside the corruption. A dry run therefore proves
day one and the guard, not the whole window (recorded in the test). (3) **Run on the box by
bind-mounting the three changed files over the image for one `run --rm`.** The image is built
from a committed sha and the leaf must not commit; waiting for a deploy would have left the swing
gate reading an artefact for another cycle. The services were not touched; the files sit in
`/opt/baskfy/sw16/` with checksums matching the tree.

**Rejected.** A guard in the NSE parser (does not cover the seeder or a copy); comparing against
the file's own `Points Change` (the fixture is self-consistent too); deleting the corrupt window
before re-fetching (the guard then has no anchor and would accept a second bad file).

**Reverse.** Delete `_refuse_implausible_levels` and its call; `index_repair.py` and the Makefile
target stand alone. The 12-slug fixture rows still on the box are listed in STATUS SW16 (a) with
their predicate.

| MD21 | **The gate's index is the NIFTY MidSmallcap 400, and the liquidity floor is ₹50 lakh.** (Maulik, 3 Sep.) `swing_index_slug` defaults to `nifty-mid-small-400` in both settings modules and in `run_detect_swing` (fallback `nifty-500`, then the reading is absent rather than wrong); his book trades mid- and small-caps, and a large-cap index says nothing about that tape. `sw_config.turnover_min_inr` on the box is ₹50,00,000 (audited change, 3 Sep) — the pack's ₹5 cr stays the code default because the floor is a per-book setting, not a rule. Effect measured the same evening: 402 → **605** liquid names, 9 → **13** candidates, and the 2 Sep gate RED (Nifty 500, close below both MAs) → **GREEN** (MidSmallcap 400, 10-day above 20-day, breadth 14.7 %) | SW17 |

## SW18.1 — The link, never the credentials · Maulik, 3 Sep 2026 (decided in conversation)

**Context.** Kite invalidates the access token at every trading day's pre-open. Something has to
produce a new one before 09:15 or the 09:14 monitor idles and no plan line can be sent. The
obvious automation is to store the Zerodha password and the TOTP seed on the box and have a job
type them.

**Decision.** Do not. At 08:45 IST on weekdays a job checks whether a usable token exists for
that day and, if not, sends Maulik one message carrying the Kite login link. He taps it, logs in
on Zerodha's own page, and the callback that already exists stores the token. A second check at
09:05 sends the second and last message. `BASKFY_KITE_LOGIN_NUDGE_ENABLED` (default false) and
`BASKFY_KITE_LOGIN_NUDGE_TO`.

**Why not the credentials.** Two reasons, either sufficient. Zerodha's terms put the login
credentials with the account holder and a stored TOTP seed defeats the second factor it exists to
be — an automated login is not a thing they sanction. And the blast radius: password + seed on a
disk is the entire trading account, transferable by one `cat`, in a repo whose own rails already
say "never print, log, or commit secrets". The nudge's worst case is a wasted tap; the seed's
worst case is the account. The reversible option, and the stricter security boundary when nothing
in force changes — the two tie-breaks the root charter names.

**Rejected alternatives.** (1) *A headless browser driving the login with stored credentials* —
the blast radius above, plus a scraper against a broker's login page. (2) *A push notification
with a confirm button* — STANDING-ANSWERS A2 forbids a channel that can act; a tap must never
place an order, and a login link cannot. (3) *A second login endpoint on the worker* — a second
minter of the `state` the callback validates is how the state got dropped from the authorize URL
once already; `kite_login_url` is now the one builder and `routers/brokers.py` calls it too.
(4) *A restart hook so the running desk picks the token up* — the desk tree is out of this
module's scope and a restart hook is an order-capable process being bounced by a Celery task.
Q-SW13-1 stands, and STATUS says so instead.

**How to reverse.** `BASKFY_KITE_LOGIN_NUDGE_ENABLED=false` stops it dead. Deleting
`tasks/kite_login_nudge.py`, its two Beat entries, the `baskfy.kite.*` route and the settings pair
removes it; `broker_oauth.kite_login_url` stays, because the router uses it.

**⚠ UNREVIEWED** on three details: the two clock times (08:45 / 09:05), the refusal when
`BASKFY_BROKER_OAUTH_STATE_PATH` is unset (silence rather than a link that dies at the callback),
and the marker living beside the token blob rather than in a table.


## SW19.1 — The desk re-reads a token that changed under it, and DRY_RUN means one thing per service · ⚠ UNREVIEWED

**Context.** Two findings from deploy #6, both Maulik's to fix and both fixed here.

(a) `app.main.kite()` caches one `Kite` for the life of the process and `__init__` loads the
token once, so a token written at 09:10 — by the SW18 login, by `/callback`, by the bridge — was
invisible until somebody restarted an order-capable container. Every morning. `Kite` now records
the blob's mtime and `refresh_token_if_changed()` re-reads it when that moves; it is called at
the top of `is_authed`, `holdings`, `ltp` and `quotes`, so the first request after a login carries
the new session and none carries a stale one. One `stat` per call. The monitor never needed it
(it builds its own client at 09:14) and keeps working unchanged.

(b) The box's `.env.staging` set `DRY_RUN` twice — `true`, then `false` 57 lines later — and
`env_file` takes the last, so the api/worker/beat ran with `false`. That is **correct** for them:
`broker_oauth.dry_run_enabled()` reads it, and `false` is what lets `/brokers/callback` redeem a
real Kite request token instead of storing a `sim_` stub. The stale `true` is gone, the surviving
line carries four comment lines saying which side reads it, and `BASKFY_DESK_DRY_RUN=true` is now
written explicitly beside it rather than relying on compose's default — the desk and the monitor
read that one, and it is the switch that turns a Confirm into money.

**Rejected.** Restarting `desk` from the nudge task (bouncing an order-capable process from a
Celery job, and it would not help a login done at any other minute); a token-reload route on the
desk (a new surface for the same fact the filesystem already carries).

**Reversal.** Drop `refresh_token_if_changed` and its four call sites — the desk then needs
`docker compose restart desk` after every login, which is what Q-SW13-1 says today.


## SW20.1 — A second, synchronous limiter rather than one shared with the order path · ⚠ UNREVIEWED

**Context.** The desk's reads were unthrottled. `baskfy_execution.ratelimit.KiteLimits` already
exists and is correct, but it is `async` — awaited inside the gateway's event loop — while the
desk's client is blocking and is called from synchronous request handlers and from the monitor's
own thread.

**The choice.** `app/core/kite_limits.py`: the same semantics (remember the last departure, sleep
out the remainder, record the new one) on a threading lock, with one spacer **per endpoint family**
because Kite's caps are per endpoint and a historical backfill must not be able to starve the
quote the monitor needs. The order path is untouched: its caps and its limiter are the same
objects they were, and the scan in `tests/test_kite_limits.py` names the order calls it is
deliberately not checking.

**Per process, and said out loud.** Two desk containers hold two limiters. The shared Redis
bucket (`baskfy:ratelimit:kite`) is what would make it global and the box runs that Redis; the
realistic overlap today is one `quote` slot a window, so the bound is stated in STATUS rather
than a distributed limiter built on a guess about which process asks first.

**Rejected.** Making `KiteLimits` sync as well (it would change the order path's behaviour to
serve the reads); one global spacer (a backfill would then throttle a quote); a limiter inside
`kiteconnect` (vendored code, and it would not see the analytics call sites).

**Reversal.** Delete `app/core/kite_limits.py` and the `self.limits.slot(...)` lines; the reads
then go out as fast as the loop asks, which is what they did before 4 Sep.


## SW21.1 — The shared limit is a spacer in Redis, not the providers' token bucket · ⚠ UNREVIEWED

**Context.** SW20.1 left the read limiter per process and said so: the web container and
`swing-monitor` hold one `DeskLimits` each, so together they can exceed a Kite endpoint's cap.
The stated fix was "the shared Redis bucket the Baskfy side already uses
(`baskfy_providers.factory.build_rate_limiter`, key `baskfy:ratelimit:kite`)", and the box runs
that Redis. This is that fix, and it is not that bucket.

**The choice.** `app/core/kite_limits.SharedSpacer`: the distributed form of `Spacer.take` — a
four-line Lua script that reads the family's next departure, claims it, and writes the one after
— keyed `baskfy:ratelimit:kite:{quote,historical,general}`, one key per family because Kite's
caps are per endpoint. Redis's own clock (`TIME`) is the reference, so two containers cannot
disagree about "now". `DeskLimits.slot` takes the shared claim **and** this process's own spacer,
always: the first bounds what the box sends, the second bounds what this process sends, and when
the desk is alone the second costs nothing because the first already spent the interval.

**Why not `RedisTokenBucket`.** It is a token bucket, and a bucket of capacity C refilling C/s
grants up to 2C inside the first second. On this desk that arithmetic is not theoretical:
`baskfy_execution/ratelimit.py` carries 18 Aug 2026, when eleven of twenty-one orders came back
"Maximum allowed order requests per second exceeded", and its per-second cap has been spacing
rather than a bucket ever since. The tightest family here is `quote` at **1 req/s**, where a
burst of two is a 100 % overshoot — precisely the endpoint a polling page and a polling monitor
would both hit. Two algorithms is the cost; the providers' bucket keeps its key and its callers.

**Degrading, deliberately.** No `BASKFY_REDIS_URL`, an unreachable server, a dropped connection,
or a queue longer than `MAX_SHARED_WAIT_SECONDS` (10 s) all fall back to this process's own
spacers — never to an unthrottled call — and are logged and counted (`DeskLimits.local_only`). A
failed connection is retried once a minute rather than given up on for the life of the process,
so a Redis restarted by a deploy does not leave the desk silently unshared. Redis is deliberately
**not** a `depends_on` in `compose.prod.yml`: the desk must be able to rebalance on any Friday
(root CLAUDE.md), and a limiter that can stop it starting is a worse failure than the one it
prevents. `GET /status` reports `read_limiter: "shared" | "per-process"` — the one route that
answers without the password — and `verify-swing.sh` asserts it plus `BASKFY_REDIS_URL` in both
containers' environments.

**The residual, and it is smaller than SW20.1's.** The pipeline's Kite provider still takes its
own bucket at `baskfy:ratelimit:kite` for the worker's calls, so the desk and a nightly backfill
can between them still exceed an endpoint's cap. The overlap is small in practice — the backfill
runs at night, the desk reads in session — and closing it means giving the ingest path these
three families and this algorithm, which is a change to the pipeline (and to a red-gate-adjacent
area) rather than to the desk. Closing it later is one edit: point `build_kite_provider` at
`SharedSpacer` and the family keys.

**Rejected.** `RedisTokenBucket` as-is (the burst above); one shared key for all families (a
backfill would then throttle the monitor's quote — SW20.1 rejected the same thing locally);
replacing the local spacers with the shared one (a Redis outage would then mean *no* limit);
raising `RateLimited` into the request handler the way the pipeline does (a limiter must not turn
a page into a 500 or a hang); qualifying the keys by API key (single account today; a second
broker account would over-throttle, which is the safe direction — noted for P4.x).

**Reversal.** `DESK_SHARED_READ_LIMITS=false` in the desk's environment, or unset
`BASKFY_REDIS_URL`: every process is back on its own spacers, which is exactly SW20's behaviour.
No data migrates, and the keys expire on their own once a family goes idle.
