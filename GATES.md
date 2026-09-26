# Gates: OV11 — Laya's inference contract corrected (26 Sep 2026)

Scope: the three review findings (wrong confidence field, dropped context, adverse signal
erased by a confident tag), plus the review's items 2–4 (sidecar tests, versioned cache keys,
pinned model and dependencies) and the chronology field. Rubric questions are Maulik's; drafted
as ⚠ UNREVIEWED and listed in NEEDS-MAULIK.md. Not in scope: reading filing attachments (D10),
threshold recalibration (needs labels), fine-tuning.

- [x] G1: Sidecar stores the calibrated `answer_confidence` as `confidence`, keeps the entropy number under its own name, falls back to `probabilities[choice]`, and writes a model tag
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -m pytest -p no:randomly infra/laya/tests -q --tb=line 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: 19 passed (infra/laya/tests, all); laya v0.3.20 agent.py L671-685 confirms answer_confidence = max(p), confidence = 1-H/log k

- [x] G2: Core readers (`tag_from_laya`, `opinion_from_laya`) read `answer_confidence` / `probabilities[choice]` and refuse a payload that carries only the entropy `confidence`
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -m pytest -p no:randomly packages/core/tests/test_catalyst_tags.py packages/core/tests/test_candidate_review.py -q --tb=line 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: 70 passed (test_catalyst_tags + test_candidate_review); chosen_probability reads answer_confidence else probabilities[choice], never confidence

- [x] G3: Cache keys are `v2` and carry the question-schema hash; the sidecar's mirrors of both key functions and both question dicts equal core's (asserted by a test that imports both)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -m pytest -p no:randomly infra/laya/tests -q --tb=line -k "mirror or key" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: 5 passed (-k "mirror or key"): test_the_two_question_dicts_are_identical, test_the_key_version_and_schema_hash_and_key_agree, test_a_changed_question_is_a_new_key

- [x] G4: The sidecar passes every string field of the queued state to Laya (setup, filing, context, timeline), short fields first so right-truncation cuts the setup's tail, never the filing
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -m pytest -p no:randomly infra/laya/tests -q --tb=line -k "context or every_field or order" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: 2 passed (-k "context or every_field or order"): list(seen) == [filing, timeline, context, setup]; laya common.py serialize_state = json.dumps(state) (insertion order), agent.py L568 truncate_left only for lists

- [x] G5: The adverse flag comes from the deterministic headline rules, so a confident Laya `corporate_action` on "SEBI order" still makes the rules baseline skip
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -m pytest -p no:randomly packages/core/tests/test_candidate_review.py -q --tb=line -k adverse 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: 1 passed (-k adverse): rules_opinion([EP], "SEBI order against …", "corporate_action", "medium") → SKIP, reason "the filing is adverse (sebi order)"; API: test_a_confident_model_tag_cannot_erase_an_adverse_filing

- [x] G6: The state gains a `timeline` field — the session date, the filing's distance from it, results due/past, screen-run age — relative to the session, never the clock
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -m pytest -p no:randomly packages/core/tests/test_candidate_review.py -q --tb=line -k timeline 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: 4 passed (-k timeline); describe_timeline uses max(context.sessions) as anchor, differences of stored dates only

- [x] G7: Model snapshot pinned by HF revision (`LAYA_MODEL_REVISION`, default = the sha the box loaded on 25 Sep) and the sidecar clears its own keys when the model tag changes
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -m pytest -p no:randomly infra/laya/tests -q --tb=line -k "revision or model_change" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: 3 passed (-k "revision or model_change"); HF API: sha 55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851, lastModified 2026-09-24T05:39:22Z; siblings include rl_agent_config.json, model.safetensors, tokenizer/*, encoder/* at root

- [x] G8: Dependencies fully pinned in `infra/laya/requirements.txt` (torch CPU wheel index), compose installs from it, deploy-swing ships it beside the loop
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c "==" decile-blueprint/infra/laya/requirements.txt && grep -n "requirements" decile-blueprint/infra/docker/compose.prod.yml tools/deploy/deploy-swing.sh | wc -l
  EXPECT: /[1-9]/
  EVIDENCE: 38 pins (torch==2.14.0+cpu, transformers==5.17.0, laya==0.3.20, huggingface-hub==1.33.0); compose.prod.yml + deploy-swing.sh reference requirements in 6 lines

- [x] G9: DB-backed API suite green — adverse preserved end to end, context and timeline in the queued state, the sidecar's new payload shape read
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test .venv/bin/python -m pytest -p no:randomly services/api/tests/test_api_overlap.py services/api/tests/test_overlap_readonly.py -q --tb=line 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: 44 passed (test_api_overlap + test_overlap_readonly) against baskfy_test on localhost:5433

- [x] G10: Lint clean — ruff check, ruff format --check, mypy strict — including the sidecar and its tests
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/ruff check . && .venv/bin/ruff format --check . | tail -1 && .venv/bin/mypy 2>&1 | tail -1
  EXPECT: /Success/
  EVIDENCE: All checks passed! | 1041 files already formatted | Success: no issues found in 911 source files (re-run 26 Sep 15:45 IST)

- [x] G11: Wire shape unchanged (no openapi.json diff) or regenerated with the TS client
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git status --porcelain | grep -c "openapi.json\|apps/web/src/lib/api" ; true
  EXPECT: /0|2/
  EVIDENCE: 0

- [x] G12: Docs — docs/07a §17, docs/02 ADR Laya section, DECISIONS-MERGE entry (⚠ UNREVIEWED), rubric draft, NEEDS-MAULIK questions, status page line
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c "answer_confidence" decile-blueprint/docs/07a-api-implementation-notes.md decile-blueprint/docs/02-tech-stack-adr.md docs/DECISIONS-MERGE.md && test -f docs/overlap/LABELLING-RUBRIC.md && grep -c "rubric" NEEDS-MAULIK.md
  EXPECT: /[1-9]/
  EVIDENCE: answer_confidence in docs/02:1, docs/07a:2, DECISIONS-MERGE:2; docs/overlap/LABELLING-RUBRIC.md present; NEEDS-MAULIK rubric section; docs/00-merge-status.md OV11 section appended

- [x] G13: One commit `OV11: green — …` on developer, working tree clean apart from pre-existing untracked dirs
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git log -1 --format=%s | cut -c1-40 && git status --porcelain | grep -v "^?? holdings-status/" | wc -l
  EXPECT: /OV11: green/
  EVIDENCE: OV11: green — Laya's inference contract | 0

- [x] G14: Deployed to the box with ship.sh from a detached worktree, laya force-recreated (bind-mounted file changed), df checked
  EVIDENCE: ship.sh from detached worktree deploy-59ce6ea (first run's baskfy-py push failed 5x on ECR dial errors; rerun succeeded): '✓ DEPLOYED 59ce6ea', pins=3 running=15 release=59ce6ea; laya recreated 16:22:25 IST (compose changed), log 'loading convaiinnovations/laya@55cf4c4ebb4e', laya:model set; df / 22% (79G free); 115 headlines tagged in 625s; 25 rows answered; v1 keys deleted (1268+325)
