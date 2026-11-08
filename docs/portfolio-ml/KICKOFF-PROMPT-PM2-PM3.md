Continue the portfolio-ML run (docs/portfolio-ml/). Read /CLAUDE.md, docs/portfolio-ml/README.md,
06-module-plan.md, STATUS.md, DECISIONS-PM.md and machinelearning/python/research/portfolio-ml/STRATEGY.md
first. PM0 is committed (24737c2); PM1 is committed but its coverage check waits on the 24-hour backfill.
Save this prompt as docs/portfolio-ml/KICKOFF-PROMPT-PM2-PM3.md before doing anything else.

Work under the autonomy charter: decide, record in DECISIONS-PM.md (⚠ UNREVIEWED), continue. Token budget
per CLAUDE.local.md: terse docs, one-line commits, no narratives. Nothing you build may place, plan or
schedule an order, and no flag is created (Track C in README.md).

## 0. Record what the fresh data changed (docs only, one commit)

- STATUS.md and STRATEGY.md §3: state plainly that signal-year 2025 is the first fully out-of-sample year
  since the model was designed and the price-only model showed no edge there (top-20 hit 4.5 % vs 5.4 %
  base, −2.2 % vs +3.9 %). Live expectation is set from 2025 until PM4 passes, not from the 2020–25 average.
- DECISIONS-PM PM4.1: 2024 moved from +1 % to +5.1 % only because the Nov export completed Q4-2024 signals'
  paths — the 2024 training set is unchanged. Single-year gates are fragile; PM4 is judged per year (2024 and
  2025, as PM4.0 already says) AND on pooled 2024+2025. Add the pooled line to 06 PM4 AC1.
- DECISIONS-PM PM1.2: D/E, ROCE, CFO/PAT exist in the NSE feed only from 2025, so in training PM1 tests
  growth and margins; balance-sheet ratios matter mainly for live scoring. If PM4 fails, record it as
  "growth/margins did not fix 2025", not "fundamentals do not work". Historical balance sheets = MCA
  annual-report XBRL, a later module (note it as PM5b).
- Fix the stale paths: 06-module-plan.md and BASKFY-INTEGRATION.md still say packages/core, baskfy_core
  and services/. Rewrite the "where the code lives" rows to where PM1 actually landed (Rust ingest in
  baskfy-worker, Python point-in-time features), keeping law 1 as stated: baskfy-core stays pure, the
  worker does I/O. Record as PM1.3.
- NEEDS-MAULIK.md: under the PM1 backfill item add: start it right after a nightly publish, outside
  18:40–21:15 IST; it shares NSE's rate limit with nightly step 6, which already stalls every ~600 requests.

## 1. PM2 — shareholding pattern, point-in-time (copy PM1's shape exactly)

Table shareholding_pit(instrument_id, period_end, filed_on, available_on, promoter_pct, promoter_pledged_pct,
fii_pct, dii_pct, mf_pct, public_pct, n_shareholders, source, PRIMARY KEY(instrument_id, period_end, filed_on)).
Source: NSE shareholding-pattern filings archive through the same rate-limited provider path PM1 uses
(the quote page's shareholdingPatterns is latest-only — not point-in-time — do not use it for history).
Same available_on rule as PM1 (filed after 15:30 → next session). Backfill command beside PM1's,
resumable, idempotent (same table hash on re-run — prove it the way PM1 did).
Features (Python, pure, beside PM1's): level and 1q/4q change of each pct; pledge rising/falling flag;
promoter buying flag (promoter_pct up ≥ 0.5 pt q/q). Joined by available_on ≤ date, never period_end.
AC: no-look-ahead tests mirroring PM1's six; coverage report by year once backfilled (expect > 85 % of
universe rows from 2019); parser unit tests on at least three filings including one bank and one with a
pledge; clippy -D warnings clean; PM1 and PM2 backfills can run in one invocation sharing the limiter.
Commit: PM2: green — <one line>. Mark PM2 "code complete, backfill pending" in STATUS.

## 2. PM3 — market cap and sector

- mcap_cr daily = close_raw × shares_outstanding from the latest fundamental_pit row with available_on ≤ date
  (fundamental_daily.marketcap_cr exists only from Aug 2025 — use it as the check, not the source; report
  the median relative error where both exist).
- instrument_sector(instrument_id, macro, sector, industry, basic_industry, as_of): from NSE's industryInfo
  on the quote the nightly fundamentals step already fetches; one row per instrument, refreshed nightly.
  Sector is treated as static across history — record this known small leak as PM3.1 and flag it in the
  table comment and in every feature it feeds.
- Features: log_mcap, mcap decile within the day's universe, sector 21/63-day equal-weight momentum, the
  name's 21/63-day return minus its sector's.
- Book: TrancheConfig.max_per_sector already exists in portfolioml/tranches.py (the "sector" column on ds);
  default 3. AC: test_mb_sector_cap (no tranche holds > 3 of one sector; a universe with fewer sectors than
  needed still fills top_n); coverage ≥ 95 % of universe names with a sector.
Commit: PM3: green — <one line>.

## 3. PM4 — prepare, do not run

Write run_pm4.py in the research folder: rebuilds features with PM1–PM3 columns joined point-in-time,
runs the same walk-forward and the same book as STRATEGY §3/§4, prints the gate table for 2024, 2025 and
pooled 2024+2025 (price-only vs with-fundamentals, side by side), and writes out/pm4/. It must refuse to run
if fundamentals_pit coverage for 2019→ is below 70 % of universe rows, printing the coverage instead —
the backfill has to finish first. Add the exact command to NEEDS-MAULIK under the backfill item.

## 4. Stop here and report

Short report: green/red per module, counts, commit hashes, open items. Do not start PM5–PM9. If anything
needs Maulik's hand (the backfill, an image ship, a credential), append it to NEEDS-MAULIK.md and continue
with what does not depend on it.
