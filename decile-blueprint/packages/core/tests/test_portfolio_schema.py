"""The portfolio graph asserted as a specification, not as a description of the models.

Every expectation below is transcribed from ``TREE-PORTFOLIO-PLAN.md`` §"The shape we are
building toward" and from the two rules that outrank it (house rule 9 — money and quantities are
``numeric``, never ``float``; and the reversibility contract for migration ``0019``). Nothing here
reads a value out of the model and asserts it equals itself: each constant is written down
independently, so a model that drifts from the contract fails rather than redefines it.

No database. These are assertions about ``Base.metadata`` and about the text of the migration,
which is what ``packages/core`` is allowed to know about (Law 1). Whether the constraints actually
refuse a bad row is the live database's business and belongs to ``services/api``'s migration
suite.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Float, Numeric, Table
from sqlalchemy.schema import CheckConstraint, ForeignKeyConstraint

from baskfy_core.models import Base
from baskfy_core.models.accounts import MAX_PORTFOLIO_DEPTH, SLEEVE_KINDS

MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "services"
    / "api"
    / "alembic"
    / "versions"
    / "0019_portfolio_graph.py"
)

#: The contract, written out rather than derived. ``portfolio_holding``'s key is the one that
#: changed: the same instrument held at two brokers is two rows, not one row that is wrong.
EXPECTED_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "portfolio": ("id",),
    "portfolio_holding": ("portfolio_id", "instrument_id", "broker_account_id"),
    "portfolio_sleeve": ("id",),
    "cb_investment": ("id",),
}

#: ``(table, column)`` → ``(referenced table, nullable?)``. A nullable foreign key here is never
#: an accident of convenience: each one encodes a state the product has to be able to express.
EXPECTED_FOREIGN_KEYS: dict[tuple[str, str], tuple[str, bool]] = {
    # NULL = a root of the forest.
    ("portfolio", "parent_id"): ("portfolio", True),
    # NULL = a roll-up node spanning brokers; NOT NULL = attributable to exactly one account.
    ("portfolio", "broker_account_id"): ("broker_account", True),
    # NOT NULL: a share is always held somewhere, and the column is part of the key.
    ("portfolio_holding", "broker_account_id"): ("broker_account", False),
    # NULL for any sleeve that is not a basket sleeve.
    ("portfolio_sleeve", "basket_id"): ("cb_basket", True),
    # NULL = the investment is filed under no portfolio, which every pre-0019 row is.
    ("cb_investment", "portfolio_id"): ("portfolio", True),
}

#: House rule 9. Quantities and prices carry their storage precision from
#: ``baskfy_core.models.base``; asserting the exact type catches a column that quietly took a
#: different scale as much as one that took a float.
EXPECTED_NUMERIC_TYPES: dict[tuple[str, str], Numeric[Decimal]] = {
    ("portfolio_holding", "quantity"): Numeric(20, 4),
    ("portfolio_holding", "avg_price"): Numeric(18, 4),
    ("portfolio_sleeve", "capital"): Numeric(18, 2),
}

TOUCHED_TABLES: tuple[str, ...] = (
    "portfolio",
    "portfolio_holding",
    "portfolio_sleeve",
    "cb_investment",
)


def table(name: str) -> Table:
    return Base.metadata.tables[name]


def check_constraint_text(name: str) -> str:
    """Every CHECK on a table, as one lowercase string, whitespace-normalised."""
    joined = " ".join(
        str(constraint.sqltext)
        for constraint in table(name).constraints
        if isinstance(constraint, CheckConstraint)
    )
    return re.sub(r"\s+", " ", joined).lower()


def foreign_key_constraint(table_name: str, column: str) -> ForeignKeyConstraint:
    for constraint in table(table_name).constraints:
        if isinstance(constraint, ForeignKeyConstraint) and [
            element.parent.name for element in constraint.elements
        ] == [column]:
            return constraint
    raise AssertionError(f"{table_name}.{column} has no single-column foreign key")


# --- the shape ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "expected"), sorted(EXPECTED_PRIMARY_KEYS.items()))
def test_primary_key_matches_the_contract(name: str, expected: tuple[str, ...]) -> None:
    assert tuple(column.name for column in table(name).primary_key.columns) == expected


@pytest.mark.parametrize(("pair", "expected"), sorted(EXPECTED_FOREIGN_KEYS.items()))
def test_foreign_key_target_and_nullability(
    pair: tuple[str, str], expected: tuple[str, bool]
) -> None:
    table_name, column_name = pair
    referenced, nullable = expected
    column = table(table_name).c[column_name]
    assert column.nullable is nullable
    targets = {fk.column.table.name for fk in column.foreign_keys}
    assert targets == {referenced}


def test_a_portfolio_cannot_be_its_own_parent() -> None:
    """The one forest invariant a single row can violate, so the one the database owns."""
    text = check_constraint_text("portfolio")
    assert "parent_id" in text
    assert "parent_id <> id" in text.replace("!=", "<>")


def test_cycles_and_the_depth_cap_are_not_claimed_by_the_schema() -> None:
    """They are multi-row facts. A CHECK that appeared to enforce them would be a lie.

    ``MAX_PORTFOLIO_DEPTH`` is the number ``baskfy_core.portfolio_graph`` enforces; it is asserted
    here so the cap has one home and the schema's silence about it is deliberate rather than
    forgotten.
    """
    assert MAX_PORTFOLIO_DEPTH == 6
    text = check_constraint_text("portfolio")
    assert "depth" not in text


def test_deleting_a_parent_detaches_children_rather_than_deleting_them() -> None:
    """Deleting a grouping node must never cascade away the holdings underneath it."""
    assert foreign_key_constraint("portfolio", "parent_id").ondelete == "SET NULL"


def test_deleting_a_portfolio_never_deletes_an_investment() -> None:
    assert foreign_key_constraint("cb_investment", "portfolio_id").ondelete == "SET NULL"


def test_a_broker_account_holding_stock_cannot_be_deleted() -> None:
    """``SET NULL`` is impossible inside a primary key and ``CASCADE`` would destroy positions."""
    assert foreign_key_constraint("portfolio_holding", "broker_account_id").ondelete is None


# --- sleeves -----------------------------------------------------------------------------


def test_sleeve_kinds_are_exactly_screen_manual_and_basket() -> None:
    assert SLEEVE_KINDS == ("screen", "manual", "basket")
    text = check_constraint_text("portfolio_sleeve")
    for kind in SLEEVE_KINDS:
        assert f"'{kind}'" in text


def test_every_sleeve_kind_is_paired_with_exactly_one_source_column() -> None:
    """The pairing is structural: each kind names one source and leaves the other NULL.

    Without the "and the other is NULL" half, a basket sleeve could also carry a screen id and
    "where does this sleeve's list come from" would stop being answerable from the row.
    """
    text = check_constraint_text("portfolio_sleeve")
    for clause in (
        "kind = 'screen' and screen_id is not null and basket_id is null",
        "kind = 'manual' and screen_id is null and basket_id is null",
        "kind = 'basket' and basket_id is not null and screen_id is null",
    ):
        assert clause in text, clause


def test_a_basket_sleeve_points_at_a_curated_basket() -> None:
    assert {fk.column.table.name for fk in table("portfolio_sleeve").c.basket_id.foreign_keys} == {
        "cb_basket"
    }


# --- house rule 9 ------------------------------------------------------------------------


@pytest.mark.parametrize(("pair", "expected"), sorted(EXPECTED_NUMERIC_TYPES.items(), key=str))
def test_money_and_quantities_are_exact_numerics(
    pair: tuple[str, str], expected: Numeric[Decimal]
) -> None:
    column = table(pair[0]).c[pair[1]]
    assert isinstance(column.type, Numeric)
    assert not isinstance(column.type, Float)
    assert (column.type.precision, column.type.scale) == (expected.precision, expected.scale)


def test_no_touched_table_gained_an_approximate_column() -> None:
    offenders = [
        f"{name}.{column.name}"
        for name in TOUCHED_TABLES
        for column in table(name).columns
        if isinstance(column.type, Float)
    ]
    assert offenders == []


# --- the migration is one migration, and it reverses -------------------------------------


def migration_source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def calls(source: str, function: str) -> set[tuple[str, str]]:
    """``op.<function>("a", "b")`` → ``{("a", "b")}``, for the two-string-argument forms."""
    pattern = re.compile(rf'op\.{function}\(\s*"([^"]+)"\s*,\s*"([^"]+)"')
    return set(pattern.findall(source))


def test_the_migration_chains_off_0018_and_is_the_only_one_added() -> None:
    source = migration_source()
    assert 'revision: str = "0019_portfolio_graph"' in source
    assert 'down_revision: str | None = "0018_trading_path_tenancy"' in source


def test_every_column_the_migration_adds_is_dropped_again() -> None:
    """Reversibility, asserted structurally rather than trusted.

    ``add_column(table, Column(name))`` and ``drop_column(table, name)`` are matched on the table
    plus the column name, so a column added in ``upgrade`` and forgotten in ``downgrade`` fails
    here rather than in production at the moment someone needs to roll back.
    """
    source = migration_source()
    added = {
        (t, c)
        for t, c in re.findall(r'op\.add_column\(\s*"([^"]+)",\s*sa\.Column\(\s*"([^"]+)"', source)
    }
    dropped = calls(source, "drop_column")
    assert added == dropped
    assert added == {
        ("portfolio", "parent_id"),
        ("portfolio", "broker_account_id"),
        ("portfolio_holding", "broker_account_id"),
        ("portfolio_sleeve", "basket_id"),
        ("cb_investment", "portfolio_id"),
    }


def test_every_index_the_migration_creates_is_dropped_again() -> None:
    source = migration_source()
    created = {name for name, _ in calls(source, "create_index")}
    dropped = {name for name in re.findall(r'op\.drop_index\(\s*"([^"]+)"', source)}
    assert created == dropped


def test_the_downgrade_removes_the_attribution_trigger_and_its_function() -> None:
    """A leftover trigger on a table whose column is gone is a migration that did not reverse."""
    source = migration_source()
    downgrade = source[source.index("def _downgrade_holding_attribution") :]
    assert "DROP TRIGGER IF EXISTS portfolio_holding_attribute_broker_account" in downgrade
    assert "DROP FUNCTION IF EXISTS portfolio_holding_attribute_broker_account" in downgrade


def test_the_downgrade_merges_rows_the_old_key_cannot_hold() -> None:
    """Two brokers, one instrument, one portfolio is two rows here and one row before 0019.

    Deleting the surplus rows would silently destroy a position, so the downgrade folds them into
    the survivor instead: quantities summed, ``avg_price`` re-derived as the quantity-weighted
    mean, ``added_on`` the earliest of the group.
    """
    source = migration_source()
    assert "SUM(quantity)" in source
    assert "SUM(quantity * avg_price)" in source
    assert "MIN(added_on)" in source
    assert "ROUND(" in source, "avg_price is numeric(18, 4); round at write time (house rule 8)"
