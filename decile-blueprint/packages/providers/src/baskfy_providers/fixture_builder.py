"""Builds the Parquet fixtures FixtureProvider reads (Prompt 2 deliverable 5).

    "Include real-shaped fixtures for ~40 instruments over 3 years, including at least one
     instrument with a split and one with a bonus."

WHAT IS REAL HERE, AND WHAT IS NOT
----------------------------------
Real, taken from ``tests/fixtures/reference-screen-export-2026-08-18.csv``:
  * the 40 symbols, their company names, series and instrument types;
  * each one's closing price, marketcap, beta, 1-year volatility and median traded value on
    2026-08-18;
  * their index memberships across the 14 universes.

Real, taken from docs/01 §5 and §9:
  * CUPID's corporate actions — bonus 4:1 (ex 09-Mar-2026), and a 10:1 split plus a 1:1 bonus in
    April 2024. The exact April day is not recorded in the bundle; 15-Apr-2024 is chosen.

**Synthetic**: every daily bar before 2026-08-18. They are a seeded random walk per symbol,
calibrated to that symbol's *real* annualised volatility and terminating on its *real* close, so
the series has a believable shape and scale. It is not market data and must never be presented as
such — it exists so tests and local development have something with the right dimensions,
distribution and corporate-action structure to work against, with zero network access.

The walk is seeded from the symbol, so the fixture is byte-identical on every machine and every
rebuild. Regenerate with ``python -m baskfy_providers.fixture_builder``.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final

import polars as pl

from baskfy_core.reference_export import ReferenceRows
from importlib import resources
from baskfy_core.trading_calendar import HOLIDAY_FILE, build_calendar, parse_seed_holidays
from baskfy_providers.reference_export_io import reference_rows
from baskfy_core.universes import UNIVERSES, slugify_index

#: docs/13: the reference export's trade date. The fixtures end here so they line up with it.
FIXTURE_AS_OF: Final = dt.date(2026, 8, 18)

#: "~40 instruments over 3 years".
FIXTURE_INSTRUMENT_COUNT: Final = 40
FIXTURE_YEARS: Final = 3

#: Root seed. Any change here regenerates every series, so it is pinned.
FIXTURE_SEED: Final = 20260818

FIXTURE_DIRNAME: Final = "providers"

#: docs/08 §Dashboard: "a sparkline per index (30-day)". The fixture carries exactly that much
#: snapshot history, so the dashboard has something to draw without carrying three years of it.
SNAPSHOT_HISTORY_DAYS: Final = 30

#: The dashboard-only indices, on top of the 14 selectable universes — docs/01 §7's "~145 index
#: rows ... Includes derived indices with no fundamentals (`Nifty50 PR 1x Inverse`, `India VIX`)".
#:
#: These are **real NSE index names**, from the families NSE publishes: broad market, sectoral,
#: thematic, strategy, derived/leveraged and fixed income. The *values* against them are synthetic
#: (see PROVENANCE), and the list has not been checked against a live NSE index file — it is what
#: a fixture needs, which is a realistic set of names at a realistic scale.
#:
#: It is deliberately shorter than 145. `baskfy_worker.tasks.snapshots` makes the point in
#: production terms — "Hardcoding a list of 145 index names would be inventing data" — and the
#: same applies here: the last few dozen names are ones we would be guessing at, and a fixture
#: full of guessed names is worse than a shorter one full of real ones. `docs/11a` §1.
DASHBOARD_INDEX_NAMES = (
    # --- Broad market (beyond the 14 selectable universes) ---
    "NIFTY MIDCAP 50",
    "NIFTY MIDCAP 100",
    "NIFTY SMALLCAP 50",
    "NIFTY SMALLCAP 100",
    "NIFTY500 MULTICAP 50:25:25",
    "NIFTY TOP 10 EQUAL WEIGHT",
    # --- Sectoral ---
    "NIFTY BANK",
    "NIFTY AUTO",
    "NIFTY FINANCIAL SERVICES",
    "NIFTY FINANCIAL SERVICES 25/50",
    "NIFTY FINANCIAL SERVICES EX-BANK",
    "NIFTY FMCG",
    "NIFTY IT",
    "NIFTY MEDIA",
    "NIFTY METAL",
    "NIFTY PHARMA",
    "NIFTY PSU BANK",
    "NIFTY PRIVATE BANK",
    "NIFTY REALTY",
    "NIFTY HEALTHCARE INDEX",
    "NIFTY CONSUMER DURABLES",
    "NIFTY OIL & GAS",
    "NIFTY MIDSMALL HEALTHCARE",
    "NIFTY MIDSMALL FINANCIAL SERVICES",
    "NIFTY MIDSMALL IT & TELECOM",
    "NIFTY CAPITAL MARKETS",
    "NIFTY CHEMICALS",
    "NIFTY CORE HOUSING",
    # --- Thematic ---
    "NIFTY COMMODITIES",
    "NIFTY CONSUMPTION",
    "NIFTY CPSE",
    "NIFTY ENERGY",
    "NIFTY INFRASTRUCTURE",
    "NIFTY MNC",
    "NIFTY PSE",
    "NIFTY SERVICES SECTOR",
    "NIFTY INDIA DIGITAL",
    "NIFTY INDIA MANUFACTURING",
    "NIFTY INDIA CONSUMPTION",
    "NIFTY INDIA DEFENCE",
    "NIFTY INDIA TOURISM",
    "NIFTY INDIA RAILWAYS PSU",
    "NIFTY HOUSING",
    "NIFTY TRANSPORTATION & LOGISTICS",
    "NIFTY MOBILITY",
    "NIFTY EV & NEW AGE AUTOMOTIVE",
    "NIFTY NON-CYCLICAL CONSUMER",
    "NIFTY RURAL",
    "NIFTY100 LIQUID 15",
    "NIFTY MIDCAP LIQUID 15",
    "NIFTY SHARIAH 25",
    "NIFTY50 SHARIAH",
    "NIFTY500 SHARIAH",
    # --- Strategy / factor ---
    "NIFTY ALPHA 50",
    "NIFTY50 VALUE 20",
    "NIFTY50 EQUAL WEIGHT",
    "NIFTY100 EQUAL WEIGHT",
    "NIFTY100 QUALITY 30",
    "NIFTY100 LOW VOLATILITY 30",
    "NIFTY100 ALPHA 30",
    "NIFTY200 QUALITY 30",
    "NIFTY200 MOMENTUM 30",
    "NIFTY200 ALPHA 30",
    "NIFTY200 VALUE 30",
    "NIFTY500 MOMENTUM 50",
    "NIFTY500 VALUE 50",
    "NIFTY500 QUALITY 50",
    "NIFTY500 LOW VOLATILITY 50",
    "NIFTY500 EQUAL WEIGHT",
    "NIFTY MIDCAP150 QUALITY 50",
    "NIFTY MIDCAP150 MOMENTUM 50",
    "NIFTY SMALLCAP250 QUALITY 50",
    "NIFTY SMALLCAP250 MOMENTUM QUALITY 100",
    "NIFTY MIDSMALLCAP400 MOMENTUM QUALITY 100",
    "NIFTY ALPHA LOW-VOLATILITY 30",
    "NIFTY ALPHA QUALITY LOW-VOLATILITY 30",
    "NIFTY ALPHA QUALITY VALUE LOW-VOLATILITY 30",
    "NIFTY QUALITY LOW-VOLATILITY 30",
    "NIFTY DIVIDEND OPPORTUNITIES 50",
    "NIFTY GROWTH SECTORS 15",
    "NIFTY HIGH BETA 50",
    "NIFTY LOW VOLATILITY 50",
    # --- Derived, leveraged and volatility (docs/01 §7 names two of these) ---
    "NIFTY50 PR 1X INVERSE",
    "NIFTY50 PR 2X LEVERAGE",
    "NIFTY50 TR 1X INVERSE",
    "NIFTY50 TR 2X LEVERAGE",
    "NIFTY50 DIVIDEND POINTS",
    "INDIA VIX",
    # --- Fixed income ---
    "NIFTY 1D RATE INDEX",
    "NIFTY 5 YR BENCHMARK G-SEC",
    "NIFTY 10 YR BENCHMARK G-SEC",
    "NIFTY 4-8 YR G-SEC",
    "NIFTY 8-13 YR G-SEC",
    "NIFTY 11-15 YR G-SEC",
    "NIFTY 15 YR AND ABOVE G-SEC",
    "NIFTY COMPOSITE G-SEC",
    "NIFTY AAA BOND PLUS SDL APR 2026 50:50",
    "NIFTY BHARAT BOND INDEX - APRIL 2030",
    "NIFTY BHARAT BOND INDEX - APRIL 2031",
    "NIFTY BHARAT BOND INDEX - APRIL 2032",
    "NIFTY BHARAT BOND INDEX - APRIL 2033",
    "NIFTY SDL APR 2027 TOP 12 EQUAL WEIGHT",
    "NIFTY SDL PLUS G-SEC JUN 2028 70:30",
)
print(len(DASHBOARD_INDEX_NAMES), len(set(DASHBOARD_INDEX_NAMES)))


#: Two indices docs/01 §7 names as publishing no fundamentals. Kept as an explicit set so the
#: consumer is forced to handle NULL rather than assuming every row has a P/E.
NO_FUNDAMENTALS: Final[frozenset[str]] = frozenset(
    {
        "NIFTY50 PR 1X INVERSE",
        "NIFTY50 PR 2X LEVERAGE",
        "NIFTY50 TR 1X INVERSE",
        "NIFTY50 TR 2X LEVERAGE",
        "NIFTY50 DIVIDEND POINTS",
        "INDIA VIX",
        "NIFTY 1D RATE INDEX",
    }
)


#: docs/01 §5 and §9 — CUPID's real, documented corporate actions.
#: docs/09 §"Adjustment algorithm" ratio convention: split 10:1 -> from=10, to=1.
DOCUMENTED_ACTIONS: Final[tuple[dict[str, object], ...]] = (
    {
        "symbol": "CUPID",
        "action_type": "bonus",
        "ex_date": dt.date(2026, 3, 9),
        "ratio_from": Decimal(4),
        "ratio_to": Decimal(1),
        "amount": None,
        "purpose": "BONUS 4:1",
        "provenance": "docs/01 §9 — bonus 4:1 ex 09-Mar-2026",
    },
    {
        "symbol": "CUPID",
        "action_type": "split",
        "ex_date": dt.date(2024, 4, 15),
        "ratio_from": Decimal(10),
        "ratio_to": Decimal(1),
        "amount": None,
        "purpose": "FACE VALUE SPLIT FROM RS.10/- TO RE.1/-",
        "provenance": "docs/01 §9 — split 10:1 in Apr 2024; exact day chosen for the fixture",
    },
    {
        "symbol": "CUPID",
        "action_type": "bonus",
        "ex_date": dt.date(2024, 4, 15),
        "ratio_from": Decimal(1),
        "ratio_to": Decimal(1),
        "amount": None,
        "purpose": "BONUS 1:1",
        "provenance": "docs/01 §9 — bonus 1:1 in Apr 2024; exact day chosen for the fixture",
    },
)

#: A cash dividend, so the adjustment step (Prompt 3) has all four action shapes to exercise.
#: Entirely synthetic; the symbol is chosen at build time from the largest instrument available.
SYNTHETIC_DIVIDEND_EX_DATE: Final = dt.date(2025, 8, 6)
SYNTHETIC_DIVIDEND_AMOUNT: Final = Decimal("12.00")

_TRADING_DAYS_PER_YEAR: Final = 252


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    """One instrument's anchor: what the reference export says it really was on the as-of date."""

    symbol: str
    name: str
    series: str
    instrument_type: str
    close: Decimal
    marketcap_cr: int
    annual_volatility: Decimal
    median_turnover: int
    universes: tuple[str, ...]


