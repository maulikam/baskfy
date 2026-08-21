"""Blended (average) factors — docs/05 §4 (Prompt 5 deliverable 3).

    "A blend is the plain arithmetic mean of its component single-window factors:
     avg_sharpe_12_6_3_1 = (sharpe_12m + sharpe_6m + sharpe_3m + sharpe_1m) / 4"

    "If **any** component is NULL the blend is NULL (do not silently average over fewer terms —
     that would rank young listings above seasoned ones)."

That NULL rule is the whole point. A stock listed four months ago has no 12-month Sharpe; averaged
over the terms it *does* have, its blend would be computed from its strongest recent window and it
would sort straight to the top of a momentum screen. The rule is not defensive coding, it is what
stops the ranking being wrong.

docs/04 is equally clear that these are never stored: "Blend factors are NOT stored.
`avg_sharpe_12_6_3_1` etc. are computed in SQL as the mean of the stored component columns. 44
blend columns would be dead weight and a migration liability; the arithmetic is free." So this
module exposes the *shapes*, and the registry turns them into SQL.
"""

from __future__ import annotations

from typing import Final

import polars as pl

#: docs/05 §4: "The eleven blend shapes used by all three families (absolute / sharpe / rsi)".
BLEND_SHAPES: Final[tuple[tuple[int, ...], ...]] = (
    (12, 9, 6, 3, 1),
    (12, 9, 6, 3),
    (12, 9, 6),
    (12, 9),
    (12, 6, 3, 1),
    (12, 6, 3),
    (12, 6),
    (12, 3, 1),
    (12, 3),
    (12, 9, 3, 1),
    (12, 9, 3),
)

#: docs/05 §4: "plus `6·3` for the sharpe family only."
SHARPE_ONLY_SHAPES: Final[tuple[tuple[int, ...], ...]] = ((6, 3),)

#: docs/01 §3 "Risk-adjusted-by-beta (5)": the blends applied to sharpe-divided-by-beta.
BETA_BLEND_SHAPES: Final[tuple[tuple[int, ...], ...]] = (
    (12, 9, 6, 3),
    (12, 6, 3),
    (12, 6),
)


def shape_suffix(shape: tuple[int, ...]) -> str:
    """``(12, 6, 3, 1)`` -> ``12_6_3_1`` — the suffix used in every factor key."""
    return "_".join(str(months) for months in shape)


def shape_label(shape: tuple[int, ...]) -> str:
    """``(12, 6, 3, 1)`` -> ``12 6 3 1`` — the reference product's own wording (docs/07)."""
    return " ".join(str(months) for months in shape)


def blend_expr(components: list[str]) -> pl.Expr:
    """Arithmetic mean of ``components``, NULL if any one of them is NULL."""
    if not components:
        raise ValueError("a blend needs at least one component")
    any_null = pl.any_horizontal([pl.col(name).is_null() for name in components])
    total = pl.sum_horizontal([pl.col(name) for name in components])
    return pl.when(any_null).then(None).otherwise(total / len(components))


def blend_sql(components: list[str]) -> str:
    """The SQL docs/04 asks for: the mean of the stored component columns.

    Written so that PostgreSQL's own NULL propagation enforces docs/05 §4's rule — any NULL
    addend makes the sum NULL, and therefore the blend NULL. No COALESCE anywhere, deliberately:
    a COALESCE here would be the silent partial average the document forbids.
    """
    if not components:
        raise ValueError("a blend needs at least one component")
    added = " + ".join(components)
    return f"(({added}) / {len(components)}.0)"
