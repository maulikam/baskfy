# Gates: host Baskfy on AWS — Phase A, gated staging

Scope: everything needed to run the site on one EC2 box in `ap-south-1` per `docs/08` §3, behind
a password, not indexable. Maulik chose Phase A + gated staging (26 Aug 2026).

```
node ~/.claude/skills/unlazy/scripts/gate-check.mjs gates/aws-phase-a-hosting.md
```

## Facts measured before writing these gates

- **Deployment tooling is at zero.** `infra/docker/compose.yml` carries only postgres, redis,
  mailpit, prometheus, grafana — no `web`, `api`, `worker` or `beat` container exists, there is
  no Dockerfile anywhere in the tree, no Terraform, no deploy workflow.
- `apps/web/next.config.ts` has **no `output: "standalone"`**, which `docs/08` §5 requires.
- Entrypoints are `uvicorn baskfy_api.app:get_app --factory` (api),
  `celery -A baskfy_worker.celery_app:app worker -Q ingest,compute,backtest,default`, and
  `celery -A baskfy_worker.celery_app:app beat`.
- The Python side is a **uv workspace** of five members; a naive `pip install .` will not build it.
- **No AWS CLI and no credentials on this machine** (`which aws` → not found, `~/.aws` absent).
  Nothing can be provisioned from here; that is a NEEDS-MAULIK item, not a gate.
- Local Docker is **29.7.2 on aarch64**, the same architecture as the `t4g.large` Graviton
  target. Images built and run here are the real artefact, so "it builds" is provable, not hoped.
- `terraform` and `tofu` are both absent. G6 installs one or is abandoned honestly.

---

- [x] G1: The web app builds as a standalone container and serves the landing page, with the
      animation and the four-allocation figure intact.
  CHECK: bash tools/deploy/verify-web-image.sh
  EXPECT: /WEB IMAGE OK/
  EVIDENCE: WEB IMAGE OK — renders, stylesheet loads, 102 MB

- [x] G2: The API builds as a container off the uv workspace and answers its health route.
  CHECK: bash tools/deploy/verify-api-image.sh
  EXPECT: /API IMAGE OK/
  EVIDENCE: API IMAGE OK — /health answers without a database, openapi mounted, clock is IST

- [x] G3: Worker and Beat start from the same image, register the real queues, and Beat loads the
      IST schedule rather than dying on import.
  CHECK: bash tools/deploy/verify-worker-image.sh
  EXPECT: /WORKER IMAGE OK/
  EVIDENCE: WORKER IMAGE OK — 17 scheduled jobs, schedule dir writable, queues split

- [x] G4: The whole Phase A stack comes up under compose on this machine — caddy, web, api,
      worker, beat, postgres, redis — and the site is reachable end to end through Caddy.
  CHECK: bash tools/deploy/verify-stack.sh
  EXPECT: /STACK OK/
  EVIDENCE: STACK OK — 8 services up, migrations at 0024_portfolio_kind_default, no SSR leak to the public host

- [x] G5: The gate actually gates. Unauthenticated requests are challenged, authenticated ones
      pass, and every response carries `X-Robots-Tag: noindex` with `robots.txt` disallowing all.
  CHECK: bash tools/deploy/verify-gate.sh
  EXPECT: /GATE OK/
  EVIDENCE: GATE OK — 401 for strangers with noindex, 200 for the password, robots.txt open and denying

- [x] G6: The Terraform for the Phase A box is valid and formatted, and describes the shape
      `docs/08` §3 specifies: t4g.large, EIP, gp3 100 GB, S3 archive, Route 53 record, SSM (no
      SSH keys), IMDSv2 required, DLM snapshots, a budget alarm.
  CHECK: bash tools/deploy/verify-terraform.sh
  EXPECT: /TERRAFORM OK/
  EVIDENCE: TERRAFORM OK — valid, formatted, 29 resources, docs/08 §3 shape asserted

- [x] G7: No secret is committed, and the safety rails hold in the deployed config: `DRY_RUN` is
      true, `BASKFY_PUBLIC_API_ENABLED` false, Track B flags false.
  CHECK: bash tools/deploy/verify-safety.sh
  EXPECT: /SAFETY OK/
  EVIDENCE: SAFETY OK — no tracked secrets, DRY_RUN true, public API shut, data ports unpublished | ⚠ NEEDS-MAULIK §20 still open: kite-momentum-rebalancer/data/.kite_token.json.key is tracked and pushed. Rotate 

- [x] G8: A deploy runbook exists that a person can follow at 3am, and it is honest about what is
      not yet possible. The repo's other five runbooks are the format.
  CHECK: bash tools/deploy/verify-runbook.sh
  EXPECT: /RUNBOOK OK/
  EVIDENCE: RUNBOOK OK — /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/docs/runbooks/07-deploy-phase-a.md present, 8 runbooks in the set, blockers filed

- [x] G9: The existing suites still pass — adding `output: "standalone"` changes how Next builds,
      so this is not a formality.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  1905 passed (1905)

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
