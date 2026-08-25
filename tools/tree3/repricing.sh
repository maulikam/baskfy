#!/usr/bin/env bash
# Tree 3 / G9b — house rule 5, checked against the archived bytes rather than against the code.
#
# `fundamental_daily` stores no `last_price`, so the only way to prove the stored P/E was
# re-priced onto the target date is to go back to the raw payload the parse came from. That is
# what archive-then-parse (docs/09) is for. This samples symbols, recomputes both stored numbers
# straight from the archived JSON, and compares.
set -uo pipefail
cd "$(dirname "$0")/../../decile-blueprint" || exit 2
PSQL=(docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc)
DATE="${1:-$("${PSQL[@]}" "select max(trade_date) from pipeline_run where data_version is not null")}"
SAMPLE="${2:-25}"

symbols=$("${PSQL[@]}" "
  select i.symbol from fundamental_daily f
  join instrument i on i.id = f.instrument_id
  where f.date = '$DATE' and f.pe is not null and f.shares_outstanding is not null
  order by f.marketcap_cr desc nulls last limit $SAMPLE")

[ -z "$symbols" ] && { echo "REPRICING INCONCLUSIVE — no rows with a P/E on $DATE"; exit 1; }

python3 - "$DATE" $symbols <<'PY'
import json, subprocess, sys, glob
from decimal import Decimal, ROUND_HALF_UP
date = sys.argv[1]
symbols = sys.argv[2:]
checked = mismatch = skipped = 0
worst = Decimal(0)
for sym in symbols:
    hits = glob.glob(f".archive/nse/equity-fundamentals/{sym}-*/{date}.json")
    if not hits:
        skipped += 1
        continue
    entry = json.load(open(hits[0]))["equityResponse"][0]
    quoted = Decimal(entry["secInfo"]["pdSymbolPe"])
    last = Decimal(str(entry["tradeInfo"]["lastPrice"]))
    shares = int(entry["tradeInfo"]["issuedSize"])
    row = subprocess.run(
        ["docker","exec","baskfy-postgres","psql","-U","baskfy","-d","baskfy","-tAc",
         "select o.close_raw, f.pe, f.marketcap_cr from fundamental_daily f "
         "join instrument i on i.id=f.instrument_id "
         "join ohlcv_daily o on o.instrument_id=f.instrument_id and o.date=f.date "
         f"where i.symbol='{sym}' and f.date='{date}'"],
        capture_output=True, text=True).stdout.strip()
    if not row:
        skipped += 1
        continue
    close_raw, db_pe, db_mcap = row.split("|")
    close_raw, db_pe, db_mcap = Decimal(close_raw), Decimal(db_pe), int(db_mcap)
    exp_pe = (quoted * close_raw / last).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    exp_mc = int((Decimal(shares) * close_raw / Decimal(10_000_000))
                 .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    checked += 1
    drift = abs(quoted - exp_pe) / exp_pe * 100
    worst = max(worst, drift)
    if exp_pe != db_pe or exp_mc != db_mcap:
        mismatch += 1
        print(f"    {sym}: pe exp={exp_pe} got={db_pe} | mcap exp={exp_mc} got={db_mcap}")
print(f"  date={date} checked={checked} mismatched={mismatch} no_archive={skipped}")
print(f"  largest look-ahead error avoided by re-pricing: {worst:.2f}%")
print("REPRICING OK" if checked and not mismatch else "REPRICING FAILED")
sys.exit(0 if checked and not mismatch else 1)
PY
