"""The instrument factsheet — docs/01 §5, docs/07 §Instruments (Prompt 10 deliverables 1, 3, 5).

docs/07 fixes the payload: "Factsheet payload mirrors the teardown §5 exactly: `header`,
`key_stats`, `pros`, `cons`, `metric_cards` (value + own-history median), `price_and_mas`,
`returns`, `sharpe_returns`, `volatility`, `rsi`, `market_quality` (incl. `regime` + the two
Wasserstein distances), `corporate_actions`, `index_memberships`."

Three things here are worth reading before changing anything.

**Medians come from the table that holds the metric's history.** docs/01 §5 block 4: "The medians
are the stock's own historical medians, giving instant 'cheap/dear vs its own history' context."
Closing price has a long history in ``ohlcv_daily``; the factor metrics have theirs in
``factor_daily``. Each median is taken over the instrument's own rows in whichever of the two
holds that metric — which is the same claim in both cases, just stored in different places.

**Percentiles are within the instrument's current primary universe.** docs/08 §"Instrument
factsheet" asks for "a small bar behind each cell showing that value's percentile within the
current universe". "Current universe" is not defined there, so it is taken as the *narrowest*
index the instrument belongs to on the as-of date — comparing a microcap against NIFTY TOTAL
MARKET says much less than comparing it against NIFTY MICROCAP 250. The chosen universe is named
in the payload so the tooltip can say what the comparison set is rather than implying one.

**The Wasserstein distances are recomputed, not stored.** ``factor_daily`` keeps the ``regime``
label; docs/05 §15 asks for the two distances too ("Expose the two distances in the API so the
label is explainable rather than magic"), and docs/04's DDL has no column for them. They are
derived from the same ``ohlcv_daily`` history the nightly job used, so they explain the stored
label rather than second-guessing it. See ``docs/10a`` §2.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

import numpy as np
from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from decile_core import pros_cons
from decile_core.models import (
    CorporateAction,
    FactorDaily,
    IndexMemberDaily,
    Instrument,
    OhlcvDaily,
)
from decile_core.regime import RegimeConfig, classify_series
from decile_core.universes import UNIVERSES, Universe

#: docs/01 §5 block 4 — the five metric cards, and where each one's history lives.
#:
#: ``close`` is read from ``ohlcv_daily`` because that is where the price series is: a database
#: with one published factor day still has years of bars, and a "median" over a single row is not a
#: median. The other four exist only on the fact row.
METRIC_CARDS: Final[tuple[tuple[str, str, str], ...]] = (
    # (key, label, source table)
    ("close", "Closing Price", "ohlcv"),
    ("ret_12m", "Rolling 1-Yr Returns (%)", "factor"),
    ("pe", "Price to Earnings", "factor"),
    ("marketcap_cr", "Marketcap", "factor"),
    ("rsi_12m", "1-Year RSI", "factor"),
)

#: docs/01 §5 blocks 6-9, and the window order the reference product prints.
WINDOW_MONTHS: Final[tuple[int, ...]] = (12, 9, 6, 3, 1)
WINDOW_LABELS: Final[tuple[str, ...]] = ("1Y", "9M", "6M", "3M", "1M")

#: docs/01 §5 block 6 adds the two skip-month returns after the five windows.
RETURN_KEYS: Final[tuple[str, ...]] = (
    *(f"ret_{m}m" for m in WINDOW_MONTHS),
    "ret_12m_minus_1m",
    "ret_12m_minus_2m",
)
# ASCII hyphens, not U+2212: these labels are compared in tests and copied into a URL slug for the
# OG image, and a minus sign that looks identical but is not would make both fail confusingly.
RETURN_LABELS: Final[tuple[str, ...]] = (*WINDOW_LABELS, "12M-1M", "12M-2M")

#: docs/05 §15: the regime is classified over the last 63 daily returns, against reference
#: distributions drawn from the instrument's own rolling windows — so it needs far more than 63
#: bars to have anything to compare against.
REGIME_HISTORY_BARS: Final = 400

#: A return series needs at least two closes.
MIN_CLOSES_FOR_RETURNS: Final = 2


#: What a cell can hold. Numbers, mostly — but docs/01 §5 block 2 puts Series and Listed On in
#: the same list, so the type has to admit a string and a date.
CellValue = Decimal | dt.date | int | str | None


@dataclass(frozen=True, slots=True)
class Cell:
    """One number, with the context that makes it mean something."""

    key: str
    label: str
    value: CellValue
    #: 0-1 within the instrument's primary universe on the as-of date. ``None`` when unknowable.
    percentile: float | None = None


@dataclass(frozen=True, slots=True)
class MetricCard:
    key: str
    label: str
    value: Decimal | int | None
    median: Decimal | int | None
    #: How many observations the median was taken over, so the UI never implies more than it has.
    observations: int


@dataclass(frozen=True, slots=True)
class Factsheet:
    symbol: str
    as_of: dt.date
    data_version: int
    header: Mapping[str, object]
    key_stats: Sequence[Cell]
    pros: Sequence[str]
    cons: Sequence[str]
    undecided: Sequence[str]
    metric_cards: Sequence[MetricCard]
    price_and_mas: Sequence[Cell]
    returns: Sequence[Cell]
    sharpe_returns: Sequence[Cell]
    volatility: Sequence[Cell]
    rsi: Sequence[Cell]
    market_quality: Mapping[str, object]
    corporate_actions: Sequence[Mapping[str, object]]
    index_memberships: Sequence[Mapping[str, object]]
    #: The universe every percentile is measured against, named for the tooltip.
    percentile_universe: Mapping[str, object] | None = None
    notes: Sequence[str] = field(default_factory=tuple)


class InstrumentNotFound(LookupError):
    """No instrument with that symbol. The router turns it into a docs/07 `404 not-found`."""


async def load_instrument(session: AsyncSession, symbol: str) -> Instrument:
    found = (
        await session.execute(select(Instrument).where(Instrument.symbol == symbol.upper()))
    ).scalar_one_or_none()
    if found is None:
        raise InstrumentNotFound(symbol)
    return found


async def search_instruments(session: AsyncSession, query: str, limit: int) -> Sequence[Instrument]:
    """docs/07: `GET /instruments?search=…&limit=…` — the ⌘K typeahead.

    Symbol prefix first, then name — someone typing "CUP" wants CUPID before "ACUPID SYSTEMS".
    Delisted instruments are excluded: they are kept for point-in-time correctness (docs/04), not
    because anyone wants to navigate to one from a search box.
    """
    needle = query.strip().upper()
    if not needle:
        return []

    # Rank: exact symbol, then symbol prefix, then anything else. Someone typing "CUP" wants CUPID
    # before "ACUPID SYSTEMS", and a screener's search box is used by people who know the ticker.
    rank = case(
        (Instrument.symbol == needle, 0),
        (Instrument.symbol.ilike(f"{needle}%"), 1),
        else_=2,
    )
    statement: Select[tuple[Instrument, int]] = (
        select(Instrument, rank.label("rank"))
        .where(
            Instrument.is_active.is_(True),
            Instrument.delisted_on.is_(None),
            (Instrument.symbol.ilike(f"%{needle}%")) | (Instrument.name.ilike(f"%{needle}%")),
        )
        .order_by(rank.asc(), Instrument.symbol.asc())
        .limit(limit)
    )
    return [row[0] for row in (await session.execute(statement)).all()]


async def _fact_row(
    session: AsyncSession, instrument_id: int, as_of: dt.date
) -> Mapping[str, object] | None:
    """The instrument's ``factor_daily`` row for ``as_of``, or the newest one before it.

    Snapping backwards matters for a factsheet in a way it does not for a screen: an instrument
    suspended for a week still has a page, and showing it empty because today's row is missing
    would be a worse answer than showing last Friday's and saying so.
    """
    row = (
        await session.execute(
            select(FactorDaily)
            .where(FactorDaily.instrument_id == instrument_id, FactorDaily.date <= as_of)
            .order_by(FactorDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return {column.name: getattr(row, column.name) for column in FactorDaily.__table__.c}


async def _bar_row(session: AsyncSession, instrument_id: int, as_of: dt.date) -> OhlcvDaily | None:
    """The instrument's bar for ``as_of``, or the newest one before it.

    The header shows a price and `factor_daily` stores only the adjusted `close`. The exchange
    print lives on the bar, and CLAUDE.md house rule 6 is explicit that "display uses `close_raw`
    where the user expects a real price" — a factsheet header is exactly that.
    """
    return (
        await session.execute(
            select(OhlcvDaily)
            .where(OhlcvDaily.instrument_id == instrument_id, OhlcvDaily.date <= as_of)
            .order_by(OhlcvDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _memberships(session: AsyncSession, instrument_id: int, as_of: dt.date) -> list[Universe]:
    """docs/01 §5 block 1: "index membership chips (e.g. *Nifty Total Market*, *Nifty Microcap
    250*)" — point-in-time, from ``index_member_daily`` (docs/06 §step 2)."""
    ids = set(
        (
            await session.execute(
                select(IndexMemberDaily.index_id).where(
                    IndexMemberDaily.instrument_id == instrument_id,
                    IndexMemberDaily.date == as_of,
                )
            )
        )
        .scalars()
        .all()
    )
    return [universe for universe in UNIVERSES if universe.index_id in ids]


