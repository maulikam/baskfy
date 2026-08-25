"""Nested portfolios: the shapes that must be refused, and the ones that must survive intact.

These assert the *spec* (house rule 2), so they are written from the four promises the module
makes rather than from its implementation: no ring of parents, no more than six levels, nothing
lost or invented by assembly, and every rupee attributed to a broker or visibly not attributed
at all.

Four of them earn their place by being the mistakes a smaller implementation makes:

* a three-node ring, because "is the new parent this portfolio?" catches only the one-node case;
* an orphan, because dropping it loses money from a total and promoting it renders somebody
  else's portfolio as if it were yours — and both look like success from the outside;
* a legal parent plus a legal subtree that are illegal together, because the depth arithmetic
  has to be done on the pair;
* a ``float`` price, because ``Decimal(0.1)`` is not ``0.1`` and the resulting number is wrong
  in a way no test of the happy path would ever show.

Pure throughout: no database, no fixtures, no clock.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import cast

import pytest

from baskfy_core.portfolio_graph import (
    MAX_DEPTH,
    ROOT_DEPTH,
    AmountTypeError,
    BrokerRollup,
    CycleError,
    DepthExceededError,
    DuplicatePortfolioError,
    Holding,
    OrphanError,
    PortfolioNode,
    Totals,
    TreeNode,
    UnknownPortfolioError,
    assemble_tree,
    check_depths,
    check_move,
    check_no_cycles,
    depth_of,
    descendant_ids,
    find_cycle,
    rollup_by_broker,
    subtree_height,
)


def node(
    pid: int,
    parent: int | None = None,
    broker: int | None = None,
    name: str | None = None,
) -> PortfolioNode:
    return PortfolioNode(
        id=pid,
        name=name if name is not None else f"P{pid}",
        parent_id=parent,
        broker_account_id=broker,
    )


def chain(length: int) -> list[PortfolioNode]:
    """Portfolios 1..length, each the child of the one before. Portfolio ``k`` sits at depth k."""
    return [node(1), *(node(k, parent=k - 1) for k in range(2, length + 1))]


def holding(
    portfolio_id: int,
    broker: int | None,
    quantity: str,
    avg_price: str,
    instrument: int = 1,
) -> Holding:
    return Holding(
        portfolio_id=portfolio_id,
        instrument_id=instrument,
        broker_account_id=broker,
        quantity=Decimal(quantity),
        avg_price=Decimal(avg_price),
    )


def bad_holding(quantity: object = Decimal(1), avg_price: object = Decimal(1)) -> Holding:
    """Build a holding from values the type checker would never allow.

    ``cast`` rather than a checker-suppression comment, which house rule 3 forbids outright and
    ``test_no_escape_hatches.py`` greps the whole tree for — so this docstring cannot spell one
    either. These tests exist precisely because the static contract can be bypassed: by JSON, by
    a broker payload, by an untyped caller. The runtime refusal is the thing being asserted.
    """
    return Holding(
        portfolio_id=1,
        instrument_id=1,
        broker_account_id=1,
        quantity=cast(Decimal, quantity),
        avg_price=cast(Decimal, avg_price),
    )


def ids_of(nodes: tuple[TreeNode, ...]) -> list[int]:
    return [child.id for child in nodes]


class TestASelfParentIsRefused:
    def test_a_self_parent_is_refused(self) -> None:
        """A portfolio inside itself is a ring of one, and it hangs every traversal."""
        rows = [node(1, parent=1)]

        assert find_cycle(rows) == (1, 1)
        with pytest.raises(CycleError) as raised:
            check_no_cycles(rows)
        assert raised.value.cycle == (1, 1)

    def test_a_self_parent_hidden_among_healthy_rows_is_still_refused(self) -> None:
        """The scan is over every row, not over the first one that looks suspicious."""
        rows = [node(1), node(2, parent=1), node(3, parent=1), node(9, parent=9)]

        with pytest.raises(CycleError) as raised:
            check_no_cycles(rows)
        assert raised.value.cycle == (9, 9)

    def test_moving_a_portfolio_onto_itself_is_refused_as_a_self_parent_cycle(self) -> None:
        rows = [node(1), node(2, parent=1)]

        with pytest.raises(CycleError) as raised:
            check_move(rows, portfolio_id=2, new_parent_id=2)
        assert raised.value.cycle == (2, 2)

    def test_assembly_refuses_a_self_parent_rather_than_dropping_the_row(self) -> None:
        """It is neither a root nor an orphan, so a naive assembler loses it without a word."""
        with pytest.raises(CycleError):
            assemble_tree([node(1), node(2, parent=2)])


class TestCyclesOfEveryLength:
    def test_a_two_node_cycle_is_refused(self) -> None:
        rows = [node(1, parent=2), node(2, parent=1)]

        cycle = find_cycle(rows)
        assert cycle is not None
        assert cycle[0] == cycle[-1]
        assert set(cycle) == {1, 2}

    def test_a_three_node_cycle_is_refused(self) -> None:
        """A→B→C→A. The two-node check that catches most bugs does not catch this one."""
        rows = [node(1, parent=2), node(2, parent=3), node(3, parent=1)]

        with pytest.raises(CycleError) as raised:
            check_no_cycles(rows)
        assert raised.value.cycle == (1, 2, 3, 1)

    def test_a_long_cycle_is_refused_at_any_length(self) -> None:
        """Nothing about the search is tuned to a particular ring size."""
        size = 12
        rows = [node(k, parent=k % size + 1) for k in range(1, size + 1)]

        cycle = find_cycle(rows)
        assert cycle is not None
        assert len(cycle) == size + 1
        assert cycle[0] == cycle[-1]

    def test_a_cycle_reachable_only_from_outside_it_is_still_found(self) -> None:
        """The offending row need not be one of the rows the walk starts from."""
        rows = [node(4, parent=1), node(1, parent=2), node(2, parent=3), node(3, parent=1)]

        with pytest.raises(CycleError):
            check_no_cycles(rows)

    def test_a_healthy_forest_reports_no_cycle(self) -> None:
        """The refusal must not fire on shared ancestry, which looks like revisiting a node."""
        rows = [node(1), node(2, parent=1), node(3, parent=1), node(4, parent=2), node(5)]

        assert find_cycle(rows) is None
        check_no_cycles(rows)

    def test_moving_a_portfolio_under_its_own_descendant_is_a_cycle(self) -> None:
        """The branch would float free of every root — a ring nothing can reach or repair."""
        rows = [node(1), node(2, parent=1), node(3, parent=2)]

        with pytest.raises(CycleError) as raised:
            check_move(rows, portfolio_id=1, new_parent_id=3)
        assert raised.value.cycle == (1, 3, 2, 1)

    def test_moving_a_portfolio_under_an_unrelated_branch_is_not_a_cycle(self) -> None:
        rows = [node(1), node(2, parent=1), node(3)]

        check_move(rows, portfolio_id=2, new_parent_id=3)

    def test_the_reported_cycle_names_the_offending_chain(self) -> None:
        """The error carries the ring, so an operator is told which rows to fix."""
        with pytest.raises(CycleError) as raised:
            check_no_cycles([node(1, parent=2), node(2, parent=1)])
        assert "->" in str(raised.value)


class TestTheDepthCap:
    def test_the_depth_of_a_root_is_one(self) -> None:
        """Pinned, because 'is a root at 0 or 1?' turns a cap of six into a cap of seven."""
        assert depth_of([node(1)], 1) == ROOT_DEPTH
        assert ROOT_DEPTH == 1

    def test_depth_is_counted_from_the_root(self) -> None:
        rows = chain(4)

        assert [depth_of(rows, k) for k in (1, 2, 3, 4)] == [1, 2, 3, 4]

    def test_a_chain_of_exactly_six_sits_at_the_depth_cap_and_is_accepted(self) -> None:
        rows = chain(MAX_DEPTH)

        assert depth_of(rows, MAX_DEPTH) == 6
        check_depths(rows)
        forest = assemble_tree(rows)
        assert forest.size == MAX_DEPTH

    def test_a_chain_of_seven_breaches_the_depth_cap_and_is_refused(self) -> None:
        rows = chain(MAX_DEPTH + 1)

        with pytest.raises(DepthExceededError) as raised:
            check_depths(rows)
        assert raised.value.portfolio_id == MAX_DEPTH + 1
        assert raised.value.depth == 7
        assert raised.value.max_depth == MAX_DEPTH

    def test_assembly_refuses_a_stored_tree_deeper_than_the_cap(self) -> None:
        """Reading damage silently spreads it into whatever renders the tree."""
        with pytest.raises(DepthExceededError):
            assemble_tree(chain(MAX_DEPTH + 1))

    def test_assembly_can_be_asked_to_ignore_the_depth_cap_for_repair(self) -> None:
        """A tool that fixes an over-deep tree has to be able to see it first."""
        forest = assemble_tree(chain(MAX_DEPTH + 3), max_depth=None)

        assert forest.size == MAX_DEPTH + 3

    def test_only_the_deep_branch_is_refused_not_the_shallow_sibling(self) -> None:
        rows = [*chain(MAX_DEPTH + 1), node(100, parent=1)]

        with pytest.raises(DepthExceededError) as raised:
            check_depths(rows)
        assert raised.value.portfolio_id == MAX_DEPTH + 1

    def test_the_depth_of_an_orphan_is_refused_rather_than_guessed(self) -> None:
        """Returning a relative depth as if it were absolute is how an orphan becomes a root."""
        rows = [node(5, parent=99), node(6, parent=5)]

        with pytest.raises(OrphanError) as raised:
            depth_of(rows, 6)
        assert raised.value.portfolio_id == 5
        assert raised.value.missing_parent_id == 99

    def test_an_orphan_fragment_is_depth_checked_as_a_lower_bound(self) -> None:
        """The rows above the fragment can only add depth, so a breach here is a certain breach."""
        rows = [node(1, parent=99), *(node(k, parent=k - 1) for k in range(2, MAX_DEPTH + 2))]

        with pytest.raises(DepthExceededError):
            check_depths(rows)

    def test_a_shallow_orphan_fragment_does_not_breach_the_depth_cap(self) -> None:
        """Unproven is not the same as broken; the lower bound must never raise falsely."""
        check_depths([node(1, parent=99), node(2, parent=1)])

    def test_a_move_that_would_push_descendants_past_the_depth_cap_is_refused(self) -> None:
        """A legal parent and a legal subtree can still be illegal together."""
        rows = [*chain(5), node(50), node(51, parent=50), node(52, parent=51)]

        with pytest.raises(DepthExceededError) as raised:
            check_move(rows, portfolio_id=50, new_parent_id=5)
        assert raised.value.depth == 8

    def test_a_move_that_lands_exactly_on_the_depth_cap_is_allowed(self) -> None:
        """A parent at depth 3 plus a subtree three levels tall is exactly six, which is legal."""
        rows = [*chain(3), node(50), node(51, parent=50), node(52, parent=51)]

        check_move(rows, portfolio_id=50, new_parent_id=3)

    def test_promoting_a_subtree_to_a_root_never_breaches_the_depth_cap(self) -> None:
        rows = chain(MAX_DEPTH)

        check_move(rows, portfolio_id=4, new_parent_id=None)

    def test_a_move_onto_a_parent_outside_the_set_is_refused(self) -> None:
        """Existence is never leaked: another user's portfolio is simply not in the set."""
        with pytest.raises(OrphanError):
            check_move([node(1)], portfolio_id=1, new_parent_id=77)

    def test_a_move_of_a_portfolio_outside_the_set_is_refused(self) -> None:
        with pytest.raises(UnknownPortfolioError):
            check_move([node(1)], portfolio_id=77, new_parent_id=1)

    def test_a_move_under_a_node_whose_own_chain_dangles_is_refused(self) -> None:
        """The new parent is present but its depth is unknown, so the result's depth is too.

        Refusing is the conservative reading: the invisible rows above the fragment can only
        make the move deeper, never shallower.
        """
        rows = [node(1), node(5, parent=99), node(6, parent=5)]

        with pytest.raises(OrphanError) as raised:
            check_move(rows, portfolio_id=1, new_parent_id=6)
        assert raised.value.missing_parent_id == 99


