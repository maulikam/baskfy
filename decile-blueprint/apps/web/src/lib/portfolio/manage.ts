import type { paths, Schemas } from "@baskfy/api-client";

import { VIEWS_NOTICE, metric, type Metric } from "@/lib/portfolio/command-center";
import {
  MONITORING_NOTE,
  formatShares,
  freeQuantity,
  holdingKeyId,
  shareOfValue,
  type AggregatedHolding,
  type HoldingBrokerLine,
  type PortfolioKind,
} from "@/lib/portfolio/organize";
import type { PortfolioRow } from "@/lib/portfolio/overview";
import { addDecimalStrings, compareDecimalStrings } from "@/lib/portfolios/decimal";

/**
 * PC6 — everything the **Manage portfolios** drawer decides, with nothing that touches the wire.
 *
 * `docs/PORTFOLIO-COMMAND-CENTER.md` §6.1 gives this leaf `lib/portfolio/manage.ts` and
 * `components/portfolio/manage/*`. The split inside that budget is PC1's: this module is pure —
 * same payload in, same preview out — and the components render it. The *writes* are handlers the
 * hosting page passes down, exactly as `new-portfolio-flow` already takes `onCreate`, because the
 * bearer token lives in the server session and `create.ts` is `server-only`. A client component
 * that imported a fetch from here would be a client component holding a token.
 *
 * THE MODEL THIS DRAWER MUST NOT BREAK
 * ------------------------------------
 * §4.1, and it is arithmetic rather than taste. A **capital portfolio** owns its holdings
 * exclusively: a share is in exactly one of them, and capital portfolios plus Unallocated add up
 * to net worth to the paisa. A **monitoring view** owns nothing — it is a lens, it may overlap
 * anything, and it enters no total. Every operation here is classified as one of three intents
 * ({@link AssignmentIntent}) precisely so that the difference cannot be lost in a form:
 *
 *   · `ASSIGN` — Unallocated → a capital portfolio. Ownership changes. Exclusivity applies.
 *   · `MOVE`   — one capital portfolio → another. Ownership changes hands; **both sides** are
 *                previewed, because a move that only shows the gain is a form that hides half of
 *                what it is about to do.
 *   · `WATCH`  — names added to a monitoring view. Ownership does **not** change, and the
 *                preview says so in words rather than leaving the reader to infer it.
 *
 * WHAT THIS LEAF FOUND THAT §6.3 DID NOT KNOW
 * -------------------------------------------
 * §6.3 said "create, update, delete, add-holdings and replace-holdings endpoints all exist", and
 * that is true as far as it goes. Read against the generated schema, "update" is narrower than it
 * sounds: `PortfolioPatchIn` carries `name`, `parent_id` and `broker_account_id` and **nothing
 * else**. So:
 *
 *   · **Objective has no field anywhere.** Not on the patch body, not on `NewPortfolioIn`, not on
 *     the `portfolio` table. It is not a control that is hard to reach; it is a column that does
 *     not exist. It is listed as unavailable with what would unblock it.
 *   · **Benchmark is settable once, at creation** (`NewPortfolioIn.benchmark_index_id`) and
 *     cannot be changed afterwards. So it is a live control on the create form and an explained
 *     absence on an existing portfolio — {@link ManageAvailability}'s `create-only` case exists
 *     for exactly this, rather than drawing a select that silently does nothing.
 *
 * Both are recorded in `docs/pc-findings/pc6.md` for the parent to fold into §2.2.
 *
 * HOW "THE ENDPOINT EXISTS" IS ENFORCED
 * -------------------------------------
 * {@link ApiPath} is `keyof paths` from the generated OpenAPI document. An action that names a
 * route the API does not serve is a **compile** error, not a test that someone remembered to
 * write. That is the difference between a claim and a guarantee, and it is why the endpoint is a
 * typed pair rather than a string in a comment.
 */

/** Every path the API actually serves, straight from its OpenAPI document. */
export type ApiPath = keyof paths;

export interface Endpoint {
  readonly method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  /** Typed against the generated document: a route that does not exist will not compile. */
  readonly path: ApiPath;
}

export type ManageActionId =
  | "create"
  | "rename"
  | "objective"
  | "benchmark"
  | "assign"
  | "move"
  | "watch"
  | "sleeve"
  | "broker"
  | "reconciliation"
  | "delete"
  | "archive"
  | "permissions"
  | "ownership"
  | "audit";

/** The drawer's sections, in the order a person works down them. */
export type ManageGroup = "identity" | "holdings" | "structure" | "connections" | "danger";

export const GROUP_LABEL: Record<ManageGroup, string> = {
  identity: "Portfolio",
  holdings: "Holdings",
  structure: "Structure",
  connections: "Connections",
  danger: "Remove",
};

/**
 * Whether the drawer may draw a control for this action, and — when it may not — why.
 *
 * `create-only` is not a hedge. A benchmark genuinely is writable, once, on the create form, and
 * genuinely is not writable afterwards; collapsing that into "available" would draw a dead select
 * on an existing portfolio and collapsing it into "unavailable" would remove a control that works.
 */
