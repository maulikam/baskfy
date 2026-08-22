"""The PORTFOLIO exposure overlay — R1-R4 tiers and the sleeve allocation solver.

Moved from the desk (`app/core/regime.py`, `app/core/regime_alloc.py`) to core at M15 (P3.3).
Both modules were **already written to core's first law** — their own docstring says so: "no Kite
calls, no SQLite, no clock reads, no config imports. Everything it needs arrives as arguments, so
live evaluation and historical replay call the SAME functions and, given identical inputs, produce
byte-identical decisions." That property is why the move is a `git mv` and an import rewrite.

NOT to be confused with :mod:`baskfy_core.instrument_regime`, which labels ONE INSTRUMENT
bull/bear/neutral and decides nothing. This decides how much money is deployed.
`packages/core/tests/test_regime_names_do_not_collide.py` keeps them apart.
"""

from baskfy_core.exposure import allocation, regime