class TestTreeAssembly:
    def test_assemble_nests_children_under_their_parents(self) -> None:
        rows = [node(1), node(2, parent=1), node(3, parent=1), node(4, parent=2)]

        forest = assemble_tree(rows)

        assert ids_of(forest.roots) == [1]
        root = forest.roots[0]
        assert ids_of(root.children) == [2, 3]
        assert ids_of(root.children[0].children) == [4]
        assert root.children[0].children[0].children == ()

    def test_the_assembled_tree_carries_the_depth_of_every_node(self) -> None:
        forest = assemble_tree(chain(3))

        root = forest.roots[0]
        assert root.depth == 1
        assert root.children[0].depth == 2
        assert root.children[0].children[0].depth == 3

    def test_assemble_keeps_several_roots_in_input_order(self) -> None:
        """No display decision is made here — a caller that wants order sorts its query."""
        rows = [node(30, name="Zulu"), node(10, name="Alpha"), node(20, name="Mike")]

        assert ids_of(assemble_tree(rows).roots) == [30, 10, 20]

    def test_assemble_keeps_siblings_in_input_order(self) -> None:
        rows = [node(1), node(9, parent=1), node(2, parent=1), node(5, parent=1)]

        assert ids_of(assemble_tree(rows).roots[0].children) == [9, 2, 5]

    def test_assemble_handles_a_child_that_arrives_before_its_parent(self) -> None:
        """A query with no ORDER BY may hand back the child first; nesting must not care."""
        rows = [node(2, parent=1), node(1)]

        forest = assemble_tree(rows)

        assert ids_of(forest.roots) == [1]
        assert ids_of(forest.roots[0].children) == [2]

    def test_an_orphan_surfaces_instead_of_vanishing(self) -> None:
        """Dropping it would delete its holdings from every total computed above it."""
        rows = [node(1), node(7, parent=99)]

        forest = assemble_tree(rows)

        assert ids_of(forest.roots) == [1]
        assert len(forest.orphans) == 1
        assert forest.orphans[0].portfolio.id == 7
        assert forest.orphans[0].missing_parent_id == 99

    def test_an_orphan_is_never_silently_promoted_to_a_root(self) -> None:
        """A root is a deliberate top; an orphan is a dangling reference. Different situations."""
        forest = assemble_tree([node(7, parent=99)])

        assert forest.roots == ()
        assert [orphan.portfolio.id for orphan in forest.orphans] == [7]

    def test_an_orphans_own_children_ride_with_it_and_are_not_orphans_themselves(self) -> None:
        rows = [node(7, parent=99), node(8, parent=7), node(9, parent=8)]

        forest = assemble_tree(rows)

        assert len(forest.orphans) == 1
        fragment = forest.orphans[0].subtree
        assert ids_of(fragment.children) == [8]
        assert ids_of(fragment.children[0].children) == [9]

    def test_assembly_never_loses_a_portfolio(self) -> None:
        """Roots, orphans and every descendant add back up to the rows supplied."""
        rows = [
            node(1),
            node(2, parent=1),
            node(3, parent=2),
            node(4),
            node(5, parent=88),
            node(6, parent=5),
        ]

        assert assemble_tree(rows).size == len(rows)

    def test_an_empty_set_assembles_to_an_empty_forest(self) -> None:
        forest = assemble_tree([])

        assert forest.roots == ()
        assert forest.orphans == ()
        assert forest.size == 0

    def test_a_duplicate_id_is_refused_rather_than_silently_deduplicated(self) -> None:
        """Which row survived would otherwise depend on iteration order."""
        with pytest.raises(DuplicatePortfolioError) as raised:
            assemble_tree([node(1), node(1, name="the other one")])
        assert raised.value.portfolio_id == 1

    def test_descendant_ids_include_the_subtree_root_itself(self) -> None:
        rows = [node(1), node(2, parent=1), node(3, parent=2), node(4)]

        assert descendant_ids(rows, 1) == (1, 2, 3)
        assert descendant_ids(rows, 2) == (2, 3)
        assert descendant_ids(rows, 4) == (4,)

    def test_descendant_ids_refuse_a_root_outside_the_set(self) -> None:
        with pytest.raises(UnknownPortfolioError):
            descendant_ids([node(1)], 77)

    def test_subtree_height_counts_the_root_as_one_level(self) -> None:
        rows = [node(1), node(2, parent=1), node(3, parent=2), node(4, parent=1)]

        assert subtree_height(rows, 1) == 3
        assert subtree_height(rows, 4) == 1


