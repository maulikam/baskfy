"use client";

import { useState, type MouseEvent } from "react";
import { TriangleAlert } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useSeriesOf } from "@/lib/regime-signal/series-flags";

type SeriesNote = { title: string; lines: readonly string[] };

const TRADE_FOR_TRADE = "Trade-for-trade: every buy and sell settles by delivery, so no intraday netting.";
const THIN = "Usually thin liquidity and a tighter price band; wider spreads and harder exits.";

const NOTES: Readonly<Record<string, SeriesNote>> = {
  BE: { title: "BE series", lines: [TRADE_FOR_TRADE, "NSE moves stocks here for surveillance or unusual price moves.", THIN] },
  BZ: {
    title: "BZ series",
    lines: [TRADE_FOR_TRADE, "Z category: the company has not met exchange listing requirements.", THIN],
  },
  SM: { title: "SM series (SME platform)", lines: ["Listed on NSE Emerge, the SME platform; trades in fixed lots.", THIN] },
  ST: { title: "ST series (SME, trade-for-trade)", lines: ["SME platform stock in trade-for-trade.", TRADE_FOR_TRADE, THIN] },
  SZ: {
    title: "SZ series (SME, Z category)",
    lines: ["SME platform stock in the Z category.", TRADE_FOR_TRADE, THIN],
  },
};

/** The note for a non-EQ series, or null for the normal EQ segment (and unknown/missing series). */
export function seriesNote(series: string | null | undefined): SeriesNote | null {
  const s = (series ?? "").trim().toUpperCase();
  if (s === "" || s === "EQ") return null;
  return NOTES[s] ?? { title: `${s} series`, lines: ["Not the normal equity (EQ) segment.", THIN] };
}

/** The flag for a symbol, looked up from the instrument master (any stock list). */
export function SymbolSeriesFlag({ symbol }: { symbol: string }) {
  const series = useSeriesOf(symbol);
  return series ? <SeriesFlag series={series} /> : null;
}

/** A yellow warning beside the stock name when it trades outside the normal EQ series. */
export function SeriesFlag({ series }: { series: string | null | undefined }) {
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const note = seriesNote(series);
  if (!note) return null;

  const onClick = (event: MouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
    const next = !pinned;
    setPinned(next);
    setOpen(next);
  };

  return (
    <Popover
      open={open}
      onOpenChange={(value) => {
        setOpen(value);
        if (!value) setPinned(false);
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`Flag: ${note.title}`}
          data-testid="series-flag"
          data-series={(series ?? "").toUpperCase()}
          className="ml-1.5 inline-flex items-center gap-0.5 rounded border border-yellow-500/60 bg-yellow-400/25 px-1 py-px align-middle text-[10px] font-semibold leading-none text-yellow-800 hover:bg-yellow-400/40 dark:text-yellow-300"
          onClick={onClick}
          onMouseEnter={() => setOpen(true)}
          onMouseLeave={() => {
            if (!pinned) setOpen(false);
          }}
        >
          <TriangleAlert aria-hidden="true" className="size-3" />
          {(series ?? "").trim().toUpperCase()}
        </button>
      </PopoverTrigger>
      <PopoverContent side="right" className="w-72 space-y-1.5 p-3 text-xs" onOpenAutoFocus={(e) => e.preventDefault()}>
        <p className="font-medium text-warning">{note.title}</p>
        <ul className="list-disc space-y-1 pl-4 text-muted-foreground">
          {note.lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
