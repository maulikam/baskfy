import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import { serverFetchJson, ServerFetchStatusError } from "@/lib/api/server-fetch";
import { auth } from "@/lib/auth";

export interface InstrumentWatchItem {
  symbol: string;
  name: string;
  watched_at: string;
  close_at_watch: string | null;
  last_close: string | null;
  moved_pct: string | null;
}

export interface InstrumentWatchlist {
  items: InstrumentWatchItem[];
  count: number;
}

const EMPTY: InstrumentWatchlist = { items: [], count: 0 };

export async function fetchInstrumentWatchlist(): Promise<InstrumentWatchlist> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token) return EMPTY;
  try {
    return (await serverFetchJson({
      url: `${serverApiOrigin()}/api/v1/watchlist/instruments`,
      headers: { Authorization: `Bearer ${token}` },
    })) as InstrumentWatchlist;
  } catch (error) {
    if (error instanceof ServerFetchStatusError && error.status === 404) return EMPTY;
    return EMPTY;
  }
}