async def _universe_size(session: AsyncSession, universe: Universe, as_of: dt.date) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(IndexMemberDaily)
                .where(
                    IndexMemberDaily.index_id == universe.index_id,
                    IndexMemberDaily.date == as_of,
                )
            )
        ).scalar_one()
    )


async def primary_universe(
    session: AsyncSession, instrument_id: int, as_of: dt.date
) -> Universe | None:
    """The narrowest index the instrument belongs to — the comparison set for the percentile bars.

    "Narrowest" is measured by membership count on the day rather than by a hard-coded ranking,
    because index sizes move and a list here would quietly go stale. `etf` and `nifty-fno` are
    skipped: they are classifications, not size bands, and being compared against every listed ETF
    tells an equity holder nothing.
    """
    memberships = [
        universe
        for universe in await _memberships(session, instrument_id, as_of)
        if universe.slug not in {"etf", "nifty-fno"}
    ]
    if not memberships:
        return None
    sized = [(await _universe_size(session, universe, as_of), universe) for universe in memberships]
    populated = [(size, universe) for size, universe in sized if size > 0]
    if not populated:
        return None
    return min(populated, key=lambda pair: (pair[0], pair[1].index_id))[1]


async def _percentiles(
    session: AsyncSession,
    universe: Universe | None,
    as_of: dt.date,
    instrument_id: int,
    columns: Sequence[str],
) -> dict[str, float]:
    """Each column's percentile rank for this instrument within ``universe`` on ``as_of``.

    One statement for all of them: `percent_rank()` over the universe's rows, ordered ascending
    with NULLs last, evaluated per column. A NULL value has no percentile — it is not "the worst",
    it is unknown — so those columns are simply absent from the result.
    """
    if universe is None or not columns:
        return {}

    facts = FactorDaily.__table__
    members = (
        select(IndexMemberDaily.instrument_id)
        .where(IndexMemberDaily.index_id == universe.index_id, IndexMemberDaily.date == as_of)
        .cte("universe_members")
    )
    ranked = (
        select(
            facts.c.instrument_id,
            *[
                func.percent_rank()
                .over(order_by=facts.c[column].asc().nullslast())
                .label(f"p_{column}")
                for column in columns
            ],
            *[facts.c[column].label(f"v_{column}") for column in columns],
        )
        .select_from(facts.join(members, facts.c.instrument_id == members.c.instrument_id))
        .where(facts.c.date == as_of)
        .cte("ranked")
    )
    row = (
        (await session.execute(select(ranked).where(ranked.c.instrument_id == instrument_id)))
        .mappings()
        .one_or_none()
    )
    if row is None:
        return {}
    return {
        column: float(row[f"p_{column}"])
        for column in columns
        if row[f"v_{column}"] is not None and row[f"p_{column}"] is not None
    }


