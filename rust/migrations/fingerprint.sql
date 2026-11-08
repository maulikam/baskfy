-- One sorted text row per schema object in `public`: what a database must match to adopt the baseline.
WITH t AS (
  SELECT c.oid, c.relname, c.relkind
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public'
    AND c.relname NOT IN ('alembic_version', '_sqlx_migrations')
    AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e')
), own_rows AS (
  SELECT 'col|' || t.relname || '|' || a.attname || '|' || format_type(a.atttypid, a.atttypmod)
         || '|' || a.attnotnull || '|' || COALESCE(pg_get_expr(ad.adbin, ad.adrelid), '') AS line
  FROM t JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped
  LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
  WHERE t.relkind IN ('r', 'p')
  UNION ALL
  SELECT 'con|' || t.relname || '|' || c.conname || '|' || pg_get_constraintdef(c.oid)
  FROM t JOIN pg_constraint c ON c.conrelid = t.oid
  UNION ALL
  SELECT 'idx|' || tablename || '|' || indexname || '|' || indexdef
  FROM pg_indexes WHERE schemaname = 'public' AND tablename NOT IN ('alembic_version', '_sqlx_migrations')
  UNION ALL
  SELECT 'view|' || t.relname || '|' || pg_get_viewdef(t.oid, true) FROM t WHERE t.relkind IN ('v', 'm')
  UNION ALL
  SELECT 'seq|' || t.relname FROM t WHERE t.relkind = 'S'
  UNION ALL
  SELECT 'trg|' || t.relname || '|' || g.tgname || '|' || pg_get_triggerdef(g.oid)
  FROM t JOIN pg_trigger g ON g.tgrelid = t.oid
  WHERE NOT g.tgisinternal AND g.tgname NOT LIKE 'ts\_%'
  UNION ALL
  SELECT 'fn|' || p.proname || '|' || pg_get_function_identity_arguments(p.oid) || '|' || md5(p.prosrc)
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'public'
    AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
  UNION ALL
  SELECT 'enum|' || y.typname || '|' || string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
  FROM pg_type y JOIN pg_namespace n ON n.oid = y.typnamespace JOIN pg_enum e ON e.enumtypid = y.oid
  WHERE n.nspname = 'public' GROUP BY y.typname
  UNION ALL
  SELECT 'hyper|' || hypertable_name || '|' || column_name || '|' || dimension_type || '|'
         || COALESCE(time_interval::text, integer_interval::text, num_partitions::text, '')
  FROM timescaledb_information.dimensions WHERE hypertable_schema = 'public'
  UNION ALL
  SELECT 'compress|' || hypertable_name || '|' || compression_enabled
  FROM timescaledb_information.hypertables WHERE hypertable_schema = 'public'
  UNION ALL
  SELECT 'job|' || hypertable_name || '|' || proc_name || '|' || schedule_interval || '|' || COALESCE(config::text, '')
  FROM timescaledb_information.jobs WHERE hypertable_schema = 'public'
)
-- A dump-and-restore rewrites CHECK expressions and renumbers Timescale's internals; neither is drift.
, canon AS (
  SELECT regexp_replace(regexp_replace(
           CASE WHEN line ~ '^(con|idx|view)\|'
                THEN replace(replace(replace(regexp_replace(line,
                       '::(character varying|text\[\]|text|numeric|integer|bigint|timestamp with time zone)', '', 'g'),
                       '(', ''), ')', ''), 'ARRAY[', '[')
                ELSE line END,
           '_materialized_hypertable_[0-9]+', '_MAT', 'g'),
           '"(mat_)?hypertable_id": [0-9]+', '"hypertable_id": N', 'g') AS line
  FROM own_rows
)
SELECT DISTINCT regexp_replace(line, '\s+', ' ', 'g') AS line FROM canon ORDER BY 1
