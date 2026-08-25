#!/usr/bin/env node
/**
 * Refuses to start a second `next dev` server against a build directory that already has one.
 *
 * Why this exists: two dev servers sharing one `.next` is not a hypothetical. Running
 * `pnpm --filter @baskfy/web run dev --port 3001` and again with `--port 3003` gives two
 * *different ports* but the *same* `.next`, and Next's dev compiler writes chunks there
 * continuously. Each server holds its own in-memory module-id map; the other one overwrites the
 * chunk files underneath it. The page then dies with
 *
 *     Runtime TypeError: __webpack_modules__[moduleId] is not a function
 *         at .next/server/webpack-runtime.js
 *
 * which reads like a source bug and is not one — `next build` of the same tree is clean. The
 * failure is silent at start-up and only surfaces on a request, so it costs an afternoon to
 * diagnose. Cheaper to refuse the second server and say why.
 *
 * A second server is still legitimate (two branches, two ports). It just needs its own build
 * directory: `BASKFY_WEB_DIST_DIR=.next-3003 pnpm run dev --port 3003`. `next.config.ts` reads
 * the same variable, so the two never touch each other's chunks.
 *
 * This process supervises `next dev` rather than exec-ing it, so the lock's PID is a process that
 * is genuinely alive for exactly as long as the server is, and a stale lock (a hard kill, a
 * reboot) is detected by probing that PID rather than by trusting a file.
 */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const WEB_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DIST_DIR = resolve(WEB_DIR, process.env.BASKFY_WEB_DIST_DIR ?? ".next");

/*
 * Deliberately NOT inside the build directory: `next dev` clears that directory as it starts, so a
 * lock kept there is deleted by the very server it is meant to describe. `node_modules/.cache` is
 * already ignored by git and already survives across runs.
 */
const LOCK_DIR = join(WEB_DIR, "node_modules", ".cache", "baskfy-web");
const LOCK = join(LOCK_DIR, `dev-server-${createHash("sha256").update(DIST_DIR).digest("hex").slice(0, 12)}.lock`);

/** Whether a PID is a live process this user owns. `kill(pid, 0)` signals nothing; it only asks. */
function isAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    // EPERM means the PID exists but belongs to someone else — alive for our purposes.
    return error instanceof Error && "code" in error && error.code === "EPERM";
  }
}

/** The lock's contents, or null when it is absent, unreadable or not ours to parse. */
function readLock() {
  if (!existsSync(LOCK)) return null;
  try {
    const parsed = JSON.parse(readFileSync(LOCK, "utf8"));
    return typeof parsed === "object" && parsed !== null ? parsed : null;
  } catch {
    return null;
  }
}

const held = readLock();
if (held && isAlive(held.pid)) {
  const where = held.argv?.length ? ` (${held.argv.join(" ")})` : "";
  process.stderr.write(
    [
      "",
      `  A dev server is already running against ${DIST_DIR}.`,
      `    pid ${held.pid}, started ${held.startedAt}${where}`,
      "",
      "  Two dev servers sharing one build directory overwrite each other's chunks; the",
      "  symptom is `__webpack_modules__[moduleId] is not a function` on a request, not at",
      "  start-up. Refusing to start a second one.",
      "",
      "  To run a second server anyway, give it its own build directory:",
      "    BASKFY_WEB_DIST_DIR=.next-alt pnpm --filter @baskfy/web run dev --port 3003",
      "",
      "  If that pid is dead and this message is wrong, delete the lock:",
      `    rm ${LOCK}`,
      "",
    ].join("\n"),
  );
  process.exit(1);
}

mkdirSync(LOCK_DIR, { recursive: true });
const passthrough = process.argv.slice(2);
writeFileSync(
  LOCK,
  `${JSON.stringify(
    { pid: process.pid, startedAt: new Date().toISOString(), argv: passthrough, distDir: DIST_DIR },
    null,
    2,
  )}\n`,
);

let released = false;
function release() {
  if (released) return;
  released = true;
  // Only clear a lock that is still ours: a racing server may already have replaced it.
  if (readLock()?.pid === process.pid) rmSync(LOCK, { force: true });
}

/* Resolve the workspace binary rather than trusting PATH, so the guard behaves the same whether
   it is run by `pnpm run dev` (which puts `node_modules/.bin` on PATH) or by hand. */
const LOCAL_NEXT = join(WEB_DIR, "node_modules", ".bin", "next");
const NEXT_BIN = existsSync(LOCAL_NEXT) ? LOCAL_NEXT : "next";

const child = spawn(NEXT_BIN, ["dev", ...passthrough], {
  cwd: WEB_DIR,
  stdio: "inherit",
  env: process.env,
  shell: false,
});

for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(signal, () => {
    child.kill(signal);
  });
}
process.on("exit", release);

child.on("error", (error) => {
  release();
  process.stderr.write(`  failed to start next dev: ${error.message}\n`);
  process.exit(1);
});
child.on("exit", (code, signal) => {
  release();
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 0);
});
