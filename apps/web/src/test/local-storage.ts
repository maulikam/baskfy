/**
 * A working `window.localStorage` for tests that need one.
 *
 * The jsdom environment this suite runs in exposes `window.localStorage` as a **plain empty
 * object** — `Object.keys()` is `[]` and `setItem` is `undefined` — rather than as a `Storage`.
 * Nothing in the app noticed until now, because until the ⌘K palette's recent items nothing in the
 * app used it (the announcement banner deliberately chose a cookie, and says so in its own
 * comment). A test that assumed the real thing failed with `setItem is not a function`, which
 * looks like a bug in the code under test and is not.
 *
 * So: an in-memory `Storage` installed per test file, spy-able like the real one.
 */

export interface TestStorage extends Storage {
  /** Everything currently held, for an assertion that would otherwise need `getItem` twice. */
  readonly entries: ReadonlyMap<string, string>;
}

export function installLocalStorage(): TestStorage {
  const entries = new Map<string, string>();
  const storage: TestStorage = {
    entries,
    get length() {
      return entries.size;
    },
    key: (index: number) => [...entries.keys()][index] ?? null,
    getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => void entries.set(key, String(value)),
    removeItem: (key: string) => void entries.delete(key),
    clear: () => entries.clear(),
  };
  Object.defineProperty(window, "localStorage", {
    value: storage,
    configurable: true,
    writable: true,
  });
  return storage;
}
