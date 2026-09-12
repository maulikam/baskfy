import "server-only";

import { serverApi } from "@/lib/api/server";
import {
  type ScreenOption,
  type SymbolSet,
  pickScreenId,
} from "@/lib/overlap/overlap";
import { fetchToday as fetchVbtToday } from "@/lib/vbt/fetch";
import { fetchToday as fetchTwtToday } from "@/lib/twt/fetch";

/**
 * The three symbol sets the Build overlap page intersects.
 *
 * Volume breakout and Three weeks tight are today's published candidates / tight names.
 * Screens is whichever screen the URL names (default: the seeded example), run fresh so the
 * intersection is against the same session the sleeves are showing — not a stale prior run.
 */

export interface OverlapSources {
  vbt: SymbolSet;
  twt: SymbolSet;
  screen: SymbolSet;
  screens: readonly ScreenOption[];
  /** Session dates the sleeves reported, for the page header. */
  asOf: {
    vbt: string | null;
    twt: string | null;
    screen: string | null;
  };
}

function symbolsFrom(rows: readonly { symbol: string }[] | null | undefined): string[] | null {
  if (rows === null || rows === undefined) return null;
  return rows.map((row) => row.symbol);
}

async function listScreens(): Promise<readonly ScreenOption[]> {
  const api = await serverApi();
  const { data } = await api.GET("/api/v1/screens");
  if (!data?.data) return [];
  return data.data.map((screen) => ({
    publicId: screen.public_id,
    name: screen.name,
  }));
}

async function runScreen(publicId: string): Promise<{
  symbols: string[] | null;
  name: string;
  asOf: string | null;
}> {
  const api = await serverApi();
  const listed = await api.GET("/api/v1/screens/{public_id}", {
    params: { path: { public_id: publicId } },
  });
  if (!listed.data) {
    return { symbols: null, name: publicId, asOf: null };
  }

  const { data, error } = await api.POST("/api/v1/screens/{public_id}/run", {
    params: { path: { public_id: publicId } },
    body: {},
  });
  if (!data || error) {
    return { symbols: null, name: listed.data.name, asOf: null };
  }
  return {
    symbols: data.rows.map((row) => row.symbol),
    name: listed.data.name,
    asOf: data.as_of,
  };
}

export async function fetchOverlapSources(screenId: string): Promise<OverlapSources> {
  const [vbtToday, twtToday, screens] = await Promise.all([
    fetchVbtToday(),
    fetchTwtToday(),
    listScreens(),
  ]);
  const resolvedId = pickScreenId(screenId, screens);
  const screenRun = await runScreen(resolvedId);

  return {
    vbt: {
      key: "vbt",
      label: "Volume breakout",
      symbols: vbtToday ? symbolsFrom(vbtToday.candidates) : null,
    },
    twt: {
      key: "twt",
      label: "Three weeks tight",
      /* Every name still holding the pattern — including watch-only — because the question is
         "which names appear on both scans", not "which names cleared the liquidity floor". */
      symbols: twtToday ? symbolsFrom(twtToday.tight) : null,
    },
    screen: {
      key: resolvedId,
      label: screenRun.name,
      symbols: screenRun.symbols,
    },
    screens,
    asOf: {
      vbt: vbtToday?.as_of ?? null,
      twt: twtToday?.as_of ?? null,
      screen: screenRun.asOf,
    },
  };
}
