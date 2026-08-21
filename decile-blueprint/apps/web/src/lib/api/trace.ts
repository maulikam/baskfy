import "server-only";

import { trace } from "@opentelemetry/api";

/**
 * The web end of PROMPTS.md Prompt 17 §1's "traces across web → API → worker → database".
 *
 * Next.js 15 runs its own OpenTelemetry instrumentation for App Router renders, so a server
 * component render is already inside a span. What is missing is carrying that span across the
 * HTTP hop to the API — `fetch` does not propagate trace context on its own unless the SDK's
 * fetch instrumentation is installed, and it is not (there is no `@vercel/otel` here, and adding
 * one would be a dependency `docs/02` does not lock).
 *
 * So the header is built by hand. It is nine lines and it is the whole of the hop:
 * `FastAPIInstrumentor` on the other side extracts `traceparent` and parents its span under this
 * one, and the API's own `traceparent` then reaches the worker through the Celery message
 * (`baskfy_api.telemetry`).
 *
 * Returns `undefined` when nothing is tracing, which is the normal case on a laptop and in the
 * test suite — the client then simply omits the header.
 */
export function currentTraceparent(): string | undefined {
  const context = trace.getActiveSpan()?.spanContext();
  if (!context) return undefined;
  // W3C traceparent: version "00", 32-hex trace id, 16-hex span id, 2-hex flags.
  // `traceFlags & 1` is the sampled bit; anything else in that byte is not ours to interpret.
  const flags = (context.traceFlags & 1) === 1 ? "01" : "00";
  return `00-${context.traceId}-${context.spanId}-${flags}`;
}
