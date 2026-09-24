# DECISIONS-FO — judgement calls of the FO run

Numbered like `DECISIONS-OP`. The pack's own are PACK.n; module entries are FO<N>.n. Each carries
the context, the choice, the rejected alternatives, and how to reverse it. **⚠ UNREVIEWED** until
Maulik has read it.

## PACK.1 — Overnight derivatives are in scope, defined-risk only (Maulik, 23 Sep 2026)

**Context.** `docs/options/02` Track C §1, §5 and §6 forbid overnight options, stock options and
futures legs for O1–O3. Maulik, in session on 23 Sep 2026, asked for "the opportunity in the FNO
segment … not only in index options but also in selling or buying options in FNO stocks and
futures", some of it held overnight. He then answered four questions: extend the Options tab;
**defined-risk overnight** (NRML allowed for spreads, long options, and futures with a stop or
hedge; no naked short options); all four instrument families (stock options buying, stock options
selling, stock futures, index futures/options); and **research + design pack first**.

**Choice.** A separate pack (`docs/fno/`) whose Track A permits carrying across the close under the
four rules of `02` §2. The options pack's Track C stays true for O1–O3, and each doc says which
sleeves it governs. His answer is the later fact (root CLAUDE.md, "the decision wins").

**Rejected.** Editing `docs/options/02` in place: it governs a run in progress (OP8 next), and a
second meaning for "Track C" in one file is how a wrong gate gets built. Allowing naked short
options with a margin cap: he chose defined risk.

**Reversal.** Delete `docs/fno/`. Nothing in force changed: no gateway code was edited by the pack,
and every FO flag defaults false.

## M.2 — F1's capital is ₹25 lakh (Maulik, in session, 25 Sep 2026)

**Context.** FO4's scan found M.1's ₹10 lakh sizes F1 to **zero lots every month**: at 1 % (the
`BASKFY_FNO_RISK_PCT_MAX` ceiling) the budget is ₹10,000, and over the last 12 months of the
research's condors one lot's max loss was ₹15,792–26,234 on NIFTY (lot 65, median ₹18,763) and
₹17,122–46,599 on BANKNIFTY (lot 30, median ₹22,080). Sizing never rounds up (`04` §3), so every
plan would be `REJECTED_SIZE` and the paper period (`04` §9, ≥ 4 opened) could never complete.

**Asked with options** — paper one lot with live unchanged (recommended), ₹25 lakh, or keep ₹10
lakh and never open — **Maulik chose ₹25 lakh.** At 1 % that is ₹25,000, exactly the per-trade
ceiling, so paper sizes exactly as live would. One lot fits in most months (both medians are
under it); a BANKNIFTY month above ₹25,000 (the year's max was ₹46,599) is still `REJECTED_SIZE`,
by design.

**Changed.** `F1_SEED_CAPITAL_INR` = ₹25,00,000, read by the seed; `01`, `02`, `03`, `04`,
README. The sizing arithmetic tests now name a ₹10 lakh sleeve explicitly and a new test pins the
seeded budget to the ceiling; the scan test proves both sides (₹25 lakh → CANDIDATE, ₹10 lakh →
`REJECTED_SIZE` "0 lots"). **Moves no money**: every FO execution flag stays false.

## PACK.2 — The F&O bhavcopy is read through `NSEProvider`, not a new provider · ⚠ UNREVIEWED

**Context.** The research needed every contract's end-of-day price for 2022–2026, including
expired contracts, which Kite cannot serve (`docs/options/07` §2). Track C §9 of the options pack
forbids new providers and scraping.

**Choice.** `NSEProvider.fo_bhavcopy(on)`, beside `bhavcopy(on)`: the same archive-then-parse
path, limiter and cookie priming, and the same wrong-date refusal (SW16). It reads the UDiFF file
from 8 Jul 2024 and the legacy `content/historical/DERIVATIVES` file before that, and maps both
onto one `FO_BHAVCOPY_SCHEMA`. The mapping is vectorised in polars (a day is ~50,000 rows). The
legacy file's turnover is in lakh and is converted to rupees. Seven tests in
`test_nse_fo_bhavcopy.py`.

**Rejected.** A vendor (a purchase, and D10); scraping NSE's option-chain API (it is live-only, and
it is scraping).

**Reversal.** Remove the method, the schema and the test file. Nothing else reads them yet.

