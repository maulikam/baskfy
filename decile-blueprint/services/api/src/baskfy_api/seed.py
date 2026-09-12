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
    python -m baskfy_api.seed swing              # sw_config for the sole tenant (SW2)
    python -m baskfy_api.seed vbt                # vb_config for the sole tenant (VB3)
    python -m baskfy_api.seed twt                # tw_config for the sole tenant (TW3)
    python -m baskfy_api.seed swing --capital 2500000 --risk 0.5   # ...and set the sleeve (SW13)
    python -m baskfy_api.seed twt --capital 2500000   # ...and fund the tight sleeve (TW11)
    python -m baskfy_api.seed e2e                # everything the browser acceptance suite needs

``bars`` is the local-development dataset docs/03 §Environments describes: "100-instrument
sample, 3 years, seeded from fixtures ... no Kite calls, provider stubbed". It goes through
:class:`FixtureProvider`, so `make seed` exercises the same port the pipeline will.
"""

from __future__ import annotations

from pathlib import Path

import argparse
import asyncio
import datetime as dt
import os
import sys
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.curated_catalogue import seed_catalogue
from baskfy_api.curated_seed import (
    seed_curated_collections,
    seed_curated_managers,
    seed_momentum_scan_basket,
)
from baskfy_api.db import session_scope
from baskfy_api.security import hash_password
from baskfy_api.settings import get_settings
from baskfy_api.swing_settings import SwingCeilings, SwingConfigPatch, apply_patch
from baskfy_api.twt_settings import TwtCeilings, TwtConfigPatch
from baskfy_api.twt_settings import apply_patch as apply_patch_twt
from baskfy_core.breadth import breadth_query
from baskfy_core.models import (
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
    PipelineRunStep,
    Plan,
    Screen,
    Subscription,
    SwConfig,
    TradingDay,
    TwConfig,
    VbConfig,
)
from baskfy_core.reference_export import ReferenceRows, read_export, to_rows
from baskfy_core.seed_data import (
    ALL_PLANS,
    EXAMPLE_SCREENS,
    NSE_EXCHANGE_CODE,
    NSE_EXCHANGE_ID,
    PLANS,
    index_def_rows,
)
from importlib import resources
from baskfy_core.trading_calendar import (
    HOLIDAY_FILE,
    build_calendar,
    default_calendar_range,
    parse_seed_holidays,
)
from baskfy_core.universes import (
    FIRST_NON_UNIVERSE_INDEX_ID,
    MARKET_HEALTH_SLUGS,
    UNIVERSE_BY_SLUG,
    slugify_index,
)
from baskfy_providers.fixtures import FixtureProvider



_REPO_ROOT = Path(__file__).resolve().parents[4]
_REFERENCE_EXPORT = _REPO_ROOT / "tests" / "fixtures" / "reference-screen-export-2026-08-18.csv"


def _reference_rows() -> ReferenceRows:
    return to_rows(read_export(_REFERENCE_EXPORT))


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
        for row in build_calendar(
            start,
            end,
            parse_seed_holidays(
                resources.files("baskfy_core.data").joinpath(HOLIDAY_FILE).read_text(encoding="utf-8")
            ),
        )
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
    data = rows if rows is not None else _reference_rows()

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
            # 0039: `series` left the instrument's key — it is an attribute NSE changes, not
            # part of a listing's identity.
            index_elements=[Instrument.exchange_id, Instrument.symbol],
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
            # 0039: `series` left the instrument's key — it is an attribute NSE changes, not
            # part of a listing's identity.
            index_elements=[Instrument.exchange_id, Instrument.symbol],
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
    last = end or _reference_rows().as_of
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


async def seed_swing_config(session: AsyncSession) -> int:
    """One ``sw_config`` row for the sole tenant, with ``sleeve_capital_inr = 0`` (SW2).

    **Zero capital is the point, not an oversight.** ``docs/swing/02-scope-and-gating.md`` §3.4
    makes writing the sleeve's capital one of the five conditions on the real-money flag, and
    ``baskfy_core.swing.sizing`` refuses every entry with ``NO_EQUITY`` while equity is zero. So a
    freshly seeded database has a swing book that detects, watches, journals and plans **nothing
    to buy** until a person decides what it may risk. Any other default would mean the number a
    trade was sized against was one this seeder chose.

    Idempotent, like every other step here: re-running converges, and re-running never resets a
    capital or a risk setting a person has already chosen. ``ON CONFLICT DO NOTHING`` rather than
    ``DO UPDATE`` is what makes that true — this is the row's *creation*, not its management.

    Seeded rather than written by migration ``0028_swing`` because the user id comes from
    ``BASKFY_SOLE_USER_ID``, and an environment variable does not belong in schema history.
    """
    user_id = await _sole_user_id(session)
    if user_id is None:
        return 0
    statement = insert(SwConfig).values(user_id=user_id, updated_by="seed")
    await session.execute(statement.on_conflict_do_nothing(index_elements=[SwConfig.user_id]))
    return 1


async def seed_vbt_config(session: AsyncSession) -> int:
    """One ``vb_config`` row for the sole tenant, with ``sleeve_capital_inr = 0`` (VB3).

    **Zero capital is the point, not an oversight**, and for the same reason it is for the swing
    book above: ``docs/vbt/02-scope-and-gating.md`` §3.4 makes writing this sleeve's capital one
    of the five conditions on the real-money flag, and ``baskfy_core.vbt.sizing`` refuses every
    entry with ``NO_SLEEVE_CAPITAL`` while equity is zero. So a freshly seeded database has a
    volume-breakout sleeve that detects, ranks, stores and plans **nothing to buy** until a person
    decides what it may risk. Any other default would mean the number a trade was sized against
    was one this seeder chose.

    Idempotent: ``ON CONFLICT DO NOTHING`` rather than ``DO UPDATE``, because this is the row's
    *creation* and not its management. Re-running never resets a capital or a stop a person has
    already chosen.

    Seeded rather than written by migration ``0037_vbt`` because the user id comes from
    ``BASKFY_SOLE_USER_ID``, and an environment variable does not belong in schema history.
    """
    user_id = await _sole_user_id(session)
    if user_id is None:
        return 0
    statement = insert(VbConfig).values(user_id=user_id, updated_by="seed")
    await session.execute(statement.on_conflict_do_nothing(index_elements=[VbConfig.user_id]))
    return 1


async def seed_twt_config(session: AsyncSession) -> int:
    """One ``tw_config`` row for the sole tenant, with ``sleeve_capital_inr = 0`` (TW3).

    **Zero capital is the point, not an oversight**, and for the same reason it is for the swing
    book and VBT-1 above. ``docs/twt/04-business-rules.md`` §9.3: *"A sleeve at ₹0 plans nothing:
    every signal is skipped ``NO_SLEEVE_CAPITAL``."* So a freshly seeded database has a
    three-weeks-tight sleeve that detects, ranks, stores and plans **nothing to buy** until a
    person decides what it may risk.

    **And on this sleeve the zero is a safety rail with a name on it.** ``docs/twt/02`` §3 and the
    root ``CLAUDE.md`` both say it in the same words: *an agent never sets
    ``tw_config.sleeve_capital_inr``.* Maulik enters the capital himself on the first live
    morning. ``sleeve_capital_inr`` is therefore written here **explicitly as zero** rather than
    left to the column's server default — not because the default would be wrong, but because
    the one number this function must never get wrong should be visible in it.

    The other five defaults are the column defaults, which are ``docs/twt/04``'s: ten slots,
    12.5 % per position, a 20 % stop, a 20 % trail, ten first-live entries.

    Idempotent: ``ON CONFLICT DO NOTHING`` rather than ``DO UPDATE``, because this is the row's
    *creation* and not its management (house rule 7). Re-running never resets a capital, a stop
    or a trail a person has already chosen — and on this sleeve resetting the trail would rearm
    every stop in the book at a different level.

    Seeded rather than written by migration ``0041_twt`` because the user id comes from
    ``BASKFY_SOLE_USER_ID``, and an environment variable does not belong in schema history.
    """
    user_id = await _sole_user_id(session)
    if user_id is None:
        return 0
    statement = insert(TwConfig).values(
        user_id=user_id, sleeve_capital_inr=Decimal("0"), updated_by="seed"
    )
    await session.execute(statement.on_conflict_do_nothing(index_elements=[TwConfig.user_id]))
    return 1


async def set_swing_sleeve(
    session: AsyncSession,
    *,
    capital_inr: Decimal | None = None,
    risk_pct: Decimal | None = None,
    changed_by: str = "seed",
) -> int:
    """Write the sleeve's capital and/or risk per trade for the sole tenant (SW13, MD1/MD2).

    The deploy has no session token, so this is ``PATCH /swing/config`` reached from the box
    instead of from a browser — the **same** :func:`apply_patch`: the server ceilings are checked
    first (a risk above ``BASKFY_SWING_RISK_PER_TRADE_PCT_MAX`` is refused, nothing written), and
    every field that moves leaves its ``sw_config_audit`` row. Idempotent the way the settings
    form is: a value already in force changes nothing and audits nothing, so running the deploy
    twice does not write two rows.

    Returns 1 when the row exists (whether or not anything moved), 0 when there is no
    ``sw_config`` row yet — the case :func:`seed_swing_config` reports the same way, because no
    account exists to key it on. It never creates the row: ``seed_swing_config`` owns creation.
    """
    user_id = await _sole_user_id(session)
    if user_id is None:
        return 0
    # Round at write time (house rule 8) to the columns' own scales — MONEY is 2 dp, RISK_PCT is
    # 3 dp — before the patch is compared with the row. `apply_patch` decides "moved" on the
    # string form, so `0.5` against a stored `0.500` would otherwise audit a change every deploy.
    patch = SwingConfigPatch(
        sleeve_capital_inr=None if capital_inr is None else capital_inr.quantize(Decimal("0.01")),
        risk_per_trade_pct=None if risk_pct is None else risk_pct.quantize(Decimal("0.001")),
    )
    if not patch.changes():
        return 1
    await apply_patch(
        session,
        user_id=user_id,
        patch=patch,
        ceilings=SwingCeilings.from_settings(get_settings()),
        changed_by=changed_by,
        now=dt.datetime.now(tz=dt.UTC),
        note="baskfy_api.seed swing",
    )
    return 1


async def set_twt_sleeve(
    session: AsyncSession,
    *,
    capital_inr: Decimal | None = None,
    changed_by: str = "seed",
) -> int:
    """Write the three-weeks-tight sleeve's capital for the sole tenant (TW11, `NEEDS-MAULIK` T3).

    **This function does not decide the number and never supplies one.** It is the place a person
    types one into, which until now did not exist: `NEEDS-MAULIK.md` T3 recorded that the sleeve's
    capital is "your keystroke" and that there was nowhere to put it — no `PATCH
    /api/v1/twt/config`, no `me/twt` form, and a `--capital` flag that `seed.py` refused for every
    command but `swing`. The only thing that worked was a direct `UPDATE` on `tw_config`, which is
    the worse option precisely because it bypasses this path: no `tw_config_audit` row, no author,
    no trail, on a sleeve whose whole design is that a person decided each number.

    So this is the swing book's :func:`set_swing_sleeve` for `tw_config`, and it is the *same*
    :func:`twt_settings.apply_patch` the settings form would use — the engine's bounds first (a
    negative capital is a 400 before anything is written), then the server ceilings, then the row,
    then one audit row per field that actually moved.

    Idempotent the way the form is: a capital already in force changes nothing and audits nothing,
    so a deploy that runs twice does not write two rows. `apply_patch` decides "moved" on the
    string form, so the value is quantised to the column's own 2 dp first (house rule 8) — without
    it, `2500000` against a stored `2500000.00` would audit a change every single deploy.

    Returns 1 when the row exists (whether or not anything moved) and 0 when there is no
    `tw_config` row yet. It never creates the row: :func:`seed_twt_config` owns creation, and owns
    the ₹0 that creation writes.
    """
    user_id = await _sole_user_id(session)
    if user_id is None:
        return 0
    patch = TwtConfigPatch(
        sleeve_capital_inr=None if capital_inr is None else capital_inr.quantize(Decimal("0.01"))
    )
    if not patch.changes():
        return 1
    await apply_patch_twt(
        session,
        user_id=user_id,
        patch=patch,
        ceilings=TwtCeilings.from_settings(get_settings()),
        changed_by=changed_by,
        now=dt.datetime.now(tz=dt.UTC),
        note="baskfy_api.seed twt",
    )
    return 1


async def _sole_user_id(session: AsyncSession) -> int | None:
    """The tenant ``sw_config`` belongs to, or ``None`` when no account exists yet.

    ``None`` rather than an exception: ``make seed`` runs against a database that may have no
    ``app_user`` at all (the ``reference`` set does not create one), and a seeder that fails
    there would make the swing schema look broken when it is merely unpopulated. The row is
    created by the next ``seed`` once an account exists.
    """
    configured = os.environ.get("BASKFY_SOLE_USER_ID")
    if configured is not None:
        exists = (
            await session.execute(select(AppUser.id).where(AppUser.id == int(configured)))
        ).scalar_one_or_none()
        return exists
    return (
        await session.execute(select(AppUser.id).order_by(AppUser.id).limit(1))
    ).scalar_one_or_none()


async def seed_reference(session: AsyncSession) -> dict[str, int]:
    return {
        "exchange": await seed_exchange(session),
        "index_def": await seed_universes(session),
        "plan": await seed_plans(session),
        "screen": await seed_example_screens(session),
        "cb_manager": await seed_curated_managers(session),
        # Returns 0 until instruments for the fixture ranking exist (after fixture/bars).
        "cb_momentum_scan": await seed_momentum_scan_basket(session),
        # The published shelf. Each entry re-runs its own screen against real bars, so this is 0
        # on a database with no price history and fills in once a backfill has run.
        "cb_catalogue": await seed_catalogue(session),
        # Last, and deliberately so: membership is a predicate over the baskets that exist, so a
        # shelf seeded before its baskets would come out empty and stay empty until the next run.
        "cb_collection": await seed_curated_collections(session),
    }


async def _run(
    command: str,
    database_url: str | None,
    *,
    capital: Decimal | None = None,
    risk: Decimal | None = None,
) -> dict[str, int]:
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
            # Ranked from the reference export, exactly as the `e2e` branch below does and for
            # the same reason — this line was the half of M46.6 that never got fixed.
            #
            # The default `scan_projection.FIXTURE_SCAN_SYMBOLS` is fifteen large caps (RELIANCE,
            # TCS, INFY, …), none of which is in the 271-row export. `seed_momentum_scan_basket`
            # returns 0 when fewer than `top_n` of its ranked symbols exist, so `make seed` — the
            # command the deploy runbook now tells you to run — produced **four collections and no
            # baskets**, and every shelf rendered as an empty box. The e2e database was fixed in
            # M46.6; staging, which runs `all`, was not. `docs/DECISIONS-MERGE.md` M48.
            counts["cb_momentum_scan"] = await seed_momentum_scan_basket(
                session, ranked_symbols=tuple(row.symbol for row in _reference_rows().instruments)
            )
        if command in ("all", "bars"):
            await seed_exchange(session)
            counts["ohlcv_daily"] = await seed_fixture_bars(session)
        if command in ("all", "swing"):
            # After the account sets above, so the sole tenant exists to key the row on.
            counts["sw_config"] = await seed_swing_config(session)
            if capital is not None or risk is not None:
                counts["sw_config_sleeve"] = await set_swing_sleeve(
                    session, capital_inr=capital, risk_pct=risk
                )
        if command in ("all", "vbt"):
            # Same placement and the same reason as the swing row above.
            counts["vb_config"] = await seed_vbt_config(session)
        if command in ("all", "twt"):
            # Same placement and the same reason again.
            counts["tw_config"] = await seed_twt_config(session)
            # TW11: and the same shape as the swing pair above — creation seeds ₹0, and only an
            # explicit `--capital` funds the sleeve, through the audited settings path.
            if capital is not None:
                counts["tw_config_sleeve"] = await set_twt_sleeve(session, capital_inr=capital)
        if command in ("all", "market"):
            await seed_reference(session)
            counts["index_snapshot_daily"] = await seed_index_snapshots(session)
            counts["market_health_daily"] = await seed_market_health(session, _reference_rows().as_of)
        if command == "e2e":
            counts.update(await seed_reference(session))
            counts["trading_day"] = await seed_trading_days(session)
            counts["factor_daily"] = await seed_reference_fixture(session)
            # The factsheet's sparklines, own-history medians and regime distances all read bars.
            counts["ohlcv_daily"] = await seed_fixture_bars(session)
            # Ranked from the reference export rather than from
            # `scan_projection.FIXTURE_SCAN_SYMBOLS`, because none of those fifteen large caps
            # (RELIANCE, TCS, INFY, …) is in the 271-row export — so the seeder took its
            # "fewer than top_n symbols exist" branch and the e2e database has, until now,
            # contained **no basket at all**. Every catalog surface in the browser suite was
            # therefore being exercised against an empty catalog. The export is already sorted by
            # AVERAGE SHARPE RETURN 12/6/3/1 desc (docs/13), which is a momentum ranking, so its
            # head is the honest input for a basket called Momentum Scan.
            # `docs/DECISIONS-MERGE.md` M46.6.
            counts["cb_momentum_scan"] = await seed_momentum_scan_basket(
                session, ranked_symbols=tuple(row.symbol for row in _reference_rows().instruments)
            )
            # The dashboard, the breadth gauges and the listings register (Prompt 11).
            counts["index_snapshot_daily"] = await seed_index_snapshots(session)
            counts["market_health_daily"] = await seed_market_health(session, _reference_rows().as_of)
            counts["pipeline_run"] = await seed_published_run(session, _reference_rows().as_of)
            counts["app_user"] = await seed_e2e_account(session)
            # Last: they need the account the line above creates.
            counts["sw_config"] = await seed_swing_config(session)
            counts["vb_config"] = await seed_vbt_config(session)
            counts["tw_config"] = await seed_twt_config(session)
        return counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baskfy-seed", description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "all",
            "reference",
            "trading-days",
            "fixture",
            "bars",
            "market",
            "swing",
            "vbt",
            "twt",
            "e2e",
        ),
        help="which seed set to apply",
    )
    parser.add_argument("--database-url", default=None, help="override BASKFY_DATABASE_URL")
    # `swing` only: the sleeve's capital and risk per trade, through the settings write path with
    # its ceilings and its audit — the deploy's stand-in for PATCH /swing/config (SW13).
    parser.add_argument(
        "--capital",
        type=Decimal,
        default=None,
        help="swing|twt: sleeve_capital_inr, e.g. 2500000",
    )
    parser.add_argument(
        "--risk", type=Decimal, default=None, help="swing: risk_per_trade_pct, e.g. 0.5"
    )
    args = parser.parse_args(argv)
    # TW11: `--capital` now reaches the three-weeks-tight sleeve too. It was refused here for
    # every command but `swing`, which is why `NEEDS-MAULIK.md` T3 could say the sleeve's capital
    # was "your keystroke" and, in the same entry, that there was nowhere to type it. `--risk`
    # stays swing-only because `tw_config` has no risk-per-trade column: this book sizes by slot
    # (`max_position_pct`), not by stop distance.
    if args.capital is not None and args.command not in ("swing", "twt"):
        parser.error("--capital applies to the `swing` and `twt` commands only")
    if args.risk is not None and args.command != "swing":
        parser.error("--risk applies to the `swing` command only")

    counts = asyncio.run(
        _run(args.command, args.database_url, capital=args.capital, risk=args.risk)
    )
    for table, count in counts.items():
        print(f"{table}: {count} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
