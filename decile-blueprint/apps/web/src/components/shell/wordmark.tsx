import Image from "next/image";
import Link from "next/link";

import { cn } from "@/lib/utils";

/**
 * The Baskfy mark and name (M37).
 *
 * The mark is the woven basket Maulik chose — three orange ribbons crossing, which is a basket and
 * also, read the other way, a rising line. It ships as PNG rather than SVG because the source is
 * raster art with soft anti-aliased edges; `brand-src/build_brand.py` mattes the white away and
 * exports the size set, and `next/image` picks the right one for the density it is drawn at.
 *
 * **One component, every surface.** The top navigation, the sign-in card, the public header and
 * the public footer all render this, so the mark cannot be one size here and another there — which
 * is exactly what happened before, when the wordmark was plain text in three separate files.
 *
 * `priority` is set because this is above the fold on every page in the application: without it
 * the mark arrives after hydration and the header visibly reflows.
 */
export interface WordmarkProps {
  className?: string;
  /** Hide the word and keep the mark. For a narrow header or a collapsed rail. */
  markOnly?: boolean;
}

export function Wordmark({ className, markOnly = false }: WordmarkProps) {
  return (
    <Link
      href="/"
      className={cn(
        "flex shrink-0 items-center gap-2 rounded-md py-0.5 transition-opacity duration-150 hover:opacity-80",
        className,
      )}
      aria-label="Baskfy — home"
    >
      <Image
        src="/brand/logo-mark-192.png"
        alt=""
        width={28}
        height={28}
        priority
        className="size-7 shrink-0"
      />
      {markOnly ? null : (
        <span className="font-display text-[1.0625rem] font-semibold tracking-[-0.03em]">
          Baskfy
        </span>
      )}
    </Link>
  );
}
