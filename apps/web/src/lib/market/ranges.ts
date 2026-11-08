/**
 * The history-range presets docs/08 §"Market Health" asks for.
 *
 * In `lib/`, not beside the chart, because both sides need them: the client control writes the
 * key into the URL and the *server* component turns it into the `from` date it fetches. A module
 * marked `"use client"` cannot be called from a server component at all, which is exactly the
 * error that put these here.
 */
export const RANGE_PRESETS = [
  { key: "1m", label: "1M", days: 30 },
  { key: "3m", label: "3M", days: 91 },
  { key: "6m", label: "6M", days: 182 },
  { key: "1y", label: "1Y", days: 365 },
  { key: "5y", label: "5Y", days: 5 * 365 },
] as const;

export type RangeKey = (typeof RANGE_PRESETS)[number]["key"];

export const DEFAULT_RANGE: RangeKey = "1y";

export const RANGE_KEYS = RANGE_PRESETS.map((preset) => preset.key);

/** Unknown keys fall back to a year rather than throwing: the value comes from a URL. */
export function rangeDays(key: string): number {
  return RANGE_PRESETS.find((preset) => preset.key === key)?.days ?? 365;
}
