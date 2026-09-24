"""The ``RESEARCH.md`` families as pure functions of polars frames (``04`` §6; FO1, FO9).

A **faithful port** of ``docs/fno/evidence/research/`` — ``cont.py`` (:func:`continuous`),
``res_options.py`` (:func:`build_option_panel`, :func:`condor_trades`: B1, B2, B3, B4 and the
defensive-exit and loss-close variants), ``res_futures.py`` and ``res_f2.py``
(:func:`futures_panel`, :func:`futures_signals`, :func:`simulate_futures`: A0-A4 and F2),
``res_debit.py`` (:func:`debit_trades`: C1, C2) and ``res_basis.py`` (:func:`basis_study`: E).
Every expression, threshold and tie-break is the script's, including its quirks (a 6.5 %
discount rate inside the research's Black-76, ₹500 as the fallback lot, ``max(TOPN, 60)`` as the
liquid universe). **A change here is a change to the research, not a refactor**: FO9's golden
(B4 N=15 n=100 +0.033R, loss close 1.5 +0.022R, F2 n=2,334 +0.017R) must keep reproducing.

What differs is only the plumbing: module-level globals and environment variables became
parameters (:class:`CondorParams`), the files became frames the caller loads, and ``print``
became :class:`Summary`. The live rules of ``04`` live in ``condor``/``series``/``vol``; this
module deliberately does **not** call them, because the research did not.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import numpy.typing as npt
import polars as pl

FloatArray = npt.NDArray[np.float64]

#: ``res_options.R_RATE``: the research discounted its Black-76 at 6.5 % (``04`` §4's live IV
#: uses r = 0; the research is kept as it ran).
RESEARCH_RATE = 0.065
#: ``res_futures.COST``/``res_f2.COST``: 0.12 % of notional per futures round trip.
FUTURES_ROUND_TRIP_COST = 0.0012
INDEX_UNDERLYINGS: frozenset[str] = frozenset({"NIFTY", "BANKNIFTY"})


# --------------------------------------------------------------------------------------------
# cont.py
# --------------------------------------------------------------------------------------------


def continuous(futures: pl.DataFrame) -> pl.DataFrame:
    """``cont.py``: the continuous, tradeable front-month series per symbol (no look-ahead).

    ``futures`` is ``panel.py``'s ``futures.parquet`` (``date, instrument, symbol, expiry, open,
    high, low, close, settle, underlying, open_interest, oi_change, volume, turnover,
    lot_size``). Sessions with the held contract outside ``(0.7, 1.4)`` are dropped.
    """
    fut = futures.filter(pl.col("settle") > 0)
    dates = fut.select("date").unique().sort("date").with_row_index("di")
    fut = fut.join(dates, on="date")
    oi = fut.group_by("symbol", "date").agg(
        pl.col("open_interest").sum().alias("oi_total"),
        pl.col("turnover").sum().alias("fut_turnover"),
        pl.col("lot_size").max().alias("lot_size"),
        pl.col("underlying").max().alias("spot"),
        pl.col("instrument").first(),
    )
    front = (
        fut.filter(pl.col("expiry") > pl.col("date"))
        .sort("expiry")
        .group_by("symbol", "di")
        .agg(
            pl.col("expiry").first().alias("held_expiry"),
            pl.col("settle").first().alias("held_settle"),
        )
    )
    nxt = front.with_columns((pl.col("di") + 1).alias("di_next")).join(
        fut.select(
            "symbol",
            pl.col("di").alias("di_next"),
            pl.col("expiry").alias("held_expiry"),
            "open",
            "high",
            "low",
            "settle",
            "date",
        ),
        on=["symbol", "di_next", "held_expiry"],
        how="inner",
    )
    nxt = nxt.with_columns(
        (pl.col("settle") / pl.col("held_settle")).alias("gc"),
        (pl.col("open") / pl.col("held_settle")).alias("go"),
        (pl.col("high") / pl.col("held_settle")).alias("gh"),
        (pl.col("low") / pl.col("held_settle")).alias("gl"),
    ).select(
        "symbol",
        "date",
        pl.col("di_next").alias("di"),
        "gc",
        "go",
        "gh",
        "gl",
        pl.col("held_expiry").alias("expiry"),
    )
    nxt = nxt.filter((pl.col("gc") > 0.7) & (pl.col("gc") < 1.4))  # noqa: PLR2004 - cont.py
    nxt = nxt.with_columns(
        pl.when((pl.col("go") <= 0) | pl.col("go").is_null())
        .then(1.0)
        .otherwise(pl.col("go"))
        .alias("go"),
        pl.when((pl.col("gh") <= 0) | pl.col("gh").is_null())
        .then(pl.max_horizontal("gc", 1.0))
        .otherwise(pl.col("gh"))
        .alias("gh"),
        pl.when((pl.col("gl") <= 0) | pl.col("gl").is_null())
        .then(pl.min_horizontal("gc", 1.0))
        .otherwise(pl.col("gl"))
        .alias("gl"),
    )
    c = (
        nxt.sort("symbol", "di")
        .with_columns(pl.col("gc").log().cum_sum().over("symbol").exp().alias("L"))
        .with_columns((pl.col("L") / pl.col("gc")).alias("Lprev"))
        .with_columns(
            (pl.col("Lprev") * pl.col("go")).alias("o"),
            (pl.col("Lprev") * pl.col("gh")).alias("h"),
            (pl.col("Lprev") * pl.col("gl")).alias("l"),
            pl.col("L").alias("c"),
            pl.col("gc").log().alias("r"),
        )
    )
    c = c.join(oi, on=["symbol", "date"], how="left")
    return c.select(
        "symbol",
        "instrument",
        "date",
        "di",
        "expiry",
        "o",
        "h",
        "l",
        "c",
        "r",
        "oi_total",
        "fut_turnover",
        "lot_size",
        "spot",
    ).sort("symbol", "di")


# --------------------------------------------------------------------------------------------
# res_options.py — Black-76 as the research priced it
# --------------------------------------------------------------------------------------------


def _ncdf(x: FloatArray) -> FloatArray:
    erf = np.vectorize(math.erf)
    return np.asarray(0.5 * (1 + erf(x / math.sqrt(2))), dtype=np.float64)


def b76(  # noqa: PLR0913, PLR0917 - the research's signature
    forward: FloatArray,
    strike: FloatArray,
    years: FloatArray,
    vol: FloatArray,
    kind: npt.NDArray[np.str_],
    rate: float = RESEARCH_RATE,
) -> FloatArray:
    """``res_options.b76``, vectorised exactly as the script ran it."""
    vol = np.maximum(vol, 1e-4)
    years = np.maximum(years, 1e-6)
    d1 = (np.log(forward / strike) + 0.5 * vol * vol * years) / (vol * np.sqrt(years))
    d2 = d1 - vol * np.sqrt(years)
    df = math.exp(-rate * 0) * np.exp(-rate * years)
    call = df * (forward * _ncdf(d1) - strike * _ncdf(d2))
    put = df * (strike * _ncdf(-d2) - forward * _ncdf(-d1))
    return np.asarray(np.where(kind == "CE", call, put), dtype=np.float64)


def implied(  # noqa: PLR0913, PLR0917 - the research's signature
    forward: FloatArray,
    strike: FloatArray,
    years: FloatArray,
    price: FloatArray,
    kind: npt.NDArray[np.str_],
    rate: float = RESEARCH_RATE,
) -> FloatArray:
    """``res_options.implied``: 50 bisections on ``[0.01, 3.0]``."""
    lo, hi = np.full_like(price, 0.01), np.full_like(price, 3.0)
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        p = b76(forward, strike, years, mid, kind, rate)
        hi = np.where(p > price, mid, hi)
        lo = np.where(p > price, lo, mid)
    return np.asarray(0.5 * (lo + hi), dtype=np.float64)


def _b76_one(  # noqa: PLR0913, PLR0917 - one leg
    forward: float, strike: float, years: float, vol: float, kind: str, rate: float
) -> float:
    return float(
        b76(
            np.array([forward]),
            np.array([strike]),
            np.array([years]),
            np.array([vol]),
            np.array([kind]),
            rate,
        )[0]
    )


# --------------------------------------------------------------------------------------------
# res_options.py — the panel (the script's module-level state, as a value)
# --------------------------------------------------------------------------------------------

_Chain = dict[tuple[float, str], tuple[float | None, int | None]]


@dataclass(frozen=True, slots=True)
class OptionPanel:
    """``res_options``'s ``GROUPS``, ``FGROUPS``, ``lots``, ``dates``, ``didx`` and ``rv``."""

    groups: dict[tuple[str, dt.date], dict[dt.date, _Chain]]
    fgroups: dict[tuple[str, dt.date, dt.date], float]
    lots: dict[str, float]
    dates: tuple[dt.date, ...]
    didx: dict[dt.date, int]
    rv20: dict[tuple[str, dt.date], float | None]
    cont: pl.DataFrame

    def trading_days_before(self, expiry: dt.date, n: int) -> dt.date | None:
        """The session ``n`` trading days before ``expiry`` (expiry itself is 0)."""
        if expiry not in self.didx:
            return None
        i = self.didx[expiry] - n
        return self.dates[i] if i >= 0 else None


