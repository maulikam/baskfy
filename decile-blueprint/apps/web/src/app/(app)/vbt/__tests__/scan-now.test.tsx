import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { VbtScanRun, VbtScanStatus } from "@/lib/vbt/fetch";
import type { VbtScanResult } from "@/lib/vbt/write";

import type { VbtScanFacts } from "../copy";
import {
  SCAN_ALREADY_RUNNING,
  SCAN_QUEUED,
  SCAN_SIGNED_OUT,
  SCAN_TOO_SOON,
  SCAN_UNAVAILABLE,
  scanRefusal,
  scanRunLine,
} from "../copy";
import { ScanNow } from "../_components/scan-now";

/**
 * "Scan now" on `/vbt` — leaf 5 of `PLAN-SCAN-SYNC.md`.
 *
 * **Built to the contract, not to a running route.** `POST /vbt/scan` answers 202 queued, 409 one
 * already in flight, 429 one a minute; `GET /vbt/scan/{run_id}` carries the state. Leaf 3 builds
 * those. So the action is mocked here exactly as `app/(app)/swing/__tests__/page.test.tsx` mocks
 * `scanNow` — the UI is green before the route exists and stays green when it lands.
 *
 * Four things are asserted, and they are the four the person actually meets:
 *
 *  1. **the button says which of the three happened, in words** — never a status code, never the
 *     service's own sentence, which is written for whoever can fix it;
 *  2. **it is a real button** — a `<button type="submit">` in a real form, disabled while a run is
 *     on its way, with one polite live region that exists in every state;
 *  3. **nothing internal reaches the reader** — the scan `components/twt/__tests__/no-internals`
 *     runs over the sibling sleeve, applied to every state this control can be in;
 *  4. **it polls while a run is on its way and stops when it lands.**
 */

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  usePathname: () => "/vbt",
  useRouter: () => ({ refresh }),
}));

function run(
  status: VbtScanStatus,
  overrides: Partial<VbtScanRun & VbtScanFacts> = {},
): VbtScanRun & VbtScanFacts {
  return {
    id: 11,
    status,
    requested_at: "2026-09-12T08:10:00+00:00",
    finished_at: status === "DONE" || status === "FAILED" ? "2026-09-12T08:12:00+00:00" : null,
    error: null,
    ...overrides,
  };
}

/** An action that answers what the contract says the service answered. */
function answering(result: VbtScanResult) {
  return vi.fn((): Promise<VbtScanResult> => Promise.resolve(result));
}

async function press(): Promise<void> {
  const form = screen.getByTestId("vbt-scan-now");
  /* The submit is synchronous; the action it starts is not. The extra tick flushes the
     transition `useActionState` opens, so the live region has settled before it is read. */
  await act(async () => {
    fireEvent.submit(form);
    await Promise.resolve();
  });
}

function statusText(): string {
  return screen.getByTestId("vbt-scan-status").textContent ?? "";
}

afterEach(() => {
  refresh.mockClear();
  vi.useRealTimers();
});

describe("accessible: the control is a real button and announces itself", () => {
  it("accessible: renders a submit button inside a real form, enabled and focusable", () => {
    render(<ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={null} />);

    const button = within(screen.getByTestId("vbt-scan-now")).getByRole("button", {
      name: "Scan now",
    });
    expect(button.tagName).toBe("BUTTON");
    expect(button).toHaveAttribute("type", "submit");
    expect(button).toBeEnabled();
    // Keyboard reachable, and the ring is the application's own `:focus-visible` rule from
    // globals.css — the control must not remove it or re-invent one.
    expect(button).not.toHaveAttribute("tabindex", "-1");
    expect(button.className).not.toMatch(/outline-none|focus:outline-0/);
    button.focus();
    expect(document.activeElement).toBe(button);
    expect(button.closest("form")).not.toBeNull();
  });

  it("accessible: disables the button and says so while a run is already on its way", () => {
    render(<ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={run("RUNNING")} />);

    const button = screen.getByRole("button", { name: "Scanning…" });
    expect(button).toBeDisabled();
  });

  it("accessible: a queued run disables it too, because a second press could only be refused", () => {
    render(<ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={run("QUEUED")} />);

    expect(screen.getByRole("button", { name: "Scanning…" })).toBeDisabled();
  });

  it("accessible: one polite live region, present before anything has happened", () => {
    render(<ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={null} />);

    const regions = screen.getAllByRole("status");
    expect(regions).toHaveLength(1);
    expect(regions[0]).toHaveAttribute("aria-live", "polite");
    expect(regions[0]).toBe(screen.getByTestId("vbt-scan-status"));
  });

  it("accessible: a finished run is not announced as a failure", () => {
    render(<ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={run("DONE")} />);

    expect(screen.getByTestId("vbt-scan-status").className).toContain("text-muted-foreground");
    expect(screen.getByRole("button", { name: "Scan now" })).toBeEnabled();
  });

  it("accessible: a failed run is coloured with the semantic token, not a palette step", () => {
    render(<ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={run("FAILED")} />);

    expect(screen.getByTestId("vbt-scan-status").className).toContain("text-negative");
  });
});

