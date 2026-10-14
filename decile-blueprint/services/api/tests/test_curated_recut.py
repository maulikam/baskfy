"""``POST /cb/baskets/{slug}/recut`` — re-cut a screen basket on demand (Maulik, 14 Oct 2026).

The spec, from his choice in session: re-run the screen the basket was saved from, **show what
was added and removed**, and write the next version **only when he confirms**. So the claims:

1. A preview (``confirm=false``) writes nothing.
2. A confirmed re-cut writes version N+1, labelled CHANGED, with the added/removed counts.
3. A re-cut that changes nothing is never written — a double click cannot stack versions.
4. The constituents come from the screen, not the body (the body has no holdings field), and the
   route has no order path.
5. Only a basket saved from a screen can be re-cut.
"""

from __future__ import annotations

import datetime as dt
import inspect
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from baskfy_api.app import create_app
from baskfy_api.problems import Problem
from baskfy_api.routers import curated_from_screen
from baskfy_api.routers.curated_from_screen import RecutIn, RecutOut, recut_basket_from_screen
from baskfy_core.basket_sizing import WeightMethod
from baskfy_core.models import CbBasketVersion, CbConstituent
from baskfy_core.screener import RankingFactor, ScreenResult, ScreenResultRow

AS_OF = dt.date(2026, 10, 14)
QUARTER = Decimal("0.2500")


def _row(rank: int, symbol: str) -> ScreenResultRow:
    return ScreenResultRow(
        rank=rank,
        instrument_id=100 + rank,
        combined_rank=rank,
        ranks=(rank, rank, rank),
        values={"symbol": symbol, "close_raw": Decimal(500), "vol_12m": Decimal("0.2")},
    )


def _result(symbols: list[str]) -> ScreenResult:
    return ScreenResult(
        as_of=AS_OF,
        data_version=7,
        sorting_factor=RankingFactor(
            position=1, key="ret_12m", label="12m return", direction="desc"
        ),
        columns=("symbol", "close_raw"),
        rows=tuple(_row(i + 1, symbol) for i, symbol in enumerate(symbols)),
    )


def _basket(*, source: str = "SCREEN", screen_id: int | None = 5) -> MagicMock:
    return MagicMock(id=1, slug="maulik", source=source, source_screen_id=screen_id)


def _screen() -> MagicMock:
    return MagicMock(
        id=5,
        public_id="scr_momo",
        user_id=None,
        definition={"index": "nifty-500", "sort_by": "ret_12m"},
    )


def _scalar(row: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=row)
    return result


def _rows(rows: list[tuple[str, Decimal]]) -> MagicMock:
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    return result


def _instruments(symbols: list[str]) -> MagicMock:
    rows = []
    for i, symbol in enumerate(symbols):
        instrument = MagicMock(id=200 + i)
        instrument.symbol = symbol
        rows.append(instrument)
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    return result


def _session(
    *,
    basket: MagicMock,
    current_names: list[str],
    screen_names: list[str],
    notes: str = "Cut from screen 'scr_momo' as of 2026-10-01, EQUAL weights.",
) -> AsyncMock:
    """Basket, its current version and that version's names, in the order the route reads them."""
    current = MagicMock(id=3, version_no=1, notes_md=notes)
    weight = (Decimal(1) / len(current_names)).quantize(Decimal("0.0001"))
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.get = AsyncMock(return_value=_screen())
    session.execute = AsyncMock(
        side_effect=[
            _scalar(basket),
            _scalar(current),
            _rows([(name, weight) for name in current_names]),
        ]
    )
    session.scalars = AsyncMock(return_value=_instruments(screen_names))
    return session


