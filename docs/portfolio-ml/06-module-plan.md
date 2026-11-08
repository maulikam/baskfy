# 06 — Module plan, PM0–PM9

Conventions as the root `CLAUDE.md`: one commit per module (`PM<N>: green — <one line>`), decisions
in `DECISIONS-PM.md`, status in `STATUS.md`. Every network fetch goes through the existing NSE
provider's cookie/header and rate-limit discipline (rule 9) — **no scraping around it**. Every new
table is idempotent on re-run (house rule 7), money/prices `numeric` (rule 9), rounded at write.

## PM0 — The pack lands
**Goal:** the research is reproducible from the repo on fresh data.
- AC1 `machinelearning/python/research/portfolio-ml/` holds the pack; `python -c "import portfolioml"` from there works in a `uv` env with `lightgbm` (record the dependency: `DECISIONS-PM.md` PM0.1, house rule 1).
- AC2 `bash export_bars_local.sh` run on the Mac (**Maulik's hand** — docker); `build_dataset.py --mb 1.0 250` → `explore_multibagger.py` → `run_multibagger.py` regenerate §3/§4 tables on data through the current session; STRATEGY §3/§4 updated, differences noted.
- AC3 `out/latest_ranking_mb_u100_h250.csv` is as of the last completed session.

## PM1 — Point-in-time fundamentals
**Goal:** quarterly results as the market saw them, stamped by filing date.
- Table `fundamental_pit(instrument_id, period_end, filed_on, period_type, revenue, ebitda, pat, eps_diluted, total_debt, total_equity, cfo, capex, shares_outstanding, source, PRIMARY KEY(instrument_id, period_end, filed_on))`. Standalone and consolidated kept apart (`period_type`); consolidated preferred where both exist.
- Source: NSE corporate filings — financial results (XBRL) via the NSE provider; BSE as fallback. Backfill 2017→ resumable; nightly step picks up new filings.
- Features (`baskfy_core.mb.fundamentals`, pure): TTM revenue/PAT growth (yoy), EBITDA and PAT margin and their 4q change, D/E, ROCE (EBIT / (equity+debt)), CFO/PAT, EPS growth, sales acceleration. Joined to a session by **`filed_on` ≤ date** (never `period_end`). Asserted by a look-ahead test.
- AC1 `test_mb_fundamentals_no_lookahead.py`: a feature at date *d* changes only when a filing with `filed_on ≤ d` exists. AC2 coverage report: share of universe rows with a TTM figure, by year (expect > 85 % from 2019). AC3 idempotent re-ingest.

## PM2 — Shareholding pattern, point-in-time
**Goal:** promoter %, pledge %, FII/DII/MF %, public %, per quarter, by filing date.
- Table `shareholding_pit(instrument_id, period_end, filed_on, promoter_pct, promoter_pledged_pct, fii_pct, dii_pct, mf_pct, public_pct, n_shareholders)`; source NSE shareholding pattern filings (the quote page's `shareholdingPatterns` is the latest only — not point-in-time; the filing archive is).
- Features: level and 1q/4q change of each; pledge rising/falling flag; promoter buying flag.
- AC as PM1 (no-lookahead test, coverage, idempotent).

## PM3 — Market cap and sector
**Goal:** the model can learn size instead of share price, and the book can be sector-neutral.
- `mcap_cr` daily = `close_raw × shares_outstanding(latest filed ≤ date)` from PM1 (`fundamental_daily.marketcap_cr` exists only from Aug 2025 — keep it as the check, not the source).
- `instrument_sector(instrument_id, macro, sector, industry, basic_industry, as_of)` from NSE's `industryInfo` on the quote the fundamentals step already fetches; **sector is treated as static** and recorded as a known small leak (DECISIONS PM3.1) until a dated source exists.
- Features: `log_mcap`, mcap decile, sector 21/63-day equal-weight momentum, name's return minus sector's. Book: `TrancheConfig.max_per_sector = 3`.
- AC1 `test_mb_sector_cap.py`. AC2 coverage ≥ 95 % of universe names with a sector.

## PM4 — The re-run (the gate for everything after)
**Goal:** one number decides. Rebuild features with PM1–PM3, same walk-forward, same book.
- AC1 Signal-year **2024** investable top-20 12-month mean return ≥ investable universe's (was +1 % vs 0 %; the unfiltered was −19 % vs 0 %) **and** 2024 top-20 hit rate ≥ 1.5× base (was 1.5×).
- AC2 No year's AUC falls below the price-only model's by more than 0.02.
- AC3 STRATEGY §3/§4 regenerated; feature importance shows fundamentals/shareholding/mcap in the top 12.
- If AC1 fails: record it, keep the price-only model as the sleeve's model, and PM5–PM9 proceed with the better of the two. A negative result is a result.

## PM5 — Delivery %, bulk/block deals, SAST
**Goal:** the smaller signals. Delivery % from the bhavcopy archive (daily); bulk/block deals and SAST/insider filings from NSE's daily reports. Features: 20-day delivery % vs 1-year, net bulk buys, insider net buys 90d. AC: coverage + no-lookahead; re-run §3 only.

## PM6 — Nightly scorer
**Goal:** `mb_score_daily` for the last completed session, every night.
- Schema per BASKFY-INTEGRATION (`mb_model`, `mb_score_daily`). Task `mb-score` **post-publish** (cannot fail the night); task `mb-train` first Saturday of each quarter: refit on every completed label, store the model file, diff the new ranking against the previous model's on the same day, refuse to publish if Spearman < 0.7 without an operator ack.
- Law 1: `baskfy_core.mb.model.score(booster, frame)`; the Booster is loaded in `services/mb`. **Date:** last completed session (data-date-clocks skill); never label a figure live.
- AC1 idempotent night. AC2 `test_mb_purity.py`. AC3 the Python worker runs it (DECISIONS PM6.1 — Rust port deferred; options recorded).

## PM7 — Pages
**Goal:** see it. Web `/mb` (read-only): top 50, rank, score, investable, sector, mcap, 12m return, distance from ATH, turnover, listing age; the §4 backtest as the "what this is" panel; disclaimers as components. Desk `/mb`: the same list as a **watchlist** with a link that pre-fills a *manual* `/analyze`. No plan builder. AC: 402-gating consistent with the other sleeves; no `execute` route touched (`tools/deploy/verify-safety.sh` still green).

## PM8 — Paper book
**Goal:** twelve monthly tranches of the live ranking, journaled, compared monthly to the backtest's expectation (27–30 % of picks touch 2×, median 12m +15–19 %, ≤ −20 % drawdown), plus the **"re-rank, don't sell"** rule tested on `mb_score_daily` history. AC: a `/mb/paper` panel with expected-vs-realised; twelve months before any real-money discussion.

## PM9 — Text
**Goal:** results-call transcripts and announcements as embeddings → a few summary features. Last; only after PM4 passes and PM8 has run six months.

## Gates before real money (mirrors `docs/vbt/02` §3)
PM4 AC1 met · PM8 twelve months with realised ≥ 70 % of expected hit rate · D3/counsel on showing a ranked list of named stocks publicly · the flag that would exist does not exist in this run.
