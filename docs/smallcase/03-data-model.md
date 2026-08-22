# 03 — Data model for the basket-product layer

Target: the existing `baskfy` Postgres. New tables carry the `cb_` prefix (curated basket)
so the product layer is visually separate from the desk schema and the screener schema it
joins. All money and prices are `numeric` (house rule 9). Rounding happens at write time
(house rule 8). Migrations use whatever mechanism the repo already uses for the Postgres
schema — **SC1's first duty is to inspect the live schema and the M22 `/baskets` code and
record, in DECISIONS-SC, what already exists and is being extended rather than duplicated.**
The spec's §10 model is the source; this file is its Baskfy adaptation.

## Reused, not rebuilt

- **Instrument master and price history** — the screener's tables (instruments, adjusted
  `close` / raw `close_raw` bars, corporate actions, trading calendar). `cb_` tables
  reference instruments by the existing key; no duplicate instrument table.
- **Order journal** — `packages/execution`'s journal remains the record of what was actually
  sent to the broker. `cb_order_batch` *references* desk plan/journal IDs; it never becomes
  a second execution record.
- **Holdings/reconcile** — the desk's holdings snapshot (quantity + t1 + collateral,
  non-negotiable #2) is the broker truth that drift detection compares against.
- **Regime/backtest machinery** — the create-flow's performance preview calls the
  screener's existing backtest paths; nothing new persisted beyond a cached preview.

## New tables

```
cb_manager(id, slug, name, kind[ENGINE|HUMAN|EXTERNAL], sebi_reg_no NULL,
           bio, strategies[], disclosures_md, created_at)
  -- seed rows: 'baskfy-engine' (kind=ENGINE), 'maulik' (kind=HUMAN)

cb_basket(id, slug, name, manager_id → cb_manager,
          type[STOCK|MF|US]  -- only STOCK buildable this run,
          access[FREE|FEE]   -- FEE inert while subscriptions flag is off,
          visibility[PUBLISHED|PRIVATE]  -- PRIVATE = user-created via /create,
          categories[], description_md, rationale_md,
          rebalance_frequency[WEEKLY|MONTHLY|QUARTERLY|ANNUAL|NEED_BASIS],
          benchmark_instrument_id NULL, launched_at, next_review_at NULL,
          source[SCAN|MANUAL]  -- SCAN baskets version themselves from MomentumScan runs,
          scan_strategy_key NULL  -- which engine strategy feeds it,
          archived_at NULL)

cb_basket_version(id, basket_id, version_no, effective_date,
                  label[CHANGED|NO_CHANGE|GENESIS], added_count, removed_count,
                  notes_md NULL, source_scan_run_id NULL, created_at,
                  UNIQUE(basket_id, version_no))
  -- immutable once written; corrections are a new version

cb_constituent(id, version_id → cb_basket_version, instrument_id,
               segment  -- Largecap/Midcap/Smallcap/Debt/Gold... display grouping,
               weight numeric(7,4),
               CHECK sum(weight) per version = 1.0000 (enforced by an insert-time
               assertion in the service layer, tested))

cb_metrics(basket_id, as_of_date, min_amount numeric, volatility_bucket[LOW|MED|HIGH],
           volatility_value numeric, ret_1m, ret_6m, ret_1y, cagr_3y, cagr_5y,
           since_inception_pct, computed_at, PRIMARY KEY(basket_id, as_of_date))
  -- one row per basket per trading day, written by the EOD metrics job (SC2)

cb_collection(id, slug, title, subtitle, basket_ids[], position, curated_meta jsonb)

cb_watchlist_item(id, user_id, basket_id, watched_at, nav_at_watch numeric,
                  UNIQUE(user_id, basket_id))
  -- nav_at_watch = the basket's index value that day, so "moved since watchlisted"
  -- is a pure lookup

cb_investment(id, user_id, basket_id, status[ACTIVE|EXITED],
              version_applied_id → cb_basket_version,
              created_at, exited_at NULL, last_invested_at)

cb_investment_holding(id, investment_id, instrument_id, qty numeric,
                      avg_price numeric, updated_at)
  -- the *intended* ledger; drift = this vs desk holdings snapshot

cb_order_batch(id, investment_id, kind[BUY|INVEST_MORE|SIP|REBALANCE|EXIT|
               PARTIAL_EXIT|CUSTOMIZE], requested_amount numeric NULL,
               desk_plan_id NULL  -- set when a desk plan was generated,
               status[DRAFT|PLANNED|EXPIRED|EXECUTED|PARTIAL|CANCELLED],
               fee_entry_id NULL, created_at, executed_at NULL)
  -- DRAFT/PLANNED from the web; EXECUTED only ever set by reading the desk journal

cb_fee_ledger(id, user_id, batch_id, kind, base_fee numeric, gst numeric,
              total numeric, collected boolean DEFAULT false, accrued_at)

cb_dividend(id, investment_id, instrument_id, ex_date, amount_per_share numeric,
            qty_held numeric, total numeric, source[CORPORATE_ACTIONS])
  -- derived from the recovered corporate-actions table joined to holdings history

cb_sip_plan(id, investment_id, amount numeric, day_of_month int,
            mode[REMINDER]  -- AUTO exists in the enum only after D3,
            status[ACTIVE|PAUSED], next_fire_date, created_at)

cb_pending_action(id, user_id, type[DRIFT|REBALANCE_AVAILABLE|SIP_DUE|GENERIC],
                  payload jsonb, created_at, dismissed_at NULL, resolved_at NULL)

cb_update_post(id, basket_id NULL, manager_id NULL, title, body_md, published_at,
               source[ENGINE|HUMAN])

cb_user_rebalance_state(user_id, version_id, state[APPLIED|SKIPPED|PENDING],
                        decided_at NULL, PRIMARY KEY(user_id, version_id))

-- Track B (dormant): created in SC1, exercised only by SC10's flag-off tests
cb_plan(id, basket_id NULL, manager_id NULL, duration[M1|M3|M6|Y1], price numeric)
cb_subscription(id, user_id, plan_id, start_at, renew_at,
                status[ACTIVE|CANCELLED|LAPSED], auto_renew boolean)
```

## Rules that ride the schema

1. **`user_id` everywhere, one value for now.** Every user-scoped row carries `user_id`;
   the only value written this run is `BASKFY_SOLE_USER_ID` (the two laws' dormant
   multi-tenant clause). No code path infers "the user" implicitly.
2. **Versions are immutable, metrics are recomputable.** `cb_basket_version` and
   `cb_constituent` are append-only facts; `cb_metrics` can be dropped and rebuilt from
   prices + versions and a test proves it (idempotent seeding, house rule 7).
3. **The desk journal is the execution truth.** `cb_order_batch.status=EXECUTED` is set by
   a sync job that reads the journal by `desk_plan_id` — never by the web layer's optimism.
4. **Point-in-time discipline.** Basket performance is computed from versions as they were
   effective on each date (no look-ahead, house rule 5): the basket's return series is a
   chain-linked series over version intervals, weights applied from each version's
   `effective_date` forward.
5. **SCAN baskets are projections.** For a `source=SCAN` basket, a version is *cut from* a
   MomentumScan run (top-N with the strategy's sizing), and the CSV/scan remains the
   upstream artifact. The projection is deterministic and tested: same scan in, same
   version out.
