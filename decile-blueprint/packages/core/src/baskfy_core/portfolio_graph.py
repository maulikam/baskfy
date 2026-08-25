"""Nested portfolios as a graph — cycles, depth, assembly, per-broker roll-up (tree 5, leaf A2).

A portfolio may sit inside another portfolio: "Retirement" holds "Equity" holds "Momentum 50".
That single nullable ``parent_id`` buys four questions this module answers, and nothing else:

1. **Is the parentage sane?** A portfolio cannot be its own parent, and no chain of them may
   close into a ring. A ring is not a display bug — it is an infinite loop in every consumer
   that walks the tree, so it is refused at the edge rather than defended against everywhere.
2. **Is it too deep?** Six levels is the deepest shape this product supports
   (:data:`MAX_DEPTH`). Exactly six is legal; seven is refused.
3. **What is the tree?** A flat sequence of rows in, children nested under parents out —
   plus, explicitly, the rows whose parent was *not in the sequence*.
4. **Whose money is where?** A subtree's holdings summed per ``broker_account_id``, each
   broker on its own line and a total alongside.

Pure, and the purity is load-bearing (Law 1 in the root working agreement). No database, no
network, no clock, no disk. It takes plain frozen dataclasses whose field names are the column
names of ``portfolio`` and ``portfolio_holding``, and it never learns where they came from.
That is what lets it be written and tested while the migration adding those columns is still
being written in another branch.

WHY FROZEN DATACLASSES AND NOT TypedDicts
-----------------------------------------
A ``TypedDict`` is a promise the type checker makes about a plain ``dict``, and this module's
hardest rule is a *runtime* one: house rule 9, money and quantities are ``Decimal`` and never
``float``. A ``TypedDict`` has no constructor, so there is no moment at which a rogue ``float``
arriving from JSON, a broker payload, or somebody's ``sum()`` can be caught — the annotation is
erased and the wrong number flows through. A frozen dataclass has ``__post_init__``, which is
exactly that moment. Frozen and ``slots=True`` also match how the rest of ``packages/core``
models its inputs (``sleeves.SleeveSpec``, ``portfolio_csv``), and immutability means a caller
cannot mutate a node underneath a traversal that is halfway through it.

WHY A ``float`` IS REFUSED RATHER THAN CONVERTED
------------------------------------------------
``Decimal(0.1)`` is ``0.1000000000000000055511151231257827021181583404541015625``. Converting
does not rescue the value: the precision was lost before this module ever saw it, and the
conversion only launders that loss into a type that looks exact. So a ``float`` raises
:class:`AmountTypeError` naming the field, at the boundary, where the caller can still find the
line that produced it. ``int`` *is* accepted and converted, because ``Decimal(10)`` is exactly
10 — no information is invented. Non-finite ``Decimal`` values (``NaN``, ``Infinity``) are
refused for the same reason a ``float`` is: they poison every sum they touch.

"SPANS BROKERS" IS NOT "UNKNOWN BROKER"
---------------------------------------
Two different nulls live in this schema and they mean opposite things:

* ``portfolio.broker_account_id IS NULL`` — the portfolio is a **roll-up node**; it deliberately
  spans brokers. This is a *label on a container*, an assertion by the person who made it.
* ``portfolio_holding.broker_account_id IS NULL`` — this holding's broker is **unknown**; the
  attribution is missing. This is *money whose location we cannot name*.

Collapsing those into one nullable key would let missing attribution hide inside a legitimate
roll-up, so they are represented by different mechanisms and can never be confused:

* the portfolio's declaration is carried through as
  :attr:`BrokerRollup.declared_broker_account_id`, and it is **never** a bucket key;
* unattributed money gets its own field, :attr:`BrokerRollup.unattributed`, which is not in
  :attr:`BrokerRollup.by_broker` at all. A caller iterating ``by_broker`` sees only real broker
  accounts; a caller that wants the whole subtree reads ``total``, which includes the
  unattributed part. There is no way to iterate the brokers and accidentally sum everything.

And a holding's broker is **never inferred** from its portfolio's declaration. The portfolio
column is somebody's label; the holding column is a fact about where shares actually sit. Filling
the fact in from the label would manufacture attribution that no broker ever confirmed.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not check ownership. Callers scope their query to one user before calling; a parent that
belongs to somebody else is simply *not in the sequence*, which surfaces here as an orphan — and
an orphan is what the router turns into ``NOT_FOUND``, so existence is never leaked.

It does not decide precision. Quantities and prices arrive already rounded at write time (house
rule 8); the sums here are exact ``Decimal`` arithmetic over them.

It does not know how a broker's quantity was assembled. Non-negotiable #2 says a holdings
quantity is ``quantity + t1_quantity + collateral_quantity``; that addition belongs to whoever
reads the broker, and :attr:`Holding.quantity` is expected to be that total already.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

__all__ = [
    "MAX_DEPTH",
    "ROOT_DEPTH",
    "AmountTypeError",
    "BrokerLine",
    "BrokerRollup",
    "CycleError",
    "DepthExceededError",
    "DuplicatePortfolioError",
    "Forest",
    "Holding",
    "Orphan",
    "OrphanError",
    "PortfolioGraphError",
    "PortfolioNode",
    "Totals",
    "TreeNode",
    "UnknownPortfolioError",
    "assemble_tree",
    "check_depths",
    "check_move",
    "check_no_cycles",
    "depth_of",
    "descendant_ids",
    "find_cycle",
    "rollup_by_broker",
    "subtree_height",
]

#: The deepest nesting this product supports, counted in portfolios. A root is depth 1, so a
#: chain of six portfolios is legal and a chain of seven is refused. Six is not arbitrary: it is
#: past the point where a person can hold the shape in their head, and every consumer of the tree
#: — the API response, the web tree view, the roll-up — pays for depth linearly.
MAX_DEPTH: Final = 6

#: The depth of a portfolio with no parent. Named because "is a root at 0 or at 1?" is the sort
#: of off-by-one that turns a cap of six into a cap of seven.
ROOT_DEPTH: Final = 1


class PortfolioGraphError(ValueError):
    """Base for every refusal in this module, so one ``except`` clause covers the layer."""


class CycleError(PortfolioGraphError):
    """The parent chain closes into a ring — including the one-node ring, A's parent being A."""

    def __init__(self, cycle: tuple[int, ...]) -> None:
        #: The ring, read child-to-parent, with the entry point repeated at the end:
        #: ``(A, B, C, A)`` means A's parent is B, B's parent is C, and C's parent is A.
        #: ``(A, A)`` is a self-parent.
        self.cycle = cycle
        super().__init__(
            "portfolio parentage forms a cycle: " + " -> ".join(str(node) for node in cycle)
        )