async def _median_from_facts(
    session: AsyncSession, instrument_id: int, column: str, as_of: dt.date
) -> tuple[Decimal | None, int]:
    """The median of one ``factor_daily`` column over the instrument's own history.

    ``percentile_cont`` rather than ``percentile_disc``: for an even number of observations the
    continuous median interpolates, which is the median as ordinarily understood. NULLs are
    excluded by the aggregate, and the count returned is the count it actually saw — so a card can
    say "median of 3 observations" instead of implying years of history it does not have.
    """
    facts = FactorDaily.__table__
    statement = select(
        func.percentile_cont(0.5).within_group(facts.c[column].asc()),
        func.count(facts.c[column]),
    ).where(facts.c.instrument_id == instrument_id, facts.c.date <= as_of)
    value, observations = (await session.execute(statement)).one()
    return (None if value is None else Decimal(str(value))), int(observations)


async def _median_close(
    session: AsyncSession, instrument_id: int, as_of: dt.date
) -> tuple[Decimal | None, int]:
    """The closing-price median, from ``ohlcv_daily`` — the table that holds the price series."""
    bars = OhlcvDaily.__table__
    statement = select(
        func.percentile_cont(0.5).within_group(bars.c.close.asc()),
        func.count(bars.c.close),
    ).where(bars.c.instrument_id == instrument_id, bars.c.date <= as_of)
    value, observations = (await session.execute(statement)).one()
    return (None if value is None else Decimal(str(value))), int(observations)


