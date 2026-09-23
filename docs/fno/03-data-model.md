# 03 — Data model: the `fo_` schema

One Alembic migration, `<next>_fno.py`, goes in `decile-blueprint/services/api/alembic/versions/`.
The head was `0050_options` when this pack was written; FO0 records the head it finds, and FO2
takes the next number. Models go in `packages/core/src/baskfy_core/models/fno.py`. The conventions
are those of `docs/options/03`: `PRICE` (18,2), `INR` (12,2), `BigIntPk`, `CreatedAt`, JSONB
`detail`; money and prices are `numeric`, rounded at write time; times are `timestamptz`, and
`trade_date` is the exchange date. Every table carries `user_id` (P4.1), except the market-data
tables (`fo_contract_daily`, `fo_underlying_daily`), which are shared facts like `ohlcv_daily`.

**Why `fo_` and not more `op_` tables** (PACK.5): the O-sleeves are intraday NIFTY and live on a
minute chain. The FO sleeves are end-of-day, span every F&O underlying, and carry across closes.
They share the NFO master (`op_contract`, widened by FO2) and nothing else. One schema per clock
keeps the minute chain, the largest table in the options pack, out of every nightly query.

**Sleeve codes** (Postgres enum `fo_sleeve`): the codes in `01`. They are fixed by the research,
so `01` is the list.

## Joins to what exists

```
op_contract (widened in FO2)       ← every NFO contract: lot, tick, strike step, expiry, token
instrument / ohlcv_daily           ← the stock's cash series (display cross-check)
index_snapshot_daily               ← NIFTY 50 / BANK NIFTY closes (display cross-check only)
trading_day                        ← the NSE calendar
order journal (packages/execution) ← real or simulated fills → fo_fill
settings_audit                     ← every fo_*config write
op_* / sw_* / vb_* / tw_* / weekly ← NOT joined (Track C §7)
```

## 1. `fo_contract_daily` — the F&O bhavcopy (market data, shared)

One row per contract per trading day, from `NSEProvider.fo_bhavcopy` (`FO_BHAVCOPY_SCHEMA`).
Partitioned by month on `trade_date`.

| Column | Type | Notes |
|---|---|---|
| `trade_date` | date | PK part |
| `instrument` | text | `FUTSTK`, `OPTSTK`, `FUTIDX`, `OPTIDX` (the legacy vocabulary, for both layouts) |
| `symbol` | text | PK part |
| `expiry` | date | PK part |
| `strike` | numeric(18,2) | PK part; 0 for futures |
| `option_type` | text | PK part; `CE`, `PE` or `XX` for futures (not null in the key) |
| `open`, `high`, `low`, `close`, `settle` | numeric(18,2) | |
| `underlying` | numeric(18,2) null | UDiFF only (8 Jul 2024 onward) |
| `open_interest`, `oi_change`, `volume` | bigint | OI in shares, as NSE prints it |
| `turnover` | numeric(20,2) | ₹ |
| `lot_size` | int null | UDiFF only |
| `source_key` | text | the raw-archive key (docs/09: every parsed row names its file) |

**Idempotent** (house rule 7): the nightly task upserts a day's rows by the PK. A re-run writes
identical rows. A file stamped with another session's date is refused by the provider, before any
write.

**Retention.** Everything for futures. For options: every contract of the two nearest monthly
expiries of each underlying, plus NIFTY and BANKNIFTY's weeklies, with non-zero OI or volume. That
is ~13,000 rows a day and ~3.3 M a year. The raw zip stays in the archive, so a trimmed row is
refetchable without asking NSE again.

## 2. `fo_underlying_daily` — the derived per-underlying series (market data, shared)

One row per underlying per day, computed from §1 by `baskfy_core.fno.series` (pure). Idempotent
and re-derivable from §1 at any time.

