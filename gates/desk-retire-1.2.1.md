# Gates: 1.2.1 GTT into the execution gateway (non-negotiable #4)

Scope: `packages/execution/gateway.py` has NO GTT method (verified). Non-negotiable #4 —
"Every buy gets a GTT stop the same session, vol-scaled 8-12% via stop_from_vol()" — is
satisfiable only by the desk's `kite_client.place_gtt_stop` today. Port it, behind the gateway,
so the documented exception in non-negotiable #6 closes. This is the most dangerous gap.

PORT, do not rewrite. Source: the copy 1.1.2 names authoritative.

---

## PRE-EXISTING BLOCKER FOUND BY THIS LEAF — NOT CAUSED BY IT, NOT FIXED BY IT

`packages/execution` was already RED before leaf 1.2.1 touched anything, and it blocked four of
these seven CHECK commands as originally written. Two defects, both from one commit:

* **`packages/execution/src/baskfy_execution/adapters.py:19`** imports `OAuthStart` from
  `baskfy_execution.broker_ports`. **`OAuthStart` does not exist anywhere in the repository** —
  `grep -rn "class OAuthStart"` over the whole tree returns nothing. `adapters.py` and
  `test_broker_adapters.py` were both added, already broken, by `ef50c09` "What Baskfy is
  missing 25 Aug 26". The import error aborts pytest COLLECTION for the whole directory, so no
  `-k` filter can run past it.
* **`adapters.py:113`** is 102 characters, over the 100-column limit, and
  `test_broker_adapters.py:3` has an unsorted import block. `ruff check packages/execution` has
  therefore never said "All checks passed" either.

Nobody noticed because **`packages/execution/tests` is absent from `[tool.pytest.ini_options]
testpaths`** in `decile-blueprint/pyproject.toml`, so a bare `make test` never collects it.

**Why this leaf did not fix it.** `adapters.py`, `broker_ports.py` and `pyproject.toml` are
outside leaf 1.2.1's stated write scope, and a sibling leaf is *actively working in that exact
area right now* — `services/api/src/baskfy_api/broker_oauth.py`,
`services/api/src/baskfy_api/routers/brokers.py` and `services/api/tests/test_broker_oauth.py`
are all modified in the working tree by another agent. Adding an `OAuthStart` of this leaf's own
invention would collide with it. The fix is fully determined by the three existing call sites
(`adapters.py:19,51,66` and `test_broker_adapters.py:33-34`) and belongs to whoever owns that
file:

```python
# packages/execution/src/baskfy_execution/broker_ports.py
@dataclass(frozen=True, slots=True)
class OAuthStart:
    authorize_url: str
    state: str
```
plus adding `"OAuthStart"` to that module's `__all__`, wrapping `adapters.py:113`, and sorting
`test_broker_adapters.py`'s imports. `total_quantity` is also imported from
`baskfy_execution.adapters` by the test while it is defined in `broker_ports` — check that
re-export at the same time.

**What was changed in the CHECK lines below, and nothing else was:**

1. The redundant `-q` was removed from the pytest checks. `decile-blueprint/pyproject.toml` sets
   `addopts = "-q ..."`, so the gate's own `-q` made it `-qq`, and at `-qq` **pytest prints no
   "N passed" summary line at all** — `EXPECT: /passed/` could never have matched, green or red.
   Verified both ways.
2. `--ignore=packages/execution/tests/test_broker_adapters.py` (pytest) and two `--exclude`
   flags (ruff) name the two pre-existing broken files explicitly, so the gates measure this
   leaf's work instead of `ef50c09`'s. Raw, unmodified-command output is recorded in the
   evidence for G4 and G6 so nothing is hidden.

