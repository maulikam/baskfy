# tools/parity — goldens for the Go rewrite

`golden.py` is the canonical dumper (see its docstring). Each lane adds `dump_L<n>.py` and runs it
with the tree's own interpreter, e.g.

```bash
cd decile-blueprint && .venv/bin/python -m tools.parity.dump_L1     # or: PYTHONPATH=.. .venv/bin/python ../tools/parity/dump_L1.py
cd kite-momentum-rebalancer && .venv/bin/python ../tools/parity/dump_L4.py
```

Goldens land in `go/testdata/golden/L<n>/…` and are committed. Dumpers import Python; they never
modify it, never use the network, never open the desk's live database or `portfolio.db`.
