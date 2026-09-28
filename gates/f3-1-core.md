# Gates: F3-1 — the pure core (`baskfy_core.fno.directional`, `F3Config`)

- [x] C1: daily levels: pivot highs/lows over the lookback; support = the highest pivot low below the close, resistance = the lowest pivot high above it; the weekly range (this week's high/low including the last session)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_fno_directional.py -k 'level or weekly' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 14 passed, 35 deselected in 0.10s

- [x] C2: direction: UP/DOWN/NONE from the daily trend, confirmed by the last completed 75-minute bar, aligned by the intraday check; any disagreement is NONE; the key level is the support (UP) or resistance (DOWN)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_fno_directional.py -k 'direction or confirm or intraday or seventy_five' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 49 passed in 0.10s

- [x] C3: the spread: short strike 1 % beyond the weekly low (UP: PUT) or high (DOWN: CALL) rounded to the step, the wing `wing_pct` further out; expiry NIFTY weekly ≥ 2 sessions left, BANKNIFTY monthly ≥ 5; sizing = 20–30 % of capital as margin on entry, bounded by the cut-loss risk budget and max_lots; an ADD only the next session, only if working, never past the full share
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_fno_directional.py -k 'strike or expiry or size or add' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 19 passed, 30 deselected in 0.09s

- [x] C4: exits, in precedence: LEVEL_BREAK (a close beyond the level by the buffer), LOSS_CUT (mark ≥ cut multiple × credit), DECAY_TARGET (mark ≤ 20 % of credit), HARD_EXIT (expiry day at the hard-exit time); HOLD otherwise
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_fno_directional.py -k 'exit or hold' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 7 passed, 42 deselected in 0.09s

- [x] C5: law 1 holds (no I/O, no clock in the module) and every F3Config number is named in 04 §11
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_fno_directional.py -k 'pure or documented' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 2 passed, 47 deselected in 0.09s
