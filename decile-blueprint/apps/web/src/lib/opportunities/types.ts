export type DataStatus = "FRESH" | "STALE" | "UNAVAILABLE";
export type SwingSetupType = "BREAKOUT" | "PULLBACK" | "RELATIVE_STRENGTH";
export type TriggerStatus = "PENDING" | "TRIGGERED";
export type OptionDirection = "BULLISH" | "BEARISH";
export type OptionStrategy = "LONG_CALL" | "LONG_PUT" | "BULL_CALL_SPREAD" | "BEAR_PUT_SPREAD";

export interface SlugName {
  slug: string;
  name: string;
}

export interface SwingScanRequest {
  universe: string;
  sectors: string[];
  setups: string[];
  min_turnover_cr: number;
  min_price: number;
  horizon_min: number;
  horizon_max: number;
  atr_pct_min: number;
  atr_pct_max: number;
  min_rr: number;
  limit: number;
}

export interface QuoteRules {
  max_spread_pct: number;
  max_quote_age_s: number;
  min_oi: number;
  min_volume: number;
}

export interface ProviderStatus {
  available: boolean;
  reason: string | null;
  dependency: string | null;
}

export interface OpportunitiesMeta {
  universes: SlugName[];
  sectors: SlugName[];
  setups: string[];
  strategies: string[];
  swing_defaults: SwingScanRequest;
  quote_rules_defaults: QuoteRules;
  options_provider: ProviderStatus;
  limits: { max_results: number; max_universe: number };
}

export interface ScoreComponent {
  key: string;
  label: string;
  value: number;
  weight: number;
  contribution: number;
}

export interface Score {
  total: number;
  components: ScoreComponent[];
}

export interface Evidence {
  key: string;
  label: string;
  value: number | null;
  threshold: number | null;
  unit: string;
  passed: boolean;
}

export interface SwingOpportunity {
  id: string;
  instrument_id: number;
  symbol: string;
  name: string;
  exchange: "NSE";
  sector: string | null;
  setup: SwingSetupType;
  status: TriggerStatus;
  actionable: boolean;
  close: number;
  evidence: Evidence[];
  trigger: {
    condition: string;
    level: number;
    status: TriggerStatus;
    triggered_on: string | null;
  };
  entry: { basis: string; price: number };
  invalidation: number;
  targets: { label: string; price: number; r_multiple: number }[];
  risk_per_share: number;
  reward_per_share: number;
  risk_reward: number;
  horizon: { min_sessions: number; max_sessions: number };
  liquidity: { avg_turnover_cr_20d: number; avg_volume_20d: number };
  volatility: { atr_14: number; atr_pct: number; hv_20_pct: number | null };
  relative_strength: { benchmark: string; excess_return_pct: number | null; lookback: number };
  score: Score;
  data_as_of: string;
  scanned_at: string;
  limitations: string[];
}

export interface SwingCounts {
  universe: number;
  evaluated: number;
  insufficient_history: number;
  qualified: number;
  pending: number;
  triggered: number;
}

export interface SwingScanResponse {
  scan_id: string;
  scanned_at: string;
  data_as_of: string | null;
  data_status: DataStatus;
  stale_reason: string | null;
  universe: string;
  counts: SwingCounts;
  warnings: string[];
  opportunities: SwingOpportunity[];
}

export interface ScanProgress {
  stage: string;
  done: number;
  total: number;
}

export interface ProblemBody {
  title: string;
  detail: string;
  status: number;
}

export interface ValidateRequest {
  setup: string;
  universe: string;
  start: string;
  end: string;
  horizon_max: number;
  slippage_bps: number;
}

export interface ValidateResponse {
  available: boolean;
  reason: string | null;
  setup: string;
  period: { start: string; end: string };
  assumptions: string[];
  trades: number;
  wins: number;
  hit_rate_pct: number | null;
  avg_return_pct: number | null;
  avg_r: number | null;
  expectancy_r: number | null;
  max_adverse_r: number | null;
  by_year: { year: number; trades: number; avg_r: number | null }[];
}

export interface OptionsAnalyzeRequest {
  symbol: string;
  direction: OptionDirection;
  strategies: string[];
  max_loss_inr: number | null;
  min_dte: number;
  max_dte: number;
  quote_rules: QuoteRules;
}

export interface OptionLeg {
  tradingsymbol: string;
  strike: number;
  option_type: "CE" | "PE";
  side: "BUY" | "SELL";
  bid: number;
  ask: number;
  exec_price: number;
  quote_age_s: number;
  spread_pct: number;
  volume: number;
  oi: number;
  iv: number | null;
  delta: number | null;
  greeks_source: string | null;
}

export interface PayoffPoint {
  underlying: number;
  pnl_per_lot: number;
}

export interface OptionCandidate {
  id: string;
  strategy: string;
  expiry: string;
  dte: number;
  lot_size: number;
  lots: number;
  actionable: boolean;
  legs: OptionLeg[];
  net_debit_per_unit: number;
  net_debit_per_lot: number;
  max_loss_per_unit: number;
  max_loss_per_lot: number;
  max_profit_per_unit: number | null;
  max_profit_per_lot: number | null;
  breakevens: number[];
  costs: { entry_inr: number; round_trip_inr: number };
  max_loss_after_costs_per_lot: number;
  payoff: PayoffPoint[];
  score: Score;
  limitations: string[];
}

export interface OptionsAnalysis {
  provider: ProviderStatus;
  underlying: { symbol: string; spot: number | null; spot_as_of: string | null };
  analyzed_at: string;
  data_status: DataStatus;
  candidates: OptionCandidate[];
  rejected: { tradingsymbol: string; reason: string }[];
  warnings: string[];
  model_note: string;
}
