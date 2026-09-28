"""F3's EOD re-test: the daily half of ``04`` §11 on the option bhavcopy, pure (F3-3).

What it tests, and what it cannot
---------------------------------
The bhavcopy is a closing file. It can prove the **daily** rules: the levels and the direction at
a close, the entry at the next session's settle, the spread's credit and its mark at each settle,
the level break judged on the session's high or low, the loss cut and the decay target judged at
the settle, the hard exit at the expiry settle, and the next-session add. It **cannot** test the
75-minute confirm, the intraday alignment, the intraday cut at the moment the level breaks, or
"cut at 50" read from a live mark — every one of those needs a print inside the session. This
proxy therefore exits at the **settle of the session** in which a level broke, which is worse
than his rule when the break comes early in the day and better when the index reverses; the
numbers here are a floor for the daily half and say nothing about the intraday half.

The index bars are the **front-month future's** open, high, low and close (the bhavcopy carries
no index candle; the UDiFF file's ``underlying`` is a closing level only), so every level sits a
basis away from the cash index. R is net P&L per unit ÷ the spread's max loss per unit, so a
full loss is -1 and a full decay a little under +credit/max-loss. Costs are the research's
index-option costs per leg per crossing (``research.leg_costs``: 0.5 % slippage with a ₹0.05
floor, STT on sells, brokerage and exchange charges with GST).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise
from typing import Final

import polars as pl

from baskfy_core.fno import directional as d
from baskfy_core.fno import research as r
from baskfy_core.fno.config import F3Config
from baskfy_core.options.config import OptionType

#: The research's index-option cost knobs (``RESEARCH.md`` method; ``04`` §3).
INDEX_COSTS: Final = r.CondorParams(slip_pct=0.005, slip_min=0.05)
#: Lot sizes for sessions before the UDiFF file carried ``NewBrdLotQty`` (8 Jul 2024).
FALLBACK_LOT: Final[dict[str, int]] = {"NIFTY": 50, "BANKNIFTY": 15}
#: The rules the closing file cannot test, named so the evidence can say so.
NOT_TESTED: Final[tuple[str, ...]] = (
    "the 75-minute confirm",
    "the intraday alignment with the session's open",
    "the intraday cut at the moment the level breaks (the proxy exits at that session's settle)",
    "the loss cut read from a live mark (the proxy reads the settle)",
)
_TRADE_COLUMNS: Final = (
    "symbol",
    "direction",
    "signal",
    "entry",
    "exit",
    "expiry",
    "option_type",
    "short",
    "wing",
    "credit",
    "lots",
    "reason",
    "sessions",
    "gross_R",
    "cost_R",
    "R",
    "pnl_inr",
)


@dataclass(frozen=True, slots=True)
class IndexPanel:
    """One underlying's daily bars (front-month future), lot sizes and option settles."""

    symbol: str
    bars: tuple[d.DailyBar, ...]
    lot_by_date: dict[dt.date, int]
    #: ``(date, expiry, strike, type) -> settle``; only rows with a positive settle.
    settle: dict[tuple[dt.date, dt.date, Decimal, OptionType], Decimal]
    #: ``(date, expiry, strike, type) -> traded`` (OI or volume that day).
    traded: dict[tuple[dt.date, dt.date, Decimal, OptionType], bool]
    #: ``date -> the expiries listed that day``.
    expiries_by_date: dict[dt.date, tuple[dt.date, ...]]
    #: ``(date, expiry) -> the strikes listed for that expiry that day``.
    strikes_by_expiry: dict[tuple[dt.date, dt.date], tuple[Decimal, ...]]


