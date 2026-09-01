#!/usr/bin/env bash
# Node 1.1.2 — prove the schema enforces §4, by trying to break it.
#
# Reading the DDL proves the DDL was written. These subcommands attempt the writes the spec
# forbids and require Postgres to refuse them, which is the only evidence that matters.
#
#   bash tools/portfolio/schema-check.sh <columns|criterion2|monitoring|cascade|roundtrip
#                                         |single-vocabulary|dev-intact>
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
DB="${DB:-baskfy_mig_test}"
API=decile-blueprint/services/api
URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/$DB"
psql() { docker exec baskfy-postgres psql -U baskfy -d "$DB" -tAc "$1" 2>&1; }

# A user, a broker account, two instruments and portfolios of each kind — the smallest world in
# which criterion 2 can be violated.
# A user, a broker account, an instrument and portfolios of each kind — the smallest world in
# which criterion 2 can be violated. Built from scratch because the migration-test database is
# freshly migrated and therefore empty: no exchange, no instrument, no user.
seed() {
  psql "
    DELETE FROM portfolio_holding; DELETE FROM portfolio;
    DELETE FROM broker_account;
    DELETE FROM app_user WHERE email = 'ledger@example.com';
    INSERT INTO exchange (id, code) VALUES (1, 'NSE') ON CONFLICT (id) DO NOTHING;
    DELETE FROM instrument WHERE symbol = 'LEDGERTEST';
    INSERT INTO instrument (exchange_id, symbol, name, instrument_type, series, is_active)
      VALUES (1, 'LEDGERTEST', 'Ledger Test Ltd', 'EQ', 'EQ', true);
    INSERT INTO app_user (email, public_id) VALUES ('ledger@example.com', 'ledger000001');
    INSERT INTO broker_account (user_id, broker_id, label)
      SELECT id, 'zerodha', 'primary' FROM app_user WHERE email = 'ledger@example.com';
    INSERT INTO portfolio (user_id, name, kind, source, started_on)
      SELECT id, 'Cap A', 'CAPITAL', 'HOLDING_GROUP', '2026-01-02' FROM app_user WHERE email='ledger@example.com';
    INSERT INTO portfolio (user_id, name, kind, source, started_on)
      SELECT id, 'Cap B', 'CAPITAL', 'HOLDING_GROUP', '2026-01-02' FROM app_user WHERE email='ledger@example.com';
    INSERT INTO portfolio (user_id, name, kind, source, started_on)
      SELECT id, 'Lens', 'MONITORING', 'HOLDING_GROUP', '2026-01-02' FROM app_user WHERE email='ledger@example.com';
  "
}

ids() {
  A=$(psql "select id from portfolio where name='Cap A' order by id limit 1")
  B=$(psql "select id from portfolio where name='Cap B' order by id limit 1")
  L=$(psql "select id from portfolio where name='Lens' order by id limit 1")
  BA=$(psql "select id from broker_account order by id limit 1")
  INS=$(psql "select id from instrument where symbol='LEDGERTEST' order by id limit 1")
  for v in A B L BA INS; do
    eval "value=\$$v"
    [ -n "$value" ] || { echo "  seed failed: $v is empty"; exit 1; }
  done
}

add() { # portfolio_id kind -> insert a holding of INS at BA
  psql "INSERT INTO portfolio_holding
          (portfolio_id, instrument_id, broker_account_id, portfolio_kind, quantity, added_on)
        VALUES ($1, $INS, $BA, '$2', 10, '2026-01-02')"
}

