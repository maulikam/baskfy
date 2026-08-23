"""Holdings sync helpers (Tree-3 leaf 3.3 / Tree-4 leaf 4.2).

Returns rows shaped like :class:`baskfy_execution.broker_ports.HoldingRow`, normalised
through :func:`normalize_holding`. Quantity contract (desk non-negotiable #2):

    total = quantity + t1_quantity + collateral_quantity

When there is no live broker session (``DRY_RUN``, missing token, or unwired broker),
this module returns an empty list or an optional JSON fixture from
``BASKFY_BROKER_HOLDINGS_FIXTURE`` — it never crashes and never places an order.

When ``DRY_RUN`` is false and an encrypted access token is present, Zerodha holdings are
fetched from Kite ``GET /portfolio/holdings`` (read-only — never the order gateway).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from baskfy_execution.broker_ports import HoldingRow, normalize_holding

from baskfy_api.broker_oauth import dry_run_enabled, token_store_for
from baskfy_providers.errors import CredentialsMissing

__all__ = [
    "fetch_kite_holdings",
    "holding_row_to_dict",
    "holdings_for_broker",
    "parse_kite_holdings_payload",
]

#: Brokers that can sync holdings once a live adapter exists.
_HOLDINGS_WIRED = frozenset({"zerodha"})

_KITE_HOLDINGS_URL = "https://api.kite.trade/portfolio/holdings"


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
                    average_price=item.get("average_price", item.get("average_price", 0)),
                    last_price=item.get("last_price"),
                    product=str(item.get("product", "CNC")),
                )
            )
        except (TypeError, ValueError):
            continue
    return rows


def fetch_kite_holdings(*, api_key: str, access_token: str) -> list[HoldingRow]:
    """GET Kite portfolio holdings. Read-only; never places an order."""
    import httpx  # noqa: PLC0415

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


def holdings_for_broker(broker_id: str) -> list[HoldingRow]:  # noqa: PLR0911 - a guard ladder
    """Sync (or safely stub) holdings for ``broker_id``.

    Never raises for a missing live broker — empty / fixture only. Does not reach the
    order path or the trading gateway.
    """
    if broker_id not in _HOLDINGS_WIRED:
        return []

    if dry_run_enabled():
        return _fixture_holdings()

    api_key = os.environ.get("BASKFY_KITE_API_KEY", "").strip()
    if not api_key:
        return _fixture_holdings()

    try:
        store = token_store_for()
        if not store.exists():
            return _fixture_holdings()
        token = store.require_fresh()
    except CredentialsMissing:
        return _fixture_holdings()
    except Exception:
        return _fixture_holdings()

    try:
        return fetch_kite_holdings(api_key=api_key, access_token=token.value)
    except Exception:
        # Network / Kite errors fall back to fixture rather than 500 the UI.
        return _fixture_holdings()
