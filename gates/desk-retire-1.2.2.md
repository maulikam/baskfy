# Gates: 1.2.2 Gateway parity audit against the live desk

Scope: enumerate every order-affecting behaviour the desk's gateway has and Baskfy's does not.
Analysis leaf — produces a document, changes no execution code.

Measured at Baskfy `7d6b7fb3aa6d23dadb30d645d28884a8b7eb6d84`, external desk `1cb5cb5`.
Deliverable: `docs/GATEWAY-PARITY.md`.

- [x] G1: Every public method of the desk's `app/core/gateway.py` is listed against its Baskfy
      counterpart, with present/absent/differs — complete, not sampled, count stated
  CHECK: grep -c "| *present\|| *absent\|| *differs" docs/GATEWAY-PARITY.md
  EXPECT: /[1-9][0-9]*/
  EVIDENCE: 40. §1.1 lists the complete named surface of the desk's gateway — 8 entries
    (`log`, `JOURNAL`, `FAILED_STATUSES`, `_DEFINITIVE_REFUSALS`, `ProductGates`,
    `OrderGateway.__init__`, `_journal`, `place`), stated as the whole file, not a sample.
    `place` is the desk gateway's ONLY public method, so it is broken out twice more: a
    13-vs-14 parameter table and a 6-row body-step table. Tally stated in the document:
    4 present-identical, 3 differs, 1 Baskfy-only; within `place`, 11 params present,
    1 absent (`market_protection`), 2 Baskfy-only (`tenant`, `plan_tenant`), 3 genuine body
    gaps and 2 Baskfy improvements.

- [x] G2: Same for `guards.py`, `risk.py`, `ratelimit.py`
  CHECK: grep -ci "guards.py" docs/GATEWAY-PARITY.md
  EXPECT: /[1-9]/
  EVIDENCE: 10. §1.2 guards.py (8 entries, all present) — 1.1.2's byte-identity claim was NOT
    restated, it was re-measured: `git show HEAD:app/core/guards.py > /tmp/g_ext.py; cmp` at the
    two SHAs returned no output. §1.3 ratelimit.py (9 entries, all present) — same `cmp`, also
    identical. §1.4 risk.py (7 entries) — `cmp` DIFFERS at char 1275/line 39; `diff -u` shows the
    entire delta is one method: external `on_pnl(self, pnl, basis="")` vs Baskfy
    `on_pnl(self, pnl)`. 32 named entries compared across §1 in total.

- [x] G3: The seven non-negotiables are each mapped to the Baskfy code that enforces them, or
      marked UNENFORCED with what is missing
  CHECK: grep -c "non-negotiable" docs/GATEWAY-PARITY.md
  EXPECT: /[1-9]/
  EVIDENCE: 13. §2 maps all seven to file:line with a scorecard table. 0 of 7 fully UNENFORCED;
    #4 partially unenforced (vol arithmetic at `baskfy_core/score.py:210`, but arming is
    desk-only and 1.2.1 is porting it). #1, #4, #6 depend on code that retiring the desk deletes.
    Test audit done as required: 16 collected tests VERIFIED by
    `pytest tests/test_seven_non_negotiables.py --collect-only -q` (12 functions, two
    parametrised ×3). Three tests fail house rule 2 and are named: #3 (line 76) is
    `assert "pledged_qty" in src` — asserts an identifier exists, would pass with an unpledge
    gate added; #4 (line 88) tests only `stop_from_vol` despite its name, asserts nothing about
    "every buy" or "same session"; #6b (line 156) is a real AST walk but scans only
    `pathlib.Path("app")`, not `packages/` or `services/`. Also found: the 16 tests live in
    `kite-momentum-rebalancer/tests/`, while `baskfy_execution/__init__.py:15` points at
    `packages/execution/tests/test_non_negotiables.py`, which does not exist — that directory
    holds 3 files / 11 tests, none of them a non-negotiable test.

- [x] G4: Behaviours only in the external repo (market-order protection band, daily loss cap
      against NAV) are included and flagged as not-yet-in-Baskfy
  CHECK: grep -ci "protection band" docs/GATEWAY-PARITY.md
  EXPECT: /[1-9]/
  EVIDENCE: 3. §3 carries six, each with source file:line and flagged not-yet-in-Baskfy.
    §3.1 market-order protection band (external `app/core/gateway.py:95-104`, `:57`,
    `app/config.py:108`, `app/kite_client.py:191-194`; `market_protection` = 0 files repo-wide).
    §3.2 `REBALANCE_ORDER_TYPE` + `ref_price` journal fallback (`app/config.py:90-91`,
    `app/main.py:451`, `app/analytics/regime_run.py:242`, `app/core/gateway.py:118`).
    §3.3 `fit_to_funds` (`app/rebalance.py:235-322`, measured 88 lines, not 1.1.2's 91; its test
    is 329 lines, not 266). §3.4 NAV loss cap (`app/core/risk.py:39-46`, `app/main.py:386-412`).
    Two further items found in this pass: §3.5 the CSRF fix, included because `/stops/arm` is a
    plain form and a silently-refused arming is non-negotiable 4 not running; §3.6 `order_type`
    missing from the DRY_RUN journal record, and the fact that `/execute` deliberately does not
    arm stops being recorded only in the retiring tree. §3.1+§3.2 explicitly marked as
    must-land-together per 1.1.2 §4.3. §3.7 records the M4 risk-ceiling hardening running the
    other way, so nobody "resolves" it by copying external.

- [x] G5: The document ends with an ordered list of what must be ported before the desk can be
      retired, each item with the source file and line
  EVIDENCE: §5 is the final section of `docs/GATEWAY-PARITY.md` (the reproduction appendix was
    moved to §4 so the port list literally ends the document). Ten items, P1–P10, each with
    source file:line, destination file, dependencies and a rough size:
    P1 protection band (ext `gateway.py:95-104`, `:57`, `config.py:108`, `kite_client.py:191-194`,
    ~15 lines) → P2 MARKET + `ref_price` (ext `config.py:90-91`, `main.py:451`,
    `regime_run.py:242`, `gateway.py:118`, `:90`, ~10 lines) — hard ordering, P1 first;
    P3a `on_pnl` basis (ext `risk.py:39-46` → `baskfy_execution/risk.py:39`, ~6 lines) →
    P3b NAV block (ext `main.py:386-412` → subtree `main.py:540-542`, ~25 lines, every helper
    already present at `main.py:145`, `db.py:729`, `db.py:642`);
    P4 `fit_to_funds` (ext `rebalance.py:235-322` → `baskfy_core/basket.py`, ~90 + 330 test lines
    — largest item); P5 CSRF + Caddyfile (ext `websec.py:77-112`, ~15 lines, deploy blocker);
    P6 GTT into the gateway (subtree `kite_client.py:247`, `:228`, `main.py:812-870` — IN FLIGHT
    in leaf 1.2.1, marked do-not-start); P7 re-home the 16 non-negotiable tests to the path
    `__init__.py:15` already falsely claims, with the three weak tests named for repair;
    P8 re-home non-negotiable 1's gates and #6c's `client_id` (subtree `main.py:519`, `:521`,
    `:524`, `:581` — no owner in `packages/`, gates retirement outright);
    P9 stop-coverage audit (`app/analytics/protection.py`, ~200 lines, depends on P6);
    P10 make `EXCLUDED_SYMBOLS` and `guards.py:11` `UNTOUCHABLE_SYMBOLS` agree by test rather
    than by coincidence (~20 test lines). Dependency graph and a recommended sequence
    (P1→P2→P3→P5→P4→P6→P9→P7→P10, P8 in parallel) close the section.
