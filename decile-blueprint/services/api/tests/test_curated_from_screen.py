"""SB1 — ``POST /cb/baskets/from-screen``: a saved screen becomes an investable basket.

Two claims matter more than the rest, and both are about trust rather than arithmetic:

1. **The constituents come from running the screen, not from the request body.** The route accepts
   no symbols at all, so ``source = 'SCREEN'`` cannot be asserted by a caller. The test that
   guards this reads the signature rather than the behaviour, because the property is the *absence*
   of a field and no amount of exercising can demonstrate that.
2. **A basket is a catalog row.** No order, no broker, no execution import — the same gate
   ``POST /cb/baskets`` sits behind, decided in ``test_baskets_readonly.py``.

The sizing itself is spec'd in ``packages/core/tests/test_basket_sizing.py``; here we only check
that the route hands the core module what the investor chose and reports back what it returned.
"""

from __future__ import annotations

import datetime as dt
import inspect
from decimal import Decimal
from typing import Protocol
from unittest.mock import AsyncMock, MagicMock

import pytest

from baskfy_api.app import create_app
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.routers import curated_from_screen
from baskfy_api.routers.curated_from_screen import (
    CustomWeightIn,
    FromScreenIn,
    FromScreenOut,
    create_basket_from_screen,
)
from baskfy_core.basket_sizing import HoldingProfile, WeightMethod
from baskfy_core.screener import RankingFactor, ScreenResult, ScreenResultRow

SCREEN_PUBLIC_ID = "scr_momo"
AS_OF = dt.date(2026, 8, 21)


class _HasPrimaryKey(Protocol):
    """What ``AsyncSession.refresh`` fills in — see ``test_curated_create.py`` for why."""

    id: int | None


def _definition() -> dict[str, object]:
    """The smallest screen definition that validates, since the query never runs here."""
    return {"index": "nifty-500", "sort_by": "ret_12m"}


def _row(rank: int, symbol: str, price: str | None) -> ScreenResultRow:
    values: dict[str, object] = {
        "symbol": symbol,
        "name": symbol.title(),
        # Rank 1 is the strongest score and the calmest vol, so SCORE and INV_VOL
        # both put more in the first name when the tests ask for those methods.
        "sorting_factor": Decimal(100 - rank),
        "vol_12m": Decimal("0.10") + Decimal(rank) * Decimal("0.01"),
    }
    if price is not None:
        values["close_raw"] = Decimal(price)
    return ScreenResultRow(
        rank=rank,
        instrument_id=100 + rank,
        combined_rank=rank,
        ranks=(rank, rank, rank),
        values=values,
    )


def _screen_result(count: int, *, price: str | None = "500") -> ScreenResult:
    return ScreenResult(
        as_of=AS_OF,
        data_version=7,
        sorting_factor=RankingFactor(
            position=1, key="ret_12m", label="12m return", direction="desc"
        ),
        columns=("symbol", "name", "close_raw"),
        rows=tuple(_row(i + 1, f"SYM{i + 1}", price) for i in range(count)),
    )


def _screen(*, user_id: int | None, name: str = "My momentum screen") -> MagicMock:
    """A ``Screen`` row stand-in.

    ``name`` is assigned rather than passed to the constructor: ``MagicMock(name=...)`` sets the
    mock's own repr name and leaves ``.name`` a mock, which then fails Pydantic much later.
    """
    screen = MagicMock(id=5, public_id=SCREEN_PUBLIC_ID, user_id=user_id, definition=_definition())
    screen.name = name
    return screen


def _found(row: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=row)
    return result


def _instruments(count: int) -> MagicMock:
    rows = []
    for i in range(count):
        instrument = MagicMock(id=200 + i)
        instrument.symbol = f"SYM{i + 1}"
        rows.append(instrument)
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    return result


def _session(count: int) -> AsyncMock:
    """A session that resolves the screen, the instruments and the manager, and records writes."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()

    session.execute = AsyncMock(return_value=_found(_screen(user_id=9)))
    session.scalar = AsyncMock(return_value=11)  # the curated manager id
    session.scalars = AsyncMock(return_value=_instruments(count))

    async def _refresh(obj: _HasPrimaryKey) -> None:
        if obj.id is None:
            obj.id = 1

    session.refresh = AsyncMock(side_effect=_refresh)
    return session


@pytest.fixture
def screened(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stand in for the screener: 30 priced names, resolved as of a fixed date."""
    session = _session(30)

    async def _resolve(_session: object, requested: dt.date | None) -> object:
        return MagicMock(as_of=AS_OF, requested=requested)

    async def _version(_session: object) -> int:
        return 7

    async def _execute(_session: object, _definition: object, **_kwargs: object) -> ScreenResult:
        return _screen_result(30)

    async def _sole(_session: object, _uid: int | None) -> int:
        return 42

    async def _slug(_session: object, name: str) -> str:
        return name.lower().replace(" ", "-")

    monkeypatch.setattr(curated_from_screen, "resolve_as_of", _resolve)
    monkeypatch.setattr(curated_from_screen, "current_data_version", _version)
    monkeypatch.setattr(curated_from_screen, "execute_screen", _execute)
    monkeypatch.setattr(curated_from_screen, "scoped_sole_user_id", _sole)
    monkeypatch.setattr(curated_from_screen, "slug_for", _slug)
    return session


