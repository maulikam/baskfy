# DECISIONS-OC — judgement calls of the condor run

Same convention as `docs/DECISIONS-MERGE.md` and `docs/swing/DECISIONS-SW.md`: numbered by
module; context, the choice, the rejected alternatives and why, the reversal path. Decisions
made without Maulik's review are tagged **⚠ UNREVIEWED** until he clears them.

Eight decisions were pre-taken in the pack (9 Sep 2026) so the run does not stall on them.

## PACK.1 — The frozen lab is ported by re-implementation, never thawed or imported · ⚠ UNREVIEWED

D4 froze `frozen/strangle/` and the safety rails say it is untouched. It nonetheless holds the
best-tested option arithmetic in the repo: `IronCondorPlanner`, BS/IV/delta, the cost model,
the depth-ladder fill simulator, the calendar. The run **re-implements** what `04` needs inside
`baskfy_core.condor` (pure, typed, `Decimal` where money), names the source function in each
docstring, and pins agreement with fixtures whose expected values were computed from the frozen
functions when the fixture was written — the test imports nothing from `frozen/`. Rejected:
thawing (a reversal of D4, which is a listed decision and Maulik's; and the lab is `float`,
SQLite-journalled and three-underlying, none of which fits the merged tree); importing across
the tree (couples the live desk to a frozen path and breaks the moment D4 is acted on either
way). Reversal: if Maulik thaws D4, the ported modules stay — they are the merged tree's, and
the lab is then a second implementation to diff against.

## PACK.2 — NIFTY only in Track A; BANKNIFTY dark behind its own flag · ⚠ UNREVIEWED

Maulik's recommendation: one index first, the broader one, and BANKNIFTY only if independently
better. The code is underlying-agnostic (every threshold is per underlying in `config`), the
backtest runs both separately, the collector may snapshot both, but the planner will not
consider BANKNIFTY while `BASKFY_CONDOR_BANKNIFTY_ENABLED` is false and the settings API refuses
to add it. Rejected: both live from day one (the same afternoon twice; doubles the first-live
risk without evidence). Reversal: one flag, plus his written decision in this file.

## PACK.3 — Monthly expiries only; weekly Tuesdays are refused, not skipped · ⚠ UNREVIEWED

The method says monthly. NIFTY also expires weekly on Tuesdays; a "skip" implemented as a gate
reason would still build a session, read the chain and tempt a manual override. So
`is_trading_day` is false on a weekly expiry and the desk process exits at 09:15 without a Kite
call. Rejected: weekly as Track B (a different premium regime, a different filter calibration —
a different strategy, not a flag). Reversal: a new config field `expiry_kinds` and a DECISIONS
entry; the calendar already knows the kind.

## PACK.4 — Delta from expiry-day Black–Scholes with hours to 15:30, and both conditions must hold · ⚠ UNREVIEWED

Delta on expiry day is a steep function of hours left and of the IV the solver finds from a
few-rupee mid; the frozen lab computed it the same way and it was usable. The short strike must
satisfy **both** the delta band and the opening-range condition; when they disagree the plan is
rejected rather than either being relaxed, because the range condition is the one that
protects the trade from the morning's own information and the delta band is the one that keeps
the credit honest. Rejected: premium-based selection (a fixed ₹ premium — drifts with VIX);
range-only (sells the strikes the delta says are too near on a quiet-but-tense day). Reversal:
`StructureConfig.selection = "DELTA_AND_RANGE" | "RANGE_ONLY"`, default the former.

## PACK.5 — The confirm covers the rule-driven exits, the 14:30 flat included · ⚠ UNREVIEWED

Non-negotiable 1 says orders fire only on a confirmed plan. Non-negotiable 4 also says every buy
gets a stop the same session without a second click — the book already arms an exit on the
strength of the entry's confirm. The option book's exits (½C, 1.5C, a strike touch, 14:30) are
the same thing in a market with no GTT for a multi-leg position: they are part of what was
confirmed, `client_id = plan_id:symbol:CLOSE`, the sentence on the Confirm button says so, and
a position can therefore be flat at 14:30 whether or not Maulik is at the desk. Rejected: a
second confirm for exits (a human who is away at 14:30 leaves an option book into the last hour
of expiry — the exact risk the method forbids); relying on the broker's MIS square-off (it is
later, at market, in the thinnest hour, and it is not the plan). The manual **Close now** button
stays as the human's override in the other direction. Reversal: a flag
`BASKFY_CONDOR_EXITS_NEED_CONFIRM`, default false, that the desk page would then honour with a
loud warning at 14:00 — not built in this run.

## PACK.6 — A backtest without observed option prices is labelled a model, and the gate reads only Tier 3 · (not reversible this run)

`07` §2 is the reason. A synthetic P&L on the page without its label is the one way this run
could make the desk believe something the data has not shown. The tier label and the caveat
are stored on the run row and rendered from it. Rejected: skipping Tier 2 (the *shape* — how
often ½C is reached before 1.5C on real index paths — is useful and honest when labelled).
Reversal: none in this run; a vendor purchase populates Tier 3 and the gate then has its
evidence.

## PACK.7 — The `04` tunables are config-file defaults, not `oc_config` columns · ⚠ UNREVIEWED

Money and limits (`oc_config`) are the trader's and are bounded by ceilings; the thresholds
(gap, range, ER, delta band, width, credit floor, profit/stop multiples, cost share) are the
*strategy's* and changing one is a change to `04`, which is a DECISIONS entry. Putting them on
a form invites a 09:58 tweak on a day the gate is about to say no. Rejected: full
user-tunability (the swing run's `sw_config` does expose a few thresholds; those are liquidity
floors the trader is meant to raise, not filter thresholds). Reversal: promote a field to
`oc_config` with a ceiling, in a later module, with the reason.

## PACK.8 — The pack does not pre-delegate the flag flip · ⚠ UNREVIEWED

The swing run's §3.5 was delegated by Maulik after it produced a loop (SW23). This run needs
four flags, two of which (`OPTIONS_ENABLED`, `INTRADAY_ENABLED`) also change what the *rest of
the desk* may do — with them on, the weekly book's gateway would accept an NFO or MIS order it
refuses today. That is a bigger door than the swing flag opened. So `02` §3.6 asks for his hand,
and says exactly how he delegates it if he chooses to (a line in NEEDS-MAULIK § Condor, then an
entry here). Rejected: copying SW23's delegation forward (a delegation is his to give, per
decision). Reversal: his line in NEEDS-MAULIK.
