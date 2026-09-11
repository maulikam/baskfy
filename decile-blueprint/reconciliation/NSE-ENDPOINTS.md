# NSE endpoints — verified against the live archive

**Verified 22 Aug 2026** by `tools/verify-nse.py`, run through `NSEProvider` itself so the cookie
priming, the token-bucket throttle, the retry policy and the circuit breaker were all exercised —
not around them. Every fetch was archived by the provider's own archive path before it was parsed,
which is what `docs/09` requires and what makes the parse reproducible without re-fetching.

## Why this document exists

`decile-blueprint` was built with its test suite network-blocked (Prompt 2's first acceptance
criterion), so **every URL shape, header and column name below was written from documented file
layouts and had never been fetched**. Its own `CLAUDE.md` said so:

> NSE URL shapes and column names are still unverified. Written from the documented file layout,
> not against a live endpoint. Confirm each URL and header row against a real fetch before the
> first production backfill.

This is that confirmation, and it found one real defect.

## The five capabilities

| Capability | URL | Result |
|---|---|---|
| `listings` | `{archive}/content/equities/EQUITY_L.csv` | **200 — 2,553 rows.** Parsed to `symbol, name, series, isin, listed_on, face_value, paid_up_value, market_lot` |
| `bhavcopy` | `{archive}/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip` | **200 — 3,649 rows** for 2026-08-21, 13 columns including `upper_circuit` |
| `index_snapshots` | `{archive}/content/indices/ind_close_all_{DDMMYYYY}.csv` | **200 — 164 indices.** NIFTY 50 at 24252.0, PE 20.5, PB 2.94, div yield 1.16 |
| `index_constituents` | `{archive}/content/indices/…` | **200 — see the table below** |
| `corporate_actions` | `{base}/api/corporates-corporateActions?index=equities&from_date=…&to_date=…` | **200 — every action in the window**, parsed to typed records. 248 rows for September 2026; 87 for 2026-09-11 alone |

`{archive}` is `https://nsearchives.nseindia.com`, `{base}` is `https://www.nseindia.com`.

> **Corrected 11 Sep 2026.** This row previously read *"200 — 20 actions in the last 30 days"*
> and recorded the URL without a date range. Twenty was never the number of actions in thirty
> days; it is the size of the default first page NSE serves when the range is missing, and the
> reconciliation mistook the page for the answer. `gates/ca-truncation.md` has the evidence: twelve
> consecutive nightly payloads of exactly 20 rows, against 87 actions NSE published for a single
> day. The date range is now mandatory and a 20-row answer to a long window is refused.

## The defect: four constituent files are not named by the rule

`_file_token` built every constituent filename as `ind_{slug-without-hyphens}list.csv`. That is
right for seven of the eleven published universes and **404s for the other four**, which would have
left four of the fourteen selectable universes permanently empty — and `factor_daily.universe_mask`
is a bit per universe, so the failure would have surfaced as screens quietly returning nothing
rather than as an error anyone could trace.

NSE is inconsistent in two ways, neither derivable from the slug:

| Universe | Rule produced | Reality | What differs |
|---|---|---|---|
| `nifty-total-market` | `ind_niftytotalmarketlist.csv` ❌ | `ind_niftytotalmarket_list.csv` ✅ | underscore before `list` |
| `nifty-large-mid-250` | `ind_niftylargemid250list.csv` ❌ | `ind_niftylargemidcap250list.csv` ✅ | "largemid**cap**" |
| `nifty-microcap-250` | `ind_niftymicrocap250list.csv` ❌ | `ind_niftymicrocap250_list.csv` ✅ | underscore before `list` |
| `nifty-mid-small-400` | `ind_niftymidsmall400list.csv` ❌ | `ind_niftymidsmallcap400list.csv` ✅ | "midsmall**cap**" |

Fixed with `_CONSTITUENT_FILE_OVERRIDES`, an explicit table — **named, not guessed**. Adding a
universe means checking the real archive, not extending the rule and hoping.

## All fourteen universes, after the fix

| Universe | Constituents | | Universe | Constituents |
|---|---:|---|---|---:|
| `nifty-50` | 50 | | `nifty-smallcap-250` | 250 |
| `nifty-next-50` | 50 | | `nifty-microcap-250` | 252 |
| `nifty-100` | 100 | | `nifty-mid-small-400` | 400 |
| `nifty-200` | 200 | | `nifty-allcap` | *derived by rule* |
| `nifty-500` | 500 | | `nifty-fno` | *derived by rule* |
| `nifty-total-market` | 752 | | `etf` | *derived by rule* |
| `nifty-large-mid-250` | 250 | | | |
| `nifty-midcap-150` | 150 | | | |

**11 fetched, 3 derived by rule, 0 failures.** Every fetched count matches the index's published
size, which is the strongest evidence available that the right file was read.

## Quirks, documented rather than worked around

- **Cookie priming is required.** `nsearchives.nseindia.com` serves the CSVs, but the session has
  to be established against `www.nseindia.com` first. `NSEProvider._prime_cookies` does this; a
  bare request without it is refused.
- **A 404 arrives as a 200-shaped body in some paths**, which is why `_read_csv` validates the
  header row rather than trusting the status alone. The four failures above surfaced as
  `UnexpectedPayload`, not as a transport error.
- **Rate limiting is real.** The throttle is `nse_rate_limit_per_second`; the probes in this run
  were spaced and nothing was refused.
- **Archive-then-parse**, always. `_archived()` returns bytes read *back* from the archive, so a
  re-parse never re-fetches and the archived file is the reproducibility record.

## What this does NOT establish

- **Kite is still unverified** — `make doctor` reports it unavailable for want of an access token.
  That is M9's external dependency, and `daily_bars` / `list_instruments` are the two capabilities
  no other provider can serve.
- Column-level parity of the bhavcopy against what the factor engine expects is M10/M11's job.
  This document establishes only that the files are reachable and parse into the declared shapes.
