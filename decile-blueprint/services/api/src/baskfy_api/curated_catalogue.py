"""The published catalogue, as data — the shelf `/baskets` and `/explore` are windows onto.

``cb_basket`` held **one** row. Every browse grid, every collection, every ranked list and the
featured strip were all showing the same basket back to the reader under different headings, and
a catalogue of one reads as a demo of a catalogue rather than a product.

Why this is a seed and not a migration or a script
--------------------------------------------------
House rule 7: re-running any seeding job produces identical rows. A catalogue that exists only
because somebody ran an ``INSERT`` against their laptop is not catalogue content — it dies with
that database and a fresh checkout is back to one row. ``seed_momentum_scan_basket`` is how the
existing basket got there, and this sits beside it for the same reason.

Where the names come from, and why none of them is typed here
-------------------------------------------------------------
**Not one constituent symbol appears in this file.** A basket whose holdings were hand-typed is a
claim the engine cannot defend: nothing connects the names to the rule, so the rule is decoration.
Every entry below names a *seeded screen* by its public id, and the seeder runs that screen
through the same path ``POST /cb/baskets/from-screen`` uses — ``execute_screen`` then
``size_basket`` — against real adjusted bars. If the screen returns nothing, the basket is not
created; it is never back-filled with something plausible.

What the data can and cannot support
------------------------------------
``fundamental_daily`` is empty (``NEEDS-MAULIK`` #15), so ``marketcap_cr`` and ``pe`` are NULL
everywhere. That rules out a value basket, a large-cap basket or anything sorted on size or
earnings — those would state a rule the engine cannot currently evaluate, which is the one thing
a thesis may not do. What the plant does carry is adjusted bars, factor rows and point-in-time
index membership, so momentum, trend, volatility and liquidity baskets are computable and
everything else waits for the fundamentals fetch.

Why no basket uses SCORE weighting
----------------------------------
``WeightMethod.SCORE`` weights each name by the screen's own ranking factor, and it is the
obvious choice for a ranked list. It is not used here, because it is only *defined* while that
factor is positive: ``_score_raw`` raises when no name carries a positive score. Measured across
these six screens on 2026-08-21, five of them produce no positive score at all — their sorting
factor is a Sharpe ratio, and Sharpe over a down year is negative — and the sixth works only
because a twelve-month return happens to be positive today. A published basket has to survive its
own rebalance in a falling market, so every entry uses EQUAL, RANK or INV_VOL, each of which is
defined whatever the market did.

Cadence and weighting are read off each rule rather than assigned for variety
-----------------------------------------------------------------------------
A rule leaning on a one-month window is reviewed monthly; one built on twelve- and nine-month
Sharpe is reviewed quarterly. **No basket is annual**, though the enum allows it: rebalancing a
momentum rule once a year outlives the signal it is built on, and picking it to make the shelf
look varied would be exactly the kind of decoration this file avoids. For the same reason every
one of these is attributed to the engine and none to the human manager — a screen is a rule the
pipeline runs, and crediting a person for it would be a false byline.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.curated_seed import seed_curated_managers
from baskfy_api.screener import (
    AsOfOutOfRange,
    current_data_version,
    execute_screen,
    latest_published_date,
    resolve_as_of,
)
from baskfy_core.basket_sizing import (
    MAX_HOLDINGS,
    Candidate,
    WeightMethod,
    cash_pct_for_tier,
    size_basket,
)
from baskfy_core.curated_baskets import MANAGER_SLUG_BASKFY_ENGINE, assert_weights_sum_to_one
from baskfy_core.models import (
    CbBasket,
    CbBasketVersion,
    CbConstituent,
    CbManager,
    Instrument,
    Screen,
)
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.screener import ScreenResult, resolve_columns

__all__ = ["CATALOGUE", "CatalogueEntry", "seed_catalogue"]

#: The screen result columns the sizer reads. Same three names ``curated_from_screen`` uses.
_PRICE_COLUMN = "close_raw"
_SCORE_COLUMN = "score"
_VOL_COLUMN = "vol_12m"

#: What the sizer is given to spend. It decides how many whole shares fit, and therefore whether
#: every requested name can be held at all — generous on purpose, because a thin amount silently
#: drops the expensive names and the basket then misrepresents its own rule. It is **not** the
#: minimum investment shown on a card: that is ``cb_metrics.min_amount``, recomputed nightly from
#: live prices.
SIZING_AMOUNT: Decimal = Decimal("500000")

#: The earliest date the screener will serve (``docs/01`` §2.13, ``DATA_START_DATE``). A basket's
#: published history cannot begin before the plant can evaluate its own rule.
EARLIEST_GENESIS: dt.date = dt.date(2024, 11, 1)

#: How far forward to step when the earliest date does not fill the basket, and how many times.
#: Two years of monthly steps: Quality Momentum's "positive on half the days of the past year"
#: filter cannot return anything until a year of history exists behind it, so its genesis is
#: naturally about a year later than the others'.
_GENESIS_STEP = dt.timedelta(days=30)
_MAX_GENESIS_ATTEMPTS = 24

#: Indicative days to the next review, by cadence. Indicative because the publisher moves it when
#: a version actually lands; it exists so a fresh catalogue does not render "next review: never".
_REVIEW_DAYS: dict[str, int] = {"MONTHLY": 30, "QUARTERLY": 91}


@dataclass(frozen=True)
class CatalogueEntry:
    """One published basket, described by the rule behind it.

    ``thesis`` is what a reader sees first and is written to docs/14 §Tone: it says what is
    computed, never what will happen. ``rationale`` is the mechanical restatement — the index, the
    sort, the filters — so a reader can check the thesis against the rule instead of trusting it.
    """

    slug: str
    name: str
    screen_public_id: str
    holdings: int
    method: WeightMethod
    rebalance_frequency: str
    categories: tuple[str, ...]
    thesis: str
    rationale: str


CATALOGUE: tuple[CatalogueEntry, ...] = (
    CatalogueEntry(
        slug="broad-market-sharpe",
        name="Broad Market Sharpe",
        screen_public_id="exmpl0000001",
        holdings=20,
        method=WeightMethod.EQUAL,
        rebalance_frequency="MONTHLY",
        categories=("momentum", "broad-market"),
        thesis=(
            "The whole listed market ranked on risk-adjusted momentum rather than raw return, so "
            "a name that rose in a straight line outranks one that rose the same amount in "
            "lurches. Liquidity floor of a crore a day in median traded value, because a rank is "
            "worthless on a stock you cannot buy."
        ),
        rationale=(
            "NIFTY Total Market, sorted on the average of the 12-, 6-, 3- and 1-month Sharpe "
            "ratios, descending. Median 1-year traded value at or above ₹1,00,00,000. Equal "
            "weights. Reviewed monthly, because the blend carries a one-month term."
        ),
    ),
    CatalogueEntry(
        slug="trend-stack",
        name="Trend Stack",
        screen_public_id="exmpl0000002",
        holdings=18,
        method=WeightMethod.RANK,
        rebalance_frequency="MONTHLY",
        categories=("momentum", "trend"),
        thesis=(
            "Only names trading above their 50-, 100- and 200-day moving averages at once — the "
            "three lines stacked in order — then ranked on twelve-month return. It is a narrow "
            "filter by design and holds nothing at all in a market where the stack has broken."
        ),
        rationale=(
            "NIFTY 500, sorted on 12-month return, descending, restricted to closes above the "
            "50-, 100- and 200-day moving averages. Median 1-year traded value at or above "
            "₹2,00,00,000. Rank weights, so the strongest name carries the most. Reviewed monthly."
        ),
    ),
    CatalogueEntry(
        slug="liquid-momentum",
        name="Liquid Momentum",
        screen_public_id="exmpl0000003",
        holdings=15,
        method=WeightMethod.RANK,
        rebalance_frequency="QUARTERLY",
        categories=("momentum", "liquidity"),
        thesis=(
            "The most heavily traded end of the market, ranked on twelve-month risk-adjusted "
            "return. The five-crore liquidity floor is the point of it: this is the basket you "
            "can move in and out of without the exit costing more than the idea earned."
        ),
        rationale=(
            "NIFTY Total Market, sorted on 12-month Sharpe, descending. Median 1-year traded "
            "value at or above ₹5,00,00,000. Rank weights. Reviewed quarterly — a twelve-month "
            "signal does not change monthly."
        ),
    ),
    CatalogueEntry(
        slug="momentum-low-volatility",
        name="Momentum, Low Volatility",
        screen_public_id="exmpl0000004",
        holdings=15,
        method=WeightMethod.INV_VOL,
        rebalance_frequency="MONTHLY",
        categories=("momentum", "low-volatility"),
        thesis=(
            "Twelve-month momentum with the most recent month cut out — the academic 12-1 "
            "construction, which drops the short-term reversal that tends to follow a hard run — "
            "and then the steadier names of those that survive. Weighted so the quietest holdings "
            "carry the most."
        ),
        rationale=(
            "NIFTY 200, sorted on 12-month return excluding the last month, descending, with "
            "12-month volatility as the second sort. Median 1-year traded value at or above "
            "₹5,00,00,000. Inverse-volatility weights. Reviewed monthly."
        ),
    ),
    CatalogueEntry(
        slug="six-month-sharpe",
        name="Six-Month Sharpe",
        screen_public_id="exmpl0000005",
        holdings=20,
        method=WeightMethod.EQUAL,
        rebalance_frequency="QUARTERLY",
        categories=("momentum", "short-window"),
        thesis=(
            "The same risk-adjusted ranking as the broader baskets, measured over six months "
            "instead of twelve. A shorter window turns over faster and reacts sooner, which cuts "
            "both ways: it catches a change in leadership earlier and it is wrong more often."
        ),
        rationale=(
            "NIFTY 500, sorted on 6-month Sharpe, descending. No liquidity floor, so this list "
            "reaches further down the market than the others. Equal weights. Reviewed quarterly."
        ),
    ),
    CatalogueEntry(
        slug="quality-momentum",
        name="Quality Momentum",
        screen_public_id="exmpl0000006",
        holdings=15,
        method=WeightMethod.RANK,
        rebalance_frequency="QUARTERLY",
        categories=("momentum", "risk-trimmed"),
        thesis=(
            "Momentum with the wildest names taken out first. Beta is capped at two, the noisiest "
            "quantile by volatility is dropped, and a name has to have closed up on at least half "
            "the days of the past year before it is eligible — a rise made of many small days "
            "rather than three enormous ones."
        ),
        rationale=(
            "NIFTY 500, sorted on the average of the 12-, 9-, 6- and 3-month Sharpe ratios, "
            "descending. Beta at or below 2, top-volatility names excluded, at least 50% positive "
            "days over 12 months, median 1-year traded value at or above ₹2,00,00,000. Rank "
            "weights. Reviewed quarterly."
        ),
    ),
)


def _decimal(value: object) -> Decimal | None:
    """A screen cell as a Decimal, or ``None`` when the cell is empty or not a number.

    Screen results carry heterogeneous cells; this refuses rather than coerces, so a name with an
    unreadable price is dropped by the sizer instead of being sized off a guess.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except ArithmeticError:
            return None
    return None


