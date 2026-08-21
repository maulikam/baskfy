import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { chromium } from "@playwright/test";
import { expect, test } from "@playwright/test";

/**
 * The two acceptance criteria that name Lighthouse:
 *
 *   * Prompt 8: "Lighthouse ≥ 95 accessibility on / and /kitchen-sink in both themes."
 *   * Prompt 10: "Lighthouse SEO ≥ 95 on the instrument route."
 *
 * Lighthouse is run as a subprocess against a Chrome started with remote debugging, and the
 * requested category's score is asserted. Both themes are covered by launching with the OS
 * preference set — `prefers-color-scheme` is what `next-themes`' default "system" resolves
 * against, so emulating it is the only way to get Lighthouse to load a page already in dark mode.
 * The SEO run is theme-independent (nothing in the category depends on colour), so it runs once.
 *
 * Kept apart from `accessibility.spec.ts` because it is slow and because a Lighthouse failure and
 * an axe failure want different debugging.
 */
const MINIMUM_SCORE = 95;

type Category = "accessibility" | "seo" | "performance";

const ACCESSIBILITY_PAGES = [
  { path: "/", name: "landing" },
  { path: "/kitchen-sink", name: "kitchen-sink" },
] as const;

/** docs/08 §Routes: "`/instruments/[symbol]` | ISR | SEO-optimised (this is the organic-traffic
 * surface)". CUPID is the instrument the whole specification is worked through. */
const SEO_PAGES = [{ path: "/instruments/CUPID", name: "instrument factsheet" }] as const;

/**
 * Prompt 18's first acceptance criterion:
 *
 *     "All pages are statically generated and score >= 95 on Lighthouse performance and SEO."
 *
 * The statically-generated half is asserted by `e2e/static-generation.spec.ts`, which reads the
 * build manifest; this is the score half. One page per *shape* rather than all eleven — the
 * landing page (the only one that fetches at build time and the only one with a table), a legal
 * document (the longest prose), an MDX blog post (the compiled-content path), and the support page
 * (the only one with a client component on it). A run takes about a minute each, and eleven runs
 * of four identical shapes buys nothing.
 */
const STATIC_PAGES = [
  { path: "/", name: "landing" },
  { path: "/terms-conditions", name: "terms" },
  { path: "/blog/what-a-decile-actually-measures", name: "blog post" },
  { path: "/support", name: "support" },
] as const;

const THEMES = ["light", "dark"] as const;

interface LighthouseAudit {
  id: string;
  score: number | null;
  title: string;
  details?: { items?: { node?: { snippet?: string } }[] };
}

interface LighthouseReport {
  categories: Record<Category, { score: number | null }>;
  audits: Record<string, LighthouseAudit>;
}

async function runLighthouse(
  url: string,
  theme: "light" | "dark",
  category: Category,
): Promise<LighthouseReport> {
  const userDataDir = await mkdtemp(join(tmpdir(), "decile-lh-"));
  const browser = await chromium.launchPersistentContext(userDataDir, {
    headless: true,
    colorScheme: theme,
    args: ["--remote-debugging-port=9222"],
  });

  try {
    /*
     * Put the theme where `next-themes` looks for it, in the profile Lighthouse will attach to.
     *
     * Emulating `prefers-color-scheme` is not enough on its own: Lighthouse opens its own tab and
     * applies its own emulation, so the OS preference this context was launched with may not
     * survive. `localStorage` does — it is per origin, shared across tabs of the same profile —
     * and it is what the theme provider actually reads. Without this the "dark" run would silently
     * measure the light theme and report a passing score for a page it never rendered.
     */
    const primer = await browser.newPage();
    await primer.goto(url);
    await primer.evaluate((value) => {
      window.localStorage.setItem("theme", value);
    }, theme);
    await primer.reload();
    if (theme === "dark") {
      await expect(primer.locator("html")).toHaveClass(/dark/);
    } else {
      await expect(primer.locator("html")).not.toHaveClass(/dark/);
    }
    await primer.close();

    const outputPath = join(userDataDir, "report.json");
    await new Promise<void>((resolve, reject) => {
      const child = spawn(
        "pnpm",
        [
          "exec",
          "lighthouse",
          url,
          "--port=9222",
          `--only-categories=${category}`,
          "--output=json",
          `--output-path=${outputPath}`,
          "--quiet",
          "--chrome-flags=--headless",
        ],
        { stdio: "inherit" },
      );
      child.on("error", reject);
      child.on("exit", (code) => (code === 0 ? resolve() : reject(new Error(`lighthouse exited ${code}`))));
    });
    return JSON.parse(await readFile(outputPath, "utf-8")) as LighthouseReport;
  } finally {
    await browser.close();
    await rm(userDataDir, { recursive: true, force: true });
  }
}

function assertScore(report: LighthouseReport, category: Category, label: string): void {
  const score = (report.categories[category].score ?? 0) * 100;
  const failed = Object.values(report.audits)
    .filter((audit) => audit.score !== null && audit.score < 1)
    .map((audit) => {
      // The offending nodes, not just the audit name: "Links do not have descriptive text" is
      // impossible to act on without knowing which link.
      const nodes = (audit.details?.items ?? [])
        .map((item) => item.node?.snippet)
        .filter((snippet): snippet is string => Boolean(snippet));
      return [`${audit.id}: ${audit.title}`, ...nodes.map((node) => `    ${node}`)].join("\n");
    });

  console.log(`${label} ${category} score: ${score}`);
  expect(score, failed.join("\n")).toBeGreaterThanOrEqual(MINIMUM_SCORE);
}

for (const { path, name } of ACCESSIBILITY_PAGES) {
  for (const theme of THEMES) {
    test(`lighthouse accessibility >= ${MINIMUM_SCORE} on ${name} (${theme})`, async ({
      baseURL,
    }) => {
      test.slow();
      const report = await runLighthouse(`${baseURL}${path}`, theme, "accessibility");
      assertScore(report, "accessibility", `${name} (${theme})`);
    });
  }
}

for (const { path, name } of SEO_PAGES) {
  test(`lighthouse SEO >= ${MINIMUM_SCORE} on ${name}`, async ({ baseURL }) => {
    test.slow();
    const report = await runLighthouse(`${baseURL}${path}`, "light", "seo");
    assertScore(report, "seo", name);
  });
}

for (const { path, name } of STATIC_PAGES) {
  for (const category of ["performance", "seo"] as const) {
    test(`lighthouse ${category} >= ${MINIMUM_SCORE} on ${name}`, async ({ baseURL }) => {
      test.slow();
      const report = await runLighthouse(`${baseURL}${path}`, "light", category);
      assertScore(report, category, name);
    });
  }
}
