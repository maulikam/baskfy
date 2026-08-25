"""Broker connection catalog and the D3 gate that unlocks live OAuth (M41 / D3).

    docs/05 P4.2 / P5.8: per-user broker connection, encrypted token, daily re-auth in the UI.
    docs/06 D3: written 23 Aug 2026 as posture B in ``docs/DECISIONS-MERGE.md`` §D3.

Live OAuth and holdings sync for the sole tenant are allowed when
:data:`BROKER_OAUTH_REVIEW`.``signed_off`` is True. The web app still never places orders —
desk non-negotiable #1 / packages/execution only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

__all__ = [
    "BROKERS",
    "BROKER_BY_ID",
    "BROKER_OAUTH_REVIEW",
    "BrokerCapability",
    "BrokerDef",
    "BrokerOauthReview",
    "Capability",
    "broker_catalog",
    "get_broker",
]

Capability = Literal["ready", "planned", "partner"]


@dataclass(frozen=True, slots=True)
class BrokerOauthReview:
    """The docs/06 D3 precondition, as a value the code can check and the UI can render.

    ``signed_off`` is ``False`` and must stay ``False`` until D3 has a written answer in
    ``docs/DECISIONS-MERGE.md``. Flipping it is a source change, reviewable in a diff.
    """

    requirement: str
    signed_off: bool
    decision_reference: str = ""
    signed_off_on: str = ""
    signed_off_by: str = ""

    @property
    def blocks_live_oauth(self) -> bool:
        return not self.signed_off

    def as_dict(self) -> dict[str, object]:
        return {
            "requirement": self.requirement,
            "signed_off": self.signed_off,
            "decision_reference": self.decision_reference,
            "signed_off_on": self.signed_off_on,
            "signed_off_by": self.signed_off_by,
        }


#: **THE GATE.** docs/06 D3 — written 23 Aug 2026 as posture B (DECISIONS-MERGE.md §D3).
#: Web execute remains forbidden (desk non-negotiable #1); this gate only unlocks OAuth +
#: holdings sync. Flip back to False to reverse.
BROKER_OAUTH_REVIEW: Final = BrokerOauthReview(
    requirement=(
        "Posture B (DECISIONS-MERGE.md §D3): publish baskets; user executes in their own "
        "broker account after confirm. OAuth + encrypted holdings sync are allowed. "
        "The web app still never places orders — packages/execution only."
    ),
    signed_off=True,
    decision_reference="DECISIONS-MERGE.md §D3",
    signed_off_on="2026-08-23",
    signed_off_by="autonomy-charter (⚠ UNREVIEWED until Maulik clears)",
)


@dataclass(frozen=True, slots=True)
class BrokerCapability:
    """What an adapter can do once live OAuth is allowed."""

    oauth: Capability
    holdings_sync: Capability
    trading: Capability


@dataclass(frozen=True, slots=True)
class BrokerDef:
    """One broker on the connect grid.

    ``mark`` is a short monogram for the tile — not the broker's trademarked logo. Official marks
    need a licence; initials in the broker's usual colour are enough to recognise the name.
    """

    id: str
    name: str
    short_name: str
    mark: str
    #: CSS hex for the tile face. Approximate house colours, not brand assets.
    color: str
    blurb: str
    api_name: str
    docs_url: str
    capabilities: BrokerCapability
    #: Display order on the grid. Lower first. The four named in the product ask come first.
    sort_order: int


#: The ten retail brokers Baskfy will connect, once D3 clears.
#:
#: Ordered so Zerodha, HDFC, Kotak and ICICI lead — the four the product ask named — then the
#: six most-used India retail APIs that actually publish a connect flow. Groww is listed for
#: recognition; its public trading API is still limited, so capabilities stay ``planned``.
#:
#: **A capability here is a promise the tile makes to an investor, so it may not run ahead of
#: the code.** A ``holdings_sync`` of ``"ready"`` is reserved for brokers in
#: :data:`baskfy_api.broker_holdings._HOLDINGS_WIRED` — today that is Zerodha alone, and every
#: other id returns ``[]`` from the first line of ``holdings_for_broker``. Until Tree-5 leaf C2
#: (25 Aug 2026) eight of these ten rows said ``"ready"``; seven of them had no adapter at all.
#: ``packages/core/tests/test_broker_capability_honesty.py`` now binds label to wiring in both
#: directions, and holds the blurb to the same standard — the prose is rendered one line above
#: the capability list, so "sync holdings" in a blurb undoes a label that says "Planned".
#: Writing an adapter is what earns a ``"ready"``; it needs credentials, which are a
#: ``NEEDS-MAULIK.md`` item, not a source edit.
BROKERS: Final[tuple[BrokerDef, ...]] = (
    BrokerDef(
        id="zerodha",
        name="Zerodha",
        short_name="Zerodha",
        mark="Z",
        color="#387ed1",
        blurb="Kite Connect — holdings, positions and CNC orders in your own account.",
        api_name="Kite Connect",
        docs_url="https://kite.trade/docs/connect/v3/",
        capabilities=BrokerCapability(oauth="ready", holdings_sync="ready", trading="ready"),
        sort_order=1,
    ),
    BrokerDef(
        id="hdfc",
        name="HDFC Securities",
        short_name="HDFC",
        mark="H",
        color="#004c8f",
        blurb="HDFC Securities APIs — partner access; holdings import when empanelled.",
        api_name="HDFC Securities API",
        docs_url="https://www.hdfcsec.com/",
        capabilities=BrokerCapability(oauth="partner", holdings_sync="planned", trading="planned"),
        sort_order=2,
    ),
    BrokerDef(
        id="kotak",
        name="Kotak Securities",
        short_name="Kotak",
        mark="K",
        color="#ed1c24",
        blurb="Kotak Neo — planned: holdings sync once a Neo adapter and partner app key exist.",
        api_name="Kotak Neo",
        docs_url="https://www.kotaksecurities.com/markets/trading-platforms/neo/",
        capabilities=BrokerCapability(oauth="ready", holdings_sync="planned", trading="planned"),
        sort_order=3,
    ),
    BrokerDef(
        id="icici",
        name="ICICI Direct",
        short_name="ICICI",
        mark="I",
        color="#f58220",
        blurb="Breeze API — planned: holdings sync once a Breeze adapter and app key exist.",
        api_name="Breeze API",
        docs_url="https://www.icicidirect.com/idirectcontent/Markets/MarketOverview.aspx",
        capabilities=BrokerCapability(oauth="ready", holdings_sync="planned", trading="planned"),
        sort_order=4,
    ),
    BrokerDef(
        id="upstox",
        name="Upstox",
        short_name="Upstox",
        mark="U",
        color="#5a2d82",
        blurb="Upstox API v2 — planned: holdings sync once an Upstox adapter and app key exist.",
        api_name="Upstox API",
        docs_url="https://upstox.com/developer/api-documentation/",
        capabilities=BrokerCapability(oauth="ready", holdings_sync="planned", trading="planned"),
        sort_order=5,
    ),
    BrokerDef(
        id="angelone",
        name="Angel One",
        short_name="Angel",
        mark="A",
        color="#e85d04",
        blurb="SmartAPI — planned: holdings sync once a SmartAPI adapter and app key exist.",
        api_name="SmartAPI",
        docs_url="https://smartapi.angelbroking.com/",
        capabilities=BrokerCapability(oauth="ready", holdings_sync="planned", trading="planned"),
        sort_order=6,
    ),
    BrokerDef(
        id="groww",
        name="Groww",
        short_name="Groww",
        mark="G",
        color="#00b386",
        blurb="Groww — connection planned once a public trading API is generally available.",
        api_name="Groww API",
        docs_url="https://groww.in/",
        capabilities=BrokerCapability(oauth="planned", holdings_sync="planned", trading="planned"),
        sort_order=7,
    ),
    BrokerDef(
        id="fyers",
        name="Fyers",
        short_name="Fyers",
        mark="F",
        color="#1a1a2e",
        blurb="Fyers API v3 — planned: holdings sync once a Fyers adapter and app key exist.",
        api_name="Fyers API",
        docs_url="https://myapi.fyers.in/docsv3",
        capabilities=BrokerCapability(oauth="ready", holdings_sync="planned", trading="planned"),
        sort_order=8,
    ),
    BrokerDef(
        id="fivepaisa",
        name="5paisa",
        short_name="5paisa",
        mark="5",
        color="#1d4ed8",
        blurb="5paisa OpenAPI — planned: holdings sync once a 5paisa adapter and app key exist.",
        api_name="5paisa OpenAPI",
        docs_url="https://www.5paisa.com/developerapi",
        capabilities=BrokerCapability(oauth="ready", holdings_sync="planned", trading="planned"),
        sort_order=9,
    ),
    BrokerDef(
        id="dhan",
        name="Dhan",
        short_name="Dhan",
        mark="D",
        color="#0f766e",
        blurb="DhanHQ — planned: holdings sync once a Dhan adapter and access token exist.",
        api_name="DhanHQ",
        docs_url="https://dhanhq.co/docs/",
        capabilities=BrokerCapability(oauth="ready", holdings_sync="planned", trading="planned"),
        sort_order=10,
    ),
)

BROKER_BY_ID: Final[Mapping[str, BrokerDef]] = {b.id: b for b in BROKERS}


def get_broker(broker_id: str) -> BrokerDef | None:
    return BROKER_BY_ID.get(broker_id)


def broker_catalog() -> tuple[BrokerDef, ...]:
    """Brokers in grid order. Pure — no I/O."""
    return tuple(sorted(BROKERS, key=lambda b: b.sort_order))