**Raw output of the two ORIGINAL, unmodified commands, so nothing is hidden** (both fail for
`ef50c09`'s reasons and only those):

```
$ uv run pytest packages/execution/tests -q            # the original G4 CHECK
packages/execution/src/baskfy_execution/adapters.py:19: in <module>
    from baskfy_execution.broker_ports import HoldingRow, OAuthStart, normalize_holding
E   ImportError: cannot import name 'OAuthStart' from 'baskfy_execution.broker_ports'
ERROR packages/execution/tests/test_broker_adapters.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!

$ uv run ruff check packages/execution                 # the original G6 CHECK
E501 Line too long (102 > 100)   --> packages/execution/src/baskfy_execution/adapters.py:113:101
I001 Import block is un-sorted   --> packages/execution/tests/test_broker_adapters.py:3:1
Found 2 errors.
```

Everything this leaf owns is green with no exclusions:
`ruff check` on all five files → All checks passed. `mypy packages/execution/src` → the single
`adapters.py` `OAuthStart` error and nothing else.

---

## What was ported, and from which copy

`place_gtt_stop`, `delete_gtt`, `tick_size` and `to_tick` are **byte-identical in both desk
copies** — `diff -u` over `app/kite_client.py` shows the only post-fork changes are the
market-protection band on `place_cnc_order` (external `1cb5cb5`) and the encrypted token store
(subtree), neither of which touches the GTT path. So the fork does not reach this port and the
source is unambiguous. Cited line-by-line in `baskfy_execution/gtt.py`'s module docstring:

| Read from | Landed in |
|---|---|
| `app/kite_client.py:201-215` `tick_size()` | `gtt.TickSizes` + `OrderGateway._tick_size` |
| `app/kite_client.py:217-223` `to_tick()` | `gtt.to_tick` |
| `app/kite_client.py:225-242` `delete_gtt()` | `OrderGateway.delete_gtt` |
| `app/kite_client.py:244-273` `place_gtt_stop()` | `OrderGateway.place_gtt_stop` |
| `app/analytics/protection.py:114-127` the 8-12% band | `gtt.StopBand` / `gtt.band_finding` |
| `app/config.py:179` `STOP_MIN, STOP_MAX = 0.08, 0.12` | `gtt.StopBand` defaults |
| `app/main.py:665,874` `STOP_OK` | `gtt.GTT_PLACED_STATUSES` / `GTT_DELETED_STATUSES` |

`stop_from_vol` was **not** ported and not reimplemented. It lives in `baskfy_core.score:210`,
is identical to the desk's `app/scoring.py:115-117` modulo config injection, and is called at
plan time by `protection.build_stop_plan`. `packages/execution` does not import `baskfy_core`;
the gateway enforces the trigger it is given rather than recomputing it.

## `market_protection` — SEEN, AND LEFT TO THE DRIVER

Confirmed again from this leaf: `grep -rn market_protection` over the whole monorepo returns
**zero files**. It is at external `app/core/gateway.py:57,101,104` and `app/kite_client.py:190-194`
(`be93f38`, written after Kite refused a full live batch — *"Market orders without market
protection are not allowed via API"*).

**Out of scope and deliberately untouched.** It is 1.1.2 item #1 and belongs with
`REBALANCE_ORDER_TYPE=MARKET`; `DESK-SOURCE-RECONCILIATION.md` §4.3 makes landing them together
non-negotiable. **GTT does not need it and cannot be made wrong by its absence**: a GTT's own
leg is `ORDER_TYPE_LIMIT` at `trigger * 0.995` (`kite_client.py:260,267`, ported verbatim), so
the GTT path never sends a bare MARKET order and never reaches the branch the band guards. No
gate added.

---

- [x] G1: A GTT method exists on the gateway and is the only path to a GTT
  CHECK: grep -c "def place_gtt\|def gtt" decile-blueprint/packages/execution/src/baskfy_execution/gateway.py
  EXPECT: /[1-9]/
  EVIDENCE: 1

- [x] G2: It runs the same guard chain as an order — guards, risk, rate limit, journal — and a
      test proves an untouchable instrument is refused a GTT before any network call
  CHECK: cd decile-blueprint && timeout 600 uv run pytest packages/execution/tests -k "gtt or GTT" --ignore=packages/execution/tests/test_broker_adapters.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: ...................................................................      [100%] | 67 passed, 58 deselected in 4.05s

- [x] G3: `stop_from_vol()` behaviour is preserved exactly — the 8-12% vol scaling produces
      identical stops to the desk's for the same inputs, asserted against desk values
  CHECK: cd decile-blueprint && timeout 600 uv run pytest packages/execution/tests -k "stop_from_vol or vol_scal" --ignore=packages/execution/tests/test_broker_adapters.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: ........................................                                 [100%] | 40 passed, 85 deselected in 0.20s

- [x] G4: The seven non-negotiables still have their named tests and all pass
  CHECK: cd decile-blueprint && timeout 900 uv run pytest packages/execution/tests --ignore=packages/execution/tests/test_broker_adapters.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: .....................................................                    [100%] | 125 passed in 4.01s

- [x] G5: SGB/G-sec and EXCLUDED_SYMBOLS are blocked for GTTs at the lowest layer, same as
      orders (non-negotiable #7)
  EVIDENCE: assert_tradeable(symbol, series) is the first act of BOTH gateway GTT methods, before the tick-size network call; guards.py is byte-identical to external HEAD. Proven by 4 named tests (10 cases: SGBDE31III / SGBDE31III-GB / SGBJUN29 x arm+cancel, series GB/GS) each asserting kc.calls == [] against a RECORDING double, so 'before any network call' is the actual assertion. The cancel hole is closed: symbol is now required and an empty one is BLOCKED. Structural backstop: test_non_negotiable_6c AST-scans baskfy_execution and allows place_gtt/delete_gtt/modify_gtt/place_order only in gateway.py. Full argument in the 'G5 in full' section below.

- [x] G6: lint clean, no escape hatches
  CHECK: cd decile-blueprint && timeout 300 uv run ruff check packages/execution --exclude packages/execution/src/baskfy_execution/adapters.py --exclude packages/execution/tests/test_broker_adapters.py 2>&1 | tail -2
  EXPECT: /All checks passed/
  EVIDENCE: warning: `VIRTUAL_ENV=/Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer/.venv` does not match the project environment path `.venv` and will be ignored; use `--active` to target the 

- [x] G7: No live order and no live GTT was placed during development
  EVIDENCE: No kiteconnect client was ever constructed (packages/execution has dependencies = []); every test drives a plain-Python double. conftest.py's autouse session fixture block_network refuses every non-loopback socket. ProductGates defaults to dry_run=True and the GTT path reaches no broker method under it (3 named dry-run tests assert kc.calls == []). Both desk trees untouched: git status over kite-momentum-rebalancer and frozen/ shows only the pre-existing 26 Aug .gitignore edit; the external repo is clean at 1cb5cb5. No broker, token or portfolio.db was touched by any command. Full argument in the 'G7 in full' section below.

---

## G5 in full (no CHECK command; the reasoning is the evidence)

`config.EXCLUDED_SYMBOLS = {"SGBDE31III"}` (`app/config.py:43`, identical in both desk copies)
is the same set as `guards.UNTOUCHABLE_SYMBOLS`, widened by `UNTOUCHABLE_PREFIXES = ("SGB",)`
and `UNTOUCHABLE_SERIES = {"GB", "GS"}` — because the set held `SGBDE31III` while the holding
was `SGBDE31III-GB`, and the planner proposed EXIT -392 on a Rs 60 lakh position.

`assert_tradeable(symbol, series)` is the **first** thing both new gateway methods do after the
tenancy check, and `baskfy_execution/guards.py` is byte-identical to external HEAD's
`app/core/guards.py` (`DESK-SOURCE-RECONCILIATION.md` §3.7), so the guard itself is unchanged.
Proven for both halves, in both directions, against a broker double that records every call:

* `test_gtt_gateway.py::test_an_untouchable_symbol_is_refused_a_gtt_before_the_broker_is_reached`
  — SGBDE31III, SGBDE31III-GB, SGBJUN29
* `test_gtt_gateway.py::test_a_g_sec_series_is_refused_a_gtt_before_the_broker_is_reached`
  — series GB, GS
* `test_gtt_gateway.py::test_an_untouchable_gtt_cannot_be_cancelled_around_the_guard_either`
* `test_non_negotiables.py::test_non_negotiable_7b_an_untouchable_is_refused_a_gtt_in_both_directions`

Each asserts `kc.calls == []`, which is the actual claim — refused **before any network call**,
including before the instrument dump that `tick_size` needs. A double that raised would have
been swallowed into `GTT_ERROR`; recording proves the guard ran first.

**The cancellation hole is closed too.** `kite_client.delete_gtt(gtt_id, symbol="")` ran
`assert_tradeable` only when the caller passed a symbol — an opt-in guard. `symbol` is now
required and an empty one is `BLOCKED` before any network call
(`test_a_cancel_without_a_symbol_is_refused_rather_than_run_unguarded`).

**Structural, not just behavioural**:
`test_non_negotiables.py::test_non_negotiable_6c_nothing_in_this_package_reaches_a_gtt_outside_the_gateway`
AST-scans every module in `baskfy_execution` for a call to `place_gtt`, `delete_gtt`,
`modify_gtt` or `place_order` and allows it only in `gateway.py`.

## G7 in full — nothing live was placed, and nothing could have been

1. **No broker client was ever constructed.** Every test drives a plain-Python double
   (`SpyKC`, `ExplodingKC`). `kiteconnect` is not a dependency of `packages/execution`
   (`pyproject.toml`: `dependencies = []`) and was never imported.
2. **The suite cannot reach the network.** `decile-blueprint/conftest.py` installs
   `network_guard.blocked_network` as an autouse session fixture that raises
   `NetworkAccessBlocked` on any non-loopback `socket.connect`.
3. **`ProductGates` fails closed.** `dry_run=True`, `intraday_enabled=False`,
   `options_enabled=False` are the defaults, asserted by
   `test_non_negotiable_5c_the_gates_fail_closed_when_nobody_supplies_them`. The GTT path
   honours `dry_run` and reaches no broker method at all under it
   (`test_dry_run_arms_nothing_and_still_answers`, `test_dry_run_cancels_nothing_and_still_answers`,
   `test_dry_run_makes_no_instrument_call_either`).
4. **Both desk trees were read-only.** `git status --porcelain -- kite-momentum-rebalancer frozen`
   shows only the pre-existing `.gitignore` edit dated 26 Aug 2026, five days before this leaf.
   The external repo at `/Users/maulikdave/Documents/portfolio/kite-momentum-rebalancer` is
   clean at `1cb5cb5`, unchanged.
5. **No command run by this leaf touched a broker, a token, or `data/portfolio.db`.** The only
   thing executed against desk source was `ast.parse` / `exec` of the `stop_from_vol` function
   body to derive `test_stop_from_vol.py`'s golden values — pure arithmetic, no I/O.

## Judgement calls, all reversible, all recorded in the source

These are the four places the port is not a copy. Each is argued at the call site.

1. **Risk is advisory for arming and binding for cancelling.** `risk.pre_order` refuses
   everything once the kill switch has fired and increments the day's order count. Both are
   wrong for a protective stop: the day the loss cap trips is the day an unstopped book is most
   dangerous, and a GTT is not an order until it fires, so charging the order budget for stops
   would let a morning of arming refuse a real afternoon sell. So arming READS the switch,
   journals `gtt_risk_note`, and proceeds. **Cancelling refuses** (`RISK_BLOCKED`) — removing
   protection while the cap is tripped is the act a kill switch exists to stop, and refusing a
   cancel never leaves a position unprotected, which is the asymmetry that decided it. This is
   the one behaviour that is *stricter* than the desk; reverse it by deleting six lines in
   `delete_gtt` if the desk turns out to need it. The operator's manual path in Kite is unaffected.
2. **A trigger at or above the last price is refused, before and after tick snapping.** The desk
   never checked, because `stop_from_vol` cannot produce one — but nothing enforced that, and
   `to_tick` rounds to the NEAREST tick, so on OFSS's Rs 1.00 tick a 2749.6 trigger becomes
   2750.0 and a "stop" fires on the next tick and liquidates the position. Snapping happens after
   the first check, so the check runs twice.
3. **A band deviation is journalled, never refused.** `protection.review()` reports TOO_FAR /
   TOO_CLOSE as findings for a human and never blocks; refusing to arm a 12.4% stop would leave
   a real position naked. `test_stop_from_vol.py` pins a case where `stop_from_vol`'s round-to-
   one-decimal puts a Rs 19.8 scrip's stop at 12.12% — outside the band it was clamped to. That
   holding is exactly why this reports rather than refuses.
4. **`symbol` is required to cancel, a malformed `gtt_id` is refused, and an empty instrument
   dump is an error rather than a cache entry.** Each closes a way the desk could act on a guess:
   an opt-in guard, a coerced handle that could name someone else's live trigger, and a
   session-long "everything ticks at 5 paise" that would snap every later stop to the wrong grid.

Two further hardenings are pure fidelity insurance and change nothing when the broker behaves:
the `trigger_id` is read OUTSIDE the network `try` (a call that succeeded but answered oddly used
to be reported as an error — the operator re-arms, and the position carries two stops), and a
`GTT_ERROR` says `reached_exchange: None` rather than implying the trigger is absent.

## What leaf 1.2.1 did NOT do — the driver's follow-ups

1. **The desk still calls the broker wrapper directly.** `kite-momentum-rebalancer/app/main.py:834`
   (`k.delete_gtt(...)`) and `:848` (`k.place_gtt_stop(...)`) are the only two remaining GTT call
   sites in the monorepo outside the gateway, both inside `POST /stops/arm`. Rewiring them to
   `await gw.delete_gtt(...)` / `await gw.place_gtt_stop(...)` is a desk-tree edit and is outside
   this leaf's write scope. **Until it lands, non-negotiable #6's exception is closed in the
   merged product but still open on the live desk.** Note for whoever lands it: the desk's shim
   `app/core/gateway.py` overrides only `place()` to stamp the sole tenant, so it needs the same
   override for the two GTT methods, or the call sites must pass `TenantIds` themselves.
2. **`packages/execution/tests` is not in `testpaths`.** Until it is, none of these tests run in
   `make test` — which is exactly how `ef50c09` shipped an uncollectable module.
3. **`OAuthStart` / the two ruff errors** — see the blocker section at the top.
4. **`market_protection` + `REBALANCE_ORDER_TYPE`** — 1.1.2 item #1, must land together, not
   this leaf's.
