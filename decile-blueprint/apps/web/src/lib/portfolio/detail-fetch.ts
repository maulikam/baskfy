import "server-only";

import { cache } from "react";

import { serverApiOrigin } from "@/lib/api/config";
import { ServerFetchStatusError, serverFetchJson } from "@/lib/api/server-fetch";
import { auth } from "@/lib/auth";
import type {
  ActivityItem,
  NavRange,
  NavSeries,
  PortfolioDetail,
} from "@/lib/portfolio/overview";

/**
 * The three server-side reads behind `PORTFOLIO_REDESIGN.md` §7's detail page.
 *
 * `GET /portfolio/{id}` answers §7's summary, holdings and source panel; `GET /portfolio/{id}/nav`
 * answers its performance block; `GET /portfolio/activity?portfolio_id=` answers its activity
 * block. They are issued together because all three are on screen at once — §7 is one page, not a
 * drawer with tabs — and three sequential hops would put the slowest of them in front of the
 * fastest.
 *
 * **Each read fails on its own.** A portfolio whose NAV job has never run still has holdings
 * worth showing, and a portfolio whose activity feed is down still has a summary. So a failure is
 * `null` plus a sentence, never an empty array: "nothing happened here" and "we could not ask"
 * are opposite messages, and rendering the second as the first is how a page lies quietly. This
 * is the same rule `lib/portfolio/fetch.ts` states for §6.6 and `lib/portfolio/inspector.ts`
 * states for the drawer.
 *
 * No write helpers, and there will not be any. §9: a rebalance produces an order plan the reader
 * takes to their broker, and nothing on this page places an order.
 */

export interface PortfolioDetailBundle {
  readonly detail: PortfolioDetail | null;
  readonly nav: NavSeries | null;
  readonly activity: readonly ActivityItem[] | null;
  /**
   * `true` only when the API *answered* that this portfolio does not exist (404). A timeout or a
   * 5xx is `false`: the portfolio may well exist, and the page must say "did not load", not
   * "not a page" (15 Sep 2026 — `/portfolio/6` read "Page not found" on most live refreshes).
   */
  readonly missing: boolean;
  /** One sentence per read that came back with nothing. `null` where the read succeeded. */
  readonly failures: {
    readonly detail: string | null;
    readonly nav: string | null;
    readonly activity: string | null;
  };
}

/** The range the page opens on. §6.3's list; 1D and 1W wait for intraday (§5.1). */
export const DEFAULT_DETAIL_RANGE: NavRange = "1Y";

const UNREACHABLE_DETAIL =
  "This portfolio's summary did not load. The API did not answer in time — try again in a moment.";

/**
 * The budget for these three reads, above the shell's 2.5 s default.
 *
 * They are the page: with no summary there is nothing to paint, so failing fast buys nothing. And
 * they are the heaviest reads the API serves — each loads the whole ledger — on a single-worker API
 * that answers concurrent requests in turn. Measured on staging, 15 Sep 2026, in market hours: one
 * `GET /portfolio/6` took 311–643 ms alone, while a live refresh's burst of calls answered together
 * at 2.1–2.5 s, so the default budget cut the summary off on most refreshes.
 */
export const DETAIL_FETCH_TIMEOUT_MS = 10_000;
const UNREACHABLE_NAV =
  "The end-of-day valuation series did not load, so the chart is missing rather than empty.";
const UNREACHABLE_ACTIVITY =
  "The activity feed did not load. What happened here is missing rather than nothing.";

/** How many activity rows §7 shows before the reader has to go to the Activity tab. */
export const ACTIVITY_LIMIT = 50;

async function authHeaders(): Promise<HeadersInit> {
  const session = await auth();
  const token = session?.accessToken;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

interface Read {
  readonly data: unknown;
  /** The API answered 404 — the one failure that means "there is no such portfolio". */
  readonly notFound: boolean;
}

async function tryJson(path: string): Promise<Read> {
  try {
    const data = await serverFetchJson({
      url: `${serverApiOrigin()}/api/v1${path}`,
      headers: await authHeaders(),
      timeoutMs: DETAIL_FETCH_TIMEOUT_MS,
    });
    return { data, notFound: false };
  } catch (error) {
    return { data: null, notFound: error instanceof ServerFetchStatusError && error.status === 404 };
  }
}

interface ActivityPayload {
  items?: ActivityItem[];
}

/**
 * A numeric portfolio id from a route parameter, or `null`.
 *
 * `GET /portfolio/{portfolio_id}` is declared `Path(ge=1)`, so anything else is not an id this
 * API can answer for. The route also still receives the older investment ids, which are not
 * numeric — see the page, which falls back to the investment ledger for those rather than
 * showing a reader a 404 for a portfolio they can see on the overview.
 */
export function numericPortfolioId(id: string): number | null {
  if (!/^[0-9]+$/.test(id)) return null;
  const parsed = Number.parseInt(id, 10);
  return Number.isSafeInteger(parsed) && parsed >= 1 ? parsed : null;
}

/**
 * §7's three reads, in parallel, each reporting its own absence.
 *
 * Wrapped in React's `cache` so `generateMetadata` and the page share one set of requests per
 * render. They used to issue the same three reads twice, doubling the burst a single-worker API
 * has to answer inside the budget.
 */
export const loadPortfolioDetail = cache(
  async (
    portfolioId: number,
    range: NavRange = DEFAULT_DETAIL_RANGE,
  ): Promise<PortfolioDetailBundle> => {
    const id = encodeURIComponent(String(portfolioId));
    const [detail, nav, activity] = await Promise.all([
      tryJson(`/portfolio/${id}`),
      tryJson(`/portfolio/${id}/nav?range=${encodeURIComponent(range)}`),
      tryJson(`/portfolio/activity?portfolio_id=${id}&limit=${ACTIVITY_LIMIT}`),
    ]);

    return {
      detail: detail.data === null ? null : (detail.data as PortfolioDetail),
      nav: nav.data === null ? null : (nav.data as NavSeries),
      activity:
        activity.data === null ? null : ((activity.data as ActivityPayload).items ?? []),
      missing: detail.notFound,
      failures: {
        detail: detail.data === null ? UNREACHABLE_DETAIL : null,
        nav: nav.data === null ? UNREACHABLE_NAV : null,
        activity: activity.data === null ? UNREACHABLE_ACTIVITY : null,
      },
    };
  },
);
