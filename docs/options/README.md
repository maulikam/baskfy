# The options run (OP) — NIFTY index options, three sleeves, scans and paper

This folder commissions the options run: Baskfy learns to **scan, plan, size, confirm (on paper)
and manage** three intraday sleeves on **NIFTY index options** — weekly (Tuesday) and monthly
(last Tuesday) expiries — the same way `docs/swing/`, `docs/vbt/` and `docs/twt/` turned a method
into a schema, a scan, a plan the desk confirms, two pages and a module plan. It was commissioned
by Maulik in session on **22 Sep 2026**:

> "now I want to have similar strategies and scans for options as well, options of Nifty 50 for
> weekly options and monthly options expiry"

and his four answers the same day, which are the charter of this pack:

| Question | His answer (verbatim intent) |
|---|---|
| Underlying | **NIFTY index options**, weekly Tuesday + monthly last-Tuesday expiries |
| Strategy families | **Premium selling (hedged)**, **Directional buying**, **Expiry-day setups** — each with its own scan of today's candidates, like Swing / Volume / Tight |
| First version | **Scans + paper trading.** Orders simulated through the gateway; `OPTIONS_ENABLED` stays false; live money is a later flag only Maulik flips; **no auto-execute for any options sleeve** |
| Holding | **Intraday only.** `assert_not_overnight_option` stays; every position is flat by the close, earlier where a method says so |

**Read order for any agent session:**
`/CLAUDE.md` (the charter — it governs unchanged) → `docs/README.md` → this file →
[`02-scope-and-gating.md`](02-scope-and-gating.md) (the law) →
[`07-data-reality.md`](07-data-reality.md) (what a backtest may claim) →
[`06-module-plan.md`](06-module-plan.md) (the task list) →
[`04-business-rules.md`](04-business-rules.md) (the numbers the tests assert) → the rest as
modules cite them. **For sleeve O1 also read `docs/condor/01`, `04` and `07`** — see below.

| Doc | What it is |
|---|---|
| [`KICKOFF-PROMPT.md`](KICKOFF-PROMPT.md) | The one prompt that starts the autonomous run |
| [`01-method.md`](01-method.md) | The three sleeves in plain words: thesis, honest limit, when, what, exit, size |
| [`02-scope-and-gating.md`](02-scope-and-gating.md) | **The law of this run.** Track A / B / C and the real-money gate, per sleeve |
| [`03-data-model.md`](03-data-model.md) | The `op_` schema: NFO contract master, chain snapshots, index minute bars, scans, sessions, plans, journal |
| [`04-business-rules.md`](04-business-rules.md) | **The numerical contract.** Every signal, strike rule, size, cost, exit and limit |
| [`05-ui-spec.md`](05-ui-spec.md) | The web app's **Options** tab (read-only hub) and the desk's `/nifty-options` operator page |
| [`06-module-plan.md`](06-module-plan.md) | **The task list.** OP0–OP15, scans first, OC modules folded in |
| [`07-data-reality.md`](07-data-reality.md) | Where intraday option prices can and cannot come from; the evidence tiers per sleeve |
| [`DECISIONS-OP.md`](DECISIONS-OP.md) | Judgement calls; the pack's own are PACK.1–PACK.14 |
| [`QUESTIONS.md`](QUESTIONS.md) | What only Maulik can answer, each with the standing default the run uses |
| [`STATUS.md`](STATUS.md) | The live status page — all ⬜ |

## The one-paragraph version

Baskfy already owns every part of an options desk except the options: a Kite provider behind a
shared 3 req/s limiter with a `quote()` path that batches 500 symbols a call, an execution gateway
that is the only path to an order and that already refuses any derivative venue without
`OPTIONS_ENABLED`, MIS without `INTRADAY_ENABLED`, and any option under a carry product
(`baskfy_execution.guards.assert_not_overnight_option`); a `TickBus`, a desk process model the
swing opening-range monitor already runs on; the plan → `plan_id` → `confirm=true` shape with a
thirty-minute expiry; per-sleeve execution flags that journal `simulated=true` whatever `DRY_RUN`
says; the `/meta/live-marks` live-overlay contract (21 Sep 2026); a read-only web hub pattern; and a
complete, never-started pack for one hedged monthly-expiry iron condor (`docs/condor/`). This run
adds the **NFO contract master** and the **expiry calendar** (from the master, never from a weekday
rule), a **chain collector** (one `quote()` a minute for the strikes that matter, with depth and
OI), **greeks computed locally** (Black-76 on a put–call-parity forward — Kite gives no greeks),
**three sleeves' scans** that show today's candidates on the web — **O1** hedged premium selling
(the condor, monthly *and* a separately-configured weekly variant, expiry day only), **O2**
directional buying (opening-range breakout with a daily trend filter → one step ITM on the nearest
non-expiring weekly), **O3** expiry-day setups (two defined-risk debit-spread triggers) — then
**plans**, **paper execution** through the real gateway's dry-run branch with a depth-ladder fill
simulator, **exits** (every position flat by 14:30 / 14:45 / 15:00 by sleeve), a **risk ledger** in
₹ and R, a **journal per sleeve**, and the **backtest tiers** the data allows. Every live flag stays
false; the web app never gains an execute route; no options sleeve can confirm its own entry.

## How this pack relates to `docs/condor/` — condor becomes sleeve O1

The condor pack (9 Sep 2026) was written for one monthly condor and never started (`docs/condor/
STATUS.md`: "Run state: not started"). This pack **absorbs** it rather than competing with it:
the OC run is not to be started; the OP run builds O1 as the condor, plus O2 and O3 beside it on
shared plumbing.

**Still authoritative (read them; this pack does not restate them):**

