# Gates: 2 — ranking engine complete (root integration)

- [ ] G1: All leaf gate files 2.A–2.H fully met (or ABANDON lines surfaced).
  CHECK: for f in gates/ranking-2.[A-H]-*.md; do node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status $f 2>&1 | tail -1; done
  EXPECT: /^(?![\s\S]*unmet)[\s\S]*ALL MET/i
  EVIDENCE: pending

- [ ] G2: Whole Python suite (with DB) + make lint green at the integrated HEAD.
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest --color=no -p no:cacheprovider 2>&1 | tail -1 && make lint 2>&1 | tail -3
  EXPECT: /passed(?![\s\S]*failed)[\s\S]*/
  EVIDENCE: pending

- [ ] G3: Desk tree suite green (the desk must rebalance on any Friday).
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest --color=no 2>&1 | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: pending

- [ ] G4: Every brief item mapped to where it is implemented, with evidence, in docs/ranking/PLAN.md "Phase 2 — brief coverage" table (corrections 1–7 + NSE, 8 new factors, volume baseline, acceleration raw+scaled, 3 modes, 4 preferences, 3 scopes + default + provenance, quality vs selection, entry/retention from validation, explainability 7 fields, same desk service, families + family weights, ablation protocol, implementation order 1–5).
  EVIDENCE: pending

- [ ] G5: Committed in verified waves; docs/DECISIONS-MERGE, NEEDS-MAULIK (anything needing hands), RUN-AND-TEST updated.
  EVIDENCE: pending

- [ ] G6: Deployed to staging outside market hours with ship.sh green, migrations 0046/0047 at head on the box, backfill-ranking run for enough history that rank_persist_20 is populated for the latest session, and a live preview of a composite screen returns provenance + ranks.
  EVIDENCE: pending
