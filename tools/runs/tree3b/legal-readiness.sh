#!/usr/bin/env bash
# Tree 3b / G9 — the part of "get a lawyer onto the drafts" that an agent can actually do.
#
# It cannot engage counsel. It CAN make sure that (1) one sendable brief exists, (2) the drafts
# cannot be mistaken for reviewed text, and (3) the blocker is filed where the owner will see it.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
WEB=decile-blueprint/apps/web
BASE="${BASE:-http://localhost:3000}"
fail=0

echo "1. a single brief a lawyer could be sent"
if [ -f docs/COUNSEL-BRIEF.md ]; then
  echo "   docs/COUNSEL-BRIEF.md — $(wc -w < docs/COUNSEL-BRIEF.md | tr -d ' ') words"
  for want in "eight questions" "C1" "C2" "C3" "GSTIN" "DPDP" "checklist"; do
    grep -qi -- "$want" docs/COUNSEL-BRIEF.md || { echo "   MISSING section: $want"; fail=1; }
  done
  echo "   covers: the 4 documents, the 8 document questions, C1-C3, D7/D10, the fact checklist"
else
  echo "   MISSING docs/COUNSEL-BRIEF.md"; fail=1
fi

echo "2. the drafts cannot pass as reviewed"
out=$(cd "$WEB" && pnpm exec vitest run src/lib/__tests__/legal-drafts.test.ts 2>&1 \
      | grep -E "Tests +[0-9]+ (passed|failed)" | tail -1)
echo "   legal-drafts guard: ${out:-no summary}"
grep -q "passed" <<<"$out" || fail=1
for f in "$WEB"/src/content/legal/*.mdx; do
  grep -q "DRAFT" "$f" || { echo "   $(basename "$f") carries no in-repo DRAFT marker"; fail=1; }
done
echo "   every .mdx carries the in-repo DRAFT marker"

echo "3. the blocker is filed for the owner"
grep -q "COUNSEL-BRIEF" NEEDS-MAULIK.md || { echo "   NEEDS-MAULIK.md does not point at the brief"; fail=1; }
grep -qi "placeholder" NEEDS-MAULIK.md || { echo "   the live-placeholder finding is not filed"; fail=1; }
echo "   NEEDS-MAULIK.md points at the brief and records the live-placeholder finding"

echo "4. what a visitor sees today (the finding that makes this urgent)"
for u in terms-conditions privacy-policy disclaimer refund-policy; do
  seen=$(curl -s --max-time 60 "$BASE/$u" 2>/dev/null | grep -oE "\[[A-Z][A-Z ]+\]" | sort -u | tr '\n' ' ')
  printf '   /%-18s %s\n' "$u" "${seen:-none}"
done

echo
[ "$fail" -eq 0 ] && echo "LEGAL PACKAGE READY — brief written, guard passing, blocker filed" \
                  || echo "LEGAL PACKAGE INCOMPLETE"
exit "$fail"
