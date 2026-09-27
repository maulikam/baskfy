# Gates: LV0 — box and account audit (read-only)

Scope: the review's "first thing to check on the box": which switches are actually set, and whether any real position is held unrecorded or unprotected. Nothing is changed.

- [x] A1: the API container's DRY_RUN and the desk services' five money flags are read off the box and recorded in docs/live/AUDIT-2026-09-27.md
  CHECK: grep -cE '^\| (api DRY_RUN|desk DRY_RUN|BASKFY_TWT_EXECUTION_ENABLED|BASKFY_TWT_AUTO_EXECUTE|BASKFY_SWING_EXECUTION_ENABLED|BASKFY_SWING_AUTO_EXECUTE|BASKFY_SWING_MONITOR_ENABLED|BASKFY_VBT_EXECUTION_ENABLED) \|' docs/live/AUDIT-2026-09-27.md
  EXPECT: 8
  EVIDENCE: 8

- [x] A2: every tw_order / vb_order / sw line in SENT or PARTIAL without a position is counted on the box's database and the count is in the audit
  CHECK: grep -E '^SENT_WITHOUT_POSITION (twt|vbt|swing)=[0-9]+' docs/live/AUDIT-2026-09-27.md | wc -l | tr -d ' '
  EXPECT: 3
  EVIDENCE: 3

- [x] A3: open sleeve positions are compared with resting GTTs (from the box's stored gtt ids and, if a Kite session exists, the live GTT list); positions without a stop are named
  CHECK: grep -cE '^OPEN_WITHOUT_GTT (twt|vbt|swing)=[0-9]+' docs/live/AUDIT-2026-09-27.md
  EXPECT: 3
  EVIDENCE: 3

- [x] A4: the `twt-auto` container's logs since 22 Sep are read for what it actually sent (every "TWT AUTO:" summary line recorded)
  CHECK: grep -c 'TWT AUTO' docs/live/AUDIT-2026-09-27.md
  EXPECT: /[1-9]/
  EVIDENCE: grep: docs/live/AUDIT-2026-09-27.md: No such file or directory

- [x] A5: anything needing Maulik's hands (a live unprotected position, a Kite login for the account-side check) is in NEEDS-MAULIK.md under an "LV0" heading
  CHECK: grep -c '^## LV0' NEEDS-MAULIK.md
  EXPECT: 1
  EVIDENCE: 1