def _liquidity_rank(cont: pl.DataFrame) -> pl.DataFrame:
    return (
        cont.filter(pl.col("instrument") == "FUTSTK")
        .sort("symbol", "di")
        .with_columns(pl.col("fut_turnover").rolling_median(20).over("symbol").alias("t20"))
        .with_columns(pl.col("t20").rank(descending=True).over("date").alias("lr"))
    )


def liquid_symbols(cont: pl.DataFrame, top_n: int) -> frozenset[str]:
    """``load_groups(None)``'s universe: every stock ever in the top ``max(top_n, 60)``."""
    liq = _liquidity_rank(cont)
    return frozenset(liq.filter(pl.col("lr") <= max(top_n, 60))["symbol"].unique().to_list())


def build_option_panel(
    options: pl.DataFrame,
    futures: pl.DataFrame,
    cont: pl.DataFrame,
    symbols: frozenset[str],
) -> OptionPanel:
    """``res_options``'s module state plus ``load_groups(symbols)``.

    ``options`` is ``options.parquet`` (the caller may pre-filter it to ``symbols`` to bound
    memory; the filter here is the script's), ``futures`` is ``futures.parquet`` and ``cont``
    is :func:`continuous`'s output.
    """
    opt = options.filter(
        (pl.col("date") >= pl.col("expiry") - pl.duration(days=32))
        & pl.col("symbol").is_in(sorted(symbols))
    )
    fut = futures.filter(pl.col("settle") > 0)
    lots_frame = (
        fut.filter(pl.col("lot_size").is_not_null())
        .group_by("symbol")
        .agg(pl.col("lot_size").median())
    )
    lots = {str(s): float(v) for s, v in lots_frame.iter_rows() if v is not None}
    dates = tuple(sorted(fut["date"].unique().to_list()))
    didx = {d: i for i, d in enumerate(dates)}
    rv = (
        cont.sort("symbol", "di")
        .with_columns((pl.col("r").rolling_std(20).over("symbol") * math.sqrt(252)).alias("rv20"))
        .select("symbol", "date", "rv20")
    )
    rv20 = {(s, d): v for s, d, v in rv.iter_rows()}
    groups: dict[tuple[str, dt.date], dict[dt.date, _Chain]] = {}
    for key, g in opt.partition_by(["symbol", "expiry"], as_dict=True).items():
        by_day: dict[dt.date, _Chain] = {}
        for day_key, gd in g.partition_by("date", as_dict=True).items():
            by_day[_as_date(day_key[0])] = {
                (float(r[0]), str(r[1])): (r[2], r[3])
                for r in gd.select("strike", "option_type", "close", "volume").iter_rows()
            }
        groups[(str(key[0]), _as_date(key[1]))] = by_day
    fgroups = {
        (s, e, d): f for s, e, d, f in fut.select("symbol", "expiry", "date", "settle").iter_rows()
    }
    return OptionPanel(groups, fgroups, lots, dates, didx, rv20, cont)


