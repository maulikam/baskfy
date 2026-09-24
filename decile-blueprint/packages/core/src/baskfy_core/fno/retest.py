"""The quarterly re-test (``docs/fno/04`` §6, FO9): every ``RESEARCH.md`` family, from the table.

Pure. The worker reads ``fo_contract_daily``; this module turns those rows into the research's
two panels exactly as ``evidence/research/panel.py`` did, runs each family through
:mod:`baskfy_core.fno.research` with the parameters of ``RESEARCH.md``'s tables, and returns one
:class:`FamilyResult` per family for ``fo_backtest_run``. Nothing here decides anything: a family
positive three runs in a row becomes a ``DECISIONS-FO`` entry *for Maulik* (``01`` §2), and no
flag moves.

**Slippage** (``04`` §6). The research charged an assumed slippage per option leg (0.5 % for the
index condor, 3 % for stock options). Where FO3's 15:00 sample has at least
``MIN_MEASURED_SESSIONS`` sessions for the names a family trades, the measured median half-spread
÷ mid replaces the assumption and the row says ``MEASURED``; otherwise ``ASSUMED``
(DECISIONS-FO FO9.2 says how a family's many names become one number). Futures families keep the
research's round-trip cost: FO3 samples options only.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

import polars as pl

from baskfy_core.fno import research as r

#: ``07`` §4's Tier 2E caveat, verbatim. Every row and every card carries it (``07`` §4).
TIER_2E_CAVEAT: Final = (
    "End-of-day closes, not fills. Slippage is an assumed 3 % of premium per leg per crossing "
    "(0.03 % for futures), not measured. `n` legs were modelled because they did not trade."
)
#: The same caveat when FO3's measured spreads replaced the assumption.
TIER_2E_CAVEAT_MEASURED: Final = (
    "End-of-day closes, not fills. Slippage is the measured 15:00 median half-spread of the "
    "names traded (FO3), not a fill. `n` legs were modelled because they did not trade."
)
#: ``04`` §6: the measured number replaces the assumption at 20 sessions.
MIN_MEASURED_SESSIONS: Final = 20
#: The stock option families need this many sampled names before a median stands for them.
MIN_MEASURED_NAMES: Final = 20
#: ``04`` §6: the re-test runs in these months.
RETEST_MONTHS: Final = frozenset({1, 4, 7, 10})

_FUTURES_COLUMNS: Final = (
    "date",
    "instrument",
    "symbol",
    "expiry",
    "open",
    "high",
    "low",
    "close",
    "settle",
    "underlying",
    "open_interest",
    "oi_change",
    "volume",
    "turnover",
    "lot_size",
)
_OPTION_COLUMNS: Final = (
    "date",
    "instrument",
    "symbol",
    "expiry",
    "strike",
    "option_type",
    "close",
    "settle",
    "open_interest",
    "volume",
)


class SlippageSource(StrEnum):
    ASSUMED = "ASSUMED"
    MEASURED = "MEASURED"


class Scope(StrEnum):
    """What a family trades, which decides the panel it needs and whose spreads apply."""

    INDEX_OPTIONS = "INDEX_OPTIONS"
    STOCK_OPTIONS = "STOCK_OPTIONS"
    FUTURES = "FUTURES"
    BASIS = "BASIS"


@dataclass(frozen=True, slots=True)
class Family:
    """One row of ``RESEARCH.md``'s tables: its key, what it is, and how it was run."""

    key: str
    label: str
    scope: Scope
    condor: r.CondorParams | None = None
    futures: tuple[r.FuturesFamily, r.FuturesParams] | None = None
    debit: r.DebitStructure | None = None


_STOCK = r.CondorParams(slip_pct=0.03)

