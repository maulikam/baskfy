"use client";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";
import type {
  ActivityItem,
  NavRange,
  NavSeries,
  PortfolioDetail,
} from "@/lib/portfolio/overview";

/**
 * What §6.5's inspector drawer reads, and how it reads it **without a page navigation**.
 *
 * §6.5: *"Row click opens a right-side inspector drawer (~40% width, no page navigation)."* The
 * constraint is the feature — comparing four portfolios means opening four drawers, and a route
 * change per row would throw away the table's scroll position and its active tab each time. So
 * the drawer's three tabs are filled by three browser fetches against the API origin, the same
 * way `screens/export-button` reaches it, and the page underneath never unmounts.
 *
 * The three reads are issued together rather than per tab. They are small, they are all wanted
 * within a second of the drawer opening, and one round trip that fills every tab beats a spinner
 * appearing each time the reader clicks a tab heading.
 *
 * Each read fails independently. A portfolio whose activity endpoint is down still shows its
 * performance and its holdings, with a sentence where the activity list would be — the same rule
 * the rest of this screen follows: an absence is reported, never rendered as an empty success.
 */

export interface InspectorData {
  detail: PortfolioDetail | null;
  nav: NavSeries | null;
  activity: readonly ActivityItem[] | null;
  /** One sentence per read that came back with nothing, keyed by tab. */
  failures: { detail: string | null; nav: string | null; activity: string | null };
}

const UNREACHABLE = "This did not load. The API did not answer — try again in a moment.";

async function getJson(path: string): Promise<unknown> {
  const token = await accessToken();
  const response = await fetch(`${apiOrigin()}/api/v1${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) throw new Error(String(response.status));
  return response.json();
}

interface ActivityPayload {
  items?: ActivityItem[];
}

export async function loadInspector(portfolioId: number): Promise<InspectorData> {
  const id = encodeURIComponent(String(portfolioId));
  const [detail, nav, activity] = await Promise.all([
    getJson(`/portfolio/${id}`).catch(() => null),
    getJson(`/portfolio/${id}/nav`).catch(() => null),
    getJson(`/portfolio/activity?portfolio_id=${id}`).catch(() => null),
  ]);

  const activityItems =
    activity === null ? null : ((activity as ActivityPayload).items ?? []);

  return {
    detail: detail === null ? null : (detail as PortfolioDetail),
    nav: nav === null ? null : (nav as NavSeries),
    activity: activityItems,
    failures: {
      detail: detail === null ? UNREACHABLE : null,
      nav: nav === null ? UNREACHABLE : null,
      activity: activity === null ? UNREACHABLE : null,
    },
  };
}

interface OverviewChartPayload {
  chart?: NavSeries;
}

/**
 * §6.3's range pills, without a page navigation either.
 *
 * `GET /portfolio/overview?range=` recomputes the whole payload, and only `chart` differs — the
 * hero metrics and the table rows are range-independent. Re-rendering the entire screen because
 * the reader wanted three months instead of one would reset the grouping tab they had chosen and
 * the section they had scrolled to, for no change in what those parts say. So the fetch happens
 * here and only the chart's state is replaced.
 *
 * `null` on failure. The caller keeps drawing the range it already has and says a new one could
 * not be loaded, rather than blanking a chart that is still true.
 */
export async function loadOverviewChart(range: NavRange): Promise<NavSeries | null> {
  try {
    const payload = (await getJson(
      `/portfolio/overview?range=${encodeURIComponent(range)}`,
    )) as OverviewChartPayload;
    return payload.chart ?? null;
  } catch {
    return null;
  }
}
