import { describe, expect, it } from "vitest";

import {
  MANAGE_ACTIONS,
  actionById,
  availableActions,
  deleteImpact,
  previewAssignment,
  refusalSentence,
  toTransferRequest,
  unavailableActions,
  unexpectedFailure,
  type AssignmentTarget,
  type ManageActionId,
} from "@/lib/portfolio/manage";
import { holdingKeyId, type AggregatedHolding, type PortfolioKind } from "@/lib/portfolio/organize";
import type { PortfolioRow } from "@/lib/portfolio/overview";
import { decimalStringsEqual } from "@/lib/portfolios/decimal";

/**
 * PC6's pure layer, asserted against the model rather than against the markup.
 *
 * The four that would each be a real defect in a product whose capital portfolios are supposed to
 * add up to net worth to the paisa:
 *
 *   · a share reachable by two capital portfolios at once;
 *   · a move that shows what a portfolio gains and not what the other loses;
 *   · a control offered for an endpoint that does not exist;
 *   · a failed write with no sentence attached to it.
 *
 * Fixtures are schema-exact — no `as unknown as` — so `tsc` fails when a fixture describes a
 * payload the API does not send. PC1 found three invented fields that way.
 */

const LONG_TERM = 1;
const MOMENTUM = 2;
const WATCHLIST = 3;

function ref(portfolioId: number, name: string, kind: PortfolioKind) {
  return { portfolio_id: portfolioId, name, kind, source: "HOLDING_GROUP" as const };
}

interface LegSpec {
  quantity: string;
  unallocated: string;
  value?: string | undefined;
  slices?: ReadonlyArray<{ portfolio: number; name: string; kind: PortfolioKind; quantity: string }>;
}

function holding(
  instrumentId: number,
  symbol: string,
  name: string,
  leg: LegSpec,
  brokerAccountId = 11,
  brokerLabel = "Zerodha",
): AggregatedHolding {
  return {
    instrument: { instrument_id: instrumentId, symbol, name },
    quantity: leg.quantity,
    allocated: (leg.slices ?? []).length > 0,
    pending_reconciliation: false,
    split_across_portfolios: false,
    ...(leg.value === undefined ? {} : { value: leg.value }),
    brokers: [
      {
        broker: { broker_account_id: brokerAccountId, broker_id: "zerodha", label: brokerLabel },
        quantity: leg.quantity,
        unallocated_quantity: leg.unallocated,
        history_source: "NONE",
        pending_reconciliation: false,
        ...(leg.value === undefined ? {} : { value: leg.value }),
        allocations: (leg.slices ?? []).map((slice) => ({
          portfolio: ref(slice.portfolio, slice.name, slice.kind),
          quantity: slice.quantity,
        })),
      },
    ],
  };
}

/** Every share of ITC is inside Long term. This is the exclusivity case. */
const ITC = holding(101, "ITC", "ITC Ltd", {
  quantity: "100",
  unallocated: "0",
  value: "10000",
  slices: [{ portfolio: LONG_TERM, name: "Long term", kind: "CAPITAL", quantity: "100" }],
});

/** 20 of 50 free; the other 30 are in Long term. The partial case. */
const HDFC = holding(102, "HDFCBANK", "HDFC Bank", {
  quantity: "50",
  unallocated: "20",
  value: "5000",
  slices: [{ portfolio: LONG_TERM, name: "Long term", kind: "CAPITAL", quantity: "30" }],
});

/** Wholly unallocated. */
const INFY = holding(103, "INFY", "Infosys", {
  quantity: "40",
  unallocated: "40",
  value: "8000",
});

/** Unallocated and unpriced — `value` is absent, which is not the same as zero. */
const TCS = holding(104, "TCS", "Tata Consultancy", { quantity: "10", unallocated: "10" });

const ROWS: readonly AggregatedHolding[] = [ITC, HDFC, INFY, TCS];

function keys(...rows: readonly AggregatedHolding[]): Set<string> {
  return new Set(
    rows.flatMap((row) =>
      (row.brokers ?? []).map((line) =>
        holdingKeyId({
          instrument_id: row.instrument.instrument_id,
          broker_account_id: line.broker.broker_account_id,
        }),
      ),
    ),
  );
}

