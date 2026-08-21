import type { FormResult } from "@/app/actions/auth";
import { cn } from "@/lib/utils";

/**
 * The one status line every auth form uses.
 *
 * A live region, so a failure is *announced* and not only shown (docs/08 §"Accessibility & quality
 * bar"), and it reserves its height so a message appearing does not push the button down —
 * Prompt 8's no-layout-shift criterion applied to a form.
 */
export function FormStatus({ result }: { result: { ok: boolean; message: string } | null }) {
  return (
    <p
      role="status"
      aria-live="polite"
      className={cn(
        "min-h-5 text-sm",
        result?.ok === false ? "text-negative" : "text-muted-foreground",
      )}
    >
      {result?.message ?? ""}
    </p>
  );
}

export type { FormResult };
