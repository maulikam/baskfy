# What Baskfy's execution layer still does not do

**Leaf 1.2.2, desk retirement tree. Written 31 Aug 2026. Every tree read was read-only; this
leaf changed no execution code.**

Leaf 1.1.2 (`docs/DESK-SOURCE-RECONCILIATION.md`) answered *which copy of the desk is
authoritative*. This document answers the different question that actually unblocks retirement:
**what does Baskfy's execution layer still not do, and in what order must it be fixed.** §5 is
the deliverable; §1–§3 are the evidence for it and §4 reproduces every measurement.

## 0. Measurement point — read this before quoting any line number

| Tree | Path | State measured |
|---|---|---|
| **Baskfy monorepo** | `/Users/maulikdave/Documents/projects/baskfy` | commit **`7d6b7fb3aa6d23dadb30d645d28884a8b7eb6d84`** ("M62: green — a day NSE published is never inferred a holiday") |
| **Live desk (external)** | `/Users/maulikdave/Documents/portfolio/kite-momentum-rebalancer` | branch `indices-board`, HEAD **`1cb5cb5454cae01e31afe96840ca2b7c06df7a4e`**, working tree clean |

**GTT is in flight and this document does not measure it.** Sibling leaf **1.2.1** is adding a
GTT path to `packages/execution` concurrently with this audit. At the committed SHA above,
`git show HEAD:decile-blueprint/packages/execution/src/baskfy_execution/gateway.py | grep -c gtt`
returns **0** — the gateway has no GTT surface at all. While this leaf was running, 1.2.1's
uncommitted work appeared in the working tree: a new
`decile-blueprint/packages/execution/src/baskfy_execution/gtt.py` (status vocabulary, `StopBand`,
`TickSizes`, `gtt_params`, `refuse_stop`, `band_finding`, `kill_switch_reason`) and an edited
`gateway.py` that imports it and adds a `self._gtt_sent` idempotency map — but as of the last
read, `OrderGateway` still exposed only `__init__`, `_journal` and `place`, with no
`place_gtt_stop`/`delete_gtt` method yet. **Everything this document says about GTT absence
describes the committed SHA and is expected to be superseded by 1.2.1.** It is recorded, not
reported as a new finding.

Two numbers from 1.1.2 are corrected by direct measurement here, both harmlessly:
external `tests/test_funding_fit.py` is **329** lines, not 266; external `fit_to_funds` spans
`app/rebalance.py:235-322` (**88** lines including its docstring), not 91.

---

## 1. Method-by-method comparison

### 1.1 `app/core/gateway.py` (external `1cb5cb5`) vs `baskfy_execution/gateway.py` (`7d6b7fb3`)

**The desk's gateway has exactly one public method.** The complete named surface of the module
is **8 entries** — 4 module-level names, 1 constructor, 1 private helper, 1 public method, and
one Baskfy-only dataclass. All 8 are below; this is the whole file, not a sample.

| # | Name | Kind | In Baskfy | Note |
|---|---|---|---|---|
| 1 | `log` | module global | present | `logging.getLogger("gateway")`, identical |
| 2 | `JOURNAL` | module constant | present | `"data/outputs/orders_journal.jsonl"`, identical |
| 3 | `FAILED_STATUSES` | module constant | present | `{"ERROR","REJECTED","BLOCKED","RISK_BLOCKED"}`, identical, comment included |
| 4 | `_DEFINITIVE_REFUSALS` | module constant (private, load-bearing) | present | identical 4-tuple |
| 5 | `ProductGates` | frozen dataclass | absent | **Baskfy-only, in Baskfy's favour.** `baskfy_execution/gateway.py:18-33`. The desk reads `C.DRY_RUN`/`C.INTRADAY_ENABLED`/`C.OPTIONS_ENABLED` off its config module directly (`app/core/gateway.py:71,74,86`); Baskfy injects a fail-closed gates callable. Nothing to port |
| 6 | `OrderGateway.__init__` | constructor | differs | External `app/core/gateway.py:42-45` takes `(kc, risk)`. Baskfy `gateway.py:62-73` adds keyword-only `gates=ProductGates` and `journal_path=JOURNAL`. A superset — nothing to port |
| 7 | `OrderGateway._journal` | private method | differs | External writes the module constant `JOURNAL`; Baskfy writes `self._journal_path`. A superset |
| 8 | `OrderGateway.place` | **the only public method** | differs | **The one row that matters.** Broken out below |

**`OrderGateway.place` — parameter-level comparison (13 external parameters, 14 Baskfy).**

| Parameter | In Baskfy | Note |
|---|---|---|
| `symbol`, `qty`, `side` | present | identical, keyword-only |
| `product="CNC"` | present | identical default |
| `order_type="LIMIT"` | present | identical default. **Baskfy has no caller that passes anything else** — see §3.2 |
| `price`, `exchange="NSE"`, `variety="regular"` | present | identical |
| `client_id`, `gross_exposure`, `series`, `tick_size` | present | identical |
| `market_protection: float \| None = None` | **absent** | **§3.1. The gap that refuses every order the moment MARKET is used** |
| `tenant: TenantIds`, `plan_tenant: TenantIds` | absent | Baskfy-only, required keyword-only. P4.3 Law-2 tenancy. In Baskfy's favour; nothing to port |

**`place` body — the five behavioural differences, in body order.**

