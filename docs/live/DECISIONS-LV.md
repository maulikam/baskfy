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

**Amended while building (28 Sep 2026).** The swing book's guard reads the reconciler's recorded
findings only, not its own naked rows: `test_swing_desk`'s route fixtures carry a deliberately
naked position (the hub leads with it; `rearm_gtt` and the 15:15 sweep exist for it) and the
book's spec has a confirm proceed beside one — SW7 — so passing the raw rows would have changed
a swing rule this pack has no mandate to change. The block still follows: the reconciler records
a NAKED issue for that row on its next pass (ten seconds, in session) and the guard reads it. TWT
and VBT pass their naked rows directly; their specs never allowed a buy beside one.

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

## LV4.1 — Login is the event: a session supervisor beside the desk (28 Sep 2026) · ⚠ UNREVIEWED

**Context.** Review gap 10 and the LV0 audit: everything but the swing scan assumed a token at its
cron minute; eight of fifteen mornings had none at 09:05, and the logins of 22 and 25 Sep came at
10:40 and 11:58.

**Choice.** A sixteenth compose service, `session-supervisor` (`app/session_supervisor.py`,
`scripts/session_supervisor_loop.py`, the desk's image), awake 08:30–15:50 IST on weekdays, ticking
every ten seconds. A token blob whose mtime moved **and** authenticates is a login: the account's
holdings are seeded into the shared risk ledger (LV3) and the reconciler runs once (LV2, restart
recovery). In session it runs the reconciler every tick and writes `lv_heartbeat` rows
(`supervisor`, `reconciler`) that `/sleeves/state` reads. It builds no plan and starts no other
process; every write it causes goes through the sleeves' handlers and the gateway. *(Amended by
LV8.1: since 28 Sep 2026 it also calls `twt_auto.drain_now` once a minute in session, so "places
nothing" is no longer literally true — the drain sends TWT's LIVE plan through `execute_line`
under the same three flags as `twt-auto`.)*

**Rejected.** Folding the reconciler into `twt-auto` (a 09:15 clock, not a session) or the swing
monitor (one sleeve's process; the reconciler must run for all three whatever is up). Kite
postbacks (no public URL on the desk). Reviving an expired TWT plan on a late login — P1.3 says
not to, and the module does not.

**Reverse.** Remove the service from compose and the deploy lists; the reconciler is then
`python -m app.reconcile` by hand.

## LV4.2 — The swing monitor reloads its watchlist and waits for a late login (28 Sep 2026) · ⚠ UNREVIEWED

**Context.** Review P1.2: `swing_monitor.main` loaded the watchlist once and `run_until_close`
subscribed once, so a "Scan now" at 11:00 added a database watch the running process never saw;
with no token at 09:14 the process failed for the day; an empty list at start never entered the
loop.

**Choice.** `run_until_close(..., reload=, reload_every_seconds=60, subscribe=, unsubscribe=,
heartbeat=)`: every minute the list is re-read (`load_watchlist` over the same connection, circuit
bands from one quote pass), a gained name is `SwingBreakout.add_watch`ed, subscribed on the bus and
on the KiteTicker, and put in the quote fallback; a lost name is dropped and unsubscribed. A name
added mid-session builds its range from the ticks it sees from then — a trigger before it was
watched is not back-filled, by design. `main()` waits for a Kite session until **15:20**
(`wait_for_session`, 30-second polls) instead of exiting, enters the loop on an empty list (writing
`monitor_ran` at once so the 09:20 alert stays quiet), and writes the `swing_monitor` heartbeat
every fifteen seconds. Trigger and order de-duplication are untouched: the store's session lock
and `client_id = plan_id:symbol` stand.

**Rejected.** A restart of the container on each scan (drops the ranges being built — the
deploy scripts already refuse to recreate the monitor in session for that reason). A pub/sub
channel (a minute's poll of one small table is the same freshness with nothing new to run).

**Reverse.** Pass no `reload`; `main()`'s wait is a constant (`SESSION_WAIT_UNTIL`).

## LV4.3 — `/sleeves/state`: one deterministic state and reason per sleeve (28 Sep 2026) · ⚠ UNREVIEWED

**Context.** Review P1.3: a 10:30 login must produce an explicit outcome per sleeve, visible beside
the buttons, and an EOD detector must never be labelled an intraday strategy.

**Choice.** `baskfy_api.sleeve_state.derive_state` — pure — over stored facts (calendar, token,
newest scan run, newest plan, heartbeats, open protection issues, watch count): `blocked` ›
`closed` › `waiting_for_login` › `scanning` › `monitoring` (swing, fresh heartbeat) › `plan_ready` ›
`missed_window` (TWT after 09:35 without a live MORNING plan; VBT with an expired plan) ›
`signal_ready` › `idle`, each with a reason and a next step, and `scan_means` saying what that
sleeve's Scan button does ("re-detects the last published session" for TWT and VBT; "today so far"
for swing). `GET /sleeves/state` is read-only; the web chip (`SleeveStateBadge`) polls it every
30 s beside the three Scan buttons. The login callback now also queues the TWT and VBT scans of
the last published session through the pages' own `request_scan` (their one-a-minute and
in-flight rules apply; a refusal is a note, never a failed login). Sources recorded: `web` for
TWT, `desk` for VBT — the tables' check constraints admit no `login`, and widening them is a
migration this pack did not need.

