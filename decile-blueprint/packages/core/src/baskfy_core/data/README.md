# Seed data files

## `nse_trading_holidays.csv`

NSE **equity segment** trading holidays, 2011 → current year, one row per holiday:
`date,name`. Weekends are *not* listed — they are derived.

### Provenance and confidence — read this before trusting it

The doc bundle does not contain an NSE holiday list, and this environment has no access to
NSE's holiday circulars, so **this file is a best-effort reconstruction, not a verified
artefact.** It is almost certainly wrong in places: Indian exchange holidays follow lunar
calendars, several are declared per-year by circular, and special sessions (Muhurat trading,
budget-day Saturdays, live trading-from-DR-site Saturdays) are not derivable from any rule.

Treat every row with `source='holiday'` or `source='derived'` in `trading_day` as *provisional*.

### How it gets corrected

`baskfy_core.trading_calendar.reconcile_from_bars()` promotes any date that has real NSE
bars/bhavcopy to `source='bhavcopy'`, which is authoritative and overrides this file. Prompt 3
(ingestion) must run that reconciliation over the full backfill, and Prompt 5's factor engine
asserts the recovered window lengths (22 / 64 / 121 / 185 / 247 trading days as of 2026-08-18,
docs/13 §3) — which is the real acceptance test for this calendar.

Until that reconciliation has run, do not use this calendar to compute a published factor.

## `india_tbill.csv`

OECD MEI India IR3TIB — monthly 3-month short-term interest rates, percent per annum.
Columns: `date,annual_pct`. First observation 2011-11-01, last 2026-06-01 (176 rows).
Fetched 24 Aug 2026 from the OECD SDMX API (`DSD_STES@DF_FINMARK`). This is a published
**proxy** for the 91-day T-bill, not the RBI auction cutoff. Worker backtests attach it via
`baskfy_core.risk_free.attach_tbill_curve`. Refresh by replacing this file; there is no
runtime OECD provider (house rule: network only through Kite/NSE).
