import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The home data layer — SC9.
 *
 * The claim under test is the one that decides whether `/home` is usable on a bad day: **a
 * failing upstream costs one module, never the page**. Home is where a signed-in person lands,
 * and a landing surface that 500s because the ranking job is behind is worse than one that says
 * "rankings are not available right now".
 *
 * `serverFetchJsonOrNull` already swallows timeout / network / non-OK into `null`; these assert
 * that every helper turns that `null` into an honest empty shape, and that the collections read
 * — which comes from a module that *throws* — is caught rather than propagated.
 */

const jsonOrNull = vi.fn<(options: unknown) => Promise<unknown>>();
const collections = vi.fn<() => Promise<{ items: unknown[] }>>();
const investments = vi.fn<() => Promise<unknown>>();

class ExploreUnavailable extends Error {}

vi.mock("@/lib/api/config", () => ({ apiOrigin: () => "http://api.test" }));
vi.mock("@/lib/auth", () => ({ auth: () => Promise.resolve({ accessToken: "t0ken" }) }));
vi.mock("@/lib/api/server-fetch", () => ({
  serverFetchJsonOrNull: (options: unknown): Promise<unknown> => jsonOrNull(options),
}));
vi.mock("@/lib/collections/fetch", () => ({
  ExploreUnavailable,
  fetchCollections: (): Promise<{ items: unknown[] }> => collections(),
}));
vi.mock("@/lib/investments/fetch", () => ({
  fetchInvestments: (): Promise<unknown> => investments(),
}));

const EMPTY_INVESTMENTS = { items: [], total: 0, net_worth: null, pending_actions: [] };

beforeEach(() => {
  vi.clearAllMocks();
  jsonOrNull.mockResolvedValue(null);
  collections.mockResolvedValue({ items: [] });
  investments.mockResolvedValue(EMPTY_INVESTMENTS);
});

describe("each read degrades on its own", () => {
  it("returns the empty ranking rather than throwing when /cb/trending is unreachable", async () => {
    const { fetchTrending, EMPTY_TRENDING } = await import("@/lib/home/fetch");
    jsonOrNull.mockResolvedValue(null);
    expect(await fetchTrending()).toEqual(EMPTY_TRENDING);
  });

  it("fills in missing envelope fields rather than handing the UI undefined", async () => {
    const { fetchTrending } = await import("@/lib/home/fetch");
    jsonOrNull.mockResolvedValue({ items: [{ key: "TOP_1Y" }] });
    const trending = await fetchTrending();
    expect(trending.items).toHaveLength(1);
    expect(trending.min_entries).toBe(0);
    expect(trending.return_convention_note).toBe("");
  });

  it("catches the collections module's throw and renders no shelves", async () => {
    const { fetchHomeCollections } = await import("@/lib/home/fetch");
    collections.mockRejectedValue(new ExploreUnavailable("down"));
    expect(await fetchHomeCollections()).toEqual([]);
  });

  it("does not swallow an error that is not an unreachable upstream", async () => {
    const { fetchHomeCollections } = await import("@/lib/home/fetch");
    collections.mockRejectedValue(new TypeError("a real bug"));
    await expect(fetchHomeCollections()).rejects.toBeInstanceOf(TypeError);
  });

  it("returns no updates rather than throwing, and caps the strip", async () => {
    const { fetchUpdates } = await import("@/lib/home/fetch");
    jsonOrNull.mockResolvedValue(null);
    expect(await fetchUpdates()).toEqual([]);

    jsonOrNull.mockResolvedValue({
      items: [
        { id: 1, title: "a", body_md: "", published_at: "2026-08-01", source: "ENGINE" },
        { id: 2, title: "b", body_md: "", published_at: "2026-08-02", source: "ENGINE" },
        { id: 3, title: "c", body_md: "", published_at: "2026-08-03", source: "ENGINE" },
        { id: 4, title: "d", body_md: "", published_at: "2026-08-04", source: "ENGINE" },
      ],
    });
    const updates = await fetchUpdates();
    expect(updates).toHaveLength(3);
    // Ids arrive as bigints; the UI keys on strings.
    expect(updates[0]?.id).toBe("1");
  });
});

describe("the page as a whole", () => {
  it("still renders every module when every upstream is down", async () => {
    const { fetchHome, EMPTY_TRENDING } = await import("@/lib/home/fetch");
    jsonOrNull.mockResolvedValue(null);
    collections.mockRejectedValue(new ExploreUnavailable("down"));

    const snapshot = await fetchHome();
    expect(snapshot.investments).toEqual(EMPTY_INVESTMENTS);
    expect(snapshot.trending).toEqual(EMPTY_TRENDING);
    expect(snapshot.collections).toEqual([]);
    expect(snapshot.updates).toEqual([]);
  });

  it("reads net worth and pending actions from the investments payload, not a second source", async () => {
    const { fetchHome } = await import("@/lib/home/fetch");
    investments.mockResolvedValue({
      items: [],
      total: 0,
      net_worth: "253000.00",
      pending_actions: [{ id: "7", type: "DRIFT", title: "Fix now", body: null }],
    });

    const snapshot = await fetchHome();
    expect(snapshot.investments.net_worth).toBe("253000.00");
    expect(snapshot.investments.pending_actions).toHaveLength(1);
    // The only calls this module makes on its own are the two it owns.
    const paths = jsonOrNull.mock.calls.map(
      (call) => (call[0] as { url: string }).url.split("/api/v1")[1],
    );
    expect(paths.sort()).toEqual(["/cb/trending", "/cb/updates"]);
  });

  it("issues its reads concurrently, not one after another", async () => {
    const { fetchHome } = await import("@/lib/home/fetch");
    let inFlight = 0;
    let peak = 0;
    jsonOrNull.mockImplementation(async () => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await new Promise((resolve) => setTimeout(resolve, 5));
      inFlight -= 1;
      return null;
    });
    await fetchHome();
    expect(peak).toBeGreaterThan(1);
  });
});

describe("home stays a read-only surface", () => {
  const HOME_DIRS = [
    join(__dirname, ".."),
    join(__dirname, "..", "..", "..", "components", "home"),
    join(__dirname, "..", "..", "..", "app", "(app)", "home"),
  ];

  function filesUnder(dir: string): string[] {
    try {
      return readdirSync(dir).flatMap((entry) => {
        const path = join(dir, entry);
        return statSync(path).isDirectory() ? filesUnder(path) : [path];
      });
    } catch {
      return [];
    }
  }

  const SOURCES = HOME_DIRS.flatMap(filesUnder).filter(
    (path) =>
      (path.endsWith(".ts") || path.endsWith(".tsx")) && !path.includes("__tests__"),
  );

  it("opens enough files to be a real sweep", () => {
    // Declared: a sweep that silently found nothing is not a sweep.
    expect(SOURCES.length).toBeGreaterThanOrEqual(6);
  });

  it("has no order-shaped affordance anywhere on it", () => {
    const banned = ["place_order", "/execute", "confirm=true", "OrderGateway"];
    const offenders: string[] = [];
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8");
      for (const needle of banned) {
        if (source.includes(needle)) offenders.push(`${path}: ${needle}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
