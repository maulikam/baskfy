# Runbooks

Five procedures, one per way this system is known to break. Written for whoever is holding the
pager at 03:00 IST, which means: the first section of each is what to do, and the reasoning is
underneath it.

**Verified against staging: NOT YET.** None of these has been executed against a real environment.
PROMPTS.md Prompt 17's third acceptance criterion — "Every runbook has been executed once against
staging and updated with real output" — is **not met**, because there is no staging environment in
this repository and the build that wrote them was not permitted to create one. Every command below
is written from the code it drives and from the vendor documentation, and every one of them should
be assumed wrong in some detail until it has been run. Each runbook carries a `Verified against:`
line; update it the first time you use it, and paste the real output in.

## The five

| Runbook | Fires on |
|---|---|
| [kite-token-expired.md](kite-token-expired.md) | `kite_token_expiring`; docs/09 calls this "the #1 pipeline failure" |
| [pipeline-failed.md](pipeline-failed.md) | `pipeline_failed`, `pipeline_abandoned`, `publish_late`, `queue_backlog`, `api_error_rate_high` |
| [bad-data-published.md](bad-data-published.md) | `data_quality_gate_failed`, or a human noticing wrong numbers |
| [restore-from-backup.md](restore-from-backup.md) | data loss; also the monthly drill |
| [razorpay-webhook-replay.md](razorpay-webhook-replay.md) | a payment that took money and granted nothing |
| [08-vbt-evening.md](08-vbt-evening.md) | the four `VBT_*` rules: an order past its three-session window, a naked position, a stale detect, a held name that stopped printing (VB7) |
| [06-swing-morning.md](06-swing-morning.md) | the five `SWING_*` rules: a naked position, the monitor not starting, a stale detect, an order open after 10:45, a GTT missing at 15:15 (SW11) |

## Before anything else

Three surfaces answer "what is actually wrong", in this order:

1. **`/admin/pipeline`** — the run history with per-step detail. docs/09 §Observability:
   "`pipeline_run_step` is the operator UI." Staff accounts only; see
   [pipeline-failed.md](pipeline-failed.md) §"Making yourself staff" if you do not have one.
2. **Grafana** — `infra/grafana/dashboards/decile-pipeline.json` and `decile-service.json`. The
   threshold lines on those panels are docs/11's budgets, not round numbers.
3. **`GET /meta/status`** — one unauthenticated request, no dashboard needed:
   ```
   curl -s https://<host>/api/v1/meta/status | jq
   ```
   `degraded: true` means the last run failed and the site is serving the previous `data_version`,
   which is docs/11 §Reliability's "graceful degradation" working as designed. That is not an
   outage. It becomes one when it is still true tomorrow.

## The one rule

**Never bump `data_version` by hand.** docs/03 makes it the gate's output: step 10 sets it only on
a run whose quality gate passed, and docs/06 §Caching keys every cached screen result on it. Moving
it forward over data the gate rejected poisons the cache *and* tells every client the poison is
fresh. If you think you need to, read [bad-data-published.md](bad-data-published.md) first — it
almost certainly has the thing you actually wanted.

- [`rebalance-half-executed.md`](rebalance-half-executed.md) — a rebalance batch stopped part way,
  or completed its buys without arming their stops. **Verified against: NOT YET.** Written from the
  18–19 Aug 2026 incidents in the desk's own journal; there is no staging with a broker in it, and
  these failures are not reproducible against production without placing real orders.