| Body step | Baskfy | Note |
|---|---|---|
| Cross-tenant refusal, before every guard | absent | Baskfy-only (`gateway.py:89-91`) via `refuse_cross_tenant`. In Baskfy's favour |
| `assert_tradeable` → `assert_not_overnight_option` → MIS gate → NFO/BFO gate → `risk.pre_order` → idempotency → rate limit | present | Byte-for-byte the same sequence, same comments, same return shapes. Layer order preserved |
| DRY_RUN journal record | differs | External `app/core/gateway.py:88-90` records `order_type`; Baskfy `gateway.py:123-124` does **not**. Cosmetic today, wrong the moment §3.2 lands — a dry run of a MARKET batch would be indistinguishable in the journal from a LIMIT one |
| `if order_type == "MARKET": params["market_protection"] = …` | **absent** | External `app/core/gateway.py:95-104`. **No `if order_type == "MARKET"` branch exists anywhere in Baskfy.** §3.1 |
| `ref_price` journal fallback on the placed record | **absent** | External `app/core/gateway.py:118` (`ref = {} if "price" in params else {"ref_price": …}`). Baskfy `gateway.py:139` journals `{"event":"placed","order_id":oid, **params}` with no fallback. §3.2 |
| LIMIT tick-rounding, exception taxonomy, `reached_exchange` | present | identical, comments included |

**Tally for §1.1: 8 named entries — 4 present-identical, 3 differs, 1 absent-from-external
(Baskfy-only). Within the one public method: 11 parameters present, 1 absent, 2 Baskfy-only;
3 body differences that are genuine gaps (`market_protection` branch, `ref_price` fallback,
`order_type` in the dry-run record), 2 that are Baskfy improvements.**

### 1.2 `app/core/guards.py` — claim verified independently

1.1.2 reports this file byte-identical. **Verified here rather than restated**, with `cmp` at
the two SHAs in §0:

```
git -C $EXT show HEAD:app/core/guards.py > /tmp/g_ext.py
cmp /tmp/g_ext.py decile-blueprint/packages/execution/src/baskfy_execution/guards.py
  -> no output (identical)
```

`guards.py` is therefore **byte-identical** and the claim holds. Its complete public surface —
**8 entries**, all `present` and all identical:

| # | Name | In Baskfy |
|---|---|---|
| 1 | `UNTOUCHABLE_SYMBOLS` (`frozenset({"SGBDE31III"})`) | present |
| 2 | `UNTOUCHABLE_PREFIXES` (`("SGB",)`) | present |
| 3 | `UNTOUCHABLE_SERIES` (`{"GB","GS"}`) | present |
| 4 | `CARRY_PRODUCTS`, `DERIVATIVE_EXCHANGES` | present |
| 5 | `UntouchableInstrumentError` | present |
| 6 | `OvernightOptionError` | present |
| 7 | `is_option`, `assert_not_overnight_option`, `assert_tradeable`, `filter_tradeable` | present |
| 8 | (no other names) | — |

One thing the byte-identity hides, recorded because it is a real coupling and not a diff:
`guards.py` carries **its own hardcoded** `UNTOUCHABLE_SYMBOLS`. It does **not** read the desk's
`config.EXCLUDED_SYMBOLS` (`kite-momentum-rebalancer/app/config.py:43`). The two lists agree
today only because both hold exactly `{"SGBDE31III"}`. See §2, non-negotiable 7.

### 1.3 `app/core/ratelimit.py` — claim verified independently

Same method, same result:

```
git -C $EXT show HEAD:app/core/ratelimit.py > /tmp/r_ext.py
cmp /tmp/r_ext.py decile-blueprint/packages/execution/src/baskfy_execution/ratelimit.py
  -> no output (identical)
```

**Byte-identical**; the claim holds. Complete surface — **9 entries**, all `present`:

| # | Name | In Baskfy |
|---|---|---|
| 1 | `Bucket` | present |
| 2 | `Bucket.__init__(capacity, per_seconds)` | present |
| 3 | `Bucket.take` (async) | present |
| 4 | `Spacer` | present |
| 5 | `Spacer.__init__(rate)` | present |
| 6 | `Spacer.take` (async) | present |
| 7 | `KiteLimits.__init__` (9/s spacer, 380/min, 2900/day) | present |
| 8 | `KiteLimits.order_slot` (async) | present |
| 9 | `KiteLimits.api_slot` (async) | present |

### 1.4 `app/core/risk.py` — one method differs, and it is the kill switch

`cmp` reports a difference at char 1275, line 39. `diff -u` gives the **entire** delta: one
method, `on_pnl`.

| # | Name | In Baskfy | Note |
|---|---|---|---|
| 1 | `RiskConfig` (5 fields, same defaults) | present | identical |
| 2 | `RiskState` (4 fields) | present | identical |
| 3 | `RiskManager.__init__` | present | identical |
| 4 | `RiskManager._roll` | present | identical |
| 5 | `RiskManager.kill` | present | identical |
| 6 | `RiskManager.on_pnl` | **differs** | External `app/core/risk.py:39` is `on_pnl(self, pnl: float, basis: str = "")` and its kill message reads `daily loss cap hit ({pnl:,.0f} {basis})`. Baskfy `baskfy_execution/risk.py:39` is `on_pnl(self, pnl: float)` — **no `basis`**. §3.4 |
| 7 | `RiskManager.pre_order` | present | identical, all four caps |

**Tally for §1.4: 7 entries — 6 present-identical, 1 differs.** That single differing method is
the whole file-level divergence.

**§1 total: 8 + 8 + 9 + 7 = 32 named entries compared. 30 present, 4 differs, 1 absent
(`market_protection`), 1 Baskfy-only (`ProductGates`) — the counts overlap because
`OrderGateway.place` is counted once as an entry and again parameter-by-parameter.**

---

## 2. The seven non-negotiables, mapped to the Baskfy code that enforces each

Root `CLAUDE.md` carries these. Each is traced to the file and line that actually enforces it in
this monorepo, or marked **UNENFORCED**. The tests are audited too: house rule 2 says a test that
only locks in what the code happens to do today is not worth writing, and this section is the
audit that catches those.

