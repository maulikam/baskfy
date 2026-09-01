#!/usr/bin/env bash
# Node 1.1.1 / G10 — this node's footprint is exactly what it claims.
#
# Deliberately NOT "the working tree is clean": the web app carries 87 uncommitted files from an
# earlier task in the same session, and a gate that failed on those would be reporting on someone
# else's work. This names the files node 1.1.1 owns and checks the two trees it must never touch.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
fail=0

echo "files this node owns:"
for f in \
  decile-blueprint/packages/core/src/baskfy_core/allocation_ledger.py \
  decile-blueprint/packages/core/tests/test_allocation_ledger.py \
  tools/portfolio/house-rules.sh \
  tools/portfolio/footprint.sh \
  gates/portfolio-spine-1.1.1.md \
  PLAN.md ; do
  if [ -f "$f" ]; then printf '   %-64s %4s lines\n' "$f" "$(wc -l < "$f" | tr -d ' ')"
  else printf '   %-64s MISSING\n' "$f"; fail=1; fi
done

echo "trees this node must never touch:"
for tree in kite-momentum-rebalancer/ frozen/; do
  n=$(git status --short "$tree" | wc -l | tr -d ' ')
  printf '   %-30s %s changed file(s)\n' "$tree" "$n"
  [ "$n" = "0" ] || fail=1
done

# The question is "did node 1.1.1 wander?", not "is the tree pristine" — a concurrent session is
# editing this repo. `backtest.py` carries a one-line comment edit (a docs cross-reference,
# M40.4 -> M46.4) that this node did not make and must not claim. It is listed by name so that
# anything NEW appearing here still fails, instead of the check being loosened to a warning.
echo "changes under packages/core, and whose they are:"
expected="allocation_ledger.py test_allocation_ledger.py backtest.py"
while read -r _status path; do
  [ -z "$path" ] && continue
  base=$(basename "$path")
  case " $expected " in
    *" $base "*)
      if [ "$base" = "backtest.py" ]; then
        printf '   %-34s not this node (1-line comment edit, concurrent session)\n' "$base"
      else
        printf '   %-34s this node\n' "$base"
      fi
      ;;
    *) printf '   %-34s UNEXPECTED — investigate before claiming this node is clean\n' "$base"; fail=1 ;;
  esac
done < <(git status --short decile-blueprint/packages/core)

echo "PLAN.md records the node as done and names what is next:"
grep -q "node 1.1.1 DONE" PLAN.md && echo "   status log updated" || { echo "   PLAN.md not updated"; fail=1; }
grep -q "Next node" PLAN.md && echo "   next node named" || { echo "   next node not named"; fail=1; }

echo
[ "$fail" -eq 0 ] && echo "FOOTPRINT OK" || echo "FOOTPRINT FAILED"
exit "$fail"
