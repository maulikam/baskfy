# frozen/strangle — the options lab, parked

**Status: frozen at M6, 22 Aug 2026. Not dead, not deleted, not maintained.**
`CLAUDE.md` D4 decided this and the safety rails say it plainly: **`frozen/` is untouched — no
refactors, no deletions, no lint fixes.** If a sweep across the repository would change a file
under here, exclude the directory instead of changing the file.

## What this is

4,347 lines of a paper-only intraday options programme for NSE index strangles — three
underlyings (NIFTY, BANKNIFTY, SENSEX), each with its own config, journal, lockout, straddle
record and session lock. It has its own trading calendar, ATM band calibration, entry-window
clock, fill simulation against real depth, adjustment logic and attribution. Live execution sits
behind seven locks in `app/strategies/strangle/live.py`, and every one of them is shut.

It works. It is frozen because it is **not part of the merge's thesis**. Baskfy is
screen → rank → construct → size → execute → stop → rebalance in cash equities; an F&O programme
multiplies the compliance surface — F&O, the algo framework, and eventually third parties — at
exactly the moment that surface is already expanding (`docs/03` §4, `docs/06` D4).

## What is still running, and where

**The live Mumbai box is unaffected by this move.** M6 was a repository change only:

- `kite-momentum-rebalancer/deploy/systemd/strangle-collect@.{service,timer}` were **deliberately
  left in place**. The box runs the pre-freeze layout until its next deliberate deploy.
- `kite-momentum-rebalancer/data/outputs/strangle_*` — the straddle records, journals and
  lockouts — were **not moved**. That is the observation series, it is not rebuildable from
  anything, and `docs/02` §6 counts it among the data that cannot be reconstructed.

Those collectors are **operations, not repository state** (D4). When the box is next deployed
from this tree the units will point at scripts that have moved; decide then whether to thaw, to
repoint them at `frozen/strangle/`, or to retire them.

## How it was disconnected

Three modules imported the subsystem at **module scope**, which would have made the equity desk
fail to boot without it. Each was deferred behind the existing `OPTIONS_ENABLED` gate rather than
deleted, so a thaw needs no edits to the live tree:

| Site | Was | Now |
|---|---|---|
| `app/analytics/ops.py` | `from ..strategies.strangle import instruments` at import | `_options_operations()` — returns `()` unless `OPTIONS_ENABLED`, and reports a missing subsystem once instead of raising |
| `app/analytics/autorun.py` | `from ..strategies.strangle.clock import OPTIONS_OPEN` at import | imported at its single use site; the options loop in `needed()` is gated |
| `app/main.py` | three `/options*` routes importing `options_view` | `_options_view()` — **404** when the gate is off or the subsystem is absent |

`ops.OPERATIONS` is now the equity operations only; `ops.all_operations()`, `ops.by_name()` and
`ops.groups()` add the options controls when they are actually available.

## Thawing it

```
git mv frozen/strangle/kite-momentum-rebalancer/app/strategies/strangle \
       kite-momentum-rebalancer/app/strategies/strangle
# ...and the same for the other paths below; the layout under frozen/strangle/ mirrors
# the desk's exactly, so every move is a straight reverse.
```

Then set `OPTIONS_ENABLED=true`. Nothing else changes: the gates find the modules and the
controls reappear. Restore `config/strangle*.yaml` too, or the config loader will not find its
bands.

## What is under here

```
kite-momentum-rebalancer/
  app/strategies/strangle/     the package (20 modules)
  app/strategies/options*.py   the five defined-risk planners
  app/analytics/options_view.py
  app/templates/options.html
  scripts/                     strangle.py, strangle_calibrate.py, options_ab.py,
                               options_research.py, and the two launchd examples
  config/                      strangle.yaml, strangle_banknifty.yaml, strangle_sensex.yaml
  tests/                       12 suites
```

## One test still fails under here, and it is expected

`tests/test_strangle_runtime.py::test_both_plists_are_valid_and_point_at_this_checkout` asserts a
committed plist's `WorkingDirectory` equals `os.getcwd()`. The plists hardcode
`~/Documents/portfolio/kite-momentum-rebalancer`, a path this checkout no longer has. It was the
M0 baseline's single failure and it is a relocation artifact, not a defect in the strategy. It is
not fixed because fixing it would mean editing a frozen tree. If you thaw, fix it then.
