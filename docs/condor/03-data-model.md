# 03 — Data model: the `oc_` schema

Postgres, one Alembic migration `<next>_condor.py` in `services/api/alembic/versions/` (the head
was `0032` when the swing run closed; OC0 records the head it found and OC2 takes the next
number), SQLAlchemy models in `packages/core/src/baskfy_core/models/condor.py`. Conventions
inherited from `models/base.py`: `PRICE` (18,2) for premiums and strikes, `INR` (12,2) for money,
`BigIntPk`, `CreatedAt`, JSONB for `detail`. Every table carries `user_id` (P4.1) and, where a
broker is involved, `broker_account_id`. Money and prices are `numeric`, never `float` (house
rule 9); rounding happens at write time (rule 8). Times are stored as `timestamptz` and
displayed in IST; `oc_session.trade_date` is the exchange date.

Nothing here replaces an existing table. The joins are:

```
instrument (id, symbol, kite_token, exchange, segment, expiry, strike, instrument_type, lot_size)
                                                          ← the NFO rows the chain reader resolves
index_snapshot_daily (index_id, date, close)              ← previous close for the gap test
desk journal (order_journal / fills, via packages/execution) ← real or simulated fills for oc_fill
sw_session / the weekly book                              ← NOT joined; Track C §7
```

`instrument` today holds NSE/BSE equities. OC2 extends the nightly `instruments` task to also
persist the **NFO** rows for the configured underlyings (`NIFTY`, and `BANKNIFTY` dark) — the
same `list_instruments` call with `exchange="NFO"`, filtered to index options, kept for the
current and next two expiries and pruned after expiry into `oc_contract_history` so a past
session's legs stay resolvable. The `expiry` and `lot_size` columns are the only source the
calendar and the sizing read.

## 1. `oc_config` — one row per user

| Column | Type | Meaning |
|---|---|---|
| `user_id` PK | bigint FK | sole user in this run |
| `account_inr` | INR | the account the ceiling is a fraction of; default **0** — nothing is planned until Maulik sets it |
| `margin_pool_inr` | INR | the collateral/margin pool the plan must fit under; default 0 |
| `execution_buffer_inr` | INR | unpledged cash kept for exits; informational, shown on the page |
| `risk_budget_inr` | INR | the per-expiry loss budget the lots come from; default **10000**; ≤ `BASKFY_CONDOR_RISK_PER_EXPIRY_INR_MAX` and ≤ `BASKFY_CONDOR_RISK_PCT_MAX` % of `account_inr` |
| `daily_loss_limit_inr` | INR | default 30000; ≤ the ceiling |
| `monthly_pause_inr` | INR | default 75000; ≤ the ceiling |
| `max_lots` | smallint | default 3; ≤ `BASKFY_CONDOR_MAX_LOTS_MAX` |
| `underlyings` | text[] | default `{NIFTY}`; `BANKNIFTY` may be added only with its flag on (the API refuses otherwise) |
| `hard_exit_time` | time | default 14:30; ≤ `BASKFY_CONDOR_HARD_EXIT_LATEST` |
| `paused_until` | date | set by the risk ledger (§8) or by hand; the plan builder refuses while today ≤ this |
| `paused_reason` | text | `MONTHLY_PAUSE` / `DAILY_LIMIT` / `MANUAL` |
| `updated_at`, `updated_by` | | `settings_audit` on every write (M4.1) |

The tunables of `04` that are *not* money (delta band, wing width, credit floor, gate thresholds,
ER cap, profit/stop multiples, cost-share cap) live in `baskfy_core.condor.config` as fields
with the pack defaults and are exposed on `/condor/settings` **read-only** in this run; changing
one is a DECISIONS-OC entry that edits `04` and the default together (PACK.7).

## 2. `oc_expiry` — the calendar the desk trusts

| Column | Type | Meaning |
|---|---|---|
| `underlying` | text | `NIFTY` / `BANKNIFTY` |
| `expiry_date` PK(with underlying) | date | from the instrument master, never computed |
| `kind` | text | `MONTHLY` / `WEEKLY` — monthly is the last expiry of the calendar month for the underlying |
| `lot_size` | smallint | as the master stated it on `seen_on` |
| `seen_on` | date | the night the row was written or last confirmed |
| `tradeable` | bool | `kind = MONTHLY` and not an event day |

Rebuilt nightly by the instruments task; a change in `lot_size` or a monthly expiry moving (an
exchange holiday) writes a `detail` note and an alert, because the sizing and the date both
depend on it.

## 3. `oc_event_day` — the days the book does not trade

