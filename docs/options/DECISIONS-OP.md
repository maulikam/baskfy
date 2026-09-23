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

## OP3.11 — The box probe answered, and the collector and scan flags are on · ⚠ UNREVIEWED

**Context.** OP3 left its six live reads as a probe for the box and both data flags false, to be
turned on after the limiter proof (pack QUESTIONS #12, default yes). Maulik: "keep going with OP3
and deploy after 21:15" (22 Sep 2026), and his standing delegation to set box flags and deploy
(DECISIONS-TW TW18).

**Probe, 22 Sep 2026, on the box after deploying `1c9a3a5`** — `options_cli probe`, read-only,
full JSON in `docs/options/evidence/op3-probe-2026-09-22.json`:

* master: 1,706 NIFTY contracts; spot NIFTY 50 23,329.
* (a) earliest minute history: NIFTY 50 and INDIA VIX both 2015-01-09 09:15.
* (b) expired contract: not answerable yet — `op_contract` has seen no expiry pass; re-run after
  29 Sep.
* (c) next expiries from the master: all lot 65, tick 0.05, strike step 50 near ATM. Tuesdays,
  **except 2026-10-19 (Monday), a holiday-shifted weekly** — the master-not-weekday rule earning
  its keep. Monthlies 29 Sep and 27 Oct.
* (d) margins: shape only (no amounts), as designed.
* (e) option quotes: 5+5 depth levels, `oi`, `oi_day_high/low`, `timestamp`,
  `last_trade_time` all present; every OI read is a multiple of 65 → **OI is in units**, confirming
  OP1's default.
* (f) basket margin for a one-lot NIFTY iron condor (22 Sep weekly): ≈ ₹1,30,904 final.

**Choice.** `BASKFY_OPTIONS_COLLECT_ENABLED=true` and `BASKFY_OPTIONS_SCAN_ENABLED=true` written into
`/opt/baskfy/.env.staging` (the api/worker/beat `env_file`; backup `.env.staging.bak-20260922-options`),
after `celery inspect active` showed both workers idle; `api worker beat` recreated; the worker's
environment read back both true with `OPTIONS_ENABLED=false` and `INTRADAY_ENABLED=false`. Data
only — no order path is touched, every options execution flag stays false.

**Rejected.** Waiting for a measured limiter share (OP3.10) — the collector is one quote call a
minute, ~3 % of the quote cap; the cost of waiting is a week without a chain.

**Reverse.** Set both false in `/opt/baskfy/.env.staging` (or restore the backup) and
`up -d api worker beat`.


## OP6.1 — `06` OP6's "wide short-put spread → `REJECTED_COST`" cannot happen at `04`'s defaults; the AC is scoped · ⚠ UNREVIEWED

**Context.** The AC (carried from condor OC5) expects a wide spread on the short put to fail the cost
test. Under `04` it cannot: (1) a spread wider than `max_spread_pct` [3 %] makes the put illiquid, so
it is never a short — `REJECTED_NO_SHORT_PUT` (§2.4); (2) a spread inside 3 % is already paid in the
credit (shorts at bid, `04` §4.4) and in the conservative close `D` the profit target is measured on
— OP4.5 rejected condor §5.2's separate `spread_cost` as double counting; and (3) the credit floor
dominates the cost test: at `C = 0.25 × 150 = 37.5` the expected gain is `0.5 × 37.5 × 65 =
₹1,218.75`, and a one-lot round trip near ₹200 is a share near 0.16 < 0.20. For O1 at defaults the
cost test binds only below ~30.5 points of credit, which the floor already refuses.
**Choice.** The AC is asserted as what the rules do: a 3.17 % short-put spread → `REJECTED_NO_SHORT_PUT`;
a 1.6 % one lowers the credit by exactly the bid's move (39.65 → 39.25) and plans; `REJECTED_COST` is
reached through the plan builder with a flat 16 % chain and `credit_floor_frac` 0.10 (C = 25.35,
share 0.234). Nothing in `04` or the code changed. **Finding for Maulik:** for O1 the cost test is
dormant at the default floor; it becomes live only if a Tier-3 finding lowers the floor.
**Rejected.** Re-adding condor's `spread_cost` (double counts; would move OP4's pinned ₹198.32);
a fixture that bends liquidity to make the AC literal. **Reversal.** Re-add `spread_cost` to
`structures.round_trip` and re-pin OP4/OP6's figures.

## OP6.2 — Which days write a session; the builder waits rather than guesses · ⚠ UNREVIEWED

