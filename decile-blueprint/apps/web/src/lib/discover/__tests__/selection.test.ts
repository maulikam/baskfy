import { describe, expect, it } from "vitest";

import {
  MAX_COMPARE,
  MIN_COMPARE,
  SELECTION_STORAGE_KEY,
  canCompare,
  canToggle,
  compareHref,
  isFull,
  normalise,
  readSelection,
  selectionFromParams,
  selectionSummary,
  toggle,
  writeSelection,
} from "@/lib/discover/selection";

/** A `Storage` that can be told to fail, because the real one does in a private window. */
function fakeStorage(initial: Record<string, string> = {}, throws = false): Storage {
  const map = new Map(Object.entries(initial));
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    key: (index: number) => [...map.keys()][index] ?? null,
    getItem: (key: string) => {
      if (throws) throw new Error("access denied");
      return map.get(key) ?? null;
    },
    setItem: (key: string, value: string) => {
      if (throws) throw new Error("quota exceeded");
      map.set(key, value);
    },
    removeItem: (key: string) => {
      map.delete(key);
    },
  };
}

describe("toggling a selection", () => {
  it("adds in the order things were picked", () => {
    expect(toggle(toggle(["a"], "b"), "c")).toEqual(["a", "b", "c"]);
  });

  it("removes a basket that is already selected", () => {
    expect(toggle(["a", "b", "c"], "b")).toEqual(["a", "c"]);
  });

  it("ignores an add at the cap rather than evicting the oldest pick", () => {
    // Silently dropping a basket the reader chose, to make room for one they just clicked, loses
    // work. The control is disabled and explains itself instead.
    expect(toggle(["a", "b", "c"], "d")).toEqual(["a", "b", "c"]);
    expect(isFull(["a", "b", "c"])).toBe(true);
    expect(canToggle(["a", "b", "c"], "d")).toBe(false);
  });

  it("still lets a selected basket be removed when the selection is full", () => {
    expect(canToggle(["a", "b", "c"], "b")).toBe(true);
    expect(toggle(["a", "b", "c"], "b")).toEqual(["a", "c"]);
  });

  it("never holds more than the cap", () => {
    expect(MAX_COMPARE).toBe(3);
    let selection: string[] = [];
    for (const slug of ["a", "b", "c", "d", "e"]) selection = toggle(selection, slug);
    expect(selection).toHaveLength(MAX_COMPARE);
  });

  it("knows when there is enough to compare", () => {
    expect(MIN_COMPARE).toBe(2);
    expect(canCompare([])).toBe(false);
    expect(canCompare(["a"])).toBe(false);
    expect(canCompare(["a", "b"])).toBe(true);
  });

  it("reports the selection the way the sticky bar says it", () => {
    expect(selectionSummary(["a", "b"])).toBe("2 of 3 baskets selected");
    expect(selectionSummary([])).toBe("0 of 3 baskets selected");
  });
});

describe("normalising untrusted input", () => {
  it("drops non-strings, blanks and duplicates, and caps the result", () => {
    expect(normalise(["a", "", "a", 7, null, "  b  ", "c", "d"])).toEqual(["a", "b", "c"]);
  });

  it("returns an empty selection for anything that is not a list", () => {
    for (const input of [null, undefined, 42, "a", { a: 1 }]) {
      expect(normalise(input)).toEqual([]);
    }
  });
});

describe("persistence", () => {
  it("holds slugs only, so nothing stale is ever read back", () => {
    const storage = fakeStorage();
    writeSelection(storage, ["a", "b"]);
    expect(JSON.parse(storage.getItem(SELECTION_STORAGE_KEY)!)).toEqual(["a", "b"]);
  });

  it("survives a reload", () => {
    const storage = fakeStorage();
    writeSelection(storage, ["a", "b"]);
    expect(readSelection(storage)).toEqual(["a", "b"]);
  });

  it("returns an empty selection when storage is absent, unreadable or corrupt", () => {
    expect(readSelection(null)).toEqual([]);
    expect(readSelection(fakeStorage({}, true))).toEqual([]);
    expect(readSelection(fakeStorage({ [SELECTION_STORAGE_KEY]: "not json" }))).toEqual([]);
    expect(readSelection(fakeStorage({ [SELECTION_STORAGE_KEY]: '{"a":1}' }))).toEqual([]);
  });

  it("caps a selection that was tampered with in storage", () => {
    const storage = fakeStorage({ [SELECTION_STORAGE_KEY]: '["a","b","c","d","e"]' });
    expect(readSelection(storage)).toHaveLength(MAX_COMPARE);
  });

  it("does not throw when the write is refused", () => {
    expect(() => writeSelection(fakeStorage({}, true), ["a"])).not.toThrow();
  });
});

describe("the compare link", () => {
  it("carries the selection in picking order", () => {
    expect(compareHref(["liquid-momentum", "trend-stack"])).toBe(
      "/discover/compare?b=liquid-momentum&b=trend-stack",
    );
  });

  it("is still a valid destination with nothing selected", () => {
    expect(compareHref([])).toBe("/discover/compare");
  });

  it("round-trips through the URL", () => {
    const selection = ["a", "b", "c"];
    const url = new URL(`https://example.test${compareHref(selection)}`);
    expect(selectionFromParams(url.searchParams)).toEqual(selection);
  });

  it("reads a Next.js searchParams object, single value or repeated", () => {
    expect(selectionFromParams({ b: "a" })).toEqual(["a"]);
    expect(selectionFromParams({ b: ["a", "b"] })).toEqual(["a", "b"]);
    expect(selectionFromParams({})).toEqual([]);
  });

  it("caps and de-duplicates a hand-typed URL", () => {
    expect(selectionFromParams({ b: ["a", "a", "b", "c", "d"] })).toEqual(["a", "b", "c"]);
  });
});
