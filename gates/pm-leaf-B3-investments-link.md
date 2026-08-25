# Gates: B3 — an investment can live inside a portfolio

Scope: the join the product copy already promises. Link and unlink a cb_investment to a portfolio; the portfolio view counts it.

- [x] G1: an investment can be linked to a portfolio the caller owns
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_cb_investment_portfolio.py -m db -k "link" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .........                                                                [100%] | 9 passed, 14 deselected in 2.69s

- [x] G2: linking to a portfolio owned by another user is refused as NOT_FOUND
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_cb_investment_portfolio.py -m db -k "tenant or other_user or not_found" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ...                                                                      [100%] | 3 passed, 20 deselected in 2.48s

- [x] G3: unlinking works and leaves the investment intact — no cascade delete of the book
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_cb_investment_portfolio.py -m db -k "unlink" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ....                                                                     [100%] | 4 passed, 19 deselected in 2.23s

- [x] G4: an unlinked investment remains readable — portfolio_id stays optional, the investment is not orphaned out of the API
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_cb_investment_portfolio.py -m db -k "optional or unassigned" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ..                                                                       [100%] | 2 passed, 21 deselected in 2.19s

- [x] G5: the investment's broker_account_id and the portfolio's broker attribution are reconciled — a mismatch is reported, not silently merged
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_cb_investment_portfolio.py -m db -k "broker or mismatch or reconcile" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .....                                                                    [100%] | 5 passed, 18 deselected in 2.32s

- [x] G6: deleting a portfolio does not delete its investments (the book is evidence)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_cb_investment_portfolio.py -m db -k "delete" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ...                                                                      [100%] | 3 passed, 20 deselected in 2.25s

- [x] G7: no order path — linking records a book entry, it never places anything
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && echo "ORDER_HITS=$(grep -cE 'place_order|OrderGateway|/execute' services/api/src/baskfy_api/routers/curated_investments.py 2>/dev/null || echo 0)"
  EXPECT: ORDER_HITS=0
  EVIDENCE: ORDER_HITS=0 | 0

- [x] G8: the pre-existing curated investment suite still passes
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && { uv run pytest services/api/tests/test_curated_investments.py --tb=line 2>&1 | tail -2; uv run pytest services/api/tests -m db -k "investment" --tb=line 2>&1 | tail -2; }; cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_curated_investments.py >/dev/null 2>&1 && uv run pytest services/api/tests -m db -k "investment" >/dev/null 2>&1 && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: 20 passed, 1171 deselected in 3.63s | GATE_OK

- [x] G9: lint + mypy strict clean
  CHECK: { cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run ruff check services/api/src/baskfy_api/routers/curated_investments.py services/api/tests/test_cb_investment_portfolio.py 2>&1 | tail -2; uv run mypy services/api/src/baskfy_api/routers/curated_investments.py services/api/tests/test_cb_investment_portfolio.py 2>&1 | tail -2; }; cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run ruff check services/api/src/baskfy_api/routers/curated_investments.py services/api/tests/test_cb_investment_portfolio.py >/dev/null 2>&1 && uv run ruff format --check services/api/src/baskfy_api/routers/curated_investments.py services/api/tests/test_cb_investment_portfolio.py >/dev/null 2>&1 && uv run mypy services/api/src/baskfy_api/routers/curated_investments.py services/api/tests/test_cb_investment_portfolio.py >/dev/null 2>&1 && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: Success: no issues found in 2 source files | GATE_OK
