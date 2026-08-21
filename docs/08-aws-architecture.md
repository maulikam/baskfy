# 08 — AWS architecture: Mumbai, in two phases

The constraint is given: Baskfy runs on AWS. This is less a migration than an admission of where
the system already lives — the desk is on AWS Lightsail in `ap-south-1` today, and its
`CLAUDE.md` says "host in AWS Mumbai for the lowest RTT" about Kite's own floor. What follows
replaces exactly one row of Decile's locked ADR (`docs/02`: "Docker Compose on a single Hetzner
CCX, Traefik TLS") and keeps the shape of everything else.

The design principle: **spend follows tenancy.** While Baskfy is one user (Phases 0–3), it is a
single EC2 box and ~$50/month. The managed, multi-AZ, autoscaling shape arrives only when other
people's money does (Phase 4+). Both phases keep the same containers, the same compose-style
topology, and the same two laws from `04` §2 — which is what makes the second phase a
re-plumbing, not a rewrite.

All prices are `ap-south-1`, on-demand, USD/month at 720 h, checked 21 Aug 2026 (sources at the
end; the two derived numbers are flagged).

---

## 1. What is fixed by the outside world

Three external facts shape everything:

1. **The registered static IP** (see `07` §4a). Order placement is legal only from an IP
   registered with Zerodha — two slots, one change per week. The desk's Lightsail static IP is
   the registered primary today. Any topology must produce **one stable egress IP for the order
   path**, and every move of that path must be staged through the secondary slot.
2. **The IST clock.** The nightly chain (18:45), publish deadline (20:15), alerts (20:30) and the
   desk's collection jobs are IST wall-clock times tied to the NSE session. Everything schedules
   in `Asia/Kolkata`; nothing depends on machine-local time.
3. **Kite's rate floor.** ~3 req/s historical, 1 req/s quote, 10 OPS hard cap per client. Nothing
   in this architecture needs to be fast; it needs to be *always on and in Mumbai*.

## 2. The two-phase shape at a glance