def _as_date(value: object) -> dt.date:
    if not isinstance(value, dt.date):
        raise TypeError(f"expected a date, got {type(value).__name__}")
    return value


class CondorMode(StrEnum):
    CONDOR = "condor"
    TREND_CREDIT = "trend_credit"


@dataclass(frozen=True, slots=True)
class CondorParams:
    """``condor_trades``'s arguments plus the script's env knobs (``SLIP_PCT``, ``TOPN``,
    ``MODE``, ``DEFEND``, ``LOSS_MULT``)."""

    n_before: int = 15
    k_short: float = 1.0
    k_wing: float = 0.5
    profit_take: float = 0.5
    exit_n: int = 1
    slip_pct: float = 0.03
    slip_min: float = 0.05
    top_n: int = 0
    mode: CondorMode = CondorMode.CONDOR
    defend: bool = False
    loss_mult: float = 0.0
    symbols: frozenset[str] | None = None


#: B4 as ``RESEARCH.md`` reports it: NIFTY + BANKNIFTY monthlies, N = 15, 0.5 % slippage.
B4 = CondorParams(n_before=15, slip_pct=0.005, symbols=INDEX_UNDERLYINGS)
#: B4 with Maulik's loss close (M.1).
B4_LOSS_CLOSE = CondorParams(n_before=15, slip_pct=0.005, loss_mult=1.5, symbols=INDEX_UNDERLYINGS)


def leg_costs(price: float, sell: bool, lot: float, params: CondorParams) -> float:
    """``res_options.leg_costs``: slippage + STT on sells + GST on brokerage and exchange."""
    slip = max(params.slip_min, params.slip_pct * price)
    brokerage = 20.0 / lot
    stt = 0.0015 * price if sell else 0.0
    exch = 0.000355 * price
    return slip + stt + 1.18 * (brokerage + exch)


def _trend_map(cont: pl.DataFrame) -> dict[tuple[str, dt.date], int]:
    nif = (
        cont.filter(pl.col("symbol") == "NIFTY")
        .sort("di")
        .with_columns((pl.col("c") > pl.col("c").rolling_mean(50)).alias("up"))
        .select("date", "up")
    )
    nifd: dict[dt.date, bool | None] = dict(nif.iter_rows())
    tr = cont.sort("symbol", "di").with_columns(
        pl.col("c").rolling_mean(50).over("symbol").alias("ma50")
    )
    trend: dict[tuple[str, dt.date], int] = {}
    for s_, d_, c_, m_ in tr.select("symbol", "date", "c", "ma50").iter_rows():
        if m_ is None or nifd.get(d_) is None:
            continue
        up = bool(nifd[d_]) if s_ not in INDEX_UNDERLYINGS else (c_ > m_)
        trend[(s_, d_)] = 1 if (c_ > m_ and up) else (-1 if (c_ < m_ and not up) else 0)
    return trend


@dataclass(frozen=True, slots=True)
class CondorRun:
    trades: pl.DataFrame
    #: ``CA_SKIPPED``: cycles whose underlying moved outside (0.7, 1.4) in one session.
    ca_skipped: tuple[tuple[str, dt.date], ...]


_CONDOR_SCHEMA = [
    "symbol",
    "entry",
    "expiry",
    "exit",
    "iv",
    "rv20",
    "credit",
    "maxloss",
    "pnl",
    "R",
    "modelled_legs",
    "credit_pct",
    "cost_R",
]

_Leg = tuple[float, str, int]


@dataclass(slots=True)
class _Walk:
    exit_val: float | None = None
    exit_d: dt.date | None = None
    modelled: int = 0
    exit_legs: list[tuple[float, bool]] = field(default_factory=list)
    broken: bool = False


def _pick(strikes: FloatArray, target: float, side: int) -> float | None:
    cands = strikes[strikes >= target] if side > 0 else strikes[strikes <= target]
    if len(cands) == 0:
        return None
    return float(cands.min()) if side > 0 else float(cands.max())