def select_specs(rows: ReferenceRows, limit: int = FIXTURE_INSTRUMENT_COUNT) -> list[FixtureSpec]:
    """Pick the instruments to synthesise.

    CUPID is pinned because it carries the documented split and bonus. The rest are the largest
    by marketcap, which keeps the fixture spread across the large/mid/small universes rather than
    landing entirely in microcaps.
    """
    by_symbol = {str(f["symbol"]): f for f in rows.factors}
    memberships: dict[str, list[str]] = {}
    for membership in rows.memberships:
        memberships.setdefault(membership.symbol, []).append(membership.universe_slug)

    instruments = {i.symbol: i for i in rows.instruments}
    ranked = sorted(
        by_symbol,
        key=lambda s: (_as_int(by_symbol[s].get("marketcap_cr")), s),
        reverse=True,
    )
    chosen = ["CUPID", *[s for s in ranked if s != "CUPID"]][:limit]

    specs: list[FixtureSpec] = []
    for symbol in chosen:
        factor = by_symbol[symbol]
        instrument = instruments[symbol]
        volatility = factor.get("vol_12m")
        specs.append(
            FixtureSpec(
                symbol=symbol,
                name=instrument.name,
                series=instrument.series,
                instrument_type=instrument.instrument_type,
                close=_as_decimal(factor.get("close")) or Decimal("100.00"),
                marketcap_cr=_as_int(factor.get("marketcap_cr")),
                annual_volatility=_as_decimal(volatility) or Decimal("0.30"),
                median_turnover=_as_int(factor.get("median_vol_12m")),
                universes=tuple(sorted(memberships.get(symbol, ()))),
            )
        )
    return specs