| | Phase A (P0–P3, one user) | Phase B (P4–P6, product) |
|---|---|---|
| Compute | 1× EC2 `t4g.large` + the existing Lightsail desk box | ECS Fargate (Graviton): `web`, `api`, `worker`, `ingest-worker`, `beat` |
| Database | Postgres 16 **+ Timescale** in a container (zero code change) | **RDS PostgreSQL 16, no Timescale** (§4) |
| Cache/broker | Redis container | ElastiCache **Valkey** `cache.t4g.micro` |
| Object storage | S3 (raw archive, backups, artefacts) | same bucket, same lifecycle |
| Ingress | Caddy on the box (as the desk does today) | CloudFront → ALB (ACM), WAF at P6 |
| Order-path egress | EC2 EIP (and the desk box's IP until P3 ends) | fck-nat or NAT Gateway with EIP (§5) |
| Email | none needed / SES sandbox | SES production (`ap-south-1`) |
| Observability | compose `--profile observability` as built | CloudWatch Logs + Sentry; AMP/Grafana only if earned |
| Cost | **≈ $50/mo** + desk's existing ~$24 + Kite ₹500 | **≈ $205–265/mo** + staging ≈ $60–90 (§7) |

## 3. Phase A — the single box (lands P2.1, carries P1–P3)

```
   you (browser) ── desk.modelbasket.in ──► Lightsail 4 GB (UNTOUCHED through P3)
                                            Caddy · FastAPI desk · SQLite · systemd
                                            static IP = REGISTERED PRIMARY (orders)
                                                 ▲
                                                 │ P2: MomentumScan over HTTPS
                                                 │ (service token; upload stays as fallback)
   you (browser) ── pipeline.baskfy.com ──► EC2 t4g.large (2 vCPU / 8 GB, Graviton)
                                            docker compose:
                                              caddy · api · worker · beat
                                              postgres (timescale image) · redis
                                              [prometheus · grafana]
                                            EIP (future SECONDARY order IP)
                                            EBS gp3 100 GB · DLM daily snapshots
                                                 │
                                                 ▼
                    S3  s3://baskfy-archive  (raw NSE files, pg_dump, SQLite backups)
```

Decisions inside the box:

- **`t4g.large` (8 GB), not medium.** Polars factor computation over the full universe and the
  Celery `compute` queue want memory headroom; the desk's own sizing doc showed how tight 4 GB
  gets. T4g is burstable — leave **unlimited mode on** for backfill night and accept the few-rupee
  surcharge rather than a stalled chain.
- **Keep the Timescale container image in Phase A.** `infra/docker/compose.yml` already pins
  `timescale/timescaledb:2.17.2-pg16` (arm64 exists); nothing needs to change while the database
  lives in compose. The Timescale exit happens once, at P3.9, into RDS (§4).
- **S3 from day one, because it is a config value.** `s3_endpoint_url` points at real S3;
  `S3RawArchive` doesn't know the difference. Bucket layout: `nse/{kind}/{date}.csv` (the
  archive's own contract), `backups/pg/`, `backups/sqlite/`, `backtests/`. Versioning on;
  lifecycle: archive objects → Standard-IA at 30 days.
- **Nightly `pg_dump` to S3 after the 20:15 publish check**, plus DLM EBS snapshots (7-day
  retention). The desk's own SQLite backup discipline continues unchanged on its box; extend its
  offsite `rsync` target to the S3 bucket so both databases' evidence lands in one place.
- **No SSH keys — SSM Session Manager**, IMDSv2 required, security group closed to the world
  except Caddy's 443. The desk box already runs fail2ban + basic-auth; the pipeline box exposes
  only the operator pages behind the same discipline.
- **DNS now, product later:** Route 53 hosted zone for `baskfy.com` (~$0.50/mo) with
  `pipeline.baskfy.com` on the EIP. `desk.modelbasket.in` stays exactly where it is.
- **P2 seam concretely:** the desk calls `GET /internal/momentum-scan?date=…&universe=…` on the
  pipeline box over HTTPS with a service token, gets the 29 columns byte-exact (P2.2's fixture
  test), and keeps CSV upload as the fallback path. Two boxes, one seam, no shared disk.

**Phase A monthly:** EC2 $32.26 + EBS 100 GB $9.12 + EIP $3.60 + snapshots ~$2 + S3 ~$1 +
CloudWatch ~$2 + Route 53 $0.50 ≈ **$50 (~₹4,400)**, beside the desk's existing Lightsail
(~$24) and Kite Connect's ₹500. A 1-year no-upfront commitment trims the EC2 line ~25 % when the
box proves itself.

## 4. The database decision — leaving Timescale at the RDS border

The `timescaledb` extension is **not available on Amazon RDS for PostgreSQL or Aurora** (checked
against both current extension lists — Timescale's licence keeps managed clones off every cloud).
Three honest options:

| Option | What it means | Verdict |
|---|---|---|
| **A. RDS PostgreSQL 16, drop Timescale** | Convert 5 hypertables to plain tables; 2 continuous aggregates become ordinary materialized views refreshed by the worker | **Recommended, at P3.9** |
| B. Self-managed Timescale on EC2/ECS forever | Keeps the schema, but you patch, replicate and back up your own Postgres under paying customers | The desk's own philosophy argues no: don't run a service for features you can't measure needing |
| C. Tiger Cloud (managed Timescale, has Mumbai) | Zero schema change, but a second cloud vendor, DPA, and bill inside a fintech stack | Viable fallback if A meets a surprise |

Why A costs almost nothing here: the whole time-series load is **daily bars for ~2,000–4,000
instruments** — roughly 7–15 M rows per wide table across 2011–2026, growing ~500 k rows/year.
That is small-table territory for vanilla Postgres with the composite PKs the schema already has;
hypertable chunking earns nothing at this volume. The codebase agrees more than the ADR does:
`integrity.py` already carries a warning path for "hypertables restored as plain tables", and the
continuous aggregates are two monthly rollups (`market_health_monthly`, `index_snapshot_monthly`)
that a `REFRESH MATERIALIZED VIEW CONCURRENTLY` after the nightly publish replaces exactly.

The change, enumerated (this is the entire blast radius, all found this session):

1. `infra/docker/compose.yml` — image `timescale/timescaledb:2.17.2-pg16` → `postgres:16`
   (Phase B envs; local dev can keep either).
2. `services/api/alembic/versions/0008_performance_indexes.py` — the `create_hypertable` loop
   and the two `WITH (timescaledb.continuous)` views get a plain-Postgres branch (plain tables +
   plain matviews + the same indexes).
3. `services/api/src/decile_api/integrity.py` — `HYPERTABLES` check inverts: the extension's
   *absence* becomes the expected state (the warning path already exists).
4. `services/api/src/decile_api/query_plans.py` — the chunk→hypertable mapping becomes inert; the
   plan reader needs no replacement at this scale.
5. `services/worker` — one new task after publish: refresh the two matviews. (`pg_cron` is
   available on RDS if in-database scheduling is ever preferred.)

Do it **as part of P3.9**, which is already the one-way, checksum-asserted migration step —
SQLite and Timescale leave in the same verified move, one migration window instead of two.

RDS specifics: start `db.t4g.small` single-AZ 50 GB gp3 (~$37/mo), step to `db.t4g.medium`
(~$68/mo, derived Mumbai rate) when P4 load-testing says so, **Multi-AZ only at paid launch**
(doubles the line). Automated backups 7 days now, 30 at launch; take a manual snapshot before
every migration; the P6.7 restore drill reads the S3 dump, not a fresh one.

## 5. Phase B — the product topology (built during P4–P5, hardened at P6)

```
                          Route 53  baskfy.com
                                │
              app.baskfy.com     │      api.baskfy.com (SSE straight to ALB,
                    │           │       CloudFront never buffers a backtest stream)
                    ▼           │                     │
             CloudFront (assets, web) ────────────────┤
                    │                                 │
   ┌────────────────▼─────────────────────────────────▼────────────────┐
   │ public subnets (2 AZ)     ALB · ACM TLS · idle-timeout 300 s      │
   │                           [WAF from P6]                           │
   ├───────────────────────────────────────────────────────────────────┤
   │ private subnets (2 AZ)          ECS Fargate on Graviton           │
   │                                                                   │
   │   web (Next standalone)   api (FastAPI)      worker (compute+dflt)│
   │   0.5 vCPU/1 GB           1 vCPU/2 GB        1 vCPU/4 GB          │
   │                                                                   │
   │   ingest-worker 0.5/1     beat 0.25/0.5  ◄── desired-count=1,     │
   │   (Kite/NSE fetches)      (schedules)        stop-before-start    │
   │                                                                   │
   │   RDS PostgreSQL 16        ElastiCache Valkey      Secrets Mgr    │
   │   db.t4g.small→medium      cache.t4g.micro         + KMS CMK      │
   │   (no Timescale, §4)       (cache + Celery broker) (token keys)   │
   ├───────────────────────────────────────────────────────────────────┤
   │ egress: fck-nat (t4g.nano+EIP, ~$5.6) or NAT GW (~$46)            │
   │         ONE EIP ──► Zerodha order APIs (REGISTERED IP) · NSE      │
   └───────────────────────────────────────────────────────────────────┘
                    │
        S3 (archive/artefacts/backups) · SES (ap-south-1) · ECR
        CloudWatch Logs+alarms · Sentry · GitHub Actions (OIDC → ECR → ECS)
```

Service-by-service notes:

- **`web`** — Next.js 15 `output: "standalone"` in a container. This is the deployment mode the
  Next team documents as supporting everything; Amplify Hosting is explicitly avoided (SSE
  buffering, version lag). CloudFront in front for assets and the marketing surface; price class
  must include Asia or Indian users hit US edges.
- **`api`** — FastAPI. The backtest SSE stream goes to `api.baskfy.com` directly on the ALB
  (idle timeout raised to 300 s), not through CloudFront — one config line instead of a
  buffering fight. The desk's WebSocket `TickBus` lives here too when the Jinja console's
  replacement needs live ticks; ALB handles WebSocket natively.
- **`worker` / `ingest-worker`** — the four Celery queues split across two services so a slow
  Kite night still cannot starve compute, same reasoning as the queue design itself. Scale-out is
  a desired-count change.
- **`beat`** — the one rule from Celery's own docs: exactly one Beat. Its own service,
  desired-count 1, deployment `minimumHealthyPercent=0 / maximumPercent=100` so a deploy stops
  the old scheduler before starting the new. (EventBridge Scheduler could replace Beat entirely;
  not worth rework of a tested schedule table — revisit only if Beat ever misbehaves.)
- **Order-path egress** — all order-placing tasks route through the private subnets' NAT, whose
  EIP is the registered IP. Start with **fck-nat** (a t4g.nano NAT AMI: ~$5.6/mo all-in, no
  per-GB fee) and move to managed NAT Gateway (~$46/mo + $0.056/GB) when an hour of NAT outage
  costs more than $40 — the EIP survives either swap, so Zerodha never notices. The IP
  choreography itself: register the new EIP as **secondary** while the desk box still holds
  primary, shadow-execute (P2.5 style), promote, and only then retire the old IP — never burn
  both slots in one calendar week.
- **Tokens and secrets** — system credentials (Kite app, Razorpay, SES) in Secrets Manager
  (~10 secrets, $4/mo). Per-user broker tokens stay in Postgres under Decile's existing
  encrypted-token path (P3.7/P4.2), with the data key wrapped by a **KMS CMK** ($1/mo) so key
  rotation is an AWS API call, not a re-encryption project.
- **Tenant isolation in the database** — P4.10's row-level-security lands on RDS unchanged;
  RDS supports RLS policies exactly as self-hosted Postgres does.
- **Observability** — Fargate's `awslogs` driver → CloudWatch Logs (watch ingestion GB — it is
  the gotcha line, ~$9/mo at modest volume); Sentry stays SaaS; the existing `/metrics` +
  Prometheus alert rules move to Amazon Managed Prometheus + Grafana **only if** the self-run
  pair on the ops box stops being enough. The app's own ops checks (publish deadline, token
  expiry, queue backlog — already written) wire to SES/CloudWatch alarms; runbook #6 joins the
  five that exist.
