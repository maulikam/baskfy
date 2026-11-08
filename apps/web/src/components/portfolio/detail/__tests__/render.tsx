import { render, type RenderResult } from "@testing-library/react";
import { StrictMode, type ReactElement } from "react";

import { TooltipProvider } from "@/components/ui/tooltip";

/**
 * Render inside the provider the app's own layout supplies.
 *
 * `app/providers.tsx` wraps the whole tree in a `TooltipProvider`, so a component that uses a
 * tooltip works in the app and would throw in a bare `render`. Reproducing the provider here
 * rather than removing the tooltips keeps the test rendering the component the app renders.
 */
export function renderWorkspace(ui: ReactElement): RenderResult {
  return render(<TooltipProvider>{ui}</TooltipProvider>);
}

/**
 * The same render, inside `StrictMode`, which is how Next runs the app in development.
 *
 * StrictMode double-invokes every state updater precisely to surface an updater with a side
 * effect in it, and this leaf shipped one: `toggleSort` called `setDirection` from inside a
 * `setSortKey` updater, so the direction flipped twice per click and the header looked dead in
 * development while working in production. A test that renders without StrictMode cannot see
 * that, which is the reason this helper exists.
 */
export function renderStrict(ui: ReactElement): RenderResult {
  return render(
    <StrictMode>
      <TooltipProvider>{ui}</TooltipProvider>
    </StrictMode>,
  );
}
