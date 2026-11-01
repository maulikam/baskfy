import { describe, expect, it } from "vitest";

import { createLatestGuard, runLatest } from "@/lib/opportunities/latest";

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

describe("stale-response guard", () => {
  it("ignores an older request that resolves after a newer one", async () => {
    const guard = createLatestGuard();
    const applied: string[] = [];
    const first = deferred<string>();
    const second = deferred<string>();

    const older = runLatest(guard, () => first.promise, (v) => applied.push(v));
    const newer = runLatest(guard, () => second.promise, (v) => applied.push(v));

    second.resolve("new");
    expect(await newer).toBe(true);
    first.resolve("old");
    expect(await older).toBe(false);
    expect(applied).toEqual(["new"]);
  });

  it("aborts the superseded request's signal", () => {
    const guard = createLatestGuard();
    const a = guard.begin();
    const b = guard.begin();
    expect(a.signal.aborted).toBe(true);
    expect(b.signal.aborted).toBe(false);
    expect(guard.isCurrent(a.id)).toBe(false);
    expect(guard.isCurrent(b.id)).toBe(true);
  });

  it("cancel aborts in flight and makes its late answer stale", async () => {
    const guard = createLatestGuard();
    const pending = deferred<number>();
    const applied: number[] = [];
    let seen: AbortSignal | null = null;
    const task = runLatest(
      guard,
      (signal) => {
        seen = signal;
        return pending.promise;
      },
      (v) => applied.push(v),
    );
    guard.cancel();
    expect((seen as AbortSignal | null)?.aborted).toBe(true);
    pending.resolve(1);
    expect(await task).toBe(false);
    expect(applied).toEqual([]);
  });
});
