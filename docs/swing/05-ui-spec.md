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
  (GREEN/AMBER/RED with the two breadth numbers that made it), the tier ("Rung 2 of 4 · up to 6
  positions · 75% of sleeve").
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
the last close, added on, expires on, source (detector/manual), note and catalyst (inline
editable), state history. **Add manual** form: symbol search (existing `/search`), setup, trigger,
stop reference. Yesterday's `sw_signal` rows for these names are shown as "fired 09:23, 5-min
range 412.30–418.90" under the row.

### `/swing/market`

`sw_market_daily` history: the three breadth series and the gate as a colour band over time
(dataviz conventions of `market/mood`), the ladder rung over time, the parabolic count, and the
index with its 10/20 MAs. A **"what would change the gate"** line: "GREEN needs ≥ 5.0% of names up
25% in a month; today 3.8%".

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

**Triggers (top).** `sw_signal` rows for today, newest first: time, symbol, setup, "5-min ORH
418.90 broken at 419.35", entry, stop, the sized line (qty, ₹ risk, % of sleeve, cap), the
`plan_id` and its countdown, and a **Confirm** button per line. Confirm POSTs
`/swing/execute {plan_id, line_id, confirm=true}`. The response renders inline: `SIMULATED` (DRY_RUN
or flag off), `SENT`, `FILLED`, `BLOCKED (reason)`. A locked-circuit signal shows without a
button. Lines a second person could confuse for the weekly book are prefixed **SWING**.

**Plan (middle).** The morning plan (`source=MORNING`, built 09:10) and the EOD preview: exit
lines first (SELL at open, RAISE GTT), then the buy-on-trigger lines that are *waiting* for a
signal, then the skips with reasons. **Confirm** on a `SELL_AT_OPEN` or `RAISE_GTT_STOP` line
goes through the same endpoint. There is no "confirm all".

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
