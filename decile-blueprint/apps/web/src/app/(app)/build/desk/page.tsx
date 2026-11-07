import { redirect } from "next/navigation";

/** `/build/desk` — DESK-UI (6 Nov 2026): the console's pages are web routes now; Desk opens the weekly rebalance. */
export default function BuildDeskPage(): never {
  redirect("/rebalance");
}
