# 07 — Feasibility study: the name, the stack, and the rules

Docs 01–06 were written from the two trees alone. This document is the pass that checks the plan
against the outside world — the code re-read where a claim is load-bearing, the domain registry,
AWS's current service matrix, and the SEBI / exchange / Zerodha rules as they stand today
(verified 21 Aug 2026, primary sources linked at the end). Three questions, three verdicts:

| Question | Verdict |
|---|---|
| Does **Baskfy** survive as the name? | **Yes — settled.** `baskfy.com` is registered to Maulik; the `.in` family is open for defensive pickup. D1 is closed |
| Does the **stack** survive the move to AWS? | **Yes, minus exactly one piece.** TimescaleDB has no managed home on AWS. Everything else keeps, or maps by configuration |
| Do the **rules** allow the product? | **Phases 0–3 unconditionally.** Phase 4+ now has a clearer, cheaper path than docs 03/06 assumed — and one risk they missed: market-data licensing |

---

## 1. The name

Checked against the registry on 21 Aug 2026, updated the same day:

| Domain | Status |
|---|---|
| `baskfy.com` | **Owned — registered to Maulik (21 Aug 2026). The product's primary domain** |
| `baskfy.in`, `baskfy.co.in` | Available — worth registering defensively; Indian users will type `.in` |
| `baskfy.co`, `baskfy.app`, `baskfy.io`, `baskfy.net`, `baskfy.ai` | Available (optional) |

So D1 is closed: **Baskfy for the product, `baskfy.com` as the primary domain** (pick up
`.in`/`.co.in` defensively when convenient), **Decile's vocabulary
carried over** (D1/decile drift/Market Pulse/Replay/hold band), `desk.modelbasket.in` unchanged
as the operator console. Before spending on brand assets, repeat `docs/14`'s checklist for the
new name: Indian TM registry search and TM-A filing in **Class 9 and Class 36** (~₹9,000/class),
social handles, and a check against the SEBI intermediary registry — that last one matters more
now than when docs/14 was written, because Phase 4's registration will put the name on a SEBI
register (see §4).

## 2. What was re-verified in the code

Claims in docs 01–06 that the merge stands on, checked directly this session:

| Claim | Result |
|---|---|
| Desk's `scoring.py` input is a strict subset of Decile's 93-column export, by exact name | **Confirmed.** `REQUIRED` computes to **29** columns (docs/03 says 30 — the extra was `name`, which scoring never requires); all 29 appear verbatim in `fixtures/reference-screen-export-2026-08-18.csv` (93 columns, counted) |
| Decile's object storage is "Cloudflare R2" | **Better than claimed.** `S3RawArchive` is written against "R2 *or any S3-compatible store*"; the setting is literally `s3_endpoint_url`. AWS S3 is a config value, not a port |
| Decile encrypts broker tokens at rest; the desk writes plaintext | **Confirmed both.** Decile: `providers/tokens.py` + `core/models/integrations.py` (with tests). Desk: `data/.kite_token.json`, `0600` but plaintext — P3.7 stands |
| TimescaleDB usage | Five hypertables (`ohlcv_daily`, `factor_daily`, `index_member_daily`, `index_snapshot_daily`, `market_health_daily`) + **two continuous aggregates** (monthly market-health and index-snapshot rollups, migration `0008`). Notably, `integrity.py` already contains a warning path for "hypertables restored as plain tables" — the codebase half-expects Timescale's absence |
| Nightly chain at 19:30 IST | **Actually 18:45 IST** (`celery_app.py`, Beat in `Asia/Kolkata`), publish-deadline check 20:15, alerts 20:30. Four queues: `ingest`/`compute`/`backtest`/`default` |
| Desk deployment claims (Lightsail Mumbai, static IP, Caddy, fail2ban, SQLite backup via the backup API, 750 writes/0.2 s measurement) | Confirmed in `deploy/README.md` and unit files as described |
| Desk rate caps 9 req/s, 9 OPS, 380/min, 2,900/day | Confirmed in code — and now sits comfortably inside Zerodha's **current** published caps (10/s, 400/min, 5,000/day — raised from the older 200/3,000; see §4) |
| Decile's ADR runtime target | `docs/02` locks "**Docker Compose on a single Hetzner CCX**, Traefik TLS". The AWS requirement supersedes this — the one ADR row the merge rewrites deliberately (see `08-aws-architecture.md`) |

Two doc corrections to carry forward (neither changes a decision): the desk contract is 29
columns, and the nightly chain starts 18:45 IST.

## 3. The stack, component by component