def _principal() -> MagicMock:
    return MagicMock(user_id=9)


def _entitlements() -> MagicMock:
    entitlements = MagicMock()
    entitlements.require_universe = MagicMock(return_value=None)
    return entitlements


async def _create(session: AsyncMock, body: FromScreenIn) -> FromScreenOut:
    return await create_basket_from_screen(body, session, _principal(), _entitlements())


class TestTheRouteIsServedAndDocumented:
    def test_openapi_exposes_the_path(self) -> None:
        paths = create_app().openapi()["paths"]
        assert "post" in paths["/api/v1/cb/baskets/from-screen"]

    def test_the_body_names_a_screen_and_never_a_holding(self) -> None:
        """The guarantee behind ``source = 'SCREEN'`` is a field that does not exist.

        If a future change adds symbols or weights to the request, the label stops meaning "this
        is what the rule returned" and starts meaning "this is what somebody posted". That is the
        whole reason this endpoint exists separately from ``POST /cb/baskets``.
        """
        fields = set(FromScreenIn.model_fields)
        assert "screen_public_id" in fields
        assert "custom_weights" in fields
        # `custom_weights` is allowed: the server still runs the screen and refuses extras.
        # A bare `weights` / `symbols` field would let the caller supply the basket.
        for forbidden in ("symbols", "constituents", "holdings_list", "weights"):
            assert forbidden not in fields

    def test_the_route_cannot_reach_execution(self) -> None:
        source = inspect.getsource(curated_from_screen)
        for forbidden in ("baskfy_execution", "OrderGateway", "place_order", "/execute"):
            assert forbidden not in source


class TestWhatGetsPersisted:
    @pytest.mark.asyncio
    async def test_a_screen_basket_is_private_and_labelled_screen(
        self, screened: AsyncMock
    ) -> None:
        result = await _create(
            screened,
            FromScreenIn(screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(100_000)),
        )
        assert result.visibility == "PRIVATE"
        assert result.type == "STOCK"
        assert result.source == "SCREEN"
        assert result.label == "GENESIS"
        assert result.version_no == 1
        assert result.screen_public_id == SCREEN_PUBLIC_ID
        assert result.as_of == AS_OF
        screened.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_the_basket_records_the_screen_it_was_cut_from(self, screened: AsyncMock) -> None:
        """Without ``source_screen_id`` the basket could never be re-cut from its own rule."""
        await _create(
            screened,
            FromScreenIn(screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(100_000)),
        )
        added = [call.args[0] for call in screened.add.call_args_list]
        baskets = [row for row in added if getattr(row, "source", None) == "SCREEN"]
        assert len(baskets) == 1
        assert baskets[0].source_screen_id == 5

    @pytest.mark.asyncio
    async def test_the_investor_can_rename_the_basket(self, screened: AsyncMock) -> None:
        result = await _create(
            screened,
            FromScreenIn(
                screen_public_id=SCREEN_PUBLIC_ID,
                amount=Decimal(100_000),
                name="High conviction 12",
            ),
        )
        assert result.name == "High conviction 12"

    @pytest.mark.asyncio
    async def test_it_falls_back_to_the_screen_name(self, screened: AsyncMock) -> None:
        result = await _create(
            screened,
            FromScreenIn(screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(100_000)),
        )
        assert result.name == "My momentum screen"


