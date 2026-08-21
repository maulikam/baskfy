# 04a — Addendum: the `trading_day` table

`PROMPTS.md` Prompt 1 deliverable 3 requires a `trading_day` table and a holiday loader, but
`docs/04-data-model.md` does not define one. This addendum specifies it. It is an addition to the
data model, not a change to it.

## Why it exists

`docs/13-csv-export-schema.md` §3 proved the factor windows are **calendar offsets snapped to
trading days**, not fixed bar counts, and that every instrument shares one window start date.
That makes "was the NSE open on date D" a load-bearing question for:

- window starts (`docs/05`, `docs/13` §3),
- the as-of snap-backwards rule (`docs/06` step 1),
- the historical-date picker (`docs/07` `GET /meta/trading-days`),
- the backtest's rebalance dates (`docs/10`).

## DDL

```sql
CREATE TABLE trading_day (
  exchange_id     smallint NOT NULL REFERENCES exchange(id),
  date            date     NOT NULL,
  is_trading_day  boolean  NOT NULL,
  holiday_name    text,                 -- NULL unless is_trading_day = false for a holiday
  source          text     NOT NULL,    -- 'weekend'|'holiday'|'derived'|'bhavcopy'
  PRIMARY KEY (exchange_id, date)
);
CREATE INDEX ON trading_day (is_trading_day, date);
```

Every calendar day in the covered range gets a row, weekends included, so that "not a trading
day" and "outside the loaded range" are distinguishable, and so snapping is one indexed lookup.

## `source` is a confidence level, in increasing order of authority

| Value | Meaning |
|---|---|
| `weekend` | Saturday or Sunday. Derived, and certain. |
| `holiday` | Listed in the seeded NSE holiday file. Only as good as that file. |
| `derived` | Assumed open because nothing said otherwise. The weakest claim in the table. |
| `bhavcopy` | Corroborated by real NSE market data for that date. Authoritative. |

## The seed list is deliberately incomplete

The bundle contains no NSE holiday list, and NSE's holiday circulars were not reachable when this
was built. `packages/core/src/decile_core/data/nse_trading_holidays.csv` therefore contains only
the holidays that are **deterministic**: the fixed Gregorian dates (Republic Day, Ambedkar
Jayanti, Maharashtra Day, Independence Day, Gandhi Jayanti, Christmas) plus Good Friday, computed
from the Easter algorithm.

India's lunar-calendar holidays — Holi, Mahashivratri, Ram Navami, Id-ul-Fitr, Bakri Id, Muharram,
Janmashtami, Ganesh Chaturthi, Dussehra, Diwali Laxmi Pujan and Balipratipada, Guru Nanak Jayanti
— are declared per year by circular and are **not** in the file. Guessing them would put
plausible-looking wrong dates into the spine of every factor window, which is a worse failure than
an obviously missing one: a seeded calendar currently yields ~255 trading days a year against a
real ~246–250.

## How it gets correct

`decile_core.trading_calendar.reconcile()` promotes any date with real NSE bars to
`source='bhavcopy'`. It never demotes: an absent bar can mean a holiday *or* a gap in the
backfill, and only Prompt 3's universe-wide view can tell those apart.

**Prompt 3 must** run that reconciliation across the full backfill, and add the
"no instrument in the universe traded on D, therefore D was a holiday" rule.
**Prompt 5 must** assert the recovered window lengths of 22 / 64 / 121 / 185 / 247 trading days
as of 2026-08-18 (`docs/13` §3). That assertion — not the seed file — is the real acceptance test
for this calendar, and no factor should be published until it passes.
