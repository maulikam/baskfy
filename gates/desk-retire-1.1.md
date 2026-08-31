# Gates: BRANCH 1.1 Session continuity — integration

- [x] B1: All FIVE children verified by the driver re-running their checks
  CHECK: for f in gates/desk-retire-1.1.1.md gates/desk-retire-1.1.2.md gates/desk-retire-1.1.3.md gates/desk-retire-1.1.4.md gates/desk-retire-1.1.5.md; do grep -c "^- \[ \]" $f; done | paste -sd+ | bc
  EXPECT: 0
  EVIDENCE: driver-verified 2026-08-31. 1.1.1 5/5, 1.1.2 5/5, 1.1.3 4/4, 1.1.4 8/8, 1.1.5 7/7 —
    0 unchecked, 0 ABANDON across all five. Driver independently re-ran: the DRY_RUN
    token-poisoning fix (22 tests pass; write-path guard confirmed at broker_oauth.py:390), the
    live bridge redirect (302 to desk.modelbasket.in verified through the public host), and the
    fork-point tree hash 2913aae on both desk copies.

- [ ] B2: A Kite session exists for a date after 2026-08-31, in whichever store the chosen
      path writes to
  EVIDENCE: **CANNOT BE SATISFIED TODAY — 31 Aug is still 31 Aug.** Kite mints one token per
    login per day, so "a date AFTER 2026-08-31" is unprovable before the morning of 1 Sep.
    What IS proven now: the box holds a live, unexpired session, `issued_at
    2026-08-31T16:17:28+05:30, expired: False`, pulled through the unchanged M58 leg after the
    redirect moved. 1.1.1 scoped the same gate the same way rather than glossing it.
    See ABANDON below; this is a timing limit, not a missing capability.

ABANDON: B2 unprovable on 2026-08-31 by construction (one Kite token per login per day). The
  capability is demonstrated — a live unexpired session is in the store and the bridge's 302 is
  verified end to end — but the literal wording needs a calendar day that has not happened.
  Provable by Maulik on the morning of 1 Sep via NEEDS-MAULIK.md §30's runbook.

- [x] B3: The desk's Friday-rebalance capability is intact or the plan explicitly records that
      it is not, with Maulik informed
  EVIDENCE: intact. Driver-verified over ssh 2026-08-31: `momentum-web` **active**,
    `momentum-daily.timer` **active**. `portfolio.db` 6,295,552 bytes, sha256 22a38d53...f283d1b
    unchanged. 1.1.5 additionally confirmed NRestarts=0, MainPID 67756 since 27 Aug, newest
    mtime under `app/` still 25 Aug (nothing touched the trading app), `/status` authed:true,
    and **Kite's own order book reports 0 orders today**.
    The Friday path now depends on 1.1.5's bridge plus one human Zerodha login; Maulik has been
    told plainly and the runbook is NEEDS-MAULIK.md §30 with a no-repo fallback.

- [x] B4: On Maulik's chosen path (Baskfy owns the redirect), a real Kite login cannot destroy a
      working session — 1.1.4's fix is in place and tested
  EVIDENCE: driver-verified. The guard moved to the only write path (`broker_oauth.py:390
  store_access_token`), refusing BOTH crossings, rather than to the caller that happened to be
  today's only one. With the box's actual env (DRY_RUN=true, empty secret) the callback now
  answers **503 pipeline-degraded and stores nothing**. 22 tests in test_broker_oauth.py pass.
  Driver also checked the live blob for prior damage: real 32-char token, **not** a `sim_` stub,
  so the window closed before anyone walked through it.

- [x] B5: The desk can obtain a session through the reverse bridge, or 1.1.5 has ABANDONed with
      a reason and Maulik has been told the Friday 4 Sep consequence plainly
  EVIDENCE: built and driver-verified live. A bare Kite return at Baskfy's callback 302s to the
  desk: `curl https://staging.baskfy.com/api/v1/brokers/callback?request_token=PROBE123`
  -> `302 -> https://desk.modelbasket.in/callback?request_token=PROBE123`. A Baskfy-initiated
  login carries `state` and falls through untouched.
  The design carries the **request_token**, not an access token — which dissolves the deployed
  desk's token format question and the client-cache problem rather than solving them, since the
  desk exchanges with its own secret and `/callback` sets the token on the cached client in
  place. 1.1.5 proved pickup 12/12 on the box against deployed source.
  Capability is narrower than the M58 leg beside it (single-use, minutes-lived vs a token that
  trades all day); six adversarial probes refused six times.
  REMAINING HUMAN STEP, stated plainly: a genuine request_token needs one Zerodha login by
  Maulik. And `BASKFY_KITE_API_SECRET` must STAY EMPTY — setting it would let Baskfy redeem the
  single-use token and starve the desk. This supersedes NEEDS-MAULIK §3 branch A and the
  driver's own earlier advice, which was wrong twice.