class TestPerBrokerRollup:
    def test_rollup_sums_one_brokers_holdings_across_the_subtree(self) -> None:
        rows = [node(1, broker=10), node(2, parent=1, broker=10)]
        holdings = [
            holding(1, broker=10, quantity="5", avg_price="100", instrument=1),
            holding(2, broker=10, quantity="3", avg_price="200", instrument=2),
        ]

        result = rollup_by_broker(rows, holdings, 1)

        assert result.broker_account_ids == (10,)
        line = result.line_for(10)
        assert line is not None
        assert line.totals == Totals(quantity=Decimal(8), cost=Decimal(1100), holdings=2)

    def test_a_node_whose_children_span_brokers_reports_each_broker_plus_a_total(self) -> None:
        """The whole point of the roll-up node: three brokers, three lines, one total."""
        rows = [
            node(1, broker=None),
            node(2, parent=1, broker=10),
            node(3, parent=1, broker=20),
            node(4, parent=1, broker=30),
        ]
        holdings = [
            holding(2, broker=10, quantity="10", avg_price="50", instrument=1),
            holding(3, broker=20, quantity="4", avg_price="25", instrument=2),
            holding(4, broker=30, quantity="1", avg_price="1000", instrument=3),
        ]

        result = rollup_by_broker(rows, holdings, 1)

        assert result.broker_account_ids == (10, 20, 30)
        assert [line.totals.cost for line in result.by_broker] == [
            Decimal(500),
            Decimal(100),
            Decimal(1000),
        ]
        assert result.total == Totals(quantity=Decimal(15), cost=Decimal(1600), holdings=3)
        assert result.spans_brokers is True

    def test_the_total_is_the_lines_plus_the_unattributed_part(self) -> None:
        rows = [node(1), node(2, parent=1)]
        holdings = [
            holding(1, broker=10, quantity="2", avg_price="100", instrument=1),
            holding(2, broker=20, quantity="3", avg_price="100", instrument=2),
            holding(2, broker=None, quantity="4", avg_price="100", instrument=3),
        ]

        result = rollup_by_broker(rows, holdings, 1)
        summed = result.unattributed
        for line in result.by_broker:
            summed = summed + line.totals

        assert summed == result.total

    def test_rollup_covers_the_whole_subtree_not_only_the_node_asked_about(self) -> None:
        rows = [node(1), node(2, parent=1), node(3, parent=2)]
        holdings = [holding(3, broker=10, quantity="7", avg_price="10")]

        assert rollup_by_broker(rows, holdings, 1).total.quantity == Decimal(7)
        assert rollup_by_broker(rows, holdings, 2).total.quantity == Decimal(7)
        assert rollup_by_broker(rows, holdings, 3).total.quantity == Decimal(7)

    def test_rollup_ignores_holdings_outside_the_subtree(self) -> None:
        """A sibling's money — or another user's — must not leak into this number."""
        rows = [node(1), node(2, parent=1), node(3)]
        holdings = [
            holding(2, broker=10, quantity="1", avg_price="100", instrument=1),
            holding(3, broker=10, quantity="99", avg_price="100", instrument=2),
            holding(404, broker=10, quantity="99", avg_price="100", instrument=3),
        ]

        assert rollup_by_broker(rows, holdings, 1).total.quantity == Decimal(1)

    def test_broker_lines_are_ordered_by_account_id_so_two_runs_agree(self) -> None:
        rows = [node(1)]
        holdings = [
            holding(1, broker=30, quantity="1", avg_price="1", instrument=1),
            holding(1, broker=10, quantity="1", avg_price="1", instrument=2),
            holding(1, broker=20, quantity="1", avg_price="1", instrument=3),
        ]

        assert rollup_by_broker(rows, holdings, 1).broker_account_ids == (10, 20, 30)

    def test_a_leaf_with_no_holdings_rolls_up_to_zero_not_to_nothing(self) -> None:
        result = rollup_by_broker([node(1, broker=10)], [], 1)

        assert result.by_broker == ()
        assert result.total == Totals()
        assert result.spans_brokers is False

    def test_a_rollup_at_a_child_excludes_the_money_held_above_it(self) -> None:
        """A subtree total looks up its own descendants, never its ancestors."""
        rows = [node(1), node(2, parent=1)]
        holdings = [
            holding(1, broker=10, quantity="100", avg_price="1", instrument=1),
            holding(2, broker=10, quantity="7", avg_price="1", instrument=2),
        ]

        assert rollup_by_broker(rows, holdings, 2).total.quantity == Decimal(7)
        assert rollup_by_broker(rows, holdings, 1).total.quantity == Decimal(107)

    def test_rollup_refuses_a_subtree_root_outside_the_set(self) -> None:
        with pytest.raises(UnknownPortfolioError):
            rollup_by_broker([node(1)], [], 77)

    def test_rollup_refuses_a_cyclic_set_rather_than_looping_forever(self) -> None:
        with pytest.raises(CycleError):
            rollup_by_broker([node(1, parent=2), node(2, parent=1)], [], 1)