export type ManageAvailability =
  | { readonly kind: "available"; readonly endpoint: Endpoint }
  | {
      readonly kind: "create-only";
      readonly endpoint: Endpoint;
      readonly reason: string;
      readonly unblockedBy: string;
    }
  | { readonly kind: "unavailable"; readonly reason: string; readonly unblockedBy: string };

export interface ManageAction {
  readonly id: ManageActionId;
  readonly title: string;
  /** What the action does, in one sentence, before the user opens it. */
  readonly blurb: string;
  readonly group: ManageGroup;
  readonly availability: ManageAvailability;
}

/**
 * The whole catalogue, available and not, in one list.
 *
 * One list rather than two because the drawer has to render both — the brief's hardest rule here
 * is that the four governance features are *named* rather than silently missing, and a separate
 * "missing things" constant is the kind of thing that drifts out of sync with the thing it
 * describes. `unavailableActions()` derives its list from this one.
 */
export const MANAGE_ACTIONS: readonly ManageAction[] = [
  {
    id: "create",
    title: "Create a portfolio",
    blurb:
      "Name a new capital portfolio or monitoring view and file holdings into it. Nothing is bought — the shares are already yours.",
    group: "identity",
    availability: { kind: "available", endpoint: { method: "POST", path: "/api/v1/portfolio" } },
  },
  {
    id: "rename",
    title: "Rename",
    blurb: "Change what a portfolio is called. Its holdings, history and benchmark are untouched.",
    group: "identity",
    availability: {
      kind: "available",
      endpoint: { method: "PATCH", path: "/api/v1/portfolios/{portfolio_id}" },
    },
  },
  {
    id: "benchmark",
    title: "Benchmark",
    blurb: "The index this portfolio's return is measured against.",
    group: "identity",
    availability: {
      kind: "create-only",
      endpoint: { method: "POST", path: "/api/v1/portfolio" },
      reason:
        "A benchmark can be chosen when the portfolio is created and not afterwards. PATCH /portfolios/{id} accepts a name, a parent and a broker account, and no benchmark field.",
      unblockedBy:
        "A benchmark_index_id field on PortfolioPatchIn, and a decision about what happens to the comparison already drawn on the chart when the index changes under it.",
    },
  },
  {
    id: "objective",
    title: "Objective",
    blurb: "What this portfolio is for, in your own words.",
    group: "identity",
    availability: {
      kind: "unavailable",
      reason:
        "Baskfy stores no objective. There is no such column on the portfolio table and no such field on either write body, so there is nowhere to put one.",
      unblockedBy:
        "An objective column on portfolio, plus the field on NewPortfolioIn and PortfolioPatchIn.",
    },
  },
  {
    id: "assign",
    title: "Assign holdings",
    blurb: "File unallocated shares into a capital portfolio. A share belongs to exactly one.",
    group: "holdings",
    availability: {
      kind: "available",
      endpoint: { method: "POST", path: "/api/v1/portfolio/{portfolio_id}/holdings" },
    },
  },
  {
    id: "move",
    title: "Move holdings",
    blurb:
      "Reallocate shares from one capital portfolio to another. Both sides are shown before anything is written.",
    group: "holdings",
    availability: {
      kind: "available",
      endpoint: { method: "POST", path: "/api/v1/portfolio/{portfolio_id}/holdings" },
    },
  },
  {
    id: "watch",
    title: "Add to a monitoring view",
    blurb: `A lens over holdings you already own. ${MONITORING_NOTE}`,
    group: "holdings",
    availability: {
      kind: "available",
      endpoint: { method: "POST", path: "/api/v1/portfolio/{portfolio_id}/holdings" },
    },
  },
  {
    id: "sleeve",
    title: "Sub-portfolios",
    /* §8's rename table governs what a reader sees: a `portfolio_sleeve` row really is a sleeve,
       and the word the user reads for it is "allocation". `no-jargon.test.ts` scans for it. */
    blurb:
      "Split a portfolio's capital into named allocations. The whole split is saved at once, because the parts have to add up to something you meant.",
    group: "structure",
    availability: {
      kind: "available",
      endpoint: { method: "PUT", path: "/api/v1/portfolios/{portfolio_id}/sleeves" },
    },
  },
  {
    id: "broker",
    title: "Broker account",
    blurb:
      "Connect a broker, or change which account a portfolio is attributed to. Re-attributing relabels the container and moves no shares.",
    group: "connections",
    availability: {
      kind: "available",
      endpoint: { method: "PATCH", path: "/api/v1/portfolios/{portfolio_id}" },
    },
  },
  {
    id: "reconciliation",
    title: "Reconciliation",
    blurb:
      "Answer the questions sync could not decide. Each open question freezes its holding's contribution until it is answered.",
    group: "connections",
    availability: {
      kind: "available",
      endpoint: { method: "GET", path: "/api/v1/portfolio/reconciliation" },
    },
  },
  {
    id: "delete",
    title: "Delete",
    blurb: "Remove a portfolio. Its holdings return to Unallocated; its recorded history does not.",
    group: "danger",
    availability: {
      kind: "available",
      endpoint: { method: "DELETE", path: "/api/v1/portfolios/{portfolio_id}" },
    },
  },
  {
    id: "archive",
    title: "Archive",
    blurb: "Keep a closed portfolio's history without it cluttering the list.",
    group: "danger",
    availability: {
      kind: "unavailable",
      reason:
        "A portfolio has a status but no archived state, and no route sets one. The only way to take a portfolio off the list today is to delete it, which destroys the history archiving exists to keep.",
      unblockedBy:
        "An archived_on column on portfolio, a filter on GET /portfolio/overview, and a decision about whether an archived portfolio still counts toward net worth.",
    },
  },
  {
    id: "permissions",
    title: "Permissions",
    blurb: "Let someone else see or edit a portfolio.",
    group: "danger",
    availability: {
      kind: "unavailable",
      reason:
        "Baskfy is single-tenant today. Every portfolio belongs to the one signed-in account and there is no second identity to grant anything to.",
      unblockedBy: "The C3 multi-tenant work — P4.2's two-token OAuth and P4.10's row-level security.",
    },
  },
  {
    id: "ownership",
    title: "Ownership",
    blurb: "Transfer a portfolio to another account.",
    group: "danger",
    availability: {
      kind: "unavailable",
      reason:
        "Same reason as permissions: portfolio.user_id is the only owner a row can have and there is no other account to transfer to.",
      unblockedBy: "The C3 multi-tenant work — P4.2's two-token OAuth and P4.10's row-level security.",
    },
  },
  {
    id: "audit",
    title: "Audit history",
    blurb: "Who changed this portfolio, what they changed and when.",
    group: "danger",
    availability: {
      kind: "unavailable",
      reason:
        "Nothing records who made a change to a portfolio. The activity feed records what happened to the money — trades, dividends, corporate actions — not what was done to the grouping.",
      unblockedBy:
        "An audit table written by the portfolio writes, which needs the multi-tenant actor identity from C3 to have anything useful to record.",
    },
  },
];

