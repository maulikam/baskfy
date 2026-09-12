import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

describe("AFH 5.7 Saved redirects to Watchlist", () => {
  it("discover/saved is a redirect, not a second list", () => {
    const page = readFileSync(join(__dirname, "..", "saved", "page.tsx"), "utf8");
    expect(page).toContain('redirect("/portfolio/watchlist")');
    expect(page).not.toContain("fetchWatchlist");
  });
});

describe("AFH 5.8 catalogue and listings pagination", () => {
  it("catalogue pages with offset", () => {
    const page = readFileSync(join(__dirname, "..", "all", "page.tsx"), "utf8");
    expect(page).toContain("offset");
    expect(page).toContain("PAGE_SIZE");
    expect(page).toContain("catalogue-pagination");
  });

  it("listings expose Previous and a page count", () => {
    const page = readFileSync(
      join(__dirname, "..", "..", "market", "listings", "page.tsx"),
      "utf8",
    );
    expect(page).toContain("listings-prev");
    expect(page).toContain("listings-page-count");
    expect(page).toContain("Previous");
  });
});
