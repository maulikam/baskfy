"""What a plan lets an account do — docs/07 §Entitlements (Prompt 13 deliverable 3).

docs/07 fixes the wire shape exactly:

```json
{ "entitlements": { "screener": true, "export_csv": true, "custom_columns": true,
                    "historical_ranks": true, "backtests": true, "api_access": false,
                    "max_screens": 50 } }
```

and the rule: "Enforcement is server-side on every gated endpoint; the UI only *reflects*
entitlements."

This module is the **single definition** of that shape. It lives in `decile_core` — which is
I/O-free — because three places need it and a second copy would be a second thing to keep in
step:

* ``decile_core.seed_data`` writes it into ``plan.features`` (docs/04's ``features jsonb``),
* ``decile_api.entitlements`` resolves it for a caller and renders it into ``GET /me``,
* ``GET /plans`` publishes it, which is what stops the web app hard-coding any of it
  (PROMPTS.md Prompt 13 acceptance criterion 4).

Why ``require`` raises instead of returning a bool
--------------------------------------------------
A gate that returns a bool is a gate every call site can forget to check. ``require`` raises
:class:`FeatureNotEntitled`, which ``decile_api.app`` renders as docs/07's ``402
payment-required`` with its ``upgrade_url``. The exception is defined here rather than in the API
so this module keeps no HTTP dependency.

The two entitlements that are *not* here
----------------------------------------
docs/01 §1 lists five paid features: "export, custom columns, historical ranks, community Slack,
AMAs". The last two are not API surfaces — nothing on this service can grant or refuse a Slack
invitation — and docs/07's payload does not name them. They are plan *features* (advertised by
``GET /plans``, see :data:`INCLUDED_FEATURE_LABELS`) rather than entitlements, so the ``/me``
payload stays exactly the seven keys docs/07 lists.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "ANONYMOUS",
    "ENTITLEMENTS_KEY",
    "FREE_TIER",
    "INCLUDED_FEATURE_LABELS",
    "MAX_SCREENS_KEY",
    "PAID",
    "REGISTERED",
    "UNIVERSES_KEY",
    "Entitlements",
    "Feature",
    "FeatureNotEntitled",
]


class Feature(StrEnum):
    """The boolean keys docs/07 §Entitlements returns, in the document's order."""

    SCREENER = "screener"
    EXPORT_CSV = "export_csv"
    CUSTOM_COLUMNS = "custom_columns"
    HISTORICAL_RANKS = "historical_ranks"
    BACKTESTS = "backtests"
    API_ACCESS = "api_access"


#: The non-boolean member of the same payload.
MAX_SCREENS_KEY: Final = "max_screens"

#: Where the entitlement block lives inside docs/04's ``plan.features jsonb``.
ENTITLEMENTS_KEY: Final = "entitlements"

#: An optional restriction *inside* the plan's entitlement block: the universe codes a plan may
#: screen. ``None`` (or absent) means every universe. Only the optional ₹0 tier sets it
#: (PROMPTS.md Prompt 13 §5: "A ₹0 free tier with a limited universe"). Deliberately **not** a
#: member of the ``/me`` payload, which docs/07 fixes at seven keys.
UNIVERSES_KEY: Final = "universes"

#: docs/07's own example payload shows ``"api_access": false``, which is also the only honest
#: answer while the public read API does not exist — Prompt 20 builds it, and docs/11 §Compliance
#: requires "a written data-redistribution opinion before enabling the public API tier". So no
#: plan grants it, including the paid ones. Recorded in docs/DECISIONS.md.
API_ACCESS_AVAILABLE: Final = False

#: docs/07 §Entitlements' own example number, and therefore the paid one.
PAID_MAX_SCREENS: Final = 50

#: NOT IN THE BUNDLE. docs/04 §"Retention & size estimates" says "prune runs older than 400 days
#: for free users", so an unpaid account is expected to exist and to be able to save screens;
#: nothing says how many. Five is chosen to be usable and clearly limited. docs/DECISIONS.md.
FREE_MAX_SCREENS: Final = 5


class FeatureNotEntitled(Exception):
    """Raised by :meth:`Entitlements.require`. Rendered as docs/07's ``402 payment-required``."""

    def __init__(self, feature: str, detail: str | None = None) -> None:
        self.feature = feature
        self.detail = detail or f"{feature!r} is not included in your plan."
        super().__init__(self.detail)


