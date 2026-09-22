# DECISIONS-OP — judgement calls of the options run

Same convention as `docs/DECISIONS-MERGE.md`, `docs/condor/DECISIONS-OC.md` and
`docs/swing/DECISIONS-SW.md`: numbered by module; context, the choice, the rejected alternatives,
the reversal path. Decisions not reviewed by Maulik are tagged **⚠ UNREVIEWED**. The pack's own,
taken 22 Sep 2026 so the run does not stall, are PACK.1–PACK.14. Maulik's four answers of 22 Sep
2026 (README) are **decisions**, not proposals, and are not tagged.

## PACK.1 — Weekly expiries are in scope; condor Track C §4 and PACK.3 are the stale half

**Context.** `docs/condor/02` Track C §4 ("No weekly expiries") and `DECISIONS-OC` PACK.3
("weekly Tuesdays are refused, not skipped") were written 9 Sep 2026 for one monthly condor. On
22 Sep 2026 Maulik asked, in session, for "options of Nifty 50 for weekly options and monthly
options expiry". **Choice.** His instruction is the later fact; the condor text is the stale half
(CLAUDE.md, "the decision wins"). Weekly expiries are traded by O1-W and O3 and underlie O2. The
condor files are **not** edited in substance or deleted: `docs/condor/README.md` and `STATUS.md` get a
header note pointing here, and condor `02` §4 and PACK.3 stay readable as history. **Reversal.** Set
`role()` for O1-W and O3 to never trade; the monthly-only book is then exactly condor's.

## PACK.2 — Condor's PACK.1, PACK.4, PACK.5, PACK.6, PACK.7 carry forward to all sleeves · ⚠ UNREVIEWED

Port the frozen lab by re-implementation, never thaw or import (PACK.1); O1's short strike must
satisfy delta **and** range (PACK.4); **one confirm covers the rule-driven exits and the hard exit**
(PACK.5) — now for O2's stop/target/time stop and O3's exits too, because an intraday option left
open because nobody clicked is the risk the method forbids, and the broker's MIS square-off is later,
at market, and not the plan; a model backtest is labelled a model (PACK.6); strategy thresholds are
config defaults, not form fields (PACK.7). Rejected: a second confirm for exits (see PACK.5's own
reasoning). Reversal: per condor's entries.

## PACK.3 — No auto-execute for any options sleeve, and no flag for one

Maulik's answer: "No auto-execute for any options sleeve." There is no `*_AUTO_EXECUTE` flag, not
even defaulted false, because a dark flag is an invitation; OP13 scans for the name. The swing (SW25/
SW26) and TWT (TW17) exceptions are his for those books and CLAUDE.md forbids an agent to add another.
Reversal: only Maulik, in writing, and it would be a new decision entry, not a flag flip.

## PACK.4 — One `op_` schema for all sleeves; the NFO master is its own table · ⚠ UNREVIEWED

The `oc_` schema was never migrated, so nothing is renamed. Three sleeves share the master, chain,
bars, calendar, session machine, plan/leg/fill shape and journal; a `sleeve` enum column keeps them
apart. Rejected: per-sleeve prefixes (the chain triplicated or cross-prefix joins); reusing
`instrument` for option rows (its `instrument_type` is `EQ|ETF|INDEX` and every screen/listing query
reads it — condor `03` was wrong to assume otherwise). Reversal: views per sleeve over the shared
tables cost nothing.

## PACK.5 — O2's shape: 15-minute OR, NIFTY 50 EMA20 trend, one step ITM, nearest non-expiring weekly · ⚠ UNREVIEWED

**Opening range 15 min** (the desk's own Strategy B used 15; 5 min is noise on an index, 30 min
leaves too little day). **Trend = previous close vs 20-day EMA of NIFTY 50** — simple, daily, fixed at
the open so the direction cannot flip intraday; deliberately *not* the swing gate's MidSmall 400
(SW17 is for a mid/small-cap book). **One step ITM** (~0.55–0.65 Δ): less theta share and a tighter
spread-to-premium than ATM, cheaper than deep ITM; rejected ATM (more theta per rupee, delta 0.5 means
half the move) and OTM (lottery tickets, the SEBI loser profile). **Nearest weekly not expiring today**:
0-DTE buying is O3's domain and defined-risk there; rejected monthly (half the gamma, more vega).
**Exits 30 % / 60 % / 45-min time stop / 15:00** — 2:1 reward-to-risk with a time stop because a
buyer who waits pays theta; rejected the desk research's 25 % stop + trailing (more parameters,
same idea; revisit with Tier 3). Reversal: every number is a config field.

