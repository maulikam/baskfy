# TWT-1 — which of the day's names to take

*12 Sep 2026. Follow-up to `STRATEGY.md`, prompted by the "Quiet for three weeks" page showing
~60 names. Code: `select_tc.py`, `select_sim.py`; outputs `out/select_*`.*

## 1. The page shows the state; the book trades the event, and the book is usually full

Three different numbers, and only the last one is a trade:

| | per day (2017 → 2026) | per year |
|---|---|---|
| names **in the screen** (the page's list) | mean 48, median 34 | — |
| **entries** — first tight day after ≥ 5 sessions out | mean 11, median 4 | ≈ 2,700 |
| entries in names turning over ≥ ₹5 cr a day | mean 7, median **2** | ≈ 2,000 |
| entries the backtest **actually bought** | | **≈ 16** |

So the backtest took about one candidate in a hundred, and the reason is not a clever filter —
it is capacity. Ten slots held for ~105 sessions each can absorb roughly 25 new lines a year.
Of the 17,011 liquid candidates since Oct 2017, 2,506 arrived while the breadth gate was shut and
almost all the rest arrived while the book was full and were never looked at. When a slot does
free up, the next 1–3 candidates of that day are taken, **largest turnover first**. That is the
whole selection rule, and on the page it reads as: ignore the list, look at the two or three
names marked *Entry today*, and only when *open positions < 10*.

## 2. Can we choose better than "largest turnover first"? Tested; no.

For each of the 21,000 liquid entries I computed twenty things a trader could see at that close
(tightness of the three weekly closes, depth of the 15-session base, where the close sits in the
base, volume dry-up, distance from the 52-week high, prior 60-day run, relative strength vs the
Midcap 150, ADR, 50-DMA slope, breadth, price, day change, turnover …) and the outcome of the
TWT-1 trade rule applied to that entry alone (next open, 20 % stop, 20 % trail). Then: does any
feature rank the outcomes, in both halves of the history?

**None does.** Every Spearman correlation is below 0.13 in absolute value and most flip sign
between 2017–22 and 2023–26 (`out/select_features_rank.csv`). Four keep their sign — lower ADR,
shallower base, close nearer the top of the base, volume not drying up — and a score built from
them sorts the out-of-sample outcomes nicely (bottom decile +0.5 %, top decile +11.5 %) and the
in-sample ones not at all (a flat 5–12 % across deciles). Put into the book:

| selection rule (10 slots, 3 a day, gate on) | CAGR | IS / OOS | max DD | trades |
|---|---|---|---|---|
| **largest turnover first** (TWT-1) | **20.9 %** | 11.1 / 36.1 | −24.7 % | 164 |
| the four-feature score first | 6.8 % | 5.0 / 9.1 | −35 % | 192 |
| only the top-30 % scores, turnover first | 15.0 % | 10.5 / 20.9 | −36 % | 157 |
| only the top-15 % scores, score first | 15.0 % | 12.9 / 17.5 | −25 % | 179 |
| only better-than-median ADR / base depth / volume | 11–17 % | | −30 % | 143–170 |
| only close in the **upper half of the base** | 18.3 % | **17.1 / 20.0** | −26 % | 162 |

Ranking by "quality" is worse than ranking by size, and every minimum bar costs return. The one
filter worth a second look is the last row: taking only bases whose last close sits in the upper
60 % of the three-week range gives a lower headline but a far more even split between the two
halves (17 / 20 instead of 11 / 36) — it is trading 2023–24's luck for consistency. It is the
O'Neil rule ("closes in the upper half of the base") and it is cheap to express on the page.
Whether to adopt it is a judgement about which decade you would rather have been robust in; the
data cannot settle it.

## 3. Taking fewer names is worse, not safer

| liquid ≥ ₹5 cr, gate on | max 1 entry a day | max 2 | max 3 |
|---|---|---|---|
| 10 slots | 11.9 % / −31 % | 14.2 % / −34 % | **22.5 % / −27 %** |
| 8 slots | 11.3 % / −30 % | 16.4 % / −30 % | 25.0 % / −29 % |
| 5 slots | 14.2 % / −33 % | 17.2 % / −32 % | 22.9 % / −32 % |

Being stingy on a given day loses: the day's largest name is not reliably the day's best, and a
slot left empty is a slot that misses the next multi-bagger. Fewer slots score higher only by
concentrating — with five slots, ten trades are three-quarters of the profit. **Ten slots, up to
three a day** is the plateau, and it means the book is full most of the time; that is the
strategy working, not a problem to filter away.

## 4. What "meticulous" means here, in practice

1. The candidates are the names marked *Entry today* on the page — not the list. Median two a
   day at ₹5 cr turnover; many days none.
2. Nothing is bought unless the gate is open (> 40 % of names above their 200-DMA) **and** a slot
   is free (< 10 open). Most days that ends the decision.
3. When there are more candidates than free slots, take the largest by 20-day turnover, up to
   three. Do not substitute a judgement ranking for this — every one tested was worse.
4. Optional, defensible: skip an entry whose close is in the lower 40 % of its three-week range
   (§2, last row). Do not add other bars; each one tested cost 3–10 points.
5. Discretion that *is* free: skipping a name for reasons the data cannot see — results within
   the next week, a corporate action, a sector the book already holds three of. None of that
   was in the backtest, so none of it is contradicted by it; keep it rare, because §3 says the
   cost of an empty slot is real.
6. Everything else is the exit: the 20 % trail decides the trade, and the book's job is to hold
   the winners for the 130-session median it took them to pay (losers are out in 35).

The daily routine at ₹25 lakh is therefore: read the page after 15:30, count the open lines, and
if there is room, queue next-open buys for the *Entry today* names in turnover order up to three.
Most days there is nothing to do.
