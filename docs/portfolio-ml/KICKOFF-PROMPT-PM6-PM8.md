# Kickoff — PM6 to PM8: the MB sleeve goes into Baskfy (11 Nov 2026)

Read first: /CLAUDE.md, docs/portfolio-ml/README.md, 06-module-plan.md, STATUS.md, DECISIONS-PM.md (PM4.6–4.9, PM9a,
PM0.16–0.19), machinelearning/python/research/portfolio-ml/STRATEGY.md and BASKFY-INTEGRATION.md.
Autonomy charter and token budget apply. One session writes to this tree.

Maulik's instruction (11 Nov 2026): "our model is good enough, integrate it into Baskfy." What that means here:
the **price-only LightGBM ranker** (touch 2x / 250 sessions, lambdarank, 95 features, PARAMS and ROUNDS exactly as
in explore_multibagger.py) becomes a **read-only sleeve, MB**. PM4 is red for fundamentals; nothing from PM1–PM3 is a
model input. No plan builder, no gateway path, no order, no flag, no auto-execute (README Track C). The honest
backtest on the page is the 2014→ one: ~17 % CAGR / −66 % max drawdown, not the 2020→ figures.

## 0. Housekeeping first (one commit each)
- The tree holds uncommitted work from the agent's cloud runs of 11 Nov: out/pm4/, PM4-RESULT.md, isolate_filter.py,
  single_split.py, last6m.py, explore_timesfm.py, out/timesfm/, explore_technicals.py, explore_sharpe.py and their
  CSVs, memory fixes in build_dataset.py / features.py / run_pm4.py, DECISIONS-PM PM4.6–4.9, PM9a, PM0.18–0.19, STATUS.
  Review the diff, run the research tests, re-run `run_pm4.py mb_u100_h250` natively once to confirm the tables
  (PM4.8), then commit: "PM4: red for fundamentals, price-only model stands; follow-up tests recorded".
- Fix the stale paths in 06-module-plan.md / BASKFY-INTEGRATION.md if any remain (Rust worker + Python research).

## PM6 — nightly scorer
Goal: `mb_score_daily` for the last completed session, every night, from a saved model file.
- Migration: `mb_model(id, trained_on, label, features jsonb, params jsonb, path, rows, positives, created_at)` and
  `mb_score_daily(date, instrument_id, model_id, score numeric(10,6), rank int, universe_n int, investable bool,
  hi63_trigger bool, hi63_trigger_date date, PRIMARY KEY(date, instrument_id))`. Rounded at write; idempotent.
- Where it runs (decide and record as PM6.1): scoring needs the 95 features and a LightGBM booster. Cheapest
  correct path first — a Python task invoked by the worker after `publish` (post-publish, cannot fail the night),
  reading bars from Postgres and reusing `portfolioml.features` unchanged so research and production share one
  feature code path. A Rust port of features + tree walker is a later module; do not start it here.
- `mb-train`: first Saturday of each quarter, and once now. Trains on every completed label (all universe rows,
  stride 5), stores the model file under the untracked data dir and a row in `mb_model`. Before a new model
  publishes, score the same day with old and new and refuse without an operator ack if Spearman < 0.7.
- `hi63_trigger`: true when the name closed at a 63-session high within the last 21 sessions (PM0.16).
- Dates: the score date is the last completed session (data-date-clocks skill). Never label it live.
- AC: feature parity test — for 3 past dates, the task's features equal the research dataset's to 1e-5;
  idempotent night (same table hash on re-run); purity test for anything placed in a core crate/package;
  the nightly still publishes when the task is made to fail (prove it); tests pass, lint clean.

## PM7 — pages (read-only)
- API: GET routes only, same auth/gating pattern as the other sleeves: today's ranking (top 50, with rank, score,
  investable, hi63 trigger, close, 20-day turnover, 12-month return, distance from ATH, listing age, sector, latest
  TTM revenue/PAT growth from fundamental_pit for display only), a name's score history, model metadata.
- Web `/mb`: the list; a "what this is" panel with the honest numbers (2014→ book ~17 % / −66 %; twelve-year
  hit-rate table; 2025 showed no edge on the full universe; price-only; not advice) as components, not footers.
- Desk `/mb`: the same list as a watchlist with a link that pre-fills a manual `/analyze`. No plan line is created.
- AC: no execute/order route touched (verify-safety still green); 402-gating consistent; web lint + unit tests.

## PM8 — paper book
- Tables `mb_paper_tranche` / `mb_paper_line`. On the first session of each month the task records the top 20
  investable names not already held (book of record, equal weight, 250-session hold) at the next session's open.
  No broker call of any kind.
- Three shadow lines beside it, all paper: (a) 63-day-high trigger at 1/20 of a tranche per name; (b) the same at
  1/16; (c) Smallcap-250 close > 100-DMA with 5-session confirmation as an exit overlay on the book of record.
- Daily marks on every bar; monthly comparison against expectation (20–23 % of picks touch 2x, 7 % halve,
  median 12-month about +6 %; trigger line drawdown about −26 %) on a `/mb/paper` panel.
- Seed it with a backfilled paper history from the walk-forward scores (clearly labelled "backtest, not live")
  so the panel is not empty for a year; the live record starts at the first tranche after deploy.
- AC: idempotent; a restart mid-month neither duplicates nor loses a tranche; no write from the web app.

## Not in this run
PM5 (delivery %, bulk/insider deals, events) and PM9 (text). Real money: not before twelve months of the paper
book at ≥ 70 % of expected hit rate, and Maulik's own decision after that.

## Report
Short, per module: green/red, counts, commit hashes, open items. Anything needing Maulik's hand (image ship,
stack restart, a model-publish ack) goes to NEEDS-MAULIK.md and work continues around it.
