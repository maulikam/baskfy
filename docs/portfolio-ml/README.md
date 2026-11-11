# The portfolio-ML run (PM) — a LightGBM multibagger ranker, inside Baskfy

This folder commissions the fifth sleeve: **MB**, a read-only ranking of which liquid NSE names are
most likely to **touch 2× within 12 months**, researched in
[`machinelearning/python/research/portfolio-ml/STRATEGY.md`](../../machinelearning/python/research/portfolio-ml/STRATEGY.md)
(8–9 Nov 2026). The research note is the **spec**; its §6 open items are this run's modules.

**Read order for any agent session:** `/CLAUDE.md` (the charter governs this run unchanged) →
`docs/README.md` → the STRATEGY note → this file → [`06-module-plan.md`](06-module-plan.md) →
[`STATUS.md`](STATUS.md). `docs/vbt/` is the sibling: copy its shape, gating and discipline, never its rules.

| Doc | What it is |
|---|---|
| [`06-module-plan.md`](06-module-plan.md) | **The task list.** PM0–PM9, each with a Goal and acceptance criteria that are tests or regenerated tables |
| [`STATUS.md`](STATUS.md) | Live status — updated at the end of every module, loud about what is NOT done |
| [`DECISIONS-PM.md`](DECISIONS-PM.md) | Judgement calls, numbered by module; pre-taken ones are PACK.1–PACK.5 |
| `../../machinelearning/python/research/portfolio-ml/BASKFY-INTEGRATION.md` | Where the code goes under law 1, schema, pages, gates |

## The one-paragraph version

The research found that a price/volume-only ranker has a persistent edge on a 12-month doubling
label (out-of-sample AUC 0.60–0.69 every year 2020–25; a monthly-tranche book of the top-20 investable
names 34 % CAGR / −20 % drawdown vs 24.5 % / −23 % for the universe) and a structural weakness: its
favourite features are *cheap, volatile, illiquid, newly listed* — lottery traits that lost 19 % in
signal-year 2024 while the universe was flat. Swing-trade labels (2–4 weeks) were tested and rejected
(§2 of the note). Label repair (penalising round-trippers) was tested and changes nothing. What is
missing is **information about the business**: point-in-time fundamentals, shareholding, market cap and
sector. This run adds that data to the plant, re-runs the research on one pass/fail number (do the
2024 picks stop losing), then — only then — gives Baskfy a nightly score table, two read-only pages
and a paper book. **No plan, no gateway path, no flag.** A ranking is a watchlist; buying a tranche is
a human's hand in `/analyze → /execute`.

## Scope law (Track A / B / C)

- **Track A (build):** data tables, features, research re-run, `mb_score_daily`, `/mb` pages, paper book.
- **Track B (dark, none planned):** nothing in this run ships behind a flag.
- **Track C (forbidden):** any order, GTT, plan line or Beat task that acts on an MB score; any flag
  whose true value places an order; any `mb_*` write from the web app. Also forbidden: training on
  non-point-in-time data (screener.in exports, today's sector list applied to 2018) — it leaks.

## Key mapping

| STRATEGY concept | Baskfy implementation |
|---|---|
| labels, features, tranche book (§1, §4, §5) | `portfolioml/` unchanged (Python `baskfy_core` is legacy since RF3); production imports it, no copy |
| the saved model (§3) | `mb_model` row + file under the untracked data dir; a `lightgbm.Booster` passed *into* `portfolioml.model`, loaded by the `mb` task (PM6.1) |
| today's ranking (`score_latest.py`) | task `mb-score` after `publish` → `mb_score_daily`; date = last completed session |
| investable floor (≥ ₹50, ≥ ₹5 cr/day), sector cap 3 | `MbConfig` fields; the page filters, the model does not |
| fundamentals / shareholding / sector (§6.1–6.3) | `fundamental_pit`, `shareholding_pit`, `instrument_sector` — PM1–PM3 |
| "re-rank, don't sell" (§6.6) | PM8's paper book rule; tested against `mb_score_daily` history |
