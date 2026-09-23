# The FO run — overnight F&O: research first, then only what survived

This folder commissions the FO run. It was asked for by Maulik in session on **23 Sep 2026**:

> "find the opportunity in the FNO segment, which is not only in index options but also in selling
> or buying options in FNO stocks and futures. Build a system around it"

His four answers the same day are the charter:

| Question | His answer |
|---|---|
| Where it lives | **Extend the Options tab** (`docs/options/05`), not a new product |
| Overnight | **Defined-risk overnight**: NRML for spreads, long options, and futures with a stop or hedge. **No naked short options** |
| Instruments | Stock options (buying), stock options (selling), stock futures, index futures/options, **all four** |
| First step | **Research + design pack**: test which edges hold up on history, then write the pack. No execution code yet |

**Read order for an agent session:** `/CLAUDE.md` (it governs unchanged) → `docs/README.md` →
this file → [`RESEARCH.md`](RESEARCH.md) (why the pack is shaped the way it is) →
[`02-scope-and-gating.md`](02-scope-and-gating.md) (the law) →
[`07-data-reality.md`](07-data-reality.md) → [`06-module-plan.md`](06-module-plan.md) →
[`04-business-rules.md`](04-business-rules.md) → the rest as modules cite them.

| Doc | What it is |
|---|---|
| [`RESEARCH.md`](RESEARCH.md) | **Every family tested on 2022–2026 end-of-day data, with every number.** Read first |
| [`01-method.md`](01-method.md) | What is built (F1, the data layer, the information page) and what is not, and what would bring each back |
| [`02-scope-and-gating.md`](02-scope-and-gating.md) | The law: the one gateway change, the four overnight rules, Tracks A/B/C, the real-money gate |
| [`03-data-model.md`](03-data-model.md) | The `fo_` schema |
| [`04-business-rules.md`](04-business-rules.md) | The numerical contract the tests assert |
| [`05-ui-spec.md`](05-ui-spec.md) | `/options/overnight`, `/options/fno`, the desk's `/fno` |
| [`06-module-plan.md`](06-module-plan.md) | FO0–FO12 |
| [`07-data-reality.md`](07-data-reality.md) | The F&O bhavcopy, what it can and cannot tell, the tiers, physical settlement |
| [`DECISIONS-FO.md`](DECISIONS-FO.md) | Judgement calls, PACK.1–PACK.8 |
| [`QUESTIONS.md`](QUESTIONS.md) | What only Maulik can answer, each with a standing default |
| [`STATUS.md`](STATUS.md) | The live status page |
| [`KICKOFF-PROMPT.md`](KICKOFF-PROMPT.md) | The prompt that starts the run |
| `evidence/` | The research scripts and raw outputs; the backtest inputs are re-fetchable through the provider |

## The one-paragraph version

The research read every NSE F&O bhavcopy from Jan 2022 to Sep 2026 (1,162 sessions, every
contract, expired ones included) through a new, tested `NSEProvider.fo_bhavcopy`. It then tested
stock-futures trend (both sides, with and without OI), cross-sectional momentum, index-futures
trend, stock iron condors and credit spreads, debit spreads and long options on breakouts,
cash-futures carry, and index monthly condors, with no look-ahead and costs at today's STT. **All
but one lost after costs**, most of them before costs too. The stock-option families that win 80 %
of the time are negative in every year. The survivor, **F1, a hedged NIFTY/BANKNIFTY monthly iron
condor entered 15 sessions before expiry and carried overnight**, made +0.033R a trade on 100
trades (t = 1.36). That is a hypothesis, not an edge, so the pack builds it **on paper only**, with
a real-money gate it has not yet met. Around it the pack builds the F&O data layer (a nightly
ingest, measured option spreads, and a quarterly re-test of every rejected family), so a verdict
can change when the evidence does. It also builds a **Stock F&O information page** that
deliberately shows no signals. None of the 20 current holdings is an F&O stock, so covered calls
are impossible.

## How this relates to `docs/options/`

The OP run (O1–O3, intraday NIFTY) is **unchanged and unfinished** (OP8 next). Its Track C (no
overnight, NIFTY only, no futures) stays true **for O1–O3**. This pack governs only `fo_` sleeves.
The two share the NFO master, the greeks and the cost model; FO2 widens the master and FO6 adds one
gateway branch, and each is constrained so no O-sleeve behaviour moves (`06`, "Coordination with
the OP run").

## What this run is not

It is not a stock-options book, a futures book or a carry book: the research rejected each
(`01` §4). It is not naked selling, not auto-execution, and not a hedge on the equity books
(QUESTIONS Q5). It is not live money: every FO flag defaults false, and only Maulik flips one. And
it is not a claim of profitability.