**Test-count verification.** The claim of 16 named tests is **correct, but the file is not where
`baskfy_execution` says it is.** `baskfy_execution/__init__.py:15` states the tests live in
`packages/execution/tests/test_non_negotiables.py` — **that file does not exist.**
`decile-blueprint/packages/execution/tests/` contains only `test_broker_adapters.py`,
`test_broker_ports.py` and `test_tenant_isolation.py`, **11** test functions in total, none of
which is a non-negotiable test. The real file is
`kite-momentum-rebalancer/tests/test_seven_non_negotiables.py`, 12 test functions, two of them
parametrised three ways: `pytest --collect-only -q` reports **16 tests collected**. The count is
right and the pointer is wrong — and the file sits inside the tree this whole exercise is
retiring. See §4, item P7.

### Non-negotiable 1 — never auto-execute

* **`confirm=true` gate:** `kite-momentum-rebalancer/app/main.py:519` (`if confirm != "true":`)
  and again for stops at `app/main.py:816`.
* **`plan_id` issued by `/analyze`:** `app/main.py:521` (`plan = PLANS.get(plan_id)`).
* **30-minute expiry:** `app/main.py:524`, and `app/main.py:821` for stop plans.
* **`DRY_RUN` simulates end to end:** **this half is in the merged packages** —
  `baskfy_execution/gateway.py:31` (`ProductGates.dry_run: bool = True`, fail-closed) and
  `gateway.py:121-125`, which returns `DRY_RUN` with a `DRY-` order id and never calls the broker.
  The desk binds it at `kite-momentum-rebalancer/app/core/gateway.py:24-30`.
* **Web app has no execute route:** enforced by
  `decile-blueprint/services/api/tests/test_explore_no_orders.py`,
  `test_ac_no_orders.py`, `test_desk_readonly.py`, `test_baskets_readonly.py` — each greps the
  API source for `place_order` / `place_gtt` / `confirm=true` and fails on a hit.
* **Verdict: ENFORCED, but three-quarters of it is in the tree being retired.** The `DRY_RUN`
  half is portable; the confirm/plan_id/expiry half exists **only** in `app/main.py` and has no
  owner in `packages/`. Retiring the desk without re-homing it deletes non-negotiable 1's
  enforcement.
* **Test quality:** `test_non_negotiable_1_...` (line 27) asserts four **literal source strings**
  in `app/main.py`. That is a spec assertion expressed as a string match — brittle (a rename of
  `PLANS` breaks it with no behaviour change) but it does assert the rule, not current behaviour.
  `test_non_negotiable_1b_...` (line 41) is genuinely behavioural: it hands the gateway an
  `ExplodingKC` whose `place_order` raises `AssertionError`, so a DRY_RUN that leaked would fail
  loudly. **Sound.**

### Non-negotiable 2 — holdings quantity = `quantity` + `t1_quantity` + `collateral_quantity`

* **Desk:** `kite-momentum-rebalancer/app/kite_client.py`, the expression asserted verbatim at
  `tests/test_seven_non_negotiables.py:70`.
* **Merged packages:** `baskfy_execution/adapters.py:127` `total_quantity(row)` and
  `broker_ports.py`, asserted by
  `decile-blueprint/packages/execution/tests/test_broker_ports.py:16`
  (`test_total_quantity_is_non_negotiable_two`) and
  `test_broker_adapters.py:36` (`test_holdings_mapper_sums_the_desk_way`).
* **Verdict: ENFORCED, in both trees, and the merged copy is the better one** (Decimal, per
  house rule 9).
* **Test quality:** the desk's test (line 63) is a bare source-string grep. The merged tests are
  behavioural and check the arithmetic. **Sound overall — the packages carry the real one.**

### Non-negotiable 3 — pledged shares sell directly; the plan flags them as info only

* **Enforcement:** `kite-momentum-rebalancer/app/kite_client.py` (`pledged_qty` surfaced), and
  the absence of any unpledge gate on the sell path — external `app/main.py:443-444` documents
  it, subtree the same.
* **Verdict: ENFORCED by absence** — there is no gate to point at, which is the point.
* **Test quality: WEAK, and this is a real finding.**
  `test_non_negotiable_3_pledged_shares_are_information_not_a_gate`
  (`tests/test_seven_non_negotiables.py:76`) asserts exactly
  `assert "pledged_qty" in src`. That asserts a **name exists in a file**. It would pass with the
  variable assigned and never read, and it would pass with an unpledge gate added three lines
  below. It does not assert the rule; it locks in the presence of an identifier. **This is the
  one test in the 16 that house rule 2 would refuse.**

### Non-negotiable 4 — every buy gets a GTT stop the same session, vol-scaled 8-12%

* **The vol-scaling half is in the merged packages:**
  `decile-blueprint/packages/core/src/baskfy_core/score.py:210` `stop_from_vol(price, ann_vol,
  cfg)`, band from `kite-momentum-rebalancer/app/config.py:179`
  (`STOP_MIN, STOP_MAX, STOP_VOL_MULT = 0.08, 0.12, 2.2`).
* **The "every buy, same session" half is UNENFORCED in `packages/`.** At SHA `7d6b7fb3` the
  merged gateway has **no GTT surface**. The whole arming pipeline is desk-only:
  `kite-momentum-rebalancer/app/kite_client.py:247` `place_gtt_stop`, `:228` `delete_gtt`;
  `app/analytics/protection.py` `build_stop_plan` + the coverage audit
  (MISSING/PARTIAL/TOO_FAR/TOO_CLOSE/ORPHAN/EXCESS); the `/stops` review page at
  `app/main.py:837`; and `/stops/arm` at `app/main.py:812-870`.
* **A structural fact worth stating plainly:** `/execute` **does not arm stops** —
  `tests/test_execute_gateway.py:186` is literally
  `test_execute_does_not_arm_stops_any_more`. The rule is satisfied by a *separate, human-confirmed
  step* on the same session, not automatically by the buy path. Any Baskfy-side re-home must
  preserve that shape or the rule silently changes meaning.
* **Verdict: PARTIALLY ENFORCED — arithmetic in `baskfy_core`, arming entirely in the desk tree.
  ⚠ Leaf 1.2.1 is closing exactly this gap concurrently (see §0); do not treat this as a new
  finding.**
