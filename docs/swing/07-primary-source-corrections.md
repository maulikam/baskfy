# 07 — Corrections from the primary sources (2 Sep 2026)

Written after the run had reached SW8, from a second pass over Kullamägi's **own** words
(his site's setups page and FAQ, the stream notes, his "focus list" article) rather than the
secondary summaries `01-method.md` was first drafted from. Five of the pack's rules were wrong
or missing. This document is the spec for **SW9.5** (`06-module-plan.md`), which lands them;
the code and test changes are already written and verified as a patch —
[`patches/2026-09-02-primary-source-corrections.patch`](patches/2026-09-02-primary-source-corrections.patch)
applies cleanly to `53f21c8` (SW8) — but they were **not** applied to the working tree, because
the run was mid-module and the patch changes numbers that `test_swing_contract_*.py` and
`test_swing_backtest.py` pin literally (35 tests). SW9.5 applies the patch and re-pins them
together, as one commit.

## What he actually says (quoted)

| Topic | His words | Where |
|---|---|---|
| Risk per trade | "My risk on most trades is usually 0.25-1%. I rarely risk more than 1% of my account on any trade." · FAQ: "Most of the time 0.3-0.5%. Rarely more than 1%." · when the account was small: "0.5-1.5%" | [setups page](https://qullamaggie.com/my-3-timeless-setups-that-have-made-me-tens-of-millions/), [FAQ](https://qullamaggie.com/faq/), [research #20](https://www.tradingresearchub.com/p/research-20-kristjan-kullamagi-an) |
| Position size | "Most of my positions are 10-20% of account size." · FAQ: "Generally 5%-25% with most being around 10-15%." · "I don't believe you should ever have more than 30% of your account over night in any stock or ETF" | setups page, FAQ |
| **Stop width** | "Stop is always lows of the day and **stop should not be wider than the ATR or ADR of the stock**" | setups page |
| Stops are hard, market | "I always use market stops, never limit stops." · "When a stock is close to my stop I use hard stops" | FAQ |
| Liquidity | "You should not be trading over 1% of daily volume on a stock" | [streams 1-5](https://retailtradersrepository.substack.com/p/kristjan-kullamagi-qullamaggie-stream) |
| **Positions at once** | "In a good market 15-20 positions, or if in a bad market in all cash, but typically will have 5-10 positions." | [streams 6-10](https://retailtradersrepository.substack.com/p/kristjan-kullamagi-qullamaggie-stream-d8c) |
| **Trades per day** | "You do not need to trade 50 things. 1, 2, 3 stocks per day… there's really no need to trade more than that." | [stream quote](https://vinnystone.com/qullamaggie-on-you-just-need-1-2-3-stocks-per-day-not-50-things/) |
| Watchlist funnel | universe 300-600 → weekly wide list 50-100 → weekly focus list 5-20 → daily focus list under 5; rebuilt at least weekly | [focus-list article](https://qullamaggie.net/how-to-consistently-find-top-trade-ideas-creating-a-focus-list/) |
| **Drawdowns** | "I try to contain them at 15-20%, which happen a few times per year." · "50% in 2014 is my biggest drawdown %-wise" (from shorts) | FAQ |
| **Index filter** | long setups only while the index's 10-day MA is above its 20-day (the filter he has said would have cut his 2022 loss by ~90%) | [breakoutshappen](https://breakoutshappen.com/stock-news/how-to-trade-like-qullamaggie-setups-strategy-and-screener) |
| Exits | "sell 1/3 to 1/2 of the position after 3-5 days, and then move the stop to break even"; trail "with the 10- or the 20-day moving average"; "wait for the first CLOSE below the 10-day". Streams add a staged version: first close below the 10-day → sell ¼-⅓, first close below the 20-day → another ¼-⅓, the rest at the 50-day, executed at end of day | setups page, streams 1-5 |
| Re-entry | stopped out of ROKU twice, re-entered: "as long as he gets a good execution, that big win will cover all of his small starter positions" | [streams 66-70](https://retailtradersrepository.substack.com/p/qullamaggie-stream-66-70-review) |
| ADR | his screens use ADR > 5%; 3.5-4% is the floor he names on stream | breakoutshappen, [tikamalma](https://tikamalma.substack.com/p/qullamaggie-swing-trading-setups) |
| Bad markets | "Fewer or no trades" | setups page |
| Win rate / R | ~25-30% win rate; "very common to get moves that are 10-20x+ your initial risk if you are good at setup selection" | setups page, streams |

## Per trade or per portfolio — the answer, with his numbers

The 0.25-1% is **per trade**: what one stop-out costs. There is **no published portfolio-level
"down X% and stop" rule**; what he does at portfolio level is (a) hold 5-10 names typically,
15-20 only in a great market, cash in a bad one, (b) enter at most 1-3 new names a day, (c) move
stops to breakeven within days so older winners carry no open risk, and (d) try to contain
drawdowns at 15-20%. So the most the book can have at *initial* risk at once is
`positions × risk_per_trade` — 1% of the sleeve at rung 0 (2 × 0.5%), 5% at the top rung
(10 × 0.5%) — and only if every stop is hit before any has been raised.

**Yes, the stop is hit often — by design.** With a 25-35% win rate, two out of three entries
stop out. The rules that make that survivable are exactly the ones the first draft got wrong or
left out: the stop is one ADR or tighter (so a loss is a fraction of one normal day's range,
and a −1R is typically 3-6% of a 10-20% position, i.e. 0.3-1% of the account), the count of
open names and of new entries per day is capped, and the whole book is throttled by its
drawdown. Winners are 5-20R. The arithmetic only works with all four.

## What changes (the patch, in one table)

| Rule | Was (04 at SW8) | Now | Code |
|---|---|---|---|
| Stop width | absolute cap 10% | `min(adr_pct × max_stop_adr_multiple [1.0], max_stop_distance_pct [10])`; a wider stop is **skipped**, not sized down | `StopConfig.max_stop_adr_multiple`, `stops.widest_stop_pct`, `plan.build_entries` |
| Index rule in the gate | close above both the 10- and 20-day MA | `IndexReading.long_bias = ma_fast > ma_slow`; `bearish = ma_fast < ma_slow`; RED when bearish, GREEN needs long_bias (`above_both` / `below_both` removed) | `market.IndexReading`, `market.market_gate` |
| New entries per session | none | `SizingConfig.max_new_entries_per_session [3]`; skip reason `SESSION_CAP` | `plan.build_entries` |
| Open positions | ladder top rung 8; `sw_config.max_open_positions` unused by the plan | ladder `((2, 25%), (4, 50%), (6, 75%), (10, 100%))`; plan takes `min(rung, sizing.max_open_positions [10])`; env ceiling `BASKFY_SWING_MAX_OPEN_POSITIONS_MAX` 10 → **20** | `MarketConfig.tiers`, `plan.build_entries`, `.env.example` |
| Drawdown containment | none | `MarketConfig.max_drawdown_pct [15]`, `resume_drawdown_pct [10]`; `market.drawdown_locked` (hysteresis); `exposure_tier(..., drawdown_pct, was_drawdown_locked)` → rung 0, no entries, `ExposureTier.drawdown_locked`; skip reason `DRAWDOWN_LOCKOUT` | `market`, `plan` |
| Position % ceiling | env ceiling 25% | env ceiling **30%** (his "never more than 30% overnight"); default stays 20% | `.env.example` |
| ADR floor | 3.5% | **4.0%** default (his screens: 5%+); `sw_config.adr_min_pct` user-raisable | `LiquidityConfig.adr_min_pct` |
| GTT leg | limit at `GTT_LIMIT_FRACTION` 0.995 × trigger (the weekly book's) | the swing route passes a **3% cushion** (`limit = trigger × 0.97`) so the GTT behaves like the market stop he uses; `place_gtt_stop` gains an optional `limit_fraction` keyword defaulting to the constant, so the weekly book is unchanged | SW7's desk route, `packages/execution/gateway.py` (additive) |

Unchanged, confirmed: risk per trade 0.5% default / 1.0% ceiling (his small-account 1.5% is
noted, not adopted — PACK.9); position 20% default; 1% of turnover cap; LOD stop; partial
⅓ on day 3-5; breakeven; 10/20 trail on the first close below; no averaging down; re-entry after
a stop-out allowed (no cooldown — the plan already only skips *open* names).

## SW9.5 — what the module does

1. `git apply docs/swing/patches/2026-09-02-primary-source-corrections.patch` (touches
   `swing/{config,market,plan,stops}.py` and `tests/test_swing_{market,plan_and_journal,stops}.py`).
2. Re-pin the contract tests to the new numbers — the 35 that go red, by file:
   * `test_swing_contract_book.py` — `TestTheGate` (index semantics: `long_bias`/`bearish`),
     `TestTheLadder.test_the_four_tiers_are_the_documented_ladder` (top rung 10);
   * `test_swing_contract_edges.py` — `TestTheIndexReadingEdges` (the four `above_both`/
     `below_both` cases become `long_bias`/`bearish` boundary cases: equal MAs are neither);
   * `test_swing_contract_detectors.py::TestTheEpThresholdsAreBoundaries::test_a_close_exactly_at_the_open_still_qualifies`
     (its fixture's ADR sits between 3.5 and 4.0 — raise the fixture's range, not the rule);
   * `test_swing_backtest.py` — 24 cases; the planted fixtures were sized under the 8-rung ladder,
     the 10% stop cap and no session cap. Re-plant so each fixture's stop is ≤ 1 ADR, and assert
     the session cap and the drawdown lock-out explicitly (two new cases).
   * `test_swing_docs_parity.py` — green once step 3 is done.
3. Docs: `04-business-rules.md` §1 (ADR 4.0), §5 (session cap; `max_open_positions` in the
   plan), §6 (widest stop), §8.2-8.5 (index rule, tiers, drawdown containment — new §8.5), §9.1
   (skip reasons, `min(rung, cap)`), §9.4 (GTT cushion); `03-data-model.md` §1 (`sw_config`:
   `max_open_positions` default 10, ceiling 20; add `sleeve_peak_inr`, `drawdown_locked`,
   `drawdown_pct` written by `swing-eod` from the sleeve's EOD NAV — `portfolio_nav` is the
   source), §3 (`sw_market_daily.drawdown_pct`, `drawdown_locked`); `01-method.md` §5-§8 tables;
   `05-ui-spec.md` (the funnel numbers on `/swing/watchlist`: auto-watch the top 20 flags by
   score + every EP; the monitor's daily focus is the top 5 by score; the gate badge shows
   "10-day above 20-day"); `.env.example` (two ceilings); `DECISIONS-SW.md` PACK.7 (drawdown
   breaker — ⚠ UNREVIEWED), PACK.8 (GTT cushion — ⚠ UNREVIEWED), PACK.9 (1.0% ceiling kept
   despite his 1.5% small-account range — ⚠ UNREVIEWED).
4. `swing-eod` (SW5's task) computes `drawdown_pct` from the sleeve's peak EOD NAV and passes
   `drawdown_pct` / `was_drawdown_locked` into `exposure_tier`; the desk's `/swing/execute`
   (SW7) passes `limit_fraction=0.97` to `place_gtt_stop`; the desk suite's 16 non-negotiable
   tests and the weekly book's GTT tests must be byte-for-byte unaffected.
5. **AC:** the full core suite green including every re-pinned contract test; `make lint`
   clean; a property test that no `BUY_ON_TRIGGER` line ever has `stop_distance_pct >
   widest_stop_pct(adr)`; a fixture sleeve 15% below its peak plans zero entries and every skip
   says `DRAWDOWN_LOCKOUT`; the same sleeve at 9.9% after a lock-out plans entries again at
   rung 0; a watchlist of ten qualifying flags yields exactly three lines and seven
   `SESSION_CAP` skips; `docs/swing/04` names every field (`test_swing_docs_parity`).

## Sources

* https://qullamaggie.com/my-3-timeless-setups-that-have-made-me-tens-of-millions/
* https://qullamaggie.com/faq/
* https://retailtradersrepository.substack.com/p/kristjan-kullamagi-qullamaggie-stream
* https://retailtradersrepository.substack.com/p/kristjan-kullamagi-qullamaggie-stream-d8c
* https://retailtradersrepository.substack.com/p/qullamaggie-stream-66-70-review
* https://vinnystone.com/qullamaggie-on-you-just-need-1-2-3-stocks-per-day-not-50-things/
* https://qullamaggie.net/how-to-consistently-find-top-trade-ideas-creating-a-focus-list/
* https://www.tradingresearchub.com/p/research-20-kristjan-kullamagi-an
* https://breakoutshappen.com/stock-news/how-to-trade-like-qullamaggie-setups-strategy-and-screener
* https://tikamalma.substack.com/p/qullamaggie-swing-trading-setups