class TestSpansBrokersIsNotUnknownBroker:
    def test_an_unknown_broker_holding_never_appears_as_a_broker_line(self) -> None:
        """Money nobody could attribute must not hide inside a broker's number."""
        rows = [node(1, broker=None)]
        holdings = [
            holding(1, broker=10, quantity="2", avg_price="100", instrument=1),
            holding(1, broker=None, quantity="5", avg_price="100", instrument=2),
        ]

        result = rollup_by_broker(rows, holdings, 1)

        assert result.broker_account_ids == (10,)
        assert result.line_for(10) is not None
        assert result.unattributed.quantity == Decimal(5)
        assert result.total.quantity == Decimal(7)

    def test_a_portfolio_that_spans_brokers_is_reported_as_a_declaration_not_a_bucket(
        self,
    ) -> None:
        """``portfolio.broker_account_id IS NULL`` is a label on the container, not a key."""
        rows = [node(1, broker=None), node(2, parent=1, broker=10)]
        holdings = [holding(2, broker=10, quantity="1", avg_price="100")]

        result = rollup_by_broker(rows, holdings, 1)

        assert result.declared_broker_account_id is None
        assert result.broker_account_ids == (10,)
        assert result.unattributed == Totals()

    def test_a_holdings_broker_is_never_inferred_from_its_portfolios_declaration(self) -> None:
        """The portfolio column is somebody's label; the holding column is where shares sit."""
        rows = [node(1, broker=10)]
        holdings = [holding(1, broker=None, quantity="3", avg_price="100")]

        result = rollup_by_broker(rows, holdings, 1)

        assert result.declared_broker_account_id == 10
        assert result.broker_account_ids == ()
        assert result.unattributed.quantity == Decimal(3)

    def test_spans_brokers_counts_unattributed_money_as_a_second_place(self) -> None:
        """One broker plus a hole is not a single-broker subtree; it is a subtree with a hole."""
        rows = [node(1)]
        holdings = [
            holding(1, broker=10, quantity="1", avg_price="1", instrument=1),
            holding(1, broker=None, quantity="1", avg_price="1", instrument=2),
        ]

        assert rollup_by_broker(rows, holdings, 1).spans_brokers is True

    def test_a_declaration_conflicts_when_the_subtree_holds_another_broker(self) -> None:
        rows = [node(1, broker=10), node(2, parent=1, broker=20)]
        holdings = [holding(2, broker=20, quantity="1", avg_price="1")]

        assert rollup_by_broker(rows, holdings, 1).declaration_conflicts is True

    def test_a_declaration_conflicts_when_the_subtree_holds_unattributed_money(self) -> None:
        rows = [node(1, broker=10)]
        holdings = [holding(1, broker=None, quantity="1", avg_price="1")]

        assert rollup_by_broker(rows, holdings, 1).declaration_conflicts is True

    def test_a_spanning_declaration_holding_one_broker_today_is_not_a_conflict(self) -> None:
        """The declaration is about what the container is for, not what is in it this morning."""
        rows = [node(1, broker=None)]
        holdings = [holding(1, broker=10, quantity="1", avg_price="1")]

        assert rollup_by_broker(rows, holdings, 1).declaration_conflicts is False

    def test_a_matching_declaration_is_not_a_conflict(self) -> None:
        rows = [node(1, broker=10)]
        holdings = [holding(1, broker=10, quantity="1", avg_price="1")]

        assert rollup_by_broker(rows, holdings, 1).declaration_conflicts is False


