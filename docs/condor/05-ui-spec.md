# 05 — UI spec: the desk's `/condor` and the web hub's `/condor`

Same division as the swing run: the **desk console** (Jinja, `kite-momentum-rebalancer/app`) is
where a plan becomes an order and is the only surface with a Confirm; the **web app**
(`decile-blueprint/apps/web`) is read-only and exists so Maulik can look at the book from a
phone without being able to touch money from it. Disclaimers are components, not footers
(house rule 9).

## 1. Navigation

Desk: a `Condor` tab beside `Swing`, visible always, its badge reading the next expiry date and
`DRY_RUN` / `LIVE` from `condor_gates()`. Web: `/condor` in the app nav with the same
`ready` / `soon` flip the swing hub used.

## 2. The desk console `/condor` (the operator page)

**Status bar** (every view): underlying · today's role (`NOT AN EXPIRY` / `EXPIRY — OBSERVING`
/ `PLANNED` / `OPEN` / `CLOSED` / `PAUSED until …`) · mode from `condor_gates()` with the four
flags named and their values · the Kite token state · the clock, and after 14:00 a countdown to
`hard_exit_time`.

**Panel A — the morning.** From 09:15 on an expiry day: a live sparkline of the index minute
closes with the 09:15–09:44 opening range shaded, the four gate numbers updating (gap, range, ER,
containment) each against its threshold, green or red, and at 09:59 the **verdict** with every
reason. On a non-expiry day the panel says which date is next and why today is not one.

**Panel B — the plan.** At 10:00 with a `TRADE` verdict: the four legs in send order with
strike, delta, bid/ask, quantity and limit; the credit in points and ₹ against the floor; the
width; lots and the arithmetic they came from (`budget ÷ (loss per lot + reserve)`); max loss;
profit target and stop in ₹; expected cost and the cost share against 20 %; the broker's margin
(hedged and transient) against the pool; a 15-minute countdown to lapse. The **Confirm** button
posts `POST /condor/execute {plan_id, confirm: true}`; its label reads **"Confirm — simulated"**
in `DRY_RUN` and the word *simulated* is not a tooltip. Under it, verbatim: *"Confirming this
plan also authorises its rule-driven exits — profit at ½C, stop at 1.5C, a short-strike touch,
and the mandatory flat at 14:30 — without a second click."* (PACK.5.) A rejected plan shows its
code and the numbers that failed it, in the same layout with the offending cell red.

**Panel C — the position.** After `OPEN`: each leg's fill, C from fills, the live **D** as a
bar between 0 and 1.5 C with ½C and C marked, spot against both short strikes, the running
net P&L after estimated exit cost, the minutes held, and a **Close now** button (MANUAL exit,
confirm dialog — the one place in the console a dialog is allowed, as it closes risk rather
than opens it). After `CLOSED`: the exit legs, the reason, the journal row.

**Panel D — the ledger.** Today's realised/marked P&L against the daily limit; the month against
the pause threshold; the first-live countdown (`n of 5 at half size`); the last six sessions as
cards (date · verdict · reason or R · simulated/real) — real and simulated never in one number.

Routes: `GET /condor`, `GET /condor/session/{date}`, `POST /condor/execute` (confirm),
`POST /condor/close` (manual), `POST /condor/event-day` (add/remove — it changes no money).
Every POST needs the console's existing CSRF and sign-in.

## 3. The web hub `/condor` (Next.js, `(app)/condor/`)

Read-only, the `test_condor_readonly.py` assertion on both sides of the wire.

* **`/condor`** — the next expiry and its countdown; the last verdict with its reasons and the
  minute chart it came from; the open position if any (D bar, P&L) refreshed every 30 s from
  `GET /condor/session/today`; the pause state.
* **`/condor/journal`** — `04` §10's summary for real and for simulated, apart; the six-bucket R
  histogram; by reason; by month; **"Backtest"** with one card per tier from `oc_backtest_run`
  and each tier's caveat verbatim, or an honest "not run yet"; the Tier-3 sample banner while
  the observed sample is below `tier3_min_expiries`.
* **`/condor/calendar`** — the year's monthly expiries from `oc_expiry`, event days marked, with
  add/remove of `MANUAL` event days (a server action behind a plain form; the read-only test
  lists it as one of exactly two allowed mutations).
* **`/me/condor`** (settings) — `oc_config`'s money fields with the ceilings shown and a 422
  rendered inline naming the ceiling; the `04` tunables read-only with their doc anchors.

API (`services/api/.../routers/condor.py`): `GET /condor/next`, `GET /condor/session/{date}`,
`GET /condor/sessions?from&to`, `GET /condor/journal`, `GET /condor/journal.backtest`,
`GET /condor/calendar?year`, `POST/DELETE /condor/event-day`, `GET/PATCH /condor/config`.

## 4. Alerts

* `CONDOR_VERDICT` at 09:59/10:00 — the verdict and reasons, or the plan summary — email and
  the dark Telegram notifier (same wiring as `SWING_FOCUS`).
* `CONDOR_FLAT` at close — the reason, the P&L, "simulated" where true; **and** an alert if the
  book is still `OPEN` at `hard_exit_time + 2 min`, which should be impossible and is therefore
  paged loudly.
* `CONDOR_MONTHLY` on the first trading day — the month's journal summary.
* Prometheus rules (OC11): `condor_open_after_hard_exit`, `condor_no_verdict_on_expiry`,
  `condor_collector_gap_minutes`, `condor_limiter_share`.
