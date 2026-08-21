"""Universe registry, plan and example-screen seeds — asserted against docs/01 and docs/13."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from baskfy_core.reference_export import EXPORT_COLUMNS
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.seed_data import EXAMPLE_SCREENS, PLANS, index_def_rows
from baskfy_core.universes import (
    CONTAINMENT_IDENTITIES,
    FIRST_NON_UNIVERSE_INDEX_ID,
    MARKET_HEALTH_SLUGS,
    UNION_IDENTITIES,
    UNIVERSE_BY_SLUG,
    UNIVERSES,
)


class TestUniverses:
    def test_fourteen_selectable_universes(self) -> None:
        """docs/01 §2.1 lists exactly 14 values in the `index` select."""
        assert len(UNIVERSES) == 14

    def test_twelve_market_health_universes(self) -> None:
        """docs/01 §6 lists 12 — the 14 minus nifty-fno and etf, not 12 extra rows."""
        assert len(MARKET_HEALTH_SLUGS) == 12
        assert set(MARKET_HEALTH_SLUGS) < {u.slug for u in UNIVERSES}
        assert set(MARKET_HEALTH_SLUGS) == {u.slug for u in UNIVERSES} - {"nifty-fno", "etf"}

    def test_csv_flags_match_the_reference_export(self) -> None:
        """docs/13 §1: 14 `is_*` columns. Ours must be spelled exactly as the file spells them."""
        assert [u.csv_flag for u in UNIVERSES] == [
            c for c in EXPORT_COLUMNS if _is_universe_flag(c)
        ]

    def test_mask_bits_are_dense_and_unique(self) -> None:
        assert sorted(u.mask_bit for u in UNIVERSES) == list(range(len(UNIVERSES)))

    def test_mask_fits_a_postgres_integer(self) -> None:
        """factor_daily.universe_mask is `integer`: 31 usable bits, sign bit excluded."""
        assert max(u.mask_bit for u in UNIVERSES) < 31

    def test_dashboard_indices_start_beyond_the_mask_range(self) -> None:
        """docs/01 §7 needs ~145 index_def rows; only the 14 universes may occupy mask bits."""
        assert max(u.index_id for u in UNIVERSES) < FIRST_NON_UNIVERSE_INDEX_ID

    def test_ui_order_is_a_permutation_of_the_universes(self) -> None:
        assert sorted(u.ui_order for u in UNIVERSES) == list(range(1, len(UNIVERSES) + 1))

    def test_index_def_rows_are_all_universes(self) -> None:
        rows = index_def_rows()
        assert len(rows) == 14
        assert all(row["is_universe"] is True for row in rows)

    @pytest.mark.parametrize(("parent", "children"), UNION_IDENTITIES)
    def test_union_identities_reference_known_universes(
        self, parent: str, children: tuple[str, ...]
    ) -> None:
        """docs/06: NIFTY 500 is the union of NIFTY 100, MIDCAP 150 and SMALLCAP 250.

        Two further union identities and a containment chain are listed alongside it.
        """
        assert parent in UNIVERSE_BY_SLUG
        assert all(child in UNIVERSE_BY_SLUG for child in children)

    @pytest.mark.parametrize(("subset", "superset"), CONTAINMENT_IDENTITIES)
    def test_containment_identities_reference_known_universes(
        self, subset: str, superset: str
    ) -> None:
        assert subset in UNIVERSE_BY_SLUG
        assert superset in UNIVERSE_BY_SLUG


def _is_universe_flag(column: str) -> bool:
    return (
        column.startswith("is_")
        and not column.endswith("_top_beta")
        and not column.endswith("_top_volatility")
    )


class TestPlans:
    def test_three_plans(self) -> None:
        assert {p.code for p in PLANS} == {"monthly", "yearly", "forever"}

    @pytest.mark.parametrize(
        ("code", "price"),
        [("monthly", "500.00"), ("yearly", "3999.00"), ("forever", "14999.00")],
    )
    def test_reference_prices(self, code: str, price: str) -> None:
        """docs/01 §1: Monthly ₹500 · Yearly ₹3,999 · Forever ₹14,999."""
        plan = next(p for p in PLANS if p.code == code)
        assert plan.price_inr == Decimal(price)

    def test_forever_has_no_billing_interval(self) -> None:
        """docs/02: Razorpay 'subscriptions plus one-time payments (needed for Forever)'."""
        assert next(p for p in PLANS if p.code == "forever").interval is None

    @pytest.mark.parametrize("code", ["monthly", "yearly"])
    def test_recurring_plans_have_an_interval(self, code: str) -> None:
        assert next(p for p in PLANS if p.code == code).interval in {"month", "year"}

    def test_prices_are_exact_decimals(self) -> None:
        assert all(isinstance(p.price_inr, Decimal) for p in PLANS)


class TestExampleScreens:
    def test_six_example_screens(self) -> None:
        """docs/01 §1: the reference product ships six read-only templates."""
        assert len(EXAMPLE_SCREENS) == 6

    def test_public_ids_are_unique_and_twelve_characters(self) -> None:
        """docs/04: user-facing entities carry a 12-character public_id used in URLs."""
        ids = [s.public_id for s in EXAMPLE_SCREENS]
        assert len(set(ids)) == len(ids)
        assert all(len(i) == 12 for i in ids)

    def test_names_are_unique(self) -> None:
        names = [s.name for s in EXAMPLE_SCREENS]
        assert len(set(names)) == len(names)

    @pytest.mark.parametrize("screen", EXAMPLE_SCREENS, ids=lambda s: s.public_id)
    def test_definitions_are_valid(self, screen: object) -> None:
        """Every seeded definition must survive the same validation a user's would."""
        assert isinstance(screen, type(EXAMPLE_SCREENS[0]))
        payload = screen.definition.model_dump(mode="json", by_alias=True)
        try:
            ScreenDefinition.model_validate(payload)
        except ValidationError as exc:  # pragma: no cover - failure path
            pytest.fail(f"{screen.public_id} is not a valid ScreenDefinition: {exc}")

    def test_definition_hashes_are_distinct(self) -> None:
        """Six templates that hash alike would collide in the screen-result cache."""
        hashes = {s.definition.definition_hash() for s in EXAMPLE_SCREENS}
        assert len(hashes) == 6

    def test_the_captured_reference_screen_is_present(self) -> None:
        """docs/13: the committed export is NIFTY TOTAL MARKET / AVERAGE SHARPE 12 6 3 1."""
        screen = next(s for s in EXAMPLE_SCREENS if s.name == "Investing 001")
        assert screen.definition.index == "nifty-total-market"
        assert screen.definition.sort_by == "avg_sharpe_12_6_3_1"
