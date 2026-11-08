"""FO2 — ``KiteProvider.fno_option_contracts``: the NFO master for every F&O underlying at once.

The options pack's ``option_contracts(underlying)`` reads one underlying's CE/PE rows; FO2 widens
the nightly master to all of them (``docs/fno/06`` FO2). Asserted: one dump, one limiter token,
every underlying's options and nothing else (no futures), each row's underlying its own ``name``,
a row missing a sizing fact skipped, and a lot above a smallint kept exactly.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from cryptography.fernet import Fernet

from baskfy_providers.kite import KiteProvider, KiteRuntime
from baskfy_providers.settings import ProviderSettings
from baskfy_providers.tokens import AccessTokenStore


class CountingLimiter:
    def __init__(self) -> None:
        self.acquired = 0

    def acquire(self, tokens: float = 1.0) -> float:
        self.acquired += int(tokens)
        return 0.0


class DumpKite:
    """Serves one NFO dump and counts the calls."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.calls: list[str | None] = []

    def set_access_token(self, access_token: str) -> None:
        del access_token

    def instruments(self, exchange: str | None = None) -> list[dict[str, object]]:
        self.calls.append(exchange)
        return self._rows

    # The rest of Kite Connect's read surface, unused here. No order verb exists on this fake.
    def historical_data(
        self,
        instrument_token: int,
        from_date: dt.date | dt.datetime | str,
        to_date: dt.date | dt.datetime | str,
        interval: str,
    ) -> list[dict[str, object]]:
        raise AssertionError("not read by the master")

    def holdings(self) -> list[dict[str, object]]:
        raise AssertionError("not read by the master")

    def margins(self, segment: str | None = None) -> dict[str, object]:
        raise AssertionError("not read by the master")

    def quote(self, *instruments: str) -> dict[str, dict[str, object]]:
        raise AssertionError("not read by the master")

    def trades(self) -> list[dict[str, object]]:
        raise AssertionError("not read by the master")


def _provider(fake: DumpKite, tmp_path: Path, limiter: CountingLimiter) -> KiteProvider:
    key = Fernet.generate_key().decode()
    path = tmp_path / "kite-token.enc"
    store = AccessTokenStore(str(path), key)
    store.save("tok")
    settings = ProviderSettings(
        _env_file=None,
        kite_api_key="k",
        kite_api_secret="s",
        kite_token_encryption_key=key,
        kite_token_path=str(path),
    )
    return KiteProvider(
        settings,
        KiteRuntime(rate_limiter=limiter, client_factory=lambda _key: fake, token_store=store),
    )


def _row(  # noqa: PLR0913 - one argument per master column the test varies
    token: int, name: str, kind: str, *, strike: float = 100.0, lot: int = 50, tick: float = 0.05
) -> dict[str, object]:
    return {
        "instrument_token": token,
        "tradingsymbol": f"{name}26OCT{int(strike)}{kind}",
        "name": name,
        "expiry": dt.date(2026, 10, 27),
        "strike": strike,
        "instrument_type": kind,
        "lot_size": lot,
        "tick_size": tick,
        "segment": "NFO-OPT" if kind in ("CE", "PE") else "NFO-FUT",
        "exchange": "NFO",
    }


def test_every_underlyings_options_in_one_call(tmp_path: Path) -> None:
    rows = [
        _row(5, "NIFTY", "CE", strike=25000, lot=65),
        _row(4, "BANKNIFTY", "PE", strike=55000, lot=30),
        _row(3, "IDEA", "CE", strike=7.5, lot=71_475),
        _row(2, "RELIANCE", "FUT", strike=0),
        _row(1, "RELIANCE", "PE", strike=1400, lot=500),
        {**_row(6, "SBIN", "CE"), "lot_size": 0},  # a sizing fact missing: skipped, not guessed
    ]
    fake = DumpKite(rows)
    limiter = CountingLimiter()
    records = _provider(fake, tmp_path, limiter).fno_option_contracts()
    assert fake.calls == ["NFO"]
    assert limiter.acquired == 1
    assert [r.instrument_token for r in records] == [1, 3, 4, 5], "sorted by token, no future"
    assert {r.underlying for r in records} == {"NIFTY", "BANKNIFTY", "IDEA", "RELIANCE"}
    idea = next(r for r in records if r.underlying == "IDEA")
    assert idea.lot_size == 71_475


def test_it_agrees_with_the_per_underlying_read_for_nifty(tmp_path: Path) -> None:
    rows = [
        _row(1, "NIFTY", "CE", strike=25000, lot=65),
        _row(2, "NIFTY", "PE", strike=25000, lot=65),
        _row(3, "BANKNIFTY", "CE", strike=55000, lot=30),
    ]
    provider = _provider(DumpKite(rows), tmp_path, CountingLimiter())
    everything = provider.fno_option_contracts()
    assert [r for r in everything if r.underlying == "NIFTY"] == provider.option_contracts("NIFTY")
