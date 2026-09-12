import { resolve } from "node:path";

import createMdx from "@next/mdx";
import remarkGfm from "remark-gfm";
import type { NextConfig } from "next";

/** The slice of webpack's config this file touches. Narrow, so nothing here is `any`. */
interface WebpackConfig {
  resolve?: {
    extensionAlias?: Record<string, string[]>;
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

/**
 * docs/11 §Security pins the response headers; docs/08 §"Design principles" pins the rest.
 *
 * `typedRoutes` is on because a route typo that only shows up as a 404 in staging is exactly the
 * class of bug a typed codebase should not have.
 */
const siteUrl = process.env.NEXT_PUBLIC_SITE_URL ?? "";
const isStagingHost = /staging\./i.test(siteUrl);

const securityHeaders = [
  // docs/11 §Security: "Strict CSP (`default-src 'self'`), HSTS, `X-Content-Type-Options`,
  // `Referrer-Policy`."
  //
  // The CSP itself is *not* here: it carries a per-request nonce, and a static header cannot.
  // `src/middleware.ts` sets it. These four are request-independent and belong at the edge.
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), interest-cohort=()",
  },
  // AUDIT 2.14: HSTS preload from staging would enrol the staging hostname forever; production
  // only.
  ...(isStagingHost
    ? []
    : [
        {
          key: "Strict-Transport-Security",
          value: "max-age=63072000; includeSubDomains; preload",
        },
      ]),
];

/**
 * MDX, for the blog and the legal documents (Prompt 18 §2 and §3).
 *
 * `docs/02` locks no content layer — it is silent on the blog beyond noting that "pricing, blog,
 * instrument pages are exactly the kind of long-tail SEO surface" SSR exists for. Prompt 18 names
 * MDX explicitly, so `@next/mdx` is the Next-native answer and adds no runtime to the client
 * bundle: MDX compiles to React server components at build time. `docs/DECISIONS.md` §18.1.
 *
 * **One remark plugin: `remark-gfm`.** This comment used to say there were none, on the reasoning
 * that "a plugin chain is a second thing that can break a build, and neither the four legal
 * documents nor the three posts need one". The second half was wrong, and silently so: MDX
 * implements CommonMark, and **pipe tables are not CommonMark** — they are a GitHub extension. So
 * `privacy-policy.mdx`'s cookie table and its retention table were being rendered as five lines of
 * literal `| Session token | Keeps you signed in | No |` text on a live legal page, which is where
 * a reader looks to find out what we store and for how long.
 *
 * Nothing about the original argument is abandoned. `remark-gfm` runs at build time and compiles
 * to the same server components, so the "adds no runtime to the client bundle" property in the
 * paragraph above is unchanged — and a plugin that turns a table into a table is not a chain.
 * `apps/web/src/lib/__tests__/legal-drafts.test.ts` now asserts the tables survive the pipeline,
 * so this cannot regress into raw pipes again without a test failing.
 */
const withMdx = createMdx({
  options: { remarkPlugins: [remarkGfm] },
});

