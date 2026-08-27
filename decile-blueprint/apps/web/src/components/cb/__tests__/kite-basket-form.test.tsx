import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { KiteBasketForm, type KiteBasketItem } from "@/components/cb/kite-basket-form";

/**
 * The hand-off is the one control on a page that never gains an order route, so what it *cannot*
 * do matters as much as what it does.
 */
const KITE_URL = "https://kite.zerodha.com/connect/basket";

function item(overrides: Partial<KiteBasketItem> = {}): KiteBasketItem {
  return {
    variety: "regular",
    tradingsymbol: "INFY",
    exchange: "NSE",
    transaction_type: "BUY",
    order_type: "MARKET",
    quantity: 10,
    product: "CNC",
    readonly: false,
    ...overrides,
  };
}

function form(): HTMLFormElement {
  const el = document.querySelector("form");
  if (!el) throw new Error("no form rendered");
  return el;
}

function forms(): HTMLFormElement[] {
  return [...document.querySelectorAll("form")];
}

function dataOf(el: HTMLFormElement): KiteBasketItem[] {
  return JSON.parse(el.querySelector('input[name="data"]')?.getAttribute("value") ?? "[]");
}

describe("the hand-off posts to Kite and nowhere else", () => {
  it("is a real form POST, because Kite answers with a page for the user", () => {
    render(<KiteBasketForm url={KITE_URL} apiKey="k" configured items={[item()]} />);
    expect(form().getAttribute("action")).toBe(KITE_URL);
    expect(form().getAttribute("method")?.toLowerCase()).toBe("post");
  });

  it("opens Kite beside Baskfy rather than replacing it", () => {
    render(<KiteBasketForm url={KITE_URL} apiKey="k" configured items={[item()]} />);
    expect(form().getAttribute("target")).toBe("_blank");
  });

  it("sends the api key and the basket, and no other field", () => {
    render(<KiteBasketForm url={KITE_URL} apiKey="pub-key" configured items={[item()]} />);
    const names = [...form().querySelectorAll("input")].map((i) => i.getAttribute("name"));
    expect(names.sort()).toEqual(["api_key", "data"]);
  });

  it("serialises the basket exactly as the API produced it", () => {
    /* The array is not re-derived in the browser: the guard that filtered it runs in Python, and
       a client that rebuilt the payload could reintroduce a row the guard removed. */
    const items = [item(), item({ tradingsymbol: "TCS", transaction_type: "SELL", quantity: 2 })];
    render(<KiteBasketForm url={KITE_URL} apiKey="k" configured items={items} />);
    const data = form().querySelector('input[name="data"]')?.getAttribute("value") ?? "";
    expect(JSON.parse(data)).toEqual(items);
  });

  it("says what the basket will do before the user commits to it", () => {
    const items = [item(), item({ tradingsymbol: "TCS", transaction_type: "SELL" })];
    render(<KiteBasketForm url={KITE_URL} apiKey="k" configured items={items} />);
    expect(screen.getByText(/1 to buy, 1 to sell/)).toBeInTheDocument();
    expect(screen.getByText(/nothing is placed from this page/i)).toBeInTheDocument();
  });
});

describe("it refuses to render a button that cannot work", () => {
  it("explains itself when the deployment has no publisher key", () => {
    render(<KiteBasketForm url={KITE_URL} apiKey="" configured={false} items={[item()]} />);
    expect(document.querySelector("form")).toBeNull();
    expect(screen.getByText(/not switched on/i)).toBeInTheDocument();
  });

  it("explains itself when nothing survived the guard", () => {
    /* An empty basket posted to Kite is an error page inside Kite, which a user reads as Baskfy
       being broken rather than as Baskfy having nothing to send. */
    render(<KiteBasketForm url={KITE_URL} apiKey="k" configured items={[]} />);
    expect(document.querySelector("form")).toBeNull();
    expect(screen.getByText(/nothing in this plan/i)).toBeInTheDocument();
  });
});

describe("what was left out is named", () => {
  it("names the excluded symbols rather than counting them", () => {
    /* "9 of 10" leaves the reader to guess which one is missing, and a plan they cannot reconcile
       against what Kite shows is a plan they are right to distrust.

       These are malformed rows, not forbidden ones: no instrument is filtered on policy grounds,
       because the account is the user's (`docs/DECISIONS-MERGE.md` M47). */
    render(
      <KiteBasketForm
        url={KITE_URL}
        apiKey="k"
        configured
        items={[item()]}
        excluded={["ACME", "ZZTOP"]}
      />,
    );
    expect(screen.getByText(/ACME, ZZTOP/)).toBeInTheDocument();
    expect(screen.getByText(/usable quantity or side/i)).toBeInTheDocument();
  });

  it("says nothing when nothing was left out", () => {
    render(<KiteBasketForm url={KITE_URL} apiKey="k" configured items={[item()]} />);
    expect(screen.queryByText(/Left out/i)).toBeNull();
  });
});


describe("Kite takes ten instruments at a time", () => {
  /*
   * https://kite.trade/docs/connect/v3/publisher/ — "You can add one or more stocks to the basket
   * (maximum 10)". `DEFAULT_SCAN_TOP_N` is 15, so the *ordinary* momentum basket is half again
   * the limit and a rebalance carries sells too. Posting fifteen rows would have been a broken
   * hand-off on the normal path, every time — which is why this is batched, not truncated.
   */
  const fifteen = Array.from({ length: 15 }, (_, i) => item({ tradingsymbol: `SYM${i}` }));

  it("splits a fifteen-name plan into two baskets rather than posting one Kite refuses", () => {
    render(<KiteBasketForm url={KITE_URL} apiKey="k" configured items={fifteen} />);
    expect(forms()).toHaveLength(2);
    expect(dataOf(forms()[0]!)).toHaveLength(10);
    expect(dataOf(forms()[1]!)).toHaveLength(5);
  });

  it("loses nothing in the split, and keeps the order the desk decided", () => {
    render(<KiteBasketForm url={KITE_URL} apiKey="k" configured items={fifteen} />);
    const sent = forms().flatMap(dataOf).map((i) => i.tradingsymbol);
    expect(sent).toEqual(fifteen.map((i) => i.tradingsymbol));
  });

  it("stays one button when the plan fits", () => {
    render(<KiteBasketForm url={KITE_URL} apiKey="k" configured items={fifteen.slice(0, 10)} />);
    expect(forms()).toHaveLength(1);
    expect(screen.getByRole("button", { name: /Review 10 orders in Kite/ })).toBeInTheDocument();
  });

  it("says why there is more than one button", () => {
    render(<KiteBasketForm url={KITE_URL} apiKey="k" configured items={fifteen} />);
    expect(screen.getByText(/ten instruments at a time/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /basket 1 of 2/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /basket 2 of 2/i })).toBeInTheDocument();
  });

  it("uses the batches the API computed when it sends them", () => {
    /* The cap lives in Python too; if the two ever disagree the server's answer is the one that
       matched what the guard actually let through. */
    render(
      <KiteBasketForm
        url={KITE_URL}
        apiKey="k"
        configured
        items={fifteen}
        batches={[fifteen.slice(0, 3), fifteen.slice(3)]}
      />,
    );
    expect(forms()).toHaveLength(2);
    expect(dataOf(forms()[0]!)).toHaveLength(3);
  });
});