class TestMoneyIsDecimalNeverFloat:
    def test_a_float_quantity_is_refused_and_not_converted(self) -> None:
        """``Decimal(0.1)`` launders a loss that already happened; it does not undo it."""
        with pytest.raises(AmountTypeError) as raised:
            bad_holding(quantity=0.1)
        assert raised.value.field == "quantity"

    def test_a_float_price_is_refused_and_not_converted(self) -> None:
        with pytest.raises(AmountTypeError) as raised:
            bad_holding(avg_price=1234.56)
        assert raised.value.field == "avg_price"

    def test_a_float_cannot_enter_a_totals_line_either(self) -> None:
        with pytest.raises(AmountTypeError):
            Totals(quantity=cast(Decimal, 1.0))

    def test_an_int_is_accepted_and_converted_exactly_to_a_decimal(self) -> None:
        """``Decimal(10)`` is exactly 10, so nothing is invented by accepting it."""
        built = bad_holding(quantity=10, avg_price=250)

        assert built.quantity == Decimal(10)
        assert isinstance(built.quantity, Decimal)
        assert isinstance(built.avg_price, Decimal)

    def test_a_bool_is_refused_because_it_is_not_a_decimal_despite_being_an_int(self) -> None:
        """A quantity of ``True`` is a bug wearing a number's clothes."""
        with pytest.raises(AmountTypeError):
            bad_holding(quantity=True)

    def test_a_string_is_refused_rather_than_parsed_into_a_decimal(self) -> None:
        """Parsing here would move the money boundary somewhere nobody can find it."""
        with pytest.raises(AmountTypeError):
            bad_holding(quantity="10")

    def test_a_non_finite_decimal_is_refused_because_it_poisons_every_sum(self) -> None:
        for poison in ("NaN", "Infinity", "-Infinity"):
            with pytest.raises(AmountTypeError):
                bad_holding(quantity=Decimal(poison))

    def test_every_number_the_rollup_returns_is_a_decimal(self) -> None:
        rows = [node(1), node(2, parent=1)]
        holdings = [
            holding(1, broker=10, quantity="1.5", avg_price="99.95", instrument=1),
            holding(2, broker=20, quantity="2.25", avg_price="10.05", instrument=2),
            holding(2, broker=None, quantity="0.75", avg_price="3.33", instrument=3),
        ]

        result = rollup_by_broker(rows, holdings, 1)

        lines = (result.total, result.unattributed, *(x.totals for x in result.by_broker))
        for totals in lines:
            # ``not isinstance(x, float)`` is not written here: mypy calls it unreachable,
            # because Decimal and float are disjoint bases and the isinstance above already
            # settles it. Finiteness is the part a type cannot promise.
            assert isinstance(totals.quantity, Decimal)
            assert isinstance(totals.cost, Decimal)
            assert totals.quantity.is_finite()
            assert totals.cost.is_finite()

    def test_the_rollup_is_exact_where_float_arithmetic_would_drift(self) -> None:
        """Three lots of 0.1 are 0.3. In binary floating point they are 0.30000000000000004."""
        rows = [node(1)]
        holdings = [
            holding(1, broker=10, quantity="1", avg_price="0.1", instrument=k) for k in (1, 2, 3)
        ]

        total = rollup_by_broker(rows, holdings, 1).total

        assert total.cost == Decimal("0.3")
        assert str(total.cost) == "0.3"
        assert total.cost != Decimal(repr(0.1 + 0.1 + 0.1))

    def test_a_decimal_sum_of_many_paise_does_not_lose_a_paisa(self) -> None:
        """Summed as floats, a hundred lots of 0.01 do not equal 1.00."""
        rows = [node(1)]
        holdings = [
            holding(1, broker=10, quantity="1", avg_price="0.01", instrument=k) for k in range(100)
        ]

        assert rollup_by_broker(rows, holdings, 1).total.cost == Decimal("1.00")


class TestTheResultShapesAreImmutable:
    def test_a_rollup_cannot_be_edited_after_it_is_returned(self) -> None:
        """Frozen throughout, so no caller can rewrite a total in place and blame the module.

        Reached through ``setattr`` rather than a plain assignment because the type checker
        refuses the plain assignment outright — which is the static half of the same guarantee.
        """
        result = rollup_by_broker([node(1)], [], 1)
        # The attribute name is a variable because a literal one is a lint error and a literal
        # assignment is a type error — two more layers of the same guarantee, neither of which
        # proves the runtime refusal that actually protects a caller.
        frozen_field = "portfolio_id"

        with pytest.raises(FrozenInstanceError):
            setattr(result, frozen_field, 2)

    def test_a_rollup_has_the_fields_the_api_layer_depends_on(self) -> None:
        result = rollup_by_broker([node(1, broker=10)], [], 1)

        assert isinstance(result, BrokerRollup)
        assert result.portfolio_id == 1
        assert result.declared_broker_account_id == 10
