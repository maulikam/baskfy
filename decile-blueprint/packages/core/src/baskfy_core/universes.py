"""The 14 selectable universes and their ``factor_daily`` mask bits.

Sources
-------
* docs/01-product-teardown.md §2.1 — the 14 values of the ``index`` select, and their UI order.
* docs/01-product-teardown.md §6   — the 12 universes offered on /market-health.
* docs/13-csv-export-schema.md §1  — the 14 ``is_*`` flag columns and their export order.
* docs/06-screener-semantics.md    — the index construction identities.

A note on two conflicting orderings: the UI select (§2.1) lists FNO before "All NSE Listed
Stocks", while the CSV export (docs/13 §1) lists ``is_nifty_allcap`` before ``is_nifty_fno``.
``index_def.id`` — and therefore the ``universe_mask`` bit — follows the **export** order, so the
CSV writer can emit flags by simply walking the bits. ``index_def.sort_order`` follows the **UI**
order, so the dropdown matches the reference product.

A note on PROMPTS.md Prompt 1 deliverable 4: it asks for "the 14 selectable universes from
docs/01 §2.1 plus the 12 market-health universes". The 12 are a strict *subset* of the 14 (they
are the 14 minus ``nifty-fno`` and ``etf``), not 12 additional rows, so this module defines 14
``index_def`` rows and marks which of them market health covers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: docs/06 §"apply_filters_on" — INFERRED ranking key for decile bucketing.
DECILE_RANK_KEY: Final = "marketcap_cr"

#: docs/06 §"Step 4" — the nightly top-beta / top-volatility cut, per universe.
TOP_RISK_FLAG_PERCENTILE: Final = 0.10


@dataclass(frozen=True, slots=True)
class Universe:
    """One selectable universe.

    ``index_id`` doubles as the ``universe_mask`` bit position (``bit = index_id - 1``), which is
    why ids are allocated densely from 1 and dashboard-only indices start at
    ``FIRST_NON_UNIVERSE_INDEX_ID``.
    """

    index_id: int
    slug: str
    name: str
    ui_order: int
    market_health: bool
    #: Whether the reference product's CSV export carries this universe's ``is_*`` flag columns.
    #: False for anything Baskfy defines that momoindiascreener.in never had — the export is a
    #: fixed 14-universe artefact and the regression corpus is read-only, so a universe of ours
    #: must not make :mod:`baskfy_core.reference_export` look for a column that cannot exist.
    in_reference_export: bool = True

    @property
    def mask_bit(self) -> int:
        return self.index_id - 1

    @property
    def mask_value(self) -> int:
        return 1 << self.mask_bit

    @property
    def csv_flag(self) -> str:
        """The export column name for this universe (docs/13 §1)."""
        return "is_etf" if self.slug == "etf" else f"is_{self.slug.replace('-', '_')}"


#: id / mask-bit order follows docs/13 §1; ui_order follows docs/01 §2.1.
UNIVERSES: Final[tuple[Universe, ...]] = (
    Universe(1, "nifty-50", "NIFTY 50", 1, market_health=True),
    Universe(2, "nifty-next-50", "NIFTY NEXT 50", 2, market_health=True),
    Universe(3, "nifty-100", "NIFTY 100", 3, market_health=True),
    Universe(4, "nifty-200", "NIFTY 200", 4, market_health=True),
    Universe(5, "nifty-500", "NIFTY 500", 5, market_health=True),
    Universe(6, "nifty-total-market", "NIFTY TOTAL MARKET", 6, market_health=True),
    Universe(7, "nifty-large-mid-250", "NIFTY LARGE MID 250", 7, market_health=True),
    Universe(8, "nifty-midcap-150", "NIFTY MIDCAP 150", 8, market_health=True),
    Universe(9, "nifty-smallcap-250", "NIFTY SMALLCAP 250", 9, market_health=True),
    Universe(10, "nifty-microcap-250", "NIFTY MICROCAP 250", 10, market_health=True),
    Universe(11, "nifty-mid-small-400", "NIFTY MID SMALL 400", 11, market_health=True),
    Universe(12, "nifty-allcap", "All NSE Listed Stocks", 13, market_health=True),
    Universe(13, "nifty-fno", "NIFTY FNO", 12, market_health=False),
    Universe(14, "etf", "All NSE Listed ETFs", 14, market_health=False),
    Universe(
        15, "nse-sme-emerge", "NSE SME (Emerge)", 15, market_health=False,
        in_reference_export=False,
    ),
)

UNIVERSE_BY_SLUG: Final[dict[str, Universe]] = {u.slug: u for u in UNIVERSES}
UNIVERSE_SLUGS: Final[tuple[str, ...]] = tuple(u.slug for u in UNIVERSES)
UNIVERSE_MASK_BIT: Final[dict[str, int]] = {u.slug: u.mask_bit for u in UNIVERSES}
MARKET_HEALTH_SLUGS: Final[tuple[str, ...]] = tuple(u.slug for u in UNIVERSES if u.market_health)

#: The universes the reference export's ``is_*`` columns cover, in mask-bit order.
REFERENCE_EXPORT_UNIVERSES: Final[tuple[Universe, ...]] = tuple(
    u for u in UNIVERSES if u.in_reference_export
)

#: NSE publishes index names as free text. This is how one becomes a stable slug — shared, because
#: the nightly step registers indices by it, the fixture builder writes it, and the seed CLI reads
#: it back. Three spellings of the same rule would be three ways to register the same index twice.
_INDEX_SLUG_STRIP: Final = re.compile(r"[^a-z0-9]+")


def slugify_index(name: str) -> str:
    """``NIFTY MIDCAP 150`` -> ``nifty-midcap-150``, ``Nifty50 PR 1x Inverse`` ->
    ``nifty50-pr-1x-inverse``."""
    return _INDEX_SLUG_STRIP.sub("-", name.strip().lower()).strip("-")


#: ``factor_daily.universe_mask`` is a PostgreSQL ``integer``: 31 usable bits. Dashboard-only
#: indices (docs/01 §7 lists ~145) are never masked, so their ids start above the mask range.
FIRST_NON_UNIVERSE_INDEX_ID: Final = 100

#: docs/06 §"Universe flags" — verified as exact (0 violations) against the reference export.
UNION_IDENTITIES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("nifty-500", ("nifty-100", "nifty-midcap-150", "nifty-smallcap-250")),
    ("nifty-large-mid-250", ("nifty-100", "nifty-midcap-150")),
    ("nifty-mid-small-400", ("nifty-midcap-150", "nifty-smallcap-250")),
)

#: The NSE Emerge (SME) series. An SME company is not a small main-board company — it listed
#: under a separate NSE platform with its own register, its own lot-size regime and its own
#: circuit bands. No NIFTY index contains one, which is why `nse-sme-emerge` is derived by series
#: rather than fetched as a constituent file, and why it sits in no `UNION_IDENTITIES` row.
SME_SERIES: Final[frozenset[str]] = frozenset({"SM", "ST", "SZ"})

#: Each pair is (subset, superset).
CONTAINMENT_IDENTITIES: Final[tuple[tuple[str, str], ...]] = (
    ("nifty-50", "nifty-100"),
    ("nifty-100", "nifty-200"),
    ("nifty-200", "nifty-500"),
    ("nifty-500", "nifty-total-market"),
    ("nifty-total-market", "nifty-allcap"),
    ("nifty-next-50", "nifty-100"),
    ("nifty-microcap-250", "nifty-total-market"),
    # Emerge names carry `instrument_type = 'EQ'`, so allcap's "every EQ instrument with a bar"
    # rule takes them in as soon as they have bars. That containment is the whole reason a
    # user can screen SME and main board together.
    ("nse-sme-emerge", "nifty-allcap"),
)
