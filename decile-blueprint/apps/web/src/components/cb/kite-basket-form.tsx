import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * "Trade this basket in Kite" — the Zerodha Publisher hand-off.
 *
 * WHAT HAPPENS WHEN THIS IS SUBMITTED
 * -----------------------------------
 * The browser posts to `kite.zerodha.com/connect/basket`. Kite renders the basket **inside the
 * user's own Kite session**, where they review every row, change what they want, and confirm — or
 * do not. Baskfy places nothing and learns nothing about the outcome.
 *
 * That is the whole point, and it is the posture `broker_connections.BROKER_OAUTH_REVIEW` states:
 * "publish baskets; user executes in their own broker account after confirm." The desk's
 * non-negotiable #1 and law #2 are untouched — there is no order here, only a suggestion handed
 * to the person whose account it is.
 *
 * WHY A REAL `<form>` AND NOT A FETCH
 * ------------------------------------
 * Kite requires a POST, and the response is a *page for the user*, not data for us. A `fetch`
 * would be blocked by CORS and would be the wrong shape anyway: the user must land in Kite. A
 * plain form navigates, needs no JavaScript, and is what Zerodha's own documentation shows.
 *
 * `target="_blank"` so the basket opens beside Baskfy rather than replacing it — someone
 * comparing the plan against what Kite shows them should not lose the plan to do it.
 *
 * The api_key in page source is not a leak: a Publisher key is public by design and identifies
 * the app to Kite. It grants no API access — no session, no holdings, no orders.
 *
 * NOTHING IS FILTERED ON POLICY GROUNDS
 * -------------------------------------
 * The desk's untouchable-instrument list is not applied to this basket. It protects one account —
 * the desk owner's — and has no business deciding what someone else may trade in their own Kite
 * session (`docs/DECISIONS-MERGE.md` M47). `excluded` below carries malformed rows only.
 */

export interface KiteBasketItem {
  variety: string;
  tradingsymbol: string;
  exchange: string;
  transaction_type: string;
  order_type: string;
  quantity: number;
  product: string;
  readonly: boolean;
}

export interface KiteBasketFormProps {
  url: string;
  apiKey: string;
  configured: boolean;
  items: readonly KiteBasketItem[];
  /**
   * The items split into baskets Kite will accept — at most ten each, which is Kite's documented
   * limit (https://kite.trade/docs/connect/v3/publisher/). One form is rendered per batch.
   */
  batches?: readonly (readonly KiteBasketItem[])[];
  /** Symbols the API's guard refused. Named, never merely counted — see below. */
  excluded?: readonly string[];
  className?: string;
}

export function KiteBasketForm({
  url,
  apiKey,
  configured,
  items,
  batches,
  excluded = [],
  className,
}: KiteBasketFormProps) {
  /* Fall back to a single basket when the caller passes none, so an older caller keeps working —
     but respect the cap either way rather than posting something Kite refuses. */
  const groups: readonly (readonly KiteBasketItem[])[] =
    batches && batches.length > 0
      ? batches
      : items.length === 0
        ? []
        : chunk(items, MAX_BASKET_ITEMS);
  /* Not configured, or nothing survived the guard: render the reason, never a dead button. A
     button that posts an empty basket lands the user on an error page inside Kite, which reads
     as Baskfy being broken rather than as Baskfy having nothing to send. */
  if (!configured || items.length === 0) {
    return (
      <p className={cn("text-sm text-muted-foreground", className)}>
        {configured
          ? "There is nothing in this plan that can be handed to Kite."
          : "Trading hand-off is not switched on for this deployment."}
      </p>
    );
  }

  const buy = items.filter((item) => item.transaction_type === "BUY").length;
  const sell = items.length - buy;

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      {/*
        One form per basket. Kite accepts at most ten instruments per basket, and the ordinary
        momentum basket is fifteen — so two buttons is the normal case here, not an edge case.
        Batching rather than truncating: dropping five names silently is the worst option, and
        naming them while still dropping them leaves the user to place them by hand.
      */}
      {groups.map((group, index) => (
        <form
          key={group.map((item) => item.tradingsymbol).join(",")}
          action={url}
          method="POST"
          target="_blank"
          rel="noopener noreferrer"
        >
          <input type="hidden" name="api_key" value={apiKey} />
          <input type="hidden" name="data" value={JSON.stringify(group)} />
          <Button type="submit" variant={index === 0 ? "primary" : "outline"} size="sm">
            {groups.length === 1
              ? `Review ${group.length} order${group.length === 1 ? "" : "s"} in Kite`
              : `Review basket ${index + 1} of ${groups.length} (${group.length} orders)`}
          </Button>
        </form>
      ))}

      <p className="text-sm leading-relaxed text-muted-foreground">
        {buy > 0 && sell > 0
          ? `${buy} to buy, ${sell} to sell. `
          : buy > 0
            ? `${buy} to buy. `
            : `${sell} to sell. `}
        Kite opens in a new tab with the basket ready. You review and confirm it there — nothing
        is placed from this page.
        {groups.length > 1
          ? ` Kite takes ten instruments at a time, so this plan is split across ${groups.length} baskets.`
          : ""}
      </p>

      {/*
        Named, not counted. "9 of 10 orders" tells someone a row is missing and leaves them to
        guess which; a plan they cannot reconcile against what Kite shows is a plan they are right
        to distrust.

        Nothing is left out on policy grounds — this is a malformed row, not a forbidden one. No
        instrument is filtered: the account is the user's and the basket is theirs to edit or
        reject in Kite. (`docs/DECISIONS-MERGE.md` M47.)
      */}
      {excluded.length > 0 ? (
        <p className="text-sm leading-relaxed text-muted-foreground">
          Left out: <span className="font-medium text-foreground">{excluded.join(", ")}</span> —
          the plan did not give a usable quantity or side for{" "}
          {excluded.length === 1 ? "it" : "them"}.
        </p>
      ) : null}
    </div>
  );
}

/** Kite's documented ceiling: https://kite.trade/docs/connect/v3/publisher/ */
const MAX_BASKET_ITEMS = 10;

function chunk(
  items: readonly KiteBasketItem[],
  size: number,
): readonly (readonly KiteBasketItem[])[] {
  const out: KiteBasketItem[][] = [];
  for (let i = 0; i < items.length; i += size) out.push([...items.slice(i, i + size)]);
  return out;
}
