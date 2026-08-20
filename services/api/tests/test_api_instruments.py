"""Contract tests for ``/instruments`` — docs/07 §Instruments, docs/01 §5 (Prompt 10).

Acceptance criteria 1 and 2 live here:

* the PROS list for the CUPID fixture reproduces the reference product's six observed lines, and
* ``away_high_*`` and the returns block match `docs/05` to 2 dp.

The third criterion — an instrument with less than a year of history shows '—', never 0 — is
asserted on both sides: the payload must carry ``null`` (here) and the page must render an em
dash (`apps/web/src/components/instrument/__tests__`).
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Final

import httpx
import pytest
from api_helpers import assert_problem, url
from screener_helpers import AS_OF, add_factor_row, add_instrument, requires_db
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from decile_core.models import CorporateAction, IndexMemberDaily, Instrument, OhlcvDaily
from decile_core.pros_cons import RULES
from decile_core.universes import UNIVERSE_BY_SLUG
from decile_providers.fixtures import FixtureProvider

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

#: A decoded JSON object. `object` rather than `Any` — CLAUDE.md house rule 3.
#:
#: Which means every read out of one has to narrow. The four helpers below are that narrowing, in
#: one place: an assertion that the shape is what the schema promises, and a typed value out.
#: A response that has drifted fails on the assertion rather than on an attribute error twenty
#: lines later.
JsonMap = dict[str, object]


def as_map(value: object) -> JsonMap:
    assert isinstance(value, dict), value
    return value


def as_rows(value: object) -> list[JsonMap]:
    assert isinstance(value, list), value
    for item in value:
        assert isinstance(item, dict), item
    return value


def as_strings(value: object) -> list[str]:
    assert isinstance(value, list), value
    for item in value:
        assert isinstance(item, str), item
    return value


def number(value: object) -> Decimal:
    """One numeric cell as an exact decimal. `float` would lose the stored precision."""
    assert isinstance(value, (Decimal, int, str)), value
    return Decimal(value)


def labels(block: object) -> list[str]:
    return [str(row["label"]) for row in as_rows(block)]


SYMBOL: Final = "CUPID"

#: docs/01 §5 block 3, verbatim: the six lines the reference product was observed to render.
#:
#: docs/05 §16 says "Ship **at least**" eight rules, and all eight fire for this fixture, so the
#: assertion is that these six appear in this order at the head of the list — not that the list
#: has exactly six. `docs/10a` records the two extras.
REFERENCE_PROS: Final[tuple[str, ...]] = (
    "The close is above 200-day moving average.",
    "The close is above 100-day moving average.",
    "The close is above 50-day moving average.",
    "The close is above 20-day moving average.",
    "The close is within 25% of all time high.",
    "The beta is less than 1.25",
)


async def seed_bars(session: AsyncSession, symbol: str) -> int:
    """Load one instrument's fixture bars into ``ohlcv_daily``.

    Per-test rather than in the module-scoped seed: the factsheet is the only surface that reads
    bars, and 40 instruments x 765 days would be paid for by every screener suite in this
    directory for nothing.
    """
    provider = FixtureProvider()
    record = next(r for r in provider.list_instruments() if r.symbol == symbol)
    assert record.kite_token is not None
    frame = provider.daily_bars(record.kite_token, dt.date(1900, 1, 1), dt.date(2100, 1, 1))
    instrument_id = (
        await session.execute(Instrument.__table__.select().where(Instrument.symbol == symbol))
    ).one()[0]
    rows = [
        {
            "instrument_id": instrument_id,
            "date": row["date"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "close_raw": row["close"],
            "volume_raw": row["volume"],
            "adj_factor": Decimal(1),
            "source": row["source"],
        }
        for row in frame.iter_rows(named=True)
    ]
    await session.execute(insert(OhlcvDaily), rows)
    await session.flush()
    return len(rows)


def body_of(response: httpx.Response) -> JsonMap:
    """Parse with ``Decimal`` rather than ``float``.

    The API serialises through ``canonical_json``, so `13.00` reaches the wire with its trailing
    zero intact (CLAUDE.md house rule 8). Reading it back through `float` would throw that away
    before the assertion ever saw it, and the tests would pass on a service that had stopped
    honouring the stored precision.
    """
    assert response.status_code == 200, response.text
    parsed: JsonMap = json.loads(response.text, parse_float=Decimal)
    return parsed


async def factsheet(api: httpx.AsyncClient, symbol: str = SYMBOL) -> JsonMap:
    return body_of(await api.get(url(f"/instruments/{symbol}")))


def cell(block: object, label: str) -> JsonMap:
    return next(row for row in as_rows(block) if row["label"] == label)


class TestSearch:
    async def test_an_exact_symbol_sorts_first(self, api: httpx.AsyncClient) -> None:
        body = body_of(await api.get(url("/instruments"), params={"search": "cupid"}))
        first = as_rows(body["data"])[0]
        assert first["symbol"] == SYMBOL
        assert first["name"]

    async def test_it_matches_on_name_too(self, api: httpx.AsyncClient) -> None:
        """docs/08 §"Command palette": the typeahead takes a symbol *or* a name."""
        hits = body_of(await api.get(url("/instruments"), params={"search": "CUPID"}))
        name = str(as_rows(hits["data"])[0]["name"])
        by_name = body_of(await api.get(url("/instruments"), params={"search": name[:6]}))
        assert SYMBOL in {row["symbol"] for row in as_rows(by_name["data"])}

    async def test_it_honours_the_limit(self, api: httpx.AsyncClient) -> None:
        body = body_of(await api.get(url("/instruments"), params={"search": "a", "limit": 3}))
        assert len(as_rows(body["data"])) <= 3

    async def test_an_empty_search_is_rejected(self, api: httpx.AsyncClient) -> None:
        assert_problem(
            await api.get(url("/instruments"), params={"search": ""}),
            400,
            "invalid-screen-definition",
        )


class TestFactsheetHeader:
    async def test_an_unknown_symbol_is_404(self, api: httpx.AsyncClient) -> None:
        assert_problem(await api.get(url("/instruments/NOSUCH")), 404, "not-found")

    async def test_it_carries_the_published_date_and_version(self, api: httpx.AsyncClient) -> None:
        body = await factsheet(api)
        assert body["as_of"] == AS_OF.isoformat()
        assert number(body["data_version"]) >= 1

    async def test_the_header_names_the_instrument(self, api: httpx.AsyncClient) -> None:
        """docs/01 §5 block 1 — symbol, price, full name, `NSE: SYMBOL`, membership chips."""
        body = await factsheet(api)
        header = as_map(body["header"])
        assert header["symbol"] == SYMBOL
        assert header["exchange"] == "NSE"
        assert number(header["close_raw"]) == Decimal("284.03")
        assert as_rows(body["index_memberships"]), "the fixture has CUPID in several universes"

    async def test_key_stats_are_the_five_docs_names(self, api: httpx.AsyncClient) -> None:
        """docs/01 §5 block 2 — P/E, Marketcap (cr), Beta, Series, Listed On."""
        body = await factsheet(api)
        assert labels(body["key_stats"]) == [
            "P/E",
            "Marketcap (cr)",
            "Beta",
            "Series",
            "Listed On",
        ]


class TestProsAndCons:
    async def test_the_six_reference_lines_are_the_first_six_pros(
        self, api: httpx.AsyncClient
    ) -> None:
        """Acceptance criterion 1 — docs/01 §5 block 3."""
        body = await factsheet(api)
        assert tuple(as_strings(body["pros"])[: len(REFERENCE_PROS)]) == REFERENCE_PROS

    async def test_every_reference_line_is_present(self, api: httpx.AsyncClient) -> None:
        body = await factsheet(api)
        assert set(REFERENCE_PROS) <= set(as_strings(body["pros"]))

    async def test_all_eight_rules_are_decided_for_this_fixture(
        self, api: httpx.AsyncClient
    ) -> None:
        """Nothing is undecided: the export gives every input the eight rules read."""
        body = await factsheet(api)
        assert body["undecided"] == []
        assert len(as_strings(body["pros"])) + len(as_strings(body["cons"])) == len(RULES)

    async def test_a_pro_and_its_con_never_both_appear(self, api: httpx.AsyncClient) -> None:
        body = await factsheet(api)
        pros = set(as_strings(body["pros"]))
        cons = set(as_strings(body["cons"]))
        for rule in RULES:
            assert not (rule.pro in pros and rule.con in cons)


class TestPriceAndMovingAverages:
    async def test_away_from_high_matches_docs_05(self, api: httpx.AsyncClient) -> None:
        """Acceptance criterion 2, first half — docs/05 §10.

        docs/05 §10 verifies CUPID at -4.83% from a close of 284.56 against a 1-year high of
        299.00. The committed export is a *different* snapshot of the same instrument: close
        284.03, same high, so `away = 284.03/299.00 - 1 = -5.01%`, which is the number the export
        stores. Both are the same formula; asserting docs/05's own arithmetic and then the row we
        actually serve is the only way to check the formula rather than the snapshot.
        """
        assert round((Decimal("284.56") / Decimal("299.00") - 1) * 100, 2) == Decimal("-4.83")

        body = await factsheet(api)
        block = body["price_and_mas"]
        high_1y = number(cell(block, "1Y High")["value"])
        close = number(as_map(body["header"])["close"])
        expected = round((close / high_1y - 1) * 100, 2)
        assert number(cell(block, "Away from 1Y High")["value"]) == expected
        assert number(cell(block, "Away from ATH")["value"]) == Decimal("-5.01")
        assert expected == Decimal("-5.01")

    async def test_the_four_moving_averages_are_the_export_values(
        self, api: httpx.AsyncClient
    ) -> None:
        body = await factsheet(api)
        block = body["price_and_mas"]
        for label, value in (
            ("MA 200", "120.21"),
            ("MA 100", "162.77"),
            ("MA 50", "213.47"),
            ("MA 20", "249.53"),
        ):
            assert number(cell(block, label)["value"]) == Decimal(value), label

    async def test_the_block_is_in_the_documented_order(self, api: httpx.AsyncClient) -> None:
        """docs/01 §5 block 5."""
        body = await factsheet(api)
        assert labels(body["price_and_mas"]) == [
            "Face Value",
            "1Y High",
            "Away from 1Y High",
            "ATH",
            "Away from ATH",
            "MA 200",
            "MA 100",
            "MA 50",
            "MA 20",
        ]


class TestReturnsAndTheOtherWindowBlocks:
    async def test_returns_match_the_export_to_2dp(self, api: httpx.AsyncClient) -> None:
        """Acceptance criterion 2, second half — docs/01 §5 block 6."""
        body = await factsheet(api)
        block = body["returns"]
        assert labels(block) == [
            "1Y",
            "9M",
            "6M",
            "3M",
            "1M",
            "12M-1M",
            "12M-2M",
        ]
        for label, value in (
            ("1Y", "726.63"),
            ("9M", "328.85"),
            ("6M", "242.87"),
            ("3M", "148.41"),
            ("1M", "37.03"),
        ):
            assert number(cell(block, label)["value"]) == Decimal(value), label

    async def test_sharpe_volatility_and_rsi_carry_five_windows_each(
        self, api: httpx.AsyncClient
    ) -> None:
        """docs/01 §5 blocks 7, 8 and 9."""
        body = await factsheet(api)
        for key in ("sharpe_returns", "volatility", "rsi"):
            assert labels(body[key]) == ["1Y", "9M", "6M", "3M", "1M"], key

    async def test_volatility_is_the_fraction_the_export_stores(
        self, api: httpx.AsyncClient
    ) -> None:
        """The open item in CLAUDE.md: docs/05 §2 says percent, docs/13 §4 says fraction.

        The export is the arbiter and it is a fraction — 0.579…, not 57.9. The API serves what is
        stored; the UI is what multiplies by 100.
        """
        body = await factsheet(api)
        assert number(cell(body["volatility"], "1Y")["value"]) == Decimal("0.57931794")

    async def test_percentiles_are_present_and_bounded(self, api: httpx.AsyncClient) -> None:
        """Deliverable 5 — a percentile per returns/sharpe/vol/RSI cell."""
        body = await factsheet(api)
        assert body["percentile_universe"] is not None
        for key in ("returns", "sharpe_returns", "volatility", "rsi"):
            for item in as_rows(body[key]):
                if item["value"] is None:
                    assert item["percentile"] is None, item
                else:
                    # `percent_rank()` is 0-1, which is what the grid's bar width expects.
                    assert Decimal(0) <= number(item["percentile"]) <= Decimal(1), item


class TestMarketQualityAndCorporateActions:
    async def test_market_quality_reports_the_regime_and_both_distances(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/01 §5 block 10 plus docs/05 §15's two Wasserstein distances."""
        await seed_bars(screener_session, SYMBOL)
        quality = as_map((await factsheet(api))["market_quality"])
        assert quality["regime"] in {"BULL", "BEAR", "NEUTRAL"}
        assert quality["regime_distance_bull"] is not None
        assert quality["regime_distance_bear"] is not None
        assert labels(quality["circuits"]) == ["1Y", "9M", "6M", "3M", "1M"]
        assert labels(quality["positive_days"]) == ["1Y", "9M", "6M", "3M", "1M"]

    async def test_the_regime_is_null_without_bars_rather_than_guessed(
        self, api: httpx.AsyncClient
    ) -> None:
        quality = as_map((await factsheet(api))["market_quality"])
        assert quality["regime"] is None
        assert quality["regime_distance_bull"] is None

    async def test_corporate_actions_are_newest_first(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/01 §5 block 11 — "bonus 4:1, split 10:1, bonus 1:1"."""
        instrument_id = (
            await screener_session.execute(
                Instrument.__table__.select().where(Instrument.symbol == SYMBOL)
            )
        ).one()[0]
        screener_session.add_all(
            [
                CorporateAction(
                    instrument_id=instrument_id,
                    action_type="bonus",
                    ex_date=dt.date(2024, 6, 3),
                    ratio_from=Decimal(1),
                    ratio_to=Decimal(4),
                    raw={},
                ),
                CorporateAction(
                    instrument_id=instrument_id,
                    action_type="split",
                    ex_date=dt.date(2025, 2, 10),
                    ratio_from=Decimal(10),
                    ratio_to=Decimal(1),
                    raw={},
                ),
            ]
        )
        await screener_session.flush()

        body = await factsheet(api)
        assert [row["ex_date"] for row in as_rows(body["corporate_actions"])] == [
            "2025-02-10",
            "2024-06-03",
        ]

        listing = body_of(await api.get(url(f"/instruments/{SYMBOL}/corporate-actions")))
        assert [row["action_type"] for row in as_rows(listing["data"])] == ["split", "bonus"]


class TestMetricCards:
    async def test_the_five_cards_are_the_documented_ones(self, api: httpx.AsyncClient) -> None:
        """docs/01 §5 block 4."""
        body = await factsheet(api)
        assert labels(body["metric_cards"]) == [
            "Closing Price",
            "Rolling 1-Yr Returns (%)",
            "Price to Earnings",
            "Marketcap",
            "1-Year RSI",
        ]

    async def test_the_median_is_the_instruments_own_history(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """ "The medians are the stock's own historical medians" — docs/01 §5 block 4."""
        await seed_bars(screener_session, SYMBOL)
        cards = as_rows((await factsheet(api))["metric_cards"])
        card = next(c for c in cards if c["key"] == "close")
        assert number(card["observations"]) > 700
        assert card["median"] is not None
        assert number(card["median"]) < number(card["value"])

    async def test_a_median_over_one_observation_says_so(self, api: httpx.AsyncClient) -> None:
        """Without bars the only close is the fact row's own — the count must not imply more."""
        cards = as_rows((await factsheet(api))["metric_cards"])
        card = next(c for c in cards if c["key"] == "ret_12m")
        assert card["observations"] == 1


class TestHistory:
    async def test_it_returns_the_close_series(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        await seed_bars(screener_session, SYMBOL)
        body = body_of(
            await api.get(url(f"/instruments/{SYMBOL}/history"), params={"from": "2026-01-01"})
        )
        assert body["field"] == "close"
        assert body["from"] == "2026-01-01"
        assert body["to"] == AS_OF.isoformat()
        points = as_rows(body["points"])
        assert len(points) > 100
        dates = [str(point["date"]) for point in points]
        assert dates == sorted(dates)
        assert number(points[-1]["value"]) == Decimal("284.0300")

    async def test_a_factor_field_is_served_from_factor_daily(self, api: httpx.AsyncClient) -> None:
        body = body_of(
            await api.get(url(f"/instruments/{SYMBOL}/history"), params={"field": "ret_12m"})
        )
        assert [point["value"] for point in as_rows(body["points"])] == [Decimal("726.63")]

    async def test_an_unknown_field_is_a_400(self, api: httpx.AsyncClient) -> None:
        response = await api.get(
            url(f"/instruments/{SYMBOL}/history"), params={"field": "close; drop table"}
        )
        assert_problem(response, 400, "invalid-screen-definition")

    async def test_a_reversed_range_is_a_400(self, api: httpx.AsyncClient) -> None:
        response = await api.get(
            url(f"/instruments/{SYMBOL}/history"),
            params={"from": "2026-08-18", "to": "2026-01-01"},
        )
        assert_problem(response, 400, "invalid-screen-definition")


class TestShortHistory:
    """Acceptance criterion 3, API half: a young listing yields NULL, never 0."""

    async def _young(self, session: AsyncSession) -> str:
        instrument_id = await add_instrument(session, "YOUNGCO")
        session.add(
            IndexMemberDaily(
                index_id=UNIVERSE_BY_SLUG["nifty-total-market"].index_id,
                date=AS_OF,
                instrument_id=instrument_id,
                source="nse_file",
            )
        )
        # Four months of history: the 1M window resolves, nothing longer does.
        await add_factor_row(
            session,
            instrument_id,
            values={
                "close": Decimal("101.00"),
                "ret_1m": Decimal("4.20"),
                "rsi_1m": Decimal("55.0000"),
                "vol_1m": Decimal("0.31000000"),
                "pos_days_1m": Decimal("52.00"),
                "circuits_1m": 0,
                "ma_20": Decimal("99.00"),
                "series": "EQ",
            },
        )
        await session.flush()
        return "YOUNGCO"

    async def test_the_long_windows_are_null_not_zero(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        symbol = await self._young(screener_session)
        body = await factsheet(api, symbol)
        for key in ("returns", "sharpe_returns", "volatility", "rsi"):
            for item in as_rows(body[key]):
                if item["label"] == "1M":
                    continue
                assert item["value"] is None, (key, item)

    async def test_undecided_rules_produce_neither_a_pro_nor_a_con(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        symbol = await self._young(screener_session)
        body = await factsheet(api, symbol)
        assert "close_above_ma_200" in as_strings(body["undecided"])
        keys = {rule.key: rule for rule in RULES}
        absent = keys["close_above_ma_200"]
        assert absent.pro not in as_strings(body["pros"])
        assert absent.con not in as_strings(body["cons"])
        # The one rule it *can* decide still fires.
        assert keys["close_above_ma_20"].pro in as_strings(body["pros"])

    async def test_a_null_cell_has_no_percentile(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        symbol = await self._young(screener_session)
        body = await factsheet(api, symbol)
        for item in as_rows(body["returns"]):
            if item["value"] is None:
                assert item["percentile"] is None