class DepthExceededError(PortfolioGraphError):
    """A portfolio would sit deeper than :data:`MAX_DEPTH` allows."""

    def __init__(self, portfolio_id: int, depth: int, max_depth: int) -> None:
        self.portfolio_id = portfolio_id
        self.depth = depth
        self.max_depth = max_depth
        super().__init__(
            f"portfolio {portfolio_id} would sit at depth {depth}, "
            f"deeper than the limit of {max_depth}"
        )


class DuplicatePortfolioError(PortfolioGraphError):
    """The same portfolio id appears twice in one sequence.

    Indexing by id would silently keep one row and discard the other, and which one survived
    would depend on iteration order. Refused instead of picked.
    """

    def __init__(self, portfolio_id: int) -> None:
        self.portfolio_id = portfolio_id
        super().__init__(f"portfolio {portfolio_id} appears more than once")


class UnknownPortfolioError(PortfolioGraphError):
    """The portfolio asked about is not in the sequence supplied."""

    def __init__(self, portfolio_id: int) -> None:
        self.portfolio_id = portfolio_id
        super().__init__(f"portfolio {portfolio_id} is not in the given set")


class OrphanError(PortfolioGraphError):
    """A portfolio's parent is not in the sequence, so the question asked has no answer here.

    Distinct from :class:`UnknownPortfolioError`: the portfolio itself is present, its *parent*
    is not. Depth cannot be computed for it, because the chain above it is invisible. This is
    the same condition :class:`Orphan` reports non-fatally from :func:`assemble_tree`.
    """

    def __init__(self, portfolio_id: int, missing_parent_id: int) -> None:
        self.portfolio_id = portfolio_id
        self.missing_parent_id = missing_parent_id
        super().__init__(
            f"portfolio {portfolio_id} names parent {missing_parent_id}, "
            "which is not in the given set"
        )


