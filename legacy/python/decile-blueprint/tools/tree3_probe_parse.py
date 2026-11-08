"""Tree 3 / G2 — prove the live NSE path end to end, through the repo's own provider.

Not a test: a probe. It makes one real network call per symbol through the factory-built
:class:`NSEProvider` — same Redis token bucket, same archive-then-parse discipline (docs/09) —
and parses the result with the repo's own parser. If this prints ``PARSED OK`` the fill is
mechanically possible; if it does not, nothing downstream is worth running.

    uv run python -m tools.tree3_probe_parse [SYMBOL[:SERIES] ...]
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
from pathlib import Path

from baskfy_providers.archive import LocalRawArchive
from baskfy_providers.factory import build_nse_provider
from baskfy_providers.settings import ProviderSettings

#: One large cap (an obvious sanity anchor) and one BE-series name, because a wrong series is
#: answered with 200 and an empty body — the failure this probe most needs to catch.
DEFAULT_SYMBOLS: tuple[tuple[str, str], ...] = (("INFY", "EQ"), ("3IINFOLTD", "BE"))


def _requested(argv: list[str]) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for arg in argv[1:]:
        symbol, _, series = arg.partition(":")
        pairs.append((symbol.strip().upper(), (series or "EQ").strip().upper()))
    return tuple(pairs) or DEFAULT_SYMBOLS


def main(argv: list[str]) -> int:
    symbols = _requested(argv)
    settings = ProviderSettings()
    # A throwaway archive: the probe must not pollute the real one with a today-dated file
    # that a later run would then read back instead of refetching.
    with tempfile.TemporaryDirectory(prefix="tree3-probe-") as tmp:
        provider = build_nse_provider(settings, LocalRawArchive(Path(tmp)))
        health = provider.check()
        if not health.available:
            print(f"PROVIDER UNAVAILABLE {health.detail}")
            return 2

        on = dt.date.today()
        ok = 0
        for symbol, series in symbols:
            records = provider.equity_fundamentals(on, [symbol], series_by_symbol={symbol: series})
            if not records:
                print(f"NO RECORD {symbol} (series={series})")
                continue
            record = records[0]
            if record.shares_outstanding is None:
                print(f"NO SHARES {symbol} pe={record.pe}")
                continue
            ok += 1
            print(
                f"PARSED OK shares_outstanding={record.shares_outstanding} "
                f"symbol={record.symbol} series={series} last={record.last_price} "
                f"marketcap_cr={record.marketcap_cr} pe={record.pe}"
            )
    print(f"PROBE {ok}/{len(symbols)} parsed")
    return 0 if ok == len(symbols) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
