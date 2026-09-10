"""``/portfolio/*`` — the read API behind ``PORTFOLIO_REDESIGN.md`` §6 and §7.

The spec's one-sentence summary says the page is thin once the ledger exists. This router is
that thinness made literal: it loads rows, hands them to :mod:`baskfy_core.allocation_ledger`,
:mod:`baskfy_core.cash_ledger`, :mod:`baskfy_core.portfolio_nav` and
:mod:`baskfy_core.reconciliation`, and renders whatever those four answer. **No arithmetic rule
lives here.** Not the exclusion of monitoring views from totals, not the choice of headline
metric, not the freeze, not the split-preserves-cost-basis identity. A rule implemented twice is
a rule two surfaces will eventually disagree about, and the surface a user meets is whichever
ran last.

THE SEVEN ROUTES, AND WHY THESE SEVEN
-------------------------------------
``GET /portfolio/overview``         §6 — one screen: hero, chart, ribbon, table, unallocated.
``GET /portfolio/holdings``         §2 — the flat broker-level truth, aggregated per §6.7.
``GET /portfolio/{id}``             §7 — summary, holdings with weight and contribution, source.
``GET /portfolio/{id}/nav``         §6.3 — one portfolio's EOD NAV series, range-filtered.
``GET /portfolio/activity``         §7 — trades, cash, dividends, actions, inbox history.
``GET /portfolio/reconciliation``   §4.3 — the inbox.
``POST /portfolio/reconciliation/{item_id}/resolve`` — answering one question in it.

Six reads and one answer. **There is no execute route and there will not be one** (§9): nothing
in this module names a side, a product, a venue, an order type or a broker session, and
``test_portfolio_overview.py`` greps the source for that vocabulary so an edit that quietly
crosses the line fails a test rather than a review. Rebalance output stays an order plan the user
takes to their broker, exactly as it is today.

The literal paths are declared **before** ``/{portfolio_id}``. FastAPI matches routes in
declaration order, so ``/portfolio/overview`` would otherwise be offered to the detail handler
and answered with a 422 about an unparseable integer.

WHY THE COMBINED CHART LIVES INSIDE ``/overview``
--------------------------------------------------
§6.3's combined chart is the *consolidated* NAV series (``portfolio_nav_daily.portfolio_id IS
NULL``), and §6.1's header, §6.2's hero and that chart are one screen that must agree with each
other about the same day. Serving the chart from a second route would let a client render a
header from one response and a chart from another taken a second later, across a nightly job
boundary — a page whose two halves disagree about what a portfolio was worth. So ``/overview``
takes the same ``range`` filter the per-portfolio series takes and embeds the consolidated
series, and ``/{id}/nav`` serves one portfolio's.

THE PAYLOAD RULES THIS MODULE EXISTS TO ENFORCE
-----------------------------------------------
1. **No bare return number** (criterion 3). Every rate in every payload carries what it is and
   the date it is measured from. Two shapes do that, and the difference between them is not
   cosmetic — see :class:`ReturnFigureOut` and :class:`LabelledRateOut`.
2. **Model and actual are never one field** (criterion 5). A subscribed portfolio's
   ``headline_return`` is the user's own; the publisher's record is ``model_return``, a separate
   field built by :func:`~baskfy_core.allocation_ledger.model_figure`, carrying ``is_model`` and
   a label that says so. There is no code path that adds them.
3. **A monitoring view is never in a total** (criterion 2, §4.1). They are not merely absent from
   the sum — they are in a *different list*, :attr:`OverviewOut.monitoring_views`, so a client
   cannot sum ``portfolios`` and accidentally include one. Their values come from a separate
   walk; :func:`~baskfy_core.allocation_ledger.portfolio_values` never sees them, because they
   hold no allocation to be seen.
4. **§9's wording.** Third-party content is "Subscribed model by {publisher}", "Published by",
   "Curated by". The words *managed*, *managed portfolio*, *advisory* and *PMS* appear nowhere in
   any string this module emits, and a test asserts that over the rendered payloads rather than
   over the source, so a phrase assembled at runtime cannot slip past it.
5. **Tenancy.** Every statement is scoped by ``user_id``, and a portfolio, a reconciliation item
   or a broker account that is not the caller's answers ``NOT_FOUND``. Never ``FORBIDDEN``: a 403
   confirms the id names a real row, which is the fact a stranger is probing for. Same rule the
   rest of this service already follows for screens and portfolios.

WHAT "TODAY" MEANS HERE, AND WHY IT IS NOT THE NAV SERIES
---------------------------------------------------------
§6.2 words the hero's second tile precisely: "Today's P&L (₹ and %, **vs previous close**)". So
it is computed the way it is worded — from the two most recent closes of the instruments the user
actually holds — and *not* from ``portfolio_nav_daily``. The reason is agreement rather than
convenience: ``current_value`` is the ledger valued at the latest close, and a P&L taken from a
different series would not be the difference between two numbers on the same page. The NAV series
answers the questions §5.1 gives it — the chart, the return figures, the drawdown — and those are
the questions where a stored, dated, unrewritable mark is the point.

A cash assignment moves no consolidated value (it moves money between two buckets inside it), so
the consolidated day move needs no flow adjustment. A *portfolio's* day move does, and that is
why the per-portfolio series, not the price difference, feeds every figure in §5.2.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import AuthenticatedDep, Principal
from baskfy_api.db import SessionDep
from baskfy_api.live_prices import live_prices_by_instrument
from baskfy_api.problems import Problem, ProblemType, not_found
from baskfy_core.allocation_ledger import (
    UNALLOCATED,
    Allocation,
    Holding,
    HoldingKey,
    MetricKind,
    PortfolioKind,
    PortfolioSource,
    ReconciliationReason,
    ReturnFigure,
    consolidated_value,
    cost_basis,
    holding_value,
    model_figure,
    portfolio_values,
    unallocated_holdings,
)
from baskfy_core.allocation_ledger import (
    Portfolio as LedgerPortfolio,
)
from baskfy_core.allocation_ledger import (
    ReconciliationItem as LedgerQuestion,
)
from baskfy_core.cash_ledger import (
    CashFlowKind,
    portfolio_cash,
    portfolio_xirr,
)
from baskfy_core.cash_ledger import (
    PortfolioCashFlow as LedgerCashFlow,
)
from baskfy_core.grouping_suggestions import (
    GroupingSuggestion,
    SubscribedBasket,
    SuggestionBasis,
    SuggestionInputs,
    suggest_groupings,
)
from baskfy_core.gst import money
from baskfy_core.models import (
    BrokerAccount,
    CbBasket,
    CbBasketVersion,
    CbConstituent,
    CbInvestment,
    CbManager,
    CbMetrics,
    CbUserRebalanceState,
    CorporateAction,
    IndexDef,
    IndexMemberDaily,
    IndexSnapshotDaily,
    Instrument,
    OhlcvDaily,
    Portfolio,
    PortfolioHolding,
    PortfolioSleeve,
    Screen,
)
from baskfy_core.models.accounts import (
    BrokerCash,
    PortfolioCashFlow,
    PortfolioNavDaily,
    ReconciliationItem,
)
from baskfy_core.portfolio_nav import (
    RETURN_PRECISION,
    DrawdownPoint,
    NavPoint,
    benchmark_comparison,
    daily_pnl,
    drawdown_series,
    max_drawdown,
    since_grouped_figure,
    twr_figure,
)
from baskfy_core.reconciliation import (
    AttentionInputs,
    AttentionItem,
    FreezeReport,
    InboxEntry,
    LedgerPosition,
    ReconciliationState,
    attention_items,
    freeze_report,
    resolve,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

ZERO: Final = Decimal("0")
ONE: Final = Decimal("1")

#: §6.6's primary call to action, spelled once. §8 renamed *divide*, *file under* and *nest* out
#: of the product; this is the sentence that replaced them, and a second spelling of it elsewhere
#: is how the two surfaces start to differ.
ORGANIZE_CTA: Final = "Organize into portfolios"

#: §4.1's sentence, verbatim, attached to every monitoring row in every listing. It is a field
#: rather than a class name on the client because the client must be able to *show* it: a muted
#: row with no explanation reads as a bug, not as a deliberate exclusion.
MONITORING_NOTE: Final = "Monitoring view — overlaps with other portfolios, excluded from totals."

#: §9. The only permitted framing for content somebody else published. Baskfy is not
#: SEBI-registered, so "managed", "advisory" and "PMS" are not available words — see the module
#: docstring's rule 4 and the test that asserts it over rendered payloads.
_SUBSCRIBED_BADGE: Final = "Subscribed model by {publisher}"
_UNKNOWN_PUBLISHER: Final = "an unnamed publisher"

#: §3's badge for the three sources the user built themselves. Holding-group wording avoids §8's
#: left column entirely: no *box*, no *book*, no *sleeve*, no *run by hand*.
_SOURCE_BADGE: Final[Mapping[PortfolioSource, str]] = {
    PortfolioSource.MY_SCREEN: "My screen",
    PortfolioSource.MY_STRATEGY: "My strategy",
    PortfolioSource.HOLDING_GROUP: "Holding group",
}

#: §6.5's Status column. Ordered by severity, and read top down: the first that applies wins.
_STATUS_PENDING: Final = "Pending reconciliation"
_STATUS_REBALANCE: Final = "Rebalance due"
_STATUS_SYNCED: Final = "Synced"

#: A return is a comparison, so it needs two marks; a drawdown needs a peak and a trough for
#: the same reason. Named because the number is a rule — ``portfolio_nav`` refuses to turn one
#: valuation into a zero, and every place that decides whether to ask it should say why, not 2.
_MIN_MARKS_FOR_A_RETURN: Final = 2

#: The two closes §6.2's "vs previous close" needs, per instrument.
_CLOSES_PER_INSTRUMENT: Final = 2

#: How many activity rows one page returns by default. An activity feed is read from the top; a
#: user who wants a year of it is exporting, not scrolling.
_ACTIVITY_DEFAULT: Final = 50
_ACTIVITY_MAX: Final = 200

#: The longest portfolio name this service accepts, matching what ``POST /portfolios`` has always
#: accepted (``baskfy_api.schemas.PortfolioCreate``). Two create routes that disagreed about how
#: long a name may be would be two products, and the user would meet whichever they clicked.
NAME_MAX: Final = 120

#: The 400 an allocation refusal answers with, and the reason it is spelled as an alias rather
#: than as a new member is the one ``routers.portfolios`` already recorded for ``TREE_VALIDATION``:
#: ``docs/07 §"Error catalogue"`` has exactly one 400, the generated TypeScript unions the type
#: strings, and adding a member would change the wire contract of every route at once. Named here
#: so that the day the catalogue gains a ledger-shaped 400, one line moves.
ALLOCATION_REFUSED: Final = ProblemType.INVALID_SCREEN_DEFINITION

#: Every basis §6.6 names, in the order the payload reports them. A list rather than a walk over
#: the enum, because the report is a promise about what was attempted: a basis that stopped being
#: attempted must disappear from here deliberately, not by an enum member being renamed.
_ALL_BASES: Final[tuple[SuggestionBasis, ...]] = (
    SuggestionBasis.BASKET_OVERLAP,
    SuggestionBasis.SECTOR,
    SuggestionBasis.PURCHASE_ERA,
)

#: The sentences §6.6's screen prints when an input is missing rather than empty. They name the
#: missing *input*, never the user's holdings — "we do not have sector data" and "your holdings
#: have nothing in common" are opposite messages and only one of them is true here.
_NO_SECTORS: Final = (
    "We do not have a sector for any of these holdings yet, so we cannot group them by sector."
)
_NO_PURCHASE_DATES: Final = (
    "We do not know when these holdings were bought, so we cannot group them by purchase year. "
    "Importing a CAS statement fills in the buy dates."
)
_NO_BASKETS: Final = (
    "You are not following any published model yet, so there is nothing for these holdings to "
    "overlap with."
)
_NOTHING_TO_SORT: Final = (
    "Everything you hold is already in a portfolio, so there is nothing here to sort."
)
_NO_INPUTS: Final = (
    "We have none of the three things a suggestion needs — no sectors, no purchase dates and no "
    "models you follow — so there is nothing we can propose yet. Sorting by hand still works."
)


class NavRange(StrEnum):
    """§6.3's ranges. 1D and 1W are absent, not disabled — §5.1 is end-of-day only in v1.

    A member that cannot honestly be served is worse than a missing one: it teaches a client to
    request a window the product answers with a straight line through one point.
    """

    M1 = "1M"
    M3 = "3M"
    Y1 = "1Y"
    Y3 = "3Y"
    ALL = "ALL"

    @property
    def days(self) -> int | None:
        """Calendar days back from the last mark, or ``None`` for the whole series."""
        return {
            NavRange.M1: 31,
            NavRange.M3: 92,
            NavRange.Y1: 366,
            NavRange.Y3: 1096,
            NavRange.ALL: None,
        }[self]


# ---------------------------------------------------------------------------
# The two shapes a rate may take, and the one shape a money move may take
# ---------------------------------------------------------------------------


class ReturnFigureOut(BaseModel):
    """One of §5.2's headline metrics, rendered with everything criterion 3 requires.

    ``kind`` is a :class:`~baskfy_core.allocation_ledger.MetricKind` value and nothing else. That
    enum is the product's whole metric vocabulary; a router that invented a sixth member would be
    the second vocabulary the ledger's own docstring warns about, and the two would drift.

    ``value`` is ``None`` exactly when ``unavailable_reason`` is set, and the reason is a sentence
    a user can read. An empty cell with no reason cannot be told apart from a zero, which is the
    failure :class:`~baskfy_core.allocation_ledger.ReturnFigure` refuses at construction.

    ``is_model`` is part of the payload rather than implied by the field name, so a client that
    renders figures generically still cannot present a publisher's record as the user's own.
    """

    kind: MetricKind
    #: What the column header says. Comes from the figure, never composed here.
    label: str
    #: The date the metric is measured from — criterion 3's "and its start date on hover".
    since: dt.date
    value: Decimal | None = None
    is_model: bool = False
    unavailable_reason: str | None = None


class LabelledRateOut(BaseModel):
    """A rate §5.2 asks for that :class:`MetricKind` has no member for — labelled, never bare.

    Two numbers are in this position and both are consolidated-level: §5.2's "show **XIRR** (the
    user's cash-flow-adjusted experience) and **TWR** (strategy quality) as two labeled numbers".
    Neither is a portfolio's headline metric, so neither has a ``MetricKind``, and the enum
    belongs to ``allocation_ledger`` — adding a member to it from here would be this router
    deciding the product's metric vocabulary.

    So the shape carries the same obligations without the enum: a label saying what the number is
    and the date it runs from. Criterion 3 asks for a label and a start date, and that is exactly
    what this is. The day ``MetricKind`` gains money-weighted and consolidated members, these two
    fields become a :class:`ReturnFigureOut` and this class goes away.
    """

    label: str
    since: dt.date | None = None
    value: Decimal | None = None
    unavailable_reason: str | None = None


class MoneyMoveOut(BaseModel):
    """A move in rupees with its percentage, over a window that is named rather than assumed.

    §6.2's "Today's P&L (₹ and %, vs previous close)" and "Total P&L". The percentage is a return
    number, so it carries a label and the date it is measured from, for the same reason every
    other rate here does. It is not a :class:`ReturnFigureOut` because a day's move is not one of
    §5.2's metrics and labelling it as one would state the wrong start event.

    ``pct`` is ``None`` when there is nothing to be a fraction of — a portfolio worth zero
    yesterday has no percentage move today, and 0% would claim it was flat.
    """

    amount: Decimal | None = None
    pct: Decimal | None = None
    label: str
    since: dt.date | None = None
    unavailable_reason: str | None = None


# ---------------------------------------------------------------------------
# Shared references
# ---------------------------------------------------------------------------


class BrokerRefOut(BaseModel):
    """One broker account, named. ``broker_id`` is the catalog id (``zerodha``, …)."""

    broker_account_id: int
    broker_id: str
    label: str


class PortfolioRefOut(BaseModel):
    """A portfolio named from somewhere else's payload — the id, the name, and its kind.

    ``kind`` travels with the name everywhere, because a reader who sees only a name cannot know
    whether the thing being named counts toward a total (§4.1).
    """

    portfolio_id: int
    name: str
    kind: PortfolioKind
    source: PortfolioSource


class InstrumentRefOut(BaseModel):
    instrument_id: int
    symbol: str
    name: str


class BrokerCashOut(BaseModel):
    """One broker account's Unallocated cash bucket (§4.4), with the day it is true for."""

    broker: BrokerRefOut
    balance: Decimal
    as_of: dt.date


class AttentionOut(BaseModel):
    """One row of §6.4's ribbon. The sentence comes from core, so every surface says it alike."""

    kind: str
    message: str
    count: int
    subject_ids: list[int] = Field(default_factory=list)
    since: dt.date | None = None


# ---------------------------------------------------------------------------
# §6.5 — the portfolio table
# ---------------------------------------------------------------------------


class PortfolioRowOut(BaseModel):
    """One row of §6.5's table, or one muted row of its Monitoring views tab.

    ``counts_toward_total`` is stated rather than inferred from ``kind``. A client that has to
    map kinds to arithmetic is a client that can get the mapping wrong; this field is the answer,
    and for a monitoring view it is ``False`` with :attr:`excluded_note` saying why in §4.1's own
    words.

    ``model_return`` is present only on a subscribed portfolio and is *never* the same field as
    ``headline_return`` (criterion 5). One is what this user experienced; the other is what the
    publisher reported. They are measured over the same window and they are still two numbers.
    """

    portfolio_id: int
    name: str
    kind: PortfolioKind
    source: PortfolioSource
    #: §3's badge, and for a subscribed portfolio §9's exact framing.
    source_badge: str
    publisher: str | None = None
    started_on: dt.date
    value: Decimal
    cash: Decimal
    counts_toward_total: bool
    excluded_note: str | None = None
    todays_pnl: MoneyMoveOut
    headline_return: ReturnFigureOut
    #: The publisher's own record. ``None`` for everything the user built themselves — there is
    #: no publisher whose record it could be, and an empty labelled figure would imply one.
    model_return: ReturnFigureOut | None = None
    brokers: list[BrokerRefOut] = Field(default_factory=list)
    #: §8 renamed "spans brokers" to "Connected to N brokers"; this is the N.
    broker_count: int = 0
    holdings_count: int = 0
    status: str
    pending_reconciliation: bool = False
    benchmark_name: str | None = None


# ---------------------------------------------------------------------------
# §6.6 — the unallocated section
# ---------------------------------------------------------------------------


class UnallocatedHoldingOut(BaseModel):
    """A holding in no capital portfolio, aggregated across brokers for display (§6.7)."""

    instrument: InstrumentRefOut
    quantity: Decimal
    value: Decimal
    brokers: list[BrokerRefOut] = Field(default_factory=list)
    #: The monitoring views this holding appears in. A lens is not an allocation (§4.1), so a
    #: holding that is only in lenses is still unallocated — and saying which lenses is what
    #: stops that from reading as a mistake.
    monitoring_views: list[PortfolioRefOut] = Field(default_factory=list)
    pending_reconciliation: bool = False


class UnallocatedOut(BaseModel):
    """§6.6's centerpiece: cash plus unassigned holdings, with a total and a way out.

    Present in every overview response, including when it is empty. A section that appears only
    when something is wrong is a section users learn to read as an alarm; §6.6 makes it the place
    a new account starts, which means it has to be a place that exists.
    """

    cash: Decimal
    cash_by_broker: list[BrokerCashOut] = Field(default_factory=list)
    holdings: list[UnallocatedHoldingOut] = Field(default_factory=list)
    holdings_value: Decimal
    holdings_count: int
    #: Cash plus holdings. The number §6.6 puts next to the heading.
    total_value: Decimal
    pending_reconciliation: bool = False
    cta: str = ORGANIZE_CTA


# ---------------------------------------------------------------------------
# §6.1 / §6.2 — the header and the hero
# ---------------------------------------------------------------------------


class SyncStatusOut(BaseModel):
    """§6.1's per-broker sync status. One row per connected account, dated."""

    broker: BrokerRefOut
    #: The day this account's holdings and cash are true for. ``None`` = never synced.
    synced_on: dt.date | None = None
    label: str


class HeroSecondaryOut(BaseModel):
    """§6.2's collapsed second row: cash, the realised/unrealised split, dividends, brokers."""

    cash: Decimal
    dividends: Decimal
    broker_count: int
    #: Market value minus cost basis, over the holdings whose cost basis is known.
    unrealised_pnl: MoneyMoveOut
    #: §5.3. A realised figure needs the purchase price of shares that are no longer held, and
    #: nothing in the ledger knows it before a CAS import — so this is a labelled absence rather
    #: than a zero, which would claim the user has never sold at a profit.
    realised_pnl: MoneyMoveOut