const longTerm: AssignmentTarget = { portfolio_id: LONG_TERM, name: "Long term", kind: "CAPITAL" };
const momentum: AssignmentTarget = { portfolio_id: MOMENTUM, name: "Momentum", kind: "CAPITAL" };
const watchlist: AssignmentTarget = {
  portfolio_id: WATCHLIST,
  name: "Watchlist",
  kind: "MONITORING",
};

function row(over: Partial<PortfolioRow> = {}): PortfolioRow {
  return {
    portfolio_id: LONG_TERM,
    name: "Long term",
    kind: "CAPITAL",
    source: "HOLDING_GROUP",
    source_badge: "Grouped",
    started_on: "2025-04-01",
    value: "480000.00",
    cash: "12000.00",
    counts_toward_total: true,
    status: "Synced",
    holdings_count: 7,
    broker_count: 1,
    pending_reconciliation: false,
    brokers: [{ broker_account_id: 11, broker_id: "zerodha", label: "Zerodha" }],
    todays_pnl: { amount: "1200.00", label: "today" },
    headline_return: {
      kind: "SINCE_GROUPED",
      label: "Since grouped",
      since: "2025-04-01",
      is_model: false,
      value: "14.2",
    },
    ...over,
  };
}

/* ------------------------------------------------------------------ the catalogue */

describe("manage actions", () => {
  it("actions: every one the drawer can perform names a method and a route the API serves", () => {
    /* `ApiPath` is `keyof paths` from the generated OpenAPI document, so a route that does not
       exist fails to COMPILE. This asserts the other half — that nothing is offered with an
       empty or malformed endpoint, which the type cannot see. */
    for (const action of availableActions()) {
      const availability = action.availability;
      expect(availability.kind).not.toBe("unavailable");
      if (availability.kind === "unavailable") continue;
      expect(availability.endpoint.path.startsWith("/api/v1/")).toBe(true);
      expect(["GET", "POST", "PUT", "PATCH", "DELETE"]).toContain(availability.endpoint.method);
    }
  });

  it("actions: create, rename, assign, move, sub-portfolio and delete each reach a real write", () => {
    const wanted: Record<string, string> = {
      create: "POST /api/v1/portfolio",
      rename: "PATCH /api/v1/portfolios/{portfolio_id}",
      assign: "POST /api/v1/portfolio/{portfolio_id}/holdings",
      move: "POST /api/v1/portfolio/{portfolio_id}/holdings",
      sleeve: "PUT /api/v1/portfolios/{portfolio_id}/sleeves",
      delete: "DELETE /api/v1/portfolios/{portfolio_id}",
    };
    for (const [id, expected] of Object.entries(wanted)) {
      const availability = actionById(id as ManageActionId).availability;
      expect(availability.kind).not.toBe("unavailable");
      if (availability.kind === "unavailable") continue;
      expect(`${availability.endpoint.method} ${availability.endpoint.path}`).toBe(expected);
    }
  });

  it("actions: an objective is named as unavailable, because no write body carries the field", () => {
    /* §6.3 said "update ... exists". `PortfolioPatchIn` carries name, parent_id and
       broker_account_id and nothing else, and `NewPortfolioIn` has no objective either. A text
       box here would be a control with nowhere to save to. */
    const availability = actionById("objective").availability;
    expect(availability.kind).toBe("unavailable");
    if (availability.kind !== "unavailable") return;
    expect(availability.reason).toMatch(/no such column|stores no objective|nowhere/i);
    expect(availability.unblockedBy).toMatch(/objective column/i);
  });

  it("actions: a benchmark is offered at creation only, and says why it cannot change later", () => {
    const availability = actionById("benchmark").availability;
    expect(availability.kind).toBe("create-only");
    if (availability.kind !== "create-only") return;
    expect(availability.endpoint.path).toBe("/api/v1/portfolio");
    expect(availability.reason).toMatch(/created/i);
    expect(availability.unblockedBy.trim()).not.toBe("");
  });

  it("actions: archive, permissions, ownership and audit each carry a reason and an unblock", () => {
    const named = unavailableActions().map((action) => action.id);
    expect(named).toEqual(
      expect.arrayContaining(["archive", "permissions", "ownership", "audit", "objective"]),
    );
    for (const action of unavailableActions()) {
      const availability = action.availability;
      if (availability.kind !== "unavailable") continue;
      /* Not a placeholder: a sentence long enough to have said something. A reason of "Coming
         soon" passes a truthiness check and tells a reader nothing. */
      expect(availability.reason.length).toBeGreaterThan(40);
      expect(availability.unblockedBy.length).toBeGreaterThan(20);
      expect(action.blurb.trim()).not.toBe("");
    }
  });

  it("actions: every id in the union resolves, and none is defined twice", () => {
    const ids = MANAGE_ACTIONS.map((action) => action.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const id of ids) expect(actionById(id).id).toBe(id);
  });

  it("actions: nothing in the catalogue is phrased as advice about a position", () => {
    /* D3: Baskfy is not registered to give investment advice, so no control may suggest one. */
    const copy = MANAGE_ACTIONS.map((action) => `${action.title} ${action.blurb}`)
      .join(" ")
      .toLowerCase();
    for (const word of ["you should buy", "you should sell", "we recommend", "trim your", "book profit"]) {
      expect(copy).not.toContain(word);
    }
  });
});

