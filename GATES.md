# M76 — the four surfaces Maulik reported, fixed where he looks

Reported twice. Last round I fixed APIs and one tile label, verified endpoints and tests, and
never opened the screens. These gates are written against the screenshots, and every CHECK is
run against **staging**, not localhost.

## Root cause of the repeat
`broker-grid.tsx:113` reads `broker.connected` (tile → "Connected", which DID ship and IS visible).
`broker-grid.tsx:206` renders `Connect {short_name}` unconditionally (panel button → unchanged).
`portfolio_holding` is 0 because nothing in the UI can trigger the sync I built.

---

- [x] **G1 — the panel says connected, and does not offer a dead Connect button**
  When `connected` is true the panel shows connected state; Connect is not the primary action.
  CHECK: `grep -c 'selected.connected' decile-blueprint/apps/web/src/components/brokers/broker-grid.tsx`
  EXPECT: >=1
  EVIDENCE: `_connection_state("zerodha") -> (True, 'connected')` on the box; Upstox `not_connected`

- [x] **G2 — a Sync holdings control exists in the UI**
  The endpoint has existed since M75 and no screen could call it.
  CHECK: `grep -c 'Sync holdings' decile-blueprint/apps/web/src/components/brokers/broker-grid.tsx`
  EXPECT: >=1
  EVIDENCE: `Sync holdings` in broker-grid.tsx; 18/18 grid tests

- [x] **G3 — holdings actually land in the database on staging**
  The gate that would have caught the whole failure. Not "the endpoint returns 200".
  CHECK: staging `select count(*) from portfolio_holding`
  EXPECT: > 0
  EVIDENCE: staging `portfolio_holding: 17`

- [x] **G4 — the Portfolio pages stop saying "not synced" once they are synced**
  Overview, Portfolios, Holdings and Activity all read from the same rows as G3.
  CHECK: staging `select count(*) from portfolio where source='HOLDING_GROUP'`
  EXPECT: > 0
  EVIDENCE: staging `portfolio where source='HOLDING_GROUP'` = 1 — "Zerodha holdings", CAPITAL

- [x] **G5 — the freshness pill says a run is in progress instead of blaming the last one**
  `freshness-pill.tsx:94` says "The last pipeline run did not publish" while a run is mid-flight.
  CHECK: `grep -c 'in progress' decile-blueprint/apps/web/src/components/shell/freshness-pill.tsx`
  EXPECT: >=1
  EVIDENCE: `pipeline_running` in /meta/status; pill renders `· updating…`

- [x] **G6 — 1 Sep data is published on staging**
  CHECK: staging `select max(date) from ohlcv_daily`
  EXPECT: 2026-09-01
  EVIDENCE: staging `max(date) = 2026-09-01`, 8537 bars, data_version 7

- [x] **G7 — gates run green and the change is deployed**
  CHECK: `make lint` and `make test` exit 0; staging serves the new tag.
  EXPECT: both 0
  EVIDENCE: LINT 0 / TEST 0 (3260 py, 2008 web, 117 client); all 8 containers on 7793198; verify-live 0; verify-safety 0

- [x] **G8 — the nightly stops asking about instruments that have never traded** *(found mid-flight)*
  EVIDENCE: deployed `active_instruments` returns 10142, was 41443; 0 bars lost; run 23 succeeded
  in 2h19m (23:06→01:25) where the old shape never finished at all.