def trading_days(end: dt.date, years: int = FIXTURE_YEARS) -> list[dt.date]:
    """The fixture's calendar, from ``baskfy_core`` so it matches the rest of the system.

    NOTE: that calendar is provisional (docs/04a) — it is missing India's lunar-calendar
    holidays, so the fixture has slightly more trading days per year than reality. That is
    acceptable for a fixture and is *not* acceptable for a published factor, which is why
    docs/04a makes reconciliation against real bars a Prompt 3 requirement.
    """
    start = end.replace(year=end.year - years)
    holidays = parse_seed_holidays(
        resources.files("baskfy_core.data").joinpath(HOLIDAY_FILE).read_text(encoding="utf-8")
    )
    return [row.date for row in build_calendar(start, end, holidays) if row.is_trading_day]


def synthesise_bars(spec: FixtureSpec, calendar: list[dt.date]) -> list[dict[str, object]]:
    """A seeded geometric random walk that lands exactly on the instrument's real close.

    The walk is generated forward from a derived starting price and then rescaled so the final
    close equals the real one; that keeps both the day-to-day distribution and the endpoint
    honest, where anchoring only one of them would not.
    """
    rng = random.Random(f"{FIXTURE_SEED}:{spec.symbol}")
    daily_sigma = float(spec.annual_volatility) / math.sqrt(_TRADING_DAYS_PER_YEAR)
    # A small positive drift: these are momentum-screen constituents, so a flat series would be
    # unrepresentative of what the factor engine will actually meet.
    daily_drift = daily_sigma * 0.04

    path: list[float] = [1.0]
    for _ in calendar[1:]:
        shock = rng.gauss(daily_drift, daily_sigma)
        path.append(max(path[-1] * math.exp(shock), 1e-6))

    final_close = float(spec.close)
    scale = final_close / path[-1]

    rows: list[dict[str, object]] = []
    for day, level in zip(calendar, path, strict=True):
        close = level * scale
        # Intraday range scaled off the same volatility, so the bar is internally consistent.
        spread = close * daily_sigma * rng.uniform(0.4, 1.6)
        open_price = close + rng.uniform(-spread, spread)
        high = max(open_price, close) + rng.uniform(0, spread)
        low = min(open_price, close) - rng.uniform(0, spread)
        turnover = spec.median_turnover or int(close * 100_000)
        shares = max(int(turnover * rng.uniform(0.5, 1.8) / max(close, 1e-6)), 1)
        rows.append(
            {
                "symbol": spec.symbol,
                "date": day,
                "open": _money(open_price),
                "high": _money(high),
                "low": _money(max(low, 0.01)),
                "close": _money(close),
                "volume": shares,
                "source": "nse",
            }
        )
    return rows