class AmountTypeError(PortfolioGraphError):
    """A money or quantity field was not something that can be exact (house rule 9)."""

    def __init__(self, field: str, value: object) -> None:
        self.field = field
        self.value = value
        super().__init__(
            f"{field} must be an exact Decimal (int is accepted and converted); "
            f"got {type(value).__name__} {value!r}"
        )


def _exact(field: str, value: object) -> Decimal:
    """Coerce to ``Decimal`` or refuse. See the module docstring for why ``float`` is refused."""
    # bool is a subclass of int, and a quantity of ``True`` is a bug wearing a number's clothes.
    if isinstance(value, bool):
        raise AmountTypeError(field, value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise AmountTypeError(field, value)
        return value
    if isinstance(value, int):
        return Decimal(value)
    raise AmountTypeError(field, value)


@dataclass(frozen=True, slots=True)
class PortfolioNode:
    """One ``portfolio`` row, reduced to the columns the graph needs.

    Deliberately permissive at construction: a node whose ``parent_id`` is its own ``id`` can be
    built, because refusing it here would make the illegal shape unrepresentable and therefore
    untestable. Parentage is judged over a *set* of nodes, by :func:`check_no_cycles` and
    :func:`assemble_tree`, which is the only place it can be judged — a ring is a property of
    several rows, never of one.
    """

    id: int
    name: str
    #: ``None`` means this portfolio is a root.
    parent_id: int | None = None
    #: ``None`` means the portfolio spans brokers — see the module docstring. It is a label on
    #: the container, and never an attribution of the money underneath it.
    broker_account_id: int | None = None


@dataclass(frozen=True, slots=True)
class Holding:
    """One ``portfolio_holding`` row.

    ``quantity`` is expected to be the *whole* position already — non-negotiable #2's
    ``quantity + t1_quantity + collateral_quantity``. This module cannot verify that and does
    not try; it says so here so nobody assembles the total twice or not at all.
    """

    portfolio_id: int
    instrument_id: int
    #: ``None`` means the broker is **unknown**, which is not the same as a portfolio spanning
    #: brokers. It is reported separately and never merged into a broker's line.
    broker_account_id: int | None
    quantity: Decimal
    avg_price: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", _exact("quantity", self.quantity))
        object.__setattr__(self, "avg_price", _exact("avg_price", self.avg_price))


@dataclass(frozen=True, slots=True)
class Totals:
    """Summed holdings. Every number here is exact; ``cost`` is ``quantity * avg_price`` summed.

    ``cost`` is the money actually put in, not a valuation: this module receives no quote and
    is structurally incapable of marking anything to market.
    """

    quantity: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)
    #: How many holding rows went into this line. A zero total with a non-zero count is a real
    #: state (a closed position that was never deleted) and the count is what tells them apart.
    holdings: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", _exact("quantity", self.quantity))
        object.__setattr__(self, "cost", _exact("cost", self.cost))

    def __add__(self, other: Totals) -> Totals:
        return Totals(
            quantity=self.quantity + other.quantity,
            cost=self.cost + other.cost,
            holdings=self.holdings + other.holdings,
        )