async def _existing_slugs(session: AsyncSession) -> set[str]:
    rows = await session.scalars(
        select(CbBasket.slug).where(CbBasket.slug.in_([entry.slug for entry in CATALOGUE]))
    )
    return {str(slug) for slug in rows}


async def _first_fillable_genesis(
    session: AsyncSession,
    base: ScreenDefinition,
    *,
    holdings: int,
    columns: list[str],
    data_version: int,
) -> tuple[ScreenResult, list[Candidate]] | None:
    """The earliest past date at which this rule returns enough names, with that date's result.

    Steps forward from :data:`EARLIEST_GENESIS` rather than starting today, so a basket's history
    is as long as its rule honestly allows. Returns ``None`` when the rule never fills inside the
    window — Quality Momentum's twelve-month positive-days filter returns nothing at the data
    start, and a basket that cannot be cut is not published rather than shortened.
    """
    for attempt in range(_MAX_GENESIS_ATTEMPTS):
        candidate_date = EARLIEST_GENESIS + _GENESIS_STEP * attempt
        definition = base.model_copy(update={"historical_date": candidate_date})
        try:
            resolution = await resolve_as_of(session, definition.historical_date)
            result = await execute_screen(
                session,
                definition,
                as_of=resolution.as_of,
                data_version=data_version,
                columns=columns,
                requested_as_of=resolution.requested,
                limit=MAX_HOLDINGS,
            )
        except AsOfOutOfRange:
            # This date is outside what the plant can serve — an answer about the date, not about
            # the rule, so step on. Narrow on purpose: any other failure is a real fault and must
            # surface rather than be absorbed into "the basket did not fill" (house rule 3).
            continue

        score_key = result.sorting_factor.key
        candidates = [
            Candidate(
                symbol=row.symbol,
                rank=row.rank,
                price=_decimal(row.values.get(_PRICE_COLUMN)),
                score=_decimal(row.values.get(_SCORE_COLUMN, row.values.get(score_key))),
                vol=_decimal(row.values.get(_VOL_COLUMN)),
            )
            for row in result.rows
        ]
        if len(candidates) >= holdings:
            return result, candidates
    return None


