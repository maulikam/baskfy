# 04f — the "live at any login time" tables (LV pack, 27–28 Sep 2026)

An addendum to `04-data-model.md` for the tables `docs/live/PLAN.md` added. Migrations 0055 and
0056; models in `baskfy_core.models.live` and `baskfy_core.models.eq_bars`. All in the `public`
schema beside the sleeves' `sw_*`/`tw_*`/`vb_*` tables, for the reason those are: the desk's stores
prefix `public.` and the web API reads the two state tables without a second connection.

| Table | Primary key | What it holds | Written by |
|---|---|---|---|
| `lv_protection_issue` | `id` | One thing wrong with one position's protection — `NAKED`, `GTT_MISSING`, `GTT_OVERSIZED`, `GTT_UNDERSIZED`, `GTT_TRIGGERED_UNFILLED`, `EXTERNAL_EXIT`, `STOP_REJECTED` — open until the reconciler stops seeing it (`resolved_at`). One open row per `(user_id, sleeve, position_id, kind)` (partial unique index). Every sleeve's buy refuses while one is open for that sleeve. | the desk's reconciler (`app/reconcile.py`) |
| `lv_heartbeat` | `(user_id, process)` | The last word from each desk process — `supervisor`, `reconciler`, `swing_monitor`, `twt_auto`: state, detail, when. `GET /sleeves/state` reads it. | the session supervisor, the reconciler, the swing monitor |
| `lv_exit_order` | `id` | A sell the broker accepted for a sleeve position and has not yet filled (VBT's `SELL_AT_OPEN`). The position is booked only from the broker's fill, never from the placement. | `app/vbt_execute.py`, reconciled by `app/reconcile.py` |
| `lv_adoption` | `id` | A holding bought by hand in Kite and adopted into a sleeve explicitly: the stated cost and quantity, the stop it came with. | the desk's `/lifecycle/adopt` |
| `risk_ledger` | `(user_id, day)` | The account-wide risk state (`baskfy_execution.risk.RiskState`) as one JSON row per IST day, taken `SELECT … FOR UPDATE` by every process that places an order. | `baskfy_execution.risk.RiskManager` through the desk's `PgRiskStateStore` |
| `eq_minute_bar` | `(instrument_id, ts)` | One-minute equity bars for the swing book's liquid universe: raw prints, `numeric(18,2)`, volume, source. A TimescaleDB hypertable on `ts` with monthly chunks. **Off on the box** (`BASKFY_EQ_BARS_ENABLED=false`, Maulik 28 Sep 2026): the table stays, empty, for the day a backtest wants it. | `baskfy_worker.eq_bars` (the 15:45 job and the backfill CLI) |

**Migration 0057 (LV8, 28 Sep 2026)** adds no table: it widens `tw_plan.source` / `vb_plan.source`
with `LIVE`, `vb_plan_line.kind` with `BUY_AT_MARKET`, and puts `provisional boolean default false`
on `tw_scan_run`, `vb_scan_run`, `tw_signal_daily`, `tw_state_daily`, `tw_breadth_daily`,
`vb_signal_daily` and `vb_breadth_daily` — recorded in `docs/twt/03` §7 and `docs/vbt/03` §7.

None of these is an input to a rank, a size or a plan. `catalyst_tag_correction` (migration 0053,
OV4) and `candidate_review_label` (0054, OV8) are the overlap page's label tables and are recorded
in `docs/DECISIONS-MERGE.md`'s overlap entries; they are listed in the schema test beside these.