| Column | Notes |
|---|---|
| `trade_date`, `symbol` | PK |
| `held_expiry` | the contract the continuous series held into this session (`04` §4) |
| `level_o/h/l/c` | continuous, roll-free, tradeable levels (`04` §4) |
| `ret` | log return of the held contract, previous settle → this settle |
| `atr14` | on the continuous levels |
| `oi_total` | futures OI across all expiries |
| `fut_turnover_20d` | 20-session median futures turnover (the liquidity rank) |
| `iv_atm` | ATM implied vol of the nearest monthly with ≥ 8 sessions left (Black-76 on the future) |
| `rv20` | 20-session realised vol of `ret`, annualised |
| `basis_ann` | `(F/S − 1) · 365 / days`, UDiFF days only |
| `in_ban` | the F&O ban list for the next session (Track C §9) |
| `ca_flag` | a session where the held future moved more than 30 % (a split or bonus): excluded from signals for 5 sessions |

## 3. `fo_scan` — tomorrow's candidates, per sleeve (per user)

Written by the nightly scan after §2 lands. Idempotent per `(user_id, sleeve, trade_date,
symbol)`. Columns: `state` (`04` §8), and the proposed structure in `detail` (legs with strike, type,
expiry and quantity sign; the entry reference prices from the bhavcopy), `credit`,
`max_loss_per_lot`, `cost_share`, `iv`, `rv20`, `iv_rv`, and the `reasons` list in words. Every skip carries its reason,
never a blank.

## 4. `fo_plan`, `fo_leg` — the morning plan and its legs (per user)

The shape of `op_plan`/`op_leg`, with three additions:

* `fo_plan.structure` (`IRON_CONDOR` for F1, `FUTURE` for F2; a later sleeve widens the enum by
  migration) and `fo_plan.kind` (`ENTRY`, `EXIT`, `ROLL`) and `fo_plan.max_loss_inr`, computed from the legs, never entered.
* `fo_leg.entry_seq`: the order legs go out in. Longs first, and the covered-overnight guard
  re-proves every prefix (`02` §2.1).
* `fo_plan.hard_exit_date`: `E − fo_hard_exit_before_expiry`, in exchange sessions from
  `trading_day`, so a holiday is handled. The exit engine and the guard both read it.

`plan_id` is deterministic; `expires_at = min(issued + 30 min, 10:30)`; the entry window is
09:20–10:30 (`04` §1). A lapsed plan is a row, not a deletion.

## 5. `fo_position`, `fo_fill`, `fo_mark` — the carried book (per user)

* `fo_position`: one row per open structure, with sleeve, symbol, legs, entry price or credit,
  max loss, profit-take and loss-close levels (F1), the current trailing stop and its GTT id (F2),
  `hard_exit_date` (F1) or next roll date (F2), and `simulated`.
* `fo_fill`: every order's fill, real or simulated (`simulated=true` whatever `DRY_RUN` says while
  the sleeve's flag is off). The paper fill walks the live depth at confirm time (options PACK.2's
  simulator, reused).
* `fo_mark`: one row per open position per session, marked at that session's bhavcopy **settle**.
  This is the nightly P&L, and the source of the drawdown and the pause rules.

## 6. `fo_journal`, `fo_sleeve_config`, `fo_book_config`

The journal is closed trades in ₹ and R, one row per structure, never pooling `(sleeve,
simulated)`. Config is per-sleeve capital (default ₹0; QUESTIONS Q1), risk %, max concurrent
positions, and the book's loss pause. Every write goes through `settings_audit`.

## 7. `fo_backtest_run`, `fo_spread_sample`

* `fo_backtest_run`: one row per family per run (`04` §6). It holds `family`, `params` (JSONB),
  `tier` (`2E`), `caveat` (the verbatim text), `sample_from`/`sample_to`, `n`, `net_r`, `gross_r`,
  `per_year` (JSONB), `slippage_source` (`ASSUMED` or `MEASURED`) and `run_at`. It is append-only,
  and the page reads the latest row per family.
* `fo_spread_sample`: the 15:00 quote sample (`01` §2), with `trade_date`, `symbol`, `expiry`,
  `strike`, `option_type`, `bid`, `ask`, `mid`, `oi` and `taken_at`. It is market data (shared)
  and append-only. The re-test reads its per-underlying median half-spread ÷ mid.
