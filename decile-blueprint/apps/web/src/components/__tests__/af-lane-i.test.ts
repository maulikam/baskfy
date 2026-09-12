import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { filterBaskets } from "@/components/discover/basket-search";
import {
  ACTIVITY_EMPTY_CONNECTED,
  ACTIVITY_EMPTY_DISCONNECTED,
  ACTIVITY_READ_ONLY,
} from "@/components/portfolio/activity-honest-copy";
import type { ExploreBasketCard } from "@/lib/explore/fetch";

const WEB_ROOT = resolve(__dirname, "../../..");

function card(partial: Partial<ExploreBasketCard> & { slug: string; name: string }): ExploreBasketCard {
  return {
    access: "FREE",
    visibility: "LISTED",
    type: "MODEL",
    categories: partial.categories ?? [],
    rebalance_frequency: "MONTHLY",
    source: "ENGINE",
    description_md: partial.description_md ?? null,
    launched_at: null,
    manager: partial.manager ?? { slug: "engine", name: "Baskfy Engine", kind: "ENGINE" },
    metrics: null,
    ...partial,
  };
}

describe("filterBaskets (AF I.3)", () => {
  const items = [
    card({ slug: "broad-market-sharpe", name: "Broad Market Sharpe", categories: ["momentum"] }),
    card({
      slug: "quality-compounders",
      name: "Quality Compounders",
      manager: { slug: "engine", name: "Baskfy Engine", kind: "ENGINE" },
      categories: ["quality"],
    }),
  ];

  it("returns all items for an empty query", () => {
    expect(filterBaskets(items, "  ")).toHaveLength(2);
  });

  it("matches name, slug and category case-insensitively", () => {
    expect(filterBaskets(items, "SHARPE").map((item) => item.slug)).toEqual([
      "broad-market-sharpe",
    ]);
    expect(filterBaskets(items, "quality").map((item) => item.slug)).toEqual([
      "quality-compounders",
    ]);
  });

  it("fails closed when nothing matches (old 'no search' behaviour would still show all)", () => {
    expect(filterBaskets(items, "zzzz-no-such-basket")).toEqual([]);
  });
});

describe("Activity honest copy (AF I.6)", () => {
  const page = readFileSync(
    join(WEB_ROOT, "src/app/(app)/portfolio/activity/page.tsx"),
    "utf8",
  );

  it("keeps the ledger-not-built sentence on the Activity page", () => {
    expect(page).toMatch(/ledger behind it is\s+built/);
    expect(page).not.toMatch(/fake transaction|sample fill|demo trade/i);
  });

  it("exports the honest strings for reuse", () => {
    expect(ACTIVITY_EMPTY_CONNECTED).toMatch(/not recorded yet/);
    expect(ACTIVITY_EMPTY_DISCONNECTED).toMatch(/Once a broker is connected/);
    expect(ACTIVITY_READ_ONLY).toMatch(/Read-only/);
  });
});

describe("BACKLOG titles (AF I.BACKLOG)", () => {
  const backlog = readFileSync(
    resolve(WEB_ROOT, "../../../docs/audit-fix/BACKLOG.md"),
    "utf8",
  );

  it("lists the eight deferred product titles and stays short", () => {
    const lines = backlog
      .trim()
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    expect(lines.length).toBeLessThanOrEqual(15);
    expect(backlog).toMatch(/Manager onboarding/);
    expect(backlog).toMatch(/Web invest \/ exit \/ rebalance plan \(D3-gated\)/);
    expect(backlog).toMatch(/CAS import \+ ledger/);
    expect(backlog).toMatch(/Notifications centre/);
    expect(backlog).toMatch(/Statements \/ tax export/);
    expect(backlog).toMatch(/Fundamentals/);
    expect(backlog).toMatch(/Brokers beyond Zerodha/);
    expect(backlog).toMatch(/Live prices product decision/);
  });
});

describe("versions route exists (AF I.1)", () => {
  it("ships a versions page under basket/[slug]/versions", () => {
    const source = readFileSync(
      join(WEB_ROOT, "src/app/(app)/basket/[slug]/versions/page.tsx"),
      "utf8",
    );
    expect(source).toMatch(/VersionDiffPanel/);
    expect(source).toMatch(/data-testid="basket-versions"/);
  });
});

describe("basket OG image (AF I.5)", () => {
  it("defines an opengraph-image route for baskets", () => {
    const source = readFileSync(
      join(WEB_ROOT, "src/app/(app)/basket/[slug]/opengraph-image.tsx"),
      "utf8",
    );
    expect(source).toMatch(/ImageResponse/);
    expect(source).toMatch(/fetchExploreBasket/);
  });
});

describe("onboarding page (AF I.4)", () => {
  it("ships the connect → import → preferences flow", () => {
    const source = readFileSync(
      join(WEB_ROOT, "src/app/(app)/onboarding/page.tsx"),
      "utf8",
    );
    expect(source).toMatch(/OnboardingWizard/);
  });
});
