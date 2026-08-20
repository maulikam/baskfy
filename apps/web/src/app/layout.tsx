import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import { headers } from "next/headers";
import type { ReactNode } from "react";

import { Providers } from "@/app/providers";
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

export default async function RootLayout({ children }: { children: ReactNode }) {
  /*
   * The per-request CSP nonce, set by `src/middleware.ts`. Reading it here makes every page
   * dynamically rendered — which they already are, because the app shell reads the session
   * cookie, and because a nonce that was baked into a static page at build time would be the
   * same nonce for every visitor and therefore no protection at all.
   */
  const nonce = (await headers()).get("x-nonce") ?? undefined;

  return (
    /*
     * `suppressHydrationWarning` is required, not incidental: `next-themes` writes the `class` and
     * `style` attributes on <html> from a blocking script before React hydrates, so the server
     * markup and the client's first read differ by design. That script is what prevents the
     * light-then-dark flash (Prompt 8 deliverable 6, "a working dark mode with no FOUC").
     */
    <html lang="en-IN" suppressHydrationWarning className={inter.variable}>
      <body className="antialiased">
        <Providers nonce={nonce}>{children}</Providers>
      </body>
    </html>
  );
}
