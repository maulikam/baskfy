# 01 — The method: what TWT-1 is, and what the research measured

Taken from [`research/tight-close/STRATEGY.md`](../../research/tight-close/STRATEGY.md) (11 Sep
2026), which is the spec. Everything numeric here is restated as a named field in
[`04-business-rules.md`](04-business-rules.md); this document is the *why*, `04` is the contract.

Nothing in this method has traded real money.

---

## 1. The pattern

William O'Neil's "three weeks tight": a leading stock that has already made a move goes quiet,
and its **weekly closes stop moving** — three of them within a few percent of each other. It is a
consolidation read at weekly resolution, which is what makes it different from a daily-bar base:
a name can swing 12 % inside the week and still close within 3 % of the previous two Fridays.

Chartink expresses it in five lines. Read literally, on the closed daily bar:

| # | Chartink's line | The reading used |
|---|---|---|
| 1 | `Close > 30` | `close_raw` — an **exchange price**, never the adjusted series |
| 2 | `Close ≥ 1.3 × "3 months ago Low"` | the low of the **calendar month** three months before this session's month, adjusted |
| 3 | `\|Max(3 weekly closes) / Min(3 weekly closes) − 1\| × 100 ≤ 3.01` | today's close as the current week's close, plus the last close of each of the two preceding weeks |
| 4 | `Market cap > 1` | a no-op — not implemented, and its absence changes nothing |
| 5 | `SMA(Volume, 50) ≥ 10,000` | adjusted volume; the average **includes** the signal day |

The alternatives were measured and are worse: completed weeks only instead of the partial current
week, and two or four months back instead of three, and a rolling 63-session low instead of the
calendar month, all score lower against Chartink's own export (`tscan_verify.py`).

## 2. The finding that decides how the signal is computed

**Chartink's backtester knows Friday's close on Monday.** Its weekly and monthly candles are
evaluated as *completed* candles, so its historical export of this scan is not the set of names a
live 15:30 run of the same scan produces.

Measured over 21 Jan → 9 Sep 2026, 9,254 stock-days:

| reading | recall against Chartink's export | precision |
|---|---|---|
| **point-in-time** — today's close is the current week's close | **64.9 %** | 61.5 % |
| look-ahead — the week's *final* close used on every day of that week | 83.1 % | 97.8 % |

The look-ahead reading reaches the same agreement the other two scans reach against their own
exports, which is what identifies the gap as Chartink's candle semantics rather than a plant bug.
The remaining 17 % are the usual plant gaps — 832 no-bar days, 602 names without a clean
50-session volume window — and near-misses at the 3.01 % edge.

**Everything in this sleeve uses the point-in-time reading**, because that is the scan a trader
can actually run at 15:30, and it is also what Chartink's *live* scan shows. `04` §3.2 is written
so that the look-ahead reading is not expressible by a configuration change: it is a different
function, and TW2 keeps it only as the thing the recall test compares against.

**Consequence for the live book, stated once:** the sleeve will name **fewer stocks on some days
than Chartink's screen shows**, and some of them will be names Chartink showed. That is correct
behaviour, not a defect, and `05` says so on the page.

## 3. It is a state, so the event is the first day of it

About 50 names hold the state on an average day — 18 in 2018, 74 in 2021 — and the median stay is
five sessions. Buying a state means buying the same name repeatedly, so the tradable object is the
**entry**: the first session on which the state is true after at least five sessions on which it
was false. There were 26,767 such entries since 2017, 21,378 of them in names turning over ₹2
crore a day.

The five-session gap is a parameter and was measured: ten sessions gives 19.7 % CAGR at −27 %,
twenty gives 15.8 % at −32 %. Five is the value.

## 4. What an entry is worth, before any strategy is built on it

Bought at the next session's open and marked *k* sessions later (`out/entry_slices.csv`);
in-sample is to 2022-12, out-of-sample 2023 →.

| slice | IS n | IS +20 | IS +60 | OOS n | OOS +20 | OOS +60 |
|---|---|---|---|---|---|---|
| all entries | 14,069 | +2.31 | +7.30 | 12,698 | +2.11 | +6.89 |
| turnover ≥ ₹2 cr | 10,355 | +2.16 | +6.87 | 11,023 | +2.19 | +6.39 |
| above 200-DMA | 11,106 | +2.28 | +6.98 | 10,830 | +2.35 | +7.54 |
| within 10 % of the 52-week high | 4,717 | +2.23 | +7.32 | 6,042 | +3.14 | +8.48 |
| base depth ≤ 8 % | 228 | +2.08 | +2.72 | 307 | −0.11 | +3.47 |
| breadth > 40 % | 10,752 | +2.70 | +7.92 | 10,923 | +2.00 | +7.05 |
| the VBT-1-style combo filter | 382 | +1.59 | +2.67 | 637 | +1.64 | +5.86 |