def build(output_dir: Path, rows: ReferenceRows | None = None) -> dict[str, int]:
    """Write every Parquet fixture. Returns row counts per file."""
    data = rows if rows is not None else reference_rows()
    specs = select_specs(data)
    calendar = trading_days(FIXTURE_AS_OF)
    output_dir.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}

    instruments = pl.DataFrame(
        [
            {
                "symbol": s.symbol,
                "name": s.name,
                "instrument_type": s.instrument_type,
                "series": s.series,
                "isin": None,
                "exchange": "NSE",
                # Deterministic surrogate tokens: Kite's real tokens are not in the bundle, and
                # inventing plausible-looking real ones would be worse than an obvious range.
                "kite_token": 900_000 + index,
                "lot_size": 1,
                "listed_on": calendar[0],
            }
            for index, s in enumerate(specs)
        ],
        strict=False,
    )
    counts["instruments"] = _write(instruments, output_dir / "instruments.parquet")

    bar_rows: list[dict[str, object]] = []
    for spec in specs:
        bar_rows.extend(synthesise_bars(spec, calendar))
    bars = pl.DataFrame(bar_rows, strict=False).sort(["symbol", "date"])
    counts["daily_bars"] = _write(bars, output_dir / "daily_bars.parquet")

    actions = pl.DataFrame([*DOCUMENTED_ACTIONS, _synthetic_dividend(specs)], strict=False).sort(
        ["symbol", "ex_date"]
    )
    counts["corporate_actions"] = _write(actions, output_dir / "corporate_actions.parquet")

    members = pl.DataFrame(
        [
            {"index_slug": slug, "date": FIXTURE_AS_OF, "symbol": spec.symbol}
            for spec in specs
            for slug in spec.universes
        ],
        strict=False,
    ).sort(["index_slug", "symbol"])
    counts["index_members"] = _write(members, output_dir / "index_members.parquet")

    snapshots = _index_snapshots(calendar)
    counts["index_snapshots"] = _write(snapshots, output_dir / "index_snapshots.parquet")

    listings = pl.DataFrame(
        [
            {
                "symbol": spec.symbol,
                "name": spec.name,
                "series": spec.series,
                "isin": None,
                "listed_on": calendar[0],
                "face_value": _money(1.0),
                "paid_up_value": _money(1.0),
                "market_lot": 1,
            }
            for spec in specs
        ],
        strict=False,
    )
    counts["listings"] = _write(listings, output_dir / "listings.parquet")

    last_bars = bars.filter(pl.col("date") == FIXTURE_AS_OF)
    bhavcopy = _bhavcopy_from_bars(last_bars, bars, specs)
    counts["bhavcopy"] = _write(bhavcopy, output_dir / "bhavcopy.parquet")

    (output_dir / "PROVENANCE.md").write_text(_provenance(specs, calendar), encoding="utf-8")
    return counts


