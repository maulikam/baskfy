# 03 — Merge analysis: the seam, the collisions, the redundancy

---

## 1. The seam is already cut

This is the finding that makes the whole merge tractable.

`app/scoring.py` declares its input contract as a `REQUIRED` list of 30 columns. Decile's
committed reference export (`fixtures/reference-screen-export-2026-08-18.csv`) has 93 columns.
**The desk's 30 are a strict subset of Decile's 93, by exact column name.**

```
desk REQUIRED (30)                         decile export column
────────────────────────────────────────   ─────────────────────
symbol, name, series, date, close           1–8    identical
marketcap, median_volume_one_year           10, 51 identical
absolute_return_{1m,3m,6m,9m,1y}            12–16  identical
sharpe_return_{1m,3m,6m,9m,1y}              17–21  identical
rsi_one_month                               26     identical
volatility_one_year                         27     identical
beta                                        32     identical
circuits_{three_months,one_year}            33, 36 identical
positive_days_percent_{3m,6m}               40, 41 identical
ma_{20,50,100,200}                          47–50  identical
away_from_high_one_year                     45     identical
is_nifty_fno                                64     identical
```

Zero renames. Zero unit conversions at the seam. Decile was reverse-engineered from the same
source the desk consumes, so the two halves were built to the same contract without either
knowing about the other.

**The merge, at its narrowest, is one function:**

```python
# today
scored = scoring.score(scoring.load_scan("data/uploads/scan.csv"))

# after
scored = scoring.score(baskfy.factors.as_of(date, universe="nifty_total_market"))
```

Everything else in this document is what it takes to be allowed to write that line and then to
build a product on top of it.

## 2. What each side has that the other does not

| | Decile | Desk |
|---|---|---|
| Factor computation from raw bars | ✅ 64 factors, 561 lines, verified against a real export | ❌ buys the answer from a competitor |
| Point-in-time index membership | ✅ | ❌ |
| Corporate-action adjustment | ✅ | ⚠️ 1 row, manual entry form |
| Backtest over history | ✅ 1,721-line PIT engine | ⚠️ regime backtest only |
| **Broker order placement** | ❌ read-only `KiteProvider` | ✅ gateway, guards, risk, rate limits, journal |
| **GTT stop management** | ❌ | ✅ vol-scaled on every position |
| **Live holdings / margins / funding check** | ❌ | ✅ incl. the pledged-quantity rule |
| **Position sizing with money** | ⚠️ equal-weight symbol diff | ✅ weights, caps, cash bands, costs, min-trade |
| **Exposure regime overlay** | ❌ (has a *different* regime — see §4) | ✅ R1–R4, 1,598 lines |
| Multi-tenant auth / billing / GST | ✅ | ❌ one shared password |
| Production deployment | ❌ compose file, never deployed | ✅ live, TLS, systemd, fail2ban, backups |
| **Real market data** | ❌ never ingested a bar | ✅ 8,198 trades, 21,276 index rows, a live account |
| Test suite | ✅ 99 suites + 18 Playwright | ✅ 53 suites |
| Written specification | ✅ 5,596 lines of `docs/` | ⚠️ `CLAUDE.md` + a 61-line skill file |

**They are almost perfectly complementary.** The overlap is narrow and the gaps interlock.

## 3. The redundancies — pick one, delete one

### 3a. Two rebalance engines · KMR wins

| | `decile_core/rebalance.py` (293 lines) | `app/rebalance.py` (231 lines) |
|---|---|---|
| Input | ranks + held symbols | scored universe + live holdings + cash + live prices |
| Output | 4 symbol lists, equal weight | concrete orders with quantities and limit prices |
| Knows about | rank buffer | cluster caps, cash bands, min trade value, cost model, short-history half-sizing, runner caps, GTT stops, funding, untouchable guards |

Decile's is a symbol diff; the desk's is a money plan. **The desk's is the real one and Decile's
is a degenerate case of it** — note that the desk already implements the same idea, as
`RETENTION_BUFFER = 5` plus `REPLACEMENT_EDGE = 8.0` (a challenger must beat the incumbent's
score by 8 points, which is a rank buffer with an added quality hurdle).

→ **Keep the desk's engine. Make it pure. Re-express Decile's rank-buffer as one input to it.**
Decile's `plan_rebalance()` becomes a thin adapter so its `/portfolios` UI keeps working.

### 3b. Two Kite integrations · both survive, split by capability

They do genuinely different jobs and must **not** be collapsed into one client:

| | Decile `providers/kite.py` | Desk `kite_client.py` + `core/gateway.py` |
|---|---|---|
| Purpose | historical bars for the whole universe | one account's holdings, margins, LTP, orders, GTT |
| Account | **the system's own** | **the user's** |
| Rate limit | ~3 req/s, Redis-shared across workers | 9 req/s, 9 OPS, 380/min, 2,900/day, in-process |
| Failure mode | retry, circuit-break | refuse, journal, kill-switch |

→ **One broker package, two faces:** `MarketData` (system credentials, shared, cached) and
`Trading` (per-user credentials, never shared, never used for ingestion). The hard rule to write
down now: **a user's access token must never fetch universe data, and the system token must never
place an order.** Decile already encrypts its token at rest; the desk currently writes a plain
`data/.kite_token.json`. The encrypted path wins.

### 3c. Two "regime" concepts · different layers, rename both

This will cause a bug if left alone. They share a word and share nothing else:

| | Decile `decile_core/regime.py` | Desk `app/core/regime.py` |
|---|---|---|
| Scope | **one instrument** | **the whole portfolio** |
| Method | Wasserstein distance of 63-day returns vs the instrument's own best/worst 20% windows | index vs MA + breadth + hysteresis + confirmation days |
| Output | `BULL`/`BEAR`/`NEUTRAL` + two distances | `R1`…`R4` exposure tier + sleeve % |
| Role | a display factor on the factsheet | **it decides how much money is deployed** |

