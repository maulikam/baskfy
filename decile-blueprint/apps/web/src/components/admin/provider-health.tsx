import type { ProviderHealthOut } from "@decile/api-client";

import { cn } from "@/lib/utils";

/**
 * `providers doctor` (Prompt 2 deliverable 6), rendered — PROMPTS.md Prompt 17 §4's
 * "provider health".
 *
 * The API answers this without making a network call: availability is derivable from
 * configuration alone, which is what lets the doctor work with no credentials and is what makes
 * this safe to render on a page load rather than behind a button.
 *
 * "serves now" and "can serve" are different columns because the gap between them is the
 * diagnosis. An adapter that *can* serve `bars` and currently serves nothing is a credential or a
 * token problem — `docs/runbooks/kite-token-expired.md`.
 */
export function ProviderHealth({ providers }: { providers: ProviderHealthOut[] }) {
  return (
    <div className="overflow-x-auto rounded border border-border">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">Provider availability and capability coverage</caption>
        <thead>
          <tr className="border-b border-border bg-muted/40 text-left">
            <th scope="col" className="px-3 py-2 font-medium">Provider</th>
            <th scope="col" className="px-3 py-2 font-medium">State</th>
            <th scope="col" className="px-3 py-2 font-medium">Serves now</th>
            <th scope="col" className="px-3 py-2 font-medium">Can serve</th>
          </tr>
        </thead>
        <tbody>
          {providers.map((provider) => (
            <tr key={provider.name} className="border-b border-border align-top last:border-0">
              <td className="px-3 py-2 font-mono">{provider.name}</td>
              <td className="px-3 py-2">
                <span
                  className={cn(
                    "inline-flex rounded px-1.5 py-0.5 font-mono text-xs",
                    provider.available
                      ? "bg-positive/10 text-positive"
                      : "bg-negative/10 text-negative",
                  )}
                >
                  {provider.available ? "ok" : "down"}
                </span>
                {provider.detail ? (
                  <p className="mt-1 max-w-prose text-xs text-muted-foreground">{provider.detail}</p>
                ) : null}
              </td>
              <td className="px-3 py-2 font-mono text-xs">
                {provider.serves_now.length ? provider.serves_now.join(", ") : "—"}
              </td>
              <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                {provider.can_serve.length ? provider.can_serve.join(", ") : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