async def seed_catalogue(session: AsyncSession, *, amount: Decimal = SIZING_AMOUNT) -> int:
    """Create every catalogue basket that does not exist yet. Returns how many were created.

    Idempotent by slug (house rule 7): a basket already present is left exactly as it is, so a
    re-run neither duplicates a shelf nor silently rewrites a version somebody may already hold.
    Re-cutting an existing basket is a *publish*, which is the version machinery's job and not a
    seeder's.

    A screen that returns too few names to fill its basket is skipped rather than shortened. A
    fifteen-name basket holding four is not the rule this entry describes, and publishing it
    would make the thesis false.

    **A database with nothing published seeds nothing**, for the same reason and not as a special
    case: a basket is cut *from* a published date's data, so with no published date there is
    nothing to cut and every entry would be skipped anyway. Returning 0 rather than letting
    `resolve_as_of` raise `NoPublishedData` matters because the test suite's `clean_db` truncates
    `pipeline_run` by design — so the raise made every db-marked test in the worker tree fail
    during setup, on a condition that is not an error but the empty state.
    """
    if await latest_published_date(session) is None:
        return 0

    await seed_curated_managers(session)

    manager_id = await session.scalar(
        select(CbManager.id).where(CbManager.slug == MANAGER_SLUG_BASKFY_ENGINE)
    )
    if manager_id is None:  # pragma: no cover - seed_curated_managers just created it
        return 0

    present = await _existing_slugs(session)
    columns = list(resolve_columns())
    data_version = await current_data_version(session)
    created = 0

    for entry in CATALOGUE:
        if entry.slug in present:
            continue

        screen = (
            await session.execute(select(Screen).where(Screen.public_id == entry.screen_public_id))
        ).scalar_one_or_none()
        if screen is None:
            continue

        base = ScreenDefinition.model_validate(screen.definition)

        # Cut the genesis **at a past date, from that date's own data** — never today's winners
        # stamped with an old effective date. Replaying one version's weights across history it
        # never held is the look-ahead house rule 5 forbids, and it was measured at +15.6pp on a
        # 5Y CAGR before it was fixed (docs/smallcase/STATUS.md, A1). Walking forward from the
        # earliest servable date means the published history starts when the rule first became
        # evaluable, which is a true sentence about every basket here.
        found = await _first_fillable_genesis(
            session,
            base,
            holdings=entry.holdings,
            columns=columns,
            data_version=data_version,
        )
        if found is None:
            continue
        result, candidates = found

        sized = size_basket(
            candidates,
            amount=amount,
            holdings=entry.holdings,
            cash_pct=cash_pct_for_tier(None),
            method=entry.method,
        )
        if len(sized.holdings) < entry.holdings:
            continue

        symbols = [holding.symbol for holding in sized.holdings]
        instruments = (
            await session.scalars(
                select(Instrument).where(
                    func.upper(Instrument.symbol).in_(symbols),
                    Instrument.is_active.is_(True),
                )
            )
        ).all()
        by_symbol = {str(row.symbol).upper(): row for row in instruments}
        if any(symbol not in by_symbol for symbol in symbols):
            # The rule ranked a name this table cannot resolve. Skipping keeps the basket honest;
            # substituting the next name down would publish a list the screen did not produce.
            continue

        # A sequence of weights, not a symbol-keyed map: the assertion iterates what it is given,
        # so a dict hands it the symbols and fails on ``str.quantize``. Caught by running it.
        assert_weights_sum_to_one([holding.weight for holding in sized.holdings])

        as_of = result.as_of
        review_days = _REVIEW_DAYS.get(entry.rebalance_frequency)
        basket = CbBasket(
            slug=entry.slug,
            name=entry.name,
            manager_id=int(manager_id),
            type="STOCK",
            access="FREE",
            visibility="PUBLISHED",
            categories=list(entry.categories),
            description_md=entry.thesis,
            rationale_md=entry.rationale,
            rebalance_frequency=entry.rebalance_frequency,
            benchmark_instrument_id=None,
            launched_at=as_of,
            next_review_at=(
                as_of + dt.timedelta(days=review_days) if review_days is not None else None
            ),
            source="SCREEN",
            scan_strategy_key=None,
            source_screen_id=int(screen.id),
            archived_at=None,
        )
        session.add(basket)
        await session.flush()

        version = CbBasketVersion(
            basket_id=basket.id,
            version_no=1,
            effective_date=as_of,
            label="GENESIS",
            added_count=len(symbols),
            removed_count=0,
            notes_md=(
                f"Cut from screen {screen.public_id!r} as of {as_of.isoformat()}, "
                f"{sized.method} weights across {len(symbols)} names."
            ),
            source_scan_run_id=None,
        )
        session.add(version)
        await session.flush()

        for holding in sized.holdings:
            session.add(
                CbConstituent(
                    version_id=version.id,
                    instrument_id=by_symbol[holding.symbol].id,
                    segment="Equity",
                    weight=holding.weight,
                )
            )
        created += 1

    return created