| Column | Type | Meaning |
|---|---|---|
| `date` PK | date | |
| `reason` | text | `RBI_POLICY` / `UNION_BUDGET` / `ELECTION_RESULT` / `MANUAL` |
| `source` | text | `SEED` (the pack's list, `04` §1.3) / `USER` |
| `user_id` | bigint | |

Editable from the web hub (Track A — it changes no money). The gate reads it at 09:59 and at
plan time; a day added after 09:59 still blocks the plan.

## 4. `oc_session` — one row per underlying per expiry day

| Column | Type | Meaning |
|---|---|---|
| `id` PK | | |
| `user_id`, `broker_account_id` | | |
| `underlying`, `trade_date` | | unique together |
| `mode` | text | `DRY_RUN` / `LIVE` — from `condor_gates()` at 09:15, never changed later |
| `state` | text | `OBSERVING → SKIPPED \| PLANNED → LAPSED \| CONFIRMED → OPEN → CLOSED`, one-way (`04` §9) |
| `prev_close`, `open_price` | PRICE | the index |
| `or_high`, `or_low` | PRICE | the 09:15–09:44 opening range |
| `obs_high`, `obs_low`, `p_0915`, `p_0959` | PRICE | the 09:15–09:59 observation |
| `gap_pct`, `range_pct`, `er` | numeric(8,4) | the gate's three numbers |
| `verdict` | text | `TRADE` / `SKIP` |
| `skip_reasons` | text[] | every reason that fired, not just the first (`04` §2.6) |
| `plan_id` | FK | the plan issued at 10:00, if any |
| `closed_reason` | text | `PROFIT` / `STOP` / `STRIKE_TOUCH` / `HARD_EXIT` / `MANUAL` / `NEVER_OPENED` |
| `pnl_inr`, `pnl_r` | | after costs, written once at close |
| `detail` | JSONB | the minute series the verdict was computed from (so the page can draw it and a test can recompute it) |

## 5. `oc_plan` and `oc_leg`

`oc_plan`: `id`, `plan_id` (the token the desk issues; `client_id = plan_id:symbol` on every
order, non-negotiable 6), `session_id`, `issued_at`, `expires_at` (`issued_at + 30 min`, and
never past 10:15 for an entry plan), `kind` (`ENTRY` / `EXIT`), `credit_points`,
`credit_inr`, `width_points`, `lots`, `lot_size`, `max_loss_inr`, `profit_target_inr`,
`stop_inr`, `expected_cost_inr`, `cost_share` (cost ÷ profit target), `margin_required_inr`
(the broker's basket estimate), `status` (`ISSUED` / `CONFIRMED` / `LAPSED` / `REJECTED`),
`rejected_code`, `confirmed_at`, `detail` (the quotes each leg was priced from).

`oc_leg`: `id`, `plan_id`, `seq` (the send order: wings 1–2, shorts 3–4 on entry; shorts 1–2,
wings 3–4 on exit), `role` (`LONG_CALL` / `LONG_PUT` / `SHORT_CALL` / `SHORT_PUT`),
`tradingsymbol`, `instrument_token`, `strike`, `option_type`, `side`, `quantity`, `limit_price`,
`delta_at_plan`, `bid`, `ask`, `depth_json`, `status` (`PENDING` / `SENT` / `FILLED` /
`PARTIAL` / `CANCELLED` / `REJECTED`), `order_id`, `filled_qty`, `avg_price`, `simulated`.

## 6. `oc_position` — the open condor, one per session

`session_id` PK, the four `oc_leg` ids, `entry_credit_points` (C, from fills not from the plan),
`entry_credit_inr`, `lots`, `opened_at`, `hard_exit_at`, `last_d_points` (D, refreshed by the
engine and persisted every 30 s so a restart resumes), `last_mark_at`, `exit_plan_id`,
`closed_at`, `simulated`.

## 7. `oc_fill` — every fill, real or simulated

`leg_id`, `order_id`, `filled_at`, `quantity`, `price`, `simulated`, `sim_method`
(`DEPTH_LADDER` / `MID` / `LIVE`), `detail` (the depth ladder walked, for a simulated fill).

## 8. `oc_journal` — the closed record, one row per session that opened

`session_id`, `underlying`, `trade_date`, `credit_inr`, `close_cost_inr`, `gross_pnl_inr`,
`costs_inr` (the full breakdown in `detail`), `net_pnl_inr`, `r_multiple` (net ÷ the risk budget
in force), `closed_reason`, `minutes_held`, `max_d_over_c` (the worst D/C seen), `simulated`,
`half_size` (the first-live multiplier, `04` §6.4). The risk ledger (`04` §8) is a query over
this table plus today's open position's mark.

## 9. `oc_chain_snapshot` — the forward dataset (`07` §3)

`underlying`, `trade_date`, `ts` (minute), `spot`, `strike`, `option_type`, `bid`, `ask`,
`last`, `volume`, `oi`, `depth_json` (five levels each side), `source` (`QUOTE`). Partitioned by
month; one row per strike per minute for the strikes within `chain.snapshot_strikes` of the
spot. Written by the collector, read by Tier 3 of the backtest when it exists, and by the paper
record's "what would the fill have been" check.

## 10. `oc_backtest_run` — append-only, per tier

`id`, `tier` (1/2/3), `underlying`, `params_json`, `date_from`, `date_to`, `sessions`,
`traded`, `skipped_by_reason_json`, `win_rate`, `expectancy_r`, `net_pnl_inr`, `max_drawdown_inr`,
`caveats` (the tier's caveat text, verbatim from `07`), `ran_at`, `git_sha`.
