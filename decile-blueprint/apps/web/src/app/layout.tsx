import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import type { ReactNode } from "react";

import { ConsentBanner } from "@/components/consent/consent-banner";
import { SITE_DESCRIPTION, SITE_NAME, SITE_TAGLINE, SITE_URL } from "@/lib/site";

import "./globals.css";

/**
 * docs/08 §"Design principles": "one sans for UI (Inter or Geist)".
 *
 * `next/font` self-hosts the file and emits `size-adjust` fallback metrics, so the fallback and
 * the real font occupy the same space — the layout does not move when the webfont arrives, which
 * is Prompt 8's fourth acceptance criterion applied to type.
 */
const inter = Inter({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-inter",
});

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: `${SITE_NAME} — momentum, ranked`,
    template: `%s · ${SITE_NAME}`,
  },
  description: SITE_DESCRIPTION,
  applicationName: SITE_NAME,
  /* Prompt 18 §4, "canonical URLs". Every page inherits `/` and overrides it with its own path;
     `src/app/__tests__/canonical.test.ts` asserts that no public route forgets to. */
  alternates: { canonical: "/" },
  openGraph: {
    type: "website",
    siteName: SITE_NAME,
    title: `${SITE_NAME} — momentum, ranked`,
    description: SITE_TAGLINE,
    url: SITE_URL,
    locale: "en_IN",
  },
  twitter: { card: "summary_large_image", title: SITE_NAME, description: SITE_TAGLINE },
  robots: { index: true, follow: true },
  formatDetection: { telephone: false, address: false, email: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  /* The browser chrome follows the theme, so the toggle does not leave a white status bar. */
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fbfbfd" },
    { media: "(prefers-color-scheme: dark)", color: "#0b0d11" },
  ],
};

/**
 * The document, and nothing else.
 *
 * **This layout reads nothing per request, on purpose.** It used to read the CSP nonce out of the
 * request headers, and a `headers()` call in the root layout opts *every* route in the app into
 * dynamic rendering — which made docs/08 §Routes' "`/`, `/pricing`, `/faq`, `/about`, `/blog/*`,
 * legal | SSG" impossible to satisfy and is Prompt 18's first acceptance criterion. The nonce is
 * now read by `(app)/layout.tsx` and `(auth)/layout.tsx`, which are dynamic anyway because they
 * read the session cookie; the statically generated `(marketing)` group supplies no nonce and is
 * served the hash-free static policy `src/middleware.ts` gives those routes (`docs/DECISIONS.md`
 * §18.2).
 *
 * The consent banner sits here rather than in a group layout because docs/11 §Compliance's DPDP
 * obligation is not route-scoped: it is the same promise on the marketing page and inside the
 * app.
 */
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    /*
     * `suppressHydrationWarning` is required, not incidental: `next-themes` writes the `class` and
     * `style` attributes on <html> from a blocking script before React hydrates, so the server
     * markup and the client's first read differ by design. That script is what prevents the
     * light-then-dark flash (Prompt 8 deliverable 6, "a working dark mode with no FOUC").
     */
    <html lang="en-IN" suppressHydrationWarning className={inter.variable}>
      <body className="antialiased">
        {children}
        <ConsentBanner />
      </body>
    </html>
  );
}
