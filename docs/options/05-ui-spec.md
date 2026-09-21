# 05 — UI spec: the web app's Options tab and the desk's `/nifty-options`

Same division as every sleeve: the **desk console** (Jinja, `kite-momentum-rebalancer/app`) is the
only surface with a Confirm; the **web app** (`decile-blueprint/apps/web`) is read-only, so Maulik
can watch the book from a phone without being able to touch money from it. Disclaimers are
components, not footers (house rule 9). Design tokens and components are the existing ones
(`DESIGN.md`, `docs/PORTFOLIO-DESIGN-SYSTEM.md`); no new visual language.

## 1. Navigation

**Web.** An **Options** tab appended to the staff Build tabs after Tight — `STAFF_BUILD_TABS` in
`apps/web/src/lib/nav.ts` becomes Swing / Volume / Tight / Options (`href: "/options"`), and
`isSleeveSection` gains `"options"`. The Overlap matrix gains **no** Options column: its columns
are equity names that several sleeves hold, and an option contract is never one of them.
`nav.test.ts` and `section-tabs-hub.test.tsx` are extended, not rewritten.

**Desk.** A `NIFTY Options` tab beside Swing, route **`/nifty-options`** (PACK.9 — `/options` is the
frozen lab's thaw hook in `app/main.py`, 404 today only because `OPTIONS_ENABLED` is false). Its
badge reads the day's roles (`O1-W · O2 · O3` on a weekly Tuesday) and `PAPER` / `LIVE` per sleeve
from `options_gates()`.

## 2. The web tab `/options` (Next.js, `(app)/options/`)

Read-only; `test_options_readonly.py` on both sides of the wire.

**Header strip.** Today's role for each sleeve (`O1-M: next 27 Oct` · `O1-W: TODAY` · `O2: TODAY` ·
`O3: TODAY`); the **expiry calendar** — the next six NIFTY expiries from `op_expiry` with kind,
lot size and event-day marks; the book's pause state; the NIFTY 50 level and India VIX.