@dataclass(frozen=True, slots=True)
class BrokerLine:
    """One broker account's share of a subtree. ``broker_account_id`` is never ``None`` here."""

    broker_account_id: int
    totals: Totals


@dataclass(frozen=True, slots=True)
class BrokerRollup:
    """A subtree's holdings, split by broker account, with the total alongside.

    ``total == sum(line.totals for line in by_broker) + unattributed``, asserted by test. The
    unattributed part is *outside* ``by_broker`` on purpose: money whose broker is unknown must
    not be able to hide inside a broker's line.
    """

    #: The subtree's root — the portfolio the roll-up was asked about.
    portfolio_id: int
    #: What the root portfolio *claims*: ``None`` means it declares itself as spanning brokers.
    declared_broker_account_id: int | None
    #: One line per real broker account, ordered by ``broker_account_id`` so two runs over the
    #: same data produce byte-identical output.
    by_broker: tuple[BrokerLine, ...]
    #: Holdings in this subtree whose ``broker_account_id`` is ``None``.
    unattributed: Totals
    #: Everything, ``unattributed`` included.
    total: Totals

    @property
    def broker_account_ids(self) -> tuple[int, ...]:
        return tuple(line.broker_account_id for line in self.by_broker)

    @property
    def spans_brokers(self) -> bool:
        """True when the money in this subtree sits in more than one nameable place.

        Unattributed money counts as one of those places: a subtree with one broker plus a
        holding nobody could attribute is not a single-broker subtree, it is a subtree with a
        hole in it, and reporting it as single-broker would be the more comfortable lie.
        """
        places = len(self.by_broker) + (1 if self.unattributed.holdings else 0)
        return places > 1

    @property
    def declaration_conflicts(self) -> bool:
        """The root claims one broker, but its subtree's money is not all at that broker.

        The reverse is *not* a conflict: a portfolio declared as spanning brokers is allowed to
        hold only one today, because the declaration is about what the container is for, not
        about what happens to be inside it this morning.
        """
        declared = self.declared_broker_account_id
        if declared is None:
            return False
        return self.broker_account_ids != (declared,) or self.unattributed.holdings > 0

    def line_for(self, broker_account_id: int) -> BrokerLine | None:
        for line in self.by_broker:
            if line.broker_account_id == broker_account_id:
                return line
        return None


@dataclass(frozen=True, slots=True)
class TreeNode:
    """A portfolio with its children nested underneath it."""

    portfolio: PortfolioNode
    children: tuple[TreeNode, ...]
    #: 1 for a root. Inside an :class:`Orphan` fragment this is depth *within the fragment*,
    #: because the rows above it were not supplied and its true depth is therefore unknown.
    depth: int

    @property
    def id(self) -> int:
        return self.portfolio.id


@dataclass(frozen=True, slots=True)
class Orphan:
    """A fragment whose top names a parent that was not in the sequence.

    It is neither dropped nor promoted to a root, because those are different situations and a
    caller must be able to tell them apart: a root is a portfolio someone deliberately put at
    the top, while an orphan is a dangling reference — a parent belonging to another user, a row
    the query's filter excluded, or referential damage. Promoting it would render another user's
    portfolio as if it were yours; dropping it would make holdings disappear from a total.
    """

    #: The parent id that could not be resolved.
    missing_parent_id: int
    #: The fragment itself, with its own descendants nested — they are not orphans, they are
    #: reachable; only the top of the fragment is dangling.
    subtree: TreeNode

    @property
    def portfolio(self) -> PortfolioNode:
        return self.subtree.portfolio


@dataclass(frozen=True, slots=True)
class Forest:
    """The result of :func:`assemble_tree`: real roots, and everything that is not one."""

    roots: tuple[TreeNode, ...]
    orphans: tuple[Orphan, ...]

    @property
    def size(self) -> int:
        """How many portfolios the forest contains — roots, orphans and every descendant.

        Equal to the number of rows supplied, always. :func:`assemble_tree` asserts it.
        """
        return sum(_count(node) for node in self.roots) + sum(
            _count(orphan.subtree) for orphan in self.orphans
        )


