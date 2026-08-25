import { redirect } from "next/navigation";

/**
 * Tree 6: permanent consumer IA move — see next.config.ts.
 *
 * A stub, not a copy of the page. `next.config.ts` redirects `/backtests/:id` before the
 * filesystem routes are consulted, so a real page here would compile, ship and never render — and
 * would drift away from `/build/backtests/[id]` silently.
 * `scripts/check-shadowed-routes.mjs` keeps it a stub.
 */
export default async function LegacyBacktestRedirect({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  redirect(`/build/backtests/${id}`);
}
