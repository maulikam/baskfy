"""Market infrastructure that the options lab happened to be built around.

THE OPTIONS LAB IS FROZEN. Sixteen of this package's twenty modules — the strategy itself:
book, rules, selection, sizing, levels, fills, adjustment, attribution, allocation, live,
session, journal, state, calibrate, market and fills_live — moved to `frozen/strangle/` at
M6 (`CLAUDE.md` D4). Four did not, and this file exists to say why.

`calendar_nse`, `clock`, `instruments` and `config` are not options strategy. They are the
NSE trading calendar, the session clock, the index registry and the YAML loader that reads
`config/strangle*.yaml`. **The equity desk depends on them**: `scripts/autorun.py` answers
"is today a trading day" from `calendar_nse.build_from_kite`, seeded with the default
underlying's index token and the `session.extra_holidays` list — and autorun runs on every
login, collecting the day's snapshot, fills and benchmarks, none of which can be backfilled
(`docs/02` §6). Freezing them would have made the equity desk's most load-bearing daily job
depend on an options lab that is deliberately absent.

They are dependency-free: `calendar_nse`, `clock` and `instruments` import nothing but the
standard library, and `config` imports only the first two plus PyYAML. Nothing here reaches
a strategy module, and nothing here can place an order.

The frozen tree keeps its own complete copy of the original package, including these four,
so thawing is a straight `git mv` of the sixteen back alongside them and needs no edits
here. See `frozen/strangle/README.md`.

**This is a seam, not a resting place.** The screener already has a real trading calendar
(`baskfy_core.trading_calendar`), and M15's move of the desk's brains into `packages/core`
is where these two should become one. Recorded as `docs/DECISIONS-MERGE.md` M6.2.
"""