async def _regime(
    session: AsyncSession, instrument_id: int, as_of: dt.date
) -> tuple[str | None, float | None, float | None]:
    """docs/05 §15's label and its two Wasserstein distances, from the instrument's own bars.

    Returns ``(None, None, None)`` when there is not enough history — which is the honest answer,
    and the one ``decile_core.regime`` gives: "an instrument with too little history to build
    references gets NEUTRAL and NULL distances".

    The stored ``factor_daily.regime`` still wins when it is populated (see ``build_factsheet``);
    this is what explains it, and what stands in for it on a database whose factor rows predate
    the regime step.
    """
    closes = (
        (
            await session.execute(
                select(OhlcvDaily.close)
                .where(OhlcvDaily.instrument_id == instrument_id, OhlcvDaily.date <= as_of)
                .order_by(OhlcvDaily.date.desc())
                .limit(REGIME_HISTORY_BARS)
            )
        )
        .scalars()
        .all()
    )
    if len(closes) < MIN_CLOSES_FOR_RETURNS:
        return None, None, None
    prices = np.array([float(close) for close in reversed(closes)], dtype=float)
    returns = np.diff(prices) / prices[:-1]
    regime, to_bull, to_bear = classify_series(returns, RegimeConfig())
    bull = None if np.isnan(to_bull) else float(to_bull)
    bear = None if np.isnan(to_bear) else float(to_bear)
    return regime, bull, bear


def _cells(
    row: Mapping[str, object],
    keys: Sequence[str],
    labels: Sequence[str],
    percentiles: Mapping[str, float],
) -> list[Cell]:
    return [
        Cell(
            key=key,
            label=label,
            value=_numeric(row.get(key)),
            percentile=percentiles.get(key),
        )
        for key, label in zip(keys, labels, strict=True)
    ]


