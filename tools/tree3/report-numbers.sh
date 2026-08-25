#!/usr/bin/env bash
# Tree 3 / G15 — every number the final report states, re-measured at report time.
#
# The failure this exists to prevent is a report whose substance is right and whose figures are
# recalled from an hour ago. Nothing here is typed by hand; if a number appears in the report it
# comes out of this script.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
PSQL=(docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc)
PUB=$("${PSQL[@]}" "select max(trade_date) from pipeline_run where data_version is not null")
BARS=$("${PSQL[@]}" "select max(date) from ohlcv_daily")

echo "== dates =="
echo "  published (what the API serves) : $PUB"
echo "  newest bars                     : $BARS"

echo
echo "== fundamental_daily =="
"${PSQL[@]}" "
  select date || ' : ' || count(*) || ' rows, ' || count(marketcap_cr) || ' mcap, '
       || count(pe) || ' pe, ' || count(shares_outstanding) || ' shares'
  from fundamental_daily group by date order by date" | sed 's/^/  /'
echo "  total rows: $("${PSQL[@]}" "select count(*) from fundamental_daily")"

echo
echo "== coverage against each date's traded universe =="
for d in "$PUB" "$BARS"; do
  "${PSQL[@]}" "
    select '  $d : ' || count(f.instrument_id) || '/' || count(*) || ' rows ('
         || round(100.0 * count(f.instrument_id) / nullif(count(*),0), 1) || '%), '
         || count(f.marketcap_cr) || ' with mcap, ' || count(f.pe) || ' with pe'
    from ohlcv_daily o
    left join fundamental_daily f
      on f.instrument_id = o.instrument_id and f.date = o.date
    where o.date = '$d'"
done

echo
echo "== factor_daily (what every surface reads) =="
"${PSQL[@]}" "
  select '  ' || date || ' : ' || count(*) || ' rows, ' || count(marketcap_cr)
       || ' mcap, ' || count(pe) || ' pe'
  from factor_daily where date in ('$PUB','$BARS') group by date order by date"

echo
echo "== archived raw files (docs/09 evidence) =="
for d in "$PUB" "$BARS"; do
  n=$(find decile-blueprint/.archive/nse/equity-fundamentals -name "$d.json" 2>/dev/null | wc -l | tr -d ' ')
  m=$(find decile-blueprint/.archive/nse/equity-meta -name "$d.json" 2>/dev/null | wc -l | tr -d ' ')
  echo "  $d : $n quote files, $m series-lookup files"
done

echo
echo "== code touched =="
# Deliberately NOT a `git diff --stat HEAD`: T9.1's parser, task and orchestrator wiring are
# themselves uncommitted working-tree changes (`git show HEAD:...nse.py` has no
# `equity_fundamentals` at all), so a diff against HEAD would credit this tree with work it did
# not do. These are the files this tree created or edited, named explicitly.
for f in \
  decile-blueprint/packages/providers/src/baskfy_providers/nse.py \
  decile-blueprint/packages/providers/src/baskfy_providers/ports.py \
  decile-blueprint/packages/providers/src/baskfy_providers/composite.py \
  decile-blueprint/packages/providers/src/baskfy_providers/fixtures.py \
  decile-blueprint/packages/providers/tests/test_nse.py \
  decile-blueprint/services/worker/src/baskfy_worker/db.py \
  decile-blueprint/services/worker/src/baskfy_worker/orchestrator.py \
  decile-blueprint/services/worker/src/baskfy_worker/factors_cli.py \
  decile-blueprint/services/worker/src/baskfy_worker/tasks/fundamentals.py \
  decile-blueprint/services/worker/src/baskfy_worker/tasks/factors.py \
  decile-blueprint/services/worker/tests/test_fundamentals.py \
  decile-blueprint/services/worker/src/baskfy_worker/fundamentals_cli.py \
  decile-blueprint/services/worker/tests/test_fundamentals_cli.py \
  decile-blueprint/tools/tree3_probe_parse.py ; do
  [ -f "$f" ] && printf "  %-72s %s lines\n" "${f#decile-blueprint/}" "$(wc -l < "$f" | tr -d ' ')"
done
echo "  gate scripts: $(ls tools/tree3/*.sh 2>/dev/null | wc -l | tr -d ' ')"

echo "== test counts =="
(cd decile-blueprint && \
  BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" \
  uv run pytest services/worker/tests/test_fundamentals.py \
     services/worker/tests/test_fundamentals_cli.py \
     packages/providers/tests/test_nse.py -q -p no:randomly 2>&1 | tail -2) | sed 's/^/  /'

echo
echo "== gate ledger =="
node ~/.claude/skills/unlazy/scripts/gate-check.mjs --status \
  "$(pwd)/gates/tree3-fundamentals-fill.md" 2>/dev/null | tail -5 | sed 's/^/  /'
echo "REPORT NUMBERS COMPLETE"