- **CI/CD** — GitHub Actions with an OIDC role (no long-lived AWS keys): build multi-arch images
  → ECR → run Alembic migration as a one-off ECS task → `aws ecs update-service`. Staging first,
  prod on tag.
- **IaC** — Terraform, S3 backend + lock table, four small modules (`network`, `data`,
  `services`, `edge`), two workspaces (`staging`, `prod`). Staging runs the same modules at
  minimum sizes (~$60–90/mo) and is what P6.6's runbook drills execute against — the "there is
  no staging" line in Decile's open-items table dies here.

## 6. What lands where — the docs/04 module map, deployed

| docs/04 target | Phase A home | Phase B home |
|---|---|---|
| `apps/web` (Next.js) | not deployed (Jinja desk is the UI) | Fargate `web` + CloudFront |
| `services/api` | compose `api` on EC2 | Fargate `api` |
| `services/worker` + Beat | compose `worker`+`beat` | Fargate `worker`, `ingest-worker`, `beat` |
| `packages/execution` order path | desk box (unchanged) → EC2 after P3 | Fargate `api`/`worker` egressing via the registered EIP |
| Postgres | Timescale container on EC2 | RDS PG16 (no Timescale) |
| Redis | container | ElastiCache Valkey |
| Raw archive / artefacts / backups | S3 | S3 (same bucket, lifecycle rules) |
| Jinja operator console | desk box, pointed at merged backend (P3.11) | retired page-by-page per P5.10 |
| strangle collectors (frozen) | desk box systemd, untouched | die with the desk box, or move to a scheduled ECS task if the series still matters |