/* -------------------------------------------------------------------- moving */

describe("moving holdings between capital portfolios", () => {
  it("move: previews both sides — what the source loses and what the destination gains", () => {
    const preview = previewAssignment({
      intent: "MOVE",
      rows: ROWS,
      selected: keys(ITC, HDFC),
      source: longTerm,
      destination: momentum,
    });

    expect(preview.canCommit).toBe(true);
    expect(preview.source).not.toBeNull();
    expect(preview.source?.label).toBe("Long term");
    expect(preview.destination.label).toBe("Momentum");

    /* ITC moves whole (10,000) and 30 of HDFC's 50 moves (3,000 of 5,000). Both sides carry the
       same figure, because a move is a transfer and not a creation. */
    expect(decimalStringsEqual(preview.source?.value.value ?? "", "13000")).toBe(true);
    expect(decimalStringsEqual(preview.destination.value.value ?? "", "13000")).toBe(true);
    expect(preview.source?.sentence).toContain("Long term loses 2 holdings");
    expect(preview.destination.sentence).toContain("Momentum gains 2 holdings");
    expect(preview.netWorthNotice).toContain("Net worth does not change");
  });

  it("move: refuses shares the source does not hold, and names the portfolio that does", () => {
    const preview = previewAssignment({
      intent: "MOVE",
      rows: ROWS,
      selected: keys(INFY),
      source: longTerm,
      destination: momentum,
    });
    expect(preview.canCommit).toBe(false);
    expect(preview.blocks).toHaveLength(1);
    expect(preview.blocks[0]?.sentence).toContain("Long term does not hold INFY at Zerodha");
    expect(preview.blocks[0]?.remedy).toContain("Assign holdings");
  });

  it("move: refuses more shares than the source holds, and says how many it has", () => {
    const preview = previewAssignment({
      intent: "MOVE",
      rows: ROWS,
      selected: keys(HDFC),
      quantities: new Map([[holdingKeyId({ instrument_id: 102, broker_account_id: 11 }), "45"]]),
      source: longTerm,
      destination: momentum,
    });
    expect(preview.canCommit).toBe(false);
    expect(preview.blocks[0]?.sentence).toContain("Long term holds 30 of HDFCBANK at Zerodha");
    expect(preview.blocks[0]?.remedy).toContain("Lower it to 30");
  });

  it("move: the same portfolio on both sides is refused before anything is sent", () => {
    const preview = previewAssignment({
      intent: "MOVE",
      rows: ROWS,
      selected: keys(ITC),
      source: longTerm,
      destination: longTerm,
    });
    expect(preview.canCommit).toBe(false);
    expect(preview.blockedReason).toContain("both sides of this move");
  });

  it("move: a monitoring view cannot be either side, because it owns nothing to move", () => {
    const preview = previewAssignment({
      intent: "MOVE",
      rows: ROWS,
      selected: keys(ITC),
      source: longTerm,
      destination: watchlist,
    });
    expect(preview.canCommit).toBe(false);
    expect(preview.blockedReason).toContain("owns nothing");
    expect(preview.blockedReason).toContain("Add to a monitoring view");
  });

  it("move: an unpriced leg reports its reason rather than counting as zero", () => {
    const preview = previewAssignment({
      intent: "ASSIGN",
      rows: ROWS,
      selected: keys(INFY, TCS),
      destination: momentum,
    });
    const unpriced = preview.lines.find((line) => line.symbol === "TCS");
    expect(unpriced?.value).toBeNull();
    expect(unpriced?.unpricedReason).toContain("rather than counted as zero");
    /* 8,000 for INFY alone, NOT 8,000 described as the whole selection. */
    expect(decimalStringsEqual(preview.destination.value.value ?? "", "8000")).toBe(true);
    expect(preview.destination.sentence).toContain("1 of which has no price today");
  });

  it("move: a request is built only from a preview that passed", () => {
    const refused = previewAssignment({
      intent: "MOVE",
      rows: ROWS,
      selected: keys(INFY),
      source: longTerm,
      destination: momentum,
    });
    expect(toTransferRequest(refused)).toBeNull();

    const allowed = previewAssignment({
      intent: "MOVE",
      rows: ROWS,
      selected: keys(ITC),
      source: longTerm,
      destination: momentum,
    });
    const request = toTransferRequest(allowed);
    expect(request?.destinationPortfolioId).toBe(MOMENTUM);
    expect(request?.sourcePortfolioId).toBe(LONG_TERM);
    expect(request?.holdings).toEqual([
      { instrument_id: 101, broker_account_id: 11, quantity: "100" },
    ]);
  });
});

