"use client";

import { useState } from "react";

import { cn } from "@/lib/utils";

/**
 * Copy the shareable basket URL (AF I.5). OG image is served from
 * `basket/[slug]/opengraph-image.tsx` for crawlers that fetch the link.
 */

export function ShareBasketLink({
  slug,
  className,
}: {
  slug: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    const url = `${window.location.origin}/basket/${slug}`;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }

  return (
    <button
      type="button"
      onClick={() => void copy()}
      data-testid="share-basket-link"
      className={cn(
        "rounded-md border border-border/70 bg-card px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground",
        className,
      )}
    >
      {copied ? "Link copied" : "Copy share link"}
    </button>
  );
}
