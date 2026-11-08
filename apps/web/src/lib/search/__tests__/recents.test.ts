import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installLocalStorage } from "@/test/local-storage";
import {
  clearRecents,
  MAX_RECENTS,
  readRecents,
  rememberRecent,
  type RecentItem,
} from "@/lib/search/recents";

const STOCK: RecentItem = {
  kind: "instrument",
  id: "CUPID",
  title: "CUPID",
  subtitle: "CUPID LIMITED",
};
const BASKET: RecentItem = {
  kind: "basket",
  id: "momentum-scan",
  title: "Momentum Scan",
  subtitle: "Baskfy Engine",
};

/* This environment's `window.localStorage` is a plain empty object, not a `Storage` — see
   `src/test/local-storage.ts`. Installed fresh per test so no state leaks between them. */
beforeEach(() => {
  installLocalStorage();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("recent items", () => {
  it("starts empty and remembers what was opened", () => {
    expect(readRecents()).toEqual([]);
    rememberRecent(STOCK);
    expect(readRecents()).toEqual([STOCK]);
  });

  it("moves a repeat visit to the front instead of duplicating it", () => {
    rememberRecent(STOCK);
    rememberRecent(BASKET);
    rememberRecent(STOCK);
    expect(readRecents().map((item) => item.id)).toEqual(["CUPID", "momentum-scan"]);
  });

  it("distinguishes two kinds that share an id", () => {
    /* A basket slug and a screen public_id could collide; the pair is the identity, not the id. */
    rememberRecent({ kind: "basket", id: "alpha", title: "Alpha" });
    rememberRecent({ kind: "screen", id: "alpha", title: "Alpha" });
    expect(readRecents()).toHaveLength(2);
  });

  it(`keeps at most ${MAX_RECENTS}`, () => {
    for (let n = 0; n < MAX_RECENTS + 4; n += 1) {
      rememberRecent({ kind: "instrument", id: `SYM${n}`, title: `SYM${n}` });
    }
    const kept = readRecents();
    expect(kept).toHaveLength(MAX_RECENTS);
    /* Newest first, so the four oldest are the ones dropped. */
    expect(kept[0]?.id).toBe(`SYM${MAX_RECENTS + 3}`);
  });

  it("clears", () => {
    rememberRecent(STOCK);
    clearRecents();
    expect(readRecents()).toEqual([]);
  });
});

describe("recent items are treated as untrusted input", () => {
  it("drops an entry whose kind is no longer one this app knows", () => {
    /* The stored value was written by an older build of this app, which makes it exactly as
       trustworthy as user input — and an unknown kind would reach `hrefFor` and index undefined. */
    window.localStorage.setItem(
      "baskfy.search.recents.v1",
      JSON.stringify([{ kind: "portfolio", id: "x", title: "X" }, STOCK]),
    );
    expect(readRecents()).toEqual([STOCK]);
  });

  it("survives a value that is not JSON at all", () => {
    window.localStorage.setItem("baskfy.search.recents.v1", "{not json");
    expect(readRecents()).toEqual([]);
  });

  it("survives a value that is JSON but not a list", () => {
    window.localStorage.setItem("baskfy.search.recents.v1", '{"kind":"instrument"}');
    expect(readRecents()).toEqual([]);
  });

  it("survives storage that throws on read", () => {
    /* Safari private browsing, and some embedded webviews. A search box that cannot open because
       a convenience feature threw is a far worse failure than one that has forgotten. */
    vi.spyOn(window.localStorage, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    expect(readRecents()).toEqual([]);
  });

  it("survives storage that throws on write, and still returns the new list", () => {
    vi.spyOn(window.localStorage, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    expect(rememberRecent(STOCK)).toEqual([STOCK]);
  });
});
