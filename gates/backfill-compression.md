# The deep backfill must not be able to corrupt the price table

**Incident, 11 Sep 2026.** The nightly pipeline for 2026-09-11 crashed at 20:22 after 97 minutes:
`aggregation 'item' expected no or a single value, got 2 values` on instrument 556. Cause: three
duplicate `(instrument_id, date)` rows in `ohlcv_daily`, all on 2026-02-01, each a pair identical
in value and differing only in `source` — one `nse`, one `kite`.

**Why the primary key did not stop it.** `ohlcv_daily` is a TimescaleDB hypertable with compression
on; every chunk before 2026-03-08 is compressed. `deep_backfill._write` already does the right
thing — `ON CONFLICT (instrument_id, date) DO NOTHING` — but **a write into a compressed chunk
cannot see the compressed row**, so the conflict never fires, and a later recompression bakes the
duplicate in permanently. The guard is correct and inoperative.

Repaired by hand (drop chunk constraint → decompress → delete the three → restore → recompress).
This file is about making the tool incapable of doing it again, because the run Maulik wants
touches **ten compressed chunks across 2,338 instruments**.

- [ ] G1: The tool refuses, by default, to write into a compressed chunk. Not a warning — a
      refusal, counted and named in the report.
  CHECK: cd decile-blueprint && grep -cE "compressed" services/worker/src/baskfy_worker/deep_backfill.py
  EXPECT: /^[1-9]/
  EVIDENCE: pending

- [ ] G2: A refusal is **visible in the report**, with the instrument count and the chunks
      involved. A skip nobody sees is the same as a silent corruption one release later.
  CHECK: cd decile-blueprint && uv run pytest services/worker/tests -k "deep_backfill and (compressed or refus)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G3: **The post-run assertion.** After any write, the tool checks for duplicate
      `(instrument_id, date)` pairs and fails loudly if it created any. House rule 7 says a job is
      idempotent; this proves it rather than trusting it.
  CHECK: cd decile-blueprint && uv run pytest services/worker/tests -k "deep_backfill and duplicate" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G4: Re-running the tool over a range it has already written adds **nothing** — the
      idempotence the `ON CONFLICT` was always meant to give, now true on a compressed table too.
  CHECK: cd decile-blueprint && uv run pytest services/worker/tests -k "deep_backfill and idempot" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G5: The existing behaviour is unchanged where it was already right: zero-close placeholder
      bars still dropped, the splice factor still computed off the non-kite segment, dates at or
      after the first existing bar still not written.
  CHECK: cd decile-blueprint && uv run pytest services/worker/tests/test_deep_backfill.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G6: `make lint` clean — ruff, ruff format, mypy --strict.
  CHECK: cd decile-blueprint && uv run ruff check . 2>&1 | tail -2 && uv run mypy --strict 2>&1 | tail -2
  EXPECT: /Success|All checks passed/
  EVIDENCE: pending

- [ ] G7: **The box has no duplicate pairs**, before the run and after it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=${AWS_PROFILE:-baskfy-poc} BASKFY_INSTANCE_ID=${BASKFY_INSTANCE_ID:-i-086986250704e4392} bash tools/deploy/box.sh "cd /opt/baskfy && docker compose -f compose.prod.yml --env-file .env.staging.compose exec -T postgres psql -U baskfy -d baskfy -t -A -c \"select count(*) from (select instrument_id,date from ohlcv_daily group by instrument_id,date having count(*)>1) t\"" 2>&1 | tail -1
  EXPECT: /^\s*0\s*$/
  EVIDENCE: pending

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
