// Shape of the desk's `GET /desk/regime/data` (regime_view.build; Rust port analytics_regime.rs).

export type TierKey = "on" | "caution" | "off" | "crash";

export interface RegimeUnavailable {
  available: false;
  message?: string | null;
}

export interface Spark {
  w: number;
  h: number;
  ma_len: number;
  price: string;
  ma: string;
  last_x: number;
  last_y: number;
  above: boolean;
  sessions: number;
}

export interface MaRow {
  length: number;
  ma: number | null;
  distance_pct: number | null;
  state: string;
  confirming_closes: number | null;
  confirmation: string;
}

export interface IndexCard {
  index_name: string;
  is_sentinel: boolean;
  spark: Spark | null;
  close: number | null;
  mas: MaRow[];
  bearish_stack: boolean;
  data_date: string | null;
  stale: boolean;
  sessions: number;
}

export interface Gate {
  code: string;
  label: string;
  current: string | null;
  threshold: string;
  status: string;
  note: string;
  fired: boolean;
}

export interface BreadthBand {
  lo: number;
  hi: number;
  label: string;
  key: TierKey;
}

export interface Veto {
  active: boolean;
  dist_50: number | null;
  state_50: string;
  conf_50: string;
  dist_200: number | null;
  state_200: string;
  conf_200: string;
  below_200: boolean;
  effect: string;
  release: string;
}

export interface ProposedOrder {
  symbol: string;
  action: string;
  qty_now: number;
  delta: number;
  qty_final: number;
  ref_price?: number | null;
  value: number;
  rank: number | null;
  half_sized?: boolean;
  tax_review?: boolean;
  note?: string | null;
  weight_now: number | null;
  weight_target: number | null;
  pnl_pct: number | null;
  pnl_value: number | null;
  days_to_ltcg: number | null;
  tax_flagged: boolean | null;
  liquidity_warning: boolean;
  order_status: string | null;
  filled_qty: number | null;
}

export interface TaxLot {
  quantity: number;
  acquired_on: string | null;
  acquired_at: number | null;
  days_held: number | null;
  days_to_ltcg: number | null;
  gain: number | null;
  long_term: boolean | null;
  codes: string[];
}

export interface TaxDetail {
  symbol: string;
  requested_qty: number;
  covered_qty: number;
  uncovered_qty: number;
  estimated_gain: number | null;
  manual_review: boolean;
  codes: string[];
  lots: TaxLot[];
  short_term_qty: number;
  long_term_qty: number;
}

export interface RebalancePlan {
  plan_id: string;
  orders: number;
  submitted_value: number | null;
  filled_value: number | null;
  failed_value: number | null;
  counts: Record<string, number>;
  linked: boolean;
}

export interface IndexStatus {
  index_name: string;
  last_final_session: string | null;
  sessions: number;
  age_days: number | null;
  stale: boolean;
  gap_count: number;
}

export interface StoreStatus {
  schema_version: number;
  journal_mode: string;
  db_bytes: number | null;
  last_collection: string | null;
  last_collection_outcome: string | null;
  consecutive_failures: number | null;
  last_index_write: string | null;
  collection_healthy: boolean;
}

export interface RegimeConfig {
  ma_lengths: number[];
  buffer_bps: number;
  confirm_days: number;
  breadth_thresholds: Record<string, string>;
  health_thresholds: Record<string, string>;
  exposure_map: Record<string, number>;
  default_weights: Record<string, number>;
  recovery_breadth_min: number;
  recovery_h20_min: number;
  ltcg_review_days: number;
  weekly_evaluation_weekday: number;
  r4_residual: string;
  breadth_required: boolean;
  min_breadth_coverage_pct: number;
  index_stale_days: number;
  whipsaw: string;
  algorithm_version: string;
  config_hash: string;
}

export interface PortfolioPosition {
  symbol: string;
  quantity: number;
  average_price: number;
  price: number;
  value: number;
  pledged_qty: number;
  excluded: boolean;
  weight_pct: number | null;
  pnl_pct: number | null;
  pnl_value: number | null;
}

export interface Portfolio {
  available: boolean;
  as_of?: string;
  nav?: number;
  invested?: number;
  cash?: number;
  cash_pct?: number;
  count?: number;
  excluded_count?: number;
  excluded_value?: number;
  positions?: PortfolioPosition[];
  tradeable?: PortfolioPosition[];
  max_weight?: number;
  max_abs_pnl?: number;
  winners?: number;
  losers?: number;
  total_pnl?: number;
  pledged_count?: number;
}