The instruction was: keep the stack unless the feasibility study says otherwise. On AWS it holds
almost everywhere.

| Component | Verdict on AWS | Note |
|---|---|---|
| Next.js 15 / React 19 / TS / Tailwind 4 / shadcn | **Keep** | Runs as a `standalone` container on ECS Fargate. Do **not** use Amplify Hosting: SSE/streaming does not reliably work there and version support lags; the container path is the one the Next.js team documents as supporting everything |
| FastAPI + Pydantic v2 + SQLAlchemy 2 async + Alembic | **Keep** | Container on Fargate (Phase B) / compose on EC2 (Phase A). Mind the ALB idle timeout for the backtest SSE stream |
| Polars numerics | **Keep** | Runs fine on ARM (Graviton) — take the ~20 % cheaper compute |
| Celery + Beat + Redis | **Keep** | The queue topology (`ingest`/`compute`/`backtest`/`default`) maps 1:1 onto Fargate services. One rule: **Beat is a singleton** — its own service, desired-count 1, stop-before-start deploys. EventBridge Scheduler is the AWS-native alternative; not worth rework when Beat is already built and tested |
| PostgreSQL 16 | **Keep** | Amazon RDS for PostgreSQL 16 in Phase B |
| **TimescaleDB** | **Drop at migration time** | The one casualty. The `timescaledb` extension is **not available on RDS or Aurora** (confirmed against the current extension lists) — Timescale's licence keeps it off every managed cloud Postgres. Options and the recommendation in `08-aws-architecture.md` §4; short version: the data is small enough (~10–15 M rows of daily bars) that plain Postgres tables + two ordinary materialized views replace it with no measurable loss |
| Redis 7 | **Keep** | ElastiCache in Phase B — pick **Valkey** (20 % cheaper node-based, and its serverless floor is ~$6/mo vs ~$91/mo for Redis-OSS serverless) |
| Cloudflare R2 | **Swap for S3** | Configuration, not code (§2). S3 also picks up backups and backtest artefacts, with lifecycle rules |
| Razorpay | **Keep** | AWS has no substitute for Indian subscriptions + GST; nothing to change |
| Resend email | **Keep now, SES later** | The compose file already proves an SMTP transport (mailpit locally), so Amazon SES (available in `ap-south-1`, $0.10/1k à-la-carte) is a small adapter whenever volume or bills argue for it. Not a Phase 0–3 concern |
| Auth.js v5 + Argon2id + OTP | **Keep** | Self-hosted auth works unchanged. Do not move to Cognito — it would re-litigate a finished, tested subsystem for no capability gain |
| OTel + Sentry + Prometheus + Grafana | **Keep, re-home gradually** | Phase A: the existing compose `--profile observability` runs on the EC2 box as-is. Phase B: CloudWatch Logs (native to Fargate) + Sentry stay; Prometheus/Grafana move to Amazon Managed Prometheus + Grafana only if the self-run pair becomes a chore |
| Caddy / Traefik | **Phase A keep, Phase B replace** | Caddy stays on the desk box until the desk retires. The product enters through CloudFront → ALB (ACM certificates) |
| SQLite (desk) | **Keep through P3, migrate at P3.9** | Unchanged from docs/05. The desk's own revisit-conditions are triggered by the merge, exactly as its README predicted |
| GitHub Actions CI | **Keep** | Add an OIDC role → ECR push → ECS deploy job in Phase B. No GHCR-vs-ECR religion; ECR is simply nearer the runtime |

**Net:** the stack survives. The move costs one deliberate schema change (Timescale out), one
config value (R2 → S3), and one superseded ADR row (Hetzner → AWS, which docs/04 §6 had already
half-taken by pointing at `ap-south-1`).

## 4. The rules of the road — what changed out there in the last 18 months

This is where the outside world moved the most since either codebase was specced, and it mostly
moved **in Baskfy's favour**.

### 4a. The static-IP requirement is now law, and the desk is already compliant