#: Every family ``RESEARCH.md`` reports, with the parameters of its table (the scripts' calls).
FAMILIES: Final[tuple[Family, ...]] = (
    Family("B4", "Index monthly iron condor, N = 15", Scope.INDEX_OPTIONS, condor=r.B4),
    Family(
        "B4_LOSS_CLOSE",
        "B4 with the 1.5x-credit loss close (F1, M.1)",
        Scope.INDEX_OPTIONS,
        condor=r.B4_LOSS_CLOSE,
    ),
    Family(
        "B1",
        "Stock iron condors, top 30, N = 10",
        Scope.STOCK_OPTIONS,
        condor=replace(_STOCK, n_before=10, top_n=30),
    ),
    Family(
        "B2",
        "Stock credit spreads on the trend's side, top 60, N = 15, 0.5 sigma",
        Scope.STOCK_OPTIONS,
        condor=replace(
            _STOCK,
            n_before=15,
            k_short=0.5,
            k_wing=0.5,
            top_n=60,
            mode=r.CondorMode.TREND_CREDIT,
        ),
    ),
    Family(
        "B3",
        "Stock iron condors exiting at E-4, top 30, N = 15",
        Scope.STOCK_OPTIONS,
        condor=replace(_STOCK, n_before=15, exit_n=4, top_n=30),
    ),
    Family(
        "C1",
        "Debit spreads on the breakout signal, 10 sessions",
        Scope.STOCK_OPTIONS,
        debit=r.DebitStructure.SPREAD,
    ),
    Family(
        "C2",
        "Long ATM option on the breakout signal, 10 sessions",
        Scope.STOCK_OPTIONS,
        debit=r.DebitStructure.LONG,
    ),
    Family(
        "F2",
        "Stock-futures breakout, long only, 3 ATR chandelier, 40 sessions, rolls costed (F2)",
        Scope.FUTURES,
        futures=(r.FuturesFamily.A0_LONG, r.F2_SPEC),
    ),
    Family(
        "A0",
        "Stock-futures breakout aligned, no OI filter, both sides, 3 ATR chandelier, 40 sessions",
        Scope.FUTURES,
        futures=(r.FuturesFamily.A0, r.FuturesParams(horizon=40, k_atr=3.0, trail=True)),
    ),
    Family(
        "A1",
        "Stock-futures breakout with OI rising, 10 sessions, 2 ATR",
        Scope.FUTURES,
        futures=(r.FuturesFamily.A1, r.FuturesParams(horizon=10, k_atr=2.0)),
    ),
    Family(
        "A1R",
        "A1 aligned with NIFTY's regime, 10 sessions, 2 ATR",
        Scope.FUTURES,
        futures=(r.FuturesFamily.A1R, r.FuturesParams(horizon=10, k_atr=2.0)),
    ),
    Family(
        "A3",
        "One-day long/short buildup, 5 sessions, 2 ATR",
        Scope.FUTURES,
        futures=(r.FuturesFamily.A3, r.FuturesParams(horizon=5, k_atr=2.0)),
    ),
    Family(
        "A2",
        "Weekly cross-sectional momentum, top/bottom 10, 5 sessions, 3 ATR",
        Scope.FUTURES,
        futures=(r.FuturesFamily.A2, r.FuturesParams(horizon=5, k_atr=3.0)),
    ),
    Family(
        "A4",
        "Index futures 20-day breakout with 50-day average, 20 sessions, 2 ATR",
        Scope.FUTURES,
        futures=(r.FuturesFamily.A4, r.FuturesParams(horizon=20, k_atr=2.0)),
    ),
    Family("E", "Cash-futures carry (front month, >= 5 days to expiry)", Scope.BASIS),
)
FAMILY_KEYS: Final = frozenset(f.key for f in FAMILIES)


@dataclass(frozen=True, slots=True)
class Panels:
    """``panel.py``'s two files, from ``fo_contract_daily`` rows."""

    futures: pl.DataFrame
    options: pl.DataFrame


#: ``symbols -> options panel rows`` for those symbols (``panel.py``'s columns). The worker pages
#: them out of Postgres; a test slices an in-memory panel.
OptionsLoader = Callable[[frozenset[str]], pl.DataFrame]

#: Stock option families run this many symbols at a time (DECISIONS-FO FO9.3). Every family's
#: state is per symbol (a condor per (symbol, expiry), a debit trade per symbol's busy window,
#: the liquidity rank from the whole continuous series), so a batch gives exactly the trades the
#: whole panel gives, at a tenth of the memory: B1 over the whole panel peaks at 2.8 GB, which a
#: 7.8 GB box with no swap cannot spare.
SYMBOL_BATCH: Final = 20


def empty_options() -> pl.DataFrame:
    """An options panel with no rows and ``panel.py``'s columns (a batch with nothing stored)."""
    return pl.DataFrame(
        schema={
            "date": pl.Date,
            "instrument": pl.String,
            "symbol": pl.String,
            "expiry": pl.Date,
            "strike": pl.Float64,
            "option_type": pl.String,
            "close": pl.Float64,
            "settle": pl.Float64,
            "open_interest": pl.Int64,
            "volume": pl.Int64,
        }
    )


def slice_loader(options: pl.DataFrame) -> OptionsLoader:
    """An :data:`OptionsLoader` over an in-memory options panel."""

    def load(symbols: frozenset[str]) -> pl.DataFrame:
        return options.filter(pl.col("symbol").is_in(sorted(symbols)))

    return load


