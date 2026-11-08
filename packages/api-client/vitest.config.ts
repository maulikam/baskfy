import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    globals: true,
    environment: "node",
    include: ["test/**/*.test.ts"],
    /* Capped for the same reason as the web app's pool: see the comment there. */
    pool: "forks",
    poolOptions: {
      forks: { maxForks: Number(process.env.VITEST_MAX_FORKS ?? 4), minForks: 1 },
    },
  },
});
