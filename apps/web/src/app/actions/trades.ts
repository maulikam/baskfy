"use server";

import { revalidatePath } from "next/cache";

import { importTradebook, syncTodaysTrades, type TradeWrite } from "@/lib/trades/fetch";

function refresh(): void {
  revalidatePath("/portfolio/activity");
  revalidatePath("/portfolio/overview");
  revalidatePath("/portfolio/holdings");
}

/** Upload a Console tradebook — the file goes server to API; the bearer never reaches the client. */
export async function importTradebookAction(form: FormData): Promise<TradeWrite> {
  const file = form.get("file");
  if (!(file instanceof File) || file.size === 0) {
    return { ok: false, error: "Choose the tradebook CSV you downloaded from Zerodha Console." };
  }
  const result = await importTradebook(file);
  if (result.ok) refresh();
  return result;
}

/** Keep today's trades before Kite flushes them tonight. Read-only at the broker. */
export async function syncTodaysTradesAction(): Promise<TradeWrite> {
  const result = await syncTodaysTrades();
  if (result.ok) refresh();
  return result;
}
