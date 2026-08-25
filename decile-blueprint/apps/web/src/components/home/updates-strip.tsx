import type { UpdatePost } from "@/lib/home/types";
import { cn } from "@/lib/utils";

/**
 * The updates strip — docs/smallcase/05 §6.1's editorial slot, backed by `cb_update_post` (SC9).
 *
 * Engine posts and manager posts land in the same feed, so the source is shown rather than
 * implied: "the engine noticed this" and "a person wrote this" are different claims, and the
 * reader is entitled to know which one they are reading.
 *
 * Bodies are markdown in the database and rendered as **plain text** here. This strip shows the
 * first line of each post as a teaser; running it through a markdown renderer to produce one
 * paragraph would buy nothing and hand an authored string an HTML surface.
 */

export interface UpdatesStripProps {
  updates: readonly UpdatePost[];
  className?: string;
}

/** The first non-empty line, stripped of the markdown punctuation a teaser cannot render. */
export function teaser(bodyMd: string, limit = 140): string {
  const firstLine =
    bodyMd
      .split("\n")
      .map((line) => line.replace(/^[#>\-*\s]+/, "").trim())
      .find((line) => line.length > 0) ?? "";
  const flattened = firstLine.replace(/[*_`]/g, "");
  return flattened.length > limit ? `${flattened.slice(0, limit - 1).trimEnd()}…` : flattened;
}

export function UpdatesStrip({ updates, className }: UpdatesStripProps) {
  if (updates.length === 0) return null;

  return (
    <section
      aria-label="Updates"
      data-testid="updates-strip"
      className={cn("space-y-2", className)}
    >
      <h2 className="text-sm font-semibold">What changed</h2>
      <ul className="space-y-2">
        {updates.map((post) => (
          <li
            key={post.id}
            className="rounded-xl border border-border/70 bg-card px-4 py-3 text-sm"
          >
            <p className="font-medium">{post.title}</p>
            <p className="mt-1 text-muted-foreground">{teaser(post.body_md)}</p>
            <p className="mt-1 text-xs text-muted-foreground">
              {post.source === "ENGINE" ? "Posted by the engine" : "Posted by the manager"} ·{" "}
              {post.published_at.slice(0, 10)}
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}
