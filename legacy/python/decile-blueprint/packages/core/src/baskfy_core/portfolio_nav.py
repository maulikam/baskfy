"""The EOD NAV series and everything measured off it — `PORTFOLIO_REDESIGN.md` §5.1, §6.2-§6.5.

§5.1 makes a decision that looks small and is not: v1 is **end of day only, presented honestly,
like a fund NAV**. One official value per portfolio per day. The combined chart, per-day P&L,
per-portfolio contribution and the drawdown overlay are all views of that one series, so if the
series is right they agree with each other by construction, and if it is wrong they are wrong
together rather than four surfaces disagreeing about the same portfolio.

**Dates, valuations and prices arrive as arguments.** Nothing here reaches a broker, a database,
a clock or a file (law 1). The nightly job that computes an EOD value lives in ``services/``;
this module is only the arithmetic it hands its output to, which is why every rule below can be
asserted at a chosen date instead of on whatever day the suite happens to run.

WHAT THIS IS NOT
----------------
Not the nightly job, not a valuation service, not a price source. It never decides what a
portfolio was worth — :mod:`baskfy_core.allocation_ledger` does that from holdings and prices,
and something outside ``packages/core`` persists the answer. This module receives the finished
marks and answers: what return do they imply, how deep was the worst fall, what did each
portfolio contribute today, and how does that compare with an index.

Not an order path either. Nothing here names a side, a product, a venue or an order type, and
``test_portfolio_nav.py`` scans the source for that vocabulary, the same guard
``allocation_ledger`` and ``portfolio_units`` already carry.

WHY A NAV POINT CARRIES ITS OWN NET FLOW
----------------------------------------
A portfolio's value changes for two unrelated reasons: the market moved, or the user moved money
(§4.4's internal cash assign, or a withdrawal). Those two must never be added together. If they
are, assigning ₹1,00,000 to a portfolio reads as a ₹1,00,000 profit, the return series jumps, and
the number the product exists to publish becomes a lie on the one day the user did nothing but
transfer their own money.

So :class:`NavPoint` carries ``value`` **and** ``net_flow``, and every derived number here
subtracts the flow before it measures anything: the day's P&L, the growth factor behind TWR, and
the wealth index the drawdown is drawn on. The alternative shape — a separate list of flows the
caller zips against the values — was rejected because it makes the mistake silent: a caller who
forgets the second list still gets a plausible chart.

**Convention: a flow lands at the close of the day it is recorded on, and ``value`` is the mark
after it.** So the day's growth factor is ``(value - net_flow) / previous_value``: money that
arrived at the close earned nothing that day. The other convention (flows at the open, factor
``value / (previous_value + net_flow)``) is equally defensible in theory and wrong here in
practice, because an EOD-only product has exactly one observation per day and no way to know when
inside the day the transfer settled. Assuming it earned a full day's return would credit the
portfolio with a gain it may not have made. The chosen convention is the conservative one, and it
is stated rather than implied because two surfaces using different conventions would disagree by
exactly one day's move on every transfer.

WHY TWR, AND WHY IT IS THE PROPERTY WORTH TESTING
-------------------------------------------------
§5.2 asks for TWR for subscribed portfolios, strategies and screens. TWR exists for exactly one
reason: **it is flow neutral**. Chain-link each day's growth factor and the size and timing of
the user's transfers cancel out, so what is left measures the strategy rather than the user's
saving habits. That is what makes it the honest number to put next to a subscribed model: both
sides are then measuring the same thing.

XIRR measures the opposite thing on purpose — the user's own money-weighted experience, timing
included — which is why §5.2's consolidated row shows **both, labelled**, and never one. A user
who added at the bottom has an XIRR far above the TWR, and neither number is wrong; presenting
one of them unlabelled is what would be wrong (criterion 3).

Flow neutrality is a property, not an implementation detail, so ``test_portfolio_nav.py`` proves
it directly: the same market path with and without a mid-series cash assign produces the same
TWR to the digit. A test that only checked a hand-computed value against this code would pass
just as happily on an implementation that quietly counted the transfer as profit.

WHY THE DRAWDOWN IS DRAWN ON A WEALTH INDEX AND NOT ON THE VALUE COLUMN
----------------------------------------------------------------------
The obvious implementation of §6.3's drawdown overlay is a running maximum over ``value``. It is
wrong for the same reason as above, and the failure is worse than a wrong number: assign cash
while the portfolio is 20% below its high and the value column prints a brand new all-time peak,
so the chart says the user recovered on a day the market did nothing. Every subsequent drawdown
is then measured from a peak that never happened.

So the drawdown is computed on the chain-linked wealth index — the growth of one rupee left alone
from the start — where a transfer contributes a factor of exactly 1 and changes nothing. The test
asserts precisely that case, because the naive version passes every test that has no flows in it.

WHY A DRAWDOWN IS NOT A ``ReturnFigure``
----------------------------------------
Every *return* this module hands back is a :class:`~baskfy_core.allocation_ledger.ReturnFigure`,
because criterion 3 says a displayed return must state what it is and when it starts, and a bare
number cannot. A drawdown is not a return: ``MetricKind`` has no member that describes it, and
borrowing one — labelling a drawdown "TWR since you subscribed" — would break the very criterion
the type exists to enforce. :class:`MaxDrawdown` is therefore its own small type whose name is
its label, and it carries the peak and trough dates a chart needs to shade the period.

WHY XIRR APPEARS HERE ONLY AS A REFUSAL
---------------------------------------
§5.2 is explicit about holding groups: *"Until then, do NOT display XIRR or since-purchase P&L
for these."* A holding group is a set of shares the user already owned; the broker sync knows the
quantity and not the price they were bought at, so there is no cash-flow history to solve. The
honest output is not a plausible number derived from an average price nobody supplied — it is a
figure with no value and a reason that tells the user what to do about it (import their CAS,
§5.3). :func:`since_purchase_figure` is that refusal, and it is computed rather than asserted: it
calls the ledger's own :func:`~baskfy_core.curated_accounting.xirr`, which returns ``None``
exactly when there is nothing to solve.

The other half — the true XIRR a holding group unlocks *after* a CAS import — is deliberately not
produced here, and this is the one place the module stops short of §5.2 on purpose.
``MetricKind`` (owned by ``allocation_ledger``, and the contract every surface labels against) has
no member meaning "money-weighted return". Returning such a figure would mean either inventing a
second, parallel metric vocabulary in this module — the duplication that guarantees two surfaces
eventually disagree about one number — or labelling an XIRR ``SINCE_GROUPED``, which is criterion
3's failure with extra steps. So :func:`since_purchase_figure` raises when handed flows that do
solve, and says which one-line change to ``MetricKind`` unblocks it. Loud and reversible beats a
quiet mislabel.

MONEY IS ``Decimal`` AND RETURNS ARE QUANTISED ONCE
---------------------------------------------------
House rule 9, and house rule 8's "round at write time" applied to a derived number: rupee amounts
go through :func:`baskfy_core.gst.money` so the API, the chart and the CSV export cannot disagree
by a paisa, and return *ratios* are quantised once, at the end, to :data:`RETURN_PRECISION`.
Quantising the intermediate growth factors instead was rejected: chain-linking a few hundred
rounded factors accumulates a visible error in the headline number, and the headline is the one
users compare against their broker's app.

Division is inexact by nature (one third has no exact decimal expansion), so the chain product is
taken in a widened arithmetic context — the same technique and the same reasoning as
:mod:`baskfy_core.portfolio_units` — and rounded exactly once, where the user can see it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from itertools import pairwise
from typing import Final

from baskfy_core.allocation_ledger import (
    MetricKind,
    Portfolio,
    PortfolioSource,
    ReturnFigure,
    headline_metric,
)
from baskfy_core.curated_accounting import CashFlow, xirr
from baskfy_core.gst import money

__all__ = [
    "RETURN_PRECISION",
    "BenchmarkComparison",
    "Contribution",
    "DayPnl",
    "DrawdownPoint",
    "MaxDrawdown",
    "NavPoint",
    "benchmark_comparison",
    "consolidated_pnl",
    "contributions",
    "daily_pnl",
    "drawdown_series",
    "max_drawdown",
    "since_grouped_figure",
    "since_purchase_figure",
    "twr_figure",
]

#: Return ratios are stored and compared at six decimal places — a ten-thousandth of a percent.
#: Deep enough that chain-linking years of daily factors does not drift into the displayed digit,
#: shallow enough that two implementations of the same series compare equal instead of differing
#: in the 27th place. Money keeps its own precision (paise, via :func:`baskfy_core.gst.money`);
#: a ratio is not money and quantising it to paise would round 8.7% to 9%.
RETURN_PRECISION: Final = Decimal("0.000001")

_ONE: Final = Decimal(1)
_ZERO: Final = Decimal(0)

#: Decimal's default context carries 28 significant digits and *rounds* anything past them. A
#: chain product over a long series is exactly the shape that reaches it, and a rounded growth
#: factor is a rounding the user never asked for happening where nobody can see it. Widening the
#: context for the product costs nothing and moves every rounding decision to one visible place.
#: The same reasoning, and the same technique, as ``portfolio_units._EXACT_PRECISION``.
_WIDE_PRECISION: Final = 60

#: A return is a comparison between two marks, so two marks is the floor. Named rather than
#: written as a literal because it is a rule of the spec ("a start and an end"), not a tuning knob.
_MIN_MARKS_FOR_A_RETURN: Final = 2

#: :func:`baskfy_core.allocation_ledger.headline_metric` owns §5.2's source -> metric table, and
#: owns it privately. Asking it for a figure and keeping the label is how this module reads that
#: table without restating it: a table copied here would be a table free to drift from the one
#: the rest of the product labels against. The probe value is discarded; only ``.kind`` is used.
_KIND_PROBE: Final = Decimal(0)


def _quantise_return(value: Decimal) -> Decimal:
    """One rounding, half-up, at the boundary where a number becomes something a user reads."""
    return value.quantize(RETURN_PRECISION, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class NavPoint:
    """One official end-of-day valuation of one portfolio (§5.1).

    ``value`` is the portfolio's worth at that close, **after** any cash that moved in or out
    that day. ``net_flow`` is that movement: positive when the user assigned cash to the
    portfolio (§4.4's internal cash inflow, the XIRR event), negative on a withdrawal, zero on an
    ordinary day. Buying a stock *inside* the portfolio is not a flow — it is cash turning into
    shares, and §4.4 is explicit that it is not an XIRR event either.

    Both fields are ``Decimal``. A negative ``value`` is refused: v1 has no leverage and no short
    positions, so a negative mark means the caller passed a P&L where a valuation belongs, and
    that mistake is far cheaper to catch here than to explain from a chart later.
    """

    on: dt.date
    value: Decimal
    net_flow: Decimal = _ZERO

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError(
                f"a portfolio's end-of-day value cannot be negative; got {self.value} on "
                f"{self.on}. A negative mark usually means a P&L was passed where a valuation "
                "belongs"
            )


@dataclass(frozen=True, slots=True)
class DayPnl:
    """One day's profit and loss, with the day's transfers taken back out (§6.2).

    ``amount`` is in rupees and quantised to paise; ``pct`` is the same move as a fraction of the
    previous close, and is ``None`` when there was no previous close to be a fraction of — a
    portfolio that was worth nothing yesterday has no percentage move today, and printing 0%
    would claim it was flat.
    """

    on: dt.date
    amount: Decimal
    pct: Decimal | None


@dataclass(frozen=True, slots=True)
class DrawdownPoint:
    """One point of §6.3's drawdown overlay, on the wealth index rather than on rupees.

    ``index`` is the growth of one rupee left alone since the series began, ``peak`` is the
    highest that index has reached so far, and ``drawdown`` is ``index / peak - 1`` — zero at a
    new high and negative everywhere else, never positive.
    """

    on: dt.date
    index: Decimal
    peak: Decimal
    drawdown: Decimal


@dataclass(frozen=True, slots=True)
class MaxDrawdown:
    """The worst peak-to-trough fall in the series, with the dates a chart needs to shade it.

    Not a :class:`ReturnFigure`, and the docstring at the top of this module says why at length:
    ``MetricKind`` has no member that means "drawdown", and borrowing one would mislabel it.
    """

    peak_on: dt.date
    trough_on: dt.date
    drawdown: Decimal


@dataclass(frozen=True, slots=True)
class Contribution:
    """What one portfolio added to the consolidated move (§6.5).

    ``share`` is this portfolio's fraction of the consolidated number. It is **not** clamped and
    **not** taken in absolute value: on a day when one portfolio gains ₹1,000 and another loses
    ₹900, the consolidated move is ₹100 and the shares are +10 and -9. Those are the true
    contributions and they are worth showing; normalising them to something that sums to 1 with
    everything positive would be a prettier chart of a fact that did not happen.

    ``share`` is ``None`` when the consolidated move is exactly zero, because a share of nothing
    is undefined and 0% would claim the portfolio did nothing.
    """

    portfolio_id: int
    amount: Decimal
    share: Decimal | None


@dataclass(frozen=True, slots=True)
class BenchmarkComparison:
    """A portfolio's return, an index's return over the same window, and the gap — as three
    separate numbers (§6.3's overlay, §7's "benchmark diff").

    The type deliberately has no single ``value``. There is no way to render this object as "the
    return", which is the point: a benchmark is a second line on a chart and a third figure in a
    summary, never something folded into the portfolio's own number. ``portfolio`` is handed back
    exactly as it came in, untouched.

    ``benchmark`` carries the same :class:`MetricKind` and start date as ``portfolio`` because it
    is measured over the same window with the same method — that is what makes the difference
    meaningful. It never carries ``is_model``: an index is not a publisher's track record, and
    :func:`benchmark_comparison` refuses a model figure outright so that criterion 5's blend
    cannot be reached from this direction either.
    """

    name: str
    portfolio: ReturnFigure
    benchmark: ReturnFigure
    difference: Decimal | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if self.difference is None and not self.unavailable_reason:
            raise ValueError(
                "a comparison with no difference must say why, or the caller renders an empty "
                "cell and the user cannot tell 'level with the index' from 'we do not know'"
            )
        if self.difference is not None and self.unavailable_reason:
            raise ValueError("a comparison cannot both have a difference and be unavailable")


# ---------------------------------------------------------------------------
# The series itself — §5.1's "one official value per portfolio per day"
# ---------------------------------------------------------------------------


def _ordered(points: Sequence[NavPoint]) -> list[NavPoint]:
    """Sort by date and refuse two marks for the same day.

    §5.1's word is *official*: one value per portfolio per day, which is what lets a chart, a
    P&L row and a drawdown all be read off the same list without asking which of two marks for
    the 14th was meant. Two entries for one date is a caller bug — usually a nightly job that ran
    twice — and the honest response is to refuse rather than to pick one, because picking one
    silently changes every number downstream of it.

    Sorting is done rather than demanded: date order is unambiguous, so requiring the caller to
    pre-sort would buy nothing but an exception on a query with no ``ORDER BY``.
    """
    ordered = sorted(points, key=lambda point: point.on)
    for previous, current in pairwise(ordered):
        if previous.on == current.on:
            raise ValueError(
                f"two end-of-day valuations for {current.on}; a portfolio has exactly one "
                "official value per day (spec section 5.1), and choosing between them silently "
                "would change every number derived from the series"
            )
    return ordered


def _growth_factors(ordered: Sequence[NavPoint]) -> list[Decimal]:
    """Each day's growth of the money that was already there, with transfers removed.

    ``(value - net_flow) / previous_value``. The subtraction is what makes TWR flow neutral, and
    it is the only line in this module that has to be right for §5.2 to mean anything.

    A previous value of zero yields a factor of exactly 1 — a return of nothing. This is the
    first-funding case: a portfolio worth nothing yesterday earned nothing today, and the cash
    that arrived merely establishes the base the next factor is measured against. The tempting
    alternative, dividing anyway, raises on the most ordinary event in the product's life.
    """
    factors: list[Decimal] = []
    for previous, current in pairwise(ordered):
        if previous.value == 0:
            factors.append(_ONE)
            continue
        factors.append((current.value - current.net_flow) / previous.value)
    return factors


def _wealth_index(ordered: Sequence[NavPoint]) -> list[Decimal]:
    """The growth of one rupee left alone since the first mark, aligned with ``ordered``.

    Starts at 1 and is chain-linked from :func:`_growth_factors`, so a cash transfer multiplies it
    by 1 and moves it not at all. Both the headline return and the drawdown are read off this
    list, which is why they can never disagree about whether the portfolio is up.
    """
    with localcontext() as context:
        context.prec = _WIDE_PRECISION
        index = [_ONE]
        for factor in _growth_factors(ordered):
            index.append(index[-1] * factor)
    return index


def _chain_linked_return(points: Sequence[NavPoint]) -> Decimal | None:
    """The whole series' time-weighted return, or ``None`` when it cannot be measured.

    ``None`` for fewer than two marks — a return is a comparison, and one valuation is not one.
    Returning zero instead is the mistake this module refuses hardest: zero says "flat", and a
    portfolio valued once is not flat, it is unmeasured. Every caller turns the ``None`` into a
    :class:`ReturnFigure` carrying a reason a user can read.
    """
    ordered = _ordered(points)
    if len(ordered) < _MIN_MARKS_FOR_A_RETURN:
        return None
    return _quantise_return(_wealth_index(ordered)[-1] - _ONE)


def _kind_for(portfolio: Portfolio) -> MetricKind:
    """§5.2's headline metric for this portfolio's source, read from the ledger, never restated."""
    return headline_metric(portfolio, _KIND_PROBE).kind


def _figure_from_series(
    portfolio: Portfolio,
    points: Sequence[NavPoint],
    kind: MetricKind,
) -> ReturnFigure:
    """Turn a NAV series into a labelled figure, or into a labelled absence.

    The absence is the interesting half. An empty series and a one-mark series fail for different
    reasons and the user can act on the difference, so they get different sentences rather than
    one shared "unavailable".
    """
    ordered = _ordered(points)
    if ordered and ordered[0].on < portfolio.started_on:
        raise ValueError(
            f"the series starts on {ordered[0].on}, before {portfolio.name!r} began on "
            f"{portfolio.started_on}; measuring a portfolio over a window that predates it "
            "reports a return the user never had (acceptance criterion 3 ties every figure to "
            "its start date)"
        )
    if not ordered:
        return ReturnFigure(
            kind=kind,
            since=portfolio.started_on,
            unavailable_reason="No end-of-day valuation for this portfolio yet",
        )
    if len(ordered) == 1:
        return ReturnFigure(
            kind=kind,
            since=portfolio.started_on,
            unavailable_reason=(
                "Only one end-of-day valuation so far; a return needs a start and an end"
            ),
        )
    return ReturnFigure(
        kind=kind,
        since=portfolio.started_on,
        value=_quantise_return(_wealth_index(ordered)[-1] - _ONE),
    )


# ---------------------------------------------------------------------------
# §5.2 — the return metric differs by source, and every one of them is labelled
# ---------------------------------------------------------------------------


def twr_figure(portfolio: Portfolio, points: Sequence[NavPoint]) -> ReturnFigure:
    """Time-weighted return over the NAV series, labelled by source (§5.2).

    Subscribed portfolios get "TWR since you subscribed", strategies "TWR since go-live", screens
    "TWR since created" — three labels, one arithmetic, and the label comes from the ledger's own
    table rather than from a copy of it here.

    A holding group is refused rather than answered. Its headline is "since grouped" (§5.2), and
    while the arithmetic underneath is the same chain-link, the *claim* is not: TWR since created
    would tell a user their return runs from a date the portfolio's shares were not bought on.
    Routing it to :func:`since_grouped_figure` keeps the two claims distinguishable at the call
    site, where the person reading the code can still see which one they meant.
    """
    if portfolio.source is PortfolioSource.HOLDING_GROUP:
        raise ValueError(
            f"{portfolio.name!r} is a holding group, whose headline metric is 'since grouped' "
            "measured from the marks at its grouping date (spec section 5.2); call "
            "since_grouped_figure() so the number the user reads says what it actually is"
        )
    return _figure_from_series(portfolio, points, _kind_for(portfolio))


def since_grouped_figure(portfolio: Portfolio, points: Sequence[NavPoint]) -> ReturnFigure:
    """ "Since grouped" return for a holding group, from the EOD marks at the grouping date (§5.2).

    This is what a holding group *can* honestly say. The user grouped shares they already owned;
    we know what the group was worth at that close and at every close since, so we know how the
    group has done since it existed. We do not know what those shares cost, so we do not know how
    the user has done on them — that is :func:`since_purchase_figure`'s refusal, and it stays a
    separate number rather than being quietly upgraded into this one.

    Flow neutral for the same reason as TWR: adding a stock to the group later, or assigning cash
    to it, contributes a growth factor of 1 rather than a fictional gain.
    """
    if portfolio.source is not PortfolioSource.HOLDING_GROUP:
        raise ValueError(
            f"{portfolio.name!r} has source {portfolio.source}; 'since grouped' is a holding "
            "group's metric, and every other source has a real start event to measure a TWR "
            "from (spec section 5.2) — call twr_figure()"
        )
    return _figure_from_series(portfolio, points, MetricKind.SINCE_GROUPED)


def since_purchase_figure(
    portfolio: Portfolio,
    flows: Sequence[CashFlow],
    first_bought: dt.date | None = None,
) -> ReturnFigure:
    """§5.2's since-purchase figure for a holding group — or its refusal when there is no history.

    ``first_bought`` is the date the earliest share was actually bought, which a CAS import
    supplies (§5.3). It is the ``since`` of the returned figure, and it is deliberately *not*
    ``portfolio.started_on``: the grouping date is when Baskfy first saw the shares, the purchase
    date is when the money went in, and a return measured from one and labelled with the other is
    criterion 3's unlabelled column wearing a label that lies. Passing no date while the flows do
    solve is refused for the same reason.


    The refusal is *computed*, not asserted. It asks the ledger's own
    :func:`~baskfy_core.curated_accounting.xirr` to solve the flows, and that function returns
    ``None`` exactly when there is nothing solvable — no flows at all before a CAS import (§5.3),
    or flows that never change sign. Reusing the solver rather than checking a boolean flag means
    this refusal cannot disagree with the number the rest of the product would have computed.

    When the flows *do* solve, this raises instead of returning. The long explanation is in the
    module docstring; the short one is that ``MetricKind`` has no member meaning "money-weighted
    return", and the two ways to return a figure anyway are to invent a parallel metric
    vocabulary in this module or to label an XIRR ``SINCE_GROUPED``. The first guarantees two
    surfaces eventually disagree about one number; the second is exactly the unlabelled column
    criterion 3 forbids. Adding ``MetricKind.XIRR_SINCE_PURCHASE`` to ``allocation_ledger``
    unblocks it in one line, and that is a decision for the module that owns the vocabulary —
    which took it: :attr:`MetricKind.XIRR_SINCE_PURCHASE` now exists, so a solvable history
    returns a properly labelled figure instead of raising.
    """
    if portfolio.source is not PortfolioSource.HOLDING_GROUP:
        raise ValueError(
            f"{portfolio.name!r} has source {portfolio.source}; the since-purchase refusal is "
            "specific to holding groups, whose shares were bought before Baskfy saw them "
            "(spec section 5.2)"
        )
    rate = xirr(flows)
    if rate is not None and first_bought is None:
        raise ValueError(
            "these flows solve to a money-weighted return, but no first-bought date was given. "
            "The figure would have to be labelled with the grouping date, which is not the date "
            "it measures from (spec section 5.2)"
        )
    if rate is None:
        return ReturnFigure(
            kind=MetricKind.SINCE_GROUPED,
            since=portfolio.started_on,
            unavailable_reason=(
                "Import your CAS to see returns from your purchase dates. Until then we know "
                "what these shares are worth, not what you paid for them"
            ),
        )
    # `MetricKind.XIRR_SINCE_PURCHASE` was added to `allocation_ledger` for exactly this: the
    # figure is measured from the day the shares were BOUGHT, not from the day we first saw them,
    # and those are different dates telling different stories. `first_bought` is therefore the
    # `since`, not `portfolio.started_on` — labelling a purchase-dated return with the grouping
    # date would be criterion 3's unlabelled column wearing a label that lies.
    # The guard above already refused a solvable rate with no date; naming it here is what lets
    # the type checker see that, without a suppression comment house rule 3 forbids.
    bought_on = first_bought
    if bought_on is None:  # pragma: no cover - unreachable past the guard above
        raise ValueError("a since-purchase figure needs the date it is measured from")
    return ReturnFigure(
        kind=MetricKind.XIRR_SINCE_PURCHASE,
        since=bought_on,
        value=rate.quantize(RETURN_PRECISION, rounding=ROUND_HALF_UP),
    )


# ---------------------------------------------------------------------------
# §6.3 — drawdown
# ---------------------------------------------------------------------------


def drawdown_series(points: Sequence[NavPoint]) -> list[DrawdownPoint]:
    """The drawdown overlay, computed on the wealth index so transfers cannot fake a recovery.

    Empty in, empty out: a series with no marks has no drawdown, and a list with one point whose
    drawdown is zero would claim a portfolio nobody has valued is at its all-time high.
    """
    ordered = _ordered(points)
    if not ordered:
        return []
    index = _wealth_index(ordered)
    peak = index[0]
    series: list[DrawdownPoint] = []
    for point, level in zip(ordered, index, strict=True):
        peak = max(peak, level)
        series.append(
            DrawdownPoint(
                on=point.on,
                index=_quantise_return(level),
                peak=_quantise_return(peak),
                drawdown=_quantise_return(level / peak - _ONE),
            )
        )
    return series


def max_drawdown(points: Sequence[NavPoint]) -> MaxDrawdown | None:
    """The worst fall in the series and the peak it fell from, or ``None`` if unmeasurable.

    ``None`` for fewer than two marks, for the same reason :func:`_chain_linked_return` returns
    ``None``: a drawdown of 0.00% is a claim ("this portfolio has never fallen") and one
    valuation does not support it.

    A series that only ever rose returns a drawdown of zero with its peak and trough on the same
    date. That is a real answer rather than an absence — the portfolio genuinely never fell — and
    a chart can shade a zero-width band or nothing at all.

    Ties go to the earlier trough: when the same depth is reached twice, the first arrival is the
    one the user remembers, and choosing deterministically matters more than which side wins.
    """
    series = drawdown_series(points)
    if len(series) < _MIN_MARKS_FOR_A_RETURN:
        return None
    worst = series[0]
    worst_peak_on = series[0].on
    running_peak_on = series[0].on
    for point in series:
        if point.drawdown == 0:
            running_peak_on = point.on
        if point.drawdown < worst.drawdown:
            worst = point
            worst_peak_on = running_peak_on
    return MaxDrawdown(peak_on=worst_peak_on, trough_on=worst.on, drawdown=worst.drawdown)


# ---------------------------------------------------------------------------
# §6.2 and §6.5 — per-day P&L and per-portfolio contribution
# ---------------------------------------------------------------------------


def daily_pnl(points: Sequence[NavPoint]) -> list[DayPnl]:
    """Each day's move in rupees and per cent, against the previous close (§6.2).

    The day's ``net_flow`` is subtracted, so assigning cash to a portfolio produces a P&L of
    exactly zero on a day the market did not move. This is the same rule §4.5 states for
    corporate actions — a split changes quantity and produces no P&L — applied to the other
    non-event that changes a portfolio's value.

    One entry per *transition*, so a series of five marks yields four days. The first mark has no
    previous close and therefore no move; emitting a zero for it would put a flat day on the
    chart that never happened.
    """
    ordered = _ordered(points)
    rows: list[DayPnl] = []
    for previous, current in pairwise(ordered):
        move = current.value - current.net_flow - previous.value
        rows.append(
            DayPnl(
                on=current.on,
                amount=money(move),
                pct=(None if previous.value == 0 else _quantise_return(move / previous.value)),
            )
        )
    return rows


def consolidated_pnl(pnl_by_portfolio: Mapping[int, Decimal]) -> Decimal:
    """The consolidated move: every capital portfolio's P&L, each quantised, then summed.

    Quantising per portfolio *before* summing is deliberate and is the same rule
    ``allocation_ledger.portfolio_value`` follows: the total a user reads must equal the sum of
    the rows they can see. Rounding only the total leaves a headline that disagrees with its own
    breakdown by a paisa, and that is the discrepancy users write in about.

    Monitoring views are §4.1-excluded from every total; they are excluded here by not being in
    the mapping, which is the caller's job and the ledger's rule, not a filter this function
    could forget to apply.
    """
    total = _ZERO
    for amount in pnl_by_portfolio.values():
        total += money(amount)
    return money(total)


def contributions(pnl_by_portfolio: Mapping[int, Decimal]) -> list[Contribution]:
    """Each portfolio's share of the consolidated move, in portfolio-id order (§6.5).

    The amounts sum to :func:`consolidated_pnl` exactly, by construction rather than by
    assertion: both quantise the same values the same way and then add them, so the parts and the
    whole are one walk over one mapping and cannot drift.
    """
    total = consolidated_pnl(pnl_by_portfolio)
    rows: list[Contribution] = []
    for portfolio_id in sorted(pnl_by_portfolio):
        amount = money(pnl_by_portfolio[portfolio_id])
        rows.append(
            Contribution(
                portfolio_id=portfolio_id,
                amount=amount,
                share=(None if total == 0 else _quantise_return(amount / total)),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# §6.3 and §7 — the benchmark overlay, kept beside the portfolio's number
# ---------------------------------------------------------------------------


def benchmark_comparison(
    figure: ReturnFigure,
    benchmark: Sequence[NavPoint],
    *,
    name: str,
) -> BenchmarkComparison:
    """The portfolio's figure, the index over the same window, and the gap — never blended.

    ``benchmark`` is an index level series: Nifty 500's close on each of the same days. It is
    modelled with :class:`NavPoint` rather than a second type because an index *is* a NAV with no
    flows — and that "no flows" is enforced, not assumed. A benchmark point carrying a transfer
    means the caller passed a portfolio's series by mistake, and comparing a portfolio against
    another portfolio while calling it a benchmark is the kind of wrong number that survives
    review.

    A model figure is refused outright. Criterion 5 forbids blending a publisher's track record
    with the user's actual performance, and while an index is neither, a difference computed
    against a model figure would be labelled "vs Nifty 500" and read as the user's own gap. The
    refusal closes that route from this direction, the way
    ``allocation_ledger.model_figure`` closes it from the other.

    A window that starts before the portfolio's own start date is refused too: the index would be
    measured over more days than the portfolio and the gap would be an artefact of the calendar.
    """
    if figure.is_model:
        raise ValueError(
            "a benchmark difference cannot be taken against a publisher's model figure: the "
            "result would read as this user's gap to the index when it is the model's "
            "(acceptance criterion 5)"
        )
    ordered = _ordered(benchmark)
    for point in ordered:
        if point.net_flow != 0:
            raise ValueError(
                f"the benchmark series carries a cash flow of {point.net_flow} on {point.on}; an "
                "index has no subscriptions and no redemptions, so this is a portfolio's series "
                "passed where a benchmark belongs"
            )
    if ordered and ordered[0].on < figure.since:
        raise ValueError(
            f"the benchmark series starts on {ordered[0].on}, before the portfolio's own start "
            f"of {figure.since}; the difference would be measured over two different windows"
        )
    index_return = _chain_linked_return(ordered)
    benchmark_figure = _benchmark_figure(figure, index_return)
    if figure.value is None or index_return is None:
        return BenchmarkComparison(
            name=name,
            portfolio=figure,
            benchmark=benchmark_figure,
            unavailable_reason=(
                figure.unavailable_reason
                if figure.value is None
                else f"No {name} history for this period yet"
            ),
        )
    return BenchmarkComparison(
        name=name,
        portfolio=figure,
        benchmark=benchmark_figure,
        difference=_quantise_return(figure.value - index_return),
    )


def _benchmark_figure(figure: ReturnFigure, index_return: Decimal | None) -> ReturnFigure:
    """The index's own labelled number: same window, same method, never ``is_model``."""
    if index_return is None:
        return ReturnFigure(
            kind=figure.kind,
            since=figure.since,
            unavailable_reason="No index history for this period yet",
        )
    return ReturnFigure(kind=figure.kind, since=figure.since, value=index_return)
