# 03 — Data model: the `op_` schema

Postgres, one Alembic migration `<next>_options.py` in `decile-blueprint/services/api/alembic/
versions/` (the head was `0049_broker_trade` when this pack was written; OP0 records the head it
finds and OP2 takes the next number), SQLAlchemy models in `packages/core/src/baskfy_core/models/
options.py`. Conventions from `models/base.py`: `PRICE` (18,2) for premiums, strikes and index
levels, `INR` (12,2) for money, `BigIntPk`, `CreatedAt`, JSONB `detail`. Every table carries
`user_id` (P4.1) except the market-data tables (`op_contract`, `op_expiry`, `op_index_minute`,
`op_chain_snapshot`), which are shared facts like `ohlcv_daily`. Money and prices are `numeric`,
never `float` (house rule 9), rounded at write time (rule 8). Times are `timestamptz`, displayed
IST; `trade_date` is the exchange date.

**Why `op_` and not `oc_`** (PACK.4): the condor schema was never migrated (the OC run never
started), so there is nothing to rename. Three sleeves share the master, the chain, the index
bars, the calendar, the session machine, the plan/leg/fill shape and the journal; one schema with
a `sleeve` column is one set of tables, one read-only test, one ledger query. Separate `oc_`/`od_`/
`ox_` schemas would triplicate the chain (the largest table) or force cross-prefix joins. The
condor's table designs (`docs/condor/03` §1–§10) are the starting point for every table below.

**Sleeve codes** (a Postgres enum `op_sleeve`): `O1M`, `O1W`, `O2`, `O3A`, `O3B`. Config and flags
group `O3A`/`O3B` as `O3`.

## Joins to what exists

```
instrument (id, symbol, kite_token, instrument_type='INDEX')  ← NIFTY 50 and INDIA VIX rows
index_snapshot_daily (index_id, date, close)                    ← previous close (gap), 20-day EMA (O2 trend), VIX close
trading_day                                                      ← the NSE calendar the whole plant uses
order journal via packages/execution                             ← real or simulated fills → op_fill
settings_audit                                                   ← every op_*config write (M4.1)
sw_* / vb_* / tw_* / the weekly book                            ← NOT joined (Track C §8)
```

**Correction to `docs/condor/03`**: `instrument` does not carry option rows or `expiry` / `strike`
columns — `instrument_type ∈ {EQ, ETF, INDEX}` (`models/reference.py`, `INSTRUMENT_TYPES`). Widening
that enum would put ~a thousand short-lived contracts a week into the table every screen, ranking
and listing query reads. The NFO master is therefore its own table.

## 1. `op_contract` — the NFO master for NIFTY options (market data, shared)

| Column | Type | Meaning |
|---|---|---|
| `instrument_token` PK | bigint | Kite's token |
| `tradingsymbol` | text | as the master states it — never parsed for meaning (weekly and monthly symbols have different shapes); the columns below are the facts |
| `underlying` | text | `NIFTY` (the master's `name`) |
| `expiry` | date | from the master |
| `strike` | PRICE | |
| `option_type` | text | `CE` / `PE` |
| `lot_size` | smallint | |
| `tick_size` | numeric(8,2) | |
| `first_seen`, `last_seen` | date | nights the row was in the master |
| `expired` | bool | `last_seen < expiry` or the master dropped it after expiry |

Written nightly by the instruments task from `instruments("NFO")` filtered to `name='NIFTY'` and
`instrument_type ∈ {CE, PE}`. **Rows are never deleted** — an expired contract stays resolvable so
a past session's legs, snapshots and journal rows keep their meaning (the condor's
`oc_contract_history`, merged in). Size: ~200 strikes × 2 × ~52 expiries a year ≈ 21 k rows a year.

## 2. `op_expiry` — the calendar the desk trusts (market data, shared)

`underlying`, `expiry_date` (PK together), `kind` (`WEEKLY` / `MONTHLY` — monthly is the last
expiry of the calendar month in the master), `lot_size` (as stated for that expiry), `seen_on`.
Rebuilt nightly. A changed `lot_size` or a moved expiry (holiday shift) writes `detail` and raises
an alert, because sizing and the day's role both depend on it.