export function actionById(id: ManageActionId): ManageAction {
  const found = MANAGE_ACTIONS.find((action) => action.id === id);
  /* Exhaustive by construction: `ManageActionId` and the catalogue are edited together, and
     `manage.test.ts` asserts every id in the union resolves. The throw is the seam saying so
     rather than a silent `undefined` reaching a component as a blank panel. */
  if (found === undefined) throw new Error(`No manage action is defined for "${id}".`);
  return found;
}

/** The actions the drawer draws a working control for. */
export function availableActions(): readonly ManageAction[] {
  return MANAGE_ACTIONS.filter((action) => action.availability.kind !== "unavailable");
}

/** The actions the drawer NAMES rather than draws. Never an empty state, never a dead button. */
export function unavailableActions(): readonly ManageAction[] {
  return MANAGE_ACTIONS.filter((action) => action.availability.kind === "unavailable");
}

export function actionsInGroup(group: ManageGroup): readonly ManageAction[] {
  return MANAGE_ACTIONS.filter((action) => action.group === group);
}

/* ------------------------------------------------------------------ assignment previews */

/** Which of §4.1's three operations a selection is. See the module docstring. */
export type AssignmentIntent = "ASSIGN" | "MOVE" | "WATCH";

/** A portfolio a selection can come from or go to. The kind travels with the name, always. */
export interface AssignmentTarget {
  readonly portfolio_id: number;
  readonly name: string;
  readonly kind: PortfolioKind;
}

export function targetFromRow(row: PortfolioRow): AssignmentTarget {
  return { portfolio_id: row.portfolio_id, name: row.name, kind: row.kind };
}

/** One capital portfolio's claim on one leg — the fact an exclusivity refusal has to name. */
export interface Holder {
  readonly portfolio_id: number;
  readonly name: string;
  readonly quantity: string;
}

/** One holding leg the operation would touch. */
export interface AssignmentLine {
  readonly keyId: string;
  readonly symbol: string;
  readonly name: string;
  readonly brokerLabel: string;
  /** Shares this line would move, as a decimal string. `"0"` where nothing can move. */
  readonly quantity: string;
  /** The value of those shares, or null with {@link unpricedReason}. Never silently zero. */
  readonly value: string | null;
  readonly unpricedReason: string | null;
  /** Capital portfolios that already hold part of this leg. */
  readonly heldBy: readonly Holder[];
}

/** One side of a transfer: what it loses, or what it gains. */
export interface AssignmentSide {
  readonly label: string;
  /** Null for Unallocated, which is a state rather than a portfolio. */
  readonly portfolioId: number | null;
  readonly kind: PortfolioKind | null;
  readonly holdings: number;
  readonly instruments: number;
  /** Rupees. A {@link Metric}, so an unpriced selection renders its reason and not a dash. */
  readonly value: Metric;
  /** The sentence shown under the side's figures. Names the side and the direction. */
  readonly sentence: string;
}

