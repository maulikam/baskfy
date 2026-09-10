# 05 — UI spec: the web hub (read-only) and the desk page (confirm)

Two faces, by design (`docs/00` — "Two faces remain"). The web app shows and annotates; the desk
console confirms. Language follows `apps/web/src/lib/vocabulary.ts`: plain words. **Disclaimers
are components, not footers** (house rule 9) — which for this sleeve is the load-bearing rule,
because every number on it comes from one favourable history.

## 1. Navigation

`lib/nav.ts` gains a **Volume breakout** entry under the same hub the swing book sits in, with
`status: "planned"` and `arrivesIn: "VB8"` until the page lands, then `"ready"`. The command
palette (`⌘K`) learns "volume breakout", "vbt", "breadth gate". Primary chrome stays five
destinations (HOME1).

## 2. `/vbt` — the hub (Next.js, `(app)/vbt/`)

Section tabs: **Today | Book | Backtest**.

### `/vbt` (Today)

* **Header**: the as-of session (the last completed one — `04` §10 — with the freshness pill),
  and the **gate badge**: `OPEN` or `SHUT` with the number that decided it, in words —
  "62.4% of 1,412 names are above their 200-day average · the gate opens above 40%". When
  `SHUT`, one line saying what the book does anyway: "Positions are managed as usual; no new
  limits are placed."
* **The breadth gauge**: the `vb_breadth_daily` series for the last year as a band chart with the
  40% line drawn, dataviz conventions of `market/mood`. Below it, the count of sessions the gate
  has been shut in the last 60.
* **Today's candidates** — `vb_signal_daily` rows with `state = SIGNAL`, ranked by `rank_key`:
  symbol, name, close, **the limit** (the signal close), the 12% stop, change %, relative volume,
  close position, 20-day return, 20-day turnover (₹ cr), the distance from the 200-DMA, a
  `locked` warning icon, and a 130-bar mini chart (SVG: close line, 200-DMA, 21-EMA, the prior
  20-day high as a level, the limit and the stop as lines).
* **What the scan rejected** — a collapsed section listing `state = SCAN_ONLY` rows with the
  letters that failed ("AHCL — B, F"). It exists because `01` §3's ablation table is the argument
  for the filters, and a page that never shows the rejects makes that argument unreadable.
* **The funnel line**, always, even at zero: "4,186 names → 1,412 with a bar and a 200-day
  average → 37 met the volume scan → 4 are signals". `vb_breadth_daily.detail.funnel`.
* No row action changes money. **Dismiss** (a note on the row) is the only mutation.

> **Built at VB8, and Dismiss was not.** `03`'s data model has no table to hold a note, and adding
> one to carry a control nobody has asked for would have been a migration in service of this
> paragraph. The hub therefore has **no server actions at all** — a stronger safety property than
> the one this section described, and `__tests__/read-only.test.tsx` asserts it that way. The tab
> reads **Positions** rather than "Book": `PORTFOLIO_REDESIGN.md` §8 retires that word from
> reader-facing copy and the swing hub already calls its equivalent tab Positions. The route keeps
> its `/vbt/book` path. DECISIONS-VB VB8.4 and VB8.5.

### `/vbt/book`

* **Working orders**: symbol, limit, quantity, value, the stop it will get, placed on, **sessions
  worked of three**, state, and — when it is live — the broker order id. A row in its third
  session is marked "cancels tonight".
* **Open positions** from `vb_position`: symbol, entry date, entry average, quantity, stop in
  force, **GTT status (armed / naked in red)**, the 21-EMA and today's distance to it, return %,
  R, sessions held, and "sells at tomorrow's open" when `exit_queued_for` is set.
* **Closed positions** below, with return %, R, hold and reason.
* Every row labelled **Simulated** while `vb_position.simulated` is true, and the two never share
  a total.
* **The fill-rate line** (`04` §7.3): "This book's limits filled 14 of 17 times within three
  sessions (82%). The study modelled 91%." It is the honest early-warning that the live result is
  parting company with the study, and it is on the page from the first fill.

### `/vbt/backtest`

The latest **finished** `vb_backtest_run` per source, side by side with STRATEGY §4's published
numbers:

| | The study (STRATEGY §4) | This run (`source`, finished at) |
|---|---|---|
| CAGR | 18.2% | … |
| max drawdown | −27.9% | … |
| trades | 761 | … |
| win rate / profit factor | 37.8% / 1.55 | … |