## PACK.3 — Non-negotiable 4 for derivatives: futures get a GTT, option structures are their own stop · ⚠ UNREVIEWED

**Context.** Non-negotiable 4: "Every buy gets a GTT stop the same session, vol-scaled 8–12 % via
`stop_from_vol()`." It was written for cash equities. Applied literally, an option spread's long
leg would get a GTT, and if that GTT fired it would leave the short leg naked, breaking Track C §1.
Applied to futures, "buy" misses the short side, which is the riskier one.

**Choice.** (a) Every future, long or short, gets a broker-side GTT stop the same session, at the
tighter of the sleeve's stop and `stop_from_vol()`. That requires one narrow gateway branch
(`02` §1). (b) A defined-risk option structure is its own stop: max loss fixed at entry, no GTT on
any leg, and option GTTs stay refused on derivative venues. **An agent may not settle a
non-negotiable's reading alone**, so this is QUESTIONS Q2, and no FO option order leaves paper
until Maulik answers.

**Rejected.** A GTT on the long leg (un-hedges the short); no stop on futures (plainly against
the rule); applying `stop_from_vol()`'s 8–12 % alone to futures (looser than the sleeve's own
stop in most names, which would make the rule weaker than the method).

**Reversal.** Maulik's answer replaces (a) or (b); the guard tests are re-pointed at it.
*(Settled by M.1: (a) and (b) as written, plus a close order at a loss of 1.5 × the credit.)*

## PACK.4 — No stock derivative is held into expiry day · ⚠ UNREVIEWED

**Context.** Stock F&O settles by physical delivery, and brokers ramp delivery margins through
expiry week (`07` §3).

**Choice.** Every stock-derivative position is flat by `E − fo_hard_exit_before_expiry`, counted in
exchange sessions. The default is `E−1` at 15:00. The research reports `E−1` against `E−4` per
sleeve (`RESEARCH.md`), and `04` §1 fixes each sleeve's value from it. Index positions settle in
cash and exit before 15:00 on expiry day.

**Rejected.** Holding to expiry and letting delivery happen. It turns a derivative sleeve into an
equity position the book has no rules for, and the cash needed is not in any budget.

**Reversal.** A config value. The guard (`02` §2.2) reads it.

## PACK.5 — One `fo_` schema, separate from `op_` · ⚠ UNREVIEWED

See `03`'s preamble. **Reversal.** Merge the tables into `op_` with a `clock` column. Nothing is
migrated yet.

## PACK.6 — The pack builds what the research supports: one paper sleeve, a data layer, an information page · ⚠ UNREVIEWED

**Context.** Maulik asked for a system across all four instrument families. `RESEARCH.md` found
that, on 2022–2026 end-of-day data at today's costs, every family except the index monthly condor
has negative expectancy, and most have none even before costs.

**Choice.** Build F1 (index monthly condor, carried overnight, paper) and the machinery to
re-test the rest: a nightly ingest, measured spreads, a quarterly re-test that reports and never
switches anything on. The stock families are not built.

**Rejected.** Building every family "for paper, to see". A scan that surfaces candidates from a
rejected family teaches its reader to trade it, and paper periods cost months of desk attention.
Also rejected: presenting OI buildup or IV rank as signals on the information page. They tested
flat or negative, so they are shown as facts (PACK.8).

**Reversal.** A family that the re-test finds positive three quarters running is written up here
for Maulik. Commissioning it is a new module with its own paper period. *(Superseded in part by
M.1: Maulik chose to build the long-only futures trend as F2, on paper.)*

## PACK.7 — F1 trades the rule as tested: no IV gate, no defensive exit · ⚠ UNREVIEWED

**Context.** Two refinements looked attractive after the fact. One was IV ÷ RV20 > 1.3 (+0.117R on
24 trades). The other was a defensive exit on a breached short strike.

**Choice.** Neither is used. The IV slice was found in-sample on 24 trades, so it is recorded on
every plan and tested on forward data. The defensive exit was tested (`RESEARCH.md` §B4): it cut
expectancy to +0.018R and moved the worst trade only from −1.02R to −0.95R.

**Rejected.** Trading the IV slice (selection on the outcome) and the defensive exit (tested
worse).

**Reversal.** Config fields `f1_min_iv_rv` (add; default none) and a defend flag, each needing its
own DECISIONS entry with forward evidence.

