import "@testing-library/jest-dom/vitest";

import * as React from "react";
import { vi } from "vitest";

/*
 * `next/dynamic` under jsdom.
 *
 * Components split out with `next/dynamic` — docs/11's "after code-splitting the table and
 * charts", done for `/build/[id]` in `results-panel.tsx` — never resolve in this environment:
 * Next's loader depends on the App Router runtime, so the subtree never mounts. That fails every
 * assertion about it and, far worse, makes every *negative* assertion about it pass for the wrong
 * reason. `expect(priceHeader()).not.toBeInTheDocument()` is green when the column is correctly
 * dropped and equally green when no table exists at all.
 *
 * This resolves the same module the app would load, through `React.lazy`, so a test exercises the
 * component rather than Next's loading machinery. It is asynchronous by nature, so a test that
 * asserts on a split subtree must await it (`findBy*`); that is the only behavioural difference,
 * and it is the truthful one.
 *
 * Whether the split actually happened is asserted where it is observable — `bundle-budget.mjs`
 * against a real build, `gates/leaf-7.5.1-verify.md` G6 — never here.
 */
vi.mock("next/dynamic", () => ({
  default: (
    loader: () => Promise<
      { default: React.ComponentType<unknown> } | React.ComponentType<unknown>
    >,
  ) => {
    const Lazy = React.lazy(async () => {
      const mod = await loader();
      return "default" in mod ? mod : { default: mod };
    });
    return function DynamicUnderTest(props: Record<string, unknown>) {
      return React.createElement(React.Suspense, { fallback: null }, React.createElement(Lazy, props));
    };
  },
}));
