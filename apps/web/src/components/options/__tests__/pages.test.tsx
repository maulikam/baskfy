import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as LiveMarksModule from "@/lib/screens/live-marks";
import {
  acMorning,
  collectorOff,
  emptyChain,
  emptyJournal,
  unseededConfig,
} from "@/lib/options/__tests__/fixtures";

import OptionsSettingsPage from "@/app/(app)/me/options/page";
import OptionsCalendarPage from "@/app/(app)/options/calendar/page";
import OptionsJournalPage from "@/app/(app)/options/journal/page";
import OptionsPage from "@/app/(app)/options/page";

/**
 * The options tab's other pages, and the rule every sleeve's pages keep: **no internal name
 * reaches the screen** — no API path, no field or column name, no environment variable, no raw
 * state code (the TWT `no-internals` rule, 11 Sep 2026). A reader is told "the options collector
 * is off", never the name of the switch.
 */

vi.mock("@/lib/screens/live-marks", async (importOriginal) => {
  const actual = await importOriginal<typeof LiveMarksModule>();
  return { ...actual, useLiveMarks: () => actual.EMPTY_LIVE_MARKS };
});

vi.mock("@/lib/options/fetch", () => ({
  fetchOptionsToday: vi.fn(),
  fetchOptionsChain: vi.fn(),
  fetchOptionsJournal: vi.fn(),
  fetchOptionsBacktest: vi.fn(),
  fetchOptionsCalendar: vi.fn(),
  fetchOptionsConfig: vi.fn(),
}));

vi.mock("@/app/(app)/options/calendar/actions", () => ({
  addEventDay: vi.fn(),
  removeEventDay: vi.fn(),
}));
vi.mock("@/app/(app)/me/options/actions", () => ({
  saveOptionsConfig: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/options",
  useRouter: () => ({ refresh: vi.fn() }),
}));

const fetches = await import("@/lib/options/fetch");

const BANNED: ReadonlyArray<{
  readonly name: string;
  readonly pattern: RegExp;
}> = [
  { name: "an API path", pattern: /\/api\/v\d/i },
  { name: "a source file", pattern: /\b[\w/-]+\.(py|ts|tsx|sql)\b/i },
  {
    name: "a database or payload field",
    pattern: /\b[a-z][a-z0-9]*(_[a-z0-9]+)+\b/,
  },
  {
    name: "a setting, alert or state code",
    pattern: /\b[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\b/,
  },
];

function scan(label: string): void {
  const text = document.body.textContent ?? "";
  for (const { name, pattern } of BANNED) {
    const hit = pattern.exec(text);
    expect(
      hit,
      `${label}: ${name} reached the screen — "${hit?.[0] ?? ""}"`,
    ).toBeNull();
  }
}

afterEach(() => cleanup());

describe("no internal name reaches an options page", () => {
  it("/options, collector off and on the AC morning", async () => {
    vi.mocked(fetches.fetchOptionsChain).mockResolvedValue(emptyChain);
    for (const [label, today] of [
      ["collector off", collectorOff],
      ["AC morning", acMorning],
    ] as const) {
      vi.mocked(fetches.fetchOptionsToday).mockResolvedValue(today);
      render(await OptionsPage());
      scan(label);
      cleanup();
    }
  });

  it("/options/journal says not run yet and shows the paper-period progress", async () => {
    vi.mocked(fetches.fetchOptionsJournal).mockResolvedValue(emptyJournal);
    vi.mocked(fetches.fetchOptionsBacktest).mockResolvedValue({
      runs: [],
      reason: "not run",
    });
    render(await OptionsJournalPage());
    expect(screen.getByTestId("options-backtest-empty")).toHaveTextContent(
      /Not run yet/,
    );
    expect(screen.getByTestId("options-journal-empty")).toBeInTheDocument();
    expect(screen.getByTestId("options-progress")).toHaveTextContent(
      "17 of 60 sessions, 9 of 25 traded",
    );
    expect(screen.getByTestId("options-fno-caveat")).toBeInTheDocument();
    scan("journal");
  });

  it("/options/calendar reads the expiries and offers remove only on a person's own day", async () => {
    vi.mocked(fetches.fetchOptionsCalendar).mockResolvedValue({
      year: 2026,
      expiries: collectorOff.expiries,
      event_days: [
        {
          date: "2026-10-06",
          reason: "RBI_POLICY",
          source: "SEED",
          source_url: null,
          note: null,
          removable: false,
        },
        {
          date: "2026-10-20",
          reason: "MANUAL",
          source: "USER",
          source_url: null,
          note: "state results",
          removable: true,
        },
      ],
    });
    render(await OptionsCalendarPage());
    expect(screen.getByTestId("options-calendar-expiries")).toHaveTextContent(
      /monthly · lot 75/,
    );
    expect(screen.getAllByRole("button", { name: "Remove" })).toHaveLength(1);
    expect(screen.getByTestId("options-event-day-form")).toBeInTheDocument();
    scan("calendar");
  });

  it("/me/options, not yet set up: says so, shows execution off, and the rules read-only", async () => {
    vi.mocked(fetches.fetchOptionsConfig).mockResolvedValue(unseededConfig);
    render(await OptionsSettingsPage());
    expect(screen.getByTestId("options-not-seeded")).toBeInTheDocument();
    expect(screen.getByTestId("options-execution")).toHaveTextContent(
      /execution disabled on this server — paper only/,
    );
    expect(
      screen.queryByTestId("options-settings-form"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("options-thresholds")).toHaveTextContent(
      "er max",
    );
    scan("settings");
  });
});
