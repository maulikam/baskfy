# 05 — UI spec: the web hub (read-only) and the desk page (confirm)

Two faces, by design (`docs/00` — "Two faces remain"). The web app shows and annotates; the desk
console confirms. Language follows `apps/web/src/lib/vocabulary.ts`: plain words, no jargon the
`baskfynavrefactorreport` retired. Disclaimers are components, not footers (house rule 9).

## 1. Navigation

The **Build** hub gains a section tab **Swing** (`/build/swing` redirects to `/swing`); the
Command palette (`⌘K`) learns "swing", "flags", "setups", "watchlist". Primary chrome stays five
destinations (HOME1). `lib/nav.ts` gets the entries with `status: "ready"` as each page lands;
until then `"planned"` with `arrivesIn: "SW5"` etc., the same convention the portfolio redesign
used.

## 2. `/swing` — the hub (Next.js, `(app)/swing/`)

Section tabs: **Setups | Watchlist | Market | Positions | Journal**.

### `/swing` (Setups)

* Header: as-of date (from `/meta/status`, with the freshness pill), the gate badge
  (GREEN/AMBER/RED with the two breadth numbers that made it, and the index rule as a word:
  "10-day above 20-day" / "10-day below 20-day" / "no index" — SW9.5, `04` §8.2), the tier
  ("Rung 2 of 4 · up to 6 positions · 75% of sleeve"; when `sw_market_daily.drawdown_locked`,
  "Locked out · sleeve 15.3% below its peak · resumes inside 10%" in place of the rung).
* A **sector strip**: the top-5 sectors by `pct_above_20dma` from `market_health_daily`, with
  the count of today's candidates in each.
* Three lists, one per setup, each a table: symbol, name, sector, status pill, score, close,
  trigger, stop reference, stop distance %, ADR %, turnover (₹ cr), base bars / gap % / streak as
  applicable, `locked` warning icon, listed-within-2y icon; a 130-bar mini chart per row (SVG,
  close line + MA10/MA20 + pivot line + stop line). Sorted by score, filter chips per status.
* Row action: **Watch** (adds to `sw_watch` — the one mutation) and **Dismiss**.
* The parabolic list is headed **"Parabolic — for the record. Not tradeable on NSE delivery."**
* Empty states say why: "No flags today — 41 names were liquid, 0 met the base rules" (the
  detector's funnel counts, from `sw_setup_daily` plus the job's `detail`).

### `/swing/watchlist`

`sw_watch` rows in `WATCHING` state: symbol, setup, trigger, stop ref, distance to trigger from
the last close, **stop distance as a share of the ADR** (a name whose stop is wider than one ADR
is shown as one the plan will skip — `04` §6.1, SW9.5), added on, expires on, source
(detector/manual), note and catalyst (inline editable), state history. **The catalyst cell**
(SW11B, STANDING-ANSWERS A3): the typed text — or, while it was empty, the newest NSE headline
the 09:10 feed auto-filled — plus the feed's link (`catalyst_feed.url`, rendered
`target=_blank rel=noopener`, opening the exchange's own copy; never the filing's text) and an
**earnings badge** from `earnings_date` when the event calendar names a result meeting. The
same link and badge appear in a Catalyst column on the Setups tab and on the desk page's
triggers and plan lines. **The funnel numbers**
(`07`, SW9.5; code since SW10.5 — STANDING-ANSWERS A14): the evening auto-watches the top
**20** `SETTING_UP` flags by score plus **every** EP (his weekly focus list of 5–20) and the
monitor watches all of them; the **daily focus** is the top **5** by score plus every EP —
`sw_watch.focus`, a stored flag the read model carries (`SwingWatchOut.focus`, with `score`
and `adr_pct`), which the notifier (SW11) pushes and the desk page puts on top; the page shows
the count against each and marks focus rows. A **MANUAL** row expires after 10 sessions unless
**re-confirmed** — a `Still watching` control on the row posts `PATCH /swing/watch/{id}` with
`{"reconfirm": true}` (a non-money write; `reconfirmed_on` and the new `expires_on` are shown).
**Add manual** form: symbol search (existing `/search`), setup, trigger,
stop reference. Yesterday's `sw_signal` rows for these names are shown as "fired 09:23, 5-min
range 412.30–418.90" under the row.

### `/swing/market`

`sw_market_daily` history: the three breadth series and the gate as a colour band over time
(dataviz conventions of `market/mood`), the ladder rung over time, the sleeve's drawdown from
its peak with the lock-out shaded (SW9.5, `04` §8.5), the parabolic count, and the index with
its 10/20 MAs. A **"what would change the gate"** line: "GREEN needs ≥ 5.0% of names up 25% in
a month and the 10-day above the 20-day; today 3.8%, 10-day above".

### `/swing/positions`