case "${1:-}" in
columns)
  seed >/dev/null
  cols=$(psql "select string_agg(column_name, ',' order by column_name)
               from information_schema.columns
               where table_name='portfolio' and column_name in ('kind','source')")
  echo "  portfolio columns: ${cols:-none}"
  bad=$(psql "insert into portfolio (user_id, name, kind, source, started_on)
              select id, 'Bad', 'NOT_A_KIND', 'HOLDING_GROUP', '2026-01-02' from app_user limit 1")
  echo "  an unknown kind is: $(grep -qi 'violates check constraint' <<<"$bad" && echo 'refused by CHECK' || echo "ACCEPTED -- $bad")"
  [ "$cols" = "kind,source" ] && grep -qi 'violates check constraint' <<<"$bad" \
    && echo "COLUMNS OK" || { echo "COLUMNS FAILED"; exit 1; }
  ;;
criterion2)
  seed >/dev/null; ids
  first=$(add "$A" CAPITAL); second=$(add "$B" CAPITAL)
  echo "  same holding into capital portfolio A: ${first:-accepted}"
  echo "  same holding into capital portfolio B: $(grep -qi 'duplicate key' <<<"$second" && echo 'REFUSED by unique index' || echo "ACCEPTED -- $second")"
  grep -qi 'duplicate key' <<<"$second" && echo "CRITERION 2 ENFORCED" || { echo "CRITERION 2 NOT ENFORCED"; exit 1; }
  ;;
monitoring)
  seed >/dev/null; ids
  add "$A" CAPITAL >/dev/null
  one=$(add "$L" MONITORING)
  psql "INSERT INTO portfolio (user_id, name, kind, source, started_on)
        SELECT id,'Lens2','MONITORING','HOLDING_GROUP','2026-01-02' FROM app_user WHERE email='ledger@example.com'" >/dev/null
  L2=$(psql "select id from portfolio where name='Lens2' order by id limit 1")
  two=$(add "$L2" MONITORING)
  n=$(psql "select count(*) from portfolio_holding")
  echo "  capital + 2 monitoring views over one holding -> $n rows"
  echo "  first lens: ${one:-accepted}   second lens: ${two:-accepted}"
  [ "$n" = "3" ] && echo "MONITORING OVERLAP OK" || { echo "MONITORING OVERLAP FAILED"; exit 1; }
  ;;
cascade)
  seed >/dev/null; ids
  add "$A" CAPITAL >/dev/null
  lie=$(psql "update portfolio_holding set portfolio_kind='MONITORING' where portfolio_id=$A")
  echo "  claiming a kind the portfolio does not have: $(grep -qi 'violates foreign key' <<<"$lie" && echo 'REFUSED' || echo "ACCEPTED -- $lie")"
  psql "update portfolio set kind='MONITORING' where id=$A" >/dev/null
  carried=$(psql "select portfolio_kind from portfolio_holding where portfolio_id=$A")
  echo "  flipping the portfolio carried its holding to: $carried"
  add "$B" CAPITAL >/dev/null
  collide=$(psql "update portfolio set kind='CAPITAL' where id=$A")
  echo "  flipping back into a collision: $(grep -qi 'duplicate key' <<<"$collide" && echo 'REFUSED' || echo "ACCEPTED -- $collide")"
  grep -qi 'violates foreign key' <<<"$lie" && [ "$carried" = "MONITORING" ] \
    && grep -qi 'duplicate key' <<<"$collide" \
    && echo "CASCADE OK" || { echo "CASCADE FAILED"; exit 1; }
  ;;
