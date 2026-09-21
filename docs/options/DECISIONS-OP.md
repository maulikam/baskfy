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