* A **drift banner** when `drift.flagged` — more than 1 CAGR point apart (VB9): "This run is
  3.4 points below the published number. The bars changed, or the code did. Do not use the
  published number until this is explained."
* The equity curve, the yearly table, the three books (`full`, `gate_off`, `raw_scan`) with
  breadth's and the trend filters' contributions as differences.
* **The caveats as a component**, `01` §5 verbatim, above the numbers and not below them —
  including the run's own addition about modelled fills.
* The run's parameters, so a reader can tell which config produced the number.

**Read-only assertion**: `apps/web/src/app/(app)/vbt/__tests__/read-only.test.tsx` asserts that
the only server actions under `/vbt` are `noteAdd` and `noteDismiss`, and that nothing under the
route imports `baskfy_execution` or calls `/desk/*` or `/vbt/execute`.

## 3. The desk console `/vbt` (Jinja, `kite-momentum-rebalancer/app`)

One page, three panels. It does **not** refresh every five seconds: this is an end-of-day
strategy and its plan is built twice a day, not tick by tick.

**Plan (top).** The current plan (`source=MORNING` before the open, `EVENING` after the close),
its `plan_id` and the countdown to its 30-minute expiry, and its lines in `04` §9.3's order:
`SELL_AT_OPEN` first, then `CANCEL_LIMIT`, then `ARM_GTT`, then `PLACE_LIMIT`. Each line carries
its quantity, level, value, the cap that bound, and a **Confirm** button. The response renders
inline: `SIMULATED` (DRY_RUN or the flag off), `SENT`, `FILLED`, `BLOCKED (reason)`. **There is
no "confirm all"**, and there is no button on a locked-circuit line. Lines are prefixed **VBT**
so nobody confuses them with the weekly book's or the swing book's.

Below the lines, **the skips with their reasons** — `04` §9.1's table, one row each. A plan
without its skips is not honest.

**Working orders (middle).** Every `vb_order` not yet terminal: symbol, limit, quantity, state,
sessions worked of three, broker order id, and a **Cancel** button (a guarded `cancel_order`, not
a buy). A row whose window ends tonight is highlighted; the evening sweep will cancel it, and the
button is for the case where a person wants it gone sooner.

**Book (bottom).** Open positions with their GTT ids, a **Re-arm GTT** button for a naked
position (the only other order-shaped action, and it is a GTT, not a buy), and the last five
manage actions.

**Status bar.** `DRY_RUN`, `BASKFY_VBT_EXECUTION_ENABLED`, the gate and its percentage, the
as-of session, Kite token age, **"DRY_RUN sessions: 7 of 20"** (`02` §3.1 — a gate here, and the
bar says so), and the session's counters from `vb_session`.

The desk's existing login, CSRF and `websec` middleware cover the page; `DRY_RUN` is already in
every desk page's header and stays so.

## 4. Alerts

The evening job sends one email through the existing alert path (a new `AlertName.VBT_EVENING`):
the gate and its number, tonight's `PLACE_LIMIT` lines with levels and quantities, tomorrow's
`SELL_AT_OPEN` lines, **the working orders that expire tonight**, positions with a naked GTT
(always, separately, in red), and the DRY_RUN session count against 20.

Four alert names, in the M20 pattern: `VBT_POSITION_NAKED` (a position without a resting GTT —
the one state the method forbids), `VBT_DETECT_STALE` (no `vb_breadth_daily` row for the
published session by 21:30), `VBT_ORDER_PAST_EXPIRY` (a `vb_order` still working after its third
session — the sweep did not run, or its cancel line was never confirmed), `VBT_POSITION_NO_BAR`
(`04` §6.5).

**Built at VB7, and not the way this section predicted it.** The runbook is
[`docs/runbooks/08-vbt-evening.md`](../runbooks/08-vbt-evening.md), not runbook 6 — 6 is the
swing book's morning and 7 is the Phase-A deploy, both of which already existed. And the four are
raised **in-process**, by `baskfy.vbt.check_*` tasks on Beat (21:30 and 21:40), rather than by
Prometheus rules: the same argument `alerts.py` makes for `publish_late`, which is that an alert
depending on a Prometheus that is not deployed is an alert nobody gets. `VBT_DETECT_STALE` reads
`vb_breadth_daily` rather than `vb_signal_daily` because a session with no signals still writes
its breadth row, and that is exactly the distinction the alert is for. See DECISIONS-VB VB7.2.

`AlertName.VBT_EVENING` — the nightly summary email above — is **not built**. The four checks
are; the digest is not, and STATUS says so.