def _walk(  # noqa: PLR0913, PLR0917 - the script's loop state, passed explicitly
    panel: OptionPanel,
    key: tuple[str, dt.date],
    d0: dt.date,
    dx: dt.date,
    legs: list[_Leg],
    iv: float,
    credit: float,
    widths: tuple[float, float, float, float],
    params: CondorParams,
) -> _Walk:
    """The day-by-day walk from ``d0`` to ``dx`` (profit take, loss close, defend, clamp)."""
    sym, e = key
    by_day = panel.groups[key]
    wc, sc, sp, wp = widths
    out = _Walk(exit_d=dx)
    f_prev = panel.fgroups[(sym, e, d0)]
    breached = False
    i0, ix = panel.didx[d0], panel.didx[dx]
    for i in range(i0 + 1, ix + 1):
        d = panel.dates[i]
        chd = by_day.get(d, {})
        fdv = panel.fgroups.get((sym, e, d))
        if fdv is None:
            continue
        if not (0.7 < fdv / f_prev < 1.4):  # noqa: PLR2004 - split/bonus, as the script
            out.broken = True
            break
        f_prev = fdv
        td = max((e - d).days, 0.5) / 365.0
        side_val = {"CE": 0.0, "PE": 0.0}
        mod = 0
        legpx: list[tuple[float, bool]] = []
        for k, cp, q in legs:
            r = chd.get((k, cp))
            if r is None or not r[1] or not r[0]:
                p = _b76_one(fdv, k, td, iv, cp, RESEARCH_RATE)
                mod += 1
            else:
                p = r[0]
            side_val[cp] += -q * p
            legpx.append((p, q > 0))
        val = min(max(side_val["CE"], 0.0), wc - sc) + min(max(side_val["PE"], 0.0), sp - wp)
        stopped = params.loss_mult > 0 and val >= (1 + params.loss_mult) * credit
        take = val <= (1 - params.profit_take) * credit
        if stopped or breached or take or i == ix:
            out.exit_val, out.exit_d, out.modelled, out.exit_legs = val, d, mod, legpx
            if stopped or breached or take:
                break
        if params.defend and (fdv >= sc or fdv <= sp):
            breached = True
    return out


def _entry_iv(ch: _Chain, forward: float, years: float) -> tuple[FloatArray, float] | None:
    strikes = np.array(sorted({k[0] for k in ch}), dtype=np.float64)
    atm = float(strikes[np.argmin(np.abs(strikes - forward))])
    if (atm, "CE") not in ch or (atm, "PE") not in ch:
        return None
    ce, pe = ch[(atm, "CE")][0], ch[(atm, "PE")][0]
    if ce is None or pe is None:
        return None
    ivs = implied(
        np.full(2, forward),
        np.full(2, atm),
        np.full(2, years),
        np.array([ce, pe], dtype=np.float64),
        np.array(["CE", "PE"]),
    )
    return strikes, float(np.mean(ivs))


def condor_trades(panel: OptionPanel, params: CondorParams) -> CondorRun:  # noqa: PLR0912, PLR0915 - the script's loop, kept whole
    """``res_options.condor_trades``: one row per cycle entered ``n_before`` sessions before
    each monthly expiry, exited at the close of ``E - exit_n`` or earlier."""
    rows: list[tuple[object, ...]] = []
    ca_skipped: list[tuple[str, dt.date]] = []
    liqd: dict[tuple[str, dt.date], float | None] = {}
    if params.top_n:
        liq = _liquidity_rank(panel.cont)
        liqd = {(s, d): r for s, d, r in liq.select("symbol", "date", "lr").iter_rows()}
    trend = _trend_map(panel.cont) if params.mode is CondorMode.TREND_CREDIT else {}
    for (sym, e), by_day in panel.groups.items():
        if params.symbols and sym not in params.symbols:
            continue
        d0 = panel.trading_days_before(e, params.n_before)
        dx = panel.trading_days_before(e, params.exit_n)
        if d0 is None or dx is None or d0 >= dx or d0 not in by_day:
            continue
        forward = panel.fgroups.get((sym, e, d0))
        if forward is None:
            continue
        if params.top_n and sym not in INDEX_UNDERLYINGS:
            rank = liqd.get((sym, d0))
            if rank is None or rank > params.top_n:
                continue
        lot = float(panel.lots.get(sym) or 500.0)
        ch = {k: v for k, v in by_day[d0].items() if v[0] and v[0] > 0}
        if len(ch) < 8:  # noqa: PLR2004 - the script's minimum chain
            continue
        years = (e - d0).days / 365.0
        found = _entry_iv(ch, forward, years)
        if found is None:
            continue
        strikes, iv = found
        if not (0.05 < iv < 1.5):  # noqa: PLR2004 - the script's sanity band
            continue
        sd = forward * iv * math.sqrt(years)
        sc = _pick(strikes, forward + params.k_short * sd, 1)
        sp = _pick(strikes, forward - params.k_short * sd, -1)
        wc = _pick(strikes, forward + (params.k_short + params.k_wing) * sd, 1)
        wp = _pick(strikes, forward - (params.k_short + params.k_wing) * sd, -1)
        if sc is None or sp is None or wc is None or wp is None or wc <= sc or wp >= sp:
            continue
        legs: list[_Leg] = [(sc, "CE", -1), (wc, "CE", 1), (sp, "PE", -1), (wp, "PE", 1)]
        if params.mode is CondorMode.TREND_CREDIT:
            tdir = trend.get((sym, d0), 0)
            if tdir == 0:
                continue
            legs = [(sp, "PE", -1), (wp, "PE", 1)] if tdir > 0 else [(sc, "CE", -1), (wc, "CE", 1)]
        px0: dict[tuple[float, str], float] = {}
        ok = True
        for k, cp, q in legs:
            r = ch.get((k, cp))
            if r is None or r[0] is None or (q < 0 and not r[1]):
                ok = False
                break
            px0[(k, cp)] = r[0]
        if not ok:
            continue
        credit = sum(-q * px0[(k, cp)] for k, cp, q in legs)
        has_ce = any(leg[1] == "CE" for leg in legs)
        has_pe = any(leg[1] == "PE" for leg in legs)
        width = max((wc - sc) if has_ce else 0, (sp - wp) if has_pe else 0)
        maxloss = width - credit
        if credit <= 0 or maxloss <= 0:
            continue
        cost = sum(leg_costs(px0[(k, cp)], q < 0, lot, params) for k, cp, q in legs)
        walk = _walk(panel, (sym, e), d0, dx, legs, iv, credit, (wc, sc, sp, wp), params)
        if walk.broken:
            ca_skipped.append((sym, e))
            continue
        if walk.exit_val is None:
            continue
        exit_cost = sum(leg_costs(p, sells, lot, params) for p, sells in walk.exit_legs)
        pnl = credit - walk.exit_val - cost - exit_cost
        rows.append(
            (
                sym,
                d0,
                e,
                walk.exit_d,
                iv,
                panel.rv20.get((sym, d0)),
                credit,
                maxloss,
                pnl,
                pnl / maxloss,
                walk.modelled,
                credit / forward,
                (cost + exit_cost) / maxloss,
            )
        )
    trades = pl.DataFrame(rows, schema=_CONDOR_SCHEMA, orient="row")
    return CondorRun(trades, tuple(ca_skipped))


