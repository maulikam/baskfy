# TWT-1 — can the CAGR be improved?

*12 Sep 2026. Follow-up to `STRATEGY.md` and `SELECTION.md`. Code: `improve_tc.py` (levers in
`vbt/sim.py`, 12 Sep additions); outputs `out/improve.csv`, `out/improve2.csv`, `out/improved_*`.*

Base for everything below is TWT-1 at the live setting — entries in names turning over ≥ ₹5 cr a
day, ten slots, up to three a day, largest turnover first, next-open entry, 20 % stop, 20 % trail
off the highest high, breadth gate > 40 %, 25 bps a side, ₹10 L from 16 Oct 2017:
**22.5 % CAGR, −26.5 % max drawdown, Calmar 0.85, Sharpe 1.28, 169 trades**
(in-sample to 2022: 13.6 %; out-of-sample 2023 →: 35.9 %).

## 1. Where the return is not coming from

`SELECTION.md` closed the door on picking better names: no feature ranks the outcomes, and every
minimum bar costs return. `STRATEGY.md` closed the door on the exit: 10 % stops kill it, a 15 %
trail is a cliff, 25–30 % trails give back too much. What is left is **what the book does with
its capital** — and two facts about the base run point at where money is being left:

- cash sits idle 22 % of the time (exposure 78.5 %): the gate is shut on 37 % of sessions — all of
  mid-2018 through 2019, spring 2020, mid-2022, spring 2025, and Dec 2025 → Apr 2026 — and while
  the book runs down its lines through those stretches the cash it frees earns nothing;
- the winners are big and slow (median winner +31 % over 129 sessions; losers are out in 35)
  while the book's line in them never grows — a name that goes from +10 % to +150 % is still
  one-tenth of the book.

So five levers were tested, none of which touches the signal, the ranking, the stop or the trail.

## 2. The levers, one at a time

| lever | CAGR | IS / OOS | max DD | Calmar | trades | verdict |
|---|---|---|---|---|---|---|
| **base** | 22.5 % | 13.6 / 35.9 | −26.5 % | 0.85 | 169 | |
| idle cash in a liquid fund at 6 % | **24.1 %** | **15.5 / 37.1** | −24.9 % | 0.97 | 169 | adopt |
| same at 7 % | 24.4 % | 15.8 / 37.3 | −24.7 % | 0.99 | 169 | (upper bound) |
| dead money: out if < entry after 40 sessions | 18.3 % | 6.8 / 35.9 | −32.9 % | | 248 | no |
| … after 60 | 21.4 % | 10.7 / 38.1 | −27.3 % | | 194 | no |
| … after 90 | 24.0 % | 11.2 / 43.7 | −32.1 % | | 188 | no — IS worse, DD worse |
| … after 120 | 22.9 % | 13.1 / 37.8 | −26.9 % | | 186 | no |
| pyramid once at +10 %, add 50 % of the line | **24.2 %** | **14.6 / 39.1** | −27.5 % | 0.88 | 161 | adopt |
| … at +15 %, add 50 % | 25.2 % | 11.5 / 47.2 | −26.6 % | | 166 | no — IS worse |
| … at +25 %, add 50 % | 23.9 % | 14.4 / 38.4 | −25.4 % | | 159 | weaker version of +10 |
| … at +15 %, add 100 % | 23.0 % | 8.2 / 47.4 | −30.4 % | | 150 | no |
| size by volatility (∝ 1/ADR, cap 15–20 %) | 21.7 % | 13.8 / 33.3 | −25.1 % | | 171 | no |
| 8 slots instead of 10 | 25.0 % | 15.5 / 39.4 | −29.0 % | 0.86 | 131 | optional (§4) |

**Idle cash.** The book is in cash a fifth of the time, and in the backtest that cash earned
nothing. Parking it in a liquid fund / overnight fund (≈ 6–7 % over this period) is not a
strategy change at all, and it is the only lever that lifts *both* halves by the same amount
(+1.9 IS, +1.2 OOS) while lowering the drawdown — because it is exactly the gate-shut months
where it pays. It is worth +1.6 points of CAGR, ₹7.6 L on the ₹60.7 L end value.

