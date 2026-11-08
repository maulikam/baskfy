import type { SwingConfig } from "@/lib/swing/fetch";

/**
 * A9: "first live sessions: N left · risk 0.250%". The risk in force is halved only when a
 * confirm would be real — a simulated desk shows the full figure (DECISIONS-SW SW10.5.3).
 */
export function firstLiveHeader(config: SwingConfig): string | null {
  if (config.first_live_sessions_left <= 0) return null;
  const risk = config.execution_enabled
    ? config.risk_per_trade_pct / 2
    : config.risk_per_trade_pct;
  return `first live sessions: ${config.first_live_sessions_left} left · risk ${risk.toFixed(3)}%`;
}
