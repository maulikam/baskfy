# Gates: 1.5.2 Decision document for the execute route — WRITE, DO NOT BUILD

Scope: root CLAUDE.md puts "web execute" among the things that stop work, alongside
non-negotiable #1 and counsel item C3. This leaf writes the decision for Maulik to sign.
**It implements nothing.** A leaf that adds an execute route has failed, not succeeded.

- [x] G1: The document states exactly which rules must change, quoting them
  CHECK: grep -ci "non-negotiable #1" docs/DECISION-EXECUTE-ROUTE.md
  EXPECT: /[1-9]/
  EVIDENCE: 9 (measured 31 Aug 2026). §2 quotes all four rules verbatim with file+line:
    non-negotiable #1 at CLAUDE.md:47; the standing "web execute" stop at CLAUDE.md:155; the
    human-track sentence "The web app never gains an execute route" at CLAUDE.md:27-34;
    D3 posture B clause 2 at docs/DECISIONS-MERGE.md §D3, compiled into code at
    packages/core/src/baskfy_core/broker_connections.py:76-86. §2.5 inventories the five tests
    and two source comments (app.py:566, app.py:569) that would have to be deleted, each with
    file and line, and records the measurement that today routers/kite.py has 0 @router.post
    and the 166-route API exposes no order/execute/GTT path.

- [x] G2: It states what becomes true if signed, in plain language, including what can then go
      wrong that cannot go wrong today
  EVIDENCE: docs/DECISION-EXECUTE-ROUTE.md §3 "What becomes true if you sign" — three parts:
    what you get, what can go wrong afterwards that cannot go wrong today (six named vectors),
    what does not change. The contrast is measured, not asserted: today's execute route is
    kite-momentum-rebalancer/app/main.py:515 behind DeskSecurity
    (kite-momentum-rebalancer/app/core/websec.py:58) whose Host allowlist defaults to
    "127.0.0.1,localhost" (websec.py:_hosts) and refuses any other Host with 421, plus an
    Origin/Referer check on every unsafe method and an optional Basic password; plans are a
    fresh uuid4 in an in-memory dict (main.py:132) expiring at 1800s (main.py:525). The
    destination surface is 166 @router.{get,post,put,patch,delete} declarations across 35
    routers in decile-blueprint/services/api/src/baskfy_api/routers/ reached over the public
    internet with Google sign-in (routers/auth.py:192 `@router.post("/auth/google")`) and an
    X-API-Key header (baskfy_api/auth.py:48). The API-key vector is quoted from source:
    packages/core/src/baskfy_core/api_keys.py:78 "What a key may read. **Every member is a
    read.**" with all four scopes granted by default (api_keys.py:95-97) — a default whose
    stated justification stops holding the day a write exists. The half-built multi-tenancy is
    quoted from CLAUDE.md:155-157 "P4.2 two-token OAuth, P4.10 RLS, and P4.11 load tests are
    not [in]".

- [x] G3: It lists the preconditions that must be green BEFORE the route is built — at minimum
      1.2.1 (GTT), 1.2.3 (drill), 1.4.x (parity) — each with its current status
  CHECK: grep -ci "precondition" docs/DECISION-EXECUTE-ROUTE.md
  EXPECT: /[1-9]/
  EVIDENCE: 12 (measured 31 Aug 2026). §4 lists five preconditions, ZERO green, each measured
    today rather than quoted from docs/00-merge-status.md (22 Aug):
    (1) 1.2.1 GTT — ABSENT: `grep -ci "gtt" packages/execution/src/baskfy_execution/gateway.py`
        = 0 over 150 lines; OrderGateway has exactly __init__/_journal/place; the only GTT path
        is kite-momentum-rebalancer/app/kite_client.py:247. gates/desk-retire-1.2.1.md: 7 of 7
        unchecked.
    (2) 1.2.3 DRY_RUN drill — NOT BUILT: no "drill" string anywhere in
        decile-blueprint/services/worker/src/. gates/desk-retire-1.2.3.md: 6 of 6 unchecked.
    (3) 1.4.1 parity — UNMEASURED, and the status page is misleading: `uv run pytest
        packages/core/tests/test_reference_parity.py -q` today = "44 passed, 2 skipped", but
        the two skips are TestStep2FullRowReproduction (test_reference_parity.py:700 and :743),
        skipped because BASKFY_PARITY_BARS is unset. The 6,934/9,166 comparison has NOT been
        run since it was recorded red; a green suite here is not parity, and the document says
        so explicitly.
    (4) 1.4.2 / 1.4.3 — RED as last measured: 223 vs 239 symbols (DECISIONS-MERGE.md §M13.1,
        14 of 16 rejected by far_from_high on unadjusted pre-split highs) and 4 order deltas of
        15 (§M14.4, AETHER/WELCORP/SHILPAMED/DIVISLAB, the substitution traced to SHILPAMED
        778.75->384.95 on 2025-10-03). Not re-run here (those leaves own it); what WAS verified
        today is that the flag is still safe — SCAN_SOURCE_DEFAULT="upload" at
        kite-momentum-rebalancer/app/config.py:90 and .env.example:163, asserted by
        tests/test_breadth_and_shadow.py:124. gates 1.4.1/1.4.2/1.4.3: 16 of 16 unchecked.
    (5) P4.2 / P4.10 / P4.11 — named as not built in CLAUDE.md:155-157 and in
        DECISIONS-MERGE.md §P4.0's do-not-do list, which ends "...public signup, fee
        collection, or web execute".

