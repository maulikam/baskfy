"use client";

import { useState } from "react";

import { RebalanceDrawer } from "@/components/portfolio/rebalance/rebalance-drawer";
import { Button } from "@/components/ui/button";
import type { PortfolioDetail } from "@/lib/portfolio/overview";
import { useRebalance, useScreens } from "@/lib/portfolios/queries";

/**
 * The client seam between PC3's detail workspace and PC4's rebalance drawer.
 *
 * PC3 exposes a `rebalanceSlot`; PC4's drawer needs the screen list and the computed diff, both of
 * which are browser reads through the existing `useScreens` / `useRebalance` hooks the rebalance
 * wizard has always used. Neither leaf may import the other (`docs/PORTFOLIO-COMMAND-CENTER.md`
 * §6.1), so the join lives here, in the parent's own file.
 *
 * **Nothing here places an order.** `useRebalance` POSTs to `/portfolios/{id}/rebalance`, which
 * COMPUTES a diff and writes no order; the drawer's last step produces a plan a person takes to
 * their broker. That is the product's first non-negotiable and the brief's own instruction.
 */
export function RebalanceSlot({
  portfolioId,
  portfolioName,
  detail,
}: {
  portfolioId: number;
  portfolioName: string;
  detail: PortfolioDetail | null;
}) {
  const screens = useScreens();
  const rebalance = useRebalance();
  const [open, setOpen] = useState(false);

  return (
    <RebalanceDrawer
      portfolioName={portfolioName}
      detail={detail}
      screens={screens.data ?? []}
      rebalance={rebalance.data ?? null}
      analysing={rebalance.isPending}
      analyseError={rebalance.error ? rebalance.error.message : null}
      onAnalyse={(input) => {
        rebalance.mutate({
          portfolioId,
          screenPublicId: input.screenPublicId,
          topN: input.topN,
          holdBuffer: input.holdBuffer,
        });
      }}
      open={open}
      onOpenChange={setOpen}
      trigger={
        <Button type="button" size="sm" className="bg-brand text-brand-foreground hover:bg-brand/90">
          Review rebalance
        </Button>
      }
    />
  );
}
