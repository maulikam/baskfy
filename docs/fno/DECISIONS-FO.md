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

## M.3 — The F1 confirm sentence names the loss close (Maulik, in session, 25 Sep 2026)

FO8.5 found that `05` §4's sentence ("…the 50 % profit take and the E−1 15:00 exit of this
structure. It places nothing else.") predates M.1's loss close, which also fires under the
confirm, so the sentence understated what the click authorises. Asked with options, Maulik chose to
name it: **"This confirm also authorises this structure's 50 % profit take, its loss close at 1.5×
the credit (shorts first), and its E−1 15:00 exit. It places nothing else."** `05` §4, the desk's
`CONFIRM_SENTENCES` and the page test carry it.

## M.4 — The scan and the monitor are on on the box; no backfill yet (Maulik, in session, 25 Sep 2026)

After FO12 deployed, asked with options, Maulik chose first "scan off, monitor on". The monitor
raises plans only from `fo_scan` and marks from the ingested bhavcopy, both behind
`BASKFY_FNO_SCAN_ENABLED`, so that pair would idle; asked again, he chose **both on, no
backfill**. Done 25 Sep 2026 ~03:55 IST: `.env.staging` gained `BASKFY_FNO_SCAN_ENABLED=true` and
`BASKFY_FNO_MONITOR_ENABLED=true`, `.env.staging.compose` gained `BASKFY_FNO_MONITOR_ENABLED=true`
(both backed up beside themselves), `fno_cli seed` wrote F1 ₹25,00,000 / F2 ₹0 for user 1, and
`verify-fno.sh` read **FNO OK**. Every FO money flag is unset (false). History starts with the
25 Sep bhavcopy: F2's 50-session signal needs ~50 nights before it can fire, and the re-test and
the IV percentile stay thin until a backfill runs (`fno_cli backfill`, ~90 min at the NSE limiter).
**Reversal:** delete the lines and `up -d worker ingest-worker beat desk fno-monitor`.

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

