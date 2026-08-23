# Gates: leaf-2.1-sip-beat

Scope: Celery Beat SIP REMINDER fires → cb_pending_action shape; no AUTO orders

- [x] G1: Beat key registered
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_worker.celery_app import BEAT_SCHEDULE; print('ok' if 'cb-sip-reminders' in BEAT_SCHEDULE else 'missing')"
  EXPECT: ok
  EVIDENCE: ok 2026-08-23T01:02Z; `cb-sip-reminders` → `baskfy.cb.sip_reminders` Mon–Fri 09:00 IST on compute; `cb-eod-metrics` retained

- [x] G2: worker SIP tests pass
  CHECK: cd decile-blueprint && uv run pytest services/worker/tests/test_curated_sip_beat.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: .... [100%] 2026-08-23T01:02Z (4 tests: beat key, route, no-broker source, idempotent SIP_DUE mock)

<!-- integrity: security, performance, memory, accuracy required -->
