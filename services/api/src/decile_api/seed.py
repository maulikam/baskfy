"""Seed reference data into an empty database (PROMPTS.md Prompt 1 deliverable 4 & 5b).

Every step is an idempotent upsert, so ``make seed`` can be re-run at any time and converges on
the same rows — the same rule docs/02 §"Non-negotiable engineering rules" #3 imposes on ingestion.

Usage:
    python -m decile_api.seed all
    python -m decile_api.seed reference          # exchange, universes, plans, example screens
    python -m decile_api.seed trading-days
    python -m decile_api.seed fixture            # the reference CSV export (docs/13)
    python -m decile_api.seed bars               # 3 years of bars from FixtureProvider
    python -m decile_api.seed market             # index snapshots + market-health breadth
    python -m decile_api.seed e2e                # everything the browser acceptance suite needs

``bars`` is the local-development dataset docs/03 §Environments describes: "100-instrument
sample, 3 years, seeded from fixtures ... no Kite calls, provider stubbed". It goes through
:class:`FixtureProvider`, so `make seed` exercises the same port the pipeline will.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Sequence
from typing import Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api.db import session_scope
from decile_api.security import hash_password
from decile_api.settings import get_settings
from decile_core.breadth import breadth_query
from decile_core.models import (
    AppUser,
    CorporateAction,
    Exchange,
    FactorDaily,
    IndexDef,
    IndexMemberDaily,
    IndexSnapshotDaily,
    Instrument,
    MarketHealthDaily,
    OhlcvDaily,
    PipelineRun,
    Plan,
    Screen,
    Subscription,
    TradingDay,
)
from decile_core.reference_export import ReferenceRows, to_rows
from decile_core.seed_data import (
    EXAMPLE_SCREENS,
    NSE_EXCHANGE_CODE,
    NSE_EXCHANGE_ID,
    PLANS,
    index_def_rows,
)
from decile_core.trading_calendar import build_calendar, default_calendar_range
from decile_core.universes import (
    FIRST_NON_UNIVERSE_INDEX_ID,
    MARKET_HEALTH_SLUGS,
    UNIVERSE_BY_SLUG,
    slugify_index,
)
from decile_providers.fixtures import FixtureProvider


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
    values = [
        {
            "id": plan.id,
            "code": plan.code,
            "price_inr": plan.price_inr,
            "interval": plan.interval,
            "features": plan.features,
        }
        for plan in PLANS
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
    """
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
    ``decile_worker.tasks.snapshots`` does nightly, and for the same reason: the 14 selectable
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

    Runs ``decile_core.breadth.breadth_query`` — the same statement the nightly step executes —
    rather than a second copy of the arithmetic. Prompt 11's first acceptance criterion checks
    that arithmetic against a hand-computed fixture, and a seed with its own version of it would
    make the check meaningless.
    """
    written = 0
    for slug in MARKET_HEALTH_SLUGS:
        universe = UNIVERSE_BY_SLUG[slug]
        constituents, above_200, above_50, near_ath, positive_1y = (
            await session.execute(breadth_query(universe.index_id, on))
        ).one()
        stmt = insert(MarketHealthDaily).values(
            index_id=universe.index_id,
            date=on,
            pct_above_200dma=above_200,
            pct_above_50dma=above_50,
            pct_within_10pct_ath=near_ath,
            pct_ret_1y_positive=positive_1y,
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


async def seed_published_run(session: AsyncSession, trade_date: dt.date) -> int:
    """A published `pipeline_run`, without which `resolve_as_of` refuses to serve any date.

    docs/06 §step 1 defines the as-of as the latest date belonging to a *published* run, and the
    reference fixture seeds `factor_daily` directly — so the run row has to be written too, or the
    API answers 503 for every request (docs/07a §8).
    """
    existing = (
        await session.execute(select(PipelineRun.id).where(PipelineRun.trade_date == trade_date))
    ).scalar_one_or_none()
    if existing is not None:
        return 0
    now = dt.datetime(trade_date.year, trade_date.month, trade_date.day, 14, 0, tzinfo=dt.UTC)
    session.add(
        PipelineRun(
            trade_date=trade_date,
            status="succeeded",
            started_at=now,
            finished_at=now,
            data_version=1,
        )
    )
    return 1


async def seed_reference(session: AsyncSession) -> dict[str, int]:
    return {
        "exchange": await seed_exchange(session),
        "index_def": await seed_universes(session),
        "plan": await seed_plans(session),
        "screen": await seed_example_screens(session),
    }


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
    parser = argparse.ArgumentParser(prog="decile-seed", description=__doc__)
    parser.add_argument(
        "command",
        choices=("all", "reference", "trading-days", "fixture", "bars", "market", "e2e"),
        help="which seed set to apply",
    )
    parser.add_argument("--database-url", default=None, help="override DECILE_DATABASE_URL")
    args = parser.parse_args(argv)

    counts = asyncio.run(_run(args.command, args.database_url))
    for table, count in counts.items():
        print(f"{table}: {count} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
