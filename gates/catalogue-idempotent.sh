#!/usr/bin/env bash
# House rule 7: re-running the seed produces identical rows.
set -uo pipefail
PSQL="docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc"
snap() { $PSQL "select (select count(*) from cb_basket)||'/'||(select count(*) from cb_basket_version)||'/'||(select count(*) from cb_constituent)||'/'||coalesce(md5(string_agg(x,',' order by x)),'-') from (select v.basket_id||':'||c.instrument_id||':'||c.weight x from cb_constituent c join cb_basket_version v on v.id=c.version_id) t;"; }
BEFORE=$(snap)
( cd decile-blueprint && uv run python -m baskfy_api.seed reference >/dev/null 2>&1 )
AFTER=$(snap)
echo "BEFORE=$BEFORE"
echo "AFTER =$AFTER"
[ "$BEFORE" = "$AFTER" ] && echo "IDEMPOTENT_OK" || echo "IDEMPOTENT_FAIL"
