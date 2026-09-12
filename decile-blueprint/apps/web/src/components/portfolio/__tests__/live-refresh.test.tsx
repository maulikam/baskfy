import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LiveRefresh } from "@/components/portfolio/live-refresh";

const refresh = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh }),
}));

vi.mock("@/lib/market/session", () => ({
  isMarketOpen: vi.fn(),
}));

import { isMarketOpen } from "@/lib/market/session";

const mockedOpen = vi.mocked(isMarketOpen);

describe("LiveRefresh", () => {
  beforeEach(() => {
    refresh.mockReset();
    mockedOpen.mockReset();
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => false,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not refresh when the market is open but overlay is off", () => {
    mockedOpen.mockReturnValue(true);
    render(<LiveRefresh overlayActive={false} intervalMs={30_000} />);
    expect(refresh).not.toHaveBeenCalled();
    expect(screen.queryByTestId("live-refresh")).toBeNull();
  });

  it("refreshes and shows copy when market is open and overlay is on", () => {
    mockedOpen.mockReturnValue(true);
    render(<LiveRefresh overlayActive={true} intervalMs={30_000} />);
    expect(refresh).toHaveBeenCalled();
    expect(screen.getByTestId("live-refresh")).toHaveTextContent(/live overlay/i);
  });

  it("does not refresh when overlay is on but the market is closed", () => {
    mockedOpen.mockReturnValue(false);
    render(<LiveRefresh overlayActive={true} intervalMs={30_000} />);
    expect(refresh).not.toHaveBeenCalled();
    expect(screen.queryByTestId("live-refresh")).toBeNull();
  });
});
