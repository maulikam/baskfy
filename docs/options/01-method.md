# 01 — The method: three intraday sleeves on NIFTY index options

Commissioned by Maulik on 22 Sep 2026 (README). This file is the method in plain words;
`04-business-rules.md` is the same method as numbers and the tests assert `04`. Where they
disagree, `04` wins and this file is corrected. **Every number below is a proposed default,
⚠ UNREVIEWED**, chosen by this pack; the rejected alternatives are in `DECISIONS-OP.md`.

## 0. The honest limit, once for all three sleeves

SEBI's studies of individual traders in equity derivatives find the segment overwhelmingly
loss-making for individuals — 91 % lost money in FY25, ₹1,05,603 crore of aggregate net losses
after costs (July 2025 study), and the FY25–FY26 study (Aug 2026) says the same shape again. Option
**buyers** lose most often; option **sellers** lose less often and more at a time. Nothing in this
pack is demonstrated to be profitable after costs on NSE. The research behind O1 concluded it is
the most *defensible* candidate (bounded loss, no naked leg, no overnight), not that it wins; O2
and O3 have weaker priors still. That is why v1 is **scans + paper**, why each sleeve has its own
paper period and its own journal, and why a no-trade day is an expected, valid outcome on every
sleeve's page.

## 1. What the three sleeves have in common

* **Underlying:** the NIFTY 50 index. Options only; no futures legs. Strikes, lot size, tick size
  and expiry dates come from Kite's NFO instrument master every night — the lot size (65 at write
  time) never appears as a constant outside test fixtures.
* **Expiries:** NIFTY has one expiry a week, on **Tuesday** (since 1 Sep 2025), and the
  **monthly** contract is the last Tuesday's; when a Tuesday is a holiday the exchange moves the
  expiry to the previous trading day and the master says so. On the last Tuesday of a month the
  weekly and the monthly are the same contract.
* **Intraday only.** Product `MIS`, nothing else; every position is flat by its sleeve's hard exit
  and all of them before 15:00. Nothing in this system is ever exercised or assigned: an ITM long
  option held to 15:30 on expiry day pays STT on its whole intrinsic value (`04` §6.3) — the
  **expiry-day STT trap** — and an agent who relaxes a hard exit meets it.
* **Defined risk.** Every structure's worst case is known to the rupee before entry: a long option
  (the premium), a debit spread (the debit), an iron condor (width − credit). No naked short leg
  exists at any instant, including between leg fills.
* **One confirm, and it covers the exits.** An entry exists only after Maulik confirms a plan
  (`plan_id`, 30 minutes, `confirm=true`). The rule-driven exits — stops, targets, time stops, the
  hard exit — are part of what he confirmed and run without a second click (condor PACK.5, carried
  forward as PACK.2 here). No sleeve confirms its own entry, ever (PACK.3).
* **Event days** (RBI policy, Union Budget, election results, days Maulik marks): no sleeve opens
  a position.
* **Sizing** comes from a per-sleeve **risk budget** — a % of that sleeve's own capital, which is
  **₹0 until Maulik sets it** (the TWT pattern). While it is ₹0, paper runs at **one lot** so the
  record accumulates in R (PACK.6); live requires capital > 0.

## 2. O1 — Hedged premium selling (the condor; monthly and weekly)

**Thesis.** On an expiry day, out-of-the-money premium decays to zero by the close unless the
index travels through it. Wait for the morning to show a contained, non-trending day; sell strikes
outside that morning's range at a modest delta; buy wings so the worst case is fixed; leave well
before the last hour. Most of the edge, if any, is in the days *not* traded.

**Limit.** Expiry-day gamma: a late move through a short strike turns a small credit into the full
width in minutes. The filter, the strike-touch exit and the 14:30 exit are the defence, and none of
them is proven.

**O1-M (monthly)** is exactly the condor pack: `docs/condor/01` §3–§6 and `docs/condor/04` §2–§9
are authoritative. Observe 09:15–09:59; skip on gap > 0.75 %, 45-minute range > 0.8 %, 09:59
outside the 09:15–09:44 range, ER > 0.30, event day; short call/put at 0.20–0.25 delta **and**
outside the opening range; wings 150 points out; credit ≥ 25 % of width; wings bought first; exit
at ½C profit, 1.5C stop, a short-strike touch, or 14:30; shorts closed first; one trade, no re-entry.