- [x] G4: It names the counsel dependency (C3) and whether it is still open
  EVIDENCE: docs/DECISION-EXECUTE-ROUTE.md §5 "The counsel dependency". C3 quoted verbatim from
    docs/COUNSEL-BRIEF.md:90 — "Whether posture B needs **RA registration** and/or
    **Kite-Publisher / empanelment**", with its own answer column "Product claims and the SEBI
    filing path. **Yes before multi-tenant paid**; no for a sole-tenant operator desk". STILL
    OPEN, verified two ways: COUNSEL-BRIEF.md:84 "None currently blocks engineering; C3 blocks
    paid multi-tenant launch", and NEEDS-MAULIK.md §13 row "Counsel checklist C1-C3 (algo ID,
    research vs advice, RA/empanelment) remains open but **non-blocking**". The brief was
    compiled 25 Aug 2026 (COUNSEL-BRIEF.md:11) and the repo records no evidence it was sent.
    §5 also names C1 (algo registration when Baskfy supplies order plans to a third-party Kite
    app) and C2 (research vs advice once personalised) as bearing directly on an execute route
    though neither appears in CLAUDE.md's stop list, and states the split answer: C3's own
    column says no registration for a sole-tenant operator desk, which is what a Maulik-only
    execute route would be; for anyone else's money C3 and C1 gate.

- [x] G5: It offers the alternative that needs no rule change — posture B, publish the basket
      and let the user confirm in their own broker — with its trade-offs
  CHECK: grep -ci "posture B" docs/DECISION-EXECUTE-ROUTE.md
  EXPECT: /[1-9]/
  EVIDENCE: 9 (measured 31 Aug 2026). §6 presents posture B as already built, not hypothetical:
    services/api/src/baskfy_api/kite_basket.py (docstring quoted — the browser posts to
    kite.zerodha.com/connect/basket and Kite renders it in the user's own session) served by
    two READ-ONLY routes, routers/kite.py:100 GET /baskets/plan/kite and :134
    GET /explore/{slug}/kite, with 0 @router.post in that file and
    test_baskets_readonly.py:123 asserting structurally that a POST may never appear there.
    Trade-offs stated with their source: MAX_BASKET_ITEMS=10 (kite_basket.py:60, Kite's own
    limit, so a 15-order rebalance is two form posts); ORDER_TYPE="MARKET" (kite_basket.py:73)
    so limit choice is manual; PRODUCT="CNC" hard-coded (:65); and the one that is easy to
    miss — `grep -c "GTT" kite_basket.py` = 0, so non-negotiable #4's same-session vol-scaled
    stop cannot follow the basket into the user's terminal (the desk arms stops separately at
    main.py:814). What it preserves is listed too. §6 also names a third option: retire
    everything about desk.modelbasket.in EXCEPT the order button, which moves no rule.

- [x] G6: NO execute route was added anywhere in the codebase
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -rn "@router.post" decile-blueprint/services/api/src/baskfy_api/routers/kite.py 2>/dev/null | wc -l
  EXPECT: 0
  EVIDENCE: 0
