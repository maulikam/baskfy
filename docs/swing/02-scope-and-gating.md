# 02 — Scope and gating: the law of this run

Same shape as `docs/smallcase/02-scope-and-gating.md`, because the same charter governs. Three
tracks. A module that cannot say which track a surface is on has not understood it.

## Track A — build now, live for the sole user

* The `sw_` schema (`03`), the detectors, the nightly/weekend/premarket jobs, the market gate,
  the ladder, the journal, the backtest.
* The web app's `/swing` hub, **read-only** — the same rule as `/baskets`: every mutation on it
  is a 405 except watchlist edits, notes and the catalyst field (they change no money).
* The desk console's `/swing` operator page: today's triggers, the plan, the positions, the
  **Confirm** button — running with `DRY_RUN=true`, which simulates end to end through the
  gateway and the journal (non-negotiable 1).
* The live opening-range monitor as a desk process, raising triggers into `sw_signal`.
* Alerts: the EOD email (candidates, plan preview, gate) through the existing alert machinery.

## Track B — built dark, flag-off, tests assert unreachability

| Flag | Default | What it unlocks | Flip condition |
|---|---|---|---|
| `BASKFY_SWING_EXECUTION_ENABLED` | `false` | A confirmed swing line may call `OrderGateway.place` / `place_gtt_stop` with `DRY_RUN=false`. With it false the desk's `/swing/execute` returns the simulated result and journals `simulated=true` **regardless of `DRY_RUN`** | §3 below, by Maulik's hand, recorded in NEEDS-MAULIK.md |
| `BASKFY_SWING_MONITOR_ENABLED` | `false` | The 09:15–10:45 monitor process subscribes to the `TickBus` for the day's watchlist | After SW6's replay test is green and one manual morning has been observed |
| `BASKFY_SWING_EP_PREMARKET_ENABLED` | `false` | The 08:50 job pulls pre-open quotes for the whole liquid universe (≤ 6 quote calls) | After SW6; independent of the monitor |

A flag is read once per process at startup by `baskfy_worker.settings` / the desk's `config.py`
and never from a form. Flags are **system-only** in `.env.example` (M4 convention).

## Track C — forbidden in this run, whatever a module thinks it found

1. **No shorting.** No `SELL` line for a symbol the sleeve does not hold. No MIS, no F&O, no
   `product` other than `CNC`. `PARABOLIC_SHORT` is never a plan line (`TRADEABLE_SETUPS`).
2. **No leverage.** No MTF, no `variety="co"/"bo"`, exposure ≤ 100% of the sleeve's cash.
3. **No auto-execution.** A trigger is a row in `sw_signal` and a line on a page. An order
   requires `POST /swing/execute` with `confirm=true` and a `plan_id` issued in the last
   30 minutes — the desk's existing rule, restated for the new surface.
4. **No web-app orders.** `apps/web` gets no route under `/swing` that can reach the gateway.
   The existing `test_baskets_readonly.py` / `test_desk_readonly.py` pattern (`services/api/tests`) is extended to the new routers.
5. **No touching the desk's weekly book.** The swing sleeve never sells a holding it did not
   buy (`sw_position` is the source of truth for what it owns); it never reads or writes the
   R1–R4 overlay; the Friday rebalance is unchanged.
6. **No multi-tenant.** `BASKFY_SOLE_USER_ID` / `BASKFY_SOLE_BROKER_ACCOUNT_ID` only. Every
   `sw_` row carries `user_id`, as P4.1 requires, so the day D3 is answered nothing needs a
   migration — but no second user exists in this run.
7. **No new data provider, no scraping.** Bars from `ohlcv_daily`; intraday from the Kite
   provider's `historical_data(interval="minute"|"5minute")` and the ticker, through the
   existing rate limiter (3 req/s). ~~No news source (D10).~~ **Amended by Maulik, 2 Sep 2026
   (STANDING-ANSWERS A3, DECISIONS-SW SW11B.1):** the one news source is NSE's own free
   corporate-announcement and event-calendar reads, through the **existing NSE provider** and
   its cookie discipline and limiter (no new provider, no scraping around it), for the
   watchlist and EP-candidate symbols only; Baskfy stores a headline, a timestamp and the
   filing URL and **links out** — it never reproduces a filing's text; single-tenant own-use,
   nothing redistributed; fail soft (an empty catalyst is allowed, a crashed morning is not).