**Rejected.** Computing state in the browser from three page payloads (three truths). Reviving
plans or triggering entries from the API (D9; the API has no execute route).

**Reverse.** Drop the router, the service, the chip and the two `request_scan` calls in
`_queue_post_login_refresh`.

## LV5.1 — Intraday equity bars: captured after the close and backfilled, not collected live (28 Sep 2026) · ⚠ UNREVIEWED

**Context.** Review gap 1: no intraday bar store for equities, so no live TWT/VBT variant can be
defined or backtested. Step 4 asks for the capture "so live TWT/VBT variants can be *defined and
backtested* rather than guessed."

**Choice.** `eq_minute_bar` (migration 0056; a TimescaleDB hypertable on `ts`, monthly chunks;
raw prints, `numeric(18,2)` rounded at write, volume from Kite's candle) written two ways, both
from `historical_data(interval="minute")`: the **session reconcile** — Beat `baskfy.eq_bars.session`
at 15:45 Mon–Fri, one call per name for the whole session, committed every 25 names — and the
**backfill** — `python -m baskfy_worker.eq_bars_cli backfill --from … --to …`, per name in Kite's
60-day windows, resumable from each name's newest stored bar, committed per window. The universe
is the swing book's `liquid_universe` as of the last published session — one predicate, never a
second. Measured on the box (`sw_scan_run` 66 and 67, 25 Sep 2026): the **liquid universe is 573 names** quoted live (554 liquid, 682 on the published re-detect) — so a session is ~570 calls,
about three minutes on the bulk lane, and a year's backfill ~3,500 calls, about twenty minutes.
Behind `BASKFY_EQ_BARS_ENABLED`, default **on**: read-only market data, and the data is the point.
Pure readers in `baskfy_core.eq_bars` (opening range over closed minutes, session volume) reuse
`baskfy_core.options.bars` for windows and five-minute bars — one definition of a 5-minute bar.

**Not built, and why.** A live tick collector (`source = 'TICKS'` is reserved). Backtesting a
variant needs history, which the backfill gives; running one live needs a stream, which is the
variant's own module (step 5). One KiteTicker connection is the swing monitor's; a second for ~570
names is well inside Kite's three, when a variant asks for it.

**Rejected.** Aggregating the swing monitor's ticks into bars (it watches five names, not 570).
Reading minute bars in the nightly chain (18:45 is the wrong hour for a 3-minute Kite pass that
competes with the 7,000-call bhavcopy-only night).

**Reverse.** Drop the Beat entry and the route; downgrade 0056. The setting turns it off first.

## LV6.1 — One lifecycle per trade, judged at the broker; adoption is explicit (28 Sep 2026) · ⚠ UNREVIEWED

**Context.** Review P1.4: "one visible lifecycle per trade … reconcile actual GTT status, not
merely an ID in the database … an explicit workflow to adopt a manually bought Kite holding …
instead of silently claiming ownership"; P2.3: "show entry, quantity, rupee risk, initial/current
stop and the exact profit-exit rule on each trade card."

**Choice.** The desk's `/lifecycle` (`app/lifecycle.py`): one row per open position across the
three books — filled and open quantity, entry, initial and current stop, rupees between entry and
stop, the **broker's** word on the stop from the live GTT list (`ARMED`, `NAKED`, `GTT_MISSING`,
`GTT_OVERSIZED`, `GTT_UNDERSIZED`, `TRIGGERED_UNFILLED`; `UNVERIFIED` when there is no Kite session,
said in those words rather than guessed), the next action, an `overdue` flag once a finding has stood
thirty minutes, and every open `lv_protection_issue` the reconciler recorded. A triggered GTT is
not a fill and the row stays unresolved. `POST /lifecycle/adopt` (`confirm=true`) puts a
hand-bought holding into a named sleeve on the person's cost and quantity, links the GTT he armed
or arms one through that sleeve's own helper and the gateway at the sleeve's stop distance, writes
`lv_adoption`, and refuses a held name, an unknown symbol, a missing confirm; a swing adoption
names its setup (the schema's constraint). The exact exit rules live once, in `app/exit_rules.py`,
built from the three configs, and the same sentence is printed under each sleeve page's book, with
"initial stop" and "₹ at risk" columns beside the existing entry and stop.

**Rejected.** A per-sleeve lifecycle (the account holds one book). Auto-adopting unknown holdings
(the review's "silently claiming ownership"). Any exit or repair action from the page — the
review: "any wider automatic exit/repair policy must be recorded as a strategy-policy change; this
review does not enable it."

**Tests.** `tests/test_lifecycle.py`: the rows, the seven stop verdicts, ordering and overdue, the
page and its JSON, adoption into each sleeve (position, fill, stop armed once, `lv_adoption` row),
linking a hand-armed GTT, the refusals, the card's rule text from config, the page's one write and
the absence of any order verb in the module.

**Reverse.** Drop the router include and the nav entry; the sleeve pages keep the two columns.

## LV8.0 — Maulik's answers, 28 Sep 2026 (in session, ~01:35 IST): live scans for TWT and VBT, entries now, minute bars off · ✅ decided by Maulik

Asked with options after he said: *"Equities we are going to trade would be a swing trade, not an
intraday trade. I think I don't want to backtest since it is already working on the daily chart. The
strategy would be the same live. What we consider is we'll collect the live data and directly start
trading on it."*

1. **TWT and VBT Scan → "Live bar, like swing".** Build today's provisional bar from Kite quotes, run
   the same detector over today-so-far, and propose entries. Same rules, live inputs. The option he
   chose said plainly that the tested entry timing changes (TWT tested "next open", VBT "limit at
   the signal close") and that he accepts that without a backtest. This reverses DECISIONS-TW
   TW12.2 and VB12's "closed session only" for the two Scan buttons — **his reversal, not an
   agent's**.
2. **Entry timing → "Now, at market with protection".** A MARKET buy at the live price the moment
   he confirms — or auto-execute confirms, for TWT — with Kite market protection and the GTT stop
   the same session. The option text: *"This is 'directly start trading on it'."* For TWT this
   widens the second named exception to non-negotiable 1 (TW17: the 09:05 MORNING plan only) to a
   plan built from a live scan at any hour of the session; recorded as **DECISIONS-TW TW19** by
   Maulik's choice of that option. VBT keeps **no** auto-execute flag: a live VBT signal is a plan
   line he confirms by hand.
3. **LV5 → "Off on the box, code stays".** `BASKFY_EQ_BARS_ENABLED=false` in the box env; the
   empty `eq_minute_bar`, the job and the CLI remain. No backfill.

Consequence for `NEEDS-MAULIK.md` LV7: questions 1 and 2 are answered (variant = the same detector
over a live bar, entering now; no backtest wanted); 3, 4 and 5 stand.

## LV8.1 — How the live scan was built: one decision function, one detector, one plan path per sleeve; no-session falls back rather than fails (28 Sep 2026) · ⚠ UNREVIEWED

**Context.** LV8.0 is Maulik's; this entry is the agent's construction choices under it.

**Choices.**

1. **The session decision is the swing book's `decide_session`, imported, not copied.** One rule
   for when "today" exists across three sleeves; the reasons it answers (`REASON_MARKET_OPEN`,
   `REASON_AFTER_CLOSE`, `REASON_PUBLISHED`, `REASON_ALREADY_PUBLISHED`) land in the scan row's
   detail.
2. **The provisional bar is built in each sleeve's own `BAR_SCHEMA`** (`live_scan.provisional_daily_bars`),
   so `run_detect_twt` / `run_detect_vbt` take it through one `pl.concat` and nothing in the
   detectors changes but a flag. Rejected: a shared bar frame re-projected per sleeve — one more
   place for a column to drift.
3. **No Kite session during the market → the published session, with a note** (`LIVE_SKIPPED_NO_QUOTES`
   in the row's detail), **not `FAILED`** as the swing scan does. The login callback queues these
   scans the moment a session arrives, so the case is "pressed before logging in", and a
   re-detect of the published day is still the honest answer to that press. Rejected: raising, as
   swing does — consistent, but a red row for "log in first" on a page that also carries the
   sleeve-state chip saying `waiting_for_login` would be two voices for one fact. Cheap to align
   later if he prefers the swing behaviour.
4. **The LIVE plan carries entries only.** Exits (`RAISE_GTT_STOP`, VBT's EMA sell) read a closed
   bar; planning them off a half day would move stops on a bar that has not closed. The TWT
   ratchet is skipped on a provisional run for the same reason.
5. **TWT's live entry reuses `BUY_AT_OPEN`** (the desk's confirm is already a MARKET buy with
   protection, LV2.3); **VBT's is a new kind, `BUY_AT_MARKET`**, because VBT's existing entry is a
   LIMIT and reusing it would have silently changed what a click sends. The desk's handler mirrors
   TWT's, including `market_protection=-1`.
6. **The supervisor drains TWT once a minute** (`DRAIN_EVERY_SECONDS=60`) rather than every
   ten-second tick: the plan lives thirty minutes, and a `todays_plan` query per tick per process
   buys nothing. The `twt-auto` compose loop is unchanged and still drains at 09:15:10; both go
   through `execute_line` under the session lock with `client_id` idempotency, so an overlap
   cannot double-send (asserted by the existing `test_re_running_does_not_double_send`).
7. **Tests pinning the reversed decision were rewritten to pin the new one** and cite LV8.0 —
   `test_twt_scan_run_model`, `test_api_twt_scan`, `test_sleeve_state`, `test_twt_schema`,
   `test_vbt_execute`'s kinds census. "Never weaken a test" is about passing a module; these
   asserted a decision the owner reversed.

8. **A re-scan keeps a LIVE plan with a line at the broker** (`IN_FLIGHT_LINE_STATES`:
   CONFIRMED / SENT / FILLED) instead of rebuilding it. `store_plan` replaces the day's plan of a
   source wholesale — the MORNING rebuild relies on that — and a rebuild thirty seconds after a
   market buy went out would delete the line the order came from and offer the same name again
   under a new `client_id`. Found in the self-review, not by a test that existed; now
   `test_a_rescan_keeps_a_live_plan_whose_line_is_at_the_broker`.
9. **TWT's live plan drops names with an order today** (`ORDERED_TODAY_STATES`), because TWT's
   `BookState` knows positions only (``03`` §6 has no working orders in this sleeve) and a market
   buy sent thirty seconds ago is not yet a position. VBT's `working_instrument_ids` already covers
   it. **And the nightly's sweep keeps a provisional `tw_signal_daily` row that a `tw_order`
   references** (`fk_tw_order_signal`, no cascade): deleting it would have failed the nightly's
   TWT step on the first evening after a live buy the closing bar did not confirm. The row stays,
   marked provisional — the honest record of what the order was taken on.

**Not built.** A live index level for the TWT/VBT gate (the swing scan's `_live_index_level`) —
both gates read breadth, which the provisional bars already move; the index rule is swing's.
Pyramiding, targets and the first-live-morning handling stay in `NEEDS-MAULIK.md` LV7 Q3–Q5.

**How to reverse.** TW19 and VB16 carry the one-line reversals; migration 0057 downgrades clean.

## LV9.0 — Maulik's answers, 28 Sep 2026 (in session, ~04:00 IST): Qullamaggie's exits on TWT and VBT, his pyramiding on all three, TWT's first live entries run · ✅ decided by Maulik

Asked with options (`NEEDS-MAULIK.md` LV7 Q3–Q5, then one round of specifics):

1. **Q5, first live TWT entries → "Let it run".** Auto-execute stays on; the first-ten half size,
   the three-a-session cap, the same-session GTT, the reconciler and `/lifecycle` are the rails.
   Nothing changes on the box for this.
2. **Q4, targets → "based on Kristjan Kullamägi's style"**, then **"TWT and VBT, replacing their
   tested exits"**: the swing book's exit rule (`StopConfig`: 1/3 sold into strength between bar 3
   and bar 5 after entry if the position is green, the stop to breakeven after the partial or at
   +1R, the remainder trailing the 10-day MA for names with ADR ≥ 6 % or the 20-day MA otherwise,
   sold at the next open on a close below it; the GTT is the exit) **replaces** TWT's 20 %
   high-water trail and VBT's close-below-21-EMA. His reversal of the two research findings
   (TWT `04` §7, VBT `04` §8 measured fixed targets and rejected them); recorded as his, not an
   agent's. **And "Auto-execute sends it too"**: TWT's `twt-auto` and the supervisor confirm the
   partial `SELL_AT_OPEN` lines under the same three flags — a widening of non-negotiable 1's
   second exception from stops-and-buys to sells as well, **his** (DECISIONS-TW TW20). VBT's
   partial stays a click.
3. **Q3, pyramiding → "lets we do what Kristjan Kullamägi doing"**, then **"On for all three
   sleeves"**: a fresh qualifying setup in a name already held is a **new entry** with its own
   size and its own stop, counted against slots and exposure; no add without a fresh signal; a
   name may be re-entered after an exit on a new signal; at most two open entries per name. Ships
   as the rule with a per-sleeve config switch to turn it off.

**Timing.** It is 04:00 IST on a trading day. These change live exits and entries; they are built
and tested now and **deployed after today's close (after 15:30, before 18:40)**, not into the
morning's session. Today's session runs on dde3c1e as deployed: LV8's live scans, the tested exits.

Leaves: **LV9** (exits) and **LV10** (pyramiding), each with its gates file; DECISIONS-TW TW20 /
TW21 and DECISIONS-VB VB17 / VB18 carry the sleeve-level records.

## LV9.1 — How the exits were built: one rule module, two adapters, decisions persisted where the plan is built from state (28 Sep 2026) · ⚠ UNREVIEWED

1. **One implementation.** `baskfy_core.exits.qulla` wraps `swing.stops.manage`; TWT and VBT never
   re-state the partial, breakeven or trail rule (`test_qulla_exits` asserts the adapter carries no
   rule of its own). The numbers are the swing book's `StopConfig`, held on each sleeve's
   `ExitConfig.qulla` so a sleeve could diverge later without touching the swing book.
2. **TWT persists the evening's decision on the position** (`partial_queued_for` /
   `partial_quantity`, `exit_queued_for` / `exit_reason_queued`, `next_trigger` for the raise,
   `trail`) because its plan is built from the book's state and the MORNING rebuild re-reads it;
   **VBT re-derives the actions each evening** from the bars, as its evening already did for the
   EMA exit, and persists only `trail` and, after the fill, `partial_done`. Two shapes, each the
   sleeve's own; the rule is still one.
3. **A decision is dated the session just closed** and a plan reads it only for that session — a
   queued sale from an evening whose morning nobody confirmed is not re-sold by accident.
4. **The hard stop stays the reconciler's business.** The rule's ``STOPPED_OUT`` is counted and
   left alone: the GTT is the exchange's copy of it, and LV2's reconciler says whether it filled.
5. **VBT's breakeven is a new kind** (`RAISE_GTT_STOP`, `_raise_gtt_stop` mirroring TWT's) rather
   than a re-arm through `ARM_GTT`, whose meaning ("a naked position gets its stop back") the
   desk's tests pin.
6. **Broker fills are never simulated** whatever the desk's `DRY_RUN`: `_apply_exit_fill` takes
   `simulated` explicitly — the rehearsal passes true, `_apply_exit_update` false.
7. **TWT's session counters key on the plan's session** for a confirmed sale (as its buys do) and
   on the fill's day for a broker report (as VBT's do).
8. **The ratchet tests keep their subject.** `test_twt_detect`'s five ratchet tests run the
   detector with `qulla_exits=False`: the 20 % ratchet is still code (the reversal is one line)
   and its tests still prove it; a new test pins that the default runs the rule instead.

**Not built.** The 04 §7.3 corporate-action branch is skipped in qulla mode (a split still
leaves the stop where it was; the alert path is untouched). No live index level, no re-test.

## LV10.1 — How pyramiding was built: counts beside the held set, the cap in one place per sleeve, lines that name their position (28 Sep 2026) · ⚠ UNREVIEWED

1. **Counts beside the set, not instead of it.** Each planner's book keeps `open_instrument_ids`
   / `open_symbols` and gains `open_entry_counts`; a builder that hands no counts means one per
   name, so every caller and test that predates LV10 still reads correctly. `slots_taken` counts
   entries when counts are given.
2. **`ALREADY_HELD` keeps its name.** The skip-reason vocabularies are pinned to documents and
   constrained in the schema; the cap is the same refusal with a detail that says the count.
3. **No schema change.** Every plan-line table already had `position_id`; LV9 put it on the core
   line types and the writers, LV10 makes the exit and raise builders fill it and the desk read
   it first (`_position_for_line`, falling back to the name's open position).
4. **The desk's guard is one helper per sleeve** (`_held_refusal` over `open_positions()`),
   reading the shipped sizing config; a store without `open_positions` degrades to the old
   one-per-name check.
5. **What still refuses a second entry:** VBT's `ALREADY_WORKING` (a limit already resting in
   the name), every session cap, the slot ceiling, the per-line size cap, exposure.

**Not built.** A per-name exposure cap beyond the count (two full slots in one name is what he
chose); adds at a fixed +R without a fresh signal (not his method).
