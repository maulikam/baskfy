"""Basket sleeves and unit counts — tree 5, leaf B2.

Two gaps, one router. The landing page has promised "a momentum basket sitting beside a long-term
core inside one portfolio" since before there was a schema that could hold one:
``portfolio_sleeve.kind`` admitted ``screen`` and ``manual``, so the sentence was marketing rather
than a feature. Migration 0019 added ``basket``; :class:`TestABasketSleeveIsExpressible` is the
proof that a person can now say it, and :class:`TestTheSourcePairingIsExclusive` is the proof
that they cannot say it wrong.

The second gap is arithmetic. A sleeve could say "₹4,00,000 of RELIANCE" and could not say "132
shares", so every reader did the division themselves on a phone calculator, from a price they
looked up elsewhere. ``baskfy_core.portfolio_units`` (leaf A3) is the pure half of the fix and it
refuses loudly when a name cannot be priced; :class:`TestUnitsAreCounted` and
:class:`TestAnUnpricedNameDegradesHonestly` are about what this surface does with that refusal.

WHAT IS ASSERTED HERE IS THE SPEC, NOT THE CODE (house rule 2)
---------------------------------------------------------------
Every number below is derived in the test from inputs the test chose — ``4_00_000 / 2500 == 160``
is written out, not read back from the response and compared with itself. Where the assertion is
an invariant rather than a value (``units x price <= amount``, ``deployed + cash == capital``,
"every money field has two decimal places"), it is asserted over every row of every sleeve, so a
future sleeve kind that breaks it fails here rather than in a screenshot.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx
import pytest
from api_helpers import assert_problem, bearer, make_user, url
from screener_helpers import AS_OF, requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import screener as screener_service
from baskfy_api.problems import Problem
from baskfy_api.routers import sleeves as router
from baskfy_core.models import (
    CbBasket,
    CbBasketVersion,
    CbConstituent,
    CbManager,
    Instrument,
    OhlcvDaily,
)
from baskfy_core.models.accounts import SLEEVE_KINDS
from baskfy_core.seed_data import EXAMPLE_SCREENS

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

Json = dict[str, object]

SCREEN_ID = EXAMPLE_SCREENS[0].public_id

#: Two names from the seeded reference export, so the instruments exist before a bar is written.
ALPHA = "CUPID"
BETA = "HFCL"
#: A third, deliberately left without a bar in the tests that exercise the degraded path.
GAMMA = "WELCORP"

#: Chosen so the arithmetic is checkable by eye: 60/40 of ₹10,00,000 at these prices is
#: 6,00,000/2,500 = 240 units and 4,00,000/1,000 = 400 units, both exact.
ALPHA_PRICE = Decimal("2500.00")
BETA_PRICE = Decimal("1000.00")
SLEEVE_CAPITAL = Decimal("1000000.00")


def obj(value: object) -> Json:
    assert isinstance(value, dict), value
    return value


def arr(value: object) -> list[Json]:
    assert isinstance(value, list), value
    return [obj(item) for item in value]


def strs(value: object) -> list[str]:
    assert isinstance(value, list), value
    return [str(item) for item in value]


def body_of(response: httpx.Response) -> Json:
    assert response.status_code == 200, response.text
    return obj(response.json())


def money(value: object) -> Decimal:
    """A money field, asserted to be the string ``numeric(_, 2)`` round-trips as.

    Pydantic serialises a ``Decimal`` with its exponent intact, so "two decimal places" is a
    property of the response bytes and not of a float the reader reconstructed.
    """
    assert isinstance(value, str), f"money must serialise as an exact decimal string: {value!r}"
    assert "." in value and len(value.split(".")[1]) == 2, f"not 2 dp: {value}"
    return Decimal(value)


def units_of(row: Json) -> int | None:
    value = row["units"]
    if value is None:
        return None
    assert isinstance(value, int), f"units must be a whole number, got {value!r}"
    return value


async def owner(session: AsyncSession, email: str) -> dict[str, str]:
    _, public_id = await make_user(session, email)
    return bearer(public_id)


async def user_and_headers(session: AsyncSession, email: str) -> tuple[int, dict[str, str]]:
    user_id, public_id = await make_user(session, email)
    return user_id, bearer(public_id)


async def make_portfolio(api: httpx.AsyncClient, headers: dict[str, str], name: str) -> int:
    response = await api.post(
        url("/portfolios"), json={"name": name, "holdings": []}, headers=headers
    )
    assert response.status_code == 201, response.text
    portfolio = obj(obj(response.json())["portfolio"])
    value = portfolio["id"]
    assert isinstance(value, int)
    return value


async def instrument_id(session: AsyncSession, symbol: str) -> int:
    value = await session.scalar(select(Instrument.id).where(Instrument.symbol == symbol))
    assert value is not None, f"{symbol} is not in the seeded reference export"
    return int(value)


async def add_bar(
    session: AsyncSession, symbol: str, close: Decimal, *, on: dt.date = AS_OF
) -> None:
    """One ``ohlcv_daily`` row. ``close_raw`` is what the router prices units at (house rule 6)."""
    session.add(
        OhlcvDaily(
            instrument_id=await instrument_id(session, symbol),
            date=on,
            open=close,
            high=close,
            low=close,
            close=close,
            close_raw=close,
            volume=100_000,
            volume_raw=100_000,
            adj_factor=Decimal(1),
            source="nse",
        )
    )
    await session.flush()


async def make_basket(  # noqa: PLR0913 - one keyword per axis the suite needs to vary
    session: AsyncSession,
    *,
    slug: str,
    name: str = "Momentum 2",
    visibility: str = "PUBLISHED",
    weights: dict[str, Decimal] | None = None,
    versioned: bool = True,
) -> CbBasket:
    """A basket with one GENESIS version and weighted constituents.

    Built row by row rather than through ``POST /cb/baskets`` because that route is the sole
    tenant's and this suite needs two different owners, a published basket and a private one.
    """
    existing = await session.scalar(select(CbManager.id).where(CbManager.slug == "b2-desk"))
    if existing is None:
        manager = CbManager(slug="b2-desk", name="B2 Desk", kind="ENGINE", strategies=[])
        session.add(manager)
        await session.flush()
        existing = manager.id
    basket = CbBasket(
        slug=slug,
        name=name,
        manager_id=int(existing),
        type="STOCK",
        access="FREE",
        visibility=visibility,
        categories=[],
        rebalance_frequency="MONTHLY",
        source="MANUAL",
    )
    session.add(basket)
    await session.flush()
    if not versioned:
        return basket
    version = CbBasketVersion(
        basket_id=int(basket.id),
        version_no=1,
        effective_date=AS_OF,
        label="GENESIS",
        added_count=2,
        removed_count=0,
    )
    session.add(version)
    await session.flush()
    for symbol, weight in (weights or {ALPHA: Decimal("0.6000"), BETA: Decimal("0.4000")}).items():
        session.add(
            CbConstituent(
                version_id=int(version.id),
                instrument_id=await instrument_id(session, symbol),
                segment="EQ",
                weight=weight,
            )
        )
    await session.flush()
    return basket


async def put_sleeves(
    api: httpx.AsyncClient, headers: dict[str, str], portfolio_id: int, sleeves: list[Json]
) -> httpx.Response:
    return await api.put(
        url(f"/portfolios/{portfolio_id}/sleeves"), json={"sleeves": sleeves}, headers=headers
    )


def basket_sleeve(slug: str, *, name: str = "Momentum", capital: str = "1000000") -> Json:
    return {"name": name, "kind": "basket", "capital": capital, "basket_slug": slug}


def manual_sleeve(*, name: str = "Long-term core", capital: str = "2500000") -> Json:
    return {"name": name, "kind": "manual", "capital": capital}


# ---------------------------------------------------------------------------
# MANAGER_SEED requires a valid `kind`; the constant is checked rather than guessed.
# ---------------------------------------------------------------------------


class TestTheKindTheDatabaseAdmits:
    def test_the_router_and_the_schema_agree_on_the_sleeve_kinds(self) -> None:
        """A kind one layer accepts and the other refuses is the bug 0019 came to end."""
        assert router.BASKET in SLEEVE_KINDS
        assert set(SLEEVE_KINDS) == {"screen", "manual", "basket"}
        for kind in SLEEVE_KINDS:
            assert kind in router.KIND_PATTERN


class TestABasketSleeveIsExpressible:
    async def test_create_a_basket_sleeve_naming_a_published_basket(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session, "b2.create@example.com")
        await make_basket(screener_session, slug="b2-create")
        portfolio_id = await make_portfolio(api, headers, "Create")

        response = await put_sleeves(api, headers, portfolio_id, [basket_sleeve("b2-create")])
        listed = body_of(response)
        rows = arr(listed["sleeves"])
        assert len(rows) == 1
        assert rows[0]["kind"] == "basket"
        assert rows[0]["basket_slug"] == "b2-create"
        assert rows[0]["basket_name"] == "Momentum 2"
        assert rows[0]["screen_public_id"] is None

        # The row itself, not only the echo: `kind='basket'` must be paired with `basket_id`.
        stored = body_of(await api.get(url(f"/portfolios/{portfolio_id}/sleeves"), headers=headers))
        assert arr(stored["sleeves"])[0]["basket_slug"] == "b2-create"

    async def test_create_is_refused_when_the_basket_does_not_exist(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session, "b2.nobasket@example.com")
        portfolio_id = await make_portfolio(api, headers, "No basket")
        response = await put_sleeves(api, headers, portfolio_id, [basket_sleeve("b2-not-a-slug")])
        assert_problem(response, 404, "not-found")

    async def test_the_pairing_refuses_a_basket_sleeve_that_names_no_basket(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """``kind='basket'`` with no ``basket_slug`` is the half-written row 0019 forbids."""
        headers = await owner(screener_session, "b2.pairing@example.com")
        portfolio_id = await make_portfolio(api, headers, "Pairing")
        response = await put_sleeves(
            api,
            headers,
            portfolio_id,
            [{"name": "Momentum", "kind": "basket", "capital": "100000"}],
        )
        body = assert_problem(response, 400, "invalid-screen-definition")
        assert "must name a basket" in str(body["errors"])


class TestTheSourcePairingIsExclusive:
    """Each kind names exactly one source column. All six wrong combinations are refused."""

    @pytest.mark.parametrize(
        ("sleeve", "expected"),
        [
            (
                {
                    "name": "S",
                    "kind": "screen",
                    "capital": "1000",
                    "screen_public_id": SCREEN_ID,
                    "basket_slug": "b2-exclusive",
                },
                "a screen sleeve cannot name a basket",
            ),
            (
                {"name": "S", "kind": "screen", "capital": "1000", "basket_slug": "b2-exclusive"},
                "a screen sleeve must name a screen",
            ),
            (
                {
                    "name": "B",
                    "kind": "basket",
                    "capital": "1000",
                    "basket_slug": "b2-exclusive",
                    "screen_public_id": SCREEN_ID,
                },
                "a basket sleeve cannot name a screen",
            ),
            (
                {"name": "M", "kind": "manual", "capital": "1000", "basket_slug": "b2-exclusive"},
                "a manual sleeve cannot name a basket",
            ),
            (
                {
                    "name": "M",
                    "kind": "manual",
                    "capital": "1000",
                    "screen_public_id": SCREEN_ID,
                },
                "a manual sleeve cannot name a screen",
            ),
        ],
    )
    async def test_an_exclusive_pairing_mismatch_is_refused(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        sleeve: Json,
        expected: str,
    ) -> None:
        headers = await owner(screener_session, f"b2.x{abs(hash(expected)) % 9973}@example.com")
        await make_basket(screener_session, slug="b2-exclusive")
        portfolio_id = await make_portfolio(api, headers, "Exclusive")
        response = await put_sleeves(api, headers, portfolio_id, [sleeve])
        body = assert_problem(response, 400, "invalid-screen-definition")
        assert expected in str(body["errors"])

    async def test_an_unknown_kind_is_a_mismatch_the_field_itself_refuses(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session, "b2.unknownkind@example.com")
        portfolio_id = await make_portfolio(api, headers, "Unknown")
        response = await put_sleeves(
            api, headers, portfolio_id, [{"name": "X", "kind": "index", "capital": "1"}]
        )
        body = assert_problem(response, 400, "invalid-screen-definition")
        assert "sleeves.0.kind" in str(body["errors"])


class TestThePromiseOnTheLandingPage:
    async def test_a_momentum_basket_sits_beside_a_long_term_core(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The sentence the product has been printing, expressed as rows for the first time."""
        headers = await owner(screener_session, "b2.beside@example.com")
        await make_basket(screener_session, slug="b2-beside")
        await add_bar(screener_session, ALPHA, ALPHA_PRICE)
        await add_bar(screener_session, BETA, BETA_PRICE)
        portfolio_id = await make_portfolio(api, headers, "One portfolio")

        listed = body_of(
            await put_sleeves(
                api,
                headers,
                portfolio_id,
                [basket_sleeve("b2-beside"), manual_sleeve()],
            )
        )
        kinds = [row["kind"] for row in arr(listed["sleeves"])]
        assert kinds == ["basket", "manual"], "both sleeves must survive, in the order given"
        assert money(listed["total_capital"]) == Decimal("3500000.00")

        allocation = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/allocation"), headers=headers)
        )
        sleeves = arr(allocation["sleeves"])
        assert [s["kind"] for s in sleeves] == ["basket", "manual"]

        momentum, core = sleeves[0], sleeves[1]
        assert [row["symbol"] for row in arr(momentum["rows"])] == [ALPHA, BETA]
        # The manual sleeve is reported and never allocated -- M34's rule, unchanged by 0019.
        assert arr(core["rows"]) == []
        assert money(core["deployed"]) == Decimal("0.00")
        assert money(core["cash"]) == Decimal("2500000.00")

        # And the portfolio still reconciles across two kinds of sleeve.
        assert money(allocation["deployed"]) + money(allocation["cash"]) == money(
            allocation["capital"]
        )


