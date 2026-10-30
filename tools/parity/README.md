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
## The worker's nightly: `nightly_diff.py` (wave 5; DECISIONS-RS R7.3, R7.6, R7.7)

The nightly is graded **differentially**, like the API: the same replayed trading day, run by two
**sides** on two clones of the bed, and everything either side wrote compared. Run from the
repository root with the system `python3` (each side runs in its own interpreter or binary):

```bash
# the full chain on a day the bed does not have yet; Python vs Rust (the default sides)
python3 tools/parity/nightly_diff.py --chain --scenario fresh --report /tmp/nightly-fresh.txt
# one step: Python's chain primes everything before it, both sides run just this step
python3 tools/parity/nightly_diff.py --step compute_factors --scenario fresh
# the harness against itself (determinism and masks): Python vs Python
python3 tools/parity/nightly_diff.py --chain --scenario rerun --sides py,py
```

| flag | meaning |
|---|---|
| `--chain` / `--step NAME` | the whole `nightly --date D`, or one step's block into a primed open run |
| `--scenario` | `fresh` (the day's chain output deleted from the bed), `rerun` (the bed as is: house rule 7), `gate-refuse`, `non-trading-day`, `provider-failure-midway`, and the index lane's review variants `index-edges` (per-universe failures, reconstruction, SW16 refusals, a new index, an unnamed row, P/E re-pricing) and `fundamentals-refused` |
| `--mutate FILE` | a SQL file applied identically to both clones after the scenario (the compute and sleeves reviews' edge cases) |
| `--date` | the replay day (default `2026-10-29`, the bed's last nightly, run 103) |
| `--sides` | two of `py`/`rs` (default `py,rs`) |
| `--profile` | `debug` (default) or `release` for the Rust binary; `--no-build` skips cargo |
| `--redis-db-a/-b` | each side's Redis index (default 11, 12; never 0) |
| `--smtp-port` | the mail sink both sides deliver to (default 18399) |
| `--max-dbs` | the database budget besides `baskfy` (default 5) |
| `--keep-dbs` | keep the run's two clones for a look (drop them yourself) |
| `--drop-cache` | drop every cached `rsn_tpl_*` / `rsn_primed_*` template and exit |

**What a side is.** `py` runs `nightly_py.py`: the production worker (`nightly_pipeline`, the
Celery task's own body) with the **replay provider** injected at process start — it swaps
`baskfy_worker.providers.build_provider_stack` for `replay_provider.replay_stack(bundle)`, turns
`kite_session_cli.refresh_quietly` into a no-op, and arms a socket guard that refuses anything but
loopback. Production Python is not changed. `rs` runs `rust/target/<profile>/baskfy-worker` with
`--replay-bundle`, which puts the Rust replay adapter behind the production `CompositeProvider` and
arms the network block. Step mode runs `step NAME --run-id N --context ctx.json` on both; the
Python half mirrors `_run_chain` block by block, pinned to the orchestrator's source by
`nightly_py.ORCHESTRATOR_SHA256` (it refuses to run when the orchestrator changes).

**Compared** — every one must be EQUAL: the side's payload (the task's return value or the raised
error; `run_id` masked), every table either side touched (TimescaleDB chunks mapped to their
hypertable) by an order-free digest in 1024 key buckets with the differing rows printed,
`pipeline_run` and `pipeline_run_step` in full (ids as ranks; `started_at`/`finished_at` as
`<before>`/`<during>` the side's start; `duration_ms` as `<set>`; an error's `traceback` as
`<set>`), every sequence, each side's Redis index, and every mail (`raised_at` masked). Any other
mask lives in `TABLE_MASKS` with its reason, and the report prints it.

**Refusals.** Only `127.0.0.1:55432` (`baskfy-rs-pg`); only databases named `rsn_*`; never
`baskfy` (a clone source only, read for templates and bundles); never Redis index 0. One heavy run
at a time machine-wide (`fcntl` on `rust/target/nightly_diff.lock`); at most five databases
besides `baskfy` (the harness evicts its own cached templates first, then waits). The run's clones
are dropped on exit, error and SIGINT included; cached templates (`rsn_tpl_<kind>_<date>`, and
`rsn_primed_<step>_<hash>` keyed by the bundle and every Python source the prime ran) persist and
are reused.

### The replay provider and its bundle

`replay_build.py --date D` derives the day's bundles from the bed (read-only) into
`rust/target/replay/<D>/<variant>/` (untracked): `base`, `gate-refuse` (two symbols in five, so the
gate's bar count fails), `provider-failure-midway` (`index_snapshots(on=D)` refused). The format
(`manifest.json` plus one JSON file per call under `calls/`) and what each call is rebuilt from
are in `replay_build.py`'s docstring and `rust/crates/baskfy-providers/src/replay.rs`. A call the
bundle cannot answer is `ProviderUnavailable("no recorded answer for <call>")` in both adapters;
the manifest's `refuse` list raises any provider error class for a matching call.

`replay_selfcheck.py <bundle>` proves the two adapters agree: 22 calls (every method, answered
and refused, plus the edges) through the Python adapter and through the Rust one
(`cargo run -p baskfy-providers --example replay_probe`), compared value for value.

### The desk's `.env` and step 11 (DECISIONS-RS R7.14)

`refresh_basket` imports the desk's `app.config`, whose `load_dotenv()` walks up from
`kite-momentum-rebalancer/app` and finds the desk's real `.env` whatever the side's cwd is.
python-dotenv never overrides a key already in the environment, so `TEST_ENV` pins every key that
file carries (`FULLY_INVESTED=false`, the Kite keys empty, `DRY_RUN=true`, ...): no real secret
reaches a side, and both sides size the basket with the desk's default cash bands. The Rust step
reads `FULLY_INVESTED` the same way (environment first, then that `.env`'s one line).

### The catch-up decision: `catch_up_diff.py`

```bash
python3 tools/parity/catch_up_diff.py --report /tmp/cu.txt
```

Asks Python's `catch_up.unlanded_sessions(through=today, lookback_days=7, now=...)` and
`baskfy-worker catch-up --now ...` the same question on one clone of the bed (`rsn_run_catchup`,
dropped on exit), at pinned IST moments inside a session, a second before
`SESSION_DATA_READY_IST` (18:00), at it and after it; first on the bed as it is, then with
2026-10-29's run deleted. Both sides only read (Rust's transaction is rolled back). Today is
never proposed before 18:00 IST. Running the nightly for each proposed day is `nightly_diff.py`'s
job; the Celery task's swing EOD and holiday demotion are not ported (R-gate.4).

## The worker's tasks: `task_diff.py` (wave 6; DECISIONS-RS R8.1–R8.3)

`nightly_diff.py` generalised to any Celery task: one case = a task, its message arguments and a
fixed **IST moment**; two fresh clones of an `rst_tpl_*` template per case; the two sides one after
the other; everything either side did compared.

```bash
# the foundation's own cases (the wave-5 tasks it wires), Python vs Rust
python3 tools/parity/task_diff.py --cases tools/parity/task_cases/pipeline.json --report /tmp/t.txt
# the harness against itself
python3 tools/parity/task_diff.py --cases tools/parity/task_cases/pipeline.json --sides py,py
# one case
python3 tools/parity/task_diff.py --cases tools/parity/task_cases/fno.json --case ingest-bhavcopy-recorded-day
```

| flag | meaning |
|---|---|
| `--cases FILE` / `--case NAME` | the lane's case file; repeat `--case` to run a subset |
| `--sides` | two of `py`/`rs` (default `py,rs`) |
| `--profile` / `--no-build` | the Rust binary (`debug`), or skip cargo |
| `--redis-db-a/-b` | Redis indices, 1–7 only (default 6, 7) |
| `--smtp-port` / `--upstream-port` | 18800–18999 only (default 18899, 18898), checked under the lock |
| `--max-dbs` | the database budget besides `baskfy`, shared with every harness on the bed (6) |
| `--keep-dbs` | keep each case's two clones (drop them yourself) |

**A side.** `py` is `task_py.py run NAME --args JSON --now T`: `app.tasks[NAME](*args, **kwargs)`,
once, with `nightly_py`'s injection (network guard, replay provider, `refresh_quietly` a no-op), the
Kite root pointed at the replay upstream, and the **injected clock** in the production modules only
(their `datetime` module reference re-pointed; libraries keep the real one), advancing in whole
seconds as the Rust side's. `rs` is
`baskfy-worker task NAME --args JSON --now T --replay-bundle DIR --replay-upstream URL`. Exit 0
returned, 75 a retryable raise, 1 any other raise, 2 usage.

**Case fields.** `name`, `task`, `args`, `now` (required); `scenario` (`rerun`, `fresh`), `date`
(bundle day, default 2026-10-29), `bundle` (`base`, `session`), `kite`, `seed_sql` (applied to both
clones; fixed timestamps), `env` (`{archive}` in a value is the side's raw archive directory, so
files a task writes under another setting are compared with the archive's), `mask` (`{table: {column: "stamp"|"elapsed"}}`), `payload_mask`
(JSON pointers), `redis_presence` (key regexes), `mail_mask` (regexes masked in each mail, beside `raised_at`;
lane twt_jobs — the evening digest's uuid `plan_id`), `note`.

**Compared.** The payload (exit code; JSON with key order, or the raised class and message); every
touched table (digest) and the run tables; sequences; Redis (Celery ids, `origin`, delivery tags
normalised, `eta`/`expires` as `<set>`; every enqueued message listed); mail (`raised_at`); the
upstream requests; write verbs at the upstream (law 2); the raw archive's new files.

### The replay upstream and the `session` bundle

`replay_build.py --date D --variant session` writes `base`'s calls plus `upstream/`
(`replay_build_session.py`): `archive/nse/fo-bhavcopy/*.zip` and `fo-ban-list/*.csv` (real raw
files, the last five on or before D, from the AWS-exit archive tarball, extracted once into
`rust/target/replay/sources/`), `kite/instruments/NFO.csv` (the bed's `op_contract` options plus
futures rebuilt from the newest F&O bhavcopy), `kite/quote/{0935,1315,1529}.json` (the recorded
chain and index minutes; equities synthesized from the day's bar), `kite/historical/<token>/minute.json`
(the recorded index minutes), `kite/holdings.json` (the bed's broker-linked holdings),
`margins.json`, `trades.json`, `profile.json` (synthetic). `replay_upstream.py`
serves `kite/` and `http/` on a loopback port and refuses every write verb; each side's
`BASKFY_RAW_ARCHIVE_DIR` starts as a copy of `archive/`, so the archive-first NSE adapter parses the
recorded file without a request. Anything not recorded answers 404.

`task_registry.py` (run from `decile-blueprint` with the usual `PYTHONPATH`) rewrites
`rust/crates/baskfy-worker/tests/fixtures/python_task_registry.json` from the live `celery_app`;
`tests/task_registry.rs` holds the Rust registry and Beat table to it.

### The TWT backtest: `twt_backtest_diff.py` (lane `sleeve_backtests`, R8.sb1)

`baskfy_worker.tasks.twt_backtest.run_twt_backtest` has no Celery task — `tools/twt/backtest.py`
(`make twt-backtest`) is its caller — so `task_diff` cannot name it. `twt_backtest_diff.py` is
`task_diff` with one seam replaced: a side runs the script's `_run` body (`py`, this file's `side`
subcommand under `task_py`'s clock) or `rust/crates/baskfy-worker/examples/twt_backtest.rs`
(`rs`); clones, lock, budget, ports and every comparison are `task_diff`'s own code. Cases:
`task_cases/sleeve_backtests_twt.json` (`args`: `start`, `end`, `sleeve`, `skip_etf_denominator`).
`TWT_BACKTEST_DIFF_KEEP=DIR` keeps each side's full stdout (the payload is printed truncated).

```bash
python3 tools/parity/twt_backtest_diff.py --smtp-port 18850 --upstream-port 18851 --report /tmp/twt-bt.txt
```
