import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import { serverFetchJsonOrNull } from "@/lib/api/server-fetch";
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
  "This portfolio's summary did not load. The API did not answer — try again in a moment.";
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

async function tryJson(path: string): Promise<unknown> {
  return serverFetchJsonOrNull({
    url: `${serverApiOrigin()}/api/v1${path}`,
    headers: await authHeaders(),
  });
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

/** §7's three reads, in parallel, each reporting its own absence. */
export async function loadPortfolioDetail(
  portfolioId: number,
  range: NavRange = DEFAULT_DETAIL_RANGE,
): Promise<PortfolioDetailBundle> {
  const id = encodeURIComponent(String(portfolioId));
  const [detail, nav, activity] = await Promise.all([
    tryJson(`/portfolio/${id}`),
    tryJson(`/portfolio/${id}/nav?range=${encodeURIComponent(range)}`),
    tryJson(`/portfolio/activity?portfolio_id=${id}&limit=${ACTIVITY_LIMIT}`),
  ]);

  return {
    detail: detail === null ? null : (detail as PortfolioDetail),
    nav: nav === null ? null : (nav as NavSeries),
    activity: activity === null ? null : ((activity as ActivityPayload).items ?? []),
    failures: {
      detail: detail === null ? UNREACHABLE_DETAIL : null,
      nav: nav === null ? UNREACHABLE_NAV : null,
      activity: activity === null ? UNREACHABLE_ACTIVITY : null,
    },
  };
}
