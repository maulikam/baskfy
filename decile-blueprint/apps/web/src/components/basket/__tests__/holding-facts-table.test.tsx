import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BasketDetail } from "@/components/basket/basket-detail";
import { WeightMethodControls } from "@/components/basket/weight-method-controls";
import { EMPTY_FACTS, type HoldingFacts } from "@/lib/basket/holding-facts";
import type { MaterializedBasket } from "@/lib/basket/materialize";

const FACTS: HoldingFacts = {
  ...EMPTY_FACTS,
  marketcapCr: 1_200,
  ret12m: 18.4,
  vol12m: 0.31,
  beta12m: 1.12,
  medianVol12m: 25_000_000,
};

function basket(facts: HoldingFacts = FACTS): MaterializedBasket {
  return {
    name: "Cut",
    thesis: "Equal-weight basket from your screen rules.",
    asOf: "2026-08-18",
    cashPct: 5,
    notional: 100_000,
    minInvestment: 10_000,
    source: "preview",
    holdings: [
      {
        rank: 1,
        symbol: "AAA",
        name: "Aaa Ltd",
        weight: 0.475,
        price: 100,
        amount: 47_500,
        facts,
      },
    ],
    deployed: 47_500,
    cash: 52_500,
    profile: "BALANCED",
    underfunded: false,
    method: "EQUAL",
  };
}

describe("BasketDetail facts", () => {
  it("adds market cap, return, bumpiness, beta and liquidity when the rows carry them", () => {
    render(<BasketDetail basket={basket()} amount={100_000} />);
    expect(screen.getByRole("columnheader", { name: "Market cap" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "1-yr return" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Bumpiness" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Beta" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Liquidity" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Sector" })).not.toBeInTheDocument();
    expect(screen.getByText("1,200 cr")).toBeInTheDocument();
  });

  it("hides a fact that no holding has, rather than drawing a column of dashes", () => {
    render(<BasketDetail basket={basket(EMPTY_FACTS)} amount={100_000} />);
    expect(screen.queryByRole("columnheader", { name: "Market cap" })).not.toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Weight" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Amount" })).toBeInTheDocument();
  });
});

describe("WeightMethodControls custom table", () => {
  it("puts the same facts next to the weight the investor types", () => {
    const onChange = vi.fn();
    render(
      <WeightMethodControls
        method="CUSTOM"
        onMethodChange={vi.fn()}
        rows={[{ symbol: "AAA", name: "Aaa Ltd", facts: FACTS }]}
        customWeights={{ AAA: 8.33 }}
        onCustomWeightsChange={onChange}
      />,
    );

    expect(screen.getByTestId("custom-weights")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Market cap" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Bumpiness" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Liquidity" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Your weight" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Sector" })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("AAA weight"), { target: { value: "20" } });
    expect(onChange).toHaveBeenCalledWith({ AAA: 20 });
  });
});