# --------------------------------------------------------------------------------------------
# Summaries (the scripts' print lines, as values)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Summary:
    """``n``, expectancy in R, its t-statistic, the win rate, the worst trade, and by year.

    ``gross_r``/``cost_r`` are set for option families (whose trades carry ``cost_R``).
    """

    n: int
    exp_r: float
    t_stat: float
    win: float
    worst: float
    gross_r: float | None
    cost_r: float | None
    per_year: Mapping[int, tuple[int, float]]


def summarise(trades: pl.DataFrame, r_column: str = "R", date_column: str = "entry") -> Summary:
    """The scripts' ``summarise``/``report`` headline numbers."""
    if trades.is_empty():
        return Summary(0, 0.0, 0.0, 0.0, 0.0, None, None, {})
    r = trades[r_column]
    mean = _as_float(r.mean())
    std = _as_float(r.std())
    t_stat = mean / (std / math.sqrt(trades.height)) if std > 0 else 0.0
    win = _as_float((r > 0).mean())
    worst = _as_float(r.min())
    gross: float | None = None
    cost: float | None = None
    if "cost_R" in trades.columns:
        gross = _as_float((r + trades["cost_R"]).mean())
        cost = _as_float(trades["cost_R"].mean())
    years = (
        trades.with_columns(pl.col(date_column).dt.year().alias("yr"))
        .group_by("yr")
        .agg(pl.len().alias("n"), pl.col(r_column).mean().alias("exp"))
        .sort("yr")
    )
    per_year = {int(y): (int(n), float(x)) for y, n, x in years.iter_rows()}
    return Summary(trades.height, mean, t_stat, win, worst, gross, cost, per_year)


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raise TypeError(f"expected a number, got {type(value).__name__}")


# --------------------------------------------------------------------------------------------
# res_futures.py / res_f2.py
# --------------------------------------------------------------------------------------------


def futures_panel(cont: pl.DataFrame) -> pl.DataFrame:
    """The indicator frame both futures scripts build over ``cont.parquet``."""
    c = cont.sort("symbol", "di").with_columns(
        pl.max_horizontal(
            pl.col("h") - pl.col("l"),
            (pl.col("h") - pl.col("c").shift(1).over("symbol")).abs(),
            (pl.col("l") - pl.col("c").shift(1).over("symbol")).abs(),
        ).alias("tr"),
    )
    c = c.with_columns(
        pl.col("tr").rolling_mean(14).over("symbol").alias("atr"),
        pl.col("c").shift(1).rolling_max(20).over("symbol").alias("hi20"),
        pl.col("c").shift(1).rolling_min(20).over("symbol").alias("lo20"),
        pl.col("c").rolling_mean(50).over("symbol").alias("ma50"),
        pl.col("c").rolling_mean(200).over("symbol").alias("ma200"),
        (pl.col("oi_total") / pl.col("oi_total").shift(5).over("symbol") - 1).alias("oi5"),
        (pl.col("c") / pl.col("c").shift(1).over("symbol") - 1).alias("ret1"),
        (pl.col("oi_total") / pl.col("oi_total").shift(1).over("symbol") - 1).alias("oi1"),
        (pl.col("c").shift(5) / pl.col("c").shift(68) - 1).over("symbol").alias("mom63"),
        pl.col("fut_turnover").rolling_median(20).over("symbol").alias("turn20"),
    )
    nifty = (
        cont.filter(pl.col("symbol") == "NIFTY")
        .sort("di")
        .with_columns((pl.col("c") > pl.col("c").rolling_mean(50)).alias("nifty_up"))
        .select("di", "nifty_up")
    )
    return c.join(nifty, on="di", how="left").sort("symbol", "di")


