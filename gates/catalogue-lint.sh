#!/usr/bin/env bash
# Every file this tree wrote or edited: ruff, ruff format, mypy, and the escape-hatch scanner.
set -uo pipefail
cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint || exit 1
FILES="services/api/src/baskfy_api/curated_catalogue.py services/api/src/baskfy_api/seed.py services/api/src/baskfy_api/curated_metrics_service.py"
FAIL=0
uv run ruff check $FILES        || { echo "RUFF_FAIL"; FAIL=1; }
uv run ruff format --check $FILES || { echo "FORMAT_FAIL"; FAIL=1; }
uv run mypy $FILES 2>&1 | tail -2
uv run mypy $FILES >/dev/null 2>&1 || { echo "MYPY_FAIL"; FAIL=1; }
uv run pytest packages/core/tests/test_no_escape_hatches.py -q -p no:randomly >/dev/null 2>&1 || { echo "ESCAPE_HATCH_FAIL"; FAIL=1; }
[ "$FAIL" = "0" ] && echo "LINT_OK" || echo "LINT_NOT_OK"
