import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import { serverFetchJson } from "@/lib/api/server-fetch";
import { auth } from "@/lib/auth";

import type { WatchBoard } from "@/lib/watchlist/board-types";

/** `GET /watchlist/board` — read on the server; null when it cannot be read. */
export async function fetchWatchBoard(): Promise<WatchBoard | null> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token) return null;
  try {
    return (await serverFetchJson({
      url: `${serverApiOrigin()}/api/v1/watchlist/board`,
      headers: { Authorization: `Bearer ${token}` },
      timeoutMs: 6000,
    })) as WatchBoard;
  } catch {
    return null;
  }
}
