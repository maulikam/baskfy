import js from "@eslint/js";
import nextPlugin from "@next/eslint-plugin-next";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

/**
 * Flat config, assembled from the individual plugins rather than from `eslint-config-next`:
 * that package still ships only the eslintrc-era entry point, which loads `@rushstack/eslint-patch`
 * and refuses to run on ESLint 9.39. The rules it would have contributed — `@next/next` and
 * `react-hooks` — are here directly.
 *
 * Prompt 8's fifth acceptance criterion is "Zero `any` in apps/web", so `no-explicit-any` is an
 * error, and so are the escape hatches around it: an unchecked cast or a `@ts-ignore` is `any`
 * with a different spelling. CLAUDE.md house rule 3 says the same for the Python side.
 *
 * `jsx-a11y` is on for the same reason the accessibility criteria exist — Lighthouse catches what
 * it can see rendered, and a lint rule catches the rest before it renders at all.
 */
export default tseslint.config(
  {
    ignores: [
      ".next/**",
      "node_modules/**",
      "next-env.d.ts",
      "playwright-report/**",
      "test-results/**",
      "coverage/**",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  {
    plugins: {
      "@next/next": nextPlugin,
      "react-hooks": reactHooks,
      "jsx-a11y": jsxA11y,
    },
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    rules: {
      ...nextPlugin.configs.recommended.rules,
      ...nextPlugin.configs["core-web-vitals"].rules,
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.flatConfigs.recommended.rules,

      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unsafe-assignment": "error",
      "@typescript-eslint/no-unsafe-member-access": "error",
      "@typescript-eslint/no-unsafe-call": "error",
      "@typescript-eslint/no-unsafe-return": "error",
      "@typescript-eslint/no-unsafe-argument": "error",
      "@typescript-eslint/ban-ts-comment": "error",
      "@typescript-eslint/consistent-type-imports": "error",
      "@typescript-eslint/no-unnecessary-condition": "off",

      /*
       * `<main>` is the app shell's scroll container, and axe's `scrollable-region-focusable`
       * requires a scrollable region to be reachable by Tab (WCAG 2.1.1). This rule's default
       * assumes the opposite for non-interactive elements, so the two disagree; the accessibility
       * audit wins, and the exception is named rather than suppressed at the call site.
       */
      "jsx-a11y/no-noninteractive-tabindex": [
        "error",
        { tags: ["main"], roles: ["tabpanel"], allowExpressionValues: true },
      ],
    },
  },
  {
    /*
     * The `.mjs` config files are outside `tsconfig.json`'s `include` (they are tooling, not
     * application source), so the type-aware rules have no program for them. Type-checking them
     * would mean widening the app's tsconfig to cover its own build tooling.
     */
    files: ["**/*.mjs"],
    ...tseslint.configs.disableTypeChecked,
  },
  {
    /*
     * Build tooling runs under Node, not in a browser: `scripts/bundle-budget.mjs` (Prompt 16
     * deliverable 5) reads the build manifest and writes a measurement, so `process` and
     * `console` are the point of it. Declared here rather than by adding the `globals` package —
     * these are all any script in this directory uses.
     *
     * `document` is the odd one out and it is not a mistake: `scripts/build-brand-svg.mjs` passes
     * a callback to Playwright's `page.evaluate()`, which serialises the function and runs it
     * inside Chromium. The file is Node; that one callback body is not.
     */
    files: ["scripts/**/*.mjs"],
    languageOptions: {
      globals: {
        process: "readonly",
        console: "readonly",
        Buffer: "readonly",
        document: "readonly",
      },
    },
  },
);