roundtrip)
  # Clean holdings first: this subcommand tests the migration's mechanics, and a duplicate left
  # by an earlier subcommand would exercise the pre-flight guard instead (see `preflight`).
  psql "DELETE FROM portfolio_holding" >/dev/null
  ( cd "$API" && BASKFY_DATABASE_URL="$URL" uv run alembic downgrade 0020_manager_identity ) >/dev/null 2>&1 || { echo "downgrade FAILED"; exit 1; }
  left=$(psql "select count(*) from information_schema.columns
               where table_name in ('portfolio','portfolio_holding')
                 and column_name in ('kind','source','portfolio_kind')")
  idx=$(psql "select count(*) from pg_indexes where indexname='uq_portfolio_holding_one_capital_portfolio'")
  echo "  after downgrade: $left new column(s), $idx index(es) remain"
  ( cd "$API" && BASKFY_DATABASE_URL="$URL" uv run alembic upgrade head ) >/dev/null 2>&1 || { echo "re-upgrade FAILED"; exit 1; }
  back=$(psql "select count(*) from information_schema.columns
               where table_name in ('portfolio','portfolio_holding')
                 and column_name in ('kind','source','portfolio_kind')")
  echo "  after re-upgrade: $back column(s) back"
  [ "$left" = "0" ] && [ "$idx" = "0" ] && [ "$back" = "3" ] \
    && echo "ROUNDTRIP OK" || { echo "ROUNDTRIP FAILED"; exit 1; }
  ;;
single-vocabulary)
  mig=decile-blueprint/services/api/alembic/versions/0021_allocation_ledger.py
  led=decile-blueprint/packages/core/src/baskfy_core/allocation_ledger.py
  out=$(cd decile-blueprint && uv run python -c "
from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource
import re, pathlib
mig = pathlib.Path('services/api/alembic/versions/0021_allocation_ledger.py').read_text()
kinds = set(re.search(r'_KINDS = \((.*?)\)', mig, re.S).group(1).replace(chr(34),'').replace(chr(39),'').split(','))
srcs  = set(re.search(r'_SOURCES = \((.*?)\)', mig, re.S).group(1).replace(chr(34),'').replace(chr(39),'').split(','))
clean = lambda s: {x.strip() for x in s if x.strip()}
ok_k = clean(kinds) == {k.value for k in PortfolioKind}
ok_s = clean(srcs) == {s.value for s in PortfolioSource}
print(f'kinds match: {ok_k}  sources match: {ok_s}')
print('ONE VOCABULARY' if ok_k and ok_s else 'VOCABULARY DRIFT')
")
  echo "  $out" | sed 's/^  //'
  grep -q "ONE VOCABULARY" <<<"$out" || exit 1
  ;;
dev-intact)
  p=$(docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc "select count(*) from portfolio")
  s=$(docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc "select count(*) from portfolio_sleeve")
  b=$(docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc "select count(*) from broker_account")
  h=$(docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc "select version_num from alembic_version")
  echo "  baskfy: $p portfolios, $s sleeves, $b broker accounts, head $h"
  desk=$(git status --short kite-momentum-rebalancer/ frozen/ | wc -l | tr -d ' ')
  echo "  desk + frozen changed files: $desk"
  [ "$p" = "3" ] && [ "$s" = "8" ] && [ "$b" = "2" ] && [ "$desk" = "0" ] \
    && echo "DEV DB INTACT" || { echo "DEV DB CHANGED"; exit 1; }
  ;;
preflight)
  # The migration must refuse a database that already breaks criterion 2, and name the holdings.
  # The violation has to be built *at 0020*, because at head the unique index already prevents it
  # — which is the whole point of the index, and the reason a first attempt at this test quietly
  # created nothing and proved nothing.
  psql "DELETE FROM portfolio_holding" >/dev/null
  seed >/dev/null; ids
  add "$A" CAPITAL >/dev/null
  ( cd "$API" && BASKFY_DATABASE_URL="$URL" uv run alembic downgrade 0020_manager_identity ) >/dev/null 2>&1
  # No portfolio_kind column and no index at 0020, so the second capital row is accepted here.
  psql "INSERT INTO portfolio_holding (portfolio_id, instrument_id, broker_account_id, quantity, added_on)
        VALUES ($B, $INS, $BA, 10, '2026-01-02')" >/dev/null
  echo "  at 0020, the same holding now sits in $(psql "select count(*) from portfolio_holding") portfolios"
  out=$( cd "$API" && BASKFY_DATABASE_URL="$URL" uv run alembic upgrade head 2>&1 )
  echo "  upgrading says:"
  grep -E "already breaks acceptance criterion 2|LEDGERTEST at broker|will not choose a winner" <<<"$out" | sed 's/^/    /'
  # Leave the database at head and clean, so the next subcommand starts from a known place.
  psql "DELETE FROM portfolio_holding" >/dev/null
  ( cd "$API" && BASKFY_DATABASE_URL="$URL" uv run alembic upgrade head ) >/dev/null 2>&1
  grep -q "already breaks acceptance criterion 2" <<<"$out" \
    && grep -q "will not choose a winner" <<<"$out" \
    && echo "PREFLIGHT OK" || { echo "PREFLIGHT FAILED — the migration did not refuse clearly"; exit 1; }
  ;;
*) echo "usage: schema-check.sh <columns|criterion2|monitoring|cascade|roundtrip|preflight|single-vocabulary|dev-intact>"; exit 2 ;;
esac
