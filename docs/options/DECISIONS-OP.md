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

---

# OP1 — The shared pure core `baskfy_core.options` (22 Sep 2026)

## OP1.1 — What OP1 builds and what it leaves to OP4 · ⚠ UNREVIEWED

**Context.** `06` OP1 lists `config`, `calendar`, `greeks` + `chain`, `costs`, `sizing`, `session`,
`risk`, `journal`, `execution` ("§8's pure decisions") and `backtest` ("engine only"); OP4 lists the
sleeves' `condor`, `directional`, `expiry_setups` and `scan`. **Choice.** OP1 holds everything the
three sleeves share: the full config (every `04` field, including the sleeves' thresholds, because
`04` says every number is a field of one `OptionsConfig`), and in `execution` only the *shared*
exit plumbing — `first_by_precedence`, `drop_on_stale` (§8.5), `feed_lost` — never a sleeve's own
exit rules, which are OP4's (`06`: "OP4 (O1 signal/structure/exits)"). The backtest engine takes a
sleeve's day function as an argument. **Rejected.** Writing the sleeves' exit evaluators now (they
belong with the signals they read, and OP4's fixtures test them). **Reversal.** Move a function.

## OP1.2 — `04` gains §14, the field table, asserted both ways · ⚠ UNREVIEWED

**Context.** `06` OP1 asks `test_options_docs_parity.py` to prove "every config field name appears
in `04`". Several fields have no bracketed name in §1–§13 (the clock fields — `observation_start`,
`o3a_window_start` — the IV solver bounds, `greeks_model`, `oi_unit`, `plan_ttl_minutes`). **Choice.**
The VBT pattern (`test_vbt_docs_parity.py`): `04` §14 lists every `group.field` with its default,
regenerated from the code and asserted **both ways**, plus a prose check that §1–§9's load-bearing
bracketed numbers are still stated. This edits `04` by *adding an inventory*, not by changing any
number. **Rejected.** A name-appears scan (weaker: a re-valued default would pass). **Reversal.**
Delete §14 and the two table tests.

## OP1.3 — Kite's OI unit is a field, defaulting to `UNITS`, unverified · ⚠ UNREVIEWED

**Context.** `04` §2.4 says the OI floor's conversion follows the unit OP0 records; OP0's read (e) is
pending a Kite session (OP0.3). **Choice.** `chain.oi_unit` ∈ {`UNITS`, `LOTS`}, default `UNITS`
(Kite Connect reports F&O `oi` in shares/units as far as its documentation shows — **not
live-verified**); `oi_in_units()` does the conversion and a test covers both. **OP3 must verify it
with its first `quote()` on NFO** and, if Kite counts lots, change the default here and in §14.
**Rejected.** Assuming lots (would admit strikes with 1/65th of the required OI). **Reversal.** One
default.

## OP1.4 — Sizing details `04` §7 leaves open · ⚠ UNREVIEWED

(a) The **first-live multiplier** applies to every `BUDGET` sizing, paper or live, while fewer than
five `simulated=false` rows exist — so a paper plan with capital set shows the half size a live plan
would take; it never applies to `PAPER_ONE_LOT` (one lot is already the floor). (b) The core also
clamps `risk_per_trade_pct` at `RISK_PCT_MAX` (the settings form refuses above it in OP2; the core
does not trust that). (c) Both O1 margin figures (hedged and transient) reject as
`REJECTED_MARGIN` — `04` §7.4's code — with the message naming which; condor's
`REJECTED_MARGIN_TRANSIENT` is the stale half. **Reversal.** Each is one line in `sizing.py`.

## OP1.5 — Execution: the latency penalty, the third attempt, and the cancel · ⚠ UNREVIEWED

**Context.** `04` §8.4: "adding `latency_ticks` [1] of adverse price per level consumed beyond the
first"; §8.2: the third attempt on a risk-reducing close is "marketable at the touch". **Choice.**
(a) Each depth level after the first fills `latency_ticks` worse than its quoted price (a flat
penalty per extra level), and a level whose penalised price is beyond the limit stops the walk; the
rest is a partial. (b) Attempt 3 is a **LIMIT at the current far touch** (ask for a buy-back, bid for
O2's sell) flagged `marketable` — never a MARKET order (OP0.8's market-protection rule). (c) Any other
leg is cancelled after its second wait (`next_attempt` returns `None`). **Rejected.** A cumulative
penalty (level *k* +*k* ticks — harsher, and the text's "per level" reads as flat); a MARKET order.
**Reversal.** Tier 3 / live fills calibrate `latency_ticks`; the shape is two lines.

## OP1.6 — The journal never pools `(sleeve code, simulated, sizing_mode)`; O3A and O3B are separate · ⚠ UNREVIEWED

`summarize` keys by the `op_sleeve` code, so O3-A and O3-B get separate summaries although they share
config, flags and a paper period (a pooled O3 number would hide which setup carries it); the page can
show both side by side. `summarize_one` raises on mixed rows. The risk ledger likewise refuses rows of
another sleeve. **Reversal.** Key by `group_of(sleeve)`.

## OP1.7 — Risk-ledger details · ⚠ UNREVIEWED

Realised R over a week or month is the sum of each row's own `r_multiple` (each trade against the R
it was taken at); the daily test divides today's realised + marked ₹ by today's R. The weekly pause
runs to the ISO week's last trading day **from the caller's trading calendar** (the core owns no
calendar); monthly to the calendar month's last day. Book pauses carry `BOOK_DAILY_INR` /
`BOOK_MONTHLY_INR` (`04` §9.3 names no code). **Reversal.** Local to `risk.py`.

## OP1.8 — Numbers: greeks are floats, money is `Decimal`, costs round per component · ⚠ UNREVIEWED

Greeks and IV are model outputs stored as `numeric(10,6)` rounded at write (OP3), so they are
computed in `float` (`math.erf`); every premium, strike, charge and budget is `Decimal`. Delta is
Black-76's forward delta (`e^{-rT}N(d1)`, so call − put = `e^{-rT}`), theta per calendar day, vega
per vol point. `charges()` rounds each component to the paisa half-up, as a contract note does, and
the total is the sum of the rounded components. **Reversal.** A new `greeks_model` version.