@pytest.fixture
def screen_returns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What the screen returns *now*; a test mutates this list before calling the route."""
    names: list[str] = []

    async def _resolve(_session: object, requested: dt.date | None) -> object:
        return MagicMock(as_of=AS_OF, requested=requested)

    async def _version(_session: object) -> int:
        return 7

    async def _execute(_session: object, _definition: object, **_kwargs: object) -> ScreenResult:
        return _result(names)

    async def _sole(_session: object, _uid: int | None) -> int:
        return 9

    monkeypatch.setattr(curated_from_screen, "resolve_as_of", _resolve)
    monkeypatch.setattr(curated_from_screen, "current_data_version", _version)
    monkeypatch.setattr(curated_from_screen, "execute_screen", _execute)
    monkeypatch.setattr(curated_from_screen, "scoped_sole_user_id", _sole)
    return names


def _entitlements() -> MagicMock:
    entitlements = MagicMock()
    entitlements.require_universe = MagicMock(return_value=None)
    return entitlements


async def _recut(session: AsyncMock, body: RecutIn) -> RecutOut:
    return await recut_basket_from_screen(
        "maulik", body, session, MagicMock(user_id=9), _entitlements()
    )


def _written(session: AsyncMock, kind: type) -> list[object]:
    return [call.args[0] for call in session.add.call_args_list if isinstance(call.args[0], kind)]


class TestTheRoute:
    def test_openapi_exposes_the_path(self) -> None:
        paths = create_app().openapi()["paths"]
        assert "post" in paths["/api/v1/cb/baskets/{slug}/recut"]

    def test_the_body_names_no_holding(self) -> None:
        for forbidden in ("symbols", "constituents", "weights", "custom_weights"):
            assert forbidden not in RecutIn.model_fields

    def test_the_route_cannot_reach_execution(self) -> None:
        source = inspect.getsource(curated_from_screen)
        for forbidden in ("baskfy_execution", "OrderGateway", "place_order", "/execute"):
            assert forbidden not in source


class TestPreviewThenConfirm:
    async def test_a_preview_names_the_changes_and_writes_nothing(
        self, screen_returns: list[str]
    ) -> None:
        screen_returns[:] = ["AAA", "BBB", "EEE", "FFF", "GGG"]
        session = _session(
            basket=_basket(),
            current_names=["AAA", "BBB", "CCC", "DDD"],
            screen_names=screen_returns,
        )
        out = await _recut(session, RecutIn())

        # The current version held four names, so the re-cut keeps four.
        assert [h.symbol for h in out.holdings] == ["AAA", "BBB", "EEE", "FFF"]
        assert out.added == ["EEE", "FFF"]
        assert out.removed == ["CCC", "DDD"]
        assert out.kept == ["AAA", "BBB"]
        assert out.changed is True
        assert out.written is False
        assert out.version_no is None
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    async def test_confirm_writes_the_next_version(self, screen_returns: list[str]) -> None:
        screen_returns[:] = ["AAA", "BBB", "EEE", "FFF"]
        session = _session(
            basket=_basket(),
            current_names=["AAA", "BBB", "CCC", "DDD"],
            screen_names=screen_returns,
        )
        out = await _recut(session, RecutIn(confirm=True))

        assert out.written is True
        assert out.version_no == 2
        (version,) = _written(session, CbBasketVersion)
        assert isinstance(version, CbBasketVersion)
        assert version.version_no == 2
        assert version.label == "CHANGED"
        assert (version.added_count, version.removed_count) == (2, 2)
        assert version.effective_date == AS_OF
        constituents = _written(session, CbConstituent)
        assert len(constituents) == 4
        assert sum(c.weight for c in constituents if isinstance(c, CbConstituent)) == Decimal(1)
        session.commit.assert_awaited_once()

    async def test_an_unchanged_cut_is_never_written(self, screen_returns: list[str]) -> None:
        screen_returns[:] = ["AAA", "BBB", "CCC", "DDD"]
        session = _session(
            basket=_basket(),
            current_names=["AAA", "BBB", "CCC", "DDD"],
            screen_names=screen_returns,
        )
        out = await _recut(session, RecutIn(confirm=True))

        assert out.changed is False
        assert out.written is False
        assert (out.added, out.removed) == ([], [])
        session.add.assert_not_called()
        assert all(h.previous_weight == QUARTER for h in out.holdings)


class TestTheCutIsCarriedForward:
    async def test_the_method_comes_from_the_current_version(
        self, screen_returns: list[str]
    ) -> None:
        screen_returns[:] = ["AAA", "BBB", "CCC"]
        session = _session(
            basket=_basket(),
            current_names=["AAA", "BBB", "CCC"],
            screen_names=screen_returns,
            notes="Cut from screen 'scr_momo' as of 2026-10-01, RANK weights.",
        )
        out = await _recut(session, RecutIn())
        assert out.method is WeightMethod.RANK
        assert out.method_fell_back is False

    async def test_custom_weights_fall_back_to_equal_and_say_so(
        self, screen_returns: list[str]
    ) -> None:
        screen_returns[:] = ["AAA", "BBB", "CCC"]
        session = _session(
            basket=_basket(),
            current_names=["AAA", "BBB", "CCC"],
            screen_names=screen_returns,
            notes="Cut from screen 'scr_momo' as of 2026-10-01, CUSTOM weights.",
        )
        out = await _recut(session, RecutIn())
        assert out.method is WeightMethod.EQUAL
        assert out.method_fell_back is True

    async def test_a_screen_that_shrank_is_trimmed_not_refused(
        self, screen_returns: list[str]
    ) -> None:
        screen_returns[:] = ["AAA", "BBB"]
        session = _session(
            basket=_basket(),
            current_names=["AAA", "BBB", "CCC", "DDD"],
            screen_names=screen_returns,
        )
        out = await _recut(session, RecutIn())
        assert [h.symbol for h in out.holdings] == ["AAA", "BBB"]
        assert out.removed == ["CCC", "DDD"]


class TestOnlyScreenBaskets:
    @pytest.mark.parametrize(
        "basket",
        [_basket(source="MANUAL"), _basket(source="SCAN"), _basket(screen_id=None)],
    )
    async def test_refused(self, screen_returns: list[str], basket: MagicMock) -> None:
        screen_returns[:] = ["AAA", "BBB"]
        session = _session(basket=basket, current_names=["AAA", "BBB"], screen_names=["AAA"])
        with pytest.raises(Problem):
            await _recut(session, RecutIn(confirm=True))
        session.add.assert_not_called()
