# 14 — Brand: Baskfy

> **Superseded 21 Aug 2026 by root `CLAUDE.md` D1, and rewritten 22 Aug 2026 (M25).** This file
> named the product **Decile** and the web app read its brand from here — `SITE_NAME` in
> `apps/web/src/lib/site.ts` cited this section verbatim — so the app rendered "Decile" long after
> the merge had renamed everything else. The original naming case is kept below under
> *"Why the name was Decile"*, because the reasoning still explains the product vocabulary D1
> deliberately keeps.

## The name

**Baskfy** — `baskfy.com` (primary, owned by Maulik since 21 Aug 2026). `baskfy.in` and
`baskfy.co.in` to be registered defensively. `desk.modelbasket.in` stays the operator console.

The product is no longer only a screener: after the merge it ranks a universe *and* turns the top
slice into a basket you can hold and rebalance. **Baskfy** names that — the basket — while the
ranking vocabulary below still names how the basket is chosen.

**Namespaces** (D1, enforced by `tools/check-namespace.sh`): `baskfy_core`, `baskfy_api`,
`baskfy_worker`, `baskfy_providers`, `baskfy_execution`; TypeScript `@baskfy/*`; environment
prefix `BASKFY_`; database `baskfy`.

**The brand word is not the vocabulary word.** "Decile" as a *name* is gone. "decile" as a
*statistical bucket* stays everywhere it was already earning its place — `decile_1`…`decile_6` are
a public API contract, "top decile" is how the filter reads, and the blog post *"What a decile
actually measures"* keeps its title and slug. M2 recorded that a blanket rename corrupted exactly
those things before it was caught; M25 renamed the brand and left the vocabulary alone.

**Pre-launch legal checklist (do before spending on brand assets):**

1. Trademark search on the Indian IP registry, **Class 9** (software) and **Class 36** (financial
   services), for "Baskfy" and phonetic equivalents.
2. File a TM-A application in both classes. Budget ~₹9,000 per class for an individual/startup.
3. Register the defensive domains: `baskfy.in`, `baskfy.co.in`.
4. Secure handles: `@baskfy` on X, `/baskfy` on YouTube, `baskfy` on Instagram/LinkedIn.
5. Confirm no conflict with an existing NBFC or RIA name in the SEBI intermediary registry.

These are Maulik's, not an agent's — they are on the human track in `CLAUDE.md`.

---

## Why the name was Decile (kept — the reasoning still explains the vocabulary)

**Decile** — `decile.in` (primary), `decile.trade` (fallback), `usedecile.com` (marketing
redirect).

It names the exact action the product performs: rank a universe, take the top slice. It is also
the reference product's own vocabulary — its filter reads *"Top decile stocks of selected index"* —
so the name is already how your users think.

**Why it cleared the constraints:**

| Constraint | Status |
|---|---|
| Exchange trademarks (NIFTY / NSE / Sensex / BSE) | Clean — no exchange mark used |
| SEBI advisory implication | Clean — no Advisor/Wealth/Profit/Signal language |
| "Screener" incumbency (screener.in) | Clean — does not compete on their brand term |
| Sector collision in India | Clean — no Indian fintech uses it |
| `.com` availability | ⚠️ Taken by an unrelated US e-commerce analytics firm. This is part of why the name moved: `baskfy.com` is owned outright |

---

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
