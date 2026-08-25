import Link from "next/link";
import type { Route } from "next";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * The primary call to action on the public surface.
 *
 * A dark pill with a light chip at its left end; on hover the chip floods the whole shell and an
 * inset ring draws inside it, so the button inverts without anything moving. The mechanics live in
 * `globals.css` under `.sbtn`, ported from vaaya.ai's own rules — this component only supplies the
 * four layers that gesture needs:
 *
 *   `.sbtn-flood`  the chip, which grows to fill the shell
 *   `.sbtn-glyph`  the mark that sits inside the chip at rest
 *   `.sbtn-fl`     the label revealed once the flood has spread
 *   `.sbtn-rest`   the label visible at rest, clear of the chip
 *
 * Both labels are rendered, always. Swapping the text on hover would make the button's width jump
 * mid-transition, and a control that resizes while you are reaching for it is a control you miss.
 */
export interface FloodButtonProps {
  /* `Route`, not `Route | string`: `typedRoutes` is on, and widening to `string` would collapse
     the union and hand back the 404-in-staging class of bug the setting exists to prevent. */
  href: Route;
  /** The label at rest. */
  children: ReactNode;
  /** The label revealed by the flood. Defaults to the resting label. */
  hoverLabel?: ReactNode;
  /** `nav` is the header's smaller instance; `hero` is full size. */
  size?: "hero" | "nav";
  /** Inverts the pair: a white shell with a dark chip, for use on a dark ground. */
  tone?: "dark" | "light";
  className?: string;
}

/** The chevron that rides in the chip. Drawn, not a glyph from a font. */
function Chevrons() {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      className="size-[1.15em]"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.25}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M5 6l6 6-6 6" />
      <path d="M13 6l6 6-6 6" />
    </svg>
  );
}

export function FloodButton({
  href,
  children,
  hoverLabel,
  size = "hero",
  tone = "dark",
  className,
}: FloodButtonProps) {
  return (
    <Link
      href={href}
      className={cn("sbtn", size === "nav" && "sbtn-nav", tone === "light" && "sbtn-light", className)}
    >
      <span className="sbtn-flood" aria-hidden="true" />
      <span className="sbtn-glyph" aria-hidden="true">
        <Chevrons />
      </span>
      <span className="sbtn-fl" aria-hidden="true">
        {hoverLabel ?? children}
      </span>
      <span className="sbtn-rest">{children}</span>
    </Link>
  );
}