describe("says: the three answers the contract names, in a reader's words", () => {
  it("says the scan started when the service takes it", async () => {
    render(<ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={null} />);
    await press();

    await waitFor(() => expect(statusText()).toBe(SCAN_QUEUED));
    expect(statusText()).toMatch(/updates on its own/);
  });

  it("says one is already running, rather than showing a refusal", async () => {
    render(
      <ScanNow action={answering({ ok: false, error: scanRefusal(409) })} lastScan={null} />,
    );
    await press();

    await waitFor(() => expect(statusText()).toBe(SCAN_ALREADY_RUNNING));
  });

  it("says the next one can start in about a minute when it is refused for pace", async () => {
    render(
      <ScanNow action={answering({ ok: false, error: scanRefusal(429) })} lastScan={null} />,
    );
    await press();

    await waitFor(() => expect(statusText()).toBe(SCAN_TOO_SOON));
    expect(statusText()).toMatch(/about a minute/);
  });

  it("says nothing changed when the service cannot be reached at all", async () => {
    render(<ScanNow action={answering({ ok: false, error: scanRefusal(0) })} lastScan={null} />);
    await press();

    await waitFor(() => expect(statusText()).toBe(SCAN_UNAVAILABLE));
    expect(statusText()).toMatch(/nothing changed/);
  });

  /* The whole point of mapping the status here: a person reading their own screen is told what
     happened to them, not what a protocol answered. */
  it("says none of it as a status code", () => {
    for (const status of [202, 409, 429, 401, 500, 0]) {
      const sentence = status === 202 ? SCAN_QUEUED : scanRefusal(status);
      expect(sentence, `${status} leaked a number`).not.toMatch(/\b\d{3}\b/);
      expect(sentence.length).toBeGreaterThan(20);
      expect(sentence.endsWith(".")).toBe(true);
    }
  });

  it("says the right sentence for every status the contract can produce", () => {
    expect(scanRefusal(409)).toBe(SCAN_ALREADY_RUNNING);
    expect(scanRefusal(429)).toBe(SCAN_TOO_SOON);
    expect(scanRefusal(401)).toBe(SCAN_SIGNED_OUT);
    expect(scanRefusal(403)).toBe(SCAN_SIGNED_OUT);
    expect(scanRefusal(500)).toBe(SCAN_UNAVAILABLE);
    expect(scanRefusal(0)).toBe(SCAN_UNAVAILABLE);
  });

  /**
   * THE GAP OF 12 SEP 2026. "None of the scan shows when the last scan performed in any
   * strategy" — the plumbing was all here and every state rendered the empty string or a bare
   * stamp. What a reader wants is three facts: when it ran, whether it finished, and whether it
   * found anything. Each is asserted below, in each state it can be in.
   */
  it("says when the last run finished, in the words a person uses", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-12T08:24:00+00:00")); // twelve minutes after it finished
    render(
      <ScanNow
        action={answering({ ok: true, message: SCAN_QUEUED })}
        lastScan={run("DONE", { found: 3 })}
      />,
    );

    expect(statusText()).toBe("Last scanned 12 minutes ago — 3 signals.");
  });

  it("says what it found, so a finished run is not just DONE", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-12T08:24:00+00:00"));
    render(
      <ScanNow
        action={answering({ ok: true, message: SCAN_QUEUED })}
        lastScan={run("DONE", { found: 58 })}
      />,
    );

    expect(statusText()).toContain("58 signals");
    expect(statusText()).not.toMatch(/\bDONE\b/);
  });

  /**
   * Finding nothing is the ordinary result for this strategy, not a fault. It must read as an
   * answer, and it must be distinguishable from a run that did not say — which is why `found`
   * is `number | null` and not a number defaulted to zero.
   */
  it("says a run that found nothing found nothing, and does not read as a failure", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-12T08:24:00+00:00"));
    render(
      <ScanNow
        action={answering({ ok: true, message: SCAN_QUEUED })}
        lastScan={run("DONE", { found: 0 })}
      />,
    );

    expect(statusText()).toBe("Last scanned 12 minutes ago — no signals, which is an ordinary day when the tape is quiet.");
    expect(statusText()).not.toMatch(/fail|error|problem|wrong/i);
    expect(screen.getByTestId("vbt-scan-status").className).toContain(
      "text-muted-foreground",
    );
  });

  it("says only when it ran when the run did not say what it found", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-12T08:24:00+00:00"));
    render(
      <ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={run("DONE")} />,
    );

    expect(statusText()).toBe("Last scanned 12 minutes ago.");
  });

  /**
   * "Two days ago" is not a useful phrase and "17,412 minutes ago" is worse, so past a day the
   * line degrades to the IST wall clock — the zone every schedule this strategy runs on is in.
   */
  it("says the wall clock instead of an age once the run is more than a day old", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-15T08:24:00+00:00"));
    render(
      <ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={run("DONE")} />,
    );

    expect(statusText()).toBe("Last scanned at 12 Sept 2026, 13:42 IST.");
  });

  it("says a scan is queued and a scan is running, and says what happens next", () => {
    const view = render(
      <ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={run("QUEUED")} />,
    );
    expect(statusText()).toBe("Scan queued. This page updates when it finishes.");
    view.unmount();

    render(
      <ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={run("RUNNING")} />,
    );
    expect(statusText()).toBe("Scanning now. This page updates when it finishes.");
  });

  /**
   * A failed run says the two facts that belong to the reader — nothing changed, and they can
   * press it again — and not the reason, which names jobs and quote sources and is a sentence for
   * whoever can act on it.
   */
  it("says a run did not finish, and when, without repeating the reason it gives itself", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-12T08:24:00+00:00"));
    const failed = run("FAILED", {
      error: "ScanNotRunnable: no quote source while ohlcv_daily is mid-write",
    });
    render(<ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={failed} />);

    expect(statusText()).toBe(scanRunLine(failed, { now: Date.now() }));
    expect(statusText()).toMatch(/did not finish/);
    // The three facts that are the reader's: when it stopped, that nothing changed, that they
    // may press again. Not the fourth, which names a job and a table.
    expect(statusText()).toContain("12 minutes ago");
    expect(statusText()).toContain("nothing changed");
    expect(statusText()).toContain("start another");
    expect(statusText()).not.toContain("ScanNotRunnable");
    expect(statusText()).not.toContain("ohlcv_daily");
  });

  /**
   * Before 12 Sep 2026 this said the empty string, which is how a strategy whose rows came from
   * the nightly came to look as though it had never been scanned at all. Two different states,
   * two different sentences.
   */
  it("says no scan has run when none has, rather than saying nothing", () => {
    render(<ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={null} />);

    expect(statusText()).toBe("No scan has run yet.");
  });

  it("names the nightly run when nobody has pressed the button but a session is published", () => {
    render(
      <ScanNow
        action={answering({ ok: true, message: SCAN_QUEUED })}
        lastScan={null}
        session="2026-09-11"
      />,
    );

    expect(statusText()).toBe(
      "No scan has been started from here yet — what is shown is the nightly run's, for the " +
        "11 Sept 2026 session.",
    );
  });

  /**
   * The server renders this control too, and a clock read during render disagrees with the one
   * the browser reads a moment later. `null` is the server's reading and it yields the stamp,
   * which cannot mismatch; the age appears on the pass after hydration.
   */
  it("says the stamp rather than an age when there is no clock to compare against", () => {
    expect(scanRunLine(run("DONE", { found: 3 }), { now: null })).toBe(
      "Last scanned at 12 Sept 2026, 13:42 IST — 3 signals.",
    );
  });
});