→ Keep both. Rename at merge time: `instrument_regime` (Decile's) and `exposure_tier` (the
desk's). Never let one import the other's name.

### 3d. Two frontends · Next.js wins, Jinja survives as the operator console

280 React files vs 13 Jinja templates. Next.js is obviously the product surface. But the 13
templates are **operationally load-bearing today** — `/analyze`, `/execute`, `/stops`,
`/reconcile`, `/ops` are how a real portfolio gets rebalanced on a real Friday.

→ Do not big-bang this. Keep the Jinja desk running against the merged backend as an internal
operator console, and rebuild page by page in Next. The desk retires the day its last page has a
green React equivalent — not before.

### 3e. Two databases · Postgres wins, and this is the riskiest migration

The desk's SQLite decision was argued from a measurement and was **correct for one user**. Its
own README names the conditions to revisit it: *a second application host, writers on a different
machine, a database into many gigabytes, or a need for replication.* The merge makes the first
two true immediately.

→ Postgres + TimescaleDB, Decile's schema, Alembic migrations. **But:** ~10 MB of the desk's data
is unrebuildable evidence (§02 6). The migration must be a one-way, verified, reversible-by-restore
operation with row-count and checksum assertions, run once, with the SQLite file kept forever.

### 3f. Two config philosophies · reconcile deliberately

The desk puts *every strategy knob in `config.py`* and instructs: "Change behaviour HERE, not in
code." Decile puts behaviour in a `ScreenDefinition` (Pydantic + Zod + JSON Schema) that users
save, share and version. Both are right for their scope.

→ Strategy knobs that belong to **a basket** (weights, caps, buffers, cash bands) move into a
versioned `BasketDefinition`, alongside `ScreenDefinition`. Knobs that belong to **the system**
(rate limits, risk ceilings, kill switch) stay as server config and are never user-editable.
This split is a security boundary, not a preference: `RISK_MAX_DAILY_LOSS_PCT` must not become a
form field.

## 4. What must not be merged

**The strangle / options lab (4,347 lines).** Paper-only, gated off, three underlyings, its own
NSE calendar, sizing, adjustment and attribution engine. It has nothing to do with a
screen→basket→execute product and would multiply the compliance surface (F&O + algo + third
party) at exactly the wrong moment.

→ **Freeze it.** Branch or separate repo, keep the systemd collectors running on the existing
box so the observation series is not broken. Revisit as a Phase 7+ product, if ever. Explicitly
out of scope for everything in `05-merge-plan.md`.

## 5. The three genuinely hard problems

### 5a. Tenancy inversion
The desk is single-tenant to its bones: one `.env`, one SQLite file, one Kite account, one
`DESK_PASSWORD`, one in-process rate limiter, one global kill switch, one `plan_id` namespace.
Making it multi-tenant is not a refactor of one layer — it is `user_id` on every analytics table,
per-user encrypted broker tokens, per-user *and* global risk ceilings, and a per-user rate limiter
that still respects the exchange's per-account caps.

This is the largest single body of work in the plan and it is why Phase 4 is its own phase.

### 5b. The regulatory step-change
The desk's README is unambiguous: *"personal tooling, not investment advice; you are responsible
for every order your account places."* Decile is built to take money from strangers. **Between
those two sentences is a SEBI registration.**

The moment orders are placed in other people's accounts on the strength of your ranking, you are
no longer running personal tooling. The relevant surfaces:

- **Advisory / research.** Publishing a ranked basket others act on is research-analyst
  territory; personalising it to a user's holdings and executing it moves toward investment
  advice. The workable precedent is the smallcase pattern: a SEBI-registered entity publishes
  the basket, the **user's own broker account** executes it via broker OAuth, and the platform
  never holds client funds or securities.
- **The 2025 retail-algo framework.** The desk's `CLAUDE.md` records "<10 orders/sec needs no
  algo registration" and enforces ≤9 OPS. That carve-out is for **a person running an algo in
  their own account.** An algo *provided to others* through a broker API is a different tier:
  exchange registration through the broker, a unique algo ID, and per-order tagging.
- **Static IP.** Already solved for one account. Per-user API keys on a shared static IP is a
  different allowlisting conversation with Zerodha.

**This is a gate, not a task.** Phases 0–3 are entirely legal today: they improve a personal
tool. Phase 4 onward requires an answer. Do not build Phase 4 before you have it.

### 5c. Decile's numbers are unproven
Restated from §01 5 because it gates everything: the parity test skips, the calendar is short,
the NSE endpoints are unverified, and the skip-month formula is actively **refuted** by a 16.7%
gap on CUPID. If Decile's factors are wrong, the merged product places wrong orders with more
confidence than the current manual CSV does.

The desk's `data/uploads/` is the answer key. Phase 1 exists to use it.

## 6. Where the merged product sits

| | Screens 64 factors | Builds a money basket | Executes in your account | Manages stops | Backtests |
|---|:--:|:--:|:--:|:--:|:--:|
| momoindiascreener.in | ✅ | ⚠️ symbol diff | ❌ | ❌ | ❌ |
| smallcase | ❌ | ✅ curated | ✅ | ❌ | ⚠️ |
| Tijori / Screener.in | ⚠️ fundamentals | ❌ | ❌ | ❌ | ❌ |
| **Baskfy (merged)** | ✅ | ✅ | ✅ | ✅ | ✅ |

The thesis in one line: **momoindiascreener tells you what to buy and stops; smallcase buys for
you but won't let you build the basket. The merge is the only product that does both.**
