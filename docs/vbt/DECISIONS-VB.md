# DECISIONS-VB — judgement calls of the volume-breakout run

Same convention as `docs/DECISIONS-MERGE.md`, `docs/swing/DECISIONS-SW.md` and
`docs/condor/DECISIONS-OC.md`: numbered by module; the context, the choice taken, the rejected
alternatives and why, and the reversal path. Decisions taken without Maulik's review are tagged
**⚠ UNREVIEWED** until he clears them.

The pack pre-takes seven so the run does not stall on them.

---

## PACK.1 — VBT-1 is a third sleeve, not a mode of an existing book · ⚠ UNREVIEWED

STRATEGY §6 names the sleeve; this decides it. The weekly book rebalances a ranked basket on a
Friday; the swing book trades intraday behind a monitor; VBT-1 is an end-of-day continuation
strategy with a three-session working limit. They share the plant, the gateway, the journal and
the desk's confirm shape and **share no rule**. So: its own tables, its own cash, its own flag,
its own pages.

Rejected: (a) a new "strategy" inside the swing schema — the `sw_` tables encode setups, pivots,
opening ranges and a ladder that mean nothing here, and every one of them would have to grow a
nullable column; (b) a screen in the screener that a person trades by hand — that is what
Chartink already is, and STRATEGY §2 is the demonstration that the screen alone is not a
strategy. Reversal: the `vb_` tables are additive and drop cleanly; nothing outside them changes.

## PACK.2 — No auto-execute flag exists for this sleeve, and none is added · (not reversible this run)

Non-negotiable 1's named exception is the swing sleeve's, by Maulik's hand (SW25/SW26). This
sleeve gets `plan → confirm → gateway → GTT` and nothing else. A VBT order exists only because a
human pressed Confirm on an unexpired plan.

It also happens to cost nothing: VBT-1 is decided at a close and sent at an open, so there is no
"the setup triggered while you were in a meeting" problem that the swing exception exists to
solve. Rejected: a symmetric flag "for consistency" — consistency is not a reason to widen the
one exception the charter names. Reversal: a written decision from Maulik and a new module, never
an edit to this run.

## PACK.3 — The VBT GTT band is 0.5–15%, and the cushion is 0.97 · ⚠ UNREVIEWED

The desk's `StopBand(0.08, 0.12)` encodes vol-scaled stops for the weekly book; the swing sleeve
already carries its own `StopBand(0.005, 0.10)` (SW PACK.3). VBT-1's stop is a flat 12% and its
ceiling is 15% (`04` §6.1), so the band is `StopBand(0.005, 0.15)`. The cushion is the swing
sleeve's mechanism reused: `limit_fraction=0.97` as an additive keyword to `place_gtt_stop`,
defaulting to the gateway's own value, so the weekly book never sees it.

Rejected: widening the desk's default band (would silently change the weekly book's findings —
Track C §8). Reversal: two constants in the desk's VBT route.

## PACK.4 — The sleeve has its own cash and never reads the account · ⚠ UNREVIEWED

`vb_config.sleeve_capital_inr`, seeded at 0. Equity is that capital plus this sleeve's own
realised and unrealised P&L (`06` VB5). The sleeve never sizes against the whole account and
never sells a holding it did not buy — `vb_position` is the source of truth for what it owns.

Rejected: sizing against the broker's holdings value (a swing entry, a weekly-book rebalance or a
deposit would silently resize every VBT line). Reversal: none needed; it is the safer default and
the one both other sleeves already use.

## PACK.5 — Only four numbers are settings; the rest is code · ⚠ UNREVIEWED

`vb_config` carries sleeve capital, max open positions, max position % and the stop %. Everything
else in `04` is a `baskfy_core.vbt.config` default. Reason, inherited verbatim from SW PACK.5: a
threshold that can be changed in a form gets changed after a bad week, which is the failure mode
the method exists to prevent; a code change leaves a diff and an entry here.

The stop is a setting and the entry window is not, deliberately: STRATEGY §4 measured the stop as
insurance across 10–15% and the entry window as **the one parameter with a cliff**. Rejected: a
"strategy editor". Reversal: promote a field with a migration and a ceiling.

## PACK.6 — `SCAN_ONLY` rows are stored, not only signals · ⚠ UNREVIEWED

`vb_signal_daily` keeps the rows that passed the five Chartink lines and failed a trend filter,
with `failed_filters`. It roughly quintuples the table (32,929 scan hits against 6,293 signals
over nine years — about 3,600 rows a year, which is nothing).

Reason: `01` §3's ablation table is the entire argument for the six filters, and a system that
stores only what it accepted cannot show a person what it rejected or answer "why not this one?".
It is also the only way `05` §2's "what the scan rejected" section can exist. Rejected: signals
only (cheaper, and blind). Reversal: a `WHERE state = 'SIGNAL'` in the writer.

## PACK.7 — The real-money gate keeps its twenty DRY_RUN sessions · ⚠ UNREVIEWED

`02` §3 restates the swing pack's **original** five conditions, including the twenty-session paper
gate that STANDING-ANSWERS A11 later withdrew for the swing sleeve. That withdrawal was argued
from a specific fact — the swing code work was complete and the rehearsal it bought was bought
instead by one DRY_RUN morning on a real session. **That argument has not been made for this
sleeve**, which has rehearsed nothing, and whose one genuinely new mechanism (the three-session
working order, VB7) can only be exercised by sessions passing.

Rejected: copying A11's rewritten gate (it is about a different sleeve's evidence). Reversal:
Maulik writes the same withdrawal here, in one line, and an agent records it before acting.

---

## VB0 — the pack

