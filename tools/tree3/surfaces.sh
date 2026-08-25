#!/usr/bin/env bash
# Tree 3 / G12 — the surfaces must render numbers, not em dashes.
#
# A full `fundamental_daily` proves the fetch ran. It does not prove a person sees anything: the
# API resolves every as-of through `latest_published_date`, so a row on the wrong date changes
# nothing on screen. This asks the running API for the same payloads the pages consume, and asks
# the running web app for the rendered HTML.
#
#   API on :8000, web on :3000. Pass a symbol to check a different name.
set -uo pipefail
API="${API:-http://localhost:8000}"
WEB="${WEB:-http://localhost:3000}"
SYMBOL="${1:-INFY}"

up() { curl -s -o /dev/null --max-time 5 -w '%{http_code}' "$1" 2>/dev/null; }
[ "$(up "$API/health")" = "200" ] || { echo "SURFACES SKIPPED — no API on $API (make api)"; exit 2; }

echo "== instrument factsheet: GET $API/api/v1/instruments/$SYMBOL =="
curl -s --max-time 20 "$API/api/v1/instruments/$SYMBOL" | python3 -c "
import sys, json
d = json.load(sys.stdin)
stats = {s['key']: s['value'] for s in d.get('key_stats', [])}
print(f\"  as_of = {d.get('as_of')}  (the date the product serves)\")
bad = 0
for key, label in (('marketcap_cr', 'M-cap (cr)'), ('pe', 'P/E')):
    value = stats.get(key)
    rendered = '—' if value is None else value
    print(f'  {label:<12} = {rendered}')
    if value is None:
        bad += 1
print('  FACTSHEET ' + ('OK' if bad == 0 else f'EM DASH on {bad} of 2'))
sys.exit(1 if bad else 0)
"
factsheet=$?

echo
echo "== screener columns, as the screener itself ranks them =="
docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc "
  with pub as (select max(trade_date) d from pipeline_run where data_version is not null)
  select count(*) || ' rows, ' || count(marketcap_cr) || ' with M-cap, ' || count(pe) || ' with P/E'
  from factor_daily, pub where date = pub.d" | sed 's/^/  /'

echo
echo "== landing page HTML: GET $WEB/ =="
if [ "$(up "$WEB/")" = "200" ]; then
  html=$(curl -s --max-time 45 "$WEB/")
  if echo "$html" | grep -q "M-cap"; then
    echo "  M-cap column is present in the rendered HTML"
    # The sample table formats caps with en-IN separators; an em dash means a NULL got through.
    echo "$html" | grep -oE 'M-cap[^<]*' | head -2 | sed 's/^/    /'
  else
    echo "  landing page did not render the sample screen table (API unreachable at build?)"
  fi
else
  echo "  SKIPPED — no web server on $WEB (make web)"
fi

echo
[ "$factsheet" -eq 0 ] && echo "SURFACES OK" || echo "SURFACES FAILED — a surface still renders an em dash"
exit "$factsheet"