Two things are unusual and both shape `04`.

**The raw entry is positive at every horizon in both halves** — more so than either of the other
two scans this repository has studied. **And the slices barely move it.** The thin names are
*better*, the tightest bases are *worse*, and the trend-and-quality combination that made VBT-1
work makes this one worse (12.3 % CAGR with a 50/200-SMA filter, against 20.9 % without).

That is why `04` §3 has **no trend filters**. VBT-1 has six of them and they are the whole
argument for that sleeve; adding them here would be copying a shape instead of a finding. The
consolidation-after-a-run *is* the filter.

## 5. The strategy

### Signal, at the close of session *t*

The scan is true today, was false on each of the previous five sessions, and the name turns over
at least the liquidity floor on a 20-session average. About forty signals a week; the book takes
at most three a day.

### Entry

**Next session's open, market order.** Measured against the alternatives: a buy-stop at the
15-session base high returns 3.7 % CAGR, a limit at the signal close 11.6 %. This is the third
study in a row in which waiting costs money on a momentum entry, and the first in which the
textbook breakout entry is catastrophic rather than merely worse.

### Sizing

Equal weight, **ten slots**, at most **three new a session**, ranked by turnover when there are
more signals than slots, never more than 1 % of the name's 20-day average turnover. Eight slots
returns 26.3 % at −27 % and fifteen returns 14.7 % at −29 %: concentration pays, and again it is
where the luck lives.

### Exits — there are exactly two, and both are stops

1. **Disaster stop 20 % below the fill**, armed as a GTT the same session. 15 % is equivalent
   (21.6 % CAGR at −23 %); **10 % breaks the strategy** — the names swing more than 12 % inside
   their own bases, so a tight stop converts the median trade into a stop-out.
2. **Trailing stop 20 % below the highest high since entry**, re-computed after every close and
   raised at the exchange when it moves. **This is the exit: 137 of 164 trades.** Tighter halves
   the CAGR and doubles the drawdown (15 % → 9.6 % at −43 %); wider holds for a year at a time and
   thins the trade count to nothing (30 % → 15.5 % on 73 trades). 20–25 % is the plateau.

No target, no partials, no time stop, no moving-average exit. A 50-SMA exit is the
higher-frequency variant of the same edge (543 trades, 15.6 % CAGR, −38.3 % drawdown) and a
21-EMA exit is a loser at 4.7 %. **`04` names no field for any of them**, for the reason the VBT
pack gives: a field that exists is a field somebody turns on.

### Regime

New entries only when **more than 40 % of the tradable universe is above its own 200-DMA**.
35–40 % is the plateau. It turns −43 % into −24.7 % for the loss of nothing: 17.2 % ungated
against 20.9 % gated.

## 6. The numbers

₹10 lakh, 2017-10-16 → 2026-09-09, 25 bps a side, fills on the exchange tick.

| | **TWT-1** | TWT-1 · 50-SMA exit | VBT-1 | MOM-1g | Midcap 150 |
|---|---|---|---|---|---|
| CAGR | **20.9 %** | 15.6 % | 18.2 % | 13.9 % | 15.4 % |
| max drawdown | **−24.7 %** (Sep 2024 → Aug 2025) | −38.3 % | −27.9 % | −31.4 % | −44.2 % |
| Calmar | 0.85 | 0.41 | 0.65 | 0.44 | 0.35 |
| Sharpe | 1.21 | 0.86 | 0.97 | 0.76 | |
| trades · win rate · profit factor | **164** · 40.9 % · 2.71 | 543 · 35 % · 1.68 | 761 · 38 % · 1.55 | 509 · 33 % · 1.56 | |
| avg win / avg loss / avg trade | +55.6 % / −12.4 % / +15.4 % | | +16.5 / −6.1 / +2.5 | | |
| avg hold · time invested | 105 sessions · 78 % | 27 · 67 % | 18 · 63 % | 31 · 71 % | 100 % |
| IS (→ 2022) / OOS (2023 →) | 11.1 % / 36.1 % | 6.2 / 30.1 | 12.6 / 26.0 | 9.5 / 19.8 | 12.3 / 20.2 |
| final equity | ₹54.1 lakh | | ₹44.4 lakh | ₹31.8 lakh | |

Year by year: +3.0 (Oct–Dec 2017), −2.8, +3.5, +13.0, **+53.1**, −3.5, **+66.9**, **+62.8**, −6.2,
+22.9 (to 9 Sep 2026). No year worse than −6 %, which is what the gate and the wide trail buy;
and three years that made everything, on 12–15 trades each.