Open positions from `sw_position`: symbol, setup, entry date, entry avg, qty open, stop in
force, GTT status (armed / **naked** in red), trail MA and today's distance to it, R showing,
partial done, days held; the next morning's proposed exit lines from the EOD plan preview.
Closed positions below with R and reason. Every row labelled **Simulated** while
`sw_position.simulated` is true.

### `/swing/journal`

`journal.summarize` over closed positions — real and simulated in **two separate cards**; the
R-distribution histogram; by setup; by month; the current loss streak and what it means for the
ladder; the **backtest card** (SW9) under its own heading with its caveats verbatim from `04` §11.
`sw_session` count against the 20-session gate (`02` §3.2): "14 of 20 paper sessions logged".

### `/swing/settings` (inside `/me`, not a hub tab)

The `sw_config` form: sleeve capital, risk per trade (bounded by the system ceiling, which is
shown as "max 1.0% — set by the server"), max position %, max positions, opening-range window,
stop mode, liquidity floors. Saving writes `settings_audit`. The exposure rung is displayed,
not editable. The execution flag is displayed as "Execution: disabled on this server" and is not
a control.

**Read-only assertion:** `apps/web/src/app/(app)/swing/__tests__/read-only.test.tsx` asserts
that the only server actions under `/swing` are `watchAdd`, `watchDismiss`, `watchAnnotate`,
`settingsSave`, and that none imports anything from `baskfy_execution` or calls
`/desk/*` / `/swing/execute`.

## 3. The desk console `/swing` (Jinja, `kite-momentum-rebalancer/app`)

One page, three panels, refreshed every 5 s during 09:15–10:45 and on demand otherwise.

**Triggers (top).** `sw_signal` rows for today, **focus names first** (A14 — `sw_watch.focus`),
newest first within each group: time, symbol, setup, "5-min ORH
418.90 broken at 419.35", entry, stop, the sized line (qty, ₹ risk, % of sleeve, cap), the
`plan_id` and its countdown, and a **Confirm** button per line. Confirm POSTs
`/swing/execute {plan_id, line_id, confirm=true}`. The response renders inline: `SIMULATED` (DRY_RUN
or flag off), `SENT`, `FILLED`, `BLOCKED (reason)`. A locked-circuit signal shows without a
button. Lines a second person could confuse for the weekly book are prefixed **SWING**.

**Plan (middle).** The morning plan (`source=MORNING`, built 09:10) and the EOD preview: exit
lines first (SELL at open, RAISE GTT), then the buy-on-trigger lines that are *waiting* for a
signal (at most three a session — `SESSION_CAP` skips say so), then the skips with reasons; a
`DRAWDOWN_LOCKOUT` day is headed with the sleeve's drawdown. **Confirm** on a `SELL_AT_OPEN` or `RAISE_GTT_STOP` line
goes through the same endpoint. There is no "confirm all". Since SW10.5: a live gap found at
09:09 is a **`PENDING_RANGE` row** among the waiting buys (STANDING-ANSWERS A7) — "SWING PENDING
EPSILONGAP — range at 92.00, no stop yet", its preview note ("≈ N shares if the stop lands 1 ADR
below"), **no button ever**, and the route refuses it with a 400 regardless; the SIGNAL plan at
window close is the line. While `sw_config.first_live_sessions_left > 0` the panel is headed
**"first live sessions: N left · risk 0.250%"** (A9; the risk in force is halved only when a
confirm would be real — a SIMULATED desk shows the full 0.500%). While a live buy is accepted
and not yet complete the panel shows the resting lines with two more one-form controls
(A8): **Reconcile fills** (`POST /swing/reconcile`, `confirm=true`), which reads the broker's
order book for today's `SENT` buys and applies each through the postback handler, and
**10:45 sweep** (`POST /swing/cutoff`), which cancels open remainders through the gateway and
frees unclaimed pending-range slots. Both are websec-covered form posts; neither places a buy.

**Book (bottom).** Open positions with GTT ids; a **Re-arm GTT** button for a naked position
(the only other order-shaped action, and it is a GTT, not a buy); the last five `manage`
actions.

Status bar: `DRY_RUN`, `BASKFY_SWING_EXECUTION_ENABLED`, monitor state (idle / running since
09:15 / stopped at 10:45 / **not enabled**), Kite token age, the session's counters written to
`sw_session`.

The desk's existing login, CSRF and `websec` middleware cover the page; `DRY_RUN` is shown in
the header of every desk page already and stays so.

## 4. Alerts

`swing-eod` sends one email through the existing alert path (`dispatch-screen-alerts`
machinery, a new `AlertName.SWING_EOD`): the gate and rung, up to 10 flags and all EPs with
levels, tomorrow's exit lines, positions with a naked GTT (**always**, separately, in red), and
the count of paper sessions. `SWING_POSITION_NAKED` is also a Prometheus alert rule in runbook 6
(M20 pattern) because a position without a stop is the one state the method forbids.
