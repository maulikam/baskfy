# DECISIONS-LV — the "live at any login time" pack

Judgement calls made while executing `docs/trading-readiness-review-2026-09-27.md` under the
autonomy charter (root CLAUDE.md). Every entry is **⚠ UNREVIEWED** until Maulik reads it. Plan and
contract: `docs/live/PLAN.md`; gates: `gates/live-*.md`.

## LV0.1 — Scope of this run: the review's steps 1–4 are built; step 5 is handed back as questions (27 Sep 2026) · ⚠ UNREVIEWED

**Context.** `/unlazy` was pointed at the review. Its delivery order is five steps: (1) market data off
`DRY_RUN`; (2) login as the event; (3) fill reconciliation and account-wide risk; (4) intraday equity
bars; (5) new strategy variants — live-entry TWT and VBT, pyramiding and re-entry, fixed targets — each
versioned and backtested. The review itself says of step 5: "No numerical thresholds are approved by this
review", "a target is a new variant, not a default", and "promote only on their own evidence".

**Choice.** Build steps 1–4 in full (LV1–LV6) plus the read-only audit the review puts first (LV0), and
**do not build step 5**. A strategy variant is a trading decision: which entry price, what maximum
addition, which target — none of which an agent may invent (precedence: the seven non-negotiables and
the sleeves' own decision logs over an acceptance criterion). What LV5 builds is the data step 5 needs and
does not have; the questions step 5 turns on are put to Maulik as options in `NEEDS-MAULIK.md` (LV7).

**Rejected.** (a) Building a "live-entry TWT" by relabelling the EOD scan or substituting today's LTP for
the tested open — the review names this as the thing not to do (P1.3). (b) Removing `ALREADY_HELD` to
"allow pyramiding" — a guard removed is not a position model. (c) Stopping after step 1 and asking — the
charter says run long; steps 2–4 are engineering against rules that already exist.

**Reverse.** Nothing to reverse; the omission is recorded here and in the status page.

## LV1.1 — Market data and order permission become two switches (27 Sep 2026) · ⚠ UNREVIEWED

**Context.** `live_prices.quotes_permitted()` returned `False` whenever `DRY_RUN` was on
(`services/api/src/baskfy_api/live_prices.py`). The API's `DRY_RUN` exists to keep *orders* in
rehearsal — but the API has no execute route (D9, non-negotiable 1), so on this process the flag guarded
nothing but quotes. The review's gap 2: "the same switch that keeps execution in rehearsal also removes
every live price from every screen."

**Choice.** `quotes_permitted()` = `market_data_enabled()` **and** an API key **and** a real, unexpired,
non-simulated token. `market_data_enabled()` reads `BASKFY_LIVE_QUOTES` (default `"true"`). The flag is
read-only market data; nothing order-capable reads it (gate M2). `DRY_RUN` keeps every meaning it has
elsewhere — the desk's `DRY_RUN` is untouched, and the OAuth callback still stores a *simulated* token
under `DRY_RUN`, which `quotes_permitted()` still refuses, so a dry-run API without a real login shows the
close exactly as before.

**Rejected.** A second money-shaped flag defaulting false (would keep the screens dark until somebody
flips it — the review's first step exists to stop that). Reading the desk's flags (a different process).

**Also built with it (the review's P1.1).** Every quote carries the exchange's own time (`LiveQuote.as_of`, from
Kite's `timestamp`/`last_trade_time`) and a `stale` verdict against the answer's `served_at` (older than
120 s); the answer counts `requested` and `covered`; the browser drops the whole overlay when its last
good answer is older than 90 s or the last fetch failed, mutes a stale row with the print's IST time in
the tooltip, and the status line says "42 of 50 live" when the answer was partial. A quote with no stamp
is not stale — its age is unknown, and the cell says so — rather than invented fresh.

**Tests.** `services/api/tests/test_live_prices.py` (the DRY_RUN and `BASKFY_LIVE_QUOTES` cases),
`test_meta_live_marks.py` (as_of, stale, served_at, requested, covered, the 120 s rule, a partial answer
still live), web `live-marks.test.ts` and `live-price.test.tsx` (age cutoff, error → close, stale row,
coverage sentence, IST tooltip).

**Reverse.** Put `if dry_run_enabled(): return False` back at the top of `quotes_permitted()`.

## LV2.1 — One reconciler for three books, reading the broker once a pass (28 Sep 2026) · ⚠ UNREVIEWED

**Context.** Review P0.1: TWT's and VBT's real buys were recorded ``SENT`` and never turned into a
position, a fill or a GTT; only the dry-run branch did. VBT's real sell booked the exit at the
reference price before the broker had filled it. Swing alone polled, handled updates and swept.

**Choice.** `kite-momentum-rebalancer/app/reconcile.py`: one pass reads the order book, the GTT list
and the holdings **once each** and hands each sleeve's open orders to that sleeve's own idempotent
`on_order_update` — swing's as it was, TWT's rewritten for partial fills and dead orders, VBT's new
for buys and pending exits. Findings about protection go to a new table `lv_protection_issue`
(public schema beside `tw_*`/`vb_*`, migration 0055 with `lv_heartbeat`, `lv_exit_order`,
`lv_adoption`, `risk_ledger`); a finding this pass could observe and did not see is resolved, and a
broker read that failed resolves nothing of its kind. Kinds: `NAKED`, `GTT_MISSING`,
`GTT_OVERSIZED`, `GTT_UNDERSIZED` (added to the contract's list: fewer shares covered than held is
also unresolved protection), `GTT_TRIGGERED_UNFILLED`, `EXTERNAL_EXIT`, `STOP_REJECTED`.
`python -m app.reconcile` runs one pass (restart recovery); the session supervisor (LV4) runs it
every ten seconds in session.

**Rejected.** Kite postbacks (need a public URL the desk does not expose; the order book is the same
truth pulled). A per-sleeve loop (three reads of the same book a pass). A KiteTicker order-update
callback (one connection, one process — the reconciler must run for every sleeve whatever process
is up).

**Tests.** `tests/test_reconcile.py` — 21: partial→complete grows one position and re-sizes one GTT;
duplicate and out-of-order reports change nothing; partial→cancel keeps what filled; rejected with
nothing filled leaves no position and no stop (the broker's reason on the line); restart recovery;
a late fill; an order the book does not carry is counted, not guessed; the five issue kinds; an
unreadable broker keeps a finding; resolution; the invariant over four order histories; the
Postgres issue store's one-open-row rule over sqlite.

**Reverse.** Remove the module and the three `on_order_update` extensions; the dry-run branch
still books the rehearsal. Downgrade 0055.

## LV2.2 — Every sleeve's buy refuses while its protection is unresolved (28 Sep 2026) · ⚠ UNREVIEWED

**Context.** P0.1: "Block additional entries when protection is unresolved."

**Choice.** `reconcile.protection_unresolved(store, naked=…)` is asked by TWT's `_buy_at_open`,
VBT's `_place_limit` and swing's `_buy` after `ALREADY_HELD`: a naked position in the sleeve's own
book, or any open `lv_protection_issue` for the sleeve, refuses the entry `PROTECTION_UNRESOLVED`
with every reason named. Stricter than before on all three sleeves, applied under the charter's tie-
break toward the stricter boundary: a book with a stop it cannot account for should not grow.
Exits, re-arms and ratchets are untouched — protection is never withheld.

**Rejected.** Refusing only the affected symbol (an `EXTERNAL_EXIT` or a missing GTT says the book
and the broker disagree, and the disagreement is not known to be one name's). Refusing across
sleeves (one sleeve's naked position is its own; the account-wide answer is LV3's cap).

**Reverse.** Delete the three guard blocks; `protection_unresolved` stays for the lifecycle page.

## LV2.3 — TWT's market buy carries Kite's market protection (28 Sep 2026) · ⚠ UNREVIEWED

**Context.** The LV0 audit: on 23 and 24 Sep 2026 `twt-auto` sent six live MARKET buys and
Zerodha rejected every one — "Market orders without market protection are not allowed via API" —
because `_buy_at_open` never passed `market_protection`; the swing book's buy does. TWT's tested
entry had never reached the exchange, on a ₹25 lakh sleeve with auto-execute armed.

**Choice.** `TWT_MARKET_PROTECTION = -1.0`, Kite's documented automatic band ("-1: automatic
market protection applied by the system"), passed on every TWT MARKET buy through the gateway's
existing `market_protection` argument (sent only with MARKET; journalled). Not a percentage of our
own: the strategy's entry is "the open, at market" and a hand-picked band is a rule the study never
measured. Kite still converts the order to a limit at its band and the exchange's LPP applies.

**Consequence Maulik must know.** With this deployed, the first weekday with a Kite session before
09:15 is TWT's first live fill (`NEEDS-MAULIK.md` LV0).

**Reverse.** Drop the argument; the broker rejects every TWT market buy again.

## LV2.4 — VBT's live sell is booked from the broker's fill, never from the placement (28 Sep 2026) · ⚠ UNREVIEWED

**Context.** P0.1's exit-side defect: `_sell_at_open` recorded a sell fill at the reference price
and reduced or closed the position the moment the broker *accepted* the order.

**Choice.** The live branch now writes an `lv_exit_order` row (`SENT`, the broker's id, the
quantity, the reference), marks the line `SENT` and the position `exit_queued`, and books nothing.
`on_order_update` books each newly filled share at the broker's average, re-sizes the resting GTT
to what is left, cancels it when nothing is (through the gateway — a GTT on sold shares would sell
what is not there), closes the position on the last share with the broker's average as `exit_avg`,
and closes the line with the broker's word on a dead order. The dry-run branch keeps its immediate
booking: a rehearsal has no broker to report a fill.

**Rejected.** A `side` column on `vb_order` (its check constraints — `stop_price < limit_price`,
one order per signal — are a buy's, and `entries_taken` would have counted a sell as an entry).

**Reverse.** Restore the immediate booking in the live branch; `lv_exit_order` stays empty.

## LV3.1 — One risk row for the account, locked by every process that decides (28 Sep 2026) · ⚠ UNREVIEWED

**Context.** Review P0.2: `app/main.py` built `RiskManager(cfg)` with no persistent state; the desk,
the swing monitor and `twt-auto` are separate processes with separate module globals, so the
day-loss cap, the order counter and the per-symbol exposure map were three each. "Simultaneous
Swing and TWT entries can spend the same cash."

**Choice.** `baskfy_execution.risk.RiskStateStore` — a protocol with `lock()`, `load()`, `save()`.
With a store, every decision (`pre_order`, `on_pnl`, `kill`, the new `release` and
`seed_positions`) runs lock → reload → decide → save; `refresh()` re-reads for the advisory
reader (`gtt.kill_switch_reason`), so a kill in one process is seen by the next GTT cancel in
another. The desk's store is `app/core/risk_store.PgRiskStateStore` over `public.risk_ledger`
(one JSON row per user per IST day, the payload the file path always wrote), taking the row
`SELECT … FOR UPDATE` inside a transaction on the desk's connection; `app.main.gateway()` passes it
when `DESK_DB_BACKEND=postgres` and nothing otherwise. `release(symbol, value)` is the reconciler's
call on a dead order's unfilled remainder (LV2); `seed_positions({symbol: notional})` is the
supervisor's call at login with the broker's holdings (LV4), so a manual Kite holding counts toward
the per-symbol and gross caps. The JSON file path and the in-memory default are unchanged.

**What this does not do.** Reservation rows per order (the contract's `risk_reservation`): the
per-symbol map under the row lock *is* the reservation — accepted `pre_order` adds, `release`
subtracts, atomically — and a second table would be a second truth. Sector caps, open stop-risk
and a continuous P&L feed are not built; the ledger holds `day_pnl` for whoever writes it.

**Tests.** `packages/execution/tests/test_risk_store.py` (9): the lock is taken and the row
saved per decision; two managers cannot spend one cap twice; the order counter is account-wide;
release gives headroom back to the other process; a seed counts a holding and never lowers a
reservation; a kill is seen across processes and by `kill_switch_reason`; a restart keeps the day;
the JSON path and the in-memory default unchanged. Desk `tests/test_risk_store.py` (3): the row
over sqlite under lock, two managers, the gateway wiring only on Postgres.

**Reverse.** Drop `store=` from `app.main.gateway()`; the manager is per process again.
