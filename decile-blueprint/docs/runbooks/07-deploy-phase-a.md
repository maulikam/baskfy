# Runbook — deploy Phase A to AWS (gated staging)

**Deploys:** the whole product on one EC2 box in `ap-south-1` — `docs/08-aws-architecture.md` §3.
**Verified against:** the stack, end to end, on Docker — `bash tools/deploy/verify-stack.sh` and
the five scripts beside it. **NOT against real AWS**, because there are no AWS credentials on any
machine this was written from (NEEDS-MAULIK §21). Every `terraform` command below is validated and
none has ever been applied. Treat the first apply as a first apply.
**Cost:** ≈$50/month (§7), with a budget alarm at $75.

## The reassuring part, first

**Nothing here can place an order.** `DRY_RUN=true` is baked into the image *and* set in
`compose.prod.yml`; the web app has no execute route; `BASKFY_PUBLIC_API_ENABLED` is false (D9).
The EIP this creates is the *future* secondary order IP — registering it with Zerodha is §8 below
and is a separate, deliberate act.

**And nothing here is public.** The site sits behind HTTP basic auth with `X-Robots-Tag: noindex`
on every response and a `robots.txt` that disallows everything. That is because the four legal
drafts under `apps/web/src/content/legal/` are **unreviewed** (NEEDS-MAULIK §19) and D3's
regulatory posture is written but ⚠ UNREVIEWED. Going public is §7, and it deliberately takes
more than one edit.

---

## 0. What only Maulik can supply

Read **NEEDS-MAULIK §21** first. Without these, stop here:

- An AWS account with `ap-south-1` enabled and a CLI profile that can create VPC/EC2/EIP/EBS/S3/
  IAM/Route 53/DLM/Budgets.
- `baskfy.com`'s registrar pointed at the Route 53 nameservers this creates (step 3).
- An email address for the budget alarm.

## 0b. Preflight

Console login is not CLI login — a browser session gives this machine nothing. Get CLI
credentials, then prove they work before Terraform creates anything:

```bash
aws configure sso                       # recommended; or `aws configure` for an access key
bash tools/deploy/preflight-aws.sh
```

It is read-only and creates nothing. It exists because `terraform apply` fails *late*: it will
create a VPC, a subnet and an internet gateway before discovering it cannot create an IAM role,
and you are then holding half a stack and a state file.

The permissions it probes are in `infra/terraform/deploy-policy.json` — derived from the 29
resources the configuration declares, not guessed. In a single-owner account, IAM Identity Center
with `AdministratorAccess` is both simpler and safer than that policy: the real risk here is not
over-permission, it is a long-lived access key on a laptop, and SSO has none.

## 1. Build and push the images

Both are `linux/arm64` — the box is Graviton, and an amd64 image runs under emulation.

```bash
cd decile-blueprint
docker build --platform linux/arm64 -f infra/docker/Dockerfile.web    -t baskfy-web:$(git rev-parse --short HEAD) \
  --build-arg NEXT_PUBLIC_SITE_URL=https://staging.baskfy.com \
  --build-arg NEXT_PUBLIC_API_URL=https://staging.baskfy.com/api/v1 \
  --build-arg BASKFY_RELEASE=$(git rev-parse --short HEAD) .
docker build --platform linux/arm64 -f infra/docker/Dockerfile.python -t baskfy-py:$(git rev-parse --short HEAD) .
```

> **`NEXT_PUBLIC_*` is a build argument, not an environment variable.** Next inlines it into the
> client bundle at build time. Setting it in `compose.prod.yml` would change nothing while looking
> like it had — the container would keep whatever host it was built with. This is the single most
> expensive mistake available in this runbook.

Then `docker push` to ECR (`aws ecr create-repository --repository-name baskfy-web` etc.), or for
a first deploy `docker save | ssh`-free transfer over SSM:
`aws ssm start-session … ` then `docker load` from S3.

## 2. Apply the Terraform

