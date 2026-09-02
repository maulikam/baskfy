# 01 — The method, and what transfers to NSE

Kristjan Kullamägi (Qullamaggie) is a momentum swing trader — not an investor, not a day
trader. He began around 2011 with a few thousand dollars, blew up several accounts, and
compounded into well over $100M trading three setups live on stream. Everything below is what
he has taught publicly; the sources are listed at the end. This document states the method
once, then says for each piece whether Baskfy adopts it as-is, adapts it for NSE/Kite, or
records it and does nothing. `04-business-rules.md` turns each adopted rule into a number.

## 1. The edge, in one sentence

Be in a healthy market, in the strongest stocks in that market, entering only at low-risk
pivot points where the stop is tight relative to the potential move. Win rate on breakouts is
only 25–35%; average winners are 3–5x (often far more) the average loser; a few outliers a
year make the year. Fundamentals barely matter for entries — price, volume and relative
strength are the signal. Nearly all entries happen in the first 60–90 minutes of the session;
the rest of the time is scanning and waiting.

## 2. Setup 1 — the breakout (flag / continuation) · ~70% of his trades

**Precondition — a leader.** A prior move of 30–100%+ in a few weeks (the flagpole). The stock
has already proven itself; the base that follows is a rest, not a reversal.

**The base.** 2 weeks to 2–3 months of consolidation. Volatility contracts, higher lows form,
the 10- and 20-day moving averages catch up to price and the stock "surfs" along them. Tight,
orderly, textbook — the more it looks like a flag the better. Volume dries up inside the base.

**Preferences.** Young stocks (recent listings, first or second base) in a hot theme, with a
high ADR (4–10%). Slow large caps cannot pay for their risk.

**Entry.** The day the stock clears the consolidation, buy the break of the **opening-range
high** — the 1-, 5- or 60-minute first candle, chosen by how fast the name moves. Stop at the
**low of the day** (or the opening-range low for a tighter stop). A close below the stop, or an
intraday hit, is an exit with no hesitation.

**Exit.** Sell one-third to one-half into strength on day 3–5. Trail the remainder with the
10-day SMA (fast movers) or the 20-day SMA (slower), exiting on a close below it. After a few
days move the stop to breakeven so the remainder is a free position that can run for weeks or
months.

| Piece | Baskfy | Where |
|---|---|---|
| Leader precondition, base geometry, MA structure, dry-up | **Adopt.** `detect_flags` on adjusted daily bars | `baskfy_core.swing.setups`, `04` §2 |
| "Young stock / hot theme" preference | **Adapt** as a score component, not a filter: `instrument.listed_on` within 2 years earns points (SW3); theme = the instrument's sector index from `index_member_daily` (SW3 lists the top sectors by breadth) | `04` §2.6 |
| ORH-break entry | **Adopt**, live, 09:15–10:45 IST, 5-minute default | `opening_range`, SW6 |
| EOD alternative when the monitor is not running | **Adapt:** a `BUY_ON_TRIGGER` line at the pivot the next day, stop at the prior day's low — see PACK.2 | `plan`, SW5 |
| LOD / ORL stop, armed the same session | **Adopt** via GTT (non-negotiable 4) | SW7 |
| Partial day 3–5, 10/20 trail, breakeven | **Adopt** | `stops.manage`, `04` §6 |

## 3. Setup 2 — the episodic pivot (EP)

A catalyst gap: blowout earnings with raised guidance, an approval, a huge contract, a product
that changes the story. Gap of at least 10%, ideally much more, on the highest volume in
months or years, **in a stock that has been neglected or basing sideways for many months** —
not one already extended. Institutions cannot build a position in a day, so a real surprise
keeps attracting buyers for days to months. His biggest winners are EPs.

**Entry.** ORH break on the 1- or 5-minute chart in the first minutes, stop at the low of the
day / opening-range low. If the gap cannot hold and the stock goes red on the day, the EP has
failed: out. **Management** as for breakouts, often held much longer.

| Piece | Baskfy | Where |
|---|---|---|
| EOD detection of the gap day (gap ≥ 10%, RVOL ≥ 3, closes strong, neglected before) | **Adopt.** `detect_eps` | `04` §3 |
| Pre-open / at-open detection | **Adapt:** NSE's pre-open session (09:00–09:08) prints an indicative open; at 09:08–09:15 Kite quotes carry it. `swing-premarket` pulls quotes for the liquid universe (≤ 6 calls of 500), `live_gap` flags candidates, the monitor watches them | SW6 |
| "News check" | **Record only.** Baskfy holds no news feed (D10 is data licensing). The candidate row carries a `catalyst` free-text field Maulik fills from the desk page; unfilled is allowed | `03` §2 |
| Upper-circuit lock (no seller, no fill) | **Adapt — the NSE twist.** `locked_upper_circuit` on the row; the trigger verdict `LOCKED_UPPER_CIRCUIT`; the plan skips it with that reason | `04` §3.5, §7 |
| Red-on-the-day = failed EP | **Adopt** | `stops.manage` |

