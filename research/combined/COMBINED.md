# Combining the scans — does trading several together beat the best one alone?

*12 Sep 2026. Code: `combo.py` (uses the new multi-sleeve `vbt.sim.run_book`); outputs `out/combo.csv`,
`out/combo.log`, `out/combo_addendum.*`, `out/combo_book_*`. Same bars, capital (₹10 L from
16 Oct 2017), costs (25 bps a side) and breadth gate as the single-strategy studies.*

"Combine" can mean three different things, and they come out very differently:

| | what it means | best result | vs TWT-1 alone (22.5 % / −26.5 %) |
|---|---|---|---|
| **A. split capital** | separate books, each sleeve on its own slice | 20.8 % / −24.6 % (50/50), 21.9 % / −24.4 % (80/20) | lower CAGR, slightly shallower DD |
| **B. shared book** | one pool of slots, every scan's signals compete, each line managed by its own exit | 19.2 % / −27.4 % | worse on both |
| **C. confluence** | only trade a name when two scans agree; or trade the union under one exit | 21.9 % / −30.6 % (TWT after a VBT signal); everything else 7–13 % | no gain, more DD |

The sleeves are the finished ones: VBT-1 (18.2 % / −27.9 %, 761 trades, PF 1.55), TWT-1 at the
₹5 cr live bar (22.5 % / −26.5 %, 169 trades, PF 2.84), MOM-1g (13.9 % / −31.4 %, 509 trades).

## A. Split capital — the only combination that behaves, and it is a blend, not a gain

| separate books, daily rebalanced | CAGR | IS / OOS | max DD | Calmar | Sharpe |
|---|---|---|---|---|---|
| TWT-1 alone | 22.5 % | 13.6 / 35.9 | −26.5 % | 0.85 | 1.28 |
| VBT-1 alone | 18.2 % | 12.6 / 26.0 | −27.9 % | 0.65 | 0.97 |
| 50 / 50 TWT + VBT | 20.8 % | 13.4 / 31.5 | −24.6 % | 0.85 | 1.27 |
| 70 / 30 | 21.6 % | 13.6 / 33.4 | −23.6 % | 0.91 | 1.32 |
| 80 / 20 | 21.9 % | 13.6 / 34.3 | −24.4 % | 0.90 | 1.32 |
| ⅓ each TWT + VBT + MOM-1g | 18.8 % | 12.3 / 27.9 | −26.5 % | 0.71 | 1.17 |
| *with the IMPROVE.md levers (cash at 6 %, TWT pyramid):* | | | | | |
| TWT-1 improved alone | 24.9 % | 14.5 / 41.0 | −23.9 % | 1.04 | 1.31 |
| 50 / 50 improved TWT + VBT | 23.3 % | 15.4 / 34.8 | −20.1 % | 1.16 | 1.35 |
| 70 / 30 | 24.0 % | 15.1 / 37.4 | −21.2 % | 1.13 | 1.37 |
| 80 / 20 | 24.4 % | 14.9 / 38.7 | −22.0 % | 1.11 | 1.36 |

Buy-and-hold slices (no rebalancing) are within 0.3 points of these.

The blend sits between the two sleeves on CAGR, as a blend must, and the diversification benefit
is small: the Sharpe ratio moves from 1.28 to 1.32–1.37 and the drawdown improves by two to four
points. The reason is that the two are not very different bets. Their monthly returns correlate
at 0.52; both are long-only small/mid-cap momentum with the same breadth gate, so they are in cash
in the same months and long in the same tapes. Where they do differ is in the flat years —
2022: VBT +16 %, TWT −7 %; 2018: VBT −19 %, TWT −2 % — which is where the drawdown improvement
comes from, and it is real but modest. Adding MOM-1g only dilutes: it is index-like return with
the same correlation.

If the goal is a smoother ride at a small cost in CAGR, 70/30 or 80/20 TWT/VBT in **separate**
books is the version to run. If the goal is the highest CAGR, TWT-1 alone is still it.

## B. One shared book — worse than either sleeve alone

This is the version that sounds most natural — one ₹25 L book, ten slots, take whatever any scan
throws up, manage each line by its own rule — and it fails, in every configuration:

| one book, 10 slots unless stated | CAGR | IS / OOS | max DD | trades (TWT / VBT / MOM) | P&L by sleeve |
|---|---|---|---|---|---|
| TWT first, then VBT | 19.2 % | 8.4 / 36.1 | −27.4 % | 177 / 49 | TWT +₹38.6 L, VBT −₹1.0 L |
| VBT first, then TWT | 10.7 % | 8.8 / 13.0 | −28.4 % | 98 / 440 | TWT +4.9, VBT +9.8 |
| 12 slots | 17.9 % | 7.0 / 35.0 | −28.4 % | 201 / 59 | |
| 15 slots, 5 a day | 14.2 % | 6.6 / 25.8 | −25.6 % | 251 / 70 | |
| 20 slots, 5 a day | 14.4 % | 6.8 / 25.7 | −27.4 % | 341 / 103 | |
| TWT capped at 6, VBT capped at 6 | 17.0 % | 13.8 / 21.2 | −29.0 % | 111 / 346 | TWT +26.5, VBT +4.0 |
| TWT capped at 5, VBT capped at 5 | 18.6 % | 11.5 / 28.8 | −31.0 % | 92 / 409 | TWT +27.4, VBT +8.0 |
| TWT + VBT + MOM | 18.2 % | 6.8 / 35.8 | −37.7 % | 157 / 34 / 81 | |
| TWT + VBT + MOM, 15 slots | 11.9 % | 3.5 / 24.5 | −38.4 % | 244 / 56 / 115 | |
| TWT + VBT, cash at 6 % | 20.9 % | 10.1 / 37.7 | −25.8 % | 177 / 49 | |
| TWT (+pyramid) + VBT, cash at 6 % | 21.6 % | 7.4 / 45.0 | −29.6 % | 168 / 46 | |