class TestUnitsAreCounted:
    async def test_a_basket_sleeve_reports_whole_units_from_its_own_weights(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """60/40 of ₹10,00,000 at ₹2,500 and ₹1,000 is 240 and 400 units. Derived, not read back."""
        headers = await owner(screener_session, "b2.units@example.com")
        await make_basket(screener_session, slug="b2-units")
        await add_bar(screener_session, ALPHA, ALPHA_PRICE)
        await add_bar(screener_session, BETA, BETA_PRICE)
        portfolio_id = await make_portfolio(api, headers, "Units")
        await put_sleeves(api, headers, portfolio_id, [basket_sleeve("b2-units")])

        allocation = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/allocation"), headers=headers)
        )
        assert allocation["priced_as_of"] == AS_OF.isoformat()
        assert strs(allocation["unpriced"]) == []
        sleeve = arr(allocation["sleeves"])[0]
        assert sleeve["units_note"] is None

        rows = {str(row["symbol"]): row for row in arr(sleeve["rows"])}
        assert units_of(rows[ALPHA]) == int(SLEEVE_CAPITAL * Decimal("0.6") / ALPHA_PRICE)
        assert units_of(rows[BETA]) == int(SLEEVE_CAPITAL * Decimal("0.4") / BETA_PRICE)
        assert money(rows[ALPHA]["price"]) == ALPHA_PRICE
        assert money(rows[ALPHA]["weight_pct"]) == Decimal("60.00")

    async def test_units_never_cost_more_than_the_amount_the_row_shows(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """A price that divides badly is the case a naive implementation rounds up on.

        ₹10,00,000 x 0.6 = ₹6,00,000 at ₹7,777.77 buys 77 units (77.14...), never 78. Rounding up
        spends money the investor does not have.
        """
        headers = await owner(screener_session, "b2.unitsfloor@example.com")
        await make_basket(screener_session, slug="b2-units-floor")
        await add_bar(screener_session, ALPHA, Decimal("7777.77"))
        await add_bar(screener_session, BETA, Decimal("333.33"))
        portfolio_id = await make_portfolio(api, headers, "Floor")
        await put_sleeves(api, headers, portfolio_id, [basket_sleeve("b2-units-floor")])

        allocation = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/allocation"), headers=headers)
        )
        sleeve = arr(allocation["sleeves"])[0]
        for row in arr(sleeve["rows"]):
            count = units_of(row)
            assert count is not None
            assert money(row["price"]) * count == money(row["amount"])
            assert money(row["amount"]) <= SLEEVE_CAPITAL * (
                money(row["weight_pct"]) / Decimal(100)
            )
        assert money(sleeve["deployed"]) + money(sleeve["cash"]) == money(sleeve["capital"])

    async def test_a_screen_sleeve_counts_units_against_the_amount_it_already_showed(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The rupee column is M34's and must not move; the unit column is taken from it.

        The screen is run for its names, then bars are written for exactly those names, so the
        assertion is about the relationship between the two columns rather than about which
        stocks the example screen happens to rank.
        """
        headers = await owner(screener_session, "b2.screenunits@example.com")
        portfolio_id = await make_portfolio(api, headers, "Screen units")
        await put_sleeves(
            api,
            headers,
            portfolio_id,
            [
                {
                    "name": "Momentum screen",
                    "kind": "screen",
                    "capital": "500000",
                    "screen_public_id": SCREEN_ID,
                    "top_n": 3,
                }
            ],
        )
        first = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/allocation"), headers=headers)
        )
        sleeve = arr(first["sleeves"])[0]
        named = [str(row["symbol"]) for row in arr(sleeve["rows"])]
        assert named, "the example screen returned no names; the fixture is not seeded"
        amounts_before = [money(row["amount"]) for row in arr(sleeve["rows"])]

        for offset, symbol in enumerate(named):
            await add_bar(screener_session, symbol, Decimal(1000 + offset))

        second = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/allocation"), headers=headers)
        )
        after = arr(second["sleeves"])[0]
        assert [money(row["amount"]) for row in arr(after["rows"])] == amounts_before, (
            "pricing a screen sleeve must not move the rupee amounts it already published"
        )
        for row in arr(after["rows"]):
            count = units_of(row)
            assert count is not None
            assert money(row["price"]) * count <= money(row["amount"])
            assert money(row["price"]) * (count + 1) > money(row["amount"])


class TestAnUnpricedNameDegradesHonestly:
    async def test_an_unpriced_basket_reports_null_units_and_says_why(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Zero would be a number. This is the absence of one, and it says so."""
        headers = await owner(screener_session, "b2.unpriced@example.com")
        await make_basket(screener_session, slug="b2-unpriced")
        portfolio_id = await make_portfolio(api, headers, "Unpriced")
        await put_sleeves(api, headers, portfolio_id, [basket_sleeve("b2-unpriced")])

        allocation = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/allocation"), headers=headers)
        )
        sleeve = arr(allocation["sleeves"])[0]
        for row in arr(sleeve["rows"]):
            assert units_of(row) is None, "an unpriceable name must not report 0 units"
            assert row["price"] is None
        assert sorted(strs(sleeve["unpriced"])) == sorted([ALPHA, BETA])
        assert sorted(strs(allocation["unpriced"])) == sorted([ALPHA, BETA])
        note = sleeve["units_note"]
        assert isinstance(note, str) and note
        assert ALPHA in note and BETA in note
        # The reason must be actionable, not decorative: it names the missing thing.
        assert "price" in note.lower()

        # The money view is still whole and still reconciles -- the weighted split of capital.
        assert money(sleeve["deployed"]) + money(sleeve["cash"]) == money(sleeve["capital"])
        by_symbol = {str(row["symbol"]): money(row["amount"]) for row in arr(sleeve["rows"])}
        assert by_symbol[ALPHA] == Decimal("600000.00")
        assert by_symbol[BETA] == Decimal("400000.00")

    async def test_one_unpriced_name_does_not_blank_the_priced_ones_in_a_screen_sleeve(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Fifty-nine of sixty names being countable is fifty-nine answers the reader needs."""
        headers = await owner(screener_session, "b2.partial@example.com")
        portfolio_id = await make_portfolio(api, headers, "Partial")
        await put_sleeves(
            api,
            headers,
            portfolio_id,
            [
                {
                    "name": "Momentum screen",
                    "kind": "screen",
                    "capital": "500000",
                    "screen_public_id": SCREEN_ID,
                    "top_n": 3,
                }
            ],
        )
        first = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/allocation"), headers=headers)
        )
        named = [str(row["symbol"]) for row in arr(arr(first["sleeves"])[0]["rows"])]
        assert len(named) >= 2, "this assertion needs at least two names to leave one unpriced"
        for symbol in named[:-1]:
            await add_bar(screener_session, symbol, Decimal("500.00"))

        allocation = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/allocation"), headers=headers)
        )
        sleeve = arr(allocation["sleeves"])[0]
        counted = {str(row["symbol"]): units_of(row) for row in arr(sleeve["rows"])}
        assert counted[named[-1]] is None
        assert all(counted[symbol] is not None for symbol in named[:-1])
        assert strs(sleeve["unpriced"]) == [named[-1]]
        note = sleeve["units_note"]
        assert isinstance(note, str) and named[-1] in note

    async def test_a_stale_bar_is_no_price_at_all(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """A close from a year ago is not a price anybody can buy at, so it is not offered."""
        headers = await owner(screener_session, "b2.stale@example.com")
        await make_basket(screener_session, slug="b2-stale")
        await add_bar(screener_session, ALPHA, ALPHA_PRICE, on=AS_OF - dt.timedelta(days=400))
        await add_bar(screener_session, BETA, BETA_PRICE)
        portfolio_id = await make_portfolio(api, headers, "Stale")
        await put_sleeves(api, headers, portfolio_id, [basket_sleeve("b2-stale")])

        allocation = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/allocation"), headers=headers)
        )
        sleeve = arr(allocation["sleeves"])[0]
        assert strs(sleeve["unpriced"]) == [ALPHA]
        rows = {str(row["symbol"]): row for row in arr(sleeve["rows"])}
        assert units_of(rows[ALPHA]) is None
        assert units_of(rows[BETA]) is None, (
            "a basket sleeve is refused as a whole when any of its names is unpriceable, so the "
            "sleeve's deployed total cannot be short by the missing name's worth"
        )

    async def test_a_zero_price_is_refused_rather_than_counted_as_infinite_units(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Zero is what a suspended instrument prints; dividing by it is not "lots of units"."""
        headers = await owner(screener_session, "b2.zeroprice@example.com")
        await make_basket(screener_session, slug="b2-zero")
        await add_bar(screener_session, ALPHA, Decimal("0.00"))
        await add_bar(screener_session, BETA, BETA_PRICE)
        portfolio_id = await make_portfolio(api, headers, "Zero")
        await put_sleeves(api, headers, portfolio_id, [basket_sleeve("b2-zero")])

        allocation = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/allocation"), headers=headers)
        )
        sleeve = arr(allocation["sleeves"])[0]
        assert strs(sleeve["unpriced"]) == [ALPHA]
        assert all(units_of(row) is None for row in arr(sleeve["rows"]))