/** A refusal the drawer makes before the server has to. Names the holding and the holder. */
export interface ExclusivityBlock {
  readonly keyId: string;
  readonly symbol: string;
  /** The sentence the user reads. Always names which portfolio holds the share. */
  readonly sentence: string;
  /** What to do instead. Never phrased as advice about the position itself. */
  readonly remedy: string;
  readonly holders: readonly Holder[];
}

export interface AssignmentPreview {
  readonly intent: AssignmentIntent;
  /** What the shares leave. Null for `WATCH`, where nothing leaves anything. */
  readonly source: AssignmentSide | null;
  readonly destination: AssignmentSide;
  readonly lines: readonly AssignmentLine[];
  readonly blocks: readonly ExclusivityBlock[];
  /** Stated wherever a monitoring view is involved. Null otherwise. */
  readonly ownershipNotice: string | null;
  /** What this does to net worth. Always shown, because the answer is always "nothing". */
  readonly netWorthNotice: string;
  /** The invariant in one sentence, at the moment it is being relied on. */
  readonly exclusivityNotice: string;
  readonly canCommit: boolean;
  /** Why the commit button is disabled, when it is. Null when it is live. */
  readonly blockedReason: string | null;
}

export interface AssignmentInput {
  readonly intent: AssignmentIntent;
  readonly rows: readonly AggregatedHolding[];
  /** `holdingKeyId`s the user ticked. */
  readonly selected: ReadonlySet<string>;
  /** `holdingKeyId -> shares`, as typed. Absent or blank means "everything that can move". */
  readonly quantities?: ReadonlyMap<string, string> | undefined;
  /** Required for `MOVE`; ignored otherwise — `ASSIGN` comes from Unallocated. */
  readonly source?: AssignmentTarget | null | undefined;
  readonly destination: AssignmentTarget | null;
}

const UNPRICED =
  "No price for this holding today, so its value is left out of the total rather than counted as zero.";

const NET_WORTH_UNCHANGED =
  "Net worth does not change. These shares are already yours — this only records which portfolio they belong to.";

const EXCLUSIVITY =
  "A share belongs to exactly one capital portfolio. Capital portfolios plus Unallocated add up to your net worth, so a share counted twice would be money you do not have.";

/**
 * The brief's own required sentence, plus what it means for ownership.
 *
 * `VIEWS_NOTICE` is PC1's constant and it is the brief's wording verbatim — "Monitoring views may
 * contain overlapping holdings and are excluded from total portfolio value." It is used rather
 * than `MONITORING_NOTE` (§4.1's shorter table label, "excluded from totals") because this is the
 * moment a person is *deciding*, and the longer sentence is the one that says which total.
 */
const WATCH_OWNERSHIP =
  "This changes no ownership. The holdings stay in whichever capital portfolio — or in Unallocated — they are in now. " +
  VIEWS_NOTICE;

/** Capital claims on one leg. A monitoring view is not an allocation (§4.1), so it is not here. */
function capitalHolders(line: HoldingBrokerLine): Holder[] {
  return (line.allocations ?? [])
    .filter((slice) => slice.portfolio.kind === "CAPITAL")
    .map((slice) => ({
      portfolio_id: slice.portfolio.portfolio_id,
      name: slice.portfolio.name,
      quantity: slice.quantity,
    }));
}

function heldBy(line: HoldingBrokerLine, portfolioId: number): string {
  return capitalHolders(line).find((holder) => holder.portfolio_id === portfolioId)?.quantity ?? "0";
}

/** Shares this leg could move under this intent, before the user narrows it. */
function movableQuantity(
  intent: AssignmentIntent,
  line: HoldingBrokerLine,
  source: AssignmentTarget | null | undefined,
): string {
  if (intent === "MOVE") return source ? heldBy(line, source.portfolio_id) : "0";
  /* A lens answers "which names", never "how many" (§4.1), so the whole leg is what it watches. */
  if (intent === "WATCH") return line.quantity;
  return freeQuantity(line);
}

function typedQuantity(typed: string | undefined, movable: string): string {
  if (typed === undefined || typed.trim() === "") return movable;
  return typed.trim();
}

function positive(value: string): boolean {
  return compareDecimalStrings(value, "0") > 0;
}