* **Test quality: the weakest coverage of the seven.**
  `test_non_negotiable_4_every_buy_is_stopped_and_the_stop_is_vol_scaled`
  (`tests/test_seven_non_negotiables.py:88`) tests **only** `stop_from_vol` clamping to the floor
  and ceiling. Despite its name it asserts **nothing** about "every buy" or "the same session."
  The behaviour is covered elsewhere — `tests/test_stops_route.py` has 20 tests including
  `test_confirming_places_one_stop_per_row` (line 120), `test_a_plan_is_single_use` (line 130),
  `test_one_rejection_does_not_abandon_the_rest` (line 141) — but the index test that claims to
  cover the rule covers half of it, and the file's own docstring says an index pointing at nothing
  is how a rule quietly stops being enforced.

### Non-negotiable 5 — product gates: CNC-only; MIS needs `INTRADAY_ENABLED`, NFO/BFO needs `OPTIONS_ENABLED`, both default off, enforced inside the gateway

* **Enforcement:** `baskfy_execution/gateway.py:106-111` — `if product == "MIS" and not
  gates.intraday_enabled` and `if exchange in ("NFO","BFO") and not gates.options_enabled`, both
  returning `BLOCKED`. Defaults at `gateway.py:31-33` are all refusing.
* **Verdict: ENFORCED, inside the gateway, and Baskfy's version is stronger than the desk's** —
  the desk reads mutable module globals, Baskfy takes an injected fail-closed dataclass.
* **Test quality: sound.**
  `test_non_negotiable_5_product_gates_block_inside_the_gateway` (line 111) is parametrised over
  MIS/NSE, NRML/NFO, NRML/BFO with an `ExplodingKC` and `dry_run=False`, so a gate that leaked
  would reach a broker that raises. `test_non_negotiable_5b_...` (line 131) asserts the three
  defaults are refusing. Both assert the rule.

### Non-negotiable 6 — all order flow through the gateway; `client_id = plan_id:symbol`; SGB/G-sec blocked at the lowest layer; the documented GTT exception

* **Layer order:** `baskfy_execution/gateway.py:93` (layer 1 untouchables), `:113` (layer 2
  risk), `:118` (layer 3 idempotency), `:120` (layer 4 rate limits). Journal at `:139`.
* **`client_id = plan_id:symbol`:** `kite-momentum-rebalancer/app/main.py:581`
  (`client_id=f"{plan_id}:{o['symbol']}"`), consumed at `baskfy_execution/gateway.py:117-119`,
  which returns `DUPLICATE` on a repeat. **The idempotency mechanism is in the packages; the
  key construction is in the desk tree.**
* **Nothing outside the gateway calls `place_order`:** AST-enforced by
  `tests/test_seven_non_negotiables.py:156`, and structurally by
  `baskfy_execution/brokers.py:34-40`, which splits `MARKET_DATA_ONLY` from `TRADING_ONLY` so a
  market-data client has no `place_order` surface at all.
* **The GTT exception:** still open at SHA `7d6b7fb3`, exactly as root `CLAUDE.md` documents.
  `/stops/arm` calls `kite_client.place_gtt_stop` directly
  (`kite-momentum-rebalancer/app/main.py:848`), borrowing only the gateway's rate limiter
  (`await gw.limits.api_slot()`, `app/main.py:846`) and the guard inside `place_gtt_stop`. The
  code says so in a comment at `app/main.py:843-845`. **⚠ 1.2.1 is closing this; see §0.**
* **Verdict: ENFORCED for orders, with the documented GTT exception still standing at the
  measured SHA.**
* **Test quality: mostly sound, one brittle.** `test_..._6_the_four_layers_are_in_order`
  (line 144) reads the gateway source and asserts the four `layer N:` comment markers appear in
  order — it asserts a **comment ordering**, so a reordering that also moved the comments would
  pass. It is nonetheless the only cheap structural check available and it is honest about being
  structural. `test_..._6b_nothing_outside_the_gateway_calls_place_order` (line 156) is a real
  AST walk over `app/**/*.py` and is the strongest test in the file. **But it walks
  `pathlib.Path("app")` — the desk's own directory, relative to CWD. It does not scan
  `decile-blueprint/packages` or `services` at all.** `test_..._6c` (line 173) is a
  literal-source-string grep for the f-string; brittle, but it does assert the rule.

### Non-negotiable 7 — filter-rejected stocks are never bought; `EXCLUDED_SYMBOLS` instruments are untouchable

* **Untouchable, at the lowest layer:** `baskfy_execution/guards.py:65-70` `assert_tradeable`,
  called at `baskfy_execution/gateway.py:93` before any network call. Prefix- and series-aware
  (`guards.py:11-14`), which is the fix for the SGBDE31III-GB incident.
* **Filter-rejected never bought:** `baskfy_core/score.py:117`
  (`u.loc[u.symbol.isin(cfg.EXCLUDED_SYMBOLS), "reject"] += "excluded_instrument;"`) and the
  planner guard at `baskfy_core/basket.py:103`.
* **Verdict: ENFORCED — and this is the non-negotiable in the healthiest shape**, because both
  halves already live in the merged packages.
* **⚠ One coupling to record.** `guards.py:11` hardcodes
  `UNTOUCHABLE_SYMBOLS = frozenset({"SGBDE31III"})`. The desk's
  `config.EXCLUDED_SYMBOLS` (`kite-momentum-rebalancer/app/config.py:43`) is a **separate** set
  that happens to hold the same one symbol. Adding a symbol to `EXCLUDED_SYMBOLS` today makes the
  **scorer** reject it but leaves the **gateway** willing to trade it. The rule as written in
  `CLAUDE.md` ("`EXCLUDED_SYMBOLS` instruments are untouchable") is satisfied by coincidence, not
  by construction. Deliberate — `services/api/src/baskfy_api/kite_basket.py:22-25` explains that
  `EXCLUDED_SYMBOLS` is one person's holding and must not leak into other users' baskets — but
  the coincidence should be a test, not a coincidence.