class HeroOut(BaseModel):
    """§6.2's five hero metrics, plus the secondary row and the honesty flags around them.

    ``current_value`` is criterion 1's number: every capital portfolio plus Unallocated plus
    cash, computed by :func:`~baskfy_core.allocation_ledger.consolidated_value` from a single
    walk over the holdings. Monitoring views are not subtracted from it — they were never in it,
    because they hold no allocation for the walk to find.

    ``pending_reconciliation`` is the freeze reaching the headline (§4.3). One open question
    anywhere makes the consolidated figure one we cannot state honestly, and the flag is how the
    page says so instead of printing a confident total.
    """

    current_value: Decimal
    todays_pnl: MoneyMoveOut
    total_pnl: MoneyMoveOut
    xirr: LabelledRateOut
    twr: LabelledRateOut
    invested: Decimal | None = None
    invested_unavailable_reason: str | None = None
    secondary: HeroSecondaryOut
    pending_reconciliation: bool = False
    #: How many holdings have no purchase price yet (§5.3). The reason ``invested`` and
    #: ``total_pnl`` can be partial, reported rather than left to be inferred from a gap.
    holdings_without_cost_basis: int = 0


# ---------------------------------------------------------------------------
# §6.3 — the NAV series
# ---------------------------------------------------------------------------


class NavPointOut(BaseModel):
    """One stored end-of-day mark. ``net_flow`` travels with it, always — see
    :class:`~baskfy_core.portfolio_nav.NavPoint`: a value without its flow is a chart that reads
    a transfer as a profit."""

    on: dt.date
    value: Decimal
    cash: Decimal
    net_flow: Decimal
    pending_reconciliation: bool = False


class DayPnlOut(BaseModel):
    on: dt.date
    amount: Decimal
    pct: Decimal | None = None


class DrawdownPointOut(BaseModel):
    on: dt.date
    index: Decimal
    peak: Decimal
    drawdown: Decimal


class MaxDrawdownOut(BaseModel):
    peak_on: dt.date
    trough_on: dt.date
    drawdown: Decimal


class BenchmarkOut(BaseModel):
    """§6.3's overlay and §7's "benchmark diff" — three numbers, never one.

    The portfolio's figure, the index's over the same window, and the gap. Built by
    :func:`~baskfy_core.portfolio_nav.benchmark_comparison`, which refuses a model figure so that
    criterion 5's blend cannot be reached from the benchmark direction either.
    """

    name: str
    portfolio: ReturnFigureOut
    benchmark: ReturnFigureOut
    difference: Decimal | None = None
    points: list[NavPointOut] = Field(default_factory=list)


class NavSeriesOut(BaseModel):
    """§6.3's chart data: the marks, the per-day moves, the drawdown, and the labelled return.

    ``portfolio_id`` is ``None`` for the consolidated series, matching
    ``portfolio_nav_daily.portfolio_id`` — one table, one shape, so the combined chart and a
    single portfolio's chart can never disagree about what a mark means.
    """

    portfolio_id: int | None = None
    range: NavRange
    from_on: dt.date | None = None
    to_on: dt.date | None = None
    points: list[NavPointOut] = Field(default_factory=list)
    daily_pnl: list[DayPnlOut] = Field(default_factory=list)
    drawdown: list[DrawdownPointOut] = Field(default_factory=list)
    max_drawdown: MaxDrawdownOut | None = None
    #: The chain-linked return over the marks shown. Labelled, like everything else here.
    total_return: LabelledRateOut
    benchmark: BenchmarkOut | None = None
    #: True when any mark in the window was taken on a day whose value could not be trusted
    #: (§4.3's freeze, carried onto the row by migration 0022).
    pending_reconciliation: bool = False


class OverviewOut(BaseModel):
    """§6, in one response. The two timestamps §6.1 requires are separate fields, deliberately.

    A price date and a sync date answer different questions — "how old is the market data" and
    "how old is our copy of what you own" — and a single "last updated" that quietly reports the
    older of the two is how a user comes to believe a stale holdings list is a stale price. They
    are two fields with two labels and they are never merged.

    ``holdings_synced_on`` is a **date**, not a wall-clock time, because a date is the finest
    truth the schema holds: ``broker_cash.as_of`` records the day a balance is true for, and
    nothing records the instant a sync ran. Rendering a fabricated time would be the page
    claiming a precision the data does not have. When a sync timestamp column exists, this field
    becomes a datetime and its label stops saying "close of".
    """

    #: §6.1: "Prices: close of {date}". ``None`` when no close has been recorded at all.
    prices_as_of: dt.date | None = None
    prices_label: str
    #: §6.1: "Holdings synced: {time}". See the class docstring for why it is a date.
    holdings_synced_on: dt.date | None = None
    holdings_synced_label: str
    sync_status: list[SyncStatusOut] = Field(default_factory=list)
    hero: HeroOut
    chart: NavSeriesOut
    attention: list[AttentionOut] = Field(default_factory=list)
    #: §6.5's table. Capital portfolios only — every row here counts toward the totals above.
    portfolios: list[PortfolioRowOut] = Field(default_factory=list)
    #: §6.5's muted tab, kept in a **separate list** so summing the table cannot include one.
    monitoring_views: list[PortfolioRowOut] = Field(default_factory=list)
    unallocated: UnallocatedOut
    open_reconciliation_count: int = 0
    #: Stated on the wire so a client never has to know §4.1 to render the page correctly.
    monitoring_excluded_note: str = MONITORING_NOTE


# ---------------------------------------------------------------------------
# §2 / §6.7 — the flat holdings truth
# ---------------------------------------------------------------------------


class HoldingSliceOut(BaseModel):
    """One capital portfolio's share of one physical position (0035).

    The wire form of `allocation_ledger.Allocation`. It exists because a holding may now be filed
    into several portfolios at once, and every surface that used to print one portfolio's name
    needs to be able to print four with their quantities instead.
    """

    portfolio: PortfolioRefOut
    quantity: Decimal


class HoldingBrokerLineOut(BaseModel):
    """One physical position: this instrument, at this broker, and what it is allocated to.

    ``allocation`` is ``None`` for Unallocated — a complete, legitimate state, never an error
    (§6.6). The monitoring views the position appears in are listed separately, because
    appearing in a lens is not being allocated (§4.1).
    """

    broker: BrokerRefOut
    quantity: Decimal
    #: 0035: how many of `quantity` are not filed into any capital portfolio yet — the most the
    #: picker may offer for a new one. Sent because the client cannot derive it: it would have to
    #: know every portfolio this leg already appears in, which is exactly what `allocations`
    #: below carries but which a checkbox has no way to add up.
    unallocated_quantity: Decimal = Decimal(0)
    #: Every capital slice of this leg, so the picker can say "already 20 in Long term" and the
    #: detail page can show where the shares went. Empty for a wholly unfiled position.
    allocations: list[HoldingSliceOut] = Field(default_factory=list)
    price: Decimal | None = None
    value: Decimal | None = None
    avg_price: Decimal | None = None
    cost_basis: Decimal | None = None
    #: The single portfolio this leg is filed into, when there is exactly one. ``None`` when it is
    #: unfiled **or split** — a caption naming the first of four would be wrong three times out
    #: of four, so `allocations` is the field to read when this is null but the leg is filed.
    allocation: PortfolioRefOut | None = None
    monitoring_views: list[PortfolioRefOut] = Field(default_factory=list)
    first_bought_on: dt.date | None = None
    history_source: str = "NONE"
    pending_reconciliation: bool = False


class AggregatedHoldingOut(BaseModel):
    """§6.7: "HDFC Bank — 320 (Zerodha 200 · Upstox 120)".

    Aggregated for display, with the broker breakdown preserved rather than summarised away. The
    ledger keeps the two positions apart on purpose — they can be allocated apart and sold apart
    — so this row's ``allocation`` is filled only when every broker line agrees about it.
    ``split_across_portfolios`` is what says otherwise, instead of one of the two answers
    silently standing for both.
    """

    instrument: InstrumentRefOut
    quantity: Decimal
    price: Decimal | None = None
    price_as_of: dt.date | None = None
    value: Decimal | None = None
    #: Filled when every broker line names the same capital portfolio; ``None`` when they differ
    #: or when the holding is unallocated.
    allocation: PortfolioRefOut | None = None
    allocated: bool = False
    split_across_portfolios: bool = False
    monitoring_views: list[PortfolioRefOut] = Field(default_factory=list)
    brokers: list[HoldingBrokerLineOut] = Field(default_factory=list)
    pending_reconciliation: bool = False


class HoldingsOut(BaseModel):
    """§2's flat broker-level truth: every share, which broker, what it is allocated to."""

    prices_as_of: dt.date | None = None
    prices_label: str
    holdings_synced_on: dt.date | None = None
    holdings_synced_label: str
    rows: list[AggregatedHoldingOut] = Field(default_factory=list)
    total_value: Decimal
    #: How many aggregated rows are in no capital portfolio. §6.6's number, repeated here so the
    #: Holdings tab can point at the same work without recounting it differently.
    unallocated_count: int = 0
    #: Instruments held with no close on record at all. They are excluded from ``total_value``
    #: and named here, because a total that silently drops a position is wrong and still adds up.
    unpriced_instrument_ids: list[int] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §7 — the detail page
# ---------------------------------------------------------------------------


class DetailHoldingOut(BaseModel):
    """§7's holdings tab: weight, today's contribution, total contribution, broker.

    ``total_contribution`` is ``None`` when the purchase price is unknown — §5.2 forbids showing
    since-purchase P&L for a holding whose history has not been imported (§5.3), and a zero would
    be exactly the forbidden number wearing a plausible face.
    """

    instrument: InstrumentRefOut
    broker: BrokerRefOut
    quantity: Decimal
    avg_price: Decimal | None = None
    price: Decimal | None = None
    value: Decimal | None = None
    #: This holding's share of the portfolio's market value, as a fraction.
    weight: Decimal | None = None
    todays_contribution: Decimal | None = None
    total_contribution: Decimal | None = None
    first_bought_on: dt.date | None = None
    history_source: str = "NONE"
    pending_reconciliation: bool = False


class SourcePanelOut(BaseModel):
    """§7's source panel. One shape for all four sources; the fields that do not apply are null.

    Four classes would force the client to branch four ways to draw one panel, and the branch is
    where the fifth source gets forgotten. ``headline`` is the sentence the panel leads with, and
    for a subscribed portfolio it is §9's exact framing — never "managed", never "advisory".
    """

    source: PortfolioSource
    headline: str
    publisher: str | None = None
    basket_slug: str | None = None
    basket_name: str | None = None
    #: The rules behind a screen- or strategy-driven portfolio, when one is linked.
    screen_public_id: str | None = None
    screen_name: str | None = None
    #: A holding group's included brokers and the date it was grouped on (§7).
    brokers: list[BrokerRefOut] = Field(default_factory=list)
    grouped_on: dt.date | None = None
    #: What a publisher's rebalance produces here: an order plan the user takes to their broker.
    #: Stated in the payload because §9 makes it a promise, not an implementation detail.
    execution_note: str = (
        "Baskfy never places an order. A rebalance produces a plan you take to your broker."
    )


class PortfolioSummaryOut(BaseModel):
    """§7's summary block. Same figures as the table row, plus invested and the benchmark diff."""

    portfolio_id: int
    name: str
    kind: PortfolioKind
    source: PortfolioSource
    source_badge: str
    publisher: str | None = None
    started_on: dt.date
    value: Decimal
    cash: Decimal
    invested: Decimal | None = None
    invested_unavailable_reason: str | None = None
    todays_pnl: MoneyMoveOut
    total_pnl: MoneyMoveOut
    headline_return: ReturnFigureOut
    model_return: ReturnFigureOut | None = None
    xirr: LabelledRateOut
    benchmark: BenchmarkOut | None = None
    counts_toward_total: bool
    excluded_note: str | None = None
    status: str
    pending_reconciliation: bool = False
    holdings_synced_on: dt.date | None = None
    prices_as_of: dt.date | None = None