function sideOf(
  label: string,
  portfolioId: number | null,
  kind: PortfolioKind | null,
  lines: readonly AssignmentLine[],
  direction: "loses" | "gains" | "watches",
  metricLabel: string,
  definition: string,
): AssignmentSide {
  const stocks = new Set(lines.map((line) => line.symbol)).size;
  const priced = lines.filter((line) => line.value !== null).map((line) => line.value);
  const unpriced = lines.length - priced.length;
  const total = addDecimalStrings(priced);
  const verb =
    direction === "loses" ? "loses" : direction === "gains" ? "gains" : "starts watching";
  return {
    label,
    portfolioId,
    kind,
    holdings: lines.length,
    instruments: stocks,
    value: metric(
      metricLabel,
      definition,
      total,
      lines.length === 0
        ? "Nothing is selected yet."
        : lines.length === 1
          ? "The one holding selected has no price today, so there is no figure to show."
          : `Not one of the ${lines.length} selected holdings has a price today, so there is no figure to show.`,
    ),
    sentence:
      lines.length === 0
        ? `${label} is unchanged — nothing is selected yet.`
        : `${label} ${verb} ${lines.length} holding${lines.length === 1 ? "" : "s"} across ${stocks} stock${stocks === 1 ? "" : "s"}${
            unpriced > 0
              ? `, ${unpriced} of which ${unpriced === 1 ? "has" : "have"} no price today and ${unpriced === 1 ? "is" : "are"} left out of the figure`
              : ""
          }.`,
  };
}

/**
 * What a transfer would do, to **both** sides, before anything is written.
 *
 * The both-sides rule is the whole reason this function exists rather than the component summing
 * a selection. A move that shows only what the destination gains is a form that has hidden half
 * of what it is about to do, and the half it hides is the one the user did not initiate — the
 * portfolio they were not looking at quietly gets smaller.
 *
 * Exclusivity is enforced **here**, in the preview, not left to the server's 400. The server does
 * refuse it, and its sentence is good; but a refusal that only arrives after the user commits is
 * a refusal they discover by being surprised. `heldBy` on every line means the drawer can always
 * say *which* portfolio holds the share, which is the fact that makes the refusal actionable.
 */
export function previewAssignment(input: AssignmentInput): AssignmentPreview {
  const { intent, rows, selected, quantities, destination } = input;
  const source = intent === "MOVE" ? (input.source ?? null) : null;
  const lines: AssignmentLine[] = [];
  const blocks: ExclusivityBlock[] = [];

  for (const row of rows) {
    for (const brokerLine of row.brokers ?? []) {
      const keyId = holdingKeyId({
        instrument_id: row.instrument.instrument_id,
        broker_account_id: brokerLine.broker.broker_account_id,
      });
      if (!selected.has(keyId)) continue;

      const holders = capitalHolders(brokerLine);
      const movable = movableQuantity(intent, brokerLine, source);
      const asked = typedQuantity(quantities?.get(keyId), movable);
      const value =
        brokerLine.value === null || brokerLine.value === undefined
          ? null
          : shareOfValue(brokerLine.value, asked, brokerLine.quantity);

      lines.push({
        keyId,
        symbol: row.instrument.symbol,
        name: row.instrument.name,
        brokerLabel: brokerLine.broker.label,
        quantity: asked,
        value,
        unpricedReason: value === null ? UNPRICED : null,
        heldBy: holders,
      });

      const block = blockFor({
        intent,
        keyId,
        symbol: row.instrument.symbol,
        brokerLabel: brokerLine.broker.label,
        asked,
        movable,
        holders,
        source,
        destination,
      });
      if (block !== null) blocks.push(block);
    }
  }

  const destinationLabel = destination?.name ?? "the destination portfolio";
  const destinationSide = sideOf(
    destinationLabel,
    destination?.portfolio_id ?? null,
    destination?.kind ?? null,
    lines,
    intent === "WATCH" ? "watches" : "gains",
    intent === "WATCH" ? "Value watched" : "Value gained",
    intent === "WATCH"
      ? "What the holdings this lens watches are worth. It is not added to net worth — a lens overlaps, so adding it would count the same shares twice."
      : "What the selected shares are worth at the last price we have, at the quantities entered.",
  );

  const sourceSide =
    intent === "WATCH"
      ? null
      : sideOf(
          intent === "MOVE" ? (source?.name ?? "the source portfolio") : "Unallocated",
          intent === "MOVE" ? (source?.portfolio_id ?? null) : null,
          intent === "MOVE" ? (source?.kind ?? null) : null,
          lines,
          "loses",
          "Value leaving",
          intent === "MOVE"
            ? "What this portfolio stops holding. Its own return is measured over what it holds, so the figures on its row change from the next close."
            : "What leaves Unallocated. These shares stop being unfiled and start counting towards a strategy.",
        );

  const blockedReason = commitBlocker({ intent, lines, blocks, source, destination });

  return {
    intent,
    source: sourceSide,
    destination: destinationSide,
    lines,
    blocks,
    ownershipNotice:
      intent === "WATCH"
        ? WATCH_OWNERSHIP
        : destination?.kind === "MONITORING"
          ? WATCH_OWNERSHIP
          : null,
    netWorthNotice: NET_WORTH_UNCHANGED,
    exclusivityNotice: EXCLUSIVITY,
    canCommit: blockedReason === null,
    blockedReason,
  };
}

interface BlockInput {
  readonly intent: AssignmentIntent;
  readonly keyId: string;
  readonly symbol: string;
  readonly brokerLabel: string;
  readonly asked: string;
  readonly movable: string;
  readonly holders: readonly Holder[];
  readonly source: AssignmentTarget | null;
  readonly destination: AssignmentTarget | null;
}