### Beside the other two sleeves

| monthly-marked | VBT-1 | MOM-1g | TWT-1 | equal-weight | 50/50 VBT-1 + TWT-1 |
|---|---|---|---|---|---|
| CAGR | 18.2 % | 13.7 % | 21.3 % | **18.4 %** | **20.3 %** |
| max drawdown | −24.9 % | −28.4 % | −23.6 % | **−22.1 %** | **−19.3 %** |

Monthly correlations 0.48 / 0.50 / 0.60. TWT-1 shares **no trade** with VBT-1 and two with MOM-1g.
VBT-1 turns over in three weeks and TWT-1 in five months: one is a swing book and the other a
position book, drawn from the same universe by different events. The research's own conclusion is
that **if two of the three ever run, VBT-1 + TWT-1 is the pair**.

## 7. What moves the number

From `out/final_sensitivity.csv`, and this is the table `05` puts on the page:

| change | CAGR | max DD | trades |
|---|---|---|---|
| **TWT-1 as specified** | **20.9 %** | **−24.7 %** | 164 |
| stop 15 / 30 % | 21.6 / 19.4 | −23 / −25 | 186 / 162 |
| trail 15 / 25 / 30 % | **9.6** / 18.2 / 15.5 | −43 / −23 / −28 | 399 / 128 / 73 |
| 8 / 15 slots | 26.3 / 14.7 | −27 / −29 | 133 / 277 |
| rank by nothing / relative volume / day-change | 15.8 / 17.2 / 16.3 | −30 / −31 / −28 | |
| costs 40 / 60 bps a side | 20.3 / 19.5 | −25 / −26 | |
| gate 30 / 35 / 45 / 50 % · none | 18.6 / 20.3 / 13.5 / 14.4 · 17.2 | −31 / −26 / −26 / −35 · −43 | |
| breakout entry (buy-stop at the base high) | 3.7 | −38 | 195 |
| limit-at-close entry | 11.6 | −28 | 174 |
| add the 50/200-SMA trend filter | 12.3 | −29 | 166 |
| entry after ≥ 10 / ≥ 20 sessions out | 19.7 / 15.8 | −27 / −32 | 188 / 199 |
| no liquidity filter / **turnover ≥ ₹5 cr** | 19.5 / **22.5** | −28 / **−27** | 169 / 169 |
| 50-SMA exit instead of the trail | 15.6 | −38 | 543 |

The last-but-one row is why this sleeve ships at a **₹5 crore** liquidity floor where the research
headline used ₹2 crore: at ₹25 lakh over ten slots a line is ₹2.5 lakh, and the 1 %-of-turnover
cap needs ₹2.5 crore of daily turnover before it stops binding. ₹5 crore is the coherent floor at
this size and it scores better. See `04` §3.5 and DECISIONS-TW **TW0.3**.

## 8. The caveats, stated plainly and carried to the page

**164 trades.** Ten of them are 53 % of gross profit; the best single trade is 11 % of it. Three
years carry the CAGR on 12–15 trades each. Sharpe 1.2 on that trade count is a result a different
draw of the same market could easily make 0.6, and a one-year average hold means the 2017–2026
window contains perhaps **fifteen independent observations of the book**. Read 20.9 % as "an edge
with the right sign and a wide confidence interval". The 50-SMA variant's 543 trades and 15.6 % are
the more believable statement of the same thing.

**The trail is the strategy, and it is slow.** Average hold 105 sessions, longest 601 (BOSCHLTD,
Aug 2022 → Jan 2025, +78 %). The GTT has to be re-set every session it ratchets, which is a
process, not a signal — and a 20 % give-back on a ₹2.5 lakh line is a ₹50,000 open loss the book
will sit through **as a matter of routine**. Anyone watching the page needs to have agreed to that
in advance; `05` §2 puts the distance-to-trigger on every open line for exactly this reason.

**Regime and history.** The out-of-sample 36 % is 2023–24; the in-sample 11 % is the guide.
Modelled fills, 25 bps, no interest on idle cash, sparse corporate actions before 2024, one
history, research code. And §2's reproduction question: the scan traded here is the one visible at
the close, not the one in Chartink's backtest export.

**One thing no backtest in this repository has measured:** the book at ₹25 lakh. Every number
above was produced at ₹10 lakh, where the 1 %-of-turnover cap binds on nothing. TW9's run from the
plant's own bars is sized against its own parameter, not against the sleeve's capital, so the two
can never be confused — but the first live line is the first line the cap has ever bound.