class FuturesFamily(StrEnum):
    """``RESEARCH.md`` §A's signal families."""

    A1 = "A1"  # 20-day breakout + trend + OI rising, both sides
    A1R = "A1R"  # A1 aligned with NIFTY's 50-day regime
    A0 = "A0"  # control: aligned breakout, no OI filter, both sides
    A0_LONG = "A0_LONG"  # A0, long only: F2's signal
    A3 = "A3"  # one-day long/short buildup
    A2 = "A2"  # weekly cross-sectional momentum, top/bottom 10
    A4 = "A4"  # index futures trend


def liquid_base(panel: pl.DataFrame, share: float = 0.6) -> pl.DataFrame:
    """``base``: stock futures with ATR and MA50, in the top ``share`` by 20-day turnover."""
    base = panel.filter(
        (pl.col("instrument") == "FUTSTK")
        & pl.col("atr").is_not_null()
        & pl.col("ma50").is_not_null()
    )
    base = base.with_columns(
        pl.col("turn20").rank(descending=True).over("di").alias("trank"),
        pl.col("turn20").count().over("di").alias("tn"),
    )
    return base.filter(pl.col("trank") <= share * pl.col("tn"))


def _side(frame: pl.DataFrame, side: int) -> pl.DataFrame:
    return frame.select("symbol", "di", pl.lit(side).alias("side"))


def futures_signals(panel: pl.DataFrame, family: FuturesFamily) -> pl.DataFrame:  # noqa: PLR0911 - one return per family
    """``symbol, di, side`` for one family, exactly as the scripts filter."""
    c, hi, lo, ma = pl.col("c"), pl.col("hi20"), pl.col("lo20"), pl.col("ma50")
    up, oi5 = pl.col("nifty_up"), pl.col("oi5")
    if family is FuturesFamily.A4:
        idx = panel.filter(
            pl.col("symbol").is_in(["NIFTY", "BANKNIFTY", "MIDCPNIFTY"])
            & pl.col("atr").is_not_null()
            & ma.is_not_null()
        )
        return pl.concat(
            [_side(idx.filter((c > hi) & (c > ma)), 1), _side(idx.filter((c < lo) & (c < ma)), -1)]
        )
    base = liquid_base(panel)
    if family is FuturesFamily.A1:
        return pl.concat(
            [
                _side(base.filter((c > hi) & (c > ma) & (oi5 > 0)), 1),
                _side(base.filter((c < lo) & (c < ma) & (oi5 > 0)), -1),
            ]
        )
    if family is FuturesFamily.A1R:
        return pl.concat(
            [
                _side(base.filter((c > hi) & (c > ma) & (oi5 > 0) & up), 1),
                _side(base.filter((c < lo) & (c < ma) & (oi5 > 0) & ~up), -1),
            ]
        )
    if family is FuturesFamily.A0:
        return pl.concat(
            [
                _side(base.filter((c > hi) & (c > ma) & up), 1),
                _side(base.filter((c < lo) & (c < ma) & ~up), -1),
            ]
        )
    if family is FuturesFamily.A0_LONG:
        return _side(base.filter((c > hi) & (c > ma) & up), 1)
    if family is FuturesFamily.A3:
        ret1, oi1 = pl.col("ret1"), pl.col("oi1")
        return pl.concat(
            [
                _side(base.filter((ret1 > 0.02) & (oi1 > 0.05)), 1),  # noqa: PLR2004
                _side(base.filter((ret1 < -0.02) & (oi1 > 0.05)), -1),  # noqa: PLR2004
            ]
        )
    wk = base.filter(pl.col("mom63").is_not_null() & (pl.col("di") % 5 == 0))
    wk = wk.with_columns(
        pl.col("mom63").rank().over("di").alias("mr"), pl.len().over("di").alias("mn")
    )
    return pl.concat(
        [
            _side(wk.filter(pl.col("mr") > pl.col("mn") - 10), 1),
            _side(wk.filter(pl.col("mr") <= 10), -1),  # noqa: PLR2004
        ]
    )


@dataclass(frozen=True, slots=True)
class FuturesParams:
    horizon: int
    k_atr: float
    trail: bool = False
    #: ``res_f2``: charge one more round trip per calendar roll inside the hold.
    charge_rolls: bool = False
    cost: float = FUTURES_ROUND_TRIP_COST


#: F2's spec as ``res_f2.py`` ran it: A0 long, 3 ATR chandelier, ≤ 40 sessions, rolls costed.
F2_SPEC = FuturesParams(horizon=40, k_atr=3.0, trail=True, charge_rolls=True)