@dataclass(frozen=True, slots=True)
class Entitlements:
    """What one caller may do.

    ``universes`` is ``None`` for "every universe" and a set of ``index_def.code`` values for a
    plan that is restricted to some of them.
    """

    granted: frozenset[Feature]
    max_screens: int = PAID_MAX_SCREENS
    universes: frozenset[str] | None = None

    def allows(self, feature: Feature) -> bool:
        return feature in self.granted

    def require(self, feature: Feature) -> None:
        """docs/07: a missing entitlement is a 402 carrying ``upgrade_url``."""
        if not self.allows(feature):
            raise FeatureNotEntitled(feature.value)

    def allows_universe(self, code: str) -> bool:
        return self.universes is None or code in self.universes

    def require_universe(self, code: str) -> None:
        """The ₹0 tier's restriction (Prompt 13 §5), enforced where the screen is run."""
        if not self.allows_universe(code):
            raise FeatureNotEntitled(
                "universes",
                f"Your plan can screen {_english_list(sorted(self.universes or ()))} only, "
                f"not {code!r}.",
            )

    def as_dict(self) -> dict[str, bool | int]:
        """docs/07 §Entitlements, exactly — seven keys, no more.

        ``universes`` is not included: docs/07 fixes this payload's members, and adding one would
        make the generated TypeScript client disagree with the document.
        """
        payload: dict[str, bool | int] = {
            feature.value: self.allows(feature) for feature in Feature
        }
        payload[MAX_SCREENS_KEY] = self.max_screens
        return payload

    def to_plan_features(self) -> dict[str, bool | int | list[str] | None]:
        """The block stored in docs/04's ``plan.features jsonb``.

        Same seven keys plus ``universes``, so a plan row carries everything needed to rebuild an
        :class:`Entitlements` and nothing else has to know the defaults.
        """
        stored: dict[str, bool | int | list[str] | None] = dict(self.as_dict())
        stored[UNIVERSES_KEY] = None if self.universes is None else sorted(self.universes)
        return stored

    @classmethod
    def from_plan_features(cls, features: Mapping[str, object]) -> Entitlements:
        """Rebuild from a ``plan.features`` row, tolerating a row written before a key existed.

        A plan row is data an operator can edit. An unknown key is ignored and a missing key
        falls back to *refused* rather than granted — the failure mode of a typo should be a
        support ticket, not a free subscription.
        """
        block = features.get(ENTITLEMENTS_KEY)
        source: Mapping[str, object] = block if isinstance(block, Mapping) else {}
        granted = {feature for feature in Feature if source.get(feature.value) is True}
        raw_max = source.get(MAX_SCREENS_KEY)
        max_screens = raw_max if isinstance(raw_max, int) and not isinstance(raw_max, bool) else 0
        raw_universes = source.get(UNIVERSES_KEY)
        universes = (
            frozenset(str(code) for code in raw_universes)
            if isinstance(raw_universes, list) and raw_universes
            else None
        )
        return cls(granted=frozenset(granted), max_screens=max_screens, universes=universes)


def _english_list(items: Iterable[str]) -> str:
    values = list(items)
    if not values:
        return "no universe"
    if len(values) == 1:
        return values[0]
    return f"{', '.join(values[:-1])} and {values[-1]}"


def _paid_features() -> frozenset[Feature]:
    granted = {
        Feature.SCREENER,
        Feature.EXPORT_CSV,
        Feature.CUSTOM_COLUMNS,
        Feature.HISTORICAL_RANKS,
        Feature.BACKTESTS,
    }
    if API_ACCESS_AVAILABLE:  # pragma: no cover - Prompt 20 flips this
        granted.add(Feature.API_ACCESS)
    return frozenset(granted)


#: Every paid plan grants the same thing. docs/01 §1 prices three plans differently and gates the
#: same five features behind all of them; nothing in the bundle distinguishes what Monthly buys
#: from what Forever buys except how long it lasts.
PAID: Final = Entitlements(granted=_paid_features(), max_screens=PAID_MAX_SCREENS)

#: A signed-in account with no paid plan. It can still run a screen — docs/01 §1 gates "export,
#: custom columns, historical ranks, community Slack, AMAs", not the screener itself.
REGISTERED: Final = Entitlements(
    granted=frozenset({Feature.SCREENER}), max_screens=FREE_MAX_SCREENS
)

#: The optional ₹0 tier (Prompt 13 §5), which is ``REGISTERED`` plus docs' "limited universe".
FREE_TIER_UNIVERSES: Final[frozenset[str]] = frozenset({"nifty-50"})
FREE_TIER: Final = Entitlements(
    granted=frozenset({Feature.SCREENER}),
    max_screens=FREE_MAX_SCREENS,
    universes=FREE_TIER_UNIVERSES,
)

#: No account at all. It may read and run the example screens, and save nothing.
ANONYMOUS: Final = Entitlements(granted=frozenset({Feature.SCREENER}), max_screens=0)


#: What ``GET /plans`` advertises, in the reference product's own terms (PROMPTS.md Prompt 13 §1:
#: "screener, export, custom columns, historical ranks, backtests, community access, AMAs").
#: ``None`` as the entitlement key marks the two that this service cannot enforce.
INCLUDED_FEATURE_LABELS: Final[tuple[tuple[str, str | None], ...]] = (
    ("Full screener across all 14 universes", Feature.SCREENER.value),
    ("CSV export of any screen", Feature.EXPORT_CSV.value),
    ("Custom result columns", Feature.CUSTOM_COLUMNS.value),
    ("Historical ranks — run any screen as of a past date", Feature.HISTORICAL_RANKS.value),
    ("Backtests", Feature.BACKTESTS.value),
    ("Community Slack access", None),
    ("Live AMAs and recordings", None),
)
