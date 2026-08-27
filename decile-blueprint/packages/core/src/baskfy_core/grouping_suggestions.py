"""Grouping suggestions and the two Phase-3 read-outs — `PORTFOLIO_REDESIGN.md` §6.6, §6.7, §10.

§6.6 is the most emphatic sentence in the whole spec, and it is about this module:

    "First-run experience: connect broker -> everything lands in Unallocated -> the product
    actively helps sort it (suggest groupings by sector, by purchase era, by overlap with a
    subscribed basket). Getting from 40 unallocated holdings to 4 named portfolios IS
    activation. Do not funnel new users to the catalog first."

So the thing being built here is not a nicety bolted onto the ledger. It is the moment the
product becomes the user's. A newly connected broker account produces one undifferentiated pile;
:mod:`baskfy_core.allocation_ledger` can say how much that pile is worth and refuse to
double-count it, but it has nothing to say about what the pile *means*. This module is the part
that reads the pile and proposes the four names.

Alongside it sit the two §10 Phase-3 read-outs that answer questions about a ledger that already
has structure: :func:`monitoring_overlaps` ("which lenses see the same money as which capital
portfolios") and :func:`contribution_breakdown` ("who moved the number today").

**Everything arrives as an argument.** Sectors, purchase dates, prices, the as-of date, the
subscribed baskets' constituents — all of them are parameters (law 1). A suggestion engine that
could fetch a sector map would be a suggestion engine you could not reproduce, and the one
property this module has to have is that the same pile suggests the same four names every time
it is asked. `test_grouping_suggestions.py` scans this source to keep it that way.

WHAT THIS IS NOT
----------------
Not a classifier and not a recommender. Nothing here is learned, weighted or tuned; every
suggestion is a group the input data already contains, and the ranking is a stated rule rather
than a score. That is deliberate: the user is about to give these groups names and then measure
their own money against them, so a suggestion they cannot reconstruct from what they can see is
worse than no suggestion.

Not an allocation either. Nothing here returns an :class:`~baskfy_core.allocation_ledger.
Allocation` or mutates one. A suggestion is an offer; §6.7's flow is where a human accepts it,
picks the kind, names it and confirms. Keeping the two apart is what stops a "helpful" first run
from silently sorting somebody's holdings for them.

WHY A ONE-HOLDING SUGGESTION IS NOISE
-------------------------------------
:data:`MIN_HOLDINGS_PER_SUGGESTION` is 2, and suggestions below it are dropped rather than ranked
last. §6.6 measures activation as *40 unallocated holdings to 4 named portfolios*. A one-holding
suggestion moves that user from 40 to 39 while costing them a naming decision — it is a rename
dressed as a grouping. The cost is asymmetric, too: the user who genuinely wants a portfolio
holding one stock can still build it by hand in §6.7's picker, whereas a list padded with
singletons turns the help into a second sorting problem, which is exactly the problem §6.6 says
the product is supposed to be solving.

HOW SUGGESTIONS ARE RANKED
--------------------------
:func:`rank_suggestions`, in order, on a key that is total so the output is deterministic:

1. **Value covered, descending.** The primary key is money, not holding count, because §6.6's
   own measure of progress is how much stops being Unallocated. The most useful suggestion is
   the one that sorts the most of the user's net worth in one decision.
2. **Holdings covered, descending.** Between two groupings worth the same, the one that empties
   more of the pile is the better single decision.
3. **Basis, by :attr:`SuggestionBasis.confidence_tier`.** Basket overlap first: the user
   subscribed to that model, so they have already said out loud that this grouping matters to
   them. Sector next: an objective property of the instrument. Purchase era last: often
   meaningful, sometimes an accident of when money happened to be free.
4. **Proposed name, alphabetically.** Not meaningful, and that is the point — it is the
   tie-break of last resort, so that two identical-looking suggestions still have a defined
   order rather than whichever one a set iterated out first.

Rule 4 is the one that matters most for trust. Suggestions that reshuffle between two identical
calls would make the first-run screen feel arbitrary, and would make every test of this module a
test of dictionary ordering.

MONITORING VIEWS OVERLAP ON PURPOSE
-----------------------------------
:func:`monitoring_overlaps` reports, it never warns. §4.1 defines a monitoring view as an
overlapping lens that is *supposed* to overlap and is excluded from every total, so an overlap
between a lens and a capital portfolio is the feature working. The report exists because a user
looking at "All defence stocks" reasonably wants to know which of their real portfolios that
money is actually sitting in — that is information, not a defect. Nothing here raises on an
overlap, nothing here is called a conflict, and the sentence every row carries is §4.1's own
label. (§6.4 does list "overlap warnings" as a LATER needs-attention item; that is a different
thing about capital portfolios drifting, and it is not this.)

CONTRIBUTIONS HAVE TO ADD UP
----------------------------
:class:`ContributionReport` refuses to exist unless the per-holding contributions sum exactly to
the total move and the per-portfolio contributions sum to it as well. "Explain today's move"
(§10) is a number a user checks against another number, and a breakdown whose parts miss the
whole by a paisa is worse than no breakdown, because it teaches them not to believe the page.
The invariant is enforced in ``__post_init__`` rather than asserted by a caller, and it holds by
construction: each holding's contribution is quantised to paise first and the total is the sum of
the quantised rows, which is the same discipline :func:`~baskfy_core.allocation_ledger.
portfolio_value` uses so that a headline never disagrees with its own breakdown.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

from baskfy_core.allocation_ledger import (
    UNALLOCATED,
    Allocation,
    Holding,
    HoldingKey,
    Portfolio,
    PortfolioKind,
    holding_value,
    validate_allocations,
)
from baskfy_core.gst import FINANCIAL_YEAR_START_MONTH, financial_year, money

__all__ = [
    "DEFAULT_SUGGESTION_LIMIT",
    "ERA_HORIZON_YEARS",
    "MIN_HOLDINGS_PER_SUGGESTION",
    "MONITORING_OVERLAP_NOTE",
    "SHARE_PRECISION",
    "UNALLOCATED_NAME",
    "ContributionReport",
    "GroupingSuggestion",
    "HoldingContribution",
    "MonitoringOverlap",
    "PortfolioContribution",
    "SubscribedBasket",
    "SuggestionBasis",
    "SuggestionInputs",
    "contribution_breakdown",
    "monitoring_overlaps",
    "purchase_era",
    "rank_suggestions",
    "suggest_by_basket_overlap",
    "suggest_by_purchase_era",
    "suggest_by_sector",
    "suggest_groupings",
]

#: A grouping of one holding is a rename, not a grouping. See the module docstring.
MIN_HOLDINGS_PER_SUGGESTION: Final = 2

#: §6.6's activation target is roughly four named portfolios, so the first-run screen offers a
#: few more than that and no more. A longer list is a second sorting problem, which is the
#: problem the screen exists to remove.
DEFAULT_SUGGESTION_LIMIT: Final = 6

#: Purchase eras older than this many financial years collapse into one "or earlier" bucket. A
#: fifteen-year investor would otherwise be handed fifteen era suggestions, most of them below
#: :data:`MIN_HOLDINGS_PER_SUGGESTION` anyway, and the recent years are the ones a user can still
#: remember a reason for.
ERA_HORIZON_YEARS: Final = 5

#: §4.1's required label, verbatim. Every overlap row carries it, so the report can never be read
#: as an accusation that something is wrong.
MONITORING_OVERLAP_NOTE: Final = (
    "Monitoring view — overlaps with other portfolios, excluded from totals."
)

#: What Unallocated is called in a report. It is not a portfolio (it has no id and cannot be
#: renamed) but it does own money, so it needs a name a row can print.
UNALLOCATED_NAME: Final = "Unallocated"

#: Shares of the day's move are display-only, at four decimals. They are never summed back into
#: a total — the rupee contributions are the ones that add up, and only they.
SHARE_PRECISION: Final = Decimal("0.0001")

#: Coverage of a subscribed basket, also four decimals: 11 of 15 constituents is 0.7333.
COVERAGE_PRECISION: Final = Decimal("0.0001")


class SuggestionBasis(StrEnum):
    """The three groupings §6.6 names, and nothing else.

    The basis is part of every suggestion because the user is entitled to know *why* the product
    thinks these eight stocks belong together before they name the result. "These are all
    Financials" and "you bought these in the same year" are very different claims, and a UI that
    presented both as an anonymous cluster would be asking for trust it has not earned.
    """

    SECTOR = "SECTOR"
    PURCHASE_ERA = "PURCHASE_ERA"
    BASKET_OVERLAP = "BASKET_OVERLAP"

    @property
    def confidence_tier(self) -> int:
        """Rank-key component 3. Lower sorts first; see the module docstring for the argument."""
        return {
            SuggestionBasis.BASKET_OVERLAP: 0,
            SuggestionBasis.SECTOR: 1,
            SuggestionBasis.PURCHASE_ERA: 2,
        }[self]


@dataclass(frozen=True, slots=True)
class SubscribedBasket:
    """A model the user subscribes to, reduced to the only thing overlap needs: its stocks.

    Deliberately not the basket object — no weights, no publisher, no performance. Overlap asks
    one question ("how much of this model is already sitting unsorted in the user's demat") and
    weights cannot change the answer. Keeping the input this thin also keeps §9's wording rules
    easy to hold: the only noun this module has for third-party content is *model*, which is the
    word §9 permits.
    """

    basket_id: int
    name: str
    constituent_instrument_ids: frozenset[int]

    def __post_init__(self) -> None:
        if not self.constituent_instrument_ids:
            raise ValueError(
                f"basket {self.basket_id} has no constituents; an empty model overlaps "
                "everything and nothing, and reporting 100% coverage of it would be a lie"
            )


@dataclass(frozen=True, slots=True)
class GroupingSuggestion:
    """One offer: "these holdings look like a portfolio, here is what you might call it".

    Not an allocation. §6.7 is where a human accepts it, chooses the kind, names it and confirms;
    until then this is a sentence the product is prepared to say.

    ``suggested_kind`` defaults to :attr:`~baskfy_core.allocation_ledger.PortfolioKind.CAPITAL`
    for every basis, and the default is worth defending because §4.1's two examples of a
    monitoring view — "All defence stocks", "Bought in 2026" — are *precisely* a sector lens and
    an era lens. The reason the default still points at a capital portfolio is that §6.6 measures
    this screen by how much money stops being Unallocated, and a monitoring view moves nothing
    out of Unallocated: it is a second way of looking at holdings that are still unsorted. So the
    default serves the flow, and §6.7 still asks the user, with §4.1's explanation in front of
    them. A suggestion never decides the kind on its own.

    ``basket_id``, ``basket_coverage`` and ``missing_instrument_ids`` are set for, and only for,
    a :attr:`SuggestionBasis.BASKET_OVERLAP`. Coverage is what makes that suggestion readable —
    "11 of the 15 stocks in this model are here" is a far stronger reason to group than "these 11
    stocks exist" — and the missing four are what the §6.7 drawer needs in order to say what the
    user would still be short of.
    """

    basis: SuggestionBasis
    proposed_name: str
    keys: tuple[HoldingKey, ...]
    value: Decimal
    rationale: str
    suggested_kind: PortfolioKind = PortfolioKind.CAPITAL
    basket_id: int | None = None
    basket_coverage: Decimal | None = None
    missing_instrument_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if len(self.keys) < MIN_HOLDINGS_PER_SUGGESTION:
            raise ValueError(
                f"{self.proposed_name!r} covers {len(self.keys)} holding(s); a suggestion below "
                f"{MIN_HOLDINGS_PER_SUGGESTION} holdings is a rename, not a grouping, and is "
                "dropped rather than shown"
            )
        if len(set(self.keys)) != len(self.keys):
            raise ValueError(f"{self.proposed_name!r} names the same holding twice")
        if self.value < 0:
            raise ValueError(f"{self.proposed_name!r} has a negative value; got {self.value}")
        is_overlap = self.basis is SuggestionBasis.BASKET_OVERLAP
        has_basket = self.basket_id is not None and self.basket_coverage is not None
        if is_overlap and not has_basket:
            raise ValueError(
                "a basket-overlap suggestion must name its basket and its coverage, or the user "
                "cannot tell how much of the model they actually hold"
            )
        if not is_overlap and (self.basket_id is not None or self.basket_coverage is not None):
            raise ValueError(
                f"a {self.basis} suggestion has no basket; coverage of a model it was not "
                "derived from would be a number with no meaning"
            )

    @property
    def holding_count(self) -> int:
        return len(self.keys)


@dataclass(frozen=True, slots=True)
class SuggestionInputs:
    """Everything :func:`suggest_groupings` is allowed to know, gathered by somebody else.

    One object rather than a long parameter list, for two reasons. It keeps law 1 visible — the
    complete set of facts this module can reach is a value the caller constructed — and it gives
    the one cross-field rule a home: purchase eras are measured against a date, and this module
    cannot read one.

    Every field is optional because the first run is exactly the case where some are missing. A
    broker that publishes no buy dates yields no era suggestions, and the sector and basket
    suggestions still work; that degrades, it does not fail.
    """

    sectors: Mapping[int, str] = field(default_factory=dict)
    first_bought: Mapping[HoldingKey, dt.date] = field(default_factory=dict)
    as_of: dt.date | None = None
    baskets: Sequence[SubscribedBasket] = ()

    def __post_init__(self) -> None:
        if self.first_bought and self.as_of is None:
            raise ValueError(
                "purchase-era buckets are measured against a date, and this package reads no "
                "clock (law 1); pass as_of alongside first_bought"
            )


@dataclass(frozen=True, slots=True)
class MonitoringOverlap:
    """One row of the §10 overlap report: a lens and a capital portfolio seeing the same money.

    ``portfolio_id`` is ``None`` when the shared holdings are Unallocated, which on a first run is
    the common case and is the most useful row on the report — a lens over holdings that are in
    no portfolio at all is a ready-made §6.7 grouping.

    There is no severity field, no flag and no threshold, because there is nothing here to grade.
    §4.1 says a monitoring view overlaps by design and is excluded from every total; this row
    says which portfolio the money is in, and :attr:`note` ends with §4.1's own sentence.
    """

    view_id: int
    view_name: str
    portfolio_id: int | None
    portfolio_name: str
    keys: tuple[HoldingKey, ...]
    value: Decimal

    @property
    def holding_count(self) -> int:
        return len(self.keys)

    @property
    def note(self) -> str:
        """The sentence a row prints. Informative, and phrased so it cannot read as a rebuke."""
        holdings = "holding" if self.holding_count == 1 else "holdings"
        return (
            f"{self.view_name} and {self.portfolio_name} both include {self.holding_count} "
            f"{holdings}. {MONITORING_OVERLAP_NOTE}"
        )


@dataclass(frozen=True, slots=True)
class HoldingContribution:
    """What one holding did to today's number, in rupees, already quantised to paise."""

    key: HoldingKey
    portfolio_id: int | None
    value: Decimal
    contribution: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioContribution:
    """One capital portfolio's share of today's move, or Unallocated's when ``portfolio_id`` is
    ``None``. Monitoring views do not appear: they hold no allocations, so attributing a move to
    one would be attributing the same rupees twice."""

    portfolio_id: int | None
    value: Decimal
    contribution: Decimal