/**
 * The scan `components/twt/__tests__/no-internals.test.tsx` runs over `/twt`, applied to every
 * state this control can be in. A route, a column, a file, a host or a setting name on a
 * customer-facing page is the defect of 11 Sep 2026, and it was found by reading a screen rather
 * than by 2,800 unit tests.
 */
const BANNED: ReadonlyArray<{ readonly name: string; readonly pattern: RegExp }> = [
  { name: "an API route", pattern: /\b(GET|POST|PATCH|PUT|DELETE)\s+\//i },
  { name: "an API path", pattern: /\/api\/v\d/i },
  { name: "a host and port", pattern: /https?:\/\/[^\s)]+/i },
  { name: "a source file", pattern: /\b[\w/-]+\.(py|ts|tsx|sql)\b/i },
  { name: "a module path", pattern: /\b(packages|services|src)\/[a-z]/i },
  { name: "a database or payload field", pattern: /\b[a-z][a-z0-9]*(_[a-z0-9]+)+\b/ },
  { name: "a setting or alert name", pattern: /\b[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\b/ },
];

describe("internal: nothing from the inside of the system reaches the reader", () => {
  it("internal: names no route, column, file or setting in any state of the control", () => {
    const states: Array<(VbtScanRun & VbtScanFacts) | null> = [
      null,
      run("QUEUED"),
      run("RUNNING"),
      run("DONE"),
      run("DONE", { found: 0 }),
      run("DONE", { found: 58 }),
      run("FAILED", { error: "ScanNotRunnable: BASKFY_VBT_SCAN_ENABLED is false" }),
    ];
    // Both spellings of every state: with a published session behind it and without, because
    // the sentence differs and the one that names the nightly run is new.
    for (const session of [null, "2026-09-11"]) {
      for (const lastScan of states) {
        const view = render(
          <ScanNow
            action={answering({ ok: true, message: SCAN_QUEUED })}
            lastScan={lastScan}
            session={session}
          />,
        );
        const text = document.body.textContent ?? "";
        for (const { name, pattern } of BANNED) {
          const hit = pattern.exec(text);
          expect(
            hit,
            `${lastScan?.status ?? "no run"}: ${name} reached the screen — "${hit?.[0] ?? ""}"`,
          ).toBeNull();
        }
        view.unmount();
      }
    }
  });

  it("internal: every sentence the button can answer with is free of them too", () => {
    for (const sentence of [
      SCAN_QUEUED,
      SCAN_ALREADY_RUNNING,
      SCAN_TOO_SOON,
      SCAN_SIGNED_OUT,
      SCAN_UNAVAILABLE,
    ]) {
      for (const { name, pattern } of BANNED) {
        expect(pattern.exec(sentence), `${name} in "${sentence}"`).toBeNull();
      }
    }
  });
});

describe("polls: it refreshes while a run is on its way and stops when it lands", () => {
  it("polls the page every five seconds while the run is queued", async () => {
    vi.useFakeTimers();
    render(
      <ScanNow
        action={answering({ ok: true, message: SCAN_QUEUED })}
        lastScan={run("QUEUED")}
      />,
    );

    expect(refresh).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(refresh).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(refresh).toHaveBeenCalledTimes(3);
  });

  it("polls while the run is running, and stops the moment it is done", async () => {
    vi.useFakeTimers();
    const view = render(
      <ScanNow
        action={answering({ ok: true, message: SCAN_QUEUED })}
        lastScan={run("RUNNING")}
      />,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(refresh).toHaveBeenCalledTimes(1);

    view.rerender(
      <ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={run("DONE")} />,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("polls not at all when the last run failed, or when there has never been one", async () => {
    vi.useFakeTimers();
    const view = render(
      <ScanNow
        action={answering({ ok: true, message: SCAN_QUEUED })}
        lastScan={run("FAILED")}
      />,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(refresh).not.toHaveBeenCalled();
    view.unmount();

    render(<ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={null} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(refresh).not.toHaveBeenCalled();
  });

  it("polls nothing once the control is gone, so a navigated-away page holds no timer", async () => {
    vi.useFakeTimers();
    const view = render(
      <ScanNow
        action={answering({ ok: true, message: SCAN_QUEUED })}
        lastScan={run("RUNNING")}
      />,
    );
    view.unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(refresh).not.toHaveBeenCalled();
  });
});