export interface HistoryRow {
  week: string;
  raw: string;
  tier: string;
  cap: number;
  actual: number | null;
  trigger: string;
  committed: boolean;
  breadth: number | null;
  transition_limited: boolean;
}

export interface Timeline {
  first: string;
  last: string;
  omitted: string;
  weeks: number;
}

export interface TierRung {
  tier: string;
  name: string;
  key: TierKey;
  buys: string;
  when: string;
  does: string;
  current: boolean;
  cap: number;
}

export interface ModeSwitch {
  label: string;
  value: string;
  state: string;
  means: string;
}

export interface Banner {
  /** `warn` or `hold`. */
  level: string;
  title: string;
  detail: string;
}

export interface PlanSummary {
  tier?: string;
  sleeve_pct?: number;
  projected_equity_pct?: number;
}

export interface RegimeData {
  available: true;
  dry_run: boolean;
  mode: string | null;
  signal_session_date: string | null;
  scheduled_week_end: string | null;
  next_evaluation_date: string | null;
  last_transition_date: string | null;
  algorithm_version: string;
  config_hash: string;
  policy_tier: string;
  tier_name: string;
  tier_key: TierKey;
  tier_meaning: string;
  raw_candidate_tier: string;
  raw_candidate_name: string;
  previous_policy_tier: string | null;
  transition_limited: boolean;
  /** The desk's NewBuyMode: `full | half | blocked` — never `none`. */
  new_buys: string | null;
  new_buys_label: string | null;
  forced_action: string;
  forced_action_label: string;
  override_active: boolean;
  sentinel_veto: boolean;
  target_equity_cap_pct: number | null;
  actual_equity_pct: number | null;
  exposure_gap_pct: number | null;
  required_reduction_pct: number | null;
  current_cash_pct: number | null;
  pending_sell_pct: number | null;
  execution_status: string;
  exposure_observed_at: string | null;
  exposure_cap_mismatch: boolean;
  bar: { within_cap: number | null; excess: number | null; cash: number | null; cap_marker: number | null };
  target_position_range: [number, number] | number[];
  cluster_weights: Record<string, number>;
  cluster_cap: number;
  largest_position: { symbol: string | null; weight_sleeve: number } | null;
  allocation_feasible: boolean | null;
  plan_summary: PlanSummary;
  gates: Gate[];
  narrative: string[];
  index_cards: IndexCard[];
  health: Record<string, number | null>;
  bearish_stack_weight: number | null;
  book_weights: Record<string, number>;
  book_weight_source: string;
  book_weights_are_defaults: boolean;
  breadth_pct: number | null;
  breadth_coverage_pct: number | null;
  breadth_usable: boolean;
  breadth_band: string | null;
  breadth_band_key: TierKey | null;
  breadth_bands: BreadthBand[];
  breadth_observed: number | null;
  breadth_eligible: number | null;
  breadth_as_of: string | null;
  breadth_universe: string | null;
  breadth_universe_hash: string | null;
  min_breadth_coverage_pct: number;
  veto: Veto;
  proposed_orders: ProposedOrder[];
  proposed_sells: ProposedOrder[];
  proposed_buys: ProposedOrder[];
  guard_orders: ProposedOrder[];
  no_plan_reason: string | null;
  tax_data_incomplete: boolean;
  evaluation_id: string;
  planned_sell_value: number | null;
  planned_buy_value: number | null;
  rebalance_plan: RebalancePlan | null;
  index_status: IndexStatus[];
  store_status: StoreStatus | null;
  forced_actions_permitted: boolean;
  config: RegimeConfig;
  portfolio: Portfolio;
  holdings_now: number | null;
  full_sized: number | null;
  half_sized: unknown[];
  below_half: unknown[];
  headline_reason: string;
  tax_detail: TaxDetail[];
  recovery_confirmations: number | null;
  recovery_required: number | null;
  tier_ladder: TierRung[];
  mode_explainer: ModeSwitch[];
  /** `Current`, `Stale` or `Incomplete`. */
  data_health: string;
  history: HistoryRow[];
  timeline: Timeline | null;
  timeline_min_weeks: number;
  banners: Banner[];
  safe_state: string | null;
}

export type RegimePayload = RegimeData | RegimeUnavailable;
