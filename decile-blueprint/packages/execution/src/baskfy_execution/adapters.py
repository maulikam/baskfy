"""Per-broker OAuth and holdings adapters (M41).

Each adapter knows how *its* broker shapes an authorize URL and a holdings payload. None of them
run while :data:`baskfy_core.broker_connections.BROKER_OAUTH_REVIEW` is unsigned — the API refuses
to call :meth:`OAuthPort.start` until that gate opens.

Zerodha is the only adapter with a real authorize URL shape today (the desk already uses Kite).
Every other broker raises :class:`BrokerNotWired` until its app credentials and redirect are
registered; the catalog still lists them so the UI is complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from urllib.parse import urlencode

from baskfy_execution.broker_ports import HoldingRow, OAuthStart, normalize_holding

__all__ = [
    "ADAPTERS",
    "BrokerAdapter",
    "BrokerNotWired",
    "LiveOauthBlocked",
    "get_adapter",
]


class BrokerNotWired(RuntimeError):
    """The catalog lists this broker; the adapter is not callable yet."""


class LiveOauthBlocked(RuntimeError):
    """D3 gate is shut — no authorize URL may leave this process."""


@dataclass(frozen=True, slots=True)
class BrokerAdapter:
    """One broker's OAuth + holdings surface.

    ``api_key_env`` names the env var that will hold the broker app key once live OAuth is
    allowed. Adapters read nothing from the environment until then — listing the name is the
    contract for operators, not a load.
    """

    broker_id: str
    api_key_env: str
    authorize_base: str | None

    def start(self, *, api_key: str, redirect_uri: str, state: str) -> OAuthStart:
        if not self.authorize_base:
            raise BrokerNotWired(
                f"{self.broker_id}: OAuth authorize URL is not wired yet"
            )
        if not api_key:
            raise BrokerNotWired(f"{self.broker_id}: missing app key ({self.api_key_env})")
        query = urlencode(
            {
                "api_key": api_key,
                "v": "3",
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return OAuthStart(authorize_url=f"{self.authorize_base}?{query}", state=state)

    def holdings_from_kite_payload(self, rows: list[dict[str, object]]) -> list[HoldingRow]:
        """Map Kite's holdings shape. Other brokers get their own mapper when wired."""
        out: list[HoldingRow] = []
        for row in rows:
            symbol = str(row.get("tradingsymbol") or row.get("symbol") or "")
            if not symbol:
                continue
            last = row.get("last_price")
            out.append(
                normalize_holding(
                    symbol=symbol,
                    exchange=str(row.get("exchange") or "NSE"),
                    quantity=_as_decimal(row.get("quantity"), default="0"),
                    t1_quantity=_as_decimal(row.get("t1_quantity"), default="0"),
                    collateral_quantity=_as_decimal(row.get("collateral_quantity"), default="0"),
                    average_price=_as_decimal(row.get("average_price"), default="0"),
                    last_price=None if last is None else _as_decimal(last, default="0"),
                    product=str(row.get("product") or "CNC"),
                )
            )
        return out


def _as_decimal(value: object, *, default: str) -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


#: Zerodha's login is the documented Kite Connect authorize endpoint.
_ZERODHA: Final = BrokerAdapter(
    broker_id="zerodha",
    api_key_env="BASKFY_KITE_API_KEY",
    authorize_base="https://kite.zerodha.com/connect/login",
)

#: Placeholders: authorize_base is None until each broker's app is registered.
_STUBS: Final[tuple[BrokerAdapter, ...]] = (
    BrokerAdapter(broker_id="hdfc", api_key_env="BASKFY_HDFC_API_KEY", authorize_base=None),
    BrokerAdapter(broker_id="kotak", api_key_env="BASKFY_KOTAK_API_KEY", authorize_base=None),
    BrokerAdapter(broker_id="icici", api_key_env="BASKFY_ICICI_API_KEY", authorize_base=None),
    BrokerAdapter(broker_id="upstox", api_key_env="BASKFY_UPSTOX_API_KEY", authorize_base=None),
    BrokerAdapter(broker_id="angelone", api_key_env="BASKFY_ANGEL_API_KEY", authorize_base=None),
    BrokerAdapter(broker_id="groww", api_key_env="BASKFY_GROWW_API_KEY", authorize_base=None),
    BrokerAdapter(broker_id="fyers", api_key_env="BASKFY_FYERS_API_KEY", authorize_base=None),
    BrokerAdapter(broker_id="fivepaisa", api_key_env="BASKFY_FIVEPAISA_API_KEY", authorize_base=None),
    BrokerAdapter(broker_id="dhan", api_key_env="BASKFY_DHAN_API_KEY", authorize_base=None),
)

ADAPTERS: Final[dict[str, BrokerAdapter]] = {
    _ZERODHA.broker_id: _ZERODHA,
    **{a.broker_id: a for a in _STUBS},
}


def get_adapter(broker_id: str) -> BrokerAdapter | None:
    return ADAPTERS.get(broker_id)


def total_quantity(row: HoldingRow) -> Decimal:
    """Non-negotiable #2: quantity + t1 + collateral."""
    return row.quantity + row.t1_quantity + row.collateral_quantity
