/**
 * Where these holdings came from — the money-safety fix, made visible.
 *
 * Before leaf C1, `POST /brokers/{id}/sync-holdings` labelled **every** non-empty response
 * "fixture holdings", a real Kite fetch included. Somebody looking at their own shares was told
 * the numbers were fake. The endpoint now carries two independent fields:
 *
 * - `source` — `live` | `fixture` | `empty` | `unwired`. Only `live` is the caller's real money.
 * - `degraded` — a live fetch was expected to work and did not, so `source` is a fallback rather
 *   than a deliberate stub.
 *
 * The rule this module encodes, and which its tests assert in both directions: **fixture and
 * degraded data must be visibly marked; clean live data must not be.** Marking live data is the
 * old bug with the sign flipped, and it is just as wrong — a user who is told their real holdings
 * are a sample stops believing the marking anywhere.
 */

import type { Schemas } from "@baskfy/api-client";

export type SyncHoldingsOut = Schemas["SyncHoldingsOut"];
export type HoldingsSource = SyncHoldingsOut["source"];

export type ProvenanceTone = "live" | "sample" | "empty" | "unavailable";

export interface ProvenanceView {
  source: HoldingsSource;
  degraded: boolean;
  dryRun: boolean;
  brokerId: string;
  brokerName: string;
  rowCount: number;
  /** Badge text. Short enough to sit beside a heading. */
  label: string;
  tone: ProvenanceTone;
  /**
   * These numbers must be visibly flagged as not the reader's real holdings. False only for a
   * clean live fetch and for an honest, undegraded empty answer.
   */
  marked: boolean;
  /**
   * The short warning shown beside the badge when {@link marked}. `null` when nothing needs
   * marking. It is a field rather than one fixed string because "not your holdings" is true of a
   * fixture and false of a degraded live read — marking both with the same words would trade one
   * inaccuracy for another.
   */
  mark: string | null;
  /** The sentence under the badge. Always names what `source` says, in prose. */
  detail: string;
}

function titleCase(brokerId: string): string {
  return brokerId
    .split(/[-_]/)
    .map((part) => (part.length === 0 ? part : `${part[0]?.toUpperCase() ?? ""}${part.slice(1)}`))
    .join(" ");
}

export function describeProvenance(
  result: SyncHoldingsOut,
  brokerName?: string | null,
): ProvenanceView {
  const name = brokerName ?? titleCase(result.broker_id);
  const rowCount = result.holdings.length;
  const base = {
    source: result.source,
    degraded: result.degraded,
    dryRun: result.dry_run,
    brokerId: result.broker_id,
    brokerName: name,
    rowCount,
  };

  if (result.source === "live") {
    if (result.degraded) {
      return {
        ...base,
        label: "Live, degraded",
        tone: "sample",
        marked: true,
        mark: "Incomplete",
        detail: `${name} answered, but the fetch was degraded — treat these ${rowCount} row${rowCount === 1 ? "" : "s"} as incomplete rather than as your full position.`,
      };
    }
    return {
      ...base,
      label: `Live from ${name}`,
      tone: "live",
      marked: false,
      mark: null,
      detail: `${name} reported these ${rowCount} row${rowCount === 1 ? "" : "s"} itself. They are your own holdings, read from the broker.`,
    };
  }

  if (result.source === "fixture") {
    return {
      ...base,
      label: "Sample data",
      tone: "sample",
      marked: true,
      mark: "Not your holdings",
      detail: result.degraded
        ? `A live read from ${name} was expected to work and did not, so these ${rowCount} row${rowCount === 1 ? "" : "s"} are stand-in sample data. They are not your holdings.`
        : `These ${rowCount} row${rowCount === 1 ? "" : "s"} are sample data, not your holdings. Nothing here was read from ${name}.`,
    };
  }

  if (result.source === "empty") {
    if (result.degraded) {
      return {
        ...base,
        label: "Nothing returned",
        tone: "unavailable",
        marked: true,
        mark: "Read failed",
        detail: `${name} returned nothing and the read was degraded, so this blank does not mean you hold nothing there.`,
      };
    }
    return {
      ...base,
      label: "Nothing held",
      tone: "empty",
      marked: false,
      mark: null,
      detail: `${name} reported no holdings.`,
    };
  }

  return {
    ...base,
    label: "No adapter yet",
    tone: "unavailable",
    marked: true,
    mark: "Nothing can be read",
    detail: `Baskfy has no holdings adapter for ${name} yet, so nothing can be read from it. Whatever you hold there is missing from every figure on this page unless you filed it by hand.`,
  };
}