class TestSizing:
    @pytest.mark.asyncio
    async def test_the_default_profile_decides_the_count(self, screened: AsyncMock) -> None:
        result = await _create(
            screened,
            FromScreenIn(screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(100_000)),
        )
        assert len(result.holdings) == 20
        assert result.profile is HoldingProfile.BALANCED
        assert result.holdings_overridden is False

    @pytest.mark.asyncio
    async def test_an_aggressive_profile_concentrates(self, screened: AsyncMock) -> None:
        result = await _create(
            screened,
            FromScreenIn(
                screen_public_id=SCREEN_PUBLIC_ID,
                amount=Decimal(100_000),
                profile=HoldingProfile.AGGRESSIVE,
            ),
        )
        assert len(result.holdings) == 12

    @pytest.mark.asyncio
    async def test_an_explicit_count_beats_the_profile(self, screened: AsyncMock) -> None:
        """The point of the whole feature: the suggestion is a default, not a rule."""
        result = await _create(
            screened,
            FromScreenIn(
                screen_public_id=SCREEN_PUBLIC_ID,
                amount=Decimal(100_000),
                profile=HoldingProfile.CONSERVATIVE,
                holdings=7,
            ),
        )
        assert len(result.holdings) == 7
        assert result.holdings_overridden is True
        assert result.profile is None

    @pytest.mark.asyncio
    async def test_a_count_the_screen_cannot_fill_is_a_bad_request(
        self, screened: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _execute(*_args: object, **_kwargs: object) -> ScreenResult:
            return _screen_result(6)

        monkeypatch.setattr(curated_from_screen, "execute_screen", _execute)
        with pytest.raises(Problem):
            await _create(
                screened,
                FromScreenIn(
                    screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(100_000), holdings=25
                ),
            )

    @pytest.mark.asyncio
    async def test_a_thin_screen_trims_the_suggestion_silently(
        self, screened: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _execute(*_args: object, **_kwargs: object) -> ScreenResult:
            return _screen_result(6)

        monkeypatch.setattr(curated_from_screen, "execute_screen", _execute)
        screened.scalars = AsyncMock(return_value=_instruments(6))

        result = await _create(
            screened,
            FromScreenIn(screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(100_000)),
        )
        assert len(result.holdings) == 6

    @pytest.mark.asyncio
    async def test_the_money_reconciles_to_the_rupee(self, screened: AsyncMock) -> None:
        result = await _create(
            screened,
            FromScreenIn(screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(100_000)),
        )
        assert result.deployed + result.cash == result.amount
        assert sum(h.amount for h in result.holdings) == result.deployed

    @pytest.mark.asyncio
    async def test_a_defensive_exposure_tier_holds_more_cash(self, screened: AsyncMock) -> None:
        """R1-R4 set the cash share and nothing else — the count is the profile's job."""
        defensive = await _create(
            screened,
            FromScreenIn(
                screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(100_000), exposure_tier="R3"
            ),
        )
        full = await _create(
            screened,
            FromScreenIn(
                screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(100_000), exposure_tier="R1"
            ),
        )
        assert defensive.cash > full.cash
        assert len(defensive.holdings) == len(full.holdings)

    @pytest.mark.asyncio
    async def test_an_explicit_zero_cash_pct_deploys_everything(self, screened: AsyncMock) -> None:
        """SB7: the investor can opt out of a cash sleeve."""
        buffered = await _create(
            screened,
            FromScreenIn(screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(100_000)),
        )
        invested = await _create(
            screened,
            FromScreenIn(
                screen_public_id=SCREEN_PUBLIC_ID,
                amount=Decimal(100_000),
                cash_pct=Decimal(0),
            ),
        )
        assert buffered.cash_pct > Decimal(0)
        assert invested.cash_pct == Decimal(0)
        assert invested.deployed > buffered.deployed

    @pytest.mark.asyncio
    async def test_an_amount_too_small_is_reported_rather_than_refused(
        self, screened: AsyncMock
    ) -> None:
        """A basket nobody can fund is still saved, flagged, and left to the investor to fix."""
        result = await _create(
            screened,
            FromScreenIn(screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(1_000)),
        )
        assert result.minimum_amount is not None
        assert result.fundable is False


class TestWeightMethods:
    @pytest.mark.asyncio
    async def test_the_default_method_is_equal(self, screened: AsyncMock) -> None:
        result = await _create(
            screened,
            FromScreenIn(screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(100_000), holdings=5),
        )
        assert result.method is WeightMethod.EQUAL
        amounts = {row.amount for row in result.holdings}
        assert len(amounts) == 1

    @pytest.mark.asyncio
    async def test_rank_gives_the_best_name_more(self, screened: AsyncMock) -> None:
        result = await _create(
            screened,
            FromScreenIn(
                screen_public_id=SCREEN_PUBLIC_ID,
                amount=Decimal(100_000),
                holdings=4,
                method=WeightMethod.RANK,
            ),
        )
        assert result.method is WeightMethod.RANK
        assert result.holdings[0].amount > result.holdings[-1].amount
        assert result.holdings[0].symbol == "SYM1"

    @pytest.mark.asyncio
    async def test_score_follows_the_screen_factor(self, screened: AsyncMock) -> None:
        result = await _create(
            screened,
            FromScreenIn(
                screen_public_id=SCREEN_PUBLIC_ID,
                amount=Decimal(100_000),
                holdings=4,
                method=WeightMethod.SCORE,
            ),
        )
        assert result.holdings[0].amount > result.holdings[-1].amount

    @pytest.mark.asyncio
    async def test_custom_applies_the_investor_numbers_to_the_screens_names(
        self, screened: AsyncMock
    ) -> None:
        result = await _create(
            screened,
            FromScreenIn(
                screen_public_id=SCREEN_PUBLIC_ID,
                amount=Decimal(100_000),
                holdings=3,
                method=WeightMethod.CUSTOM,
                custom_weights=[
                    CustomWeightIn(symbol="SYM1", weight=Decimal(70)),
                    CustomWeightIn(symbol="SYM2", weight=Decimal(20)),
                    CustomWeightIn(symbol="SYM3", weight=Decimal(10)),
                ],
            ),
        )
        assert result.holdings[0].weight == Decimal("0.7000")
        assert result.holdings[1].weight == Decimal("0.2000")
        assert result.holdings[2].weight == Decimal("0.1000")

    @pytest.mark.asyncio
    async def test_custom_cannot_name_a_holding_the_screen_did_not_select(
        self, screened: AsyncMock
    ) -> None:
        with pytest.raises(Problem):
            await _create(
                screened,
                FromScreenIn(
                    screen_public_id=SCREEN_PUBLIC_ID,
                    amount=Decimal(100_000),
                    holdings=2,
                    method=WeightMethod.CUSTOM,
                    custom_weights=[
                        CustomWeightIn(symbol="SYM1", weight=Decimal(1)),
                        CustomWeightIn(symbol="SYM2", weight=Decimal(1)),
                        CustomWeightIn(symbol="FAKE", weight=Decimal(1)),
                    ],
                ),
            )

    @pytest.mark.asyncio
    async def test_custom_without_a_weight_for_a_chosen_name_is_refused(
        self, screened: AsyncMock
    ) -> None:
        with pytest.raises(Problem):
            await _create(
                screened,
                FromScreenIn(
                    screen_public_id=SCREEN_PUBLIC_ID,
                    amount=Decimal(100_000),
                    holdings=2,
                    method=WeightMethod.CUSTOM,
                    custom_weights=[CustomWeightIn(symbol="SYM1", weight=Decimal(1))],
                ),
            )


class TestWhoMaySave:
    @pytest.mark.asyncio
    async def test_an_example_screen_is_a_legitimate_source(self, screened: AsyncMock) -> None:
        """A first-time user has no screens of their own — the shipped ones must work."""
        screened.execute = AsyncMock(
            return_value=_found(_screen(user_id=None, name="Example screen"))
        )

        result = await _create(
            screened,
            FromScreenIn(screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(100_000)),
        )
        assert result.source == "SCREEN"

    @pytest.mark.asyncio
    async def test_somebody_elses_private_screen_is_reported_absent(
        self, screened: AsyncMock
    ) -> None:
        """404 rather than 403: the API does not confirm another user's screen id exists."""
        screened.execute = AsyncMock(return_value=_found(_screen(user_id=1234, name="Not yours")))

        with pytest.raises(Problem):
            await _create(
                screened,
                FromScreenIn(screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(100_000)),
            )

    @pytest.mark.asyncio
    async def test_a_missing_screen_is_not_found(self, screened: AsyncMock) -> None:
        screened.execute = AsyncMock(return_value=_found(None))

        with pytest.raises(Problem):
            await _create(
                screened, FromScreenIn(screen_public_id="scr_nope", amount=Decimal(100_000))
            )

    @pytest.mark.asyncio
    async def test_the_universe_entitlement_is_checked_before_any_work(
        self, screened: AsyncMock
    ) -> None:
        entitlements = _entitlements()
        entitlements.require_universe = MagicMock(
            side_effect=Problem(
                ProblemType.PAYMENT_REQUIRED, "this plan does not include nifty-500"
            )
        )
        with pytest.raises(Problem):
            await create_basket_from_screen(
                FromScreenIn(screen_public_id=SCREEN_PUBLIC_ID, amount=Decimal(100_000)),
                screened,
                _principal(),
                entitlements,
            )
        screened.commit.assert_not_awaited()