**O1-W (weekly)** is the same method on the weekly Tuesdays that are **not** the month's last,
configured separately (`04` §3). Weekly and monthly contracts on their own expiry morning are both
zero-days-to-expiry options on the same index, so the gate and structure start from the same
numbers; what differs is **frequency** (three or four weekly Tuesdays a month against one monthly),
so O1-W carries **half the risk per trade** [0.5 % of sleeve capital against 1.0 %] and its own
weekly loss limit, and its thresholds may diverge once its own Tier 3 sample says so.

**Weekly: expiry day only — not the day before.** Proposed and taken as PACK.7: a position opened
at 10:00 on Monday on Tuesday's contract and closed by 14:30 Monday captures only the intraday
slice of a premium whose largest decay happens **overnight**, which this system may not hold —
while still carrying high gamma. The credit-to-cost ratio is therefore worse than on the expiry
day itself, for no gain in sample size. The collector records Monday chains anyway, so Tier 3 can
test the one-day-before variant later from observed prices rather than argument.

**On the last Tuesday of a month** only O1-M trades; O1-W refuses (the same contract, the same
afternoon — one bet, not two).

## 3. O2 — Directional buying (opening-range breakout with a trend filter)

**Thesis.** When the index breaks its first fifteen minutes' range in the direction of its daily
trend, the move tends to extend more often on those days than on counter-trend breaks. A bought
option makes that a defined-risk bet whose loss is the premium at the stop, and whose payoff is
convex in the move. This is the swing book's opening-range idea (`docs/swing/`, the break of the
first-candle high) and the desk's own options research (`kite-momentum-rebalancer/docs/
OPTIONS_STRATEGIES.md`, Strategy B) applied to the index, with the trend filter doing the job the
swing book's market gate does.

**Limit.** A buyer pays theta and the spread every minute and loses when the move does not come
fast enough; on a quiet day the trade dies by the time stop. Opening-range breakouts on an index
fail often; the edge, if any, is in the size of the winners against many small losers. The SEBI
studies are harshest on buyers. This sleeve's paper period is the longest.

**When.** Any trading day that is not an event day. The opening range is **09:15–09:29** (15
one-minute bars). Entries 09:30–13:30 only. One trade a day.

**Trend filter (daily, fixed at the open).** NIFTY 50's previous close above its 20-day EMA ⇒ only
**calls** today; below ⇒ only **puts**. The index read is **NIFTY 50 itself**, because the sleeve
trades NIFTY — *not* the swing gate's MidSmallcap 400, which is SW17's decision for a book that
trades mid- and small-caps. An agent must not "harmonise" the two (CLAUDE.md, "the decision wins").

**Skip the day** on a gap > 1.0 % from the previous close (the move has happened before the range
exists), an opening range wider than 0.9 % of the index (the stop would be too far), or a
previous-day India VIX close above 22 (premium too expensive for a buyer).

**Trigger.** A completed **5-minute** bar closes above `OR high × (1 + 0.05 %)` (calls) or below
`OR low × (1 − 0.05 %)` (puts), in the trend's direction only.

**What to buy.** One strike **in the money** by one step (50 points for NIFTY; ≈0.55–0.65 delta),
on the **nearest weekly expiry that does not expire today** — on a Tuesday that means next
Tuesday's contract; on a Monday it is the next day's. Liquidity test as O1's (`04` §2.4).

**When to leave.** Whichever comes first: the premium falls **30 %** below the entry fill (stop);
the premium rises **60 %** above it (target); the index closes a 5-minute bar back **inside the
opening range** (the thesis is void); **45 minutes** after entry the premium is not at least 10 %
up (time stop — a buyer does not wait); **15:00** (hard exit). No adding, no averaging down.

**How much.** Risk per trade 0.5 % of sleeve capital, where the risk of one lot is
`premium × 30 % × lot_size + cost reserve`. The whole premium is the true worst case (a gap
through the stop); the plan shows both numbers.

## 4. O3 — Expiry-day setups (two defined-risk debit spreads)

O1 trades the expiry days that are **quiet**. O3 trades the ones that **break**. Both are debit
spreads on the expiring contract — long a strike near the money, short one 100 points further in
the direction — so the loss is the debit and the gain is capped at the width. Expiry-day gamma
works *for* a spread that is right early and moves quickly toward full width.

**Limit.** Two triggers on a small number of days: ~50 expiry Tuesdays a year minus event days
minus the days no trigger fires. Samples accumulate slowly; the sample banner stays up a long time.
The spread's short leg caps the upside a naked long would have had; that is the price of cheaper
entry and lower theta.

**O3-A — range-break debit spread.** Morning range 09:15–10:14 (60 bars), range ≤ 1.2 % of the
index. After 10:15 and before 13:00, a completed 5-minute bar closes beyond the range by 0.05 %,
**and** the path from 09:15 to that bar is efficient (Kaufman ER ≥ 0.40 — the opposite of O1's
condition). Buy the strike nearest the spot, sell the strike 100 points further in the break's
direction, same expiry (today). Required: debit ≤ 55 % of the width.

**O3-B — gap-hold debit spread.** The open gaps ≥ 0.50 % from the previous close, and by 09:45 the
index has not retraced half of the gap (never traded through `prev_close + gap/2` for an up-gap).
At 09:45 buy the strike nearest the spot and sell the one 100 points further in the gap's
direction. Required: debit ≤ 55 % of the width.

**Exits (both).** Spread value ≥ 80 % of width (target); spread value ≤ 50 % of the entry debit
(stop); the index closes a 5-minute bar back inside the morning range (O3-A) or through
`prev_close + gap/2` (O3-B); **14:45** hard exit. Entry: long leg first, then the short; exit: short
first, then the long — never short more than long.

**Exclusivity.** One expiry-day structure per expiry day across O1 and O3: whichever plan is
confirmed first holds the slot, and the other sleeve's scan shows `SLOT_TAKEN`. At most one O3
trade a day. (O1's gate refuses gaps > 0.75 % and trends; O3 wants exactly those, so the overlap is
small by construction.)

**How much.** Risk per trade 0.5 % of sleeve capital; one lot's risk is `debit × lot_size + cost
reserve`.

## 5. How much, for the book as a whole

Each sleeve's capital is its own number and is never sized against the account (the TWT rule).
Book-level limits (`04` §9) sit on top: a daily, weekly and monthly loss limit per sleeve in R, and a
book-wide daily ₹ limit once any capital is set. A breached limit pauses; a pause is a refusal, not
a warning; the scans still run and the paper record stays unbroken.

## 6. The path to real money, per sleeve (detail in `02` §3)

1. Tier 1 (signals on real index minute bars, 2015→) and Tier 2 (modelled prices, labelled) per
   sleeve, separately.
2. Paper on the live chain through the real desk: **O1-M 6 monthly expiries** (≥ 3 traded),
   **O1-W 12 weekly expiries** (≥ 6 traded), **O2 60 sessions** (≥ 25 traded), **O3 20 expiry
   days** (≥ 8 traded), each with zero rule violations.
3. Tier 3 on observed prices from the collector, with its sample on the page.
4. Maulik's written capital and risk decision for that sleeve; his hand on four flags.
5. First live trades at half the risk budget (the swing run's A9 / condor §6.4) for five trades.

## Sources

* NSE NIFTY 50 derivatives contract specification — https://www.nseindia.com/static/products-services/equity-derivatives-nifty50
* NSE circular moving NIFTY derivatives expiry to Tuesday (effective 1 Sep 2025) — FAOP circulars index, https://www.nseindia.com/resources/exchange-communication-circulars (OP0 records the circular number it finds; this pack does not quote one it has not read)
* SEBI measures to strengthen the equity index derivatives framework (Nov 2024: one weekly expiry per exchange, expiry-day ELM, upfront premium) — https://www.sebi.gov.in/legal/circulars/oct-2024/measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html
* SEBI study, FY25 (July 2025) — https://www.sebi.gov.in/sebi_data/attachdocs/jul-2025/1751900271726.pdf
* SEBI study, FY25–FY26 (Aug 2026) — https://www.sebi.gov.in/reports-and-statistics/research/aug-2026/study-profitability-of-individual-traders-in-the-equity-derivatives-segment-fy25-fy26-_103835.html
* Condor sources — `docs/condor/01` § Sources
