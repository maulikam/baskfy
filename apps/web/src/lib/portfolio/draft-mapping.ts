/**
 * The translation between §6.7's flow and `POST /portfolio` — pure, no I/O.
 *
 * The flow (`new-portfolio-flow.tsx`) hands back a {@link PortfolioDraft}, which is the shape the
 * *screen* works in: a start the user picked from five tiles, a benchmark they chose by name. The
 * API takes the shape the *ledger* works in: §3's `source`, and a numeric `benchmark_index_id`.
 * Those are not the same shape and neither is wrong.
 *
 * Split out of `create.ts` so it can be tested without a session, a network or a database. The
 * mapping is where a silent error would live — a wrong source produces a portfolio that describes
 * itself as something the user did not choose — so it is the part that most needs cheap tests.
 *
 * TWO TRANSLATIONS, AND NEITHER IS THE IDENTITY
 * ---------------------------------------------
 * **Start → source.** Five starts collapse to four sources: "From broker holdings" and "Empty"
 * are both a `HOLDING_GROUP`, because §3's source describes *where the portfolio's rule comes
 * from*, and neither of those has a rule — the user assembled it. §5.2 then gives both the most
 * conservative metric ("since grouped"), which is the honest answer for a grouping whose
 * purchase history we do not have.
 *
 * **Benchmark name → index id.** The flow offers "Nifty 500"; the column is a foreign key to
 * `index_def`. The ids are database-assigned, so they are resolved from `GET /meta/universes`
 * rather than carried as a table of integers in the web app — a hard-coded id is correct until
 * somebody reseeds, and then it is silently pointing at a different index.
 */

import type { Schemas } from "@baskfy/api-client";

import { holdingKeyId, type PortfolioDraft } from "@/lib/portfolio/organize";

export type NewPortfolioIn = Schemas["NewPortfolioIn"];
type PortfolioSource = NewPortfolioIn["source"];

/**
 * §6.7's five starting points onto §3's four sources.
 *
 * Declared as a total record rather than a `switch` with a default: a `default` branch would
 * quietly absorb a sixth start if one were ever added, and the absorbed value would be
 * `HOLDING_GROUP` — which decides the headline metric (§5.2) and the badge. TypeScript refuses to
 * compile this table if a start is missing, so the omission is caught where it is cheap.
 */
/**
 * Every start that CREATES a portfolio. `EXISTING` is excluded because it does not create one —
 * it files holdings into a portfolio that already has a source, a kind and a name, and it reaches
 * a different route entirely. Excluding it here is what keeps the table below total and honest:
 * an entry for `EXISTING` would have to invent a source that is never read.
 */
type CreatingStart = Exclude<PortfolioDraft["start"], "EXISTING">;

const SOURCE_FOR_START: Readonly<Record<CreatingStart, PortfolioSource>> = {
  SUBSCRIBED: "SUBSCRIBED",
  MY_SCREEN: "MY_SCREEN",
  MY_STRATEGY: "MY_STRATEGY",
  // Both of these are a grouping the user assembled, not a rule they follow.
  HOLDINGS: "HOLDING_GROUP",
  EMPTY: "HOLDING_GROUP",
};

/** §3's source for a draft. Total by construction — see {@link SOURCE_FOR_START}. */
export function sourceForStart(start: PortfolioDraft["start"]): PortfolioSource {
  if (start === "EXISTING") {
    // Reached only by a caller that routed an "add to an existing portfolio" draft through the
    // create body. Throwing beats returning a plausible source: the latter would create a second
    // portfolio silently, beside the one the user meant to add to.
    throw new Error("EXISTING files into a portfolio that already has a source; it creates none");
  }
  return SOURCE_FOR_START[start];
}

/** One row of `GET /meta/universes`: enough to turn a benchmark's name into its id. */
export interface BenchmarkOption {
  index_id: number;
  slug: string;
  name: string;
}

/**
 * The `index_def` id for a benchmark the user picked by name, or `null`.
 *
 * Matching is case- and space-insensitive because the flow shows "Nifty 500" and the seed stores
 * "NIFTY 500"; requiring them to agree exactly would make the benchmark silently unset for every
 * user, which is the failure this function exists to prevent.
 *
 * `null` when nothing matches, and that is a real answer rather than a fallback: §6.3 says a
 * portfolio with no benchmark of its own falls back to the product default. Guessing an id would
 * point the chart at whichever index happened to be first.
 */
export function benchmarkIndexId(
  benchmark: string,
  options: readonly BenchmarkOption[],
): number | null {
  const wanted = normalise(benchmark);
  if (!wanted) return null;
  const hit = options.find(
    (option) => normalise(option.name) === wanted || normalise(option.slug) === wanted,
  );
  return hit ? hit.index_id : null;
}

function normalise(value: string): string {
  return value.trim().toLowerCase().replaceAll(/[\s_-]+/gu, "");
}

/** A draft, translated into the body `POST /portfolio` accepts. */
export function bodyForDraft(
  draft: PortfolioDraft,
  benchmarks: readonly BenchmarkOption[],
): NewPortfolioIn {
  return {
    name: draft.name.trim(),
    kind: draft.kind,
    source: sourceForStart(draft.start),
    benchmark_index_id: benchmarkIndexId(draft.benchmark, benchmarks),
    // 0035: a quantity per holding, omitted when the user did not narrow it. `undefined` rather
    // than `null` so the key is absent from the JSON entirely — the API reads a missing quantity
    // as "the whole free remainder", and a literal null would mean the same thing while looking
    // like a value somebody chose.
    holdings: holdingsForDraft(draft),
  };
}


/**
 * The holding entries a draft sends, shared by the create route and the add-to-existing one.
 *
 * 0035: a quantity per holding, omitted when the user did not narrow it. `undefined` rather than
 * `null` so the key is absent from the JSON entirely — the API reads a missing quantity as
 * "everything available", and a literal null would mean the same thing while looking like a value
 * somebody chose.
 */
export function holdingsForDraft(
  draft: PortfolioDraft,
): Array<{ instrument_id: number; broker_account_id: number; quantity?: string }> {
  return draft.keys.map((key) => {
    const typed = draft.quantities?.get(holdingKeyId(key))?.trim();
    return {
      instrument_id: key.instrument_id,
      broker_account_id: key.broker_account_id,
      ...(typed === undefined || typed === "" ? {} : { quantity: typed }),
    };
  });
}