## 7. Money

| | Monthly (USD) |
|---|---|
| **Phase A** — EC2 t4g.large + 100 GB gp3 + EIP + snapshots + S3 + CW + Route 53 | **≈ $50** (~₹4,400) |
| ongoing besides it | desk Lightsail ~$24 · Kite Connect ₹500 |
| **Phase B prod** — ALB $23 · IPv4 ×3 $10.8 · Fargate (5 svcs, Graviton) ~$100 · RDS t4g.small→medium $37–68 · Valkey ~$10 · fck-nat $5.6 · Secrets+KMS $5 · S3+SES ~$3 · CloudWatch ~$9 | **≈ $205–240** (NAT GW instead of fck-nat: +$40; Multi-AZ RDS at launch: +$37–68) |
| **Phase B staging** (same modules, minimum sizes, off-hours stoppable) | ≈ $60–90 |
| One-time / annual | TM filing ~₹18k (2 classes) · RA registration + NISM (verify current fee schedule) · possible NSE data agreement ~₹1 L/yr (the `07` §4d gate) · domains ~₹2k/yr |

Two flagged derivations: the RDS `db.t4g.medium` Mumbai rate (~$61) is scaled from the verified
micro rate, and the ElastiCache micro rate is a us-east-1 proxy — re-price both in the AWS
calculator before committing a budget. Everything else is a published Mumbai rate.