### VB0.1 — The research folder is committed with the pack · ⚠ UNREVIEWED

`research/volume-breakout/` was untracked when this run started. VB2's acceptance criteria
compare against `out/final_trades.csv` and `out/final_metrics.json`, and `01` cites STRATEGY.md
as the spec, so both must be in the repository or the run's central claim is uncheckable.

Committed: `STRATEGY.md`, `KICKOFF-PROMPT.md`, the `vbt/` package, the driver scripts, the
`out/` results (660 KB, the equity chart included), `chartink_backtest.csv` (165 KB) and
`data/index_series.csv` (1.2 MB — the benchmark closes `final.py` compares against). **Not**
committed and left gitignored: `aws/` (the 68 MB bar export — regenerate with
`export_bars_aws.sh`), `data/panel.pkl`, `data/sig2.pkl`, `out/g2_*`, and the near-empty local
Docker export `/*.csv.gz` that `aws/` superseded. Total added: about 2 MB across 45 files.

Rejected: leaving it untracked (VB2's goldens would compare against files that are not in the
repo, and `01`'s citations would dangle); committing `aws/` too (68 MB of derived data that a
script regenerates, and CLAUDE.md's `data/` stays untracked). Reversal: `git rm --cached`.

### VB0.2 — The six-filter reading is the spec; `grid2.py` is the stale half · ⚠ UNREVIEWED

**Measured, not assumed, on 10 Sep 2026 against `research/volume-breakout/aws/`.**

STRATEGY §3 lists six trend filters A–F and says two more — `Close > 50-DMA` and 20-day
`ADR ≤ 8%` — were tested and dropped as redundant. The research script `grid2.py` still applies
all eight when it builds `data/sig2.pkl`. The two readings are not the same, so one of them
produced STRATEGY's headline numbers:

| reading | signals | CAGR | max DD | trades | PF |
|---|---|---|---|---|---|
| **six filters (STRATEGY §3 A–F)** | **6,293** | **18.23%** | **−27.94%** | **761** | **1.55** |
| eight filters (`grid2.py` as it stands) | 6,254 | 17.47% | −27.94% | 765 | 1.51 |
| STRATEGY §3/§4 as published | 6,293 | 18.2% | −27.9% | 761 | 1.55 |

**The six-filter reading reproduces the published numbers exactly.** So `04` §3.2 carries exactly
six filters, `TrendConfig` has no field for the other two, and the ablation table in `01` §3 is
the six-filter one.

This is CLAUDE.md's own rule applied to a research tree: the note is the later, deliberate
statement of the rules; the script is code that was edited after it produced the pickle the final
run consumed. **The stale half is fixed, not the decision** — a note is added at the head of
`grid2.py` rather than changing the arithmetic of a research script nobody will re-run.

Rejected: (a) implementing eight filters "because the code did it" — it would miss the published
number by 0.76 CAGR points and 4 trades, and VB2 would then have to explain a discrepancy it
created; (b) implementing eight and re-publishing STRATEGY's tables — a research re-write is not
this run's to do. Reversal: add the two fields to `TrendConfig` and re-run VB2; the delta is
measured above.

### VB0.3 — VBT breadth is its own series, not `market_health_daily` · ⚠ UNREVIEWED

STRATEGY §6 says "`market_health_daily` already carries `pct_above_20dma`; this needs the 200-day
cousin". The 200-day cousin **already exists** — `market_health_daily.pct_above_200dma`, the
first of `baskfy_core.breadth.BREADTH_COLUMNS` — but it is computed over an **index's
point-in-time membership** (`index_member_daily`), not over the whole traded universe, and on the
plant's ordinary calendar rather than the thin-session-corrected one.

Those are two different measurements of two different populations. Reusing the existing column
would change the gate that produced every number in STRATEGY §4. So `vb_breadth_daily` is its own
table and its own computation (`04` §4), and VB2 asserts it reproduces `out/breadth200.csv` to
1e-12.

**Measured while deciding**: the research series' denominator nominally includes ETFs, but the
plant holds only 7–8 bars for each of the 349 ETF names, so **no ETF ever has a 200-DMA and none
ever entered the denominator** — excluding them explicitly, as `04` §1 does, reproduces the
research series exactly (max absolute difference 0.0000 over 2,396 sessions, and the 40% gate
verdict differs on 0 of them). The explicit exclusion is kept because the day the plant starts
fetching ETF bars it must not silently change the gate.

Rejected: (a) using `pct_above_200dma` (a different population; would need STRATEGY re-run);
(b) leaving ETFs in the denominator to "match the research" (matches today by accident, drifts
tomorrow by construction). Reversal: one predicate in `breadth_above_dma`.

### VB0.4 — The gate is two-valued, not three · ⚠ UNREVIEWED

The swing sleeve's gate is GREEN / AMBER / RED because its ladder uses the middle value to shrink
exposure. VBT-1 has no ladder and no exposure tiers: STRATEGY §3's rule is a single threshold, and
§4's sensitivity table measures it as one. So `vb_breadth_daily.gate` is `OPEN` / `SHUT`.

Rejected: a three-valued gate with an amber band that nothing reads (a column that means nothing
is a column somebody later gives a meaning to). Reversal: additive.

### VB0.5 — The nightly step is post-publish and cannot fail the night · ⚠ UNREVIEWED

`PipelineStep.COMPUTE_VBT` goes into `POST_PUBLISH_STEPS` beside `REFRESH_BASKET` and
`COMPUTE_SWING`, wrapped so it cannot raise — `steps.py` says in as many words that a step added
after these two must either join the set or be one the run's success depends on, and that this is
a decision rather than an edit. This is the decision: **a VBT signal nobody wrote is a page
saying "no candidates today"; a nightly run that failed is a screener serving yesterday to
everybody.** The trade is not close.

Rejected: making it a blocking step (a detector bug would hold back a good `data_version`).
Reversal: remove it from the set and unwrap the step.

---

## VB1 — the pure core

### VB1.1 — The indicators densify against the calendar · ⚠ UNREVIEWED

`with_vbt_indicators` lays every instrument's bars out against the session calendar with a
**null** where the name did not print, and every rolling statistic carries
`min_samples = round(0.9 x window)`. The alternative — computing over the rows that exist — is
simpler and is a different rule: the 50-day volume average of a name that missed two of the last
fifty sessions would then be the average of its last fifty *traded* bars, reaching back further
in time for exactly the illiquid names the filters are trying to judge.

This is what `04` §2.2 means by "the way a screener that only sees traded bars computes them",
and it is what the research's dense numpy panel did. Verified while deciding: Polars'
`rolling_mean(window, min_samples=n)` counts **non-null values** and produces a value at a null
position, matching pandas' `rolling(window, min_periods=n)` exactly; `ewm_mean(..., adjust=False,
ignore_nulls=False)` matches `pandas.Series.ewm(..., adjust=False)` likewise.

The cost is memory: a full-history panel is 4,186 x 2,396 rows. VB2's runner processes
instruments in chunks for that reason. Rejected: row-wise rolling (a different rule, silently);
a `rolling_*_by` window keyed on a session index (equivalent for the averages, but the
prior-20-session high and the 20-session-ago close both need an exact session offset that only a
dense frame gives). Reversal: one function.

### VB1.2 — `close_position` is null at a missing bar, 0.5 at a rangeless one · ⚠ UNREVIEWED

The research computes `where(high - low > 0, (close - low) / range, 0.5)`, which hands a
**missing** bar the same 0.5 it hands a locked one. Here a missing bar is null and a rangeless bar
is 0.5.

No rule can tell the difference — filter D is only evaluated where the close exists — but the
column is also read by a page, and "this bar closed mid-range" is a false statement about a
session the name did not trade. Rejected: matching the research exactly (identical results, a
column that lies to a reader). Reversal: one `when` clause.

### VB1.3 — There is no risk-based sizing, and that is the strategy · ⚠ UNREVIEWED

The swing sleeve sizes by risk per trade because its stop is a technical level whose distance
varies by name. VBT-1's stop is a **flat percentage of the entry**, so risk-per-trade sizing and
equal-weight sizing are the same arithmetic with different constants, and the research sized it
equal-weight. `SizingConfig` therefore has no `risk_per_trade_pct` and `size_entry` takes no
stop distance into its budgets.

The stop still appears in the sized result, because `risk_inr` is what a person reads to know
what one full stop-out costs. Rejected: a `risk_per_trade_pct` that would always resolve to the
same slot (a knob with no effect is a knob somebody will turn). Reversal: a fifth budget in
`size_entry`.

---

## VB2 — the goldens

**The headline: the core reproduces the study exactly.** 32,929 scan hits, 6,293 signals, all
**761** trades matching on symbol, entry date, exit date, quantity and reason, every entry and
exit price agreeing to **₹0.0000**, CAGR **18.23%**, maximum drawdown **−27.94%**, the exit split
**688 EMA / 62 stop / 1 no-bar / 10 end-of-run**, and `01` §4's yearly table to one decimal.
Eleven of the twelve neighbourhood cases reproduce to a tenth of a point. The four entries below
are the things that had to be settled to get there.

### VB2.1 — Multiplying a price by one is not a no-op, and it cost 589 trades · ⚠ UNREVIEWED

The first full run matched 172 of 761 trades and then diverged. The cause was one line: the fill
test computed `limit × (1 − fill_through_pct/100)` before comparing it with the session's low,
and with a through-requirement of zero that multiplication is by exactly one.

It is not a no-op. A bar price converted from a `float` carries about **fifty** significant
digits of exact binary expansion; Decimal rounds the result of a multiplication to the context's
**twenty-eight**. So a limit that a low touched *exactly* — the same price, the same bit pattern,
which is what "a pullback to the previous close" looks like — came back a hair **above** that low
and never filled. The study's float arithmetic has no such step.

`_fill_threshold` returns the limit itself when there is nothing to discount it by. Rejected:
raising the Decimal context's precision inside the engine (fixes this instance, leaves the same
trap for the next exact comparison, and makes every other operation slower for no reason);
comparing in float (house rule 9, and the fix would have been invisible). Reversal: none wanted;
the guard is one branch with a docstring that explains itself.

**What it says about the engine generally:** exact comparisons against a tape price must not pass
through Decimal arithmetic first. Money may; prices being compared with a bar may not.

### VB2.2 — One name sits exactly on its own average, and two libraries disagree · ⚠ UNREVIEWED

`breadth_series` and the research's own series agree on the **gate's verdict at 40% on every one
of 2,396 sessions**, and on the percentage itself to within one name in the numerator (~0.1 of a
percentage point out of ~1,100 measured names).

The residual is a genuine tie. PRIVISCL's adjusted close has been ₹0.10 for months; its 200-day
average is ₹0.10. Polars' rolling mean returns `0.09999999999999999` and pandas' returns `0.1`,
a difference of one unit in the last place, and `close > average` flips. On 2018-05-30 that is
390 names above out of 1,114 rather than 389 — 35.009% against 34.919%.

At the strategy's own 40% it changes nothing, ever. At a **35%** gate it flips one session of
2,396, and 0.2 CAGR points follow — which is not a defect to be tuned away but a measurement of
how thin the 2018 margin is, and an argument for 40 rather than the edge of the plateau.

Rejected: rounding breadth to a fixed number of decimals before the comparison (a threshold that
depends on a rounding rule is worse, not better); reproducing pandas' summation order (chasing an
implementation, not a rule). Reversal: none; the tolerance is documented in `04` §4.6 and
asserted in `test_vbt_goldens.py`.

### VB2.3 — The end-of-run liquidations are trades · ⚠ UNREVIEWED

Ten of the study's 761 are positions still open on the last session, sold at its close. The first
draft of `04` §11 excluded them from the win rate as an artefact.

They are not an artefact. A book that is 63% invested is always carrying something, and reporting
only the trades that closed on their own terms would flatter the win rate by exactly the
positions whose outcome is unknown. They are labelled `END_OF_RUN`, counted in every statistic,
and the page says how many there were. Rejected: excluding them (a nicer number about a different
book). Reversal: one filter, and a line on the page saying it was applied.

### VB2.4 — The engine's tick is the paise; the desk's is five · ⚠ UNREVIEWED

The study floored stops and rounded exits to ₹0.01. NSE quotes most cash equities in ₹0.05, which
is what a level the desk *sends* must be snapped to (`04` §7.1, `TICK_INR`).

Both are right, for different jobs, so the tick is a `BacktestParams` field rather than a
constant: reproducing the study means using the study's tick, and placing an order means using
the exchange's. A run that does not say which tick it used has not said what it measured.
Rejected: one tick everywhere (either the reproduction fails or the desk sends a price the
exchange rejects). Reversal: the field.

---

## VB3 — schema and settings

### VB3.1 — The migration is generated from the models, then committed as ordinary code · ⚠ UNREVIEWED

Twelve tables and about 280 columns is more than anyone types twice without a typo, and a
migration that disagrees with its models by one nullability is a bug nobody finds until a night
job writes a null.

So `0037_vbt.py`'s body was **emitted from `Base.metadata`** by a throwaway script and committed
as ordinary `op.create_table` calls — self-contained Alembic code, no import of the models, which
is the rule a migration has to obey because models change and history does not. The generator is
not committed: its output is the artefact, and keeping it would invite someone to re-run it
against changed models and overwrite a migration that has already been applied somewhere.

Verified rather than assumed: a scratch database was created, migrated from base to head, and
compared against the models with **Alembic's own `compare_metadata`** — **0 differences among the
`vb_` tables**. Then downgraded to `0036_check_name` (all twelve dropped) and upgraded again
(all twelve back), and compared once more. Rejected: hand-writing it (typo risk with no
detection); `alembic revision --autogenerate` against the dev database (it writes to a database
another session is using, and it was at `0026` anyway).

### VB3.2 — `0037`, not `0036` — the head moved mid-run · ⚠ UNREVIEWED

The pack recorded `0035_split_allocation` as the head and `0036` as the next free number. While
VB1 and VB2 were being built, a concurrent session committed `0036_check_name`, and `alembic
heads` reported **two heads** — the only symptom a linear history gives.

The migration was renumbered to `0037_vbt`, revising `0036_check_name`. Chaining onto the
committed state is the rule when two sessions collide; the alternative (asking the other session
to renumber) trades a rename for a coordination round-trip and gets nothing. `docs/vbt/03` now
says so, and says what the symptom looked like, because it will happen again.

### VB3.3 — Four settings, and `max_open_positions` has a ceiling above the strategy's own · ⚠ UNREVIEWED

`vb_config` carries the sleeve's capital, the position count, the position cap and the stop
(PACK.5). The ceiling on the count is **15** while the strategy's own `SizingConfig.max_slots` is
**10**, which looks like a contradiction and is not: `04` §9.1 takes `min(setting, max_slots)`, so
a setting above ten cannot widen the book — it can only fail to narrow it.

The ceiling is higher than the slot count deliberately, so the number stays visible as a setting a
person may lower rather than one the server has already pinned. Rejected: a ceiling of 10 (the
setting and the ceiling would then be the same number, and a form field that can only ever be
lowered reads as broken); no ceiling at all (a setting with no bound is not a setting, it is a
hole). Reversal: one default in `Settings`.

### VB3.4 — The db-marked tests ran against their own database · ⚠ UNREVIEWED

`BASKFY_TEST_DATABASE_URL` points at one shared `baskfy_test`, and `screener_helpers.seeded_database()`
**drops and re-migrates** it. A concurrent session was running the whole API suite against that
same database, and the symptom was `relation "app_user" does not exist` in the middle of a test
that had just created one.

VB3's runs therefore used a private `baskfy_vb_test`. Worth recording because the failure looks
exactly like a broken migration and is not, and because it will recur on any machine where two
sessions test at once. The fixture itself is unchanged: it is the *environment variable* that was
pointed elsewhere, which is the smallest change that fixes it and the only one that leaves CI
alone.

---

## VB4 — the nightly job

### VB4.1 — The step is thirteenth, after `compute_swing`, and cannot fail the night · ⚠ UNREVIEWED

`steps.py` asks for this to be a decision rather than an edit: *"A step added after these two must
either join this set or be a step the run's success depends on, which is a decision, not an
edit."* This is the decision. `COMPUTE_VBT` joins `POST_PUBLISH_STEPS` and is wrapped by
`run_compute_vbt_step`, which cannot raise.

**A `vb_signal_daily` row nobody wrote is a page saying "no candidates today"; a nightly run that
failed is a screener serving yesterday to everybody.** The trade is not close. After
`compute_swing` rather than before it because the swing book is the older sleeve and its evening
has an established shape; nothing in either step reads the other's rows.

Rejected: a blocking step (a detector bug would hold back a good `data_version`); a separate
Beat-only job with no step at all (the pipeline's own record would then not say whether the
sleeve ran, and `/admin/pipeline` at 21:00 is where an operator looks).

### VB4.2 — The 21:10 retry asks before it works · ⚠ UNREVIEWED

The swing book's 21:00 entry re-detects unconditionally and is idempotent, which is fine: its
detectors read 200 sessions of a *liquid* universe. This detector densifies **260 sessions of the
whole cash register** — about 800,000 rows before a single window is computed — so re-deriving
rows the chain has already written costs minutes to arrive at the same answer.

So `baskfy.vbt.detect` counts the session's rows first and returns `{"skipped": "already
detected"}` when there are any. `make vbt DATE=… FORCE=1` is the escape hatch for the case the
rule gets wrong: a threshold changed and the stored rows are stale.

Rejected: unconditional re-detection (correct, and wasteful every ordinary evening); no retry at
all (a night the chain failed its quality gate would have no signals, and the bars were fine).

### VB4.3 — A thin session gets a row saying it was thin · ⚠ UNREVIEWED

`04` §2.1 removes muhurat and special-Saturday sessions from the rolling calendar. The job could
simply write nothing on such a day. It writes a `vb_breadth_daily` row with `thin_session = true`,
a shut gate and zero counts instead.

A hole in a daily series reads like a job that failed, and the one thing this pack keeps insisting
on is that "no signals" and "no data" must never look the same. Rejected: no row (indistinguishable
from an outage); a row with the day's real breadth (it would be breadth over ~200 names, which is
the number the rule exists to refuse). Reversal: one branch.