class PortfolioDetailOut(BaseModel):
    """``GET /portfolio/{id}`` — §7 in one response: summary, holdings, source panel."""

    summary: PortfolioSummaryOut
    holdings: list[DetailHoldingOut] = Field(default_factory=list)
    source_panel: SourcePanelOut
    brokers: list[BrokerRefOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §7 — activity
# ---------------------------------------------------------------------------


class ActivityKind(StrEnum):
    """What one activity row is. The cash kinds mirror ``portfolio_cash_flow.kind`` exactly.

    ``CORPORATE_ACTION`` and ``RECONCILIATION`` are the two rows that are not cash movements.
    Both belong in §7's list and neither is a P&L event — a split changes quantity and average
    price and produces zero P&L (§4.5, criterion 6), and a question being asked or answered is a
    fact about our bookkeeping, not about the user's money.
    """

    BUY = "BUY"
    SELL = "SELL"
    DIVIDEND = "DIVIDEND"
    ASSIGN = "ASSIGN"
    RELEASE = "RELEASE"
    EXTERNAL_DEPOSIT = "EXTERNAL_DEPOSIT"
    EXTERNAL_WITHDRAWAL = "EXTERNAL_WITHDRAWAL"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    RECONCILIATION = "RECONCILIATION"


class ActivityItemOut(BaseModel):
    """One row of §7's activity feed."""

    kind: ActivityKind
    on: dt.date
    description: str
    portfolio: PortfolioRefOut | None = None
    instrument: InstrumentRefOut | None = None
    broker: BrokerRefOut | None = None
    quantity: Decimal | None = None
    amount: Decimal | None = None
    #: False for corporate actions and for reconciliation rows — see :class:`ActivityKind`.
    is_pnl_event: bool = True
    #: Set on a ``RECONCILIATION`` row: which item, in what state, and what it asked.
    reconciliation_item_id: int | None = None
    reconciliation_state: ReconciliationState | None = None


class ActivityOut(BaseModel):
    items: list[ActivityItemOut] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# §4.3 — the reconciliation inbox
# ---------------------------------------------------------------------------


class ReconciliationItemOut(BaseModel):
    """One question in §4.3's inbox, with the sentence it asks and the portfolio to pre-select.

    ``suggested_portfolio`` is a suggestion and is labelled as one everywhere it travels: the
    ledger is explicit that a suggestion is never an attribution, and a UI that pre-selects
    without saying so is a UI that collects clicks rather than answers.
    """

    item_id: int
    state: ReconciliationState
    reason: str
    question: str
    instrument: InstrumentRefOut
    broker: BrokerRefOut
    quantity: Decimal
    detected_on: dt.date
    suggested_portfolio: PortfolioRefOut | None = None
    resolved_portfolio: PortfolioRefOut | None = None
    resolved_at: dt.datetime | None = None
    #: True while this question withholds its holding from performance (§4.3).
    freezes: bool = False


class ReconciliationInboxOut(BaseModel):
    """The inbox, plus exactly what the open questions in it are currently forbidding."""

    items: list[ReconciliationItemOut] = Field(default_factory=list)
    open_count: int = 0
    #: Portfolios whose §6.5 Status column must read "Pending reconciliation".
    pending_portfolio_ids: list[int] = Field(default_factory=list)
    unallocated_pending: bool = False
    consolidated_pending: bool = False
    pending_status_label: str = _STATUS_PENDING


class ResolveBody(BaseModel):
    """Which portfolio the detected change belongs to. One field, because that is the whole act.

    A dismissal ("there is nothing here to attribute") is a different answer with a different
    consequence — it changes no allocation — and it is not this route. Folding both into one
    endpoint with a boolean would make the more dangerous of the two the default shape.
    """

    portfolio_id: int = Field(gt=0)


class ResolveOut(BaseModel):
    """What answering the question did: the item, and the allocation the answer implied."""

    item: ReconciliationItemOut
    #: The capital portfolio the holding now counts against. Written in the same transaction as
    #: the state change — :class:`~baskfy_core.reconciliation.ResolutionOutcome` returns them
    #: together precisely so a caller cannot persist one and lose the other.
    allocated_to: PortfolioRefOut
    #: Whether the holding is now contributing to performance again.
    unfroze: bool = True


# ---------------------------------------------------------------------------
# §6.6 / §6.7 — the two shapes onboarding needs: a suggestion, and a new portfolio
#
# These are the write half of the redesign's surface, and they are deliberately the *only* write
# half. `POST /portfolio` creates a grouping and moves a bookkeeping allocation; it names no side,
# no product, no venue and no broker session, exactly like every read above it (§9,
# non-negotiable #1). Creating a portfolio is filing, not trading.
# ---------------------------------------------------------------------------


class HoldingKeyOut(BaseModel):
    """One physical position, named the way :class:`~baskfy_core.allocation_ledger.HoldingKey`
    names it: an instrument at a broker account.

    Instrument alone would merge two brokers' positions into one, and §6.7 is explicit that the
    two legs of "HDFC Bank — 320 (Zerodha 200 · Upstox 120)" display together and *allocate
    separately*. So the pair is the unit everywhere, on the wire as well as in the ledger.
    """

    instrument_id: int
    broker_account_id: int


class GroupingSuggestionOut(BaseModel):
    """One offer from :func:`~baskfy_core.grouping_suggestions.suggest_groupings`, field for field.

    Nothing here is composed by this router. ``proposed_name``, ``rationale``, ``value`` and the
    coverage are what the pure module produced, because they are what the pure module's tests
    check — a sentence re-worded on the way out is a sentence nothing asserts. The client
    (``apps/web/src/lib/portfolio/organize.ts``) mirrors this shape and re-applies the module's
    own rank key locally, so the three names have to agree down to the underscore.

    ``basket_id``, ``basket_coverage`` and ``missing_instrument_ids`` are set for, and only for, a
    :attr:`~baskfy_core.grouping_suggestions.SuggestionBasis.BASKET_OVERLAP` — the dataclass
    refuses any other combination at construction, so the constraint is carried rather than
    restated.
    """

    basis: SuggestionBasis
    proposed_name: str
    keys: list[HoldingKeyOut] = Field(default_factory=list)
    #: The group's market value at the prices the suggestion was computed from. Rupees, exact.
    value: Decimal
    rationale: str
    suggested_kind: PortfolioKind
    basket_id: int | None = None
    #: Constituents held over constituents in the model, four places. Never over holdings — a
    #: second broker account does not make a user hold more of a model.
    basket_coverage: Decimal | None = None
    missing_instrument_ids: list[int] = Field(default_factory=list)


class SuggestionBasisStatusOut(BaseModel):
    """Whether one of §6.6's three bases could be computed at all, and if not, why not.

    This field exists because of the difference between "we looked and these holdings do not
    group by sector" and "we have no sector data for anybody". Both produce no sector suggestion,
    and only one of them is a fact about the user's portfolio; collapsing them into an empty list
    would have the screen tell a new user their holdings have nothing in common when what
    actually happened is that a reference table is empty.

    ``considered`` is how many holdings the basis had an input for. Zero with ``available`` false
    is a missing input; a small number with ``available`` true is a real, thin answer.
    """

    basis: SuggestionBasis
    available: bool
    considered: int = 0
    unavailable_reason: str | None = None


class SuggestionsOut(BaseModel):
    """``GET /portfolio/suggestions`` — §6.6's first-run helper, with its own honesty attached.

    §6.6 is the spec's most emphatic sentence ("getting from 40 unallocated holdings to 4 named
    portfolios IS activation"), and the failure it is most exposed to is a blank screen that
    reads as a verdict. So the payload carries three things a bare list cannot:

    * ``bases`` — per-basis availability, so the screen can say *which* input was missing;
    * ``unavailable_reason`` — set only when **no** basis could be computed at all, which is the
      one case where "no ideas" would be a lie rather than an answer. It is ``None`` the moment
      even one basis ran, including when that basis found nothing;
    * ``unpriced_instrument_ids`` — holdings excluded from every suggestion because no close is
      on record for them. :func:`~baskfy_core.allocation_ledger.holding_value` refuses a missing
      price and it is right to: a suggestion whose headline value quietly omitted a position
      would rank below where it belongs and mislead the decision it exists to inform.

    ``sectors`` is keyed by instrument id **as a string**, because that is what a JSON object key
    is; the §6.7 picker filters by it. It is empty when nothing in this database knows a sector,
    and ``bases`` then says so in a sentence.
    """

    suggestions: list[GroupingSuggestionOut] = Field(default_factory=list)
    #: ``{instrument_id -> sector}``, for §6.7's sector filter. Empty is a legitimate answer.
    sectors: dict[str, str] = Field(default_factory=dict)
    #: How many physical positions are in no capital portfolio — §6.6's pile, counted.
    unallocated_count: int = 0
    #: The value that pile carries, over the positions that could be priced.
    unallocated_value: Decimal
    bases: list[SuggestionBasisStatusOut] = Field(default_factory=list)
    unavailable_reason: str | None = None
    unpriced_instrument_ids: list[int] = Field(default_factory=list)
    #: §6.6's primary call to action, spelled once — the same constant the overview serves.
    cta: str = ORGANIZE_CTA


class HoldingKeyIn(BaseModel):
    """One holding to allocate, and **how many of it** (Phase 3, 10 Sep 2026).

    This class used to say, at length, that there was no quantity field and there must not be
    one — that v1 allocated a holding whole, and that partial allocation was a Phase-3 item which
    would arrive "as a migration and a new field, not as a quantity that was here all along".
    This is that migration and that field.

    Maulik asked for it in his own words: *"one stock can appear in multiple portfolios, so if
    stock a bought 100 qty for shortterm 20 for long term 34 for some swing 36 for momentum"*.
    The old text was right that the ledger had to be stable first; it is, and
    :func:`~baskfy_core.allocation_ledger.validate_against_holdings` is what now carries the
    invariant the whole-holding rule used to carry for free.

    ``quantity`` is **optional, and omitting it means "all of it"** — which keeps every existing
    client working unchanged and makes the common case ("file this whole holding into Long term")
    the shortest thing to write. It is not defaulted to a number here because the number depends
    on the position, which this schema cannot see; the route resolves it against the ledger.
    """

    model_config = ConfigDict(extra="forbid")

    instrument_id: int = Field(gt=0)
    broker_account_id: int = Field(gt=0)
    #: How many shares of this holding to file. ``None`` means the unallocated remainder of the
    #: position — everything not already filed elsewhere. A quantity larger than that remainder
    #: is refused by name and number rather than clamped: silently filing fewer shares than the
    #: user asked for would leave them believing a portfolio holds something it does not.
    quantity: Decimal | None = Field(default=None, gt=0)


class NewPortfolioIn(BaseModel):
    """``POST /portfolio`` — §3's source, §4.1's kind, §6.7's benchmark, and the holdings to file.

    The four facts that make a portfolio in the redesign's model are all required to be *stated*,
    and none of them is inferred:

    * ``kind`` decides arithmetic, not styling (§4.1). A capital portfolio takes each holding
      exclusively and sums into net worth; a monitoring view is a lens that never enters a total.
      Defaulting it would make the more consequential of the two arrive by accident.
    * ``source`` decides the headline metric (§5.2) and the badge (§3). ``SUBSCRIBED`` here means
      the user is filing holdings against a model somebody else published — §9's framing, never
      "managed", never "advisory".
    * ``benchmark_index_id`` is §6.3's per-portfolio override. ``None`` is not "no comparison": it
      means the surface falls back to the product default.
    * ``holdings`` may be empty. §6.7 offers "Empty" as a starting point, and a portfolio with a
      name and no positions yet is a legitimate thing to have made.

    ``extra="forbid"`` for the same reason :class:`HoldingKeyIn` has it: this is the schema §4.2
    is written into, and a body that carries an unknown key is a body somebody expected to mean
    something.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=NAME_MAX)
    kind: PortfolioKind
    source: PortfolioSource
    benchmark_index_id: int | None = Field(default=None, gt=0)
    holdings: list[HoldingKeyIn] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading — one picture of the user's ledger, assembled once
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Prices:
    """The latest two closes for the instruments a user holds, plus what could not be priced.

    ``as_of`` is the newest close on record for anything in the set, and it is the single date
    §6.1's header quotes. Per-instrument dates are kept too: an instrument whose newest close is
    older than ``as_of`` is stale rather than missing, and the difference is one the ribbon shows
    (§6.4) rather than one that silently shrinks a total.

    ``unpriced`` names instruments with no close at all. They are excluded from valuation and
    reported, because :func:`~baskfy_core.allocation_ledger.holding_value` is right to refuse a
    missing price: valuing it at zero produces a total that is wrong and still adds up.
    """

    as_of: dt.date | None
    latest: Mapping[int, Decimal]
    previous: Mapping[int, Decimal]
    dated: Mapping[int, dt.date]
    unpriced: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _Position:
    """One physical holding, plus every portfolio row that claims a piece of it.

    The ledger's key is ``(instrument_id, broker_account_id)`` and the schema stores that
    position once per portfolio it appears in. Until 0035 that meant at most one CAPITAL row, and
    this class had a ``capital_portfolio_id: int | None`` to match. It now means **any number of
    capital slices** — Maulik's 100-share ITC filed 20/34/36/10 is four rows — so the field is a
    mapping and ``holding.quantity`` is their sum plus whatever is unfiled.

    ``capital_slices`` is ordered by portfolio id so every surface that walks it renders the same
    way twice, and so a test can assert a list rather than a set.
    """

    key: HoldingKey
    holding: Holding
    capital_slices: Mapping[int, Decimal]
    #: The broker-owned pile's rows for this position — the UNALLOCATED shares, which are backed
    #: by real rows rather than derived. `_apply_allocation` takes from these first, and must
    #: decrement them: a remainder that is a row and is not decremented is a double-count.
    pile_slices: Mapping[int, Decimal]
    monitoring_portfolio_ids: tuple[int, ...]
    first_bought_on: dt.date | None
    history_source: str

    @property
    def sole_capital_portfolio_id(self) -> int | None:
        """The one portfolio this holding is filed into, or ``None`` if it is split or unfiled.

        The pre-0035 question, kept because several surfaces genuinely still ask it — "can this
        sell be attributed without asking", "which single portfolio does this row belong under".
        It answers ``None`` for a *split* holding as well as an unfiled one, which is correct for
        every one of those callers: neither has a single answer, and both must take the branch
        that asks rather than the branch that assumes.
        """
        return next(iter(self.capital_slices)) if len(self.capital_slices) == 1 else None

    @property
    def allocated_quantity(self) -> Decimal:
        return sum(self.capital_slices.values(), ZERO)

    @property
    def unallocated_quantity(self) -> Decimal:
        """What is left of the position after every slice. §6.6's pile, per holding."""
        remainder = self.holding.quantity - self.allocated_quantity
        return remainder if remainder > ZERO else ZERO


@dataclass(frozen=True, slots=True)
class _Ledger:
    """Everything one user's portfolio surfaces read, loaded once and shared.

    Assembled by :func:`_load_ledger` in a fixed number of statements regardless of how many
    portfolios or holdings the user has. The overview draws a row per portfolio and a line per
    holding; doing that with a query per row is how a page that renders in milliseconds on a
    developer's three portfolios takes seconds on a real account.
    """

    user_id: int
    portfolios: Mapping[int, LedgerPortfolio]
    rows: Mapping[int, Portfolio]
    positions: tuple[_Position, ...]
    prices: _Prices
    instruments: Mapping[int, Instrument]
    brokers: Mapping[int, BrokerAccount]
    broker_cash: tuple[BrokerCash, ...]
    flows: tuple[LedgerCashFlow, ...]
    flow_rows: tuple[PortfolioCashFlow, ...]
    entries: tuple[InboxEntry, ...]
    item_rows: Mapping[int, ReconciliationItem]
    rebalance_due_portfolio_ids: frozenset[int]
    #: Broker-owned holding groups. Their rows are Unallocated, not a strategy's slice — see
    #: `_load_ledger`. Empty for a user with no synced broker.
    pile_ids: frozenset[int]
    #: ``portfolio_id -> the basket it tracks``, for subscribed portfolios linked to one.
    baskets: Mapping[int, CbBasket]
    #: ``portfolio_id -> the publisher's name``. §9's ``{publisher}``.
    publishers: Mapping[int, str]
    #: ``portfolio_id -> the saved screen behind it``, for screen- and strategy-driven ones.
    screens: Mapping[int, Screen]
    benchmarks: Mapping[int, IndexDef]

    @property
    def holdings(self) -> list[Holding]:
        return [position.holding for position in self.positions]

    @property
    def allocations(self) -> list[Allocation]:
        """One allocation per capital position, carrying that slice's quantity.

        Absence means Unallocated: since 10 Sep 2026 the remainder is derived
        (``held - sum(slices)``) rather than stored, so an explicit unallocated row is refused by
        `Allocation` itself and would have been double-counting waiting to happen.
        """
        return [
            Allocation(key=position.key, portfolio_id=portfolio_id, quantity=quantity)
            for position in self.positions
            for portfolio_id, quantity in position.capital_slices.items()
            if quantity > ZERO
        ]

    @property
    def ledger_positions(self) -> list[LedgerPosition]:
        """Every appearance of every holding — the shape §4.5's fan-out and §4.3's freeze need.

        **One capital row per SLICE since 0035**, each carrying that slice's quantity, plus one
        Unallocated row for any remainder. A split holding appears once per portfolio it is filed
        into, which is what makes a corporate action fan out to all of them and what makes a
        freeze bite on the right ones.

        A monitoring view still sees the WHOLE holding: a lens answers "which names", not "how
        many" (§4.1), and it enters no total, so there is nothing here to double-count.
        """
        found: list[LedgerPosition] = []
        for position in self.positions:
            for portfolio_id, quantity in position.capital_slices.items():
                found.append(
                    LedgerPosition(
                        portfolio_id=portfolio_id,
                        holding=replace(position.holding, quantity=quantity),
                    )
                )
            remainder = position.unallocated_quantity
            if remainder > ZERO or not position.capital_slices:
                found.append(
                    LedgerPosition(
                        portfolio_id=UNALLOCATED,
                        holding=replace(position.holding, quantity=remainder),
                    )
                )
            for view_id in position.monitoring_portfolio_ids:
                found.append(LedgerPosition(portfolio_id=view_id, holding=position.holding))
        return found

    def priced(self) -> dict[int, Decimal]:
        """The price map the ledger is handed. Unpriced instruments are absent, never zero."""
        return dict(self.prices.latest)

    def value_of(self, holding: Holding) -> Decimal | None:
        """One holding's market value, or ``None`` when its instrument has no close on record."""
        if holding.key.instrument_id in self.prices.unpriced:
            return None
        return holding_value(holding, self.prices.latest)


def _label_for_prices(as_of: dt.date | None) -> str:
    """§6.1's first timestamp, spelled the way §5.1 insists: a close, on a named day."""
    if as_of is None:
        return "No closing prices yet"
    return f"Prices: close of {as_of.isoformat()}"


def _label_for_sync(on: dt.date | None) -> str:
    """§6.1's second timestamp. See :class:`OverviewOut` for why it names a day, not an instant."""
    if on is None:
        return "Holdings not synced yet"
    return f"Holdings synced: {on.isoformat()}"


async def _load_ledger(session: AsyncSession, user_id: int) -> _Ledger:
    """One user's whole picture, in a fixed number of statements. Scoped by ``user_id`` throughout.

    The scoping is not decoration and it is not "belt and braces": every join below starts from
    ``portfolio.user_id`` or ``broker_account.user_id``, so a row belonging to another tenant is
    not filtered out of a result — it is never in one. That is the difference between a predicate
    that can be forgotten in one branch and a shape that has nowhere to put a foreign row.
    """
    portfolio_rows = list(
        (await session.scalars(select(Portfolio).where(Portfolio.user_id == user_id))).all()
    )
    rows_by_id = {int(row.id): row for row in portfolio_rows}
    # THE BROKER'S OWN GROUP IS UNALLOCATED, NOT A STRATEGY (11 Sep 2026).
    #
    # `broker_holdings_sync` files a synced account's shares into a portfolio it owns —
    # `source=HOLDING_GROUP` with a `broker_account_id`, named "Zerodha holdings" — and that
    # portfolio is CAPITAL, because the shares are real money and must sum into net worth.
    #
    # Which meant every share was "already allocated" the moment it synced, and §6.7's picker
    # offered `0 of 0 free` on every row. Maulik caught it on the first real screen and named the
    # cause exactly: "every stock from every strategy in every basket would eventually go to zero
    # holding ... since all the stocks would be held in Zerodha".
    #
    # So a pile is identified here, once, and every surface below reads it: its rows are the
    # UNALLOCATED remainder, not a slice. Nothing about the totals changes — the shares are still
    # counted, still exactly once — but "free to file" now means what the user means by it, and
    # the pile no longer appears beside Long term and Swing as if it were one of them.
    #
    # Read from `is_broker_pile` (0038) rather than inferred from `source` + `broker_account_id`.
    # The inference was tried first and a test caught it: `POST /portfolio` sets
    # `broker_account_id` on any group whose legs share one account, and HOLDING_GROUP is one of
    # §6.7's offered sources — so a user grouping their IT stocks at Zerodha matched it exactly,
    # and lost its whole value the moment it was created.
    pile_ids = frozenset(int(row.id) for row in portfolio_rows if row.is_broker_pile)
    portfolios = {
        int(row.id): LedgerPortfolio(
            portfolio_id=int(row.id),
            name=row.name,
            kind=PortfolioKind(row.kind),
            source=PortfolioSource(row.source),
            started_on=row.started_on,
        )
        for row in portfolio_rows
    }

    holding_rows = list(
        (
            await session.scalars(
                select(PortfolioHolding)
                .join(Portfolio, Portfolio.id == PortfolioHolding.portfolio_id)
                .where(Portfolio.user_id == user_id)
                .order_by(
                    PortfolioHolding.instrument_id,
                    PortfolioHolding.broker_account_id,
                    PortfolioHolding.portfolio_id,
                )
            )
        ).all()
    )
    positions = _positions_from(holding_rows, pile_ids)

    instrument_ids = sorted({position.key.instrument_id for position in positions})
    prices = await _load_prices(session, instrument_ids)
    instruments = (
        {
            int(row.id): row
            for row in (
                await session.scalars(select(Instrument).where(Instrument.id.in_(instrument_ids)))
            ).all()
        }
        if instrument_ids
        else {}
    )

    broker_rows = list(
        (
            await session.scalars(
                select(BrokerAccount)
                .where(BrokerAccount.user_id == user_id)
                .order_by(BrokerAccount.id)
            )
        ).all()
    )
    brokers = {int(row.id): row for row in broker_rows}
    broker_ids = sorted(brokers)

    cash_rows = (
        list(
            (
                await session.scalars(
                    select(BrokerCash)
                    .where(BrokerCash.broker_account_id.in_(broker_ids))
                    .order_by(BrokerCash.broker_account_id)
                )
            ).all()
        )
        if broker_ids
        else []
    )

    flow_rows = (
        list(
            (
                await session.scalars(
                    select(PortfolioCashFlow)
                    .where(PortfolioCashFlow.broker_account_id.in_(broker_ids))
                    .order_by(PortfolioCashFlow.occurred_on, PortfolioCashFlow.id)
                )
            ).all()
        )
        if broker_ids
        else []
    )

    item_rows = list(
        (
            await session.scalars(
                select(ReconciliationItem)
                .where(ReconciliationItem.user_id == user_id)
                .order_by(ReconciliationItem.detected_on.desc(), ReconciliationItem.id.desc())
            )
        ).all()
    )

    sleeve_rows = (
        list(
            (
                await session.scalars(
                    select(PortfolioSleeve)
                    .where(PortfolioSleeve.portfolio_id.in_(sorted(rows_by_id)))
                    .order_by(PortfolioSleeve.sort_order, PortfolioSleeve.id)
                )
            ).all()
        )
        if rows_by_id
        else []
    )
    baskets, publishers, screens = await _load_sources(session, sleeve_rows)

    benchmark_ids = sorted(
        {int(row.benchmark_index_id) for row in portfolio_rows if row.benchmark_index_id}
    )
    benchmarks = (
        {
            int(row.id): row
            for row in (
                await session.scalars(select(IndexDef).where(IndexDef.id.in_(benchmark_ids)))
            ).all()
        }
        if benchmark_ids
        else {}
    )

    return _Ledger(
        user_id=user_id,
        portfolios=portfolios,
        rows=rows_by_id,
        positions=positions,
        prices=prices,
        instruments=instruments,
        brokers=brokers,
        broker_cash=tuple(cash_rows),
        flows=tuple(_flow_from(row) for row in flow_rows),
        flow_rows=tuple(flow_rows),
        entries=tuple(_entry_from(row) for row in item_rows),
        item_rows={int(row.id): row for row in item_rows},
        rebalance_due_portfolio_ids=await _rebalance_due(session, user_id, sleeve_rows),
        pile_ids=pile_ids,
        baskets=baskets,
        publishers=publishers,
        screens=screens,
        benchmarks=benchmarks,
    )


def _positions_from(
    rows: Sequence[PortfolioHolding], pile_ids: frozenset[int] = frozenset()
) -> tuple[_Position, ...]:
    """Gather ``portfolio_holding`` rows back into the physical positions they describe.

    A position's quantity is the SUM of its CAPITAL rows (0035) — a holding filed 20/34/36 is
    three rows and ninety shares. ``pile_ids`` names the broker-owned groups whose rows are the
    UNALLOCATED remainder rather than a strategy's slice: they are counted in the quantity, so
    the total is right, and left out of `capital_slices`, so `unallocated_quantity` reports what
    is actually free to file. Without that, a freshly synced account has every share "allocated"
    to "Zerodha holdings" and the picker offers nothing.

    A position's average price is taken from a capital row when it has one. Every row for a
    position should agree about the price — a lens is a view of the same shares, and §4.5's
    fan-out is what keeps that true through a split. When there is no capital row at all, the
    largest monitoring quantity stands in: disagreeing lenses are a bug somewhere upstream, and
    the reading that loses the fewest shares is the one that does not quietly shrink the user's
    net worth.

    A NULL quantity becomes zero rather than being dropped. Migration 0019's column is nullable
    and a row imported without a quantity column is a real state — a position we know about and
    cannot size. It counts as a row and as no money, which is what
    ``portfolios.py``'s roll-up already does with the same fact.
    """
    grouped: dict[HoldingKey, list[PortfolioHolding]] = {}
    for row in rows:
        key = HoldingKey(
            instrument_id=int(row.instrument_id), broker_account_id=int(row.broker_account_id)
        )
        grouped.setdefault(key, []).append(row)

    positions: list[_Position] = []
    for key, group in grouped.items():
        capital = [row for row in group if row.portfolio_kind == PortfolioKind.CAPITAL]
        monitoring = [row for row in group if row.portfolio_kind == PortfolioKind.MONITORING]
        source_row = capital[0] if capital else max(group, key=lambda r: r.quantity or ZERO)
        # 0035: THE POSITION IS THE SUM OF ITS CAPITAL SLICES. Before it, one capital row was the
        # whole position and this read its quantity directly; with a holding filed 20/34/36/10
        # that would report 20 shares of a 100-share position and every total below it would be
        # wrong by the rest. Monitoring rows are excluded from the sum for the reason they are
        # excluded from every total (§4.1) — a lens sees the same shares again, it does not add
        # more of them.
        filed = {
            int(row.portfolio_id): row.quantity
            for row in sorted(capital, key=lambda r: int(r.portfolio_id))
            if row.quantity is not None
        }
        quantity = (
            sum(filed.values(), ZERO)
            if filed
            else (source_row.quantity if source_row.quantity is not None else ZERO)
        )
        # The pile's rows are counted above and excluded here: they are the remainder, and
        # `unallocated_quantity` derives them back as `quantity - sum(capital_slices)`.
        slices = {pid: qty for pid, qty in filed.items() if pid not in pile_ids}
        piles = {pid: qty for pid, qty in filed.items() if pid in pile_ids}
        positions.append(
            _Position(
                key=key,
                holding=Holding(key=key, quantity=quantity, avg_price=source_row.avg_price),
                capital_slices=slices,
                pile_slices=piles,
                monitoring_portfolio_ids=tuple(int(row.portfolio_id) for row in monitoring),
                first_bought_on=source_row.first_bought_on,
                history_source=source_row.history_source,
            )
        )
    return tuple(positions)


async def _load_prices(session: AsyncSession, instrument_ids: Sequence[int]) -> _Prices:
    """The two most recent closes for each instrument, in one statement.

    ``close_raw`` rather than ``close``: house rule 6 says the adjusted series is what factors
    read and the exchange print is what the user is shown wherever they expect a real price. A
    portfolio page is exactly that place — the number next to a stock has to be the number the
    broker's app shows.

    Two closes per instrument, ranked per instrument rather than by a shared date, because a
    thinly traded name may not have printed on the newest day in the set. Taking "the two most
    recent dates overall" would then compare a stock against a day it did not trade on.
    """
    if not instrument_ids:
        return _Prices(as_of=None, latest={}, previous={}, dated={}, unpriced=())

    ranked = (
        select(
            OhlcvDaily.instrument_id.label("instrument_id"),
            OhlcvDaily.date.label("date"),
            OhlcvDaily.close_raw.label("close_raw"),
            func.row_number()
            .over(partition_by=OhlcvDaily.instrument_id, order_by=OhlcvDaily.date.desc())
            .label("recency"),
        )
        .where(OhlcvDaily.instrument_id.in_(list(instrument_ids)))
        .subquery()
    )
    rows = (
        await session.execute(
            select(ranked.c.instrument_id, ranked.c.date, ranked.c.close_raw, ranked.c.recency)
            .where(ranked.c.recency <= _CLOSES_PER_INSTRUMENT)
            .order_by(ranked.c.instrument_id, ranked.c.recency)
        )
    ).all()

    latest: dict[int, Decimal] = {}
    previous: dict[int, Decimal] = {}
    dated: dict[int, dt.date] = {}
    for row in rows:
        instrument_id = int(row.instrument_id)
        if int(row.recency) == 1:
            latest[instrument_id] = row.close_raw
            dated[instrument_id] = row.date
        else:
            previous[instrument_id] = row.close_raw
    # LIVE MARKS OVER THE CLOSE, WHERE THE BROKER HAS ONE (M82).
    #
    # Everything above is `close_raw` — the previous session's print. Correct for a screener, wrong
    # for the page that answers "what is my money worth": Maulik asked for the market, and on
    # 2 Sep 2026 ATHERENERG was trading at 1692.50 while this showed the close.
    #
    # Only `latest` is overlaid. `previous` stays the prior close, which makes "Today" a live price
    # against yesterday's close — the same arithmetic the broker's own app does. Overwriting
    # `previous` too would silently zero the day's change.
    #
    # `dated` is left alone as well: it records which session the stored close came from, and a
    # live mark has no session. Nothing that reads it starts meaning something else.
    live = await live_prices_by_instrument(session, list(instrument_ids))
    for instrument_id, price in live.items():
        latest[instrument_id] = price

    unpriced = tuple(sorted(set(instrument_ids) - set(latest)))
    as_of = max(dated.values()) if dated else None
    return _Prices(as_of=as_of, latest=latest, previous=previous, dated=dated, unpriced=unpriced)


def _flow_from(row: PortfolioCashFlow) -> LedgerCashFlow:
    """One stored flow as the ledger's own value, with §4.4's rules re-checked on the way in.

    The construction is the check: :class:`~baskfy_core.cash_ledger.PortfolioCashFlow` refuses an
    external flow that names a portfolio and an internal one that does not, which is migration
    0022's constraint stated where a reader meets it. A row that somehow got past the database
    fails loudly here rather than being averaged into an XIRR.
    """
    return LedgerCashFlow(
        broker_account_id=int(row.broker_account_id),
        kind=CashFlowKind(row.kind),
        amount=row.amount,
        occurred_on=row.occurred_on,
        portfolio_id=int(row.portfolio_id) if row.portfolio_id is not None else None,
        instrument_id=int(row.instrument_id) if row.instrument_id is not None else None,
        quantity=row.quantity,
        note=row.note,
    )


def _entry_from(row: ReconciliationItem) -> InboxEntry:
    """One stored inbox row as the domain's :class:`~baskfy_core.reconciliation.InboxEntry`."""
    return InboxEntry(
        item_id=int(row.id),
        item=LedgerQuestion(
            key=HoldingKey(
                instrument_id=int(row.instrument_id),
                broker_account_id=int(row.broker_account_id),
            ),
            quantity=row.quantity,
            reason=ReconciliationReason(row.reason),
            suggested_portfolio_id=(
                int(row.suggested_portfolio_id) if row.suggested_portfolio_id else None
            ),
        ),
        detected_on=row.detected_on,
        state=ReconciliationState(row.state),
        resolved_portfolio_id=(
            int(row.resolved_portfolio_id) if row.resolved_portfolio_id else None
        ),
        resolved_on=row.resolved_at.date() if row.resolved_at is not None else None,
    )


async def _load_sources(
    session: AsyncSession, sleeves: Sequence[PortfolioSleeve]
) -> tuple[dict[int, CbBasket], dict[int, str], dict[int, Screen]]:
    """§7's source panel inputs: the basket a portfolio tracks, its publisher, and its screen.

    Read through ``portfolio_sleeve``, which is where migration 0019 put the link from a
    portfolio to a curated basket and to a saved screen. Nothing here re-derives a portfolio's
    ``source`` from what it happens to be linked to: ``portfolio.source`` is the declared fact
    (§3) and a missing link means the panel has less to say, not that the badge changes.
    """
    basket_ids = sorted({int(s.basket_id) for s in sleeves if s.basket_id is not None})
    screen_ids = sorted({int(s.screen_id) for s in sleeves if s.screen_id is not None})

    basket_by_id: dict[int, CbBasket] = {}
    publisher_by_basket: dict[int, str] = {}
    if basket_ids:
        rows = (
            await session.execute(
                select(CbBasket, CbManager)
                .join(CbManager, CbManager.id == CbBasket.manager_id)
                .where(CbBasket.id.in_(basket_ids))
            )
        ).all()
        for basket, manager in rows:
            basket_by_id[int(basket.id)] = basket
            publisher_by_basket[int(basket.id)] = manager.name

    screen_by_id: dict[int, Screen] = {}
    if screen_ids:
        screen_by_id = {
            int(row.id): row
            for row in (
                await session.scalars(select(Screen).where(Screen.id.in_(screen_ids)))
            ).all()
        }

    baskets: dict[int, CbBasket] = {}
    publishers: dict[int, str] = {}
    screens: dict[int, Screen] = {}
    for sleeve in sleeves:
        portfolio_id = int(sleeve.portfolio_id)
        if sleeve.basket_id is not None and portfolio_id not in baskets:
            basket = basket_by_id.get(int(sleeve.basket_id))
            if basket is not None:
                baskets[portfolio_id] = basket
                publishers[portfolio_id] = publisher_by_basket[int(basket.id)]
        if sleeve.screen_id is not None and portfolio_id not in screens:
            screen = screen_by_id.get(int(sleeve.screen_id))
            if screen is not None:
                screens[portfolio_id] = screen
    return baskets, publishers, screens


async def _rebalance_due(
    session: AsyncSession, user_id: int, sleeves: Sequence[PortfolioSleeve]
) -> frozenset[int]:
    """§6.4's "rebalance available on a subscribed portfolio", derived rather than guessed.

    A portfolio is due when the basket it tracks has a version this user has neither applied nor
    skipped — ``cb_user_rebalance_state.state = 'PENDING'``. That row is the product's existing
    record of "the publisher moved and you have not decided yet", and reusing it is what keeps
    the ribbon agreeing with whatever surface the user goes on to answer it from.
    """
    basket_to_portfolios: dict[int, list[int]] = {}
    for sleeve in sleeves:
        if sleeve.basket_id is not None:
            basket_to_portfolios.setdefault(int(sleeve.basket_id), []).append(
                int(sleeve.portfolio_id)
            )
    if not basket_to_portfolios:
        return frozenset()

    rows = (
        await session.execute(
            select(CbBasketVersion.basket_id)
            .join(
                CbUserRebalanceState,
                CbUserRebalanceState.version_id == CbBasketVersion.id,
            )
            .where(
                CbUserRebalanceState.user_id == user_id,
                CbUserRebalanceState.state == "PENDING",
                CbBasketVersion.basket_id.in_(sorted(basket_to_portfolios)),
            )
        )
    ).all()
    due: set[int] = set()
    for row in rows:
        due.update(basket_to_portfolios.get(int(row.basket_id), ()))
    return frozenset(due)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _figure_out(figure: ReturnFigure) -> ReturnFigureOut:
    """Render a ledger figure without deciding anything about it. Label included, always."""
    return ReturnFigureOut(
        kind=figure.kind,
        label=figure.label,
        since=figure.since,
        value=figure.value,
        is_model=figure.is_model,
        unavailable_reason=figure.unavailable_reason,
    )


def _broker_ref(account: BrokerAccount) -> BrokerRefOut:
    return BrokerRefOut(
        broker_account_id=int(account.id), broker_id=account.broker_id, label=account.label
    )


def _portfolio_ref(portfolio: LedgerPortfolio) -> PortfolioRefOut:
    return PortfolioRefOut(
        portfolio_id=portfolio.portfolio_id,
        name=portfolio.name,
        kind=portfolio.kind,
        source=portfolio.source,
    )


def _instrument_ref(instrument: Instrument) -> InstrumentRefOut:
    return InstrumentRefOut(
        instrument_id=int(instrument.id), symbol=instrument.symbol, name=instrument.name
    )


def _source_badge(portfolio: LedgerPortfolio, publisher: str | None) -> str:
    """§3's badge, and for third-party content §9's only permitted framing.

    "Subscribed model by {publisher}". Never "managed portfolio", never "advisory", never "PMS" —
    Baskfy is not SEBI-registered and those words describe a service it does not provide. When
    the publisher's name is unknown the sentence still says what the thing is; dropping to a bare
    "Subscribed" would lose the attribution that makes the framing accurate.
    """
    if portfolio.source is PortfolioSource.SUBSCRIBED:
        return _SUBSCRIBED_BADGE.format(publisher=publisher or _UNKNOWN_PUBLISHER)
    return _SOURCE_BADGE[portfolio.source]


def _nav_point(row: PortfolioNavDaily) -> NavPoint:
    """A stored mark as the domain's point. ``cash`` is inside ``value`` for return arithmetic.

    ``portfolio_nav_daily`` stores the market value and the cash separately so a surface can show
    the split, but a NAV is what the portfolio was *worth* — a return computed on holdings alone
    would move when the user assigned cash into the portfolio and bought nothing with it. The
    payload keeps both halves; the arithmetic gets the sum.
    """
    return NavPoint(on=row.date, value=row.market_value + row.cash, net_flow=row.net_flow)


def _chain_linked(points: Sequence[NavPoint]) -> Decimal | None:
    """The time-weighted return over a series, using ``portfolio_nav``'s own public arithmetic.

    :func:`~baskfy_core.portfolio_nav.daily_pnl` already publishes each transition's move as a
    fraction of the previous close, with the day's flow removed — which is exactly the growth
    factor minus one that TWR chain-links. Multiplying those is therefore not a second
    implementation of the return: it is the same numbers, from the same function, in the same
    convention (a flow lands at the close, so it earns nothing that day).

    A ``pct`` of ``None`` means the previous mark was zero, and contributes a factor of exactly
    one — the first-funding case, and the same answer ``_growth_factors`` gives it.

    This exists because §5.2's consolidated TWR has no :class:`MetricKind` and therefore no
    ``twr_figure`` to call: that function labels by a portfolio's source, and the consolidated
    series is not a portfolio.
    """
    moves = daily_pnl(points)
    if not moves:
        return None
    product = ONE
    for move in moves:
        product *= ONE + (move.pct if move.pct is not None else ZERO)
    return (product - ONE).quantize(RETURN_PRECISION)


def _money_move(
    *,
    amount: Decimal | None,
    pct: Decimal | None,
    label: str,
    since: dt.date | None,
    unavailable_reason: str | None = None,
) -> MoneyMoveOut:
    return MoneyMoveOut(
        amount=amount,
        pct=pct,
        label=label,
        since=since,
        unavailable_reason=unavailable_reason,
    )


def _todays_move(ledger: _Ledger, positions: Sequence[_Position], *, label: str) -> MoneyMoveOut:
    """§6.2's "vs previous close", over exactly the positions handed in.

    Computed from the two closes rather than from the NAV series — see the module docstring. A
    position whose instrument has no previous close contributes nothing to the move and is not an
    error: a stock that listed yesterday has no yesterday.
    """
    move = ZERO
    base = ZERO
    measured = False
    for position in positions:
        instrument_id = position.key.instrument_id
        latest = ledger.prices.latest.get(instrument_id)
        previous = ledger.prices.previous.get(instrument_id)
        if latest is None or previous is None:
            continue
        measured = True
        quantity = position.holding.quantity
        move += quantity * (latest - previous)
        base += quantity * previous
    if not measured:
        return _money_move(
            amount=None,
            pct=None,
            label=label,
            since=None,
            unavailable_reason="No previous close to compare against yet",
        )
    return _money_move(
        amount=money(move),
        pct=None if base == ZERO else (money(move) / base).quantize(RETURN_PRECISION),
        label=label,
        since=None,
    )


def _cost_and_value(
    ledger: _Ledger, positions: Sequence[_Position]
) -> tuple[Decimal | None, Decimal | None, int]:
    """``(invested, value of the positions that know their cost, how many do not)``.

    §5.2 forbids a since-purchase figure for a holding whose purchase price is unknown, so the
    total P&L is taken over the subset that *does* know it and the rest are counted. Summing the
    whole portfolio's value against a partial cost would report a profit that includes shares
    nobody has priced the purchase of — the exact number §5.3 says to wait for a CAS import for.
    """
    invested = ZERO
    valued = ZERO
    unknown = 0
    known = False
    for position in positions:
        basis = cost_basis(position.holding)
        value = ledger.value_of(position.holding)
        if basis is None or value is None:
            unknown += 1
            continue
        known = True
        invested += basis
        valued += value
    if not known:
        return None, None, unknown
    return money(invested), money(valued), unknown


def _status_for(*, pending: bool, rebalance_due: bool) -> str:
    """§6.5's Status column, read top down by severity.

    Pending outranks everything: a portfolio whose numbers are on hold has a more urgent story
    than one with a rebalance waiting, and showing the rebalance first would invite the user to
    act on figures the product has just said it cannot state.
    """
    if pending:
        return _STATUS_PENDING
    if rebalance_due:
        return _STATUS_REBALANCE
    return _STATUS_SYNCED


def _headline_for(portfolio: LedgerPortfolio, points: Sequence[NavPoint]) -> ReturnFigureOut:
    """§5.2's headline metric for one portfolio, routed by source and never by convenience.

    A holding group goes to :func:`~baskfy_core.portfolio_nav.since_grouped_figure` and everything
    else to :func:`~baskfy_core.portfolio_nav.twr_figure`. The two compute the same chain-link and
    make different *claims*, and the routing is at the call site so the person reading this can
    see which claim was meant. Both refuse to invent a number from a series that is too short and
    return a labelled absence instead.
    """
    if portfolio.source is PortfolioSource.HOLDING_GROUP:
        return _figure_out(since_grouped_figure(portfolio, points))
    return _figure_out(twr_figure(portfolio, points))


def _model_for(portfolio: LedgerPortfolio, value: Decimal | None) -> ReturnFigureOut | None:
    """The publisher's own record, as a separate figure — criterion 5, structurally.

    ``None`` for anything the user built themselves.
    :func:`~baskfy_core.allocation_ledger.model_figure` refuses those outright, and calling it
    anyway to catch the exception would be asking a question whose answer is already known.
    """
    if portfolio.source is not PortfolioSource.SUBSCRIBED:
        return None
    return _figure_out(model_figure(portfolio, value))


async def _model_return_values(session: AsyncSession, ledger: _Ledger) -> dict[int, Decimal | None]:
    """The publisher's TWR **over each user's own window**, from ``cb_metrics``.

    ``cb_metrics.since_inception_pct`` is the basket's absolute return from its launch, as a
    percentage, one row per trading day. Two of them bracket the window the user has been
    subscribed for, and the model's return over that window is what is left when the earlier one
    is divided out::

        (1 + now/100) / (1 + at_start/100) - 1

    That is a real number about the publisher's record, measured over the same days as the user's
    own figure — which is what makes the pair worth showing side by side (§5.2). It is emphatically
    not blended with the user's return, and it cannot be: it travels in a different field, built by
    a function that stamps ``is_model``.

    ``None`` — a labelled "publisher has not reported for this period" — whenever either anchor is
    missing. The percent-to-ratio division is the one unit conversion in this module and it is
    here rather than at the call site, because a ratio and a percentage that look alike in a
    payload is precisely the kind of error nobody spots in review.
    """
    subscribed = {
        portfolio_id: portfolio
        for portfolio_id, portfolio in ledger.portfolios.items()
        if portfolio.source is PortfolioSource.SUBSCRIBED
    }
    values: dict[int, Decimal | None] = dict.fromkeys(subscribed, None)
    if not subscribed:
        return values

    hundred = Decimal("100")
    for portfolio_id, portfolio in subscribed.items():
        basket = ledger.baskets.get(portfolio_id)
        if basket is None:
            continue
        anchor = await session.scalar(
            select(CbMetrics.since_inception_pct)
            .where(
                CbMetrics.basket_id == basket.id,
                CbMetrics.as_of_date <= portfolio.started_on,
                CbMetrics.since_inception_pct.is_not(None),
            )
            .order_by(CbMetrics.as_of_date.desc())
            .limit(1)
        )
        current = await session.scalar(
            select(CbMetrics.since_inception_pct)
            .where(
                CbMetrics.basket_id == basket.id,
                CbMetrics.since_inception_pct.is_not(None),
            )
            .order_by(CbMetrics.as_of_date.desc())
            .limit(1)
        )
        if anchor is None or current is None:
            continue
        base = ONE + anchor / hundred
        if base == ZERO:
            continue
        values[portfolio_id] = ((ONE + current / hundred) / base - ONE).quantize(RETURN_PRECISION)
    return values


# ---------------------------------------------------------------------------
# NAV series loading
# ---------------------------------------------------------------------------


def _nav_stmt(user_id: int, portfolio_id: int | None) -> Select[tuple[PortfolioNavDaily]]:
    """The stored series for one portfolio, or the consolidated one when ``portfolio_id`` is None.

    ``IS NULL`` rather than ``= NULL``: the consolidated series is stored with a NULL portfolio
    (migration 0022), and SQL's three-valued logic would answer an equality against it with
    ``UNKNOWN`` and return an empty chart that looks like a portfolio with no history.
    """
    stmt = select(PortfolioNavDaily).where(PortfolioNavDaily.user_id == user_id)
    if portfolio_id is None:
        stmt = stmt.where(PortfolioNavDaily.portfolio_id.is_(None))
    else:
        stmt = stmt.where(PortfolioNavDaily.portfolio_id == portfolio_id)
    return stmt.order_by(PortfolioNavDaily.date)


async def _nav_rows(
    session: AsyncSession, user_id: int, portfolio_id: int | None
) -> list[PortfolioNavDaily]:
    return list((await session.scalars(_nav_stmt(user_id, portfolio_id))).all())


async def _nav_by_portfolio(
    session: AsyncSession, user_id: int
) -> dict[int | None, list[PortfolioNavDaily]]:
    """Every series this user has, in one statement, keyed the way the ledger keys portfolios.

    A row per portfolio in §6.5's table each needing its own headline metric would otherwise be a
    query per row, and the metric is not optional — criterion 3 makes the label part of the
    number, so a table cannot be drawn without it.
    """
    rows = list(
        (
            await session.scalars(
                select(PortfolioNavDaily)
                .where(PortfolioNavDaily.user_id == user_id)
                .order_by(PortfolioNavDaily.date)
            )
        ).all()
    )
    grouped: dict[int | None, list[PortfolioNavDaily]] = {}
    for row in rows:
        key = int(row.portfolio_id) if row.portfolio_id is not None else None
        grouped.setdefault(key, []).append(row)
    return grouped


def _windowed(rows: Sequence[PortfolioNavDaily], window: NavRange) -> list[PortfolioNavDaily]:
    """§6.3's range filter, measured back from the **last mark** rather than from today.

    From today would make a range mean something different every morning for a series the nightly
    job has not caught up with — "1M" on a chart whose newest mark is six weeks old would be
    empty, which reads as "you have no history" instead of "the data is behind". Anchoring on the
    data says what is actually true, and §6.4's stale-price row is what says the data is behind.
    """
    days = window.days
    if days is None or not rows:
        return list(rows)
    cutoff = rows[-1].date - dt.timedelta(days=days)
    return [row for row in rows if row.date >= cutoff]


async def _benchmark(
    session: AsyncSession,
    *,
    index: IndexDef | None,
    figure: ReturnFigureOut,
    rows: Sequence[PortfolioNavDaily],
) -> BenchmarkOut | None:
    """§6.3's overlay: the index over exactly the days the portfolio's marks cover.

    ``None`` when there is no benchmark set, no index levels for the window, or the portfolio's
    own figure is unavailable — a difference against a number that does not exist is not a
    smaller number, it is not a number. :func:`~baskfy_core.portfolio_nav.benchmark_comparison`
    refuses a model figure, so the overlay can never become a route to criterion 5's blend.
    """
    if index is None or not rows or figure.value is None:
        return None
    levels = list(
        (
            await session.execute(
                select(IndexSnapshotDaily.date, IndexSnapshotDaily.level)
                .where(
                    IndexSnapshotDaily.index_id == index.id,
                    IndexSnapshotDaily.date >= rows[0].date,
                    IndexSnapshotDaily.date <= rows[-1].date,
                    IndexSnapshotDaily.level.is_not(None),
                )
                .order_by(IndexSnapshotDaily.date)
            )
        ).all()
    )
    if len(levels) < _MIN_MARKS_FOR_A_RETURN:
        return None
    points = [NavPoint(on=row.date, value=row.level) for row in levels if row.level is not None]
    if points[0].on < figure.since:
        # The index has levels from before this portfolio began. Trimming rather than refusing:
        # the comparison is meaningful over the days both series cover, and the domain refuses a
        # window that starts early precisely so a caller has to make this choice explicitly.
        points = [point for point in points if point.on >= figure.since]
    if len(points) < _MIN_MARKS_FOR_A_RETURN:
        return None
    comparison = benchmark_comparison(
        ReturnFigure(
            kind=figure.kind,
            since=figure.since,
            value=figure.value,
        ),
        points,
        name=index.name,
    )
    return BenchmarkOut(
        name=comparison.name,
        portfolio=_figure_out(comparison.portfolio),
        benchmark=_figure_out(comparison.benchmark),
        difference=comparison.difference,
        points=[
            NavPointOut(on=point.on, value=point.value, cash=ZERO, net_flow=ZERO)
            for point in points
        ],
    )


@dataclass(frozen=True, slots=True)
class _SeriesMeta:
    """Everything about a NAV series that is not the marks themselves.

    Gathered into one value rather than passed as four more parameters, for the reason
    :class:`~baskfy_core.reconciliation.AttentionInputs` gives for the same move: a call site with
    six positional facts, two of them a date and a string, is where "which argument was which"
    bugs live. It also names the one that is easy to get wrong — ``label`` is the sentence the
    chart's own return figure is titled with, and it comes from the portfolio's headline metric
    (§5.2) rather than being composed at the call site.
    """

    portfolio_id: int | None
    window: NavRange
    label: str
    since: dt.date | None
    benchmark: BenchmarkOut | None = None


def _series_out(rows: Sequence[PortfolioNavDaily], meta: _SeriesMeta) -> NavSeriesOut:
    """Render one NAV series with everything §6.3 draws off it."""
    portfolio_id = meta.portfolio_id
    window = meta.window
    points = [_nav_point(row) for row in rows]
    drawdown: list[DrawdownPoint] = drawdown_series(points) if points else []
    worst = max_drawdown(points) if len(points) >= _MIN_MARKS_FOR_A_RETURN else None
    total = _chain_linked(points)
    return NavSeriesOut(
        portfolio_id=portfolio_id,
        range=window,
        from_on=rows[0].date if rows else None,
        to_on=rows[-1].date if rows else None,
        points=[
            NavPointOut(
                on=row.date,
                value=row.market_value + row.cash,
                cash=row.cash,
                net_flow=row.net_flow,
                pending_reconciliation=row.pending_reconciliation,
            )
            for row in rows
        ],
        daily_pnl=[
            DayPnlOut(on=move.on, amount=move.amount, pct=move.pct) for move in daily_pnl(points)
        ],
        drawdown=[
            DrawdownPointOut(
                on=point.on, index=point.index, peak=point.peak, drawdown=point.drawdown
            )
            for point in drawdown
        ],
        max_drawdown=(
            MaxDrawdownOut(
                peak_on=worst.peak_on, trough_on=worst.trough_on, drawdown=worst.drawdown
            )
            if worst is not None
            else None
        ),
        total_return=LabelledRateOut(
            label=meta.label,
            since=rows[0].date if rows else meta.since,
            value=total,
            unavailable_reason=(
                None if total is not None else "Not enough end-of-day valuations yet"
            ),
        ),
        benchmark=meta.benchmark,
        pending_reconciliation=any(row.pending_reconciliation for row in rows),
    )


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


async def _owned_portfolio(
    session: AsyncSession, portfolio_id: int, principal: Principal
) -> Portfolio:
    """One of *this* caller's portfolios, or ``NOT_FOUND``.

    Another tenant's portfolio answers exactly as one that does not exist. A 403 would confirm
    the id names a real row and whose it is, which is the fact a stranger is probing for — the
    same rule ``portfolios.py`` and ``curated_investments.py`` already follow.
    """
    row: Portfolio | None = await session.scalar(
        select(Portfolio).where(
            Portfolio.id == portfolio_id, Portfolio.user_id == principal.require_user()
        )
    )
    if row is None:
        raise not_found("portfolio", str(portfolio_id))
    return row


# ---------------------------------------------------------------------------
# GET /portfolio/overview — §6
# ---------------------------------------------------------------------------


@router.get("/overview", response_model=OverviewOut)
async def portfolio_overview(
    session: SessionDep,
    principal: AuthenticatedDep,
    window: Annotated[
        NavRange,
        Query(alias="range", description="§6.3's chart range. End-of-day only in v1."),
    ] = NavRange.Y1,
) -> OverviewOut:
    """§6's single screen: header, hero, chart, ribbon, table, and the unallocated section.

    Everything on it is one consistent picture of one moment, which is the reason it is one
    response: a client assembling the header from one call and the totals from another would
    eventually straddle a nightly job and render a page whose halves disagree.

    **The totals here exclude monitoring views and cannot include one.** Not by a filter — by
    construction. A monitoring view holds no allocation (§4.1), and
    :func:`~baskfy_core.allocation_ledger.portfolio_values` sums allocations; there is no branch
    in this handler that could forget the exclusion, because there is no branch. The muted rows
    are rendered from a second, separate walk into a second, separate list.
    """
    user_id = principal.require_user()
    ledger = await _load_ledger(session, user_id)
    series = await _nav_by_portfolio(session, user_id)
    model_values = await _model_return_values(session, ledger)

    holdings = ledger.holdings
    allocations = ledger.allocations
    prices = ledger.priced()
    priced_holdings = [
        holding for holding in holdings if holding.key.instrument_id not in ledger.prices.unpriced
    ]
    priced_positions = [
        position
        for position in ledger.positions
        if position.key.instrument_id not in ledger.prices.unpriced
    ]
    priced_allocations = [
        allocation
        for allocation in allocations
        if allocation.key.instrument_id not in ledger.prices.unpriced
    ]

    unallocated_cash = money(sum((row.balance for row in ledger.broker_cash), ZERO))
    portfolio_cash_by_id = {
        portfolio_id: portfolio_cash(ledger.flows, portfolio_id)
        for portfolio_id, portfolio in ledger.portfolios.items()
        if portfolio.kind is PortfolioKind.CAPITAL
    }
    total_cash = money(unallocated_cash + sum(portfolio_cash_by_id.values(), ZERO))

    current_value = consolidated_value(
        priced_holdings, priced_allocations, ledger.portfolios, prices, cash=total_cash
    )
    values = portfolio_values(priced_holdings, priced_allocations, ledger.portfolios, prices)

    freeze = freeze_report(ledger.entries, ledger.ledger_positions)
    invested, invested_value, without_basis = _cost_and_value(ledger, ledger.positions)

    hero = HeroOut(
        current_value=current_value,
        todays_pnl=_todays_move(ledger, priced_positions, label="Change since the previous close"),
        total_pnl=(
            _money_move(
                amount=money(invested_value - invested),
                pct=(
                    None
                    if invested == ZERO
                    else ((invested_value - invested) / invested).quantize(RETURN_PRECISION)
                ),
                label="Total P&L since purchase",
                since=None,
            )
            if invested is not None and invested_value is not None
            else _money_move(
                amount=None,
                pct=None,
                label="Total P&L since purchase",
                since=None,
                unavailable_reason=(
                    "Import your CAS to see returns from your purchase dates. Until then we know "
                    "what these shares are worth, not what you paid for them"
                ),
            )
        ),
        xirr=_consolidated_xirr(ledger, current_value),
        twr=_consolidated_twr(series.get(None, [])),
        invested=invested,
        invested_unavailable_reason=(
            None if invested is not None else "No purchase prices on record yet"
        ),
        secondary=HeroSecondaryOut(
            cash=total_cash,
            dividends=money(
                sum(
                    (flow.amount for flow in ledger.flows if flow.kind is CashFlowKind.DIVIDEND),
                    ZERO,
                )
            ),
            broker_count=len(ledger.brokers),
            unrealised_pnl=(
                _money_move(
                    amount=money(invested_value - invested),
                    pct=None,
                    label="Unrealised P&L",
                    since=None,
                )
                if invested is not None and invested_value is not None
                else _money_move(
                    amount=None,
                    pct=None,
                    label="Unrealised P&L",
                    since=None,
                    unavailable_reason="No purchase prices on record yet",
                )
            ),
            realised_pnl=_money_move(
                amount=None,
                pct=None,
                label="Realised P&L",
                since=None,
                unavailable_reason=(
                    "Realised P&L needs the purchase price of shares you no longer hold. "
                    "Import your CAS to unlock it"
                ),
            ),
        ),
        pending_reconciliation=freeze.consolidated_pending,
        holdings_without_cost_basis=without_basis,
    )

    capital_rows: list[PortfolioRowOut] = []
    monitoring_rows: list[PortfolioRowOut] = []
    for portfolio_id, portfolio in sorted(ledger.portfolios.items()):
        # The broker's own group is Unallocated and is drawn as Unallocated (11 Sep 2026). Listing
        # it here too would put "Zerodha holdings" beside "Long term" and "Swing" as though it
        # were a fourth strategy, and its shares would be read twice by anyone adding up the
        # page — once in the row and once in §6.6's section. Maulik asked for exactly this: "when
        # we see all the portfolios in one single place, we do not count the same stock, same
        # quantity twice."
        #
        # The shares are not lost: `_unallocated_out` counts them, and `consolidated_value` walks
        # the holdings rather than the rows, so the total is unchanged either way.
        if portfolio_id in ledger.pile_ids:
            continue
        members = _members_of(ledger, portfolio)
        row = PortfolioRowOut(
            portfolio_id=portfolio_id,
            name=portfolio.name,
            kind=portfolio.kind,
            source=portfolio.source,
            source_badge=_source_badge(portfolio, ledger.publishers.get(portfolio_id)),
            publisher=ledger.publishers.get(portfolio_id),
            started_on=portfolio.started_on,
            value=(
                values.get(portfolio_id, ZERO)
                if portfolio.kind is PortfolioKind.CAPITAL
                else _monitoring_value(ledger, members)
            ),
            cash=portfolio_cash_by_id.get(portfolio_id, ZERO),
            counts_toward_total=portfolio.kind is PortfolioKind.CAPITAL,
            excluded_note=(None if portfolio.kind is PortfolioKind.CAPITAL else MONITORING_NOTE),
            todays_pnl=_todays_move(
                ledger,
                [
                    position
                    for position in members
                    if position.key.instrument_id not in ledger.prices.unpriced
                ],
                label="Change since the previous close",
            ),
            headline_return=_headline_for(
                portfolio, [_nav_point(row) for row in series.get(portfolio_id, [])]
            ),
            model_return=_model_for(portfolio, model_values.get(portfolio_id)),
            brokers=[
                _broker_ref(ledger.brokers[account_id])
                for account_id in sorted({p.key.broker_account_id for p in members})
                if account_id in ledger.brokers
            ],
            broker_count=len({position.key.broker_account_id for position in members}),
            holdings_count=len(members),
            status=_status_for(
                pending=freeze.is_pending(portfolio_id),
                rebalance_due=portfolio_id in ledger.rebalance_due_portfolio_ids,
            ),
            pending_reconciliation=freeze.is_pending(portfolio_id),
            benchmark_name=_benchmark_name(ledger, portfolio_id),
        )
        if portfolio.kind is PortfolioKind.CAPITAL:
            capital_rows.append(row)
        else:
            monitoring_rows.append(row)

    consolidated_rows = _windowed(series.get(None, []), window)
    chart = _series_out(
        consolidated_rows,
        _SeriesMeta(
            portfolio_id=None,
            window=window,
            label="Consolidated time-weighted return",
            since=None,
        ),
    )

    ribbon = attention_items(
        AttentionInputs(
            # Today, not the price date. §6.4's stale-price row exists to say *how far behind*
            # the market data is, and a ribbon drawn as of the data's own newest day can never
            # notice that the data has stopped arriving — it would always be zero days behind.
            as_of=dt.datetime.now(tz=dt.UTC).date(),
            holdings=holdings,
            allocations=allocations,
            entries=ledger.entries,
            rebalance_due_portfolio_ids=sorted(ledger.rebalance_due_portfolio_ids),
            prices_as_of=ledger.prices.as_of,
            check_prices=bool(ledger.positions),
        )
    )

    synced_on = max((row.as_of for row in ledger.broker_cash), default=None)
    return OverviewOut(
        prices_as_of=ledger.prices.as_of,
        prices_label=_label_for_prices(ledger.prices.as_of),
        holdings_synced_on=synced_on,
        holdings_synced_label=_label_for_sync(synced_on),
        sync_status=_sync_status(ledger),
        hero=hero,
        chart=chart,
        attention=[_attention_out(item) for item in ribbon],
        portfolios=capital_rows,
        monitoring_views=monitoring_rows,
        unallocated=_unallocated_out(ledger, unallocated_cash, freeze),
        open_reconciliation_count=sum(1 for entry in ledger.entries if entry.freezes),
    )


def _attention_out(item: AttentionItem) -> AttentionOut:
    return AttentionOut(
        kind=item.kind.value,
        message=item.message,
        count=item.count,
        subject_ids=list(item.subject_ids),
        since=item.since,
    )


def _sync_status(ledger: _Ledger) -> list[SyncStatusOut]:
    """§6.1's per-broker sync status, one row per connected account, dated or explicitly never."""
    dated = {int(row.broker_account_id): row.as_of for row in ledger.broker_cash}
    return [
        SyncStatusOut(
            broker=_broker_ref(account),
            synced_on=dated.get(account_id),
            label=_label_for_sync(dated.get(account_id)),
        )
        for account_id, account in sorted(ledger.brokers.items())
    ]


def _members_of(ledger: _Ledger, portfolio: LedgerPortfolio) -> list[_Position]:
    """The positions one portfolio holds — its allocations, or for a lens its membership rows."""
    if portfolio.kind is PortfolioKind.CAPITAL:
        return [
            position
            for position in ledger.positions
            if portfolio.portfolio_id in position.capital_slices
        ]
    return [
        position
        for position in ledger.positions
        if portfolio.portfolio_id in position.monitoring_portfolio_ids
    ]


def _monitoring_value(ledger: _Ledger, members: Sequence[_Position]) -> Decimal:
    """A lens's market value, summed on its own and never through the totals path.

    §4.1 excludes monitoring views from every total, and the ledger enforces that by refusing to
    let one hold an allocation at all — so :func:`portfolio_values` cannot produce this number and
    must not be asked to. It is computed here, for display only, and lands in a field that says
    ``counts_toward_total=False`` beside a note in §4.1's own words.
    """
    total = ZERO
    for position in members:
        value = ledger.value_of(position.holding)
        if value is not None:
            total += value
    return money(total)


def _benchmark_name(ledger: _Ledger, portfolio_id: int) -> str | None:
    row = ledger.rows.get(portfolio_id)
    if row is None or row.benchmark_index_id is None:
        return None
    index = ledger.benchmarks.get(int(row.benchmark_index_id))
    return index.name if index is not None else None


def _valued_on(ledger: _Ledger, events: Sequence[LedgerCashFlow]) -> dt.date:
    """The date the closing value handed to XIRR is true for.

    The closing value is the ledger priced at the latest close, so that close is the date the
    cash-flow series ends on — not the date of the last transfer. Using the transfer date would
    ask the solver for the return of a series whose final inflow and final value land on the same
    day, which is a rate over no elapsed time.

    :func:`~baskfy_core.cash_ledger.portfolio_xirr` refuses an ``as_of`` earlier than the last
    event, and rightly: such a series describes a portfolio valued before its own history
    finished. So when the prices are older than the last transfer — a sync that arrived ahead of
    the nightly close — the last event's date is used instead, and the figure measures the
    shorter window rather than raising on a page load.
    """
    last_event = max(flow.occurred_on for flow in events)
    prices_on = ledger.prices.as_of
    return prices_on if prices_on is not None and prices_on >= last_event else last_event


def _consolidated_xirr(ledger: _Ledger, closing_value: Decimal) -> LabelledRateOut:
    """§5.2's consolidated XIRR — the user's own money-weighted experience, labelled.

    Computed from §4.4's internal flows and nothing else: ``ASSIGN`` and ``RELEASE`` are the two
    events that are genuinely the user putting money in and taking it out, while buys, sells and
    dividends are already inside the closing value and counting them again would count them twice.
    That filtering is :func:`~baskfy_core.cash_ledger.xirr_events`' job, and this calls
    :func:`~baskfy_core.cash_ledger.portfolio_xirr` once per capital portfolio and once more over
    the flows as a whole.

    The consolidated series is assembled by re-labelling every flow onto a single synthetic
    portfolio id, because the domain's XIRR takes one portfolio at a time and the consolidated
    question is "what did *this user* earn on the money they moved into Baskfy's portfolios" —
    the same flows, one series. Nothing is stored under that id; it never leaves this function.

    ``None`` with a reason whenever the flows do not solve, which is the honest answer for an
    account that has never assigned cash: a money-weighted return with no contribution to weight
    is not a small number, it is not a number.
    """
    label = "XIRR since your first cash assignment"
    events = [flow for flow in ledger.flows if flow.kind.is_xirr_event]
    if not events:
        return LabelledRateOut(
            label=label,
            value=None,
            unavailable_reason="Assign cash to a portfolio to start measuring XIRR",
        )
    since = min(flow.occurred_on for flow in events)
    as_of = _valued_on(ledger, events)
    synthetic = -1
    combined = [
        LedgerCashFlow(
            broker_account_id=flow.broker_account_id,
            kind=flow.kind,
            amount=flow.amount,
            occurred_on=flow.occurred_on,
            portfolio_id=synthetic,
        )
        for flow in events
    ]
    rate = portfolio_xirr(combined, synthetic, as_of=as_of, closing_value=closing_value)
    return LabelledRateOut(
        label=label,
        since=since,
        value=rate,
        unavailable_reason=(
            None if rate is not None else "Not enough cash-flow history to solve a rate yet"
        ),
    )


def _consolidated_twr(rows: Sequence[PortfolioNavDaily]) -> LabelledRateOut:
    """§5.2's consolidated TWR — strategy quality, flow neutral, beside the XIRR, never merged."""
    points = [_nav_point(row) for row in rows]
    value = _chain_linked(points)
    return LabelledRateOut(
        label="Time-weighted return since your first valuation",
        since=rows[0].date if rows else None,
        value=value,
        unavailable_reason=(None if value is not None else "Not enough end-of-day valuations yet"),
    )


def _unallocated_out(ledger: _Ledger, cash: Decimal, freeze: FreezeReport) -> UnallocatedOut:
    """§6.6's section: cash plus the holdings in no capital portfolio, aggregated for display.

    "In no capital portfolio" is the ledger's own answer
    (:func:`~baskfy_core.allocation_ledger.unallocated_holdings`), not a re-derivation — which
    matters because a holding that sits in three monitoring views is still unallocated (§4.1), and
    a second implementation is exactly where that stops being true.
    """
    loose_keys = {
        holding.key for holding in unallocated_holdings(ledger.holdings, ledger.allocations)
    }
    by_instrument: dict[int, list[_Position]] = {}
    for position in ledger.positions:
        if position.key in loose_keys:
            by_instrument.setdefault(position.key.instrument_id, []).append(position)

    rows: list[UnallocatedHoldingOut] = []
    total = ZERO
    for instrument_id, group in sorted(by_instrument.items()):
        instrument = ledger.instruments.get(instrument_id)
        if instrument is None:
            continue
        quantity = sum((position.holding.quantity for position in group), ZERO)
        value = ZERO
        for position in group:
            priced = ledger.value_of(position.holding)
            if priced is not None:
                value += priced
        total += value
        rows.append(
            UnallocatedHoldingOut(
                instrument=_instrument_ref(instrument),
                quantity=quantity,
                value=money(value),
                brokers=[
                    _broker_ref(ledger.brokers[position.key.broker_account_id])
                    for position in group
                    if position.key.broker_account_id in ledger.brokers
                ],
                monitoring_views=[
                    _portfolio_ref(ledger.portfolios[view_id])
                    for position in group
                    for view_id in position.monitoring_portfolio_ids
                    if view_id in ledger.portfolios
                ],
                pending_reconciliation=any(freeze.is_frozen(position.key) for position in group),
            )
        )

    holdings_value = money(total)
    return UnallocatedOut(
        cash=cash,
        cash_by_broker=[
            BrokerCashOut(
                broker=_broker_ref(ledger.brokers[int(row.broker_account_id)]),
                balance=row.balance,
                as_of=row.as_of,
            )
            for row in ledger.broker_cash
            if int(row.broker_account_id) in ledger.brokers
        ],
        holdings=rows,
        holdings_value=holdings_value,
        holdings_count=len(rows),
        total_value=money(cash + holdings_value),
        pending_reconciliation=freeze.unallocated_pending,
    )


# ---------------------------------------------------------------------------
# GET /portfolio/holdings — §2 and §6.7
# ---------------------------------------------------------------------------


@router.get("/holdings", response_model=HoldingsOut)
async def portfolio_holdings(
    session: SessionDep,
    principal: AuthenticatedDep,
) -> HoldingsOut:
    """§2's flat broker-level truth: every share, which broker, and what it is allocated to.

    §6.7 asks for the same stock at two brokers to be shown aggregated with the breakdown
    preserved — *HDFC Bank — 320 (Zerodha 200 · Upstox 120)* — and that is exactly what this
    returns: an aggregate row with the per-broker lines under it, never an aggregate that has
    thrown the lines away. The ledger keeps the two positions apart because they can be allocated
    apart and sold apart, and a display that merged them would make one of those facts
    unrepresentable.

    Unallocated holdings are in this list like everything else. They are the user's shares; a
    holdings view that quietly omitted them would be the one bug this endpoint cannot have.
    """
    ledger = await _load_ledger(session, principal.require_user())
    freeze = freeze_report(ledger.entries, ledger.ledger_positions)

    by_instrument: dict[int, list[_Position]] = {}
    for position in ledger.positions:
        by_instrument.setdefault(position.key.instrument_id, []).append(position)

    rows: list[AggregatedHoldingOut] = []
    total = ZERO
    unallocated_count = 0
    for instrument_id, group in sorted(by_instrument.items()):
        instrument = ledger.instruments.get(instrument_id)
        if instrument is None:
            continue
        lines = [_broker_line(ledger, position, freeze) for position in group]
        quantity = sum((position.holding.quantity for position in group), ZERO)
        value: Decimal | None = None
        for position in group:
            priced = ledger.value_of(position.holding)
            if priced is not None:
                value = (value or ZERO) + priced
        if value is not None:
            total += value

        # 0035: a holding can be split ACROSS PORTFOLIOS as well as across brokers, and this row
        # already had the vocabulary for it. `allocated_ids` now gathers every slice of every leg,
        # so `split_across_portfolios` is true for one broker filed four ways exactly as it was
        # for one name held at two brokers — which is what the user means by the word either way.
        allocated_ids: set[int | None] = set()
        for position in group:
            allocated_ids.update(position.capital_slices)
            if position.unallocated_quantity > ZERO or not position.capital_slices:
                allocated_ids.add(UNALLOCATED)
        one_allocation = len(allocated_ids) == 1 and allocated_ids != {UNALLOCATED}
        allocation_id = next(iter(allocated_ids)) if one_allocation else None
        if allocated_ids == {UNALLOCATED}:
            unallocated_count += 1

        rows.append(
            AggregatedHoldingOut(
                instrument=_instrument_ref(instrument),
                quantity=quantity,
                price=ledger.prices.latest.get(instrument_id),
                price_as_of=ledger.prices.dated.get(instrument_id),
                value=money(value) if value is not None else None,
                allocation=(
                    _portfolio_ref(ledger.portfolios[allocation_id])
                    if allocation_id is not None and allocation_id in ledger.portfolios
                    else None
                ),
                allocated=allocated_ids != {UNALLOCATED},
                split_across_portfolios=len(allocated_ids) > 1,
                monitoring_views=[
                    _portfolio_ref(ledger.portfolios[view_id])
                    for position in group
                    for view_id in position.monitoring_portfolio_ids
                    if view_id in ledger.portfolios
                ],
                brokers=lines,
                pending_reconciliation=any(freeze.is_frozen(p.key) for p in group),
            )
        )

    synced_on = max((row.as_of for row in ledger.broker_cash), default=None)
    return HoldingsOut(
        prices_as_of=ledger.prices.as_of,
        prices_label=_label_for_prices(ledger.prices.as_of),
        holdings_synced_on=synced_on,
        holdings_synced_label=_label_for_sync(synced_on),
        rows=rows,
        total_value=money(total),
        unallocated_count=unallocated_count,
        unpriced_instrument_ids=list(ledger.prices.unpriced),
    )


def _broker_line(
    ledger: _Ledger, position: _Position, freeze: FreezeReport
) -> HoldingBrokerLineOut:
    """One physical position as a line under its aggregated row (§6.7)."""
    account = ledger.brokers.get(position.key.broker_account_id)
    value = ledger.value_of(position.holding)
    # Named only when there is ONE name to give (0035). A split leg has no single allocation, and
    # printing the first of four would be a caption that is wrong three times out of four.
    allocation_id = position.sole_capital_portfolio_id
    return HoldingBrokerLineOut(
        broker=(
            _broker_ref(account)
            if account is not None
            else BrokerRefOut(
                broker_account_id=position.key.broker_account_id,
                broker_id="unknown",
                label="unknown",
            )
        ),
        quantity=position.holding.quantity,
        unallocated_quantity=position.unallocated_quantity,
        allocations=[
            HoldingSliceOut(
                portfolio=_portfolio_ref(ledger.portfolios[portfolio_id]), quantity=quantity
            )
            for portfolio_id, quantity in position.capital_slices.items()
            if portfolio_id in ledger.portfolios
        ],
        price=ledger.prices.latest.get(position.key.instrument_id),
        value=value,
        avg_price=position.holding.avg_price,
        cost_basis=cost_basis(position.holding),
        allocation=(
            _portfolio_ref(ledger.portfolios[allocation_id])
            if allocation_id is not None and allocation_id in ledger.portfolios
            else None
        ),
        monitoring_views=[
            _portfolio_ref(ledger.portfolios[view_id])
            for view_id in position.monitoring_portfolio_ids
            if view_id in ledger.portfolios
        ],
        first_bought_on=position.first_bought_on,
        history_source=position.history_source,
        pending_reconciliation=freeze.is_frozen(position.key),
    )


# ---------------------------------------------------------------------------
# GET /portfolio/activity — §7
# ---------------------------------------------------------------------------


@router.get("/activity", response_model=ActivityOut)
async def portfolio_activity(
    session: SessionDep,
    principal: AuthenticatedDep,
    portfolio_id: Annotated[
        int | None, Query(ge=1, description="Only this portfolio's activity.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=_ACTIVITY_MAX)] = _ACTIVITY_DEFAULT,
) -> ActivityOut:
    """§7's activity: trades, cash assignments, dividends, corporate actions, reconciliation.

    Five sources, one ordered list, newest first. They are merged rather than paginated
    separately because the question a user brings here — "why does this number look like that" —
    is answered by the sequence of events, and three lists in three tabs is that sequence taken
    apart.

    **A corporate action is not a P&L event** (§4.5, criterion 6), and the row says so on the
    wire: ``is_pnl_event=False``. So does a reconciliation row — a question being asked or
    answered is bookkeeping, not money. Marking them is what stops a client from summing the feed
    into a figure that double-counts a split.
    """
    user_id = principal.require_user()
    if portfolio_id is not None:
        await _owned_portfolio(session, portfolio_id, principal)
    ledger = await _load_ledger(session, user_id)

    items: list[ActivityItemOut] = []
    for flow in ledger.flow_rows:
        if portfolio_id is not None and (
            flow.portfolio_id is None or int(flow.portfolio_id) != portfolio_id
        ):
            continue
        items.append(_flow_activity(ledger, flow))

    for entry in ledger.entries:
        if portfolio_id is not None and portfolio_id not in {
            entry.resolved_portfolio_id,
            entry.item.suggested_portfolio_id,
        }:
            continue
        items.append(_reconciliation_activity(ledger, entry, ledger.item_rows[entry.item_id]))

    items.extend(await _corporate_actions(session, ledger, portfolio_id))

    items.sort(key=lambda item: (item.on, item.kind.value), reverse=True)
    return ActivityOut(items=items[:limit], total=len(items))


def _flow_activity(ledger: _Ledger, row: PortfolioCashFlow) -> ActivityItemOut:
    """One cash-ledger row as an activity line, in §8's vocabulary.

    The sentences never say *sleeve*, *divide*, *file under* or *run by hand*. An assignment is
    "Assigned ₹x to {portfolio}", which is what §4.4 says it is — the user moving their own money
    into a portfolio, and the one cash event a per-portfolio XIRR is measured from.
    """
    kind = ActivityKind(row.kind)
    portfolio = (
        ledger.portfolios.get(int(row.portfolio_id)) if row.portfolio_id is not None else None
    )
    instrument = (
        ledger.instruments.get(int(row.instrument_id)) if row.instrument_id is not None else None
    )
    account = ledger.brokers.get(int(row.broker_account_id))
    name = portfolio.name if portfolio is not None else "Unallocated"
    symbol = instrument.symbol if instrument is not None else ""
    description = {
        ActivityKind.BUY: f"Bought {row.quantity} {symbol} in {name}",
        ActivityKind.SELL: f"Sold {row.quantity} {symbol} from {name}",
        ActivityKind.DIVIDEND: f"Dividend from {symbol} in {name}",
        ActivityKind.ASSIGN: f"Assigned {row.amount} to {name}",
        ActivityKind.RELEASE: f"Released {row.amount} from {name} back to Unallocated",
        ActivityKind.EXTERNAL_DEPOSIT: f"Deposit of {row.amount} into Unallocated cash",
        ActivityKind.EXTERNAL_WITHDRAWAL: f"Withdrawal of {row.amount} from Unallocated cash",
    }[kind]
    return ActivityItemOut(
        kind=kind,
        on=row.occurred_on,
        description=row.note or description,
        portfolio=_portfolio_ref(portfolio) if portfolio is not None else None,
        instrument=_instrument_ref(instrument) if instrument is not None else None,
        broker=_broker_ref(account) if account is not None else None,
        quantity=row.quantity,
        amount=row.amount,
        is_pnl_event=True,
    )


def _reconciliation_activity(
    ledger: _Ledger, entry: InboxEntry, row: ReconciliationItem
) -> ActivityItemOut:
    """§7's reconciliation history. A dismissed question stays in the feed, never deleted.

    §4.3's freeze is part of why a return series looks the way it does, and a question that was
    asked and waved away is part of the explanation. Deleting the row would make the shape of the
    chart unexplainable a year later — which is the reason ``dismiss`` is a state and not a
    ``DELETE``.
    """
    instrument = ledger.instruments.get(entry.key.instrument_id)
    account = ledger.brokers.get(entry.key.broker_account_id)
    resolved = (
        ledger.portfolios.get(entry.resolved_portfolio_id)
        if entry.resolved_portfolio_id is not None
        else None
    )
    if entry.state is ReconciliationState.OPEN:
        description = entry.question
    elif resolved is not None:
        description = f"Reconciled to {resolved.name}"
    else:
        description = "Reconciliation item dismissed — nothing to attribute"
    return ActivityItemOut(
        kind=ActivityKind.RECONCILIATION,
        on=entry.resolved_on or entry.detected_on,
        description=description,
        portfolio=_portfolio_ref(resolved) if resolved is not None else None,
        instrument=_instrument_ref(instrument) if instrument is not None else None,
        broker=_broker_ref(account) if account is not None else None,
        quantity=entry.quantity,
        is_pnl_event=False,
        reconciliation_item_id=entry.item_id,
        reconciliation_state=entry.state,
    )


async def _corporate_actions(
    session: AsyncSession, ledger: _Ledger, portfolio_id: int | None
) -> list[ActivityItemOut]:
    """Splits, bonuses and dividends on instruments the user holds, since each portfolio began.

    Reported and marked ``is_pnl_event=False``: §4.5 is explicit that a corporate action changes
    quantity and average price and **must not** appear as a P&L event, and criterion 6 is the
    test of it. Showing them is still necessary — a user whose share count doubled overnight has
    a question, and this is the row that answers it.
    """
    positions = (
        ledger.positions
        if portfolio_id is None
        else [
            position
            for position in ledger.positions
            if portfolio_id in position.capital_slices
            or portfolio_id in position.monitoring_portfolio_ids
        ]
    )
    instrument_ids = sorted({position.key.instrument_id for position in positions})
    if not instrument_ids:
        return []
    earliest = min((portfolio.started_on for portfolio in ledger.portfolios.values()), default=None)
    stmt = select(CorporateAction).where(CorporateAction.instrument_id.in_(instrument_ids))
    if earliest is not None:
        stmt = stmt.where(CorporateAction.ex_date >= earliest)
    rows = list((await session.scalars(stmt.order_by(CorporateAction.ex_date.desc()))).all())

    items: list[ActivityItemOut] = []
    for row in rows:
        instrument = ledger.instruments.get(int(row.instrument_id))
        if instrument is None:
            continue
        items.append(
            ActivityItemOut(
                kind=ActivityKind.CORPORATE_ACTION,
                on=row.ex_date,
                description=f"{row.action_type.title()} on {instrument.symbol}",
                instrument=_instrument_ref(instrument),
                amount=row.amount,
                is_pnl_event=False,
            )
        )
    return items


# ---------------------------------------------------------------------------
# GET /portfolio/reconciliation and its one answer — §4.3
# ---------------------------------------------------------------------------


@router.get("/reconciliation", response_model=ReconciliationInboxOut)
async def portfolio_reconciliation(
    session: SessionDep,
    principal: AuthenticatedDep,
    state: Annotated[
        ReconciliationState | None,
        Query(description="Filter by lifecycle state. Omit for the open questions only."),
    ] = None,
) -> ReconciliationInboxOut:
    """§4.3's inbox, plus what the open questions in it are currently forbidding.

    The default is the open questions, because that is what an inbox is: the list of things sync
    could not decide. History is reachable — pass a state, or read §7's activity feed, where a
    resolved question sits in sequence with the events around it.

    :attr:`ReconciliationInboxOut.pending_portfolio_ids` is not a convenience. §6.5's Status
    column and §4.3's freeze are the same fact, and a client that derived one from the item list
    would eventually derive it differently from the way
    :func:`~baskfy_core.reconciliation.freeze_report` does — most obviously for ``UNKNOWN_INFLOW``,
    where the shares exist in the account and nothing is allocated, so it is *Unallocated* that
    goes pending.
    """
    user_id = principal.require_user()
    ledger = await _load_ledger(session, user_id)
    freeze = freeze_report(ledger.entries, ledger.ledger_positions)
    wanted = [
        entry
        for entry in ledger.entries
        if (state is None and entry.freezes) or (state is not None and entry.state is state)
    ]
    return ReconciliationInboxOut(
        items=[_item_out(ledger, entry) for entry in wanted],
        open_count=sum(1 for entry in ledger.entries if entry.freezes),
        pending_portfolio_ids=sorted(freeze.pending_portfolio_ids),
        unallocated_pending=freeze.unallocated_pending,
        consolidated_pending=freeze.consolidated_pending,
    )


def _item_out(ledger: _Ledger, entry: InboxEntry) -> ReconciliationItemOut:
    row = ledger.item_rows[entry.item_id]
    instrument = ledger.instruments.get(entry.key.instrument_id)
    account = ledger.brokers.get(entry.key.broker_account_id)
    suggested = (
        ledger.portfolios.get(entry.item.suggested_portfolio_id)
        if entry.item.suggested_portfolio_id is not None
        else None
    )
    resolved = (
        ledger.portfolios.get(entry.resolved_portfolio_id)
        if entry.resolved_portfolio_id is not None
        else None
    )
    return ReconciliationItemOut(
        item_id=entry.item_id,
        state=entry.state,
        reason=entry.item.reason.value,
        question=entry.question,
        instrument=(
            _instrument_ref(instrument)
            if instrument is not None
            else InstrumentRefOut(
                instrument_id=entry.key.instrument_id, symbol="", name="Unknown instrument"
            )
        ),
        broker=(
            _broker_ref(account)
            if account is not None
            else BrokerRefOut(
                broker_account_id=entry.key.broker_account_id,
                broker_id="unknown",
                label="unknown",
            )
        ),
        quantity=entry.quantity,
        detected_on=entry.detected_on,
        suggested_portfolio=_portfolio_ref(suggested) if suggested is not None else None,
        resolved_portfolio=_portfolio_ref(resolved) if resolved is not None else None,
        resolved_at=row.resolved_at,
        freezes=entry.freezes,
    )


@router.post("/reconciliation/{item_id}/resolve", response_model=ResolveOut)
async def resolve_reconciliation_item(
    body: ResolveBody,
    session: SessionDep,
    principal: AuthenticatedDep,
    item_id: Annotated[int, Path(ge=1)],
) -> ResolveOut:
    """Answer one of §4.3's questions by naming the capital portfolio the change belongs to.

    **Not an order path.** This records a bookkeeping decision: which logical portfolio a change
    sync already observed should count against. It reaches no broker and moves no share; the
    shares moved before we ever saw them.

    Two things must land together and they do, in one transaction:
    :class:`~baskfy_core.reconciliation.ResolutionOutcome` carries both the entry in its new
    terminal state *and* the allocation the answer implies, precisely so a caller cannot persist
    one and lose the other. An item marked RESOLVED whose holding is still in no portfolio would
    be an unfrozen holding with nowhere to contribute — a quietly wrong total in place of a
    loudly pending one.

    The three refusals come from the domain, not from here: an already-answered question is not
    re-answerable, a portfolio that does not exist cannot be named, and a monitoring view holds
    no allocation at all (§4.1) so resolving to one would leave the total short. Each becomes a
    400 with the domain's own sentence, which names what was wrong in words the user can act on.
    """
    user_id = principal.require_user()
    ledger = await _load_ledger(session, user_id)
    row = ledger.item_rows.get(item_id)
    if row is None:
        raise not_found("reconciliation item", str(item_id))
    # Ownership of the target is checked before the domain sees it, so another tenant's portfolio
    # answers NOT_FOUND rather than reaching `resolve` and coming back as "does not exist" —
    # the same answer, but arrived at without ever loading a foreign row.
    await _owned_portfolio(session, body.portfolio_id, principal)

    entry = next(entry for entry in ledger.entries if entry.item_id == item_id)
    today = dt.datetime.now(tz=dt.UTC).date()
    try:
        outcome = resolve(entry, body.portfolio_id, ledger.portfolios, today)
    except ValueError as exc:
        raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, str(exc)) from exc

    await session.execute(
        update(ReconciliationItem)
        .where(ReconciliationItem.id == item_id, ReconciliationItem.user_id == user_id)
        .values(
            state=ReconciliationState.RESOLVED.value,
            resolved_portfolio_id=body.portfolio_id,
            resolved_at=dt.datetime.now(tz=dt.UTC),
        )
    )
    allocation = outcome.implied_allocation
    assert allocation is not None, "resolve() always implies an allocation; dismiss() does not"
    await _apply_allocation(session, ledger, allocation, resolved_on=today)
    await session.flush()

    refreshed = await _load_ledger(session, user_id)
    return ResolveOut(
        item=_item_out(refreshed, next(e for e in refreshed.entries if e.item_id == item_id)),
        allocated_to=_portfolio_ref(ledger.portfolios[body.portfolio_id]),
        unfroze=outcome.unfreezes,
    )


async def _apply_allocation(
    session: AsyncSession,
    ledger: _Ledger,
    allocation: Allocation,
    *,
    resolved_on: dt.date,
) -> None:
    """File ``allocation.quantity`` shares into ``allocation.portfolio_id``, taking them from
    somewhere they already are. **Shares are conserved by this function, always.**

    This is where the Phase-3 invariant actually lives. 0035 removed the unique index that used
    to enforce "one capital portfolio per holding" and deliberately put nothing in its place,
    because ``sum(slices) <= held`` is a fact about a group of rows that no CHECK can see. What
    makes it true is that every path into `portfolio_holding` moves quantity rather than
    asserting a total — and this is that path.

    Before 0035 the whole row moved: its ``portfolio_id`` changed and the position was now
    somewhere else, entire. Now a *quantity* moves:

    1. take from the **unallocated remainder** first, because those shares are spoken for by
       nobody and moving them costs no other portfolio's return series;
    2. then from other capital slices, **smallest first**, so filing 30 out of 20/34/36 empties
       the 20 before it touches the 34 — the fewest portfolios disturbed, and the small slice a
       user is most likely to have meant to consolidate;
    3. a source slice drained to zero is deleted rather than left as a zero row, because a
       portfolio holding zero of something is not a holding, and the ledger's `Allocation`
       refuses to represent it.

    Asking for more than the position holds is a caller bug and raises, rather than filing what
    is available: silently allocating 80 when 100 was asked for leaves the user believing a
    portfolio holds shares it does not. `new_portfolio` checks first so a *user* gets a sentence
    naming the numbers instead.

    ``resolved_on`` dates the new row. A resolution is an answer about attribution, not a
    restatement of how many shares exist, so `first_bought_on` and `history_source` are carried
    across from the position rather than re-derived.
    """
    existing = next(
        (position for position in ledger.positions if position.key == allocation.key), None
    )
    target_id = allocation.portfolio_id
    wanted = allocation.quantity

    if existing is None:
        # Nothing recorded for this position at all — an inflow answered before a sync ever saw
        # it. There is nothing to take from, so the row is written as stated.
        session.add(
            PortfolioHolding(
                portfolio_id=target_id,
                instrument_id=allocation.key.instrument_id,
                broker_account_id=allocation.key.broker_account_id,
                portfolio_kind=PortfolioKind.CAPITAL.value,
                quantity=wanted,
                avg_price=None,
                added_on=resolved_on,
                first_bought_on=None,
                history_source="NONE",
            )
        )
        return

    if wanted > existing.holding.quantity:
        raise ValueError(
            f"cannot file {wanted} shares of {allocation.key}: only "
            f"{existing.holding.quantity} are held"
        )

    # (1) the broker's pile — the unfiled shares, spoken for by nobody — then (2) other slices
    # smallest first. The target's own slice is never a source: taking from it to give to it
    # would be a no-op that also deleted the row.
    #
    # The pile's rows are DONORS, not a derived remainder, and that distinction is the whole of
    # this fix (11 Sep 2026). `unallocated_quantity` is a computed number; the shares behind it
    # sit in "Zerodha holdings" as real rows, and filing 20 into Long term has to decrement one
    # of them. Subtracting the computed remainder and leaving the rows alone would have written
    # a 20-share slice beside an untouched 100-share pile — 120 shares of a 100-share position,
    # the same double-count the sync was fixed for, arriving through the other door.
    outstanding = wanted
    donors = [
        *sorted(existing.pile_slices.items(), key=lambda pair: pair[0]),
        *sorted(
            ((pid, qty) for pid, qty in existing.capital_slices.items() if pid != target_id),
            key=lambda pair: (pair[1], pair[0]),
        ),
    ]
    for donor_id, donor_quantity in donors:
        if outstanding <= ZERO:
            break
        taken = min(outstanding, donor_quantity)
        outstanding -= taken
        if taken >= donor_quantity:
            await session.execute(
                delete(PortfolioHolding).where(
                    PortfolioHolding.portfolio_id == donor_id,
                    PortfolioHolding.instrument_id == allocation.key.instrument_id,
                    PortfolioHolding.broker_account_id == allocation.key.broker_account_id,
                )
            )
        else:
            await session.execute(
                update(PortfolioHolding)
                .where(
                    PortfolioHolding.portfolio_id == donor_id,
                    PortfolioHolding.instrument_id == allocation.key.instrument_id,
                    PortfolioHolding.broker_account_id == allocation.key.broker_account_id,
                )
                .values(quantity=donor_quantity - taken)
            )

    already = existing.capital_slices.get(target_id)
    if already is not None:
        await session.execute(
            update(PortfolioHolding)
            .where(
                PortfolioHolding.portfolio_id == target_id,
                PortfolioHolding.instrument_id == allocation.key.instrument_id,
                PortfolioHolding.broker_account_id == allocation.key.broker_account_id,
            )
            .values(quantity=already + wanted)
        )
        return

    session.add(
        PortfolioHolding(
            portfolio_id=target_id,
            instrument_id=allocation.key.instrument_id,
            broker_account_id=allocation.key.broker_account_id,
            portfolio_kind=PortfolioKind.CAPITAL.value,
            quantity=wanted,
            avg_price=existing.holding.avg_price,
            added_on=resolved_on,
            first_bought_on=existing.first_bought_on,
            history_source=existing.history_source,
        )
    )


# ---------------------------------------------------------------------------
# GET /portfolio/suggestions — §6.6
#
# Declared before `/{portfolio_id}`, like every other literal path here.
# ---------------------------------------------------------------------------


async def _sector_map(session: AsyncSession, instrument_ids: Sequence[int]) -> dict[int, str]:
    """``{instrument_id -> sector}``, derived from point-in-time membership of a sectoral index.

    There is no ``instrument.sector`` column, and inventing one here would be a reference-data
    decision taken inside a read handler. What this database *does* record is index membership:
    ``index_def`` separates selectable universes (``is_universe``) from every other index, and the
    others are the sector indices — NIFTY BANK, NIFTY IT, NIFTY PHARMA. Membership of one of them
    is the closest thing to a sector that exists here, and it is a fact somebody loaded rather
    than a label this router made up.

    Two properties matter and both are in the ``DISTINCT ON``. The **most recent** membership row
    wins, because sector membership is point-in-time and a stock that left an index last year is
    not still in it (house rule 5's habit applied to a read). And ties break on ``index_def.id``,
    so a stock in two sector indices gets the same answer on every call — a suggestion screen
    that re-ordered itself between two refreshes would be a screen nobody trusts.

    An empty result is the expected answer on a database that has loaded no sector membership,
    and the caller reports it as a missing input rather than as a verdict about the holdings.
    """
    if not instrument_ids:
        return {}
    rows = (
        await session.execute(
            select(IndexMemberDaily.instrument_id, IndexDef.name)
            .join(IndexDef, IndexDef.id == IndexMemberDaily.index_id)
            .where(
                IndexMemberDaily.instrument_id.in_(list(instrument_ids)),
                IndexDef.is_universe.is_(False),
            )
            .distinct(IndexMemberDaily.instrument_id)
            .order_by(
                IndexMemberDaily.instrument_id,
                IndexMemberDaily.date.desc(),
                IndexDef.id,
            )
        )
    ).all()
    return {int(row.instrument_id): row.name for row in rows}


async def _subscribed_baskets(session: AsyncSession, user_id: int) -> list[SubscribedBasket]:
    """The models this user actually follows, reduced to the stocks in their latest cut.

    Two relationships count as following a model, and both are ones the user asserted:

    * an **ACTIVE** ``cb_investment`` — they put money against the model;
    * a **SUBSCRIBED** portfolio whose sleeve names a basket — they filed holdings under it.

    A watchlist entry deliberately does not count. ``suggest_by_basket_overlap`` writes its own
    rationale — *"You subscribe to {name}, and you already hold 11 of its 15 stocks"* — and a
    bookmark is not a subscription. Passing a watched basket in would put a sentence on the screen
    that is not true, which is a worse failure than one suggestion fewer.

    Baskets with no constituents in their latest version are skipped rather than passed on:
    :class:`~baskfy_core.grouping_suggestions.SubscribedBasket` refuses an empty model, because an
    empty model overlaps everything and nothing and reporting 100% coverage of it would be a lie.
    """
    invested = select(CbInvestment.basket_id).where(
        CbInvestment.user_id == user_id, CbInvestment.status == "ACTIVE"
    )
    filed = (
        select(PortfolioSleeve.basket_id)
        .join(Portfolio, Portfolio.id == PortfolioSleeve.portfolio_id)
        .where(
            Portfolio.user_id == user_id,
            Portfolio.source == PortfolioSource.SUBSCRIBED.value,
            PortfolioSleeve.basket_id.is_not(None),
        )
    )
    basket_ids = sorted(
        {int(value) for value in (await session.scalars(invested)).all()}
        | {int(value) for value in (await session.scalars(filed)).all() if value is not None}
    )
    if not basket_ids:
        return []

    baskets = {
        int(row.id): row
        for row in (
            await session.scalars(select(CbBasket).where(CbBasket.id.in_(basket_ids)))
        ).all()
    }
    # The newest cut of each basket. `cb_basket_version` is append-only — a correction is a new
    # row, never an UPDATE — so "the model as it stands" is the highest `version_no`, and reading
    # anything older would ask the user about constituents the publisher has already replaced.
    latest = (
        select(
            CbBasketVersion.id.label("version_id"),
            CbBasketVersion.basket_id.label("basket_id"),
            func.row_number()
            .over(
                partition_by=CbBasketVersion.basket_id,
                order_by=CbBasketVersion.version_no.desc(),
            )
            .label("recency"),
        )
        .where(CbBasketVersion.basket_id.in_(basket_ids))
        .subquery()
    )
    rows = (
        await session.execute(
            select(latest.c.basket_id, CbConstituent.instrument_id)
            .join(CbConstituent, CbConstituent.version_id == latest.c.version_id)
            .where(latest.c.recency == 1)
        )
    ).all()

    members: dict[int, set[int]] = {}
    for row in rows:
        members.setdefault(int(row.basket_id), set()).add(int(row.instrument_id))

    found: list[SubscribedBasket] = []
    for basket_id in basket_ids:
        basket = baskets.get(basket_id)
        constituents = members.get(basket_id)
        if basket is None or not constituents:
            continue
        found.append(
            SubscribedBasket(
                basket_id=basket_id,
                name=basket.name,
                constituent_instrument_ids=frozenset(constituents),
            )
        )
    return found


def _suggestion_out(suggestion: GroupingSuggestion) -> GroupingSuggestionOut:
    """The dataclass on the wire. Field for field, and nothing re-derived — see the class."""
    return GroupingSuggestionOut(
        basis=suggestion.basis,
        proposed_name=suggestion.proposed_name,
        keys=[
            HoldingKeyOut(instrument_id=key.instrument_id, broker_account_id=key.broker_account_id)
            for key in suggestion.keys
        ],
        value=suggestion.value,
        rationale=suggestion.rationale,
        suggested_kind=suggestion.suggested_kind,
        basket_id=suggestion.basket_id,
        basket_coverage=suggestion.basket_coverage,
        missing_instrument_ids=list(suggestion.missing_instrument_ids),
    )


@router.get("/suggestions", response_model=SuggestionsOut)
async def portfolio_suggestions(
    session: SessionDep,
    principal: AuthenticatedDep,
) -> SuggestionsOut:
    """§6.6's first-run helper: the unallocated pile in, a short ranked list of named groups out.

    > First-run experience: connect broker → everything lands in Unallocated → the product
    > actively helps sort it (suggest groupings by sector, by purchase era, by overlap with a
    > subscribed basket). **Getting from 40 unallocated holdings to 4 named portfolios IS
    > activation.**

    This route gathers the four facts :class:`~baskfy_core.grouping_suggestions.SuggestionInputs`
    permits — sectors, first-bought dates, an as-of date and the models the user follows — and
    hands them to :func:`~baskfy_core.grouping_suggestions.suggest_groupings`. **The ranking, the
    rationale sentences, the coverage arithmetic and the two-holding minimum all live there**, and
    none of them is re-implemented here. That is the same rule the read routes above follow, and
    it is what lets ``apps/web/.../organize.ts`` port the rank key and stay in step: there is one
    definition of a suggestion and it is tested in ``packages/core``.

    **Unpriced positions are excluded, not zeroed.**
    :func:`~baskfy_core.allocation_ledger.holding_value` raises on a missing close and it is right
    to; a suggestion whose headline value quietly omitted a position would rank below where it
    belongs. They are named in ``unpriced_instrument_ids`` instead.

    **A missing input is reported, never disguised.** Sectors and purchase dates are both things
    this database frequently does not have (there is no sector column, and ``first_bought_on`` is
    NULL until a CAS import). §6.6's screen must be able to say *which* input was missing, because
    "we have no sector data" and "your holdings have nothing in common" are opposite messages and
    a bare empty list says the second one. ``bases`` carries the first;
    ``unavailable_reason`` is set only when no basis could run at all.

    Nothing here writes. Accepting a suggestion is ``POST /portfolio`` below, where a human has
    chosen the kind, the name and the benchmark — a suggestion is a sentence the product is
    prepared to say, never an allocation.
    """
    user_id = principal.require_user()
    ledger = await _load_ledger(session, user_id)
    today = dt.datetime.now(tz=dt.UTC).date()

    unallocated = unallocated_holdings(ledger.holdings, ledger.allocations)
    unpriced = sorted(
        {
            holding.key.instrument_id
            for holding in unallocated
            if holding.key.instrument_id in ledger.prices.unpriced
        }
    )
    priced = [
        holding
        for holding in unallocated
        if holding.key.instrument_id not in ledger.prices.unpriced
    ]
    prices = ledger.priced()
    unallocated_value = money(sum((holding_value(holding, prices) for holding in priced), ZERO))

    sectors = await _sector_map(
        session, sorted({position.key.instrument_id for position in ledger.positions})
    )
    # A purchase dated after today is corrupt rather than ancient, and `purchase_era` refuses it
    # by raising. Dropping the row keeps one bad date from costing every other holding its era.
    first_bought = {
        position.key: position.first_bought_on
        for position in ledger.positions
        if position.first_bought_on is not None and position.first_bought_on <= today
    }
    baskets = await _subscribed_baskets(session, user_id)

    # Only the facts that apply to the pile count as "we have this input". A sector map that
    # covers three allocated holdings and none of the unallocated ones is, for this screen, no
    # sector map at all — and reporting it as present would leave the empty list unexplained.
    pile = {holding.key for holding in priced}
    with_sector = sum(1 for holding in priced if holding.key.instrument_id in sectors)
    with_dates = sum(1 for key in first_bought if key in pile)

    inputs = SuggestionInputs(
        sectors=sectors,
        first_bought={key: on for key, on in first_bought.items() if key in pile},
        as_of=today,
        baskets=baskets,
    )
    suggestions = suggest_groupings(priced, prices, inputs)

    nothing_to_sort = not unallocated
    bases = [
        SuggestionBasisStatusOut(
            basis=SuggestionBasis.BASKET_OVERLAP,
            available=bool(baskets),
            considered=len(baskets),
            unavailable_reason=None if baskets else _NO_BASKETS,
        ),
        SuggestionBasisStatusOut(
            basis=SuggestionBasis.SECTOR,
            available=with_sector > 0,
            considered=with_sector,
            unavailable_reason=None if with_sector else _NO_SECTORS,
        ),
        SuggestionBasisStatusOut(
            basis=SuggestionBasis.PURCHASE_ERA,
            available=with_dates > 0,
            considered=with_dates,
            unavailable_reason=None if with_dates else _NO_PURCHASE_DATES,
        ),
    ]
    # Ordered as `_ALL_BASES` declares, so a client rendering the list never has to sort it.
    order = {basis: rank for rank, basis in enumerate(_ALL_BASES)}
    bases.sort(key=lambda status: order[status.basis])

    if nothing_to_sort:
        reason: str | None = _NOTHING_TO_SORT
    elif any(status.available for status in bases):
        reason = None
    else:
        reason = _NO_INPUTS

    return SuggestionsOut(
        suggestions=[_suggestion_out(suggestion) for suggestion in suggestions],
        sectors={str(instrument_id): name for instrument_id, name in sorted(sectors.items())},
        unallocated_count=len(unallocated),
        unallocated_value=unallocated_value,
        bases=bases,
        unavailable_reason=reason,
        unpriced_instrument_ids=unpriced,
    )


# ---------------------------------------------------------------------------
# POST /portfolio — §3, §4.1, §4.2, §6.7
# ---------------------------------------------------------------------------


async def _owned_broker_account(
    session: AsyncSession, broker_account_id: int, user_id: int
) -> None:
    """One of *this* caller's broker accounts, or ``NOT_FOUND``. Never ``FORBIDDEN``.

    A 403 confirms the id names a real account and says it belongs to somebody else, which is the
    fact a stranger is probing for. Same rule the reads above already follow for portfolios and
    reconciliation items.
    """
    found = await session.scalar(
        select(BrokerAccount.id).where(
            BrokerAccount.id == broker_account_id, BrokerAccount.user_id == user_id
        )
    )
    if found is None:
        raise not_found("broker account", str(broker_account_id))


async def _benchmark_index(session: AsyncSession, index_id: int) -> None:
    """The §6.3 overlay this portfolio overrides to, or ``NOT_FOUND``.

    ``index_def`` is reference data with no owner, so "not yours" cannot arise — but "does not
    exist" can, and storing an id that names nothing would give the portfolio a benchmark column
    that renders as no comparison at all while looking, in the database, like a deliberate choice.
    """
    found = await session.scalar(select(IndexDef.id).where(IndexDef.id == index_id))
    if found is None:
        raise not_found("benchmark index", str(index_id))


def _add_to_monitoring_view(
    session: AsyncSession, portfolio_id: int, position: _Position, *, added_on: dt.date
) -> None:
    """Put a holding in a lens without moving it (§4.1).

    A monitoring view holds **no allocation**. The row written here carries
    ``portfolio_kind = MONITORING``, which is exactly the value 0021's partial unique index
    excludes — so lenses overlap each other and overlap capital portfolios freely, and the
    holding's capital allocation (or its absence) is untouched. That is the whole difference
    between the two kinds, and it is a difference in what gets written, not in styling.

    Quantity, average price and provenance are copied from the position rather than restated:
    a lens is a view of the same shares, and §4.5's corporate-action fan-out keeps every copy in
    step. A second, independently-stated quantity would be a second truth to drift.
    """
    session.add(
        PortfolioHolding(
            portfolio_id=portfolio_id,
            instrument_id=position.key.instrument_id,
            broker_account_id=position.key.broker_account_id,
            portfolio_kind=PortfolioKind.MONITORING.value,
            quantity=position.holding.quantity,
            avg_price=position.holding.avg_price,
            added_on=added_on,
            first_bought_on=position.first_bought_on,
            history_source=position.history_source,
        )
    )


@router.post("", response_model=PortfolioDetailOut, status_code=201)
async def new_portfolio(
    body: NewPortfolioIn,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> PortfolioDetailOut:
    """§6.7's confirm step: name a grouping, choose its kind and source, and file holdings into it.

    **Not an order path.** This writes a ``portfolio`` row and moves bookkeeping allocations. It
    reaches no broker, names no side and no product, and moves no share — the shares are already
    in the user's demat and stay exactly where they are. §9's promise that a rebalance produces a
    plan the user takes to their broker is untouched by this route existing.

    WHY THIS IS NOT ``POST /portfolios``
    ------------------------------------
    The older route creates a node in the portfolio *forest* — a parent, a broker attribution, a
    list of symbols and quantities — and it hard-codes ``kind=CAPITAL``, ``source=HOLDING_GROUP``
    because those columns arrived under it in 0021/0022 and it has no way to ask. The redesign's
    model is a different object: §4.1's kind is an arithmetic decision, §3's source decides the
    headline metric, §6.7 asks for a benchmark, and §4.2 allocates **whole holdings the user
    already owns** rather than importing symbol-and-quantity lines. Bolting four fields onto the
    old body would have made one route that means two things; this is the second thing.

    THE FOUR REFUSALS, AND WHY EACH IS THE ANSWER IT IS
    ---------------------------------------------------
    1. **A holding already in another capital portfolio** — 400, naming the portfolio it is in.
       Acceptance criterion 2 is that a holding can never be in two capital portfolios, and
       0021's partial unique index enforces it in Postgres. The index would refuse this write on
       its own, with an integrity error nobody can act on; the pre-check exists so the answer
       says *which* holding and *which* portfolio. The ``IntegrityError`` guard below is still
       kept, because between the check and the insert is a window, and a race must produce the
       same sentence rather than a 500.
    2. **A foreign broker account, instrument or benchmark** — 404, never 403. See
       :func:`_owned_broker_account`.
    3. **A holding the caller does not hold** — 404, for the same reason: the ledger knows every
       position this user has, and a pair that is not one of them is not a thing to allocate.
    4. **A quantity** — 422, from the schema, because :class:`HoldingKeyIn` has no such field and
       forbids extras (§4.2).

    A **monitoring view takes no capital allocation** (§4.1). Its holdings are written as
    ``MONITORING`` rows, which 0021's index ignores, so a lens may freely overlap a capital
    portfolio *and* another lens — and the holdings it watches stay wherever they were, including
    Unallocated. That is why the conflict check above runs only for ``CAPITAL``: refusing a lens
    for overlapping would be refusing it for doing its job.
    """
    user_id = principal.require_user()
    today = dt.datetime.now(tz=dt.UTC).date()

    if body.benchmark_index_id is not None:
        await _benchmark_index(session, body.benchmark_index_id)

    # De-duplicated by holding and ordered the way the ledger orders holdings, so two bodies
    # naming the same legs in a different order produce the same portfolio and the same refusal.
    # Two lines for one holding ADD UP rather than the last winning: a client that sends
    # {ITC: 20} and {ITC: 14} means 34, and picking one silently would file the wrong number.
    requested = _requested_quantities(body.holdings)
    wanted = sorted(requested, key=lambda key: (key.instrument_id, key.broker_account_id))
    for key in wanted:
        await _owned_broker_account(session, key.broker_account_id, user_id)

    ledger = await _load_ledger(session, user_id)
    by_key = {position.key: position for position in ledger.positions}
    chosen: list[tuple[_Position, Decimal]] = []
    for key in wanted:
        position = by_key.get(key)
        if position is None:
            raise not_found(
                "holding",
                f"instrument {key.instrument_id} at broker account {key.broker_account_id}",
            )
        asked = requested[key]
        # `None` means "everything not already filed elsewhere" (see `HoldingKeyIn.quantity`).
        # Resolved here rather than in the schema because only the ledger knows the remainder.
        chosen.append((position, position.unallocated_quantity if asked is None else asked))

    if body.kind is PortfolioKind.CAPITAL:
        _refuse_over_allocation(ledger, chosen)
        empty = [position for position, quantity in chosen if quantity <= ZERO]
        if empty:
            raise Problem(
                ALLOCATION_REFUSED,
                "Every holding in a capital portfolio needs shares in it, and "
                + ", ".join(_symbol_of(ledger, position.key.instrument_id) for position in empty)
                + " has none free. Free some, or leave it out of the selection.",
                instrument_ids=[position.key.instrument_id for position in empty],
            )

    # NULL means "a roll-up spanning brokers" and NOT NULL means "everything under this is
    # attributable to exactly one account" (see `models.accounts.Portfolio`). Stating it only
    # when it is true is the point of the column; an empty portfolio spans nothing and gets NULL.
    accounts = {position.key.broker_account_id for position, _ in chosen}
    portfolio = Portfolio(
        user_id=user_id,
        name=body.name,
        kind=body.kind.value,
        source=body.source.value,
        # §5.2 measures every metric from this date and criterion 3 requires it on the wire, so
        # it is the day the grouping begins — today — and never inferred later from `created_at`.
        started_on=today,
        benchmark_index_id=body.benchmark_index_id,
        broker_account_id=accounts.pop() if len(accounts) == 1 else None,
    )

    try:
        async with session.begin_nested():
            session.add(portfolio)
            await session.flush()
            portfolio_id = int(portfolio.id)
            for position, quantity in chosen:
                if body.kind is PortfolioKind.CAPITAL:
                    await _apply_allocation(
                        session,
                        ledger,
                        Allocation(key=position.key, portfolio_id=portfolio_id, quantity=quantity),
                        resolved_on=today,
                    )
                else:
                    # A lens takes no quantity (§4.1): it answers "which names", not "how many",
                    # and it enters no total. A body that named one for a MONITORING view is not
                    # refused — it is simply not a fact a lens can carry.
                    _add_to_monitoring_view(session, portfolio_id, position, added_on=today)
            await session.flush()
    except IntegrityError as exc:
        # The window between the pre-check and the insert. `uq_portfolio_holding_one_capital_
        # portfolio` is the only constraint these writes can breach that a user can act on, and
        # the savepoint above means the refusal leaves the caller's transaction usable rather
        # than poisoned — so this answers with a sentence instead of a 500 and a rolled-back page.
        raise Problem(
            ALLOCATION_REFUSED,
            "One of these holdings was put into another capital portfolio while this one was "
            "being created. A holding belongs to exactly one capital portfolio. Reload your "
            "holdings and try again.",
        ) from exc

    return await portfolio_detail(session, principal, int(portfolio.id))


def _refuse_over_allocation(
    ledger: _Ledger,
    chosen: Sequence[tuple[_Position, Decimal]],
) -> None:
    """Refuse a selection that asks for more shares than the user holds, in words.

    **This replaced `_refuse_double_allocation` on 10 Sep 2026.** That function refused a holding
    that was already in another capital portfolio, because acceptance criterion 2 said a holding
    belongs to exactly one. Maulik removed that rule — filing the same stock into four portfolios
    is the feature — so the refusal it was making is gone, and the only thing left to refuse is
    arithmetic: you cannot file 60 shares of a 50-share position.

    Every conflict is collected rather than the first one raised, because a user who selected
    twelve holdings in §6.7's picker and got told about one of them would fix it, retry, and be
    told about the next. The refusal names the symbol, what was asked for and what is actually
    free, which is what the picker needs to show a number the user can correct to.

    It refuses rather than clamping to the available quantity. Filing 40 when 60 was asked for
    would leave the user believing a portfolio holds half again what it does, and nothing on the
    page would say otherwise.
    """
    conflicts = [
        (position, wanted) for position, wanted in chosen if wanted > position.unallocated_quantity
    ]
    if not conflicts:
        return
    named: list[str] = []
    subject_ids: list[int] = []
    for position, wanted in conflicts:
        symbol = _symbol_of(ledger, position.key.instrument_id)
        free = position.unallocated_quantity
        where = _filed_elsewhere(ledger, position)
        named.append(f"{symbol}: you asked for {wanted:g} and only {free:g} are free{where}")
        subject_ids.append(position.key.instrument_id)
    raise Problem(
        ALLOCATION_REFUSED,
        "A holding cannot be filed into more shares than you own — "
        + "; ".join(named)
        + ". Lower those quantities, or free the shares from the portfolios they are in.",
        conflicts=[
            {
                "instrument_id": position.key.instrument_id,
                "broker_account_id": position.key.broker_account_id,
                "requested": str(wanted),
                "available": str(position.unallocated_quantity),
                "slices": {str(pid): str(qty) for pid, qty in position.capital_slices.items()},
            }
            for position, wanted in conflicts
        ],
        instrument_ids=subject_ids,
    )


def _requested_quantities(
    lines: Sequence[HoldingKeyIn],
) -> dict[HoldingKey, Decimal | None]:
    """Collapse the body's holding lines into one request per holding.

    Two lines for the same holding **add up** rather than the last one winning: a client that
    sends ``{ITC: 20}`` and ``{ITC: 14}`` means 34, and silently picking one would file a number
    the user never asked for. A line with no quantity means "all of it" (see
    `HoldingKeyIn.quantity`), which cannot be added to anything and so wins over any explicit
    amount for the same holding — the wider of the two intents, and the one a picker sends when
    the user ticks the whole row.
    """
    requested: dict[HoldingKey, Decimal | None] = {}
    for line in lines:
        key = HoldingKey(instrument_id=line.instrument_id, broker_account_id=line.broker_account_id)
        if line.quantity is None:
            requested[key] = None
        elif key not in requested:
            requested[key] = line.quantity
        elif requested[key] is not None:
            requested[key] = (requested[key] or ZERO) + line.quantity
    return requested


def _symbol_of(ledger: _Ledger, instrument_id: int) -> str:
    """The ticker a refusal names, falling back to the id when the instrument is not loaded.

    A message that says "instrument 4711" is still actionable; one that raises while building a
    refusal turns a 400 the user could fix into a 500 they cannot.
    """
    instrument = ledger.instruments.get(instrument_id)
    return instrument.symbol if instrument is not None else str(instrument_id)


def _filed_elsewhere(ledger: _Ledger, position: _Position) -> str:
    """ ", already 20 in 'Long term' and 34 in 'Swing'" — or "" when nothing is filed.

    The sentence fragment that turns "only 46 are free" from a bare refusal into something the
    user can act on without opening another page.
    """
    if not position.capital_slices:
        return ""
    parts = []
    for portfolio_id, quantity in position.capital_slices.items():
        portfolio = ledger.portfolios.get(portfolio_id)
        name = portfolio.name if portfolio is not None else str(portfolio_id)
        parts.append(f"{quantity:g} in {name!r}")
    return ", already " + " and ".join(parts)


class AddHoldingsIn(BaseModel):
    """``POST /portfolio/{id}/holdings`` — file more shares into a portfolio that already exists.

    The same holding entries `POST /portfolio` takes, and the same meaning: a quantity narrows the
    request, omitting it means "everything available".
    """

    model_config = ConfigDict(extra="forbid")

    holdings: list[HoldingKeyIn] = Field(min_length=1)


@router.post("/{portfolio_id}/holdings", response_model=PortfolioDetailOut)
async def add_holdings(
    session: SessionDep,
    principal: AuthenticatedDep,
    portfolio_id: Annotated[int, Path(ge=1)],
    body: AddHoldingsIn,
) -> PortfolioDetailOut:
    """Add shares to a portfolio that already exists. **Not an order path.**

    Maulik, 11 Sep 2026: *"the current system does not allow to add stock into existing portfolio,
    it only allows to create a new one"*. He was right, and it made the product unusable after the
    first pass: everything he owned went into one group called "Swing Manual", and the only way to
    build a second was to free the shares by hand first — with no screen that could free them.

    **This route MOVES shares, and that is the difference between it and `POST /portfolio`.**
    Creating a group takes only what is unallocated, because a new portfolio has no claim on
    anything yet. Adding to an existing one is the operation a person reaches for when the shares
    are already somewhere and belong somewhere else, so the cap here is the **whole position**,
    not the free remainder. `_apply_allocation` takes the free shares first and then the smallest
    other slice, so the fewest portfolios are disturbed and Unallocated drains before anything is
    taken out of a group the user built.

    What it refuses is still arithmetic: more shares than exist. And it refuses a **broker pile**
    as the target — those rows are Unallocated by definition (0038), and "add these shares to
    Unallocated" is a *removal*, which is a different verb and deserves its own route rather than
    this one quietly doing two things.

    A MONITORING view takes membership, not quantity (§4.1): a lens answers "which names", never
    "how many", so a quantity sent for one is accepted and ignored rather than refused — the body
    is shared with the create route and a client should not have to know which kind it is talking
    to before it can send a holding.
    """
    user_id = principal.require_user()
    await _owned_portfolio(session, portfolio_id, principal)

    requested = _requested_quantities(body.holdings)
    wanted = sorted(requested, key=lambda key: (key.instrument_id, key.broker_account_id))
    for key in wanted:
        await _owned_broker_account(session, key.broker_account_id, user_id)

    ledger = await _load_ledger(session, user_id)
    if portfolio_id in ledger.pile_ids:
        raise Problem(
            ALLOCATION_REFUSED,
            f"{ledger.portfolios[portfolio_id].name!r} is where unsorted shares already live, so "
            "there is nothing to add to it. Remove them from the portfolio they are in instead.",
        )
    portfolio = ledger.portfolios[portfolio_id]
    today = dt.datetime.now(tz=dt.UTC).date()

    by_key = {position.key: position for position in ledger.positions}
    chosen: list[tuple[_Position, Decimal]] = []
    for key in wanted:
        position = by_key.get(key)
        if position is None:
            raise not_found(
                "holding",
                f"instrument {key.instrument_id} at broker account {key.broker_account_id}",
            )
        asked = requested[key]
        held_here = position.capital_slices.get(portfolio_id, ZERO)
        # `None` means "everything this portfolio does not already hold" — the free shares plus
        # whatever is filed elsewhere. On the create route the same `None` means only the free
        # shares, because a portfolio that does not exist yet cannot be taking from itself.
        chosen.append((position, position.holding.quantity - held_here if asked is None else asked))

    if portfolio.kind is PortfolioKind.CAPITAL:
        _refuse_more_than_is_held(ledger, chosen, portfolio_id)

    async with session.begin_nested():
        for position, quantity in chosen:
            if quantity <= ZERO:
                # Already entirely in this portfolio, or an explicit zero. Not an error: a user
                # ticking a row that is already filed here has asked for a state that is true.
                continue
            if portfolio.kind is PortfolioKind.CAPITAL:
                await _apply_allocation(
                    session,
                    ledger,
                    Allocation(key=position.key, portfolio_id=portfolio_id, quantity=quantity),
                    resolved_on=today,
                )
            elif portfolio_id not in position.monitoring_portfolio_ids:
                _add_to_monitoring_view(session, portfolio_id, position, added_on=today)
        await session.flush()

    return await portfolio_detail(session, principal, portfolio_id)


def _refuse_more_than_is_held(
    ledger: _Ledger, chosen: Sequence[tuple[_Position, Decimal]], portfolio_id: int
) -> None:
    """The only arithmetic left to refuse when shares may be moved: more than you own.

    `_refuse_over_allocation` guards the create route against the free remainder; this one guards
    against the whole position, because moving a holding out of one portfolio and into another is
    the point of the route rather than a thing to prevent. What both refuse is a portfolio ending
    up with shares nobody has.
    """
    over = [
        (position, wanted)
        for position, wanted in chosen
        if wanted > position.holding.quantity - position.capital_slices.get(portfolio_id, ZERO)
    ]
    if not over:
        return
    named = [
        f"{_symbol_of(ledger, position.key.instrument_id)}: asked for {wanted:g}, "
        f"{position.holding.quantity:g} held in total"
        for position, wanted in over
    ]
    raise Problem(
        ALLOCATION_REFUSED,
        "You cannot file more shares than you own — " + "; ".join(named) + ".",
        instrument_ids=[position.key.instrument_id for position, _ in over],
    )


# ---------------------------------------------------------------------------
# GET /portfolio/{id} and /{id}/nav — §7 and §6.3
#
# Declared last so the literal paths above match first. See the module docstring.
# ---------------------------------------------------------------------------


@router.get("/{portfolio_id}", response_model=PortfolioDetailOut)
async def portfolio_detail(
    session: SessionDep,
    principal: AuthenticatedDep,
    portfolio_id: Annotated[int, Path(ge=1)],
) -> PortfolioDetailOut:
    """§7's detail page: summary, holdings with weight and contribution, and the source panel.

    A monitoring view is served here like any other portfolio, with its value, its holdings and
    its own return — and with ``counts_toward_total=False`` and §4.1's note beside it. Refusing to
    show one would be the wrong lesson: a lens is a legitimate thing to look at, it is only an
    illegitimate thing to *add up*.
    """
    user_id = principal.require_user()
    await _owned_portfolio(session, portfolio_id, principal)
    ledger = await _load_ledger(session, user_id)
    portfolio = ledger.portfolios[portfolio_id]
    members = _members_of(ledger, portfolio)
    freeze = freeze_report(ledger.entries, ledger.ledger_positions)
    model_values = await _model_return_values(session, ledger)
    nav_rows = await _nav_rows(session, user_id, portfolio_id)
    points = [_nav_point(row) for row in nav_rows]

    priced_members = [
        position for position in members if position.key.instrument_id not in ledger.prices.unpriced
    ]
    value = money(
        sum(
            (ledger.value_of(position.holding) or ZERO for position in priced_members),
            ZERO,
        )
    )
    cash = (
        portfolio_cash(ledger.flows, portfolio_id)
        if portfolio.kind is PortfolioKind.CAPITAL
        else ZERO
    )
    invested, invested_value, _ = _cost_and_value(ledger, members)
    headline = _headline_for(portfolio, points)

    holdings: list[DetailHoldingOut] = []
    for position in members:
        instrument = ledger.instruments.get(position.key.instrument_id)
        account = ledger.brokers.get(position.key.broker_account_id)
        if instrument is None or account is None:
            continue
        holding_value_ = ledger.value_of(position.holding)
        basis = cost_basis(position.holding)
        latest = ledger.prices.latest.get(position.key.instrument_id)
        previous = ledger.prices.previous.get(position.key.instrument_id)
        holdings.append(
            DetailHoldingOut(
                instrument=_instrument_ref(instrument),
                broker=_broker_ref(account),
                quantity=position.holding.quantity,
                avg_price=position.holding.avg_price,
                price=latest,
                value=holding_value_,
                weight=(
                    None
                    if holding_value_ is None or value == ZERO
                    else (holding_value_ / value).quantize(RETURN_PRECISION)
                ),
                todays_contribution=(
                    None
                    if latest is None or previous is None
                    else money(position.holding.quantity * (latest - previous))
                ),
                total_contribution=(
                    None
                    if basis is None or holding_value_ is None
                    else money(holding_value_ - basis)
                ),
                first_bought_on=position.first_bought_on,
                history_source=position.history_source,
                pending_reconciliation=freeze.is_frozen(position.key),
            )
        )

    row = ledger.rows[portfolio_id]
    index = (
        ledger.benchmarks.get(int(row.benchmark_index_id))
        if row.benchmark_index_id is not None
        else None
    )
    brokers = [
        _broker_ref(ledger.brokers[account_id])
        for account_id in sorted({position.key.broker_account_id for position in members})
        if account_id in ledger.brokers
    ]
    synced_on = max((cash_row.as_of for cash_row in ledger.broker_cash), default=None)
    summary = PortfolioSummaryOut(
        portfolio_id=portfolio_id,
        name=portfolio.name,
        kind=portfolio.kind,
        source=portfolio.source,
        source_badge=_source_badge(portfolio, ledger.publishers.get(portfolio_id)),
        publisher=ledger.publishers.get(portfolio_id),
        started_on=portfolio.started_on,
        value=value,
        cash=cash,
        invested=invested,
        invested_unavailable_reason=(
            None if invested is not None else "No purchase prices on record yet"
        ),
        todays_pnl=_todays_move(ledger, priced_members, label="Change since the previous close"),
        total_pnl=(
            _money_move(
                amount=money(invested_value - invested),
                pct=(
                    None
                    if invested == ZERO
                    else ((invested_value - invested) / invested).quantize(RETURN_PRECISION)
                ),
                label="Total P&L since purchase",
                since=None,
            )
            if invested is not None and invested_value is not None
            else _money_move(
                amount=None,
                pct=None,
                label="Total P&L since purchase",
                since=None,
                unavailable_reason=("Import your CAS to see returns from your purchase dates"),
            )
        ),
        headline_return=headline,
        model_return=_model_for(portfolio, model_values.get(portfolio_id)),
        xirr=_portfolio_xirr_out(ledger, portfolio, money(value + cash)),
        benchmark=await _benchmark(session, index=index, figure=headline, rows=nav_rows),
        counts_toward_total=portfolio.kind is PortfolioKind.CAPITAL,
        excluded_note=None if portfolio.kind is PortfolioKind.CAPITAL else MONITORING_NOTE,
        status=_status_for(
            pending=freeze.is_pending(portfolio_id),
            rebalance_due=portfolio_id in ledger.rebalance_due_portfolio_ids,
        ),
        pending_reconciliation=freeze.is_pending(portfolio_id),
        holdings_synced_on=synced_on,
        prices_as_of=ledger.prices.as_of,
    )
    return PortfolioDetailOut(
        summary=summary,
        holdings=holdings,
        source_panel=_source_panel(ledger, portfolio, brokers),
        brokers=brokers,
    )


def _portfolio_xirr_out(
    ledger: _Ledger, portfolio: LedgerPortfolio, closing_value: Decimal
) -> LabelledRateOut:
    """One portfolio's money-weighted return from §4.4's internal flows, labelled.

    A monitoring view has none and is told so rather than given a zero: a lens owns no cash, so
    there is no contribution for a money-weighted return to weight.
    """
    label = "XIRR since your first cash assignment"
    if portfolio.kind is PortfolioKind.MONITORING:
        return LabelledRateOut(
            label=label,
            value=None,
            unavailable_reason=(
                "A monitoring view holds no cash of its own, so it has no money-weighted return"
            ),
        )
    events = [
        flow
        for flow in ledger.flows
        if flow.portfolio_id == portfolio.portfolio_id and flow.kind.is_xirr_event
    ]
    if not events:
        return LabelledRateOut(
            label=label,
            value=None,
            unavailable_reason="Assign cash to this portfolio to start measuring XIRR",
        )
    rate = portfolio_xirr(
        ledger.flows,
        portfolio.portfolio_id,
        as_of=_valued_on(ledger, events),
        closing_value=closing_value,
    )
    return LabelledRateOut(
        label=label,
        since=min(flow.occurred_on for flow in events),
        value=rate,
        unavailable_reason=(
            None if rate is not None else "Not enough cash-flow history to solve a rate yet"
        ),
    )


def _source_panel(
    ledger: _Ledger, portfolio: LedgerPortfolio, brokers: Sequence[BrokerRefOut]
) -> SourcePanelOut:
    """§7's source panel, in §9's language for anything somebody else published.

    "Subscribed model by {publisher}" — never "managed", never "advisory", never "PMS". Baskfy is
    not SEBI-registered and those words describe a service it does not provide; using them would
    be a regulatory claim, not a UI choice.
    """
    portfolio_id = portfolio.portfolio_id
    basket = ledger.baskets.get(portfolio_id)
    screen = ledger.screens.get(portfolio_id)
    publisher = ledger.publishers.get(portfolio_id)
    if portfolio.source is PortfolioSource.SUBSCRIBED:
        headline = _SUBSCRIBED_BADGE.format(publisher=publisher or _UNKNOWN_PUBLISHER)
    elif portfolio.source is PortfolioSource.MY_SCREEN:
        headline = (
            f"Built from your screen {screen.name!r}"
            if screen is not None
            else "Built from your own screening rules"
        )
    elif portfolio.source is PortfolioSource.MY_STRATEGY:
        headline = "Driven by your saved strategy rules"
    else:
        headline = f"Holdings you grouped on {portfolio.started_on.isoformat()}"
    return SourcePanelOut(
        source=portfolio.source,
        headline=headline,
        publisher=publisher,
        basket_slug=basket.slug if basket is not None else None,
        basket_name=basket.name if basket is not None else None,
        screen_public_id=screen.public_id if screen is not None else None,
        screen_name=screen.name if screen is not None else None,
        brokers=list(brokers),
        grouped_on=(
            portfolio.started_on if portfolio.source is PortfolioSource.HOLDING_GROUP else None
        ),
    )


@router.get("/{portfolio_id}/nav", response_model=NavSeriesOut)
async def portfolio_nav(
    session: SessionDep,
    principal: AuthenticatedDep,
    portfolio_id: Annotated[int, Path(ge=1)],
    window: Annotated[
        NavRange, Query(alias="range", description="§6.3's ranges. End-of-day only in v1.")
    ] = NavRange.Y1,
) -> NavSeriesOut:
    """One portfolio's stored EOD NAV series (§5.1), range-filtered, with §6.3's derived views.

    The series is read, never recomputed. §5.1's whole argument is that a mark is a claim about a
    day — "valued at close of {date}" — and recomputing it from today's allocations would rewrite
    history every time a holding moved between portfolios. A chart that changes shape because a
    user renamed something is not a record.
    """
    user_id = principal.require_user()
    row = await _owned_portfolio(session, portfolio_id, principal)
    ledger = await _load_ledger(session, user_id)
    portfolio = ledger.portfolios[portfolio_id]
    all_rows = await _nav_rows(session, user_id, portfolio_id)
    rows = _windowed(all_rows, window)
    headline = _headline_for(portfolio, [_nav_point(r) for r in all_rows])
    index = (
        ledger.benchmarks.get(int(row.benchmark_index_id))
        if row.benchmark_index_id is not None
        else None
    )
    return _series_out(
        rows,
        _SeriesMeta(
            portfolio_id=portfolio_id,
            window=window,
            label=headline.label,
            since=portfolio.started_on,
            benchmark=await _benchmark(session, index=index, figure=headline, rows=rows),
        ),
    )


__all__ = [
    "AggregatedHoldingOut",
    "HoldingsOut",
    "NavRange",
    "NavSeriesOut",
    "OverviewOut",
    "PortfolioDetailOut",
    "ReconciliationInboxOut",
    "ResolveBody",
    "ResolveOut",
    "portfolio_activity",
    "portfolio_detail",
    "portfolio_holdings",
    "portfolio_nav",
    "portfolio_overview",
    "portfolio_reconciliation",
    "resolve_reconciliation_item",
    "router",
]