function nameHolders(holders: readonly Holder[]): string {
  return holders
    .map((holder) => `${formatShares(holder.quantity)} in ${holder.name}`)
    .join(", ");
}

function blockFor(input: BlockInput): ExclusivityBlock | null {
  const { intent, keyId, symbol, brokerLabel, asked, movable, holders, source, destination } = input;
  const where = `${symbol} at ${brokerLabel}`;

  /* A lens may overlap anything — that is what a lens is for — so there is no exclusivity to
     enforce and refusing an overlap here would be refusing it for doing its job (§4.1). */
  if (intent === "WATCH") return null;

  if (intent === "MOVE") {
    const held = holders.find((holder) => holder.portfolio_id === source?.portfolio_id);
    if (held === undefined || !positive(held.quantity)) {
      return {
        keyId,
        symbol,
        sentence:
          holders.length === 0
            ? `${source?.name ?? "That portfolio"} does not hold ${where}, so there is nothing to move out of it.`
            : `${source?.name ?? "That portfolio"} does not hold ${where}. It is held as ${nameHolders(holders)}.`,
        remedy:
          holders.length === 0
            ? `${where} is unallocated. Use Assign holdings to file it instead of moving it.`
            : `Move it from ${holders[0]?.name ?? "the portfolio that holds it"} instead.`,
        holders,
      };
    }
    if (compareDecimalStrings(asked, held.quantity) > 0) {
      return {
        keyId,
        symbol,
        sentence: `${source?.name ?? "The source portfolio"} holds ${formatShares(held.quantity)} of ${where}, and ${formatShares(asked)} were asked for.`,
        remedy: `Lower it to ${formatShares(held.quantity)} or fewer. The rest is held as ${nameHolders(holders)}.`,
        holders,
      };
    }
    return null;
  }

  /* ASSIGN. The exclusivity rule in the one place it bites: shares already inside a capital
     portfolio are not free, and the drawer has to say which portfolio holds them. */
  const elsewhere = holders.filter((holder) => holder.portfolio_id !== destination?.portfolio_id);
  if (!positive(movable)) {
    return {
      keyId,
      symbol,
      sentence:
        elsewhere.length === 0
          ? `There are no free shares of ${where} to assign.`
          : `Every share of ${where} is already in a capital portfolio — ${nameHolders(elsewhere)} — and a share belongs to exactly one.`,
      remedy:
        elsewhere.length === 0
          ? "Nothing of this holding is unallocated, so there is nothing to file."
          : `Use Move holdings to take it out of ${elsewhere[0]?.name ?? "the portfolio holding it"} and put it here.`,
      holders: elsewhere,
    };
  }
  if (compareDecimalStrings(asked, movable) > 0) {
    return {
      keyId,
      symbol,
      sentence:
        elsewhere.length === 0
          ? `${formatShares(movable)} of ${where} are free and ${formatShares(asked)} were asked for.`
          : `Only ${formatShares(movable)} of ${where} are free. The rest is already assigned — ${nameHolders(elsewhere)} — and a share belongs to exactly one capital portfolio.`,
      remedy:
        elsewhere.length === 0
          ? `Lower it to ${formatShares(movable)} or fewer.`
          : `Lower it to ${formatShares(movable)}, or use Move holdings to take the rest out of ${elsewhere[0]?.name ?? "the portfolio holding it"}.`,
      holders: elsewhere,
    };
  }
  return null;
}

function commitBlocker(input: {
  readonly intent: AssignmentIntent;
  readonly lines: readonly AssignmentLine[];
  readonly blocks: readonly ExclusivityBlock[];
  readonly source: AssignmentTarget | null;
  readonly destination: AssignmentTarget | null;
}): string | null {
  const { intent, lines, blocks, source, destination } = input;
  if (destination === null) return "Choose where these holdings should go.";
  if (intent === "MOVE") {
    if (source === null) return "Choose which portfolio the shares come out of.";
    if (source.portfolio_id === destination.portfolio_id) {
      return `${source.name} is both sides of this move, which would change nothing. Choose a different destination.`;
    }
    if (source.kind === "MONITORING" || destination.kind === "MONITORING") {
      return "A monitoring view owns nothing, so there is nothing to move into or out of one. Use Add to a monitoring view instead — it changes no ownership.";
    }
  }
  if (intent === "ASSIGN" && destination.kind === "MONITORING") {
    return "Assigning files shares into a capital portfolio, and this is a monitoring view. Use Add to a monitoring view instead — it changes no ownership.";
  }
  if (intent === "WATCH" && destination.kind === "CAPITAL") {
    return `${destination.name} is a capital portfolio, not a lens. Filing shares into it changes ownership, so use Assign holdings or Move holdings.`;
  }
  if (lines.length === 0) return "Select at least one holding.";
  if (blocks.length > 0) {
    return `${blocks.length} of the selected holding${blocks.length === 1 ? "" : "s"} cannot be ${intent === "MOVE" ? "moved" : "assigned"}. Each says why above.`;
  }
  return null;
}

