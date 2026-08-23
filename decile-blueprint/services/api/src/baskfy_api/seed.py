"""Seed reference data into an empty database (PROMPTS.md Prompt 1 deliverable 4 & 5b).

Every step is an idempotent upsert, so ``make seed`` can be re-run at any time and converges on
the same rows — the same rule docs/02 §"Non-negotiable engineering rules" #3 imposes on ingestion.

Usage:
    python -m baskfy_api.seed all
    python -m baskfy_api.seed reference          # exchange, universes, plans, example screens
    python -m baskfy_api.seed trading-days
    python -m baskfy_api.seed fixture            # the reference CSV export (docs/13)
    python -m baskfy_api.seed bars               # 3 years of bars from FixtureProvider
    python -m baskfy_api.seed market             # index snapshots + market-health breadth
    python -m baskfy_api.seed e2e                # everything the browser acceptance suite needs

``bars`` is the local-development dataset docs/03 §Environments describes: "100-instrument
sample, 3 years, seeded from fixtures ... no Kite calls, provider stubbed". It goes through
:class:`FixtureProvider`, so `make seed` exercises the same port the pipeline will.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import os
import sys
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.db import session_scope
from baskfy_api.security import hash_password
from baskfy_api.settings import get_settings
from baskfy_core.breadth import breadth_query
from baskfy_core.curated_baskets import ConstituentDraft, VersionDraft
from baskfy_core.models import (
    AppUser,
    BasketConstituent,
    BasketVersion,
    CorporateAction,
    CuratedBasket,
    CuratedManager,
    Exchange,
    FactorDaily,
    IndexDef,
    IndexMemberDaily,
    IndexSnapshotDaily,
    Instrument,
    MarketHealthDaily,
    OhlcvDaily,
    PipelineRun,
    PipelineRunStep,
    Plan,
    Screen,
    ScreenRun,
    Subscription,
    TradingDay,
)
from baskfy_core.momentum_scan import MomentumScan
from baskfy_core.reference_export import ReferenceRows, to_rows
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.seed_data import (
    ALL_PLANS,
    EXAMPLE_SCREENS,
    NSE_EXCHANGE_CODE,
    NSE_EXCHANGE_ID,
    PLANS,
    index_def_rows,
)
from baskfy_core.trading_calendar import build_calendar, default_calendar_range
from baskfy_core.universes import (
    FIRST_NON_UNIVERSE_INDEX_ID,
    MARKET_HEALTH_SLUGS,
    UNIVERSE_BY_SLUG,
    slugify_index,
)
from baskfy_providers.fixtures import FixtureProvider

CURATED_MANAGER_ROWS: Final[tuple[dict[str, object], ...]] = (
    {
        "slug": "baskfy-engine",
        "name": "Baskfy Momentum Engine",
        "kind": "ENGINE",
        "sebi_reg_no": None,
        "bio": (
            "Baskfy's rules-based momentum engine turns the documented D1-D6 rankings and "
            "MomentumScan outputs into model baskets for Maulik's own account."
        ),
        "strategies": ["D1", "D2", "D3", "D4", "D5", "D6", "MomentumScan"],
        "disclosures_md": (
            "Model output for Maulik's own research and account. It is not investment advice. "
            "Market investments are subject to risk; past performance does not guarantee "
            "future returns."
        ),
    },
    {
        "slug": "maulik",
        "name": "Maulik",
        "kind": "HUMAN",
        "sebi_reg_no": None,
        "bio": (
            "Maulik's hand-curated baskets for his own account, maintained separately from the "
            "rules-based Baskfy engine."
        ),
        "strategies": ["Hand-curated baskets"],
        "disclosures_md": (
            "Personal model baskets for Maulik's own research and account. They are not "
            "investment advice. Market investments are subject to risk; past performance does "
            "not guarantee future returns."
        ),
    },
)

CURATED_SCAN_BASKET_SLUG: Final = "baskfy-momentum-scan"
CURATED_SCAN_SCREEN_PUBLIC_ID: Final = "curated-momentum-scan"
MAX_CURATED_SCAN_CONSTITUENTS: Final = 50
_WEIGHT_UNITS: Final = 10_000


class CuratedScanProjectionError(ValueError):
    """A scan cannot be projected without losing identity or determinism."""


def _equal_four_decimal_weights(symbols: Sequence[str]) -> dict[str, Decimal]:
    """Allocate exactly 10,000 weight units with a stable largest-remainder tie break."""
    ordered = sorted(symbols)
    count = len(ordered)
    if count == 0:
        raise CuratedScanProjectionError("a curated scan must contain at least one symbol")
    if count > MAX_CURATED_SCAN_CONSTITUENTS:
        raise CuratedScanProjectionError(
            f"a curated scan is capped at {MAX_CURATED_SCAN_CONSTITUENTS} constituents"
        )

    base, remainder = divmod(_WEIGHT_UNITS, count)
    return {
        symbol: Decimal(base + (position < remainder)) / Decimal(_WEIGHT_UNITS)
        for position, symbol in enumerate(ordered)
    }


def _validated_scan_symbols(scan: MomentumScan) -> tuple[str, ...]:
    if type(scan.as_of) is not dt.date:
        raise CuratedScanProjectionError("scan.as_of must be a date")
    if scan.universe != "nse_cash":
        raise CuratedScanProjectionError("curated MomentumScan universe must be nse_cash")
    if type(scan.data_version) is not int or scan.data_version <= 0:
        raise CuratedScanProjectionError("scan.data_version must be a positive integer")
    if not scan.screen_run_id or not scan.screen_run_id.strip():
        raise CuratedScanProjectionError("scan.screen_run_id must be non-empty")
    if "symbol" not in scan.frame.columns:
        raise CuratedScanProjectionError("scan.frame must contain symbol")
    raw_symbols = scan.frame.get_column("symbol").to_list()
    if any(not isinstance(symbol, str) or not symbol.strip() for symbol in raw_symbols):
        raise CuratedScanProjectionError("every scan symbol must be a non-empty string")
    symbols = tuple(str(symbol).strip().upper() for symbol in raw_symbols)
    if len(set(symbols)) != len(symbols):
        raise CuratedScanProjectionError("scan symbols must be unique")
    return symbols


async def _resolve_scan_instruments(
    session: AsyncSession, symbols: tuple[str, ...]
) -> dict[str, int]:
    resolved_rows = (
        (
            await session.execute(
                select(Instrument.symbol, Instrument.id)
                .join(Exchange, Exchange.id == Instrument.exchange_id)
                .where(
                    Exchange.code == "NSE",
                    Instrument.symbol.in_(symbols),
                    Instrument.series == "EQ",
                    Instrument.is_active.is_(True),
                )
            )
        )
        .tuples()
        .all()
    )
    by_symbol: dict[str, int] = {}
    ambiguous: set[str] = set()
    for symbol, instrument_id in resolved_rows:
        if symbol in by_symbol:
            ambiguous.add(symbol)
        by_symbol[symbol] = instrument_id
    missing = sorted(set(symbols) - by_symbol.keys())
    if missing or ambiguous:
        detail = []
        if missing:
            detail.append(f"missing NSE EQ instruments: {missing}")
        if ambiguous:
            detail.append(f"ambiguous NSE EQ instruments: {sorted(ambiguous)}")
        raise CuratedScanProjectionError("; ".join(detail))
    return by_symbol


async def _curated_scan_manager(session: AsyncSession) -> CuratedManager:
    manager = await session.scalar(
        select(CuratedManager).where(CuratedManager.slug == "baskfy-engine")
    )
    if manager is None:
        await seed_curated_product_reference(session)
        manager = await session.scalar(
            select(CuratedManager).where(CuratedManager.slug == "baskfy-engine")
        )
    if manager is None:  # pragma: no cover - protects against future seed refactors
        raise CuratedScanProjectionError("the baskfy-engine manager could not be seeded")
    return manager


async def _curated_scan_owner_id(session: AsyncSession) -> int:
    owner = await session.scalar(
        select(AppUser).where(AppUser.public_id == get_settings().sole_user_id)
    )
    if owner is None:  # pragma: no cover - manager seeding immediately precedes this lookup
        raise CuratedScanProjectionError("the sole curated-scan owner could not be resolved")
    return owner.id


def _curated_scan_definition() -> ScreenDefinition:
    return ScreenDefinition(
        index="nifty-total-market",
        sort_by="avg_sharpe_12_6_3_1",
        sort_direction="desc",
        series=["EQ", "BE"],
    )


async def _curated_scan_source_run(
    session: AsyncSession,
    scan: MomentumScan,
    symbols: tuple[str, ...],
    by_symbol: dict[str, int],
    owner_id: int,
) -> tuple[ScreenRun, bool]:
    definition = _curated_scan_definition()
    definition_payload = definition.model_dump(mode="json", by_alias=True)
    screen = await session.scalar(
        select(Screen).where(Screen.public_id == CURATED_SCAN_SCREEN_PUBLIC_ID)
    )
    if screen is None:
        screen = Screen(
            public_id=CURATED_SCAN_SCREEN_PUBLIC_ID,
            user_id=owner_id,
            name="Baskfy MomentumScan projection",
            definition=definition_payload,
            columns=["symbol"],
            is_example=False,
        )
        session.add(screen)
        await session.flush()
    elif screen.user_id != owner_id or screen.definition != definition_payload:
        raise CuratedScanProjectionError(
            "the reserved MomentumScan source screen has conflicting ownership or definition"
        )

    scan_results = [
        {"rank": rank, "instrument_id": by_symbol[symbol], "factor_value": None}
        for rank, symbol in enumerate(symbols, start=1)
    ]
    source_run = await session.scalar(
        select(ScreenRun).where(
            ScreenRun.screen_id == screen.id,
            ScreenRun.as_of == scan.as_of,
            ScreenRun.definition_hash == definition.definition_hash(),
        )
    )
    if source_run is None:
        source_run = ScreenRun(
            screen_id=screen.id,
            as_of=scan.as_of,
            definition_hash=definition.definition_hash(),
            result_count=len(scan_results),
            results=scan_results,
        )
        session.add(source_run)
        await session.flush()
        return source_run, True
    if source_run.result_count != len(scan_results) or source_run.results != scan_results:
        raise CuratedScanProjectionError(
            "the persisted source scan differs from this scan under the same identity"
        )
    return source_run, False


async def _curated_scan_basket(
    session: AsyncSession, launched_at: dt.date, manager: CuratedManager
) -> tuple[CuratedBasket, bool]:
    basket = await session.scalar(
        select(CuratedBasket).where(CuratedBasket.slug == CURATED_SCAN_BASKET_SLUG)
    )
    if basket is None:
        basket = CuratedBasket(
            slug=CURATED_SCAN_BASKET_SLUG,
            name="Baskfy Momentum Scan",
            manager_id=manager.id,
            type="STOCK",
            access="FREE",
            visibility="PUBLISHED",
            categories=["Momentum", "Quantitative"],
            description_md="A rules-based projection of Baskfy's selected MomentumScan output.",
            rationale_md=(
                "Constituents come from the documented MomentumScan pipeline and are allocated "
                "equally at four-decimal precision."
            ),
            rebalance_frequency="WEEKLY",
            benchmark_instrument_id=None,
            launched_at=launched_at,
            next_review_at=None,
            source="SCAN",
            scan_strategy_key="MomentumScan",
            archived_at=None,
        )
        session.add(basket)
        await session.flush()
        return basket, True
    if (
        basket.manager_id != manager.id
        or basket.source != "SCAN"
        or basket.scan_strategy_key != "MomentumScan"
    ):
        raise CuratedScanProjectionError(
            f"existing {CURATED_SCAN_BASKET_SLUG} basket is not the MomentumScan projection"
        )
    return basket, False


async def _curated_scan_genesis(
    session: AsyncSession,
    scan: MomentumScan,
    basket: CuratedBasket,
    source_run: ScreenRun,
    constituents: tuple[ConstituentDraft, ...],
) -> bool:
    expected_notes = (
        f"Genesis projection from MomentumScan {scan.screen_run_id}; "
        f"data version {scan.data_version}."
    )
    existing = await session.scalar(
        select(BasketVersion)
        .where(BasketVersion.basket_id == basket.id)
        .order_by(BasketVersion.version_no)
        .limit(1)
    )
    if existing is None:
        from baskfy_api.curated_baskets import CuratedBasketRepository  # noqa: PLC0415

        await CuratedBasketRepository(session).create_version(
            basket.id,
            VersionDraft(
                effective_date=scan.as_of,
                constituents=constituents,
                notes_md=expected_notes,
                source_scan_run_id=source_run.id,
            ),
        )
        return True

    stored = (
        (
            await session.execute(
                select(BasketConstituent.instrument_id, BasketConstituent.weight).where(
                    BasketConstituent.version_id == existing.id
                )
            )
        )
        .tuples()
        .all()
    )
    expected = {(row.instrument_id, row.weight) for row in constituents}
    if (
        existing.version_no != 1
        or existing.effective_date != scan.as_of
        or existing.source_scan_run_id != source_run.id
        or existing.notes_md != expected_notes
        or set(stored) != expected
    ):
        raise CuratedScanProjectionError(
            "existing MomentumScan genesis differs; immutable versions are never reseeded"
        )
    return False


async def seed_curated_momentum_scan(
    session: AsyncSession,
    scan: MomentumScan,
) -> dict[str, int]:
    """Project one already-selected MomentumScan output into an immutable genesis version.

    The caller owns strategy ranking and passes only its selected top-N rows. This boundary owns
    persistence: it resolves every symbol in one query, records the source scan in the existing
    ``screen_run`` audit model, and creates the product projection exactly once. A later scan is
    a publish operation, not a seed, so an existing but different genesis is refused rather than
    silently rewritten.
    """
    symbols = _validated_scan_symbols(scan)
    weights = _equal_four_decimal_weights(symbols)
    by_symbol = await _resolve_scan_instruments(session, symbols)
    manager = await _curated_scan_manager(session)
    owner_id = await _curated_scan_owner_id(session)
    source_run, source_created = await _curated_scan_source_run(
        session, scan, symbols, by_symbol, owner_id
    )
    basket, basket_created = await _curated_scan_basket(session, scan.as_of, manager)
    constituents = tuple(
        ConstituentDraft(
            instrument_id=by_symbol[symbol],
            segment="Equity",
            weight=weight,
        )
        for symbol, weight in weights.items()
    )
    version_created = await _curated_scan_genesis(
        session,
        scan,
        basket,
        source_run,
        constituents,
    )

    return {
        "screen_run": int(source_created),
        "cb_basket": int(basket_created),
        "cb_basket_version": int(version_created),
        "cb_constituent": len(symbols) if version_created else 0,
    }


def _sole_user_email(public_id: str) -> str:
    """Derive a stable, non-routable address without embedding an operator-supplied identifier."""
    # 48 hex characters retain 192 bits of collision resistance while keeping the local part
    # well below RFC 5321's 64-octet ceiling (`sole-` + digest = 53 ASCII octets).
    digest = hashlib.sha256(public_id.encode("utf-8")).hexdigest()[:48]
    return f"sole-{digest}@users.invalid"


async def seed_curated_product_reference(session: AsyncSession) -> dict[str, int]:
    """Seed the sole operator and the two first-party manager profiles.

    The operator upsert deliberately does nothing on conflict: a seed must never reset a name,
    address, or credential that the operator has subsequently chosen. Manager copy is reference
    data, so it converges on every run. No auth, subscription, payment, or broker-token table is
    touched here.
    """
    settings = get_settings()
    operator = insert(AppUser).values(
        public_id=settings.sole_user_id,
        email=_sole_user_email(settings.sole_user_id),
        name="Maulik",
        password_hash=None,
    )
    await session.execute(operator.on_conflict_do_nothing(index_elements=[AppUser.public_id]))

    managers = insert(CuratedManager).values(list(CURATED_MANAGER_ROWS))
    await session.execute(
        managers.on_conflict_do_update(
            index_elements=[CuratedManager.slug],
            set_={
                "name": managers.excluded.name,
                "kind": managers.excluded.kind,
                "sebi_reg_no": managers.excluded.sebi_reg_no,
                "bio": managers.excluded.bio,
                "strategies": managers.excluded.strategies,
                "disclosures_md": managers.excluded.disclosures_md,
            },
        )
    )
    return {"app_user": 1, "cb_manager": len(CURATED_MANAGER_ROWS)}


async def seed_exchange(session: AsyncSession) -> int:
    stmt = insert(Exchange).values(id=NSE_EXCHANGE_ID, code=NSE_EXCHANGE_CODE)
    await session.execute(
        stmt.on_conflict_do_update(index_elements=[Exchange.id], set_={"code": stmt.excluded.code})
    )
    return 1


async def seed_universes(session: AsyncSession) -> int:
    rows = index_def_rows()
    stmt = insert(IndexDef).values(rows)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[IndexDef.id],
            set_={
                "slug": stmt.excluded.slug,
                "name": stmt.excluded.name,
                "is_universe": stmt.excluded.is_universe,
                "sort_order": stmt.excluded.sort_order,
            },
        )
    )
    return len(rows)


async def seed_plans(session: AsyncSession) -> int:
    """docs/01 §1's three plans, plus the flagged ₹0 row (Prompt 13 §5).

    The ₹0 row exists only when `BASKFY_FREE_TIER_ENABLED` is on. PROMPTS.md Prompt 13 §5 makes
    that tier optional and puts it "behind a feature flag", and a flag that leaves the plan in the
    catalogue while hiding it from one endpoint is a flag that half-works: `POST /checkout/session`
    and every join through `plan` would still find it. Flipping the flag on and re-running
    `make seed` — which is idempotent — is what creates the row.

    The pricing copy (label, tagline) is stored on the row so the web app reads it from the API
    rather than holding a second copy (Prompt 13 acceptance criterion 4).
    """
    catalogue = ALL_PLANS if get_settings().free_tier_enabled else PLANS
    values = [
        {
            "id": plan.id,
            "code": plan.code,
            "price_inr": plan.price_inr,
            "interval": plan.interval,
            "features": {**plan.features, "label": plan.label, "tagline": plan.tagline},
        }
        for plan in catalogue
    ]
    stmt = insert(Plan).values(values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Plan.id],
            set_={
                "code": stmt.excluded.code,
                "price_inr": stmt.excluded.price_inr,
                "interval": stmt.excluded.interval,
                "features": stmt.excluded.features,
            },
        )
    )
    return len(values)


async def seed_example_screens(session: AsyncSession) -> int:
    values = [
        {
            "public_id": screen.public_id,
            "user_id": None,
            "name": screen.name,
            # Stored through canonical_json so the persisted bytes match what gets hashed.
            "definition": screen.definition.model_dump(mode="json", by_alias=True),
            "columns": screen.columns,
            "is_example": True,
        }
        for screen in EXAMPLE_SCREENS
    ]
    stmt = insert(Screen).values(values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Screen.public_id],
            set_={
                "name": stmt.excluded.name,
                "definition": stmt.excluded.definition,
                "columns": stmt.excluded.columns,
                "is_example": stmt.excluded.is_example,
            },
        )
    )
    return len(values)


async def seed_trading_days(session: AsyncSession, today: dt.date | None = None) -> int:
    """Materialise the calendar for 2011..current year (Prompt 1 deliverable 3).

    Rows already promoted to ``source='bhavcopy'`` by ingestion are left alone: real market data
    outranks the seed list, and re-seeding must never demote it.
    """
    start, end = default_calendar_range(today or dt.date.today())
    rows = [
        {
            "exchange_id": NSE_EXCHANGE_ID,
            "date": row.date,
            "is_trading_day": row.is_trading_day,
            "holiday_name": row.holiday_name,
            "source": row.source,
        }
        for row in build_calendar(start, end)
    ]
    for chunk in _chunks(rows, 2000):
        stmt = insert(TradingDay).values(chunk)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[TradingDay.exchange_id, TradingDay.date],
                set_={
                    "is_trading_day": stmt.excluded.is_trading_day,
                    "holiday_name": stmt.excluded.holiday_name,
                    "source": stmt.excluded.source,
                },
                where=TradingDay.source != "bhavcopy",
            )
        )
    return len(rows)


async def seed_reference_fixture(session: AsyncSession, rows: ReferenceRows | None = None) -> int:
    """Load the 271-row reference export into instrument / factor_daily / index_member_daily."""
    data = rows if rows is not None else to_rows()

    instrument_values = [
        {
            "exchange_id": NSE_EXCHANGE_ID,
            "symbol": inst.symbol,
            "name": inst.name,
            "series": inst.series,
            "instrument_type": inst.instrument_type,
            "is_active": True,
        }
        for inst in data.instruments
    ]
    stmt = insert(Instrument).values(instrument_values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Instrument.exchange_id, Instrument.symbol, Instrument.series],
            set_={"name": stmt.excluded.name, "instrument_type": stmt.excluded.instrument_type},
        )
    )

    ids = dict((await session.execute(select(Instrument.symbol, Instrument.id))).tuples().all())

    factor_values = []
    for row in data.factors:
        payload = {k: v for k, v in row.items() if k != "symbol"}
        payload["instrument_id"] = ids[str(row["symbol"])]
        factor_values.append(payload)
    factor_stmt = insert(FactorDaily).values(factor_values)
    await session.execute(
        factor_stmt.on_conflict_do_update(
            index_elements=[FactorDaily.instrument_id, FactorDaily.date],
            set_={
                c: factor_stmt.excluded[c]
                for c in factor_values[0]
                if c not in ("instrument_id", "date")
            },
        )
    )

    membership_values = [
        {
            "index_id": UNIVERSE_BY_SLUG[m.universe_slug].index_id,
            "date": m.date,
            "instrument_id": ids[m.symbol],
        }
        for m in data.memberships
    ]
    member_stmt = insert(IndexMemberDaily).values(membership_values)
    await session.execute(member_stmt.on_conflict_do_nothing())

    return len(factor_values)


async def seed_fixture_bars(session: AsyncSession, provider: FixtureProvider | None = None) -> int:
    """Load instruments, daily bars and corporate actions from FixtureProvider (docs/03 `local`).

    The bars land in ``ohlcv_daily`` with ``close == close_raw`` and ``adj_factor == 1``: they are
    raw exchange prints, and docs/09 is explicit that adjustment is the pipeline's job, not a
    provider's. Prompt 3's ``apply_adjustments`` is what will make ``close`` diverge from
    ``close_raw`` for CUPID's split and bonuses.

    IT REFUSES TO RUN OVER A POPULATED TABLE, AND THAT GUARD WAS PAID FOR (M7).
    ``tests/fixtures/providers/PROVENANCE.md`` says it plainly: every bar before 2026-08-18 is a
    seeded random walk. The rows carry ``source='nse'`` like real ones, so nothing downstream can
    tell them apart — and the upsert is keyed on ``(instrument_id, date)``, so running this against
    a database holding a real backfill silently **replaces real closes with synthetic ones**. It
    did exactly that once: `make seed` over the restored 1,138,300-bar backfill rewrote 25,256 real
    bars, caught only because the row counts were snapshotted either side of the command.

    So: if any bars exist, this is a no-op that says why. `make seed` is for bringing a fresh
    database up, and a populated one is not that. Force it with ``BASKFY_SEED_FORCE_BARS=1`` when
    you genuinely want the fixture market — a scratch database, or the e2e run, which builds its
    own from empty.
    """
    existing = int(
        (await session.execute(select(func.count()).select_from(OhlcvDaily))).scalar_one()
    )
    if existing and os.getenv("BASKFY_SEED_FORCE_BARS") != "1":
        print(
            f"ohlcv_daily: SKIPPED — {existing:,} bars already present. The fixture bars are a "
            f"random walk (tests/fixtures/providers/PROVENANCE.md) and would overwrite real "
            f"closes on matching (instrument_id, date). Set BASKFY_SEED_FORCE_BARS=1 to override."
        )
        return 0

    source = provider if provider is not None else FixtureProvider()

    instruments = source.list_instruments()
    instrument_values = [
        {
            "exchange_id": NSE_EXCHANGE_ID,
            "symbol": record.symbol,
            "name": record.name,
            "series": record.series,
            "instrument_type": record.instrument_type,
            "kite_token": record.kite_token,
            "lot_size": record.lot_size,
            "listed_on": record.listed_on,
            "is_active": True,
        }
        for record in instruments
    ]
    stmt = insert(Instrument).values(instrument_values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Instrument.exchange_id, Instrument.symbol, Instrument.series],
            set_={
                "name": stmt.excluded.name,
                "instrument_type": stmt.excluded.instrument_type,
                "kite_token": stmt.excluded.kite_token,
                "lot_size": stmt.excluded.lot_size,
                "listed_on": stmt.excluded.listed_on,
            },
        )
    )

    ids = dict((await session.execute(select(Instrument.symbol, Instrument.id))).tuples().all())

    window_start = dt.date(1900, 1, 1)
    window_end = dt.date(2100, 1, 1)
    bar_count = 0
    for record in instruments:
        if record.kite_token is None:
            continue
        frame = source.daily_bars(record.kite_token, window_start, window_end)
        if frame.height == 0:
            continue
        instrument_id = ids[record.symbol]
        values = [
            {
                "instrument_id": instrument_id,
                "date": row["date"],
                # Unadjusted on the way in; `close` becomes the adjusted series in Prompt 3.
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "close_raw": row["close"],
                "volume_raw": row["volume"],
                "adj_factor": 1,
                "source": row["source"],
            }
            for row in frame.iter_rows(named=True)
        ]
        for chunk in _chunks(values, 2000):
            bar_stmt = insert(OhlcvDaily).values(list(chunk))
            await session.execute(
                bar_stmt.on_conflict_do_update(
                    index_elements=[OhlcvDaily.instrument_id, OhlcvDaily.date],
                    set_={
                        c: bar_stmt.excluded[c]
                        for c in values[0]
                        if c not in ("instrument_id", "date")
                    },
                )
            )
        bar_count += len(values)

    actions = source.corporate_actions(dt.date(1900, 1, 1))
    if actions:
        action_values = [
            {
                "instrument_id": ids[action.symbol],
                "action_type": action.action_type,
                "ex_date": action.ex_date,
                "ratio_from": action.ratio_from,
                "ratio_to": action.ratio_to,
                "amount": action.amount,
                "raw": action.raw,
            }
            for action in actions
            if action.symbol in ids
        ]
        action_stmt = insert(CorporateAction).values(action_values)
        await session.execute(
            action_stmt.on_conflict_do_update(
                index_elements=[
                    CorporateAction.instrument_id,
                    CorporateAction.action_type,
                    CorporateAction.ex_date,
                ],
                set_={
                    "ratio_from": action_stmt.excluded.ratio_from,
                    "ratio_to": action_stmt.excluded.ratio_to,
                    "amount": action_stmt.excluded.amount,
                    "raw": action_stmt.excluded.raw,
                },
            )
        )

    return bar_count


def _chunks[T](rows: Sequence[T], size: int) -> list[Sequence[T]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


#: The account `apps/web`'s Playwright suite signs in as. A fixed public_id keeps the JWT's `sub`
#: stable across runs, so a token minted in one run is not silently valid in the next.
#: RFC 2606 reserves `example.com` for documentation and testing, and it is deliverable-looking
#: enough for `email-validator` to accept — unlike the `.test` TLD, which RFC 6761 marks
#: special-use and the validator refuses outright. `docs/12a` §12.
E2E_EMAIL: Final = "e2e@example.com"
E2E_PUBLIC_ID: Final = "e2e000000001"
#: The browser suite's password. Published on purpose: it is a constant in a throwaway database,
#: and a secret that lives in a config file the suite also reads is not a secret. `docs/12a` §12.
E2E_PASSWORD: Final = "e2e-suite-password"


#: How far back `seed_index_snapshots` walks looking for snapshot dates. The fixture carries 30
#: trading days (docs/08 §Dashboard's "30-day" sparkline), which is ~44 calendar days; 90 gives
#: room without turning the seed into a scan.
SNAPSHOT_LOOKBACK_DAYS: Final = 90


async def seed_index_snapshots(
    session: AsyncSession, provider: FixtureProvider | None = None, end: dt.date | None = None
) -> int:
    """Load ``index_snapshot_daily`` from FixtureProvider — docs/01 §7's dashboard.

    Registers an ``index_def`` row for any index the fixture publishes that we have no row for,
    with an id allocated from ``FIRST_NON_UNIVERSE_INDEX_ID`` upward. That mirrors what
    ``baskfy_worker.tasks.snapshots`` does nightly, and for the same reason: the 14 selectable
    universes are hand-pinned because they carry ``factor_daily`` mask bits, while the dashboard
    grows to whatever the exchange publishes.
    """
    source = provider if provider is not None else FixtureProvider()
    last = end or to_rows().as_of
    # Walk calendar days and keep the ones the provider answers for. `index_snapshots(on)` is the
    # whole port — asking it per date is what a backfill does, and it keeps the seed honest about
    # only knowing what the provider will actually serve.
    by_date = {
        day: found
        for day in (last - dt.timedelta(days=offset) for offset in range(SNAPSHOT_LOOKBACK_DAYS))
        if (found := source.index_snapshots(day))
    }
    dates = sorted(by_date)
    if not dates:
        return 0

    known = dict((await session.execute(select(IndexDef.slug, IndexDef.id))).tuples().all())
    next_id = max(
        [
            FIRST_NON_UNIVERSE_INDEX_ID,
            *(v + 1 for v in known.values() if v >= FIRST_NON_UNIVERSE_INDEX_ID),
        ]
    )

    values: list[dict[str, object]] = []
    for on in dates:
        for record in by_date[on]:
            slug = slugify_index(record.index_slug)
            index_id = known.get(slug)
            if index_id is None:
                index_id = next_id
                next_id += 1
                known[slug] = index_id
                await session.execute(
                    insert(IndexDef)
                    .values(
                        id=index_id,
                        slug=slug,
                        name=record.index_slug.replace("-", " ").upper(),
                        is_universe=False,
                        sort_order=index_id,
                    )
                    .on_conflict_do_nothing()
                )
            values.append(
                {
                    "index_id": index_id,
                    "date": on,
                    "level": record.level,
                    "change_abs": record.change_abs,
                    "change_pct": record.change_pct,
                    # NULL, never 0 — docs/01 §7's derived indices publish no fundamentals.
                    "pe": record.pe,
                    "pb": record.pb,
                    "div_yield": record.div_yield,
                }
            )

    for chunk in _chunks(values, 5_000):
        stmt = insert(IndexSnapshotDaily).values(list(chunk))
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[IndexSnapshotDaily.index_id, IndexSnapshotDaily.date],
                set_={
                    column: stmt.excluded[column]
                    for column in ("level", "change_abs", "change_pct", "pe", "pb", "div_yield")
                },
            )
        )
    return len(values)


async def seed_market_health(session: AsyncSession, on: dt.date) -> int:
    """Breadth for the twelve market-health universes, on the dates factor rows exist for.

    Runs ``baskfy_core.breadth.breadth_query`` — the same statement the nightly step executes —
    rather than a second copy of the arithmetic. Prompt 11's first acceptance criterion checks
    that arithmetic against a hand-computed fixture, and a seed with its own version of it would
    make the check meaningless.
    """
    written = 0
    for slug in MARKET_HEALTH_SLUGS:
        universe = UNIVERSE_BY_SLUG[slug]
        constituents, above_200, above_50, near_ath, positive_1y, above_20 = (
            await session.execute(breadth_query(universe.index_id, on))
        ).one()
        stmt = insert(MarketHealthDaily).values(
            index_id=universe.index_id,
            date=on,
            pct_above_200dma=above_200,
            pct_above_50dma=above_50,
            pct_within_10pct_ath=near_ath,
            pct_ret_1y_positive=positive_1y,
            pct_above_20dma=above_20,
            constituent_count=constituents,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[MarketHealthDaily.index_id, MarketHealthDaily.date],
                set_={
                    column: stmt.excluded[column]
                    for column in (
                        "pct_above_200dma",
                        "pct_above_50dma",
                        "pct_within_10pct_ath",
                        "pct_ret_1y_positive",
                        "constituent_count",
                    )
                },
            )
        )
        written += 1
    return written


async def seed_e2e_account(session: AsyncSession) -> int:
    """One subscribed user, for the browser acceptance suite (Prompt 9).

    Subscribed because the export is entitlement-gated (docs/07 §Entitlements) and the suite has
    to walk through it.

    Prompt 12 removed the stub credential check, so this account now needs a **real Argon2id
    hash** — the suite signs in through `POST /auth/login` like anyone else. The password is a
    published constant with no value outside a throwaway database, and `seed e2e` is not a command
    anything but a test database should ever be pointed at. It is hashed at the settings' cost,
    which the browser suite deliberately turns down (`playwright.config.ts`).
    """
    user = insert(AppUser).values(
        public_id=E2E_PUBLIC_ID,
        email=E2E_EMAIL,
        name="End-to-end tester",
        password_hash=hash_password(E2E_PASSWORD, get_settings()),
        email_verified_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    # Conflict on `public_id`, not on `email`: the public id is what identifies this account
    # across re-seeds, and the address is a property of it that has changed at least once. An
    # upsert keyed on the address leaves the old row behind and then collides on the id.
    await session.execute(
        user.on_conflict_do_update(
            index_elements=[AppUser.public_id],
            set_={
                "email": user.excluded.email,
                "name": user.excluded.name,
                "password_hash": user.excluded.password_hash,
                "email_verified_at": user.excluded.email_verified_at,
            },
        )
    )
    user_id = (
        await session.execute(select(AppUser.id).where(AppUser.public_id == E2E_PUBLIC_ID))
    ).scalar_one()

    existing = (
        await session.execute(select(Subscription.id).where(Subscription.user_id == user_id))
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            Subscription(
                user_id=user_id,
                plan_id=PLANS[0].id,
                status="active",
                started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                current_period_end=dt.datetime(2030, 1, 1, tzinfo=dt.UTC),
            )
        )
    return 1


async def seed_published_run(
    session: AsyncSession, trade_date: dt.date, data_version: int = 1
) -> int:
    """A published `pipeline_run`, without which `resolve_as_of` refuses to serve any date.

    docs/06 §step 1 defines the as-of as the latest date belonging to a *published* run, and the
    reference fixture seeds `factor_daily` directly — so the run row has to be written too, or the
    API answers 503 for every request (docs/07a §8).

    ``data_version`` is a parameter rather than a constant because the cache-invalidation tests
    need to publish a *second* version for the same date (docs/06 §Caching keys every entry on it);
    they used to do that through a second, hand-written copy of this function, and the two drifted
    the moment this one started writing the step row docs/03 requires.
    """
    existing = (
        await session.execute(select(PipelineRun.id).where(PipelineRun.trade_date == trade_date))
    ).scalar_one_or_none()
    if existing is not None:
        return 0
    now = dt.datetime(trade_date.year, trade_date.month, trade_date.day, 14, 0, tzinfo=dt.UTC)
    run = PipelineRun(
        trade_date=trade_date,
        status="succeeded",
        started_at=now,
        finished_at=now,
        data_version=data_version,
    )
    session.add(run)
    await session.flush()
    # docs/03: "Every step writes a row in `pipeline_run_step`". A run row claiming to have
    # published with no record of publishing is precisely the inconsistency
    # `baskfy_api.integrity`'s `published_runs_have_steps` assertion exists to catch — and it
    # would catch this one, on a seeded database, for a reason that has nothing to do with a bad
    # backup. So the step is written, and its payload says plainly that it was seeded rather than
    # run: the fixture is `factor_daily` rows loaded directly, not a pipeline that executed.
    session.add(
        PipelineRunStep(
            run_id=run.id,
            step="publish",
            status="succeeded",
            rows_in=1,
            rows_out=1,
            duration_ms=0,
            error={
                "seeded": True,
                "detail": (
                    "written by `baskfy_api.seed`, not by a pipeline run; the reference export "
                    "is loaded into factor_daily directly (docs/13 §5)"
                ),
                "data_version": data_version,
            },
        )
    )
    return 1


async def seed_reference(session: AsyncSession) -> dict[str, int]:
    counts = await seed_curated_product_reference(session)
    counts.update(
        {
            "exchange": await seed_exchange(session),
            "index_def": await seed_universes(session),
            "plan": await seed_plans(session),
            "screen": await seed_example_screens(session),
        }
    )
    return counts


async def _run(command: str, database_url: str | None) -> dict[str, int]:
    async with session_scope(database_url) as session:
        counts: dict[str, int] = {}
        if command in ("all", "reference"):
            counts.update(await seed_reference(session))
        if command in ("all", "trading-days"):
            # The exchange row is a foreign key for trading_day.
            await seed_exchange(session)
            counts["trading_day"] = await seed_trading_days(session)
        if command in ("all", "fixture"):
            await seed_exchange(session)
            counts["factor_daily"] = await seed_reference_fixture(session)
        if command in ("all", "bars"):
            await seed_exchange(session)
            counts["ohlcv_daily"] = await seed_fixture_bars(session)
        if command in ("all", "market"):
            await seed_reference(session)
            counts["index_snapshot_daily"] = await seed_index_snapshots(session)
            counts["market_health_daily"] = await seed_market_health(session, to_rows().as_of)
        if command == "e2e":
            counts.update(await seed_reference(session))
            counts["trading_day"] = await seed_trading_days(session)
            counts["factor_daily"] = await seed_reference_fixture(session)
            # The factsheet's sparklines, own-history medians and regime distances all read bars.
            counts["ohlcv_daily"] = await seed_fixture_bars(session)
            # The dashboard, the breadth gauges and the listings register (Prompt 11).
            counts["index_snapshot_daily"] = await seed_index_snapshots(session)
            counts["market_health_daily"] = await seed_market_health(session, to_rows().as_of)
            counts["pipeline_run"] = await seed_published_run(session, to_rows().as_of)
            counts["app_user"] = await seed_e2e_account(session)
        return counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baskfy-seed", description=__doc__)
    parser.add_argument(
        "command",
        choices=("all", "reference", "trading-days", "fixture", "bars", "market", "e2e"),
        help="which seed set to apply",
    )
    parser.add_argument("--database-url", default=None, help="override BASKFY_DATABASE_URL")
    args = parser.parse_args(argv)

    counts = asyncio.run(_run(args.command, args.database_url))
    for table, count in counts.items():
        print(f"{table}: {count} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
