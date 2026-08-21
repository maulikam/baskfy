import Link from "next/link";

import { ThemeToggle } from "@/components/shell/theme-toggle";
import { Button } from "@/components/ui/button";
import { SITE_NAME } from "@/lib/site";

/**
 * The public header. Deliberately not the app shell: docs/08 §"App shell" describes a sidebar for
 * an authenticated tool, and a signed-out visitor reading the refund policy has no use for a
 * Screens link they cannot open.
 */
const LINKS = [
  { href: "/pricing", label: "Pricing" },
  { href: "/faq", label: "FAQ" },
  { href: "/blog", label: "Blog" },
  { href: "/about", label: "About" },
  { href: "/support", label: "Support" },
] as const;

export function SiteHeader() {
  return (
    <header className="border-b border-border">
      <nav
        aria-label="Site"
        className="mx-auto flex h-14 max-w-5xl items-center gap-6 px-6 text-sm"
      >
        <Link href="/" className="font-semibold tracking-tight">
          {SITE_NAME}
        </Link>
        <ul className="hidden gap-5 text-muted-foreground sm:flex">
          {LINKS.map((link) => (
            <li key={link.href}>
              <Link className="hover:text-foreground" href={link.href}>
                {link.label}
              </Link>
            </li>
          ))}
        </ul>
        <div className="ml-auto flex items-center gap-2">
          <ThemeToggle />
          <Button variant="outline" size="sm" asChild>
            <Link href="/login">Sign in</Link>
          </Button>
        </div>
      </nav>
    </header>
  );
}
