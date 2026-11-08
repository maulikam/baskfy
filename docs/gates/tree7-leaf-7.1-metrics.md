# Gates: Tree 7 leaf 7.1 — populate cb_metrics

Scope: Run the existing EOD metrics job so /baskets cards stop returning metrics: null.

- [x] G1: compute_all_metrics (or celery task) can be invoked from a documented command
  CHECK: rg -n "compute_all_metrics|baskfy.cb.compute_metrics" decile-blueprint/services/api/src/baskfy_api/curated_metrics_service.py decile-blueprint/services/worker/src/baskfy_worker/celery_app.py | head -5
  EXPECT: /
  EVIDENCE: curated_metrics_service + celery_app; CLI `baskfy_worker.cb_metrics_cli`; `make cb-metrics`

- [x] G2: After a run, cb_metrics COUNT > 0 (or ABANDON: no published baskets / no price history)
  CHECK: docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc "SELECT COUNT(*) FROM cb_metrics"
  EXPECT: /^[1-9]/
  EVIDENCE: COUNT=1 · basket_id=1 · as_of 2026-08-21 · ret_1y=-3.71 · LOW · min_amount 203374

- [x] G3: At least one basket card path joins non-null metrics in code
  CHECK: rg -n "CbMetrics|metrics" decile-blueprint/services/api/src/baskfy_api/routers/explore.py | head -8
  EXPECT: /
  EVIDENCE: explore.py `_card` + outer join on latest metrics (SC2)

- [x] G4: Documented how to re-run in RUN-AND-TEST.md or docs/00-merge-status Tree 7
  CHECK: rg -n "cb_metrics|compute_all_metrics|cb-eod-metrics|cb-metrics" RUN-AND-TEST.md docs/00-merge-status.md | head -8
  EXPECT: /
  EVIDENCE: RUN-AND-TEST §catalog metrics; docs/00 Tree 7 section
