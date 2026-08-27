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
  if (!draft.name.trim()) {
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