### VB4.4 — The context levels come back into today's money · ⚠ UNREVIEWED

`limit_price` is the bar's own `close_raw` — the exchange print, which is what a broker is sent.
The stored `sma_200`, `ema_21` and `high_20_prior` are adjusted-series values **divided by the
row's `adj_factor`**, so a page can compare them with the limit.

Without that, the morning after a 1:2 split a page would show a limit of ₹48 against a 200-day
average of ₹90 and report a name below a trend it is comfortably above. The factor is stored
beside them, so tomorrow's job can tell that a split happened overnight. `upper_circuit` goes the
other way — multiplied by the factor on the way *in* — because the band is an exchange print and
`high` has been adjusted in place, and an unconverted comparison would report the whole market
locked.

Rejected: storing the adjusted levels raw (a page that compares two different spaces); storing
only the limit (the page then has no trend context at all, and `05` §2's mini chart wants it).

---

## VB5 — the sleeve's cash and book

### VB5.1 — Cash and cash-available are different numbers · ⚠ UNREVIEWED

`SleeveValue` carries both. `cash` is what the sleeve has; `cash_available` is `cash` minus what
its **resting limits** have spoken for. A plan sizes against the second.

This is the distinction a naive implementation loses, and losing it is expensive in exactly one
way: three limits resting at ₹1 lakh each look like ₹10 lakh of cash to a fourth line, and on the
morning all four fill the book is 40% over-committed. `04` §9.1's `SLOTS_FULL` counts working
orders beside positions for the same reason, and the two rules have to agree or the second is
decoration.

`cash_available` is floored at zero: a book cannot un-commit money, and a negative budget would
size a line at zero rather than refusing it, which is a different sentence on the page.

Rejected: one number (the failure above); tracking commitment only in the plan builder (the desk
and the page would each re-derive it, and one of them would get it wrong).

### VB5.2 — A suspended holding is marked at its entry, not dropped · ⚠ UNREVIEWED

A position in a name that has not printed since it was bought has no mark. It is valued at its
**entry**, so the sleeve's equity still contains it.

Dropping it would report an equity the sleeve does not have, and the number would silently
improve the day a holding went bad enough to be suspended. `04` §6.5's five-session write-off is
what eventually removes it, at the last close anybody saw. Rejected: excluding it (an equity that
flatters itself); marking it at zero (a claim nobody has evidence for).

### VB5.3 — The sleeve's database tests live in the worker tree · ⚠ UNREVIEWED

`baskfy_api.vbt_sleeve` is an API module, and its database tests are in
`services/worker/tests/test_vbt_sleeve_db.py`.

Two reasons, and the first is the honest one: the worker's conftest already provides a
per-test `session` against a migrated database, while the API's `screener_helpers.seeded_database()`
also loads the 271-row reference export — which this loader does not need and which, on the day
this was written, was failing for an unrelated reason (a concurrent session's in-flight migration
had dropped a unique constraint that seeder upserts on). The second reason is that the reader of
this loader that matters most **is** the evening job, which is the worker's.

Rejected: a hand-rolled engine fixture in the API tree (a fourth copy of a fixture three trees
already have). Reversal: move the file when the API's seeder is fixed; the test body does not
change.

---

## VB6 — the plan, and the click that turns it into an order

### VB6.1 — The evening's session counts are derived, not incremented · ⚠ UNREVIEWED

`vb_order.sessions_worked` is **recomputed** every evening from the calendar
(`sessions_between(signal_date, today)`) rather than incremented by one.

An incrementing counter has to remember whether tonight already ran — and the evening job runs
again on a retry, on a manual `make vbt-plan`, and on the morning rebuild. A derived count cannot
be wrong however many times the job runs, and that is the difference between a rule and a
bookkeeping convention. A test runs the evening three times and asserts the count is still two.

Rejected: an increment guarded by a "did tonight run" flag (a second piece of state to keep
right, and the flag is the thing that goes wrong). Reversal: none wanted.

### VB6.2 — What counts as a DRY_RUN session · ⚠ UNREVIEWED

`02` §3.1 asks for **twenty** DRY_RUN sessions before the execution flag may flip, so what
counts as one is a decision rather than a detail.

A session counts when it closed in `DRY_RUN` mode **and** either a line was confirmed on it, or
the plan had no executable line at all. The second clause is not a loophole: a shut gate with an
empty book produces nothing to confirm, and a gate that could only be satisfied on days the
market cooperated would never be satisfied — the sleeve is in cash 37% of the time by design.
What it refuses is the case the gate is actually about: **a session with lines on the page that
nobody rehearsed.**

Rejected: counting every session the job ran (twenty of those prove the *job* runs, which the
nightly step already proves); counting only sessions with a confirm (unreachable in a quiet
market, and the rule would then push a person to confirm something in order to satisfy it —
exactly the wrong incentive).

### VB6.3 — A sell without a live price goes; a buy without one does not · ⚠ UNREVIEWED

A `MARKET` order carries no price, and the gateway's risk layer refuses to value one without a
`reference_price`. The desk reads a live price per confirm. When there is none — no Kite session,
a sqlite desk, a token that expired overnight — the `SELL_AT_OPEN` still goes, valued at the
position's entry.

The swing sleeve refuses a *buy* in that situation and is right to (A8): no price means no
protection percentage, and a chased entry carries risk the sizing never saw. An **exit** is the
opposite case. Blocking a sell for want of a quote leaves a position the rules have decided to
close sitting in the book overnight, which is precisely the failure the exit rule exists to
prevent. The reference price affects the risk layer's valuation, never the fill.

This sleeve has no live buy path at all to make the symmetric mistake with: a `PLACE_LIMIT` is a
LIMIT at a level the plan already knows (`04` §7.1), so it needs no quote.

Rejected: blocking both (a stuck position); guessing a price for the buy too (the level *is* the
strategy). Reversal: one fallback expression.

### VB6.4 — The sweep produces a cancel **line**; it does not cancel · ⚠ UNREVIEWED

When a working order finishes its third session, the evening writes a `CANCEL_LIMIT` line and
leaves the `vb_order` row in `SENT`. Only a confirmed line calls the gateway's `cancel_order`.

A `SENT` order is live at a broker, and marking it `CANCELLED` in the database without telling
the broker would leave a real order resting against a book that believes it is gone — the worst
of the available states. An order that never reached a broker (`PROPOSED`, `CONFIRMED`) has
nothing to cancel and is marked `EXPIRED` by the sweep itself.

Rejected: cancelling in the sweep (a job that places or cancels orders without a person is the
thing `02` Track C §3 forbids, whatever direction it moves exposure in). Reversal: none.

### VB6.5 — The desk page does not poll · ⚠ UNREVIEWED

The swing page refreshes every five seconds because its triggers arrive inside a session. This
one refreshes on demand.

The plan is built twice a day and nothing about this sleeve fires while the market is open, so a
five-second poll would be motion without information — and a page that looks live invites a
person to sit in front of it during a session, which is not how this strategy is traded.
`05` §3 says so; this records that it is a choice. Rejected: matching the swing page for
consistency (consistency of *behaviour* between two sleeves that behave differently is a
disguise, not a virtue).

---

## VB7 — the working order that stops being one

### VB7.1 — The expiry window is a config field, and the property test parametrises it · ⚠ UNREVIEWED

`EntryConfig.limit_valid_sessions` is 3, and `test_vbt_expiry_property.py` runs its whole
argument at 1, 2, 3, 5 and 10 rather than at three alone.

`04` §7.2 is **the parameter with a cliff**: two sessions returns 11.4% a year where three
returns 18.2% (STRATEGY §4's ablation). A number that matters that much is the number a future
reader is most likely to want to try — and a test suite that has the literal three sprinkled
through it turns a one-field experiment into an afternoon. Parametrising it also proves what the
sensitivity table cannot: that the *machinery* is window-agnostic and only the returns are not.

Rejected: pinning three everywhere and calling the cliff a reason for rigidity (it is a reason
for care, which is not the same thing); a hypothesis strategy over the window too (it would make
every failure report a random window, and the five values that matter are known).

### VB7.2 — The four alerts are raised in-process, not by Prometheus · ⚠ UNREVIEWED

`05` §4 predicted Prometheus rules in `infra/prometheus/alerts.yml`. VB7 built
`baskfy.vbt.check_*` tasks on Beat instead, at 21:30 and 21:40, each raising through
`baskfy_worker.alerts.dispatch`.

This is `alerts.py`'s own argument, applied: the four facts here — an order past its window, a
naked position, a detector that did not run, a held name that stopped printing — are things this
codebase *knows*, with a date and a row id attached, not threshold crossings over a window. And
the box has no Prometheus deployed. A rule that fires only in an environment that does not exist
is not an alert; it is a note. The swing book made the same call at SW11 and this follows it, so
one pattern covers both sleeves.

Not exclusive: nothing here stops a Prometheus rule being added for the same names later, the way
`publish_late` is raised from both sides. Reversal: delete four Beat entries.

### VB7.3 — Every check is silent on a weekend, and none of them writes · ⚠ UNREVIEWED

Each check returns `{"checked": False, "reason": "not a weekday"}` on a Saturday, and none of the
four issues an UPDATE — a test asserts the row counts do not move.

Silence: a naked position cannot come to harm while the exchange is shut, and the condition is
still true on Monday at 21:40, which is when it is worth waking someone for. Paging for a weekend
is how a check trains people to ignore it.

No writes: the fix for every one of these is a confirmed line on the desk page. A check that
cancelled the order it found would be a job that cancels orders without a person, which is what
`02` Track C §3 forbids in the direction people forget — VB6.4 made the same call for the sweep.

Rejected: a check that auto-cancels a `PROPOSED` order (defensible, and still the beginning of a
worker that acts); running the book checks daily including weekends (noise).

### VB7.4 — A null `expires_after_session` is not a late order · ⚠ UNREVIEWED

`check_orders_past_expiry` filters on `expires_after_session IS NOT NULL`. A row whose window is
unset is a row the evening has not adopted yet — the state a manually inserted order or a
half-finished migration leaves — and paging about it says "cancel this" when the answer is "set
its window". Runbook 8's step 4 is that answer. Rejected: treating null as expired (it would
cancel orders that had not started); treating null as never-expiring (that is what the row
already does, silently, and the runbook is what makes it visible).

### VB7.5 — `VBT_DETECT_STALE` reads breadth, not signals, with four days of tolerance · ⚠ UNREVIEWED

`05` §4 said "no `vb_signal_daily` row for the published session". The check reads
`vb_breadth_daily` instead, and allows four days.

A session where nothing qualified writes **no** signal rows and **one** breadth row. Alerting on
missing signals would page on every quiet night — and quiet nights are most of them: the study's
6,293 signals over 2,396 sessions leave many days empty. The breadth row is what distinguishes
"nothing qualified" from "the detector did not run", which is the whole question. Four days
covers a Thursday-to-Monday holiday weekend without a page.

Rejected: reading `pipeline_run_step` for `compute_vbt` (it records that the step ran, not that
it produced anything); a one-day tolerance (it pages every long weekend, and a check that cries
wolf on the calendar gets muted).

---

## VB8 — the pages

### VB8.1 — The modelled fill rate is 83.8%, and `04` §7.3's 91% was invented · ⚠ UNREVIEWED

`05` §2's fill-rate line compares the live book against "the study's modelled rate". VB0 wrote
that rate as **91%** in `04` §7.3. Nothing measured it, and nothing in STRATEGY says it — it was
a plausible-sounding number in a document whose whole purpose is to be the contract the tests
assert. VB8 measured it instead and the doc now carries the measurement.

**The measurement.** `BacktestResult.orders_offered` counts the distinct `(name, signal session)`
pairs that ever reached the fill comparison — past the three-a-session cap and the ten slots,
with a bar, and not locked at a circuit. 761 of 908 filled: **83.8%**.

**The denominator is the whole argument.** The engine makes a working order out of *every*
signal, and 5,193 of them expired unfilled; filled over all of those is 12.8%. But an order that
never got a slot was never an order anybody placed, so 12.8% measures the slot count, not the
market. The live book only ever writes a `vb_order` for a line the plan produced under the same
caps, so 83.8% is the number the two books can honestly be compared on.

Rejected: keeping 91% and calling it approximate (a contract document with a made-up number in it
is worse than no number, and this run's own rule is that tests assert `04`); showing no modelled
rate until VB9 (the comparison is the point of the line, and the measurement cost one run).
Reversal: `PUBLISHED.modelled_fill_rate_pct`, one field.

**What this says about the rest of `04`.** Every *rule* in it is asserted by a test and every
threshold is a config field checked both ways by §12. This number was neither — it lived in prose
as a comparison figure. It is worth reading the remaining prose numbers in that light; §12's
table covers the thresholds, and the study's own results are now in
`baskfy_core.vbt.published`, which the goldens check against `out/final_metrics.json`.

### VB8.2 — `DRY_RUN_SESSIONS_REQUIRED` moves to the core, as a constant and not a config field · ⚠ UNREVIEWED

`02` §3's twenty sessions was a literal in `kite-momentum-rebalancer/app/vbt_desk.py`. The web
page and the API's settings view both print it too, so it is now
`baskfy_core.vbt.config.DRY_RUN_SESSIONS_REQUIRED` and the desk imports it.

A module constant rather than a field of `VbtConfig`, the way `TICK_INR` is: nothing in the
method reads it, it is not a knob anybody may turn — only Maulik can shorten the paper run
(`NEEDS-MAULIK.md` V4) — and putting it in the dataclass would add a row to `04` §12's table of
*strategy* thresholds, where a governance number does not belong.

Rejected: an environment variable (a gate you can shorten by editing a `.env` is not a gate);
leaving three copies (they had already diverged in kind — the desk's was a number, `02`'s was a
sentence).

### VB8.3 — The regenerated API artefacts carry another session's changes, unavoidably · ⚠ UNREVIEWED

`packages/api-client/openapi.json` and `src/generated/schema.ts` are generated from the whole
FastAPI app, and while VB8 ran, a concurrent portfolio-redesign session had staged source changes
in the same tree. Regenerating therefore picked up its field-level edits along with the six
`/vbt` paths and twelve `Vbt*` schemas.

They are committed anyway. CI asserts `git diff --exit-code` on both files, so leaving them stale
would turn a generated artefact into a red build for everybody; and the alternative — hand-editing
a generated file to exclude a neighbour's work — is worse than the overlap. Verified before
committing that the only *structural* additions are this run's: six paths added, none removed,
twelve schemas added, none removed.

Every other VB commit named its own paths explicitly (`git commit -- <paths>`) and left the
other session's index untouched. This one could not, for these two files only. Reversal: the
other session regenerates, which it must do anyway.

### VB8.4 — The per-row **Dismiss** note is not built · ⚠ UNREVIEWED

`05` §2 ends the Today tab with "No row action changes money. **Dismiss** (a note on the row) is
the only mutation." It does not exist.

`03`'s data model has twelve tables and none of them holds a note. Building the affordance would
have meant a thirteenth table, a migration, an API write and a server action — in service of a
control nobody has asked for, on a surface whose entire safety argument is that it cannot write.
The swing hub's watchlist earns its writes because a person curates it over days; a volume
breakout is a one-session event whose row is gone by the next evening, so there is little to
dismiss.

**What this buys.** The hub now has *no* server actions at all, and
`__tests__/read-only.test.tsx` asserts the strongest available claim: nothing under `/vbt` is a
`use server` module and nothing under it renders a form. That is a better safety property than
the one `05` §2 described.

Rejected: building it to match the spec (a migration in service of a drawing); leaving a dead
control on the page. Reversal: a `vb_note` table and one action, if a real week of use asks for
it. Recorded in `05` §2 and STATUS as not done, rather than quietly dropped.

### VB8.5 — The middle tab reads "Positions", not "Book" · ⚠ UNREVIEWED

`05` §2 names the hub's three tabs "Today | Book | Backtest". The label rendered is **Positions**;
the route keeps its documented `/vbt/book` path.

`PORTFOLIO_REDESIGN.md` §8 retires "book", "box" and "sleeve" from what a reader sees, and
`apps/web/src/lib/__tests__/no-jargon.test.ts` enforces it across the app. The swing hub already
calls its equivalent tab "Positions". Two allocations that show the same thing should not name it
two different ways, and the word this run uses in its own documents is not automatically the word
a reader wants.

The same rule rewrote several sentences on these pages: "the live book has no intraday data"
became "trading live, there is no intraday data", "this box" became "here", and "the sleeve holds
nothing" became "nothing is held". None of it changes a number or a claim.

Rejected: adding "book" to the jargon test's exception list (that list is for words used in a
meaning §8 never legislated over — a trading book is exactly the meaning it did); renaming the
route as well (a documented path changed to satisfy a copy rule is over-reach, and `05` §2 names
`/vbt/book`).

---

## VB9 — the backtest on the page

### VB9.1 — Two ways to re-run the study, and they answer different questions · ⚠ UNREVIEWED

`06` VB9 asks for "`tools/vbt/backtest.py` **and** a `baskfy.vbt.backtest` task". Both exist and
they are not duplicates:

* the **task** (`baskfy_worker.tasks.vbt_backtest`) runs over the plant's `ohlcv_daily`, appends
  a `vb_backtest_run` row and flags drift. It is the one that matters, because it measures the
  data the book will actually trade on;
* the **tool** runs over the research export, prints, and touches no database. It is what you
  reach for when the question is "did my change move a number" and the answer wanted is a diff.

Both call the same core functions, so neither can drift from the book (`04` §11). Rejected: only
the task (a database round trip to answer a question about an engine change is friction that
stops people asking); only the tool (it cannot put a number on the page, which is the module's
goal).

### VB9.2 — The three books share one detection pass · ⚠ UNREVIEWED

`full`, `gate_off` and `raw_scan` differ only in what they may act on — the gate vector and the
signal column — so `three_books()` detects once and runs the engine three times.

Detecting three times would spend three times the work to produce the same signals, and would
leave open the possibility of the three disagreeing about what a signal was, which would make the
differences between them uninterpretable. The differences are the whole point: breadth's
contribution is `full − gate_off` and the trend filters' is `full − raw_scan`.

**Measured on the export, 10 Sep 2026** — and the numbers say something the CAGR column alone
does not:

| book | CAGR | max drawdown | trades |
|---|---|---|---|
| `full` | 18.23% | −27.94% | 761 |
| `gate_off` | 18.48% | −48.78% | 1,037 |
| `raw_scan` | 0.76% | −48.10% | 958 |

The gate **costs** a quarter of a CAGR point and **buys 21 points of drawdown**. Anyone reading
the ablation as "the gate is worth −0.25%" has read the wrong column, and `/vbt/backtest` says so
in a sentence under the contributions.

### VB9.3 — The API's "latest finished" needed a second condition, and did not have one · ⚠ UNREVIEWED

`03` §8 says a failed re-run must never displace the last good number. `baskfy_api.vbt.backtests`
filtered on `finished_at IS NOT NULL` alone — and a *failed* run sets `finished_at` too, by the
same section's rule. So a re-run that raised would have replaced a real result with a card full
of blanks: the exact failure an append-only table exists to prevent.

Fixed by requiring `stats IS NOT NULL` as well, in both the API's query and the worker's
`latest_finished`, with a test that seeds a good run and a failed one and asserts the page reads
the good one. Found at VB9 by writing the failure test first; VB8 shipped the bug because its own
fixtures only ever contained runs that succeeded.

### VB9.4 — The full-history run is measured on the export, not on the plant · ⚠ UNREVIEWED

VB9's acceptance asks that "the run over the full history completes on the dev box in **< 30
minutes**". The engine half is measured: **23 seconds** for all three books over the whole
2017–2026 history, printed by `tools/vbt/backtest.py`.

The plant-loaded half is **not measured**, and STATUS says so. No database this run had access to
holds the full 4,186-name history — the test database carries fixtures — so the number that would
complete the claim is the `ohlcv_daily` read, and it cannot be produced here honestly. The engine
being three orders of magnitude inside the budget is evidence that the whole is likely to fit,
not proof that it does. It goes to `NEEDS-MAULIK.md` as a one-command check on the box.

Rejected: quoting the 23 seconds as though it were the whole run (it is a quarter of the work);
synthesising 7 million bars to make a number (a timing measured on invented data answers a
question nobody asked).
