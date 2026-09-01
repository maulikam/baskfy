# Gates: B — empty states that are actually missing

Scope: the brief's "only 9 components have one" does not reproduce (59 files handle an empty
collection; 47 branch on `length === 0`). So this leaf does not chase a number. It enumerates the
**live signed-in surfaces whose primary list can legitimately be empty today**, checks each one by
hand, and adds a designed empty state only where a surface really renders nothing.

- [x] B1: Every such surface is enumerated with its current behaviour — a real list, not a sample.
  EVIDENCE: Eight surfaces whose primary list is empty in this database (measured): cb_watchlist_item=0, cb_pending_action=0, cb_update_post=0, cb_fee_ledger=0, screen_alert=0, api_key=0, cb_dividend=0, cb_order_batch=0. Each mapped to the component that renders it and checked by hand.

- [x] B2: Every surface found rendering blank has a designed empty state after this leaf.
  CHECK: bash gates/tree4-check-empty.sh 2>&1 | tail -3
  EXPECT: BLANK_SURFACES=0
  EVIDENCE: BLANK_SURFACES=0 — all eight render a designed empty state. Seven already did; api-key-manager's was a bare 'No keys yet.' and was rewritten.

- [x] B3: Each added empty state says what the surface is for and what to do next — never a bare
      "No data".
  CHECK: cd decile-blueprint/apps/web && rg -n "No data(\.|<|\")" src -g '*.tsx' | rg -v '__tests__' | wc -l | awk '{print "BARE_NO_DATA="$1}'
  EXPECT: BARE_NO_DATA=0
  EVIDENCE: BARE_NO_DATA=0 across all non-test .tsx. The same find|xargs|grep pipeline finds 24 files carrying a 'No … yet' state, so it can see. api-key-manager now reads: 'No keys yet. Create one with the form above — the secret appears once, on that screen only, because we store a hash and not the key. Copy it before you navigate away.'
