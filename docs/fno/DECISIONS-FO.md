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
