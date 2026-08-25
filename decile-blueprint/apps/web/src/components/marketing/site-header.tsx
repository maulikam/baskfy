import Link from "next/link";

import { FloodButton } from "@/components/marketing/flood-button";
import { Wordmark } from "@/components/shell/wordmark";

/**
 * The public header — a floating capsule, not a bar.
 *
 * Ported from vaaya.ai (24 Aug 2026) at Maulik's instruction, from its computed styles rather than
 * from a screenshot: the nav sits `fixed` 16px from the top, is 52px tall, carries white at 85%
 * behind a 24px backdrop blur, one soft `0 2px 20px rgba(0,0,0,0.05)` shadow and **no border**,
 * with 24px between its items and 20/24px of inset padding. Links are 15px at 70% black and take a
 * full-round hover wash rather than an underline.
 *
 * The absence of a border is doing real work. A hairline would draw the capsule as a *panel* over
 * the page; without one it reads as a pane of glass resting on it, which is why the blur is worth
 * its cost here and nowhere else in this codebase.
 *
 * Not the app shell: docs/08 §"App shell" describes a sidebar for an authenticated tool, and a
 * signed-out visitor reading the refund policy has no use for a Screens link they cannot open.
 */
const LINKS = [
  { href: "/baskets", label: "Baskets" },
  { href: "/market/today", label: "Market" },
  { href: "/pricing", label: "Pricing" },
  { href: "/faq", label: "FAQ" },
  { href: "/about", label: "About" },
] as const;

export function SiteHeader() {
  return (
    <header className="pointer-events-none fixed inset-x-0 top-4 z-50 flex items-center justify-between px-4 md:px-8">
      <nav
        aria-label="Site"
        className="vaaya-pill-bar pointer-events-auto flex items-center gap-6 py-2.5 pl-5 pr-6"
      >
        {/* 32px, so the capsule computes to vaaya's 52px: 32 + 10 top + 10 bottom. */}
        <span className="flex h-8 items-center">
          <Wordmark />
        </span>
        <ul className="hidden items-center gap-1 sm:flex">
          {LINKS.map((link) => (
            <li key={link.href}>
              <Link
                href={link.href}
                className="rounded-full px-3 py-1.5 text-[15px] text-foreground/70 transition-colors hover:bg-foreground/5 hover:text-foreground"
              >
                {link.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>

      <div className="pointer-events-auto flex items-center gap-3">
        <Link
          href="/login"
          className="vaaya-pill hidden h-11 items-center px-5 text-[15px] text-foreground/70 transition-colors hover:text-foreground sm:flex"
        >
          Sign in
        </Link>
        <FloodButton href="/register" size="nav" hoverLabel="Create an account">
          Get started
        </FloodButton>
      </div>
    </header>
  );
}