def simulate_futures(
    panel: pl.DataFrame, signals: pl.DataFrame, params: FuturesParams
) -> pl.DataFrame:
    """``simulate``: signal at close t, entry at open t+1, a k-ATR stop touched intraday
    (at the stop, or the open if gapped through), trailing if asked, else the close after
    ``horizon`` sessions. One trade per symbol at a time."""
    arr = {
        str(key[0]): g
        for key, g in panel.partition_by("symbol", as_dict=True, maintain_order=True).items()
    }
    out: list[tuple[object, ...]] = []
    for key, g in signals.partition_by("symbol", as_dict=True).items():
        s = arr[str(key[0])]
        di = s["di"].to_numpy()
        o = s["o"].to_numpy()
        h = s["h"].to_numpy()
        lo = s["l"].to_numpy()
        cl = s["c"].to_numpy()
        atr = s["atr"].to_numpy()
        dates = s["date"].to_list()
        exps = s["expiry"].to_list()
        pos = {int(d): i for i, d in enumerate(di)}
        busy_until = -1
        for row in g.sort("di").iter_rows(named=True):
            i = pos.get(int(row["di"]))
            if i is None or i + 1 >= len(di) or i <= busy_until or not np.isfinite(atr[i]):
                continue
            side = int(row["side"])
            e = float(o[i + 1])
            risk = params.k_atr * float(atr[i])
            if risk <= 0:
                continue
            stop = e - side * risk
            exit_px: float | None = None
            j = i + 1
            last = min(i + params.horizon, len(di) - 1)
            best = e
            while j <= last:
                if params.trail and j > i + 1:
                    best = max(best, float(cl[j - 1])) if side > 0 else min(best, float(cl[j - 1]))
                    stop = max(stop, best - risk) if side > 0 else min(stop, best + risk)
                if side > 0 and lo[j] <= stop:
                    exit_px = min(stop, float(o[j])) if j > i + 1 else stop
                    break
                if side < 0 and h[j] >= stop:
                    exit_px = max(stop, float(o[j])) if j > i + 1 else stop
                    break
                j += 1
            if exit_px is None:
                j = last
                exit_px = float(cl[j])
            rolls = (
                sum(1 for m in range(i + 2, j + 1) if exps[m] != exps[m - 1])
                if params.charge_rolls
                else 0
            )
            pnl = side * (exit_px - e) - params.cost * e * (1 + rolls)
            out.append((str(key[0]), dates[i + 1], side, pnl / risk, pnl / e, j - i, rolls))
            busy_until = j
    return pl.DataFrame(
        out, schema=["symbol", "entry", "side", "R", "ret", "days", "rolls"], orient="row"
    )


def f2_trades(cont: pl.DataFrame) -> pl.DataFrame:
    """``res_f2.py``: F2's spec with roll costs."""
    panel = futures_panel(cont)
    return simulate_futures(panel, futures_signals(panel, FuturesFamily.A0_LONG), F2_SPEC)


# --------------------------------------------------------------------------------------------
# res_debit.py
# --------------------------------------------------------------------------------------------


class DebitStructure(StrEnum):
    SPREAD = "spread"
    LONG = "long"


def _exit_mark(  # noqa: PLR0913, PLR0917 - res_debit's inner ``px``
    chx: _Chain, strike: float, cp: str, forward: float, years: float, iv: float
) -> tuple[float, int]:
    """The traded close if the leg traded that day, else Black-76 at the entry IV (modelled)."""
    r = chx.get((strike, cp))
    if r and r[0] and r[1]:
        return r[0], 0
    return _b76_one(forward, strike, years, iv, cp, RESEARCH_RATE), 1


