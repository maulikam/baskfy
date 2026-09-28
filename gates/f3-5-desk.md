# Gates: F3-5 — the desk

- [ ] K1: in the entry window the monitor raises an F3 ENTRY plan from the scan's CANDIDATE only when the intraday check agrees and the level has not broken; a CREDIT_SPREAD enters wing first, short second, abandoning the entry if the wing does not fill; a paper fill records the position with credit, max loss, level, strikes and expiry
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_fno_f3.py -k 'entry or enter or spread' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: pending

- [ ] K2: every minute in session the monitor judges each open F3 position: LEVEL_BREAK, LOSS_CUT, DECAY_TARGET, HARD_EXIT; with BASKFY_FNO_F3_AUTO_EXIT false it raises an EXIT plan and an alert and sends nothing; with it true (and execution enabled, DRY_RUN false) it sends the exit itself, short first then wing
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_fno_f3.py -k 'exit or auto or watch' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: pending

- [ ] K3: the next session an ADD plan is raised only if the position is working, the direction and level hold, and the full margin share is not reached; a confirmed ADD grows the position's lots and its GTT-less risk record
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_fno_f3.py -k 'add or pyramid' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: pending

- [ ] K4: the flags: BASKFY_FNO_F3_EXECUTION_ENABLED and BASKFY_FNO_F3_AUTO_EXIT default false in code, compose and the desk's .env.example; verify-fno.sh asserts both false on the box; the gating test proves paper with either off
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c 'BASKFY_FNO_F3_AUTO_EXIT' decile-blueprint/infra/docker/compose.prod.yml kite-momentum-rebalancer/app/config.py tools/deploy/verify-fno.sh | awk -F: '{s+=$2} END {print (s>=3)?"FLAGS PRESENT":"FLAGS MISSING"}'
  EXPECT: FLAGS PRESENT
  EVIDENCE: pending

- [ ] K5: the /fno page shows the F3 block (direction, levels, weekly range, the spread, the position's mark against the 80 % target and the cut, the next rule, the flag state) and its confirm sentence names what the click authorises
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_fno_f3.py tests/test_fno_desk.py -k 'page or view or sentence or F3' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: pending
