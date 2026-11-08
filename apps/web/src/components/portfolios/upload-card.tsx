"use client";

import { ArrowRight, Download, Upload } from "lucide-react";
import Link from "next/link";
import { useRef, useState } from "react";

import { ErrorState } from "@/components/data/error-state";
import { ImportReport } from "@/components/portfolios/import-report";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiOrigin } from "@/lib/api/config";
import { useUploadCsv } from "@/lib/portfolios/queries";

/**
 * Step 1 of docs/08's wizard: "choose portfolio (or upload CSV, with a downloadable sample)".
 *
 * The sample link points straight at `GET /portfolios/sample-csv` rather than at a copy of the
 * file in this repository, so the file a user downloads is the file the parser is tested against.
 *
 * Nothing is imported silently, and nothing navigates away from the report. docs/08 asks for
 * unmatched symbols "prominently rather than silently dropping", and a page that jumps to the
 * rebalance the instant an upload succeeds shows that report for about a frame. So the report
 * stays, and continuing is a button the user presses.
 */
export interface UploadCardProps {
  /** Supplied to replace an existing portfolio's holdings; omitted to create a new one. */
  portfolioId?: number;
  onImported?: (portfolioId: number) => void;
}

export function UploadCard({ portfolioId, onImported }: UploadCardProps) {
  const upload = useUploadCsv();
  const [name, setName] = useState("");
  const input = useRef<HTMLInputElement>(null);

  async function submit(file: File) {
    const result = await upload.mutateAsync({
      file,
      ...(portfolioId === undefined ? {} : { portfolioId }),
      ...(name.trim() ? { name: name.trim() } : {}),
    });
    onImported?.(result.portfolio.id);
  }

  return (
    <div className="space-y-4 rounded-md border border-border p-4" data-testid="upload-card">
      <div className="flex flex-wrap items-end gap-3">
        {portfolioId === undefined ? (
          <div className="min-w-48 flex-1 space-y-1">
            <Label htmlFor="portfolio-name">Name (optional)</Label>
            <Input
              id="portfolio-name"
              value={name}
              placeholder="Taken from the file name"
              onChange={(event) => setName(event.target.value)}
            />
          </div>
        ) : null}

        <div className="space-y-1">
          <Label htmlFor="portfolio-csv">Holdings CSV</Label>
          <input
            ref={input}
            id="portfolio-csv"
            type="file"
            accept=".csv,text/csv"
            data-testid="csv-input"
            className="block text-sm file:mr-3 file:h-9 file:rounded-md file:border file:border-input file:bg-card file:px-3 file:text-sm"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void submit(file);
            }}
          />
        </div>

        <Button variant="outline" size="sm" asChild>
          <a href={`${apiOrigin()}/api/v1/portfolios/sample-csv`} data-testid="sample-csv">
            <Download aria-hidden="true" />
            Sample CSV
          </a>
        </Button>
      </div>

      <p className="text-xs text-muted-foreground">
        <Upload aria-hidden="true" className="mr-1 inline size-3" />
        One row per holding. A <code>symbol</code> column is all that is required;{" "}
        <code>quantity</code> and <code>avg_price</code> are used if present, and any other column
        is ignored. Symbols that cannot be matched are listed rather than dropped.
      </p>

      {upload.isPending ? <p className="text-sm text-muted-foreground">Reading the file…</p> : null}
      {upload.error ? <ErrorState error={upload.error} /> : null}
      {upload.data ? (
        <>
          <ImportReport report={upload.data.report} />
          {portfolioId === undefined ? (
            <Button variant="primary" size="sm" asChild>
              <Link
                href={`/portfolios/${upload.data.portfolio.id}/rebalance` as never}
                data-testid="continue-to-rebalance"
              >
                Rebalance {upload.data.portfolio.name}
                <ArrowRight aria-hidden="true" />
              </Link>
            </Button>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
