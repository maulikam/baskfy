"""Holdings sync helpers (Tree-3 leaf 3.3 / Tree-4 leaf 4.2 / Tree-5 leaf C1).

Returns rows shaped like :class:`baskfy_execution.broker_ports.HoldingRow`, normalised
through :func:`normalize_holding`. Quantity contract (desk non-negotiable #2):

    total = quantity + t1_quantity + collateral_quantity

**Provenance (Tree-5 leaf C1).** :func:`holdings_for_broker` returns a
:class:`HoldingsResult`, not a bare list, because a bare list cannot say where its numbers
came from. Before this, every non-empty result was labelled "fixture holdings" by the
router — *including a genuine live Kite fetch*, which told a user their real money was
fake. ``source`` now answers that question directly:

``live``
    A real broker fetch returned rows. These are the caller's actual holdings.
``fixture``
    Fabricated rows from ``BASKFY_BROKER_HOLDINGS_FIXTURE``. ``degraded`` says whether the
    fixture was *asked for* (DRY_RUN / an unconfigured deployment) or *fallen back to*
    after a live path failed.
``empty``
    A path that produced no rows at all. Structurally cannot carry any.
``unwired``
    ``broker_id`` has no holdings adapter in this codebase (see ``_HOLDINGS_WIRED``).

``degraded`` is deliberately a second axis rather than a fifth enum value: ``source``
answers "where did these numbers come from", ``degraded`` answers "is this what we
intended to serve". A degraded fixture is still a fixture — collapsing the two into one
enum would make every client that switches on ``source`` re-learn the whole set to keep
telling live from fake, which is the mistake being fixed.

When ``DRY_RUN`` is false and an encrypted access token is present, Zerodha holdings are
fetched from Kite ``GET /portfolio/holdings`` (read-only — never the order gateway).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from baskfy_execution.broker_ports import HoldingRow, normalize_holding

from baskfy_api.broker_oauth import dry_run_enabled, token_store_for
from baskfy_providers.errors import ProviderError

__all__ = [
    "HOLDINGS_SOURCES",
    "HoldingsResult",
    "HoldingsSource",
    "fetch_kite_holdings",
    "holding_row_to_dict",
    "holdings_for_broker",
    "parse_kite_holdings_payload",
]

logger = logging.getLogger(__name__)

#: The provenance enum, pinned by the Tree-5 contract. Never collapse it to a boolean.
HoldingsSource = Literal["live", "fixture", "empty", "unwired"]

#: The same four values as data, for tests and for clients that enumerate them.
HOLDINGS_SOURCES: frozenset[str] = frozenset({"live", "fixture", "empty", "unwired"})

#: Sources that describe rows we actually hold. Everything else must carry none.
_ROW_BEARING_SOURCES: frozenset[str] = frozenset({"live", "fixture"})

#: Brokers that can sync holdings once a live adapter exists.
_HOLDINGS_WIRED = frozenset({"zerodha"})

_KITE_HOLDINGS_URL = "https://api.kite.trade/portfolio/holdings"

#: Everything a read-only holdings GET can legitimately fail with: httpx's whole transport and
#: status family, a body that is not decodable JSON, and a local socket error. Named as a tuple
#: so the fallback below stays a narrow ``except`` rather than a blanket one (house rule 3) — a
#: bare ``ValueError`` would have been wide enough to swallow a bug in our own row mapping and
#: report it to the user as "the broker is down".
_LIVE_FETCH_ERRORS: tuple[type[Exception], ...] = (
    httpx.HTTPError,
    httpx.InvalidURL,
    json.JSONDecodeError,
    UnicodeDecodeError,
    OSError,
)


@dataclass(frozen=True, slots=True)
class HoldingsResult:
    """Holdings plus the provenance of those holdings.

    The invariants below are enforced in ``__post_init__`` on a *frozen* dataclass whose
    rows are a ``tuple``, so they hold for the object's whole lifetime rather than only at
    the moment it was built. In particular ``source="empty"`` can never carry rows: there
    is no constructor that accepts them and no mutation that could add them afterwards.
    """

    rows: tuple[HoldingRow, ...]
    source: HoldingsSource
    degraded: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        if self.source not in HOLDINGS_SOURCES:
            raise ValueError(f"unknown holdings source {self.source!r}")
        if self.source in _ROW_BEARING_SOURCES and not self.rows:
            # A row-bearing source with no rows is a lie in the other direction: it would
            # tell a client "these are live numbers" about an empty list.
            raise ValueError(f"source={self.source!r} requires at least one row")
        if self.source not in _ROW_BEARING_SOURCES and self.rows:
            raise ValueError(f"source={self.source!r} must not carry rows")
        if self.degraded and self.source not in ("fixture", "empty"):
            raise ValueError(f"source={self.source!r} cannot be degraded")

    @property
    def is_live(self) -> bool:
        """True only for numbers a broker actually reported."""
        return self.source == "live"

    @classmethod
    def live(cls, rows: Sequence[HoldingRow], *, detail: str = "") -> HoldingsResult:
        """A real broker fetch. No rows means ``empty`` — an empty live fetch is not live."""
        if not rows:
            return cls.empty(detail=detail or "the broker reported no holdings")
        return cls(rows=tuple(rows), source="live", detail=detail)

    @classmethod
    def fixture(
        cls, rows: Sequence[HoldingRow], *, detail: str = "", degraded: bool = False
    ) -> HoldingsResult:
        """Fabricated rows. ``degraded`` marks a fallback rather than a deliberate stub."""
        if not rows:
            return cls.empty(detail=detail, degraded=degraded)
        return cls(rows=tuple(rows), source="fixture", degraded=degraded, detail=detail)

    @classmethod
    def empty(cls, *, detail: str = "", degraded: bool = False) -> HoldingsResult:
        """No rows. Takes no ``rows`` argument at all — that is the invariant, structurally."""
        return cls(rows=(), source="empty", degraded=degraded, detail=detail)

    @classmethod
    def unwired(cls, broker_id: str) -> HoldingsResult:
        """``broker_id`` has no holdings adapter here. Never degraded — it is a static fact.

        No ``detail``: ``source="unwired"`` is the whole statement, and the router already
        names the broker in the prose it builds around it.
        """
        return cls(rows=(), source="unwired")


def holding_row_to_dict(row: HoldingRow) -> dict[str, object]:
    """JSON-friendly HoldingRow (Decimal → str) for the API response."""
    return {
        "symbol": row.symbol,
        "exchange": row.exchange,
        "quantity": str(row.quantity),
        "t1_quantity": str(row.t1_quantity),
        "collateral_quantity": str(row.collateral_quantity),
        "average_price": str(row.average_price),
        "last_price": None if row.last_price is None else str(row.last_price),
        "product": row.product,
        # Documented sum rule for clients / tests (non-negotiable #2).
        "total_quantity": str(row.quantity + row.t1_quantity + row.collateral_quantity),
    }


def parse_kite_holdings_payload(
    payload: Mapping[str, object] | list[object],
) -> list[HoldingRow]:
    """Map a Kite holdings JSON body to :class:`HoldingRow` list.

    Accepts either the full ``{"data": [...]}`` envelope or a bare list of holding dicts.
    Unknown / malformed rows are skipped rather than failing the whole sync.
    """
    if isinstance(payload, list):
        items: list[object] = payload
    else:
        raw = payload.get("data", payload)
        items = raw if isinstance(raw, list) else []

    rows: list[HoldingRow] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        symbol = item.get("tradingsymbol") or item.get("symbol")
        if not symbol:
            continue
        try:
            rows.append(
                normalize_holding(
                    symbol=str(symbol),
                    exchange=str(item.get("exchange", "NSE")),
                    quantity=item.get("quantity", 0),
                    t1_quantity=item.get("t1_quantity", 0),
                    collateral_quantity=item.get("collateral_quantity", 0),
                    average_price=item.get("average_price", 0),
                    last_price=item.get("last_price"),
                    product=str(item.get("product", "CNC")),
                )
            )
        except (TypeError, ValueError):
            continue
    return rows


def fetch_kite_holdings(*, api_key: str, access_token: str) -> list[HoldingRow]:
    """GET Kite portfolio holdings. Read-only; never places an order."""
    headers = {
        "Authorization": f"token {api_key}:{access_token}",
        "X-Kite-Version": "3",
    }
    response = httpx.get(_KITE_HOLDINGS_URL, headers=headers, timeout=30.0)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, (dict, list)):
        return []
    return parse_kite_holdings_payload(body)


def _fixture_holdings() -> list[HoldingRow]:
    raw_path = os.environ.get("BASKFY_BROKER_HOLDINGS_FIXTURE", "").strip()
    if not raw_path:
        return []
    path = Path(raw_path)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    rows: list[HoldingRow] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            rows.append(
                normalize_holding(
                    symbol=str(item["symbol"]),
                    exchange=str(item.get("exchange", "NSE")),
                    quantity=item.get("quantity", 0),
                    t1_quantity=item.get("t1_quantity", 0),
                    collateral_quantity=item.get("collateral_quantity", 0),
                    average_price=item.get("average_price", "0"),
                    last_price=item.get("last_price"),
                    product=str(item.get("product", "CNC")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def _stub_holdings(detail: str) -> HoldingsResult:
    """A fixture we asked for: DRY_RUN, or a deployment with no live session configured.

    Not ``degraded`` — nothing failed here. The rows are fabricated and say so.
    """
    return HoldingsResult.fixture(_fixture_holdings(), detail=detail, degraded=False)


def _degraded_holdings(detail: str) -> HoldingsResult:
    """A fixture we fell back to after a live path we expected to work did not.

    Still ``source="fixture"`` — the numbers are just as fabricated — but ``degraded`` is
    true, because "you are seeing sample data because we are broken" is a different
    statement from "you are seeing sample data because this box has no broker session".
    """
    return HoldingsResult.fixture(_fixture_holdings(), detail=detail, degraded=True)


def holdings_for_broker(broker_id: str) -> HoldingsResult:  # noqa: PLR0911 - a guard ladder
    """Sync (or safely stub) holdings for ``broker_id``, tagged with where they came from.

    Never raises for a missing live broker — the worst case is a tagged empty result. Does
    not reach the order path or the trading gateway; the only network call it can make is
    Kite's read-only ``GET /portfolio/holdings``.
    """
    if broker_id not in _HOLDINGS_WIRED:
        return HoldingsResult.unwired(broker_id)

    if dry_run_enabled():
        # No detail: the router's DRY_RUN prose and the response's own ``dry_run`` field
        # already say it, and repeating it would read twice in one sentence. Under DRY_RUN
        # the note is byte-identical to the pre-C1 one — only the live path changed.
        return _stub_holdings("")

    api_key = os.environ.get("BASKFY_KITE_API_KEY", "").strip()
    if not api_key:
        return _stub_holdings("no broker app key is configured on this deployment")

    try:
        store = token_store_for()
        if not store.exists():
            return _stub_holdings("this account has not connected a broker session yet")
        token = store.require_fresh()
    except (ProviderError, OSError) as exc:
        # Explicit, narrow, and reported: an expired or unreadable token is a session that
        # should have worked. Only the exception *type* is logged — the message can name
        # the token path, and nothing about a credential belongs in a log line.
        logger.warning(
            "holdings: stored broker session unusable for %s (%s); serving fixture",
            broker_id,
            type(exc).__name__,
        )
        return _degraded_holdings(
            f"the stored broker session could not be used ({type(exc).__name__})"
        )

    try:
        rows = fetch_kite_holdings(api_key=api_key, access_token=token.value)
    except _LIVE_FETCH_ERRORS as exc:
        # A named family, not a blanket catch: a bug in our own row mapping still escapes and
        # is seen. 500-ing the holdings page over a broker timeout is worse than showing
        # labelled sample data — and the label is what pays for the resilience, because the
        # caller is told this is a degraded fixture rather than their money.
        logger.warning(
            "holdings: live fetch failed for %s (%s); serving fixture",
            broker_id,
            type(exc).__name__,
        )
        return _degraded_holdings(f"the live holdings fetch failed ({type(exc).__name__})")

    return HoldingsResult.live(rows, detail="fetched from the broker's holdings endpoint")