## 4. Setup 3 — the parabolic short

He tells beginners to skip it. Stocks up 50–100%+ in days (sometimes hundreds of percent),
three to five or more consecutive large green days, low float, price far above every MA. He
waits for the first exhaustion — the first red day or a failed bounce — shorts into it with the
stop above the high of the day, covers a third to a half into the first flush, trails with the
10-day MA. Smaller size, only in frothy markets.

| Piece | Baskfy | Where |
|---|---|---|
| Detection (RUNNING / EXHAUSTION) | **Adopt, detect-only.** It is a useful *market-froth* gauge and a "do not chase this" list | `detect_parabolic` |
| Shorting | **Do nothing.** NSE cash equities cannot be shorted for delivery; intraday (MIS) and F&O sit behind product gates that default off. `TRADEABLE_SETUPS` excludes it and SW10 asserts no plan line can carry it | `02` Track C, PACK.1 |

## 5. Universe and scanning

US listed stocks and ADRs from ~$1 up, dollar volume of at least a few million a day, ADR
above ~3.5–4%. Daily scans rank by performance: top gainers 1M (30%+), 3M (60%+), 6M (100%+),
filtered by ADR and liquidity; flags are found by eyeballing charts from that list. A
premarket gap scan (10%+ on abnormal premarket volume) finds EPs. Themes are tracked because
leaders cluster.

