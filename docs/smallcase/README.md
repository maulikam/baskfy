# The smallcase run (SC) — read this second

This folder commissions the next autonomous run after the merge: building the
**smallcase-shaped product layer** on top of Baskfy. The source of truth for what smallcase
actually is — every screen, rule, fee and flow, observed live on 22 Aug 2026 — is
[`../../smallcase-product-documentation.md`](../../smallcase-product-documentation.md) at the
repo root. This folder translates that document into Baskfy modules with acceptance criteria,
the same way `docs/01–08` translated the merge.

**Read order for any agent session:**
`/CLAUDE.md` (the charter — it governs this run unchanged) → `docs/README.md` →
this file → [`02-scope-and-gating.md`](02-scope-and-gating.md) (what is forbidden) →
[`06-module-plan.md`](06-module-plan.md) (the task list) → the remaining docs as each module
cites them. The single prompt that starts the run is in
[`KICKOFF-PROMPT.md`](KICKOFF-PROMPT.md).

| Doc | What it is |
|---|---|
| [`KICKOFF-PROMPT.md`](KICKOFF-PROMPT.md) | The one prompt Maulik pastes into a fresh CLI session |
| [`01-requirements.md`](01-requirements.md) | The smallcase spec mapped into Baskfy terms — what we replicate, adapt, or skip |
| [`02-scope-and-gating.md`](02-scope-and-gating.md) | **The law of this run.** Track A (build now) / Track B (build dark, flag-off) / Track C (forbidden until D3) |
| [`03-data-model.md`](03-data-model.md) | Schema for the basket-product layer, and how it joins the existing desk/screener schema |
| [`04-business-rules.md`](04-business-rules.md) | Fees, min-amount math, volatility buckets, returns/XIRR, market hours, rebalance semantics |
| [`05-ui-spec.md`](05-ui-spec.md) | Routes and screens for the Next.js app, mapped from the observed smallcase UI |
| [`06-module-plan.md`](06-module-plan.md) | **The task list.** SC0–SC12, every module with a Goal and acceptance criteria |
| [`STATUS.md`](STATUS.md) | The live status page for this run — updated at the end of every module |
| [`DECISIONS-SC.md`](DECISIONS-SC.md) | Judgement calls made under the Autonomy charter, numbered by module |

## The one-paragraph version

The merge left Baskfy with a complete private loop: screen → rank → construct → size →
execute → monitor → rebalance, running for one user (Maulik) in one Kite account, with the web
app as a read-only face and the desk console as the only order path. smallcase is the proof
that this exact loop, packaged as **published model baskets that users apply in their own
broker accounts**, is a product people pay for. The SC run builds that packaging: a catalog of
curated baskets with versioned constituents and weights, rebalance timelines and apply-flows,
discovery/filtering, watchlists, investment accounting (XIRR, dividends, drift), a fee and
subscription ledger — everything smallcase does — **but wired single-tenant**, with Baskfy's
own momentum engine playing the role smallcase's in-house manager (Windmill Capital) plays,
and with every multi-tenant, payment-collecting, or web-executing surface built dark behind
flags until D3 has a written answer.

## The key mapping (memorize this)

| smallcase concept | Baskfy implementation |
|---|---|
| smallcase (the basket) | **CuratedBasket** — a published, versioned model portfolio |
| Manager (e.g. Windmill Capital) | The **Baskfy momentum engine** (strategies D1–D6, MomentumScan output) is the first manager; Maulik hand-curated baskets are the second; user-created baskets are private |
| Rebalance version published by manager | A **BasketVersion** cut from a MomentumScan run (or by hand) |
| "Apply rebalance" one-click order | The desk's existing `/analyze` → plan (`plan_id`, 30-min expiry) → `POST /execute confirm=true` — **already the right shape; do not invent a second order path** |
| Broker OAuth session (Tier-2 auth) | The existing Kite token bridge (single account). Multi-user OAuth is Track C |
| User's demat = source of truth, drift repair | The desk's existing reconcile machinery, surfaced as "Fix now" pending actions |
| Subscription / fee-based lock | Entitlement scaffolding behind `BASKFY_SUBSCRIPTIONS_ENABLED=false` (Track B) |
| Transaction fee ₹100+GST | **FeeLedger computation only** — no payment collection (Track B/C) |
| SEBI disclosure furniture | Disclosure **components** (house rule 9), rendered on every performance surface |

## What this run is not

It is not Phase 4. No stranger's money, no stranger's broker account, no payment collection,
no public sign-up, no order placement from the web app. Those wait for D3
(see `docs/06-decisions-required.md` and `02-scope-and-gating.md` here). The run's product
test is simpler and honest: **at the end, Maulik can browse a catalog of Baskfy's own
baskets in the web app exactly the way a smallcase user browses smallcases, watch them,
inspect versions and rebalance timelines, see his real investment accounting, and apply a
rebalance through the desk console — while the codebase is one flag-flip and one legal
answer away from doing it for others.**