## PACK.8 — The Stock F&O page shows facts, never a candidate · ⚠ UNREVIEWED

**Context.** A page of IV ranks and OI buildups looks like a signal screen whatever its header
says.

**Choice.** It has no candidate, buy or sell column, and no green or red on any ratio. There is a
one-line banner with the research's verdict, and "Families tested and rejected" sits below the
table with the latest re-test beside each (`04` §5, `05` §3).

**Rejected.** A ranked "opportunities" list. It would be the misleading surface root CLAUDE.md's
11 Sep lesson describes.

**Reversal.** A re-test verdict change (PACK.6) is the only thing that can add a signal column.

## M.1 — Maulik's answers to Q1, Q2 and Q4, and the run's order (in session, 23 Sep 2026)

These are **Maulik's decisions**, not agent judgement, taken with the options and trade-offs in
front of him. Agents build on them and do not reopen them.

| Q | His answer | What it means in the pack |
|---|---|---|
| Q2: non-negotiable 4 for derivatives | **"Structure stop + close order"** | A defined-risk option structure gets no GTT: its max loss is fixed at entry. **In addition**, the desk closes the whole structure, shorts first, when the cost to close reaches **(1 + 1.5) × the entry credit**, a loss of 1.5× the credit (`f1_loss_close_mult`). Futures, long or short, get a broker-side GTT stop the same session. This settles PACK.3 |
| Q1: F1 capital | **₹10 lakh** | `fo_sleeve_config.capital_inr` for F1 is seeded at ₹10,00,000 by FO2's seed, for both underlyings together. At 1.0 % risk that is ₹10,000 max loss per structure (under the ₹25,000 ceiling). Seeding it moves no money: every execution flag stays false |
| Q4: build a rejected family anyway | **"Futures trend, long only"** | Sleeve **F2**, stock-futures breakout, long only, **paper only**. `01` §1b and `04` §10 |
| Order vs the OP run | **"Finish OP first"** | FO0 starts only after OP15 is ✅. The pack waits |

**The evidence he was shown and what it added** (run after his answers, so the pack states the
cost of each):

