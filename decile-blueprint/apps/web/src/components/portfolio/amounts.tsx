"use client";

import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

import { formatMoney, formatMoneyMove } from "@/lib/portfolio/overview";

/**
 * Show/hide amounts — the first control in `PORTFOLIO_REDESIGN.md` §6.1.
 *
 * It is context rather than a prop because the toggle lives in the header and the money it hides
 * is in the hero metrics, the table, the chart axis and the inspector drawer. Threading a boolean
 * through five components is how one of them ends up still printing the number after the reader
 * asked for it to be hidden — which, on a screen someone opens in an office, is the whole point
 * of the control.
 *
 * Masking is presentation only: the values are still in the DOM's `title` and in the payload.
 * This is a shoulder-surfing courtesy, not a security boundary, and it is not sold as one.
 */

interface AmountsValue {
  readonly visible: boolean;
  readonly toggle: () => void;
}

const AmountsContext = createContext<AmountsValue>({ visible: true, toggle: () => {} });

export function AmountsProvider({
  children,
  initialVisible = true,
}: {
  children: ReactNode;
  initialVisible?: boolean;
}) {
  const [visible, setVisible] = useState(initialVisible);
  const value = useMemo<AmountsValue>(
    () => ({ visible, toggle: () => setVisible((current) => !current) }),
    [visible],
  );
  return <AmountsContext.Provider value={value}>{children}</AmountsContext.Provider>;
}

export function useAmounts(): AmountsValue {
  return useContext(AmountsContext);
}

/** A rupee figure that obeys the toggle. `—` when the ledger has no answer. */
export function Money({ value }: { value: string | number | null }) {
  const { visible } = useAmounts();
  return <>{formatMoney(value, visible)}</>;
}

/** A signed rupee delta that obeys the toggle. */
export function MoneyDelta({ value }: { value: string | number | null }) {
  const { visible } = useAmounts();
  return <>{formatMoneyMove(value, visible)}</>;
}