| Piece | Baskfy | Where |
|---|---|---|
| Price floor, turnover floor, ADR floor | **Adapt:** ₹20, ₹5 cr/day average turnover (the desk's `MIN_MEDIAN_DAILY_VALUE`), ADR ≥ 3.5% | `LiquidityConfig` |
| 1/3/6M leaders | **Adopt:** `factor_daily.ret_1m/ret_3m/ret_6m` (price return — M27) sort the **Leaders** tab; the flag detector runs on the full liquid universe, not only leaders, because a base can start before the 1M list notices | SW4, `05` §2 |
| "Eyeballing charts" | **Adapt:** the score in `04` §2.6 ranks; the web page shows the last 130 bars as a small chart per candidate so the eye still decides | `05` §2 |
| Themes | **Adapt:** sector index breadth (`market_health_daily` per index) → a "hot sectors" strip; candidates carry their sector | SW4 |

## 6. Market environment and exposure

He does not fight the tape. Breadth (how many stocks are up 25%+ in a month; 52-week highs),
whether the indices sit above their 10- and 20-day MAs, and — above all — his own results: if
breakouts work he presses, if they fail he shrinks and sits out. **Progressive exposure.** In a
strong market, full 2x margin with many positions; in a chop, cash for weeks.

| Piece | Baskfy | Where |
|---|---|---|
| Breadth gauge | **Adopt:** `% up ≥ 25% over 20 bars`, `% at 52-week highs`, `% above 20-DMA` over the liquid universe, written to `sw_market_daily` | `market.breadth_snapshot`, SW4 |
| Index vs 10/20 MA | **Adopt:** NIFTY 500 from `index_snapshot_daily` (NIFTY 50 as fallback) | `market.market_gate` |
| Own results → exposure | **Adopt** as a 4-rung ladder (2/25% → 4/50% → 6/75% → 8/100% of the sleeve), up one rung after 5 closed trades net positive R in a GREEN tape, down one on 3 straight losses, to the bottom on RED | `market.exposure_tier`, `04` §8 |
| 2x margin | **Do nothing.** 100% of the sleeve's cash is the ceiling. No MTF | `02` Track C |
| The desk's R1–R4 overlay | **Separate.** The desk's weekly book keeps its overlay; the swing sleeve has its own ladder. Neither reads the other (`test_regime_names_do_not_collide.py` precedent) | PACK.4 |

## 7. Risk and sizing

Risk per trade 0.25–1% of the account, most often 0.3–0.5%. Size derives from the stop: a stop
4% below with 0.5% risk is a 12.5% position. Positions capped at 20–25% even for the best
setups. Never average down. Never widen a stop. Trade like a robot.

| Piece | Baskfy | Where |
|---|---|---|
| Risk-derived size with named caps | **Adopt** | `sizing.size_position`, `04` §5 |
| The ceilings | **Adapt** into the M4.1 boundary: `BASKFY_SWING_RISK_PER_TRADE_PCT_MAX`, `BASKFY_SWING_MAX_POSITION_PCT_MAX` are **system-only**; the chosen values live in `sw_config` and are validated ≤ ceiling | `03` §1, `.env.example` |
| Never average down / never widen | **Adopt** structurally: `ALREADY_HELD` skip; `apply` takes `max(stop, new_stop)`; the desk's `/swing/execute` refuses a stop below the current one | SW7, SW10 |

## 8. Routine

Premarket: gap scan, news, EP candidates. First hour: entries. End of day: 15–30 minutes on
positions and scans. Weekend: a full scan, a watchlist of a few dozen forming flags, the
levels that would trigger next week. Most of the work is boring scanning and waiting; it took
him about four years of losses before it clicked.

| Piece | Baskfy | Where |
|---|---|---|
| Premarket | `swing-premarket` Beat 08:50 IST: pulls pre-open quotes, refreshes the watch levels to exchange prices, writes `sw_watch` for the day | SW6 |
| First hour | the monitor, 09:15–10:45 IST, a desk process on the `TickBus` | SW6 |
| EOD | `swing-eod` after the nightly `publish` step: detectors, market gate, `stops.manage` on every open position, tomorrow's plan preview, alert email | SW4, SW5 |
| Weekend | `swing-weekend` Saturday 07:00 IST: the full scan over the last 5 sessions, watchlist candidates with levels, the weekly breadth note | SW4 |
| "Four years of losses" | The shadow gate in `02` §3: no real-money flag flip before 20 DRY_RUN sessions and a journal Maulik has read | `02` §3 |

## 9. What NSE changes, in one table

| US / his practice | NSE / Kite reality | What the pack does |
|---|---|---|
| Premarket trading, gap visible from 04:00 | Pre-open auction 09:00–09:08; indicative open in quotes from ~09:08 | EP candidates found at 09:08–09:15, watched from 09:15 |
| No price bands on most names | 5/10/20% bands on non-F&O names; a gap can lock at the upper band with no seller | `locked_upper_circuit` everywhere; a locked name is never a line |
| Shorting is routine | No delivery shorting; MIS squares off at 15:20; F&O only for ~180 names | Parabolic short is detect-only |
| 2x overnight margin | MTF exists but is Track C here | 100% of sleeve cash max |
| T+1, sell same day freely | T+1 settlement; a CNC buy can be sold the same day (becomes intraday) or next day | Day-0 stop-outs are allowed; the GTT handles them |
| $ volume | ₹ turnover — `ohlcv_daily.turnover` (exchange) else close × volume | `turnover_avg` |
| One session, 6.5h | 09:15–15:30 (375 minutes) | `SESSION_MINUTES` in `opening_range` |
| Brokerage ~0 | Zerodha delivery: ₹0 brokerage, STT 0.1% both sides, exchange/GST/stamp ≈ 0.02% | `MIN_TRADE_VALUE` ₹10,000 keeps costs < 0.3% of the trade |

## Sources

* Qullamaggie's 3 Timeless Trading Setups — https://www.scribd.com/document/536284459/3-TIMELESS-setups-that-have-made-me-TENS-OF-MILLIONS-Qullamaggie
* Kullamägi's Swing Trading Strategy Guide — https://www.scribd.com/document/980252718/Kristjan-Kullamagi-s-Trading-Method-Comprehensive-Overview
* The 3 Trading Setups That Turned Kristjan Kullamägi From Security Guard to $100M Trader — https://tradingmomentum.substack.com/p/the-3-trading-setups-that-turned
* How to Trade Like Qullamaggie: Setups, Strategy and Screener — https://breakoutshappen.com/stock-news/how-to-trade-like-qullamaggie-setups-strategy-and-screener
* Qullamaggie Setups & Kristjan Kullamägi Story — https://www.kristjankullamagi.com/
* How Kristjan Kullamägi Trades Breakouts & Episodic Pivots — https://stocksandfuturestrading.com/how-kristjan-kullamagi-trades-breakouts-episodic-pivots-to-make-huge-returns/
* Legends Of Trading: Qullamaggie — https://www.timothysykes.com/blog/qullamaggie/

None of this is investment advice. Two of three breakout trades lose by his own numbers; the
method only works with strict stop discipline and small per-trade risk, which is why the
sizing, the stop rules and the gate are code and not suggestions.