## PACK.6 — Sleeve capital starts at ₹0; paper runs one lot and measures in R · ⚠ UNREVIEWED

TWT's rule (a sleeve at ₹0 plans nothing) would leave paper with no trades until Maulik sets
capital. So in `PAPER` mode a ₹0 sleeve plans **one lot** (`sizing_mode=PAPER_ONE_LOT`) and R =
the one-lot risk; in `LIVE` mode ₹0 is `REJECTED_NO_SLEEVE_CAPITAL`. The journal never pools the two
modes. Rejected: a made-up paper capital (a number Maulik didn't choose would look like his);
blocking paper on capital (starves the evidence). Reversal: set capital; the next plan sizes from it.

## PACK.7 — O1-W trades the weekly expiry day only, not the day before · ⚠ UNREVIEWED

Intraday-only on 1-DTE captures only the day's slice of a premium whose largest decay is overnight,
while still carrying high gamma; credit-to-cost is worse and no sample is gained. The collector
records Monday chains so Tier 3 can test the variant on observed prices. Rejected: Monday+Tuesday
(two positions on one weekly contract and double the exposure to one week's move). Reversal: a
`condor_weekly.trade_days_before_expiry` field, default 0.

## PACK.8 — Greeks: Black-76 on the put–call-parity forward, calendar time · ⚠ UNREVIEWED

Kite gives no greeks. Black-76 on the ATM parity forward absorbs dividends and the rate without a
dividend model; calendar-time `T` is consistent across 0-DTE and 7-DTE and matches how the market
quotes theta. Rejected: Black–Scholes on spot with a flat rate (condor `04` §3.3 — biased by
dividends and the futures basis on multi-day O2 contracts); trading-time `T` (better intraday on
0-DTE, but a second convention to explain; revisit if Tier 3 shows delta bias). Every snapshot row
records `greeks_model`. Reversal: a new model version; old rows keep theirs.

## PACK.9 — Desk route `/nifty-options`; web tab `/options` · ⚠ UNREVIEWED

The desk's `app/main.py` still owns `/options`, `/options/run`, `/options/data` for the frozen lab's
thaw (404 today because `OPTIONS_ENABLED` is false — i.e. they would come alive on exactly the flip
this book needs). Reusing the path would collide on thaw or on the flip. The web app has no
`/options` route, so the tab takes it. Rejected: `/condor` (names one sleeve of three); `/fno`
(implies futures and stocks). Reversal: a redirect.

## PACK.10 — O3 = two debit-spread setups, and one expiry-day slot shared with O1 · ⚠ UNREVIEWED

"Expiry-day setups" defined concretely as O3-A (60-min range break with ER ≥ 0.40) and O3-B (gap ≥
0.5 % that holds half the gap to 09:45), both ATM/100-point debit spreads on the expiring contract,
14:45 exit. Debit spreads because they are defined-risk, cheaper than a naked long on 0-DTE, and a
spread that is right early moves quickly toward full width. O1 wants quiet days, O3 wants breaking
ones; one slot per expiry day keeps the book from holding a short-gamma condor and a long-gamma
spread on the same afternoon. Rejected: 0-DTE naked longs (the STT trap and the SEBI loser profile);
short-premium expiry setups (that is O1); more than two setups (samples are scarce). Reversal: a
setup is a config group; disable by `enabled=false`.

## PACK.11 — Scans run in the worker from the database; the desk's own bars decide plans; scan/collector flags are operational · ⚠ UNREVIEWED

The web tab reads `op_scan`, computed each minute by the worker from the collector's snapshot and
`op_index_minute` — so the tab adds no Kite load and works whether or not the desk monitor is on.
The desk computes the same functions from tick-built bars for **plans**, and its numbers are
authoritative; the tab labels itself advisory. `BASKFY_OPTIONS_COLLECT_ENABLED` and
`BASKFY_OPTIONS_SCAN_ENABLED` move no money (like TWT's nightly flag); OP3 may set their defaults to
true after the limiter measurement, recorded here. Rejected: the web calling Kite directly (limiter
contention, a second quote path); the desk writing scans (couples the tab to the desk being up).
Reversal: the flags.

## PACK.12 — BANKNIFTY and every other underlying are Track C in v1 (supersedes condor PACK.2)

Maulik's answer was NIFTY index options. Condor built BANKNIFTY dark; this pack does not build it at
all — the code is underlying-agnostic and the config refuses anything but `NIFTY`. Reversal: a config
value and a decision entry.

## PACK.13 — Per-sleeve paper periods and loss limits in R · ⚠ UNREVIEWED

Paper periods scale with how often each sleeve trades (O1-M 6 monthlies, O1-W 12 weeklies, O2 60
sessions, O3 20 expiry days, each with a minimum traded count) — equal calendar time would give O1-M
a sample of 3 and O2 of 120. Loss limits are in R per sleeve (day 2R, week 4R, month 8R) because R
exists in paper-one-lot mode too, so the ledger is exercised before any capital exists; book-level ₹
limits apply once capital is set. Rejected: condor's flat ₹ limits for all sleeves (meaningless at ₹0
capital). Reversal: config fields.

## PACK.14 — The flag flip is not pre-delegated; the `OPTIONS_ENABLED` side door must be closed first · ⚠ UNREVIEWED

Condor PACK.8 carried. `OPTIONS_ENABLED` and `INTRADAY_ENABLED` change what the whole desk may do,
and `OPTIONS_ENABLED` also wakes the frozen lab's deferred hooks; §3 of `02` therefore requires
OP13's side-door test before any flip and Maulik's own hand for it. Reversal: his line in
NEEDS-MAULIK § Options.

---

# OP0 — Baseline, read-in, verified facts (22 Sep 2026)

## OP0.1 — Cost rates verified from primary sources; `04` §6 edited (facts, not judgement)

**Context.** `04` §6 carried the condor pack's STT (0.1 % premium / 0.125 % exercise) with a ⚠, the
author suspecting a 2026 Budget change. **Found (read 22 Sep 2026):**

| Rate | Value | Source |
|---|---|---|
| STT, sale of an option | **0.15 % of premium** (was 0.1 %), transactions on/after **1 Apr 2026** | Memorandum Explaining the Provisions in the Finance Bill, 2026, "Increase in tax rates of Securities Transaction Tax", Clause 143, p. 63 — https://www.indiabudget.gov.in/doc/memo.pdf ; corroborated by https://zerodha.com/charges/ ("0.15% on sell side (on premium)") |
| STT, option exercised | **0.15 % of intrinsic** (was 0.125 %), same date | same memorandum; Zerodha: "0.15% of the intrinsic value on options that are bought and exercised" |
| (STT, futures — not traded here) | 0.05 % (was 0.02 %) | same memorandum |
| NSE transaction charge, options | **0.03553 % of premium** (was 0.03503 %), from 1 Mar 2026 | https://zerodha.com/charges/ ; NSE circular NSE/FA/73061, 27 Feb 2026 (https://nsearchives.nseindia.com/content/circulars/FA73061.pdf) — its text rolls the IPFT contribution back to the pre-Apr-2023 level and revises transaction charges from 1 Mar 2026; the circular's table itself could not be machine-read, so the numbers are Zerodha's |
| NSE IPFT, options | **₹0.01 per crore of premium (+GST)** | Zerodha charges page, verbatim; consistent with FA73061's roll-back (+₹49.99 moved into the transaction charge: 0.03503 % → 0.03553 %) |
| Brokerage | ₹20 flat per executed F&O order | Zerodha charges page |
| SEBI fee / stamp | ₹10 per crore / 0.003 % buy side | Zerodha charges page |
| GST | 18 % on brokerage + SEBI + transaction charges | Zerodha charges page |
| MIS auto square-off, F&O | **15:26 IST**; **₹50 + 18 % GST per order** squared off | https://support.zerodha.com/category/trading-and-markets/trading-faqs/market-sessions/articles/intraday-auto-square-off-timings |

**Choice.** `04` §6 now carries 0.15 / 0.15 / 0.03553 / 0.01 with "verified OP0". OP1's `CostRates`
docstring must carry these URLs and "read 22 Sep 2026". Every hard exit (≤ 15:00) sits 26 minutes
before the broker's square-off, so `auto_squareoff_inr` stays unreachable. **Reversal.** A later
Budget or circular is a new dated `CostRates` row; the 90-day review warning catches drift.

## OP0.2 — NIFTY's Tuesday expiry: the primary source

NSE circular **NSE/FAOP/68747, 25 Jun 2025** ("Revision in Expiry Day of Index and Stock Derivatives
Contracts – Update", modifying 68685 of 23 Jun 2025): NIFTY weekly "Thursday of the week → Tuesday of
the week"; NIFTY monthly/quarterly/half-yearly "Last Thursday → Last Tuesday"; newly generated
contracts expiring on/after **1 Sep 2025** carry the Tuesday expiry
(https://nsearchives.nseindia.com/content/circulars/FAOP68747.pdf). Corroboration from the live
account: the order book of 21 Sep 2026 (OP0.4) holds `NIFTY26922…` contracts — expiry 22 Sep 2026, a
Tuesday. **This does not license a weekday rule** (`04` §1.1): the calendar still reads the master;
the circular is why a Tuesday-moved-to-Monday fixture is the right test.

## OP0.3 — Live Kite reads (a)(b)(c)(e)(f) are deferred, not guessed · ⚠ UNREVIEWED

**Context.** `06` OP0 asks for rate-limited read-only calls: earliest minute history for NIFTY 50 and
INDIA VIX, an expired weekly's absence and error text, the next eight expiries from
`instruments("NFO")` (weekday, `lot_size`, `tick_size`, strike step, which is monthly), `quote()` on
five options (fields, depth levels, OI unit), `basket_order_margins` on a fixture condor. This session
has no Kite session (the dev Mac has no `data/.kite_token.json`; the agent was told not to log in).
**Choice.** Record each as **pending** in STATUS with the exact call, rather than write down a value
from memory. What is known from the code and Kite Connect's docs (not live-verified) is recorded as
such: the instrument dump's columns (`instrument_token, exchange_token, tradingsymbol, name,
last_price, expiry, strike, tick_size, lot_size, instrument_type, segment, exchange`), `quote()` at
≤ 500 instruments per call (`QUOTE_BATCH_SIZE = 500`, `baskfy_providers/kite.py:76`) with five depth
levels a side and `oi`, `oi_day_high`, `oi_day_low`, `timestamp`, `last_trade_time`, and minute
history served ≤ 60 days per request. **Lot size is not recorded as a number** — it is read from the
master (`04` §1.4); a literal belongs only in fixtures. **Nothing downstream is blocked:** OP1 is pure
arithmetic over fixtures; OP2's `op_contract` columns follow the documented dump and are checked the
first night the loader runs; OP3 performs (a)–(f) as its first Kite-session act and fills STATUS.
**Rejected.** Using the box's token (not this session's to use); writing remembered values (a
number in STATUS is believed — CLAUDE.md's 11 Sep lesson). **Reversal.** Any agent or Maulik with a
Kite session runs the six calls and replaces "pending" with the answers.

## OP0.4 — The F&O segment is active: verified, not an open question

The coordinating session's read-only `get_orders` on **21 Sep 2026** showed filled (`COMPLETE`) NFO
orders at 14:42 IST — `NIFTY2692223700CE`, `NIFTY2692223200PE`, `NIFTY2692223750CE`,
`NIFTY2692223150PE`, product `MIS`. Filled NFO orders mean the segment is active on the account the
desk trades; Maulik also answered "yes" in session on 22 Sep 2026. It is recorded as fact in STATUS
and **not** raised in NEEDS-MAULIK. The margin pool and `basket_order_margins` for a real basket
remain his (§3.5).

**A consequence for later modules.** Those orders were placed **outside Baskfy** (Kite basket
orders, by hand) — the account already carries manual intraday NIFTY option trades, here a
condor-shaped four-leg basket with 50-point wings. Track C §8 is therefore concrete, not
theoretical: OP9/OP11's reconciliation must identify the options book's own positions by its
`client_id`/order tag and never read, mark, count against a limit, or close a position it did not
open. OP13 should hold a test with a foreign NFO MIS position present in the broker fixture.

## OP0.5 — `OPTIONS_ENABLED` side door: the "stays dark" comments are not true for `/ops` · ⚠ UNREVIEWED

**Found** (read-only; the probe set the attribute in one throwaway process, as the desk's own tests
do — no env file touched, no network): with `C.OPTIONS_ENABLED = True`,
`app.analytics.ops._options_operations()` returns **five strangle operations**
(`strangle_check`, `strangle_collect`, `strangle_calibrate`, `strangle_calibrate_write`,
`strangle_session`) and `by_name()` includes them. Its comment says it returns nothing until the
subsystem is thawed, but the guard is an `ImportError` on `strategies.strangle.instruments` — one of
the four modules M6 deliberately left in the live tree — so the import succeeds. The operations would
appear on `/ops` and, when started, spawn `python -m scripts.strangle`, which is frozen and would fail
with `ModuleNotFoundError`. Nothing reaches a broker (the lab was paper-only and its code is absent),
but the desk would show controls that cannot work. The autorun loop stays dark only **by accident**:
`_observed_today` imports `strategies.strangle.calibrate` (frozen), so the loop raises the caught
`ImportError` after 09:15; before 09:15 the entry-window veto returns nothing. Full inventory with
file:line in STATUS.

**Choice.** OP0 records it and changes no desk code (inventory module; D4 and `02` put the
side-door test in OP13). **Recommended for OP13:** gate `_options_operations()` and the autorun
options loop on the presence of a *frozen* module (`find_spec("app.strategies.strangle.book")`,
the same probe `tests/_frozen.py:21` uses) rather than on `instruments`, and add the test `02`
names. **Reversal.** n/a — nothing changed.

## OP0.6 — The gateway's product gate: `OPTIONS_ENABLED` alone admits MIS and NRML-futures on derivative venues · ⚠ UNREVIEWED

**Found.** `baskfy_execution.guards.product_exchange_refusal` (`guards.py:76–108`, AFE `8cbe278`,
12 Sep 2026) returns `""` for **any product** on a `DERIVATIVE_EXCHANGES` venue once
`options_enabled` is true (`guards.py:91–94`) — the MIS branch that demands `intraday_enabled` is only
reached for cash venues. So with `OPTIONS_ENABLED=true` and `INTRADAY_ENABLED=false`, the desk's
default gateway (`app/core/gateway.py:24–30`, which the weekly book uses) would pass an **NFO MIS**
order, and an **NRML future** on NFO/BFO/MCX/CDS (`assert_not_overnight_option` blocks only
*options* under carry products). Non-negotiable 5 says "MIS needs `INTRADAY_ENABLED`"; `02`'s
"four flags at once" is true only because `options_gates()` (OP2) will AND them for the options book.
Swing, TWT and VBT pin `options_enabled=False` and are unaffected. `git log -S` shows no decision
saying derivative-venue MIS should skip the intraday flag — AFE's intent was to close the MCX/NRML
hole, and this is a residual of the same shape.

**Choice.** Not fixed in OP0 (a change to the gateway's gate is a non-negotiable surface and needs
its own tests; OP0 is an inventory). **Recommended for OP2**, beside `options_gates()`: MIS needs
`intraday_enabled` on every venue, and a derivative venue admits only `MIS` (futures are Track C
anyway) — the stricter boundary with nothing in force changing, since both flags are false
everywhere today. Until then **no one may set `OPTIONS_ENABLED=true`** on the desk — already true
(PACK.14), now with a second reason. **Reversal.** n/a — nothing changed.

## OP0.7 — `.env.example`'s `BASKFY_CONDOR_*` block is read by nothing

`.env.example` (lines ~254–271, added in `bd3f32f`) declares `BASKFY_CONDOR_EXECUTION_ENABLED`,
`_MONITOR_`, `_CHAIN_COLLECT_`, `_BANKNIFTY_` and six ceilings; `grep` finds no reader in either tree.
OP2 replaces the block with `02`'s `BASKFY_OPTIONS_*` names (all false; BANKNIFTY dropped, PACK.12).
Left untouched in OP0 so the rename lands with the settings that read it.

## OP0.8 — Zerodha's retail-algo rules shape OP10, not OP0

Zerodha's own write-up (https://inthemoneybyzerodha.substack.com/p/sebi-algo-trading-changes-april-2026,
read 22 Sep 2026), effective **1 Apr 2026**: a **static IP** is mandatory for API orders (a primary
and a backup); below **10 orders/second** no strategy registration is needed (the options book peaks
at a handful per minute); API orders are tagged and treated distinctly by the exchange; and a
**MARKET order must carry non-zero market protection**. `04` §8.1 already forbids MARKET entries; §8.2's
third, marketable exit attempt must therefore stay a **LIMIT at the touch**, never a MARKET order, in
OP10. Whether Zerodha requires anything of the user for the algo-ID tag (versus stamping it itself)
is not stated in that source — recorded unverified in NEEDS-MAULIK § Options O4 for `02` §3.5.

## OP0.9 — Baseline suites: counts collected and targeted runs green; full runs not repeated · ⚠ UNREVIEWED

The coordinator's standing memory constraint for this Mac (≤ 2 agents; run the full desk suite only
if code changed) applies; OP0 changed no code. Recorded instead: collected counts (desk 2,123;
screener tree 9,071), the gate-and-guard files in both trees run green (desk 143 + 12 subtests;
`packages/execution/tests/test_non_negotiables.py` 25), and the last full green recorded in
`docs/00-merge-status.md`. **Reversal.** `tools/ci-local.sh` at the start of OP1 gives the full
baseline before the first code change.
