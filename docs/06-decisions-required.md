# 06 — Decisions required

Nine things I cannot decide for you. Each says what it blocks and what I would pick.

---

### D1 · The name — **blocks P0.3, and everything after it**

Three names are in play: the folder says **baskfy**, the live desk answers at
**desk.modelbasket.in**, and Decile's `docs/14` has a full brand workup for **Decile**
(`decile.in`, a trademark checklist, and a product vocabulary — D1, *decile drift*, *Market
Pulse*, *Replay*, the *hold band*).

**Recommendation: Baskfy for the product, keep Decile's vocabulary.** The merged product builds
*baskets*; "decile" names only the ranking step, which is now one stage of five. But that
vocabulary is genuinely good and costs nothing to carry over. Keep `desk.modelbasket.in` as the
internal operator console — it never becomes a customer surface.

**Whatever you pick, pick it in Phase 0.** The rename is ~1,200 files of mechanical work now and
compounding pain later.

---

### D2 · Monorepo or two repos — **blocks P0.2**

**Recommendation: one repo.** Decile's workspace already has exactly the right shape
(`packages/core` I/O-free, `services/*` for everything that touches the world) *and enforces it
with tests*. The desk's `core/regime.py` was independently written to the same rule. They want to
be in the same tree.

Use `git subtree add` rather than copying files — 91 + 22 commits of written-out reasoning are
part of what makes these codebases maintainable, and a copy throws them away.

---

### D3 · ⛔ The regulatory posture — **blocks all of Phase 4**

**This is the big one.** The desk today is personal tooling: *"you are responsible for every
order your account places."* Decile is built to take money from strangers. Between those two
sentences sits a SEBI registration.

Three viable postures:

| | What you sell | Who executes | Registration |
|---|---|---|---|
| **A · Screener only** | data, rankings, backtests | nobody — users act elsewhere | none beyond normal disclaimers |
| **B · Publish baskets, users execute** | basket definitions + a one-click order flow in the **user's own** Kite account | the user, in their own account, via broker OAuth | research analyst / advisory, plus the algo framework via the broker. **This is the smallcase pattern** |
| **C · Manage money** | a managed portfolio | you | PMS — a different business entirely |

**Recommendation: B, and design for it from Phase 0** — never hold client funds or securities,
never place an order the user has not confirmed, always execute in the user's own broker account.
The desk's non-negotiable #1 (*never auto-execute; explicit confirm; plan expires in 30 minutes*)
is already exactly the right architecture for B. That is a happy accident worth protecting.

Two specifics to raise with counsel, not to guess at:
1. The desk's `CLAUDE.md` records *"<10 orders/sec needs no algo registration."* That carve-out
   is for **a person running an algo in their own account.** An algo *supplied to others* through
   a broker API is a different tier — exchange registration through the broker, an algo ID, and
   per-order tagging. Confirm which side of the line B lands on.
2. Whether a ranked basket published to subscribers is research, advice, or neither, and what
   changes when it is personalised to their existing holdings.

**Phases 0–3 are entirely unaffected.** You can get six weeks of real value before you need this
answer — but do not build Phase 4 before you have it.

---

### D4 · What happens to the strangle lab — **blocks P0.7**

4,347 lines, paper-only, gated off, with its own calendar, sizing, adjustment and attribution.

**Recommendation: freeze, don't delete.** Move it to `frozen/strangle/`, keep the systemd
collectors running so the observation series stays unbroken, and exclude it from every gate. It
is real work and it may become a product — just not this one, and not while the compliance
surface is already expanding.

---

### D5 · How much history to backfill — **shapes P1.2's cost and duration**

Decile's `Makefile` suggests `FROM=2011-01-01`. Kite's historical API is rate-limited (~3 req/s,
≤2000-day chunks, ≤3 concurrent) across ~2,000 instruments.

**Recommendation: 2011 for the equity universe.** Momentum backtests need at least one full
cycle, and 2011 covers 2013, 2018 and 2020. Budget several days of wall-clock and run it
resumably (the backfill already supports `ingest_cursor`). Consider a shorter first pass to
2018 purely to unblock P1.5's parity test, then extend.

---

### D6 · Static IP under multi-tenancy — **blocks P4.2 in practice**

Kite allowlists order placement by IP. One account on one static IP is solved. **N users' API
keys egressing from one shared IP is a different conversation with Zerodha** and I do not know
their answer.

**Recommendation: ask Zerodha before Phase 4 is designed, not after.** The answer changes the
deployment topology — a shared NAT egress versus per-tenant egress are very different systems.
Posture B (D3) helps here: the user's own key, the platform's IP.

---

### D7 · Free vs paid, and what gates execution — **blocks P4.7**

Decile has the machinery built (entitlements, Razorpay, GST invoices, tiers) and it is aimed at a
screener. Executing baskets is a different value proposition and probably a different price.

Open sub-questions: is screening free and execution paid? Is the ₹0 tier on (`DECILE_FREE_TIER_ENABLED`
is currently off, and its "limited universe" of NIFTY 50 is invented)? `FREE_MAX_SCREENS = 5` is
also invented — `docs/07` only ever shows the paid figure of 50.

**Recommendation: defer the pricing, but decide the *shape* now** — screening free with a capped
universe, execution paid — because it determines what the entitlement service gates and therefore
what P4.7 builds.

---

### D8 · Migrate the desk's SQLite, or start clean — **blocks P3.9**

~10 MB, of which `trades`, `fills`, `rebalance_orders/versions`, `regime_evaluations` and
`snapshots` are **unrebuildable**. That data is the evidence the strategy is being judged on, and
it is also the only real input the merged system's analytics will have for months.

**Recommendation: migrate, with assertions.** One-way, scripted, row-count and checksum per
table, NAV series recomputed and compared, SQLite file archived forever. Starting clean throws
away the only track record you have.

---

### D9 · Does Decile's public API survive the merge? — **affects P6.5**

It is built, and held shut by two locks — a config flag and a **source constant** a test protects,
requiring a lawyer's written data-redistribution opinion. Its own `CLAUDE.md` notes the
"derived analytics, not raw bars" line is *not airtight*: `ma_20(t)·20 − ma_20(t−1)·20` recovers
`close(t) − close(t−20)`, and nothing bounds what a determined caller could reconstruct across
many `as_of` dates. The webhook sender also has no SSRF protection, contained only by being
unreleased.

**Recommendation: keep it shut through Phase 6.** It adds a legal question and an unsolved
security hole to a plan that already has enough of both, and it serves no user the merged product
needs. Revisit as a deliberate, separate project.

---

## Answer order

```
D1 name ──► D2 repo ──► P0
D5 history ──► P1
D3 SEBI ──────────────────► gates P4   ← start the conversation NOW; it has the longest lead time
D4 strangle ──► P0.7
D8 SQLite ──► P3.9
D6 static IP ──► P4.2      ← ask Zerodha early
D7 pricing ──► P4.7
D9 public API ──► P6.5
```

D3 and D6 both depend on other people and have the longest lead times. **Start those two
conversations in week one**, while Phase 0 and Phase 1 proceed without them.
