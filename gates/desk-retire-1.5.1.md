# Gates: 1.5.1 Live-broker UI pages — inventory and port plan

Scope: M26 moved the desk's record to the web app read-only and deliberately left the
live-broker pages on the console pending D3. D3 posture B is now signed off. Inventory what is
left on the desk's UI and plan the port. Document only — no UI is built here.

- [x] G1: Every page/route the desk serves is listed — complete, count stated
  CHECK: grep -c "^| " docs/DESK-UI-PORT.md
  EXPECT: /[1-9][0-9]*/
  EVIDENCE: `grep -c "^| " docs/DESK-UI-PORT.md` → **77**. The count is stated in the document
  as "**Count: 38 URL surfaces — 34 declared routes, 1 static mount, 3 conditional docs
  routes.**" (docs/DESK-UI-PORT.md §1). 38 is measured, not asserted:
  `grep -cE '^@app\.(get|post)\("' kite-momentum-rebalancer/app/main.py` → **34** (24 GET +
  10 POST), plus `app.mount("/static", …)` at main.py:60, plus /docs, /redoc, /openapi.json
  which exist only when DESK_DOCS=true (app/config.py:38, default false). The §1 inventory
  table has 38 numbered rows, one per surface. Completeness argued in "### Nothing is reachable
  that is not in this table": all 12 files in app/templates/ are named by a
  TemplateResponse call in main.py, app/static/ holds only assets, and
  `grep -n "include_router\|mount(" app/main.py` returns the single /static mount at line 60.
  Both source trees were compared route-for-route: the sorted route sets diff clean
  ("ROUTE SETS IDENTICAL", 34 = 34) — no route exists in one copy and not the other. The
  divergence is below the route line and is tabulated (options.html + analytics/options_view.py
  live-copy-only, so /options 404s in the merged tree; telemetry.py, token_store.py,
  scan_source.py, breadth_source.py, analytics/pg.py merged-copy-only; 16 strangle modules
  live-copy-only; base.html / index.html / settings.html differ).

- [x] G2: Each is classified: already ported to Baskfy, needs porting, or dies with the desk
  CHECK: grep -ci "already ported\|needs porting\|dies with" docs/DESK-UI-PORT.md
  EXPECT: /[1-9]/
  EVIDENCE: `grep -ci "already ported\|needs porting\|dies with" docs/DESK-UI-PORT.md` → **44**.
  Every one of the 38 rows carries a classification in the final column, using exactly those
  words. §2 "Classification tally" sums them to 38: **already ported 10** (routes 9, 10, 13, 14,
  16, 17, 23, 24, 25, 29 — each naming the Baskfy page and API route that does it, e.g.
  /reconcile → Baskfy /reconcile + `GET /desk/reconcile` at routers/desk.py:412; /indices →
  /market/today over `GET /indices/dashboard` at routers/market_data.py:105), **needs porting 9**
  (15, 18, 19, 20, 21, 22, 26, 27, 28), **dies with the desk 14** (2, 3, 6, 7, 8, 30–38),
  **ORDER-CAPABLE/blocked 5** (1, 4, 5, 11, 12). Five routes are two things in one URL and their
  secondary halves are tabulated separately rather than rounded away (#1, #3, #9, #23, #34).
  Each classification is justified in the row itself — e.g. #15 `/indices/constituents` is
  "needs porting" because a repo-wide grep for `constituents` across apps/web/src and
  services/api/src finds only *basket* constituents, never an index drilldown; #30–33 /settings
  "dies with the desk" because it configures a process that will not exist.

- [x] G3: Any page that can place or confirm an order is flagged as blocked on the item-1
      decision (1.5.2), not planned as ordinary work
  EVIDENCE: §3 is a dedicated heading — "## 3. BLOCKED — routes that can place, confirm, modify
  or cancel an order, or set a GTT" — carrying the line "**These are not ordinary porting work
  and are deliberately absent from the §4 plan.**" and naming both blockers: leaf 1.5.2 /
  `docs/DECISION-EXECUTE-ROUTE.md`, and non-negotiable #1 plus the standing rail "The web app
  never gains an execute route." Five routes are listed with per-route evidence:
  `POST /execute` (main.py:515–650, `gw.place(...)` per order); `POST /analyze` (main.py:291 —
  mints the plan_id that main.py:519–521 is the sole acceptor of, i.e. the other half of the
  confirm pair); `GET /` (index.html:169 posts /analyze, index.html:401 posts confirm=true to
  /execute); `GET /stops` (main.py:786–810, mints STOP_PLANS[plan_id] on the same 30-min
  expiry; M26.2 already refused to port it); `POST /stops/arm` (main.py:812–870, `k.delete_gtt`
  then `k.place_gtt_stop` — GTT create AND cancel at the broker, and the documented exception to
  non-negotiable #6). Verified absent from the ordinary plan: the §4 table's seven rows are P1
  ops history, P2 collection health, P3 stop coverage *reported not armed*, P4 index
  constituents, P5 live reconcile *with the reconcile_fills write side-effect explicitly
  removed*, P6 regime backtest, P6c the pre-existing non-executing Kite Publisher hand-off
  marked "*(conditional on 1.5.2)*". No §3 route appears in §4 or §4b.

- [x] G4: The read-only pages have an ordered port plan with effort noted
  EVIDENCE: §4 "Ordered port plan — the read-only pages" is a P1→P6 ordered table with four
  columns: page, why this order, "Baskfy API needed first", and effort in person-days.
  P1 desk operations run history (`GET /desk/ops/operations`, `/desk/ops/jobs`,
  `/desk/ops/jobs/{id}`) — 2–3 d; P2 daily-collection health strip (`GET /desk/collection`) —
  1–2 d; P3 stop coverage reported not armed (`GET /desk/protection`, first live-broker read) —
  3–4 d; P4 index constituents drilldown (`GET /indices/{index}/constituents`) — 2 d; P5
  reconcile against live broker state (`GET /desk/reconcile/live`) — 4–5 d; P6 regime backtest
  evidence (`GET /desk/regime/backtest`) — 2 d; P6c conditional Kite Publisher hand-off (endpoint
  already exists at routers/kite.py:100) — 1 d. Stated total: "**Read-only total: 14–18
  person-days** (P1–P6), of which 7–9 are the two live-broker reads (P3, P5) and 7–9 are pure
  database reads (P1, P2, P4, P6)." §4b lists the four non-order writes separately with their own
  effort (2 d, 3 d, 2 d, 0.5 d) and says they follow P1–P6 rather than interleave.