def panels_from_contracts(contracts: pl.DataFrame) -> Panels:
    """``evidence/research/panel.py`` over ``fo_contract_daily`` (or the day files) rows.

    ``contracts`` carries the table's columns; ``trade_date`` or the files' ``date`` both work.
    Futures: every FUTSTK/FUTIDX row. Options: OPTSTK, and OPTIDX on NIFTY/BANKNIFTY, of the two
    nearest expiries that have a future that day, with non-zero OI or volume.
    """
    frame = (
        contracts.rename({"trade_date": "date"})
        if "trade_date" in contracts.columns
        else (contracts)
    )
    is_future = pl.col("instrument").is_in(["FUTSTK", "FUTIDX"])
    futures = frame.filter(is_future).select(_FUTURES_COLUMNS)
    near2 = (
        futures.select("date", "symbol", "expiry")
        .unique()
        .sort("expiry")
        .group_by("date", "symbol", maintain_order=True)
        .head(2)
    )
    options = (
        frame.filter(
            (pl.col("instrument") == "OPTSTK")
            | (
                (pl.col("instrument") == "OPTIDX")
                & pl.col("symbol").is_in(sorted(r.INDEX_UNDERLYINGS))
            )
        )
        .join(near2, on=["date", "symbol", "expiry"], how="inner")
        .filter((pl.col("open_interest") > 0) | (pl.col("volume") > 0))
        .select(_OPTION_COLUMNS)
    )
    return Panels(futures.sort("date", "symbol", "expiry"), options)


@dataclass(frozen=True, slots=True)
class Slippage:
    """The per-leg slippage a family ran at, and where it came from."""

    pct: float | None
    source: SlippageSource
    names: int


def family_slippage(family: Family, measured: Mapping[str, tuple[float, int]]) -> Slippage:
    """``measured``: symbol -> (median half-spread ÷ mid, sessions) from FO3.

    Index families take the **worse** of NIFTY's and BANKNIFTY's, and only when both have
    ``MIN_MEASURED_SESSIONS``. Stock option families take the median of the per-name medians
    once ``MIN_MEASURED_NAMES`` names have enough sessions. Futures and the basis study are
    always ``ASSUMED``: FO3 samples options only.
    """
    if family.scope is Scope.INDEX_OPTIONS:
        got = [measured.get(s) for s in sorted(r.INDEX_UNDERLYINGS)]
        ready = [v[0] for v in got if v is not None and v[1] >= MIN_MEASURED_SESSIONS]
        if len(ready) == len(r.INDEX_UNDERLYINGS):
            return Slippage(max(ready), SlippageSource.MEASURED, len(ready))
        return Slippage(None, SlippageSource.ASSUMED, 0)
    if family.scope is Scope.STOCK_OPTIONS:
        ready = [
            v[0]
            for s, v in measured.items()
            if s not in r.INDEX_UNDERLYINGS and v[1] >= MIN_MEASURED_SESSIONS
        ]
        if len(ready) >= MIN_MEASURED_NAMES:
            return Slippage(statistics.median(ready), SlippageSource.MEASURED, len(ready))
    return Slippage(None, SlippageSource.ASSUMED, 0)


@dataclass(frozen=True, slots=True)
class FamilyResult:
    """One ``fo_backtest_run`` row's content."""

    family: str
    label: str
    params: dict[str, object]
    caveat: str
    sample_from: dt.date
    sample_to: dt.date
    n: int
    net_r: float | None
    gross_r: float | None
    per_year: dict[str, object]
    slippage_source: SlippageSource


def _params(family: Family, slippage: Slippage) -> dict[str, object]:
    out: dict[str, object] = {"label": family.label, "scope": family.scope.value}
    if family.condor is not None:
        c = family.condor
        out |= {
            "n_before": c.n_before,
            "k_short": c.k_short,
            "k_wing": c.k_wing,
            "profit_take": c.profit_take,
            "exit_n": c.exit_n,
            "slip_pct": c.slip_pct if slippage.pct is None else slippage.pct,
            "top_n": c.top_n,
            "mode": c.mode.value,
            "loss_mult": c.loss_mult,
        }
    if family.futures is not None:
        sig, p = family.futures
        out |= {
            "signal": sig.value,
            "horizon": p.horizon,
            "k_atr": p.k_atr,
            "trail": p.trail,
            "charge_rolls": p.charge_rolls,
            "round_trip_cost": p.cost,
        }
    if family.debit is not None:
        out |= {"structure": family.debit.value, "horizon": 10, "slip_pct": 0.03}
    if slippage.source is SlippageSource.MEASURED:
        out["measured_names"] = slippage.names
    return out


def _per_year(summary: r.Summary) -> dict[str, object]:
    return {
        str(year): {"n": n, "exp_r": round(exp, 4)}
        for year, (n, exp) in sorted(summary.per_year.items())
    }