def _index_snapshots(calendar: list[dt.date]) -> pl.DataFrame:
    """One row per index per day for the last ``SNAPSHOT_HISTORY_DAYS`` trading days.

    Every index walks its own seeded series so the 30-day sparklines differ from each other and
    the % change on the as-of date is the walk's own last step rather than a number invented
    separately from the level — a dashboard sorted by a % change that does not match its own
    sparkline is a fixture that teaches the reader something false.
    """
    window = calendar[-SNAPSHOT_HISTORY_DAYS:]
    rows: list[dict[str, object]] = []

    entries = [(u.slug, u.name) for u in UNIVERSES] + [
        (slugify_index(name), name) for name in DASHBOARD_INDEX_NAMES
    ]
    for position, (slug, name) in enumerate(entries):
        rng = random.Random(f"{FIXTURE_SEED}:index:{slug}")
        level = 1000.0 + position * 137.5
        has_fundamentals = name not in NO_FUNDAMENTALS and slug != "etf"
        previous = level
        for day in window:
            step = rng.gauss(0.0004, 0.009)
            previous = level
            level = max(level * (1.0 + step), 1.0)
            rows.append(
                {
                    "index_slug": slug,
                    "date": day,
                    "level": _money(level),
                    "change_abs": _money(level - previous),
                    "change_pct": _money((level / previous - 1.0) * 100.0),
                    "pe": _money(20.0 + position * 0.1) if has_fundamentals else None,
                    "pb": _money(3.0 + position * 0.02) if has_fundamentals else None,
                    "div_yield": _money(1.2) if has_fundamentals else None,
                }
            )
    return pl.DataFrame(rows, strict=False).sort(["date", "index_slug"])