## OP1.9 — The Tier 2 caveat stays verbatim although it says "Black–Scholes"

`04` §13.2 requires condor `07` §4's caveat **verbatim**, and it names Black–Scholes while this pack's
model is Black-76 on the forward (PACK.8; for Tier 2 the forward is spot, so the two coincide up to
the discounting). Verbatim wins; the test asserts the string is in condor `07`. The source spells the
en dash as `–` only because ruff's confusable-character rule forbids the literal. Changing the
wording is Maulik's edit to `07`, then here.

## OP1.10 — Lint: per-function `noqa: PLR0913` with a reason, the house convention

Pure functions take every input explicitly (law 1 puts `now` and the config in the signature), so
eleven exceed ruff's five-argument limit; each carries `# noqa: PLR0913 - <why>` as `factors.py` and
`momentum_scan.py` do, and the rest are keyword-only. No per-file blanket ignore was added.

## OP1.11 — Mutation: `baskfy_core.options` joins the harness; 89.6 %, every survivor justified · ⚠ UNREVIEWED

**Context.** `06` OP1: "Mutation harness at the `factors` threshold; score recorded." The threshold
the harness enforces is Prompt 19 §6's "fix or justify every surviving mutant" (swing's last recorded
score is 88.2 %). **Choice.** `tools/mutation.py` gains `OPTIONS_TARGETS` (ten modules; `config.py`
excluded for swing's reason) with a per-module primary test file. The first run scored **74.5 %**
(415/557); the survivors showed real gaps — `role()`'s `trades` flag never asserted, sells *at*
their limit, a scratch trade counted as a win, Monday rows, last month's losses, input-order ties,
`frozen=True` on every dataclass — so `test_options_edges.py` (68 tests) and three calendar tests
were added, each asserting a `04` rule at its boundary. The second run: **499/557 killed, 89.6 %**;
the 58 survivors are each justified in `reconciliation/mutant-justifications.json` (25 are
`slots=True` flips, the rest measure-zero float boundaries, sub-paisa sums, skip-row defaults that
no statistic reads, and two exit-rank ties decided by string hashing). Report:
`decile-blueprint/reconciliation/MUTANTS-options.md` (its own file, so swing's `MUTANTS.md` is not
overwritten by a partial run). **Reversal.** Remove the targets.

---

# OP2 — Schema, the NFO master, settings, `options_gates()` (22 Sep 2026)

## OP2.1 — The gateway's product gate: a derivative venue admits MIS only, and MIS needs `INTRADAY_ENABLED` there too · ⚠ UNREVIEWED

**Context.** OP0.6: `product_exchange_refusal` returned `""` for any product on a
`DERIVATIVE_EXCHANGES` venue once `options_enabled` was true, so `OPTIONS_ENABLED` alone admitted NFO
MIS without `INTRADAY_ENABLED` and NRML futures — against non-negotiable 5 ("MIS needs
`INTRADAY_ENABLED`"). `06`'s preamble says "nothing in this plan edits the gateway's product gates";
that sentence was written before OP0 found the hole and is the stale half for this one change (the
coordinator carried OP0.6 into OP2 by instruction). **Choice.** On a derivative venue: no
`options_enabled` → the existing "F&O/derivatives disabled" refusal; any product but `MIS` → refused
("only MIS is allowed on a derivative venue … nothing is carried past the close"); `MIS` without
`intraday_enabled` → "MIS/intraday disabled". `assert_not_overnight_option` is unchanged and still
refuses an option under a carry product first. The cash branch is untouched. Tests first:
`packages/execution/tests/test_derivative_product_gate.py` (every product × both switches × every
derivative venue, plus gateway-level cases with a recording broker: 0 calls). A GTT on a derivative
venue (its leg is CNC by construction) is now refused whatever the switches. Both flags are false in
every environment, so nothing in force changed — the stricter boundary (charter tie-break). Weekly
desk, swing, TWT and VBT suites green. **Rejected.** Leaving it to `options_gates()` (the weekly
desk's gateway does not call it); allowing NRML futures behind a third flag (futures are Track C §6).
**Reversal.** Delete the two added branches in `guards.py`.

## OP2.2 — The `op_` schema: 17 tables, one enum, one migration (`0050_options`) · ⚠ UNREVIEWED

`03` §1–§12 plus **`op_config_audit`** (`03` §7's "settings_audit on every write", shaped like
`tw_config_audit` with a `scope` column: `BOOK` or the sleeve group). Interpretations: `op_event_day`
carries `user_id` (as `03` §3 lists), so its key is `(user_id, date)`; `op_sleeve_config` is keyed by
sleeve **group** (`O1M`, `O1W`, `O2`, `O3` — `03` "config and flags group O3A/O3B as O3"), a text
column with a CHECK, while session/plan/scan/journal/backtest rows use the Postgres enum `op_sleeve`;
`op_book_config` gains `underlying` (default `NIFTY`, `CHECK = 'NIFTY'`) so "adding BANKNIFTY" has a
place to be refused (OP2.9); `op_leg.instrument_token` references `op_contract` (never deleted, so
the reference is always resolvable); `op_chain_snapshot` has no FK to the master (46 k inserts a day
for a fact the collector just read). Downgrade drops every table (partitions with their parent) and
the enum, and is **tested** (`test_options_schema.py` round trip on `baskfy_test`). **Reversal.**
`alembic downgrade 0049_broker_trade`.

## OP2.3 — `options_gates()`: one pure rule, two readers; what the gateway is handed · ⚠ UNREVIEWED

`baskfy_core.options.gating.options_gates(sleeve, flags)` is the only AND of the four switches;
`app/options_gates.py` (desk, reads `app.config` at call time) and `baskfy_worker.options` (worker
settings) are thin readers. Mode is `LIVE` iff all four are on. **What the gateway is handed in
PAPER** is a judgement: with the sleeve's execution flag **off**, `dry_run=True` plus
`options_enabled=intraday_enabled=True` — so OP10's paper confirm runs the real gateway path and is
simulated by its dry-run branch (otherwise the product gate refuses every paper leg before the code
under rehearsal is reached, contradicting `06` OP10). With the execution flag **on** but not all four,
the desk's real switches pass through, so a half-flipped configuration is refused by the gateway
itself (`06` OP13's defence in depth). `dry_run=True` in every PAPER row is what guarantees no broker
call (tests at all three layers). **Rejected.** Always passing the desk's real switches (paper would
be blocked, not simulated); always passing True/True (a half-flip would silently simulate).
**Reversal.** The two `return` lines in `gating.py`.

## OP2.4 — Vocabularies later modules own are not CHECK-constrained yet · ⚠ UNREVIEWED

Vocabularies OP1 owns (sleeves, expiry kinds, option types, sides, session states, modes, sizing
modes) are imported from `baskfy_core.options` into the models and CHECKed. A scan's state, skip and
reject codes, close reasons and `verdict` are OP4/OP6–OP9's; they are text now and get their CHECK
from the module that defines them (a transcription now would be a list that module must match —
TW3.1's problem). No `04` threshold is a CHECK (condor PACK.7). **Reversal.** A later migration adds
each CHECK with its module.

## OP2.5 — Chain partitions Sep 2026 → Dec 2027, no DEFAULT partition · ⚠ UNREVIEWED

A DEFAULT partition would silently absorb a month nobody created, and `CREATE TABLE … PARTITION OF`
for that month would then be refused. So the migration creates 16 monthly partitions (IST
boundaries) and `baskfy_worker.options.partitions.ensure_chain_partition` creates later ones
idempotently; OP3's collector must call it for the current and next month before writing. A row for
an unpartitioned month fails loudly (tested). **Reversal.** Add a DEFAULT partition.

## OP2.6 — Event days seeded: six RBI MPC decision days for FY 2026-27; no Budget date · ⚠ UNREVIEWED

Verified 22 Sep 2026 from the RBI press release "Meeting Schedule of the Monetary Policy Committee
for 2026-2027" (23 Mar 2026, https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=62422):
Apr 6–8, Jun 3–5, Aug 3–5, Oct 5–7, Dec 2–4 2026, Feb 3–5 2027. The decision day is the meeting's
last day (the statements of 5 Jun and 5 Aug 2026 are so dated): **8 Apr, 5 Jun, 5 Aug, 7 Oct, 4 Dec
2026, 5 Feb 2027**, each with the URL in the seed file and on the row (`source_url`). The Union Budget
2027-28 date is **not seeded** — not announced by the Finance Ministry as of today (1 Feb is
convention, not a source); no election result is announced. QUESTIONS Q4 stands for anything else.
Note 7 Oct 2026 is a Wednesday (not an expiry) — it blocks O2 only. **Reversal.** Delete a row
(source `SEED`) or add one on the web tab (OP5).

## OP2.7 — The nightly master: its Beat slot, its refusals, and the provider read OP2 needed · ⚠ UNREVIEWED

`baskfy.options.refresh_master` at **19:30 IST mon–fri** (the morning's Kite token is still valid;
the calendar is current long before 09:15; one read on the bulk lane). No Kite session → skipped. An
**empty** dump is refused, never applied (it would mark every live contract expired). Contracts absent
from tonight's dump are marked `expired`, never deleted. `op_expiry` is rebuilt from tonight's listed
contracts with `calendar.kind` (monthly = the last listed expiry of the month, no weekday rule); an
expiry absent tonight is left as it was if in the past, and marked `withdrawn_on` + alerted if it had
not yet happened (the holiday-shift shape). A lot-size or kind change is alerted and written to
`detail.history`. Where one expiry states two lot sizes the larger is kept and noted (under-counting
risk per lot is the worse error). `06` lists provider reads under OP3, but the master's read belongs
with its table, so `KiteProvider.option_contracts(underlying)` and `OptionContractRecord` land here
(read-only, dump columns only, never a symbol parse). **Reversal.** Remove the Beat entry; the CLI
(`python -m baskfy_worker.options_cli refresh-master`) still works by hand.

## OP2.8 — Where the flags live, and how the worker reads the desk's switches · ⚠ UNREVIEWED

Desk `config.py`: the 4 execution + 3 operational flags and 6 ceilings, all in `LOCKED_KEYS`
(`OPTIONS_ENABLED`/`INTRADAY_ENABLED` already were). API settings: the 7 prefixed flags + 6 ceilings
(the two desk switches are the desk's). Worker settings: the same 13 **plus** the desk's `DRY_RUN`,
`OPTIONS_ENABLED`, `INTRADAY_ENABLED` read under their unprefixed names **as strings and parsed the
desk's way** (dry unless exactly `false`; on only if exactly `true`), so pydantic's lenient booleans
("yes", "1") can never read a switch as live that the desk reads as off (tested). All defaults false;
the root, `decile-blueprint` and desk `.env.example` carry the block; the never-read
`BASKFY_CONDOR_*` block is removed (OP0.7). No `*AUTO*` name exists anywhere (tested in all three).
**Reversal.** Config edits.

## OP2.9 — BANKNIFTY (or any non-NIFTY underlying) is a 422 in every config · ⚠ UNREVIEWED

A new problem type `underlying-not-allowed` (422, declared above `SETTING_ABOVE_CEILING` for the
OpenAPI-order reason on `SETTING_BELOW_FLOOR`). Both patch models (`OptionsBookPatch`,
`OptionsSleevePatch`) refuse a non-NIFTY `underlying` before any other validation; the pure
`CalendarConfig` already raises; `op_book_config` has the DB CHECK. **Rejected.** Reusing
`setting-above-ceiling` (it is the scope, not a server bound). **Reversal.** The allowed tuple.

## OP2.10 — `OptionsSettings` is a module, not yet a route; the seed is a worker CLI · ⚠ UNREVIEWED

`baskfy_api.options_settings` holds the patch models, the ceiling checks (including the derived
per-trade ₹ risk, capital × risk %, checked on the row as it would stand — either field can cross
it), and `apply_book_patch` / `apply_sleeve_patch` with an audit row per moved field, bounds first so
a refused patch writes nothing (tested on the DB). The routes are OP5's (`05` §2). Seeding is
`python -m baskfy_worker.options_cli seed` (`06` puts the event-day seed in `baskfy_worker/seeds/`,
and the API cannot import the worker, so `make seed` does not call it). Every capital seeds as **0**;
the other sleeve defaults are read off `OptionsConfig` so `03` §7 and `04` cannot disagree. Idempotent
(`ON CONFLICT DO NOTHING`); never resets a chosen number (tested). **Reversal.** Move the seed into
`baskfy_api.seed` if the dependency direction ever allows it.

## OP3.1 — Option quotes are a new `OptionQuoteRecord` and `KiteProvider.option_quotes(keys)`, not a wider `QuoteRecord` · ⚠ UNREVIEWED

**Context.** `06` OP3 lets the run extend `QuoteRecord` or add an `OptionQuoteRecord` and asks that
the choice be recorded. `QuoteRecord` feeds the swing premarket scan (a live book). **Choice.** A new
`OptionQuoteRecord` (depth as `DepthLevelRecord`s with Kite's zero padding dropped, `oi`,
`oi_day_high/low`, `timestamp` → `as_of`, `last_trade_time`) returned by a new
`option_quotes(keys)` that takes whole `EXCHANGE:SYMBOL` keys, so the collector's one call carries
the NFO contracts **and** `NSE:NIFTY 50` for the spot. Same 500-key batching and one limiter token
per batch as `quotes()`. Also new, read-only: `minute_bars(token, start, end)` (60-day windows,
aware IST bars), `basket_order_margins(legs)` (a calculation; `consider_positions=False` so the
answer is the basket's own, never a function of other books' positions — Track C §8), and
`margins_shape()` (field names and types only — no account figure leaves the provider). The
basket verb is a separate runtime-checked Protocol so every existing test double stays a valid
client; a client without it is refused. **Rejected.** Widening `QuoteRecord` (changes a live book's
input for a sleeve that has not traded); `quotes(..., exchange="NFO")` (one exchange per call would
cost the spot a second quote). **Reversal.** Fold the fields into `QuoteRecord`.

## OP3.2 — `BASKFY_OPTIONS_COLLECT_ENABLED` and `BASKFY_OPTIONS_SCAN_ENABLED` stay default **false** · ⚠ UNREVIEWED

`06` OP3 and PACK.11 let this module set both defaults to true once the limiter measurement is
green. **The orchestrator's instruction for this session supersedes that: the collector ships
disabled by its own flag, default false, and the orchestrator may enable it on the box later, after
the rate-limit proof and the live probe** (QUESTIONS Q12's standing default: yes, once green). So
the doc's "the run sets the default to true" is the stale half for OP3 — `06` now says so. The
scan flag is untouched (the scans are OP4). Neither flag moves money. **Reversal.** Set
`BASKFY_OPTIONS_COLLECT_ENABLED=true` in the box's env (operational; not a Track B money flag).

## OP3.3 — The index-bar tasks sit behind the collector's flag; the backfill does not · ⚠ UNREVIEWED

`baskfy.options.index_bars` (each minute) and `.index_bars_eod` (15:45) are half the collector's
Kite load and its ATM hint, so they share `collect_gate` (flag → 09:15–15:30 → NSE calendar → Kite
session, cheapest first, every refusal before any network call). `.backfill_index_bars(from, to)`
and the CLI `backfill-index-bars` are on demand, not recurring load, so they are not flagged; they
take the **bulk** lane under the historical clock so they yield to the collector and the desk.
**Rejected.** A third operational flag for bars alone (one more switch to forget). **Reversal.** Give
the bar tasks their own settings field.

## OP3.4 — The collector centres its strikes on the last stored NIFTY 50 close; a spot-only quote only when there is none · ⚠ UNREVIEWED

Which strikes to quote needs a spot before the call. The newest `op_index_minute` NIFTY 50 close
within four days (normally the minute before; at 09:15, yesterday's 15:29) picks the ±15-strike
window — a one-minute-old ATM still brackets the true ATM by fourteen strikes, and a 1 % gap by ten.
Only with no stored bar in four days does the collector spend a second `quote()` on the spot alone.
The rows' `spot` is always the one quoted in the same call as the chain. **Rejected.** Two quote calls
every minute (doubles the quote-clock share for nothing); a strike window from yesterday's daily
close (wrong after a big move). **Reversal.** `SPOT_HINT_MAX_AGE`.

## OP3.5 — Every options read waits on the box's `read` ceiling and then its endpoint family's clock — the desk's keys, rates and order · ⚠ UNREVIEWED

Kite's caps are per family (quote 1 req/s, historical 3, general 10) under the combined read ceiling
M85 put on `baskfy:ratelimit:kite:read`. The worker's `KiteProvider` so far took only the ceiling;
the desk (`kite_limits.py`, SW21) takes `read` **then** `baskfy:ratelimit:kite:<family>`. OP3 adds
`KiteFamily`, `kite_family_key`, `KITE_FAMILY_RATE_PER_SECOND` (= the desk's `RATE_FOR_FAMILY`,
asserted by reading the desk's source) and `build_kite_family_limiter(settings, family, lane)` =
`LayeredCallSpacer([bulk?], read, family)` — the family clock last, as the desk does, so the
tightest per-endpoint clock is the exact one. `baskfy_worker.options.reads.build_options_kite`
builds one adapter per family (quote, historical, general). If any clock cannot be built (Redis
gone) the limiter is `None` and the adapter refuses every call — never a partial limiter.
**Recorded, not changed:** the existing worker `quotes()` path (swing premarket) still takes only
the ceiling, not the quote family clock — another book's input; a later swing module may move it.
**Reversal.** Build the options adapters with `build_kite_provider` instead.

## OP3.6 — OP0's six live reads are one read-only CLI probe, run on the box by the orchestrator · ⚠ UNREVIEWED

No Kite session on this Mac and no login allowed, so the reads are code, not answers:
`python -m baskfy_worker.options_cli probe` inside the worker container prints one JSON report of
(a)–(f), each read independent (a failure is recorded with its error type and text and the rest
run). (a) probes 1–14 January of each year from 2010, then month by month in the year before the
first hit; (b) needs an expired NIFTY contract in `op_contract` — there is none until the nightly
master has watched an expiry pass (the first is **29 Sep 2026**'s, marked expired that evening), so
before then it says so; (c) reads expiries/lots/ticks/strike step/kind off the master with no
weekday rule; (d) is `margins_shape()` — names and types, no figures; (e) quotes ATM ± 2 CE on the
nearest expiry and gives an **OI-unit verdict by divisibility** (OI in units is always a multiple
of the lot; five lot-counted OIs all landing on multiples of ~65 by chance is ~1 in 10⁹) — `UNITS`,
`LOTS` or `UNDETERMINED`; `chain.oi_unit` stays at OP1.3's `UNITS` until the probe answers;
(f) asks the margin calculator about a fixture ±4/±7-step condor, one lot a leg, MIS. Nothing is
written to the database; the module's source names no order verb (tested) and it runs end to end
against a fake client that has none. **Reversal.** Delete the subcommand.

## OP3.7 — Only closed minutes are written; the upsert overwrites; an inverted bar is dropped · ⚠ UNREVIEWED

A minute bar is written only once `ts + 1 min ≤ now` — a forming bar is not a fact yet. The upsert
on `(instrument_id, ts)` overwrites prices with Kite's latest reading, so the 15:45 reconcile
corrects any minute and re-running a day is identical (house rule 7). Prices round to two places at
write. A bar with `high < low` is dropped and logged, never repaired by swapping. **Reversal.** The
`closed_bars` filter.

## OP3.8 — The minute tasks are `acks_late=False` and expire after 55 s · ⚠ UNREVIEWED

A chain minute redelivered a minute later would stamp an old `ts` with new prices, and a backlog of
minutes after a worker stall would burst the quote clock. So the two minute tasks ack on receipt
and Beat sends them with `expires=55`. Beat fires every minute 09:00–15:59 mon–fri; the task's own
gate decides. **Reversal.** Beat options.

## OP3.9 — Greeks outside `numeric(10,6)` are stored null, never truncated; `greeks_model` marks every row a forward was computed for · ⚠ UNREVIEWED

`|x| ≥ 10⁴` cannot be stored in the column; truncating it would store a wrong number, so it is null
(realistic NIFTY greeks are far inside). A row whose IV the solver refuses keeps its quote with null
greeks; a row with no forward (no spot, or `T ≤ 0`) has null `greeks_model` too. **Reversal.** Widen
the column.

## OP3.10 — The Tier-1 backfill and the live limiter measurement are not run; the budget is arithmetic · ⚠ UNREVIEWED

`06` OP3 asks the backfill to run on the dev stack for a year and STATUS to state the full duration,
and the limiter share to be measured with the swing morning replayed from its timing probe. No Kite
session (⛁); and `docs/swing/status/S2-kite-timing.md` is still "NOT RUN YET", so there is nothing to
replay. Instead: (1) the proof is structural and measured on Redis — the options reads take the
desk's own clocks (OP3.5), and a test runs a desk-shaped and a collector-shaped caller together on
one departure clock and shows their combined gaps never beat the cap; (2) the share is arithmetic —
≤ 2 quote calls/min of a 60/min cap (3.3 %), 2 historical calls/min of 180 (1.1 %), ≤ 4 of the
180/min read ceiling (2.2 %); (3) the backfill's duration is an **estimate**: 1 Jan 2015 → today is
≈ 4,282 days = 72 sixty-day windows × 2 indices = 144 calls, ≥ 72 s of bulk-lane spacing plus
Kite's response time — a few minutes, and ≈ 2.2 M rows. The box's first run replaces the estimate.

## OP4.1 — What OP4 builds: six pure modules, each sleeve's exits included, and one worker task · ⚠ UNREVIEWED

**Context.** `06` OP4 names `options.condor`, `.directional`, `.expiry_setups`, `.scan` and the task
`baskfy.options.scan`; OP1.1 left each sleeve's **exit rules** to OP4 ("they belong with the signals
they read"). **Choice.** Six modules in `baskfy_core.options`: the four named, plus `bars` (windows,
5-minute bars, Kaufman's ER — the readings all three sleeves share, so they cannot disagree) and
`structures` (the priced chain `ChainView`, `CandidateLeg`/`Candidate`, the round trip, sizing with
the sleeve's own settings — what a candidate and a plan are both made of). Each sleeve module has a
day function, a `build` (the one OP6-OP8's plan builders will call) and an `exit_decision` with its
precedence (condor §7.6, `04` §4.5, §5.3), including `04` §9.2's budget breach as `STOP`. Worker:
`baskfy_worker/options/scan.py` + task `baskfy.options.scan` + Beat `options-scan`. **Rejected.**
Putting the shared pieces in `chain.py`/`execution.py` (OP1's modules, mutation-scored; widening
them would reopen their scores); a plan builder now (OP6). **Reversal.** Move functions.

## OP4.2 — A 5-minute bar is named by the start of its last minute and is complete when that minute is stored · ⚠ UNREVIEWED

**Context.** `04` §4.2's window is "[09:30, 13:30] (the trigger bar's close time)" and §5.1's
"[10:19, 13:00]"; neither says whether a bar "closes at" 10:19 or 10:20. **Choice.** The close time
is the start of the bar's last minute — the convention condor uses for "the 09:59 bar" — which
makes 10:19 the 10:15-10:19 bar, the first after the 10:14 range, and 13:29 the last O2 bar. A bar
is complete once its last one-minute bar is stored (that minute's close *is* its close); a missing
middle minute does not void it. The decision minute that prices a trigger is the minute after
(10:20 for a 10:19 close). **Rejected.** Closing at the end instant (10:20) — the 10:15-10:19 bar
would fall outside `[10:19, 13:00]`'s spirit only by the naming; all-five-minutes completeness
(one lost minute would make a trigger impossible for five minutes). **Reversal.** `bars.five_minute_bars`.

## OP4.3 — A window is judged once its last minute is stored, or `stale_scan_seconds` after it should have been · ⚠ UNREVIEWED

**Context.** The scan runs at the same minute the index-bar task writes the previous minute, so at
10:00 the 09:59 bar may not be stored yet; a verdict computed then would flip a minute later, and
`04` §10's states are one-way. **Choice.** `bars.window_settled`: final when the window's last minute
is stored, or once `chain.stale_scan_seconds` [120 s] have passed after it closed (then a missing
minute is `INCOMPLETE_OBSERVATION`). While a window fills: gap needs the 09:15 bar; a range excess is
reported at once (a range only grows); containment and ER are judged only on the complete window.
Reasons found early are shown live (`OBSERVING`, `BUILDING_RANGE`). No new config field — the grace
reuses §2.5's scan staleness. **Rejected.** Judging at the clock time regardless (flips); a separate
grace field (one more number for the same idea). **Reversal.** Pass a different `grace_seconds`.

## OP4.4 — A decided candidate is priced from its decision minute's snapshot; greeks are recomputed from the quotes · ⚠ UNREVIEWED

**Context.** `04` §10: the scan's candidate equals a plan built from the same inputs; a scan
re-computed each minute from the latest quotes would move a `WOULD_TRADE` to a `REJECTED_CREDIT` as
premiums decay, breaking one-way states and per-minute idempotence. **Choice.** Before a sleeve
decides, the latest snapshot (a preview). Once decided, the snapshot at its **decision minute** — O1's
`plan_time`, O3-B's `o3b_plan_time`, O2's and O3-A's trigger minute — the first stored minute at or
after it. The pure `scan.wanted_minutes` runs the scan with an empty `SnapshotBook` and records which
minutes it asked for; the worker loads those, then runs `scan_all`. Greeks and the forward are
recomputed by `chain`'s own functions from the stored quotes (the plan builder will do the same from
its quotes); the collector's stored greeks stay Tier 3's record. `stale` = the snapshot is missing or
more than `stale_scan_seconds` from the minute it should describe. **Rejected.** Reading the stored
greeks (a plan would then use a different computation than its scan); latest-only pricing.
**Reversal.** `SnapshotBook` keys.

## OP4.5 — The round trip is `04` §6.1's charges on every order a plan implies, both crossings at the conservative side · ⚠ UNREVIEWED

**Context.** `04` §6.4's `cost_share = expected_round_trip / expected_gain` does not say at what
price the exit orders are costed; condor §5.2 adds a separate `spread_cost`. **Choice.** Each leg
enters at its attempt-1 limit (buy `ask + tick`, sell `bid - tick`) and exits at the same quotes'
opposite attempt-1 limit; `expected_round_trip = charges(those orders)` — O1 eight, O2 two, O3 four
(`04` §6.1). Crossing the spread both ways at the conservative side *is* §6.2's slippage, so no
separate spread cost is added; condor §5.2's `spread_cost` is the stale half (`04` §3.1 does not cite
condor §5). O1 also warns `RESERVE_EXCEEDED` when the round trip per lot exceeds
`reserve_per_lot_inr`. **OP6-OP8 pin these figures to the paisa.** **Rejected.** Exit priced at the
profit-take premiums (unknown at plan time); adding condor's spread cost (double counting).
**Reversal.** `structures.round_trip`.

## OP4.6 — Three refusal codes `04` does not name, and liquidity checked twice · ⚠ UNREVIEWED

(a) `REJECTED_NO_SHORT_PUT` — condor §4.2's "symmetric" of `REJECTED_NO_SHORT_CALL`, so the page says
which side failed. (b) `REJECTED_NO_CONTRACT` — the strike a rule names (O2's ITM contract, a spread
leg) is not quoted two-sided this minute. (c) `REJECTED_NO_CHAIN` — no snapshot, no quotes for the
expiry, or no tick/step to read; the scan shows it rather than guessing. (d) Liquidity: a short (O1)
is **selected** among contracts liquid for one lot, then every leg is re-checked at the **sized**
quantity (`REJECTED_ILLIQUID`) — sizing needs the credit, which needs the legs. O1's wings take the
depth test only (`04` §2.4). (e) A debit ≤ 0 (crossed quotes) is `REJECTED_DEBIT`. Rejection order:
paused, slot, chain, lot size, structure, premium, sizing, (premium cap), liquidity, cost.
**Reversal.** Each is one branch in the sleeve's `build`.

## OP4.7 — Among in-band shorts the one nearest `delta_target` wins; a tie goes further from the money · ⚠ UNREVIEWED

Condor §4.1 names no tie-break. The further strike is the safer short (less gamma, further from the
range). **Reversal.** `condor.select_short`'s key.

## OP4.8 — O2's trend: an SMA-seeded EMA over 120 NIFTY 50 closes; missing data refuses the day · ⚠ UNREVIEWED

**Context.** `04` §4.1 says "20-day EMA of NIFTY 50 closes from `index_snapshot_daily` through the
previous session" and "India VIX previous close", nothing about seeding or missing data.
**Choice.** `alpha = 2/(n+1)`, seeded with the mean of the first `n` closes, fed the last 120 stored
closes (`nifty-50`) so the seed has decayed to nothing; fewer than 20 → `TREND_UNKNOWN`. VIX from
`index_snapshot_daily` `india-vix`, falling back to the last stored `INDIA VIX` minute before today;
none → `VIX_UNKNOWN`. Both are skip reasons, not guesses. The same daily series gives every sleeve's
`prev_close` (O1/O3 with none show `NO_PREV_CLOSE`). NIFTY 50, not the swing gate's MidSmall 400
(PACK.5; the kickoff's note). **Rejected.** Seeding with the first close (a 20-close series would
then depend on its first print); trading without VIX. **Reversal.** `directional.ema`, `DAILY_HISTORY`.

## OP4.9 — O3-A and O3-B are separate rows; O3 borrows `DAY_SKIPPED`; O3-B holds the day at scan level · ⚠ UNREVIEWED

`op_sleeve` has `O3A` and `O3B`, so each gets its own `op_scan` row (OP1.6 keeps them apart in the
journal too). `04` §10's O3 vocabulary has no state for a setup whose conditions failed (a gap too
small, a wide morning range, a broken hold); it uses O2's `DAY_SKIPPED` with the reasons
(`GAP_TOO_SMALL`, `GAP_TOO_BIG`, `HOLD_BROKEN`, `RANGE_TOO_WIDE`, `INCOMPLETE_OBSERVATION`,
`EVENT_DAY`). `04` §5.5 "if both would fire, O3-B holds": when O3-B has `TRIGGERED` with a viable
candidate and its session is not `LAPSED`/`SKIPPED`, O3-A is `SLOT_TAKEN` with `O3B_HOLDS` (its
candidates carry `REJECTED_SLOT_TAKEN`). O3-B's hold completeness is the 30 bars 09:15-09:44, derived
from the two clock fields — no new field. A disabled setup is `NOT_TODAY`/`SETUP_DISABLED`.
**Reversal.** `scan._scan_o3a`/`_scan_o3b`.

## OP4.10 — The scan task is gated on both flags, reads only the database, and runs 20 s after the collector · ⚠ UNREVIEWED

**Context.** `06` puts the task behind `BASKFY_OPTIONS_SCAN_ENABLED`; the orchestrator's instruction
for this session: no DB or Kite work while the collector flag is off, and share OP3's queue/limiter
conventions. **Choice.** Refusals before any database session, cheapest first: the scan flag, the
collector flag (no collector, nothing to scan), 09:15-15:30, `BASKFY_SOLE_USER_ID` (the tenant the
`op_` rows belong to — the swing/TWT/VBT convention); then, inside the session, the NSE calendar.
The task makes **no Kite call** (PACK.11), so it needs no limiter at all. Beat `options-scan` every
minute 09:00-15:59 mon-fri on the `default` queue with `expires=55` (OP3.8) and `countdown=20`, so
the collector's minute and the bars are stored first; `acks_late=False` like the collector. Rows are
upserted on `(user_id, sleeve, ts)` — a re-run of a minute rewrites the same row. Mode per sleeve is
`options_gates()` (PAPER with every money flag false), so a candidate with sleeve capital ₹0 is paper
one lot. **Rejected.** Scanning with the collector off (it would write `NO_CHAIN` rows all day); a
per-user loop over every `op_sleeve_config` owner (P4 multi-tenancy is not in force for options).
**Reversal.** The flags; the Beat entry.

## OP4.11 — How the O1 scan walks `04` §10 · ⚠ UNREVIEWED

`OBSERVING` until the gate is final **and** the clock has reached `plan_time`; then `WOULD_SKIP` with
every gate reason, or the candidate built from the `plan_time` snapshot → `WOULD_TRADE`, or
`WOULD_SKIP`/`SLOT_TAKEN` with its rejection. A session row for the sleeve (the desk's, OP9) in
`PLANNED`/`CONFIRMED`/`OPEN` shows `PLANNED`; `CLOSED`/`LAPSED`/`SKIPPED`, or the clock past
`entry_window_end`, shows `DONE`, keeping the verdict in `numbers.verdict`. `PAUSED` overrides every
state but `NOT_TODAY`, keeps the numbers and adds `REJECTED_PAUSED`. **Reversal.** `scan._scan_o1`.

## OP5.1 — The surface: ten paths, twelve handlers, exactly two mutations, the patch atomic by construction · ⚠ UNREVIEWED

**Context.** `05` §2 lists the routes and "no other verb on any path"; `06` OP5 wants "exactly two
mutations (event day, settings), everything else 405". **Choice.** `routers/options.py` serves the
ten paths of `05` §2 (twelve handlers). The event day's two halves share one path —
`POST /options/event-day` (body `{date, reason='MANUAL', note}`) and `DELETE /options/event-day?date=`
— so "exactly two mutations" is two paths: `/options/event-day {post, delete}` and
`/options/config {patch}`. The settings PATCH takes `{book?, sleeves: {O1M|O1W|O2|O3: …}}` and
checks **every** part's bounds (`check_book`, `check_sleeve` on the current row) before writing any
part, so a two-part patch that crosses a ceiling on its second part changes neither — atomic by
construction, not only by the transaction's rollback (the test harness never rolls back, so only
the former is testable). Every handler resolves the sole tenant (`scoped_sole_user_id`), and
`test_options_readonly.py` asserts the verb set exactly, 405 on every other verb against the
running app, no execution/broker name in the router or its two modules, and — app-wide — no
`execute`/`confirm`/`place_order` path anywhere under `/api/v1`. **Rejected.** A `PATCH` per sleeve
(`/options/config/{sleeve}` — a path `05` does not name); a separate `/options/event-day/{date}`
DELETE path (a third mutating path for one mutation). **Reversal.** The router.

## OP5.2 — An empty tab names its reason, most fundamental first · ⚠ UNREVIEWED

**Context.** With the collector and scan flags off (their defaults; the box when OP5 shipped)
`op_scan` is empty, and the TWT lesson (DECISIONS-TW TW14.1) is that "nothing found" and "nothing
looked" must not render alike. **Choice.** `GET /options/today` carries `empty_reason`:
`collector_off` if the API's `BASKFY_OPTIONS_COLLECT_ENABLED` is false; else `scan_off`; else
`no_scan_yet_today` on a session day with no row for today; else `never_scanned`; `null` when
today's rows are served. A previous session's rows are still served, dated, with `live=false` and
the `As of close, <day>` label, beside the reason. The API reads the two flags from its own env;
on the box the API and the worker share `.env.staging` (`compose.prod.yml`'s `python-image`), so
they agree — **after a flag flip both containers must be recreated**. **Rejected.** Inferring
"collector off" from the absence of chain rows (a quiet holiday and a switched-off collector would
read alike). **Reversal.** `options_read.empty_reason`.

## OP5.3 — The shared live overlay touches the two header index levels only · ⚠ UNREVIEWED

**Context.** The orchestrator asked to reuse `useLiveMarks` / `/meta/live-marks` (4aad7a4) where the
UI spec shows live values; `05` §2 says "the page never calls Kite … the collector is the one reader
of the chain, so the tab adds no load to the limiter". **Choice.** Both honoured: the NIFTY 50 and
India VIX levels in the header strip are `<LivePrice>` cells inside one `<LiveMarksProvider>` (two
NSE symbols, the screens' cached, rate-limited, session-hours-only read — not an options read and
not a chain read); their base value is the collector's last minute bar, else the last
`index_snapshot_daily` close, and the cell says which. Every scan number, candidate price, chain
quote and position mark stays on the collector's minute / the desk's mark. Option contracts are
never overlaid: `/meta/live-marks` quotes `NSE:` symbols, and an NFO quote from the web would be a
second reader of the chain. **Rejected.** Overlaying candidate legs with live quotes (a candidate
priced from a different minute than its decision is not the plan the desk would build — OP4.4).
**Reversal.** `components/options/header-strip.tsx`.

## OP5.4 — Today's roles come from `op_expiry` through the pure `calendar.role` · ⚠ UNREVIEWED

The header's `O1-M: next 29 Sep · O1-W: today · …` is `baskfy_core.options.calendar.role` /
`next_session` fed one stub `Contract` per live `op_expiry` row (withdrawn expiries excluded,
OP2.7) and the tenant's event days — the same rule the scan uses, without loading the ~thousand-row
master for a header. The NSE session-day answer is the API's `is_session_day`. **Reversal.**
`options_read.roles_for`.

## OP5.5 — The API reports `PAPER` or `DESK_DECIDES`, never `LIVE` · ⚠ UNREVIEWED

The API carries the four per-group execution flags (OP2.8) but not the desk's `OPTIONS_ENABLED` /
`INTRADAY_ENABLED` / `DRY_RUN`, so it cannot compute `options_gates()`. It reports `PAPER` while a
group's flag is false (certain), and `DESK_DECIDES` when it is true — never a guessed `LIVE`. The
flags are reported, never branched on (`test_options_readonly` counts each exactly once).
**Reversal.** `routers/options._gates`.

## OP5.6 — Event days: `source='USER'`, reason `MANUAL`, seeded days not removable, conflicts are 400 · ⚠ UNREVIEWED

`05` §2 says "add/remove of `MANUAL` event days"; the schema's vocabulary (`03` §3, OP2) is
`reason ∈ {RBI_POLICY, UNION_BUDGET, ELECTION_RESULT, MANUAL}`, `source ∈ {SEED, USER}`. A web add
writes `source='USER'`, reason `MANUAL` by default (any schema reason accepted). DELETE removes only
a `USER` row; a `SEED` row (a source-verified RBI date, OP2.6) is refused with 400 — removing one is
a DECISIONS-OP entry. A duplicate add is 400 too: the problem catalogue has no generic 409 and a new
problem type for one form was not worth a catalogue change. **Reversal.** `options_read.add_event_day`
/ `remove_event_day`.

## OP5.7 — The journal page reads `op_journal`; the paper progress counts ended PAPER sessions · ⚠ UNREVIEWED

`GET /options/journal` summarises `op_journal` rows with OP1's `summarize` (never pooled across
`(sleeve, simulated, sizing_mode)`), plus each pool's R values for the histogram, skips by first
reason from `op_session` (`SKIPPED`), and per group the paper progress of `02` §3.2: sessions =
`op_session` rows in `PAPER` mode that ended (`CLOSED`/`LAPSED`/`SKIPPED` — "a skipped day counts"),
traded = simulated `op_journal` rows. **Not** enforced here: "consecutive" and "zero rule
violations" — OP11 owns the ledger and OP15 the evidence. Both tables are empty until OP9-OP11 run.
**Reversal.** `options_read.journal`.

## OP5.8 — The AC's three sleeve states are asserted over fixtures; the browser spec asserts the empty tab · ⚠ UNREVIEWED

**Context.** `06` OP5's AC: "Playwright renders `/options` from fixtures showing an O1 `WOULD_SKIP`
with all its reasons, an O2 `ARMED` with distance-to-trigger, an O3 candidate spread, and the
`As of close` label outside hours." The Playwright stack runs the real API over `baskfy_e2e`, which
has no `op_scan` rows and no seam to inject them. **Choice.** The swing hub's precedent (DECISIONS-SW
SW4.3): the three states and the clock label are asserted in the page's rendered-DOM tests over
fixtures written to the API's shapes (`app/(app)/options/__tests__/page.test.tsx`, over a mocked
fetch), and the four API states are asserted against a real database (`test_api_options.py`: the AC
morning's rows served as written, `Live`/`stale`/after-close). `e2e/options.spec.ts` renders the tab
in a browser with the flags at their defaults and asserts the caveat, the paper label and the named
empty reason. **Not run here:** the e2e spec (`tools/ci-local.sh` skips Playwright — browsers, a
seeded `baskfy_e2e`, both servers). **Reversal.** Seed `op_scan` rows into `baskfy_e2e` and move the
three assertions into the spec.

## OP5.9 — The web tab says "strategy" and "options account", and translates every code · ⚠ UNREVIEWED

`no-jargon.test.ts` (PORTFOLIO_REDESIGN §8) bans "sleeve" and "book" in user-visible strings, and
the TWT `no-internals` rule bans snake_case and SCREAMING_CASE on screen. The tab says "strategy"
and "options account"; `05` §2's panel names are kept ("Premium selling", "Directional",
"Expiry-day setups") with the codes `O1-M`, `O1-W`, `O2`, `O3-A`, `O3-B` as short labels;
`lib/options/view.ts` translates every `04` state, reason and rejection code and sentence-cases one
it has not met. The rules on `/me/options` are listed with their keys as words ("er max") and their
`04` anchor. The SEBI caveat of `01` §0 is `<FnoRiskCaveat/>`, above the numbers on every options
page, and `<ScanOnly/>` labels the tab "Scan · paper only" (house rule 9). **Reversal.**
`lib/options/view.ts`, `components/options/caveats.tsx`.

## OP5.10 — The chain panel: ±10 strikes around ATM from the forward, ΔOI from the day's first minute · ⚠ UNREVIEWED

`GET /options/chain` serves, per expiry (default the nearest two live ones), the collector's latest
minute today (else its latest ever), strikes within 10 steps of the ATM strike nearest the parity
forward (else the spot), ΔOI against the same contract's first snapshot of that session, ATM IV as
the mean of the ATM call's and put's IV, and put/call OI over the strikes served. Collapsed by
default on the page; numbers, no chart (`05` §2 v1). **Reversal.** `options_read._chain_for`.
