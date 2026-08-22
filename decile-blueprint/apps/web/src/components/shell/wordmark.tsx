import Image from "next/image";
import Link from "next/link";

import { cn } from "@/lib/utils";

/**
 * The Baskfy mark and name (M37).
 *
 * The mark is the woven basket Maulik chose — orange ribbons crossing, which is a basket and also,
 * read the other way, a rising line. It ships as **SVG**: the vector is the supplied artwork, so it
 * is crisp at any density and at any size without a set of raster steps to pick between.
 * `scripts/build-brand-svg.mjs` optimises it and squares its viewBox; the PNG set beside it exists
 * only for the places a vector cannot go — favicons, touch icons, share cards.
 *
 * ## Sizing
 *
 * The mark is set against the **cap height of the word beside it, not its font size**, which is
 * the whole reason the first attempt looked undersized. "Baskfy" at 17px has a cap height near
 * 12px; a 28px mark carrying 6% of baked-in padding put roughly 25px of ink against it, and the
 * two read as different weights of the same lockup. The viewBox is tight to the ink now (padding
 * belongs to icons, which get cropped by an operating system) and the mark is drawn at 32px, so
 * the ink genuinely overhangs the type the way a lockup should.
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
        src="/brand/logo.svg"
        alt=""
        width={32}
        height={32}
        priority
        /*
         * `unoptimized` because there is nothing to optimise: the optimiser resizes and re-encodes
         * raster formats, and it refuses SVG outright unless `dangerouslyAllowSVG` is set — a flag
         * that exists to stop *remote* SVGs executing script, and one this project should not turn
         * on to serve its own logo. The file is already minified and gzips to 38 KB; sending it
         * straight from `/public` skips a `/_next/image` round trip it would fail anyway.
         */
        unoptimized
        className="size-8 shrink-0"
      />
      {markOnly ? null : (
        <span className="font-display text-[1.125rem] font-semibold tracking-[-0.035em]">
          Baskfy
        </span>
      )}
    </Link>
  );
}
