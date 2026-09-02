#!/usr/bin/env python
"""Run the S2 Kite timing probe by hand (SW11, docs/swing/STANDING-ANSWERS A4).

    cd decile-blueprint
    uv run python ../tools/swing/kite_timing_probe.py --dry-run          # a scripted morning
    uv run python ../tools/swing/kite_timing_probe.py --symbols RELIANCE,TCS --token 738561

The Beat task ``baskfy.swing.timing_probe`` (09:04 IST, weekdays, gated by
``BASKFY_SWING_TIMING_PROBE=true``) is the same body — ``baskfy_worker.tasks.swing_timing_probe
.probe_once`` — over the Kite provider. This is the same walk started from a terminal, for the
morning Maulik is at the desk: it needs a Kite session in the provider's token store (log in
before 09:00), starts sampling at the next scheduled moment, and writes ``S2-kite-timing.md``
(and the ``.done`` marker after a good run) into ``--out`` (default ``docs/swing/status``).

``--dry-run`` reads nothing: it drives the probe with a scripted source under one hypothesis
and prints the report, so the shape of the answer can be seen before a real morning is spent.
Nothing here can act on a price.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from baskfy_providers.records import QuoteRecord  # noqa: E402 - after the path constants
from baskfy_worker.tasks.swing_timing_probe import (  # noqa: E402
    IST,
    ProbeReport,
    probe_once,
    render_report,
    run_timing_probe,
)

DEFAULT_OUT = ROOT / "docs" / "swing" / "status"


class DrivenClock:
    def __init__(self, start: dt.datetime) -> None:
        self.now_at = start

    def now(self) -> dt.datetime:
        return self.now_at

    def sleep(self, seconds: float) -> None:
        self.now_at += dt.timedelta(seconds=seconds)


class ScriptedSource:
    """The 'pre-open prints from 09:08, candle from 09:20:35' hypothesis, for --dry-run."""

    def __init__(self, clock: DrivenClock) -> None:
        self.clock = clock

    def quotes(self, symbols: Sequence[str]) -> list[QuoteRecord]:
        now = self.clock.now()
        opened = now.time() >= dt.time(9, 8)
        return [
            QuoteRecord(
                symbol=s,
                last_price=Decimal("101.50") if now.time() >= dt.time(9, 7, 30) else Decimal("100"),
                volume=12_000 if opened else 0,
                prev_close=Decimal("100"),
                open=Decimal("101.50") if opened else None,
                as_of=now.replace(tzinfo=None),
            )
            for s in symbols
        ]

    def minute_candles(
        self, token: int, start: dt.datetime, end: dt.datetime
    ) -> list[dict[str, object]]:
        last = dt.time(9, 20) if self.clock.now().time() >= dt.time(9, 20, 35) else dt.time(9, 19)
        out: list[dict[str, object]] = []
        minute = start
        while minute.time() <= last:
            out.append({"date": minute, "open": 100, "high": 101, "low": 99, "close": 100.5})
            minute += dt.timedelta(minutes=1)
        return out


def _dry_run(symbols: list[str], token: int) -> int:
    day = dt.datetime.now(tz=IST).date()
    clock = DrivenClock(dt.datetime.combine(day, dt.time(9, 4), tzinfo=IST))
    report: ProbeReport = run_timing_probe(
        ScriptedSource(clock), symbols=symbols, token=token, day=day, now=clock.now,
        sleep=clock.sleep,
    )
    sys.stdout.write(render_report(report, generated_at=clock.now()))
    return 0


def _real(symbols: list[str], token: int, out: Path) -> int:
    from baskfy_providers.factory import build_kite_provider  # noqa: PLC0415 - only for a run
    from baskfy_providers.settings import get_provider_settings  # noqa: PLC0415
    from baskfy_worker.tasks.celery_tasks import KiteProbeSource  # noqa: PLC0415
    from baskfy_worker.telemetry import provider_retry_hooks  # noqa: PLC0415

    provider = build_kite_provider(get_provider_settings(), provider_retry_hooks())
    result = probe_once(
        KiteProbeSource(provider), out_dir=out, symbols=symbols, token=token,
        now=lambda: dt.datetime.now(tz=IST),
    )
    if "skipped" in result:
        print(result["skipped"])
        return 1
    print(f"written {result['report']}; good run: {result['good']}")
    return 0 if result["good"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--symbols", default="RELIANCE,TCS,HDFCBANK,INFY,SBIN",
                        help="comma-separated NSE symbols to quote")
    parser.add_argument("--token", type=int, default=738561,
                        help="the Kite instrument token whose minute candles are asked for "
                             "(default: RELIANCE)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where the report goes")
    parser.add_argument("--dry-run", action="store_true", help="scripted source; print only")
    args = parser.parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.dry_run:
        return _dry_run(symbols, args.token)
    return _real(symbols, args.token, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
