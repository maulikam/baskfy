import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { VbtScanRun, VbtScanStatus } from "@/lib/vbt/fetch";
import type { VbtScanResult } from "@/lib/vbt/write";

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

function run(status: VbtScanStatus, overrides: Partial<VbtScanRun> = {}): VbtScanRun {
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

  it("says what the last run did before anything is pressed", () => {
    render(
      <ScanNow
        action={answering({ ok: true, message: SCAN_QUEUED })}
        lastScan={run("DONE")}
      />,
    );

    expect(statusText()).toMatch(/^Last scan finished /);
    expect(statusText()).toContain("IST");
  });

  /**
   * A failed run says the two facts that belong to the reader — nothing changed, and they can
   * press it again — and not the reason, which names jobs and quote sources and is a sentence for
   * whoever can act on it.
   */
  it("says a run did not finish without repeating the reason it gives itself", () => {
    const failed = run("FAILED", {
      error: "ScanNotRunnable: no quote source while ohlcv_daily is mid-write",
    });
    render(<ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={failed} />);

    expect(statusText()).toBe(scanRunLine(failed));
    expect(statusText()).toMatch(/did not finish/);
    expect(statusText()).not.toContain("ScanNotRunnable");
    expect(statusText()).not.toContain("ohlcv_daily");
  });

  it("says nothing at all when no scan has ever been run", () => {
    render(<ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={null} />);

    expect(statusText()).toBe("");
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
    const states: Array<VbtScanRun | null> = [
      null,
      run("QUEUED"),
      run("RUNNING"),
      run("DONE"),
      run("FAILED", { error: "ScanNotRunnable: BASKFY_VBT_SCAN_ENABLED is false" }),
    ];
    for (const lastScan of states) {
      const view = render(
        <ScanNow action={answering({ ok: true, message: SCAN_QUEUED })} lastScan={lastScan} />,
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