**Corrected by FO10 (25 Sep 2026).** Pooling paper and live broke `03` §6 ("never pooling
`(sleeve, simulated)`"): the scan now reads the paper journal only (FO10.1), and 0 means the
₹75,000 ceiling, not "not set" (FO10.5). The month rule stands.

## FO4.10 — `fno_cli` imported `trading_days` from a module that never defined it · ⚠ UNREVIEWED

At `29483a3` `python -m baskfy_worker.fno_cli` failed on import (`backfill.trading_days` does not
exist). It now imports it from `baskfy_worker.fno.underlying`, and a test runs the CLI's `scan`.

## FO9.1 — The re-test registry is `RESEARCH.md`'s table plus F1 and F2 · ⚠ UNREVIEWED

Fifteen families: B4, B4 with the loss close (F1), B1, B2, B3, C1, C2, F2, A0, A1, A1R, A2, A3,
A4 and E, each with the parameters of the script call that produced its `RESEARCH.md` row
(`res_futures.py` names each futures family's horizon and ATR multiple; the stock option rows are
the stored parquets' names). Rebuilt from the raw day files through the worker's own panel
function, every one reproduces: B4 n = 100 +0.0334R, loss close +0.0221R, F2 n = 2,334 +0.0171R,
B1 −0.0318R (gross +0.027), B2 −0.042, B3 −0.055, C1 −0.236, C2 −0.105, A0 −0.023, A1 −0.051,
A2 −0.004, A4 −0.059, E median carry 5.6 % a.y. with 0.7 % of stock-days clearing 7 % net. The
panels equal `futures.parquet` and `options.parquet` exactly. The golden is a test gate
(`test_fno_retest.py`), run with `BASKFY_FNO_RESEARCH_DIR`.

## FO9.2 — How measured spreads become one number per family · ⚠ UNREVIEWED

The research ran one slippage per family, so the measured replacement is one number too. Index
families take the **worse** of NIFTY's and BANKNIFTY's medians, and only when both have 20
sessions. Stock option families take the median of the per-name medians once 20 names have 20
sessions (FO3 samples the top 30). Futures families and the carry study stay `ASSUMED`: FO3
samples options only. **Rejected:** per-name slippage inside the port (it changes the research's
code, and the golden would no longer pin it).

## FO9.3 — Stock option families run a batch of symbols at a time · ⚠ UNREVIEWED

B1 over the whole option panel peaks at **2.8 GB**. The box has 7.8 GB, about 3.8 GB free, and no
swap, and it has been taken down by memory once (NEEDS-MAULIK §35). Every family's state is per
symbol, so running ten symbols at a time gives **exactly** the whole panel's trades (tested: a
batch of 7 equals the whole panel to the fourth decimal), at 1.6 GB peak on the Mac for the whole
suite including the futures panel. The worker pages each batch out of Postgres from a thread.

## FO9.4 — A family with no trades stores n = 0 and a null expectancy · ⚠ UNREVIEWED

The first quarters after a partial backfill will have families with nothing to trade. "0.0R" would
read as a measured zero. The row says n = 0 with net and gross R null, and the carry study
before any UDiFF day says so the same way.

## FO9.5 — The quarterly run is the first Saturday of Jan/Apr/Jul/Oct at 06:00, on the compute queue · ⚠ UNREVIEWED

Outside the session and the 18:30–23:30 nightly window, and `run_retest` refuses a second run in
the same month unless forced (`fno_cli retest --force`). Behind `BASKFY_FNO_SCAN_ENABLED` like the
rest of the data layer. It writes one row per family per FO tenant and moves nothing else.

## FO5.1 — Capital and risk % are settings on `PATCH /fno/config`, under the ceilings · ⚠ UNREVIEWED

`03` §6 names "per-sleeve capital, risk %, max concurrent positions, and the book's loss pause" as
the config, "every write through settings_audit", and `05` §4 puts "capital, risk % and pauses" on
"the settings form, audited"; `options_settings` already carries `sleeve_capital_inr` the same way.
"Money-free" is read as: the write cannot build, size into, confirm or place an order. The patch
takes `capital_inr`, `risk_per_trade_pct`, `max_lots` (≤ 10), `max_open_positions` (F1 ≤ 2, one per
underlying; F2 ≤ 10) and `paper_enabled` per group, and the book's `monthly_pause_inr`; capital ×
risk % ≤ `BASKFY_FNO_RISK_PER_TRADE_INR_MAX`, risk % ≤ `…_RISK_PCT_MAX`, pause ≤
`…_BOOK_MONTHLY_LOSS_INR_MAX`, each refusal naming its env var; atomic across parts; one
`fo_config_audit` row per moved field. `paused_*` and every flag are not fields. **Rejected:**
capital as env-only (contradicts `03` §6). **Reversal:** drop the two fields from `FnoSleevePatch`.

## FO5.2 — The API lives at `/fno/*`; the pages at `/options/overnight` and `/options/fno` · ⚠ UNREVIEWED

`test_options_readonly.py` pins `/api/v1/options/*` to OP5's exact list; FO routes under it would
reopen that contract. Three paths: `GET /fno/overnight`, `GET /fno/info`, `GET|PATCH /fno/config`.

## FO5.3 — The sub-nav is its own row above the intraday tabs · ⚠ UNREVIEWED

`OPTIONS_SUBNAV` (`Intraday (NIFTY)` · `Overnight` · `Stock F&O`) is drawn first on every `/options`
page; `SECTION_TABS.options` (Today | Journal | Calendar) is unchanged and still drawn on the
intraday pages, whose routes light "Intraday (NIFTY)" by the longest-prefix rule.

## FO5.4 — The 1-year IV percentile needs 200 stored sessions · ⚠ UNREVIEWED

Share of the stored `iv_atm` values in the 365 days to `as_of` that are ≤ that day's value, shown
only when ≥ 200 sessions have one; otherwise null, with the session count on the page. A
"percentile" of a few months is a different number with the same name.

## FO5.5 — No live overlay on `/options/fno` · ⚠ UNREVIEWED

`05` §3 *allows* the price column the four-screen-pages overlay. `04` §5's price column is the
**futures settle**; the shared overlay is a cash last price, and putting one in a settle column
mislabels it. Every column stays `As of close`. **Reversal:** add a separate "last (live)" column.

## FO5.6 — Research text is quoted in `fno_read`, not read from `docs/` · ⚠ UNREVIEWED

The API image does not ship `docs/`. The B4 line, the loss-close line, the F2 line, `07` §4's Tier 2E
caveat (verbatim) and the verdict table are constants; σ is written "sd" and the minus sign as "-"
(ruff's confusables rule). B4's own 0.5 % index slippage is stated beside the verbatim caveat, which
says 3 % (the stock-option default). The page links "the research" to its own verdict section
(`#families`), because the web app serves no `RESEARCH.md`.

## FO5.7 — The verdict rows read FO9's `fo_backtest_run.family` codes · ⚠ UNREVIEWED

The codes are `baskfy_core.fno.retest.FAMILIES`' keys: B4 ↔ `B4`/`B4_LOSS_CLOSE` (the evidence card
shows both); the long-only trend ↔ `F2`/`A0_LONG`; both sides ↔ `A0`; OI filters ↔
`A1`/`A1R`/`A3`; then `A2`, `A4`, `B1`, `B2`, `B3`, `C1`, `C2`, `E`. The newest run of any of a
row's codes is shown beside it, with its caveat. A family FO9 adds later needs a row here.

## FO5.8 — Small reads, stated · ⚠ UNREVIEWED

* The underlying's level is the index price the bhavcopy prints beside the index future (UDiFF
  days only); the live overlay reads it as `NIFTY 50` / `NIFTY BANK`.