* **F1 with the 1.5× loss close** (`evidence/idx_stop15.txt`): n = 100, **+0.022R** (was +0.033R),
  t = 1.01, win 83 %, worst trade **−0.73R** (was −1.02R), max drawdown −1.35R (was −1.64R). By
  year: −0.010, −0.004, +0.078, +0.010, +0.047. At 1.0× the credit: +0.025R, worst −0.66R; at
  2.0×: +0.023R, worst −0.95R. **So the stop costs about a third of the edge and cuts the tail by
  about a quarter.** One consequence: 2022 turns slightly negative, so `02` §3 item 4 ("positive
  in three of five years, *including 2022*") is **not met by Tier 2E as it stands**. That gate
  item is Maulik's to waive in writing when the time comes, and the pack does not pre-waive it.
* **F2 re-costed with its rolls** (`evidence/research/res_f2.py`): a live long held for about 22
  sessions crosses an expiry on 0.96 of trades, and each roll at E−1 is another round trip.
  n = 2,334, **+0.017R** (t = 0.71), win 39 %. By year: −0.026, **+0.455**, −0.129, −0.143,
  −0.141. **Four of five years are negative**, and 2024–2026 lost steadily. F2 therefore carries a
  banner on every surface (`05`), and its real-money gate cannot be met on this Tier 2E alone.

## FO2.1 — One OP test constant moves: `OPTIONS_HEAD` is now `0052_fno` · ⚠ UNREVIEWED

`test_options_schema.py` migrates to and asserts the options schema's head. `0052_fno` alters two
`op_` columns (FO2.2), so it *is* an options migration, and the constant the file says "a later
options migration moves this, and only this" moved. No assertion was weakened. **Rejected:** a
separate `0053` for the widening so 0052 touches no `op_` table — a split for its own sake.
**Reversal:** none needed; the constant follows the head.

## FO2.2 — `op_contract.lot_size` and `op_expiry.lot_size` widen to integer · ⚠ UNREVIEWED

The widened master holds every stock underlying, and IDEA's lot (71,475) overflows `smallint`.
The O-sleeves read NIFTY only (proved by `test_fno_master_widened.py`), so nothing they compute
changes. **Reversal:** the downgrade narrows them back after deleting non-NIFTY rows.

## FO2.3 — `fo_ingest_day` holds each night's ingest state and the ban list; `fo_config_audit` is added · ⚠ UNREVIEWED

`04` §4 wants a day with no file by 23:30 shown `MISSING`, never interpolated, and `03` puts
`in_ban` on `fo_underlying_daily`, which is derived later. A small `fo_ingest_day` row
(`PENDING`/`INGESTED`/`MISSING`, the ban list for the next session) is what the status page and the
scan read; `in_ban` is copied from it when the series is derived. `fo_config_audit` mirrors
`op_config_audit`. **Rejected:** a `pipeline_run` row (that table is the screener's publication
contract). **Reversal:** drop the table; the ingest still writes `fo_contract_daily`.

## FO2.4 — `fo_sleeve_config` is keyed by flag group (F1, F2), not by sleeve code · ⚠ UNREVIEWED

M.1 gave F1 ₹10 lakh as one sleeve covering NIFTY and BANKNIFTY. Keying capital by `F1N`/`F1B`
would split or double it. The seed writes F1 ₹10,00,000, F2 ₹0, risk 1.0 %, max open F1 = 2 (one
per underlying), F2 = 5, one audit row per inserted sleeve. Positions, plans and journals still
carry the sleeve code (`F1N`, `F1B`, `F2`). **Reversal:** re-key with a migration.

## FO2.5 — The ban list is archived under the date it names, and a wrong-date file is refused · ⚠ UNREVIEWED

`NSEProvider.fo_ban_list(for_session)` reads `fo_secban.csv` through the NSE limiter and client and
refuses a file whose "Trade Date" is not `for_session` (SW16's rule); the nightly asks again next
hour. The file has no historical archive at NSE, so the ban list exists from the first night the
ingest runs, never backfilled.

## FO2.6 — A master dump with no NIFTY is refused whole; the change alert stays NIFTY-only · ⚠ UNREVIEWED

The widened refresh writes every underlying, but a dump missing NIFTY means the dump is wrong, not
that NIFTY delisted, and writing it would starve the O-sleeves. `OPTIONS_MASTER_CHANGED` keeps
alerting on NIFTY alone; other underlyings' changes are counted in the task result, so a
lot-size revision across 200 stocks is not 200 emails.

## FO2.7 — Retention keeps 10k–24k option rows a day, above `03`'s ~13,000 estimate · ⚠ UNREVIEWED

The rule (two nearest monthlies per underlying plus the index weeklies, non-zero OI or volume)
is `03`'s; the estimate was not. The rule wins; `03` §1 now says the measured range.

## FO1.1 — The CA flag uses the research's band: above 1.4 or below 0.7 · ⚠ UNREVIEWED

`04` §4 says "`|ret| > ln(1.4)`, or below `ln(0.7)`". Read literally the first clause already
covers every ratio below 1/1.4 ≈ 0.714, which would make "below ln(0.7)" dead text. The research
(`cont.py`) flags a ratio > 1.4 or < 0.7, and `03` §2 says "more than 30 %". The band is the
research's. `ca_recent` counts five sessions including the flagged one.

## FO1.2 — `covered` adds the broker book, the plan's filled legs and the order; FO6 must not double-count · ⚠ UNREVIEWED

`is_covered(book_after)` takes the book *after* the order. The helper adds the broker positions,
this plan's already-filled legs, and the order. **FO6 must pass the broker book without this
plan's own fills**, or a filled long counts twice and overstates the cover. FO6's tests assert it.

## FO1.3 — F2's time exit counts entry day as session 1 · ⚠ UNREVIEWED

`04` §10's "40 sessions" is counted the way `res_f2.py` counted, so the port reproduces n = 2,334.

## FO1.4 — Paper at ₹0 records `lots_at_ceiling` · ⚠ UNREVIEWED

`04` §3 says paper with ₹0 capital "runs one lot and records what the live size would have been".
With no capital there is no live size, so the record is the lots the ₹25,000 ceiling alone would
allow, named `lots_at_ceiling`.

## FO1.5 — F1's plan cost estimate assumes the exit at the entry mids plus 0.5 % slippage (min ₹0.05) · ⚠ UNREVIEWED

The round trip for `REJECTED_COST` needs an exit price before there is one; the entry mids with
the research's index slippage are the neutral guess.

## FO1.6 — `vol` prices options at their close with r = 0; the research used 6.5 % · ⚠ UNREVIEWED

`04` §4 says `r = 0` for `iv_atm`; the research's condor used 6.5 %. Each keeps its own: the stored
IV follows `04`, the research port stays verbatim so the golden holds.

## FO1.7 — `stop_from_vol` is re-implemented pure and tested equal to `baskfy_core.score.stop_from_vol` · ⚠ UNREVIEWED

The F2 GTT trigger needs it inside `baskfy_core.fno`; a test pins the two together so they cannot
drift.

## FO1.8 — The research port: A0, A1, A2, A4, B1, C1 and the basis study run and match; B2, B3, C2 are ported and not run · ⚠ UNREVIEWED

B4 reproduces **trade by trade** against the stored `condor_index_15_0.005_0.parquet`: n = 100,
+0.0334R (t 1.36), +0.0221R with the 1.5× loss close (worst −0.73R); F2 n = 2,334, +0.0171R;
`continuous` rebuilds `cont.parquet` exactly. The golden runs only with `BASKFY_FNO_RESEARCH_DIR`
set (the data is 1.9 GB and not in the repo), and skips loudly otherwise. FO9 runs B2/B3/C2.

## FO2.8 — `fo_underlying_daily`'s levels are anchored to the session's settle · ⚠ UNREVIEWED

FO1's `continuous_futures` compounds from 1.0 at the window's first session: a unitless index
whose base moves with the window. Stored at `numeric(18,2)`, an ATR of 0.03 rounds to zero, and
two nights' rows would sit on different bases. `baskfy_core.fno.underlying.derive_underlying`
rescales each night's window so `level_c` on the session equals the held contract's settle (ratio
back-adjustment, in rupees). `ret`, `rv20` and every ratio a signal reads are scale-free and
unchanged. A reader comparing levels across nights re-derives one window (the scan does).
**Rejected:** storing the unitless index (loses precision); a fixed base date (a new listing has
none). On the 22 Sep 2026 bhavcopy: 216 underlyings, NIFTY 23,456 with IV 10.1 % vs RV20 7.1 %,
54 thin names with no IV (either leg did not trade — null by `04` §4).

## FO3.1 — A one-sided or crossed quote is stored, with the missing side or the mid null · ⚠ UNREVIEWED

The sample exists to measure what a fill costs, and "there was no bid at 15:00" is a measurement.
A one-sided or empty book is a row with the missing side null and a null mid. A crossed book keeps
both prices and a null mid. A key Kite does not answer writes no row. The median statistic reads
two-sided, uncrossed rows only.

## FO3.2 — The top 30 are ranked on the median futures turnover of the last 20 ingested sessions · ⚠ UNREVIEWED

Read from `fo_contract_daily` directly, so the sample does not depend on the derivation having run
that night.

## FO3.3 — "Near monthly" is the first monthly strictly after today · ⚠ UNREVIEWED

On an expiry day the expiring contract's 15:00 book is an unwind, not a spread a new position would
pay; the sample reads the next month.

## FO3.4 — ATM is the strike nearest the previous session's futures settle · ⚠ UNREVIEWED

`op_contract` holds options, not futures, and a futures quote per name would widen the call.
± 3 strikes around yesterday's settle still spans today's ATM on all but a > 3-strike move.

## FO3.5 — One call of ≤ 500 keys; more is split and counted · ⚠ UNREVIEWED

32 names × 7 strikes × 2 types = 448 keys. A widened list is split rather than truncated, and the
task result counts the calls.

## FO3.6 — The results calendar (Q8) has no table yet · ⚠ UNREVIEWED

The task reads `NSEProvider.results_calendar` forward for the sampled names; its record is the NSE
raw archive plus the task's JSON result. A queryable table needs a migration, and FO4 (the scan,
which reads events) owns it. **Reversal:** none needed.

## FO3.7 — Outside 09:15–15:30 the task refuses · ⚠ UNREVIEWED

A 15:00 Beat entry that runs late (a worker restart) must not sample a closed book as if it were
15:00.

**A note on history.** The first draft of `spreads.py` went into `418809d` (FO2's wiring commit)
because that commit staged the worker directory whole while FO3 was being written. It is left in
place rather than rewriting history; FO3's commit carries the finished file.

## FO6.1 — The covered predicate is injected into the gateway, fail closed · ⚠ UNREVIEWED

**Context.** `packages/execution` does not import `baskfy_core` (`tenancy.py`, `gtt.py`), and FO1's
`covered.uncovered` must not be re-coded. **Choice.** `OrderGateway(coverage=...)` takes the
predicate as a callable (the same pattern as `ProductGates`); the FO gateway wires
`baskfy_core.fno.covered.uncovered`. With nothing wired, every option order carrying a `fo_plan`
reference is refused. The gateway itself composes broker + plan fills + order. **Rejected.** Making
execution depend on core (breaks a stated layering); a copy of the predicate (two rules that could
drift). **Reversal.** Import `uncovered` in `guards.py` and drop the parameter.

## FO6.2 — The `fo_plan` reference must describe the order it rides on · ⚠ UNREVIEWED

`FoPlanRef(plan_id, sleeve, step, broker_positions, plan_filled)`. The gateway refuses a reference
with no plan id, an unknown sleeve, a venue other than NFO, an F1 reference on a non-option or an F2
reference on a non-future, and an option order whose `step` is not that order (signed quantity,
underlying, strike, type, and an exact expiry code between them, so `NIFTY` cannot match
`NIFTYNXT50` and strike 25000 cannot match 125000). Stricter than `02` §1 states; nothing in force
sends a `fo_plan` yet. A `fo_plan` option under MIS still meets the cover rule.

## FO6.3 — `covered.net_of_plan` is the FO1.2 contract as a function · ⚠ UNREVIEWED

Kite reports net quantity per contract, not per plan. `net_of_plan(raw, plan_filled)` removes the
plan's fills so `book_after` adds them once (tested: the naive composition would admit a short twice
its cover; the gateway refuses it). A broker book lagging a fill leaves a short residue, which only
makes the guard stricter. `broker_positions` is documented as the NRML option book: an MIS long from
another book is squared off at the close and must not count as overnight cover (FO7/FO8 owe this
filter when they read `positions()`).

## FO6.4 — The F2 GTT branch: stock futures only, leg NRML · ⚠ UNREVIEWED

A GTT with a `fo_plan` reference is admitted only for an F2 reference, on NFO, for a future whose
underlying is not an index (`INDEX_UNDERLYINGS`), with `OPTIONS_ENABLED` and
`BASKFY_FNO_CARRY_ENABLED`; its leg is `NRML` (`gtt_order_leg(product=...)`, default CNC unchanged).
An option GTT is refused before the reference is even read, under every switch and every plan.
`modify_gtt_quantity` and `delete_gtt` are unchanged; F2's evening trail (FO7) needs a
trigger-moving modify that keeps the NRML leg, which does not exist yet.

## FO6.5 — `fno_gates` hands the gateway `intraday_enabled=False` in every row · ⚠ UNREVIEWED

Mirrors OP2.3 otherwise: with the sleeve's flag off the gate is PAPER with `dry_run=True` and both
derivative switches true (a paper confirm runs the real gateway path, simulated whatever `DRY_RUN`
says); with it on but not all four, the real `OPTIONS_ENABLED`/`BASKFY_FNO_CARRY_ENABLED` pass
through so the gateway refuses a half-flip. `INTRADAY_ENABLED` is never read, so no FO gate ever
admits MIS. The desk's weekly gateway (`_gates_from_config`) does not read the carry flag (tested).

## FO4.1 — A proposal that fails a pure check is written with the plan's `REJECTED_*` name; no CHECK, no 0053 · ⚠ UNREVIEWED

`04` §8's scan vocabulary has no word for "entry day, but the structure the bhavcopy proposes
would be refused". Writing `CANDIDATE` would misstate it, so the scan writes the `PlanState` the
plan would reach (`REJECTED_STRUCTURE`, `REJECTED_LIQUIDITY`, `REJECTED_SIZE`, `REJECTED_COST`)
with every reason by name. `fo_scan.state` stays plain text as FO2 left it: no CHECK and no
migration, so no schema head constant moves. **Reversal:** a later migration may CHECK the union.

## FO4.2 — The scan runs for every user with `fo_sleeve_config` rows, chained in `run_night` inside a savepoint · ⚠ UNREVIEWED

The FO run's tenants are the users the seed wrote. `run_night` calls the scan after each
`INGESTED` night (so the hourly retries re-run it idempotently); a scan that raises is logged and
returned in the result, and the ingest is kept. `baskfy.fno.scan` exists for a re-run by hand
behind `BASKFY_FNO_SCAN_ENABLED` and has no Beat entry; `fno_cli scan --date` is the operator's.
**Rejected:** a separate 23:45 Beat entry — a second clock for one fact.

## FO4.3 — When F2 cannot be evaluated at all, one row with symbol `*` says why · ⚠ UNREVIEWED

A night not ingested, or a window with no series, has no per-stock answer. One `NO_DATA` row for
the sleeve, reason in words, rather than no rows (which reads as "nothing signalled").

## FO4.4 — F2 writes a row for every universe name, `NO_SIGNAL` included; names outside the universe write none · ⚠ UNREVIEWED

~130 rows a night is small, and "why is X not a candidate" is the question the page gets. A name
inside the corporate-action exclusion is `NO_SIGNAL` with that reason; a window too short to
answer is `NO_DATA`. Open F2 positions always get an `OPEN_POSITION` row.

## FO4.5 — "NSE industry" is the narrowest NSE sector index the cash instrument is in · ⚠ UNREVIEWED

`instrument` carries no industry. The scan reads `index_member_daily` on the latest date ≤ the
scanned session over `SECTOR_INDEX_SLUGS`, narrowest index wins (the swing book's rule). A name in
no sector index is capped only by `f2_max_open`. Candidates are admitted in order of 20-session
median futures turnover. **Reversal:** store NSE's industry on `instrument` and read it instead.

## FO4.6 — No results-calendar table: F1 reads `op_event_day`, F2 has no results skip · ⚠ UNREVIEWED

FO3.6 handed the table to FO4. It is not added: `01` §1b and `04` §10 give F2 no event rule, the
tested rule (`res_f2.py`) had none, a 40-session hold contains a results date for nearly every
name, and FO3 collects the calendar for the top 30 only, so a skip would be partial. Q8's answer
stays open for FO5 to show a name's next results date. F1's event window is entry session through
hard-exit session, inclusive; the proposal is still written on `SKIPPED_EVENT` so it can be
accepted on the plan (`01` §1).

## FO4.7 — F1's proposal is priced on the scanned session's bhavcopy · ⚠ UNREVIEWED

`F` = the target monthly future's settle; `σ` = `atm_iv` on that monthly at the session's closes;
`T` = calendar days from the entry session to expiry; credit from the legs' settles. Listed
strikes from `op_contract`, else the bhavcopy's. Liquidity is what a closing file proves: each leg
printed, each short's OI ≥ 500 lots (OI in units ÷ lot), each wing traded; the live spread and the
basket margin stay with the 09:20 plan (FO7). Sizing is `Mode.PAPER` because F1 paper sizes
exactly as live. On the seeded ₹10 lakh a NIFTY lot (75) with a ~250-point max loss is
`REJECTED_SIZE` — the ceiling working, as `04` §10 says of F2.

## FO4.8 — F2's candidate numbers · ⚠ UNREVIEWED

Entry reference = the chosen contract's settle on the scanned session; ATR14 in rupees by FO2.8's
rescale on the held settle; the GTT's `ann_vol` = RV20; the cost prices the exit at the entry (the
FO1.5 analogue); `cost_share` = round trip ÷ one lot's R, in percent. With capital > 0 a zero-lot
size is `REJECTED_SIZE` and takes no capacity; at ₹0 the row is `CANDIDATE` (paper, one lot) and
says when live would be `REJECTED_SIZE` (`lots_at_ceiling` 0). A ban list not yet stored for the
next session leaves candidates standing with that reason; the 09:20 plan re-checks.

## FO4.9 — Pauses read `fo_journal` for both modes; the month is the next session's; a book amount of 0 is "not set" · ⚠ UNREVIEWED

The paper period tests the rules as live would apply them, so paper trades count. `04` §7's
"calendar month" is that of the session the rows describe. `fo_book_config.monthly_pause_inr` is
seeded 0 and FO10 owns what 0 means; until then the book pause fires only on an amount set.

## FO4.10 — `fno_cli` imported `trading_days` from a module that never defined it · ⚠ UNREVIEWED

At `29483a3` `python -m baskfy_worker.fno_cli` failed on import (`backfill.trading_days` does not
exist). It now imports it from `baskfy_worker.fno.underlying`, and a test runs the CLI's `scan`.
