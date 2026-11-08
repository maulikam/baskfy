"""Generate a SYNTHETIC scan CSV for smoke-testing the scoring pipeline.

This is NOT market data. Numbers are produced by a seeded RNG so runs are
reproducible; symbol names are real NSE tickers only so the output is readable.
Replace data/uploads/sample_scan.csv with a real weekly scan export before
drawing any conclusion about actual stocks.

Usage:  python -m tests.make_sample_scan
"""
from __future__ import annotations

import csv
import os
import random

OUT = "data/uploads/sample_scan.csv"
SCAN_DATE = "2026-08-14"

HEADER = [
    "symbol", "name", "series", "date", "close", "marketcap", "median_volume_one_year",
    "absolute_return_one_month", "absolute_return_three_months", "absolute_return_six_months",
    "absolute_return_nine_months", "absolute_return_one_year",
    "sharpe_return_one_month", "sharpe_return_three_months", "sharpe_return_six_months",
    "sharpe_return_nine_months", "sharpe_return_one_year",
    "rsi_one_month", "volatility_one_year", "beta",
    "circuits_three_months", "circuits_one_year",
    "positive_days_percent_three_months", "positive_days_percent_six_months",
    "ma_20", "ma_50", "ma_100", "ma_200", "away_from_high_one_year", "is_nifty_fno",
]

# (symbol, name, strength 0..1) — strength drives the whole synthetic profile
UNIVERSE = [
    ("BSE", "BSE Ltd", 0.97), ("DIXON", "Dixon Technologies", 0.95),
    ("KAYNES", "Kaynes Technology", 0.93), ("PGEL", "PG Electroplast", 0.92),
    ("SUZLON", "Suzlon Energy", 0.90), ("RRKABEL", "R R Kabel", 0.88),
    ("POLYCAB", "Polycab India", 0.87), ("AMBER", "Amber Enterprises", 0.86),
    ("HAL", "Hindustan Aeronautics", 0.85), ("BDL", "Bharat Dynamics", 0.84),
    ("DIVISLAB", "Divi's Laboratories", 0.83), ("LAURUSLABS", "Laurus Labs", 0.82),
    ("MAXHEALTH", "Max Healthcare", 0.81), ("PERSISTENT", "Persistent Systems", 0.80),
    ("COFORGE", "Coforge Ltd", 0.79), ("TRENT", "Trent Ltd", 0.78),
    ("CDSL", "Central Depository Services", 0.77), ("MCX", "Multi Commodity Exchange", 0.76),
    ("SOLARINDS", "Solar Industries", 0.75), ("APARINDS", "Apar Industries", 0.74),
    ("CUMMINSIND", "Cummins India", 0.72), ("SIEMENS", "Siemens Ltd", 0.71),
    ("ABB", "ABB India", 0.70), ("BHARTIARTL", "Bharti Airtel", 0.69),
    ("ICICIBANK", "ICICI Bank", 0.67), ("SBIN", "State Bank of India", 0.66),
    ("LT", "Larsen & Toubro", 0.65), ("SUNPHARMA", "Sun Pharmaceutical", 0.64),
    ("TITAN", "Titan Company", 0.62), ("MARUTI", "Maruti Suzuki", 0.61),
    ("BAJFINANCE", "Bajaj Finance", 0.60), ("HDFCBANK", "HDFC Bank", 0.58),
    ("INFY", "Infosys Ltd", 0.55), ("TCS", "Tata Consultancy Services", 0.53),
    ("RELIANCE", "Reliance Industries", 0.52), ("AXISBANK", "Axis Bank", 0.50),
    ("ULTRACEMCO", "UltraTech Cement", 0.49), ("GRASIM", "Grasim Industries", 0.47),
    ("JSWSTEEL", "JSW Steel", 0.45), ("TATAMOTORS", "Tata Motors", 0.43),
    ("HINDUNILVR", "Hindustan Unilever", 0.40), ("NESTLEIND", "Nestle India", 0.38),
    ("ASIANPAINT", "Asian Paints", 0.34), ("BRITANNIA", "Britannia Industries", 0.32),
    ("COALINDIA", "Coal India", 0.30), ("ONGC", "Oil & Natural Gas Corp", 0.28),
    ("TATASTEEL", "Tata Steel", 0.26), ("WIPRO", "Wipro Ltd", 0.24),
    ("BANDHANBNK", "Bandhan Bank", 0.20), ("IDEA", "Vodafone Idea", 0.12),
]


