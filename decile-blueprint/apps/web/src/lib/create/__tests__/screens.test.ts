import type { ScreenOut } from "@baskfy/api-client";
import { describe, expect, it } from "vitest";

import { groupScreens, pickDefaultScreenId } from "@/lib/create/screens";
import { defaultDefinition } from "@/lib/screens/defaults";

function screen(
  publicId: string,
  name: string,
  isExample: boolean,
): ScreenOut {
  return {
    public_id: publicId,
    name,
    is_example: isExample,
    editable: !isExample,
    columns: ["symbol", "name", "sorting_factor", "close_raw"],
    created_at: "2026-08-18T00:00:00Z",
    updated_at: "2026-08-18T00:00:00Z",
    definition: defaultDefinition() as ScreenOut["definition"],
  };
}

const TEMPLATES = [
  screen("exmpl0000001", "Investing 001", true),
  screen("exmpl0000002", "Trend Stack", true),
];

const MINE = [screen("usr000000001", "My momentum", false)];

describe("groupScreens", () => {
  it("keeps templates and the investor's own screens in separate lists", () => {
    const grouped = groupScreens([...TEMPLATES, ...MINE]);
    expect(grouped.examples.map((s) => s.public_id)).toEqual([
      "exmpl0000001",
      "exmpl0000002",
    ]);
    expect(grouped.mine.map((s) => s.public_id)).toEqual(["usr000000001"]);
  });

  it("is empty on both sides when nothing has been seeded", () => {
    expect(groupScreens([])).toEqual({ examples: [], mine: [] });
  });
});

describe("pickDefaultScreenId", () => {
  it("opens on a template for a first login that has not saved a screen", () => {
    expect(pickDefaultScreenId(TEMPLATES)).toBe("exmpl0000001");
  });

  it("honours ?screen= when that id is in the list", () => {
    expect(pickDefaultScreenId([...TEMPLATES, ...MINE], "usr000000001")).toBe(
      "usr000000001",
    );
  });

  it("ignores a ?screen= that the caller cannot see", () => {
    expect(pickDefaultScreenId(TEMPLATES, "someone-elses")).toBe("exmpl0000001");
  });

  it("falls through to the investor's own screen when there are no templates", () => {
    expect(pickDefaultScreenId(MINE)).toBe("usr000000001");
  });

  it("is null only when there is nothing to pick", () => {
    expect(pickDefaultScreenId([])).toBeNull();
  });
});
