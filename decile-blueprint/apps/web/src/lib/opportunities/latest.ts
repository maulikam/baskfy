export interface LatestTicket {
  id: number;
  signal: AbortSignal;
}

export interface LatestGuard {
  begin(): LatestTicket;
  isCurrent(id: number): boolean;
  cancel(): void;
}

export function createLatestGuard(): LatestGuard {
  let seq = 0;
  let controller: AbortController | null = null;
  return {
    begin() {
      controller?.abort();
      controller = new AbortController();
      seq += 1;
      return { id: seq, signal: controller.signal };
    },
    isCurrent(id: number) {
      return id === seq;
    },
    cancel() {
      controller?.abort();
      controller = null;
      seq += 1;
    },
  };
}

export async function runLatest<T>(
  guard: LatestGuard,
  task: (signal: AbortSignal) => Promise<T>,
  apply: (value: T) => void,
): Promise<boolean> {
  const ticket = guard.begin();
  const value = await task(ticket.signal);
  if (!guard.isCurrent(ticket.id)) return false;
  apply(value);
  return true;
}