/* ------------------------------------------------------------------------- delete */

/** One thing a delete destroys that cannot be rebuilt, and why it cannot. */
export interface DeletionLoss {
  readonly what: string;
  readonly detail: string;
}

export interface DeleteImpact {
  readonly portfolio: AssignmentTarget;
  readonly holdingsCount: number;
  readonly value: Metric;
  readonly cash: Metric;
  /** What happens to the shares. The answer is never "they are deleted". */
  readonly holdingsFate: string;
  /** What is genuinely lost. Each entry names the record and why it cannot come back. */
  readonly lost: readonly DeletionLoss[];
  /** What happens to anything nested under this portfolio. */
  readonly childrenNote: string;
  /** The exact text the user must type to confirm — the portfolio's own name. */
  readonly confirmPhrase: string;
  readonly endpoint: Endpoint;
}

/**
 * What deleting this portfolio actually costs, read off the schema rather than guessed.
 *
 * Two facts do the work here, and both are in the database rather than in a doc:
 *
 *   · `portfolio_holding.portfolio_id` is `ON DELETE CASCADE`. Those rows are *bookkeeping* — the
 *     shares are in the user's demat and nothing here reaches a broker — so the shares survive
 *     and become unallocated. "Delete the portfolio" never means "sell the stock", and the panel
 *     says so in as many words, because that is the fear a person brings to a red button.
 *   · `portfolio_nav_daily.portfolio_id` and `portfolio_cash_flow.portfolio_id` are also
 *     `ON DELETE CASCADE`, and §5.1 is explicit that the NAV series is **stored, not
 *     recomputed**. So the chart, the drawdown and the cash-flow rows XIRR is solved from are
 *     gone for good — recreating a portfolio with the same holdings tomorrow starts its history
 *     at tomorrow. That is the real cost of the button and it is the one nobody expects.
 */
export function deleteImpact(row: PortfolioRow): DeleteImpact {
  const isView = row.kind === "MONITORING";
  const count = row.holdings_count ?? 0;
  const lost: DeletionLoss[] = [
    {
      what: "Its recorded value history",
      detail:
        "The daily value series behind this portfolio's chart and its drawdown is stored, not recalculated, so it goes with the row. A new portfolio holding the same shares tomorrow starts its history at tomorrow.",
    },
    {
      what: `Its return since ${row.started_on}`,
      detail: `${row.headline_return.label} is measured from the day this grouping began. Deleting it restarts that clock; nothing can measure it again from ${row.started_on}.`,
    },
  ];
  if (!isView) {
    lost.push({
      what: "Its cash assignments and dividend records",
      detail:
        "The dated flows in and out of this portfolio are what XIRR is solved from. They are attached to the row and are deleted with it.",
    });
  }

  return {
    portfolio: targetFromRow(row),
    holdingsCount: count,
    value: metric(
      "Value in it today",
      "What the holdings filed into this portfolio are worth at the last price we have.",
      row.value,
      "Nothing in this portfolio has been priced yet.",
    ),
    cash: metric(
      "Cash assigned to it",
      "Cash you assigned to this portfolio. It returns to the unassigned balance.",
      row.cash,
      "No cash has been assigned to this portfolio.",
    ),
    holdingsFate: isView
      ? `Nothing is unfiled. ${row.name} is a monitoring view, so it owns none of the ${count} holding${count === 1 ? "" : "s"} it watches — they stay exactly where they are, in whichever capital portfolio or in Unallocated.`
      : count === 0
        ? "There are no holdings in it, so nothing is unfiled."
        : `The ${count} holding${count === 1 ? "" : "s"} in it ${count === 1 ? "is" : "are"} NOT deleted. The shares stay in your demat account, untouched — only the rows saying they belong to ${row.name} go. They return to Unallocated, where you can file them somewhere else.`,
    lost,
    childrenNote:
      "Any portfolio sitting inside this one is promoted to the top level rather than deleted with it, with its own holdings untouched.",
    confirmPhrase: row.name,
    endpoint: { method: "DELETE", path: "/api/v1/portfolios/{portfolio_id}" },
  };
}

/* ------------------------------------------------------------------------ outcomes */

/**
 * What a write handler answers. Mirrors `lib/portfolio/create.CreateResult` deliberately, so a
 * page can pass the existing server action straight in.
 */
export type ManageOutcome =
  | { readonly ok: true; readonly portfolioId: number; readonly message?: string | undefined }
  | { readonly ok: false; readonly reason: string };

/**
 * The sentence a failed write shows.
 *
 * The server's own wording is preferred whenever there is one, because it names the stock and the
 * portfolio and this layer cannot. What this function guarantees is that there is **always a
 * sentence** — a write that fails silently, or with an empty string, is the defect this gate
 * exists for. Baskfy has shipped it twice: a primary action wired to a callback nobody passed,
 * and an Add button that swallowed three separate faults.
 */