def _count(node: TreeNode) -> int:
    return 1 + sum(_count(child) for child in node.children)


def _index(rows: Iterable[PortfolioNode]) -> dict[int, PortfolioNode]:
    """Index by id, preserving input order and refusing duplicates."""
    index: dict[int, PortfolioNode] = {}
    for row in rows:
        if row.id in index:
            raise DuplicatePortfolioError(row.id)
        index[row.id] = row
    return index


def find_cycle(rows: Iterable[PortfolioNode]) -> tuple[int, ...] | None:
    """Return the first ring found, child-to-parent with the entry point repeated, or ``None``.

    Every node has at most one parent, so the parent pointers form a functional graph and the
    search is a walk up the chain rather than a general depth-first search. A node already on
    the current walk closes a ring — which catches ``A -> A`` and ``A -> B -> C -> A`` by the
    same rule, with no special case for either. Chains proven acyclic are remembered, so the
    whole scan is linear however deeply the rows share ancestry.
    """
    index = _index(rows)
    acyclic: set[int] = set()
    for start in index:
        seen: dict[int, int] = {}
        path: list[int] = []
        current: int | None = start
        while current is not None and current in index and current not in acyclic:
            if current in seen:
                return (*path[seen[current] :], current)
            seen[current] = len(path)
            path.append(current)
            current = index[current].parent_id
        acyclic.update(path)
    return None


def check_no_cycles(rows: Iterable[PortfolioNode]) -> None:
    """Raise :class:`CycleError` if any parent chain closes into a ring."""
    cycle = find_cycle(rows)
    if cycle is not None:
        raise CycleError(cycle)


def _chain(index: dict[int, PortfolioNode], portfolio_id: int) -> tuple[list[int], int | None]:
    """Walk from a portfolio up to the top of its chain.

    Returns the ids walked (the portfolio first, its topmost reachable ancestor last) and the
    unresolved parent id if the chain dangles — ``None`` when it ends at a real root. Assumes
    the caller has already ruled out cycles; otherwise this would not terminate.
    """
    if portfolio_id not in index:
        raise UnknownPortfolioError(portfolio_id)
    walked: list[int] = []
    current = portfolio_id
    while True:
        walked.append(current)
        parent_id = index[current].parent_id
        if parent_id is None:
            return walked, None
        if parent_id not in index:
            return walked, parent_id
        current = parent_id


def depth_of(rows: Iterable[PortfolioNode], portfolio_id: int) -> int:
    """Depth counted from the root, where a root is :data:`ROOT_DEPTH`.

    Raises :class:`OrphanError` rather than returning a number when the chain above the
    portfolio dangles. A relative depth returned as if absolute is exactly how an orphan gets
    quietly promoted to a root, and callers must not be handed a number they cannot trust.
    """
    index = _index(rows)
    check_no_cycles(index.values())
    walked, dangling_parent = _chain(index, portfolio_id)
    if dangling_parent is not None:
        raise OrphanError(walked[-1], dangling_parent)
    return len(walked)


def check_depths(rows: Iterable[PortfolioNode], max_depth: int = MAX_DEPTH) -> None:
    """Raise :class:`DepthExceededError` if any portfolio sits deeper than the cap.

    Orphan fragments are checked too, against their *relative* depth. That is a lower bound on
    the true depth — the rows above the fragment can only add to it — so a fragment that already
    breaches the cap is a certain breach, while one that does not is merely unproven. Checking
    the lower bound catches what can be caught and never raises falsely.
    """
    index = _index(rows)
    check_no_cycles(index.values())
    for portfolio_id in index:
        depth = len(_chain(index, portfolio_id)[0])
        if depth > max_depth:
            raise DepthExceededError(portfolio_id, depth, max_depth)


