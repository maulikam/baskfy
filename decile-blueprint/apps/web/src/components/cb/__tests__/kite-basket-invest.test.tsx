import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

/* `vi.hoisted`, not a bare `const`. `vi.mock` is hoisted above every statement in the file, so a
   factory closing over `const GET = vi.fn()` reads the binding before it is initialised — the mock
   silently does not take, the component calls the *real* `browserApi()`, and the spy sits at zero
   calls while every assertion fails for a reason that has nothing to do with the component. */
const { GET } = vi.hoisted(() => ({ GET: vi.fn() }));
vi.mock("@/lib/api/browser", () => ({ browserApi: () => ({ GET }) }));

import { KiteBasketInvest } from "@/components/cb/kite-basket-invest";

/**
 * A curated basket stores **weights**, not quantities. "12% RELIANCE" cannot be sent to a broker,
 * and turning it into "buy 5 shares" needs the one thing only the reader knows: how much money.
 * So the amount is the whole of this form, and what these assert is that the number the user
 * typed is the number the server was asked about — and that a stale basket can never be posted.
 */
function basket(items: { tradingsymbol: string; quantity: number }[]) {
  return {
    data: {
      url: "https://kite.zerodha.com/connect/basket",
      api_key: "pub-key",
      configured: true,
      items: items.map((i) => ({
        variety: "regular",
        exchange: "NSE",
        transaction_type: "BUY",
        order_type: "MARKET",
        product: "CNC",
        readonly: false,
        ...i,
      })),
      batches: [],
      excluded: [],
    },
  };
}

beforeEach(() => {
  GET.mockReset();
  if (typeof Element.prototype.scrollIntoView !== "function") {
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      writable: true,
      configurable: true,
      value: () => {},
    });
  }
});

describe("the amount is the whole of the question", () => {
  it("asks the server about the amount the user actually typed", async () => {
    const user = userEvent.setup();
    GET.mockResolvedValue(basket([{ tradingsymbol: "INFY", quantity: 3 }]));
    render(<KiteBasketInvest basketSlug="momentum-scan" basketName="Momentum Scan" />);

    const amount = screen.getByLabelText(/amount to invest/i);
    await user.clear(amount);
    await user.type(amount, "250000");
    await user.click(screen.getByRole("button", { name: /work out the shares/i }));

    await waitFor(() => expect(GET).toHaveBeenCalledTimes(1));
    const options = GET.mock.calls[0]![1] as { params: unknown };
    expect(options.params).toEqual({
      path: { slug: "momentum-scan" },
      query: { amount: "250000" },
    });
  });

  it("renders the basket the server computed, not one it derived", async () => {
    /* The arithmetic is `build_invest_plan`'s — the same function the plan preview uses. Doing it
       here in floating point would produce a basket that disagreed with that preview in rupees,
       and the user would have no way to tell which was right. */
    const user = userEvent.setup();
    GET.mockResolvedValue(
      basket([
        { tradingsymbol: "INFY", quantity: 33 },
        { tradingsymbol: "TCS", quantity: 16 },
      ]),
    );
    render(<KiteBasketInvest basketSlug="s" />);
    await user.click(screen.getByRole("button", { name: /work out the shares/i }));

    await waitFor(() => expect(document.querySelector("form[method='POST']")).not.toBeNull());
    const posted = document.querySelector('input[name="data"]')?.getAttribute("value") ?? "[]";
    const sent = JSON.parse(posted) as { quantity: number }[];
    expect(sent.map((i) => i.quantity)).toEqual([33, 16]);
  });

  it("clears the old basket before asking about a new amount", async () => {
    /* The important one. Leaving the previous basket on screen while a new amount is in flight
       shows share counts belonging to the old number — and the button under them would post
       exactly those, to a real broker, for a real amount of money. */
    const user = userEvent.setup();
    GET.mockResolvedValue(basket([{ tradingsymbol: "INFY", quantity: 3 }]));
    render(<KiteBasketInvest basketSlug="s" />);
    await user.click(screen.getByRole("button", { name: /work out the shares/i }));
    await waitFor(() => expect(document.querySelector("form[method='POST']")).not.toBeNull());

    let resolve: ((value: unknown) => void) | undefined;
    GET.mockReturnValue(
      new Promise((r) => {
        resolve = r;
      }),
    );
    await user.click(screen.getByRole("button", { name: /recalculate/i }));

    // Mid-flight: no basket on screen, so nothing stale can be submitted.
    expect(document.querySelector("form[method='POST']")).toBeNull();
    resolve?.(basket([{ tradingsymbol: "INFY", quantity: 9 }]));
    await waitFor(() => expect(document.querySelector("form[method='POST']")).not.toBeNull());
  });
});

describe("it fails in words a reader can act on", () => {
  it("never asks the server about a non-positive amount", async () => {
    /* `min={1}` means the browser refuses this one itself, before any handler runs — which is the
       better outcome: native validation is localised and announced to assistive tech for free.
       So the property asserted is the one that matters (the server is not asked), not the
       mechanism. */
    const user = userEvent.setup();
    render(<KiteBasketInvest basketSlug="s" />);
    const amount = screen.getByLabelText(/amount to invest/i);
    await user.clear(amount);
    await user.type(amount, "0");
    await user.click(screen.getByRole("button", { name: /work out the shares/i }));

    expect((amount as HTMLInputElement).checkValidity()).toBe(false);
    expect(GET).not.toHaveBeenCalled();
  });

  it("still refuses in words if native validation is bypassed", async () => {
    /* The guard behind the guard. A value can reach the handler without passing constraint
       validation — a programmatic submit, a browser that does not validate — and "the button did
       nothing" is the worst way to be told no. */
    const user = userEvent.setup();
    render(<KiteBasketInvest basketSlug="s" />);
    const amount = screen.getByLabelText(/amount to invest/i);
    await user.clear(amount);

    fireEvent.submit(document.querySelector("form")!);

    await waitFor(() =>
      expect(screen.getByText(/enter an amount in rupees/i)).toBeInTheDocument(),
    );
    expect(GET).not.toHaveBeenCalled();
  });

  it("shows the API's own sentence when it has one", async () => {
    /* The API names the symbols it has no recent close for. Replacing that with a generic
       "something went wrong" would throw away the only part a reader could act on. */
    const user = userEvent.setup();
    GET.mockResolvedValue({ error: { detail: "no recent close for GLENMARK, BBOX" } });
    render(<KiteBasketInvest basketSlug="s" />);
    await user.click(screen.getByRole("button", { name: /work out the shares/i }));

    await waitFor(() =>
      expect(screen.getByText(/no recent close for GLENMARK, BBOX/)).toBeInTheDocument(),
    );
    expect(document.querySelector("form[method='POST']")).toBeNull();
  });

  it("says the amount is a ceiling, because whole shares undershoot it", async () => {
    const user = userEvent.setup();
    GET.mockResolvedValue(basket([{ tradingsymbol: "INFY", quantity: 3 }]));
    render(<KiteBasketInvest basketSlug="s" />);
    await user.click(screen.getByRole("button", { name: /work out the shares/i }));

    await waitFor(() =>
      expect(screen.getByText(/a little under your amount/i)).toBeInTheDocument(),
    );
  });
});
