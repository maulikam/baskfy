# 14 — Brand: Decile

## The name

**Decile** — `decile.in` (primary), `decile.trade` (fallback), `usedecile.com` (marketing redirect).

It names the exact action the product performs: rank a universe, take the top slice. It is also
the reference product's own vocabulary — its filter reads *"Top decile stocks of selected index"* —
so the name is already how your users think.

**Why it clears the constraints:**

| Constraint | Status |
|---|---|
| Exchange trademarks (NIFTY / NSE / Sensex / BSE) | Clean — no exchange mark used |
| SEBI advisory implication | Clean — no Advisor/Wealth/Profit/Signal language |
| "Screener" incumbency (screener.in) | Clean — does not compete on their brand term |
| Sector collision in India | Clean — no Indian fintech uses it |
| `.com` availability | ⚠️ Taken by an unrelated US e-commerce analytics firm (Nice Class 42, different market). Low practical risk; `.in` is the correct primary anyway for an India-equities product |

**Pre-launch legal checklist (do before spending on brand assets):**

1. Trademark search on the Indian IP registry, **Class 9** (software) and **Class 36** (financial
   services), for "Decile" and phonetic equivalents.
2. File a TM-A application in both classes. Budget ~₹9,000 per class for an individual/startup.
3. Register `decile.in` + defensive `decile.co.in`, `deciles.in`.
4. Secure handles: `@decile_in` on X, `/decile` on YouTube, `decile.in` on Instagram/LinkedIn.
5. Confirm no conflict with an existing NBFC or RIA name in the SEBI intermediary registry.

## Product vocabulary — the name does work for you

The whole interface can speak one consistent language:

| Concept | Wording |
|---|---|
| A saved screen | a **Decile** ("your Momentum Decile") |
| The top-ranked bucket | **D1** — then D2, D3 … |
| Rank movement between runs | **decile drift** |
| Rebalance action | *"Rebalance to D1"* |
| Rank-buffer hold band | the **hold band** (D1 + buffer) |
| Breadth / market health | **Market Pulse** |
| Backtest | **Replay** — "Replay this screen from 2015" |
| Alerts | **Drift alerts** — "3 names left D1 today" |

That vocabulary is a real moat: it is memorable, it is teachable in one sentence, and it makes
the rank-buffer rebalancing rule (the thing that actually saves users money in turnover) feel
native rather than bolted on.

## Positioning line

> **Decile — rank every NSE stock by momentum, and know exactly which ones still belong in the top
> ten percent.**

Alternative, shorter: *"Momentum, ranked."*

## Tone

Precise, unhyped, numerate. The product's credibility comes from showing its work — verified
formulas, stated assumptions, honest backtests. Copy should never promise outcomes; it should
promise **clarity about the data**. The banned-phrase lint in Prompt 18 enforces this.

## Naming inside the codebase

- Repo / monorepo root: `decile`
- Python namespace: `decile_core`, `decile_api`, `decile_worker`, `decile_providers`
- TS packages: `@decile/web`, `@decile/api-client`
- Docker images: `ghcr.io/<org>/decile-api`, `decile-web`, `decile-worker`
- Database: `decile`
- Env prefix: `DECILE_`