@dataclass(frozen=True, slots=True)
class ContributionReport:
    """A breakdown of today's move that is checked, at construction, to add up.

    Both decompositions are verified against the same total: per holding and per portfolio. A
    report that could be built with parts that miss the whole would be a report a user learns to
    distrust after the first time they add the column up themselves, and §10's "explain today's
    move" is worth nothing without that trust.

    The check can only be satisfied exactly because the arithmetic is Decimal end to end and each
    row is quantised before anything is summed. Binary fractions would drift here on ordinary
    Indian prices, and the drift would be small enough to survive review and large enough to be
    visible in a paise column.
    """

    total_move: Decimal
    holdings: tuple[HoldingContribution, ...]
    portfolios: tuple[PortfolioContribution, ...]

    def __post_init__(self) -> None:
        by_holding = sum((row.contribution for row in self.holdings), Decimal("0"))
        if by_holding != self.total_move:
            raise ValueError(
                f"holding contributions sum to {by_holding} but the total move is "
                f"{self.total_move}; a breakdown that misses its own total is worse than none"
            )
        by_portfolio = sum((row.contribution for row in self.portfolios), Decimal("0"))
        if by_portfolio != self.total_move:
            raise ValueError(
                f"portfolio contributions sum to {by_portfolio} but the total move is "
                f"{self.total_move}; every holding belongs to exactly one capital portfolio or "
                "to Unallocated, so the two decompositions must agree"
            )

    def share_of_move(self, contribution: Decimal) -> Decimal | None:
        """A row's share of the day, for display only, or ``None`` on a flat day.

        Never summed back into anything. Shares are ratios of rounded rupees and a set of them
        need not total one; the rupee column is the one that adds up, and it is the one
        ``__post_init__`` guards. A move that nets to zero has no shares at all rather than a
        division nobody can interpret, and on a negative day a positive holding's share is
        negative — which is arithmetically right and is why the caller, not this method, decides
        how to word it.
        """
        if self.total_move == 0:
            return None
        return (contribution / self.total_move).quantize(SHARE_PRECISION, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Suggestions — §6.6
# ---------------------------------------------------------------------------


def _sorted_keys(keys: Sequence[HoldingKey]) -> tuple[HoldingKey, ...]:
    """A holding order that does not depend on how the caller happened to list them.

    Determinism has to reach inside a suggestion as well as between suggestions: two runs that
    proposed the same group with its members in a different order would produce objects that
    compare unequal, and every equality test in this module's suite would be a test of input
    ordering.
    """
    return tuple(sorted(keys, key=lambda key: (key.instrument_id, key.broker_account_id)))


def _group_value(keys: Sequence[HoldingKey], values: Mapping[HoldingKey, Decimal]) -> Decimal:
    """Sum of already-quantised holding values, quantised again for the headline.

    Same discipline as the ledger's :func:`~baskfy_core.allocation_ledger.portfolio_value`: rows
    first, then the total, so the number on the suggestion equals the sum of the rows the §6.7
    picker will show underneath it.
    """
    return money(sum((values[key] for key in keys), Decimal("0")))


def _holding_values(
    holdings: Sequence[Holding], prices: Mapping[int, Decimal]
) -> dict[HoldingKey, Decimal]:
    """Value every holding once. A missing price raises, out of
    :func:`~baskfy_core.allocation_ledger.holding_value` — a suggestion whose headline value
    quietly omitted an instrument would rank below where it belongs and mislead the very
    decision it exists to inform."""
    return {holding.key: holding_value(holding, prices) for holding in holdings}


def suggest_by_sector(
    unallocated: Sequence[Holding],
    prices: Mapping[int, Decimal],
    sectors: Mapping[int, str],
) -> list[GroupingSuggestion]:
    """Group unsorted holdings by the sector each instrument belongs to (§6.6).

    Sectors arrive as ``{instrument_id -> sector}`` and are never looked up here. A holding whose
    instrument has no entry is skipped rather than filed under "Unknown": an "Unknown" bucket is
    a grouping with no shared meaning, and offering the user a portfolio named after the gap in
    our own data is worse than offering nothing.

    The same instrument held at two brokers contributes both holdings, because both are separate
    positions that can be allocated separately (§6.7's ``HDFC Bank — 320 (Zerodha 200 · Upstox
    120)`` displays as one row but allocates as two).
    """
    values = _holding_values(unallocated, prices)
    grouped: dict[str, list[HoldingKey]] = {}
    for holding in unallocated:
        sector = sectors.get(holding.key.instrument_id)
        if sector is None:
            continue
        grouped.setdefault(sector, []).append(holding.key)

    suggestions: list[GroupingSuggestion] = []
    for sector, keys in grouped.items():
        if len(keys) < MIN_HOLDINGS_PER_SUGGESTION:
            continue
        ordered = _sorted_keys(keys)
        suggestions.append(
            GroupingSuggestion(
                basis=SuggestionBasis.SECTOR,
                proposed_name=sector,
                keys=ordered,
                value=_group_value(ordered, values),
                rationale=f"These {len(ordered)} holdings are all {sector}.",
            )
        )
    return rank_suggestions(suggestions)


def purchase_era(bought_on: dt.date, as_of: dt.date) -> str:
    """The era label a purchase date falls in. Indian financial years, and here is why.

    Three bucketings were available and only one of them is stable:

    * **Calendar year.** §4.1's own example of a lens is literally "Bought in 2026", so this is
      the user's spoken vocabulary — but it is not the vocabulary of the documents they will
      reconcile against.
    * **A rolling window** ("bought in the last twelve months", "one to three years ago"). This
      matches how holding periods are actually reasoned about, and it is disqualified by the
      thing that makes it feel natural: the buckets move. A portfolio the user built in March
      from the "last twelve months" suggestion contains holdings that are no longer in it by
      June, so the group they named stops meaning what it meant when they named it. Groupings
      the user is about to attach money and a return series to must not drift under them.
    * **Financial year**, which this returns. Apr-to-Mar is the boundary the user's CAS, their
      broker's P&L statement and their tax paperwork already use, so the bucket matches a
      document they can check — and unlike a rolling window, a holding never migrates out of the
      era it was bought in.

    ``as_of`` is used only for the horizon: eras more than :data:`ERA_HORIZON_YEARS` financial
    years back collapse into a single "or earlier" bucket, so a long-time investor is offered a
    handful of eras rather than one per year of their investing life. It is a parameter and not a
    clock reading, which is the whole of law 1 in one argument.
    """
    if bought_on > as_of:
        raise ValueError(
            f"a purchase dated {bought_on} is after the as-of date {as_of}; a holding cannot "
            "have been bought in the future, so this is a caller error rather than an era"
        )
    start = _financial_year_start(bought_on)
    cutoff = _financial_year_start(as_of) - ERA_HORIZON_YEARS
    if start <= cutoff:
        return f"Bought in FY {financial_year(cutoff, FINANCIAL_YEAR_START_MONTH)} or earlier"
    return f"Bought in FY {financial_year(start, FINANCIAL_YEAR_START_MONTH)}"


def _financial_year_start(day: dt.date) -> int:
    """The calendar year an Indian financial year begins in: 12 Feb 2026 belongs to FY 2025-26."""
    return day.year if day.month >= FINANCIAL_YEAR_START_MONTH else day.year - 1


def suggest_by_purchase_era(
    unallocated: Sequence[Holding],
    prices: Mapping[int, Decimal],
    first_bought: Mapping[HoldingKey, dt.date],
    *,
    as_of: dt.date,
) -> list[GroupingSuggestion]:
    """Group unsorted holdings by when they were first bought (§6.6), in financial years.

    ``first_bought`` is keyed by holding, not by instrument, because the same stock bought at two
    brokers two years apart is two purchases and belongs in two eras — and because before a CAS
    import (§5.3) many holdings have no known buy date at all. Those are skipped: a holding with
    no date is not "bought a long time ago", it is a holding whose history the product has not
    been told, and §5.2 is emphatic that the two are never conflated.
    """
    values = _holding_values(unallocated, prices)
    grouped: dict[str, list[HoldingKey]] = {}
    for holding in unallocated:
        bought_on = first_bought.get(holding.key)
        if bought_on is None:
            continue
        grouped.setdefault(purchase_era(bought_on, as_of), []).append(holding.key)

    suggestions: list[GroupingSuggestion] = []
    for era, keys in grouped.items():
        if len(keys) < MIN_HOLDINGS_PER_SUGGESTION:
            continue
        ordered = _sorted_keys(keys)
        suggestions.append(
            GroupingSuggestion(
                basis=SuggestionBasis.PURCHASE_ERA,
                proposed_name=era,
                keys=ordered,
                value=_group_value(ordered, values),
                rationale=f"You first bought these {len(ordered)} holdings in the same year.",
            )
        )
    return rank_suggestions(suggestions)


def suggest_by_basket_overlap(
    unallocated: Sequence[Holding],
    prices: Mapping[int, Decimal],
    basket: SubscribedBasket,
) -> GroupingSuggestion | None:
    """How much of a subscribed model is already sitting unsorted, as one suggestion or none.

    This is the strongest of the three bases and the reason is not statistical: the user chose to
    subscribe to this model, so unlike a sector or a date, the grouping is one they have already
    told the product they care about.

    Coverage is *constituents matched over constituents in the model*, not over holdings. Eleven
    holdings that cover eleven of fifteen constituents is 0.7333 even if two of them are the same
    stock at two brokers, because the question the number answers is "how much of the model do I
    hold", and a second broker account does not make a user hold more of a model.

    Returns ``None`` rather than an empty suggestion when fewer than
    :data:`MIN_HOLDINGS_PER_SUGGESTION` holdings match — including the case where nothing
    matches at all, which on a first run is extremely common and must not produce a row saying
    "0% of this model".
    """
    values = _holding_values(unallocated, prices)
    matched = [
        h.key for h in unallocated if h.key.instrument_id in basket.constituent_instrument_ids
    ]
    if len(matched) < MIN_HOLDINGS_PER_SUGGESTION:
        return None

    held_instruments = {key.instrument_id for key in matched}
    coverage = (
        Decimal(len(held_instruments)) / Decimal(len(basket.constituent_instrument_ids))
    ).quantize(COVERAGE_PRECISION, rounding=ROUND_HALF_UP)
    missing = tuple(sorted(basket.constituent_instrument_ids - held_instruments))
    ordered = _sorted_keys(matched)
    return GroupingSuggestion(
        basis=SuggestionBasis.BASKET_OVERLAP,
        proposed_name=basket.name,
        keys=ordered,
        value=_group_value(ordered, values),
        rationale=(
            f"You subscribe to {basket.name}, and you already hold "
            f"{len(held_instruments)} of its {len(basket.constituent_instrument_ids)} stocks."
        ),
        basket_id=basket.basket_id,
        basket_coverage=coverage,
        missing_instrument_ids=missing,
    )


def rank_suggestions(suggestions: Sequence[GroupingSuggestion]) -> list[GroupingSuggestion]:
    """Order suggestions most-useful-first, on a key with no ties left in it.

    The four components and the argument for each are in the module docstring. The property this
    function has to have is that the key is *total*: value, holding count and basis can all tie,
    and the proposed name is the backstop, so two calls on the same suggestions always produce
    the same list. Two suggestions identical down to the name are the same suggestion.
    """
    return sorted(suggestions, key=_rank_key)


def _rank_key(suggestion: GroupingSuggestion) -> tuple[Decimal, int, int, str]:
    return (
        -suggestion.value,
        -suggestion.holding_count,
        suggestion.basis.confidence_tier,
        suggestion.proposed_name,
    )


def suggest_groupings(
    unallocated: Sequence[Holding],
    prices: Mapping[int, Decimal],
    inputs: SuggestionInputs,
    *,
    limit: int = DEFAULT_SUGGESTION_LIMIT,
) -> list[GroupingSuggestion]:
    """§6.6's first-run helper: the pile in, a short ranked list of named groups out.

    All three bases are computed and then ranked together, so a sector grouping worth more than a
    basket overlap outranks it — the tiering between bases only decides ties, because the primary
    question the screen answers is "what is the biggest single decision available to me now".

    Suggestions overlap freely and deliberately. The same holding will usually appear in a sector
    suggestion, an era suggestion and possibly a basket overlap, and de-duplicating them here
    would be this module choosing the user's portfolios for them. §4.2's one-capital-portfolio
    rule binds allocations, and a suggestion is not an allocation — it becomes one only when a
    human accepts it in §6.7, at which point the ledger enforces the rule.

    An empty pile yields an empty list. Nothing is invented for a user with nothing to sort, and
    §6.6's screen is only shown "when anything is unallocated" in the first place.
    """
    if limit < 1:
        raise ValueError(f"a suggestion list of {limit} entries would help nobody")

    suggestions: list[GroupingSuggestion] = []
    if inputs.sectors:
        suggestions.extend(suggest_by_sector(unallocated, prices, inputs.sectors))
    if inputs.first_bought and inputs.as_of is not None:
        suggestions.extend(
            suggest_by_purchase_era(unallocated, prices, inputs.first_bought, as_of=inputs.as_of)
        )
    for basket in inputs.baskets:
        overlap = suggest_by_basket_overlap(unallocated, prices, basket)
        if overlap is not None:
            suggestions.append(overlap)
    return rank_suggestions(suggestions)[:limit]


# ---------------------------------------------------------------------------
# Overlap detection — §10 Phase 3, read through §4.1
# ---------------------------------------------------------------------------


def _capital_index(
    allocations: Sequence[Allocation], portfolios: Mapping[int, Portfolio]
) -> dict[HoldingKey, int | None]:
    """``{holding -> capital portfolio}``, built only after the ledger has approved the set.

    :func:`~baskfy_core.allocation_ledger.validate_allocations` is the public gate that catches a
    holding allocated twice, a holding allocated to a monitoring view and a holding allocated to
    a portfolio that does not exist. Calling it first and then indexing locally means this module
    can never report an overlap or a contribution derived from an allocation set the consolidated
    total would refuse — the parts and the whole are never allowed to disagree about whether the
    data is even legal.
    """
    validate_allocations(allocations, portfolios)
    return {
        allocation.key: allocation.portfolio_id
        for allocation in allocations
        if allocation.portfolio_id is not UNALLOCATED
    }


def monitoring_overlaps(
    holdings: Sequence[Holding],
    allocations: Sequence[Allocation],
    portfolios: Mapping[int, Portfolio],
    memberships: Mapping[int, Sequence[HoldingKey]],
    prices: Mapping[int, Decimal],
) -> list[MonitoringOverlap]:
    """Which lens sees the same money as which capital portfolio, and how much of it (§10).

    ``memberships`` is ``{monitoring view id -> holdings in that lens}`` and is a separate input
    from ``allocations`` on purpose: §4.1 says membership of a lens is not an allocation, which
    is the whole reason lenses may overlap, and the ledger's :class:`~baskfy_core.
    allocation_ledger.Allocation` therefore refuses to carry one. A capital portfolio appearing
    in ``memberships`` is a caller error and raises — its membership is already stated by the
    allocations, and accepting it here would open a second, unvalidated way to say where a
    holding sits.

    **This is a report and not a check.** No result of this function means anything is wrong. A
    monitoring view that overlaps three capital portfolios is a monitoring view doing its job;
    the user asked to see all their defence stocks and those stocks live somewhere. What the
    report adds is the *somewhere*. Every row ends with §4.1's own sentence.

    A holding in a lens that is in no capital portfolio is reported against Unallocated
    (``portfolio_id`` is ``None``), which on a first run is both the commonest row and the most
    actionable one: a lens over unsorted holdings is a §6.7 grouping waiting to be accepted.
    """
    index = _capital_index(allocations, portfolios)
    known = {holding.key: holding for holding in holdings}

    rows: list[MonitoringOverlap] = []
    for view_id, keys in memberships.items():
        view = portfolios.get(view_id)
        if view is None:
            raise ValueError(f"membership names portfolio {view_id}, which does not exist")
        if view.kind is not PortfolioKind.MONITORING:
            raise ValueError(
                f"{view.name!r} is a capital portfolio, and a capital portfolio's membership is "
                "its allocations (spec section 4.1); passing it as a monitoring view would give "
                "the same holding two places to be recorded"
            )
        by_portfolio: dict[int | None, list[HoldingKey]] = {}
        for key in keys:
            if key not in known:
                raise KeyError(
                    f"{view.name!r} includes {key}, which is not in the holdings supplied; a "
                    "lens over a position we cannot value would report an overlap of no size"
                )
            by_portfolio.setdefault(index.get(key, UNALLOCATED), []).append(key)

        for portfolio_id, shared in by_portfolio.items():
            ordered = _sorted_keys(shared)
            value = money(sum((holding_value(known[key], prices) for key in ordered), Decimal("0")))
            rows.append(
                MonitoringOverlap(
                    view_id=view_id,
                    view_name=view.name,
                    portfolio_id=portfolio_id,
                    portfolio_name=(
                        UNALLOCATED_NAME if portfolio_id is None else portfolios[portfolio_id].name
                    ),
                    keys=ordered,
                    value=value,
                )
            )
    return sorted(rows, key=_overlap_key)


def _overlap_key(row: MonitoringOverlap) -> tuple[Decimal, int, int, int]:
    """Biggest overlap first; then view, then portfolio, with Unallocated last within a view.

    A view and a portfolio identify a row uniquely, so the last two components make the order
    total and the output reproducible.
    """
    return (
        -row.value,
        row.view_id,
        1 if row.portfolio_id is None else 0,
        row.portfolio_id if row.portfolio_id is not None else 0,
    )


# ---------------------------------------------------------------------------
# Contribution analysis — §10 Phase 3, "explain today's move"
# ---------------------------------------------------------------------------


def contribution_breakdown(
    holdings: Sequence[Holding],
    allocations: Sequence[Allocation],
    portfolios: Mapping[int, Portfolio],
    values: Mapping[HoldingKey, Decimal],
    day_changes: Mapping[HoldingKey, Decimal],
) -> ContributionReport:
    """Who moved today's number, per holding and per capital portfolio (§6.5, §7, §10).

    ``day_changes`` is the rupee change of each *holding* — quantity times the change in price,
    computed by whoever owns the EOD marks (§5.1). It arrives already computed for the same
    reason prices do: this package holds no marks and reads no clock, and a "day" is a pair of
    valuation dates somebody else chose.

    Both maps must cover every holding. A missing entry raises rather than defaulting to zero,
    which is :func:`~baskfy_core.allocation_ledger.holding_value`'s rule and for its reason: a
    zero for an unknown change produces a total that is wrong and still balances, and balancing
    is precisely the evidence a reader would use to conclude it is right.

    The total move is the sum of the quantised per-holding contributions — not a separately
    computed figure that the parts are then checked against. That is what makes
    :class:`ContributionReport`'s invariant satisfiable exactly rather than nearly, and it is why
    the second decomposition, per portfolio, is a real check: it is summed over a different
    grouping of the same rows, so a bug in the allocation index shows up as a refusal to build
    the report instead of as a plausible column.

    Monitoring views are absent by construction. They hold no allocations, so their holdings are
    already counted under whichever capital portfolio or Unallocated actually owns them, and
    giving a lens its own contribution row would attribute the same rupees twice — §4.1's
    exclusion from totals, applied to a total made of moves.
    """
    index = _capital_index(allocations, portfolios)

    rows: list[HoldingContribution] = []
    for holding in holdings:
        if holding.key not in values:
            raise KeyError(f"no value supplied for {holding.key}")
        if holding.key not in day_changes:
            raise KeyError(
                f"no day change supplied for {holding.key}; treating it as flat would understate "
                "the move while leaving the breakdown looking complete"
            )
        rows.append(
            HoldingContribution(
                key=holding.key,
                portfolio_id=index.get(holding.key, UNALLOCATED),
                value=money(values[holding.key]),
                contribution=money(day_changes[holding.key]),
            )
        )

    total_move = sum((row.contribution for row in rows), Decimal("0"))

    per_portfolio: dict[int | None, tuple[Decimal, Decimal]] = {
        UNALLOCATED: (
            Decimal("0"),
            Decimal("0"),
        )
    }
    for portfolio in portfolios.values():
        if portfolio.kind is PortfolioKind.CAPITAL:
            per_portfolio[portfolio.portfolio_id] = (Decimal("0"), Decimal("0"))
    for row in rows:
        value, contribution = per_portfolio.get(row.portfolio_id, (Decimal("0"), Decimal("0")))
        per_portfolio[row.portfolio_id] = (value + row.value, contribution + row.contribution)

    portfolio_rows = [
        PortfolioContribution(portfolio_id=portfolio_id, value=value, contribution=contribution)
        for portfolio_id, (value, contribution) in per_portfolio.items()
    ]
    return ContributionReport(
        total_move=total_move,
        holdings=tuple(sorted(rows, key=_holding_contribution_key)),
        portfolios=tuple(sorted(portfolio_rows, key=_portfolio_contribution_key)),
    )


def _holding_contribution_key(row: HoldingContribution) -> tuple[Decimal, int, int]:
    """Biggest mover first; the holding key makes the order total."""
    return (-row.contribution, row.key.instrument_id, row.key.broker_account_id)


def _portfolio_contribution_key(row: PortfolioContribution) -> tuple[Decimal, int, int]:
    """Biggest mover first, Unallocated last among equals."""
    return (
        -row.contribution,
        1 if row.portfolio_id is None else 0,
        row.portfolio_id if row.portfolio_id is not None else 0,
    )
