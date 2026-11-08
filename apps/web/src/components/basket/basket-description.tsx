"use client";

import { useState } from "react";

/**
 * Basket thesis — full text with an honest "Read more" when it runs long.
 * Truncating mid-sentence with no ellipsis was the audit §1.10 finding.
 */

const PREVIEW_CHARS = 220;

export function BasketDescription({ markdown }: { markdown: string }) {
  const plain = markdown.replace(/[#*_`]/g, "").trim();
  const needsToggle = plain.length > PREVIEW_CHARS;
  const [open, setOpen] = useState(false);
  const shown = !needsToggle || open ? plain : `${plain.slice(0, PREVIEW_CHARS).trimEnd()}…`;

  return (
    <div className="max-w-[62ch] space-y-2 text-sm leading-relaxed text-muted-foreground">
      <p>{shown}</p>
      {needsToggle ? (
        <button
          type="button"
          className="text-accent underline-offset-4 hover:underline"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? "Show less" : "Read more"}
        </button>
      ) : null}
    </div>
  );
}
