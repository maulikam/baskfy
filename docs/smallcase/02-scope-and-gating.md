# 02 — Scope and gating: the law of this run

The root `CLAUDE.md` says it plainly: **no Phase-4+ (multi-tenant) work happens in this repo
until D3 has a written answer in `docs/DECISIONS-MERGE.md`.** The smallcase product is,
commercially, a Phase-4 product. This run threads that needle by sorting every feature into
three tracks. When a module is ambiguous about which track something belongs to, the
stricter track wins, and the call is recorded in `DECISIONS-SC.md`.

## Track A — build now, fully live (single-tenant)

Everything that serves the one existing user in his own account, or serves no order at all:

- The entire catalog/model layer: CuratedBasket, BasketVersion, constituents, collections,
  managers, computed min-amount/volatility/returns, timelines.
- All read surfaces in the web app: explore, detail, manager, collections, watchlist,
  investments dashboard, investment detail, fees ledger (display), orders history
  (read-only), pending actions, updates feed.
- Investment accounting: ledgers, XIRR, dividends, realized PnL, drift detection.
- Plan generation for invest/apply/exit — producing a desk plan is not placing an order.
- SIP **reminders** (not auto-orders).
- The create/customize builder and private baskets.
- Fee-ledger **computation** (journal entries, no money moves).
- Disclosure components.

## Track B — build dark: flag-off, tested unreachable

Machinery the product needs the day D3 clears, cheap to build correctly now, dangerous to
expose. Each ships behind an env flag that **defaults off**, with a test asserting the
surface is unreachable while off (the D9 pattern — a source constant plus a flag where the
stakes justify it):

| Feature | Flag | What "dark" means |
|---|---|---|
| Subscription plans, entitlement checks, fee-based locks | `BASKFY_SUBSCRIPTIONS_ENABLED` | Plans/Subscription tables exist; every basket renders as Free Access; the lock UI and paywall routes 404 |
| Fee collection (vs computation) | `BASKFY_FEE_COLLECTION_ENABLED` | Ledger rows accrue with `collected=false`; no payment call sites exist at all — the flag gates only the *display* of "will be charged" copy |
| Multi-tenant fields | (no flag — dormant schema) | `user_id`/`broker_account_id` on every order-shaped row per the two laws' dormant clause; a single constant `BASKFY_SOLE_USER_ID` is the only value ever written |
| Public sign-up / onboarding | `BASKFY_PUBLIC_SIGNUP_ENABLED` | Routes exist as 404-when-off; no email/OTP infra is built |
| Web-app execution | **no flag — Track C.** Do not build it dark. The M23-era test (no order route reachable from the web app) stays green and is extended to the new routes |

## Track C — forbidden this run, no exceptions

- Placing any order from the web app, flagged or not.
- Live auto-execution of anything (SIP auto-orders included) — desk non-negotiable #1.
- Broker OAuth for anyone other than the operator's own account; storing a third party's
  API key or token.
- Collecting money: payment gateways, invoices, subscription charges.
- Publishing baskets to anyone outside the repo (public marketing pages, public API — D9
  stays shut).
- Anything that requires representing Baskfy as a SEBI-registered entity.

## The access-gating matrix (implemented as middleware in SC10)

Adapted from the observed product (§8 of the spec). "Broker session" today means the token
bridge reports a live Kite session for the sole account.

| Surface | Requires |
|---|---|
| Explore, detail (free basket), manager, collections | web login only |
| Constituents of a fee-based basket, portfolio report | web login + subscription entitlement (flag-gated; while off, everything is Free Access) |
| Watchlist, investments dashboard, accounting views | web login |
| Order history detail, plan preview (invest/apply/exit) | web login + live broker session |
| Executing a plan | **desk console only** + live broker session + market open + `confirm=true` |
| Rebalance apply outside market hours | blocked with closed-market state + "notify me" |

## Why this is safe to run autonomously

Every Track-A deliverable is something Maulik can already do today with his own account,
re-arranged into better furniture. Every Track-B deliverable is inert without a flag flip
that only a human deploy changes. Track C has no code. The two laws hold: `packages/core`
still touches nothing, and `packages/execution` remains the only path to an order.
