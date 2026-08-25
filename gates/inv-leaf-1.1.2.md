# Gates: 1.1.2 Desk-fill → EXECUTED

Scope: Worker promotes PLANNED batches to EXECUTED only when the desk journal for desk_plan_id is settled. Synthetic cb-sim ids stay PLANNED.

- [x] G1: Sync tests green (settled → EXECUTED, partial → PARTIAL, cb-sim → unchanged)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/worker/tests/test_curated_batch_sync.py --tb=line 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: .......                                                                  [100%] | 7 passed in 0.41s

- [x] G2: Beat registers baskfy.cb.sync_batches
  CHECK: rg -n "baskfy.cb.sync_batches" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/services/worker/src/baskfy_worker/celery_app.py /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/services/worker/src/baskfy_worker/tasks/celery_tasks.py
  EXPECT: baskfy.cb.sync_batches
  EVIDENCE: /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/services/worker/src/baskfy_worker/tasks/celery_tasks.py:379:@shared_task(name="baskfy.cb.sync_batches", acks_late=True) | /Users/maulikdave

- [x] G3: Sync module does not import OrderGateway
  CHECK: rg -n "OrderGateway|place_order" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/services/worker/src/baskfy_worker/tasks/curated_batch_sync.py || echo NONE
  EXPECT: NONE
  EVIDENCE: NONE