* **Test quality: sound.** `test_non_negotiable_7_...` (line 184) is parametrised over
  `SGBDE31III`, `SGBDE31III-GB`, `SGBJUN29` and asserts the raise. `test_..._7b` (line 193) runs
  the full gateway with `dry_run=False` against an `ExplodingKC` and asserts the raise happens
  before the broker is touched. Both assert the rule. Neither covers the `EXCLUDED_SYMBOLS`
  ↔ `UNTOUCHABLE_SYMBOLS` coupling above.

### Scorecard

| # | Non-negotiable | Status at `7d6b7fb3` |
|---|---|---|
| 1 | Never auto-execute | ENFORCED — but confirm/plan_id/expiry live **only** in the retiring tree |
| 2 | Holdings quantity includes T1 + collateral | ENFORCED, in both trees |
| 3 | Pledged shares sell directly | ENFORCED (by absence of a gate); **its test is not** |
| 4 | Every buy gets a vol-scaled GTT stop | **PARTIALLY UNENFORCED** — arithmetic in `baskfy_core`, arming desk-only. ⚠ 1.2.1 in flight |
| 5 | Product gates, inside the gateway | ENFORCED, and stronger in Baskfy than on the desk |
| 6 | All order flow through the gateway | ENFORCED for orders; **GTT exception still open** at this SHA. ⚠ 1.2.1 in flight |
| 7 | Filter-rejected / `EXCLUDED_SYMBOLS` untouchable | ENFORCED; two symbol lists agree by coincidence |

