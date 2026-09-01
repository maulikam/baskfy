#!/usr/bin/env bash
# The one surface a stranger can reach must render its sample screen, not an apology.
set -uo pipefail
OUT=$(mktemp)
CODE=$(curl -s -o "$OUT" -w "%{http_code}" --max-time 60 http://127.0.0.1:3000/)
echo "LANDING_HTTP=$CODE"
if grep -q "sample screen could not be loaded" "$OUT"; then echo "ERROR_TEXT=present"; else echo "ERROR_TEXT=absent"; fi
ROWS=$(grep -o 'data-testid="sample-row"' "$OUT" | wc -l | tr -d ' ')
[ "$ROWS" = "0" ] && ROWS=$(grep -o '<tr' "$OUT" | wc -l | tr -d ' ')
echo "SAMPLE_ROWS=$ROWS"
if [ "$CODE" = "200" ] && ! grep -q "sample screen could not be loaded" "$OUT" && [ "$ROWS" != "0" ]; then echo "LANDING_OK"; else echo "LANDING_FAIL"; fi
rm -f "$OUT"