class TestMoneyIsRoundedAtWriteTime:
    async def test_every_money_field_carries_storage_precision(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """House rule 8. ``portfolio_sleeve.capital`` is ``numeric(18, 2)``; so is the response.

        A column that shows paise beside one that does not is how the API, the UI and the CSV
        export come to disagree in the last digit. :func:`money` asserts the exponent on every
        field it touches, so this test is a sweep rather than a spot check.
        """
        headers = await owner(screener_session, "b2.precision@example.com")
        await make_basket(screener_session, slug="b2-precision")
        await add_bar(screener_session, ALPHA, Decimal("2499.99"))
        await add_bar(screener_session, BETA, Decimal("1000.01"))
        portfolio_id = await make_portfolio(api, headers, "Precision")
        await put_sleeves(
            api,
            headers,
            portfolio_id,
            [basket_sleeve("b2-precision"), manual_sleeve(capital="1234567")],
        )

        allocation = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/allocation"), headers=headers)
        )
        total_deployed = Decimal(0)
        for sleeve in arr(allocation["sleeves"]):
            deployed = money(sleeve["deployed"])
            cash = money(sleeve["cash"])
            capital = money(sleeve["capital"])
            assert deployed + cash == capital, f"{sleeve['name']} does not reconcile"
            total_deployed += deployed
            row_total = Decimal(0)
            for row in arr(sleeve["rows"]):
                row_total += money(row["amount"])
                money(row["weight_pct"])
                if row["price"] is not None:
                    money(row["price"])
            assert row_total == deployed, f"{sleeve['name']}: rows do not add up to deployed"
        assert money(allocation["deployed"]) == total_deployed
        assert money(allocation["deployed"]) + money(allocation["cash"]) == money(
            allocation["capital"]
        )

    async def test_the_listing_rounds_capital_the_same_way(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session, "b2.roundlist@example.com")
        await make_basket(screener_session, slug="b2-round")
        portfolio_id = await make_portfolio(api, headers, "Round")
        listed = body_of(
            await put_sleeves(
                api,
                headers,
                portfolio_id,
                [basket_sleeve("b2-round", capital="1000000"), manual_sleeve(capital="1")],
            )
        )
        total = sum((money(row["capital"]) for row in arr(listed["sleeves"])), Decimal(0))
        assert money(listed["total_capital"]) == total == Decimal("1000001.00")