**Count: 0 of 7 fully UNENFORCED. 1 of 7 (#4) is partially unenforced, and its unenforced half is
the one 1.2.1 is porting right now. 3 of 7 (#1, #4, #6) depend on code that would disappear with
the desk tree.** The honest summary is not "Baskfy is missing the non-negotiables" — it is
**"three of the seven are enforced by files that retiring the desk deletes."**

**And one documentation defect: `baskfy_execution/__init__.py:15` points at a test file that does
not exist.** The docstring's promise ("each of the seven has a named test in
`packages/execution/tests/test_non_negotiables.py`") is false today, which is precisely the
failure mode that file's own convention exists to prevent.

---

## 3. Behaviours that exist only on the live desk

All four confirmed independently at the SHAs in §0, plus two more found in this pass.

### 3.1 Market-order protection band — absent from the entire monorepo

* **Source:** external `app/core/gateway.py:95-104` (the `if order_type == "MARKET"` branch and
  the int/float sentinel cast), the parameter at `app/core/gateway.py:57`, the config key at
  `app/config.py:108` (`MARKET_PROTECTION = float(os.getenv("MARKET_PROTECTION", "-1"))`), and
  the second site at `app/kite_client.py:191-194` (the no-limit-price branch of
  `place_cnc_order`, guarded so that path is not a trap).
* **Commit:** `be93f38`, 25 Aug 2026. Kite refuses a bare market order over the API — *"Market
  orders without market protection are not allowed via API"* — and the first live MARKET batch was
  refused in full before anything reached the exchange.
* **In Baskfy:** the string `market_protection` appears in **zero** files repo-wide. There is no
  `if order_type == "MARKET"` branch in `baskfy_execution/gateway.py` at all.
* **Latent, not live** — because Baskfy also lacks the MARKET switch (§3.2) and still sends
  LIMIT. **§3.1 and §3.2 must land together.** Porting MARKET without the band re-creates
  `be93f38`'s incident inside the merged product: every buy and every sell refused at the API
  boundary. Porting the band alone is harmless (the branch never fires), so if they must split,
  the band goes first.

### 3.2 `REBALANCE_ORDER_TYPE=MARKET`, plus the `ref_price` journal fallback

* **Source:** external `app/config.py:90-91`
  (`REBALANCE_ORDER_TYPE = os.getenv("REBALANCE_ORDER_TYPE", "MARKET").upper()`);
  applied at `app/main.py:451` (`order_type=C.REBALANCE_ORDER_TYPE`) and deliberately again at
  `app/analytics/regime_run.py:242` so the two CNC paths cannot disagree; journal fallback at
  `app/core/gateway.py:118`; `order_type` in the dry-run record at `app/core/gateway.py:90`.
* **Commit:** `0a596f4`, 25 Aug 2026. A limit priced at the instant of analysis rests until the
  close and lapses on exactly the names a momentum rebalance wants.
* **In Baskfy:** `REBALANCE_ORDER_TYPE` appears **nowhere**. Subtree `app/main.py:579` is
  hardcoded `order_type="LIMIT", price=o["ref_price"]`; subtree
  `app/analytics/regime_run.py` hardcodes `order_type="LIMIT"`. `baskfy_execution/gateway.py:139`
  journals `**params` with no `ref_price` fallback, and `gateway.py:123-124` omits `order_type`
  from the dry-run record.
* **`ref_price` must keep being passed even under MARKET.** It is ignored as a limit price but it
  is what values the order for `risk.pre_order` (`gateway.py:112`). Dropping it zeroes every
  order's value and silently disables the per-position and gross-exposure caps.

### 3.3 `fit_to_funds` — the plan is fitted to available margin, not merely warned about

* **Source:** external `app/rebalance.py:235-322` (88 lines), called from `app/main.py:320-327`
  under `if C.FIT_PLAN_TO_FUNDS and plan["funding"].get("checked")` with a re-check against
  Kite's own margin answer at `app/main.py:323-327`. Config keys `app/config.py:121`
  (`FIT_PLAN_TO_FUNDS`) and `app/config.py:126` (`FUNDING_HEADROOM_PCT`, default 0.5).
  Executable spec: external `tests/test_funding_fit.py`, **329** lines.
* **Commit:** `0a596f4`. Trims the **lowest-scoring** buys until the batch fits; sells are never
  touched; a buy trimmed below `MIN_TRADE_VALUE` is dropped whole and marked `HOLD`. Runs at
  **analyse** time only — trimming behind a confirmation would place something other than what
  was agreed.
* **In Baskfy:** `fit_to_funds`, `FIT_PLAN_TO_FUNDS` and `FUNDING_HEADROOM_PCT` return zero hits
  across `kite-momentum-rebalancer/` and `decile-blueprint/packages/`. `tests/test_funding_fit.py`
  does not exist. **What Baskfy does have** is the funding *check* — subtree
  `app/main.py:264` `_funding_check`, tested at `tests/test_execute_gateway.py:537-619` — so the
  shortfall is measured and displayed but never acted on. Without the fit, `/execute` sends
  largest-first, the money runs out partway down the queue, and **sort order picks the portfolio.**

### 3.4 Daily loss cap measured against NAV, not against invested

* **Source:** external `app/core/risk.py:39-46` (`on_pnl(pnl, basis="")`) and the NAV computation
  at `app/main.py:386-412`:
  `pnl = (book_value + cash) - previous_snapshot_nav - today's_recorded_cashflows`, with the
  baseline date named in the kill message because a missed session cannot be backfilled.
* **Commit:** `5d1ff3b`, 25 Aug 2026. The kill switch fired at 12:44 on 25 Aug for a
  Rs 24,29,636 loss that did not happen — it read the desk's own 12:04 sale of seven positions as
  a loss. The day's real change was Rs -54,965. **The dangerous half is the other sign:** a day
  that deploys cash raises book value, so the figure goes positive and a genuine loss is masked —
  blindest on the days of heaviest turnover, which are the days the switch exists for.
* **In Baskfy, both halves are missing.** `baskfy_execution/risk.py:39` is
  `def on_pnl(self, pnl: float):` with no `basis` — the **only** difference between that file and
  external HEAD's `app/core/risk.py`. Subtree `app/main.py:540-542` is verbatim the buggy version:
  ```python
  prev_invested = float(rows[-1]["invested"]) if rows else 0.0
  if prev_invested > 0:
      _risk.on_pnl(float(plan.get("book_value") or 0.0) - prev_invested)
  ```
* **Everything the fix needs already exists in the subtree:** `app/main.py:145` `_latest_nav`,
  `app/analytics/db.py:729` `snapshot_series`, `app/analytics/db.py:642` `cashflows_by_date`.
  This is a hunk swap, not a build.

### 3.5 Also live-desk-only: the CSRF fix, and its order-path consequence

* **Source:** external `app/core/websec.py:77-112` (accept `Sec-Fetch-Site: same-origin`; stop
  treating `Origin: null` as an origin to check) plus `deploy/caddy/Caddyfile`
  (`Referrer-Policy same-origin`). Commit `1cb5cb5`.
* **In Baskfy:** subtree `app/core/websec.py` is the pre-fix version and subtree
  `deploy/caddy/Caddyfile` still says `Referrer-Policy no-referrer`. **Deploying the subtree
  behind its own Caddyfile reproduces the four-day outage exactly.**
* **Why it belongs in a gateway-parity document:** `/stops/arm` is a plain `<form method=post>`
  (`kite-momentum-rebalancer/app/main.py:812`). A CSRF layer that refuses it silently is
  non-negotiable **4** not running. `/analyze` and `/execute` were unaffected only because
  `fetch()` sends a real Origin regardless of referrer policy — which is exactly what hid the bug
  for four days.

### 3.6 Found in this pass: two more, both smaller

* **`order_type` missing from the DRY_RUN journal record.** External `app/core/gateway.py:90`
  records it; `baskfy_execution/gateway.py:123-124` does not. Harmless while everything is LIMIT;
  the moment §3.2 lands, a dry run of a MARKET batch is indistinguishable in the journal from a
  LIMIT one, which defeats the purpose of a dry run.
* **The `/execute`-does-not-arm-stops shape is desk-only knowledge.** Nothing in `packages/`
  records that non-negotiable 4 is satisfied by a *separate confirmed step* rather than by the buy
  path. `tests/test_execute_gateway.py:186` and `:205` are the only place that invariant is
  written down, and they are in the retiring tree.

### 3.7 Running the other way — do not "fix" this by copying external

The subtree's **M4 risk-ceiling hardening** is **not** on the live desk. Subtree
`app/analytics/settings.py` moves six `RISK_*` keys into the read-only set and
`app/templates/settings.html` displays rather than offers them. **The live desk still lets an
operator raise its own kill-switch threshold, position cap, gross-exposure multiple and daily
order cap from a web form.** Copying external's `settings.py` over the subtree's would be a
security regression. Not a port item; recorded so it is not "resolved" the wrong way.

---

## 4. Reproducing every measurement in this document

```bash
BAS=/Users/maulikdave/Documents/projects/baskfy
EXT=/Users/maulikdave/Documents/portfolio/kite-momentum-rebalancer
BX=$BAS/decile-blueprint/packages/execution/src/baskfy_execution

# the two SHAs this document measures at
git -C "$BAS" rev-parse HEAD          # -> 7d6b7fb3aa6d23dadb30d645d28884a8b7eb6d84
git -C "$EXT" rev-parse HEAD          # -> 1cb5cb5454cae01e31afe96840ca2b7c06df7a4e

# no GTT surface in the gateway at the measured SHA (leaf 1.2.1 changes this)
git -C "$BAS" show HEAD:decile-blueprint/packages/execution/src/baskfy_execution/gateway.py \
  | grep -c gtt                       # -> 0

# byte-identity verified here, not restated (§1.2, §1.3)
git -C "$EXT" show HEAD:app/core/guards.py    > /tmp/g_ext.py && cmp /tmp/g_ext.py  "$BX/guards.py"
git -C "$EXT" show HEAD:app/core/ratelimit.py > /tmp/r_ext.py && cmp /tmp/r_ext.py  "$BX/ratelimit.py"

# risk.py: one differing method, and the diff is the whole delta (§1.4)
git -C "$EXT" show HEAD:app/core/risk.py > /tmp/rk_ext.py && diff -u /tmp/rk_ext.py "$BX/risk.py"

# the protection band exists nowhere in the monorepo (§3.1)
grep -rIl market_protection --exclude-dir={node_modules,.git,__pycache__,.venv} "$BAS" | wc -l   # -> 0

# 16 non-negotiable tests, and the path __init__.py claims does not exist (§2)
cd "$EXT/../../projects/baskfy/kite-momentum-rebalancer" && \
  python3 -m pytest tests/test_seven_non_negotiables.py --collect-only -q | tail -1   # -> 16 tests collected
ls "$BAS/decile-blueprint/packages/execution/tests/"        # no test_non_negotiables.py
grep -n test_non_negotiables "$BX/__init__.py"              # the false pointer, line 15

# the two live-desk-only sizes corrected from 1.1.2 (§0)
wc -l "$EXT/tests/test_funding_fit.py"                      # -> 329
awk 'NR>=235 && NR<=322' "$EXT/app/rebalance.py" | wc -l    # -> 88
```

---

## 5. What must be ported before the desk can be retired — in order

Ten items. Sizes are the code to write, excluding tests unless a test is named. Dependencies are
stated where they are real; anything unstated is independent and can run in parallel.

---

**P1. Market-order protection band into the merged gateway.** *Blocks P2 — hard.*

* **From:** external `app/core/gateway.py:95-104` (the branch), `app/core/gateway.py:57` (the
  `market_protection` parameter), `app/config.py:108` (`MARKET_PROTECTION`),
  `app/kite_client.py:191-194` (the second site).
* **Into:** `decile-blueprint/packages/execution/src/baskfy_execution/gateway.py` (the branch and
  the parameter — the merged gateway cannot read `app.config`, so the default arrives through the
  caller or through `ProductGates`), `kite-momentum-rebalancer/app/config.py` (the key),
  `kite-momentum-rebalancer/app/kite_client.py` (the second site).
* **Size:** ~15 lines of gateway, 1 config key, 1 env key in `.env.example`, ~40 lines of test.
* **Why first:** it is inert on its own (the branch never fires under LIMIT) and it is the
  precondition for P2. There is no ordering in which shipping it early is wrong.

---

**P2. `REBALANCE_ORDER_TYPE`, the `ref_price` journal fallback, and `order_type` in the dry-run
record.** *Depends on P1. Must not ship before it.*

* **From:** external `app/config.py:90-91`; `app/main.py:451`;
  `app/analytics/regime_run.py:242`; `app/core/gateway.py:118` (journal fallback);
  `app/core/gateway.py:90` (dry-run `order_type`).
* **Into:** `kite-momentum-rebalancer/app/config.py`; subtree `app/main.py:579` (replacing the
  hardcoded `order_type="LIMIT"`); subtree `app/analytics/regime_run.py`; and both journal lines
  in `baskfy_execution/gateway.py:123-124` and `:139`.
* **Size:** ~10 changed lines across 4 files, 1 config key. Small in code, largest in consequence.
* **Keep `price=o["ref_price"]` on the call.** Under MARKET it is not a limit; it is the order's
  value for `risk.pre_order` (`baskfy_execution/gateway.py:112`). Dropping it disables the
  per-position and gross caps silently.
* **Also fixes:** §3.6's dry-run journal gap, which only matters once this lands.

---

**P3. Daily loss cap against NAV.** *Two halves; the risk half must land first. Independent of
P1/P2.*

* **P3a — `on_pnl(pnl, basis="")`.** From external `app/core/risk.py:39-46` into
  `decile-blueprint/packages/execution/src/baskfy_execution/risk.py:39`. **This is the only
  difference between those two files** (§1.4). ~6 lines.
* **P3b — the NAV computation.** From external `app/main.py:386-412` into subtree
  `app/main.py:540-542`, replacing the `prev_invested` block verbatim. Every helper it needs is
  already in the subtree: `app/main.py:145` `_latest_nav`, `app/analytics/db.py:729`
  `snapshot_series`, `app/analytics/db.py:642` `cashflows_by_date`. ~25 lines.
* **Size:** ~31 lines total. **Highest safety value per line in this list** — it is the only item
  that today can both fire falsely *and* stay silent through a real loss.

---

**P4. `fit_to_funds`.** *Independent, but its docstring assumes P2; land P2 first or reword.*

* **From:** external `app/rebalance.py:235-322` (88 lines); call site
  `app/main.py:320-327`; config `app/config.py:121` and `:126`; spec
  `tests/test_funding_fit.py` (329 lines).
* **Into:** `decile-blueprint/packages/core/src/baskfy_core/basket.py` — it is pure arithmetic on
  the plan dict, so Law 1 puts it there, not in the desk shim — with the binding in subtree
  `app/rebalance.py` and the call at subtree `app/main.py` immediately after `_funding_check`
  (`app/main.py:264`).
* **Size:** ~90 lines of implementation + 2 config keys + ~330 lines of ported test. **Largest
  single item.**
* **Note:** `MIN_TRADE_VALUE` (subtree `app/config.py:69`) and `costs.order_cost` /
  `costs.cost_pct` are already in `baskfy_core`, so nothing else has to move with it.

---

**P5. The CSRF fix and the Caddyfile.** *Independent. Order-path relevant via non-negotiable 4.*

* **From:** external `app/core/websec.py:77-112`; external `deploy/caddy/Caddyfile`
  (`Referrer-Policy same-origin`).
* **Into:** subtree `app/core/websec.py` (a real module in the subtree, not a shim) and subtree
  `deploy/caddy/Caddyfile`. Take external `tests/test_websec.py` with it.
* **Size:** ~15 lines + a one-line Caddy change.
* **Deploying the subtree as-is behind its own Caddyfile silently refuses `/stops/arm`** — a risk
  control that does not run. This is a deploy blocker, not a nice-to-have.

---

**P6. GTT into the gateway — non-negotiable 6's documented exception.** *In flight in leaf
1.2.1; do not start it.*

* **From:** subtree `app/kite_client.py:247` `place_gtt_stop`, `:228` `delete_gtt`; the arming
  loop at subtree `app/main.py:812-870`; the coverage audit in
  `app/analytics/protection.py`.
* **Into:** `baskfy_execution`. At SHA `7d6b7fb3` the gateway has no GTT surface; 1.2.1's
  uncommitted `gtt.py` plus a `self._gtt_sent` idempotency map were in the working tree when this
  leaf finished, with the `OrderGateway` method itself not yet added.
* **Preserve the status vocabulary exactly.** `STOP_OK` in subtree `app/main.py` matches
  `kite_client`'s own constants, because a hand-written set once reported "0 armed, 17 failed"
  after 16 successful triggers — and a false failure invites a re-arm, which is how a position
  ends up with two stops.

---

**P7. Re-home `test_seven_non_negotiables.py` out of the retiring tree.** *Depends on P6 for #4's
assertions; everything else can move now.*

* **From:** `kite-momentum-rebalancer/tests/test_seven_non_negotiables.py` (16 collected tests,
  verified).
* **Into:** `decile-blueprint/packages/execution/tests/test_non_negotiables.py` — **the path
  `baskfy_execution/__init__.py:15` already claims it lives at, and which does not exist.**
* **Size:** ~210 lines moved, plus rewrites for the tests that grep desk source (#1 at line 27,
  #2 at line 63, #3 at line 76, #6c at line 173 all read `app/` files).
* **Do not move it verbatim.** Fix these while moving:
  * **#3 (line 76) is `assert "pledged_qty" in src`** — asserts an identifier exists. Replace
    with a behavioural assertion that a pledged holding produces a SELL with no unpledge gate.
  * **#4 (line 88) tests only `stop_from_vol`** despite being named
    `..._every_buy_is_stopped_and_...`. Add the "every buy, same session" half once P6 gives it
    something in `packages/` to assert against.
  * **#6b (line 156) walks `pathlib.Path("app")` only.** Widen it to
    `decile-blueprint/packages` and `decile-blueprint/services`, or the AST check stops covering
    the code that will actually place the orders.

---

**P8. Re-home non-negotiable 1's gates and #6c's `client_id`.** *Blocks retirement outright.
Depends on wherever `/execute` lands.*

* **From:** subtree `app/main.py:519` (`confirm != "true"`), `:521` (`PLANS.get(plan_id)`),
  `:524` (30-minute expiry), `:581` (`client_id=f"{plan_id}:{o['symbol']}"`), and the same four
  for stops at `:816`, `:818`, `:821`, `:848`.
* **Into:** no owner exists in `packages/` today. The gateway consumes `client_id`
  (`baskfy_execution/gateway.py:117-119`) but does not construct it, and nothing in `packages/`
  knows about `confirm`, `plan_id` or the 30-minute window.
* **Size:** small in code, large in decision — it needs a home first. **This is the item that
  makes "retire the desk" a design question rather than a port.** Non-negotiable 1 is the desk's
  first rule and it currently lives in one FastAPI file in the tree being deleted.

---

**P9. Re-home the stop-coverage audit.** *Depends on P6.*

* **From:** subtree `app/analytics/protection.py` — `build_stop_plan` and the
  MISSING/PARTIAL/TOO_FAR/TOO_CLOSE/ORPHAN/EXCESS finding vocabulary; the `/stops` review page
  at subtree `app/main.py:837`.
* **Into:** the pure half (`build_stop_plan`, the findings) belongs in `baskfy_core` under Law 1;
  the arming half goes with P6.
* **Size:** ~200 lines. **Keep the EXCESS-outranks-everything ordering:** an uncovered position
  loses money if the market falls; an over-covered one sells shares you do not own, which is short
  delivery and an auction penalty. On 18 Aug 2026 the book carried 10,383 GTT shares against 9,478
  held.

---

**P10. Make non-negotiable 7's two symbol lists agree by construction.** *Independent. Smallest
item; do it whenever.*

* **Where:** `baskfy_execution/guards.py:11` hardcodes
  `UNTOUCHABLE_SYMBOLS = frozenset({"SGBDE31III"})`; subtree `app/config.py:43` holds a separate
  `EXCLUDED_SYMBOLS = {"SGBDE31III"}`. They agree today only by coincidence.
* **Do not merge them.** `services/api/src/baskfy_api/kite_basket.py:22-25` explains why:
  `EXCLUDED_SYMBOLS` is one person's long-term holding and must not leak into other users'
  baskets. The right fix is a **test** that fails if the desk's set ever grows beyond what
  `guards.assert_tradeable` already refuses — not a shared constant.
* **Size:** ~20 lines of test, no production change.

---

### Dependency graph, stated once

```
P1 (band) ──must precede──> P2 (MARKET + ref_price)
P3a (on_pnl basis) ──must precede──> P3b (NAV computation)
P6 (GTT, leaf 1.2.1) ──must precede──> P7's #4 assertion, and P9
P8 needs a home for /execute before it can start
P4, P5, P10 are independent of everything above
```

**Recommended order:** P1 → P2 → P3 → P5 → P4 → (P6 lands from 1.2.1) → P9 → P7 → P10, with P8
resolved as a design decision in parallel because it gates retirement rather than any single port.

**P1+P2+P3+P5 is roughly 70 lines of production code and closes every incident-driven gap from
25 Aug 2026.** P4 is the largest build. P6–P9 are the actual retirement work: they are not about
behaviour that is missing, they are about behaviour that is correct but living in the wrong tree.
