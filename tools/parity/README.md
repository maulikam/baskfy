# tools/parity — goldens for the Go rewrite

`golden.py` is the canonical dumper (see its docstring). Each lane adds `dump_L<n>.py` and runs it
with the tree's own interpreter, e.g.

```bash
cd decile-blueprint && .venv/bin/python -m tools.parity.dump_L1     # or: PYTHONPATH=.. .venv/bin/python ../tools/parity/dump_L1.py
cd kite-momentum-rebalancer && .venv/bin/python ../tools/parity/dump_L4.py
```

Goldens land in `go/testdata/golden/L<n>/…` and are committed. Dumpers import Python; they never
modify it, never use the network, never open the desk's live database or `portfolio.db`.

## The desk: `desk_diff.py` (wave D, DECISIONS-RS R8.3–R8.6)

Bodies are compared with each side's port written `<port>` and its run directory written
`<run>` (the settings page prints `KITE_TOKEN_FILE` under it). `DESK_DIFF_DUMP=<dir>` writes the
full differing bodies (the report truncates at 1,500 characters). A `--fixture` is an absolute
path. Integrator run (RS-D): every `desk_cases/*.json` in one run plus `--plans`, 485/491 EQUAL,
every side effect EQUAL (STATUS.md, wave D integration).

```bash
python3 tools/parity/desk_diff.py --py-port 18501 --rs-port 18502 --smtp-port 18503 \
    [--backend postgres|sqlite] [--plans] [--python-both] [--cases 'tools/parity/desk_cases/<lane>.json']
```

Both desks run from empty run directories with `DRY_RUN=true`, the replay Kite
(`desk_fake_kite.py` / `baskfy-desk --harness`, fixture `desk_fixtures/default`, rebuilt by
`desk_fixture_build.py` from the corpus, read only), an injected clock and the harness `uuid4`
sequence (`desk_harness.py` patches the Python seams; the desk's code is unchanged). Each side gets
its own `rsd_*` bed clone (or SQLite file), Redis index 8–15, and the run holds the machine-wide
`nightly_diff.lock`. Responses, plan digests, order/GTT calls, `data/` files (journal, execution
logs), tables, Redis and mail are compared; see the script's docstring for the case format
(`clock` presets, `capture`, `mask`, `db_mask`, `plan`). `--python-both` is the self-check: it must
be all EQUAL before a Rust difference means anything. Answer keys for the crate's own tests:
`desk_settings_dump.py` (settings) and `desk_probes.py` (`%`/`format()`, orjson, Jinja2).