def descendant_ids(rows: Iterable[PortfolioNode], root_id: int) -> tuple[int, ...]:
    """Every portfolio in the subtree rooted at ``root_id``, the root itself included.

    Breadth-first, so the order is stable and shallow nodes come first.
    """
    index = _index(rows)
    check_no_cycles(index.values())
    if root_id not in index:
        raise UnknownPortfolioError(root_id)
    children = _children_by_parent(index)
    found: list[int] = [root_id]
    cursor = 0
    while cursor < len(found):
        for child in children.get(found[cursor], ()):
            found.append(child.id)
        cursor += 1
    return tuple(found)


def subtree_height(rows: Iterable[PortfolioNode], root_id: int) -> int:
    """How many levels the subtree occupies, counting the root as 1."""
    index = _index(rows)
    check_no_cycles(index.values())
    if root_id not in index:
        raise UnknownPortfolioError(root_id)
    children = _children_by_parent(index)
    return _height(children, root_id)


def _height(children: dict[int, list[PortfolioNode]], node_id: int) -> int:
    below = children.get(node_id, ())
    if not below:
        return 1
    return 1 + max(_height(children, child.id) for child in below)


def _children_by_parent(index: dict[int, PortfolioNode]) -> dict[int, list[PortfolioNode]]:
    """Children grouped under their parent's id, in input order.

    Rows whose parent is absent from the index are not grouped here — they are the orphan tops,
    and :func:`assemble_tree` handles them by name rather than losing them in a bucket.
    """
    grouped: dict[int, list[PortfolioNode]] = {}
    for node in index.values():
        parent_id = node.parent_id
        if parent_id is not None and parent_id in index:
            grouped.setdefault(parent_id, []).append(node)
    return grouped


def assemble_tree(rows: Iterable[PortfolioNode], max_depth: int | None = MAX_DEPTH) -> Forest:
    """Nest a flat sequence of portfolios into trees, and report what could not be nested.

    Roots and children keep the order they arrived in — this module makes no display decisions,
    so a caller that wants alphabetical order sorts its query and gets it.

    ``max_depth`` defaults to the cap: a stored tree that breaches it is damage, and reading it
    silently would spread the damage into whatever renders it. Pass ``None`` to assemble
    whatever is there — a repair tool needs to see the broken shape in order to fix it.

    Raises :class:`CycleError`, :class:`DuplicatePortfolioError` and, unless ``max_depth`` is
    ``None``, :class:`DepthExceededError`.
    """
    index = _index(rows)
    check_no_cycles(index.values())
    children = _children_by_parent(index)

    roots: list[TreeNode] = []
    orphans: list[Orphan] = []
    for node in index.values():
        parent_id = node.parent_id
        if parent_id is None:
            roots.append(_build(children, node, ROOT_DEPTH, max_depth))
        elif parent_id not in index:
            orphans.append(
                Orphan(
                    missing_parent_id=parent_id,
                    subtree=_build(children, node, ROOT_DEPTH, max_depth),
                )
            )

    forest = Forest(roots=tuple(roots), orphans=tuple(orphans))
    if forest.size != len(index):
        # Unreachable while cycles are refused above, since every non-root, non-orphan node is
        # then reachable from exactly one top. Checked rather than assumed: the whole promise of
        # this function is that no portfolio vanishes, and a promise nothing verifies is a hope.
        raise PortfolioGraphError(f"assembly lost portfolios: {len(index)} in, {forest.size} out")
    return forest


def _build(
    children: dict[int, list[PortfolioNode]],
    node: PortfolioNode,
    depth: int,
    max_depth: int | None,
) -> TreeNode:
    if max_depth is not None and depth > max_depth:
        raise DepthExceededError(node.id, depth, max_depth)
    return TreeNode(
        portfolio=node,
        children=tuple(
            _build(children, child, depth + 1, max_depth) for child in children.get(node.id, ())
        ),
        depth=depth,
    )


