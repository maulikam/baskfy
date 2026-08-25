import { redirect } from "next/navigation";

/**
 * Redirect stub for `/collection/[slug]`.
 *
 * `next.config.ts` already redirects this path, and Next evaluates `redirects()` before the
 * filesystem routes — so this file is never rendered at the edge. It exists as the second line of
 * defence for a client-side navigation that never reaches the edge, which is the convention Tree 6
 * established for every moved path. `scripts/check-shadowed-routes.mjs` asserts that a redirected
 * path holds nothing *but* a redirect; this is that and no more.
 */
export default async function LegacyCollectionRedirect({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  redirect(`/baskets/collections/${slug}`);
}