class TestTenantIsolation:
    async def test_a_private_basket_is_invisible_to_another_tenant(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``cb_basket`` has no owner column, so visibility *is* the tenancy boundary.

        A PRIVATE basket belongs to the sole tenant, who is the only account that can create one.
        Another account naming its slug is told the basket does not exist -- 404 and not 403,
        because a 403 confirms that the slug names something real.
        """
        alice_id, alice = await user_and_headers(screener_session, "b2.alice@example.com")
        _, bob = await user_and_headers(screener_session, "b2.bob@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(alice_id))
        await make_basket(screener_session, slug="b2-private", visibility="PRIVATE")

        bobs_portfolio = await make_portfolio(api, bob, "Bob")
        assert_problem(
            await put_sleeves(api, bob, bobs_portfolio, [basket_sleeve("b2-private")]),
            404,
            "not-found",
        )

        # And the same slug is usable by the tenant it belongs to, so the refusal is isolation
        # rather than the feature being broken.
        alices_portfolio = await make_portfolio(api, alice, "Alice")
        listed = body_of(
            await put_sleeves(api, alice, alices_portfolio, [basket_sleeve("b2-private")])
        )
        assert arr(listed["sleeves"])[0]["basket_slug"] == "b2-private"

    async def test_isolation_survives_a_published_basket_being_shared(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A PUBLISHED basket is everyone's -- and is resolved without a sole-tenant lookup."""
        alice_id, _alice = await user_and_headers(screener_session, "b2.alice2@example.com")
        _, bob = await user_and_headers(screener_session, "b2.bob2@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(alice_id))
        await make_basket(screener_session, slug="b2-shared")
        portfolio_id = await make_portfolio(api, bob, "Bob shared")
        listed = body_of(await put_sleeves(api, bob, portfolio_id, [basket_sleeve("b2-shared")]))
        assert arr(listed["sleeves"])[0]["basket_slug"] == "b2-shared"

    async def test_one_tenant_never_reads_anothers_allocation(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, alice = await user_and_headers(screener_session, "b2.alice3@example.com")
        _, bob = await user_and_headers(screener_session, "b2.bob3@example.com")
        await make_basket(screener_session, slug="b2-cross")
        portfolio_id = await make_portfolio(api, alice, "Alice only")
        await put_sleeves(api, alice, portfolio_id, [basket_sleeve("b2-cross")])
        assert_problem(
            await api.get(url(f"/portfolios/{portfolio_id}/allocation"), headers=bob),
            404,
            "not-found",
        )


class TestAnArchivedBasketIsNotASource:
    async def test_an_archived_basket_cannot_back_a_new_sleeve(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """A standing instruction to follow something nobody maintains is not an instruction."""
        headers = await owner(screener_session, "b2.archived@example.com")
        basket = await make_basket(screener_session, slug="b2-archived")
        basket.archived_at = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
        await screener_session.flush()
        portfolio_id = await make_portfolio(api, headers, "Archived")
        assert_problem(
            await put_sleeves(api, headers, portfolio_id, [basket_sleeve("b2-archived")]),
            404,
            "not-found",
        )


class TestTheRegimeCapArithmetic:
    """The cap applied to a basket sleeve, without a desk schema to stand up.

    ``_basket_sleeve`` is the whole of that arithmetic and it takes the cap as an argument, so
    the rule can be asserted directly: capital run to a published rule is scaled, the sleeve
    still reconciles, and nothing is ever rounded *up* into money the investor does not have.
    """

    @staticmethod
    def _sleeve() -> router.SleeveOut:
        return router.SleeveOut(
            id=1,
            name="Momentum",
            kind="basket",
            capital=SLEEVE_CAPITAL,
            basket_slug="b2-unit",
            basket_name="Momentum 2",
        )

    def test_no_cap_deploys_against_the_whole_capital(self) -> None:
        sized = router._basket_sleeve(
            self._sleeve(),
            {ALPHA: Decimal("0.6000"), BETA: Decimal("0.4000")},
            {ALPHA: ALPHA_PRICE, BETA: BETA_PRICE},
            AS_OF,
            None,
        )
        counted = {row.symbol: row.units for row in sized.rows}
        assert counted == {ALPHA: 240, BETA: 400}
        assert sized.deployed == SLEEVE_CAPITAL
        assert sized.deployed + sized.cash == sized.capital

    def test_a_seventy_percent_cap_scales_the_sleeve_and_still_reconciles(self) -> None:
        """R2 caps equity at 70%: ₹7,00,000 split 60/40 is 168 and 280 units, not 240 and 400."""
        sized = router._basket_sleeve(
            self._sleeve(),
            {ALPHA: Decimal("0.6000"), BETA: Decimal("0.4000")},
            {ALPHA: ALPHA_PRICE, BETA: BETA_PRICE},
            AS_OF,
            Decimal(70),
        )
        counted = {row.symbol: row.units for row in sized.rows}
        assert counted == {ALPHA: 168, BETA: 280}
        assert sized.deployed == Decimal("700000.00")
        # The withheld 30% is cash against the sleeve's *own* capital, not a smaller sleeve.
        assert sized.capital == SLEEVE_CAPITAL
        assert sized.deployed + sized.cash == sized.capital

    def test_a_capped_sleeve_never_deploys_more_than_an_uncapped_one(self) -> None:
        weights = {ALPHA: Decimal("0.6000"), BETA: Decimal("0.4000")}
        prices = {ALPHA: Decimal("7777.77"), BETA: Decimal("333.33")}
        uncapped = router._basket_sleeve(self._sleeve(), weights, prices, AS_OF, None)
        for tier_cap in (Decimal(100), Decimal(70), Decimal(40), Decimal(0)):
            sized = router._basket_sleeve(self._sleeve(), weights, prices, AS_OF, tier_cap)
            assert sized.deployed <= uncapped.deployed
            assert sized.deployed + sized.cash == sized.capital
            for row in sized.rows:
                count = row.units
                assert count is not None
                assert row.price is not None
                assert row.price * count == row.amount


class TestASilentEmptySleeveNowSaysWhy:
    """An empty sleeve had two meanings and told you neither.

    "The screen ran and matched nothing" and "the screen fell over" both arrived as no rows,
    ₹0 deployed, and the sleeve's whole capital sitting in cash — identical on the page, and not
    the same news for somebody with ₹50 lakh in that sleeve. ``source_note`` separates them, and
    stays ``None`` when the source spoke for itself.
    """

    async def test_a_source_that_no_longer_exists_is_named_rather_than_left_blank(
        self, screener_session: AsyncSession
    ) -> None:
        found = await router._screen_symbols(screener_session, "not-a-real-id", 3)
        assert found.symbols == ()
        assert found.note is not None
        assert "not-a-real-id" in found.note
        assert "cash" in found.note

    async def test_a_source_that_cannot_be_run_says_so(
        self, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure used to be caught and dropped on the floor; now it reaches the reader."""

        async def _boom(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("the cache is down")

        monkeypatch.setattr(screener_service, "run_screen", _boom)
        found = await router._screen_symbols(screener_session, SCREEN_ID, 3)
        assert found.symbols == ()
        assert found.note is not None
        assert "RuntimeError" in found.note
        assert "other sleeves are unaffected" in found.note

    async def test_a_source_that_answered_normally_carries_no_note(
        self, screener_session: AsyncSession
    ) -> None:
        found = await router._screen_symbols(screener_session, SCREEN_ID, 3)
        assert found.note is None, "a working source must not be labelled as a failure"
        assert found.symbols

    async def test_a_basket_with_nothing_published_says_so_over_http(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session, "b2.noversion@example.com")
        await make_basket(screener_session, slug="b2-empty", versioned=False)
        portfolio_id = await make_portfolio(api, headers, "Empty")
        await put_sleeves(api, headers, portfolio_id, [basket_sleeve("b2-empty")])

        allocation = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/allocation"), headers=headers)
        )
        sleeve = arr(allocation["sleeves"])[0]
        assert arr(sleeve["rows"]) == []
        assert money(sleeve["deployed"]) == Decimal("0.00")
        assert money(sleeve["cash"]) == money(sleeve["capital"])
        note = sleeve["source_note"]
        assert isinstance(note, str)
        assert "b2-empty" in note


class TestABadlyWeightedBasketIsNamedRatherThanCrashed:
    def test_weights_that_do_not_fit_inside_one_sleeve_name_the_basket(self) -> None:
        """A stored basket summing to 140% is a data defect, and the answer says which one.

        ``allocate_units`` refuses to normalise weights somebody chose, which is right. What is
        wrong is letting that surface as an unlabelled 500: the operator reading the log has to
        be told which basket to go and look at.
        """
        sleeve = router.SleeveOut(
            id=1,
            name="Momentum",
            kind="basket",
            capital=SLEEVE_CAPITAL,
            basket_slug="b2-overweight",
            basket_name="Overweight",
        )
        with pytest.raises(Problem) as caught:
            router._basket_sleeve(
                sleeve,
                {ALPHA: Decimal("0.7000"), BETA: Decimal("0.7000")},
                {ALPHA: ALPHA_PRICE, BETA: BETA_PRICE},
                AS_OF,
                None,
            )
        assert "b2-overweight" in str(caught.value)
        assert caught.value.status == 500


class TestTheRegimeCapOverHttp:
    async def test_the_cap_is_opt_in_and_scales_a_basket_sleeve(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Capital run to a published rule is capped; capital somebody runs themselves is not.

        The desk's tier is read from the ``desk`` schema, which this suite does not populate, so
        the assertion is the one that holds either way: opting in never *raises* what a sleeve
        deploys, and a manual sleeve is untouched whatever the tier says.
        """
        headers = await owner(screener_session, "b2.cap@example.com")
        await make_basket(screener_session, slug="b2-cap")
        await add_bar(screener_session, ALPHA, ALPHA_PRICE)
        await add_bar(screener_session, BETA, BETA_PRICE)
        portfolio_id = await make_portfolio(api, headers, "Cap")
        await put_sleeves(api, headers, portfolio_id, [basket_sleeve("b2-cap"), manual_sleeve()])

        plain = body_of(
            await api.get(url(f"/portfolios/{portfolio_id}/allocation"), headers=headers)
        )
        capped = body_of(
            await api.get(
                url(f"/portfolios/{portfolio_id}/allocation"),
                params={"apply_regime_cap": "true"},
                headers=headers,
            )
        )
        assert plain["applied_regime_cap"] is False
        assert money(capped["deployed"]) <= money(plain["deployed"])
        manual_before = arr(plain["sleeves"])[1]
        manual_after = arr(capped["sleeves"])[1]
        assert money(manual_after["capital"]) == money(manual_before["capital"])
        assert money(manual_after["deployed"]) == Decimal("0.00")


class TestItIsStillNotAnOrderPath:
    def test_the_router_names_no_order_and_no_gateway(self) -> None:
        """Law 2 and non-negotiable #1. Units are a report; an order needs a side and a venue."""
        import inspect  # noqa: PLC0415 - only this assertion needs it

        source = inspect.getsource(router)
        for forbidden in (
            "baskfy_" + "execution",
            "kiteconnect",
            "place_order",
            "place_gtt",
            "OrderGateway",
            "/exec" + "ute",
        ):
            assert forbidden not in source, f"routers/sleeves.py names {forbidden}"

    def test_no_mutating_verb_beyond_the_put_that_edits_a_division(self) -> None:
        from baskfy_api.app import create_app  # noqa: PLC0415 - building an app is not free

        spec = create_app().openapi()
        paths = {
            p: sorted(spec["paths"][p]) for p in spec["paths"] if "sleeve" in p or "allocation" in p
        }
        assert paths
        for path, methods in paths.items():
            assert set(methods) <= {"get", "put"}, f"{path} exposes {methods}"
