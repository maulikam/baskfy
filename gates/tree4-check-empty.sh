#!/usr/bin/env bash
# Every live surface whose primary list is empty in this database must render a designed message.
# The surface list is the eight tables measured empty on 2026-08-25, mapped to what renders them.
set -uo pipefail
cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web || exit 1
BLANK=0
check() {
  local file="$1" label="$2"
  if [ ! -f "$file" ]; then echo "  MISSING $label ($file)"; BLANK=$((BLANK+1)); return; fi
  if grep -qE 'length === 0|length===0|EmptyState' "$file"; then
    echo "  ok      $label"
  else
    echo "  BLANK   $label"; BLANK=$((BLANK+1))
  fi
}
check "src/app/(app)/me/watchlist/page.tsx"                  "watchlist"
check "src/app/(app)/home/page.tsx"                          "home / pending actions"
check "src/app/(app)/fees/page.tsx"                          "fee ledger"
check "src/components/integrations/alert-manager.tsx"        "screen alerts"
check "src/components/integrations/api-key-manager.tsx"      "api keys"
check "src/app/(app)/me/investments/[id]/orders/page.tsx"    "order batches"
check "src/app/(app)/me/investments/[id]/page.tsx"           "dividends"
check "src/app/(app)/me/investments/page.tsx"                "investments list"
echo "BLANK_SURFACES=$BLANK"
BARE=$(find src -name '*.tsx' -not -path '*__tests__*' -print0 | xargs -0 grep -lE '>[[:space:]]*(No data|No results)[.<"]' 2>/dev/null | wc -l | tr -d ' ')
echo "BARE_NO_DATA=$BARE"
