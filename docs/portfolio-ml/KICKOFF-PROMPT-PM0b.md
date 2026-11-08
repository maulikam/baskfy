Continue the portfolio-ML run (docs/portfolio-ml/). Read README.md, STATUS.md, DECISIONS-PM.md and
STRATEGY.md first. PM1–PM3 code is committed; the backfill is Maulik's. This prompt is research on data
already on disk; it does not need the backfill, the image or the stack. Save it as
docs/portfolio-ml/KICKOFF-PROMPT-PM0b.md. Autonomy charter and token budget apply.

## PM0.2 — skip-held becomes the default (tested on the 10 Sep scores, redo on 6 Nov)

Add `skip_held: bool = True` to `TrancheConfig` in portfolioml/tranches.py: a name held by any of the
last 12 tranches is not eligible for the new one. (On 10 Sep scores, investable top-10: 33.6 % / −21.6 % /
Sharpe 1.91 → 36.9 % / −17.0 % / 2.25; 2022 +9 % → +17 %, 2025 −6 % → −1 %; distinct names 348 → 538.
The hit rate barely moves; losses shrink — a diversification effect, because re-buying concentrates the
model's favourites, which are its biased type.) Re-run §4 with and without on the 6 Nov scores, top-10
and top-20, and record before/after as PM0.2 in DECISIONS-PM and in STRATEGY §4. If the 6 Nov numbers
do not confirm the direction, say so and leave the default False.

Also: an entry-cadence test (`explore_cadence.py` exists on the cloud side, approximate accounting —
rewrite it against `tranches.simulate` instead): monthly top-10/20, fortnightly top-5, weekly top-2,
every-2-sessions top-1, daily top-1, all with skip_held. Report a table of CAGR / DD / Sharpe / distinct
names / median pick. Expected: all in one band; the finding is that cadence does not matter, and weekly
top-2 is the operational recommendation. Record as PM0.3; do not pick the best cell.

## PM0.4 — the six years nobody has looked at (2014–2019)

Bars now start 2011. Features need ~3 years of warm-up (r756, n_hist ≥ 260), so the first testable
signal year is 2014. In explore_multibagger.py the year loop defaults to ≥ 2020 — parametrise
first_test_year and run the identical walk-forward (same features, same lambdarank, same 250-round
fixed model, same 256-session purge, TRAIN_STRIDE 5) for signal years 2014–2019 as well as 2020–2025.
Report the §3 table for all twelve years: base rate, AUC, top-20 and top-decile hit rate vs base,
12-month mean and median return of the top-20 vs universe, worst-point mean. Then run the §4 book
(investable, top-10 and top-20, skip_held) from 2014 and report CAGR / DD / Sharpe per calendar year
beside Smallcap 250 and the investable equal-weight. The years that matter: 2015–16 (sideways),
2018–19 (bear; ~1 % of names doubled). AC: STRATEGY §3/§4 gain the 2014–19 rows; §0 and §6.5 are
rewritten from what they show; STATUS says in one line whether the edge existed before 2020. A negative
result is a result — record it exactly.

## PM0.5 — separate the two effects in 2024

Same model, same 2024 test rows, two training sets: bars from 2017 only (the old export's span) vs bars
from 2011. Report both 2024 lines (hit rate vs base, 12m mean vs universe) and the AUC. One commit,
DECISIONS-PM PM0.5: which of "more history" and "the seam repair" moved 2024, as far as this isolates it.

## Report

Short: the twelve-year table, the skip_held before/after, the cadence table, the 2024 split, commit
hashes, open items. Do not start PM5–PM9. Do not touch the running stack.
