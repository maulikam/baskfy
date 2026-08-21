import type { FactorOut } from "@decile/api-client";

/**
 * Deterministic fixtures for the kitchen sink.
 *
 * Seeded rather than random: Prompt 8's second acceptance criterion measures a 4,000-row scroll,
 * and a measurement whose input changes on every run is not a measurement. The generator is the
 * same 32-bit LCG every time, so a regression in scroll performance is a regression in the code
 * rather than in the data.
 *
 * The shape mirrors `ScreenRunResponse.rows` (docs/07 §"Running a screen") so the kitchen sink's
 * table and the real results table take the same columns.
 */
export interface DemoRow {
  rank: number;
  symbol: string;
  name: string;
  sortingFactor: number;
  closeRaw: number;
  series: "EQ" | "BE";
  marketcapCr: number;
  ret12m: number;
  sharpe12m: number;
  vol12m: number;
  beta12m: number;
  ma200: number;
}

const MODULUS = 2 ** 31;
const MULTIPLIER = 1_103_515_245;
const INCREMENT = 12_345;

function lcg(seed: number): () => number {
  let state = seed % MODULUS;
  return () => {
    state = (MULTIPLIER * state + INCREMENT) % MODULUS;
    return state / MODULUS;
  };
}

const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";

function symbolFor(index: number, random: () => number): string {
  const length = 4 + Math.floor(random() * 3);
  let out = "";
  for (let position = 0; position < length; position += 1) {
    out += ALPHABET[Math.floor(random() * ALPHABET.length)] ?? "X";
  }
  return `${out}${index % 10}`;
}

function round(value: number, decimals: number): number {
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}

export function makeRows(count: number, seed = 20260818): DemoRow[] {
  const random = lcg(seed);
  return Array.from({ length: count }, (_, index) => {
    const close = round(20 + random() * 4000, 2);
    return {
      rank: index + 1,
      symbol: symbolFor(index, random),
      name: `${symbolFor(index, random)} Industries Limited`,
      sortingFactor: round(12 - (index / count) * 14, 2),
      closeRaw: close,
      series: random() > 0.98 ? "BE" : "EQ",
      marketcapCr: Math.round(500 + random() * 900_000),
      ret12m: round(-60 + random() * 300, 2),
      sharpe12m: round(-2 + random() * 16, 2),
      // docs/13 §2 finding 4: volatility is stored as a decimal fraction, 0.179-0.618 observed.
      vol12m: round(0.15 + random() * 0.5, 10),
      beta12m: round(0.2 + random() * 1.6, 10),
      ma200: round(close * (0.6 + random() * 0.8), 2),
    };
  });
}

/** A handful of registry entries, in the families docs/08 §"Screen editor" groups by. */
export const DEMO_FACTORS: FactorOut[] = [
  { key: "ret_12m", label: "ABSOLUTE RETURN 1 YEAR", family: "absolute_return", unit: "percent", higher_is_better: true },
  { key: "ret_3m", label: "ABSOLUTE RETURN 3 MONTHS", family: "absolute_return", unit: "percent", higher_is_better: true },
  { key: "sharpe_12m", label: "SHARPE RETURN 1 YEAR", family: "sharpe_return", unit: "ratio", higher_is_better: true },
  { key: "avg_sharpe_12_6_3_1", label: "AVERAGE SHARPE RETURN 12 6 3 1 MONTHS", family: "sharpe_return", unit: "ratio", higher_is_better: true },
  { key: "rsi_12m", label: "RSI 1 YEAR", family: "rsi", unit: "index", higher_is_better: true },
  { key: "sharpe_div_beta_12m", label: "SHARPE RETURN 1 YEAR / BETA", family: "risk_adjusted", unit: "ratio", higher_is_better: true },
  { key: "ret_12m_minus_1m", label: "RETURN 12 MINUS 1 MONTHS", family: "skip_month", unit: "percent", higher_is_better: true },
  { key: "vol_12m", label: "VOLATILITY 1 YEAR", family: "non_momentum", unit: "fraction", higher_is_better: false },
  { key: "beta_12m", label: "BETA", family: "non_momentum", unit: "ratio", higher_is_better: false },
];

/** A 30-point series for the sparklines, again seeded. */
export function makeSeries(points: number, seed: number): number[] {
  const random = lcg(seed);
  let value = 100;
  return Array.from({ length: points }, () => {
    value += (random() - 0.45) * 6;
    return round(value, 2);
  });
}
