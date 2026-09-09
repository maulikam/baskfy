# QUESTIONS — for Maulik, with the pack's standing default for each

The run never waits on these. Each has the default the run proceeds on; an answer here (or in
NEEDS-MAULIK § Condor) becomes a `DECISIONS-OC.md` entry and the module that reads it adjusts.

| # | Question | Standing default the run uses |
|---|---|---|
| Q1 | The money: account ₹, margin pool ₹, execution buffer ₹, starting risk budget ₹ (the method says ₹10,000–₹15,000 to begin, ₹25,000 later). Written into `DECISIONS-OC.md` as the risk decision `02` §3.4 requires | `oc_config` seeded at **0** account and pool, ₹10,000 budget, 3 max lots; nothing is planned until the account and pool are set; everything is drilled with fixtures |
| Q2 | Will you buy minute-level historical NSE option data for Tier 3 (`07` §2)? If so, which vendor and which years | Tier 3 runs over the collector's own snapshots only; the gate's evidence is the six paper expiries plus a small Tier 3 |
| Q3 | The event-day list for FY 2026–27: the six RBI MPC dates, Budget day, any election-result date | The seed in `04` §1.3 as best known at write time, with `source=SEED`; you edit on `/condor/calendar` |
| Q4 | Is the F&O segment active on the Zerodha account the desk trades, and is the margin pool pledged / in cash? (`02` §3.5) | OC0 reads `margins()` and records what it finds; nothing depends on it until the flags |
| Q5 | Do you want the collector (`BASKFY_CONDOR_CHAIN_COLLECT_ENABLED`) on from the next expiry, so the forward dataset starts before the run finishes? | Off until OC3's limiter measurement; then the run asks in NEEDS-MAULIK — it is one flag and a deploy |
| Q6 | PACK.5 — do you accept that one confirm covers the rule-driven exits including the 14:30 flat? | Yes, as written; the Confirm button says so verbatim |
| Q7 | The wing widths (150 / 300) and the credit floor (25 %) are "initial values to test". After Tier 1/2 and the paper expiries, do you want the run to propose alternatives, or leave the numbers to you? | The run reports sensitivity (OC9's ±25 % table) and proposes nothing; the defaults stand |
| Q8 | Delegation of the four-flag flip (PACK.8) — if you choose to delegate, one line under NEEDS-MAULIK § Condor | Not delegated; the run ends at `02` §3 with everything drilled and the flags false |
