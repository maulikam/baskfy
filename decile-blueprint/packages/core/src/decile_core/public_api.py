"""What the public read API may serve, and the review that gates it — Prompt 20 deliverable 2.

    "A versioned public read API exposing ONLY derived analytics (screen results, factor values,
     breadth), never raw vendor bars, with a machine-readable terms-of-use endpoint. Gate the
     entire feature behind a flag that stays OFF until the data-redistribution review in docs/11
     is signed off; **make that dependency explicit in the code** and the admin UI."

docs/11 §"Compliance & legal (India)" is the reason this module exists, quoted here in full
because it is the requirement, not a summary of one:

    "**Data licensing:** broker-sourced market data is licensed for the licensee's own use. Serve
     derived analytics; do not expose a raw-bar API to third parties without written clearance.
     Get a written data-redistribution opinion before enabling the public API tier."

Everything in this module is pure data: the version, the prefix, the column whitelist, the rule
that produces it, and the terms of use as a serialisable document. The gate itself
(:data:`DATA_REDISTRIBUTION_REVIEW`) is a constant that a human must edit — deliberately not a
setting, not an environment variable and not an admin toggle. An operator can turn the feature
*off*; nobody can turn it *on* without a commit that changes this file, which is the closest thing
a codebase has to a signature.

The line this module draws
--------------------------
"Derived analytics, never raw vendor bars" needs an operational test, because every derived number
is downstream of a bar. The rule here is:

    **no public field is denominated in rupees per share.**

That is :data:`is_public_column`, and it excludes ``close``, ``close_raw``, ``high_1y``,
``high_ath`` and the four moving averages — every column that reproduces, or is one subtraction
away from reproducing, an exchange print for a single instrument on a single day. What survives is
returns, sharpe returns, RSI, volatility, beta, marketcap, P/E, away-from-high percentages,
circuit counts, median traded value and the series code: statistics over a window, which is what
"derived analytics" means.

It is a *conservative* line and it is not airtight — see ``docs/DECISIONS.md`` §20.4 for the
residual reconstruction risk that keeps the flag off regardless.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from decile_core.factor_registry import FACTORS, FactorUnit, columns
from decile_core.seed_data import PRODUCT_NAME

__all__ = [
    "DATA_REDISTRIBUTION_REVIEW",
    "PUBLIC_API_PREFIX",
    "PUBLIC_API_VERSION",
    "PUBLIC_COLUMNS",
    "PUBLIC_SORT_FACTORS",
    "RAW_BAR_FIELDS",
    "TERMS_VERSION",
    "WITHHELD_COLUMNS",
    "DataRedistributionReview",
    "TermsOfUse",
    "is_public_column",
    "is_public_sort",
    "terms_of_use",
]

#: docs/07 versions the product API at ``/api/v1``. The public tier is versioned *separately*
#: because its compatibility promise is different: ``/api/v1`` may change with the web app that
#: consumes it, and a third-party integration cannot be redeployed alongside us.
PUBLIC_API_VERSION: Final = "v1"
PUBLIC_API_PREFIX: Final = f"/api/public/{PUBLIC_API_VERSION}"

#: Bumped whenever the wording of :func:`terms_of_use` changes in a way that changes what a caller
#: is permitted to do. A key issued under one version keeps working; the endpoint reports the
#: current version so an integration can detect the change.
TERMS_VERSION: Final = "2026-08-21"


@dataclass(frozen=True, slots=True)
class DataRedistributionReview:
    """The docs/11 §Compliance precondition, as a value the code can check and the UI can render.

    ``signed_off`` is ``False`` and must stay ``False`` until a lawyer has produced the written
    opinion docs/11 asks for. Flipping it is a source change, reviewable in a diff, attributable
    to a person — which is the property that matters for a compliance gate and that a runtime
    setting does not have.
    """

    requirement: str
    signed_off: bool
    #: Who signed it, when, and where the opinion lives. All empty until one exists.
    opinion_reference: str = ""
    signed_off_on: str = ""
    signed_off_by: str = ""

    @property
    def blocks_public_api(self) -> bool:
        return not self.signed_off

    def as_dict(self) -> dict[str, object]:
        return {
            "requirement": self.requirement,
            "signed_off": self.signed_off,
            "opinion_reference": self.opinion_reference,
            "signed_off_on": self.signed_off_on,
            "signed_off_by": self.signed_off_by,
        }


#: **THE GATE.** docs/11 §"Compliance & legal (India)", verbatim.
DATA_REDISTRIBUTION_REVIEW: Final = DataRedistributionReview(
    requirement=(
        "Broker-sourced market data is licensed for the licensee's own use. Serve derived "
        "analytics; do not expose a raw-bar API to third parties without written clearance. Get "
        "a written data-redistribution opinion before enabling the public API tier. "
        "(docs/11-nonfunctional.md, Compliance & legal (India).)"
    ),
    signed_off=False,
)

#: Field names that are an exchange print or a single day's traded volume, spelled the way the
#: screener, the factsheet and the CSV export spell them. Asserted absent from every public
#: response by ``services/api/tests/test_public_api.py``.
RAW_BAR_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "open",
        "high",
        "low",
        "close",
        "close_raw",
        "volume",
        "turnover",
        "adj_factor",
        "vol_day_val",
        "high_1y",
        "high_ath",
        "ma_20",
        "ma_50",
        "ma_100",
        "ma_200",
    }
)


def is_public_column(key: str, unit: FactorUnit) -> bool:
    """The one rule. See the module docstring for why it is stated in units.

    ``FactorUnit.PRICE`` is "rupees per share": ``close``, ``close_raw``, the two highs and the
    four moving averages. Everything else in docs/01 §4's picker is a ratio, a percentage, a
    count, a crore figure or a text code.
    """
    if key in RAW_BAR_FIELDS:
        return False
    return unit is not FactorUnit.PRICE


#: docs/01 §4's column picker, filtered by :func:`is_public_column`. This is the *only* projection
#: the public screen-results endpoint will build, so a column added to the picker later is
#: public-by-default only if it passes the rule above.
PUBLIC_COLUMNS: Final[tuple[str, ...]] = tuple(
    column.key for column in columns() if is_public_column(column.key, column.unit)
)

#: What the public API refuses to serve at all, as a list a human can read in the terms document.
WITHHELD_COLUMNS: Final[tuple[str, ...]] = tuple(
    column.key for column in columns() if not is_public_column(column.key, column.unit)
)

#: Ranking factors a public screen may sort by. **Not the same set as** :data:`PUBLIC_COLUMNS`.
#:
#: docs/01 §4's column picker and docs/01 §3's ranking factors are different lists: every blend
#: (``avg_sharpe_12_6_3_1``, which is what the flagship example screen sorts by) is a ranking
#: factor and not a picker column. Judging a sort by the *column* whitelist would therefore refuse
#: every blend-sorted screen — including the one docs/13's export was captured from — for no
#: compliance reason at all. The rule applied is the same one; the registry is the input.
PUBLIC_SORT_FACTORS: Final[frozenset[str]] = frozenset(
    key for key, factor in FACTORS.items() if is_public_column(key, factor.unit)
)


def is_public_sort(key: str) -> bool:
    """Whether a screen sorted by ``key`` may be served publicly.

    docs/06 always projects ``sorting_factor`` alongside the requested columns, so a screen sorted
    by ``close_raw`` would put an exchange print into the payload through a door the column
    whitelist does not cover. This is that door.
    """
    return key in PUBLIC_SORT_FACTORS


@dataclass(frozen=True, slots=True)
class TermsOfUse:
    """The machine-readable terms-of-use document Prompt 20 deliverable 2 asks for.

    Machine-readable means a caller's *code* can act on it: ``attribution_required``,
    ``redistribution_permitted`` and ``rate_limit_per_minute`` are fields, not sentences buried in
    prose. The prose is there too, in ``summary`` and ``clauses``, because a human reads it once
    before the code reads it forever.
    """

    version: str
    product: str
    api_version: str
    summary: str
    clauses: tuple[str, ...]
    permitted_use: tuple[str, ...]
    prohibited_use: tuple[str, ...]
    served_fields: tuple[str, ...]
    withheld_fields: tuple[str, ...]
    attribution_required: bool
    attribution_text: str
    redistribution_permitted: bool
    caching_max_age_seconds: int
    disclaimer: str
    contact: str

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "product": self.product,
            "api_version": self.api_version,
            "summary": self.summary,
            "clauses": list(self.clauses),
            "permitted_use": list(self.permitted_use),
            "prohibited_use": list(self.prohibited_use),
            "served_fields": list(self.served_fields),
            "withheld_fields": list(self.withheld_fields),
            "attribution_required": self.attribution_required,
            "attribution_text": self.attribution_text,
            "redistribution_permitted": self.redistribution_permitted,
            "caching_max_age_seconds": self.caching_max_age_seconds,
            "disclaimer": self.disclaimer,
            "contact": self.contact,
        }


#: docs/11 §Compliance: "Not a SEBI-registered investment adviser." The same sentence the UI's
#: `<Disclaimer/>` and every alert email carry — an API response is an analytics surface too.
PUBLIC_DISCLAIMER: Final = (
    f"{PRODUCT_NAME} is not a SEBI-registered investment adviser. Everything this API returns is "
    "factual analysis of published market data, not investment advice, and carries no "
    "recommendation to buy or sell any security."
)


def terms_of_use(*, contact: str) -> TermsOfUse:
    """Build the document. ``contact`` is deployment configuration, everything else is fixed."""
    return TermsOfUse(
        version=TERMS_VERSION,
        product=PRODUCT_NAME,
        api_version=PUBLIC_API_VERSION,
        summary=(
            f"{PRODUCT_NAME} serves derived analytics — factor values, screen results and market "
            "breadth — computed from licensed market data. The underlying price and volume "
            "records are not served and may not be reconstructed."
        ),
        clauses=(
            "The data returned by this API is derived analytics. It is not the underlying "
            "exchange record and is not a substitute for one.",
            "Access is granted per API key, to the account the key belongs to. A key may not be "
            "shared, resold or embedded in a product distributed to third parties.",
            "Systematic reconstruction of a price or volume series from these responses is "
            "prohibited, whether by differencing derived values across dates or otherwise.",
            "Rate limits are published per key and are enforced. Circumventing them by rotating "
            "keys or addresses terminates access.",
            "Access may be suspended without notice if the data licence under which the "
            "underlying market data is held requires it.",
        ),
        permitted_use=(
            "Internal research and analysis within the account holder's own organisation.",
            "Display of individual derived values in the account holder's own interface, with "
            "attribution.",
        ),
        prohibited_use=(
            "Redistribution or resale of the responses, in whole or in part.",
            "Reconstruction of raw open/high/low/close/volume series.",
            "Presenting the values as investment advice or as a recommendation.",
        ),
        served_fields=PUBLIC_COLUMNS,
        withheld_fields=WITHHELD_COLUMNS,
        attribution_required=True,
        attribution_text=f"Analytics by {PRODUCT_NAME}",
        redistribution_permitted=False,
        # One trading day. The data changes once a night (docs/09 §Schedule), so a caller that
        # caches for a day is doing the right thing and one that polls per minute is not.
        caching_max_age_seconds=86_400,
        disclaimer=PUBLIC_DISCLAIMER,
        contact=contact,
    )


def review_status() -> Mapping[str, object]:
    """The gate, as the admin UI renders it (Prompt 20 deliverable 2's "and the admin UI")."""
    return DATA_REDISTRIBUTION_REVIEW.as_dict()
