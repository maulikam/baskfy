import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  BUMPINESS_THRESHOLDS,
  BumpinessDots,
  RankBadge,
  REFERENCE_SCORE_SCALE,
  ScoreBar,
  bumpinessBand,
  scoreBarScale,
} from "@/components/screens/cell-encodings";

describe("scoreBarScale", () => {
  it("derives the scale from the rows on screen", () => {
    expect(scoreBarScale([1, 2, 4])).toBe(4);
    expect(scoreBarScale([0.5, 1.5])).toBe(1.5);
  });

  it("falls back to REFERENCE_SCORE_SCALE when nothing positive is present", () => {
    expect(scoreBarScale([0, -1, Number.NaN])).toBe(REFERENCE_SCORE_SCALE);
  });
});

describe("ScoreBar proportional widths", () => {
  it("two scores above 3 paint different widths when scaled to the row set", () => {
    const scale = scoreBarScale([3.5, 5.13]);
    const { rerender } = render(<ScoreBar value={3.5} scale={scale} />);
    const narrow = screen.getByTestId("score-bar-fill").style.transform;

    rerender(<ScoreBar value={5.13} scale={scale} />);
    const wide = screen.getByTestId("score-bar-fill").style.transform;

    expect(narrow).not.toBe(wide);
    expect(wide).toBe("scaleX(1)");
    expect(narrow).toBe(`scaleX(${(3.5 / scale).toFixed(4)})`);
  });

  it("the largest score on screen paints full width", () => {
    render(<ScoreBar value={2} scale={4} />);
    expect(screen.getByTestId("score-bar-fill").style.transform).toBe("scaleX(0.5)");
    render(<ScoreBar value={4} scale={4} />);
    expect(screen.getAllByTestId("score-bar-fill")[1]?.style.transform).toBe("scaleX(1)");
  });

  it("negative and non-finite scores render without throwing", () => {
    expect(() => render(<ScoreBar value={-1} scale={3} />)).not.toThrow();
    expect(() => render(<ScoreBar value={Number.NaN} scale={3} />)).not.toThrow();
    expect(screen.getAllByTestId("score-bar-fill").every((node) => node.style.transform.includes("scaleX(0)"))).toBe(
      true,
    );
  });
});

describe("bumpinessBand", () => {
  it("uses absolute thresholds justified against seeded vol spread", () => {
    expect(BUMPINESS_THRESHOLDS).toEqual([0.25, 0.35, 0.45, 0.55]);
    expect(bumpinessBand(0.179)).toBe(1);
    expect(bumpinessBand(0.366)).toBe(3);
    expect(bumpinessBand(0.618)).toBe(5);
  });

  it("separates the calmest name from the median", () => {
    expect(bumpinessBand(0.179)).toBeLessThan(bumpinessBand(0.366)!);
  });
});

describe("BumpinessDots", () => {
  it("renders sr-only text for assistive tech", () => {
    render(<BumpinessDots value={0.366} />);
    expect(screen.getByText(/bumpiness 3 of 5/i)).toBeInTheDocument();
  });
});

describe("RankBadge", () => {
  it("exposes ranks 1–3 as visible numbers", () => {
    render(<RankBadge rank={1} />);
    expect(screen.getByText("1")).toBeVisible();
  });

  it("renders ranks above 3 as plain right-aligned figures", () => {
    render(<RankBadge rank={12} />);
    const badge = screen.getByText("12");
    expect(badge.className).toContain("text-right");
    expect(badge.className).not.toContain("rounded-full");
  });
});