**Dead money.** Cutting a line that is still below its entry after N sessions looks like it
should recycle capital into the next candidate. It does not: the in-sample half is worse at every
N, and the one N that lifts the headline (90 sessions, 24.0 %) does so purely through 2023–24
while deepening the drawdown to −32 %. The names that sit below entry for three months and then work are part of the edge —
the 20 % stop is already the time-out this strategy wants.

**Pyramiding.** One add, at +10 %, of half the original line, bought at the next open, with the
existing 20 %-off-highest-high trail covering the whole enlarged line. Both halves improve (+1.0
IS, +3.2 OOS). It is the classic follow-through rule and it is the only *trade-management* change
that survives the split. The larger or later versions (+15 %, +25 %, add 100 %) buy more of the
line at worse prices and the in-sample half pays for it. Note the add competes with new entries
for cash: when the book is full there is nothing to add with, which is why the trade count drops
from 169 to 161 and why the lever is worth less than its OOS number suggests.

**Volatility sizing.** Bigger lines in calm names, smaller in wild ones — worse in both halves.
The wild names are where the 100 %+ winners come from; shrinking them is the same mistake as
the "quality" filters in `SELECTION.md`.

**Fewer slots.** Eight slots instead of ten lifts the CAGR by 2.5 points and the drawdown by 2.5
points with it; 131 trades instead of 169, and the top ten trades become 58 % of the profit
instead of 51 %. It is concentration, not edge — see §4.

## 3. Combinations

| variant | CAGR | IS / OOS | max DD | Calmar | Sharpe | trades | PF | top-10 share | final ₹ |
|---|---|---|---|---|---|---|---|---|---|
| base @ ₹5 cr | 22.5 % | 13.6 / 35.9 | −26.5 % | 0.85 | 1.28 | 169 | 2.84 | 51 % | 60.7 L |
| yield 6 % | 24.1 % | 15.5 / 37.1 | −24.9 % | 0.97 | 1.36 | 169 | 2.85 | 52 % | 68.3 L |
| **yield 6 % + pyramid +10 % / 50 %** | **24.9 %** | **14.5 / 41.0** | **−23.9 %** | **1.04** | 1.31 | 164 | 3.14 | 53 % | **72.1 L** |
| yield 6 % + pyramid +25 % / 50 % | 24.2 % | 14.9 / 38.2 | −25.9 % | 0.94 | 1.32 | 161 | 2.99 | 58 % | 68.8 L |
| yield 6 % + dead money 60 | 23.3 % | 12.8 / 39.6 | −20.9 % | | 1.29 | 194 | 2.67 | | |
| yield 6 % + 8 slots | 26.6 % | 17.2 / 41.0 | −27.6 % | 0.97 | 1.41 | 131 | 2.87 | 58 % | 81.6 L |
| yield 6 % + pyramid + 8 slots | 25.9 % | 15.1 / 42.6 | −33.4 % | 0.77 | 1.29 | 133 | 2.84 | 65 % | 77.3 L |
| yield + pyramid, stop 15 % | 23.2 % | 12.0 / 40.4 | −29.4 % | 0.79 | 1.18 | 188 | | | |
| yield + pyramid, stop 25 % | 23.6 % | 14.5 / 37.6 | −23.9 % | 0.99 | 1.28 | 163 | | | |
| yield + pyramid, trail 18 % | 23.5 % | 11.8 / 41.7 | −29.5 % | 0.80 | 1.24 | 200 | | | |
| yield + pyramid, trail 22 % | 22.2 % | 15.0 / 32.9 | −27.1 % | 0.82 | 1.09 | 158 | | | |
| yield + pyramid, trail 25 % | 20.3 % | 13.9 / 30.0 | −26.4 % | 0.77 | 1.05 | 105 | | | |

The two robust levers stack: **24.9 % CAGR, −23.9 % max drawdown, Calmar 1.04**, against the
base's 22.5 % / −26.5 % / 0.85 — a better return *and* a shallower drawdown, with the same trade
count and the same 20 / 20 exit. The stop and trail were re-checked on top of the pair and the
2017–2022 answer is unchanged: 20 / 20 is the plateau; 15 % stop or 18 % trail adds trades and
drawdown, 22–25 % trail gives back the winners.

Year by year, recommended variant (base in brackets):