def _bhavcopy_from_bars(
    last: pl.DataFrame, bars: pl.DataFrame, specs: list[FixtureSpec]
) -> pl.DataFrame:
    """A bhavcopy for the as-of date, with the series and circuit bands docs/09 requires."""
    series_by_symbol = {s.symbol: s.series for s in specs}
    previous = (
        bars.filter(pl.col("date") < FIXTURE_AS_OF)
        .sort("date")
        .group_by("symbol")
        .last()
        .select(["symbol", pl.col("close").alias("prev_close")])
    )
    joined = last.join(previous, on="symbol", how="left")

    rows: list[dict[str, object]] = []
    for row in joined.iter_rows(named=True):
        close = Decimal(str(row["close"]))
        prev = row["prev_close"]
        rows.append(
            {
                "symbol": row["symbol"],
                "series": series_by_symbol.get(str(row["symbol"])),
                "date": FIXTURE_AS_OF,
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "prev_close": prev if prev is not None else row["open"],
                "volume": row["volume"],
                "turnover": _quantise(close * Decimal(int(row["volume"])), "0.01"),
                "trades": max(int(row["volume"]) // 250, 1),
                # NSE's standard 20% band, which is what the circuits factor counts against.
                "upper_circuit": _quantise(close * Decimal("1.20"), "0.0001"),
                "lower_circuit": _quantise(close * Decimal("0.80"), "0.0001"),
            }
        )
    return pl.DataFrame(rows, strict=False)


def _synthetic_dividend(specs: list[FixtureSpec]) -> dict[str, object]:
    target = max(specs, key=lambda s: s.marketcap_cr)
    return {
        "symbol": target.symbol,
        "action_type": "dividend",
        "ex_date": SYNTHETIC_DIVIDEND_EX_DATE,
        "ratio_from": None,
        "ratio_to": None,
        "amount": SYNTHETIC_DIVIDEND_AMOUNT,
        "purpose": f"DIVIDEND - RS.{SYNTHETIC_DIVIDEND_AMOUNT} PER SHARE",
        "provenance": "SYNTHETIC — no dividend calendar is in the bundle",
    }


def _write(frame: pl.DataFrame, path: Path) -> int:
    frame.write_parquet(path)
    return frame.height


def _money(value: float) -> Decimal:
    return _quantise(Decimal(str(value)), "0.0001")


def _quantise(value: Decimal, exponent: str) -> Decimal:
    return value.quantize(Decimal(exponent), rounding=ROUND_HALF_UP)


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        return int(value)
    return 0


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        return Decimal(str(value))
    return None


def _provenance(specs: list[FixtureSpec], calendar: list[dt.date]) -> str:
    return f"""# Fixture provenance

Generated by `python -m baskfy_providers.fixture_builder`. Deterministic: seed {FIXTURE_SEED}.

## Real

- {len(specs)} symbols, names, series and instrument types — from
  `tests/fixtures/reference-screen-export-2026-08-18.csv` (docs/13).
- Each symbol's close, marketcap, 1-year volatility and median traded value on
  {FIXTURE_AS_OF.isoformat()} — same source.
- Index memberships across the 14 universes — same source.
- CUPID's corporate actions — docs/01 §5 and §9 (bonus 4:1 ex 09-Mar-2026; 10:1 split and 1:1
  bonus in Apr 2024; the exact April day is not in the bundle and was chosen).

## Synthetic

- **Every daily bar before {FIXTURE_AS_OF.isoformat()}.** A seeded geometric random walk per
  symbol, calibrated to that symbol's real annualised volatility and rescaled to terminate on its
  real close. Believable shape and scale; not market data. Never present it as such.
- Kite instrument tokens (a 900000+ surrogate range — the real ones are not in the bundle).
- Index snapshot levels, % changes, PE, PB and dividend yield — a seeded walk per index over the
  last {SNAPSHOT_HISTORY_DAYS} trading days. The index *names* are real NSE index names; the
  numbers against them are not, and the list is {len(DASHBOARD_INDEX_NAMES)} dashboard-only
  indices plus the 14 universes rather than docs/01 §7's ~145 — the remainder would be guessed.
- One cash dividend, so the adjustment step has all four action shapes to exercise.
- Circuit bands (a flat 20% NSE band around the close).

## Shape

- {len(calendar)} trading days, {calendar[0].isoformat()} to {calendar[-1].isoformat()}.
- Built on `baskfy_core.trading_calendar`, which docs/04a records as **provisional** — it is
  missing India's lunar-calendar holidays, so this fixture has slightly more trading days per
  year than reality.
"""


def default_output_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "tests" / "fixtures"
        if candidate.is_dir():
            return candidate / FIXTURE_DIRNAME
    raise FileNotFoundError("no tests/fixtures directory found above this file")


def main() -> int:
    output = default_output_dir()
    counts = build(output)
    for name, count in counts.items():
        print(f"{name}: {count} rows")
    print(f"written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