def debit_trades(  # noqa: PLR0912, PLR0915 - the script's loop, kept whole
    panel: OptionPanel, horizon: int = 10, structure: DebitStructure = DebitStructure.SPREAD
) -> pl.DataFrame:
    """``res_debit.py`` (C1 spread / C2 long) on the A0 signal. ``panel`` is built over
    :func:`liquid_symbols` (the script's ``load_groups(None)``)."""
    params = CondorParams(slip_pct=0.03)
    c = panel.cont.filter(pl.col("instrument") == "FUTSTK").sort("symbol", "di")
    c = c.with_columns(
        pl.col("c").shift(1).rolling_max(20).over("symbol").alias("hi20"),
        pl.col("c").shift(1).rolling_min(20).over("symbol").alias("lo20"),
        pl.col("c").rolling_mean(50).over("symbol").alias("ma50"),
        pl.col("fut_turnover").rolling_median(20).over("symbol").alias("turn20"),
    )
    nifty = (
        panel.cont.filter(pl.col("symbol") == "NIFTY")
        .sort("di")
        .with_columns((pl.col("c") > pl.col("c").rolling_mean(50)).alias("nifty_up"))
        .select("di", "nifty_up")
    )
    c = c.join(nifty, on="di", how="left").with_columns(
        pl.col("turn20").rank(descending=True).over("di").alias("tr"),
        pl.len().over("di").alias("tn"),
    )
    c = c.filter(pl.col("tr") <= 0.6 * pl.col("tn"))
    cc, hi, lo, ma, up = (
        pl.col("c"),
        pl.col("hi20"),
        pl.col("lo20"),
        pl.col("ma50"),
        pl.col("nifty_up"),
    )
    sig = pl.concat(
        [
            c.filter((cc > hi) & (cc > ma) & up).select("symbol", "date", pl.lit(1).alias("side")),
            c.filter((cc < lo) & (cc < ma) & ~up).select(
                "symbol", "date", pl.lit(-1).alias("side")
            ),
        ]
    ).sort("symbol", "date")
    expiries: dict[str, list[dt.date]] = {}
    for sym_e, e_ in panel.groups:
        expiries.setdefault(sym_e, []).append(e_)
    rows: list[tuple[object, ...]] = []
    busy: dict[str, dt.date] = {}
    for sym, d, side in sig.iter_rows():
        if d not in panel.didx or panel.didx[d] + 1 >= len(panel.dates):
            continue
        if sym in busy and d <= busy[sym]:
            continue
        d1 = panel.dates[panel.didx[d] + 1]
        es = sorted(
            e
            for e in expiries.get(sym, [])
            if e in panel.didx and panel.didx[e] - panel.didx[d1] >= 12  # noqa: PLR2004
        )
        if not es:
            continue
        e = es[0]
        by_day = panel.groups[(sym, e)]
        forward = panel.fgroups.get((sym, e, d1))
        if forward is None or d1 not in by_day:
            continue
        ch = {k: v for k, v in by_day[d1].items() if v[0] and v[0] > 0 and v[1]}
        cp = "CE" if side > 0 else "PE"
        strikes = np.array(sorted({k[0] for k in ch if k[1] == cp}), dtype=np.float64)
        if len(strikes) < 4:  # noqa: PLR2004
            continue
        years = (e - d1).days / 365.0
        kb = float(strikes[np.argmin(np.abs(strikes - forward))])
        kb_px = ch[(kb, cp)][0]
        if kb_px is None:
            continue
        iv = float(
            implied(
                np.array([forward]),
                np.array([kb]),
                np.array([years]),
                np.array([kb_px], dtype=np.float64),
                np.array([cp]),
            )[0]
        )
        target = forward * (1 + side * 0.5 * iv * math.sqrt(years))
        ks: float | None = float(strikes[np.argmin(np.abs(strikes - target))])
        if structure is DebitStructure.LONG:
            ks = None
        elif ks is not None and (ks - kb) * side <= 0:
            continue
        ks_px = ch[(ks, cp)][0] if ks is not None else 0.0
        debit = kb_px - (ks_px or 0.0)
        width = abs(ks - kb) if ks is not None else 1e12
        if debit <= 0 or debit >= width:
            continue
        lot = float(panel.lots.get(sym) or 500.0)
        ix = min(panel.didx[d1] + horizon, panel.didx[e] - 1)
        dx = panel.dates[ix]
        fx = panel.fgroups.get((sym, e, dx))
        chx = by_day.get(dx, {})
        if fx is None or not (0.7 < fx / forward < 1.4):  # noqa: PLR2004
            continue
        tx = max((e - dx).days, 0.5) / 365.0

        pb, m1 = _exit_mark(chx, kb, cp, fx, tx, iv)
        ps, m2 = _exit_mark(chx, ks, cp, fx, tx, iv) if ks is not None else (0.0, 0)
        val = min(max(pb - ps, 0.0), width)
        cost = leg_costs(kb_px, False, lot, params) + leg_costs(pb, True, lot, params)
        if ks is not None:
            cost += leg_costs(ks_px or 0.0, True, lot, params) + leg_costs(ps, False, lot, params)
        pnl = val - debit - cost
        fut_ret = side * (fx / forward - 1) - FUTURES_ROUND_TRIP_COST
        rows.append((sym, d1, side, iv, debit / forward, pnl / debit, fut_ret, m1 + m2))
        busy[sym] = dx
    return pl.DataFrame(
        rows,
        schema=["symbol", "entry", "side", "iv", "debit_pct", "R_spread", "fut_ret", "modelled"],
        orient="row",
    )


# --------------------------------------------------------------------------------------------
# res_basis.py
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BasisStudy:
    sessions: int
    quantiles: Mapping[float, float]
    share_net_above_7pct: float
    by_month: pl.DataFrame


def basis_study(futures: pl.DataFrame, cycle_cost: float = 0.0045) -> BasisStudy:
    """``res_basis.py`` (E): front-month annualised basis on UDiFF days, ≥ 5 days to expiry."""
    f = futures.filter(
        (pl.col("instrument") == "FUTSTK")
        & pl.col("underlying").is_not_null()
        & (pl.col("settle") > 0)
    )
    front = (
        f.sort("expiry")
        .group_by("symbol", "date")
        .first()
        .with_columns((pl.col("expiry") - pl.col("date")).dt.total_days().alias("dte"))
        .filter(pl.col("dte") >= 5)  # noqa: PLR2004
    )
    front = front.with_columns(
        (pl.col("settle") / pl.col("underlying") - 1).alias("basis")
    ).with_columns((pl.col("basis") * 365 / pl.col("dte")).alias("ann"))
    front = front.filter(pl.col("ann").abs() < 1.0)
    front = front.with_columns(
        (pl.col("basis") - cycle_cost).alias("net_to_expiry"),
        ((pl.col("basis") - cycle_cost) * 365 / pl.col("dte")).alias("net_ann"),
    )
    quantiles = {
        q: _as_float(front.select(pl.col("ann").quantile(q)).item())
        for q in (0.1, 0.25, 0.5, 0.75, 0.9)
    }
    share = front.filter(pl.col("net_ann") > 0.07).height / front.height  # noqa: PLR2004
    liquid = front.with_columns(
        pl.col("turnover").rank(descending=True).over("date").alias("r")
    ).filter(
        pl.col("r") <= 50  # noqa: PLR2004
    )
    by_month = (
        liquid.group_by(pl.col("date").dt.strftime("%Y-%m").alias("m"))
        .agg(
            pl.col("ann").median().alias("median_ann_top50"),
            (pl.col("net_ann") > 0.07).mean().alias("share_net_above_7pct"),  # noqa: PLR2004
        )
        .sort("m")
    )
    return BasisStudy(front["date"].n_unique(), quantiles, share, by_month)