export function refusalSentence(action: ManageAction, raw: string | null | undefined): string {
  const given = typeof raw === "string" ? raw.trim() : "";
  if (given !== "") return given;
  return `${action.title} did not save, and the service gave no reason. Nothing was changed — your holdings and portfolios are exactly as they were.`;
}

/** The sentence for a handler that threw rather than answering. Never a stack trace. */
export function unexpectedFailure(action: ManageAction, error: unknown): string {
  const detail = error instanceof Error && error.message.trim() !== "" ? ` (${error.message})` : "";
  return `${action.title} could not be saved because the request itself failed${detail}. Nothing was changed — this form is still open with everything you entered.`;
}

/** The sentence for a control whose write the hosting page has not wired yet. */
export function notWiredReason(action: ManageAction): string {
  return `This page has not passed a save handler for "${action.title}" yet, so the button would do nothing. Nothing you have entered is lost.`;
}

/* ----------------------------------------------------------------- write requests */

/** The body `POST /portfolio/{id}/holdings` wants, built from a preview that passed. */
export interface TransferRequest {
  readonly intent: AssignmentIntent;
  /** The portfolio the shares end up in. The route's `{portfolio_id}`. */
  readonly destinationPortfolioId: number;
  /**
   * Where they come from, for `MOVE`. The API has no "from" parameter — `_apply_allocation`
   * takes the free shares first and then the smallest other slice — so this is carried for the
   * confirmation sentence and for the journal the caller writes, never as a wire field that
   * would be silently ignored.
   */
  readonly sourcePortfolioId: number | null;
  readonly holdings: ReadonlyArray<{
    readonly instrument_id: number;
    readonly broker_account_id: number;
    /** Null means "all of it", which is what the API reads a missing quantity as. */
    readonly quantity: string | null;
  }>;
}

/** `SleeveIn`, re-exported rather than re-typed, so a drift in the API is a compile error. */
export type SleeveDraft = Schemas["SleeveIn"];
export type SleeveRow = Schemas["SleeveOut"];

/**
 * The request a passing preview implies. Returns null for a preview that cannot commit, so a
 * caller physically cannot build a body out of a refused selection.
 *
 * A `WATCH` sends no quantities. §4.1 again: a lens answers "which names", not "how many", and
 * the route accepts a quantity for one only to keep the body shared — sending a number that the
 * server is documented to ignore would be this client asserting something it does not mean.
 */
export function toTransferRequest(preview: AssignmentPreview): TransferRequest | null {
  if (!preview.canCommit) return null;
  const destinationPortfolioId = preview.destination.portfolioId;
  /* A view being *created* is previewed against a placeholder id, because it has no id until the
     server gives it one. That preview is for reading, never for sending: the create route builds
     its own body. Refusing a non-positive id here means no caller can turn one into a request by
     accident. */
  if (destinationPortfolioId === null || destinationPortfolioId <= 0) return null;
  const holdings: TransferRequest["holdings"] = preview.lines.flatMap((line) => {
    const key = parseKeyId(line.keyId);
    if (key === null) return [];
    return [
      {
        instrument_id: key.instrument_id,
        broker_account_id: key.broker_account_id,
        quantity: preview.intent === "WATCH" ? null : line.quantity,
      },
    ];
  });
  if (holdings.length === 0) return null;
  return {
    intent: preview.intent,
    destinationPortfolioId,
    sourcePortfolioId: preview.source?.portfolioId ?? null,
    holdings,
  };
}

function parseKeyId(id: string): { instrument_id: number; broker_account_id: number } | null {
  const [instrument, broker] = id.split(":");
  if (instrument === undefined || broker === undefined) return null;
  const instrumentId = Number(instrument);
  const brokerId = Number(broker);
  if (!Number.isInteger(instrumentId) || !Number.isInteger(brokerId)) return null;
  return { instrument_id: instrumentId, broker_account_id: brokerId };
}

/** The sentence shown after a write that worked. Names what changed, never just "Saved". */
export function successSentence(action: ManageAction, subject: string): string {
  switch (action.id) {
    case "create":
      return `${subject} was created. It is on the command centre now.`;
    case "rename":
      return `Renamed to ${subject}. Its holdings, history and benchmark are unchanged.`;
    case "assign":
      return `Filed into ${subject}. Those shares are out of Unallocated and counting towards it from the next close.`;
    case "move":
      return `Moved into ${subject}. Both portfolios' figures change from the next close.`;
    case "watch":
      return `${subject} is watching them. No ownership changed. ${VIEWS_NOTICE}`;
    case "sleeve":
      return `How ${subject} is split up was saved whole — every allocation, in one write.`;
    case "broker":
      return `${subject} is re-attributed. No share moved — this relabels the container.`;
    case "delete":
      return `${subject} was deleted. Its holdings are back in Unallocated.`;
    default:
      return `${action.title} saved for ${subject}.`;
  }
}