Two things go wrong, and both are about capacity rather than signals:

1. **The slow sleeve starves the fast one.** TWT-1 holds a line for ~100 sessions; VBT-1 for ~18.
   With TWT given priority, the tight-base lines sit in the slots and VBT-1 gets 49 trades in nine
   years — the scraps — and loses money on them, because VBT-1's edge is a thin one (PF 1.55) that
   needs the full flow of breakout days to add up. With VBT given priority the opposite happens:
   VBT churns 440 trades through the slots, TWT gets 98 entries instead of 169 and misses the
   ones that mattered, and the book does 10.7 % — worse than *either* sleeve alone.
2. **Widening the book does not fix it.** More slots means smaller lines; TWT's return is made by
   ten names becoming big, and at 15–20 slots each of those is a fifteenth of the book.
   Per-sleeve caps (5+5, 6+6) are just the split-capital case run inside one account, with the
   same equity/10 sizing — and they land below the split (17–19 %) because the two sleeves now
   also share the day's three-entry limit and the cash.

Interleaving strategies with different holding periods in one set of slots is the classic mistake
here; the data says it as plainly as it can.

## C. Confluence — two scans agreeing is not a better signal

| signal | signals | CAGR | IS / OOS | max DD | trades |
|---|---|---|---|---|---|
| TWT-1 entry **and** in the momentum state (close ≥ 1.3 × 30/90-day-ago low) | 11,549 | 12.7 % | −0.4 / 33.5 | −47.5 % | 219 |
| TWT-1 entry **and** a VBT-1 signal in the last 20 sessions | 2,127 | 21.9 % | 10.4 / 40.4 | −30.6 % | 188 |
| VBT-1 signal **and** was three-weeks-tight in the last 10 sessions (VBT exit) | 1,342 | 10.8 % | 15.0 / 4.9 | −30.2 % | 532 |
| … same, TWT exit | | 12.2 % | 12.6 / 11.3 | −27.7 % | 205 |
| VBT-1 signal **and** tight in the last 25 (VBT exit) | 2,102 | 10.8 % | 13.3 / 7.3 | −35.0 % | 626 |
| VBT-1 signal and **not** tight in the last 25 (the complement) | 4,191 | 8.3 % | 3.2 / 15.6 | −35.9 % | 748 |
| union VBT-1 ∪ TWT-1, one exit (20 % stop / 20 % trail) | 23,949 | 11.9 % | 8.7 / 16.7 | −31.7 % | 190 |
| union, one exit (limit entry, 12 % stop, 21-EMA) | | 7.2 % | 2.5 / 13.6 | −35.0 % | 1,066 |

- A tight base that is *also* a momentum name (already up 30 % off its low) is a much worse tight
  base: 12.7 % with a −47 % drawdown and a negative in-sample half. The scan's edge is in bases
  that have not run yet.
- A tight base that had a volume breakout in the previous month does as well as any tight base
  (21.9 % vs 22.5 %) on an eighth of the signals — the breakout adds nothing; it is the base that
  matters.
- The volume breakout *out of* a tight base — the textbook "VCP breakout" — is not a better
  breakout. Both the tight and the not-tight subsets of VBT-1 do 8–11 %, far below the 18.2 % of
  the whole, and the halves flip sign (tight: IS 15 / OOS 5; not tight: IS 3 / OOS 16). VBT-1 is a
  volume strategy whose thin per-trade edge needs the full flow of signals to keep the slots full;
  cutting the flow in half by any criterion hurts more than the criterion helps.
- Pouring both scans into one exit rule is the worst of all: each exit was fitted to its own
  entry's holding period, and neither survives the other's names.

## What to do with this

1. **Do not merge the sleeves into one book.** Run TWT-1 as designed. If VBT-1 is run too, it
   gets its own capital, its own ten slots and its own P&L — a second sleeve in the Baskfy sense,
   not a second feed into the same slots.
2. **A 70/30 or 80/20 split (separate books) is a defensible smoothing choice**, worth two to four
   points of drawdown and about a point of CAGR against TWT-1 alone — with the IMPROVE.md levers
   on, 24.0–24.4 % / −21 to −22 % versus 24.9 % / −23.9 %. It is a preference about the ride, not
   an improvement in the return; the two sleeves are 0.5-correlated and share the gate.
3. **Confluence filters are off the table**: every "both scans agree" rule tested is a filter,
   and every filter on these capacity-bound books costs more than it selects (the same conclusion
   as `tight-close/SELECTION.md`).
4. MOM-1g stays a watchlist. Adding it to anything dilutes.

The pattern across all three studies today is the same one: these strategies make their return
by keeping ten slots filled with the largest candidates of a narrow signal and letting a handful
of them run. Anything that changes *which* names fill the slots — a quality score, a second scan's
agreement, another sleeve's signals — makes it worse; only what the book does with its cash
(IMPROVE.md) has moved the number, and only by two points.
