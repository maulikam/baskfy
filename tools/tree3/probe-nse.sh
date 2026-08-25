#!/usr/bin/env bash
# Tree 3 / G1 — establish the exact failure mode of NSE quote-equity from this box.
# Not "NSE is blocked": which host, which endpoint, which layer, which status.
set -uo pipefail

UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

probe() { # name url
  local name="$1" url="$2" code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
    -b "$JAR" -c "$JAR" \
    -H "User-Agent: $UA" \
    -H 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8' \
    -H 'Accept-Language: en-GB,en;q=0.9' \
    -L "$url")
  printf '  %-34s %-58s %s\n' "$name" "${url:0:58}" "$code"
}

echo "PROBE nse reachability  $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "  egress ip: $(curl -s --max-time 10 https://ifconfig.me || echo unknown)"
echo
echo "  --- archive host (nsearchives) ---"
probe "listings EQUITY_L.csv"   "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
echo
echo "  --- api host (www.nseindia.com) ---"
probe "site root"               "https://www.nseindia.com/"
probe "api/marketStatus"        "https://www.nseindia.com/api/marketStatus"
probe "api/quote-equity INFY"   "https://www.nseindia.com/api/quote-equity?symbol=INFY"
probe "api/equity-meta INFY"    "https://www.nseindia.com/api/equity-meta-info?symbol=INFY"

echo
echo "  --- who refuses ---"
curl -s --max-time 20 -b "$JAR" -c "$JAR" -H "User-Agent: $UA" -D - -o /dev/null \
  'https://www.nseindia.com/api/quote-equity?symbol=INFY' 2>/dev/null \
  | grep -iE '^(HTTP/|server:|x-akamai|set-cookie: _abck)' | sed 's/^/    /' | head -8

echo
echo "PROBE COMPLETE"