def profile(rng: random.Random, sym: str, name: str, k: float) -> dict:
    """Build one internally-consistent row from a strength factor k."""
    r1y = round(-25 + 190 * k ** 1.6 + rng.uniform(-8, 8), 2)
    r9m = round(r1y * rng.uniform(0.62, 0.88), 2)
    r6m = round(r1y * rng.uniform(0.40, 0.66), 2)
    r3m = round(r1y * rng.uniform(0.18, 0.40), 2)
    r1m = round(r1y * rng.uniform(0.04, 0.16), 2)

    vol = round(rng.uniform(0.22, 0.34) + (1 - k) * 0.18, 3)
    sh1y = round(r1y / 100 / max(vol, 0.05), 2)
    prof = {
        "sharpe_return_one_year": sh1y,
        "sharpe_return_nine_months": round(sh1y * rng.uniform(0.8, 1.2), 2),
        "sharpe_return_six_months": round(sh1y * rng.uniform(0.7, 1.3), 2),
        "sharpe_return_three_months": round(sh1y * rng.uniform(0.6, 1.4), 2),
        "sharpe_return_one_month": round(sh1y * rng.uniform(0.3, 1.8), 2),
    }

    close = round(rng.uniform(180, 4200), 1)
    # stronger name → tighter, better-stacked MA ribbon under price
    gap = 0.006 + (1 - k) * 0.02
    ma20 = round(close / (1 + gap * rng.uniform(0.5, 2.2)), 1)
    ma50 = round(ma20 / (1 + gap * rng.uniform(0.8, 2.5)), 1)
    ma100 = round(ma50 / (1 + gap * rng.uniform(0.8, 2.5)), 1)
    ma200 = round(ma100 / (1 + gap * rng.uniform(0.8, 2.5)), 1)
    if k < 0.35:  # laggards lose the ribbon
        ma20, ma50 = round(close * 1.03, 1), round(close * 1.08, 1)
        ma100, ma200 = round(close * 1.12, 1), round(close * 1.18, 1)

    mcap = round(rng.uniform(2_000, 90_000) * (0.4 + k), 0)
    return dict(
        symbol=sym, name=name, series="EQ", date=SCAN_DATE, close=close,
        marketcap=mcap,
        median_volume_one_year=round(rng.uniform(6e7, 9e9) * (0.3 + k), 0),
        absolute_return_one_month=r1m, absolute_return_three_months=r3m,
        absolute_return_six_months=r6m, absolute_return_nine_months=r9m,
        absolute_return_one_year=r1y,
        rsi_one_month=round(34 + 46 * k + rng.uniform(-6, 6), 1),
        volatility_one_year=vol,
        beta=round(rng.uniform(0.6, 1.7), 2),
        circuits_three_months=rng.choice([0, 0, 0, 0, 1, 1, 2]),
        circuits_one_year=rng.choice([0, 0, 1, 2, 3, 5, 7]),
        positive_days_percent_three_months=round(38 + 22 * k + rng.uniform(-4, 4), 1),
        positive_days_percent_six_months=round(40 + 20 * k + rng.uniform(-4, 4), 1),
        ma_20=ma20, ma_50=ma50, ma_100=ma100, ma_200=ma200,
        away_from_high_one_year=round(-1.5 - 26 * (1 - k) ** 1.7 - rng.uniform(0, 3), 1),
        is_nifty_fno=1 if k > 0.45 or mcap > 40_000 else 0,
        **prof,
    )


def edge_cases(rows: list[dict]) -> list[dict]:
    """Rows that must trip exactly one hard filter each (plus one untouchable)."""
    base = rows[10].copy()

    be = base.copy()
    be.update(symbol="MADHAV", name="Madhav Copper (T2T)", series="BE")

    illiquid = base.copy()
    illiquid.update(symbol="TINYCO", name="Micro Cap Ltd",
                    median_volume_one_year=1.8e7)

    circuits = base.copy()
    circuits.update(symbol="CIRCUITX", name="Circuit Prone Ltd",
                    circuits_three_months=9, circuits_one_year=14)

    far = base.copy()
    far.update(symbol="FALLENANG", name="Fallen Angel Ltd",
               away_from_high_one_year=-46.0)

    down = base.copy()
    down.update(symbol="DOWNTREND", name="Downtrend Ltd",
                close=100.0, ma_20=104.0, ma_50=112.0, ma_100=124.0, ma_200=140.0,
                absolute_return_three_months=-14.0, absolute_return_six_months=-22.0)

    sgb = base.copy()
    sgb.update(symbol="SGBDE31III", name="Sovereign Gold Bond 2031 III",
               series="GB", close=7412.0)

    return [be, illiquid, circuits, far, down, sgb]


def main() -> None:
    rng = random.Random(20260814)
    rows = [profile(rng, s, n, k) for s, n, k in UNIVERSE]
    rows += edge_cases(rows)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=HEADER)
        wr.writeheader()
        wr.writerows(rows)
    print(f"wrote {OUT}: {len(rows)} rows (SYNTHETIC — not market data)")


if __name__ == "__main__":
    main()