The shape of the spend is the point: **nothing above $75/month exists until the SEBI gate is
passed**, and the expensive half arrives with the phase that needs other people's money to make
sense anyway.

## 8. Sequence, restated as AWS moves

| Plan step | AWS move |
|---|---|
| P0 | AWS account hygiene: org, `ap-south-1` default, CloudTrail on, budgets + billing alarms, Route 53 zone for `baskfy.com`, S3 archive bucket |
| P1–P2 (P2.1) | Phase A EC2 up via compose; backfill (an evening — `07` §4b); S3 archive live; desk consumes MomentumScan over the seam |
| P3.9 | RDS created; **one migration window**: SQLite → Postgres → RDS, Timescale dropped (§4), checksums asserted, SQLite archived to S3 forever |
| P3 end | Execution path moves off the laptop-era topology: EIP registered as secondary IP, shadowed, promoted |
| P4–P5 | Phase B modules applied; Fargate services live; desk box's remaining job is the Jinja console |
| P6 | WAF, Multi-AZ, staging drills of all six runbooks, restore-from-S3 proven, pen-test of the order path, data-licensing position (`07` §4d) |
| P5.10 done | Lightsail box retired; its IP slot freed; `desk.modelbasket.in` becomes a redirect |

---

**Sources** (checked 21 Aug 2026): [RDS PG extension list](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html) ·
[Aurora PG extension list](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraPostgreSQLReleaseNotes/AuroraPostgreSQL.Extensions.html) ·
[Fargate pricing](https://aws.amazon.com/fargate/pricing/) · [Lightsail pricing](https://aws.amazon.com/lightsail/pricing/) ·
[Lightsail static IP free while attached](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-static-ip-addresses-in-amazon-lightsail.html) ·
[public IPv4 charge](https://aws.amazon.com/blogs/aws/new-aws-public-ipv4-address-charge-public-ip-insights/) ·
[ALB idle timeout](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/application-load-balancers.html) ·
[ElastiCache Valkey pricing note](https://aws.amazon.com/about-aws/whats-new/2024/10/amazon-elasticache-valkey/) ·
[SES pricing](https://aws.amazon.com/ses/pricing/) · [SES endpoints incl. ap-south-1](https://docs.aws.amazon.com/general/latest/gr/ses.html) ·
[Next.js deployment docs](https://nextjs.org/docs/app/getting-started/deploying) · [Amplify SSR support matrix](https://docs.aws.amazon.com/amplify/latest/userguide/ssr-amplify-support.html) ·
[Celery: single Beat](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html) · [EventBridge-scheduled ECS tasks](https://aws.amazon.com/blogs/containers/migrate-cron-jobs-to-event-driven-architectures-using-amazon-elastic-container-service-and-amazon-eventbridge/) ·
[fck-nat](https://fck-nat.dev/stable/) · [CloudFront pricing](https://aws.amazon.com/cloudfront/pricing/pay-as-you-go) ·
[Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/) · [KMS pricing](https://aws.amazon.com/kms/pricing/) ·
[CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) · [RDS backup storage FAQ](https://aws.amazon.com/rds/faqs/) ·
Mumbai instance rates: cloudprice.net / aws-pricing.com (t4g), CloudKeeper (NAT), AWS pricing CSV (ALB); RDS medium + ElastiCache flagged as derived/proxy above.