| | 2017 (Q4) | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 (to 9 Sep) |
|---|---|---|---|---|---|---|---|---|---|---|
| return | +4.2 (+4.5) | −0.1 (−2.2) | +6.7 (+2.8) | +23.6 (+22.8) | +56.3 (+62.1) | −5.8 (−7.3) | +68.7 (+65.6) | +71.4 (+54.9) | −2.2 (−2.6) | +26.3 (+25.5) |
| trades | 3 (3) | 24 (24) | 4 (4) | 25 (27) | 10 (11) | 29 (27) | 16 (17) | 15 (15) | 16 (16) | 22 (25) |

Eight of ten years are better; the flat years (2018, 2019, 2022, 2025) are where the liquid-fund
yield shows, 2024 is where the pyramid shows. 2021 is the one year that gives back (−6 points):
the pyramid adds took cash that the base used for fresh entries in a year when every fresh entry
worked — the cost of the add competing with new lines, noted above.

## 4. What is *not* recommended, and why the 26.6 % row is a trap

`yield 6 % + 8 slots` prints the highest CAGR in the table (26.6 %) and its drawdown is not much
worse. It is not adopted because it is the same edge on fewer, bigger lines: the top ten trades
carry 58 % of the profit instead of 51 %, and with the pyramid on top (65 %, −33 % DD) the book is
one bad quarter from a drawdown nobody would sit through. `STRATEGY.md` already showed the
concentration cliff (five slots: ten trades are three-quarters of the profit). Ten slots at
₹25 L is ₹2.5 L a line, which is also the size that stays inside the ₹5 cr-a-day liquidity bar
without the fill rule mattering. If the account grows and the liquidity bar is raised, revisit —
not before.

Dead-money exits, volatility sizing, tighter or wider stops and trails, and the larger pyramids
are all rejected for the same reason: none of them improves the 2017–2022 half, and a change that
only improves 2023–24 is a change that only improves the two best years the small-cap tape has
ever had.

## 5. Recommendation and the mechanics

Adopt two changes to the *operation*, none to the scan or the exit:

1. **Idle cash is never idle.** Whatever is not in a stock sits in a liquid / overnight fund
   (LIQUIDBEES or a liquid-fund unit that Kite can hold and pledge). Buying a stock means
   redeeming the equivalent that day; the settlement lag (T+1 for the ETF, T+1 to T+2 for a
   liquid fund) is covered by keeping one line's worth of cash unparked, or by pledging. This is
   worth ≈ +1.6 points and it is the change with no downside — it is what a treasury would do.
2. **One add per trade.** When a line's close is ≥ 10 % above the entry price and it has not been
   added to, buy 50 % of the original rupee value at the next open, *if there is cash to do it*.
   The GTT stop is then re-set for the enlarged quantity at the same level (20 % below the
   highest high since entry) — under the desk rules every buy carries a GTT, so the add is a
   second buy order plus one GTT modify. Never a second add; never an add on a name that is
   below its entry.

Expected effect, 2017–2026: 22.5 % → **≈ 24.9 % CAGR**, max drawdown −26.5 % → **−23.9 %**,
₹10 L → ₹72 L instead of ₹61 L. Both halves of the history improve.

What has not changed, and is worth restating: 164 trades over nine years is a small sample; the
top ten trades are still over half of the profit; the out-of-sample number (41 %) is 2023–24 and
should not be read as the run-rate; and the in-sample number (14.5 %) is the honest floor to plan
around — it is index-like return with a shallower drawdown, and the year the tape gives a 2023,
the book takes it. The improvement here is a real one because it is the same in both halves, but
it is two points, not a different strategy.

## 6. Changes for the TW run

If adopted, `KICKOFF-PROMPT.md` / the sleeve's `04-business-rules` get two lines:
idle cash parked in `LIQUIDBEES` (or the fund chosen in QUESTIONS), redeemed on the day a buy is
queued; and the pyramid rule above as a second signal type on the positions page ("add today",
qty = 50 % of the original, shown only when the close ≥ entry × 1.10 and no add yet), with the
GTT re-set as part of the same desk action. The scan, the ranking, the three-a-day cap, the
20 % stop and the 20 % trail are unchanged.
