import type { Instrumentation } from "next";

/**
 * Sentry and trace context for the Next.js runtime — PROMPTS.md Prompt 17 deliverable 1,
 * "Sentry for exceptions in all three runtimes".
 *
 * docs/02 §Observability: "OpenTelemetry → Grafana/Tempo/Loki, **Sentry** for errors."
 *
 * ## Server-side only, on purpose
 *
 * This file runs on the server. There is deliberately **no** `instrumentation-client.ts` and
 * **no** `withSentryConfig` wrapper in `next.config.ts`, which is what would pull the Sentry SDK
 * into the browser bundle. docs/11 budgets the screens route at 250 KB gzip of client JS and it is
 * already at roughly 194 KB (`apps/web/scripts/bundle-budget.mjs`); the client SDK plus its
 * replay/tracing integrations would eat most of the remaining headroom for a signal we can get
 * more cheaply later.
 *
 * The consequence is stated plainly rather than hidden: **browser exceptions are not reported.**
 * Server components, route handlers, server actions and middleware are. `docs/DECISIONS.md`
 * §17.12 records the trade and what it would take to reverse it.
 *
 * ## No DSN, no SDK
 *
 * `Sentry.init({ dsn: "" })` is a no-op that still installs every integration. Skipping `init`
 * entirely means a laptop and the Playwright suite carry none of the machinery — and, more to the
 * point, nothing in a test run can accidentally post to a real Sentry project.
 */
export async function register(): Promise<void> {
  const dsn = process.env.SENTRY_DSN ?? "";
  if (!dsn) return;

  // `NEXT_RUNTIME` is "nodejs" or "edge". The Node build is imported dynamically so the edge
  // bundle never pulls it in — importing `@sentry/nextjs` at module scope would.
  const Sentry = await import("@sentry/nextjs");

  Sentry.init({
    dsn,
    environment: process.env.BASKFY_ENVIRONMENT ?? "local",
    release: process.env.BASKFY_RELEASE || undefined,
    // docs/11 §Security keeps a PII inventory (email, name, payment metadata). An exception report
    // is the easiest place for one of them to escape, so this is set explicitly rather than left
    // to the SDK's default.
    sendDefaultPii: false,
    // Traces go to Tempo (docs/02). Two tracers sampling the same request independently produce
    // two disagreeing pictures of it; the API attaches the OTel trace id to its own Sentry events
    // (`baskfy_api.sentry`), which is what joins the two systems.
    tracesSampleRate: 0,
    // The API's structured logs are redacted at the formatter (`baskfy_api.logging`). This is the
    // web app's equivalent: a last pass over anything credential-shaped before it leaves.
    beforeSend(event) {
      if (event.request?.headers) {
        for (const header of ["authorization", "cookie", "x-revalidate-secret"]) {
          if (header in event.request.headers) {
            event.request.headers[header] = "[redacted]";
          }
        }
      }
      if (event.request?.query_string && typeof event.request.query_string === "string") {
        event.request.query_string = event.request.query_string.replace(
          /(token|secret|password|code)=[^&]+/gi,
          "$1=[redacted]",
        );
      }
      return event;
    },
  });
}

/**
 * Next's server-error hook (App Router). Reports an error thrown while rendering a server
 * component, running a server action or handling a route — the cases `register`'s global handlers
 * do not see, because Next catches them itself and renders an error boundary.
 *
 * A no-op when no DSN is configured, for the same reason as above.
 */
export const onRequestError: Instrumentation.onRequestError = async (err, request, context) => {
  if (!process.env.SENTRY_DSN) return;
  const Sentry = await import("@sentry/nextjs");
  Sentry.captureRequestError(err, request, context);
};
