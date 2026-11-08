"""Deterministic bars for the desk-SCORE parity tests (docs/ranking/PLAN.md C2, leaf 2.B).

Closed-form, like `_scan_fixture`: no network, no database, no randomness, the same numbers on
every machine. It is shared by `packages/core/tests/test_desk_score_service.py` and by the desk's
own `kite-momentum-rebalancer/tests/test_desk_score_parity.py`, which imports it by path the way
`test_generated_scan.py` imports `_scan_fixture` — so both trees are graded on one fixture.

What it has to contain, and why each piece is here:

* **Forty-eight symbols over 320 sessions**, so percentiles and the 1/99 clips run over a real
  cross-section rather than five names, and every twelve-month window is full.
* **Every reject token `score.apply_filters` can emit**, so parity covers the rejected half of the
  table and not only the ranked half: a trend breaker (`below50&200DMA`), a six-month faller
  (`neg3M&6M`), a circuit-hitter (`circuits`, carried), a thin name (`illiquid`), a crashed name
  (`far_from_high`), a trade-to-trade series (`T2T_series`, carried) and the untouchable SGB
  (`excluded_instrument`).
* **Two symbols with identical bars and identical carried columns**, so SCORE ties and the book's
  tie-break — whatever order `sort_values` leaves equal scores in — is part of what must match.
* **A young listing** (210 sessions of history): old enough for a 200-day average, too young for
  any one-year window. The engine nulls the one-year median turnover and away-from-high with the
  return, so the book rejects it (`illiquid;far_from_high;`) rather than ranking a row it cannot
  score — and parity has to hold on that path too.
* **A symbol with bars but no carried row**, which the scan's inner join drops entirely.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import polars as pl

BARS = 320
START = dt.date(2025, 1, 6)


@dataclass(frozen=True)
class Spec:
    symbol: str
    drift: float = 0.0012
    amp: float = 0.04
    period: float = 11.0
    #: From this bar on, the close is multiplied by ``shock_factor`` (a crash or a trend break).
    shock_bar: int | None = None
    shock_factor: float = 1.0
    #: After ``fade_bar`` the drift reverses to ``fade_drift`` (a rolled-over trend).
    fade_bar: int | None = None
    fade_drift: float = 0.0
    first_bar: int = 0
    volume: float = 1_500_000.0
    series: str = "EQ"
    marketcap: float = 15_000.0
    beta: float = 1.0
    circuits_3m: int = 0
    circuits_1y: int = 0
    fno: int = 1
    carried: bool = True
    #: Two specs sharing a ``twin`` key get byte-identical bars (a guaranteed SCORE tie).
    twin: int | None = None


def _healthy(i: int) -> Spec:
    return Spec(
        symbol=f"MOM{i:02d}",
        drift=0.0004 + (i % 9) * 0.00022,
        amp=0.02 + (i % 5) * 0.011,
        period=7.0 + (i % 7) * 2.5,
        volume=700_000.0 + (i % 11) * 260_000.0,
        marketcap=4_000.0 + (i % 6) * 5_500.0,
        beta=round(0.7 + (i % 8) * 0.13, 2),
        circuits_1y=i % 10,
        fno=i % 2,
    )


SPECS: tuple[Spec, ...] = (
    *(_healthy(i) for i in range(36)),
    # --- one per reject token --------------------------------------------------------------
    Spec("TRENDBRK", drift=0.0008, fade_bar=200, fade_drift=-0.0035),
    Spec("SIXMFALL", drift=0.0030, fade_bar=180, fade_drift=-0.0016, amp=0.01),
    Spec("CIRCUITY", drift=0.0015, circuits_3m=7, circuits_1y=12),
    Spec("THINNAME", drift=0.0014, volume=40_000.0),
    Spec("CRASHED", drift=0.0016, shock_bar=290, shock_factor=0.55),
    Spec("TTSERIES", drift=0.0017, series="BE"),
    Spec("SGBDE31III", drift=0.0003, amp=0.005, fno=0),
    # --- ties, youth, and a name the carried frame does not know ----------------------------
    Spec("TWINA", drift=0.0013, amp=0.03, period=9.0, twin=1),
    Spec("TWINB", drift=0.0013, amp=0.03, period=9.0, twin=1),
    Spec("YOUNG", drift=0.0020, first_bar=110),
    Spec("NOCARRY", drift=0.0018, carried=False),
    Spec("PARABOL", drift=0.0045, amp=0.015, volume=3_000_000.0, beta=1.8, marketcap=25_000.0),
)


def trading_days() -> list[dt.date]:
    """Weekdays only, so every window is the length the calendar says it is."""
    days: list[dt.date] = []
    day = START
    while len(days) < BARS:
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def _close(index: int, spec: Spec, bar: int) -> float:
    if spec.fade_bar is not None and bar > spec.fade_bar:
        log_level = spec.drift * spec.fade_bar + spec.fade_drift * (bar - spec.fade_bar)
    else:
        log_level = spec.drift * bar
    seed = spec.twin if spec.twin is not None else index
    wave = spec.amp * math.sin(bar / spec.period + seed)
    jitter = 0.006 * math.sin(bar * 1.731 + seed * 0.37)
    shock = spec.shock_factor if spec.shock_bar is not None and bar >= spec.shock_bar else 1.0
    return round(100.0 * math.exp(log_level) * (1.0 + wave + jitter) * shock, 2)


def bars() -> pl.DataFrame:
    days = trading_days()
    rows: list[dict[str, object]] = []
    for index, spec in enumerate(SPECS):
        seed = spec.twin if spec.twin is not None else index
        for bar, day in enumerate(days):
            if bar < spec.first_bar:
                continue
            close = _close(index, spec, bar)
            volume = spec.volume * (1.0 + 0.25 * math.sin(bar / 5.0 + seed))
            rows.append(
                {
                    "symbol": spec.symbol,
                    "instrument_id": 1000 + index,
                    "date": day,
                    "open": round(close * 0.996, 2),
                    "high": round(close * 1.011, 2),
                    "low": round(close * 0.989, 2),
                    "close": close,
                    "volume": round(volume),
                    "close_raw": close,
                    "volume_raw": round(volume),
                }
            )
    return pl.DataFrame(rows)


def carried() -> pl.DataFrame:
    """`symbol`, `series` and the five CARRIED_COLUMNS, as the desk's newest upload has them."""
    return pl.DataFrame(
        [
            {
                "symbol": spec.symbol,
                "series": spec.series,
                "marketcap": spec.marketcap,
                "beta": spec.beta,
                "circuits_three_months": spec.circuits_3m,
                "circuits_one_year": spec.circuits_1y,
                "is_nifty_fno": spec.fno,
            }
            for spec in SPECS
            if spec.carried
        ]
    )


def as_of() -> dt.date:
    return trading_days()[-1]
