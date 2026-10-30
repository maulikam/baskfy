"""The replay provider, Python half: one recorded trading day served from a bundle, no network.

The twin of ``rust/crates/baskfy-providers/src/replay.rs`` (DECISIONS-RS R7.3). Both read the
same bundle (format 1, documented in that file's module docs and in ``tools/parity/README.md``)
and must answer every call identically — the same records, the same frames, the same refusal
messages — so that a difference in what the Python and Rust nightlies write is a difference in
the code, never in the inputs. ``replay_selfcheck.py`` drives both over one bundle and compares.

Harness code only. It is injected into the Python worker by ``nightly_py.py`` (which monkeypatches
``baskfy_worker.providers.build_provider_stack`` at process start); production Python is
unchanged. It sits behind the production ``CompositeProvider``, exactly as Kite and NSE do.

A call the bundle cannot answer raises ``ProviderUnavailable("no recorded answer for <call>")``;
a call the manifest's ``refuse`` list names raises the error class it names.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from functools import cached_property
from pathlib import Path
from typing import Any

import polars as pl

from baskfy_providers import errors as perrors
from baskfy_providers.ports import Capability, ProviderHealth
from baskfy_providers.records import (
    BHAVCOPY_SCHEMA,
    DAILY_BARS_SCHEMA,
    CorporateAction,
    EquityFundamental,
    IndexSnapshot,
    InstrumentRecord,
    ListingRecord,
)

PROVIDER_NAME = "replay"
BUNDLE_FORMAT = 1

_KINDS: dict[str, type[perrors.ProviderError]] = {
    "ProviderError": perrors.ProviderError,
    "ProviderUnavailable": perrors.ProviderUnavailable,
    "CredentialsMissing": perrors.CredentialsMissing,
    "TransientProviderError": perrors.TransientProviderError,
    "RateLimited": perrors.RateLimited,
    "UpstreamUnavailable": perrors.UpstreamUnavailable,
    "ProviderDataError": perrors.ProviderDataError,
    "UnexpectedPayload": perrors.UnexpectedPayload,
}


def _unavailable(message: str) -> perrors.ProviderUnavailable:
    return perrors.ProviderUnavailable(message, provider=PROVIDER_NAME)


def _arg_text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _call_text(method: str, args: Sequence[tuple[str, str]]) -> str:
    return f"{method}({', '.join(f'{k}={v}' for k, v in args)})"


def _decimal_at(value: str | None, scale: int, column: str) -> Decimal | None:
    if value is None:
        return None
    d = Decimal(value)
    q = d.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_EVEN)
    if q != d:
        raise _unavailable(f"recorded value {d} in column {column!r} has more than {scale} places")
    return q


class ReplayProvider:
    """The replay adapter over one bundle directory (``ReplayProvider`` in Rust)."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        path = self._root / "manifest.json"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise _unavailable(f"cannot read replay manifest {path}: {exc}") from exc
        manifest = json.loads(text)
        if manifest.get("format") != BUNDLE_FORMAT:
            raise _unavailable(f"replay bundle format {manifest.get('format')} is not {BUNDLE_FORMAT}")
        self.manifest: dict[str, Any] = manifest
        self.trade_date = dt.date.fromisoformat(manifest["trade_date"])

    # --- HealthReporting -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.LIST_INSTRUMENTS,
                Capability.DAILY_BARS,
                Capability.INDEX_CONSTITUENTS,
                Capability.INDEX_SNAPSHOTS,
                Capability.CORPORATE_ACTIONS,
                Capability.LISTINGS,
                Capability.BHAVCOPY,
                Capability.EQUITY_FUNDAMENTALS,
            }
        )

    def check(self) -> ProviderHealth:
        return ProviderHealth(
            name=PROVIDER_NAME,
            available=True,
            capabilities=self.capabilities(),
            detail=f"replay bundle {self._root} for {self.trade_date.isoformat()}",
        )

    # --- plumbing ------------------------------------------------------------------------------

    def _refused(self, method: str, args: Sequence[tuple[str, str]]) -> None:
        for refusal in self.manifest.get("refuse", []):
            if refusal["method"] != method:
                continue
            wanted = refusal.get("args") or {}
            if all(any(name == k and given == _arg_text(v) for name, given in args) for k, v in wanted.items()):
                cls = _KINDS.get(refusal.get("kind") or "ProviderUnavailable", perrors.ProviderUnavailable)
                raise cls(refusal["message"], provider=PROVIDER_NAME)

    @staticmethod
    def _no_answer(method: str, args: Sequence[tuple[str, str]]) -> perrors.ProviderUnavailable:
        return _unavailable(f"no recorded answer for {_call_text(method, args)}")

    def _read(self, relative: str, method: str, args: Sequence[tuple[str, str]]) -> Any:
        path = self._root / "calls" / relative
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise self._no_answer(method, args) from None
        except OSError as exc:
            raise _unavailable(
                f"cannot read the recorded answer for {_call_text(method, args)}: {exc}"
            ) from exc
        try:
            return json.loads(text)
        except ValueError as exc:
            raise _unavailable(
                f"the recorded answer for {_call_text(method, args)} does not parse: {exc}"
            ) from exc

    @cached_property
    def _bars(self) -> dict[str, Any] | None:
        path = self._root / "calls" / "daily_bars.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    # --- BarsProvider --------------------------------------------------------------------------

    def list_instruments(self) -> list[InstrumentRecord]:
        self._refused("list_instruments", [])
        rows = self._read("list_instruments.json", "list_instruments", [])
        return [InstrumentRecord.model_validate(r) for r in rows]

    def daily_bars(self, token: int, start: dt.date, end: dt.date) -> pl.DataFrame:
        args = [("token", str(token)), ("start", start.isoformat()), ("end", end.isoformat())]
        self._refused("daily_bars", args)
        bundle = self._bars
        if bundle is None:
            raise self._no_answer("daily_bars", args)
        held_start = dt.date.fromisoformat(bundle["start"])
        held_end = dt.date.fromisoformat(bundle["end"])
        if start < held_start or end > held_end:
            raise _unavailable(
                f"no recorded answer for {_call_text('daily_bars', args)}: the bundle holds "
                f"{held_start.isoformat()}..{held_end.isoformat()}"
            )
        rows = bundle["by_token"].get(str(token))
        if rows is None:
            raise self._no_answer("daily_bars", args)
        picked = [r for r in rows if start <= dt.date.fromisoformat(r["date"]) <= end]
        return pl.DataFrame(
            {
                "symbol": [r["symbol"] for r in picked],
                "date": [dt.date.fromisoformat(r["date"]) for r in picked],
                "open": [_decimal_at(r["open"], 4, "open") for r in picked],
                "high": [_decimal_at(r["high"], 4, "high") for r in picked],
                "low": [_decimal_at(r["low"], 4, "low") for r in picked],
                "close": [_decimal_at(r["close"], 4, "close") for r in picked],
                "volume": [int(r["volume"]) for r in picked],
                "source": ["kite" for _ in picked],
            },
            schema=DAILY_BARS_SCHEMA,
        )

    # --- ReferenceProvider ---------------------------------------------------------------------

    def index_constituents(self, index_slug: str, on: dt.date) -> list[str]:
        args = [("index_slug", index_slug), ("on", on.isoformat())]
        self._refused("index_constituents", args)
        if "/" in index_slug or "\\" in index_slug or index_slug.startswith("."):
            raise self._no_answer("index_constituents", args)
        rows = self._read(f"index_constituents/{index_slug}/{on.isoformat()}.json", "index_constituents", args)
        return [str(s) for s in rows]

    def index_snapshots(self, on: dt.date) -> list[IndexSnapshot]:
        args = [("on", on.isoformat())]
        self._refused("index_snapshots", args)
        rows = self._read(f"index_snapshots/{on.isoformat()}.json", "index_snapshots", args)
        return [IndexSnapshot.model_validate(r) for r in rows]

    def corporate_actions(self, since: dt.date) -> list[CorporateAction]:
        args = [("since", since.isoformat())]
        self._refused("corporate_actions", args)
        rows = self._read(f"corporate_actions/{since.isoformat()}.json", "corporate_actions", args)
        return [CorporateAction.model_validate(r) for r in rows]

    def listings(self) -> list[ListingRecord]:
        self._refused("listings", [])
        rows = self._read("listings.json", "listings", [])
        return [ListingRecord.model_validate(r) for r in rows]

    def bhavcopy(self, on: dt.date) -> pl.DataFrame:
        args = [("on", on.isoformat())]
        self._refused("bhavcopy", args)
        rows = self._read(f"bhavcopy/{on.isoformat()}.json", "bhavcopy", args)

        def dec(name: str, scale: int) -> list[Decimal | None]:
            return [_decimal_at(r.get(name), scale, name) for r in rows]

        return pl.DataFrame(
            {
                "symbol": [r.get("symbol") for r in rows],
                "series": [r.get("series") for r in rows],
                "date": [dt.date.fromisoformat(r["date"]) if r.get("date") else None for r in rows],
                "open": dec("open", 4),
                "high": dec("high", 4),
                "low": dec("low", 4),
                "close": dec("close", 4),
                "prev_close": dec("prev_close", 4),
                "volume": [r.get("volume") for r in rows],
                "turnover": dec("turnover", 2),
                "trades": [r.get("trades") for r in rows],
                "upper_circuit": dec("upper_circuit", 4),
                "lower_circuit": dec("lower_circuit", 4),
            },
            schema=BHAVCOPY_SCHEMA,
        )

    def equity_fundamentals(
        self,
        on: dt.date,
        symbols: Sequence[str],
        *,
        series_by_symbol: Mapping[str, str] | None = None,
    ) -> list[EquityFundamental]:
        del series_by_symbol  # a hint, never an answer
        args = [("on", on.isoformat())]
        self._refused("equity_fundamentals", args)
        rows = self._read(f"equity_fundamentals/{on.isoformat()}.json", "equity_fundamentals", args)
        recorded = [EquityFundamental.model_validate(r) for r in rows]
        by_symbol: dict[str, EquityFundamental] = {}
        for record in recorded:
            by_symbol.setdefault(record.symbol, record)
        seen: set[str] = set()
        out: list[EquityFundamental] = []
        for symbol in symbols:
            if symbol in seen:
                continue
            seen.add(symbol)
            if symbol in by_symbol:
                out.append(by_symbol[symbol])
        return out

    # --- the duck-typed reads ------------------------------------------------------------------

    def sme_listings(self) -> list[ListingRecord]:
        self._refused("sme_listings", [])
        rows = self._read("sme_listings.json", "sme_listings", [])
        return [ListingRecord.model_validate(r) for r in rows]

    def etf_symbols(self) -> list[str]:
        self._refused("etf_symbols", [])
        return [str(s) for s in self._read("etf_symbols.json", "etf_symbols", [])]

    def index_level(self, slug: str) -> Decimal | None:
        args = [("slug", slug)]
        self._refused("index_level", args)
        raise self._no_answer("index_level", args)

    def fno_underlyings(self) -> list[str]:
        self._refused("fno_underlyings", [])
        return [str(s) for s in self._read("fno_underlyings.json", "fno_underlyings", [])]


def replay_stack(root: Path | str, settings: object | None = None) -> object:
    """The replay adapter behind the production ``CompositeProvider``, with the configured breaker."""
    from baskfy_providers.composite import CompositeProvider  # noqa: PLC0415
    from baskfy_providers.settings import get_provider_settings  # noqa: PLC0415

    resolved = settings or get_provider_settings()
    return CompositeProvider(
        [ReplayProvider(root)],
        failure_threshold=getattr(resolved, "provider_circuit_failure_threshold"),
        reset_seconds=getattr(resolved, "provider_circuit_reset_seconds"),
    )