```bash
cd decile-blueprint/infra/terraform
cp terraform.tfvars.example terraform.tfvars   # gitignored; fill in the budget email
# tf.sh runs the official image with ~/.aws mounted read-only and AWS_PROFILE passed through.
# Without both, terraform reports "No valid credential sources found" while `aws sts
# get-caller-identity` works fine in the same shell — the container simply cannot see your
# SSO token cache.
bash ../../../tools/deploy/tf.sh init
bash ../../../tools/deploy/tf.sh plan     # READ IT
bash ../../../tools/deploy/tf.sh apply
```

Read the plan. It should create **29 resources** and no more. If it proposes destroying an
`aws_eip`, an `aws_s3_bucket` or an `aws_route53_zone`, stop — all three carry `prevent_destroy`
and a plan that wants to replace one means something upstream changed that you did not intend.

State starts local. Once the archive bucket exists, uncomment the `backend "s3"` block in
`versions.tf` and `terraform init -migrate-state`.

## 3. Point DNS

`terraform output nameservers` → set those four at the registrar for `baskfy.com`. Until this
propagates, Caddy cannot answer the ACME challenge and there is no certificate.

```bash
dig +short NS baskfy.com
dig +short staging.baskfy.com     # should be the EIP
```

## 4. First deploy

There is no SSH. `terraform output ssm_command` gives you the way in.

```bash
aws ssm start-session --region ap-south-1 --target i-…
sudo cloud-init status --wait     # <- do this first, see below
sudo -iu ec2-user
cd /opt/baskfy
```

> **The SSM agent comes online before user-data finishes.** On the first apply this cost fifteen
> minutes of confusion: `docker compose version` printed nothing, the clock said UTC and
> `/opt/baskfy/READY` was absent, all of which reads exactly like a failed bootstrap. Cloud-init
> was simply still running — `cloud-init status --wait` returns `done` and every check then
> passes. A registered instance is not a provisioned one.

Put `compose.prod.yml`, `Caddyfile`, and the two env files there. The env files are **not** in the
repository and must never be:

```bash
bash tools/deploy/gate-password.sh     # run on your laptop; store the plaintext in a password manager
```

`.env.staging.compose` needs `BASKFY_DB_PASSWORD`, `BASKFY_JWT_SECRET`, `BASKFY_GATE_USER`,
`BASKFY_GATE_PASSWORD_HASH` (`$$`-escaped — the script does it), `BASKFY_SITE_ADDRESS=staging.baskfy.com`,
`BASKFY_PUBLIC_URL=https://staging.baskfy.com`, and
`BASKFY_ACME_EMAIL_DIRECTIVE="email you@example.com"`.

Then:

```bash
docker compose -f compose.prod.yml --env-file .env.staging.compose run --rm migrate   # alembic upgrade head
docker compose -f compose.prod.yml --env-file .env.staging.compose run --rm seed      # `baskfy_api.seed reference`
docker compose -f compose.prod.yml --env-file .env.staging.compose up -d
```

Migrations are a **separate, explicit step** and not a `depends_on`. A migration that runs on
every container restart is a migration that runs during an outage, on a box already unhappy.

**The `seed` line was missing until 27 Aug 2026, and its absence is a bug this runbook caused.**
A migrated database is an empty one: `plan`, `cb_collection`, the universes and the factor
registry are all populated by `baskfy_api.seed`, and nothing here ran it. What that looked like
from the outside was `/pricing` rendering its "Before you buy" preamble with no plan cards
underneath — `GET /plans` answering `{"data":[]}` — and `/discover/collections` rendering an empty
directory. Two pages reported as broken; one empty table each, under code that was working.

**It seeds `reference`, not `all`.** `all` also loads the 271-row export from `tests/fixtures/`,
which is not in the production image and fails with `FileNotFoundError` on the box — correctly, as
that fixture is a random-walk answer key and has no business near real closes. `reference` carries
what a deployment cannot derive: the exchange, index definitions, the three plans, the example
screens, the curated managers and the collection shelves. Bars, factors and baskets come from the
pipeline.

Seeding is idempotent (house rule 7), so run it on every deploy rather than only the first.
Verify it took, because a silent no-op here is exactly the failure it just caused:

```bash
docker exec baskfy-staging-postgres-1 psql -U baskfy -d baskfy \
  -c "select (select count(*) from plan) plans, (select count(*) from cb_collection) shelves;"
```

## 5. Verify

```bash
curl -sI https://staging.baskfy.com/            # 401, with X-Robots-Tag: noindex
curl -s  https://staging.baskfy.com/robots.txt  # 200, Disallow: /
curl -sI -u user:pass https://staging.baskfy.com/   # 200
docker compose logs web | grep -c ENOTFOUND     # must be 0 — see below
```

That last one is the check worth understanding. Server-side rendering reaches the API at
`http://api:8000` across the container network, never through Caddy — see `serverApiOrigin()` in
`apps/web/src/lib/api/config.ts`. If SSR ever goes out to the public host it meets Caddy's own
password, gets a 401, and **every data page renders its error state while returning HTTP 200**. It
looks like a data bug and is a networking one.

## 6. Rollback

The images are tagged by commit, so rollback is a tag change:

```bash
BASKFY_WEB_IMAGE=baskfy-web:<previous-sha> BASKFY_PY_IMAGE=baskfy-py:<previous-sha> \
  docker compose -f compose.prod.yml --env-file .env.staging.compose up -d
```

**Migrations do not roll back with the image.** If the bad deploy migrated, decide deliberately:
`alembic downgrade -1` is only safe when the migration was additive. Take a manual EBS snapshot
first — `docs/runbooks/restore-from-backup.md` is the other half of this.

Rebuilding the box itself is not rollback: the root volume is `delete_on_termination = false` and
Postgres lives on it. See `restore-from-backup.md` before terminating anything.

