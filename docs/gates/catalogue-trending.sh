#!/usr/bin/env bash
# With the catalogue filled, the computable ranked lists should clear their entry floor and
# publish instead of withholding for want of baskets.
set -uo pipefail
TOK=$(curl -s --max-time 20 -X POST http://127.0.0.1:8000/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"e2e@example.com","password":"e2e-suite-password"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
[ -z "$TOK" ] && { echo "NO_TOKEN"; exit 1; }
curl -s --max-time 30 -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/v1/cb/trending | python3 -c "
import json,sys
d=json.load(sys.stdin)
lists=d.get('lists') or d.get('items') or (d if isinstance(d,list) else [])
pub=[l for l in lists if not (l.get('withheld_reason') or l.get('withheld'))]
for l in lists:
    n=len(l.get('entries') or [])
    print('  -', l.get('key'), '| entries:', n, '| withheld:', l.get('withheld_reason') or l.get('withheld') or '-')
print('TOTAL_LISTS='+str(len(lists)))
print('PUBLISHED_LISTS='+str(len(pub)))
"
