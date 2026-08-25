# Gates: 1.2.1 Rebalance notification delivered once

Scope: Worker emails once per REBALANCE_AVAILABLE pending action; second run is a no-op.

- [x] G1: Notify tests green (first send, idempotent second, payload.notified_at)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/worker/tests/test_curated_rebalance_notify.py --tb=line 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: ....                                                                     [100%] | 4 passed in 0.31s

- [x] G2: Beat registers baskfy.cb.rebalance_notify
  CHECK: rg -n "baskfy.cb.rebalance_notify" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/services/worker/src/baskfy_worker/celery_app.py
  EXPECT: baskfy.cb.rebalance_notify
  EVIDENCE: 186:        "task": "baskfy.cb.rebalance_notify",

- [x] G3: Email template function exists
  CHECK: rg -n "rebalance_available|rebalance_notify" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/services/api/src/baskfy_api/email/templates.py
  EXPECT: rebalance
  EVIDENCE: 506:def rebalance_available(
