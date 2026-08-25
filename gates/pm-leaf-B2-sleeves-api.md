# Gates: B2 — sleeves router learns baskets and units

Scope: kind='basket' sleeves become creatable and allocatable; the allocation response carries unit counts, not only rupees and weights.

> ENVIRONMENT NOTE (measured, 2026-08-25). `baskfy_test` is shared, and every `db`-marked module
> resets it (`screener_helpers._reset_and_seed` drops `public`, drops the timescaledb extension,
> re-migrates and re-seeds). While leaves B1 and B3 were running their own `db` suites, a run of
> this leaf's suite failed 7 times in 12 with `relation "app_user" does not exist` and
> `alembic upgrade head returned non-zero`, in varying counts — a cross-leaf race, not a defect in
> the code under test. Every EVIDENCE line below is from a run that completed inside a quiet
> window; the attempt count is recorded where it took more than one. The same suite is
> deterministic on an uncontended database (all 35 passed, 8 runs out of 8).

> GATE DEFECT FOUND AND FIXED, in this file's own instruments (2026-08-25). Every pytest gate
> here was written as `EXPECT: passed` against a piped `tail -3`, and `expectMatches` is a
> substring test — so `15 passed, 24 errors` satisfied `passed`. It was not hypothetical: a
> `gate-check.mjs` run on a scratch copy of this file recorded G9 as **PASS** on the evidence
> `ERROR ...TestTheRegimeCapOverHttp... | 15 passed, 24 errors in 2.09s`. This is the same
> false-pass class the driver already found on the compound `ruff && mypy` gates, and the same
> remedy is applied: each pytest CHECK now runs once, prints its own tail for a human, and then
> emits a decisive `GATE_OK` / `GATE_FAILED` from pytest's exit status, with `EXPECT` matching
> the token. Proven capable of failing: the identical command with `-k "nosuchtestname"` prints
> `31 deselected in 0.08s` and `GATE_FAILED`. No gate was weakened — eight gates that could not
> fail now can.

- [x] G1: a basket sleeve can be created with basket_id, and kind='basket' without basket_id is refused
  CHECK: out=$(cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_sleeves_basket.py -m db -k "create or pairing" --tb=line 2>&1); rc=$?; echo "$out" | tail -3; [ $rc -eq 0 ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: 9 passed, 26 deselected in 3.67s | GATE_OK

- [x] G2: kind='screen' with a basket_id, and kind='manual' with either id, are all refused
  CHECK: out=$(cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_sleeves_basket.py -m db -k "mismatch or exclusive" --tb=line 2>&1); rc=$?; echo "$out" | tail -3; [ $rc -eq 0 ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: 6 passed, 29 deselected in 3.10s | GATE_OK

- [x] G3: a portfolio holding a momentum basket sleeve beside a manual core sleeve is expressible and returns both
  CHECK: out=$(cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_sleeves_basket.py -m db -k "beside or mixed or coexist" --tb=line 2>&1); rc=$?; echo "$out" | tail -3; [ $rc -eq 0 ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: 1 passed, 34 deselected in 2.42s | GATE_OK

- [x] G4: GET /portfolios/{id}/allocation returns unit counts per name, sourced from A3
  CHECK: out=$(cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_sleeves_basket.py -m db -k "units" --tb=line 2>&1); rc=$?; echo "$out" | tail -3; [ $rc -eq 0 ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: 5 passed, 30 deselected in 3.32s | GATE_OK

- [x] G5: allocation without available prices degrades honestly — units are null with a stated reason, never silently zero
  CHECK: out=$(cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_sleeves_basket.py -m db -k "unpriced or no_price or honest" --tb=line 2>&1); rc=$?; echo "$out" | tail -3; [ $rc -eq 0 ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: 4 passed, 31 deselected in 3.10s | GATE_OK

- [x] G6: money in the allocation response is rounded at write time and matches storage precision (house rule 8)
  CHECK: out=$(cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_sleeves_basket.py -m db -k "precision or round" --tb=line 2>&1); rc=$?; echo "$out" | tail -3; [ $rc -eq 0 ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: 2 passed, 33 deselected in 2.70s | GATE_OK

- [x] G7: tenant isolation — a basket sleeve cannot reference another user's private basket
  CHECK: out=$(cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_sleeves_basket.py -m db -k "tenant or isolation" --tb=line 2>&1); rc=$?; echo "$out" | tail -3; [ $rc -eq 0 ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: 3 passed, 32 deselected in 2.58s | GATE_OK

- [x] G8: no order path in this router
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && echo "ORDER_HITS=$(grep -cE 'place_order|OrderGateway|/execute|confirm=True' services/api/src/baskfy_api/routers/sleeves.py 2>/dev/null || echo 0)"
  EXPECT: ORDER_HITS=0
  EVIDENCE: ORDER_HITS=0 | 0

- [x] G9: the pre-existing sleeve suite still passes
  CHECK: out=$(cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_sleeves_are_not_orders.py services/api/tests/test_api_sleeves_basket.py -k "sleeve" --tb=line 2>&1); rc=$?; echo "$out" | tail -3; [ $rc -eq 0 ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: 43 passed in 7.94s | GATE_OK

- [x] G10: lint + mypy strict clean
  CHECK: { cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run ruff check services/api/src/baskfy_api/routers/sleeves.py services/api/tests/test_api_sleeves_basket.py 2>&1 | tail -2; uv run mypy services/api/src/baskfy_api/routers/sleeves.py 2>&1 | tail -2; }; cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run ruff check services/api/src/baskfy_api/routers/sleeves.py services/api/tests/test_api_sleeves_basket.py >/dev/null 2>&1 && uv run mypy services/api/src/baskfy_api/routers/sleeves.py >/dev/null 2>&1 && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: Success: no issues found in 1 source file | GATE_OK