An O1 day (`calendar.role` trades) writes one `op_session` — `PLANNED` or `SKIPPED` with every reason.
An O1 day that is an event day is `SKIPPED / EVENT_DAY` (`04` §1.3: skipped, never shifted), so it
counts as a skipped session in the paper period. Any other day (O1-W on the monthly Tuesday, O1-M on
a weekly, a holiday) is `NO_SESSION` and writes nothing. Before `plan_time`, while the 09:15-09:59
window is unsettled (OP4.3), or before the decision minute's snapshot is stored, the answer is
`NOT_READY` and nothing is written — the Beat entry asks again next minute. First asked at or after
`entry_window_end` with nothing decided → `WINDOW_CLOSED`, nothing written: a morning the builder
never saw is not a skipped day (`02` §3.2: a day the desk was down does not count). **Rejected.**
Writing `SKIPPED/NO_CHAIN` at 10:00 when the collector is a few seconds late (a false skip).
**Reversal.** `plan.decide_o1`'s early returns.

## OP6.3 — `CostRates` pinned: `OPTIONS_COST_RATES_REVIEWED_ON` is a constant, and the plan itemises the note · ⚠ UNREVIEWED

OP0 verified every rate on 22 Sep 2026 (OP0.1); OP6 did not re-read the sources (same day) but pins
them: `config.OPTIONS_COST_RATES_REVIEWED_ON = 2026-09-22` is `CostRates.reviewed_on`'s default,
tests assert each rate's value, each source URL/circular in the docstring, and the 90-day warning.
The plan stores `04` §6.1's components over the eight orders in `op_plan.detail.costs`, each rounded
once over all eight (one day's orders are one contract note); its total **is** the scan's
`round_trip_inr` (tested). Worked fixture: brokerage 160.00, STT 6.01, NSE txn 2.87, SEBI 0.01, IPFT
0.00, stamp 0.12, GST 29.32 = ₹198.33. The one-tick limit improvement is not added as a cost (the
plan's prices already carry it; OP4.5 stands). **Reversal.** Re-verify and move the constant.

## OP6.4 — A plan is priced from the scan's decision snapshot and refused if it is stale · ⚠ UNREVIEWED

The builder reads the first stored snapshot at or after `plan_time` (the scan's, OP4.4), so plan and
scan are one computation (tested equal). If that snapshot is more than `stale_scan_seconds` [120] old
when the plan would be issued (the builder ran late), the session is `SKIPPED / STALE_CHAIN` — no plan
from stale quotes (`02` §3.2's "no stale-quote entry"). **Reversal.** Remove the check.

## OP6.5 — Margin: two calculator reads, the transient basket is the three-leg entry prefix; no answer is a paper warning · ⚠ UNREVIEWED

`04` §7.4 / condor §6.3 ask for the hedged figure and the "transient" one between wing and short
fills. With wings sent first there is never a naked short, so the worst intermediate state is both
wings plus the first short; the builder asks Kite's `/margins/basket` (`consider_positions=False`,
read-only, OP3.1) for the four-leg basket and that three-leg prefix, and `sizing.margin_check` takes
the larger against `margin_pool_inr − margin in use` (plans of today's `CONFIRMED`/`OPEN` sessions).
Lots are fixed before the calculator is asked (Track C §10; tested). No answer (no Kite session, or a
broker error, recorded not swallowed): PAPER → the plan is issued with `MARGIN_UNKNOWN`; LIVE →
`REJECTED_MARGIN`. Pool ₹0 in PAPER → `MARGIN_POOL_UNSET` warning (OP1). **Rejected.** Asking only
`initial_total` as the transient (the naked sum — far above any real intermediate state).
**Reversal.** `plan.O1Decision.margin_baskets`.

## OP6.6 — Where the builder lives, and what switches it on · ⚠ UNREVIEWED

Pure decision in `baskfy_core.options.plan` (so OP9's desk process can call the same functions);
database writes, the calculator read and the alert in `baskfy_worker.options.plan`; task
`baskfy.options.plan_o1`, Beat `options-plan-o1` every minute 10:00-10:16 mon-fri (countdown 40 s,
expires 55 s) — each run lapses expired plans (`PLANNED → LAPSED`) then decides O1-M and O1-W,
idempotent per date (a decided session is returned untouched; the session insert is `ON CONFLICT DO
NOTHING`; the `plan_id` is a deterministic `O1M-YYYYMMDD-<sha12>`, a valid `client_id` half). **Dark
by default**: gated on `BASKFY_OPTIONS_MONITOR_ENABLED` (`02`: the monitor "raises plans";
operational, moves no money, default false and false on the box) and the collect flag. CLI
`options_cli plan [--at]` ignores only the monitor flag. **Rejected.** Gating on the scan flag (on on
the box — it would start writing plans the day this deploys, before OP9's replay proves the desk);
putting the builder in the desk now (OP9's module). **Reversal.** Drop the Beat entry when OP9's
desk raises plans itself; both paths are idempotent, so running both is safe meanwhile.

## OP6.7 — `AlertName.OPTIONS_PLAN`: one per sleeve per day, rendered through the mail transport · ⚠ UNREVIEWED

Sent once, when the session is first written: a skip lists every reason; a plan carries sleeve,
date, mode, `plan_id`, expiry, credit (points and ₹), lots and sizing mode, max loss, round-trip cost
and share, margin, every leg in send order with its limit, the 10:15 expiry and "nothing is sent
without a confirm on the desk". Severity `warning` (not a failure — the TWT_EVENING precedent),
runbook `docs/runbooks/11-options-plan.md`. "Rendered in Mailpit" is asserted through the real
`Mailer` over a recording transport (no Mailpit container was running on this Mac). **Reversal.**
`plan.plan_alert`.

## OP7.1 — The O2 builder is its own module pair; the shared plan vocabulary moves up · ⚠ UNREVIEWED

**Context.** OP6 put O1's pure decision in `baskfy_core.options.plan` and its database side in
`baskfy_worker.options.plan`. O2 (and OP8's O3) need the same vocabulary — `PlanState`, `PlanLeg`,
`Verdict`, `plan_legs`, `plan_costs`, `plan_id_for`, the never-naked assertion, `lapse_expired`,
`PlanReport`, `costs_json`, `leg_row`, the session row's values and its race-safe insert.
**Choice.** Keep `plan.py` as *the shared plan vocabulary plus O1*, and add
`baskfy_core.options.plan_o2` + `baskfy_worker.options.plan_o2` beside it. Three small refactors,
no behaviour change: `finalize_o1`'s inline prefix loop became `plan.assert_never_naked(legs)`;
`plan_id_for` now takes a `Decided` protocol (sleeve, trade date, legs) instead of `O1Decision`;
the worker's `_leg_row`/`_session_values`/`_write_session` became public `leg_row`,
`session_values`, `write_session_row` with the O1 wrappers delegating to them. O1's tests are
untouched and still green. **Rejected.** Growing `plan.py` past 1,200 lines once OP8 lands;
copying the cost and leg code per sleeve (two places to get `04` §6.1 wrong). **Reversal.** Inline
the three helpers back and delete `plan_o2.py`.

## OP7.2 — Which days write an O2 session, and what each answer is called · ⚠ UNREVIEWED

Every non-event trading day is an O2 day (`calendar.role`), so every one of them writes an
`op_session` **once it is decided**: `PLANNED`, or `SKIPPED` with every reason. The five answers:

| State | When | Written? |
|---|---|---|
| `NO_SESSION` | not a trading day | no |
| `NOT_READY` | before 09:30 (`BEFORE_ENTRY_WINDOW`), the 09:15–09:29 range not settled (`RANGE_NOT_SETTLED`), no with-trend break yet (`NO_TRIGGER_YET`), the trigger minute's chain not stored (`NO_DECISION_SNAPSHOT`), or the master has no future expiry (`NO_EXPIRY`) | no |
| `SKIPPED` | a day filter refused (`04` §4.1), an event day, the window closed with no break (`NO_TRIGGER`), the contract refused (`REJECTED_*`), or the chain was stale (`STALE_CHAIN`) | yes |
| `PLANNED` | a with-trend break, a contract in band, sized and affordable | yes |
| `WINDOW_CLOSED` | first asked at or after 13:30 with a trigger standing (OP7.5) | no |

**The one that took thought:** a day that armed and never broke. O1's `WINDOW_CLOSED` writes
nothing because a morning the builder never saw is not a skipped day (OP6.2). O2's no-break day is
different — it *was* watched all day and it decided: `SKIPPED / NO_TRIGGER`, verdict `SKIP`, so the
paper period counts it and the journal can say how often the tape simply never broke. Counter-trend
breaks are on `op_session.numbers.counter_trend_breaks`, seen and never traded (`04` §4.2).
**Rejected.** Writing `SKIPPED / NO_TRIGGER` the moment 13:30 passes (the last 5-minute bar is not
final yet — `find_trigger` waits `stale_scan_seconds` for its last minute); treating an armed day
as `WINDOW_CLOSED` (it would vanish from the paper period). **Reversal.** `plan_o2.decide_o2`'s
early returns.

## OP7.3 — O2 asks the broker nothing at all; the premium is the ceiling · ⚠ UNREVIEWED

`04` §7.4 names **O1 and O3** for the basket-margin ceiling; a long option is paid for in full, so
there is no margin to ask about. `finalize_o2` therefore takes no `MarginQuote`, the worker module
imports no provider, `op_plan.margin_required_inr` stays `NULL`, and a test scans both the module
and the task for `baskfy_providers` / `build_options_kite` / `basket_order_margins` as well as the
usual order-path names. What replaces it on the plan: `detail.premium_inr` (what leaves the
account) and `detail.gap_through_inr` — `04` §4.6's true worst case, the whole premium, which the
AC asks to see. The sleeve's own ceiling is the premium cap of §4.6, applied inside
`directional.build` before any of this. **Rejected.** Asking `/margins/basket` for the single buy
so the number is "consistent across sleeves" (a Kite call per trigger that answers the premium we
already know); writing the premium into `margin_required_inr` (O1's "margin in use" sum would then
double-count a debit as blocked margin). **Reversal.** Add a `MarginReader` argument to
`build_o2_plan` and a `margin` field to `O2Plan`.

## OP7.4 — A master with no future expiry is `NOT_READY`, never a skip · ⚠ UNREVIEWED

`expiry_for_o2` returns the smallest master expiry strictly after today; with a stale `op_contract`
it can return `None`. That is a data gap, not a verdict, so the builder answers `NOT_READY /
NO_EXPIRY` and writes nothing — the nightly master refresh fixes it and the next minute decides.
Same reasoning as OP6.2's refusal to write a false skip when the collector is a few seconds late.
**Reversal.** `plan_o2.decide_o2`'s `NO_EXPIRY` branch.

## OP7.5 — A trigger at or after 13:30 gets no plan, and the task runs to 13:34 · ⚠ UNREVIEWED

`06` OP7 fixes `expires_at = min(issued + 30 min, 13:30)` and `op_plan` requires `expires_at >
issued_at`, so a plan issued at 13:30 or later could not outlive its own issue. The builder
answers `WINDOW_CLOSED` instead of writing a plan nobody could confirm. **Consequence, and it is a
finding for Maulik, not a bug:** a break whose 5-minute bar closes at 13:29 or 13:30 is watched,
recorded and never planned, and one closing after ≈ 13:24 leaves under five minutes to confirm. Widening it means either a later `entry_window_end` (a `04` §4.2
change) or letting `expires_at` run past the window (a non-negotiable-1 change) — neither is an
agent's to make. The Beat entry runs 09:30–13:34 rather than to 13:30, because `find_trigger` only
calls the window closed `stale_scan_seconds` after its last minute; without those four minutes a
no-trigger day would never be written. **Reversal.** `plan_o2_gate_free`'s window and the
`ENTRY_WINDOW_CLOSED` branch.

## OP7.6 — O2's premium cap and cost test are dormant at `04`'s ceilings (the OP6.1 pattern) · ⚠ UNREVIEWED

**Finding for Maulik.** Two of `04` §4's refusals cannot fire at the documented defaults:

* **Premium cap** (§4.6, 10 % of sleeve capital). Lots come from the risk budget:
  `lots ≤ budget / risk_per_lot` and `risk_per_lot > E × 0.30 × lot`, so the premium is under
  `budget / 0.30`. With `BASKFY_OPTIONS_RISK_PCT_MAX` 1 %, that is at most `1 / 0.30 = 3.33 %` of
  capital — a third of the cap. It can only bind if the risk-per-trade ceiling is raised above 3 %.
* **Cost test** (§6.4, share ≤ 0.15). The round trip on one lot is ~₹78 and the expected gain is
  `0.60 × E × 65`, so it binds only below `E ≈ 13` points — and O2 buys one step **in** the money,
  where `E` is at least the 50-point intrinsic. On the fixture morning the share is 0.0099.

Nothing in `04` or the code changed. The tests assert that arithmetic, and show the cap
refusing when a ceiling is raised to 5 %, so the rule is proven to work and its dormancy is
written down rather than discovered later. `REJECTED_BUDGET`,
by contrast, is very much live: at ₹1,00,000 of sleeve capital the budget is ₹250 against the
fixture's ₹4,273 of risk per lot. At that risk per lot (an ITM NIFTY call near 200 points) the
0.5 % rule needs **≈ ₹8.5 lakh of sleeve capital for one lot — ≈ ₹17 lakh while §7.5's first-live
multiplier halves the budget** — or a larger `risk_per_trade_pct`. That is a number for the human
track (D7/C3), not an agent's.
**Reversal.** Raise `risk_pct_max` or `risk_per_trade_pct` in `op_sleeve_config` and the cap
becomes reachable.

## OP7.7 — How `06` OP7's "0.45-delta ITM strike on a fast day" is built · ⚠ UNREVIEWED

**Context.** The AC wants the one-step-ITM contract refused for delta. On a chain that agrees with
itself this cannot happen on the low side: the strike is `atm(spot) − 50 < forward`, so
`ln(F/K) > 0`, `d1 > 0` and a call's delta is always above 0.50 — the band's floor is unreachable
while the spot and the option book tell the same story. **Choice.** The fixture is the fast day the
AC names: the collector's minute quotes the index at 25,150 while the chain's own parity forward is
still 25,000 (the book has not followed the move), so the nominally ITM 25,100 CE prices at
`|delta|` 0.4508 → `REJECTED_DELTA`, with the number in the message. The upper bound (0.75) is
reachable the ordinary way, on a quiet expiry-eve chain. **Finding:** in production, a low-side
`REJECTED_DELTA` means spot and chain disagree — worth watching once real minutes exist (OP12).
**Rejected.** Moving the band or `itm_steps` to make the AC literal on a consistent chain.
**Reversal.** The fixture in `TestTheDelta`.

## OP7.8 — `OPTIONS_PLAN` covers O2 too, one alert a day, with the exits in words · ⚠ UNREVIEWED

One alert name for every sleeve (`labels.sleeve` separates them), sent once, when the session is
first written. An O2 plan's summary carries the contract and its limit, lots and sizing mode, the
break it came from (level and time), the premium, the stop and target prices with their rupee
figures, the time stop, the hard exit, the gap-through worst case, the cost share, the expiry and
"nothing is sent without a confirm on the desk". Runbook `docs/runbooks/11-options-plan.md` gained
an O2 section and lost its O1-only title. **Rejected.** A second `AlertName` for O2 (a new alert
name is a new Prometheus rule, a new runbook and a new row in every inventory, for the same event).
**Reversal.** `plan_o2.plan_alert`.

## OP8.1 — O3-A's plan lapses at `min(issued + 30 min, 13:30)`: a new field, `o3a_entry_window_end` · ⚠ UNREVIEWED

`04` §5.1 gives O3-A a *trigger* window (bars closing 10:19–13:00) but no entry-window end, which
`03` §9's `expires_at = min(issued_at + 30 min, entry_window_end)` needs. Using 13:00 would give a
12:55 trigger five minutes to confirm and a 13:00 trigger none. **Choice.** A config field
`expiry_setups.o3a_entry_window_end = 13:30` (§14 and §5.1 updated in the same change, the parity
test asserting it both ways): the last trigger still gets its full 30 minutes, and the 14:45 hard
exit is still 75 minutes past the latest confirm — O2's shape (its trigger window and entry window
both end 13:30, OP7.5). O3-B's is §5.2's own `o3b_window_end` (10:00). **Rejected.** 13:00 (a late
trigger is unconfirmable); no cap at all (a plan could outlive the setup's own timetable).
**Reversal.** Change the field; the builder and the task's window read it.

## OP8.2 — "One O3 a day" is read from O3-B's session, and O3-B is decided first · ⚠ UNREVIEWED

`04` §5.5: "One O3 trade per day across both setups; if both would fire, O3-B (earlier) holds."
**Choice.** O3-A is refused `REJECTED_SLOT_TAKEN` with the reason `O3B_HOLDS` while today's O3-B
session is `PLANNED`, `CONFIRMED`, `OPEN` or `CLOSED`; a `LAPSED` or `SKIPPED` O3-B frees it. The
task decides O3-B before O3-A every minute (`plan_o3.ORDER`, tested), so O3-A always reads a row
O3-B wrote, never a guess. This is the scan's `_o3b_holds` stated on the rows (the scan also asks
that O3-B's candidate be viable, which a `PLANNED` session implies). O3-B's window (to 10:00) ends
before O3-A's first trigger bar (10:19), so the order is never contended. It is separate from the
expiry-day slot (§8.6), which the first *confirmed* O1-or-O3 plan holds. **Rejected.** Letting
both plan and refusing at confirm (two alerts for one trade); holding the day on a lapsed O3-B
(a plan nobody confirmed would block a real setup). **Reversal.** `_O3B_HOLDING` in
`baskfy_core.options.plan_o3`.

## OP8.3 — Which days write an O3 session · ⚠ UNREVIEWED

The O1/O2 rule (OP6.2, OP7.2) carried: an expiry day writes one session per enabled setup —
`PLANNED`, or `SKIPPED` with every reason (setup refusals, `NO_TRIGGER`, `REJECTED_*`,
`STALE_CHAIN`, `REJECTED_MARGIN`); an event expiry is `SKIPPED / EVENT_DAY`; a non-expiry day, a
holiday or a disabled setup (`o3a_enabled` / `o3b_enabled` false) is `NO_SESSION` and writes
nothing; before the setup's first decision time, while its window is unsettled, before a trigger or
before the decision minute's chain is stored it is `NOT_READY`, and first asked after the entry
window with nothing decided it is `WINDOW_CLOSED` — neither writes. **Reversal.** The early returns
in `decide_o3`.

## OP8.4 — O3's margin: the hedged two-leg basket, no transient figure; `ask_margin` takes a protocol · ⚠ UNREVIEWED

`04` §7.4 asks the calculator for O1 and O3. O1 also asks about its transient three-leg prefix,
because wings-then-first-short is costlier than the finished condor. O3's only prefix short of the
whole spread is the long alone, which costs its premium and holds no margin. So
`O3Decision.margin_baskets()` returns the hedged basket and an empty transient one, and `margin_check`
runs on the hedged figure. `baskfy_worker.options.plan.ask_margin` now takes any decision with
`margin_baskets()` (a `Protocol`) instead of `O1Decision` — no behaviour change for O1, whose tests
are green unedited. **Rejected.** A second copy of `ask_margin` for O3. **Reversal.** Type it back to
`O1Decision` and give O3 its own.

## OP8.5 — O3's invalidation level is written to the paisa · ⚠ UNREVIEWED

O3-B's invalidation level is `half_gap = prev_close + gap / 2`, computed from the database's
four-place daily close. House rule 8 (round at write time) applies to it like every price on the
plan, so `exits_for` stores it through `paise` (25,100.00, not 25,100.0000). Found by the database
test, not by the pure one: `Decimal("25100") == Decimal("25100.0000")` in Python, but not in the
JSON the page reads.

## OP9.1 — The desk monitor owns positions and exits; the worker's builders keep raising plans · ⚠ UNREVIEWED

`06` OP9's bullet says the desk process "calls the plan builders", and PACK.11 says the desk's
tick-built bars decide plans. OP6–OP8 built those builders as worker minute tasks behind the same
`BASKFY_OPTIONS_MONITOR_ENABLED` flag, idempotent per date, with the database writes, the margin
calculator and the `OPTIONS_PLAN` alert all tested on a real database; OP6.6 named the reversal
("drop the Beat entry when OP9's desk raises plans itself"). **Choice.** Keep one plan writer: the
worker. The desk's `options_monitor` does what only a live process can — marks open legs from the
websocket, evaluates every exit per tick and on idle passes, and raises the exit plan — and it
reconciles its tick bars to `op_index_minute` (the minutes the worker's builders read), so a plan
and an exit read the same bars. None of OP9's acceptance criteria concern plan raising; all of
them are exits, feed loss and resume, and each is green. **Rejected.** Re-implementing the plan
writes in the desk (a second writer for `op_plan`, and every OP6–OP8 database test re-done against
the desk's adapter); calling the worker from the desk (a cross-tree import the desk does not have).
**Reversal.** Give `NiftyOptionsMonitor` a `plan_at(now)` that calls `plan.decide_o1` /
`plan_o2.decide_o2` / `plan_o3.decide_o3` on `self.bars()` and a desk store for the writes, then drop
the three Beat entries.

## OP9.2 — The strategy module is `app/strategies/nifty_options.py`, not `options_monitor.py` · ⚠ UNREVIEWED

`tests/test_frozen_boundary.py` forbids any module-scope import whose dotted path contains
`strategies.options` — the frozen strangle lab's name (D4). `app.strategies.options_monitor`
contains it, so the name `06` wrote would fail the boundary the moment the runner imported it.
**Choice.** `app/strategies/nifty_options.py` (the desk's route is already `/nifty-options`,
PACK.9); the runner is `app/options_monitor.py` and the clock `app/options_clock.py`, which do not
collide. **Rejected.** Loosening the boundary test to exact-module matching (weakening a D4 guard
to fit a filename); importing the strategy inside a function (legal, but every reader would trip on
the same trap). **Reversal.** Rename, and tighten the boundary test first.

## OP9.3 — A polled index quote counts as the feed; `FEED_LOST` means no NIFTY price from anywhere · ⚠ UNREVIEWED

When the websocket goes quiet, the quote fallback (the swing B10 pattern: ≤ 1 `quote` call per 5 s,
keyed by instrument token, `quote_raw`) delivers the index and every leg as ticks. `04` §8.5's
"no index tick for `stale_index_seconds`" is read as *no NIFTY price by any path*: a desk that
still gets the index every five seconds from the REST endpoint is not blind, and closing a
condor for want of a websocket while the price is known would be an exit the rule did not intend.
`FEED_LOST` fires when both paths are silent — the replay's `o1-feed-lost` has no index price
after 14:04:45 and raises `HARD_EXIT / FEED_LOST` at 14:05:50. **Rejected.** Websocket-only.
**Reversal.** Tag polled ticks and skip them in `NiftyOptionsMonitor._index`.

## OP9.4 — `baskfy_core.options.exits`: one pure `evaluate`, and one EXIT plan per position · ⚠ UNREVIEWED

`06` OP9 names `exits.evaluate`; no such module existed — each sleeve's rule was its own function.
**Choice.** `exits.evaluate(position, marks, index, now)` composes them: the conservative mark
(longs at the bid, shorts at the ask; `None` when a leg has no two-sided quote), staleness at
`stale_quote_seconds` (drops only `PROFIT`/`TARGET`, §8.5), the marked loss for §9.2's budget
breach, the latest *completed* 5-minute close after entry for O2/O3-A invalidation and the latest
1-minute bar for O3-B, and feed loss first. With no mark at all only the hard exit can fire (a stop
cannot be judged on nothing). A verdict becomes one `op_plan` of `kind='EXIT'`, `plan_id =
<entry plan>-X` (deterministic; `ON CONFLICT DO NOTHING`), `status='ISSUED'`, `expires_at` = the
session close (an exit under the entry's confirm must never lapse, PACK.2), with the code, reason,
mark and loss in `detail`; `op_position.exit_plan_id` is set only where it is null. The monitor
never evaluates a position again once it has an exit plan. **Reversal.** The exit plan's shape is
OP10's to consume; change it there and here together.

## OP9.5 — The replay fixtures are priced by Black-76, and their answer key is independent of the monitor · ⚠ UNREVIEWED

`tools/options/make_fixtures.py` writes five days: O1 decaying to `PROFIT`, the same day
restarted at 11:30, O1 open when the index feed stops at 14:05, O2 going nowhere to `TIME_STOP`,
and an O3-A trend to `TARGET`. Legs are priced by `greeks.black76_price` on the spot at a flat 14 %
vol, 1 % either side on the 0.05 tick. The expected exit is computed by `expected()` in that
script, which walks the same tick timeline and applies each rule's text to the numbers — including
every STOP, staleness for the profit rules only, and §8.5's 30 silent seconds after 14:00 — with no
call into `exits`. The desk test also asserts the key names what `06` asks (PROFIT, TIME_STOP at
10:51:00, TARGET, FEED_LOST at 14:05). Two things this found: the first feed-lost fixture opened a
condor at 13:50 with strikes 150 points out, so its ₹0.07 credit tripped condor §7's stop on the
first tick (the monitor was right; the fixture was not a real position), and the harness, loaded as
`replay`, collided with the swing suite's own `replay` module — it is loaded as `options_replay`.

## OP9.6 — The monitor runs by hand until OP15 wires its container · ⚠ UNREVIEWED

`python -m app.options_monitor` is complete (flag first, `BASKFY_SOLE_USER_ID`, the desk's Postgres
adapter, the ticker with new legs followed via `kws.subscribe` + full mode). The compose service and
its scheduling loop (the `swing-monitor` / `twt-auto` shape in `infra/docker`) are deployment, and
deployment is OP15's. With the flag false on the box it would exit 0 anyway.

## OP10.1 — The send procedure is pure (`baskfy_core.options.executor`); the desk supplies a venue · ⚠ UNREVIEWED

`04` §8.2–§8.4 (longs first, attempt 1 at the touch plus a tick, one reprice, cancel; abandon on any
short leg and close what filled in exit order; shorts first on exit; a marketable third attempt for
a risk-reducing close) is `run_entry` / `run_exit` over a `Venue` protocol, built on OP1's
`advance_entry`, `next_exit_leg`, `next_attempt`. Every attempt asserts `never_naked` on the
position it could leave *before* it is sent. That is what makes `06`'s "never-naked across 500 seeded
fill sequences per structure" a pure property test (`test_options_executor.py`, 3 × 500 seeds, both
outcomes exercised). The desk's `DeskVenue` is the only adapter: `gateway.place(...)` per attempt on
the desk's event loop (the executor runs in a worker thread), then the depth-ladder simulator on a
`DRY_RUN` answer. **Rejected.** Writing the procedure inside the desk module (untestable without a
gateway and a database). **Reversal.** Inline the two functions.

## OP10.2 — The options gateways share the desk's risk manager · ⚠ UNREVIEWED

One gateway per sleeve (`options_execute.options_gateway`), gated by that sleeve's
`options_gates.product_gates`, its own journal file, and **`app.main._risk`** — the vbt/twt rule
("one account, one daily-loss cap, one order counter", `vbt_desk.vbt_gateway`). A day's paper legs
(≤ ~20 orders) are small against the 500-a-day cap. **Rejected** (considered first, then reversed
before it landed): a separate `RiskManager` for options — it would let a paper book and the live
weekly book each think they had the whole cap. **Reversal.** Pass a separate `RiskManager`.

## OP10.3 — LIVE is refused with `LIVE_NOT_BUILT` · ⚠ UNREVIEWED

With all four switches on (`02` §3), the gateway would send a real NFO order and answer `PLACED`;
the fill then has to be read back from the broker's order book (partials, the wait, the cancel of
§8.2), which OP10 does not build. Rather than send a real order it cannot track, `execute_entry`
refuses a LIVE plan before any send (409, `LIVE_NOT_BUILT`; tested). Every switch is false, so
nothing in force changes. **Reversal.** Build the live venue (order-status polling through the
gateway's read path) and remove the refusal — a module of its own, before any flag flip.

## OP10.4 — Client ids: `plan:symbol`, `plan:symbol:CLOSE`, and `:R<n>` for every later attempt · ⚠ UNREVIEWED

`04` §8.1 names `plan_id:symbol` and `plan_id:symbol:CLOSE`; the gateway's idempotency map answers a
repeated id `DUPLICATE`, so a reprice (attempt 2), a marketable close (attempt 3) or a retried exit
pass needs its own. Entries number by attempt; closes number by the leg's count of closing orders
already written (`op_order` rows on the leg's opposite side), so a `PARTIAL_EXIT` retried next second
never collides. Built by hand like vbt's (`mint_client_id` refuses a `:` inside a half).

## OP10.5 — Paper fills are immediate, at the resting level; a refusal is an unfilled leg · ⚠ UNREVIEWED

In PAPER the simulator walks the snapshot's depth at once — no `fill_wait_seconds` wait (a paper
order has nothing to wait for; the live venue will). It fills at the resting level's price inside the
limit (`04` §8.4: the ladder, not the limit), so a condor at bids 20.00 / asks 5.10 opens at a 29.80
credit, not the limits' 29.70 (the test pins it). A gateway refusal (`BLOCKED`, `RISK_BLOCKED`) is a
leg that filled nothing: an entry abandons, an exit retries — never an exception mid-sequence.

## OP10.6 — The store is tested on real PostgreSQL, not a sqlite twin · ⚠ UNREVIEWED

`06` OP10 says "`PgOptionsStore` (sqlite twin in tests)". The `op_` schema exists only as the
screener's Postgres migration (partitioned tables, JSONB, arrays); a twin would be a second schema to
keep in step and would not exercise `ANY(?)`, `RETURNING`, or the constraints. The desk tests run on
`BASKFY_TEST_DATABASE_URL` (the migrated `baskfy_test`) and skip without it, as the worker's
database tests do. Found by them: the desk adapter returns a `date` as an ISO string (sqlite's shape),
so `PlanRow.trade_date` is coerced. **Reversal.** Add the twin.

## OP10.7 — Who closes a raised exit: the monitor process's loop, through the executor · ⚠ UNREVIEWED

The monitor raises an EXIT plan (OP9.4) and never sends. `run_session` gained an `act(now)` hook run
after every evaluation pass; the runner hands it `options_execute.run_pending_exits`, which closes
every open position with an exit plan under its entry's confirm (PACK.2), marks the exit plan
`CONFIRMED` and the session `CLOSED` with the rule's code as `closed_reason`. A `PARTIAL_EXIT` stays
open and is retried on the next pass. **Close now** (`POST /nifty-options/close`) raises a `MANUAL`
exit plan and closes at once. The monitor module still contains no placing verb (OP9's scan).

## OP10.8 — The page is the confirm surface; `05` §3's status bar, mark bar and ledger are not built · ⚠ UNREVIEWED

`/nifty-options` shows today's plans with their legs in send order, the **"Confirm — simulated"**
button and the sleeve's sentence verbatim (tested), and open positions with their mark and **Close
now**. Not yet: the four-flag status bar, the Kite token state, the mark on a stop–target bar, spot
against the levels, and the ledger panel — the ledger is OP11's, the rest OP14's hardening. Recorded
in STATUS as NOT done.
