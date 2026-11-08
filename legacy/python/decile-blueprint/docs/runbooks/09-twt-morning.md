# Runbook 9 — the three-weeks-tight sleeve (TWT-1)

**Verified against:** NOT YET — written from `baskfy_worker/tasks/twt.py`,
`baskfy_worker/tasks/twt_evening.py` and the desk's `app/twt_desk.py` / `app/twt_execute.py`,
like the eight before it. No TWT evening has run anywhere, no `tw_position` row has ever been
created by this product, and `BASKFY_TWT_EXECUTION_ENABLED` is **false** in every environment.

**The full first-morning sequence is `docs/twt/FIRST-LIVE-MORNING.md` at the repository root.**
It is longer than this file and it is the one to read before a session, not during one. This
file is what an alert needs at 21:05 or at 15:16.

**Read this first.** While `BASKFY_TWT_EXECUTION_ENABLED` is false, nothing below can reach a
broker: the desk runs the whole path — guards, risk, rate limits, journal — through the
gateway's dry-run adapter and records `simulated = true`. The alerts are still worth reacting
to; the point of rehearsing is to find out whether the machinery is trustworthy before it is
trusted.

**Nothing here places a buy.** The only order-shaped actions are a stop and the replacement of a
stop, both confirmed by hand on the desk's `/twt` page, both through the TWT gateway.

## TWT_EVENING (warning, ~21:05 IST)

Not a failure — the evening's plan. Gate, entries, exits, ratchets and skips, with the
`plan_id`. Nothing to do except read it, and the page (`$DESK/twt`) is the list.

**A plan expires thirty minutes after it is built.** The one in this message cannot be confirmed
tomorrow morning; the morning job rebuilds it (`make twt-plan DATE=… SOURCE=MORNING`), re-sized
against the same session. The morning plan re-sizes, it does not re-detect.

An evening with no entries is the *normal* evening: the study's nine years produced 164 trades,
about eighteen a year.

## TWT_POSITION_NAKED (critical, any time of day)

An `OPEN` position with `quantity_open > 0` and no `gtt_id`. **This is the one state the method
forbids** (non-negotiable 4), and on this sleeve it is also the fill-day rule: the backtest's
`STOP_DAY0` — a name whose entry session's own low is 20 % under its open — is modelled in the
live book *only* by the GTT armed in the same request as the fill.

Three causes, and they want different things:

1. **A fill without an arm.** Rare: the arm is in the same request precisely so this is one
   failure and not two.
2. **A ratchet that cancelled and did not re-arm.** `RAISE_GTT_STOP` is delete-and-replace. When
   the cancel succeeds and the arm fails, the desk records the intent, nulls the `gtt_id` and
   answers `BLOCKED` naming the position **NAKED**. *This is the likely one*, because the
   ratchet runs on up to ten lines a session for months. (When the **cancel** fails the old stop
   is still resting and nothing was placed — that is a stop one session behind, not a naked
   line.)
3. **A corporate action** — see `TWT_ADJUSTMENT_RESET` below.

What to do, in order:

```bash
curl -fsS -X POST $DESK/twt/rearm -u desk:$DESK_PW -H "Origin: $DESK" -d confirm=true -d position_id=<id>
```

If the re-arm is refused, read the refusal. *At or above the last price* means the name has
fallen to or below its own stop: a GTT is no longer the answer, and the decision is whether to
sell, by hand, in the Kite app. *An untouchable instrument* means the sleeve should never have
bought it. Otherwise arm the stop by hand in Kite (trigger as the plan shows, limit = trigger ×
0.97, the full open quantity) and then tell Baskfy:

```bash
curl -fsS -X POST $DESK/twt/reconcile -u desk:$DESK_PW -H "Origin: $DESK" -d confirm=true
```

**Never cancel a resting stop to tidy up a discrepancy.** If Baskfy and Kite disagree about
whether a GTT exists, the safe direction is always *more* protection.

## TWT_GTT_MISSING_AT_1515 (critical, 15:15 IST)

The 15:15 sweep found an open line with no resting GTT **and could not re-arm it**. Everything
under `TWT_POSITION_NAKED` applies, and the clock does not: the session ends at 15:30, so this
is a now problem.

Run the sweep by hand to see the current count — it is idempotent and keyed on the day:

```bash
curl -fsS -X POST $DESK/twt/sweep -u desk:$DESK_PW -H "Origin: $DESK" -d confirm=true
```

Expect `naked: 0`. Do not leave the desk on a non-zero count.

## TWT_ADJUSTMENT_RESET (critical, ~21:05 IST)

A split or a bonus happened under a position this sleeve holds: `ohlcv_daily.adj_factor` no
longer equals the position's `entry_adj_factor`. **The GTT resting at the exchange is quoting a
pre-split price on a post-split instrument**, and a person has to look at it.

The sleeve refuses to "fix" this itself. Re-deriving the trail from the newly adjusted series
produces a *lower* trigger — that is what a split does arithmetically — and cancelling a resting
stop to arm a lower one is the one thing this sleeve must never do on its own (`docs/twt/04`
§7.3, DECISIONS-TW TW0.7). So the line is not emitted and this alert is raised instead.

What to do: open the name in Kite, look at the resting GTT, and decide. If the right answer is a
new trigger at the post-split level, cancel and re-arm **in the Kite app**, then run
`/twt/reconcile` so the book and the exchange agree. The desk's `RAISE_GTT_STOP` will refuse a
lower trigger and is right to.

## How to stop the sleeve

One line, and it never removes protection:

```bash
curl -fsS -X POST $DESK/twt/halt -u desk:$DESK_PW -H "Origin: $DESK" -d confirm=true
```

It zeroes `tw_config.sleeve_capital_inr` (audited, with the previous value, into
`tw_config_audit`) and expires every live plan. Resting GTTs stay exactly where they are, and
`ARM_GTT` and `RAISE_GTT_STOP` keep working: a halted sleeve cannot **buy**, and everything it
already holds keeps its stop and keeps ratcheting.

Every POST above needs `-H "Origin: $DESK"`. Without it the desk answers 403 with a sentence
about forms on other sites, and it reads exactly like a permissions problem.