## 3. `op_event_day` — `date` PK, `reason` (`RBI_POLICY` / `UNION_BUDGET` / `ELECTION_RESULT` / `MANUAL`), `source` (`SEED` / `USER`), `user_id`

As condor `03` §3. Editable from the web tab (it changes no money). Read at every plan build; a day
added after the open still blocks the next plan.

## 4. `op_index_minute` — index one-minute bars (market data, shared)

`instrument_id` (NIFTY 50, INDIA VIX), `ts` (minute start, PK together), `open`, `high`, `low`,
`close`, `source` (`KITE_HIST` / `TICKS`). Written by the intraday bar task (each minute during the
session), the end-of-day reconcile, and the Tier 1 backfill (2015→ as far as Kite serves — OP0
verifies). The desk builds its own bars from ticks for decisions and reconciles against these once
per checkpoint (the swing run's A4 pattern); a disagreement larger than one tick is logged, the
plan uses the desk's.

## 5. `op_chain_snapshot` — the forward dataset (market data, shared; partitioned by month)

| Column | Type | Meaning |
|---|---|---|
| `ts` | timestamptz | the minute |
| `instrument_token` | bigint | → `op_contract` |
| `expiry`, `strike`, `option_type` | | denormalised for the backtest's scans |
| `spot` | PRICE | NIFTY 50 last price at the same call |
| `bid`, `ask`, `last` | PRICE | top of book |
| `bid_qty`, `ask_qty` | integer | |
| `depth_json` | JSONB | five levels each side, as Kite gives them |
| `volume`, `oi` | bigint | |
| `forward` | PRICE | the parity forward for this expiry at this minute (`04` §2.2) |
| `iv`, `delta`, `gamma`, `theta`, `vega` | numeric(10,6) | computed at write by `options.greeks`, **rounded at write**; null where the solver refuses |
| `greeks_model` | text | e.g. `black76-parity-v1` — so a model change never silently changes old rows |
| `source` | text | `QUOTE` (collector) / `VENDOR` (a purchase, if ever) |

Which contracts (`04` §2.1): the two nearest expiries, `snapshot_strikes` [15] strikes either side
of the ATM strike, both types — ≤ 124 symbols, **one `quote()` call a minute** (the provider batches
500). On the last Tuesday that is the monthly plus next week's. ~375 minutes × 124 ≈ 46 k rows a day,
~11 M a year: partition by month, BRIN on `ts`. Rows are append-only; a re-run of a minute is a
no-op on `(ts, instrument_token)` (house rule 7).

## 6. `op_scan` — each sleeve's candidates, each minute

`id`, `user_id`, `sleeve`, `trade_date`, `ts`, `state` (the sleeve's scan state, `04` §10),
`reasons` (text[], every reason not only the first), `numbers` (JSONB: gate values, OR, ER, trend,
distances to trigger), `candidates` (JSONB: each candidate structure priced from the same minute's
snapshot — legs, bid/ask, IV, delta, debit/credit, lots at the budget, max loss, cost share),
`as_of_minute` (the snapshot minute used), `stale` (bool). Latest row per sleeve per day is what the
web tab shows; the history is the scan's own audit trail. Pruned to 90 days except the rows a
session referenced.

## 7. Configuration

`op_book_config` — one row per user: `account_inr` [0], `margin_pool_inr` [0], `daily_loss_limit_inr`
[0 = derived, `04` §9.3], `monthly_pause_inr` [0 = derived], `paused_until`, `paused_reason`,
`updated_at`, `updated_by`.

`op_sleeve_config` — one row per user per sleeve (`O1M`, `O1W`, `O2`, `O3`): `sleeve_capital_inr`
[**0**], `risk_per_trade_pct` [O1M 1.0, O1W 0.5, O2 0.5, O3 0.5], `max_lots` [O1M 3, others 2],
`paper_enabled` [true], `hard_exit_time` [O1 14:30, O2 15:00, O3 14:45], `paused_until`,
`paused_reason`, `updated_at`, `updated_by`. Bounded by `02`'s ceilings; `settings_audit` on every
write — `op_config_audit` (`user_id`, `scope` = `BOOK` or the sleeve group, `key`, `old_value`,
`new_value`, `changed_at`, `changed_by`, `note`; OP2, `DECISIONS-OP` OP2.2).

Strategy thresholds (gap, range, ER, delta band, widths, stops, targets, windows) are **not**
columns: they are fields of `baskfy_core.options.config` with the defaults of `04`, shown read-only
on the settings page; changing one is a `DECISIONS-OP` entry that edits `04` and the default
together (condor PACK.7, carried as PACK.2).

## 8. `op_session` — one row per sleeve per trade date

As `oc_session` (condor `03` §4) plus `sleeve`, `expiry_used` (the contract's expiry), and
generalised observation columns in `numbers` JSONB (O1's gap/range/ER/containment; O2's trend,
OR, trigger bar; O3's morning range, ER, gap). `mode` (`PAPER` / `LIVE`, from `options_gates()` at
09:15, never changed later), `state` (`04` §11), `verdict`, `skip_reasons`, `plan_id`,
`closed_reason`, `pnl_inr`, `pnl_r`, `slot_holder` (for O1/O3 exclusivity: the sleeve that holds
today's expiry-day slot). Unique `(user_id, sleeve, trade_date)`.

## 9. `op_plan`, `op_leg`, `op_order`, `op_fill`

`op_plan`: as `oc_plan` plus `sleeve`, `structure` (`IRON_CONDOR` / `LONG_OPTION` / `DEBIT_SPREAD`),
`sizing_mode` (`BUDGET` / `PAPER_ONE_LOT`), `debit_points` / `credit_points`, `risk_per_lot_inr`,
`risk_budget_inr`, `entry_window_end`. `expires_at = min(issued_at + 30 min, entry_window_end)`.

`op_leg`: as `oc_leg` (`seq`, `role` ∈ `LONG_CALL` / `LONG_PUT` / `SHORT_CALL` / `SHORT_PUT`,
`instrument_token`, `strike`, `side`, `quantity`, `limit_price`, `iv_at_plan`, `delta_at_plan`,
`bid`, `ask`, `depth_json`, `status`, `filled_qty`, `avg_price`, `simulated`).

`op_order`: one row per order the desk sends, `client_id = plan_id:symbol` (entry) or
`plan_id:symbol:CLOSE` (exit) — minted by `packages/execution` (M66), `product` (always `MIS`),
`exchange` (`NFO`), the gateway's result, `simulated`. (Each sleeve has its own order table —
`vb_order`, `tw_order` — and this is the options book's.)

`op_fill`: `leg_id`, `order_id`, `filled_at`, `quantity`, `price`, `simulated`, `sim_method`
(`DEPTH_LADDER` / `LIVE`), `detail` (the ladder walked).

## 10. `op_position` — the open structure, one per session

`session_id` PK, leg ids, `entry_points` (C for a credit, the debit for a debit, the premium for a
long — **from fills, not the plan**), `entry_inr`, `lots`, `opened_at`, `hard_exit_at`, `peak_value`,
`last_mark_points`, `last_mark_at` (persisted every 30 s so a restart resumes), `exit_plan_id`,
`closed_at`, `simulated`.

## 11. `op_journal` — one row per session that opened

`session_id`, `sleeve`, `trade_date`, `expiry_used`, `structure`, `entry_inr`, `exit_inr`,
`gross_pnl_inr`, `costs_inr` (breakdown in `detail`), `net_pnl_inr`, `risk_budget_inr`,
`r_multiple`, `closed_reason`, `minutes_held`, `mae_r` / `mfe_r` (worst and best marked excursion,
in R), `simulated`, `sizing_mode`, `half_size`. The ledger (`04` §9) is a query over this plus
today's open marks. **Sleeves, and real vs simulated, are never pooled in one number.**

## 12. `op_backtest_run` — append-only

As `oc_backtest_run` plus `sleeve`: `tier`, `params_json`, `date_from`, `date_to`, `sessions`,
`signals`, `traded`, `skipped_by_reason_json`, `win_rate`, `expectancy_r`, `net_pnl_inr`,
`max_drawdown_r`, `caveats` (verbatim from `07`, stored on the row), `ran_at`, `git_sha`.
