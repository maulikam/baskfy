"""Trending — the ranked lists the home surface shows (SC9).

Pure functions: candidate rows in, ranked lists out. No database, no network, no disk, no
clock (CLAUDE.md house rule 1 / the first of the two laws). ``as_of`` is a parameter, never
``date.today()``.

What this module is careful about
---------------------------------
**A ranking that cannot be computed is withheld, not faked.** ``docs/smallcase/06`` names the
SC9 acceptance criterion in one line — "computable rankings only … each list labeled with what
it actually ranks; no fake 'most invested'". A catalog with two baskets has no "top three", and
a platform with one investor has no "most invested in": publishing either would be a number the
product cannot stand behind. So every list carries a :class:`WithheldReason` slot, and
:func:`rank_lists` fills it rather than emitting a thin list. With today's single-tenant
database every population-based list correctly withholds, and the same code ranks properly
against a populated fixture — which is what the tests assert.

**Two different floors, because they protect two different things.**
:data:`MIN_ENTRIES` is about meaning: a "ranked list" of one or two rows is a sentence, not a
ranking, and the rank numbers imply a competition that did not happen.
:data:`MIN_POPULATION` is about people: a popularity signal computed over four users both means
nothing statistically *and* leaks how those four behaved. Non-population lists (returns, cost,
recency) are properties of the basket and need only the first floor.

**Every list states what it actually ranks.** :attr:`TrendingListDef.ranks_by` is not a
marketing subtitle; it is the sentence the UI is required to render next to the title, so that
"Movers this month" can never be read as "the best baskets". The observed product's home page
shows nine ranked lists with no such line, and the difference is the whole point.

**Absent is excluded, not last.** A basket with no ``ret_1y`` is younger than the window; it has
not lost the race, it did not enter it. Sorting NULLs to the bottom would publish a rank for a
number that does not exist. Counts behave the same way: a zero-watcher basket is not the ninth
most watched, it is not watched.

**Rounded at write time (house rule 8).** ``metric_display`` is produced here, once, so the API,
the card and any CSV export cannot disagree about what "12.3%" was.

Return convention — what these numbers do NOT include
-----------------------------------------------------
The return-ranked lists rank **price returns**, straight from ``cb_metrics``, which is computed
from ``ohlcv_daily.close``: split- and bonus-adjusted, dividend-free
(``docs/DECISIONS-MERGE.md`` M39.3). :data:`RETURN_RANKED_KEYS` is the machine-readable form of
that caveat, so the surface rendering these lists can carry the sentence CLAUDE.md requires of
"any new surface that shows a return".
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

__all__ = [
    "LIST_LENGTH",
    "MIN_ENTRIES",
    "MIN_POPULATION",
    "RETURN_RANKED_KEYS",
    "TRENDING_LISTS",
    "TrendingCandidate",
    "TrendingEntry",
    "TrendingList",
    "TrendingListDef",
    "TrendingListKey",
    "TrendingPopulation",
    "WithheldReason",
    "definition_for",
    "rank_lists",
]

TrendingListKey = Literal[
    "TOP_1M",
    "TOP_1Y",
    "TOP_CAGR_5Y",
    "BUDGET_FRIENDLY",
    "RECENTLY_LAUNCHED",
    "RECENTLY_REBALANCED",
    "MOST_WATCHED",
    "MOST_INVESTED",
    "MOST_INFLOWS",
]

#: Why a list refused to publish itself. Machine-readable so the UI can choose its own words,
#: and so a caller can tell "nothing to rank" apart from "not enough people to rank over".
WithheldReason = Literal["TOO_FEW_BASKETS", "TOO_FEW_PEOPLE"]

MetricKind = Literal["PCT", "MONEY", "DATE", "COUNT"]

#: A ranked list of one or two rows is not a ranking — the rank numbers imply a competition
#: that did not happen. Three is the smallest number of rows for which "1st" means anything.
MIN_ENTRIES: Final = 3

#: Distinct people a popularity signal must stand on before it may be published. Below this a
#: "most watched" list is both meaningless and a disclosure of how a handful of named people
#: behaved. Nothing about single-tenant mode is special here; the floor is the same rule that
#: will still apply at ten thousand users.
MIN_POPULATION: Final = 5

#: How many rows one home-page list shows. A module, not a leaderboard.
LIST_LENGTH: Final = 5

#: Percent metrics are stored and displayed at 2dp (``cb_metrics`` PCT_2DP).
_PCT_QUANTUM: Final = Decimal("0.01")

#: Money on catalog surfaces is whole rupees, as ``curated_metrics.min_amount`` already rounds it.
_MONEY_QUANTUM: Final = Decimal("1")


@dataclass(frozen=True, slots=True)
class TrendingListDef:
    """One ranked list, defined once.

    ``ranks_by`` is a contract with the reader, not decoration: the surface renders it beside
    the title so a list can never be mistaken for an endorsement.
    """

    key: TrendingListKey
    title: str
    ranks_by: str
    metric_label: str
    metric_kind: MetricKind
    #: Attribute of :class:`TrendingCandidate` this list ranks on.
    field: str
    direction: Literal["asc", "desc"]
    #: True when the metric measures what people did, not what the basket is. Those lists carry
    #: the :data:`MIN_POPULATION` floor as well, and name the population they were computed over.
    population_based: bool
    #: Attribute of :class:`TrendingPopulation` holding that count. Empty for the rest.
    population_field: str = ""


TRENDING_LISTS: Final[tuple[TrendingListDef, ...]] = (
    TrendingListDef(
        key="TOP_1M",
        title="Moved most this month",
        ranks_by="Ranked by 1-month price return, highest first.",
        metric_label="1M return",
        metric_kind="PCT",
        field="ret_1m",
        direction="desc",
        population_based=False,
    ),
    TrendingListDef(
        key="TOP_1Y",
        title="Moved most this year",
        ranks_by="Ranked by 1-year price return, highest first.",
        metric_label="1Y return",
        metric_kind="PCT",
        field="ret_1y",
        direction="desc",
        population_based=False,
    ),
    TrendingListDef(
        key="TOP_CAGR_5Y",
        title="Longest run behind them",
        ranks_by="Ranked by 5-year CAGR, highest first. Only baskets with five years of history.",
        metric_label="5Y CAGR",
        metric_kind="PCT",
        field="cagr_5y",
        direction="desc",
        population_based=False,
    ),
    TrendingListDef(
        key="BUDGET_FRIENDLY",
        title="Cheapest to start",
        ranks_by="Ranked by the smallest amount that buys one whole share of every constituent.",
        metric_label="Min. amount",
        metric_kind="MONEY",
        field="min_amount",
        direction="asc",
        population_based=False,
    ),
    TrendingListDef(
        key="RECENTLY_LAUNCHED",
        title="New here",
        ranks_by="Ranked by launch date, newest first. New is not the same as proven.",
        metric_label="Launched",
        metric_kind="DATE",
        field="launched_at",
        direction="desc",
        population_based=False,
    ),
    TrendingListDef(
        key="RECENTLY_REBALANCED",
        title="Just reviewed",
        ranks_by="Ranked by the effective date of the newest published version, newest first.",
        metric_label="Last review",
        metric_kind="DATE",
        field="last_rebalanced_on",
        direction="desc",
        population_based=False,
    ),
    TrendingListDef(
        key="MOST_WATCHED",
        title="Most watchlisted",
        ranks_by="Ranked by how many people have this on a watchlist.",
        metric_label="Watchers",
        metric_kind="COUNT",
        field="watchers",
        direction="desc",
        population_based=True,
        population_field="watchers",
    ),
    TrendingListDef(
        key="MOST_INVESTED",
        title="Most invested in",
        ranks_by="Ranked by how many people currently hold it.",
        metric_label="Investors",
        metric_kind="COUNT",
        field="investors",
        direction="desc",
        population_based=True,
        population_field="investors",
    ),
    TrendingListDef(
        key="MOST_INFLOWS",
        title="Biggest inflows",
        ranks_by="Ranked by rupees committed through recorded buy batches.",
        metric_label="Recorded inflow",
        metric_kind="MONEY",
        field="inflow_amount",
        direction="desc",
        population_based=True,
        population_field="contributors",
    ),
)

#: The lists whose metric is a return, and therefore owe the reader the price-return sentence.
RETURN_RANKED_KEYS: Final[frozenset[str]] = frozenset(
    definition.key for definition in TRENDING_LISTS if definition.metric_kind == "PCT"
)

_BY_KEY: Final[dict[str, TrendingListDef]] = {d.key: d for d in TRENDING_LISTS}


def definition_for(key: str) -> TrendingListDef:
    """The definition behind a list key.

    Raises :class:`KeyError` rather than returning ``None``: a caller naming a list that does
    not exist has a bug, and swallowing it would publish an empty module instead.
    """
    return _BY_KEY[key]


@dataclass(frozen=True, slots=True)
class TrendingCandidate:
    """One basket, with every metric any list might rank it on.

    Every metric is optional and ``None`` means *not known*, which is not the same as zero:
    a basket younger than a year has no ``ret_1y``, and it is excluded from that list rather
    than ranked at the bottom of it.
    """

    slug: str
    name: str
    ret_1m: Decimal | None = None
    ret_1y: Decimal | None = None
    cagr_5y: Decimal | None = None
    min_amount: Decimal | None = None
    launched_at: dt.date | None = None
    last_rebalanced_on: dt.date | None = None
    watchers: int | None = None
    investors: int | None = None
    inflow_amount: Decimal | None = None


@dataclass(frozen=True, slots=True)
class TrendingPopulation:
    """How many distinct people stand behind each popularity signal.

    Counted over the whole platform, not per basket: the question the floor asks is "are there
    enough people here for popularity to mean anything", and one basket watched by four people
    fails it however concentrated those four are.
    """

    watchers: int = 0
    investors: int = 0
    contributors: int = 0


@dataclass(frozen=True, slots=True)
class TrendingEntry:
    """One row of a ranked list, already rounded for display (house rule 8)."""

    rank: int
    slug: str
    name: str
    metric_value: Decimal | None
    metric_date: dt.date | None
    metric_display: str


@dataclass(frozen=True, slots=True)
class TrendingList:
    """One ranked list, or an honest account of why it is not one."""

    key: TrendingListKey
    title: str
    ranks_by: str
    metric_label: str
    metric_kind: MetricKind
    population_based: bool
    #: People the signal was computed over. ``None`` for lists that rank a basket's own
    #: properties, where a population is not a meaningful number rather than a zero one.
    population: int | None
    #: Baskets that had the metric at all, before the length cap.
    eligible: int
    entries: tuple[TrendingEntry, ...]
    withheld_reason: WithheldReason | None
    withheld_note: str | None
    #: True when the ranked metric is a price return, so the surface owes the M39.3 sentence.
    price_return_caveat: bool

    @property
    def published(self) -> bool:
        return self.withheld_reason is None


def _sort_key(value: object) -> Decimal:
    """One comparable scale for the three metric shapes a list can rank on.

    Dates become their ordinal so "newest first" is the same ``desc`` code path as "highest
    first"; there is no second sort implementation to keep in step with the first. Anything
    else is a definition naming a field this module cannot rank, which is a bug worth raising
    rather than a row worth skipping.
    """
    if isinstance(value, dt.date):
        return Decimal(value.toordinal())
    if isinstance(value, bool):  # bool is an int; a flag is not a rankable metric.
        raise TypeError("a boolean is not a rankable metric")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value
    raise TypeError(f"unrankable metric value: {value!r}")


def _eligible(candidate: TrendingCandidate, definition: TrendingListDef) -> object | None:
    """The candidate's value for this list, or ``None`` when it does not belong in it.

    Counts and money are additionally required to be positive: a basket nobody watches is not
    the last of the most-watched, and a zero-rupee inflow is the absence of an inflow.
    """
    value: object = getattr(candidate, definition.field)
    if value is None:
        return None
    if definition.metric_kind in ("COUNT", "MONEY"):
        # A quantity has to be positive in both directions. Descending: a basket nobody watches
        # is not the last of the most-watched. Ascending: "cheapest to start" ranks a cost, and
        # a zero minimum is not a cheap basket — it is a metrics row nobody has computed yet.
        numeric = Decimal(value) if isinstance(value, int) else value
        if not isinstance(numeric, Decimal) or numeric <= 0:
            return None
    return value


def _display(value: object, definition: TrendingListDef) -> str:
    """The rounded string every surface must use — computed once, here (house rule 8)."""
    if definition.metric_kind == "PCT":
        assert isinstance(value, Decimal)
        return f"{value.quantize(_PCT_QUANTUM, rounding=ROUND_HALF_UP)}%"
    if definition.metric_kind == "MONEY":
        assert isinstance(value, Decimal)
        return f"₹{value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP):,}"
    if definition.metric_kind == "DATE":
        assert isinstance(value, dt.date)
        return value.isoformat()
    assert isinstance(value, int)
    return str(value)


def _withheld_note(reason: WithheldReason, definition: TrendingListDef, count: int) -> str:
    """The sentence a person reads instead of a ranking. Names the real number, never hedges."""
    if reason == "TOO_FEW_BASKETS":
        return (
            f"Not enough baskets to rank yet — {count} "
            f"{'has' if count == 1 else 'have'} a {definition.metric_label.lower()}, "
            f"and a ranking needs {MIN_ENTRIES}."
        )
    return (
        f"Not enough people yet — this ranks what investors did, and {count} "
        f"{'person has' if count == 1 else 'people have'} done it. "
        f"It stays hidden until {MIN_POPULATION}."
    )


def _population_for(definition: TrendingListDef, population: TrendingPopulation) -> int | None:
    if not definition.population_based:
        return None
    value = getattr(population, definition.population_field)
    assert isinstance(value, int)
    return value


def _rank_one(
    definition: TrendingListDef,
    candidates: Sequence[TrendingCandidate],
    population: TrendingPopulation,
) -> TrendingList:
    people = _population_for(definition, population)

    scored: list[tuple[TrendingCandidate, object]] = []
    for candidate in candidates:
        value = _eligible(candidate, definition)
        if value is not None:
            scored.append((candidate, value))

    reason: WithheldReason | None = None
    note: str | None = None
    if definition.population_based and people is not None and people < MIN_POPULATION:
        # People first: a list that fails both floors fails this one more informatively, and
        # "there are three of us" is the true reason the ranking does not exist.
        reason, note = "TOO_FEW_PEOPLE", _withheld_note("TOO_FEW_PEOPLE", definition, people)
    elif len(scored) < MIN_ENTRIES:
        reason, note = (
            "TOO_FEW_BASKETS",
            _withheld_note("TOO_FEW_BASKETS", definition, len(scored)),
        )

    entries: tuple[TrendingEntry, ...] = ()
    if reason is None:
        # Ties break on slug, ascending, in both directions — so the same catalog always
        # produces the same page, and a tie never depends on row order out of the database.
        descending = definition.direction == "desc"

        def order(pair: tuple[TrendingCandidate, object]) -> tuple[Decimal, str]:
            scale = _sort_key(pair[1])
            return (-scale if descending else scale, pair[0].slug)

        ordered = sorted(scored, key=order)
        entries = tuple(
            TrendingEntry(
                rank=index + 1,
                slug=candidate.slug,
                name=candidate.name,
                metric_value=value if isinstance(value, Decimal) else None,
                metric_date=value if isinstance(value, dt.date) else None,
                metric_display=_display(value, definition),
            )
            for index, (candidate, value) in enumerate(ordered[:LIST_LENGTH])
        )

    return TrendingList(
        key=definition.key,
        title=definition.title,
        ranks_by=definition.ranks_by,
        metric_label=definition.metric_label,
        metric_kind=definition.metric_kind,
        population_based=definition.population_based,
        population=people,
        eligible=len(scored),
        entries=entries,
        withheld_reason=reason,
        withheld_note=note,
        price_return_caveat=definition.key in RETURN_RANKED_KEYS,
    )


def rank_lists(
    candidates: Iterable[TrendingCandidate],
    *,
    population: TrendingPopulation,
) -> tuple[TrendingList, ...]:
    """Every list in :data:`TRENDING_LISTS`, ranked or withheld, in declaration order.

    Always returns all nine. A withheld list is information — it tells the surface, and the
    reader, that the product knows the ranking exists and knows it cannot yet stand behind it.
    Dropping it silently would be indistinguishable from never having built it.
    """
    rows = tuple(candidates)
    return tuple(_rank_one(definition, rows, population) for definition in TRENDING_LISTS)
