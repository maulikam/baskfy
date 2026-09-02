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

## §3 — The real-money gate (the "four years of losses", compressed)

`BASKFY_SWING_EXECUTION_ENABLED=true` may be set only when **all** of the following are true,
each with evidence in `STATUS.md` and a NEEDS-MAULIK.md entry Maulik has ticked:

1. SW10 green: with the flag false, a property test proves no swing code path calls
   `OrderGateway.place`; with it true and `DRY_RUN=true`, the full morning drill produces
   simulated fills and a simulated GTT for every confirmed line and **0 orders reach a broker**.
2. **20 DRY_RUN sessions** logged in `sw_session` (one row per trading day the monitor or the
   EOD plan ran), each with its plan, its confirms, its simulated fills and its `stops.manage`
   actions — the paper track record.
3. The backtest (SW9) over 2017→ has been run and its R-distribution, win rate and expectancy
   are on the `/swing/journal` page under a **"Backtest, EOD approximation"** heading with its
   caveats (no intraday history → entries at next-day open above pivot; no circuit modelling
   before 2020).
4. Maulik has read the paper journal and written in `DECISIONS-SW.md` the risk-per-trade and
   sleeve capital he is starting with (the pack's defaults are 0.5% and ₹0 — a sleeve with no
   capital plans nothing).
5. The first real session runs at **half** the configured risk (`sw_config.first_live_sessions`
   counts down from 5 with `risk_multiplier=0.5`) — the pack's version of "start small".

There is no engineering path around this gate. The run ends with the flag false.
