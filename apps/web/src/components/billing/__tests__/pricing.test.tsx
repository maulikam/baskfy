import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

/**
 * `CheckoutButton` imports the `startCheckout` server action, which imports Auth.js, which imports
 * `next/server` — none of which resolves under vitest's node environment and none of which this
 * suite is about. Stubbed at the module boundary so the *rendering* rules can be asserted; the
 * action itself is exercised end to end by `services/api/tests/test_api_billing.py` against the
 * endpoint it calls.
 */
vi.mock("@/app/actions/billing", () => ({
  startCheckout: () => Promise.resolve({ ok: false, message: "not exercised here" }),
}));

import type { InvoiceOut, PlanOut } from "@decile/api-client";

import { InvoiceTable } from "@/components/billing/invoice-table";
import { PlanCard } from "@/components/billing/plan-card";
import { billingSuffix, formatPrice, repricingNote } from "@/lib/billing/format";

/**
 * The pricing and invoice primitives.
 *
 * These assert the *rendering rules* Prompt 13 §5 and docs/11 §Compliance impose, driving them
 * with payloads shaped exactly like `GET /plans` and `GET /invoices`. What each plan actually
 * grants is the API's business — `services/api/tests/test_api_billing.py` checks that — and the
 * point here is that this app displays whatever it is told and decides nothing.
 */

function plan(overrides: Partial<PlanOut> = {}): PlanOut {
  return {
    code: "monthly",
    label: "Monthly",
    tagline: "Everything, billed every month.",
    price_inr: "500.00",
    currency: "INR",
    interval: "month",
    features: [
      { label: "Full screener across all 14 universes", entitlement: "screener" },
      { label: "CSV export of any screen", entitlement: "export_csv" },
      { label: "Community Slack access", entitlement: null },
    ],
    entitlements: {
      screener: true,
      export_csv: true,
      custom_columns: true,
      historical_ranks: true,
      backtests: true,
      api_access: false,
      max_screens: 50,
    },
    price_from_dec_2026: "899.00",
    disclosure: null,
    ...overrides,
  };
}

const ACCOUNT = { email: "asha@example.com", name: "Asha Rao" };

describe("plan card", () => {
  it("shows the price the API sent, formatted in rupees", () => {
    render(
      <PlanCard plan={plan()} featured={false} signedIn account={ACCOUNT} currentPlanCode={null} />,
    );
    expect(screen.getByText("₹500")).toBeInTheDocument();
  });

  it("lists every feature the API named, including the ones it cannot enforce", () => {
    render(
      <PlanCard plan={plan()} featured={false} signedIn account={ACCOUNT} currentPlanCode={null} />,
    );
    const card = screen.getByRole("article", { name: "Monthly" });
    expect(within(card).getByText("CSV export of any screen")).toBeInTheDocument();
    expect(within(card).getByText("Community Slack access")).toBeInTheDocument();
  });

  it("renders the point-of-sale disclosure when the plan carries one", () => {
    /** docs/11 §Compliance: the Forever plan states what "forever" means, at the point of sale. */
    const forever = plan({
      code: "forever",
      label: "Forever",
      interval: null,
      price_inr: "14999.00",
      disclosure: "Forever means the lifetime of the website.",
      price_from_dec_2026: null,
    });
    render(
      <PlanCard plan={forever} featured={false} signedIn account={ACCOUNT} currentPlanCode={null} />,
    );
    expect(screen.getByText(/lifetime of the website/)).toBeInTheDocument();
  });

  it("says nothing about a disclosure the API did not send", () => {
    render(
      <PlanCard plan={plan()} featured={false} signedIn account={ACCOUNT} currentPlanCode={null} />,
    );
    expect(screen.queryByText(/lifetime/)).toBeNull();
  });

  it("sends a signed-out visitor to sign in rather than to a button that fails", () => {
    render(
      <PlanCard
        plan={plan()}
        featured={false}
        signedIn={false}
        account={null}
        currentPlanCode={null}
      />,
    );
    const link = screen.getByRole("link", { name: /sign in to choose monthly/i });
    expect(link).toHaveAttribute("href", "/login?next=%2Fpricing");
  });

  it("does not offer to sell the plan the account is already on", () => {
    render(
      <PlanCard
        plan={plan()}
        featured={false}
        signedIn
        account={ACCOUNT}
        currentPlanCode="monthly"
      />,
    );
    expect(screen.getByRole("button", { name: /your current plan/i })).toBeDisabled();
  });
});

describe("price formatting", () => {
  it("renders whole rupees", () => {
    expect(formatPrice("3999.00")).toBe("₹3,999");
  });

  it("passes a value it cannot parse through unchanged rather than showing NaN", () => {
    expect(formatPrice("not a number")).toBe("not a number");
  });

  it.each([
    ["month", "/mo"],
    ["year", "/yr"],
    [null, "one-time"],
  ] as const)("suffixes a %s plan with %s", (interval, expected) => {
    expect(billingSuffix(interval)).toBe(expected);
  });

  it("mentions the December 2026 increase only when the API sent one", () => {
    expect(repricingNote(plan())).toContain("December 2026");
    expect(repricingNote(plan({ price_from_dec_2026: null }))).toBeNull();
  });
});

function invoice(overrides: Partial<InvoiceOut> = {}): InvoiceOut {
  return {
    invoice_number: "DCL/2026-27/000001",
    invoice_date: "2026-08-21",
    plan_code: "monthly",
    amount_inr: "500.00",
    taxable_inr: "423.73",
    cgst_inr: "38.14",
    sgst_inr: "38.13",
    igst_inr: "0.00",
    gst_inr: "76.27",
    gst_rate: "18.00",
    place_of_supply: "Karnataka (29)",
    sac_code: "998439",
    status: "captured",
    pdf_url: "/api/v1/invoices/DCL/2026-27/000001/pdf",
    ...overrides,
  };
}

describe("invoice table", () => {
  it("prints the API's exact amounts rather than re-deriving them", () => {
    /** CLAUDE.md house rule 9: money is exact. A paisa lost to a float is a manual reconciliation. */
    render(<InvoiceTable invoices={[invoice()]} />);
    expect(screen.getByText("423.73")).toBeInTheDocument();
    expect(screen.getByText("76.27")).toBeInTheDocument();
    expect(screen.getByText("500.00")).toBeInTheDocument();
  });

  it("links each row to its own PDF, named for the invoice", () => {
    render(<InvoiceTable invoices={[invoice()]} />);
    const link = screen.getByRole("link", { name: /invoice DCL\/2026-27\/000001/ });
    expect(link.getAttribute("href")).toContain("/api/v1/invoices/DCL/2026-27/000001/pdf");
  });

  it("shows an em dash rather than a zero for a figure the API did not send", () => {
    render(<InvoiceTable invoices={[invoice({ taxable_inr: null, gst_inr: null })]} />);
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });
});