## 7. Going public — NOT YET

This is the one procedure in this runbook that is blocked on something no engineering can clear.

**Preconditions, all four:** counsel has cleared `terms-conditions`, `privacy-policy`,
`refund-policy` and `disclaimer` (NEEDS-MAULIK §19); D3's posture is reviewed rather than
⚠ UNREVIEWED; C3 is settled if anything is being charged; and someone has re-read `docs/07` §4d on
market-data display licensing.

Then, and only then: remove `basic_auth` from the `route` block in the `Caddyfile`, delete the
`handle /robots.txt` block so the app's own file serves, drop the `X-Robots-Tag` header, and add
the apex A record (deliberately absent from `dns.tf`). Four edits, on purpose — removing the
password should not be something a fat finger can do.

> **Edit 1 of 4 was taken on 27 Aug 2026, ahead of the preconditions, on Maulik's explicit
> instruction.** `basic_auth` is gone (`docs/DECISIONS-MERGE.md` M46.4). The cause was Google's
> OAuth verification: it fetches the homepage, the privacy policy and the terms anonymously and
> refused the app while each answered 401 — six of its eight reported failures were this gate, and
> Google sign-in is now the only way into the product.
>
> **The other three edits have NOT been made, and that is the whole safety margin.** The
> `handle /robots.txt` block still serves `Disallow: /`, `X-Robots-Tag: noindex` is still set on
> every response including errors, and there is still no apex A record. So the site is *reachable
> by anyone holding a URL* and still *not discoverable through a search engine* — `noindex`
> governs indexing, not fetching, which is why a verification crawler reads what a search crawler
> is told to ignore.
>
> The four preconditions above remain **unmet**: counsel has not cleared the legal drafts
> (NEEDS-MAULIK §19), and D3 is still ⚠ UNREVIEWED. Do not take edits 2–4 until they are.
> `tools/deploy/verify-gate.sh` now fails if either remaining mechanism is removed.

## 8. The order path — separate, and later

The EIP this creates is the **future secondary** Zerodha IP. docs/08 §5: register it as secondary
while the desk box still holds primary, shadow-execute, promote, and only then retire the old one.
**Never burn both slots in one calendar week** — one change per week is Zerodha's limit, and using
both leaves you unable to correct a mistake for seven days.

---

## Symptom → cause

| Symptom | Cause | Fix |
|---|---|---|
| Caddy crash-loops, `wrong argument count ... 'email'` | `BASKFY_ACME_EMAIL_DIRECTIVE` unset and the Caddyfile has a bare `email` | The directive is the whole line, not the value: `BASKFY_ACME_EMAIL_DIRECTIVE="email you@example.com"` or leave it empty |
| Every login fails, password is definitely right | Compose ate the `$` in the bcrypt hash | `$$`-escape it; `tools/deploy/gate-password.sh` does this for you |
| `beat` restarts forever, `Permission denied: 'celerybeat-schedule'` | Beat writing to a root-owned working directory | It needs `--schedule=/var/lib/baskfy/celerybeat-schedule` and the `baskfy-beat` volume; both are in `compose.prod.yml` |
| Pages return 200 but every panel shows an error | SSR is hairpinning to the public host and meeting the gate | `BASKFY_INTERNAL_API_ORIGIN=http://api:8000` on the `web` service |
| `robots.txt` returns 401 | `basic_auth` scoped by position instead of by matcher | Caddy orders directives itself; the `@gated` matcher and the `route` block are what scope it |
| Site serves unstyled HTML | `.next/static` or `public/` missing from the image | They are excluded from standalone output and copied explicitly; see `Dockerfile.web` |
| No certificate | DNS not propagated, or port 80 closed | Port 80 must stay open for the ACME HTTP-01 challenge |
| Bill above $75 | Budget alarm fired | §7's whole promise. Find out what changed before raising the limit |
| A DNS record is visibly present in the registrar UI but resolves nowhere | The registrar's DNS page remembers the last domain you selected — the record went onto a different zone | Check the *other* records on that page. `www → someotherdomain.com` means you are editing that domain. Confirm with `dig +short A <name>.<that-domain>` |
| `dig @ns01...` says a record does not exist, but the UI shows it | The zone is served by different nameservers than the ones you queried | `dig +norecurse NS <domain> @a.gtld-servers.net` gives the delegation the registry actually publishes; query those |
| `ping` times out but the site works | ICMP is not in the security group — only TCP 80 and 443 are | Working as designed (§3, "closed to the world except Caddy's 443"). Use `curl -I`, not `ping`, to test reachability |
| Deploy "succeeds" but serves the previous build | A layer push timed out, `set -e` killed the script before its success line, and `compose pull` fetched a stale `latest` | `push-images.sh` retries 5×. Verify the artefact, not the exit code: `curl -s -u … https://host/ \| grep -c <expected-hostname>` |
