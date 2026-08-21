import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end checks for Prompt 8's acceptance criteria.
 *
 * They run against a **production build** (`next start`), not the dev server: the criteria are
 * about layout stability, scroll performance and Lighthouse scores, and every one of those is
 * different under an unminified dev bundle with hot-reload sockets attached. A 60fps measurement
 * taken in dev mode would be a measurement of the dev server.
 */
const PORT = 3100;
const API_PORT = 8100;
const BASE_URL = `http://127.0.0.1:${PORT}`;
const API_URL = `http://127.0.0.1:${API_PORT}`;

/**
 * A database of its own, seeded from the docs/13 reference export.
 *
 * Separate from `BASKFY_TEST_DATABASE_URL` (which the Python suite truncates and re-migrates
 * between modules) so that a `pytest` run and a `playwright` run cannot pull the ground out from
 * under each other.
 */
const DB = "postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_e2e";
const DB_PG = "postgresql://baskfy:baskfy@localhost:5433/baskfy_e2e";
const REDIS = process.env.BASKFY_REDIS_URL ?? "redis://localhost:6380/0";

/** Shared between the two servers: the API verifies what the web app mints (docs/07 §header). */
const JWT_SECRET = "playwright-placeholder-secret-abcdefgh";
const REPO_ROOT = new URL("../../", import.meta.url).pathname;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"]],
  timeout: 120_000,
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    /* A fixed viewport, because the layout-shift and scroll measurements depend on how many rows
       fit on screen. */
    viewport: { width: 1440, height: 900 },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  /*
   * Two servers, in order. The API migrates and seeds its own database before it starts listening,
   * so the whole suite is one command on a clean machine — `make up` for Postgres and Redis, then
   * `pnpm run e2e`. A suite whose setup lives in a README is a suite that stops being run.
   */
  webServer: [
    {
      command: [
        `cd ${REPO_ROOT}services/api`,
        "uv run alembic upgrade head",
        `cd ${REPO_ROOT}`,
        "uv run python -m baskfy_api.seed e2e",
        `uv run uvicorn baskfy_api.app:get_app --factory --port ${API_PORT}`,
      ].join(" && "),
      url: `${API_URL}/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 300_000,
      env: {
        BASKFY_DATABASE_URL: DB,
        BASKFY_REDIS_URL: REDIS,
        BASKFY_JWT_SECRET: JWT_SECRET,
        BASKFY_ENVIRONMENT: "test",
        BASKFY_LOG_JSON: "false",
        BASKFY_LOG_LEVEL: "WARNING",
        // docs/07 caps anonymous traffic at 10/min. A browser suite makes far more than that from
        // one address, and the limiter is asserted in the Python suite instead.
        BASKFY_RATE_LIMIT_ANONYMOUS_PER_MINUTE: "100000",
        BASKFY_RATE_LIMIT_AUTHENTICATED_PER_MINUTE: "100000",
        // The browser calls the API directly (docs/03 §"Request path"), so its origin has to be
        // allowed or every preflight fails. `127.0.0.1` and `localhost` are different origins to
        // a browser, and Playwright uses the first.
        BASKFY_CORS_ORIGINS: JSON.stringify([BASE_URL, `http://localhost:${PORT}`]),
        // Prompt 12. The suite drives plain HTTP, so a `Secure` refresh cookie would never come
        // back; the links in verification and reset emails have to point at *this* web server;
        // and Argon2id at production cost would put ~20 MiB and a few hundred milliseconds on
        // every sign-in in the suite.
        BASKFY_COOKIE_SECURE: "false",
        BASKFY_WEB_ORIGIN: BASE_URL,
        BASKFY_ARGON2_TIME_COST: "1",
        BASKFY_ARGON2_MEMORY_KIB: "8",
        BASKFY_ARGON2_PARALLELISM: "1",
        // Delivery to mailpit (Prompt 12 §3), which `make up` starts. The suite reads the code it
        // was sent out of mailpit's HTTP API, exactly as a developer reads it in the browser.
        BASKFY_EMAIL_TRANSPORT: "smtp",
        BASKFY_SMTP_HOST: "127.0.0.1",
        BASKFY_SMTP_PORT: process.env.BASKFY_SMTP_PORT ?? "1025",
        // docs/11 §Security caps auth endpoints tightly; the suite makes far more from one
        // address, and `test_api_auth.py` asserts the limiter with the real numbers.
        BASKFY_RATE_LIMIT_AUTH_PER_MINUTE: "100000",
      },
    },
    {
      command: `pnpm exec next build && pnpm exec next start --port ${PORT}`,
      url: BASE_URL,
      reuseExistingServer: !process.env.CI,
      timeout: 300_000,
      env: {
        BASKFY_JWT_SECRET: JWT_SECRET,
        AUTH_SECRET: JWT_SECRET,
        BASKFY_DATABASE_URL_PG: DB_PG,
        NEXT_PUBLIC_API_URL: `${API_URL}/api/v1`,
        // Prompt 12 replaced the stub credential check with the real `/auth/*` endpoints, so
        // there is nothing left to opt into: the browser suite registers and signs in for real
        // against the API this config also starts.
      },
    },
  ],
});
