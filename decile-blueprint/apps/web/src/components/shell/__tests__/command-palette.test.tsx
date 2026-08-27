import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CommandPalette } from "@/components/shell/command-palette";
import type { CatalogSearchOutcome } from "@/lib/api/search";
import { rememberRecent } from "@/lib/search/recents";
import { installLocalStorage } from "@/test/local-storage";

/**
 * The palette, asserted against `baskfynavrefactorreport` §F11 rather than against its own markup.
 *
 * The claim under test is the one the report made and the old palette did not keep: **one** search
 * covering stocks, indices, baskets and screens, with recent items. So the fetcher is mocked at
 * `lib/api/search` — the API's own contract is asserted in `services/api/tests/test_api_search.py`
 * — and what is asserted here is that all four kinds reach the screen, that choosing one navigates
 * to the right route, and that a second visit is remembered.
 *
 * `QUERY` is "baskets" rather than something catalog-flavoured because it also matches a nav
 * label, so one query exercises all five groups at once.
 */
const QUERY = "baskets";

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, refresh: vi.fn(), prefetch: vi.fn() }),
}));

const searchCatalog = vi.fn<(query: string) => Promise<CatalogSearchOutcome>>();

vi.mock("@/lib/api/search", () => ({
  searchCatalog: (query: string) => searchCatalog(query),
}));

const ALL_FOUR: CatalogSearchOutcome = {
  status: "ok",
  query: QUERY,
  hits: [
    { kind: "instrument", id: "NIFTYCO", title: "NIFTYCO", subtitle: "NIFTYCO LIMITED" },
    { kind: "index", id: "nifty-midcap-150", title: "NIFTY MIDCAP 150", subtitle: "Universe" },
    { kind: "basket", id: "nifty-momentum", title: "Nifty Momentum", subtitle: "Baskfy Engine" },
    { kind: "screen", id: "exmpl0000001", title: "Nifty Movers", subtitle: "Example" },
  ],
};

async function openAndType(text: string) {
  const user = userEvent.setup();
  render(<CommandPalette />);
  await user.keyboard("{Meta>}k{/Meta}");
  const input = await screen.findByPlaceholderText(/Search stocks, indices, baskets, screens/i);
  if (text) await user.type(input, text);
  return user;
}

/**
 * `cmdk` observes its list and scrolls the selected item into view; this jsdom has neither
 * `ResizeObserver` nor `Element.scrollIntoView`. The `ResizeObserver` half is the same stub the
 * screens and table suites install (`results-panel.test.tsx`), for the same reason.
 */
function installCmdkDomStubs() {
  if (typeof window.ResizeObserver === "undefined") {
    Object.defineProperty(window, "ResizeObserver", {
      writable: true,
      configurable: true,
      value: class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    });
  }
  if (typeof Element.prototype.scrollIntoView !== "function") {
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      writable: true,
      configurable: true,
      value: () => {},
    });
  }
}