const nextConfig: NextConfig = {
  reactStrictMode: true,
  /*
   * `docs/08-aws-architecture.md` §5: "Next.js 15 `output: 'standalone'` in a container. This is
   * the deployment mode the Next team documents as supporting everything; Amplify Hosting is
   * explicitly avoided (SSE buffering, version lag)."
   *
   * Standalone emits `.next/standalone/server.js` with only the `node_modules` the server
   * actually reached, which is what lets the runtime image be `node:22-slim` with no pnpm store
   * and no workspace symlinks inside it — see `infra/docker/Dockerfile.web`.
   *
   * It costs nothing in development: the flag only changes what `next build` writes. Two things
   * it does *not* carry, and the Dockerfile copies them by hand because of it — `public/` and
   * `.next/static/` — which is documented behaviour, not a bug, and the reason a container that
   * boots but serves unstyled HTML is the classic first failure here. `verify-web-image.sh`
   * asserts a real stylesheet loads for exactly that reason.
   */
  output: "standalone",
  /*
   * Standalone traces file dependencies outward from this directory. `apps/web` is one member of
   * a pnpm workspace, so the trace has to be rooted at the workspace root — two levels up, where
   * `pnpm-workspace.yaml` lives — or the server bundle comes out missing `@baskfy/api-client`
   * and every dependency pnpm hoisted into the root `node_modules/.pnpm` store.
   *
   * Resolved rather than left to inference: Next's own guess walks up looking for a lockfile, and
   * inside the Docker build context there is more than one candidate above this directory.
   */
  outputFileTracingRoot: resolve(import.meta.dirname, "../.."),
  // Next 15.5 enables segment explorer devtools by default; when the dev
  // manifest drifts (common after large refactors / HMR), SegmentViewNode fails
  // to resolve and the client webpack runtime throws "reading 'call'".
  experimental: {
    devtoolSegmentExplorer: false,
  },
  /*
   * Normally `.next`. `scripts/dev-guard.mjs` reads the same variable so that a second dev server
   * — a second branch on a second port — gets a build directory of its own. Two dev servers
   * sharing one directory overwrite each other's chunks and the page dies with
   * `__webpack_modules__[moduleId] is not a function`, which looks like a source bug and is not.
   */
  distDir: process.env.BASKFY_WEB_DIST_DIR ?? ".next",
  /* `.mdx` is only a *page* extension for completeness; every MDX file in this repo is imported
     as a component from a `.tsx` route, so the content and the route metadata stay apart. */
  pageExtensions: ["ts", "tsx", "mdx"],
  /*
   * `@baskfy/api-client` ships TypeScript source, not a build artefact: it is generated from
   * `openapi.json` and regenerated by CI, and a compiled `dist/` would be a second thing to keep
   * in step. Next compiles it as part of this app instead.
   */
  transpilePackages: ["@baskfy/api-client"],
  typedRoutes: true,
  poweredByHeader: false,
  eslint: {
    // `pnpm run lint` runs eslint explicitly; running it again inside `next build` doubles the
    // work and hides failures behind a build log.
    ignoreDuringBuilds: true,
  },
  /*
   * Tree 6 IA: old consumer paths → Market · Baskets · Build · Me hubs.
   *
   * The second sentence here used to read "`/baskets` itself is the new catalog (no redirect)",
   * which stopped being true at M48 (`6195b57`): the catalogue is `/discover`, and `/baskets` is
   * one of the sources below. Mirrored by `LEGACY_REDIRECTS` in `lib/nav.ts`.
   */
  redirects() {
    return Promise.resolve([
      /*
       * Google sign-in replaced the whole email/password funnel (`docs/DECISIONS-MERGE.md` M46),
       * so four pages stopped existing. They keep resolving rather than 404ing, because the two
       * that mattered most were *followed out of email*: a verification link and a reset link,
       * sent to people who by definition are trying to get into an account they cannot reach.
       * Landing them on a 404 is the worst possible answer to "I cannot sign in".
       *
       * `permanent: false`. These are not a rename — the destination does something different
       * from what the source promised, and a 308 would be cached in the browser forever against
       * the day any of these paths means something again.
       */
      { source: "/register", destination: "/login", permanent: false },
      { source: "/forgot-password", destination: "/login", permanent: false },
      { source: "/reset-password", destination: "/login", permanent: false },
      { source: "/verify-email", destination: "/login", permanent: false },
      { source: "/dashboard", destination: "/market/today", permanent: true },
      { source: "/market-health", destination: "/market/mood", permanent: true },
      { source: "/listings", destination: "/market/listings", permanent: true },
      { source: "/explore", destination: "/discover", permanent: true },
      /*
       * `/baskets` → `/discover`. The hub was named after the product's taxonomy; it is named
       * after the reader's task now. Every old path keeps resolving, `:slug` included, so a
       * bookmark or a link in an old email still lands on the right shelf.
       */
      { source: "/baskets", destination: "/discover", permanent: true },
      /*
       * These two pointed at `/discover/featured` and `/discover/plan` until `0716204` deleted
       * both pages as orphans, which left the redirects aiming at a 404 — the one thing the
       * paragraph above promises they will never do. They land on the hub instead: it is the
       * catalogue the featured basket was a single shelf of, and the surface the plan page
       * handed off from.
       */
      { source: "/baskets/featured", destination: "/discover", permanent: true },
      { source: "/baskets/plan", destination: "/discover", permanent: true },
      { source: "/baskets/collections", destination: "/discover/collections", permanent: true },
      {
        source: "/baskets/collections/:slug",
        destination: "/discover/collections/:slug",
        permanent: true,
      },
      // Collections shipped under the Tree-6 consumer IA rather than as a second
      // top-level noun. `/collection/:slug` is the shape the brief asked for, so it
      // resolves — the same way every other moved path does.
      { source: "/collection/:slug", destination: "/discover/collections/:slug", permanent: true },
      { source: "/collections", destination: "/discover/collections", permanent: true },
      { source: "/screens", destination: "/build", permanent: true },
      { source: "/screens/new", destination: "/build/new", permanent: false },
      { source: "/screens/:id", destination: "/build/:id", permanent: true },
      { source: "/screens/:id/columns", destination: "/build/:id/columns", permanent: true },
      { source: "/backtests", destination: "/build/backtests", permanent: true },
      { source: "/backtests/:id", destination: "/build/backtests/:id", permanent: true },
      /*
       * PORTFOLIO_REDESIGN.md §2: the money left Me. `Me → Investments | Portfolios | Watchlist`
       * became `Portfolio → Overview | Portfolios | Holdings | Activity | Watchlist`, and Me kept
       * profile, brokers, subscription and security only.
       *
       * Two families of source, all landing in `/portfolio`:
       *
       * - the flat pre-Tree-6 paths (`/investments`, `/portfolios`, `/watchlist`), re-pointed at
       *   the new homes instead of chaining through `/me/*` — the first hop's destination holds
       *   no page any more, and a two-hop redirect is a second thing to keep correct;
       * - the Tree-6 `/me/*` paths themselves, which is what a bookmark from this week is.
       *
       * `/me/investments/:path*` → `/portfolio/:path*` carries the investment detail pages
       * (`[id]`, `[id]/costs`, `[id]/customize`, `[id]/orders`) to `/portfolio/[id]`: post-merge
       * an investment *is* a portfolio, so its detail page is the portfolio detail page (§7).
       * The exact sources are listed before the wildcards, which is the order Next matches in.
       *
       * `/me` itself is deliberately NOT here — see `src/app/(app)/me/page.tsx`.
       */
      { source: "/investments", destination: "/portfolio/overview", permanent: true },
      { source: "/investments/:path*", destination: "/portfolio/:path*", permanent: true },
      { source: "/portfolios", destination: "/portfolio/portfolios", permanent: true },
      { source: "/watchlist", destination: "/portfolio/watchlist", permanent: true },
      { source: "/me/investments", destination: "/portfolio/overview", permanent: true },
      { source: "/me/investments/:path*", destination: "/portfolio/:path*", permanent: true },
      { source: "/me/portfolios", destination: "/portfolio/portfolios", permanent: true },
      { source: "/me/watchlist", destination: "/portfolio/watchlist", permanent: true },
    ]);
  },
  /*
   * The package's own imports are written with the `.js` extensions that NodeNext resolution
   * requires of ESM. The files on disk are `.ts`, so webpack is told the mapping explicitly.
   */
  webpack(config: WebpackConfig): WebpackConfig {
    const resolve = config.resolve ?? {};
    return {
      ...config,
      resolve: {
        ...resolve,
        extensionAlias: { ...resolve.extensionAlias, ".js": [".ts", ".tsx", ".js"] },
      },
    };
  },
  headers() {
    return Promise.resolve([{ source: "/:path*", headers: securityHeaders }]);
  },
};

export default withMdx(nextConfig);
