# DECISIONS-SW — judgement calls of the swing run

Same convention as `docs/DECISIONS-MERGE.md` and `docs/smallcase/DECISIONS-SC.md`: numbered by
module; context, the choice, the rejected alternatives and why, the reversal path. Decisions
made without Maulik's review are tagged **⚠ UNREVIEWED** until he clears them.

Six decisions were pre-taken in the pack so the run does not stall on them:

## PACK.1 — The parabolic short is detected, never traded · (not reversible this run)

NSE cash equities cannot be shorted for delivery; MIS squares off at 15:20 and F&O covers ~180
names, and both products sit behind `INTRADAY_ENABLED` / `OPTIONS_ENABLED`, which default off
(non-negotiable 5). `TRADEABLE_SETUPS` excludes `PARABOLIC_SHORT`; SW10 proves no plan line can
carry it. Rejected: an F&O short leg (a new product, a new risk model, and he tells beginners to
skip the setup anyway). Reversal: a written decision to enable `OPTIONS_ENABLED`, then a new
module — not an edit to this run.

## PACK.2 — Two entry modes, EOD trigger and live ORH, both in this run · ⚠ UNREVIEWED

Maulik chose both (2 Sep 2026). The EOD mode (a `BUY_ON_TRIGGER` line at the pivot, sent as a
LIMIT buy the next morning on confirm, stop at the prior day's low) needs no open-hour process
and is the fallback whenever the monitor is off; the live mode (5-minute ORH break, stop at the
range low) is the method as taught. They share the plan, the sizing and the stops. Rejected:
EOD only (loses the ORH's selectivity), live only (a missed morning = no trades). Reversal:
disable the monitor flag; the EOD mode stands alone.

## PACK.3 — The swing GTT band is 0.5–10%, not the desk's 8–12% · ⚠ UNREVIEWED

The desk's `StopBand(0.08, 0.12)` encodes vol-scaled stops for a weekly book. A swing stop at the
low of the day is typically 2–6% away and is *supposed* to be tight. `packages/execution.gtt`
already injects the band (it is "journalled, not refused"), so the desk's `/swing/execute`
passes `StopBand(min_pct=0.005, max_pct=0.10)`; the sizing refuses anything wider than 10%
before a GTT is ever built. Rejected: widening the desk's default (would silently change the
weekly book's findings). Reversal: one constant in the desk's swing route.

## PACK.4 — The swing sleeve has its own exposure ladder, separate from R1–R4 · ⚠ UNREVIEWED

The desk's R1–R4 overlay decides the weekly momentum book's equity share from index/breadth
signals. His progressive exposure is a different thing — it reads the trader's own results. Both
exist; neither imports the other (`test_regime_names_do_not_collide.py` is the precedent, and
SW10 adds the swing edge). The sleeve is a `MY_STRATEGY` capital portfolio in the M34/
PORTFOLIO_REDESIGN sense, with `sleeve_capital_inr` as its cash. Rejected: feeding the swing
book through R1–R4 (would put swing entries under a weekly cap they have nothing to do with).
Reversal: a `regime_cap` input on `build_entries`, as `sleeves.py` already accepts.

## PACK.5 — Only the risk knobs are settings; the pattern thresholds are code · ⚠ UNREVIEWED

`sw_config` carries sleeve capital, risk per trade, max position %, max positions, the
opening-range window, the stop mode and the three liquidity floors. Base geometry, EP gap,
ladder tiers etc. stay `baskfy_core.swing.config` defaults. Reason: a threshold that can be
changed in a form gets changed after a bad week, which is the failure mode the method exists
to prevent; a code change leaves a diff and a DECISIONS entry. Rejected: a full "strategy
editor". Reversal: promote a field to `sw_config` with a migration and a ceiling.

## PACK.6 — The ladder reads simulated trades until execution is enabled · ⚠ UNREVIEWED

Before the real-money flag flips there are no real closes, and a ladder that reads nothing
never moves — which would leave the paper period unable to exercise SW8. So `exposure_tier`
reads simulated closes while `BASKFY_SWING_EXECUTION_ENABLED=false`, and real closes only once
it is true (the paper rung is reset to 0 at the flip; the journal page keeps both cards).
Rejected: mixing them (a paper streak would size real money). Reversal: a query filter.

---

(Module entries follow, newest at the bottom.)

## SW0.1 — The pack is committed with two documentation corrections in it · ⚠ UNREVIEWED

**Context.** SW0 is "read the pack and record where the run stands", and it is the commit that
puts `docs/swing/` into the repository. Reading the pack against the code found two places where
the document was incomplete rather than wrong, and both were fixed in the same reading:

* `04` §6.1 described the default stop as "low of the day" without naming the `stop_mode` value
  `LOW_OF_DAY`. The engine has the value; the numerical contract did not mention it, so SW1's
  docs-parity test — which asserts that every string the engine can write is named in `04` —
  failed on it. The document now names both modes.
* `03` §2 and §3 gave `sw_setup_daily` and `sw_market_daily` primary keys with no `user_id`,
  which contradicts `02` Track C §6. See SW2.1; the fix is a note in `03` pointing at it.
* `03` §1 said the audit was `updated_at`/`updated_by`, "as `settings_audit` does for the desk".
  See SW2.2; `03` gains §1b.

**The choice.** Commit the pack *as adopted* — corrections included — rather than committing it
verbatim and then correcting it in SW1 and SW2. The rejected alternative (three commits to reach
a document that is right) buys a tidier history and costs a reader two commits in which the
specification says something the run already knew to be incomplete.

**Reversal.** `git show` the SW0 commit; the three edits are the only non-additive hunks in it.

## SW0.2 — The dev database cannot feed a detector, and SW0 says so rather than fixing it

**Context.** The detectors need 125 sessions of bars (`flag.lookback_bars + base_max_bars`). The
local dev database holds **ten**, for 180 instruments, and no `factor_daily` rows at all. Several
acceptance criteria in SW3–SW5 are phrased "on the dev stack".

**The choice.** Record it loudly in STATUS as a fact about the machine, and let each module
decide what to do about it when it gets there, rather than starting the run with a backfill.
A backfill is an evening of Kite calls (D5, `docs/07` §4b), it is not what SW0 is for, and the
modules that need bars can build their own fixtures — which they need anyway, because a test
that depends on whatever the developer's database happens to contain is not a test.

**Rejected.** Running `make backfill` first: hours of network before a line of the run's own code
exists, and it would still not make an acceptance criterion reproducible on another machine.

**Reversal.** None needed; it is an observation. If a real-data check becomes necessary, the
backfill command is in `RUN-AND-TEST.md` §2.
