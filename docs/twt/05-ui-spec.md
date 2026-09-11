# 05 — UI spec: the web hub and the desk page

Two surfaces, the same split every sleeve in this repository uses.

* **`apps/web` `/twt`** — read-only, for looking at. Route group `(app)`, beside `/vbt` and
  `/swing`. Every mutation is a 405 except a note and a dismissal (they change no money). Track A.
* **The desk console `/twt`** (`kite-momentum-rebalancer/app/templates/twt.html`) — the operator
  page, where a plan line becomes an order after a click. Track A, running `DRY_RUN=true`.

The desk page is the only place a TWT order can be created (law 2, Track C §4).

---

## §1 `/twt` — the hub (web, read-only)

Three cards down the page, mobile-first, and the copy lives in `copy.ts` beside `/vbt`'s.

### 1.1 The gate

The breadth gauge: `pct_above_dma` against the 40 % line, the word `OPEN` or `SHUT`, the session
it was read on, and the funnel in a disclosure — universe → with a bar → with a 200-DMA → above it.
When the gate is `SHUT` the card says what that means in one sentence: **no new entries; the open
book is managed as always.** A reader must never have to infer that exits keep running.

### 1.2 Today's tight names

The state (`tw_state_daily`) for the last published session, about fifty rows, sorted by
`turnover_avg_20` descending, with:

| column | from |
|---|---|
| symbol, close (exchange price) | `tw_state_daily.close_raw` |
| the three weekly closes and the range % | `week_close_0/1/2`, `week_range_pct` |
| how far above the month-3 low | `month_low_ratio` |
| sessions in state | `sessions_in_state` |
| **entry today?** | a badge, from `tw_signal_daily` — `SIGNAL`, `SCAN_ONLY (turnover)`, or nothing |
| 20-day turnover | `turnover_avg_20`, in ₹ crore |

The `SCAN_ONLY` rows are shown, greyed, with the reason. A screen that hides what it rejected
cannot be audited by the person whose money it is.

**The freshness line says which Chartink this is.** One sentence under the table, always present:
*"Computed point-in-time from the close of <session>. Chartink's own backtest export uses the
week's final close on every day of that week, so it names some stocks this screen does not — see
the method note."* `01` §2 is the link. This is the single most likely support question about this
sleeve and the answer belongs on the page, not in a doc.

### 1.3 The open book

One row per `OPEN` `tw_position`:

| column | meaning |
|---|---|
| symbol, entry date, entry price, quantity | |
| **highest high since entry** | `high_since`, with the date it was set |
| **stop in force** | `gtt_trigger`, and `NAKED` in red when `gtt_id` is null |
| **distance to trigger** | `(last − trigger) / last × 100`, the number `01` §8 says a person must have agreed to in advance |
| unrealised | marked live |
| hold | sessions since entry |
| half size | a badge when `half_size` |

Plus, when `next_trigger` is set for the last session: **"ratchet due tomorrow: ₹X → ₹Y"**. The
web page shows it; only the desk can act on it.

### 1.4 The half-size counter

One line: **"N of 10 first-live entries remaining at half size."** It is `02` §3.6's discipline and
a reader should be able to see it without opening a settings page. When execution is disabled the
line says so instead.

## §2 `/twt` — the desk page (operator)

The shape of `/vbt` and `/swing`, in this order down the page:

1. **The session strip** — date, `mode` (`DRY_RUN` / `LIVE`), gate, counts from `tw_session`
   including **ratchets**, and the flag states. `DRY_RUN` is a badge, not a footnote.
2. **Exits first.** `ARM_GTT` lines, then `RAISE_GTT_STOP` lines, each with the old trigger, the
   new trigger, `high_since` and the distance to the last price. Exits are first for the reason the
   swing desk puts them first: a morning that runs out of attention should have armed the stops.
3. **Entries.** `BUY_AT_OPEN` lines with quantity, value, the 20 % stop the fill will be given, the
   cap that bound, and the rank key. Every skip below them, with its reason in words.
4. **The book**, as §1.3, plus a **Re-arm** button per naked line (the swing book's `rearm_gtt`
   path, one position, nothing else).
5. **Confirm**, per line. `confirm=true`, the `plan_id`, the `line_id`. A plan older than thirty
   minutes shows an expired banner and the buttons are gone — not disabled, gone.

**The 15:15 strip.** From `gtt_sweep_at` the page carries a red band naming every open line without
a resting GTT, and it stays until the sweep is clean. `TWT_GTT_MISSING_AT_1515` is the alert.

## §3 The backtest card (`/twt/backtest`, web)

`02` §3.3 makes this a condition of the flag, so it is a page, not a table in a doc.

* The latest **finished** `tw_backtest_run` per `source`, side by side: `PLANT` and
  `RESEARCH_EXPORT`. Never mixed, never averaged.
* CAGR, max drawdown, Calmar, Sharpe, trades, win rate, profit factor, average hold, exposure, and
  the in-sample / out-of-sample split.
* The yearly table and the equity curve.
* **The gate-on / gate-off comparison** — the one number that justifies the gate.
* **The drift flag**: `abs(cagr_pct_delta) > 1.0` against `01` §6 renders a warning naming both
  numbers. A silent drift is the failure mode this card exists to prevent.
* **`01` §8's caveats, verbatim, as a component** — not a footer (house rule 9's spirit: a
  disclaimer is a component). Specifically: 164 trades, ten of them half the profit, about fifteen
  independent observations, the 20 % give-back as routine, and **no backtest in this repository was
  produced at ₹25 lakh**.

## §4 What the pages must never do

* No order route in `apps/web` under `/twt` (Track C §4), asserted by the read-only test.
* No form that writes a `04` threshold. The bounded settings of `02` are the only editable
  numbers, and `trail_pct` is bounded **below**.
* No page claims the sleeve has traded while `simulated` is true on its fills; simulated rows are
  labelled everywhere and are summarised separately.
* No bare dash. A metric the sleeve cannot compute is labelled with its reason — the house rule the
  portfolio work established and this pack inherits.