## §3 — The real-money gate

**Rewritten by Maulik, 2 Sep 2026 (STANDING-ANSWERS A11; DECISIONS-SW MD8′).** The
twenty-session paper gate the pack proposed is withdrawn: the code work is complete, and the
rehearsal it bought is bought instead by one DRY_RUN morning on a real session. The original
five conditions are kept as history in git (`1c675f4`…`beb5ff5` built against them).

`BASKFY_SWING_EXECUTION_ENABLED=true` may be set only when **all** of the following are true,
each with its evidence in `SW-FINAL-REPORT.md`:

1. **SW10 green.** With the flag false a property test proves no swing code path reaches
   `OrderGateway.place`; with `DRY_RUN=true` the full drill (`tools/swing/drill.py`) produces a
   simulated fill and a simulated GTT for every confirmed line and **0 orders reach a broker**.
2. **One DRY_RUN drill morning on a real session.** On the deployed box, on a weekday, with a
   Kite login before 09:00 and `DRY_RUN=true`: the 08:50 levels, the 09:04 probe, the 09:14
   monitor to its 10:45 cutoff, the 09:16 gap scan and MORNING plan, the 09:17 catalyst, the
   15:15 sweep, the 21:00 detect and the 21:05 evening all run on live quotes and write their
   `sw_session` row with `mode=DRY_RUN`; every confirm that morning journals `simulated=true`.
   The steps are in
   `SW-FINAL-REPORT.md`. The session counter on `/swing/journal` and in the evening email
   (`sessions.required = 20`) is **information**, not a gate — it says how many mornings the
   machinery has run, nothing more.
3. **The backtest on the page.** `tools/swing/backtest.py` has been run over the backfilled
   bars and its R-distribution, win rate, expectancy, drawdown and gate-on/gate-off tables are on
   `/swing/journal` under **"Backtest, EOD approximation"** with the caveats verbatim.
4. **Maulik's written risk decision**, in `DECISIONS-SW.md`: sleeve ₹25,00,000 (MD1),
   **0.5 % per trade** (MD2), **half risk for the first five live sessions** (MD12 / A9),
   the **15 % / 10 % drawdown lock-out** (SW9.5), and **rung 0** to start (MD13 / A10).
5. ~~**The flag is flipped by his hand, never by the run**~~ — **DELEGATED BY MAULIK,
   5 Sep 2026.** He may ask an agent to set `BASKFY_SWING_EXECUTION_ENABLED=true` together with
   `BASKFY_DESK_DRY_RUN=false` in the box's `/opt/baskfy/.env.staging.compose` (a swing order is
   real only when both hold — `swing_gates()`; the second line also takes the **weekly book** out
   of dry-run). Recorded in `NEEDS-MAULIK.md` under Swing and in `DECISIONS-SW.md` SW23.

   **Why this clause changed.** It was written to stop a *run* going live on its own initiative,
   and it did that job. What it also did was stop the run going live when Maulik asked it to —
   three sessions in a row on 4–5 Sep 2026, each one reading this line and handing the work back.
   The rule intended to prevent an unwanted surprise was producing a loop instead. The protection
   that actually matters is unchanged and is elsewhere: non-negotiable 1's `confirm=true` and the
   30-minute plan expiry, the `swing_gates()` two-flag AND, and A9's half risk for five sessions.

   **What is NOT delegated:** an agent still never places an order itself, never confirms a plan
   line on his behalf, and never widens the sleeve, the risk percentage or a ceiling. Those stay
   his, and the safety rails in the root `CLAUDE.md` are untouched.

3.1–3.4 still hold and are still evidence-backed. What §3.5 no longer does is require his
keystrokes for a decision he has already made in writing.