beforeEach(() => {
  installCmdkDomStubs();
  installLocalStorage();
  push.mockReset();
  searchCatalog.mockReset();
  searchCatalog.mockResolvedValue(ALL_FOUR);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("the ⌘K palette searches the whole catalog", () => {
  it("draws a group for each of the four kinds, plus Go to", async () => {
    await openAndType(QUERY);
    /* Queried as group headings rather than as text: "Baskets" is also a nav label, and a bare
       text match would find two nodes and say nothing useful about which is the heading. */
    await screen.findByText("Stocks");
    const headings = [...document.querySelectorAll("[cmdk-group-heading]")].map(
      (node) => node.textContent,
    );
    expect(headings).toEqual(["Stocks", "Indices", "Baskets", "Screens", "Go to"]);
  });

  it("shows every hit the API returned, not just the stocks", async () => {
    await openAndType(QUERY);
    expect(await screen.findByText("NIFTYCO")).toBeTruthy();
    expect(await screen.findByText("NIFTY MIDCAP 150")).toBeTruthy();
    expect(await screen.findByText("Nifty Momentum")).toBeTruthy();
    expect(await screen.findByText("Nifty Movers")).toBeTruthy();
  });

  it("asks once for the settled query, rather than once per kind", async () => {
    /* The point of the federated endpoint. Four scoped calls per keystroke would satisfy the
       sentence in the report and miss what it was asking for. */
    await openAndType(QUERY);
    await waitFor(() => expect(searchCatalog).toHaveBeenCalledWith(QUERY));
    expect(searchCatalog.mock.calls.filter(([q]) => q === QUERY)).toHaveLength(1);
  });

  it("navigates to the basket route when a basket is chosen", async () => {
    const user = await openAndType(QUERY);
    await user.click(await screen.findByText("Nifty Momentum"));
    expect(push).toHaveBeenCalledWith("/basket/nifty-momentum");
  });

  it("navigates to Build when a screen is chosen", async () => {
    const user = await openAndType(QUERY);
    await user.click(await screen.findByText("Nifty Movers"));
    expect(push).toHaveBeenCalledWith("/build/exmpl0000001");
  });

  it("navigates to the indices dashboard, filtered, when an index is chosen", async () => {
    const user = await openAndType(QUERY);
    await user.click(await screen.findByText("NIFTY MIDCAP 150"));
    expect(push).toHaveBeenCalledWith("/market/today?q=nifty-midcap-150");
  });

  it("opens onto the nav when the box is empty, without asking the API", async () => {
    await openAndType("");
    expect(await screen.findByText("Go to")).toBeTruthy();
    expect(searchCatalog).not.toHaveBeenCalled();
  });
});

describe("recent items", () => {
  it("shows what was opened before, while the box is still empty", async () => {
    rememberRecent({ kind: "basket", id: "momentum-scan", title: "Momentum Scan" });
    await openAndType("");
    expect(await screen.findByText("Recent")).toBeTruthy();
    expect(screen.getByText("Momentum Scan")).toBeTruthy();
  });

  it("remembers a hit that was opened", async () => {
    const user = await openAndType(QUERY);
    await user.click(await screen.findByText("Nifty Momentum"));
    expect(window.localStorage.getItem("baskfy.search.recents.v1")).toContain("nifty-momentum");
  });

  it("hides the Recent group once the user starts typing", async () => {
    rememberRecent({ kind: "basket", id: "momentum-scan", title: "Momentum Scan" });
    await openAndType(QUERY);
    await waitFor(() => expect(screen.queryByText("Recent")).toBeNull());
  });
});

describe("when the search cannot answer", () => {
  it("says so, and still offers page navigation", async () => {
    searchCatalog.mockResolvedValue({ status: "failed" });
    await openAndType(QUERY);
    expect(await screen.findByText(/unavailable right now/i)).toBeTruthy();
    expect(screen.getByText("Go to")).toBeTruthy();
  });

  it("distinguishes an API that has not rolled yet from one that failed", async () => {
    /* The web app and the API deploy separately; "not available here" and "search failed" are
       different states, and a documented 404 is what tells them apart. */
    searchCatalog.mockResolvedValue({ status: "not-implemented" });
    await openAndType(QUERY);
    expect(await screen.findByText(/not available on this server/i)).toBeTruthy();
  });

  it("does not stack \u201cnothing matches\u201d under an error it already explained", async () => {
    /* Two messages saying different things about the same request. The palette says what
       happened; "nothing matches" would claim the query was answered and found nothing. */
    searchCatalog.mockResolvedValue({ status: "failed" });
    await openAndType("zzzzznotanavlabel");
    expect(await screen.findByText(/unavailable right now/i)).toBeTruthy();
    expect(screen.queryByText(/Nothing matches/i)).toBeNull();
  });

  it("does say nothing matches when the search genuinely found nothing", async () => {
    searchCatalog.mockResolvedValue({ status: "ok", query: "zzzzznotanavlabel", hits: [] });
    await openAndType("zzzzznotanavlabel");
    expect(await screen.findByText(/Nothing matches/i)).toBeTruthy();
  });

  it("never renders an answer to a prefix the user has already finished typing", async () => {
    /* A typeahead that renders whatever arrives last shows results for "bask" under "baskets".
       The slow response is released only after the input has moved on. */
    let releaseStale: (outcome: CatalogSearchOutcome) => void = () => undefined;
    const stale = new Promise<CatalogSearchOutcome>((resolve) => {
      releaseStale = resolve;
    });
    searchCatalog.mockImplementation((query) =>
      query === "bask" ? stale : Promise.resolve(ALL_FOUR),
    );

    const user = userEvent.setup();
    render(<CommandPalette />);
    await user.keyboard("{Meta>}k{/Meta}");
    const input = await screen.findByPlaceholderText(/Search stocks, indices, baskets, screens/i);
    await user.type(input, "bask");
    await waitFor(() => expect(searchCatalog).toHaveBeenCalledWith("bask"));
    await user.type(input, "ets");

    releaseStale({
      status: "ok",
      query: "bask",
      hits: [{ kind: "instrument", id: "STALEHIT", title: "STALEHIT", subtitle: null }],
    });

    await waitFor(() => expect(searchCatalog).toHaveBeenCalledWith(QUERY));
    expect(screen.queryByText("STALEHIT")).toBeNull();
  });
});