def check_move(
    rows: Iterable[PortfolioNode],
    portfolio_id: int,
    new_parent_id: int | None,
    max_depth: int = MAX_DEPTH,
) -> None:
    """Refuse a re-parenting that would create a ring or breach the depth cap.

    A move is where both failures actually happen. Two of them are easy to miss:

    * making a portfolio a child of **its own descendant** severs that whole branch into a ring
      floating free of any root — the naive check, "is the new parent this portfolio?", catches
      only the one-node case;
    * a legal parent and a legal subtree can still be illegal **together**. Moving a subtree
      three levels tall under a parent at depth 4 puts its leaves at depth 6; under a parent at
      depth 5 it puts them at 7. The parent alone is fine and the subtree alone is fine, so the
      arithmetic has to be done on the pair.

    Passing ``None`` promotes the portfolio to a root, which can never deepen anything.
    """
    index = _index(rows)
    check_no_cycles(index.values())
    if portfolio_id not in index:
        raise UnknownPortfolioError(portfolio_id)

    height = subtree_height(index.values(), portfolio_id)
    if new_parent_id is None:
        if height > max_depth:
            raise DepthExceededError(portfolio_id, height, max_depth)
        return

    if new_parent_id == portfolio_id:
        raise CycleError((portfolio_id, portfolio_id))
    if new_parent_id not in index:
        raise OrphanError(portfolio_id, new_parent_id)

    inside = descendant_ids(index.values(), portfolio_id)
    if new_parent_id in inside:
        # Report the ring the move would make, read child-to-parent from the portfolio being
        # moved: it would name the new parent, and the new parent's existing chain leads back
        # to it. The chain is walked from the new parent, so the moved portfolio goes in front.
        upward, _ = _chain(index, new_parent_id)
        raise CycleError((portfolio_id, *upward[: upward.index(portfolio_id) + 1]))

    parent_walk, dangling = _chain(index, new_parent_id)
    if dangling is not None:
        raise OrphanError(parent_walk[-1], dangling)
    deepest = len(parent_walk) + height
    if deepest > max_depth:
        raise DepthExceededError(portfolio_id, deepest, max_depth)


def rollup_by_broker(
    rows: Iterable[PortfolioNode],
    holdings: Iterable[Holding],
    root_id: int,
) -> BrokerRollup:
    """Sum the subtree rooted at ``root_id``, one line per broker account plus a total.

    Holdings pointing outside the subtree are ignored rather than summed — that is how a
    sibling's money, or another user's, fails to leak into this number. Holdings whose
    ``broker_account_id`` is ``None`` land in :attr:`BrokerRollup.unattributed`, never in a
    broker's line: see the module docstring on why unknown attribution is not the same thing as
    a portfolio that spans brokers.

    All arithmetic is ``Decimal``; a ``float`` cannot enter, because :class:`Holding` refuses
    one at construction.
    """
    index = _index(rows)
    inside = frozenset(descendant_ids(index.values(), root_id))

    per_broker: dict[int, Totals] = {}
    unattributed = Totals()
    for holding in holdings:
        if holding.portfolio_id not in inside:
            continue
        line = Totals(
            quantity=holding.quantity,
            cost=holding.quantity * holding.avg_price,
            holdings=1,
        )
        broker = holding.broker_account_id
        if broker is None:
            unattributed = unattributed + line
        else:
            per_broker[broker] = per_broker.get(broker, Totals()) + line

    by_broker = tuple(
        BrokerLine(broker_account_id=broker, totals=per_broker[broker])
        for broker in sorted(per_broker)
    )
    total = unattributed
    for entry in by_broker:
        total = total + entry.totals

    return BrokerRollup(
        portfolio_id=root_id,
        declared_broker_account_id=index[root_id].broker_account_id,
        by_broker=by_broker,
        unattributed=unattributed,
        total=total,
    )