/* ---------------------------------------------------------------- exclusivity */

describe("exclusivity between capital portfolios", () => {
  it("exclusivity: a share already in a capital portfolio cannot be assigned to a second one", () => {
    const preview = previewAssignment({
      intent: "ASSIGN",
      rows: ROWS,
      selected: keys(ITC),
      destination: momentum,
    });
    expect(preview.canCommit).toBe(false);
    expect(preview.blocks).toHaveLength(1);
    /* The refusal is worthless without the holder's NAME — that is the fact that lets the user
       fix it, and the reason the block carries `holders` rather than a boolean. */
    expect(preview.blocks[0]?.sentence).toContain("100 in Long term");
    expect(preview.blocks[0]?.sentence).toContain("a share belongs to exactly one");
    expect(preview.blocks[0]?.holders.map((holder) => holder.name)).toEqual(["Long term"]);
    expect(preview.blocks[0]?.remedy).toContain("Move holdings");
  });

  it("exclusivity: a partly-free leg says how many are free and where the rest is", () => {
    const preview = previewAssignment({
      intent: "ASSIGN",
      rows: ROWS,
      selected: keys(HDFC),
      quantities: new Map([[holdingKeyId({ instrument_id: 102, broker_account_id: 11 }), "35"]]),
      destination: momentum,
    });
    expect(preview.canCommit).toBe(false);
    expect(preview.blocks[0]?.sentence).toContain("Only 20 of HDFCBANK at Zerodha are free");
    expect(preview.blocks[0]?.sentence).toContain("30 in Long term");
  });

  it("exclusivity: the free part of a split leg assigns without complaint", () => {
    const preview = previewAssignment({
      intent: "ASSIGN",
      rows: ROWS,
      selected: keys(HDFC),
      destination: momentum,
    });
    expect(preview.blocks).toHaveLength(0);
    expect(preview.canCommit).toBe(true);
    /* The default is the FREE quantity, never the whole position: 20 of 50, worth 2,000. */
    expect(preview.lines[0]?.quantity).toBe("20");
    expect(decimalStringsEqual(preview.destination.value.value ?? "", "2000")).toBe(true);
  });

  it("exclusivity: the invariant is stated on every preview, not only on a refusal", () => {
    const clean = previewAssignment({
      intent: "ASSIGN",
      rows: ROWS,
      selected: keys(INFY),
      destination: momentum,
    });
    expect(clean.blocks).toHaveLength(0);
    expect(clean.exclusivityNotice).toContain("exactly one capital portfolio");
  });

  it("exclusivity: it does not apply to a monitoring view, which may overlap freely", () => {
    const preview = previewAssignment({
      intent: "WATCH",
      rows: ROWS,
      selected: keys(ITC, HDFC, INFY),
      destination: watchlist,
    });
    expect(preview.blocks).toHaveLength(0);
    expect(preview.canCommit).toBe(true);
    /* Nothing leaves anything: a lens has no source side at all. */
    expect(preview.source).toBeNull();
    expect(preview.ownershipNotice).toContain("changes no ownership");
    expect(preview.ownershipNotice).toContain("excluded from total portfolio value");
  });

  it("exclusivity: assigning into a monitoring view is refused as the wrong verb", () => {
    const preview = previewAssignment({
      intent: "ASSIGN",
      rows: ROWS,
      selected: keys(INFY),
      destination: watchlist,
    });
    expect(preview.canCommit).toBe(false);
    expect(preview.blockedReason).toContain("Add to a monitoring view");
  });

  it("exclusivity: a view's request carries names and no share counts", () => {
    const preview = previewAssignment({
      intent: "WATCH",
      rows: ROWS,
      selected: keys(ITC),
      destination: watchlist,
    });
    const request = toTransferRequest(preview);
    expect(request?.holdings).toEqual([
      { instrument_id: 101, broker_account_id: 11, quantity: null },
    ]);
  });
});

