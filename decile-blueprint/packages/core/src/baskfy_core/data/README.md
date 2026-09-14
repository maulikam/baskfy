# Seed data files

## `nse_trading_holidays.csv`

NSE **equity segment** trading holidays, 2011 → current year, one row per holiday:
`date,name,confidence`. Weekends are *not* listed — they are derived. `confidence` is `fixed`
for a date computed from a rule (a fixed Gregorian date, or Good Friday via the Easter
algorithm) or `circular` for a lunar/movable date taken from NSE's dated circular for that year;
`baskfy_core.trading_calendar.parse_seed_holidays` rejects any other value.

### Provenance and confidence — read this before trusting it

The doc bundle does not contain a complete NSE holiday list, and most years' holiday circulars
were not reachable when this was built, so **this file is a best-effort reconstruction for most
years, not a verified artefact.** It is almost certainly wrong in places for the years where only
`fixed` dates are seeded: Indian exchange holidays follow lunar calendars, several are declared
per-year by circular, and special sessions (Muhurat trading, budget-day Saturdays, live
trading-from-DR-site Saturdays) are not derivable from any rule.

**2026 is the exception.** Its ten lunar/movable equity holidays were added on 14 Sep 2026 from
NSE's 2026 circular, cross-checked across two independent sources (niftyscanner.in and groww.in)
that agreed, each row carrying `confidence=circular`. No other year has had its circular read, so
every other year's file still holds only the deterministic (`fixed`) dates.

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
