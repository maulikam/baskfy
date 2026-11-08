/**
 * Timed server-side fetch for RSC pages — Tree 5 / RSC perf.
 *
 * RSC navigations were hanging 5–20s when the API was slow or unreachable because
 * `fetch(..., { cache: "no-store" })` had no AbortSignal. List/shell pages must fail
 * fast into empty/error UI rather than block the whole flight.
 *
 * Default budget: 2500ms (under the ~1–2s user-facing target once the API is warm;
 * cold starts still abort so the shell paints).
 */
import "server-only";

/** Default RSC→API hop budget (ms). Overridable via `BASKFY_SERVER_FETCH_TIMEOUT_MS`. */
export const SERVER_FETCH_TIMEOUT_MS: number = (() => {
  const raw = process.env.BASKFY_SERVER_FETCH_TIMEOUT_MS?.trim();
  if (!raw) return 2500;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 2500;
})();

export class ServerFetchTimeoutError extends Error {
  readonly path: string;
  readonly timeoutMs: number;

  constructor(path: string, timeoutMs: number) {
    super(`Timed out after ${timeoutMs}ms fetching ${path}`);
    this.name = "ServerFetchTimeoutError";
    this.path = path;
    this.timeoutMs = timeoutMs;
  }
}

/**
 * A non-OK response, with the status and the problem body kept rather than flattened into a
 * message string.
 *
 * `gates/sleeve-read-contract.md` C7: the sleeves used to see `Error("… responded 404")` here
 * and could only answer `null` to it, so a refusal, a degraded deployment and an empty database
 * arrived at every page as the same value. Keeping the body is what lets
 * `@/lib/api/sleeve-read` tell them apart. Still an `Error`, so every existing `catch` keeps
 * behaving exactly as it did.
 */
export class ServerFetchStatusError extends Error {
  readonly url: string;
  readonly status: number;
  /** The parsed problem+json body, or `null` when the response was not JSON. */
  readonly problem: Record<string, unknown> | null;

  constructor(url: string, status: number, problem: Record<string, unknown> | null) {
    super(`${url} responded ${status}`);
    this.name = "ServerFetchStatusError";
    this.url = url;
    this.status = status;
    this.problem = problem;
  }
}

async function readProblem(response: Response): Promise<Record<string, unknown> | null> {
  try {
    const body: unknown = await response.json();
    return typeof body === "object" && body !== null
      ? (body as Record<string, unknown>)
      : null;
  } catch {
    /* A gateway's HTML error page, or an empty body. There is nothing to tell apart. */
    return null;
  }
}

export type ServerFetchJsonOptions = {
  /** Absolute or origin-relative URL. */
  url: string;
  headers?: HeadersInit;
  /** Override default timeout. */
  timeoutMs?: number;
  /** Passed to fetch; default `no-store` for trading-sensitive pages. */
  cache?: RequestCache;
};

/**
 * GET JSON with AbortSignal.timeout. Throws on non-OK, abort, or network error.
 */
export async function serverFetchJson(options: ServerFetchJsonOptions): Promise<unknown> {
  const timeoutMs = options.timeoutMs ?? SERVER_FETCH_TIMEOUT_MS;
  const signal = AbortSignal.timeout(timeoutMs);
  let response: Response;
  try {
    response = await fetch(options.url, {
      method: "GET",
      ...(options.headers ? { headers: options.headers } : {}),
      cache: options.cache ?? "no-store",
      signal,
    });
  } catch (error) {
    if (error instanceof Error && (error.name === "TimeoutError" || error.name === "AbortError")) {
      throw new ServerFetchTimeoutError(options.url, timeoutMs);
    }
    throw error;
  }
  if (!response.ok) {
    throw new ServerFetchStatusError(
      options.url,
      response.status,
      await readProblem(response),
    );
  }
  return response.json();
}

/**
 * Same as {@link serverFetchJson} but returns `null` on timeout / network / non-OK —
 * for investor surfaces that prefer empty states over throwing.
 */
export async function serverFetchJsonOrNull(
  options: ServerFetchJsonOptions,
): Promise<unknown> {
  try {
    return await serverFetchJson(options);
  } catch {
    return null;
  }
}

/** Wrap undici/global fetch with a default timeout for openapi-fetch clients. */
export function timedFetch(
  timeoutMs: number = SERVER_FETCH_TIMEOUT_MS,
): (request: Request) => Promise<Response> {
  return (request: Request): Promise<Response> => {
    const parent = request.signal;
    const budget = AbortSignal.timeout(timeoutMs);
    // AbortSignal.any is Node 20+ / modern browsers; fall back to budget-only if missing.
    const signals = AbortSignal as typeof AbortSignal & {
      any?: (signals: AbortSignal[]) => AbortSignal;
    };
    const signal =
      parent && typeof signals.any === "function" ? signals.any([parent, budget]) : budget;
    return fetch(new Request(request, { signal }));
  };
}