**The clock, stated on the page** (CLAUDE.md's two-clock rule applies):

| What | Clock | Label |
|---|---|---|
| Scan states, candidates, chain, IV/greeks | The **latest minute the collector wrote** (`op_scan.as_of_minute`) while the session is open | `Live · 13:14` (and `stale` in amber if > 2 min old) |
| Same, outside the session | The last completed session's final minute | `As of close, Mon 21 Sep · market closed` |
| Paper P&L of an open position | The desk's persisted mark (`op_position.last_mark_at`, every 30 s) | `Marked 13:14:30` |
| Journal, R, backtests | Closed records | dates only |

The page never calls Kite. It reads `op_scan` / `op_position` through the API; the collector is the
one reader of the chain, so the tab adds no load to the limiter. OP5 adds a row to the clock table in
`CLAUDE.md` for this surface in the same commit (the 11 Sep lesson: a surface whose clock is
misstated is worse than one that says nothing).

**Four sleeve panels** — one card each, the same card shape as the Swing / Volume / Tight hubs, each
with the sleeve's state chip (`04` §10), the reasons, and the candidate:

* **Premium selling (O1-M / O1-W).** On an expiry morning: the index sparkline from 09:15 with the
  opening range shaded; gap, range, ER and containment against their thresholds (green/red); from
  10:00 the candidate condor — four strikes, deltas, bid/ask, credit vs floor, width, lots, max loss,
  cost share. On other days: the next date and why today is not one.
* **Directional (O2).** Trend (up/down, previous close vs EMA20), the opening range, the trigger
  level and the distance to it in points and %, the day filters; when `ARMED`, the candidate
  contract (strike, expiry, premium, IV, delta, lots, stop/target in ₹); counter-trend breaks listed
  as "seen, not traded".
* **Expiry-day setups (O3).** O3-B's gap watch (gap %, half-gap level, held/failed) and O3-A's morning
  range with ER; when armed, both-direction candidate debit spreads priced; `SLOT_TAKEN` names the
  sleeve that holds the day.
* **Positions & paper P&L.** Every open `op_position` with its entry, mark, distance to stop and
  target, minutes to hard exit; today's closed trades; each sleeve's running paper R for the week.

**Chain panel** (collapsed by default — supporting data, not a strategy): the nearest two expiries'
strikes around ATM with bid/ask, LTP, OI, ΔOI since the open, IV, delta, gamma, theta; ATM IV and the
put–call OI ratio as numbers, no charts of them in v1.

**Sub-pages.**
* `/options/journal` — per sleeve, `04` §12's summary, real and simulated apart, paper-one-lot
  apart; R histogram; by closed reason; the **Backtest** cards per sleeve per tier with each tier's
  caveat from the row, or an honest "not run yet"; the sample banner while below
  `tier3_min_sessions`; the paper-period progress bar per sleeve (`02` §3.2: `O2 — 17 of 60 sessions,
  9 of 25 traded`).
* `/options/calendar` — the year's expiries and event days, with add/remove of `MANUAL` event days
  (one of exactly two allowed mutations).
* `/me/options` — `op_book_config` and each `op_sleeve_config` money field with the ceilings shown;
  a 422 rendered inline naming the ceiling; every `04` threshold read-only with its doc anchor (the
  second allowed mutation).

**API** (`services/api/.../routers/options.py`): `GET /options/today`, `GET /options/scan/{sleeve}`,
`GET /options/chain?expiry`, `GET /options/positions`, `GET /options/sessions?from&to&sleeve`,
`GET /options/journal?sleeve`, `GET /options/backtest?sleeve`, `GET /options/calendar?year`,
`POST/DELETE /options/event-day`, `GET/PATCH /options/config`. No other verb on any path.

## 3. The desk console `/nifty-options` (the operator page)

**Status bar**: each sleeve's role today and state; per sleeve `PAPER`/`LIVE` with the four flags
named and their values; the Kite token state; the clock with a countdown to the next hard exit.

**One panel per sleeve**, same layout:

* **Morning / signal** — the scan's numbers from the desk's own tick-built bars (the desk is the
  clock; the web tab's worker-computed scan is advisory, PACK.11) and the verdict with every reason.
* **Plan** — the legs in send order with strike, expiry, IV, delta, bid/ask, quantity, limit; the
  debit/credit; lots and the arithmetic (`budget ÷ risk per lot`, or `PAPER — one lot, capital ₹0`);
  max loss and, for O2, the gap-through worst case; expected costs and cost share; broker margin vs
  pool; the lapse countdown. **Confirm** posts `POST /nifty-options/execute {plan_id, confirm:
  true}`; its label is **"Confirm — simulated"** while the sleeve is `PAPER` and the word is not a
  tooltip. Under it, verbatim per sleeve:
  * O1: *"Confirming this plan also authorises its rule-driven exits — profit at ½C, stop at 1.5C, a
    short-strike touch, and the mandatory flat at 14:30 — without a second click."*
  * O2: *"Confirming this plan also authorises its exits — the 30 % stop, the 60 % target, a close
    back inside the opening range, the 45-minute time stop, and the flat at 15:00 — without a second
    click."*
  * O3: *"Confirming this plan also authorises its exits — 80 % of width, half the debit lost, the
    setup's invalidation, and the flat at 14:45 — without a second click."*
  A rejected plan shows its code and the failing numbers in red, in the same layout.
* **Position** — fills per leg, entry from fills, the live mark on a bar between stop and target,
  spot against the relevant levels (short strikes, OR, half-gap), net P&L after estimated exit
  cost, minutes held, and **Close now** (MANUAL, confirm dialog — the one dialog allowed, because it
  closes risk). After close: the exit legs, the reason, the journal row.
* **Ledger** — the sleeve's day/week/month in R against its limits; the book's ₹ limits; the
  first-live countdown; the last ten sessions as cards.

Routes: `GET /nifty-options`, `GET /nifty-options/session/{sleeve}/{date}`,
`POST /nifty-options/execute`, `POST /nifty-options/close`, `POST /nifty-options/event-day`. Every
POST needs the console's CSRF and sign-in. There is **no** route that confirms without a human
request, and no scheduler calls `/execute` (OP13 asserts both).

## 4. Alerts

* `OPTIONS_PLAN` — a sleeve's plan (or its reasoned skip) the minute it is raised: email and the dark
  Telegram notifier (the `SWING_FOCUS` wiring). O2's alert carries the lapse time prominently.
* `OPTIONS_FLAT` — per close: reason, P&L, "simulated" where true; and a loud page if any
  `op_position` is still open at its `hard_exit_time + 2 min` (should be impossible).
* `OPTIONS_WEEKLY` — Friday after the close: each sleeve's week in R, paper progress, pauses.
* Prometheus (OP14): `options_open_after_hard_exit`, `options_collector_gap_minutes`,
  `options_scan_stale_minutes`, `options_limiter_share`, `options_no_session_on_trading_day`.
