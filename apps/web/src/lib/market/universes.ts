/**
 * The twelve universes docs/01 §6's market-health selector lists.
 *
 *     "universe selector (`nifty-allcap`, `nifty-50`, `nifty-next-50`, `nifty-100`, `nifty-200`,
 *      `nifty-500`, `nifty-total-market`, `nifty-large-mid-250`, `nifty-midcap-150`,
 *      `nifty-smallcap-250`, `nifty-microcap-250`, `nifty-mid-small-400`)"
 *
 * The 14 selectable universes minus `nifty-fno` and `etf`, which are classifications rather than
 * size bands and carry no breadth row.
 *
 * Duplicated from `decile_core.universes` for the same reason
 * `apps/web/src/lib/screens/operands.ts` is: no `/meta/` endpoint publishes the market-health
 * subset, and `/meta/universes` publishes all fourteen without saying which have breadth.
 * `packages/core/tests/test_market_health_universe_parity.py` keeps the two in step.
 */
export interface UniverseOption {
  slug: string;
  name: string;
}

/** docs/01 §6's own order, which puts `nifty-allcap` first. */
export const HEALTH_UNIVERSES: readonly UniverseOption[] = [
  { slug: "nifty-allcap", name: "All NSE Listed Stocks" },
  { slug: "nifty-50", name: "NIFTY 50" },
  { slug: "nifty-next-50", name: "NIFTY NEXT 50" },
  { slug: "nifty-100", name: "NIFTY 100" },
  { slug: "nifty-200", name: "NIFTY 200" },
  { slug: "nifty-500", name: "NIFTY 500" },
  { slug: "nifty-total-market", name: "NIFTY TOTAL MARKET" },
  { slug: "nifty-large-mid-250", name: "NIFTY LARGE MID 250" },
  { slug: "nifty-midcap-150", name: "NIFTY MIDCAP 150" },
  { slug: "nifty-smallcap-250", name: "NIFTY SMALLCAP 250" },
  { slug: "nifty-microcap-250", name: "NIFTY MICROCAP 250" },
  { slug: "nifty-mid-small-400", name: "NIFTY MID SMALL 400" },
] as const;

export const DEFAULT_HEALTH_UNIVERSE = "nifty-500";
