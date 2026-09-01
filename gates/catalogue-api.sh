#!/usr/bin/env bash
# The catalogue API returns the whole shelf, through a real HTTP request with a real session.
set -uo pipefail
TOK=$(curl -s --max-time 20 -X POST http://127.0.0.1:8000/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"e2e@example.com","password":"e2e-suite-password"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
[ -z "$TOK" ] && { echo "NO_TOKEN"; exit 1; }
curl -s --max-time 20 -H "Authorization: Bearer $TOK" 'http://127.0.0.1:8000/api/v1/explore?limit=50' \
 | python3 -c "
import json,sys
d=json.load(sys.stdin)
items=d.get('items') or d.get('data') or []
print('EXPLORE_BASKETS='+str(len(items)))
blank=0
for b in items:
    m=b.get('metrics') or {}
    if m.get('min_amount') is None or m.get('volatility_bucket') is None: blank+=1
    print('  -', b.get('slug'), '| min:', m.get('min_amount'), '| vol:', m.get('volatility_bucket'), '| 1y:', m.get('ret_1y'))
print('BLANK_CARDS='+str(blank))
"