def _numeric(value: object) -> Decimal | int | None:
    """Narrow a fact-row cell. Anything else is not a number and is reported as absent."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (Decimal, int)):
        return value
    return None


async def build_factsheet(
    session: AsyncSession,
    symbol: str,
    as_of: dt.date,
    data_version: int,
) -> Factsheet:
    """docs/01 §5's eleven blocks, in the reference product's order."""
    instrument = await load_instrument(session, symbol)
    row = await _fact_row(session, instrument.id, as_of)
    bar = await _bar_row(session, instrument.id, as_of)
    memberships = await _memberships(session, instrument.id, as_of)
    universe = await primary_universe(session, instrument.id, as_of)

    notes: list[str] = []
    if row is None:
        # No fact row at all: a listing with no published factors yet. Every block renders empty
        # rather than the page 404-ing — the instrument exists, we just have nothing to say.
        notes.append("No factor row has been published for this instrument yet.")
        row = {}
    elif row.get("date") != as_of:
        notes.append(
            f"No row for {as_of.isoformat()}; showing {row.get('date')!s}, the most recent one."
        )

    percentile_columns = [
        *RETURN_KEYS,
        *(f"sharpe_{m}m" for m in WINDOW_MONTHS),
        *(f"vol_{m}m" for m in WINDOW_MONTHS),
        *(f"rsi_{m}m" for m in WINDOW_MONTHS),
    ]
    percentiles = await _percentiles(session, universe, as_of, instrument.id, percentile_columns)

    pros, cons = pros_cons.evaluate(row)
    undecided = pros_cons.undecided(row)

    cards: list[MetricCard] = []
    for key, label, source in METRIC_CARDS:
        median, observations = (
            await _median_close(session, instrument.id, as_of)
            if source == "ohlcv"
            else await _median_from_facts(session, instrument.id, key, as_of)
        )
        cards.append(
            MetricCard(
                key=key,
                label=label,
                value=_numeric(row.get(key)),
                median=median,
                observations=observations,
            )
        )

    computed_regime, to_bull, to_bear = await _regime(session, instrument.id, as_of)
    stored_regime = row.get("regime")
    regime = str(stored_regime) if isinstance(stored_regime, str) else computed_regime

    actions = (
        (
            await session.execute(
                select(CorporateAction)
                .where(CorporateAction.instrument_id == instrument.id)
                .order_by(CorporateAction.ex_date.desc())
            )
        )
        .scalars()
        .all()
    )

    return Factsheet(
        symbol=instrument.symbol,
        as_of=as_of,
        data_version=data_version,
        header={
            "symbol": instrument.symbol,
            "name": instrument.name,
            "exchange": "NSE",
            # docs/02 rule 2 and CLAUDE.md house rule 6: display uses the exchange print, so the
            # header price is the bar's `close_raw` and falls back to the adjusted close only when
            # there is no bar to read. `close` travels with it because the PROS rules, the
            # away-from-high cells and every percentile are computed on the adjusted series.
            "close_raw": _numeric(bar.close_raw if bar is not None else row.get("close")),
            "close": _numeric(bar.close if bar is not None else row.get("close")),
            "isin": instrument.isin,
        },
        # docs/01 §5 block 2, in its order: "P/E, Marketcap (cr), Beta, Series, Listed On."
        # The last two are the instrument's own columns rather than the fact row's, which is why
        # a cell holds more than a number.
        key_stats=[
            Cell("pe", "P/E", _numeric(row.get("pe"))),
            Cell("marketcap_cr", "Marketcap (cr)", _numeric(row.get("marketcap_cr"))),
            Cell("beta_12m", "Beta", _numeric(row.get("beta_12m"))),
            Cell("series", "Series", instrument.series),
            Cell("listed_on", "Listed On", instrument.listed_on),
        ],
        pros=pros,
        cons=cons,
        undecided=undecided,
        metric_cards=cards,
        # docs/01 §5 block 5, in its order: "Face Value, 1Y High, Away from 1Y High, ATH, Away
        # from ATH, MA 200/100/50/20."
        price_and_mas=[
            Cell("face_value", "Face Value", _numeric(instrument.face_value)),
            Cell("high_1y", "1Y High", _numeric(row.get("high_1y"))),
            Cell("away_high_1y", "Away from 1Y High", _numeric(row.get("away_high_1y"))),
            Cell("high_ath", "ATH", _numeric(row.get("high_ath"))),
            Cell("away_high_ath", "Away from ATH", _numeric(row.get("away_high_ath"))),
            *[Cell(f"ma_{k}", f"MA {k}", _numeric(row.get(f"ma_{k}"))) for k in (200, 100, 50, 20)],
        ],
        returns=_cells(row, RETURN_KEYS, RETURN_LABELS, percentiles),
        sharpe_returns=_cells(
            row, [f"sharpe_{m}m" for m in WINDOW_MONTHS], WINDOW_LABELS, percentiles
        ),
        volatility=_cells(row, [f"vol_{m}m" for m in WINDOW_MONTHS], WINDOW_LABELS, percentiles),
        rsi=_cells(row, [f"rsi_{m}m" for m in WINDOW_MONTHS], WINDOW_LABELS, percentiles),
        market_quality={
            "regime": regime,
            "regime_distance_bull": to_bull,
            "regime_distance_bear": to_bear,
            "median_vol_12m": _numeric(row.get("median_vol_12m")),
            "circuits": [
                {"label": label, "value": _numeric(row.get(f"circuits_{m}m"))}
                for m, label in zip(WINDOW_MONTHS, WINDOW_LABELS, strict=True)
            ],
            "positive_days": [
                {"label": label, "value": _numeric(row.get(f"pos_days_{m}m"))}
                for m, label in zip(WINDOW_MONTHS, WINDOW_LABELS, strict=True)
            ],
        },
        corporate_actions=[
            {
                "action_type": action.action_type,
                "ex_date": action.ex_date,
                "ratio_from": action.ratio_from,
                "ratio_to": action.ratio_to,
                "amount": action.amount,
            }
            for action in actions
        ],
        index_memberships=[
            {"slug": universe_row.slug, "name": universe_row.name} for universe_row in memberships
        ],
        percentile_universe=(
            None if universe is None else {"slug": universe.slug, "name": universe.name}
        ),
        notes=notes,
    )