/* --------------------------------------------------------------------- delete */

describe("deleting a portfolio", () => {
  it("delete: names what is lost, and says the holdings return to unallocated", () => {
    const impact = deleteImpact(row());
    expect(impact.holdingsFate).toContain("NOT deleted");
    expect(impact.holdingsFate).toContain("stay in your demat");
    expect(impact.holdingsFate).toContain("return to Unallocated");

    const lost = impact.lost.map((entry) => entry.what).join(" | ");
    expect(lost).toContain("recorded value history");
    expect(lost).toContain("2025-04-01");
    expect(lost).toContain("cash assignments");
    expect(impact.childrenNote).toContain("promoted to the top level");
    expect(impact.confirmPhrase).toBe("Long term");
    expect(impact.endpoint).toEqual({
      method: "DELETE",
      path: "/api/v1/portfolios/{portfolio_id}",
    });
  });

  it("delete: the value and cash figures carry a reason when there is no figure", () => {
    const impact = deleteImpact(row({ value: "", cash: "" }));
    expect(impact.value.value).toBeNull();
    expect(impact.value.unavailable).toContain("has been priced");
    expect(impact.cash.value).toBeNull();
    expect(impact.cash.unavailable).toContain("No cash");
  });

  it("delete: a monitoring view owns nothing, so nothing is unfiled by deleting it", () => {
    const impact = deleteImpact(
      row({ portfolio_id: WATCHLIST, name: "Watchlist", kind: "MONITORING", holdings_count: 4 }),
    );
    expect(impact.holdingsFate).toContain("owns none of the 4 holdings it watches");
    expect(impact.holdingsFate).toContain("stay exactly where they are");
    /* No cash-flow record to lose: a lens never had one. */
    expect(impact.lost.map((entry) => entry.what).join(" ")).not.toContain("cash assignments");
  });
});

/* ------------------------------------------------------------------ refusals */

describe("a write that does not succeed", () => {
  it("fails with a sentence even when the service gives no reason at all", () => {
    const action = actionById("rename");
    expect(refusalSentence(action, "HDFC Bank is already in Long term")).toBe(
      "HDFC Bank is already in Long term",
    );
    for (const empty of [null, undefined, "", "   "]) {
      const sentence = refusalSentence(action, empty);
      expect(sentence).toContain("did not save");
      expect(sentence).toContain("Nothing was changed");
    }
  });

  it("fails with a sentence when the request itself throws, and never with a stack trace", () => {
    const sentence = unexpectedFailure(actionById("delete"), new Error("fetch failed"));
    expect(sentence).toContain("fetch failed");
    expect(sentence).toContain("Nothing was changed");
    expect(sentence).not.toContain("at ");

    const bare = unexpectedFailure(actionById("delete"), "not an error");
    expect(bare).toContain("could not be saved");
    expect(bare).toContain("Nothing was changed");
  });
});
