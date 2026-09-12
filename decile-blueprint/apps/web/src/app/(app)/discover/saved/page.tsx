import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { PAGES } from "@/lib/vocabulary";

/**
 * AFH 5.7 — Discover → Saved and Portfolio → Watchlist were the same list under two names.
 * Saved redirects to Watchlist; one door, one name.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/discover/saved"].title,
  description: PAGES["/discover/saved"].blurb,
  robots: { index: false, follow: false },
};

export default function SavedPage() {
  redirect("/portfolio/watchlist");
}