* R on an open position is the settle mark's P&L ÷ `fo_position.max_loss_inr`; null (shown "—")
  when the desk has not recorded one — F2 until FO7 writes it.
* Days to the near monthly: calendar days from `as_of` to the first monthly strictly after it in
  `op_expiry` (withdrawn excluded; FO3.3's rule); its lot, else the bhavcopy's futures lot.
* The paper tally counts closed paper trades, distinct opened months (F1) and rolls (F2); rule
  violations are `null` ("not counted yet") until FO10's ledger exists.
* The web app gets no FO settings form; the PATCH is for the desk's settings (FO8) and by hand.

## FO7.1 — The FO gateway is paper-pinned; a LIVE sleeve is refused before any order · ⚠ UNREVIEWED

**Context.** FO7 builds the executor FO8 will call. A live fill has to be read back from the
broker's order book and a live stop from its GTT book; neither is built. **Choice.**
`fno_execute.build_fo_gateway` hands the gateway `fno_gates.product_gates(sleeve)` with `dry_run`
forced true (in PAPER it is true already, FO6.5), and `execute_entry` answers `LIVE_NOT_BUILT`
when all four switches are on (the options pack's OP10.3). Every leg still runs the real guards,
the covered-overnight guard, risk, the rate limit and the journal. **Rejected.** Passing a LIVE
gate through to a path that cannot track a real fill. **Reversal.** Drop the `replace(...,
dry_run=True)` in `paper_gates` when the live read-back is built (and its tests say so).

## FO7.2 — F1 is sent by the options pack's pure executor, with O1's rules · ⚠ UNREVIEWED

`baskfy_core.options.executor.run_entry`/`run_exit` already are `04` §2's sequences (longs first;
abandon on a short protecting leg and close the filled longs; shorts first on exit; a protecting
long never before its short) and assert never-naked before each attempt. Its only sleeve-dependent
rule is which close is risk-reducing (the marketable third attempt), and for a condor that is O1's
reading. The desk's `OptionVenue` sends each attempt with a `FoPlanRef` carrying the order as its
`step` and the plan's held legs, so the gateway re-proves every prefix too. **Rejected.** A second
copy of the sequencing in `baskfy_core.fno`. **Reversal.** Give the executor an FO rule set.

## FO7.3 — The F2 trail moves its GTT through `modify_gtt_quantity(fo_plan=, floor_trigger=)` · ⚠ UNREVIEWED

FO6.4 left the trigger-moving modify to FO7. With a `fo_plan` reference the modify meets
`fo_gtt_refusal` (the F2 stock-future branch `place_gtt_stop` rests the stop under; an option GTT
refused whatever the switches), its leg stays `NRML`, and `floor_trigger` — the trigger it
replaces — is **required**; a trigger below it is refused before anything is sent ("a trail is
never lowered", `04` §10, now enforced at the gateway as well as in `monitor.f2_trail_step`).
Without `fo_plan` the method is byte-for-byte what it was (`test_gtt_modify_and_cancel.py` green).
**Reversal.** Remove the two keywords; `test_fno_gtt_trail_modify.py` goes with them.

## FO7.4 — On paper the broker book is empty; a live reader nets the plan's fills out · ⚠ UNREVIEWED

`FoPlanRef.broker_positions` is the broker's NRML option book net of the plan's own fills. On paper
the broker holds none of the plan's legs, so there is nothing to net (and `net_of_plan` over an
empty book would count each paper fill as a short, refusing every short leg); nor is the real
account's NRML book cover for a simulated short. `paper_book` is therefore empty and a paper plan
must cover itself. `live_book(raw)` is the reader a live path would pass. **Reversal.** Pass a
different `broker_book`.

## FO7.5 — A paper GTT's handle is `PAPER-<position id>`; a paper stop "fires" on the monitor's check · ⚠ UNREVIEWED

The dry-run GTT has no exchange id, and the evening modify needs one (the gateway refuses a
non-positive-integer trigger id). The paper handle is the position id, addressed as an integer to
the dry-run branch only (the gateway is paper-pinned, FO7.1). On paper nothing rests at an
exchange, so the monitor's 60-second check of the live last price against the stop is the GTT
firing: an EXIT plan (`STOP`) sold through the paper gateway. A real trigger id would be deleted
when a position closes for any other reason; a paper handle rests nowhere and is not.

## FO7.6 — Plan ids are deterministic per user: `<sleeve>-<yyyymmdd>-<symbol>-u<user>` · ⚠ UNREVIEWED

`fo_plan.plan_id` is unique across the table (P4.1: every row carries a user), so an id without the
user would let one user's plan block another's. Exits are `<entry>-X`, rolls `<entry>-R<n>`. Each
raise is a no-op when its id exists (house rule 7).

## FO7.7 — A rejected plan with zero lots stores `lots = 1`; the sized count is in `detail` · ⚠ UNREVIEWED

`04` §8 wants every `REJECTED_*` plan written with its reason; `fo_plan.lots > 0` is a table
constraint. A `REJECTED_SIZE` plan is never confirmable (the confirm refuses anything not
`ISSUED`), so `lots` is inert there and `detail.lots_sized` carries the true 0. **Reversal.** Relax
the constraint to `lots >= 0` by migration.

## FO7.8 — F2's live plan: entry at the ask, stop on the fill, the ATR and vol from the scan · ⚠ UNREVIEWED

At 09:20 the plan is priced at the live ask (what a buy pays); the fill re-computes the stop
(`entry − 3 × ATR14` at the signal close's ATR) and the GTT trigger (`max(stop, stop_from_vol)`,
with the scan row's `rv20` as the annual vol). F2 plans are raised and expire on F1's window
(09:20; `min(issued + 30 min, 10:30)`): `02` Track C §3 asks the same 30 minutes of every entry.
F2 trades the **previous session's** scan only. A partially filled future is sold back at once
(`ABANDONED_PARTIAL`): no half-sized carry.

## FO7.9 — F2's trail reads the held contract's close; the stop is carried unchanged through a roll · ⚠ UNREVIEWED

The research trails on the continuous series; the desk holds one contract, and within it the two
agree. Across a roll the highest close and the stop are carried as they stand (`02` Track C §5:
"stop carried"), not rebased by the calendar spread. The mark after a roll is the rolls' realised
P&L plus the current contract's settle against its own entry. **Reversal.** Rebase on the roll's
two fills.

## FO7.10 — An unreadable basket margin refuses the plan (`REJECTED_MARGIN`) · ⚠ UNREVIEWED

Margin is a ceiling the plan must be shown to fit under (`04` §3). If `basket_order_margins` or the
free margin cannot be read, the plan is refused with that reason rather than issued unproven. The
figure used is the basket's `final.total` (the hedge benefit counted) against the equity segment's
`net`.

## FO7.11 — Plan states around a position · ⚠ UNREVIEWED

An EXIT plan is written `CONFIRMED` with the entry's `confirmed_at` and `parent_plan_id` (the confirm
it runs under), then `FILLING` → `CLOSED`; the entry goes `OPEN` → `EXITING` → `CLOSED`. A ROLL plan
is `ISSUED` → `FILLING` → `OPEN` (`04` §8). A roll that sells the held month but cannot buy the next
leaves the position flat (`ROLL_INCOMPLETE`, alerted) — the safe side. A partial exit stays
`FILLING` and is tried on the next pass, never naked.

## FO7.12 — Where the carried state lives · ⚠ UNREVIEWED

`fo_position.legs` is `{"legs": [...], "carry": {...}}`: the legs with their fills, and F1's strikes
or F2's trail stop, highest close, ATR at entry, vol, time-exit date, rolls and realised P&L.
Paper-checklist violations (`LATE_EXIT`, `NAKED_FUTURE`, `04` §9) append to the entry plan's
`detail.violations` (there is no violations table and `fo_position` has no `detail`). FO10's
journal reads both. The attempt counter behind each client id is `detail.attempts` on the plan, so
an id is never reused across passes or restarts (the gateway would answer `DUPLICATE`).

## FO7.13 — Marks fire nothing; the monitor's process runs 09:14 to 23:30 · ⚠ UNREVIEWED

`04` §1: the settle is checked "for the mark", and "the exit plan fires on the live check". A night
records whether the profit take or loss close would have hit at the settle (`fo_mark.detail`) and
raises nothing. Because the book is marked after the 18:30 bhavcopy, `scripts/fno_monitor_loop.py`
keeps the day's window open to 23:30 and restarts a crashed monitor inside it; compose is FO12's.
The desk's Postgres adapter hands `numeric` back as `float`; the store re-quantizes price columns
at their storage precision.

## FO8.1 — The plan card overlays the live book when the desk has a Kite session · ⚠ UNREVIEWED

`05` §4 asks for the legs "re-priced from live quotes" with bid/ask, spread % and OI. The plan's
stored bid/ask are the monitor's 09:20 read (`fo_leg`); `fo_leg` has no OI column. `GET /fno`
therefore overlays `kite_quotes` (bid, ask, OI, live credit on mids) when the desk is signed in,
and otherwise shows the 09:20 figures with the reason ("no Kite session; legs show the 09:20
pricing"). OI is live-only. The overlay reads; it moves nothing. **Reversal.** Drop `view_sources`.

## FO8.2 — "Max loss in R" and "free margin" are read from what FO7 stores · ⚠ UNREVIEWED

R on the card is `max_loss_inr ÷ risk_budget_inr` (how many sleeve risk budgets the structure can
lose). Free margin is not a column: the card shows the monitor's `margin_check` sentence
("₹X within free ₹Y") beside `margin_required_inr`. **Reversal.** Store `free_inr` on the plan.

## FO8.3 — The nav badge: open positions and the nearest hard-exit date, cached a minute · ⚠ UNREVIEWED

The date is the earliest of F1's `hard_exit_date` and F2's `time_exit_date` (an F2 `E - 1` roll
carries the position and is not an exit). `fno_desk.nav_badge` is a Jinja global read by every
desk page, cached 60 s, and never raises (the modes alone show if the book cannot be read). The
tab's tooltip names each sleeve's PAPER/LIVE from `fno_gates()`; a LIVE sleeve turns the tab's
chip red. The desk nav had no `NIFTY Options` tab, so it gains one beside `F&O Overnight`.

## FO8.4 — A missing confirm is refused by name; the POST answers JSON · ⚠ UNREVIEWED

`confirm` and `plan_id` default to `""`, so a missing confirm is `400 CONFIRM_REQUIRED` before any
read (not FastAPI's anonymous 422), and an unknown plan `404 UNKNOWN_PLAN` before a gateway is
built. The answer is JSON, as `/nifty-options/execute`'s; the page's script shows the outcome or
the refusal code inline, never a spinner. A non-`OPEN` outcome carries `code` (the outcome) and
`detail` (the gateway's refusals, e.g. `RISK_BLOCKED KILL SWITCH: ...`). The client id is FO7's
`plan_id:tradingsymbol` (a retry `:R<n>`), so `plan_id:leg` is the leg's tradingsymbol; a second
post is `409 NOT_ISSUED` from the atomic `ISSUED → CONFIRMED` update.

## FO8.5 — F1's confirm sentence is `05` §4's verbatim, and it omits the loss close · ⚠ UNREVIEWED — needs Maulik's wording

"This confirm also authorises the 50 % profit take and the E−1 15:00 exit of this structure. It
places nothing else." FO7's monitor also fires the **1.5 × credit loss close** (and `LATE_EXIT`)
under the same confirm (`04` §1), so "It places nothing else" understates what the click
authorises. The page renders the spec's sentence unchanged (an agent does not rewrite a sentence
the operator confirms under); the fix is a one-line change to `fno_desk.CONFIRM_SENTENCES` and
`test_fno_desk.F1_SENTENCE` once the wording is decided.

## FO10.1 — Pauses and ledgers are per `(sleeve, simulated)`; a stored pause names its mode · ⚠ UNREVIEWED

**Context.** FO4.9 counted paper and live journal rows together for `04` §7; `03` §6 says the
journal never pools `(sleeve, simulated)`. **Choice.** `baskfy_core.fno.ledger.evaluate_pauses`
takes a `simulated` and reads that mode's rows only; `build_ledger` returns one line per
`(sleeve, underlying, simulated)` and one book per mode; `figures` raises `PooledRows` on mixed
rows. A pause the ledger writes carries its mode in `paused_reason` (`PAPER:F2_MONTH_R`,
`LIVE:BOOK_MONTH_INR`) and binds that mode only; a reason without a mode prefix is hand-set and
binds both (`column_pause_applies`). The worker's scan evaluates **paper**
(`baskfy_worker.fno.ledger.ENTRIES_SIMULATED = True`: every entry is paper while FO7.1 stands); the
desk's monitor evaluates the mode `fno_gates` gives the sleeve now. **Rejected.** A per-mode column
(needs 0053 under `services/api`, which another session holds). **Reversal.** Flip
`ENTRIES_SIMULATED` (or read the desk's switches) when the live read-back is built.

## FO10.2 — F1's pause is per underlying, stands until Maulik lifts it, and lives in the audit trail · ⚠ UNREVIEWED

`04` §7 gives F2's pause an end (the month) and F1's none, so F1's stands until lifted: only closes
after the latest `fo_config_audit` row `lift:F1N` / `lift:F1B` count toward the next run
(`baskfy_worker.fno.ledger.lift_f1_pause`, for FO5's settings form). `fo_sleeve_config` is keyed by
the group `F1` (FO2.4), so writing `paused_until` there would pause BANKNIFTY for NIFTY's losses;
the desk records the pause as one audit row `pause:F1N` (`new_value = PAPER:F1_LOSS_RUN`) per run
and enforces it from the journal (scan and monitor). **Rejected.** An automatic end (a cycle,
a month): not in `04`, and three max-ish losses in a row are a reason to look. **Reversal.** Give
`FoPause.paused_until` a date for F1 in `evaluate_pauses`.

## FO10.3 — One journal row per structure; each roll's costs itemised inside it; R is the planned max loss · ⚠ UNREVIEWED

`fo_journal` is keyed on `position_id` (`03` §6: "one row per structure"), and an F2 roll carries
the position (FO7.9), so a roll is not its own row: `detail.costs.rolls[]` holds each roll's plan,
day, sold and bought turnover and **its own** two-order costs, and `rolls` counts them. Costs are
from the fills: F1 legs pay `docs/options/04` §6.1 (`options.costs.charges`), futures `04` §10's
per-order charges **with the slippage line at zero**, because a paper fill walks the depth and a
live one pays the spread — the 0.03 % is already in the price. R is `fo_position.max_loss_inr`
(the condor's max loss on the credit taken, the future's entry-to-trigger risk; `RESEARCH.md`
"P&L ÷ the planned maximum loss"), falling back to the entry plan's. `r_multiple` is rounded half
up to the column's two places at write (house rule 8), so −0.605R is stored and paused as −0.61R.

## FO10.4 — An abandoned entry that filled is journalled through a closed position · ⚠ UNREVIEWED

An `ABANDONED_PARTIAL` entry whose longs filled and were sold back cost real money, and
`fo_journal.position_id` is a non-null key. The desk writes one `fo_position` opened at the first
fill and closed at once (`closed_reason = ABANDONED_PARTIAL`), attaches the fills, and journals it.
It is never "opened" on the paper checklist and neither counts toward nor breaks F1's loss run; it
counts in the book's rupees and F2's month R. FO7's replay asserted "no `fo_position` row"; it now
asserts no **open** position and one closed `ABANDONED_PARTIAL` row (`test_fno_monitor.py`). An
abandon that left a leg open is not journalled (it is not flat; FO7 alerts it). **Reversal.** Drop
`_journal_abandoned` in `fno_execute` and restore the assertion.

## FO10.5 — `fo_book_config.monthly_pause_inr = 0` means the ₹75,000 ceiling · ⚠ UNREVIEWED

The seed writes 0 (FO2) and FO4.9 left the meaning to FO10. `04` §7 calls the amount a ceiling-bound
limit; "0 = off" would let the seed disable the book's loss limit, which a ceiling exists to
prevent. So 0 (not set) is `BASKFY_FNO_BOOK_MONTHLY_LOSS_INR_MAX` (₹75,000), and an amount above
the ceiling is held to it (`ledger.book_pause_limit`). **Rejected.** 0 = off (the looser boundary).
**Reversal.** One line in `book_pause_limit`.

## FO10.6 — The paper checklist is derived; a violation restarts the count; no 0053 · ⚠ UNREVIEWED

No violations table (it would need 0053 under `services/api`): `baskfy_worker.fno.ledger.read_checklist`
derives each `04` §9 violation from the tables — `UNCOVERED_SHORT` by replaying a condor's fills in
order, `INTO_EXPIRY` from the legs' expiry against the close, `JOURNAL_GAP` for a closed position
without its row **and** for a landed session an open position has no `fo_mark` for, `MISSED_CYCLE`
for an F1 `CANDIDATE` scan with no plan raised (a lapsed or refused plan is a skip) — plus the
desk's recorded `LATE_EXIT` / `NAKED_FUTURE` (`fo_plan.detail.violations`, FO7.12). Paper rows only.
"Consecutive" and "zero violations" read together: a group's cycles and sessions count from the
day after its latest violation (`checklist.paper_checklist`). F2's period starts at its first
entry plan's day. **Reversal.** Count from the first day and report violations beside it.

## FO10.7 — A paused entry is written `REJECTED_PAUSED` · ⚠ UNREVIEWED

`04` §8's `REJECTED_*` family gains `PlanState.REJECTED_PAUSED` (the options pack's name). At 09:20
the monitor asks `fno_ledger.entry_block` (the journal's pauses for the sleeve's mode, and the
stored pauses that bind it) and, paused, writes the entry plan with its reasons and no legs —
never confirmable — so the checklist sees a skip rather than a missed cycle. The scan's `PAUSED`
row still says it the night before. `fo_plan.status` has no CHECK, so no migration.

## FO10.8 — `FNO_WEEKLY`, dark behind `BASKFY_FNO_MONITOR_ENABLED` · ⚠ UNREVIEWED

The options pack's OP11 precedent: Beat `fno-weekly` Friday 16:35 IST, one alert per FO tenant, a
line per `(sleeve, simulated)` for the week, the month per book, the pauses per mode. The worker
gains `fno_monitor_enabled` (default false) only to stay dark; the alert names
`docs/runbooks/13-fno-book.md`, which FO12 extends with the book's other alerts.


## FO11.1 — Hypothesis joins the desk's requirements, test-only, at the screener's pin · ⚠ UNREVIEWED

**Context.** `06` FO11 asks for "a fuzz over the execute route". The route, its executor and the
`fo_` tables live in the desk, whose suite had no Hypothesis; the options pack's OP13 kept its
Hypothesis proofs in `packages/core` because they were pure. **Choice.** `hypothesis==6.165.10`
(the screener's locked version, `decile-blueprint/pyproject.toml`) is added to
`kite-momentum-rebalancer/requirements.txt` beside `pytest`, with a comment saying why.
**Rejected.** A seeded `random` loop (a fuzz without shrinking); moving the route test into the
screener's venv (it cannot import the desk). **Reversal.** Remove the line; `TestTheExecuteRouteFuzz`
becomes a parametrized list.

## FO11.2 — "No NRML order can be formed" is read as: nothing reaches a broker · ⚠ UNREVIEWED

With every FO flag false a paper confirm **does** build NRML orders — through the gateway's dry-run
branch, journalled, simulated (FO6.5, FO7.1); that is Track A's paper book. The proof therefore
asserts what the sentence protects: a recording broker sees 0 calls, no gateway journal holds a
broker event (`placed`, `rejected`, `error`, `gtt_placed`, `gtt_modified`, …), every `fo_fill` is
`simulated`, every journalled order is NRML on the plan's own legs whatever product/venue/quantity
fields are tampered into the form, and the answer is 200 exactly when `confirm == "true"`, the
plan is the caller's own `ISSUED` entry and the clock is inside its 30 minutes.

## FO11.3 — OP13's "only the POST route calls `execute_entry`" lists both books · ⚠ UNREVIEWED

`test_options_safety_proof.test_only_the_post_route_calls_execute_entry` had been **red since FO7/FO8**:
its source scan matches any `execute_entry(` in the desk, and the FO book has its own
(`fno_execute` defines it, `fno_desk`'s `POST /fno/execute` calls it). The expected list now names
both pairs exactly (four files), which is no weaker for the options book; FO11's own
`test_only_the_fno_post_route_calls_the_fno_execute_entry` pins the FO pair and that the monitor
never confirms. **Reversal.** Scope the OP scan to `options_*` files.

## FO11.4 — The `place_order` scan's two allowances · ⚠ UNREVIEWED

Law 2's scan (`test_fno_safety_proof`) walks the packages, services, the desk's `app`/`scripts` and
`tools/`. `app/kite_client.py` is the broker adapter the gateway itself calls (the allowance
`test_seven_non_negotiables` 6b already makes); `tools/friday-drill.py` calls `place_order` on its
own recording spy to prove the spy records. Everything else is refused by name.

## FO11.5 — The drill isolates the desk's database and journal before importing `app.main` · ⚠ UNREVIEWED

`tools/fno/drill.py` confirms through the real `POST /fno/execute`, so it builds `app.main`, which
migrates and reads the desk database it is configured with on import. The drill now points
`db.DB_BACKEND`/`DB_PATH` and the order journal at its temporary directory first (the suite's
`conftest` isolation). Its first run in this session, before that line existed, imported `app.main`
against the local development database named in the desk's `.env` (an idempotent `migrate` and a
settings read; nothing written by the drill itself). The `fo_` rows it writes go only to the test
database it is given (name must contain `test`), under throwaway users it deletes.

## FO11.6 — F1B's fixture is BANKNIFTY listed on NIFTY's strikes and lot · ⚠ UNREVIEWED

FO7's replay master lists NIFTY only. The FO11 matrix and drill run F1B through the same monitor,
route and gateway with a master that also lists BANKNIFTY on the fixture's strikes and lot (65),
because what is under test is the gate and the sequence, not BANKNIFTY's contract spec.

## FO12.1 — The worker raises the desk's F&O alerts from the rows the desk writes · ⚠ UNREVIEWED

The desk cannot reach the worker's alert sinks. `baskfy.fno.alerts` (Beat `fno-alerts`, every 5
minutes) reads `fo_plan`, `fo_journal` and `fo_position` and raises `FNO_PLAN`, `FNO_EXIT`,
`FNO_HARD_EXIT_TOMORROW` and `FNO_LATE_EXIT` once each, deduplicated by a Redis marker. If Redis is
down it sends rather than stays silent (a duplicate email beats a missing hard-exit warning).
`FNO_BHAVCOPY_MISSING` is raised by the ingest's final 23:30 attempt. Only today's events alert, so
the first run after a deploy does not resend history.

## FO12.2 — Each alert is dark behind its operational flag · ⚠ UNREVIEWED

The four desk alerts need `BASKFY_FNO_MONITOR_ENABLED`; the missing-bhavcopy alert needs
`BASKFY_FNO_SCAN_ENABLED`, like the ingest itself. The worker reads the monitor flag from
`.env.staging` and the desk from `.env.staging.compose`, so both files carry it (OP15's lesson).

## FO12.3 — "Hard exit tomorrow" goes out from 18:00 and covers F2's time exit · ⚠ UNREVIEWED

The same rule as the desk's nav badge (FO8.3): the earliest of F1's hard-exit date and F2's
40-session exit, when it is the next session.

## FO12.4 — `fno-monitor` joins compose with every FO money flag pinned false · ⚠ UNREVIEWED

The desk block names `BASKFY_FNO_CARRY_ENABLED`, `BASKFY_FNO_F1_EXECUTION_ENABLED`,
`BASKFY_FNO_F2_EXECUTION_ENABLED` and `BASKFY_FNO_MONITOR_ENABLED`, each `${…:-false}`; the
`fno-monitor` service runs FO7's loop and idles with the monitor flag false. `deploy-swing.sh`
restarts it, `ship.sh` expects fifteen running services and runs `verify-fno.sh` after
`verify-options.sh`. The verify asserts the money flags false and reports the operational ones.

## M.5 — F3, the directional index credit spread: Maulik's method and his three answers (in session, 28 Sep 2026)

**Context.** Maulik brought a method (a trader's thread: sell weekly index options in the market's
direction; daily support/resistance, 75-minute confirm, intraday trend; strikes ~1 % beyond the
weekly candle; ~80 % decay target; out immediately on a level break; 20–30 % of capital first,
pyramid the next day if working; ~1 % a week; "sell 20–30 rupee options and cut at 50") and asked:
*"here instead of banknifty choose nifty for weekly expiry and choose bank nifty for monthly
expiry and code it"*. Three points the F&O charter cannot settle by itself were put to him with
options, and he answered:

1. **Structure — "Credit spread with a far wing".** The thread sells naked; `02` §2.1 forbids a
   naked short option at any instant. The wing caps the gap loss and the exchange's spread margin
   makes his "20–30 % of capital" a max-loss share rather than a notional. Rejected: a naked short
   (breaks the charter), a ratio or a condor (not his trade).
2. **The non-negotiable exit — "Auto-exit under a new flag".** His rule is "out immediately, not
   after one more candle"; a plan that waits for a click is not that rule. `BASKFY_FNO_F3_AUTO_EXIT`
   (default false, desk only, his hand) lets the monitor confirm the EXIT plan it raised. Entries
   and adds stay clicks. This is the **third** auto-execute exception in the product and it is his,
   recorded in root `CLAUDE.md` non-negotiable 1 and `02` Track B; an agent may not widen it.
   Rejected: click-only (his rule unmet), auto-exit without a flag (a money path an agent turned on).
3. **Instruments — "Yes, exactly that": NIFTY on the weekly expiry, BANKNIFTY on the monthly.**
   BANKNIFTY has had no weekly since NSE's 2024 change, which is why the thread's "weekly Bank
   Nifty" cannot be coded as written.

**Taken by the agent under the charter (⚠ UNREVIEWED), each cheap to reverse in `F3Config`:**
the numbers of `04` §11 (a rolling five-session "weekly candle"; the 75-minute confirm as the last
bar's close against its ten-bar average; the intraday check as index against the session open;
the level buffer 0.10 %; the wing 2 % of the index beyond the short; the loss cut at 2 × credit,
from his 20–30 → 50 example; the hard exit at 15:00 on the expiry day, index-only); `F3N`/`F3B`
under one config group `F3`; capital ₹0 (Q10) so paper runs one lot; the EOD re-test claims only
the daily half. **Not built in this pack:** the web app's card (read-only; a later leaf), and real
money, which waits on `02` §3 like F1.