def _dec(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def index_panel(contracts: pl.DataFrame, symbol: str) -> IndexPanel:
    """The panel for ``symbol`` from bhavcopy rows (``fo_contract_daily``'s columns, or the day
    files' with ``date``). The front month is the nearest unexpired future each session."""
    frame = (
        contracts.rename({"trade_date": "date"}) if "trade_date" in contracts.columns else contracts
    ).filter(pl.col("symbol") == symbol)
    fut = (
        frame.filter((pl.col("instrument") == "FUTIDX") & (pl.col("expiry") >= pl.col("date")))
        .sort("date", "expiry")
        .group_by("date", maintain_order=True)
        .first()
        .sort("date")
    )
    bars: list[d.DailyBar] = []
    lot_by_date: dict[dt.date, int] = {}
    last_lot = FALLBACK_LOT.get(symbol, 50)
    for row in fut.iter_rows(named=True):
        o, h, lo, c = (row[k] for k in ("open", "high", "low", "close"))
        if any(v is None for v in (o, h, lo, c)):
            continue
        bars.append(d.DailyBar(row["date"], _dec(o), _dec(h), _dec(lo), _dec(c)))
        if row.get("lot_size"):
            last_lot = int(row["lot_size"])
        lot_by_date[row["date"]] = last_lot
    opt = frame.filter(pl.col("instrument") == "OPTIDX").select(
        "date", "expiry", "strike", "option_type", "settle", "open_interest", "volume"
    )
    settle: dict[tuple[dt.date, dt.date, Decimal, OptionType], Decimal] = {}
    traded: dict[tuple[dt.date, dt.date, Decimal, OptionType], bool] = {}
    expiries: defaultdict[dt.date, set[dt.date]] = defaultdict(set)
    strikes: defaultdict[tuple[dt.date, dt.date], set[Decimal]] = defaultdict(set)
    for row in opt.iter_rows(named=True):
        kind = row["option_type"]
        if kind not in ("CE", "PE") or row["settle"] is None:
            continue
        key = (row["date"], row["expiry"], _dec(row["strike"]), OptionType(kind))
        price = _dec(row["settle"])
        if price > 0:
            settle[key] = price
        traded[key] = bool((row["open_interest"] or 0) > 0 or (row["volume"] or 0) > 0)
        expiries[row["date"]].add(row["expiry"])
        strikes[(row["date"], row["expiry"])].add(_dec(row["strike"]))
    return IndexPanel(
        symbol=symbol,
        bars=tuple(bars),
        lot_by_date=lot_by_date,
        settle=settle,
        traded=traded,
        expiries_by_date={k: tuple(sorted(v)) for k, v in expiries.items()},
        strikes_by_expiry={k: tuple(sorted(v)) for k, v in strikes.items()},
    )


def monthly_expiries(listed: tuple[dt.date, ...]) -> tuple[dt.date, ...]:
    """The last listed expiry of each calendar month: the monthly contract."""
    by_month: dict[tuple[int, int], dt.date] = {}
    for expiry in listed:
        key = (expiry.year, expiry.month)
        by_month[key] = max(by_month.get(key, expiry), expiry)
    return tuple(sorted(by_month.values()))


def strike_step(strikes: tuple[Decimal, ...], near: Decimal) -> Decimal | None:
    """The most common gap between adjacent listed strikes around ``near`` (a tie to the finer)."""
    if len(strikes) < 2:  # noqa: PLR2004 - a step needs two strikes
        return None
    window = sorted(strikes, key=lambda k: (abs(k - near), k))[:21]
    ordered = sorted(window)
    gaps: dict[Decimal, int] = {}
    for a, b in pairwise(ordered):
        gaps[b - a] = gaps.get(b - a, 0) + 1
    top = max(gaps.values())
    return min(g for g, n in gaps.items() if n == top)


def _leg_cost(price: Decimal, *, sell: bool, lot: int) -> Decimal:
    return Decimal(str(r.leg_costs(float(price), sell, float(lot), INDEX_COSTS)))


@dataclass(slots=True)
class _Open:
    direction: d.Direction
    level: Decimal
    strikes: d.SpreadStrikes
    expiry: dt.date
    signal: dt.date
    entry: dt.date
    entry_index: int
    credit: Decimal
    lots: int
    lot: int
    max_loss_unit: Decimal
    #: Per-unit credit and entry cost summed over the lots (the add brings its own).
    credit_total: Decimal
    cost_total: Decimal
    added: bool = False


@dataclass(frozen=True, slots=True)
class F3RetestResult:
    symbol: str
    trades: pl.DataFrame
    summary: r.Summary
    skipped: dict[str, int] = field(default_factory=dict)


def _intrinsic(strike: Decimal, kind: OptionType, close: Decimal) -> Decimal:
    return (
        max(strike - close, Decimal(0))
        if kind is OptionType.PE
        else max(close - strike, Decimal(0))
    )


def _legs(
    panel: IndexPanel, day: dt.date, o: _Open, close: Decimal
) -> tuple[Decimal, Decimal] | None:
    """The short's and the wing's values on ``day``.

    On the expiry day, and after it, the legs are worth their **intrinsic** against the session's
    close: NSE's file prints the index's final settlement *level* in an expired option's settle
    column, not a premium. Before expiry the settles are read, and a settle that is not a premium
    (above a quarter of the index) is treated as missing rather than believed.
    """
    kind = o.strikes.option_type
    if day >= o.expiry:
        return _intrinsic(o.strikes.short, kind, close), _intrinsic(o.strikes.wing, kind, close)
    short = panel.settle.get((day, o.expiry, o.strikes.short, kind))
    if short is None or short > close / 4:
        return None
    wing = panel.settle.get((day, o.expiry, o.strikes.wing, kind)) or Decimal(0)
    if wing > close / 4:
        return None
    return short, wing


def _close(  # noqa: PLR0913 - the trade's whole record, by keyword
    o: _Open,
    *,
    day: dt.date,
    legs: tuple[Decimal, Decimal],
    reason: str,
    sessions: int,
    symbol: str,
) -> dict[str, object]:
    short, wing = legs
    mark = short - wing
    # Exit costs: both legs cross again (the short is bought back, the wing sold), per lot.
    exit_cost = _leg_cost(short, sell=False, lot=o.lot) + _leg_cost(
        max(wing, Decimal("0.05")), sell=True, lot=o.lot
    )
    cost_unit = o.cost_total / o.lots + exit_cost
    gross_unit = o.credit_total / o.lots - mark
    net_unit = gross_unit - cost_unit
    units = o.lots * o.lot
    return {
        "symbol": symbol,
        "direction": o.direction.value,
        "signal": o.signal,
        "entry": o.entry,
        "exit": day,
        "expiry": o.expiry,
        "option_type": o.strikes.option_type.value,
        "short": float(o.strikes.short),
        "wing": float(o.strikes.wing),
        "credit": float(o.credit),
        "lots": o.lots,
        "reason": reason,
        "sessions": sessions,
        "gross_R": float(gross_unit / o.max_loss_unit),
        "cost_R": float(cost_unit / o.max_loss_unit),
        "R": float(net_unit / o.max_loss_unit),
        "pnl_inr": float(net_unit * units),
    }


def _try_entry(  # noqa: PLR0913 - the signal, the session and every refusal by name
    panel: IndexPanel,
    *,
    lv: d.Levels,
    direction: d.Direction,
    signal: dt.date,
    day: dt.date,
    entry_index: int,
    f3: F3Config,
    skipped: dict[str, int],
) -> _Open | None:
    listed = panel.expiries_by_date.get(day, ())
    if panel.symbol == "NIFTY":
        expiry = choose_expiry(panel, listed, day, f3.min_sessions_weekly)
    else:
        expiry = choose_expiry(panel, monthly_expiries(listed), day, f3.min_sessions_monthly)
    if expiry is None:
        skipped["no_expiry"] = skipped.get("no_expiry", 0) + 1
        return None
    step = strike_step(panel.strikes_by_expiry.get((day, expiry), ()), lv.close)
    if step is None:
        skipped["no_strikes"] = skipped.get("no_strikes", 0) + 1
        return None
    strikes = d.spread_strikes(direction, lv, step, f3)
    short = panel.settle.get((day, expiry, strikes.short, strikes.option_type))
    wing = panel.settle.get((day, expiry, strikes.wing, strikes.option_type))
    if short is None or wing is None:
        skipped["no_settle"] = skipped.get("no_settle", 0) + 1
        return None
    if not panel.traded.get((day, expiry, strikes.short, strikes.option_type), False):
        skipped["short_not_traded"] = skipped.get("short_not_traded", 0) + 1
        return None
    credit = d.credit_per_unit(short, wing)
    if short < f3.short_premium_min_inr or credit <= 0:
        skipped["thin_credit"] = skipped.get("thin_credit", 0) + 1
        return None
    lot = panel.lot_by_date.get(day, FALLBACK_LOT.get(panel.symbol, 50))
    entry_cost = _leg_cost(short, sell=True, lot=lot) + _leg_cost(wing, sell=False, lot=lot)
    return _Open(
        direction=direction,
        level=d.key_level(direction, lv) or lv.close,
        strikes=strikes,
        expiry=expiry,
        signal=signal,
        entry=day,
        entry_index=entry_index,
        credit=credit,
        lots=1,
        lot=lot,
        max_loss_unit=d.max_loss_per_unit(strikes, credit),
        credit_total=credit,
        cost_total=entry_cost,
    )


def sessions_left(panel: IndexPanel, entry: dt.date, expiry: dt.date) -> int:
    """Sessions after ``entry`` up to ``expiry``: the panel's own sessions, and weekdays beyond
    the panel's last bar (an expiry past the sample's end is still a real date)."""
    dates = [b.date for b in panel.bars]
    inside = sum(1 for day in dates if entry < day <= expiry)
    last = dates[-1] if dates else entry
    beyond = 0
    day = max(last, entry) + dt.timedelta(days=1)
    while day <= expiry:
        if day.weekday() < 5:  # noqa: PLR2004 - a weekday
            beyond += 1
        day += dt.timedelta(days=1)
    return inside + beyond


def choose_expiry(
    panel: IndexPanel, listed: tuple[dt.date, ...], entry: dt.date, min_sessions: int
) -> dt.date | None:
    """``directional.choose_expiry``'s rule on the panel's calendar: the nearest listed expiry
    with at least ``min_sessions`` sessions left after ``entry``."""
    for expiry in sorted(set(listed)):
        if expiry >= entry and sessions_left(panel, entry, expiry) >= min_sessions:
            return expiry
    return None


def _level_broken_on_bar(o: _Open, bar: d.DailyBar, f3: F3Config) -> bool:
    extreme = bar.low if o.direction is d.Direction.UP else bar.high
    return d.level_broken(o.direction, o.level, extreme, f3)


def run_symbol(  # noqa: PLR0912, PLR0915 - the whole hold, session by session
    panel: IndexPanel,
    f3: F3Config,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> F3RetestResult:
    """Every F3 trade the daily rules would have taken on ``panel``, in sample ``[start, end]``.

    The signal is read at session ``t``'s close from bars up to ``t``; the entry is at ``t + 1``'s
    settle; each later session judges the exit on its bar and settle. One position at a time.
    """
    bars = panel.bars
    rows: list[dict[str, object]] = []
    skipped: dict[str, int] = {}
    open_: _Open | None = None
    pending: tuple[d.Levels, d.Direction, dt.date] | None = None
    need = d.bars_needed(f3)
    for i, bar in enumerate(bars):
        day = bar.date
        if open_ is not None:
            sessions = i - open_.entry_index
            legs = _legs(panel, day, open_, bar.close)
            mark = None if legs is None else legs[0] - legs[1]
            reason: str | None = None
            if day >= open_.expiry:
                # Every exit on the expiry day is the same settle; the label says the position
                # ended by expiry, not by a decision (04 §11's precedence is for a live mark).
                reason = d.F3ExitReason.HARD_EXIT.value
            elif _level_broken_on_bar(open_, bar, f3):
                reason = d.F3ExitReason.LEVEL_BREAK.value
            elif mark is not None and mark >= d.loss_cut_mark(open_.credit, f3):
                reason = d.F3ExitReason.LOSS_CUT.value
            elif mark is not None and mark <= d.decay_target_mark(open_.credit, f3):
                reason = d.F3ExitReason.DECAY_TARGET.value
            if reason is not None:
                if legs is None:
                    # No usable settle on the exit day (a leg that stopped printing): intrinsic.
                    kind = open_.strikes.option_type
                    legs = (
                        _intrinsic(open_.strikes.short, kind, bar.close),
                        _intrinsic(open_.strikes.wing, kind, bar.close),
                    )
                rows.append(
                    _close(
                        open_,
                        day=day,
                        legs=legs,
                        reason=reason,
                        sessions=sessions,
                        symbol=panel.symbol,
                    )
                )
                open_ = None
            elif (
                not open_.added
                and legs is not None
                and mark is not None
                and sessions >= 1
                and mark <= open_.credit * (1 - f3.add_working_pct / Decimal(100))
            ):
                # The next-session add (04 §11): one more lot at the same strikes, at today's
                # settle, only while the daily direction still reads the same.
                lv_now = d.levels(bars[: i + 1], f3) if i + 1 >= need else None
                if lv_now is not None and d.daily_direction(lv_now) is open_.direction:
                    open_.added = True
                    open_.lots += 1
                    open_.credit_total += mark
                    open_.cost_total += _leg_cost(legs[0], sell=True, lot=open_.lot) + _leg_cost(
                        max(legs[1], Decimal("0.05")), sell=False, lot=open_.lot
                    )
        if open_ is None and pending is not None:
            lv, direction, signal = pending
            pending = None
            if (start is None or day >= start) and (end is None or day <= end):
                open_ = _try_entry(
                    panel,
                    lv=lv,
                    direction=direction,
                    signal=signal,
                    day=day,
                    entry_index=i,
                    f3=f3,
                    skipped=skipped,
                )
        if open_ is None and i + 1 >= need and (end is None or day <= end):
            tonight = d.levels(bars[: i + 1], f3)
            if tonight is not None:
                direction = d.daily_direction(tonight)
                if direction is not d.Direction.NONE:
                    pending = (tonight, direction, day)
    if open_ is not None and bars:
        # A position still open at the sample's end is marked to the last settle and counted, so
        # nothing in flight is silently dropped; its reason says what it is.
        last = bars[-1]
        legs = _legs(panel, last.date, open_, last.close)
        if legs is not None:
            rows.append(
                _close(
                    open_,
                    day=last.date,
                    legs=legs,
                    reason="OPEN_AT_END",
                    sessions=len(bars) - 1 - open_.entry_index,
                    symbol=panel.symbol,
                )
            )
    trades = (
        pl.DataFrame(rows, schema_overrides={"signal": pl.Date, "entry": pl.Date, "exit": pl.Date})
        .select(list(_TRADE_COLUMNS))
        .sort("entry")
        if rows
        else pl.DataFrame(schema={c: pl.Null for c in _TRADE_COLUMNS})
    )
    return F3RetestResult(panel.symbol, trades, _summarise(trades), skipped)


def _summarise(trades: pl.DataFrame) -> r.Summary:
    """``research.summarise``, which needs two trades for a standard deviation; one trade is its
    own summary with no t-statistic."""
    if trades.is_empty():
        return r.Summary(0, 0.0, 0.0, 0.0, 0.0, None, None, {})
    if trades.height >= 2:  # noqa: PLR2004 - a standard deviation needs two
        return r.summarise(trades)
    row = trades.row(0, named=True)
    net, cost = float(row["R"]), float(row["cost_R"])
    year = row["entry"].year
    return r.Summary(1, net, 0.0, float(net > 0), net, net + cost, cost, {year: (1, net)})


def run_f3_retest(
    contracts: pl.DataFrame,
    f3: F3Config,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> dict[str, F3RetestResult]:
    """Both underlyings, keyed by symbol."""
    return {
        symbol: run_symbol(index_panel(contracts, symbol), f3, start=start, end=end)
        for symbol in f3.underlyings
    }