What the desk did in self-defence (a Mumbai box because "Kite authorises order placement against
an IP allowlist") became a **SEBI mandate**: circular of 4 Feb 2025 ("Safer participation of
retail investors in Algorithmic trading"), implemented by NSE's standards of May 2025, in force
for all brokers since **1 April 2026**. The operative rules at Zerodha today: order placement
only from registered static IPs; **max two IPs per developer account** (one primary, one
secondary), changeable **once per calendar week**; a static IP maps to **one client at a time**;
sharing only within immediate family. Non-order endpoints (historical data, quotes, WebSocket,
holdings) need no registered IP.

Consequences for the plan:

- The Lightsail box's static IP is a **registered regulatory artefact**, not just a convenience.
  Keep the box alive until the very end of P3, and use the **secondary IP slot** to stage any
  move of the execution path (register the new EIP as secondary → prove it → promote). The
  once-a-week change limit makes a botched cutover cost seven days — choreograph it.
- **D6 is now largely answered from public sources.** Per-user *API keys* are not the multi-tenant
  shape; posture B runs the user's OAuth session through *the platform's* app, egressing from the
  platform's registered IP. The remaining D6 question for Zerodha is narrower: is a
  user-confirmed basket flow under a platform app treated like Kite Publisher (see 4c), or does
  it need algo-provider empanelment?

### 4b. Kite Connect got dramatically cheaper, and the backfill is an afternoon, not days

- Execution APIs: **free since March 2025**. The paid tier — **₹500/month per app** — now
  includes both WebSocket streaming *and* historical candles (the old ₹2,000 + ₹2,000 structure
  is gone). Zerodha also advertises free API partnerships for startups building retail products
  (`partnerships@zerodha.com`) — worth one email before Phase 4.
- Current order caps: 10/second (now also the **regulatory** per-client ceiling — above it the
  strategy must be exchange-registered), 400/minute, 5,000/day. The desk's 9 / 380 / 2,900
  self-caps sit inside all three.
- **D5 resizes.** Daily candles are served in ≤2,000-day chunks at ~3 req/s. 2011→today is ~5,700
  calendar days = 3 requests per instrument; ~2,000 instruments ≈ **6,000 requests ≈ 35–40
  minutes** of rate-limited calls, plus NSE corporate-action and constituent fetches. P1.2's
  "budget several days of wall-clock" collapses to "budget an evening, keep it resumable". Depth
  is there — Zerodha's daily candles reach the late 1990s for many NSE names.

### 4c. Phase 4's registration path is concrete now

Two regulatory developments since the desk's CLAUDE.md was written sharpen D3's posture B:

1. **Model portfolios are now, explicitly, Research Analyst territory.** The RA amendment
   (Dec 2024) + SEBI's Jan 2025 guidelines bring "recommendation of model portfolio" inside
   research services, with a required research report per portfolio (methodology, rationale,
   update dates). The entry bar dropped: graduate degree + NISM cert, no experience requirement,
   **part-time RA now exists**, deposits scale with client count (₹1 L up to 150 clients → ₹10 L
   above 1,000), fees capped at ₹1.51 L/family/year for individuals. This is the smallcase
   pattern made legible: publish ranked baskets as a registered RA; the user executes in their
   own account. **D3 = posture B, via RA registration** — it is genuinely reachable for a
   single-founder product, and the conversation with counsel now has a specific register to
   point at.
2. **The algo-provider question has a carve-out worth testing.** The retail-algo framework
   requires *algo providers* (third parties whose orders flow through broker APIs into other
   people's accounts) to be **empanelled with each exchange** — NSE's empanelment machinery is
   live. But Zerodha states the framework "does not apply to Kite-Publisher-style flows because
   the end user places the orders manually." Baskfy's inherited non-negotiable #1 — never
   auto-execute, explicit confirm on a 30-minute plan — is exactly the fact pattern of that
   carve-out. **The counsel question, precisely:** does a user-confirmed rebalance plan fired
   through Kite Connect qualify as "manual" the way Publisher does, or does confirmation of a
   *batch* still make Baskfy an algo provider needing empanelment? Both answers have workable
   designs; they differ in paperwork and lead time. Ask in week one, as docs/06 already insists.

### 4d. The risk docs 01–06 missed: market-data licensing

Two documents, read together, put a real constraint on the *screener* half:

- Zerodha's support docs prohibit displaying Kite Connect data on other platforms: the API "is
  primarily an execution suite, not a data vending service."
- NSE's non-display policy defines even **derived data** ("processed such that the underlying
  market data cannot be identified, recreated, or re-engineered") and still requires a separate
  agreement with NSE Data & Analytics for *external redistribution* of it. EOD data licences
  start around **₹1 L/year**.

Decile's UI shows `close_raw` — the exchange print — plus OHLCV-derived columns, to paying
subscribers. Ranks and scores are defensibly derived; a displayed close price is not. Many Indian
screeners run in this grey zone; Baskfy should not *plan* to. Treat it like D9 is treated:

- Add a launch-gate item beside P6.5: **a written data-licensing position** (counsel opinion
  and/or an NSE Data & Analytics agreement), budgeted at ~₹1 L/year if the agreement is the
  answer.
- Cheap interim mitigations exist and are product-compatible: lead with derived columns (factor
  values, ranks, D1 membership — the things Baskfy is actually for), keep raw OHLCV columns
  gated or delayed, and keep the public API shut (D9's recommendation, reinforced).
- This does **not** touch Phases 0–3: computing factors from licensed-to-you broker data for
  your own account's trading is normal API usage.

### 4e. Timing footnote

The SEBI algo framework's phased rollout completed 1 Apr 2026; NSE updated its algo-provider
evaluation criteria on 30 Apr 2026 and its empanelled-provider list as recently as 31 Jul 2026.
The regime is live and being administered — a 2026 product should treat it as settled ground,
not pending rules.

## 5. What this study changes in docs/05 and docs/06

| Item | Change |
|---|---|
| P1.2 backfill | Re-estimate: an evening of rate-limited calls, not days (§4b). Keep resumability anyway |
| P2.1 "stand up the pipeline on the Mumbai box (or a second box)" | Becomes concrete: the Phase-A EC2 design in `08-aws-architecture.md` §3 |
| P3.9 SQLite → Postgres | Lands on RDS PostgreSQL 16 **without Timescale**; the same migration commit converts the five hypertables and two continuous aggregates (see `08` §4) |
| P4.2 / D6 static IP | Largely answered (§4a): platform egress IP + user OAuth; residual question is the Publisher-carve-out one (§4c). Stage IP moves through the secondary slot |
| P6 launch gate | **Add two items:** the data-licensing position (§4d), and re-registration of production egress IPs as a change-managed step |
| D3 | Posture B, concretely: RA registration + the empanelment question to counsel (§4c). Start both conversations in week one — unchanged advice, sharper brief |
| D5 | 2011 stays right; the cost objection is gone (§4b) |
| Budget lines | Kite Connect ₹500/mo (system data credentials); TM filings ~₹18k (two classes); RA registration + NISM costs; possible NSE data agreement ~₹1 L/yr (gate item) |

Nothing in this study weakens the merge thesis. The two things that got harder since the codebases
were written (static IPs, data licensing) are things the desk's architecture already absorbs or
the plan can gate; the things that got easier (API pricing, RA clarity, backfill speed) are the
expensive ones. The seam holds.

---

**Sources** (verified 21 Aug 2026):
[SEBI circular, 4 Feb 2025](https://www.sebi.gov.in/legal/circulars/feb-2025/safer-participation-of-retail-investors-in-algorithmic-trading_91614.html) ·
[SEBI circular 132, 30 Sep 2025 (glide path to 1 Apr 2026)](https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf) ·
[NSE implementation standards INVG67858](https://nsearchives.nseindia.com/content/circulars/INVG67858.pdf) ·
[Zerodha: static IP / algo compliance](https://kite.trade/forum/discussion/15912/preparing-to-comply-with-sebis-retail-algo-rules-static-ip-ratelimits-order-types) ·
[Kite Connect API FAQs](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/kite-connect-api-faqs) ·
[Kite Connect pricing](https://zerodha.com/products/api/) · [₹500 revision](https://kite.trade/forum/discussion/15015/revising-kite-connect-fees-from-2000-to-500-per-month) ·
[Rate limits & order caps](https://kite.trade/docs/connect/v3/exceptions/) · [Order rate limits](https://support.zerodha.com/category/trading-and-markets/alerts-and-nudges/kite-error-messages/articles/order-rate-limits-on-kite) ·
[Zerodha on redisplaying Kite data](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/can-i-use-historical-and-live-data-taken-from-kite-connect-api-on-other-platforms) ·
[NSE non-display / derived-data policy](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/Non_Display_Policy.pdf) ·
[NSE data price list, Apr 2026](https://nsearchives.nseindia.com/web/mediaattachment/2026-03/NSE_Pricing_file_-_Domestic_clients_20260309171343.pdf) ·
[NSE empanelled algo providers](https://www.nseindia.com/static/trade/empanelled-algo-providers-exchange) ·
[RA/IA framework changes](https://www.mondaq.com/india/securities/1574012/navigating-the-updated-regulatory-framework-for-investment-advisers-and-research-analysts) ·
[smallcase disclosures](https://www.smallcase.com/meta/disclosures/) · [smallcase fees](https://www.smallcase.com/learn/smallcase-fees-and-charges/) ·
[RDS PostgreSQL extension list](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html) ·
[Aurora PostgreSQL extension list](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraPostgreSQLReleaseNotes/AuroraPostgreSQL.Extensions.html) ·
[Kite historical data retention/chunks](https://kite.trade/forum/discussion/14149/historical-data-retention-policy) ·
GoDaddy availability API for the domain table.