| Condor doc | Authoritative for | Notes |
|---|---|---|
| `01-method.md` §1, §3–§6 | O1's thesis, gate, structure, exits, sizing arithmetic | Now **O1-M** (monthly). O1-W inherits the same method, configured separately (`04` §3 here) |
| `04-business-rules.md` §2 (gate), §3.2–3.4 (quote, delta, liquidity), §4 (structure, sequences), §7 (exits), §9 (state machine) | O1's numerical contract | Module paths move from `baskfy_core.condor` to `baskfy_core.options.condor`; config groups gain a `variant` (`MONTHLY`/`WEEKLY`). §3.3's delta model is replaced by `04` §2.3 here (Black-76 on the parity forward) |
| `07-data-reality.md` | The tier model and its caveat text | Extended per sleeve in `07` here; the Tier 2 caveat stays verbatim |
| `DECISIONS-OC.md` PACK.1, PACK.4–PACK.7 | Port-by-reimplementation, delta ∧ range, confirm covers exits, labelled models, tunables in config | Carried forward as this pack's PACK.2, and applied to all three sleeves |

**Overridden by this pack (the condor doc is the stale half; each file gets a header note):**

| Condor section | Overridden by | Why |
|---|---|---|
| `02` Track C §4 "No weekly expiries" and `DECISIONS-OC` PACK.3 | `02` here, PACK.1 | Maulik's 22 Sep 2026 instruction asks for weekly and monthly. The condor doc described intent on 9 Sep; his instruction is the later fact (CLAUDE.md, "the decision wins") |
| `01` §2 / `DECISIONS-OC` PACK.2 — BANKNIFTY built dark | `02` Track C here | His answer was *NIFTY index options*. BANKNIFTY is not built dark in this run; the code stays underlying-agnostic so adding it later is a config row and a decision |
| `03-data-model.md` (`oc_` schema) | `03` here (`op_` schema) | One schema for three sleeves (PACK.4). Also a factual correction: condor `03` says `instrument` carries `expiry`/`strike`/`instrument_type` option rows; it does not — `instrument_type ∈ {EQ, ETF, INDEX}` (`models/reference.py`), so the NFO master is its own table |
| `05-ui-spec.md` routes `/condor` | `05` here | One Options tab and one desk page for all sleeves; the desk route is `/nifty-options` because `/options` is the frozen lab's thaw hook (PACK.9) |
| `06-module-plan.md` OC0–OC12, `KICKOFF-PROMPT.md`, `STATUS.md` | `06`, `KICKOFF-PROMPT.md`, `STATUS.md` here | The OC modules are folded into OP modules (`06` has the map) |
| `04` §5.1 STT rate 0.1 % | `04` §6 here | The rate may be stale (Union Budget 2026); OP0 verifies — see `04` §6 |
| `04` §6 sizing from a flat ₹ risk budget | `04` §7 here | Per-sleeve capital starting at ₹0 (the TWT pattern) with risk as % of it; the condor's ₹ ceilings survive as env ceilings |

## The key mapping

| Concept | Baskfy implementation |
|---|---|
| Which days are expiries, weekly vs monthly | `baskfy_core.options.calendar` over `op_expiry`, rebuilt nightly from `instruments("NFO")`; monthly = the last NIFTY expiry in the calendar month; a holiday-shifted Tuesday is whatever the master says |
| The chain, IV and greeks | Collector → `op_chain_snapshot` (bid/ask/depth/OI each minute); `options.greeks` = Black-76 on the ATM put–call-parity forward, IV by bracketed solver |
| O1 hedged premium selling | `options.condor` — the condor pack's gate/structure/exits, variants `MONTHLY` and `WEEKLY` |
| O2 directional buying | `options.directional` — 15-min opening range + daily trend filter → long CE/PE one step ITM on the nearest weekly that does not expire today |
| O3 expiry-day setups | `options.expiry_setups` — O3-A range-break debit spread, O3-B gap-hold debit spread |
| Today's candidates on the web | `op_scan` rows written each minute by the worker; the **Options** tab beside Overlap / Swing / Volume / Tight |
| Plan, confirm, paper fill | Desk `/nifty-options`; `POST /nifty-options/execute {plan_id, confirm:true}`; the gateway's dry-run branch; depth-ladder simulator; `op_fill.simulated=true` |
| Never overnight | `assert_not_overnight_option` (unchanged) + each sleeve's hard exit (`04` §3–§5) ≤ `BASKFY_OPTIONS_HARD_EXIT_LATEST` [15:00] |
| Real money | Four-way AND per sleeve: `DRY_RUN=false` ∧ `OPTIONS_ENABLED` ∧ `INTRADAY_ENABLED` ∧ `BASKFY_OPTIONS_<SLEEVE>_EXECUTION_ENABLED` — all false, flipped only by Maulik |

## What this run is not

Not the strangle lab thawed (D4: `frozen/strangle/` is read for lessons, never imported, moved or
edited). Not an overnight book. Not naked short options. Not stock options, not BANKNIFTY, not
SENSEX. Not auto-execution: the swing sleeve's SW25/SW26 exception and TWT's TW17 are theirs and
do not extend here. Not a claim of profitability — `01` §0 says why, with SEBI's numbers.

**The run's product test:** on any trading morning Maulik opens **Options** on his phone and sees,
per sleeve, whether today is a day it trades, what it is waiting for, and the exact candidate it
would take — priced from the live chain, with IV and delta, lots and max loss; on the desk one click
confirms a paper plan; the simulated fills, the exit and the flat-by-close all happen; and the
journal tells him, per sleeve, in ₹ and R and apart from every other sleeve, what the paper record
says.
