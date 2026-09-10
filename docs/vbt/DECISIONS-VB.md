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
