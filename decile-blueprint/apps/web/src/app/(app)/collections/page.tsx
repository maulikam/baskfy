import { redirect } from "next/navigation";

/** Redirect stub for `/collections` — see `collection/[slug]/page.tsx` for why this exists. */
export default function LegacyCollectionsRedirect() {
  redirect("/baskets/collections");
}