def run_family(  # noqa: PLR0913 - the family, its data, its spreads and two sharing knobs
    family: Family,
    futures: pl.DataFrame,
    load_options: OptionsLoader,
    measured: Mapping[str, tuple[float, int]] | None = None,
    *,
    cont: pl.DataFrame | None = None,
    batch: int = SYMBOL_BATCH,
) -> FamilyResult:
    """Run one family. ``cont`` may be passed to share one continuous series between families."""
    if futures.is_empty():
        raise ValueError("the re-test needs futures rows; the panel is empty")
    first, last = futures["date"].min(), futures["date"].max()
    if not isinstance(first, dt.date) or not isinstance(last, dt.date):
        raise TypeError("the panel's dates are not dates")
    slippage = family_slippage(family, measured or {})
    caveat = (
        TIER_2E_CAVEAT_MEASURED if slippage.source is SlippageSource.MEASURED else TIER_2E_CAVEAT
    )
    series = cont if cont is not None else r.continuous(futures)

    def result(
        n: int, net: float | None, gross: float | None, per_year: dict[str, object]
    ) -> FamilyResult:
        return FamilyResult(
            family=family.key,
            label=family.label,
            params=_params(family, slippage),
            caveat=caveat,
            sample_from=first,
            sample_to=last,
            n=n,
            # No trades is no expectancy, not an expectancy of zero.
            net_r=None if net is None or n == 0 else round(net, 4),
            gross_r=None if gross is None or n == 0 else round(gross, 4),
            per_year=per_year,
            slippage_source=slippage.source,
        )

    if family.scope is Scope.BASIS:
        if futures.filter(
            (pl.col("instrument") == "FUTSTK") & pl.col("underlying").is_not_null()
        ).is_empty():
            # Before 8 Jul 2024 the file carries no underlying price (``04`` §4): no basis at all.
            return result(0, None, None, {})
        study = r.basis_study(futures)
        return result(
            study.sessions,
            None,
            None,
            {
                "quantiles_ann": {str(q): round(v, 4) for q, v in study.quantiles.items()},
                "share_net_above_7pct": round(study.share_net_above_7pct, 4),
            },
        )
    if family.futures is not None:
        signal, params = family.futures
        panel = r.futures_panel(series)
        summary = r.summarise(r.simulate_futures(panel, r.futures_signals(panel, signal), params))
        return result(summary.n, summary.exp_r, None, _per_year(summary))
    universe = (
        r.INDEX_UNDERLYINGS
        if family.scope is Scope.INDEX_OPTIONS
        else r.liquid_symbols(series, family.condor.top_n if family.condor else 0)
    )
    ordered = sorted(universe)
    parts: list[pl.DataFrame] = []
    for i in range(0, len(ordered), max(batch, 1)):
        symbols = frozenset(ordered[i : i + max(batch, 1)])
        panel_o = r.build_option_panel(load_options(symbols), futures, series, symbols)
        parts.append(_trades(family, panel_o, slippage))
    traded = [p for p in parts if not p.is_empty()]
    # No trade in any batch (a sparse table) summarises to n = 0 rather than failing.
    trades = pl.concat(traded).sort("entry", "symbol") if traded else pl.DataFrame()
    # ``res_debit`` names its R column ``R_spread`` for both structures.
    summary = r.summarise(trades, r_column="R_spread" if family.debit is not None else "R")
    return result(summary.n, summary.exp_r, summary.gross_r, _per_year(summary))


def _trades(family: Family, panel: r.OptionPanel, slippage: Slippage) -> pl.DataFrame:
    if family.debit is not None:
        return r.debit_trades(panel, structure=family.debit)
    if family.condor is None:
        raise ValueError(f"family {family.key} has no parameters")
    params = (
        family.condor if slippage.pct is None else replace(family.condor, slip_pct=slippage.pct)
    )
    return r.condor_trades(panel, params).trades


def retest_due(today: dt.date, last_run: dt.date | None) -> bool:
    """``04`` §6: once in each of January, April, July and October."""
    if today.month not in RETEST_MONTHS:
        return False
    return last_run is None or (last_run.year, last_run.month) != (today.year, today.month)


#: A progress hook the worker may pass to hear each family finish (logging), never required.
Progress = Callable[[FamilyResult], None]


def run_all(
    futures: pl.DataFrame,
    load_options: OptionsLoader,
    measured: Mapping[str, tuple[float, int]] | None = None,
    *,
    families: tuple[Family, ...] = FAMILIES,
    progress: Progress | None = None,
) -> list[FamilyResult]:
    """Every family, one continuous series shared between them."""
    cont = r.continuous(futures)
    out: list[FamilyResult] = []
    for family in families:
        res = run_family(family, futures, load_options, measured, cont=cont)
        if progress is not None:
            progress(res)
        out.append(res)
    return out
