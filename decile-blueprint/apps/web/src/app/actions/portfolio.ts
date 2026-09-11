"use server";

import { revalidatePath } from "next/cache";

import { createPortfolio, type CreateResult } from "@/lib/portfolio/create";
import type { PortfolioDraft } from "@/lib/portfolio/organize";

/**
 * §6.7's confirm button, from a client click.
 *
 * A server action rather than a browser `fetch` for the reason every other write in this app is
 * one: the bearer token lives in the server session, and handing it to the browser to make the
 * call would put it somewhere the page's own scripts can read.
 *
 * On success the portfolio surfaces are revalidated, because the holdings that just moved out of
 * Unallocated are on three of them — §6.6's centrepiece, §6.5's table and §2's Holdings tab — and
 * a user who confirms and still sees the stock unallocated will reasonably conclude it did not
 * work and do it again.
 */
export async function createPortfolioAction(draft: PortfolioDraft): Promise<CreateResult> {
  // The name is required to CREATE a portfolio and meaningless when adding to one that exists —
  // it already has a name, and the EXISTING flow never asks for one. Guarding unconditionally
  // refused every add before it reached the route, which is why the button did nothing at all
  // (11 Sep 2026).
  if (draft.start !== "EXISTING" && !draft.name.trim()) {
    return { ok: false, reason: "Give the portfolio a name first." };
  }
  const result = await createPortfolio(draft);
  if (result.ok) {
    revalidatePath("/portfolio/overview");
    revalidatePath("/portfolio/holdings");
    revalidatePath("/portfolio/portfolios");
  }
  return result;
}

/* ------------------------------------------------------------------ *
 * PC6's management drawer — the six writes beside `create`
 * ------------------------------------------------------------------ *
 *
 * `gates/pc6.md` built the drawer with every write behind an optional handler, so an unwired
 * control disables itself beside its reason rather than swallowing a click. These are the
 * handlers. Without them the drawer would open and do nothing, which is the defect the leaf's
 * own G7 exists to prevent — and which this product has shipped twice.
 *
 * Server actions, for the same reason `createPortfolioAction` is one: the bearer token lives in
 * the server session, and handing it to the browser would put it where the page's own scripts
 * can read it.
 *
 * Every one returns a `ManageOutcome`, and every failure carries a SENTENCE — the server's own
 * where there is one, because it names the stock and the portfolio and this layer cannot.
 */

import type { SleeveIn, SleeveOut } from "@baskfy/api-client";

import { serverApi } from "@/lib/api/server";
import type { ManageOutcome, TransferRequest } from "@/lib/portfolio/manage";

/** Everything a portfolio write touches. One list, so no action can forget a surface. */
function revalidatePortfolioSurfaces(): void {
  revalidatePath("/portfolio/overview");
  revalidatePath("/portfolio/holdings");
  revalidatePath("/portfolio/portfolios");
}

/**
 * The sentence a failure shows.
 *
 * RFC 9457's `detail` is the server's own wording and is always preferred. The fallback says what
 * is true — the write did not happen and nothing changed — rather than "Something went wrong",
 * which tells a reader nothing about whether to try again.
 */
function reasonFrom(error: unknown, fallback: string): string {
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim() !== "") return detail;
  }
  return fallback;
}

export async function renamePortfolioAction(
  portfolioId: number,
  name: string,
): Promise<ManageOutcome> {
  const trimmed = name.trim();
  if (trimmed === "") return { ok: false, reason: "Give the portfolio a name first." };

  const api = await serverApi();
  const { data, error } = await api.PATCH("/api/v1/portfolios/{portfolio_id}", {
    params: { path: { portfolio_id: portfolioId } },
    body: { name: trimmed },
  });
  if (!data) {
    return { ok: false, reason: reasonFrom(error, "The portfolio was not renamed.") };
  }
  revalidatePortfolioSurfaces();
  return { ok: true, portfolioId, message: `Renamed to ${trimmed}.` };
}

/**
 * Assign, move and watch, all three.
 *
 * The API has no "from" parameter — `_apply_allocation` takes the free shares first and then the
 * smallest other slice — so `sourcePortfolioId` never becomes a wire field. It is carried for the
 * confirmation sentence, and sending it would be a value silently ignored.
 */
export async function transferHoldingsAction(
  request: TransferRequest,
): Promise<ManageOutcome> {
  if (request.holdings.length === 0) {
    return { ok: false, reason: "Choose at least one holding first." };
  }

  const api = await serverApi();
  const { data, error } = await api.POST("/api/v1/portfolio/{portfolio_id}/holdings", {
    params: { path: { portfolio_id: request.destinationPortfolioId } },
    body: {
      holdings: request.holdings.map((holding) => ({
        instrument_id: holding.instrument_id,
        broker_account_id: holding.broker_account_id,
        quantity: holding.quantity,
      })),
    },
  });
  if (!data) {
    return {
      ok: false,
      reason: reasonFrom(error, "Nothing moved. Your holdings are exactly as they were."),
    };
  }
  revalidatePortfolioSurfaces();
  return { ok: true, portfolioId: request.destinationPortfolioId };
}

/** Read before write: `SleeveForm` refuses to save a division it has not first read. */
export async function loadSleevesAction(portfolioId: number): Promise<readonly SleeveOut[]> {
  const api = await serverApi();
  const { data } = await api.GET("/api/v1/portfolios/{portfolio_id}/sleeves", {
    params: { path: { portfolio_id: portfolioId } },
  });
  // `SleeveListOut` is `{ sleeves, total_capital }` — not the `{ data }` envelope most list
  // routes use. An empty array is the honest answer for a portfolio that has never been divided
  // and for one the read could not reach; the form refuses to SAVE a division it has not read.
  return data?.sleeves ?? [];
}

export async function saveSleevesAction(
  portfolioId: number,
  sleeves: readonly SleeveIn[],
): Promise<ManageOutcome> {
  const api = await serverApi();
  const { data, error } = await api.PUT("/api/v1/portfolios/{portfolio_id}/sleeves", {
    params: { path: { portfolio_id: portfolioId } },
    body: { sleeves: [...sleeves] },
  });
  if (!data) {
    return { ok: false, reason: reasonFrom(error, "The division was not saved.") };
  }
  revalidatePortfolioSurfaces();
  return { ok: true, portfolioId };
}

export async function reattributePortfolioAction(
  portfolioId: number,
  brokerAccountId: number | null,
): Promise<ManageOutcome> {
  const api = await serverApi();
  const { data, error } = await api.PATCH("/api/v1/portfolios/{portfolio_id}", {
    params: { path: { portfolio_id: portfolioId } },
    body: { broker_account_id: brokerAccountId },
  });
  if (!data) {
    return { ok: false, reason: reasonFrom(error, "The broker account was not changed.") };
  }
  revalidatePortfolioSurfaces();
  return { ok: true, portfolioId };
}

/**
 * Delete.
 *
 * The shares survive — they are in the demat, and `portfolio_holding` is bookkeeping. What is
 * genuinely lost is the stored NAV series, the return since `started_on` and the dated flows XIRR
 * is solved from. PC6's panel says exactly that before it asks for the name to be typed.
 */
export async function deletePortfolioAction(portfolioId: number): Promise<ManageOutcome> {
  const api = await serverApi();
  const { error, response } = await api.DELETE("/api/v1/portfolios/{portfolio_id}", {
    params: { path: { portfolio_id: portfolioId } },
  });
  if (!response.ok) {
    return { ok: false, reason: reasonFrom(error, "The portfolio was not deleted.") };
  }
  revalidatePortfolioSurfaces();
  return { ok: true, portfolioId };
}
